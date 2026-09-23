"""Turning an uploaded uplift table into the arrays and feature matrix the learners fit (plan B §3, §4).

An uplift table is a Phase 1 table with one more column that matters - who was treated - and a
stricter idea of what may be a feature. Everything here is a pure function of a frame and the
configuration, so the checks (`engine.uplift.checks`), the training flow and the scoring flow all
see the same columns, the same arrays and the same category levels.

**Reuse Phase 1's verdicts, do not re-derive them.** Phase 1 validation already decided which columns
are mostly empty, constant or identifier-shaped (`HIGH_NULL_COLUMN`, `CONSTANT_COLUMN`,
`HIGH_CARDINALITY_ID_LIKE`, each carrying `details.will_be_dropped`) and which look like personal data
(`PII_DETECTED`). :func:`fit_feature_spec` takes that `ValidationReport` and honours it - including
the user's acknowledgements - rather than running a second, subtly different set of heuristics that
could keep a column the Validate page told the user would go. The direct constant / ID-like tests
below are only a fallback for columns Phase 1 did not rule on (or when no report is supplied), and
PII is detected with `engine.stages.ingest.detect_pii`, the engine's single PII detector, when no
report says otherwise.

**What is never a feature.** The key, the outcome and the treatment flag obviously; the treatment
date and campaign id, because they describe the experiment rather than the customer; the split,
consent, fairness and suppression columns, because Phase 1 reserves them and keeps them out of its
own feature set (`engine.stages.prepare._reserved_columns`); whatever the user excluded; and every
date-like column. A date is dropped rather than encoded because in an uplift table a date is almost
always *about the campaign* (sent, opened, converted) and so leaks either the treatment or the
outcome; `FEATURE_AFTER_TREATMENT` inspects exactly these columns.

**Fixed categories.** Text, boolean and categorical columns become pandas `category` columns whose
levels are frozen at fit time (the 100 most frequent, ties broken by the level text), so a scoring
file with a level the model never saw scores it as missing instead of silently shifting every other
level's code. Numeric columns become `float64`.

**Arrays.** Treatment and outcome are returned as `int` 0/1 arrays. A treatment value that is not
clearly 0 or 1 - including a null - is never guessed: :func:`coerce_treatment` counts it and returns
`None`, and `TREATMENT_NOT_BINARY` reports the count.

`numpy` and `pandas` are imported inside function bodies so `import engine` stays fast.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from engine.config import ColumnType, PiiHandling

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    import numpy as np
    import numpy.typing as npt
    import pandas as pd

    from engine.config import UseCaseConfig
    from engine.contracts import ValidationReport
    from engine.uplift.config import UpliftConfig

    IntArray = npt.NDArray[np.int_]

__all__ = [
    "MAX_CATEGORY_LEVELS",
    "PHASE1_DROP_REASONS",
    "FeatureSpec",
    "apply_feature_spec",
    "candidate_feature_columns",
    "coerce_outcome",
    "coerce_treatment",
    "date_like_columns",
    "detect_treatment_column",
    "fit_feature_spec",
    "is_date_like",
    "reserved_feature_exclusions",
    "split_holdout",
]

MAX_CATEGORY_LEVELS: Final[int] = 100
"""Levels kept per categorical feature; rarer levels are scored as missing."""

TYPE_SAMPLE_ROWS: Final[int] = 5_000
"""Non-null values a column's type (and PII shape) is inferred from. Enough to be stable, cheap to parse."""

PHASE1_DROP_REASONS: Final[dict[str, str]] = {
    "HIGH_NULL_COLUMN": "high_null",
    "CONSTANT_COLUMN": "constant",
    "HIGH_CARDINALITY_ID_LIKE": "id_like",
}
"""Phase 1 findings whose `details.will_be_dropped` removes a column, and the reason recorded for it.

The reason strings are `engine.contracts.DroppedColumn.reason` values, so `prepare.json` and the
uplift feature spec speak one vocabulary.
"""

_PII_REASON: Final[str] = "pii"
_DATE_REASON: Final[str] = "date"
_CONSTANT_REASON: Final[str] = "constant"
_ID_LIKE_REASON: Final[str] = "id_like"
_UNSUPPORTED_REASON: Final[str] = "unsupported_type"

_TREATED_TEXT: Final[frozenset[str]] = frozenset({"1", "true"})
_CONTROL_TEXT: Final[frozenset[str]] = frozenset({"0", "false"})

_OUTCOME_POSITIVE_TEXT: Final[tuple[str, ...]] = ("true", "1", "yes", "y")
"""The conventional positive spellings, in the order Phase 1's `_positive_mask` prefers them."""

_TEMPORAL_TYPES: Final[frozenset[ColumnType]] = frozenset({ColumnType.DATE, ColumnType.DATETIME})
_NUMERIC_TYPES: Final[frozenset[ColumnType]] = frozenset({ColumnType.INTEGER, ColumnType.FLOAT})


# ---------------------------------------------------------------------------
# The treatment column and the two arrays
# ---------------------------------------------------------------------------
def detect_treatment_column(columns: Sequence[str], config: UpliftConfig) -> str | None:
    """The treatment column: the configured one if present, else the first hint present.

    Matching is exact first, then case-insensitive, and the file's own spelling is returned. A
    configured name that is **not** in the file returns `None` rather than falling back to a hint:
    the user said which column records the experiment, and quietly measuring a different one would
    answer a question nobody asked. `TREATMENT_COLUMN_MISSING` then names the configured column.
    """
    names = [str(name) for name in columns]
    if config.treatment_column is not None:
        return _find_column(names, config.treatment_column)
    for hint in config.treatment_column_hints:
        found = _find_column(names, hint)
        if found is not None:
            return found
    return None


def _find_column(names: Sequence[str], wanted: str) -> str | None:
    if wanted in names:
        return wanted
    lowered = wanted.lower()
    return next((name for name in names if name.lower() == lowered), None)


def coerce_treatment(values: pd.Series) -> tuple[IntArray | None, int]:
    """`(0/1 int array, bad count)`; the array is `None` when any value is not clearly 0 or 1.

    Accepted: booleans, the numbers 0 and 1 (`1.0` too), and the text `"0"`/`"1"` or
    `"true"`/`"false"` (any case, surrounding spaces ignored). Everything else counts as bad,
    **including a null**: a customer with no record of whether they were contacted belongs to
    neither arm, and guessing an arm would bias both.
    """
    import numpy as np
    import pandas as pd

    if pd.api.types.is_bool_dtype(values.dtype) and not values.isna().any():
        return np.asarray(values.to_numpy(dtype=bool), dtype=np.int_), 0
    if pd.api.types.is_numeric_dtype(values.dtype) and not pd.api.types.is_bool_dtype(values.dtype):
        numbers = pd.to_numeric(values, errors="coerce")
        bad = int((~numbers.isin((0, 1)) | numbers.isna()).sum())
        if bad:
            return None, bad
        return np.asarray(numbers.to_numpy(dtype=float), dtype=np.int_), 0
    coded = values.map(_treatment_code)
    bad = int(coded.isna().sum())
    if bad:
        return None, bad
    return np.asarray(coded.to_numpy(dtype=float), dtype=np.int_), 0


def _treatment_code(value: object) -> float | None:
    """1.0, 0.0 or None (bad) for one treatment cell."""
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, int | float):
        number = float(value)
        if math.isnan(number):
            return None
        return number if number in (0.0, 1.0) else None
    if hasattr(value, "item") and not isinstance(value, str):  # numpy scalars
        return _treatment_code(value.item())
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TREATED_TEXT:
            return 1.0
        if text in _CONTROL_TEXT:
            return 0.0
    return None


def coerce_outcome(values: pd.Series, positive_label: object | None) -> tuple[IntArray, str]:
    """`(0/1 int array, positive label as text)`. Raises `ValueError` unless the outcome is binary.

    The positive label follows Phase 1 exactly (`engine.stages.prepare._positive_mask`): the
    configured `target.positive_label` when set, else `true` over `false`, `1` over `0`, `yes` over
    `no`, and failing all of those the rarer of the two values. Labels compare by one normalised
    spelling, so `1`, `1.0` and `"1"` are the same class.

    A null outcome is refused, not treated as a negative: a customer whose result is unknown did
    not fail to convert. Messages carry counts only, never a value (plan section 13.7).
    """
    import numpy as np

    missing = int(values.isna().sum())
    if missing:
        raise ValueError(
            f"The outcome is missing in {missing} rows; an uplift outcome must be recorded for every row."
        )
    keys = values.map(_label_key)
    counts = keys.value_counts()
    labels = sorted(str(label) for label in counts.index)
    if len(labels) != 2:
        raise ValueError(f"The outcome must have exactly two values; it has {len(labels)}.")
    positive: str | None = None
    if positive_label is not None:
        wanted = _label_key(positive_label)
        if wanted not in labels:
            raise ValueError("The configured positive label does not occur in the outcome column.")
        positive = wanted
    if positive is None:
        positive = next((token for token in _OUTCOME_POSITIVE_TEXT if token in labels), None)
    if positive is None:
        positive = min(labels, key=lambda label: (int(counts[label]), label))
    array = np.asarray(keys.eq(positive).to_numpy(dtype=bool), dtype=np.int_)
    return array, positive


def _label_key(value: object) -> str:
    """One comparable spelling per label - the same normalisation as Phase 1's prepare stage."""
    if hasattr(value, "item") and not isinstance(value, str | bytes):  # numpy scalars
        value = value.item()
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    return str(value).strip().lower()


# ---------------------------------------------------------------------------
# Which columns may be features
# ---------------------------------------------------------------------------
def reserved_feature_exclusions(
    config: UseCaseConfig, *, primary_key: str, target: str, treatment_column: str | None
) -> tuple[str, ...]:
    """Every configured column that describes the experiment or the run and so is never a feature.

    The spec's list (key, outcome, treatment, treatment date, campaign id, split time and group,
    consent, user exclusions) plus the columns Phase 1 itself reserves and keeps out of its features
    (the configured target, the fairness column and the two suppression columns), so an uplift
    model never learns from a column a propensity model on the same file is forbidden to use.
    """
    uplift = config.uplift
    candidates: Iterable[str | None] = (
        primary_key,
        target,
        treatment_column,
        *uplift.reserved_columns(),
        config.target.column,
        config.split.time_column,
        config.split.group_column,
        config.governance.consent_column,
        config.evaluation.fairness_column,
        config.actions.suppression.opt_out_column,
        config.actions.suppression.recently_contacted_column,
        *config.prepare.exclude_columns,
    )
    reserved: list[str] = []
    for name in candidates:
        if name is not None and name not in reserved:
            reserved.append(name)
    return tuple(reserved)


def _non_reserved(frame: pd.DataFrame, reserved: Sequence[str]) -> tuple[str, ...]:
    skip = set(reserved)
    return tuple(str(name) for name in frame.columns if str(name) not in skip)


def _inferred_type(series: pd.Series) -> ColumnType:
    """Phase 1's type verdict for a column (`engine.stages.ingest.infer_column_type`), on a sample."""
    from engine.stages.ingest import infer_column_type

    observed = series.dropna()
    return infer_column_type(observed.head(TYPE_SAMPLE_ROWS))


def is_date_like(series: pd.Series) -> bool:
    """True for a datetime column, or a text column Phase 1's type inference reads as dates.

    Numbers are never dates here (an epoch integer is indistinguishable from a count), and digit
    strings are numbers before they are dates, exactly as in `infer_column_type`.
    """
    import pandas as pd

    dtype = series.dtype
    if pd.api.types.is_datetime64_any_dtype(dtype):
        return True
    if pd.api.types.is_bool_dtype(dtype) or pd.api.types.is_numeric_dtype(dtype):
        return False
    if (
        isinstance(dtype, pd.CategoricalDtype)
        or pd.api.types.is_object_dtype(dtype)
        or pd.api.types.is_string_dtype(dtype)
    ):
        return _inferred_type(series) in _TEMPORAL_TYPES
    return False


def date_like_columns(frame: pd.DataFrame, columns: Sequence[str]) -> tuple[str, ...]:
    """The subset of `columns` (in that order) that :func:`is_date_like` accepts."""
    return tuple(name for name in columns if name in frame.columns and is_date_like(frame[name]))


def candidate_feature_columns(
    frame: pd.DataFrame,
    config: UseCaseConfig,
    *,
    primary_key: str,
    target: str,
    treatment_column: str | None,
) -> tuple[str, ...]:
    """Every column that could be a feature, in file order: not reserved and not date-like.

    The data-quality drops (empty, constant, ID-like, PII) are :func:`fit_feature_spec`'s business;
    this is the *structural* answer - the columns the randomness check predicts treatment from.
    """
    reserved = reserved_feature_exclusions(
        config, primary_key=primary_key, target=target, treatment_column=treatment_column
    )
    columns = _non_reserved(frame, reserved)
    dates = set(date_like_columns(frame, columns))
    return tuple(name for name in columns if name not in dates)


# ---------------------------------------------------------------------------
# The feature spec: fitted once on the training file, replayed on every scoring file
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FeatureSpec:
    """The features a learner fits on, their category levels, and every column left out with why.

    A column absent from `categorical_levels` is numeric (`float64`). `dropped` maps a column to one
    of `high_null`, `constant`, `id_like`, `pii`, `date` or `unsupported_type`.
    """

    feature_columns: tuple[str, ...]
    categorical_levels: dict[str, tuple[str, ...]] = field(default_factory=dict)
    dropped: dict[str, str] = field(default_factory=dict)


def _phase1_drops(validation: ValidationReport | None, pii_handling: PiiHandling) -> dict[str, str]:
    """Column -> reason, from the Phase 1 findings that remove a column."""
    drops: dict[str, str] = {}
    if validation is None:
        return drops
    # PII first: it outranks a statistical reason, as in Phase 1's prepare stage.
    if pii_handling is not PiiHandling.KEEP:
        for item in validation.checks:
            if item.code == "PII_DETECTED" and item.column is not None:
                drops.setdefault(item.column, _PII_REASON)
    for item in validation.checks:
        reason = PHASE1_DROP_REASONS.get(item.code)
        if reason is None or item.column is None or item.column in drops:
            continue
        if item.details.get("will_be_dropped") is True and not item.acknowledged:
            drops[item.column] = reason
    return drops


def _acknowledged_keeps(validation: ValidationReport | None) -> set[tuple[str, str]]:
    """`(reason, column)` pairs the user chose to keep by acknowledging the Phase 1 finding."""
    if validation is None:
        return set()
    return {
        (PHASE1_DROP_REASONS[item.code], item.column)
        for item in validation.checks
        if item.code in PHASE1_DROP_REASONS and item.column is not None and item.acknowledged
    }


def _is_texty(series: pd.Series) -> bool:
    import pandas as pd

    dtype = series.dtype
    if isinstance(dtype, pd.CategoricalDtype):
        return True
    return bool(pd.api.types.is_object_dtype(dtype) or pd.api.types.is_string_dtype(dtype))


def _looks_like_id(series: pd.Series, name: str) -> bool:
    """Phase 1's identifier rule: near-unique and complete, and text or named like an identifier."""
    from engine.stages.validate import ID_DISTINCT_RATIO, ID_LIKE_PATTERN, ID_MIN_ROWS

    rows = len(series)
    if rows < ID_MIN_ROWS or bool(series.isna().any()):
        return False
    if int(series.nunique(dropna=True)) < ID_DISTINCT_RATIO * rows:
        return False
    return _is_texty(series) or re.search(ID_LIKE_PATTERN, name, re.IGNORECASE) is not None


def _column_kind(series: pd.Series) -> str:
    """`numeric`, `categorical`, `date` or `unsupported` for one candidate column."""
    import pandas as pd

    dtype = series.dtype
    if pd.api.types.is_datetime64_any_dtype(dtype):
        return "date"
    if pd.api.types.is_bool_dtype(dtype):
        return "categorical"
    if pd.api.types.is_numeric_dtype(dtype) and not pd.api.types.is_complex_dtype(dtype):
        return "numeric"
    if isinstance(dtype, pd.CategoricalDtype):
        return "date" if _inferred_type(series) in _TEMPORAL_TYPES else "categorical"
    if pd.api.types.is_object_dtype(dtype) or pd.api.types.is_string_dtype(dtype):
        inferred = _inferred_type(series)
        if inferred in _TEMPORAL_TYPES:
            return "date"
        if inferred in _NUMERIC_TYPES:
            return "numeric"
        return "categorical"
    return "unsupported"


def _level_text(series: pd.Series) -> pd.Series:
    """Each non-null value as its level text; nulls stay null. Fit and apply share this spelling."""
    return series.map(_level_of, na_action="ignore")


def _level_of(value: object) -> str:
    if hasattr(value, "item") and not isinstance(value, str | bytes):  # numpy scalars
        value = value.item()
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _top_levels(series: pd.Series) -> tuple[str, ...]:
    counts = _level_text(series).dropna().value_counts()
    ranked = sorted(
        ((int(count), str(level)) for level, count in counts.items()), key=lambda p: (-p[0], p[1])
    )
    return tuple(level for _, level in ranked[:MAX_CATEGORY_LEVELS])


def _pii_columns(frame: pd.DataFrame, columns: Sequence[str]) -> set[str]:
    """Columns the engine's one PII detector flags, for when no Phase 1 report is available."""
    from engine.stages.ingest import detect_pii

    found: set[str] = set()
    for name in columns:
        series = frame[name]
        observed = series.dropna().head(TYPE_SAMPLE_ROWS)
        if len(observed) == 0:
            continue
        if detect_pii(observed, name, _inferred_type(series)):
            found.add(name)
    return found


def fit_feature_spec(
    frame: pd.DataFrame,
    config: UseCaseConfig,
    *,
    primary_key: str,
    target: str,
    treatment_column: str | None,
    validation: ValidationReport | None = None,
) -> FeatureSpec:
    """Decide the features once, on the training file; :func:`apply_feature_spec` replays it.

    Precedence, first match wins: personal data (unless `prepare.pii_handling` is `keep`), then
    Phase 1's `will_be_dropped` findings the user did not acknowledge, then - only for columns Phase 1
    did not already rule on - the direct constant and identifier tests, then the type: dates and
    unsupported types are dropped, text/boolean/category columns become categoricals and numbers
    stay numbers. An empty feature list is returned as such; the caller decides what that means.
    """
    reserved = reserved_feature_exclusions(
        config, primary_key=primary_key, target=target, treatment_column=treatment_column
    )
    columns = _non_reserved(frame, reserved)
    pii_handling = config.prepare.pii_handling
    dropped = {
        name: reason for name, reason in _phase1_drops(validation, pii_handling).items() if name in columns
    }
    if validation is None and pii_handling is not PiiHandling.KEEP:
        for name in sorted(_pii_columns(frame, columns)):
            dropped[name] = _PII_REASON
    kept_by_user = _acknowledged_keeps(validation)

    features: list[str] = []
    levels: dict[str, tuple[str, ...]] = {}
    for name in columns:
        if name in dropped:
            continue
        series = frame[name]
        kind = _column_kind(series)
        if kind == "date":
            dropped[name] = _DATE_REASON
            continue
        if kind == "unsupported":
            dropped[name] = _UNSUPPORTED_REASON
            continue
        if int(series.nunique(dropna=True)) <= 1 and (_CONSTANT_REASON, name) not in kept_by_user:
            dropped[name] = _CONSTANT_REASON
            continue
        if _looks_like_id(series, name) and (_ID_LIKE_REASON, name) not in kept_by_user:
            dropped[name] = _ID_LIKE_REASON
            continue
        features.append(name)
        if kind == "categorical":
            levels[name] = _top_levels(series)
    ordered_drops = {name: dropped[name] for name in columns if name in dropped}
    return FeatureSpec(feature_columns=tuple(features), categorical_levels=levels, dropped=ordered_drops)


def apply_feature_spec(frame: pd.DataFrame, spec: FeatureSpec) -> pd.DataFrame:
    """The feature matrix in spec order: `float64` numerics and fixed-level `category` columns.

    A feature missing from `frame` raises `KeyError` naming it (scoring a file without a column the
    model was trained on cannot be answered honestly). A level the spec does not know becomes NaN.
    The index of `frame` is kept.
    """
    import pandas as pd

    missing = [name for name in spec.feature_columns if name not in frame.columns]
    if missing:
        raise KeyError(f"The file has no column {missing[0]!r}, which the uplift model was trained on.")
    data: dict[str, Any] = {}
    for name in spec.feature_columns:
        series = frame[name]
        levels = spec.categorical_levels.get(name)
        if levels is None:
            data[name] = pd.to_numeric(series, errors="coerce").astype("float64")
        else:
            data[name] = pd.Categorical(_level_text(series), categories=list(levels))
    return pd.DataFrame(data, index=frame.index, columns=list(spec.feature_columns))


# ---------------------------------------------------------------------------
# The hold-out
# ---------------------------------------------------------------------------
def split_holdout(t: IntArray, y: IntArray, *, test_fraction: float, seed: int) -> tuple[IntArray, IntArray]:
    """`(train positions, test positions)`, both sorted, stratified on the four `(t, y)` cells.

    Stratifying on treatment *and* outcome keeps the hold-out's treated share and both arms'
    conversion rates equal to the file's, which is what makes its Qini curve comparable across runs.
    Each cell sends `round(test_fraction * size)` rows (halves up) to the test side, drawn with
    `np.random.default_rng(seed)`.
    """
    import numpy as np

    treatment = np.asarray(t)
    outcome = np.asarray(y)
    if treatment.shape != outcome.shape or treatment.ndim != 1:
        raise ValueError("t and y must be one-dimensional arrays of the same length.")
    if not 0.0 < test_fraction < 1.0:
        raise ValueError("test_fraction must lie strictly between 0 and 1.")
    rng = np.random.default_rng(seed)
    test_parts: list[IntArray] = []
    for arm in (0, 1):
        for label in (0, 1):
            cell = np.flatnonzero((treatment == arm) & (outcome == label))
            size = math.floor(len(cell) * test_fraction + 0.5)
            if size:
                test_parts.append(rng.permutation(cell)[:size])
    test = np.sort(np.concatenate(test_parts)) if test_parts else np.empty(0, dtype=np.int_)
    mask = np.ones(len(treatment), dtype=bool)
    mask[test] = False
    return np.asarray(np.flatnonzero(mask), dtype=np.int_), np.asarray(test, dtype=np.int_)
