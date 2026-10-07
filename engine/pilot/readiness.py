"""The data readiness report: a dataset build, read back for a client analyst (Plan E M60, DEC-907).

After the client's tables are uploaded and a dataset is built, this report says - in the client's
words, not ours - what arrived, how well it links up, whether there is enough history and enough
examples of the outcome to learn from, which personal details were found and masked, and what
blocks the pilot, with the fix. It ends in one traffic light: *Ready*, *Ready with warnings* or
*Not ready: here is why*.

It computes nothing the build did not already record. Every number comes from an artefact the
build wrote or a record the upload made:

* `datasets/<id>/build_status.json` - which client and recipe, and whether the build finished;
* `datasets/<id>/build_report.json` - the checks, rows per source, join coverage, positives per
  snapshot and the features (written even when the build stopped on an error);
* `datasets/<id>/dataset_manifest.json` - rows, customers and snapshot dates, when it finished;
* the recipe, its mappings and the sources in the client store, and each source's `profile.json` -
  the file names, which column is the event date, the dates it covers, the personal details found.

The verdict is the build's own: *Not ready* exactly when the build report has an unacknowledged
error (`BuildReport.passed` is false) or the build failed before writing one; *Ready with warnings*
when it passed with warnings; *Ready* otherwise. The report explains the verdict; it never overrides
it (Plan E §3: no change to checks).

**Planning the test (Plan J M93).** Three sections help plan the pilot rather than judge the data,
so none of them changes the verdict:

* *The outcome, checked* - the outcome definition in words (window, grace period, tables that leave
  a customer out), the share of customers with the outcome per month of prediction dates, how many
  there are, the future-data check's verdict, and `LABEL_RATE_UNSTABLE` when that share jumps from
  one month to the next (the month table's own sums) by more than `configs/pilot/readiness.yaml`
  allows.
* *Can we measure it?* - the smallest change a test is sure to see at a 3, 5, 10 and 15% control
  group (`engine.measurement.planner`), from the customers at the latest prediction date and the
  dataset's own base rate, which the report says it took from there.
* *Past campaigns* - only when a treatment column is named: whether past campaigns chose their
  customers at random (`engine.uplift.checks.treatment_history`, `TREATMENT_HISTORY_NOT_RANDOM`), and
  whether the history is enough to learn who a campaign changes now or only from the next cycle.
  This one reads the built dataset itself, and shows counts only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, NamedTuple, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from engine.pilot.document import (
    AnyBlock,
    Bullets,
    Callout,
    Heading,
    KeyValues,
    Paragraph,
    ReportDocument,
    Table,
    Verdict,
    VerdictState,
)

if TYPE_CHECKING:
    from engine.clients import ClientStore
    from engine.config import UseCaseConfig
    from engine.contracts import ValidationCheck
    from engine.measurement.planner import Direction
    from engine.onboarding.specs import (
        BuildReport,
        DatasetManifest,
        LabelSpec,
        OnboardingSpec,
        SnapshotStat,
        SourceSpec,
    )
    from engine.storage import Storage
    from engine.uplift.checks import TreatmentHistory

__all__ = [
    "READINESS_SETTINGS_FILENAME",
    "MonthRate",
    "ReadinessFacts",
    "ReadinessNotFoundError",
    "ReadinessSettings",
    "campaign_aim",
    "collect_readiness",
    "label_in_words",
    "label_rate_checks",
    "load_readiness_settings",
    "month_rates",
    "readiness_document",
]

M = TypeVar("M", bound=BaseModel)

_ENTITY_KEY: Final[str] = "entity_key"
_EVENT_TIME: Final[str] = "event_time"


class ReadinessNotFoundError(LookupError):
    """No build of that dataset exists."""


@dataclass(frozen=True)
class SourceLine:
    source_id: str
    file_name: str
    role: str
    rows: int
    date_column: str | None
    key_column: str | None
    first: date | None
    last: date | None
    coverage: float | None
    personal: tuple[tuple[str, tuple[str, ...], bool], ...]
    """(column, kinds, free_text) for each column the profile flagged."""


@dataclass(frozen=True)
class ReadinessFacts:
    """Everything the report shows, gathered from the artefacts; no number is computed here."""

    dataset_id: str
    client_name: str
    use_case_id: str
    use_case_name: str
    state: str
    status_error: str | None
    report: BuildReport | None
    manifest: DatasetManifest | None
    sources: tuple[SourceLine, ...]
    history_needed_days: int
    min_history_days: int
    horizon_days: int | None
    min_outcomes: int
    outcome_description: str
    built_at: datetime | None
    label: LabelSpec | None = None
    """The outcome definition the build used (the recipe's, else the use case's): Plan J M93."""
    treatment: TreatmentHistory | None = None
    """What the treatment column the user named says about past campaigns; None when none was named."""
    treatment_note: str | None = None
    """Why a named treatment column could not be looked at (the dataset was not built or not readable)."""


def _read(storage: Storage, key: str, model: type[M]) -> M | None:
    """The artefact at `key`, or None when it is absent or unreadable - a report shows what exists."""
    from engine.storage import StorageError

    try:
        if not storage.exists(key):
            return None
        return storage.read_model(key, model)
    except (StorageError, ValueError):  # a file that does not validate is as unreadable as a missing one
        return None


def collect_readiness(
    storage: Storage,
    store: ClientStore,
    dataset_id: str,
    *,
    root: Path | None = None,
    treatment_column: str | None = None,
) -> ReadinessFacts:
    """Read the artefacts of one dataset build. Raises `ReadinessNotFoundError` when there is none.

    With `treatment_column` (Plan J M93), the built dataset is read too and the treatment-history
    check runs on that column; the report shows only its counts and verdicts.
    """
    from engine.clients import ClientStoreError
    from engine.config import load_use_case
    from engine.onboarding.datasets import (
        DATASET_MANIFEST_FILENAME,
        DATASET_REPORT_FILENAME,
        DATASET_STATUS_FILENAME,
        dataset_key,
    )
    from engine.onboarding.specs import BuildReport, BuildStatus, DatasetManifest

    status = _read(storage, dataset_key(dataset_id, DATASET_STATUS_FILENAME), BuildStatus)
    report = _read(storage, dataset_key(dataset_id, DATASET_REPORT_FILENAME), BuildReport)
    manifest = _read(storage, dataset_key(dataset_id, DATASET_MANIFEST_FILENAME), DatasetManifest)
    if status is None and report is None:
        raise ReadinessNotFoundError(dataset_id)

    client_id = status.client_id if status is not None else (manifest.client_id if manifest else "")
    spec: OnboardingSpec | None = None
    spec_id = status.spec_id if status is not None else (manifest.spec_id if manifest else None)
    try:
        spec = store.get_spec(spec_id) if spec_id else None
    except ClientStoreError:
        spec = None
    try:
        client_name = store.get_client(client_id).name if client_id else ""
    except ClientStoreError:
        client_name = client_id
    use_case_id = spec.use_case if spec is not None else (manifest.use_case if manifest else "")
    config = load_use_case(use_case_id, root) if use_case_id else None

    sources = _source_lines(storage, store, spec, report) if spec is not None else ()
    snapshots = spec.snapshot_spec if spec is not None else (config.onboarding.snapshots if config else None)
    label = (
        spec.label_spec
        if spec is not None and spec.label_spec is not None
        else (config.label if config else None)
    )
    horizon = label.window_days if label is not None else None
    min_history = snapshots.min_history_days if snapshots is not None else 0
    treatment: TreatmentHistory | None = None
    treatment_note: str | None = None
    if treatment_column:
        treatment, treatment_note = _treatment_history(
            storage, dataset_id, manifest, config, treatment_column=treatment_column
        )
    return ReadinessFacts(
        dataset_id=dataset_id,
        client_name=client_name,
        use_case_id=use_case_id,
        use_case_name=config.name if config is not None else use_case_id,
        state=(
            status.state.value if status is not None else ("done" if report and report.passed else "failed")
        ),
        status_error=status.error if status is not None else None,
        report=report,
        manifest=manifest,
        sources=sources,
        history_needed_days=min_history + (horizon or 0),
        min_history_days=min_history,
        horizon_days=horizon,
        min_outcomes=config.validation.min_positive if config is not None else 0,
        outcome_description=(label.description if label is not None and label.description else ""),
        built_at=report.built_at if report is not None else None,
        label=label,
        treatment=treatment,
        treatment_note=treatment_note,
    )


def _treatment_history(
    storage: Storage,
    dataset_id: str,
    manifest: DatasetManifest | None,
    config: UseCaseConfig | None,
    *,
    treatment_column: str,
) -> tuple[TreatmentHistory | None, str | None]:
    """`engine.uplift.checks.treatment_history` on the built dataset, or the reason it cannot run."""
    import io

    import pandas as pd

    from engine.onboarding.datasets import DATASET_FRAME_FILENAME, dataset_key
    from engine.storage import StorageError
    from engine.uplift.checks import treatment_history

    if manifest is None or config is None:
        return None, (
            f"Past campaigns were not looked at: the dataset was not built, so the column "
            f"'{treatment_column}' cannot be read yet."
        )
    try:
        frame = pd.read_parquet(
            io.BytesIO(storage.read_bytes(dataset_key(dataset_id, DATASET_FRAME_FILENAME)))
        )
    except (StorageError, OSError, ValueError):
        return None, "Past campaigns were not looked at: the built dataset could not be read."
    primary_key: str | list[str] = (
        manifest.primary_key[0] if len(manifest.primary_key) == 1 else list(manifest.primary_key)
    )
    return (
        treatment_history(
            frame,
            config,
            treatment_column=treatment_column,
            primary_key=primary_key,
            target=manifest.target,
        ),
        None,
    )


def _source_lines(
    storage: Storage, store: ClientStore, spec: OnboardingSpec, report: BuildReport | None
) -> tuple[SourceLine, ...]:
    from engine.clients import ClientStoreError
    from engine.onboarding.specs import SourceProfile

    coverage = {stat.source_id: stat for stat in (report.sources if report is not None else ())}
    event_time_column: dict[str, str] = {}
    key_column: dict[str, str] = {}
    for mapping_id in spec.mapping_ids:
        try:
            mapping = store.get_mapping(mapping_id)
        except ClientStoreError:
            continue
        for column in mapping.columns:
            if column.standard == _EVENT_TIME:
                event_time_column[mapping.source_id] = column.source
            if column.standard == _ENTITY_KEY:
                key_column[mapping.source_id] = column.source
    lines: list[SourceLine] = []
    for source_id in (spec.entity_source_id, *spec.event_source_ids):
        try:
            source: SourceSpec = store.get_source(source_id)
        except ClientStoreError:
            continue
        profile = _read(
            storage, f"clients/{source.client_id}/sources/{source_id}/profile.json", SourceProfile
        )
        date_column = event_time_column.get(source_id)
        first = last = None
        personal: list[tuple[str, tuple[str, ...], bool]] = []
        if profile is not None:
            candidate = next((c for c in profile.time_candidates if c.column == date_column), None)
            if candidate is not None:
                first, last = candidate.earliest, candidate.latest
            for profiled in profile.profile.columns:
                if profiled.pii_kinds:
                    personal.append((profiled.name, profiled.pii_kinds, False))
                elif profiled.free_text_pii_kinds:
                    personal.append((profiled.name, profiled.free_text_pii_kinds, True))
        stat = coverage.get(source_id)
        lines.append(
            SourceLine(
                source_id=source_id,
                file_name=source.file_name,
                role=source.role or (stat.role if stat else ""),
                rows=stat.rows if stat is not None else source.rows,
                date_column=date_column,
                key_column=key_column.get(source_id),
                first=first,
                last=last,
                coverage=stat.join_coverage if stat is not None else None,
                personal=tuple(personal),
            )
        )
    return tuple(lines)


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------
_UNFINISHED: Final[frozenset[str]] = frozenset({"pending", "running"})


def _verdict(facts: ReadinessFacts) -> VerdictState:
    report = facts.report
    if report is None and facts.state in _UNFINISHED:
        return "info"
    if report is None or not report.passed or facts.state == "failed":
        return "not_ready"
    return "warnings" if report.warning_count else "ready"


def _blocking(report: BuildReport | None) -> list[ValidationCheck]:
    from engine.contracts import Severity

    if report is None:
        return []
    return [c for c in report.checks if c.severity is Severity.ERROR and not c.acknowledged]


def _warnings(report: BuildReport | None) -> list[ValidationCheck]:
    from engine.contracts import Severity

    if report is None:
        return []
    return [
        c
        for c in report.checks
        if c.severity is Severity.WARNING or (c.severity is Severity.ERROR and c.acknowledged)
    ]


_KIND_NAMES: Final[dict[str, str]] = {
    "email": "e-mail addresses",
    "phone": "phone numbers",
    "pan": "PAN numbers",
    "aadhaar": "Aadhaar numbers",
    "name": "names",
    "address": "addresses",
    "ssn": "national ID numbers",
    "passport": "passport numbers",
}


def _kinds(kinds: tuple[str, ...]) -> str:
    names = [_KIND_NAMES.get(kind, kind) for kind in kinds]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _where(check: ValidationCheck, files: dict[str, str]) -> str:
    parts = []
    if check.source_id:
        parts.append(files.get(check.source_id, check.source_id))
    if check.column and check.column not in (_ENTITY_KEY, _EVENT_TIME):
        parts.append(f"column '{check.column}'")
    return ", ".join(parts)


def _problem(check: ValidationCheck, files: dict[str, str], root: Path | None, *, tone: str) -> Callout:
    from engine.pilot.data_request import load_wording
    from engine.pilot.help import code_help

    plain = load_wording(root).plain

    entry = code_help(check.code, root)
    where = _where(check, files)
    title = (entry.title if entry is not None else check.code) + (f" ({where})" if where else "")
    parts = [f"What we found: {plain(check.message)}"]
    if entry is not None:
        parts.append(f"What it means: {entry.meaning}")
        parts.append(f"How to fix it: {entry.fix}")
    if check.suggestion:
        parts.append(f"Exactly: {plain(check.suggestion)}")
    if check.acknowledged:
        parts.append("This was acknowledged by your team, so it does not block the pilot.")
    return Callout(title=title, text=" ".join(parts), tone=tone)  # type: ignore[arg-type]


def readiness_document(
    facts: ReadinessFacts, *, root: Path | None = None, now: datetime | None = None
) -> ReportDocument:
    """The readiness report of one dataset build."""
    from engine.utils.time import utc_now

    verdict = _verdict(facts)
    report = facts.report
    files = {line.source_id: line.file_name for line in facts.sources}
    blocking = _blocking(report)
    warnings = _warnings(report)

    if verdict == "not_ready":
        if blocking:
            first = blocking[0]
            from engine.pilot.help import code_help

            entry = code_help(first.code, root)
            headline = entry.title if entry is not None else first.code
            more = f" and {len(blocking) - 1} more problem(s)" if len(blocking) > 1 else ""
            verdict_block = Verdict(
                state=verdict,
                title=f"Not ready: {headline}{more}",
                text="The pilot cannot start until this is fixed. Each problem below says exactly what to change.",
            )
        else:
            verdict_block = Verdict(
                state=verdict,
                title="Not ready: the dataset could not be built",
                text=(facts.status_error or "The build did not finish.")
                + " Please share this report with your Minfy contact.",
            )
    elif verdict == "info":
        verdict_block = Verdict(
            state=verdict,
            title="Still building",
            text="The dataset is still being built. This report fills in once the build finishes.",
        )
    elif verdict == "warnings":
        verdict_block = Verdict(
            state=verdict,
            title="Ready with warnings",
            text=f"The data can be used for the pilot. {len(warnings)} point(s) below are worth a look.",
        )
    else:
        verdict_block = Verdict(
            state=verdict, title="Ready", text="The data can be used for the pilot as it is."
        )

    blocks: list[AnyBlock] = [verdict_block]

    if blocking:
        blocks.append(Heading(text="What blocks the pilot"))
        blocks += [_problem(check, files, root, tone="error") for check in blocking]

    blocks += [
        Heading(text="Tables received"),
        Table(
            columns=("File", "Table", "Rows", "Date column", "From", "To"),
            rows=tuple(
                (
                    line.file_name,
                    _role_title(line.role, root),
                    f"{line.rows:,}",
                    line.date_column or ("not needed" if line.role == "entity" else "not mapped"),
                    line.first.isoformat() if line.first else "",
                    line.last.isoformat() if line.last else "",
                )
                for line in facts.sources
            ),
            empty_text="The recipe for this dataset could not be found, so the tables cannot be listed.",
        ),
    ]
    linked = [line for line in facts.sources if line.coverage is not None]
    blocks.append(Heading(text="How the tables link up"))
    if linked:
        blocks += [
            Paragraph(
                text="The share of each table's rows whose customer ID was found in the customer table. "
                "Close to 100% is what we want; a low share usually means IDs were written or "
                "pseudonymised differently in that file."
            ),
            Table(
                columns=("File", "Rows linked to a known customer"),
                rows=tuple((line.file_name, f"{(line.coverage or 0.0):.0%}") for line in linked),
            ),
        ]
    else:
        blocks.append(Paragraph(text="Not measured: the build stopped before the tables were linked."))

    history_rows: list[tuple[str, str]] = []
    firsts = [line.first for line in facts.sources if line.first is not None]
    lasts = [line.last for line in facts.sources if line.last is not None]
    if firsts and lasts:
        span = (max(lasts) - min(firsts)).days
        history_rows.append(("Your data covers", f"{min(firsts)} to {max(lasts)} ({span:,} days)"))
    history_rows.append(
        (
            "Needed",
            f"{facts.history_needed_days:,} days: {facts.min_history_days:,} of history before each prediction date"
            + (f" and {facts.horizon_days:,} after it to see the outcome" if facts.horizon_days else ""),
        )
    )
    blocks += [Heading(text="History available and needed"), KeyValues(rows=tuple(history_rows))]

    blocks.append(Heading(text="Examples of the outcome"))
    if facts.outcome_description:
        from engine.pilot.data_request import load_wording

        blocks.append(
            Paragraph(text=f"The outcome learned from: {load_wording(root).plain(facts.outcome_description)}")
        )
    snapshots = report.snapshots if report is not None else ()
    if snapshots:
        kept = [s for s in snapshots if s.positives is not None and not s.censored and not s.dropped_reason]
        total = sum(s.positives or 0 for s in kept)
        blocks.append(
            Table(
                columns=("Prediction date", "Customers", "With the outcome", "Share", "Used?"),
                rows=tuple(
                    (
                        s.date.isoformat(),
                        f"{s.entities:,}",
                        f"{s.positives:,}" if s.positives is not None else "",
                        f"{s.positive_rate:.1%}" if s.positive_rate is not None else "",
                        (
                            "yes"
                            if s in kept
                            else (
                                "no: too recent to know the outcome"
                                if s.censored
                                else f"no: {s.dropped_reason or 'no outcome'}"
                            )
                        ),
                    )
                    for s in snapshots
                ),
            )
        )
        enough = total >= facts.min_outcomes
        blocks.append(
            Callout(
                title=f"{total:,} examples of the outcome across the dates used",
                text=(
                    f"The platform needs at least {facts.min_outcomes:,}. "
                    + (
                        "That is enough to learn from."
                        if enough
                        else "That is not enough yet; more history would add more."
                    )
                ),
                tone="success" if enough else "warning",
            )
        )
    else:
        blocks.append(Paragraph(text="Not measured: the build stopped before the outcome was worked out."))

    settings = load_readiness_settings(root)
    blocks += _outcome_checked(facts, files, root, settings)
    blocks += _can_we_measure(facts, settings, root)
    blocks += _past_campaigns(facts, files, root)

    personal = [
        (line.file_name, column, kinds, free, column == line.key_column)
        for line in facts.sources
        for column, kinds, free in line.personal
    ]
    blocks.append(Heading(text="Personal details found and masked"))
    if personal:
        blocks.append(
            Table(
                columns=("File", "Column", "Looks like", "What the platform did"),
                rows=tuple(
                    (
                        file,
                        column,
                        _kinds(kinds),
                        (
                            "Used only to link the tables, never learned from. If these are real contact "
                            "details, pseudonymise the IDs in the next extract."
                            if key
                            else (
                                "Contact details hidden wherever the text is shown"
                                if free
                                else "Not learned from, hidden wherever shown"
                            )
                        ),
                    )
                    for file, column, kinds, free, key in personal
                ),
                caption="Please leave personal details out of the next extract.",
            )
        )
    else:
        blocks.append(Paragraph(text="None found."))

    if warnings:
        blocks.append(Heading(text="Warnings"))
        blocks += [_problem(check, files, root, tone="warning") for check in warnings]

    if facts.manifest is not None:
        manifest = facts.manifest
        dates = manifest.snapshot_dates
        blocks += [
            Heading(text="The dataset built"),
            KeyValues(
                rows=(
                    ("Rows", f"{manifest.n_rows:,} (one per customer per prediction date)"),
                    ("Customers", f"{manifest.n_entities:,}"),
                    (
                        "Prediction dates",
                        f"{len(dates)}: {dates[0]} to {dates[-1]}" if dates else "none",
                    ),
                )
            ),
        ]
    blocks.append(
        Bullets(
            items=(
                "Every number on this page comes from the platform's record of this build.",
                "No customer-level data is shown: only counts, dates, file and column names.",
            )
        )
    )
    return ReportDocument(
        kind="readiness",
        title="Data readiness report",
        subtitle=f"{facts.client_name} - {facts.use_case_name}" if facts.client_name else facts.use_case_name,
        client_name=facts.client_name,
        generated_at=now or utc_now(),
        facts=(
            ("Client", facts.client_name or "not recorded"),
            ("Use case", facts.use_case_name),
            ("Dataset", facts.dataset_id),
            ("Built", facts.built_at.strftime("%d %b %Y, %H:%M UTC") if facts.built_at else "not finished"),
        ),
        blocks=tuple(blocks),
        footer="Terms: a prediction date is a date the platform stands at and predicts from.",
    )


def _role_title(role: str, root: Path | None) -> str:
    from engine.pilot.data_request import load_wording

    tables = load_wording(root).tables
    return tables[role].title if role in tables else role


# ---------------------------------------------------------------------------
# Plan J M93: planning the test - the outcome checked, "Can we measure it?", past campaigns
# ---------------------------------------------------------------------------
READINESS_SETTINGS_FILENAME: Final[str] = "pilot/readiness.yaml"
"""Relative to the configuration root, like `pilot/help.yaml`."""


class ReadinessSettings(BaseModel):
    """`configs/pilot/readiness.yaml`: the planning sections' settings (defaults when the file is absent)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    control_group_shares: tuple[float, ...] = Field(
        default=(0.03, 0.05, 0.10, 0.15), min_length=1, max_length=10
    )
    alpha: float = Field(default=0.05, gt=0.0, lt=1.0)
    power: float = Field(default=0.80, gt=0.0, lt=1.0)
    label_rate_tolerance: float = Field(default=0.5, gt=0.0)
    label_rate_min_z: float = Field(default=3.0, ge=0.0)

    @model_validator(mode="after")
    def _shares(self) -> ReadinessSettings:
        if any(not 0.0 < share <= 0.5 for share in self.control_group_shares):
            raise ValueError("Each control-group share must be more than 0 and at most 0.5.")
        return self


def load_readiness_settings(root: Path | None = None) -> ReadinessSettings:
    """The settings under `root` (default: the configuration root), or the defaults with no file."""
    from engine.config import config_root, load_yaml

    path = config_root(root) / READINESS_SETTINGS_FILENAME
    if not path.is_file():
        return ReadinessSettings()
    return ReadinessSettings.model_validate(load_yaml(path))


_OPS_IN_WORDS: Final[dict[str, str]] = {
    "eq": "is",
    "ne": "is not",
    "gt": "is above",
    "gte": "is at least",
    "lt": "is below",
    "lte": "is at most",
    "in": "is one of",
    "is_null": "is blank",
    "not_null": "is filled in",
}


def _table_word(role: str, root: Path | None) -> str:
    return _role_title(role, root).lower()


def label_in_words(label: LabelSpec, root: Path | None = None) -> str:
    """The outcome definition as one plain sentence: what counts, over which days, who is left out."""
    if label.type.value == "column":
        return f"A customer has the outcome when the column '{label.column}' of the customer table says so."
    table = _table_word(label.role or "", root)
    days = label.horizon_days or 0
    window = f"the {days:,} days after the prediction date"
    if label.grace_days:
        window += f", plus {label.grace_days:,} days' grace ({days + label.grace_days:,} days in all)"
    if label.type.value == "event_absence":
        sentence = f"A customer has the outcome when there is no {table} record in {window}"
    elif label.type.value == "event_presence":
        sentence = f"A customer has the outcome when there is at least one {table} record in {window}"
    else:
        which = "a" if label.any_event else "every"
        sentence = (
            f"A customer has the outcome when {which} {table} record in {window} meets the condition "
            f"'{label.expression}'"
        )
    if label.where is not None:
        value = label.where.value
        shown = (
            ""
            if value is None
            else " " + (", ".join(map(str, value)) if isinstance(value, list) else str(value))
        )
        sentence += f", counting only records whose '{label.where.column}' {_OPS_IN_WORDS[label.where.op.value]}{shown}"
    sentence += "."
    if label.exclude_roles:
        tables = [_table_word(role, root) for role in label.exclude_roles]
        listed = tables[0] if len(tables) == 1 else ", ".join(tables[:-1]) + " or " + tables[-1]
        sentence += f" A customer with any {listed} record in that time is left out altogether."
    return sentence


def _kept(snapshots: tuple[SnapshotStat, ...]) -> list[SnapshotStat]:
    """The prediction dates the dataset kept, with a known count of customers with the outcome."""
    return [
        s
        for s in snapshots
        if s.positives is not None and not s.censored and not s.dropped_reason and s.entities > 0
    ]


class MonthRate(NamedTuple):
    """One calendar month of prediction dates the dataset kept: what the month table shows."""

    month: str
    """`YYYY-MM`."""
    label: str
    """`Jan 2024`."""
    customers: int
    positives: int


def month_rates(snapshots: tuple[SnapshotStat, ...]) -> list[MonthRate]:
    """The kept prediction dates summed per calendar month, in date order (the "share per month" table)."""
    months: dict[str, MonthRate] = {}
    for s in sorted(_kept(snapshots), key=lambda s: s.date):
        key = s.date.strftime("%Y-%m")
        before = months.get(key)
        customers = s.entities + (before.customers if before is not None else 0)
        positives = (s.positives or 0) + (before.positives if before is not None else 0)
        months[key] = MonthRate(key, s.date.strftime("%b %Y"), customers, positives)
    return list(months.values())


def label_rate_checks(
    snapshots: tuple[SnapshotStat, ...], *, tolerance: float, min_z: float
) -> tuple[ValidationCheck, ...]:
    """`LABEL_RATE_UNSTABLE` for each month whose outcome share jumped from the month before.

    The kept prediction dates are summed per calendar month first (:func:`month_rates`, the same sums
    as the report's month table), so weekly snapshots are judged month over month. A jump counts when
    it is more than `tolerance` of the earlier month's share (relative) AND more than `min_z` standard
    errors of a two-proportion difference, so a small extract's noise is not called unstable.
    """
    from engine.contracts import Severity, ValidationCheck

    found: list[ValidationCheck] = []
    for before, after in pairwise(month_rates(snapshots)):
        n1, n2 = before.customers, after.customers
        r1, r2 = before.positives / n1, after.positives / n2
        change = abs(r2 - r1) / r1 if r1 > 0 else (math.inf if r2 > 0 else 0.0)
        pooled = (before.positives + after.positives) / (n1 + n2)
        spread = math.sqrt(pooled * (1.0 - pooled) * (1.0 / n1 + 1.0 / n2))
        z = abs(r2 - r1) / spread if spread > 0 else 0.0
        if change <= tolerance or z <= min_z:
            continue
        moved = "more than doubled" if not math.isfinite(change) or change > 1.0 else f"moved by {change:.0%}"
        found.append(
            ValidationCheck(
                code="LABEL_RATE_UNSTABLE",
                severity=Severity.WARNING,
                message=(
                    f"The share of customers with the outcome went from {r1:.1%} in {before.label} to "
                    f"{r2:.1%} in {after.label}: it {moved}, more than the {tolerance:.0%} allowed and "
                    "more than chance would explain."
                ),
                suggestion=(
                    "Check the outcome definition and the extract for those months: a changed process, "
                    "a new product or a gap in the data can change what the outcome means."
                ),
                details={
                    "from_date": before.month,
                    "to_date": after.month,
                    "from_rate": round(r1, 4),
                    "to_rate": round(r2, 4),
                    "relative_change": None if not math.isfinite(change) else round(change, 4),
                    "z": round(z, 2),
                    "tolerance": tolerance,
                    "min_z": min_z,
                },
            )
        )
    return tuple(found)


def _leak_verdict(report: BuildReport | None) -> str:
    if report is None:
        return "Not run: the build stopped before it."
    if any(check.code == "FUTURE_EVENTS_LEAKED" for check in report.checks):
        return (
            "Failed: records dated after a prediction date reached what the model learns from. "
            "Do not use this dataset."
        )
    check = report.leak_check
    if check is None:
        return "Not run: the build stopped before it."
    return (
        f"Passed: {check.rows_probed:,} of {check.rows_total:,} rows were built again with records dated "
        "after the prediction dates added, and nothing changed."
    )


def _outcome_checked(
    facts: ReadinessFacts, files: dict[str, str], root: Path | None, settings: ReadinessSettings
) -> list[AnyBlock]:
    """The outcome definition in words, its share per month, its count, the future-data check."""
    if facts.label is None:
        return []
    report = facts.report
    kept = _kept(report.snapshots if report is not None else ())
    rows: list[tuple[str, str]] = [("Definition", label_in_words(facts.label, root))]
    if kept:
        positives = sum(s.positives or 0 for s in kept)
        customers = sum(s.entities for s in kept)
        rows.append(
            (
                "Customers with the outcome",
                f"{positives:,} of {customers:,} across the {len(kept)} prediction date(s) used "
                f"({positives / customers:.1%})",
            )
        )
    else:
        rows.append(("Customers with the outcome", "Not measured: no prediction date was kept."))
    if facts.label.exclude_roles:
        counted = [s.excluded for s in kept]
        if kept and all(count is not None for count in counted):
            left_out = sum(count or 0 for count in counted)
            rows.append(
                (
                    "Customers left out",
                    f"{left_out:,} across the {len(kept)} prediction date(s) used, for a record in a table "
                    "that leaves a customer out",
                )
            )
        else:
            rows.append(("Customers left out", "Not recorded by this build."))
    rows.append(("Future-data check", _leak_verdict(report)))
    blocks: list[AnyBlock] = [Heading(text="The outcome, checked"), KeyValues(rows=tuple(rows))]
    if kept:
        blocks.append(
            Table(
                columns=("Month", "Customers", "With the outcome", "Share"),
                rows=tuple(
                    (m.label, f"{m.customers:,}", f"{m.positives:,}", f"{m.positives / m.customers:.1%}")
                    for m in month_rates(report.snapshots if report is not None else ())
                ),
                caption="The base rate per month of prediction dates: it should move slowly, if at all.",
            )
        )
        unstable = label_rate_checks(
            report.snapshots if report is not None else (),
            tolerance=settings.label_rate_tolerance,
            min_z=settings.label_rate_min_z,
        )
        blocks += [_problem(check, files, root, tone="warning") for check in unstable]
    return blocks


def _base_rate(facts: ReadinessFacts) -> tuple[float | None, int | None, str]:
    """`(base rate, customers at the latest prediction date, where the base rate came from)`.

    The customers are counted at the latest prediction date the build made, outcome known or not; the
    base rate pools only the dates the dataset kept with a known outcome."""
    report = facts.report
    every = report.snapshots if report is not None else ()
    kept = _kept(every)
    eligible: int | None = None
    if every:
        eligible = max(every, key=lambda s: s.date).entities
    elif facts.manifest is not None:
        eligible = facts.manifest.n_entities
    if not kept:
        return None, eligible, "Not measured: this dataset has no prediction date with a known outcome."
    positives = sum(s.positives or 0 for s in kept)
    customers = sum(s.entities for s in kept)
    source = (
        f"From this dataset: {positives:,} of {customers:,} customers had the outcome across the "
        f"{len(kept)} prediction date(s) used, a base rate of {positives / customers:.1%}."
    )
    return positives / customers, eligible, source


def campaign_aim(use_case_id: str, root: Path | None = None) -> Direction:
    """Which way a campaign of this use case tries to move its outcome: `"down"` for an outcome the
    use case exists to prevent (`engine.pilot.roi.outcome_is_good_by_default`, the pilot value view's
    rule), `"up"` otherwise, and `"either"` when the use case cannot be read."""
    from engine.config import load_use_case
    from engine.pilot.roi import outcome_is_good_by_default

    if not use_case_id:
        return "either"
    try:
        config = load_use_case(use_case_id, root)
    except Exception:  # a report shows what it can; an unreadable use case leaves the aim unknown
        return "either"
    outcome = config.label.name if config.label is not None else config.target.column
    if outcome is None:
        return "either"
    return "up" if outcome_is_good_by_default(use_case_id, outcome, root) else "down"


_AIM_WORDS: Final[dict[str, str]] = {
    "down": "a fall in the outcome, which is what a campaign of this use case aims for",
    "up": "a rise in the outcome, which is what a campaign of this use case aims for",
    "either": "up or down",
}


def _can_we_measure(facts: ReadinessFacts, settings: ReadinessSettings, root: Path | None) -> list[AnyBlock]:
    """The smallest change a test is sure to see at each control-group share (engine.measurement), in
    the direction the use case's campaigns aim for (:func:`campaign_aim`)."""
    from engine.measurement.planner import arm_sizes, mde_two_proportions

    if facts.label is None and facts.report is None:
        return []
    base_rate, eligible, source = _base_rate(facts)
    blocks: list[AnyBlock] = [Heading(text="Can we measure it?")]
    if eligible is None or eligible <= 0:
        blocks.append(Paragraph(text="Not measured: the build did not record how many customers there are."))
        return blocks
    aim = campaign_aim(facts.use_case_id, root)
    confidence = (1.0 - settings.alpha) * 100.0
    blocks.append(
        Paragraph(
            text=(
                f"How small a change a campaign to the {eligible:,} customers at the latest prediction date "
                f"can show, for each size of control group: the change is seen with a chance of "
                f"{settings.power:.0%} at {confidence:g}% confidence, {_AIM_WORDS[aim]}. {source}"
            )
        )
    )
    rows: list[tuple[str, str, str, str]] = []
    reasons: list[str] = []
    for share in settings.control_group_shares:
        n_treat, n_control = arm_sizes(eligible, share)
        mde = mde_two_proportions(
            n_treat, n_control, base_rate, settings.alpha, settings.power, direction=aim
        )
        if mde.points is None or mde.relative is None:
            seen = "not measured"
            if mde.reason and mde.reason not in reasons:
                reasons.append(mde.reason)
        else:
            seen = f"{mde.points:.1f} points ({mde.relative:.0%} of the base rate)"
        rows.append((f"{share:.0%}", f"{n_control:,}", f"{n_treat:,}", seen))
    blocks.append(
        Table(
            columns=(
                "Control group",
                "Customers held back",
                "Customers contacted",
                "Smallest change it can see",
            ),
            rows=tuple(rows),
            caption=(
                "A campaign whose real effect is smaller than this will usually read as no clear change. "
                "To see a smaller change, hold back more customers or contact more of them."
            ),
        )
    )
    blocks += [Paragraph(text=f"Not measured: {reason}") for reason in reasons]
    return blocks


_ASSIGNMENT_WORDS: Final[dict[str, str]] = {
    "random": "At random",
    "model_selected": "Not at random: by a model or a rule",
    "unknown": "Not known",
}


def _past_campaigns(facts: ReadinessFacts, files: dict[str, str], root: Path | None) -> list[AnyBlock]:
    """What the named treatment column says about past campaigns, and what to do first."""
    if facts.treatment is None and facts.treatment_note is None:
        return []
    blocks: list[AnyBlock] = [Heading(text="Past campaigns")]
    history = facts.treatment
    if history is None:
        blocks.append(Paragraph(text=facts.treatment_note or ""))
        return blocks

    def count(value: int | None) -> str:
        return "not known" if value is None else f"{value:,}"

    blocks.append(
        KeyValues(
            rows=(
                ("Column", history.column),
                ("How customers were chosen", _ASSIGNMENT_WORDS[history.assignment]),
                ("Contacted", count(history.treated)),
                ("Held back", count(history.control)),
                (
                    "With the outcome",
                    (
                        "not known"
                        if history.treated_positives is None or history.control_positives is None
                        else f"{history.treated_positives:,} contacted, {history.control_positives:,} held back"
                    ),
                ),
            )
        )
    )
    if history.check is not None:
        blocks.append(_problem(history.check, files, root, tone="warning"))
    else:
        blocks.append(Callout(title="How customers were chosen", text=history.message, tone="info"))
    now = history.verdict == "uplift_now"
    blocks.append(
        Callout(
            title=(
                "Learn who each campaign changes: now"
                if now
                else "Learn who each campaign changes: next cycle"
            ),
            text=history.verdict_message,
            tone="success" if now else "info",
        )
    )
    return blocks
