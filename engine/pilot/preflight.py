"""The pre-flight checker: the client's files, checked on the client's laptop (Plan E M59, DEC-906).

A client analyst runs `python -m scripts.preflight <folder>` before anything leaves their systems.
Nothing is uploaded and nothing is written except one HTML page. The page answers what a first
upload would otherwise answer a week later: which file is which table, how many rows, which dates
they cover, how well the tables link up through the customer ID, which columns look like personal
details, and what is wrong with the format.

It does not have a second implementation of anything. Every file is read and profiled by the same
code the platform uses on an upload (`engine.stages.ingest.read_upload`, `profile_source`), so the
role it guesses, the dates it reads and the personal details it finds are the ones the platform
will find. The link-up and format checks are the onboarding checks themselves
(`engine.onboarding.validate`), run on the columns the profile identified as the customer ID and
the event date - the mapping a person would confirm on the mapping screen. What they report keeps
its code, so the readiness report after the upload and the pre-flight page before it speak one
vocabulary, explained by one catalogue (`configs/pilot/help.yaml`).

Where the checker cannot know - which column is the ID when no column looks like one, which table
a file is when neither its name nor its columns say - it says so (`PREFLIGHT_KEY_NOT_FOUND`,
`PREFLIGHT_TABLE_NOT_RECOGNISED`) rather than guessing.

Only counts, column names, file names and date ranges reach the page; no cell value does, except
the one date `DATE_FORMAT_AMBIGUOUS` quotes to ask its question, which is the client's own file on
the client's own laptop.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

from pydantic import BaseModel, ConfigDict

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
    import pandas as pd

    from engine.config import UseCaseConfig
    from engine.onboarding.specs import SourceProfile

__all__ = [
    "PREFLIGHT_CODES",
    "PreflightFinding",
    "PreflightResult",
    "PreflightTable",
    "preflight_document",
    "run_preflight",
]

logger = logging.getLogger(__name__)

PREFLIGHT_CODES: Final[frozenset[str]] = frozenset(
    {
        "PREFLIGHT_FILE_UNREADABLE",
        "PREFLIGHT_FILE_SKIPPED",
        "PREFLIGHT_TABLE_NOT_RECOGNISED",
        "PREFLIGHT_TABLE_MISSING",
        "PREFLIGHT_KEY_NOT_FOUND",
    }
)
"""The checker's own codes, for what only a checker without a mapping screen can run into."""

ONBOARDING_CHECKS_RUN: Final[tuple[str, ...]] = (
    "NO_ENTITY_SOURCE",
    "MULTIPLE_ENTITY_SOURCES",
    "TOO_MANY_SOURCES",
    "SOURCE_TOO_LARGE",
    "ENTITY_DUPLICATE_KEYS",
    "JOIN_KEY_COVERAGE_LOW",
    "KEY_FORMAT_MISMATCH",
    "EVENT_TIME_UNPARSEABLE",
    "DATE_FORMAT_AMBIGUOUS",
)
"""The onboarding checks that need nothing but the two identified columns. The others need a
confirmed mapping, a feature list or a built dataset, none of which exist before the upload."""

READABLE_SUFFIXES: Final[dict[str, Literal["csv", "parquet"]]] = {
    ".csv": "csv",
    ".txt": "csv",
    ".parquet": "parquet",
    ".pq": "parquet",
}

Severity = Literal["error", "warning", "info"]

_SEVERITY_RANK: Final[dict[str, int]] = {"error": 0, "warning": 1, "info": 2}
_ENTITY_KEY: Final[str] = "entity_key"
_EVENT_TIME: Final[str] = "event_time"
_ID_NAME: Final[re.Pattern[str]] = re.compile(
    r"(^|_)(id|key|code|no|num|number)$|^(cust|customer|subscriber|account|acct|msisdn_hash)", re.I
)
_DATE_NAME: Final[re.Pattern[str]] = re.compile(r"(^|_)(dt|date|time|day|week|month|ts)$|date", re.I)
_REPORT_FILENAME: Final[str] = "preflight_report.html"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PreflightFinding(_Strict):
    code: str
    severity: Severity
    file: str | None = None
    column: str | None = None
    message: str
    """The fact, with its numbers. The explanation and the fix come from the help catalogue."""


class PreflightTable(_Strict):
    file: str
    role: str | None
    title: str
    rows: int
    rows_read: int
    columns: int
    key_column: str | None = None
    date_column: str | None = None
    first_date: date | None = None
    last_date: date | None = None
    key_coverage: float | None = None
    personal_columns: tuple[tuple[str, tuple[str, ...]], ...] = ()
    """(column, kinds) for every column that looks like personal details or mentions them."""


class PreflightResult(_Strict):
    folder: str
    use_cases: tuple[str, ...]
    use_case_names: tuple[str, ...]
    tables: tuple[PreflightTable, ...]
    findings: tuple[PreflightFinding, ...]
    history_min_days: int
    history_recommended_days: int
    history_first: date | None
    history_last: date | None
    checked_at: datetime

    @property
    def verdict(self) -> VerdictState:
        if any(f.severity == "error" for f in self.findings):
            return "not_ready"
        if any(f.severity == "warning" for f in self.findings):
            return "warnings"
        return "ready"


@dataclass(frozen=True)
class _Read:
    path: Path
    frame: pd.DataFrame
    profile: SourceProfile
    rows: int
    role: str | None
    key: str | None
    time: str | None


# ---------------------------------------------------------------------------
# Reading and identifying
# ---------------------------------------------------------------------------
def _files(paths: list[Path]) -> list[Path]:
    found: list[Path] = []
    for path in paths:
        if path.is_dir():
            found += sorted(p for p in path.iterdir() if p.is_file() and not p.name.startswith("."))
        elif path.is_file():
            found.append(path)
    return [p for p in found if p.name != _REPORT_FILENAME]


def _role_from_name(file_name: str, files_by_role: dict[str, str]) -> str | None:
    """The role whose requested file name (`bills.csv`) this file carries, stem to stem."""
    stem = Path(file_name).stem.lower()
    for role, requested in files_by_role.items():
        if stem == Path(requested).stem.lower():
            return role
    return None


def _key_column(read_profile: SourceProfile, frame: pd.DataFrame, preferred: str | None) -> str | None:
    """The customer ID column: the entity table's own ID name when this table has it, else the
    likeliest ID-shaped column the profiler proposed, else nothing."""
    columns = [str(c) for c in frame.columns]
    if preferred is not None and preferred in columns:
        return preferred
    candidates = [c.column for c in read_profile.key_candidates]
    named = [c for c in candidates if _ID_NAME.search(c)]
    if named:
        return named[0]
    named = [c for c in columns if _ID_NAME.search(c)]
    return named[0] if named else None


def _time_column(read_profile: SourceProfile, frame: pd.DataFrame) -> str | None:
    """The event date: of the columns that read as dates, the one filled on most rows (an event's
    own date is always filled; a resolution or due date often is not), leftmost on a tie. When no
    column reads as a date, a column *named* like one, so the format check can say what is wrong
    with it instead of the page only saying that nothing was found."""
    nulls = {column.name: column.null_rate for column in read_profile.profile.columns}
    order = {str(name): index for index, name in enumerate(frame.columns)}
    if read_profile.time_candidates:
        best = min(
            read_profile.time_candidates,
            key=lambda c: (-round(c.parse_rate, 2), nulls.get(c.column, 1.0), order.get(c.column, 0)),
        )
        return best.column
    named = [str(c) for c in frame.columns if _DATE_NAME.search(str(c))]
    return named[0] if named else None


def _read_one(
    path: Path, config: UseCaseConfig, files_by_role: dict[str, str]
) -> tuple[_Read | None, PreflightFinding | None]:
    from engine.onboarding.sources import profile_source
    from engine.stages.ingest import IngestError, read_upload
    from engine.storage import LocalStorage

    file_format = READABLE_SUFFIXES.get(path.suffix.lower())
    if file_format is None:
        return None, PreflightFinding(
            code="PREFLIGHT_FILE_SKIPPED",
            severity="info",
            file=path.name,
            message=f"{path.name} is not a CSV or Parquet file, so it was not checked.",
        )
    storage = LocalStorage(path.parent)
    try:
        result = read_upload(storage, path.name, file_format=file_format)
    except IngestError as exc:
        return None, PreflightFinding(
            code="PREFLIGHT_FILE_UNREADABLE", severity="error", file=path.name, message=exc.message
        )
    except Exception as exc:  # an unreadable file must become a finding, never a crash
        logger.info("preflight.read_failed file=%s error=%s", path.name, type(exc).__name__)
        return None, PreflightFinding(
            code="PREFLIGHT_FILE_UNREADABLE",
            severity="error",
            file=path.name,
            message=f"{path.name} could not be read as a {file_format.upper()} file.",
        )
    try:
        profile = profile_source(
            result.frame,
            config,
            source_id=path.name,
            client_id="preflight",
            file_name=path.name,
            file_format=result.file_format,
            file_size_bytes=storage.size_bytes(path.name),
            delimiter=result.delimiter,
            encoding=result.encoding,
            row_count=result.row_count,
            fingerprint=result.fingerprint,
        )
    except Exception as exc:  # a file the profiler cannot handle is a finding, never a crash
        logger.info("preflight.profile_failed file=%s error=%s", path.name, type(exc).__name__)
        return None, PreflightFinding(
            code="PREFLIGHT_FILE_UNREADABLE",
            severity="error",
            file=path.name,
            message=f"{path.name} was read, but its columns could not be examined (check the header row).",
        )
    role = _role_from_name(path.name, files_by_role)
    if role is None and profile.role_candidates:
        top = profile.role_candidates[0]
        role = top.role if top.confidence > _NOT_RECOGNISED_BELOW else None
    return (
        _Read(
            path=path,
            frame=result.frame,
            profile=profile,
            rows=result.row_count,
            role=role,
            key=None,
            time=None,
        ),
        None,
    )


_NOT_RECOGNISED_BELOW: Final[float] = 0.10
"""At or below this the detector fell back to a default (its floor is 0.05): "we could not tell"."""


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------
def run_preflight(
    paths: list[Path],
    use_cases: tuple[str, ...] | None = None,
    *,
    root: Path | None = None,
    now: datetime | None = None,
) -> PreflightResult:
    """Check the files at `paths` (files, or folders of them) against the data request of `use_cases`."""
    from engine.config import get_roles, load_use_case
    from engine.contracts import Severity as CheckSeverity
    from engine.onboarding.validate import CHECK_REGISTRY, OnboardingCheckParams, SourceFacts
    from engine.pilot.data_request import build_data_request, load_wording
    from engine.utils.time import utc_now

    wording = load_wording(root)
    ids = use_cases or wording.default_use_cases
    configs = tuple(load_use_case(use_case_id, root) for use_case_id in ids)
    request = build_data_request(ids, root)
    roles = get_roles(root)
    entity_role = roles.entity_role
    files_by_role = {role: words.file for role, words in wording.tables.items()}

    findings: list[PreflightFinding] = []
    reads: list[_Read] = []
    for path in _files(paths):
        read, finding = _read_one(path, configs[0], files_by_role)
        if finding is not None:
            findings.append(finding)
        if read is not None:
            reads.append(read)

    entity_reads = [r for r in reads if r.role == entity_role]
    entity_key: str | None = None
    identified: list[_Read] = []
    for read in sorted(reads, key=lambda r: r.role != entity_role):
        if read.role is None:
            findings.append(
                PreflightFinding(
                    code="PREFLIGHT_TABLE_NOT_RECOGNISED",
                    severity="warning",
                    file=read.path.name,
                    message=f"{read.path.name} does not look like any table the data request lists.",
                )
            )
        key = _key_column(read.profile, read.frame, entity_key)
        if read.role == entity_role and entity_key is None:
            entity_key = key
        time = None if read.role == entity_role else _time_column(read.profile, read.frame)
        if key is None:
            findings.append(
                PreflightFinding(
                    code="PREFLIGHT_KEY_NOT_FOUND",
                    severity="error",
                    file=read.path.name,
                    message=f"No column of {read.path.name} looks like a customer ID.",
                )
            )
        if read.role not in (None, entity_role) and time is None:
            findings.append(
                PreflightFinding(
                    code="EVENT_TIME_UNMAPPED",
                    severity="error",
                    file=read.path.name,
                    message=f"No column of {read.path.name} reads as a date, so its rows cannot be placed in time.",
                )
            )
        identified.append(replace(read, key=key, time=time))

    # --- the onboarding checks, on the identified columns ----------------------------------------
    facts: list[SourceFacts] = []
    for read in identified:
        if read.role is None:
            continue
        renamed: dict[str, str] = {}
        if read.key is not None:
            renamed[read.key] = _ENTITY_KEY
        if read.time is not None:
            renamed[read.time] = _EVENT_TIME
        frame = read.frame[list(renamed)].rename(columns=renamed) if renamed else None
        facts.append(
            SourceFacts(
                source_id=read.path.name,
                role=read.role,
                rows=read.rows,
                mapped_standard=tuple(renamed.values()),
                frame=frame,
            )
        )
    limits = configs[0].onboarding.limits
    params = OnboardingCheckParams(
        sources=tuple(facts),
        entity="customer",  # the client's word, whatever the use case calls its rows
        entity_role=entity_role,
        max_source_rows=limits.max_source_rows,
        max_sources=limits.max_sources,
    )
    for code in ONBOARDING_CHECKS_RUN:
        try:
            checks = CHECK_REGISTRY[code](params)
        except Exception as exc:  # one check failing must not hide the others
            logger.info("preflight.check_failed code=%s error=%s", code, type(exc).__name__)
            continue
        for check in checks:
            severity: Severity = "error" if check.severity is CheckSeverity.ERROR else "warning"
            if check.code == "DATE_FORMAT_AMBIGUOUS":
                severity = "warning"  # answered by naming the format when the file is mapped
            findings.append(
                PreflightFinding(
                    code=check.code,
                    severity=severity,
                    file=check.source_id,
                    column=_client_column(identified, check.source_id, check.column),
                    message=check.message,
                )
            )

    # --- personal details ---------------------------------------------------------------------------
    tables: list[PreflightTable] = []
    personal_by_column: dict[tuple[str, str, tuple[str, ...]], list[str]] = {}
    entity_keys = None
    if entity_reads:
        first = next(r for r in identified if r.role == entity_role)
        if first.key is not None:
            entity_keys = first.frame[first.key]
    for read in identified:
        personal: list[tuple[str, tuple[str, ...]]] = []
        for column in read.profile.profile.columns:
            kinds = tuple(dict.fromkeys((*column.pii_kinds, *column.free_text_pii_kinds)))
            if kinds:
                personal.append((column.name, kinds))
                code = "PII_DETECTED" if column.pii_kinds else "PII_IN_FREE_TEXT"
                shown = column.pii_kinds or column.free_text_pii_kinds
                personal_by_column.setdefault((code, column.name, shown), []).append(read.path.name)
        coverage = None
        if read.role not in (None, entity_role) and read.key is not None and entity_keys is not None:
            from engine.onboarding.sources import join_coverage

            coverage = join_coverage(read.frame[read.key], entity_keys)
        span = next((c for c in read.profile.time_candidates if c.column == read.time), None)
        tables.append(
            PreflightTable(
                file=read.path.name,
                role=read.role,
                title=wording.tables[read.role].title if read.role in wording.tables else "Not recognised",
                rows=read.rows,
                rows_read=len(read.frame),
                columns=len(read.frame.columns),
                key_column=read.key,
                date_column=read.time,
                first_date=span.earliest if span is not None else None,
                last_date=span.latest if span is not None else None,
                key_coverage=coverage,
                personal_columns=tuple(personal),
            )
        )

    for (code, name, kinds), files in personal_by_column.items():
        where = files[0] if len(files) == 1 else f"{len(files)} files ({', '.join(files)})"
        is_key = any(t.key_column == name for t in tables)
        if code == "PII_DETECTED":
            message = f"'{name}' in {where} looks like {_kinds(kinds)}."
            if is_key:
                message += (
                    " It is the customer ID column: if these are real contact numbers, replace them "
                    "with pseudonymised codes as the data request describes."
                )
        else:
            message = f"'{name}' in {where} is free text, and some of it contains {_kinds(kinds)}."
        findings.append(
            PreflightFinding(
                code=code,
                severity="warning",
                file=files[0] if len(files) == 1 else None,
                column=name,
                message=message,
            )
        )

    # --- tables the request asks for --------------------------------------------------------------
    present = {read.role for read in identified}
    label_roles = {c.label.role for c in configs if c.label is not None and c.label.role}
    for requested in request.tables:
        if requested.role in present or requested.role == entity_role:
            continue  # a missing customer table is NO_ENTITY_SOURCE's, already reported
        if requested.role in label_roles:
            findings.append(
                PreflightFinding(
                    code="LABEL_ROLE_MISSING",
                    severity="error",
                    message=f"No file looks like the {requested.title.lower()} table ({requested.file}), which the outcome is worked out from.",
                )
            )
            continue
        findings.append(
            PreflightFinding(
                code="PREFLIGHT_TABLE_MISSING",
                severity="error" if requested.need == "required" else "warning",
                message=(
                    f"No file looks like the {requested.title.lower()} table ({requested.file}), which the "
                    f"data request marks {requested.need}."
                ),
            )
        )

    # --- history ------------------------------------------------------------------------------------
    firsts = [t.first_date for t in tables if t.first_date is not None]
    lasts = [t.last_date for t in tables if t.last_date is not None]
    first_day = min(firsts) if firsts else None
    last_day = max(lasts) if lasts else None
    if first_day is not None and last_day is not None:
        covered = (last_day - first_day).days
        if covered < request.history_min_days:
            findings.append(
                PreflightFinding(
                    code="TOO_LITTLE_HISTORY",
                    severity="error",
                    message=(
                        f"The dated tables cover {covered:,} days ({first_day} to {last_day}); these use cases "
                        f"need at least {request.history_min_days:,}."
                    ),
                )
            )
    if entity_reads:
        customers = max(r.rows for r in entity_reads)
        if customers < request.min_customers:
            findings.append(
                PreflightFinding(
                    code="ROWS_TOO_FEW",
                    severity="warning",
                    file=entity_reads[0].path.name,
                    message=(
                        f"The customer table has {customers:,} rows. The platform needs at least "
                        f"{request.min_customers:,} rows to learn from; with one row per customer per "
                        "prediction date it may still get there, but more customers make results steadier."
                    ),
                )
            )
    ordered = sorted(findings, key=lambda f: (_SEVERITY_RANK[f.severity], f.file or "", f.code))
    return PreflightResult(
        folder=", ".join(str(p) for p in paths),
        use_cases=tuple(c.id for c in configs),
        use_case_names=tuple(c.name for c in configs),
        tables=tuple(tables),
        findings=tuple(ordered),
        history_min_days=request.history_min_days,
        history_recommended_days=request.history_recommended_days,
        history_first=first_day,
        history_last=last_day,
        checked_at=now or utc_now(),
    )


def _client_column(reads: list[_Read], file: str | None, column: str | None) -> str | None:
    """The client's own name for the column a check calls `entity_key` or `event_time`."""
    if column is None:
        return None
    for read in reads:
        if read.path.name == file:
            if column == _ENTITY_KEY:
                return read.key
            if column == _EVENT_TIME:
                return read.time
    return column


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


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------
_VERDICT_TEXT: Final[dict[VerdictState, tuple[str, str]]] = {
    "ready": ("The files look ready to send", "Nothing here stops the platform from reading these files."),
    "warnings": (
        "The files can be sent; please read the warnings",
        "Nothing blocks the platform, but the points below would make the pilot better or safer.",
    ),
    "not_ready": (
        "Please fix the problems below before sending",
        "At least one problem would stop the platform from using these files.",
    ),
    "info": ("", ""),
}

_SEVERITY_LABEL: Final[dict[str, str]] = {"error": "Problem", "warning": "Warning", "info": "Note"}
_TONE: Final[dict[str, Literal["error", "warning", "info"]]] = {
    "error": "error",
    "warning": "warning",
    "info": "info",
}


def _pct(value: float) -> str:
    return f"{value:.0%}"


def preflight_document(result: PreflightResult, root: Path | None = None) -> ReportDocument:
    """The one-page pre-flight report."""
    from engine.pilot.help import code_help

    verdict = result.verdict
    title, text = _VERDICT_TEXT[verdict]
    errors = sum(1 for f in result.findings if f.severity == "error")
    warnings = sum(1 for f in result.findings if f.severity == "warning")
    blocks: list[AnyBlock] = [
        Verdict(
            state=verdict,
            title=title,
            text=f"{text} {errors} problem(s), {warnings} warning(s).",
        ),
        Heading(text="Files found"),
        Table(
            columns=("File", "Looks like", "Rows", "Customer ID column", "Date column", "From", "To"),
            rows=tuple(
                (
                    t.file,
                    t.title,
                    f"{t.rows:,}",
                    t.key_column or "not found",
                    t.date_column or ("not needed" if t.role == "entity" else "not found"),
                    t.first_date.isoformat() if t.first_date else "",
                    t.last_date.isoformat() if t.last_date else "",
                )
                for t in result.tables
            ),
            empty_text="No readable file was found.",
        ),
    ]
    linked = [t for t in result.tables if t.key_coverage is not None]
    if linked:
        blocks += [
            Heading(text="How the tables link up"),
            Paragraph(
                text=(
                    "The share of each table's rows whose customer ID is also in the customer table. "
                    "Close to 100% is what we want."
                )
            ),
            Table(
                columns=("File", "Rows linked to a known customer"),
                rows=tuple((t.file, _pct(t.key_coverage or 0.0)) for t in linked),
            ),
        ]
    history: list[tuple[str, str]] = [
        ("Needed (minimum)", f"{result.history_min_days:,} days"),
        ("Recommended", f"{result.history_recommended_days:,} days"),
    ]
    if result.history_first is not None and result.history_last is not None:
        span = (result.history_last - result.history_first).days
        history.insert(
            0, ("Your files cover", f"{result.history_first} to {result.history_last} ({span:,} days)")
        )
    blocks += [Heading(text="History"), KeyValues(rows=tuple(history))]
    personal = [(t.file, column, kinds) for t in result.tables for column, kinds in t.personal_columns]
    blocks.append(Heading(text="Personal details found"))
    if personal:
        blocks.append(
            Table(
                columns=("File", "Column", "Looks like"),
                rows=tuple((file, column, _kinds(kinds)) for file, column, kinds in personal),
                caption="Remove these columns (or the contact details inside them) before sending.",
            )
        )
    else:
        blocks.append(
            Paragraph(text="No column looks like names, phone numbers, e-mail addresses, Aadhaar or PAN.")
        )
    blocks.append(Heading(text="What to fix"))
    if not result.findings:
        blocks.append(Paragraph(text="Nothing. The files match the data request."))
    for finding in result.findings:
        entry = code_help(finding.code, root)
        where = ", ".join(
            part for part in (finding.file, finding.column and f"column '{finding.column}'") if part
        )
        heading = entry.title if entry is not None else finding.code
        body = finding.message
        if entry is not None:
            body = f"{finding.message} {entry.meaning} What to do: {entry.fix}"
        blocks.append(
            Callout(
                title=f"{_SEVERITY_LABEL[finding.severity]}: {heading}" + (f" ({where})" if where else ""),
                text=body,
                tone=_TONE[finding.severity],
            )
        )
    blocks.append(
        Bullets(
            items=(
                "This check ran on this computer only. Nothing was uploaded.",
                "It reads the first two million rows of a very large file; row counts are always exact.",
            )
        )
    )
    return ReportDocument(
        kind="preflight",
        title="Pre-flight check of your files",
        subtitle="Run before sending data for the pilot",
        generated_at=result.checked_at,
        facts=(
            ("Use cases", ", ".join(result.use_case_names)),
            ("Folder", result.folder),
        ),
        blocks=tuple(blocks),
        footer="Questions about a finding? Send this page, not the data, to your Minfy contact.",
    )
