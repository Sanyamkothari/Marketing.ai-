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
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, TypeVar

from pydantic import BaseModel

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
    from engine.contracts import ValidationCheck
    from engine.onboarding.specs import (
        BuildReport,
        DatasetManifest,
        OnboardingSpec,
        SourceSpec,
    )
    from engine.storage import Storage

__all__ = ["ReadinessFacts", "ReadinessNotFoundError", "collect_readiness", "readiness_document"]

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
    storage: Storage, store: ClientStore, dataset_id: str, *, root: Path | None = None
) -> ReadinessFacts:
    """Read the artefacts of one dataset build. Raises `ReadinessNotFoundError` when there is none."""
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
    horizon = label.horizon_days if label is not None else None
    min_history = snapshots.min_history_days if snapshots is not None else 0
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
