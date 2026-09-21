"""Validation stage (M2): every check of plan section 6.3 as a pure function of a DataFrame.

The architectural rule (M2_DESIGN section 2.1, DEC-065): **a check knows about data, not about a
request.** Every check takes a :class:`pandas.DataFrame` and a flat, frozen, fully-defaulted
:class:`CheckParams`, and returns a :class:`CheckResult`. It never sees a ``UseCaseConfig``, a
``Storage``, a ``DatasetProfile``, a run, an upload or the clock, so ``check(df, CheckParams())`` is
always legal and the same checks can later be re-run over engineered features.

Three layers compose them:

``run_checks``
    pure; derives the frame facts once, runs every check of a mode in table order, swallows any
    exception a check raises and sorts the findings (errors, then warnings, then info).
``validate_frame``
    the generic entry point: ``run_checks``, then acknowledgements, then the counted report.
``validate_for_training`` / ``validate_against_schema``
    the run-level conveniences the API calls. ``params_from_config`` is the only place a
    ``UseCaseConfig`` is read.

Nothing here raises on bad data (plan section 6.3) and nothing here logs a data value (plan
section 13.7). Heavy libraries are imported inside function bodies so ``import engine`` stays fast.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final, Protocol, TypeAlias, cast

from engine.config import ColumnRole, ColumnType, PiiHandling, ProblemType, RunMode, SplitType
from engine.contracts import Severity, ValidationCheck, ValidationReport
from engine.utils.ids import seed_from
from engine.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence
    from datetime import datetime

    import pandas as pd

    from engine.config import UseCaseConfig
    from engine.contracts import FeatureSchema


__all__ = [
    "BASELINE_FOLDS",
    "BASELINE_MAX_CATEGORIES",
    "BASELINE_MAX_FEATURES",
    "BASELINE_MIN_CLASS_ROWS",
    "CHECKS_BY_CODE",
    "CHECK_ORDER",
    "CHECK_REGISTRY",
    "DATETIME_PARSE_RATE",
    "ID_DISTINCT_RATIO",
    "ID_LIKE_PATTERN",
    "ID_MIN_ROWS",
    "LEAKAGE_AFTER_TARGET_PATTERN",
    "LEAKAGE_MAX_CATEGORIES",
    "LEAKAGE_MIN_ROWS",
    "LEAKAGE_PATTERN",
    "LEAKAGE_SAMPLE_ROWS",
    "PII_KIND_LABELS",
    "POSITIVE_TOKENS",
    "REDACTED",
    "SEVERITY_RANK",
    "TIME_LIKE_PATTERN",
    "VALUE_COUNTS_MAX_DISTINCT",
    "CheckFn",
    "CheckParams",
    "CheckResult",
    "CheckSpec",
    "ColumnStatsLike",
    "FrameFacts",
    "baseline_auc",
    "candidate_columns",
    "check",
    "check_consent_column_missing",
    "check_constant_column",
    "check_high_cardinality_id_like",
    "check_high_null_column",
    "check_leakage_suspected",
    "check_pii_detected",
    "check_pk_missing",
    "check_pk_not_unique",
    "check_pk_nulls",
    "check_rows_too_few",
    "check_schema_mismatch",
    "check_suppression_column_missing",
    "check_target_constant",
    "check_target_imbalance_severe",
    "check_target_missing",
    "check_target_not_binary",
    "check_target_too_few_positives",
    "check_time_column_missing",
    "check_time_column_unparseable",
    "checks_for",
    "column_stats",
    "derive_facts",
    "facts_for",
    "key_candidates",
    "leakage_exempt_names",
    "leakage_sample",
    "looks_like_id",
    "params_from_config",
    "resolve_positive_label",
    "run_checks",
    "sample_values",
    "single_feature_auc",
    "target_candidate_in",
    "time_candidates",
    "types_compatible",
    "validate_against_schema",
    "validate_for_training",
    "validate_frame",
    "validation_detail",
    "value_counts_for",
]


logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Module constants (M2_DESIGN sections 2.1, 2.5, 2.25)
# ---------------------------------------------------------------------------
#: Catalog defaults, repeated here so `CheckParams()` needs no config and no catalog read.
TIME_LIKE_PATTERN: Final[str] = "date|time|month|week|day|_ts$|_at$"
LEAKAGE_PATTERN: Final[str] = r"^(churn|converted|outcome)"
LEAKAGE_AFTER_TARGET_PATTERN: Final[str] = r"_date$"
ID_LIKE_PATTERN: Final[str] = r"(^id$|_id$|^id_|_key$|customer|cust)"

#: Leakage sampling and branch limits.
LEAKAGE_SAMPLE_ROWS: Final[int] = 50_000
LEAKAGE_MIN_ROWS: Final[int] = 100
LEAKAGE_MAX_CATEGORIES: Final[int] = 50
BASELINE_MIN_CLASS_ROWS: Final[int] = 30
BASELINE_MAX_FEATURES: Final[int] = 200
BASELINE_FOLDS: Final[int] = 3
BASELINE_MAX_CATEGORIES: Final[int] = 20

#: `looks_like_id` (M2_DESIGN section 1.6, recomputed from facts rather than from a profile).
ID_MIN_ROWS: Final[int] = 20
ID_DISTINCT_RATIO: Final[float] = 0.99

#: Share of non-null values that must parse as dates before a column counts as a date.
DATETIME_PARSE_RATE: Final[float] = 0.95
#: Share of non-null values that must parse as numbers before a column counts as numeric.
NUMERIC_PARSE_RATE: Final[float] = 0.99
#: Mean character length above which a string column is TEXT rather than STRING.
TEXT_MEAN_LENGTH: Final[float] = 50.0
#: Distinct-count ceiling below which `value_counts_for` counts a column exactly.
VALUE_COUNTS_MAX_DISTINCT: Final[int] = 10

REDACTED: Final[str] = "[REDACTED]"

#: Errors before warnings before info (M2_DESIGN section 2.3).
SEVERITY_RANK: Final[Mapping[Severity, int]] = {
    Severity.ERROR: 0,
    Severity.WARNING: 1,
    Severity.INFO: 2,
}

PII_KIND_LABELS: Final[Mapping[str, str]] = {
    "email": "email addresses",
    "phone": "phone numbers",
    "pan": "PAN numbers",
    "aadhaar": "Aadhaar numbers",
    "name": "personal names",
}

POSITIVE_TOKENS: Final[tuple[str, ...]] = (
    "1",
    "true",
    "t",
    "yes",
    "y",
    "churned",
    "converted",
    "late",
    "delayed",
    "fault",
    "reactivated",
)

#: plan section 6.3 table order, verbatim, plus DEC-030's code.
CHECK_ORDER: Final[tuple[str, ...]] = (
    "PK_MISSING",
    "PK_NOT_UNIQUE",
    "PK_NULLS",
    "TARGET_MISSING",
    "TARGET_NOT_BINARY",
    "TARGET_CONSTANT",
    "TARGET_TOO_FEW_POSITIVES",
    "TARGET_IMBALANCE_SEVERE",
    "ROWS_TOO_FEW",
    "LEAKAGE_SUSPECTED",
    "TIME_COLUMN_MISSING",
    "TIME_COLUMN_UNPARSEABLE",
    "HIGH_NULL_COLUMN",
    "CONSTANT_COLUMN",
    "HIGH_CARDINALITY_ID_LIKE",
    "PII_DETECTED",
    "SCHEMA_MISMATCH",
    "CONSENT_COLUMN_MISSING",
    "SUPPRESSION_COLUMN_MISSING",
)

_NUMERIC_TYPES: Final[frozenset[ColumnType]] = frozenset(
    {ColumnType.INTEGER, ColumnType.FLOAT, ColumnType.BOOLEAN}
)
_TEMPORAL_TYPES: Final[frozenset[ColumnType]] = frozenset({ColumnType.DATE, ColumnType.DATETIME})
_TEXTUAL_TYPES: Final[frozenset[ColumnType]] = frozenset({ColumnType.STRING, ColumnType.TEXT})
_ID_TYPES: Final[frozenset[ColumnType]] = frozenset({ColumnType.STRING, ColumnType.TEXT, ColumnType.INTEGER})

_BOOL_TRUE_TOKENS: Final[frozenset[str]] = frozenset({"true", "t", "yes", "y", "1"})
_BOOL_FALSE_TOKENS: Final[frozenset[str]] = frozenset({"false", "f", "no", "n", "0"})
_BOOL_TOKENS: Final[frozenset[str]] = _BOOL_TRUE_TOKENS | _BOOL_FALSE_TOKENS

_BOTH_MODES: Final[frozenset[RunMode]] = frozenset(RunMode)
_TRAIN_ONLY: Final[frozenset[RunMode]] = frozenset({RunMode.TRAIN})
_SCORE_ONLY: Final[frozenset[RunMode]] = frozenset({RunMode.SCORE})


# ---------------------------------------------------------------------------
# Value objects (M2_DESIGN section 2.1)
# ---------------------------------------------------------------------------
class ColumnStatsLike(Protocol):
    """The read-only shape of `engine.stages.ingest.ColumnStats`.

    Declared structurally so this module keeps a single runtime dependency on ingest (type
    inference and PII detection) and none at all on the exact-second-pass record.
    """

    @property
    def row_count(self) -> int: ...

    @property
    def null_count(self) -> int: ...

    @property
    def distinct_count(self) -> int: ...

    @property
    def is_unique(self) -> bool: ...

    @property
    def value_counts(self) -> Mapping[str, int]: ...


@dataclass(frozen=True, slots=True)
class FrameFacts:
    """A pure memo over a frame: the O(rows) facts several checks share.

    It can never disagree with the frame, because it is a function of the frame alone.
    """

    columns: tuple[str, ...]
    row_count: int
    types: Mapping[str, ColumnType]
    null_counts: Mapping[str, int]
    distinct_counts: Mapping[str, int]
    is_unique: Mapping[str, bool]
    pii_kinds: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class CheckParams:
    """Flat, scalar, frozen. EVERY field has a default, so ``check(df, CheckParams())`` is legal."""

    # --- column roles (names only; the frame supplies the values) ---------------
    primary_key: str | None = None
    target: str | None = None
    time_column: str | None = None
    group_column: str | None = None
    consent_column: str | None = None
    opt_out_column: str | None = None
    recently_contacted_column: str | None = None
    fairness_column: str | None = None
    excluded_columns: tuple[str, ...] = ()

    # --- thresholds and switches (each named exactly like its config leaf) ------
    entity: str = "customer"
    problem_type: ProblemType = ProblemType.BINARY_CLASSIFICATION
    positive_label: str | int | bool | None = None
    min_rows: int = 1_000
    min_positive: int = 200
    imbalance_warn_min_rate: float = 0.01
    imbalance_warn_max_rate: float = 0.99
    high_null_column_rate: float = 0.60
    leakage_check: bool = True
    leakage_baseline: bool = True
    leakage_auc_threshold: float = 0.98
    split_type: SplitType = SplitType.RANDOM_STRATIFIED
    pii_handling: PiiHandling = PiiHandling.REDACT
    suppress_opted_out: bool = True
    suppress_recently_contacted: bool = True

    # --- patterns, so no check ever reaches for the catalog ---------------------
    time_like_pattern: str = TIME_LIKE_PATTERN
    leakage_pattern: str = LEAKAGE_PATTERN
    leakage_after_target_pattern: str = LEAKAGE_AFTER_TARGET_PATTERN
    id_like_pattern: str = ID_LIKE_PATTERN

    # --- target vocabulary, so the helpers stay pure ----------------------------
    target_aliases: tuple[str, ...] = ()
    target_definition: str = ""
    target_label_source: str = ""

    # --- determinism and cost ---------------------------------------------------
    seed: int = 0
    sample_rows: int = LEAKAGE_SAMPLE_ROWS

    # --- optional facts, never required -----------------------------------------
    row_count: int | None = None
    schema: FeatureSchema | None = None
    exact: Mapping[str, ColumnStatsLike] | None = None
    facts: FrameFacts | None = None


@dataclass(frozen=True, slots=True)
class CheckResult:
    """What one check found. Never an exception, never a partial ValidationReport."""

    code: str
    findings: tuple[ValidationCheck, ...] = ()
    skipped: bool = False
    skip_reason: str = ""

    @property
    def passed(self) -> bool:
        return not self.findings


CheckFn: TypeAlias = "Callable[[pd.DataFrame, CheckParams], CheckResult]"


@dataclass(frozen=True, slots=True)
class CheckSpec:
    """One registered check: what it emits, when it runs and where it sits in the table."""

    code: str
    fn: CheckFn
    severity: Severity
    modes: frozenset[RunMode]
    order: int
    acknowledgeable: bool
    needs_target: bool


_REGISTRY: list[CheckSpec] = []


def check(
    code: str,
    *,
    severity: Severity,
    modes: frozenset[RunMode],
    acknowledgeable: bool = False,
    needs_target: bool = False,
) -> Callable[[CheckFn], CheckFn]:
    """Register a check and return it unchanged, so it stays directly importable and callable."""

    def decorate(fn: CheckFn) -> CheckFn:
        _REGISTRY.append(
            CheckSpec(
                code=code,
                fn=fn,
                severity=severity,
                modes=modes,
                order=CHECK_ORDER.index(code),
                acknowledgeable=acknowledgeable,
                needs_target=needs_target,
            )
        )
        return fn

    return decorate


# ---------------------------------------------------------------------------
# Frame facts (M2_DESIGN section 2.1)
# ---------------------------------------------------------------------------
_InferFn: TypeAlias = "Callable[[pd.Series], ColumnType]"
_DetectPiiFn: TypeAlias = "Callable[[pd.Series, str, ColumnType], tuple[str, ...]]"


def _ingest_helpers() -> tuple[_InferFn, _DetectPiiFn]:
    """`infer_column_type` and `detect_pii` for `derive_facts`, both from the ingest stage.

    PII detection has exactly **one** definition in the engine - `engine.stages.ingest.detect_pii`
    (M2_DESIGN section 1.5) - and this module calls it rather than keeping a second copy of the
    detector table. A rule about people's data must have one behaviour: a second table would be
    dormant while ingest exists and would resurface the moment it did not, with whatever guards
    the real one has since grown (the distinct-ratio guard that stops an ordinary low-cardinality
    category such as `gender` being read as a roster of personal names is exactly such a guard).
    The import is a function-body import of a module that never imports this one, so the engine's
    import graph stays the DAG `tests/integration/test_engine_imports.py` pins.

    Type inference keeps a local equivalent (:func:`_infer_column_type`, M2_DESIGN section 1.4)
    for the case where `ingest` is still a stub in another worktree; ingest's own function wins
    whenever it exists, so the two can never both be live.
    """
    from engine.stages import ingest

    infer = cast("_InferFn | None", getattr(ingest, "infer_column_type", None))
    return (_infer_column_type if infer is None else infer), ingest.detect_pii


def derive_facts(frame: pd.DataFrame) -> FrameFacts:
    """A pure function of the frame alone. Same frame, same facts, always."""
    infer, detect = _ingest_helpers()
    columns = tuple(str(name) for name in frame.columns)
    rows = len(frame)
    types: dict[str, ColumnType] = {}
    nulls: dict[str, int] = {}
    distincts: dict[str, int] = {}
    unique: dict[str, bool] = {}
    pii: dict[str, tuple[str, ...]] = {}
    for name in columns:
        series = frame[name]
        inferred = infer(series)
        null_count = int(series.isna().sum())
        distinct = int(series.nunique(dropna=True))
        types[name] = inferred
        nulls[name] = null_count
        distincts[name] = distinct
        unique[name] = distinct == rows - null_count and null_count < rows
        pii[name] = detect(series, name, inferred)
    return FrameFacts(
        columns=columns,
        row_count=rows,
        types=types,
        null_counts=nulls,
        distinct_counts=distincts,
        is_unique=unique,
        pii_kinds=pii,
    )


def facts_for(frame: pd.DataFrame, params: CheckParams) -> FrameFacts:
    """`params.facts` when the caller already computed them, else `derive_facts(frame)`."""
    return params.facts if params.facts is not None else derive_facts(frame)


# ---------------------------------------------------------------------------
# Fallback type inference (M2_DESIGN section 1.4)
# ---------------------------------------------------------------------------
def _infer_column_type(series: pd.Series) -> ColumnType:
    """M2_DESIGN section 1.4's branch table, first match wins."""
    import pandas as pd

    values = series.dropna()
    if len(values) == 0:
        return ColumnType.STRING
    distinct = int(values.nunique(dropna=True))
    if pd.api.types.is_bool_dtype(series):
        return ColumnType.BOOLEAN
    if pd.api.types.is_datetime64_any_dtype(series):
        return ColumnType.DATE if bool((values.dt.normalize() == values).all()) else ColumnType.DATETIME
    if pd.api.types.is_integer_dtype(series):
        if distinct == 2 and set(values.unique()) <= {0, 1}:
            return ColumnType.BOOLEAN
        return ColumnType.INTEGER
    if pd.api.types.is_float_dtype(series):
        if distinct == 2 and set(values.unique()) <= {0.0, 1.0}:
            return ColumnType.BOOLEAN
        whole = bool((values % 1 == 0).all()) and float(values.abs().max()) < 2**53
        return ColumnType.INTEGER if whole else ColumnType.FLOAT
    text = values.astype(str)
    tokens = {str(v).strip().lower() for v in values.unique()}
    if (
        distinct == 2
        and tokens <= _BOOL_TOKENS
        and bool(tokens & _BOOL_TRUE_TOKENS)
        and bool(tokens & _BOOL_FALSE_TOKENS)
    ):
        return ColumnType.BOOLEAN
    numeric = pd.to_numeric(values, errors="coerce")
    if distinct > 2 and float(numeric.notna().mean()) >= NUMERIC_PARSE_RATE:
        parsed = numeric.dropna()
        return ColumnType.INTEGER if bool((parsed % 1 == 0).all()) else ColumnType.FLOAT
    stamps = pd.to_datetime(text, errors="coerce", format="mixed")
    if float(stamps.notna().mean()) >= DATETIME_PARSE_RATE:
        kept = stamps.dropna()
        return ColumnType.DATE if bool((kept.dt.normalize() == kept).all()) else ColumnType.DATETIME
    if float(text.str.len().mean()) > TEXT_MEAN_LENGTH:
        return ColumnType.TEXT
    return ColumnType.STRING


# ---------------------------------------------------------------------------
# Shared helpers (M2_DESIGN section 2.5) - all pure
# ---------------------------------------------------------------------------
def _cell_str(value: object) -> str:
    """One cell as the user's file held it. `""` for null; never a repr, never longer than 200."""
    import pandas as pd

    if value is None or value is pd.NaT or value is pd.NA:
        return ""
    if pd.api.types.is_bool(value):
        return "true" if value else "false"
    if isinstance(value, pd.Timestamp):
        rendered = value.date().isoformat() if value == value.normalize() else value.isoformat()
    elif isinstance(value, float):
        if value != value:  # NaN is the only float unequal to itself
            return ""
        rendered = str(int(value)) if value.is_integer() else str(value)
    else:
        rendered = str(value)
        if rendered.endswith(".0") and rendered[:-2].lstrip("-").isdigit():
            rendered = rendered[:-2]
    return rendered if len(rendered) <= 200 else rendered[:199] + "…"


def _norm(value: object) -> str:
    return _cell_str(value).strip().lower()


def _effective_rows(params: CheckParams, facts: FrameFacts) -> int:
    """The FILE's row count when the frame is a capped read, else the frame's."""
    return facts.row_count if params.row_count is None else params.row_count


def column_stats(
    frame: pd.DataFrame,
    params: CheckParams,
    name: str,
    facts: FrameFacts | None = None,
) -> tuple[int, int, int, bool]:
    """`(rows, null_count, distinct_count, is_unique)` for one column.

    Reads `params.exact[name]` when an exact second pass ran over a capped read, else the facts, so
    no check can accidentally trust a sample.
    """
    resolved = facts_for(frame, params) if facts is None else facts
    exact = None if params.exact is None else params.exact.get(name)
    if exact is not None:
        return (exact.row_count, exact.null_count, exact.distinct_count, exact.is_unique)
    return (
        resolved.row_count,
        resolved.null_counts.get(name, 0),
        resolved.distinct_counts.get(name, 0),
        resolved.is_unique.get(name, False),
    )


def value_counts_for(
    frame: pd.DataFrame,
    params: CheckParams,
    name: str,
    facts: FrameFacts | None = None,
) -> dict[str, int] | None:
    """Exact stringified counts for a low-cardinality column, else `None`."""
    resolved = facts_for(frame, params) if facts is None else facts
    exact = None if params.exact is None else params.exact.get(name)
    if exact is not None and exact.value_counts:
        return dict(exact.value_counts)
    if name not in resolved.columns or resolved.distinct_counts.get(name, 0) > VALUE_COUNTS_MAX_DISTINCT:
        return None
    counts: dict[str, int] = {}
    for value, count in frame[name].value_counts(dropna=True).items():
        key = _cell_str(value)
        counts[key] = counts.get(key, 0) + int(count)
    return counts


def sample_values(
    frame: pd.DataFrame,
    name: str,
    facts: FrameFacts,
    limit: int = 5,
) -> tuple[str, ...]:
    """Up to `limit` stringified non-null values in file order, or `[REDACTED]` for a PII column."""
    if name not in facts.columns:
        return ()
    kept = frame[name].dropna().head(limit)
    if facts.pii_kinds.get(name):
        return (REDACTED,) * len(kept)
    return tuple(_cell_str(value) for value in kept.tolist())


def looks_like_id(facts: FrameFacts, name: str) -> bool:
    """Distinct on nearly every row, complete, and of an identifier-shaped type."""
    rows = facts.row_count
    if name not in facts.columns or rows < ID_MIN_ROWS:
        return False
    if facts.null_counts.get(name, 0) != 0 or not facts.is_unique.get(name, False):
        return False
    if facts.types.get(name) not in _ID_TYPES:
        return False
    return facts.distinct_counts.get(name, 0) >= ID_DISTINCT_RATIO * rows


def key_candidates(facts: FrameFacts, params: CheckParams) -> tuple[str, ...]:
    """Unique, non-null columns, id-like names first, then file position."""
    pattern = re.compile(params.id_like_pattern, re.IGNORECASE)
    scored: list[tuple[int, int, str]] = []
    for position, name in enumerate(facts.columns):
        if not facts.is_unique.get(name, False) or facts.null_counts.get(name, 0) != 0:
            continue
        scored.append((0 if pattern.search(name) else 1, position, name))
    return tuple(name for _, _, name in sorted(scored))


def time_candidates(facts: FrameFacts, params: CheckParams) -> tuple[str, ...]:
    """Columns whose name is time-like AND whose values parse as dates."""
    pattern = re.compile(params.time_like_pattern, re.IGNORECASE)
    return tuple(
        name for name in facts.columns if pattern.search(name) and facts.types.get(name) in _TEMPORAL_TYPES
    )


def target_candidate_in(facts: FrameFacts, params: CheckParams) -> str | None:
    """The first of `params.target_aliases` present in the frame, else `None`."""
    lowered = {name.lower(): name for name in facts.columns}
    for alias in params.target_aliases:
        if alias in facts.columns:
            return alias
        found = lowered.get(alias.lower())
        if found is not None:
            return found
    return None


def resolve_positive_label(
    frame: pd.DataFrame,
    params: CheckParams,
    facts: FrameFacts | None = None,
) -> tuple[str | None, int, int]:
    """`(label, positives, negatives)` for a two-valued target (DEC-056).

    `(None, 0, 0)` when the target is absent, constant or has more than two values; the caller then
    emits TARGET_MISSING / TARGET_CONSTANT / TARGET_NOT_BINARY instead.
    """
    resolved = facts_for(frame, params) if facts is None else facts
    target = params.target
    if not target or target not in resolved.columns:
        return (None, 0, 0)
    counts = value_counts_for(frame, params, target, resolved)
    if counts is None or len(counts) != 2:
        return (None, 0, 0)
    values = sorted(counts)
    label: str | None = None
    if params.positive_label is not None:
        wanted = _norm(params.positive_label)
        label = next((value for value in values if value.strip().lower() == wanted), None)
    if label is None:
        label = next((value for value in values if value.strip().lower() in POSITIVE_TOKENS), None)
    if label is None:
        low, high = values
        tie = counts[low] == counts[high]
        label = high if tie or counts[high] < counts[low] else low
    positives = counts[label]
    negatives = sum(count for value, count in counts.items() if value != label)
    return (label, positives, negatives)


def _join_labels(labels: Sequence[str]) -> str:
    if len(labels) <= 1:
        return labels[0] if labels else ""
    return f"{', '.join(labels[:-1])} and {labels[-1]}"


def _finding(
    code: str,
    severity: Severity,
    message: str,
    suggestion: str,
    *,
    column: str | None = None,
    details: Mapping[str, object] | None = None,
    acknowledgeable: bool = False,
) -> ValidationCheck:
    return ValidationCheck(
        code=code,
        severity=severity,
        message=message,
        suggestion=suggestion,
        column=column,
        details=dict(details or {}),
        acknowledgeable=acknowledgeable,
    )


# ---------------------------------------------------------------------------
# 2.6 - 2.24 The checks
# ---------------------------------------------------------------------------
@check("PK_MISSING", severity=Severity.ERROR, modes=_BOTH_MODES)
def check_pk_missing(frame: pd.DataFrame, params: CheckParams) -> CheckResult:
    """The primary key was not chosen, or the chosen name is not in the file."""
    facts = facts_for(frame, params)
    key = params.primary_key
    if key and key in facts.columns:
        return CheckResult(code="PK_MISSING")
    candidates = key_candidates(facts, params)
    if not key:
        suggestion = (
            f"Pick one of: {', '.join(candidates[:3])}."
            if candidates
            else f"No column in this file is unique and complete, so no column can identify a {params.entity}."
        )
    else:
        suggestion = f"The file has no column called '{key}'."
    return CheckResult(
        code="PK_MISSING",
        findings=(
            _finding(
                "PK_MISSING",
                Severity.ERROR,
                f"Choose the column that identifies each {params.entity}.",
                suggestion,
                details={"selected": key, "candidates": list(candidates[:5]), "present": False},
            ),
        ),
    )


@check("PK_NOT_UNIQUE", severity=Severity.ERROR, modes=_BOTH_MODES)
def check_pk_not_unique(frame: pd.DataFrame, params: CheckParams) -> CheckResult:
    """The key repeats, so the file holds more than one row per entity."""
    facts = facts_for(frame, params)
    key = params.primary_key
    if not key or key not in facts.columns:
        return CheckResult(code="PK_NOT_UNIQUE", skipped=True, skip_reason="no primary key column")
    rows, nulls, distinct, unique = column_stats(frame, params, key, facts)
    if unique or distinct == 0:
        return CheckResult(code="PK_NOT_UNIQUE")
    present = rows - nulls
    per_entity = present / distinct
    return CheckResult(
        code="PK_NOT_UNIQUE",
        findings=(
            _finding(
                "PK_NOT_UNIQUE",
                Severity.ERROR,
                f"This file has {per_entity:.1f} rows per {params.entity} on average. "
                f"The model needs one row per {params.entity}.",
                f"Combine the rows so each {params.entity} appears once, or upload a file that "
                f"already has one row per {params.entity}.",
                column=key,
                details={
                    "rows": rows,
                    "distinct_keys": distinct,
                    "duplicate_rows": present - distinct,
                    "rows_per_entity": round(per_entity, 2),
                    "sampled": params.exact is None
                    and params.row_count is not None
                    and params.row_count > facts.row_count,
                },
            ),
        ),
    )


@check("PK_NULLS", severity=Severity.ERROR, modes=_BOTH_MODES)
def check_pk_nulls(frame: pd.DataFrame, params: CheckParams) -> CheckResult:
    """Rows with no identifier cannot be joined back to the customer's systems."""
    facts = facts_for(frame, params)
    key = params.primary_key
    if not key or key not in facts.columns:
        return CheckResult(code="PK_NULLS", skipped=True, skip_reason="no primary key column")
    rows, nulls, _distinct, _unique = column_stats(frame, params, key, facts)
    if nulls == 0 or rows == 0:
        return CheckResult(code="PK_NULLS")
    return CheckResult(
        code="PK_NULLS",
        findings=(
            _finding(
                "PK_NULLS",
                Severity.ERROR,
                f"{nulls:,} rows have no {params.entity} identifier, so their predictions could not "
                "be joined back to your systems.",
                f"Fill in the missing '{key}' values, or remove those rows, and upload again.",
                column=key,
                details={"null_count": nulls, "null_rate": round(nulls / rows, 4), "rows": rows},
            ),
        ),
    )


@check("TARGET_MISSING", severity=Severity.ERROR, modes=_TRAIN_ONLY)
def check_target_missing(frame: pd.DataFrame, params: CheckParams) -> CheckResult:
    """No outcome column was chosen, or the chosen name is not in the file."""
    facts = facts_for(frame, params)
    target = params.target
    if target and target in facts.columns:
        return CheckResult(code="TARGET_MISSING")
    definition = params.target_definition
    if target:
        message = (
            f"This file has no column called '{target}'. "
            "Choose the column that records the outcome to learn."
        )
    elif definition:
        message = f"Choose the column that records the outcome to learn: {definition}."
    else:
        message = "Choose the column that records the outcome to learn."
    expected = params.target_aliases[0] if params.target_aliases else None
    candidate = target_candidate_in(facts, params)
    parts: list[str] = []
    if expected:
        parts.append(
            f"The template for this use case calls it '{expected}'. "
            "Download the template to see the expected columns."
        )
    if candidate is not None:
        parts.append(f"This file has a column called '{candidate}' - is that it?")
    return CheckResult(
        code="TARGET_MISSING",
        findings=(
            _finding(
                "TARGET_MISSING",
                Severity.ERROR,
                message,
                " ".join(parts),
                details={
                    "selected": target,
                    "expected": expected,
                    "candidate": candidate,
                    "label_source": params.target_label_source,
                },
            ),
        ),
    )


@check("TARGET_NOT_BINARY", severity=Severity.ERROR, modes=_TRAIN_ONLY, needs_target=True)
def check_target_not_binary(frame: pd.DataFrame, params: CheckParams) -> CheckResult:
    """A yes/no model needs exactly two outcome values."""
    facts = facts_for(frame, params)
    target = params.target
    if not target or target not in facts.columns:
        return CheckResult(code="TARGET_NOT_BINARY", skipped=True, skip_reason="no target column")
    if params.problem_type is not ProblemType.BINARY_CLASSIFICATION:
        return CheckResult(code="TARGET_NOT_BINARY", skipped=True, skip_reason="not a binary problem")
    _rows, _nulls, distinct, _unique = column_stats(frame, params, target, facts)
    if distinct <= 2:
        return CheckResult(code="TARGET_NOT_BINARY")
    numeric = facts.types.get(target) in {ColumnType.INTEGER, ColumnType.FLOAT}
    definition = params.target_definition
    if numeric:
        suggestion = (
            "This column holds numbers, so you can switch the problem type to Regression "
            "(a number) instead."
        )
    else:
        suggestion = (
            f"Pick the column that records the outcome as two values, or derive one "
            f"(for example, 1 when {definition or 'the event happened'} and 0 otherwise)."
        )
    return CheckResult(
        code="TARGET_NOT_BINARY",
        findings=(
            _finding(
                "TARGET_NOT_BINARY",
                Severity.ERROR,
                f"'{target}' has {distinct:,} different values. A yes/no model needs exactly two.",
                suggestion,
                column=target,
                details={
                    "distinct_count": distinct,
                    "numeric": numeric,
                    "switch_to": "regression" if numeric else None,
                    "override_path": "problem_type",
                    "override_value": "regression" if numeric else None,
                    "sample_values": list(sample_values(frame, target, facts)),
                },
            ),
        ),
    )


@check("TARGET_CONSTANT", severity=Severity.ERROR, modes=_TRAIN_ONLY, needs_target=True)
def check_target_constant(frame: pd.DataFrame, params: CheckParams) -> CheckResult:
    """Every row carries the same outcome, so there is nothing to tell apart."""
    facts = facts_for(frame, params)
    target = params.target
    if not target or target not in facts.columns:
        return CheckResult(code="TARGET_CONSTANT", skipped=True, skip_reason="no target column")
    rows, nulls, distinct, _unique = column_stats(frame, params, target, facts)
    if distinct > 1 or rows == 0:
        return CheckResult(code="TARGET_CONSTANT")
    if distinct == 0:
        message = f"'{target}' is empty in every row, so there is no outcome to learn."
        value: str | None = None
    else:
        message = (
            f"Every row has the same value in '{target}', so there is nothing for the model " "to tell apart."
        )
        samples = sample_values(frame, target, facts, limit=1)
        value = samples[0] if samples else None
    return CheckResult(
        code="TARGET_CONSTANT",
        findings=(
            _finding(
                "TARGET_CONSTANT",
                Severity.ERROR,
                message,
                "Upload data that contains both outcomes - rows where the event happened and rows "
                "where it did not.",
                column=target,
                details={"distinct_count": distinct, "value": value, "null_count": nulls},
            ),
        ),
    )


@check("TARGET_TOO_FEW_POSITIVES", severity=Severity.ERROR, modes=_TRAIN_ONLY, needs_target=True)
def check_target_too_few_positives(frame: pd.DataFrame, params: CheckParams) -> CheckResult:
    """Too few examples of the event for a model to learn it reliably."""
    facts = facts_for(frame, params)
    label, positives, negatives = resolve_positive_label(frame, params, facts)
    if label is None:
        return CheckResult(
            code="TARGET_TOO_FEW_POSITIVES", skipped=True, skip_reason="target is not two-valued"
        )
    if positives >= params.min_positive:
        return CheckResult(code="TARGET_TOO_FEW_POSITIVES")
    total = positives + negatives
    return CheckResult(
        code="TARGET_TOO_FEW_POSITIVES",
        findings=(
            _finding(
                "TARGET_TOO_FEW_POSITIVES",
                Severity.ERROR,
                f"Only {positives:,} positive examples. "
                f"At least {params.min_positive:,} are needed for a reliable model.",
                f"Add more history, or widen the outcome window in the definition of " f"'{params.target}'.",
                column=params.target,
                details={
                    "positive_label": label,
                    "positive_count": positives,
                    "negative_count": negatives,
                    "min_positive": params.min_positive,
                    "positive_rate": round(positives / total, 4) if total else 0.0,
                    "override_path": "validation.min_positive",
                },
            ),
        ),
    )


@check(
    "TARGET_IMBALANCE_SEVERE",
    severity=Severity.WARNING,
    modes=_TRAIN_ONLY,
    acknowledgeable=True,
    needs_target=True,
)
def check_target_imbalance_severe(frame: pd.DataFrame, params: CheckParams) -> CheckResult:
    """One class is so rare that accuracy stops meaning anything."""
    facts = facts_for(frame, params)
    label, positives, negatives = resolve_positive_label(frame, params, facts)
    total = positives + negatives
    if label is None or total == 0:
        return CheckResult(
            code="TARGET_IMBALANCE_SEVERE", skipped=True, skip_reason="target is not two-valued"
        )
    rate = positives / total
    if params.imbalance_warn_min_rate <= rate <= params.imbalance_warn_max_rate:
        return CheckResult(code="TARGET_IMBALANCE_SEVERE")
    if rate < params.imbalance_warn_min_rate:
        message = (
            f"Only {rate:.2%} of rows are positive. "
            "The model can look accurate while finding almost none of them."
        )
    else:
        message = (
            f"{rate:.2%} of rows are positive. "
            "The model can look accurate while finding almost none of the negatives."
        )
    return CheckResult(
        code="TARGET_IMBALANCE_SEVERE",
        findings=(
            _finding(
                "TARGET_IMBALANCE_SEVERE",
                Severity.WARNING,
                message,
                "Training continues. Judge it on PR-AUC and recall rather than accuracy, and "
                "consider setting Class imbalance to 'Class weights' in Model search.",
                column=params.target,
                details={
                    "positive_rate": round(rate, 4),
                    "positive_count": positives,
                    "negative_count": negatives,
                    "min_rate": params.imbalance_warn_min_rate,
                    "max_rate": params.imbalance_warn_max_rate,
                    "override_path": "model_search.imbalance",
                    "override_value": "class_weights",
                },
                acknowledgeable=True,
            ),
        ),
    )


@check("ROWS_TOO_FEW", severity=Severity.ERROR, modes=_TRAIN_ONLY)
def check_rows_too_few(frame: pd.DataFrame, params: CheckParams) -> CheckResult:
    """Too little history to train a model that generalises."""
    facts = facts_for(frame, params)
    rows = _effective_rows(params, facts)
    if rows >= params.min_rows:
        return CheckResult(code="ROWS_TOO_FEW")
    return CheckResult(
        code="ROWS_TOO_FEW",
        findings=(
            _finding(
                "ROWS_TOO_FEW",
                Severity.ERROR,
                f"This file has {rows:,} rows. "
                f"At least {params.min_rows:,} are needed to train a model that generalises.",
                f"Upload a longer history - more {params.entity}s, or more snapshot dates.",
                details={"rows": rows, "min_rows": params.min_rows},
            ),
        ),
    )


@check("TIME_COLUMN_MISSING", severity=Severity.ERROR, modes=_TRAIN_ONLY)
def check_time_column_missing(frame: pd.DataFrame, params: CheckParams) -> CheckResult:
    """A time-based split was requested but there is no column that says when a row was taken."""
    facts = facts_for(frame, params)
    if params.split_type is not SplitType.TIME_BASED:
        return CheckResult(code="TIME_COLUMN_MISSING", skipped=True, skip_reason="split is not time-based")
    name = params.time_column
    if name and name in facts.columns:
        return CheckResult(code="TIME_COLUMN_MISSING")
    candidates = time_candidates(facts, params)
    message = (
        f"This file has no column called '{name}', and the data is split by date."
        if name
        else "This use case splits the data by date, so it needs the column that says when each "
        "row was taken."
    )
    suggestion = (
        f"Choose one of: {', '.join(candidates[:3])}."
        if candidates
        else "Add a snapshot date column, or change Split type to Random (stratified) in Data split."
    )
    return CheckResult(
        code="TIME_COLUMN_MISSING",
        findings=(
            _finding(
                "TIME_COLUMN_MISSING",
                Severity.ERROR,
                message,
                suggestion,
                details={
                    "selected": name,
                    "candidates": list(candidates[:5]),
                    "split_type": SplitType.TIME_BASED.value,
                    "override_path": "split.time_column",
                },
            ),
        ),
    )


@check("TIME_COLUMN_UNPARSEABLE", severity=Severity.ERROR, modes=_TRAIN_ONLY)
def check_time_column_unparseable(frame: pd.DataFrame, params: CheckParams) -> CheckResult:
    """The configured time column does not read as a date."""
    import pandas as pd

    facts = facts_for(frame, params)
    name = params.time_column
    if not name or name not in facts.columns:
        return CheckResult(code="TIME_COLUMN_UNPARSEABLE", skipped=True, skip_reason="no time column")
    inferred = facts.types.get(name, ColumnType.STRING)
    if inferred in _TEMPORAL_TYPES:
        return CheckResult(code="TIME_COLUMN_UNPARSEABLE")
    values = frame[name].dropna()
    checked = len(values)
    parse_rate: float | None = None
    if checked:
        parsed = pd.to_datetime(values.astype(str), errors="coerce", format="mixed")
        parse_rate = float(parsed.notna().mean())
        if parse_rate >= DATETIME_PARSE_RATE:
            return CheckResult(code="TIME_COLUMN_UNPARSEABLE")
    suggestion = "Use a standard date format such as 2026-08-01, or choose a different time column."
    if parse_rate is not None:
        unparsed = checked - round(parse_rate * checked)
        suggestion += f" {unparsed:,} of {checked:,} values could not be read as a date."
    return CheckResult(
        code="TIME_COLUMN_UNPARSEABLE",
        findings=(
            _finding(
                "TIME_COLUMN_UNPARSEABLE",
                Severity.ERROR,
                f"'{name}' does not read as a date, so the data cannot be split by time.",
                suggestion,
                column=name,
                details={
                    "inferred_type": inferred.value,
                    "parse_rate": None if parse_rate is None else round(parse_rate, 4),
                    "checked_rows": checked or None,
                    "sample_values": list(sample_values(frame, name, facts)),
                },
            ),
        ),
    )


@check("HIGH_NULL_COLUMN", severity=Severity.WARNING, modes=_TRAIN_ONLY, acknowledgeable=True)
def check_high_null_column(frame: pd.DataFrame, params: CheckParams) -> CheckResult:
    """Columns that are mostly empty; PK_NULLS already covers the key."""
    facts = facts_for(frame, params)
    if facts.row_count == 0:
        return CheckResult(code="HIGH_NULL_COLUMN", skipped=True, skip_reason="empty frame")
    findings: list[ValidationCheck] = []
    for name in facts.columns:
        if name == params.primary_key:
            continue
        rows, nulls, _distinct, _unique = column_stats(frame, params, name, facts)
        if rows == 0:
            continue
        rate = nulls / rows
        if rate <= params.high_null_column_rate:
            continue
        findings.append(
            _finding(
                "HIGH_NULL_COLUMN",
                Severity.WARNING,
                f"'{name}' is empty in {rate:.0%} of rows.",
                "It will be dropped before training. Keep it by clearing it from the exclusions in "
                "Data preparation.",
                column=name,
                details={
                    "null_count": nulls,
                    "null_rate": round(rate, 4),
                    "threshold": params.high_null_column_rate,
                    "will_be_dropped": True,
                },
                acknowledgeable=True,
            )
        )
    return CheckResult(code="HIGH_NULL_COLUMN", findings=tuple(findings))


@check("CONSTANT_COLUMN", severity=Severity.WARNING, modes=_TRAIN_ONLY, acknowledgeable=True)
def check_constant_column(frame: pd.DataFrame, params: CheckParams) -> CheckResult:
    """A column with one value everywhere cannot help the model."""
    facts = facts_for(frame, params)
    findings: list[ValidationCheck] = []
    for name in facts.columns:
        if name in (params.primary_key, params.target):
            continue
        if facts.distinct_counts.get(name, 0) != 1:
            continue
        samples = sample_values(frame, name, facts, limit=1)
        findings.append(
            _finding(
                "CONSTANT_COLUMN",
                Severity.WARNING,
                f"'{name}' has the same value in every row, so it cannot help the model.",
                "It will be dropped before training.",
                column=name,
                details={"value": samples[0] if samples else None, "will_be_dropped": True},
                acknowledgeable=True,
            )
        )
    return CheckResult(code="CONSTANT_COLUMN", findings=tuple(findings))


@check("HIGH_CARDINALITY_ID_LIKE", severity=Severity.WARNING, modes=_TRAIN_ONLY, acknowledgeable=True)
def check_high_cardinality_id_like(frame: pd.DataFrame, params: CheckParams) -> CheckResult:
    """Identifier-shaped columns that are not the chosen key (DEC-052)."""
    facts = facts_for(frame, params)
    rows = facts.row_count
    findings: list[ValidationCheck] = []
    for name in facts.columns:
        if name in (params.primary_key, params.target) or not looks_like_id(facts, name):
            continue
        distinct = facts.distinct_counts.get(name, 0)
        findings.append(
            _finding(
                "HIGH_CARDINALITY_ID_LIKE",
                Severity.WARNING,
                f"'{name}' has a different value in almost every row, so it looks like an "
                "identifier rather than a feature.",
                "It will be dropped before training. Keep it by clearing it from the exclusions in "
                "Data preparation.",
                column=name,
                details={
                    "distinct_count": distinct,
                    "rows": rows,
                    "distinct_ratio": round(distinct / rows, 4) if rows else 0.0,
                    "threshold": ID_DISTINCT_RATIO,
                    "will_be_dropped": True,
                },
                acknowledgeable=True,
            )
        )
    return CheckResult(code="HIGH_CARDINALITY_ID_LIKE", findings=tuple(findings))


@check("PII_DETECTED", severity=Severity.WARNING, modes=_BOTH_MODES, acknowledgeable=True)
def check_pii_detected(frame: pd.DataFrame, params: CheckParams) -> CheckResult:
    """Columns that look like personal data. Never records a matched value (plan 13.7)."""
    facts = facts_for(frame, params)
    handling = params.pii_handling
    if handling is PiiHandling.DROP_COLUMNS:
        suggestion = "The column will be dropped before training."
    elif handling is PiiHandling.KEEP:
        suggestion = "It will be used as it is. Change PII handling in Data preparation to redact or drop it."
    else:
        suggestion = "Its values will be replaced with [REDACTED] before training."
    findings: list[ValidationCheck] = []
    for name in facts.columns:
        kinds = facts.pii_kinds.get(name, ())
        if not kinds:
            continue
        labels = [PII_KIND_LABELS.get(kind, kind) for kind in kinds]
        findings.append(
            _finding(
                "PII_DETECTED",
                Severity.WARNING,
                f"'{name}' looks like it contains {_join_labels(labels)}.",
                suggestion,
                column=name,
                details={"pii_kinds": list(kinds), "handling": handling.value},
                acknowledgeable=True,
            )
        )
    return CheckResult(code="PII_DETECTED", findings=tuple(findings))


@check("CONSENT_COLUMN_MISSING", severity=Severity.ERROR, modes=_BOTH_MODES)
def check_consent_column_missing(frame: pd.DataFrame, params: CheckParams) -> CheckResult:
    """A consent column is configured but the file does not carry it."""
    facts = facts_for(frame, params)
    name = params.consent_column
    if name is None:
        return CheckResult(
            code="CONSENT_COLUMN_MISSING", skipped=True, skip_reason="no consent column configured"
        )
    if name in facts.columns:
        return CheckResult(code="CONSENT_COLUMN_MISSING")
    return CheckResult(
        code="CONSENT_COLUMN_MISSING",
        findings=(
            _finding(
                "CONSENT_COLUMN_MISSING",
                Severity.ERROR,
                f"This use case only contacts {params.entity}s who have given consent, and the file "
                f"has no '{name}' column.",
                f"Add a '{name}' column holding true or false, or clear Consent column in "
                "Governance & privacy.",
                details={"consent_column": name, "override_path": "governance.consent_column"},
            ),
        ),
    )


@check("SUPPRESSION_COLUMN_MISSING", severity=Severity.WARNING, modes=_BOTH_MODES, acknowledgeable=True)
def check_suppression_column_missing(frame: pd.DataFrame, params: CheckParams) -> CheckResult:
    """A suppression switch is on but the column it needs is not in the file (DEC-030)."""
    facts = facts_for(frame, params)
    wanted: tuple[tuple[bool, str | None, str, str, str], ...] = (
        (
            params.suppress_opted_out,
            params.opt_out_column,
            "opt_out",
            f"is not in this file, so opted-out {params.entity}s cannot be suppressed.",
            "actions.suppression.suppress_opted_out",
        ),
        (
            params.suppress_recently_contacted,
            params.recently_contacted_column,
            "recently_contacted",
            f"is not in this file, so recently contacted {params.entity}s cannot be suppressed.",
            "actions.suppression.suppress_recently_contacted",
        ),
    )
    findings: list[ValidationCheck] = []
    for enabled, name, role, tail, override_path in wanted:
        if not enabled or name is None or name in facts.columns:
            continue
        findings.append(
            _finding(
                "SUPPRESSION_COLUMN_MISSING",
                Severity.WARNING,
                f"'{name}' {tail}",
                "Add the column, or turn the matching switch off in Actions & output.",
                details={
                    "suppression_column": name,
                    "role": role,
                    "override_path": override_path,
                    "override_value": False,
                },
                acknowledgeable=True,
            )
        )
    return CheckResult(code="SUPPRESSION_COLUMN_MISSING", findings=tuple(findings))


# ---------------------------------------------------------------------------
# 2.28 SCHEMA_MISMATCH
# ---------------------------------------------------------------------------
_COMPATIBLE_GROUPS: Final[tuple[frozenset[ColumnType], ...]] = (
    _NUMERIC_TYPES,
    _TEMPORAL_TYPES,
    _TEXTUAL_TYPES,
)


def types_compatible(expected: ColumnType, actual: ColumnType) -> bool:
    """Equal, or both in one widening group: numeric, temporal or textual."""
    if expected is actual:
        return True
    return any(expected in group and actual in group for group in _COMPATIBLE_GROUPS)


@check("SCHEMA_MISMATCH", severity=Severity.ERROR, modes=_SCORE_ONLY)
def check_schema_mismatch(frame: pd.DataFrame, params: CheckParams) -> CheckResult:
    """Compare the frame's columns against the schema the champion was fitted with (DEC-057)."""
    facts = facts_for(frame, params)
    schema = params.schema
    if schema is None:
        return CheckResult(code="SCHEMA_MISMATCH", skipped=True, skip_reason="no schema given")
    present = {name: facts.types[name] for name in facts.columns}
    expected = {column.name: column for column in schema.columns}
    missing = [
        column.name
        for column in schema.columns
        if column.required and column.name not in present and column.name != schema.target
    ]
    extra = [name for name in facts.columns if name not in expected and name != params.primary_key]
    type_changed = [
        {
            "name": column.name,
            "expected": column.inferred_type.value,
            "actual": present[column.name].value,
        }
        for column in schema.columns
        if column.name in present and not types_compatible(column.inferred_type, present[column.name])
    ]
    details: dict[str, object] = {
        "missing": missing,
        "extra": extra,
        "type_changed": type_changed,
        "model_version_id": schema.model_version_id,
        "expected_columns": [column.name for column in schema.columns],
    }
    if missing or type_changed:
        message = "This file does not match the data the model was trained on."
        if missing:
            message += f" Missing: {', '.join(missing)}."
        if type_changed:
            changed = ", ".join(
                f"{item['name']} (was {item['expected']}, now {item['actual']})" for item in type_changed
            )
            message += f" Changed type: {changed}."
        return CheckResult(
            code="SCHEMA_MISMATCH",
            findings=(
                _finding(
                    "SCHEMA_MISMATCH",
                    Severity.ERROR,
                    message,
                    "Upload a file with the same columns as the training data. The template for "
                    "this use case lists them.",
                    details=details,
                ),
            ),
        )
    if extra:
        return CheckResult(
            code="SCHEMA_MISMATCH",
            findings=(
                _finding(
                    "SCHEMA_MISMATCH",
                    Severity.WARNING,
                    f"This file has {len(extra)} column(s) the model was not trained on: "
                    f"{', '.join(extra)}.",
                    "They will be ignored while scoring.",
                    details=details,
                    acknowledgeable=True,
                ),
            ),
        )
    return CheckResult(code="SCHEMA_MISMATCH")


# ---------------------------------------------------------------------------
# 2.25 - 2.27 LEAKAGE_SUSPECTED
# ---------------------------------------------------------------------------
def leakage_exempt_names(params: CheckParams) -> frozenset[str]:
    """Columns no leakage branch may flag (DEC-062).

    The answer in all of its spellings, the identifier, the six columns the engine governs rather
    than learns from, and anything the user already excluded. Every one of them has its own check,
    so the rule redirects a column to the right message rather than silencing it.
    """
    names: set[str] = set()
    if params.target:
        names.add(params.target)
    names |= set(params.target_aliases)
    if params.primary_key:
        names.add(params.primary_key)
    if params.time_column:
        names.add(params.time_column)
    if params.group_column:
        names.add(params.group_column)
    if params.consent_column:
        names.add(params.consent_column)
    if params.opt_out_column:
        names.add(params.opt_out_column)
    if params.recently_contacted_column:
        names.add(params.recently_contacted_column)
    if params.fairness_column:
        names.add(params.fairness_column)
    names |= set(params.excluded_columns)
    return frozenset(names)


def candidate_columns(
    frame: pd.DataFrame,
    params: CheckParams,
    facts: FrameFacts | None = None,
) -> tuple[str, ...]:
    """The columns LEAKAGE_SUSPECTED may consider. All three branches use exactly this set."""
    resolved = facts_for(frame, params) if facts is None else facts
    exempt = leakage_exempt_names(params)
    return tuple(name for name in resolved.columns if name not in exempt)


def leakage_sample(frame: pd.DataFrame, params: CheckParams) -> pd.DataFrame:
    """The one sampling rule, shared by the AUC branch and the baseline branch.

    A RANDOM sample, never `head`: an upload sorted by date or by outcome would make `head`
    systematically wrong. `params.seed` comes from the upload id, so the same upload gives the same
    verdict in every process.
    """
    if len(frame) <= params.sample_rows:
        return frame
    return frame.sample(n=params.sample_rows, random_state=params.seed)


def single_feature_auc(x: pd.Series, y: pd.Series) -> float | None:
    """The Mann-Whitney U rank statistic - the AUC of the best monotone single-feature model.

    No model is fitted, and ties are averaged, which is the standard AUC tie rule. Returns `None`
    for degenerate input (one class, too few rows, a constant column) rather than raising.
    """
    mask = x.notna() & y.notna()
    xs = x[mask]
    ys = y[mask]
    n1 = int(ys.sum())
    n0 = len(ys) - n1
    if n0 == 0 or n1 == 0 or len(ys) < LEAKAGE_MIN_ROWS or int(xs.nunique()) < 2:
        return None
    ranks = xs.rank(method="average")
    auc = (float(ranks[ys == 1].sum()) - n1 * (n1 + 1) / 2) / (n0 * n1)
    return float(max(auc, 1.0 - auc))


def _binary_target(work: pd.DataFrame, target: str, label: str) -> pd.Series:
    import pandas as pd

    wanted = label.strip().lower()
    flags = [1 if _norm(value) == wanted else 0 for value in work[target].tolist()]
    return pd.Series(flags, index=work.index, dtype="int64")


def _leakage_feature(
    work: pd.DataFrame,
    name: str,
    inferred: ColumnType,
    y: pd.Series,
    max_categories: int,
) -> pd.Series | None:
    """One candidate column as a numeric series the rank statistic can read."""
    import pandas as pd

    series = work[name]
    if inferred is ColumnType.BOOLEAN and not pd.api.types.is_numeric_dtype(series):
        mapped = series.map(lambda value: _boolean_token(value))
        return pd.to_numeric(mapped, errors="coerce")
    if inferred in _NUMERIC_TYPES:
        return pd.to_numeric(series, errors="coerce")
    if inferred in _TEMPORAL_TYPES:
        stamps = pd.to_datetime(series, errors="coerce", format="mixed")
        numbers = stamps.astype("int64").astype("float64")
        return numbers.mask(stamps.isna())
    if inferred in _TEXTUAL_TYPES:
        if int(series.nunique(dropna=True)) > max_categories:
            return None
        means = y.groupby(series).mean()
        return pd.to_numeric(series.map(means), errors="coerce")
    return None


def _boolean_token(value: object) -> float | None:
    token = _norm(value)
    if token in _BOOL_TRUE_TOKENS:
        return 1.0
    if token in _BOOL_FALSE_TOKENS:
        return 0.0
    return None


def baseline_auc(
    frame: pd.DataFrame,
    params: CheckParams,
    facts: FrameFacts,
    columns: Sequence[str],
) -> tuple[float | None, tuple[tuple[str, float], ...]]:
    """Out-of-fold ROC-AUC of a logistic regression over `columns`, and every feature it weighed.

    The second element is `(feature, mean absolute standardised coefficient)` for every column of
    the design matrix, largest first, so the caller can report both how many features were used
    and the few that drove the score.

    Out-of-fold, never in-sample: an L2 logistic regression with 200 standardised features can
    exceed 0.98 on clean data by overfitting alone, which would fire on every wide upload.
    sklearn is imported here so `import engine` stays fast.
    """
    import numpy as np
    import pandas as pd
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import StratifiedKFold

    if not params.leakage_baseline or not columns:
        return (None, ())
    target = params.target
    if not target:
        return (None, ())
    work = leakage_sample(frame, params)
    if len(work) < LEAKAGE_MIN_ROWS:
        return (None, ())
    label, _positives, _negatives = resolve_positive_label(frame, params, facts)
    if label is None:
        return (None, ())
    y = _binary_target(work, target, label)
    if int(y.sum()) < BASELINE_MIN_CLASS_ROWS or int(len(y) - y.sum()) < BASELINE_MIN_CLASS_ROWS:
        return (None, ())

    blocks: list[pd.DataFrame] = []
    names: list[str] = []
    for name in columns:
        inferred = facts.types.get(name, ColumnType.STRING)
        series = work[name]
        if series.isna().all():
            continue
        if inferred in _TEXTUAL_TYPES:
            if int(series.nunique(dropna=True)) > BASELINE_MAX_CATEGORIES:
                continue
            dummies = pd.get_dummies(series.astype("string"), prefix=name, dummy_na=False, dtype="float64")
            if dummies.shape[1] == 0:
                continue
            blocks.append(dummies)
            names.extend(str(column) for column in dummies.columns)
            continue
        column = _leakage_feature(work, name, inferred, y, BASELINE_MAX_CATEGORIES)
        if column is None or column.isna().all():
            continue
        blocks.append(column.astype("float64").to_frame(name))
        names.append(name)
        if len(names) >= BASELINE_MAX_FEATURES:
            break
    if not blocks:
        return (None, ())
    matrix = pd.concat(blocks, axis=1).iloc[:, :BASELINE_MAX_FEATURES]
    names = [str(column) for column in matrix.columns]
    values = matrix.to_numpy(dtype="float64", copy=True)
    medians = np.nanmedian(values, axis=0)
    medians = np.where(np.isnan(medians), 0.0, medians)
    holes = np.isnan(values)
    values[holes] = np.take(medians, np.where(holes)[1])
    means = values.mean(axis=0)
    deviations = values.std(axis=0)
    deviations[deviations == 0.0] = 1.0
    values = (values - means) / deviations

    labels = y.to_numpy(dtype="int64")
    folds = StratifiedKFold(n_splits=BASELINE_FOLDS, shuffle=True, random_state=params.seed)
    scores = np.zeros(len(labels), dtype="float64")
    weights = np.zeros(values.shape[1], dtype="float64")
    for train_index, test_index in folds.split(values, labels):
        model = LogisticRegression(
            # `penalty="l2"` is the default and is deprecated as an explicit argument in the
            # pinned scikit-learn 1.9.1, so it is left unset rather than warned about on every run.
            solver="lbfgs",
            C=1.0,
            max_iter=200,
            random_state=params.seed,
        )
        model.fit(values[train_index], labels[train_index])
        scores[test_index] = model.predict_proba(values[test_index])[:, 1]
        weights += np.abs(model.coef_[0]) / BASELINE_FOLDS
    auc = single_feature_auc(pd.Series(scores, index=y.index), y)
    order = np.argsort(-weights)
    top = tuple((names[int(index)], round(float(weights[int(index)]), 4)) for index in order)
    return (auc, top)


@check(
    "LEAKAGE_SUSPECTED",
    severity=Severity.ERROR,
    modes=_TRAIN_ONLY,
    acknowledgeable=True,
    needs_target=True,
)
def check_leakage_suspected(frame: pd.DataFrame, params: CheckParams) -> CheckResult:
    """Three branches over one candidate set: name pattern, per-column AUC, whole-frame baseline."""
    code = "LEAKAGE_SUSPECTED"
    facts = facts_for(frame, params)
    if not params.leakage_check:
        return CheckResult(code=code, skipped=True, skip_reason="leakage_check is off")
    if params.problem_type is not ProblemType.BINARY_CLASSIFICATION:
        return CheckResult(code=code, skipped=True, skip_reason="not a binary problem")
    target = params.target
    if not target or target not in facts.columns:
        return CheckResult(code=code, skipped=True, skip_reason="no target column")
    label, _positives, _negatives = resolve_positive_label(frame, params, facts)
    if label is None:
        return CheckResult(code=code, skipped=True, skip_reason="target is not two-valued")

    candidates = candidate_columns(frame, params, facts)
    positions = {name: index for index, name in enumerate(facts.columns)}
    target_position = positions[target]
    name_pattern = re.compile(params.leakage_pattern, re.IGNORECASE)
    after_pattern = re.compile(params.leakage_after_target_pattern, re.IGNORECASE)
    named: dict[str, tuple[str, str]] = {}
    for name in candidates:
        if name_pattern.search(name):
            named[name] = ("name_pattern", params.leakage_pattern)
        elif positions[name] > target_position and after_pattern.search(name):
            named[name] = ("after_target", params.leakage_after_target_pattern)

    work = leakage_sample(frame, params)
    scored: dict[str, float] = {}
    if len(work) >= LEAKAGE_MIN_ROWS:
        y = _binary_target(work, target, label)
        for name in candidates:
            feature = _leakage_feature(
                work, name, facts.types.get(name, ColumnType.STRING), y, LEAKAGE_MAX_CATEGORIES
            )
            if feature is None:
                continue
            auc = single_feature_auc(feature, y)
            if auc is not None and auc > params.leakage_auc_threshold:
                scored[name] = auc

    findings: list[ValidationCheck] = []
    for name in candidates:
        if name in scored:
            findings.append(
                _leakage_finding(
                    params,
                    name,
                    reason="auc",
                    auc=scored[name],
                    pattern=named.get(name, (None, None))[1],
                    sampled_rows=len(work),
                    target_position=target_position,
                    column_position=positions[name],
                    severity=Severity.ERROR,
                )
            )
        elif name in named:
            reason, pattern = named[name]
            findings.append(
                _leakage_finding(
                    params,
                    name,
                    reason=reason,
                    auc=None,
                    pattern=pattern,
                    sampled_rows=len(work),
                    target_position=target_position,
                    column_position=positions[name],
                    severity=Severity.WARNING,
                )
            )

    if not scored and params.leakage_baseline:
        auc, top = baseline_auc(frame, params, facts, candidates)
        if auc is not None and auc > params.leakage_auc_threshold:
            findings.append(
                _finding(
                    code,
                    Severity.WARNING,
                    f"The data predicts '{target}' almost perfectly ({auc:.1%} AUC) with a simple "
                    "model, even though no single column does. Some combination of columns may "
                    "contain the answer.",
                    "Check the columns listed below for anything recorded after the outcome, and "
                    "exclude them in Data preparation. Training continues either way.",
                    details={
                        "reason": "baseline",
                        "auc": round(auc, 4),
                        "threshold": params.leakage_auc_threshold,
                        "model": "logistic_regression",
                        "folds": BASELINE_FOLDS,
                        "sampled_rows": len(work),
                        "features_used": len(top),
                        "top_features": [{"column": name, "weight": weight} for name, weight in top[:3]],
                        "acknowledge": code,
                    },
                    acknowledgeable=True,
                )
            )
    return CheckResult(code=code, findings=tuple(findings))


def _leakage_finding(
    params: CheckParams,
    name: str,
    *,
    reason: str,
    auc: float | None,
    pattern: str | None,
    sampled_rows: int,
    target_position: int,
    column_position: int,
    severity: Severity,
) -> ValidationCheck:
    definition = params.target_definition
    if reason == "auc":
        suggestion = (
            f"If '{name}' is only known after the outcome, exclude it. If it is genuinely "
            "available before, confirm and the run continues."
        )
    else:
        suggestion = (
            f"'{name}' is named like an outcome. If it is known before "
            f"{definition or 'the outcome'}, confirm and the run continues."
        )
    return _finding(
        "LEAKAGE_SUSPECTED",
        severity,
        f"Column '{name}' almost perfectly predicts the target. It may contain the answer. " "Exclude it?",
        suggestion,
        column=name,
        details={
            "reason": reason,
            "auc": None if auc is None else round(auc, 4),
            "threshold": params.leakage_auc_threshold,
            "pattern": pattern,
            "sampled_rows": sampled_rows,
            "target_position": target_position,
            "column_position": column_position,
            "override_path": "prepare.exclude_columns",
            "override_value": [*params.excluded_columns, name],
            "acknowledge": f"LEAKAGE_SUSPECTED:{name}",
        },
        acknowledgeable=True,
    )


# ---------------------------------------------------------------------------
# Registry (built once, after every check is defined)
# ---------------------------------------------------------------------------
CHECK_REGISTRY: Final[tuple[CheckSpec, ...]] = tuple(sorted(_REGISTRY, key=lambda spec: spec.order))
CHECKS_BY_CODE: Final[Mapping[str, CheckSpec]] = {spec.code: spec for spec in CHECK_REGISTRY}


def checks_for(mode: RunMode) -> tuple[CheckSpec, ...]:
    """The checks that run in `mode`, in plan section 6.3 table order."""
    return tuple(spec for spec in CHECK_REGISTRY if mode in spec.modes)


# ---------------------------------------------------------------------------
# 2.1 Composition
# ---------------------------------------------------------------------------
def run_checks(
    frame: pd.DataFrame,
    params: CheckParams,
    *,
    mode: RunMode,
) -> tuple[ValidationCheck, ...]:
    """Layer 1 - pure. Every check of `mode` runs; none may stop another.

    A check that raises is logged once as its exception class name - never its message, which a
    pandas exception can quote a cell value into (plan section 13.7) - and contributes no finding.
    """
    facts = facts_for(frame, params)
    resolved = replace(params, facts=facts)
    collected: list[ValidationCheck] = []
    for spec in checks_for(mode):
        if spec.needs_target and not resolved.target:
            continue
        try:
            result = spec.fn(frame, resolved)
        except Exception as exc:  # no check may ever break a run
            logger.warning("validation.check_failed code=%s error=%s", spec.code, type(exc).__name__)
            continue
        collected.extend(result.findings)
    positions = {name: index for index, name in enumerate(facts.columns)}

    def sort_key(item: ValidationCheck) -> tuple[int, int, int, str]:
        return (
            SEVERITY_RANK[item.severity],
            CHECK_ORDER.index(item.code),
            -1 if item.column is None else positions.get(item.column, -1),
            item.column or "",
        )

    return tuple(sorted(collected, key=sort_key))


def _is_acknowledged(item: ValidationCheck, acknowledged: frozenset[str]) -> bool:
    if not item.acknowledgeable:
        return False
    if item.column is not None and f"{item.code}:{item.column}" in acknowledged:
        return True
    return item.code in acknowledged


def validate_frame(
    frame: pd.DataFrame,
    params: CheckParams,
    *,
    mode: RunMode,
    upload_id: str,
    run_id: str | None = None,
    acknowledged: Iterable[str] = (),
    now: datetime | None = None,
) -> ValidationReport:
    """Layer 2 - the generic entry point: checks, acknowledgements, counts.

    Everything above this is convenience and everything below it is pure, so a later caller that
    wants to validate an engineered feature frame needs only a frame, a `CheckParams` and an id.
    """
    from engine.utils.time import utc_now

    checks = run_checks(frame, params, mode=mode)
    wanted = frozenset(acknowledged)
    marked = tuple(
        item.model_copy(update={"acknowledged": True}) if _is_acknowledged(item, wanted) else item
        for item in checks
    )
    error_count = sum(1 for item in marked if item.severity is Severity.ERROR and not item.acknowledged)
    warning_count = sum(1 for item in marked if item.severity is Severity.WARNING)
    return ValidationReport(
        run_id=run_id,
        upload_id=upload_id,
        mode=mode,
        checks=marked,
        error_count=error_count,
        warning_count=warning_count,
        passed=error_count == 0,
        validated_at=now or utc_now(),
    )


def params_from_config(
    config: UseCaseConfig,
    *,
    primary_key: str | None = None,
    target: str | None = None,
    seed: int = 0,
    row_count: int | None = None,
    schema: FeatureSchema | None = None,
) -> CheckParams:
    """The ONLY place a `UseCaseConfig` is read.

    Copies scalars and column names out of the config and the catalog into a flat `CheckParams`;
    nothing else in this module touches a config at runtime.
    """
    validation = config.validation
    suppression = config.actions.suppression
    patterns = config.catalog.column_name_patterns
    aliases: list[str] = []
    if config.target.column:
        aliases.append(config.target.column)
    for column in config.template.columns:
        if column.role is ColumnRole.TARGET and column.name not in aliases:
            aliases.append(column.name)
    return CheckParams(
        primary_key=primary_key,
        target=target,
        time_column=config.split.time_column,
        group_column=config.split.group_column,
        consent_column=config.governance.consent_column,
        opt_out_column=suppression.opt_out_column,
        recently_contacted_column=suppression.recently_contacted_column,
        fairness_column=config.evaluation.fairness_column,
        excluded_columns=tuple(config.prepare.exclude_columns),
        entity=config.entity,
        problem_type=config.problem_type,
        positive_label=config.target.positive_label,
        min_rows=validation.min_rows,
        min_positive=validation.min_positive,
        imbalance_warn_min_rate=validation.imbalance_warn_min_rate,
        imbalance_warn_max_rate=validation.imbalance_warn_max_rate,
        high_null_column_rate=validation.high_null_column_rate,
        leakage_check=validation.leakage_check,
        leakage_baseline=bool(getattr(validation, "leakage_baseline", True)),
        leakage_auc_threshold=validation.leakage_auc_threshold,
        split_type=config.split.type,
        pii_handling=config.prepare.pii_handling,
        suppress_opted_out=suppression.suppress_opted_out,
        suppress_recently_contacted=suppression.suppress_recently_contacted,
        time_like_pattern=patterns.time_like,
        leakage_pattern=patterns.leakage,
        leakage_after_target_pattern=patterns.leakage_after_target,
        id_like_pattern=str(getattr(patterns, "id_like", ID_LIKE_PATTERN)),
        target_aliases=tuple(aliases),
        target_definition=config.target.definition,
        target_label_source=config.target.label_source,
        seed=seed,
        row_count=row_count,
        schema=schema,
    )


def validate_for_training(
    frame: pd.DataFrame,
    config: UseCaseConfig,
    *,
    primary_key: str | None,
    target: str | None,
    acknowledged: Iterable[str] = (),
    upload_id: str,
    run_id: str | None = None,
    row_count: int | None = None,
    now: datetime | None = None,
) -> ValidationReport:
    """Layer 3 - the run-level convenience `POST /runs` calls for a training run."""
    params = params_from_config(
        config,
        primary_key=primary_key,
        target=target,
        seed=seed_from(upload_id),
        row_count=row_count,
    )
    return validate_frame(
        frame,
        params,
        mode=RunMode.TRAIN,
        upload_id=upload_id,
        run_id=run_id,
        acknowledged=acknowledged,
        now=now,
    )


def validate_against_schema(
    frame: pd.DataFrame,
    schema: FeatureSchema,
    *,
    primary_key: str | None,
    config: UseCaseConfig | None = None,
    acknowledged: Iterable[str] = (),
    upload_id: str,
    run_id: str | None = None,
    row_count: int | None = None,
    now: datetime | None = None,
) -> ValidationReport:
    """Layer 3, score mode: the scoring file against the schema the model was fitted with."""
    if config is None:
        params = CheckParams(
            primary_key=primary_key,
            seed=seed_from(upload_id),
            row_count=row_count,
            schema=schema,
        )
    else:
        params = params_from_config(
            config,
            primary_key=primary_key,
            seed=seed_from(upload_id),
            row_count=row_count,
            schema=schema,
        )
    return validate_frame(
        frame,
        params,
        mode=RunMode.SCORE,
        upload_id=upload_id,
        run_id=run_id,
        acknowledged=acknowledged,
        now=now,
    )


def validation_detail(report: ValidationReport) -> str:
    """'No problems found' | '2 warnings' | '1 error - 2 warnings' | '3 errors'."""
    parts: list[str] = []
    if report.error_count:
        parts.append(f"{report.error_count} error" + ("s" if report.error_count != 1 else ""))
    if report.warning_count:
        parts.append(f"{report.warning_count} warning" + ("s" if report.warning_count != 1 else ""))
    return " · ".join(parts) if parts else "No problems found"
