"""The onboarding checks (Phase 2 plan section 7): can this client's data be built into a dataset,
and will that dataset mean anything?

The architecture is the one `engine.stages.validate` established (DEC-065) and for the same reason:
**a check knows about data, not about a request.** Every check here is a pure function of one flat,
frozen, fully-defaulted :class:`OnboardingCheckParams`, so `check(OnboardingCheckParams())` is always
legal, nothing raises on bad data, and the build stage - which owns the DuckDB connection, the clock
and the config - is the only thing that has to know where the numbers came from. There is no
`params_from_config` here: every threshold field is named exactly like its `onboarding.*` config
leaf, so the build stage copies them across and this module never reads a config.

What the params hold is *measured* facts: how many rows a source has, which standard names its
mapping produced, the mapped table itself. A fact nobody measured is absent, not zero - an empty
`future_event_rows` means the audit did not run or found nothing, and both of those honestly produce
no finding, while a fabricated zero would be a claim the build made no leak.

**A mapped table** is the `MappingSpec` applied to a source: standard column names (`entity_key`,
`event_time`, plus the role's standard columns). `event_time` arrives as the client wrote it unless
the mapping pinned a `cast`, which is the point: EVENT_TIME_UNPARSEABLE and DATE_FORMAT_AMBIGUOUS
are the checks whose answer is that cast. Once one is pinned, a value that still fails to read is a
transform failure, counted by `transforms.apply_transform` and reported by the mapping stage; from
here a `NaT` and a date the client never filled in are the same thing and this module does not
pretend to tell them apart.

This module owns eighteen of the twenty-six codes in `ONBOARDING_VALIDATION_CODES`. The other eight
belong to `labels.py` (LABEL_*), `snapshots.py` (SNAPSHOT_OUTSIDE_DATA_RANGE, TOO_LITTLE_HISTORY) and
the mapping stage (MAPPING_*, VALUE_UNMAPPED), which raise them where the numbers are; `CHECK_ORDER`
is the list of the eighteen and a test holds the registry to exactly it.

Nothing here logs a data value (plan section 13.7): the logger sees a code and an exception class
name. Messages are another matter - a date nobody can read is only fixable by the person who can see
which date it was, so DATE_FORMAT_AMBIGUOUS quotes one real value, exactly as the Phase 1 checks
quote sample values.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, TypeAlias

from engine.contracts import Severity
from engine.onboarding.sources import JOIN_COVERAGE_OK, join_coverage, key_format_mismatch
from engine.onboarding.specs import OnboardingCheck, SnapshotMode
from engine.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    import pandas as pd


__all__ = [
    "CHECK_ORDER",
    "CHECK_REGISTRY",
    "CheckFn",
    "OnboardingCheckParams",
    "SourceFacts",
    "check_date_format_ambiguous",
    "check_entity_attributes_not_time_versioned",
    "check_entity_duplicate_keys",
    "check_entity_key_unmapped",
    "check_event_time_unmapped",
    "check_event_time_unparseable",
    "check_feature_all_null",
    "check_feature_name_collision",
    "check_future_events_leaked",
    "check_join_key_coverage_low",
    "check_join_key_unmapped",
    "check_key_format_mismatch",
    "check_multiple_entity_sources",
    "check_no_entity_source",
    "check_required_standard_column_unmapped",
    "check_source_too_large",
    "check_too_many_features",
    "check_too_many_sources",
    "run_onboarding_checks",
]

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# The mapped-table contract
# ---------------------------------------------------------------------------
_ENTITY_KEY: Final[str] = "entity_key"
_EVENT_TIME: Final[str] = "event_time"
_SNAPSHOT_COLUMN: Final[str] = "snapshot_date"
"""The three names the engine owns. A mapping renames a client's columns to these, the build
registers each mapped table as a view of its role, and every query is written against them."""

#: plan section 7 table order, restricted to the codes this module raises.
CHECK_ORDER: Final[tuple[str, ...]] = (
    "NO_ENTITY_SOURCE",
    "MULTIPLE_ENTITY_SOURCES",
    "ENTITY_KEY_UNMAPPED",
    "ENTITY_DUPLICATE_KEYS",
    "JOIN_KEY_UNMAPPED",
    "EVENT_TIME_UNMAPPED",
    "EVENT_TIME_UNPARSEABLE",
    "DATE_FORMAT_AMBIGUOUS",
    "JOIN_KEY_COVERAGE_LOW",
    "KEY_FORMAT_MISMATCH",
    "REQUIRED_STANDARD_COLUMN_UNMAPPED",
    "FEATURE_ALL_NULL",
    "FEATURE_NAME_COLLISION",
    "TOO_MANY_FEATURES",
    "ENTITY_ATTRIBUTES_NOT_TIME_VERSIONED",
    "FUTURE_EVENTS_LEAKED",
    "SOURCE_TOO_LARGE",
    "TOO_MANY_SOURCES",
)

_SEVERITY_RANK: Final[Mapping[Severity, int]] = MappingProxyType(
    {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}
)

_DATE_FORMATS: Final[tuple[str, ...]] = (
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%d-%m-%Y",
    "%d.%m.%Y",
    "%Y/%m/%d",
    "%d-%b-%Y",
    "%d %b %Y",
    "%Y-%m-%d %H:%M:%S",
)
"""Formats EVENT_TIME_UNPARSEABLE reads a column against, ISO first so a tie never resolves to a
regional guess. They are strptime patterns because that is what goes in `ColumnTransform.date_format`."""

_KEY_TRANSFORM_LABELS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "strip": "removing the spaces around them",
        "lower": "lower-casing them",
        "lstrip_zeros": "removing the leading zeros",
    }
)


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SourceFacts:
    """One uploaded source as it actually stands, after its mapping was applied.

    `rows` is the FILE's row count and `frame` may be a capped read of it, so a size limit and a
    duplicate-key count never quietly answer about different amounts of data.
    """

    source_id: str
    role: str
    rows: int = 0
    mapped_standard: tuple[str, ...] = ()
    frame: pd.DataFrame | None = None
    event_time_format: str | None = None


@dataclass(frozen=True, slots=True)
class OnboardingCheckParams:
    """Flat, scalar, frozen. EVERY field has a default, so `check(OnboardingCheckParams())` is legal.

    The threshold fields are named exactly like their `onboarding.*` config leaves and carry the same
    defaults, so the build stage copies them across one for one and no check ever reads a config.
    """

    # --- measured facts ---------------------------------------------------------
    sources: tuple[SourceFacts, ...] = ()
    feature_names: tuple[str, ...] = ()
    feature_null_fractions: Mapping[str, float] = field(default_factory=dict)
    future_event_rows: Mapping[str, int] = field(default_factory=dict)
    label_name: str = ""

    # --- what the use case asks for ---------------------------------------------
    required_standard_columns: tuple[str, ...] = ()
    entity: str = "customer"
    entity_role: str = "entity"
    snapshot_mode: SnapshotMode = SnapshotMode.PERIODIC

    # --- thresholds and limits ---------------------------------------------------
    max_features: int = 300
    drop_if_null_fraction_above: float = 0.98
    max_source_rows: int = 50_000_000
    max_sources: int = 10
    join_coverage_warn: float = JOIN_COVERAGE_OK
    join_coverage_error: float = 0.30
    max_unparseable_fraction: float = 0.02


CheckFn: TypeAlias = "Callable[[OnboardingCheckParams], tuple[OnboardingCheck, ...]]"

_REGISTRY: dict[str, CheckFn] = {}


def _check(code: str) -> Callable[[CheckFn], CheckFn]:
    """Register a check and return it unchanged, so it stays directly importable and callable."""

    def decorate(fn: CheckFn) -> CheckFn:
        _REGISTRY[code] = fn
        return fn

    return decorate


# ---------------------------------------------------------------------------
# Shared readers - all pure, all tolerant of a source nobody mapped
# ---------------------------------------------------------------------------
def _entity_sources(params: OnboardingCheckParams) -> tuple[SourceFacts, ...]:
    return tuple(source for source in params.sources if source.role == params.entity_role)


def _event_sources(params: OnboardingCheckParams) -> tuple[SourceFacts, ...]:
    return tuple(source for source in params.sources if source.role != params.entity_role)


def _column(source: SourceFacts, name: str) -> pd.Series[Any] | None:
    frame = source.frame
    if frame is None or name not in frame.columns:
        return None
    return frame[name]


def _entity_keys(params: OnboardingCheckParams) -> pd.Series[Any] | None:
    """The ids every event table has to join to, from the first entity source that carries them."""
    for source in _entity_sources(params):
        keys = _column(source, _ENTITY_KEY)
        if keys is not None:
            return keys
    return None


def _event_time_text(source: SourceFacts) -> pd.Series[Any] | None:
    """`event_time` as the client wrote it, nulls dropped, or `None` when there is nothing to read."""
    import pandas as pd

    series = _column(source, _EVENT_TIME)
    if series is None:
        return None
    if pd.api.types.is_datetime64_any_dtype(series):
        return None
    values = series.dropna()
    return None if values.empty else values.astype(str)


def _ordinal(day: int) -> str:
    suffix = "th" if 11 <= day % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


# ---------------------------------------------------------------------------
# Which tables we were given
# ---------------------------------------------------------------------------
@_check("NO_ENTITY_SOURCE")
def check_no_entity_source(params: OnboardingCheckParams) -> tuple[OnboardingCheck, ...]:
    """Without one row per entity there is no row to build, so this is said first and plainly."""
    if _entity_sources(params):
        return ()
    return (
        OnboardingCheck(
            code="NO_ENTITY_SOURCE",
            severity=Severity.ERROR,
            message=(
                f"None of the {len(params.sources)} tables uploaded holds one row per "
                f"{params.entity}, so there is nothing to build a row for."
            ),
            suggestion=f"Add the table that has one row per {params.entity}.",
            details={
                "source_count": len(params.sources),
                "roles": sorted({source.role for source in params.sources}),
                "entity_role": params.entity_role,
            },
        ),
    )


@_check("MULTIPLE_ENTITY_SOURCES")
def check_multiple_entity_sources(params: OnboardingCheckParams) -> tuple[OnboardingCheck, ...]:
    """Two entity tables means two answers to "who exists", and the build would silently pick one."""
    entities = _entity_sources(params)
    if len(entities) <= 1:
        return ()
    return (
        OnboardingCheck(
            code="MULTIPLE_ENTITY_SOURCES",
            severity=Severity.ERROR,
            message=(
                f"{len(entities)} tables are marked as the {params.entity} table. Only one table can "
                f"hold one row per {params.entity}."
            ),
            suggestion=(
                f"Keep the one that lists every {params.entity} and change the others to the kind of "
                "event they record."
            ),
            details={"source_ids": [source.source_id for source in entities]},
        ),
    )


@_check("TOO_MANY_SOURCES")
def check_too_many_sources(params: OnboardingCheckParams) -> tuple[OnboardingCheck, ...]:
    """Phase 2 reads files, and every extra table is another full read and another join."""
    count = len(params.sources)
    if count <= params.max_sources:
        return ()
    return (
        OnboardingCheck(
            code="TOO_MANY_SOURCES",
            severity=Severity.ERROR,
            message=f"{count} tables were uploaded for this use case; the limit is {params.max_sources}.",
            suggestion=(
                "Remove the tables this use case does not need, or combine the ones that hold the "
                "same kind of row into one file."
            ),
            details={"source_count": count, "max_sources": params.max_sources},
        ),
    )


@_check("SOURCE_TOO_LARGE")
def check_source_too_large(params: OnboardingCheckParams) -> tuple[OnboardingCheck, ...]:
    """Measured against the file, not the frame: a capped read would report a limit it cleared."""
    return tuple(
        OnboardingCheck(
            code="SOURCE_TOO_LARGE",
            severity=Severity.ERROR,
            message=(
                f"The {source.role} table has {source.rows:,} rows; this engine reads files of up to "
                f"{params.max_source_rows:,} rows."
            ),
            suggestion=(
                f"Upload a shorter history, or filter the file down to the {params.entity}s and the "
                "dates this use case needs."
            ),
            source_id=source.source_id,
            details={"rows": source.rows, "max_source_rows": params.max_source_rows},
        )
        for source in params.sources
        if source.rows > params.max_source_rows
    )


# ---------------------------------------------------------------------------
# Whether the tables can be joined at all
# ---------------------------------------------------------------------------
@_check("ENTITY_KEY_UNMAPPED")
def check_entity_key_unmapped(params: OnboardingCheckParams) -> tuple[OnboardingCheck, ...]:
    """The id of the entity table is the spine of the dataset; nothing can be attached without it."""
    return tuple(
        OnboardingCheck(
            code="ENTITY_KEY_UNMAPPED",
            severity=Severity.ERROR,
            message=(
                f"The {params.entity} table has no column mapped to the {params.entity} id, so its "
                "rows cannot be identified or joined to anything."
            ),
            suggestion=(
                f"On the mapping screen, map the column that identifies each {params.entity} - "
                "usually the customer number or account id."
            ),
            source_id=source.source_id,
            details={"standard": _ENTITY_KEY},
        )
        for source in _entity_sources(params)
        if _ENTITY_KEY not in source.mapped_standard
    )


@_check("JOIN_KEY_UNMAPPED")
def check_join_key_unmapped(params: OnboardingCheckParams) -> tuple[OnboardingCheck, ...]:
    """An event table with no id is a pile of rows that belong to nobody."""
    return tuple(
        OnboardingCheck(
            code="JOIN_KEY_UNMAPPED",
            severity=Severity.ERROR,
            message=(
                f"The {source.role} table has no column mapped to the {params.entity} id, so none of "
                f"its rows can be attached to a {params.entity}."
            ),
            suggestion=(
                f"On the mapping screen, map the column of the {source.role} table that holds the "
                f"{params.entity} id."
            ),
            source_id=source.source_id,
            details={"standard": _ENTITY_KEY, "role": source.role},
        )
        for source in _event_sources(params)
        if _ENTITY_KEY not in source.mapped_standard
    )


@_check("ENTITY_DUPLICATE_KEYS")
def check_entity_duplicate_keys(params: OnboardingCheckParams) -> tuple[OnboardingCheck, ...]:
    """A repeated id means the entity table is really an event table, or holds one row per version.

    The second case is the common one and has a one-click answer, so the suggestion names it: keep
    the latest row per entity by a date column the table actually carries. When it carries none, the
    offer is left out rather than invented - a dedupe by a column nobody can name is not a fix.
    """
    findings: list[OnboardingCheck] = []
    for source in _entity_sources(params):
        keys = _column(source, _ENTITY_KEY)
        if keys is None:
            continue
        rows = len(keys)
        distinct = int(keys.nunique(dropna=True))
        duplicates = int(keys.dropna().duplicated().sum())
        if duplicates == 0:
            continue
        by = _latest_by_column(source)
        offer = (
            f"Keep only the latest row per {params.entity} by '{by}' - one click on the mapping "
            "screen - or upload a table that already has one row per "
            f"{params.entity}."
            if by is not None
            else (
                f"Combine the rows so each {params.entity} appears once, or upload a table that "
                f"already has one row per {params.entity}."
            )
        )
        findings.append(
            OnboardingCheck(
                code="ENTITY_DUPLICATE_KEYS",
                severity=Severity.ERROR,
                message=(
                    f"The {params.entity} table has {rows:,} rows for {distinct:,} different "
                    f"{params.entity}s, so {duplicates:,} rows repeat an id. The dataset needs one "
                    f"row per {params.entity}."
                ),
                suggestion=offer,
                column=_ENTITY_KEY,
                source_id=source.source_id,
                details={
                    "rows": rows,
                    "distinct_keys": distinct,
                    "duplicate_rows": duplicates,
                    "transform": "dedupe",
                    "dedupe_by": by,
                },
            )
        )
    return tuple(findings)


def _latest_by_column(source: SourceFacts) -> str | None:
    import pandas as pd

    frame = source.frame
    if frame is None:
        return None
    dated = [
        str(name)
        for name in frame.columns
        if str(name) != _ENTITY_KEY and pd.api.types.is_datetime64_any_dtype(frame[name])
    ]
    if not dated:
        return None
    return _EVENT_TIME if _EVENT_TIME in dated else dated[0]


@_check("JOIN_KEY_COVERAGE_LOW")
def check_join_key_coverage_low(params: OnboardingCheckParams) -> tuple[OnboardingCheck, ...]:
    """Ids that do not join produce features that are null for reasons nothing else will explain.

    The only acknowledgeable finding in this module: a client whose complaints file genuinely covers
    one region knows that, and the dataset they get is honest about it - every other error here is a
    thing that must be fixed before a row can be built at all.
    """
    entity_keys = _entity_keys(params)
    if entity_keys is None:
        return ()
    findings: list[OnboardingCheck] = []
    for source in _event_sources(params):
        keys = _column(source, _ENTITY_KEY)
        if keys is None:
            continue
        coverage = join_coverage(keys, entity_keys)
        if coverage >= params.join_coverage_warn:
            continue
        severity = Severity.ERROR if coverage < params.join_coverage_error else Severity.WARNING
        findings.append(
            OnboardingCheck(
                code="JOIN_KEY_COVERAGE_LOW",
                severity=severity,
                message=(f"Only {coverage:.0%} of {source.role} rows belong to a known {params.entity}."),
                suggestion="Check that the key columns match (e.g. leading zeros, prefixes).",
                column=_ENTITY_KEY,
                source_id=source.source_id,
                details={
                    "coverage": coverage,
                    "role": source.role,
                    "warn_below": params.join_coverage_warn,
                    "error_below": params.join_coverage_error,
                },
                acknowledgeable=True,
            )
        )
    return tuple(findings)


@_check("KEY_FORMAT_MISMATCH")
def check_key_format_mismatch(params: OnboardingCheckParams) -> tuple[OnboardingCheck, ...]:
    """The near-miss join that a single transform fixes, named by `sources.key_format_mismatch`.

    Judged there against the same bar JOIN_KEY_COVERAGE_LOW uses, so a transform offered here is one
    that actually clears the check that flagged the mismatch.
    """
    entity_keys = _entity_keys(params)
    if entity_keys is None:
        return ()
    findings: list[OnboardingCheck] = []
    for source in _event_sources(params):
        keys = _column(source, _ENTITY_KEY)
        if keys is None:
            continue
        transform = key_format_mismatch(keys, entity_keys)
        if transform is None:
            continue
        findings.append(
            OnboardingCheck(
                code="KEY_FORMAT_MISMATCH",
                severity=Severity.WARNING,
                message=(
                    f"The {params.entity} ids in the {source.role} table are written differently from "
                    f"the ones in the {params.entity} table, but they match after "
                    f"{_KEY_TRANSFORM_LABELS[transform]}."
                ),
                suggestion=(
                    f"Apply '{transform}' to the id column of the {source.role} table on the mapping "
                    "screen, and the two tables join."
                ),
                column=_ENTITY_KEY,
                source_id=source.source_id,
                details={
                    "transform": transform,
                    "coverage": join_coverage(keys, entity_keys),
                    "role": source.role,
                },
            )
        )
    return tuple(findings)


# ---------------------------------------------------------------------------
# Whether the events can be placed in time
# ---------------------------------------------------------------------------
@_check("EVENT_TIME_UNMAPPED")
def check_event_time_unmapped(params: OnboardingCheckParams) -> tuple[OnboardingCheck, ...]:
    """An undated event cannot be put on one side of a snapshot, so it can feed nothing."""
    return tuple(
        OnboardingCheck(
            code="EVENT_TIME_UNMAPPED",
            severity=Severity.ERROR,
            message=(
                f"The {source.role} table has no column mapped to the date each row happened, so its "
                "rows cannot be placed before or after a snapshot date."
            ),
            suggestion=(
                f"On the mapping screen, map the column that says when each {source.role} row " "happened."
            ),
            source_id=source.source_id,
            details={"standard": _EVENT_TIME, "role": source.role},
        )
        for source in _event_sources(params)
        if _EVENT_TIME not in source.mapped_standard
    )


@_check("EVENT_TIME_UNPARSEABLE")
def check_event_time_unparseable(params: OnboardingCheckParams) -> tuple[OnboardingCheck, ...]:
    """Dates that do not read as dates, with the format most of them are written in.

    The format offered is measured, never guessed: every candidate is parsed over the column and the
    one that reads the most values is quoted with the share it reads, so the user can see what the
    rest of the column has to be changed to match. A column no candidate reads at all gets no
    format - the answer is then not a format anybody can name.
    """
    import pandas as pd

    findings: list[OnboardingCheck] = []
    for source in params.sources:
        values = _event_time_text(source)
        if values is None:
            continue
        checked = len(values)
        mixed = pd.to_datetime(values, errors="coerce", format="mixed")
        failed = int(mixed.isna().sum())
        if failed / checked <= params.max_unparseable_fraction:
            continue
        dominant = _dominant_date_format(values)
        suggestion = (
            f"Most of these dates are written as '{dominant[0]}' ({dominant[1]:.0%} of them). Write "
            "the rest of the column the same way, then set that date format on the mapping screen."
            if dominant is not None
            else (
                "Use one standard date format for the whole column, such as 2026-08-01, and upload "
                "the file again."
            )
        )
        findings.append(
            OnboardingCheck(
                code="EVENT_TIME_UNPARSEABLE",
                severity=Severity.ERROR,
                message=(
                    f"{failed:,} of the {checked:,} dates in the {source.role} table could not be "
                    "read as a date."
                ),
                suggestion=suggestion,
                column=_EVENT_TIME,
                source_id=source.source_id,
                details={
                    "checked_rows": checked,
                    "unparsed_rows": failed,
                    "unparsed_fraction": round(failed / checked, 4),
                    "threshold": params.max_unparseable_fraction,
                    "suggested_format": None if dominant is None else dominant[0],
                },
            )
        )
    return tuple(findings)


def _dominant_date_format(values: pd.Series[Any]) -> tuple[str, float] | None:
    import pandas as pd

    best: tuple[str, float] | None = None
    for candidate in _DATE_FORMATS:
        rate = float(pd.to_datetime(values, errors="coerce", format=candidate).notna().mean())
        if rate > 0.0 and (best is None or rate > best[1]):
            best = (candidate, rate)
    return best


@_check("DATE_FORMAT_AMBIGUOUS")
def check_date_format_ambiguous(params: OnboardingCheckParams) -> tuple[OnboardingCheck, ...]:
    """Dates that read two ways, asked as a question with one of the client's own values in it.

    Raised as an ERROR although plan section 7 lists it as a warning, and never acknowledgeable: a
    silent guess moves up to eleven twelfths of a column by whole months, which no metric would ever
    show, and "confirmed" here means naming the format - one field on the mapping screen - rather
    than ticking a box that leaves the engine still guessing. Pinning `date_format` on the mapping
    answers the question, and the finding disappears.
    """
    import pandas as pd

    findings: list[OnboardingCheck] = []
    for source in params.sources:
        if source.event_time_format is not None:
            continue
        values = _event_time_text(source)
        if values is None:
            continue
        day_first = pd.to_datetime(values, errors="coerce", dayfirst=True, format="mixed")
        month_first = pd.to_datetime(values, errors="coerce", dayfirst=False, format="mixed")
        differs = day_first.notna() & month_first.notna() & (day_first != month_first)
        affected = int(differs.sum())
        if affected == 0:
            continue
        example = str(values[differs].iloc[0])
        as_day_first = pd.Timestamp(day_first[differs].iloc[0])
        as_month_first = pd.Timestamp(month_first[differs].iloc[0])
        findings.append(
            OnboardingCheck(
                code="DATE_FORMAT_AMBIGUOUS",
                severity=Severity.ERROR,
                message=(
                    f"Is {example} the {_ordinal(as_day_first.day)} of "
                    f"{calendar.month_name[as_day_first.month]} or the "
                    f"{_ordinal(as_month_first.day)} of {calendar.month_name[as_month_first.month]}? "
                    f"{affected:,} dates in the {source.role} table read differently depending on the "
                    "answer."
                ),
                suggestion=(
                    f"Set the date format of the {source.role} table's date column on the mapping "
                    "screen: '%d/%m/%Y' for day first, '%m/%d/%Y' for month first. The whole column "
                    "is then read that one way."
                ),
                column=_EVENT_TIME,
                source_id=source.source_id,
                details={
                    "affected_rows": affected,
                    "day_first": as_day_first.date().isoformat(),
                    "month_first": as_month_first.date().isoformat(),
                },
            )
        )
    return tuple(findings)


# ---------------------------------------------------------------------------
# Whether the dataset will carry what the use case asked for
# ---------------------------------------------------------------------------
@_check("REQUIRED_STANDARD_COLUMN_UNMAPPED")
def check_required_standard_column_unmapped(
    params: OnboardingCheckParams,
) -> tuple[OnboardingCheck, ...]:
    """A column the use case requires that no uploaded table was mapped to."""
    mapped = {name for source in params.sources for name in source.mapped_standard}
    return tuple(
        OnboardingCheck(
            code="REQUIRED_STANDARD_COLUMN_UNMAPPED",
            severity=Severity.ERROR,
            message=(f"This use case needs '{name}', and no column of any uploaded table is mapped to it."),
            suggestion=(
                f"Map a column to '{name}' on the mapping screen, or upload the table that holds it."
            ),
            column=name,
            details={"standard": name},
        )
        for name in params.required_standard_columns
        if name not in mapped
    )


@_check("ENTITY_ATTRIBUTES_NOT_TIME_VERSIONED")
def check_entity_attributes_not_time_versioned(
    params: OnboardingCheckParams,
) -> tuple[OnboardingCheck, ...]:
    """Periodic snapshots over an entity table with no as-of date: today's values, dated history.

    A warning rather than an error because most client masters are like this and the dataset is
    still worth building - but the user has to know that a plan change made last week is in the row
    for every snapshot of last year, which is the one thing about the dataset that no metric shows.
    """
    if params.snapshot_mode is not SnapshotMode.PERIODIC:
        return ()
    return tuple(
        OnboardingCheck(
            code="ENTITY_ATTRIBUTES_NOT_TIME_VERSIONED",
            severity=Severity.WARNING,
            message=(
                f"{params.entity.capitalize()} attributes are taken as they are today, not as they "
                f"were at each snapshot: the {params.entity} table has no column mapped to the date "
                "a row became true."
            ),
            suggestion=(
                "If your system keeps the history of these columns, upload the dated version and map "
                "its date; otherwise read features built from them as describing today."
            ),
            source_id=source.source_id,
            details={"snapshot_mode": params.snapshot_mode.value, "standard": _EVENT_TIME},
        )
        for source in _entity_sources(params)
        if _EVENT_TIME not in source.mapped_standard
    )


@_check("FEATURE_NAME_COLLISION")
def check_feature_name_collision(params: OnboardingCheckParams) -> tuple[OnboardingCheck, ...]:
    """Two dataset columns with one name: the second silently replaces the first when assembled.

    Counted over everything the built dataset will carry - the two names the engine owns, the
    entity table's mapped attributes, the features and the label - because a feature colliding with
    a mapped attribute is the collision that actually happens, and a feature list checked against
    itself would never see it.
    """
    names: list[str] = [_ENTITY_KEY, _SNAPSHOT_COLUMN]
    for source in _entity_sources(params):
        names.extend(name for name in source.mapped_standard if name not in {_ENTITY_KEY, _EVENT_TIME})
    names.extend(params.feature_names)
    if params.label_name:
        names.append(params.label_name)
    return tuple(
        OnboardingCheck(
            code="FEATURE_NAME_COLLISION",
            severity=Severity.ERROR,
            message=(
                f"{names.count(name)} columns of the built dataset would all be called '{name}', so "
                "only one of them would survive."
            ),
            suggestion=f"Rename the feature called '{name}' on the features screen.",
            column=name,
            details={"occurrences": names.count(name)},
        )
        for name in sorted({name for name in names if names.count(name) > 1})
    )


@_check("TOO_MANY_FEATURES")
def check_too_many_features(params: OnboardingCheckParams) -> tuple[OnboardingCheck, ...]:
    """More features than the limit: every one is a windowed aggregate over the whole event history."""
    count = len(params.feature_names)
    if count <= params.max_features:
        return ()
    return (
        OnboardingCheck(
            code="TOO_MANY_FEATURES",
            severity=Severity.ERROR,
            message=(
                f"The feature list has {count:,} features; this use case allows {params.max_features:,}."
            ),
            suggestion=(
                f"Remove {count - params.max_features:,} features, or narrow the list of windows, on "
                "the features screen."
            ),
            details={"feature_count": count, "max_features": params.max_features},
        ),
    )


@_check("FEATURE_ALL_NULL")
def check_feature_all_null(params: OnboardingCheckParams) -> tuple[OnboardingCheck, ...]:
    """A feature that is empty for almost every row, which is nearly always the recipe, not the data."""
    return tuple(
        OnboardingCheck(
            code="FEATURE_ALL_NULL",
            severity=Severity.WARNING,
            message=(
                f"The feature '{name}' is empty for {fraction:.0%} of rows, so it will be dropped "
                "before training."
            ),
            suggestion=(
                "A feature that is empty for almost every row usually means the window is too short "
                f"or the filter matches nothing. Widen the window or relax the filter on '{name}', or "
                "leave it out."
            ),
            column=name,
            details={
                "null_fraction": round(fraction, 4),
                "threshold": params.drop_if_null_fraction_above,
                "will_be_dropped": True,
            },
        )
        for name, fraction in sorted(params.feature_null_fractions.items())
        if fraction > params.drop_if_null_fraction_above
    )


@_check("FUTURE_EVENTS_LEAKED")
def check_future_events_leaked(params: OnboardingCheckParams) -> tuple[OnboardingCheck, ...]:
    """Events from after the snapshot date reached a feature: an engine fault, never the user's.

    The count is measured by the build stage against the queries it actually ran, because only it
    holds the connection. All this check does is say what the number means - and the model refuses
    to let it be acknowledged, because a dataset that has seen the future is not usable whoever
    signs it off.
    """
    return tuple(
        OnboardingCheck(
            code="FUTURE_EVENTS_LEAKED",
            severity=Severity.ERROR,
            message=(
                f"{rows:,} {role} events that had not happened yet at the snapshot date were counted "
                "into the features for that date."
            ),
            suggestion=(
                "This is a fault in the engine, not a problem with your data: do not use this "
                "dataset, and report the build id."
            ),
            details={"role": role, "rows": rows},
        )
        for role, rows in sorted(params.future_event_rows.items())
        if rows > 0
    )


# ---------------------------------------------------------------------------
# Registry and composition
# ---------------------------------------------------------------------------
CHECK_REGISTRY: Final[Mapping[str, CheckFn]] = MappingProxyType(
    {code: _REGISTRY[code] for code in CHECK_ORDER}
)
"""Every check this module owns, in plan section 7 table order. Built from `CHECK_ORDER` rather than
from registration order, so a code the table lists that nobody wrote a check for fails at import
rather than going quietly unchecked for a release."""


def run_onboarding_checks(params: OnboardingCheckParams) -> tuple[OnboardingCheck, ...]:
    """Run every check, errors first, and let none of them stop another.

    A check that raises is logged as its exception class name - never its message, which a pandas
    exception can quote a cell value into (plan section 13.7) - and contributes no finding: a build
    report that is missing one check is worth far more than no build report at all.
    """
    collected: list[OnboardingCheck] = []
    for code, fn in CHECK_REGISTRY.items():
        try:
            collected.extend(fn(params))
        except Exception as exc:  # no check may ever break a build report
            logger.warning("onboarding.check_failed code=%s error=%s", code, type(exc).__name__)
    return tuple(sorted(collected, key=_sort_key))


def _sort_key(finding: OnboardingCheck) -> tuple[int, int, str, str]:
    return (
        _SEVERITY_RANK[finding.severity],
        CHECK_ORDER.index(finding.code),
        finding.source_id or "",
        finding.column or "",
    )
