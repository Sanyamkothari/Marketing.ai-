"""Synthetic data generator for the Marketing AI engine (plan §10).

Everything this module produces is **synthetic**: it is generated from a seeded pseudo-random
number generator and describes no real person, account, order or asset. It exists so the M2+
tests (and a developer at a terminal) have realistic-looking, deterministic CSVs to ingest,
validate, train on and score. It is never a source of numbers shown to a user (plan §13.3), and
every file it writes carries ``synthetic`` in its name.

Design notes
------------
*Config driven.* Nothing here branches on a use-case id. The generator reads the use-case config
through :func:`engine.config.load_use_case` and builds one column per ``template.columns`` entry,
dispatching on ``(role, type)`` through :data:`_MAKERS`. Ranges, category levels, date cadence,
blank rates and boolean rates are all derived from the **five example values the template itself
carries**, so a new use case needs no change here.

*Learnable signal.* The target is a genuine logistic function of the feature columns plus a time
trend plus unobservable noise, with the intercept solved so the realised positive rate matches
``positive_rate``. The draw is 0/1 internally and is *rendered* through the labels the template
declares (:func:`target_labels`), so a use case whose file spells the outcome ``Yes``/``No``
(plan §4.1: a binary target may carry any two values) gets ``Yes``/``No`` with no code that knows
which use case that is. A plain logistic regression on the clean targeted-advertisement data
clears the plan §10 golden check (ROC-AUC > 0.7) by a wide margin;
``tests/fixtures/test_make_data.py`` asserts it.

*Deterministic.* Every draw comes from a :class:`numpy.random.Generator` seeded by a BLAKE2b digest
of ``(seed, use_case_id, column_name, purpose)``. No global RNG, no dict-ordering dependence and no
clock reads, so the same spec always produces a byte-identical CSV, and adding a column to one use
case cannot shift the values of another.

*Frame shape.* Booleans and dates are carried as the strings the templates use (``true``/``false``,
ISO ``YYYY-MM-DD``, ``""`` for blank) so the DataFrame and the CSV agree exactly with
``templates/<use_case>_template.csv``; integers use pandas' nullable ``Int64`` so missing values
render as empty fields rather than ``12.0``.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import math
import re
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Final

import numpy as np
import numpy.typing as npt
import pandas as pd

from engine.config import (
    ColumnRole,
    ColumnType,
    TemplateColumn,
    UseCaseConfig,
    load_all_use_cases,
    load_use_case,
)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_ROWS: Final[int] = 10_000
DEFAULT_SEED: Final[int] = 20_260_921
DEFAULT_POSITIVE_RATE: Final[float] = 0.12

#: Number of days the generated snapshot dates span, ending at the latest template example date.
SNAPSHOT_SPAN_DAYS: Final[int] = 364
#: How far back a `last_contacted_at` date may sit behind its row's snapshot date.
CONTACT_LOOKBACK_DAYS: Final[int] = 120

#: Logistic coefficients: the k-th feature column (template order) gets
#: ``FEATURE_WEIGHT_BASE * FEATURE_WEIGHT_DECAY**k`` with alternating sign.
FEATURE_WEIGHT_BASE: Final[float] = 1.3
FEATURE_WEIGHT_DECAY: Final[float] = 0.8
#: Weight on the standardised snapshot-date offset; small, but enough that an ordered
#: (time-based) split sees a genuinely different positive rate in train and test.
TIME_TREND_WEIGHT: Final[float] = 0.35
#: Standard deviation of the unobservable part of the logit. Caps the achievable ROC-AUC.
NOISE_SD: Final[float] = 1.2
#: Weight on the product of the first two feature signals, and on the (centred) square of the
#: third. Plan §10 asks the test ROC-AUC to beat *both* 0.7 and a logistic baseline, so the truth
#: cannot be purely linear in the features: these two terms are what a tree can exploit and a plain
#: logistic regression cannot. They stay small enough that the linear part still dominates.
INTERACTION_WEIGHT: Final[float] = 1.5
CURVATURE_WEIGHT: Final[float] = 1.1

#: Null rates injected into numeric feature columns, keyed by their index among the numeric
#: feature columns. Positions 1 and 2 are used so the strongest signal (position 0) stays intact.
#: The plan does not say which columns should be missing, so this is a documented choice.
NUMERIC_NULL_RATES: Final[Mapping[int, float]] = MappingProxyType({1: 0.04, 2: 0.09})

_DIGITS_RE: Final[re.Pattern[str]] = re.compile(r"^(?P<prefix>.*?)(?P<digits>\d+)$")


# ---------------------------------------------------------------------------
# Variants (plan §6.3 validation codes)
# ---------------------------------------------------------------------------
CLEAN: Final[str] = "clean"
SCORING: Final[str] = "scoring"


@dataclass(frozen=True, slots=True)
class Variant:
    """One named fixture flavour.

    ``code`` is the single plan §6.3 validation code the variant is built to trigger; ``None`` for
    the two healthy variants. ``rows`` / ``positive_rate``, when set, override the spec because the
    variant is *defined* by that shape (``too_few_rows`` must have too few rows).

    ``requires_roles`` names the template roles the variant needs a column for. A use case mapped
    onto an outside file need not carry every optional role - the public Telco Customer Churn file
    has no date column at all - and a variant that breaks a column the template does not have
    simply does not apply there (:func:`variant_applies`).
    """

    name: str
    code: str | None
    has_target: bool
    description: str
    rows: int | None = None
    positive_rate: float | None = None
    requires_roles: tuple[ColumnRole, ...] = ()


_VARIANT_LIST: Final[tuple[Variant, ...]] = (
    Variant(CLEAN, None, True, "Healthy training file: 10k rows, learnable signal, ~12% positives."),
    Variant(SCORING, None, False, "Healthy scoring file: the same columns without the target."),
    Variant(
        "duplicate_keys",
        "PK_NOT_UNIQUE",
        True,
        "50 rows repeat the primary key of row 0.",
    ),
    Variant("null_keys", "PK_NULLS", True, "20 rows have a blank primary key."),
    Variant(
        "leaky_column",
        "LEAKAGE_SUSPECTED",
        True,
        "Adds 'campaign_result_score', a post-outcome number that predicts the target at AUC ~0.99.",
    ),
    Variant(
        "too_few_positives",
        "TARGET_TOO_FEW_POSITIVES",
        True,
        "150 positives (< min_positive 200) but still 1.5% of rows, so the imbalance warning stays quiet.",
    ),
    Variant(
        "too_few_rows",
        "ROWS_TOO_FEW",
        True,
        "900 rows (< min_rows 1000) at a 30% positive rate, so only the row count is wrong.",
        rows=900,
        positive_rate=0.30,
    ),
    Variant(
        "constant_target",
        "TARGET_CONSTANT",
        True,
        "Every row carries the positive label. TARGET_IMBALANCE_SEVERE (warning) comes with it.",
    ),
    Variant(
        "non_binary_target",
        "TARGET_NOT_BINARY",
        True,
        "A third target level on 120 rows, spelled the way the template spells the other two.",
    ),
    Variant(
        "constant_column",
        "CONSTANT_COLUMN",
        True,
        "Adds 'data_source', one distinct value everywhere.",
    ),
    Variant(
        "high_null_column",
        "HIGH_NULL_COLUMN",
        True,
        "Adds 'survey_nps_score', 82% blank.",
    ),
    Variant(
        "pii_column",
        "PII_DETECTED",
        True,
        "Adds fake 'billing_contact_email' (@example.invalid) and 'billing_contact_phone' (+1-555-555-xxxx).",
    ),
    Variant(
        "id_like_column",
        "HIGH_CARDINALITY_ID_LIKE",
        True,
        "Adds 'external_ref', distinct on every row and not the primary key.",
    ),
    Variant(
        "unparseable_time",
        "TIME_COLUMN_UNPARSEABLE",
        True,
        "The time column holds fiscal-week labels such as 'FY26-W32' that no date parser accepts.",
        requires_roles=(ColumnRole.TIME,),
    ),
    Variant(
        "renamed_column",
        "SCHEMA_MISMATCH",
        False,
        "Scoring file whose first feature column is renamed to '<name>_v2'.",
    ),
    Variant(
        "missing_column",
        "SCHEMA_MISMATCH",
        False,
        "Scoring file with the first feature column dropped.",
    ),
    Variant(
        "type_changed_column",
        "SCHEMA_MISMATCH",
        False,
        "Scoring file whose first numeric feature arrives as text, with 'unknown' in 5% of rows.",
    ),
)

VARIANT_SPECS: Final[Mapping[str, Variant]] = MappingProxyType({v.name: v for v in _VARIANT_LIST})

#: Variant name -> the plan §6.3 validation code it is designed to trigger (``None`` = healthy).
VARIANTS: Final[Mapping[str, str | None]] = MappingProxyType({v.name: v.code for v in _VARIANT_LIST})

#: Names of the columns this module adds on top of a template, per variant.
LEAKY_COLUMN: Final[str] = "campaign_result_score"
CONSTANT_COLUMN: Final[str] = "data_source"
HIGH_NULL_COLUMN: Final[str] = "survey_nps_score"
PII_EMAIL_COLUMN: Final[str] = "billing_contact_email"
PII_PHONE_COLUMN: Final[str] = "billing_contact_phone"
ID_LIKE_COLUMN: Final[str] = "external_ref"
#: What `type_changed_column` writes where a number is missing. Deliberately not a pandas NA token.
TYPE_CHANGE_SENTINEL: Final[str] = "unknown"

#: The third level `non_binary_target` plants when the target is not numeric. Any value that is
#: neither label would do; the plan does not name one, so this is a documented choice.
THIRD_TARGET_LABEL: Final[str] = "Unknown"

#: How a target spells "positive" when `target.positive_label` is null and the engine must guess.
#: The same tokens `engine.stages.validate.resolve_positive_label` falls back to.
POSITIVE_TOKENS: Final[frozenset[str]] = frozenset({"1", "true", "yes", "y"})

#: What a target of each type spells "negative" with when the template's examples show one level
#: only, so no second level can be read off them.
_NEGATIVE_FALLBACK: Final[Mapping[ColumnType, str]] = MappingProxyType(
    {ColumnType.INTEGER: "0", ColumnType.FLOAT: "0", ColumnType.BOOLEAN: "false"}
)


def variant_applies(variant: str, config: UseCaseConfig) -> bool:
    """Whether `variant` has anything to break in this use case's template.

    A use case mapped onto an outside file carries only the columns that file has, so a variant
    that corrupts an optional column may have no column to corrupt: the public Telco Customer
    Churn file has no date column, and `unparseable_time` is about a date column. Such a variant
    is *skipped* - `generate` returns the clean frame rather than raising - and a caller driving a
    matrix off `VARIANTS` asks this first so it parametrises over the pairs that mean something.
    """
    return all(config.template.by_role(role) for role in VARIANT_SPECS[variant].requires_roles)


def variants_for(use_case_id: str, config_root: Path | None = None) -> tuple[str, ...]:
    """The variants that mean something for this use case, in `VARIANTS` order."""
    config = load_use_case(use_case_id, config_root)
    return tuple(name for name in VARIANTS if variant_applies(name, config))


# ---------------------------------------------------------------------------
# How the target is spelled (plan §4.1)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TargetLabels:
    """The two (and, for `non_binary_target`, three) values a use case's target column carries.

    `positive` / `negative` / `third` are already in the dtype the frame stores, so a caller can
    compare against them directly. `dtype` is `Int64` for a target whose template type is integer
    and whose labels are integers, and `object` for a labelled one such as `Yes`/`No`.
    """

    positive: str | int
    negative: str | int
    third: str | int
    dtype: str


def _target_column(config: UseCaseConfig) -> TemplateColumn:
    targets = config.template.by_role(ColumnRole.TARGET)
    if not targets:  # pragma: no cover - every predictive template has a target
        raise ValueError(f"{config.id} has no target column")
    return targets[0]


def _label_text(value: str | int | bool) -> str:
    """`target.positive_label` as the template would write it: `true`, not `True`."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _looks_like_an_integer(value: str) -> bool:
    try:
        int(value)
    except ValueError:
        return False
    return True


def target_labels(config: UseCaseConfig) -> TargetLabels:
    """How this use case spells its target, from the template and `target.positive_label` alone.

    Plan §4.1 lets a binary target carry any two values. The positive label is the configured one
    when there is one, otherwise the example level that reads as positive (`1`, `yes`, `true`),
    otherwise the last example level. The negative label is the *other* level the template's five
    examples show - `No` for the Telco file, `0` for the engine's own templates - because the
    examples are the only place a use case says what its second value looks like.
    """
    column = _target_column(config)
    levels = tuple(dict.fromkeys(value.strip() for value in column.examples if value.strip() != ""))
    configured = config.target.positive_label
    positive = _label_text(configured) if configured is not None else ""
    if not positive:
        positive = next(
            (level for level in levels if level.lower() in POSITIVE_TOKENS),
            levels[-1] if levels else "1",
        )
    # Prefer the template's own spelling, so `positive_label: "yes"` still writes the file's `Yes`.
    positive = next((level for level in levels if level.lower() == positive.lower()), positive)
    negative = next(
        (level for level in levels if level.lower() != positive.lower()),
        _NEGATIVE_FALLBACK.get(column.type, "no"),
    )
    third = "2" if column.type in (ColumnType.INTEGER, ColumnType.FLOAT) else THIRD_TARGET_LABEL
    while third.lower() in (positive.lower(), negative.lower()):  # pragma: no cover - no shipped clash
        third = f"{third}_x"
    if column.type is ColumnType.INTEGER and all(
        _looks_like_an_integer(value) for value in (positive, negative, third)
    ):
        return TargetLabels(int(positive), int(negative), int(third), "Int64")
    return TargetLabels(positive, negative, third, "object")


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class GenerationSpec:
    """What to generate. Two specs that compare equal produce byte-identical CSVs."""

    use_case_id: str
    rows: int = DEFAULT_ROWS
    seed: int = DEFAULT_SEED
    variant: str = CLEAN
    positive_rate: float = DEFAULT_POSITIVE_RATE
    config_root: Path | None = None

    def __post_init__(self) -> None:
        if self.variant not in VARIANT_SPECS:
            raise ValueError(f"unknown variant {self.variant!r}; known: {', '.join(sorted(VARIANT_SPECS))}")
        if self.rows < 1:
            raise ValueError(f"rows must be >= 1, got {self.rows}")
        if not 0.0 < self.positive_rate < 1.0:
            raise ValueError(f"positive_rate must be in (0, 1), got {self.positive_rate}")

    @property
    def spec_variant(self) -> Variant:
        return VARIANT_SPECS[self.variant]

    @property
    def effective_rows(self) -> int:
        override = self.spec_variant.rows
        return self.rows if override is None else override

    @property
    def effective_positive_rate(self) -> float:
        override = self.spec_variant.positive_rate
        return self.positive_rate if override is None else override

    @property
    def file_name(self) -> str:
        """``<use_case>_synthetic_<variant>.csv`` — 'synthetic' is part of the contract (§13.3)."""
        stem = self.use_case_id.replace("-", "_")
        return f"{stem}_synthetic_{self.variant}.csv"


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------
def _seed_for(*parts: object) -> int:
    """A stable 64-bit seed from the given parts. Independent of PYTHONHASHSEED and of dict order."""
    payload = "|".join(str(part) for part in parts).encode("utf-8")
    return int.from_bytes(hashlib.blake2b(payload, digest_size=8).digest(), "big")


def _rng(*parts: object) -> np.random.Generator:
    return np.random.default_rng(_seed_for(*parts))


# ---------------------------------------------------------------------------
# Hints derived from the template's own five example values
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class NumericHint:
    """The range and shape of a numeric column, derived from its five template examples."""

    mean: float
    sd: float
    low: float
    high: float
    decimals: int
    is_count: bool
    is_lognormal: bool = False


@dataclass(frozen=True, slots=True)
class CategoryHint:
    levels: tuple[str, ...]
    weights: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class DateHint:
    anchor: date
    cadence_days: int
    periods: int
    blank_rate: float


def _example_floats(column: TemplateColumn) -> tuple[float, ...]:
    return tuple(float(value) for value in column.examples if value.strip() != "")


def _decimals(column: TemplateColumn) -> int:
    widths = [len(value.partition(".")[2]) for value in column.examples if "." in value]
    return max(widths) if widths else 0


def numeric_hint(column: TemplateColumn) -> NumericHint:
    """Derive a plausible distribution for a numeric column from its example values alone.

    Four shapes, chosen from the examples alone:

    * **count** — integer examples, all in [0, 20]: Poisson at the example mean (tickets, outages).
    * **rate** — float examples all inside [0, 1]: normal clipped to [0, 1], so genuine zeros
      survive the way the templates show them (a click-through rate of ``0.000``).
    * **positive magnitude** — every example strictly positive: lognormal matched to the example
      mean and spread, which gives the right-skewed tail a spend, a latency or a tenure really has
      and avoids the pile-up a clipped normal would leave at zero.
    * **signed** — anything else (an example is zero or negative): normal clipped to the range.
    """
    values = _example_floats(column)
    if not values:  # pragma: no cover - every shipped template has numeric examples
        return NumericHint(mean=0.0, sd=1.0, low=0.0, high=1.0, decimals=0, is_count=False)
    lowest, highest = min(values), max(values)
    mean = float(np.mean(values))
    spread = max(highest - lowest, abs(mean) * 0.5, 1e-9)
    decimals = _decimals(column)
    integer = column.type is ColumnType.INTEGER
    if integer and lowest >= 0.0 and highest <= 20.0:
        return NumericHint(
            mean=max(mean, 0.5), sd=0.0, low=0.0, high=highest * 4.0 + 4.0, decimals=0, is_count=True
        )
    if not integer and lowest >= 0.0 and highest <= 1.0:
        return NumericHint(mean=mean, sd=spread / 2.0, low=0.0, high=1.0, decimals=decimals, is_count=False)
    low = 0.0 if lowest >= 0.0 else lowest - spread
    return NumericHint(
        mean=mean,
        sd=spread / 2.0,
        low=low,
        high=highest + spread,
        decimals=decimals,
        is_count=False,
        is_lognormal=lowest > 0.0,
    )


def category_hint(column: TemplateColumn) -> CategoryHint:
    """Levels in template order; weights from how often each level appears among the examples."""
    levels = tuple(dict.fromkeys(value for value in column.examples if value.strip() != ""))
    counts = [float(column.examples.count(level)) + 0.5 for level in levels]
    total = sum(counts)
    return CategoryHint(levels=levels, weights=tuple(count / total for count in counts))


def _blank_rate(column: TemplateColumn) -> float:
    blanks = sum(1 for value in column.examples if value.strip() == "")
    return blanks / len(column.examples)


def date_hint(column: TemplateColumn) -> DateHint:
    """Anchor on the latest example date; take the cadence from the smallest gap between examples."""
    parsed = sorted({date.fromisoformat(value) for value in column.examples if value.strip() != ""})
    anchor = parsed[-1] if parsed else date(2026, 8, 1)
    gaps = [(later - earlier).days for earlier, later in itertools.pairwise(parsed)]
    positive_gaps = [gap for gap in gaps if gap > 0]
    cadence = min(positive_gaps) if positive_gaps else 1
    periods = max(SNAPSHOT_SPAN_DAYS // cadence, 2)
    return DateHint(anchor=anchor, cadence_days=cadence, periods=periods, blank_rate=_blank_rate(column))


def _bool_rate(column: TemplateColumn) -> float:
    true_count = sum(1 for value in column.examples if value.strip().lower() == "true")
    return min(max(true_count / len(column.examples), 0.15), 0.9)


# ---------------------------------------------------------------------------
# Column makers, registered by (role, type)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Column:
    """One generated column: what goes in the frame, plus its standardised view for the signal."""

    role: ColumnRole
    values: list[str] | npt.NDArray[np.float64]
    dtype: str
    signal: npt.NDArray[np.float64] | None = None


@dataclass(frozen=True, slots=True)
class MakerContext:
    column: TemplateColumn
    rows: int
    seed: int
    use_case_id: str
    #: Day offsets of the snapshot date, 0 = oldest. ``None`` while the time column is being built.
    snapshot_offsets: npt.NDArray[np.float64] | None
    snapshot_anchor: date | None

    def rng(self, purpose: str) -> np.random.Generator:
        return _rng(self.seed, self.use_case_id, self.column.name, purpose)


Maker = Callable[[MakerContext], Column]


def _standardise(values: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
    sd = float(np.std(values))
    if sd <= 0.0:
        return np.zeros_like(values)
    return (values - float(np.mean(values))) / sd


def _sample_numeric(hint: NumericHint, rows: int, rng: np.random.Generator) -> npt.NDArray[np.float64]:
    if hint.is_count:
        drawn = rng.poisson(lam=hint.mean, size=rows).astype(np.float64)
    elif hint.is_lognormal:
        sigma_squared = math.log(1.0 + (hint.sd / hint.mean) ** 2)
        mu = math.log(hint.mean) - sigma_squared / 2.0
        drawn = rng.lognormal(mean=mu, sigma=math.sqrt(sigma_squared), size=rows)
    else:
        drawn = rng.normal(loc=hint.mean, scale=hint.sd, size=rows)
    clipped: npt.NDArray[np.float64] = np.clip(drawn, hint.low, hint.high)
    return np.round(clipped, hint.decimals)


def _make_integer(ctx: MakerContext) -> Column:
    values = _sample_numeric(numeric_hint(ctx.column), ctx.rows, ctx.rng("integer"))
    return Column(role=ctx.column.role, values=values, dtype="Int64", signal=_standardise(values))


def _make_float(ctx: MakerContext) -> Column:
    values = _sample_numeric(numeric_hint(ctx.column), ctx.rows, ctx.rng("float"))
    return Column(role=ctx.column.role, values=values, dtype="float64", signal=_standardise(values))


def _make_category(ctx: MakerContext) -> Column:
    hint = category_hint(ctx.column)
    indices = ctx.rng("category").choice(len(hint.levels), size=ctx.rows, p=list(hint.weights))
    values = [hint.levels[int(index)] for index in indices]
    return Column(
        role=ctx.column.role,
        values=values,
        dtype="object",
        signal=_standardise(indices.astype(np.float64)),
    )


def _make_boolean(ctx: MakerContext) -> Column:
    rate = _bool_rate(ctx.column)
    flags = ctx.rng("boolean").random(ctx.rows) < rate
    values = ["true" if flag else "false" for flag in flags]
    return Column(
        role=ctx.column.role,
        values=values,
        dtype="object",
        signal=_standardise(flags.astype(np.float64)),
    )


def _make_text(ctx: MakerContext) -> Column:
    phrases = tuple(value for value in ctx.column.examples if value.strip() != "")
    blank_rate = _blank_rate(ctx.column)
    rng = ctx.rng("text")
    picked = rng.choice(len(phrases), size=ctx.rows) if phrases else np.zeros(ctx.rows, dtype=np.int64)
    blanks = rng.random(ctx.rows) < blank_rate
    values = [
        "" if blank or not phrases else phrases[int(index)]
        for blank, index in zip(blanks, picked, strict=True)
    ]
    # Free text is excluded from the signal: plan §5 marks it "ignored by the Phase 1 model".
    return Column(role=ctx.column.role, values=values, dtype="object", signal=None)


def _make_primary_key(ctx: MakerContext) -> Column:
    match = _DIGITS_RE.match(ctx.column.examples[0])
    if match is None:  # pragma: no cover - every shipped template key ends in digits
        prefix, width, start = f"{ctx.column.name}-", 6, 1
    else:
        prefix = match.group("prefix")
        width = len(match.group("digits"))
        start = min(
            int(candidate.group("digits"))
            for candidate in (_DIGITS_RE.match(value) for value in ctx.column.examples)
            if candidate is not None
        )
    span = max(ctx.rows * 3, ctx.rows + 1)
    numbers = np.sort(ctx.rng("primary_key").choice(span, size=ctx.rows, replace=False)) + start
    values = [f"{prefix}{int(number):0{width}d}" for number in numbers]
    return Column(role=ctx.column.role, values=values, dtype="object", signal=None)


def _make_snapshot(ctx: MakerContext) -> Column:
    hint = date_hint(ctx.column)
    periods = ctx.rng("snapshot").integers(0, hint.periods, size=ctx.rows).astype(np.float64)
    offsets = (hint.periods - 1 - periods) * hint.cadence_days  # 0 = oldest row
    oldest = hint.anchor - timedelta(days=(hint.periods - 1) * hint.cadence_days)
    values = [(oldest + timedelta(days=int(offset))).isoformat() for offset in offsets]
    if ctx.column.type is ColumnType.DATETIME:
        values = [f"{value}T00:00:00" for value in values]
    return Column(role=ctx.column.role, values=values, dtype="object", signal=_standardise(offsets))


def _make_contact(ctx: MakerContext) -> Column:
    rng = ctx.rng("contact")
    blank_rate = _blank_rate(ctx.column)
    blanks = rng.random(ctx.rows) < blank_rate
    lookback = rng.integers(0, CONTACT_LOOKBACK_DAYS, size=ctx.rows)
    offsets = ctx.snapshot_offsets
    anchor = ctx.snapshot_anchor
    values: list[str] = []
    for index in range(ctx.rows):
        if blanks[index] or offsets is None or anchor is None:
            values.append("")
            continue
        snapshot = anchor + timedelta(days=int(offsets[index]))
        values.append((snapshot - timedelta(days=int(lookback[index]))).isoformat())
    return Column(role=ctx.column.role, values=values, dtype="object", signal=None)


_MAKERS: Final[Mapping[tuple[ColumnRole, ColumnType], Maker]] = MappingProxyType(
    {
        (ColumnRole.PRIMARY_KEY, ColumnType.STRING): _make_primary_key,
        (ColumnRole.PRIMARY_KEY, ColumnType.INTEGER): _make_primary_key,
        (ColumnRole.TIME, ColumnType.DATE): _make_snapshot,
        (ColumnRole.TIME, ColumnType.DATETIME): _make_snapshot,
        (ColumnRole.CONTACT, ColumnType.DATE): _make_contact,
        (ColumnRole.CONTACT, ColumnType.DATETIME): _make_contact,
        (ColumnRole.CONSENT, ColumnType.BOOLEAN): _make_boolean,
        (ColumnRole.FEATURE, ColumnType.INTEGER): _make_integer,
        (ColumnRole.FEATURE, ColumnType.FLOAT): _make_float,
        (ColumnRole.FEATURE, ColumnType.STRING): _make_category,
        (ColumnRole.FEATURE, ColumnType.BOOLEAN): _make_boolean,
        (ColumnRole.FEATURE, ColumnType.TEXT): _make_text,
        (ColumnRole.FEATURE, ColumnType.DATE): _make_snapshot,
    }
)


def maker_for(column: TemplateColumn) -> Maker:
    """The registered maker for a column, or a clear error naming the unregistered pair."""
    try:
        return _MAKERS[(column.role, column.type)]
    except KeyError:
        raise KeyError(
            f"no synthetic-data maker registered for role={column.role.value!r} "
            f"type={column.type.value!r} (column {column.name!r})"
        ) from None


# ---------------------------------------------------------------------------
# The learnable signal
# ---------------------------------------------------------------------------
def _sigmoid(values: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
    return 0.5 * (1.0 + np.tanh(0.5 * values))


def _solve_intercept(logits: npt.NDArray[np.float64], positive_rate: float) -> float:
    """Bisect for the intercept that makes the mean predicted probability equal ``positive_rate``."""
    low, high = -40.0, 40.0
    for _ in range(200):
        middle = (low + high) / 2.0
        if float(np.mean(_sigmoid(logits + middle))) < positive_rate:
            low = middle
        else:
            high = middle
    return (low + high) / 2.0


def _target_values(
    config: UseCaseConfig,
    columns: Mapping[str, Column],
    rows: int,
    seed: int,
    positive_rate: float,
) -> npt.NDArray[np.int64]:
    logits = np.zeros(rows, dtype=np.float64)
    signals: list[npt.NDArray[np.float64]] = []
    for template_column in config.template.columns:
        built = columns.get(template_column.name)
        if built is None or built.signal is None:
            continue
        if built.role is ColumnRole.FEATURE:
            weight = FEATURE_WEIGHT_BASE * (FEATURE_WEIGHT_DECAY ** len(signals))
            logits += (weight if len(signals) % 2 == 0 else -weight) * built.signal
            signals.append(built.signal)
        elif built.role is ColumnRole.TIME:
            logits += TIME_TREND_WEIGHT * built.signal
    if len(signals) >= 2:
        logits += INTERACTION_WEIGHT * signals[0] * signals[1]
    if len(signals) >= 3:
        logits += CURVATURE_WEIGHT * (signals[2] ** 2 - 1.0)
    noise_rng = _rng(seed, config.id, "__target_noise__")
    logits = logits + noise_rng.normal(0.0, NOISE_SD, size=rows)
    logits = logits + _solve_intercept(logits, positive_rate)
    draws = _rng(seed, config.id, "__target_draw__").random(rows)
    return (draws < _sigmoid(logits)).astype(np.int64)


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------
def _build_columns(config: UseCaseConfig, rows: int, seed: int) -> dict[str, Column]:
    """Build every non-target column, the time column first so the contact column can lean on it."""
    ordered = sorted(
        (column for column in config.template.columns if column.role is not ColumnRole.TARGET),
        key=lambda column: 0 if column.role is ColumnRole.TIME else 1,
    )
    built: dict[str, Column] = {}
    snapshot_offsets: npt.NDArray[np.float64] | None = None
    snapshot_anchor: date | None = None
    for template_column in ordered:
        ctx = MakerContext(
            column=template_column,
            rows=rows,
            seed=seed,
            use_case_id=config.id,
            snapshot_offsets=snapshot_offsets,
            snapshot_anchor=snapshot_anchor,
        )
        column = maker_for(template_column)(ctx)
        built[template_column.name] = column
        if template_column.role is ColumnRole.TIME:
            hint = date_hint(template_column)
            snapshot_anchor = hint.anchor - timedelta(days=(hint.periods - 1) * hint.cadence_days)
            snapshot_offsets = np.array(
                [
                    (date.fromisoformat(str(value)[:10]) - snapshot_anchor).days
                    for value in _as_strings(column.values)
                ],
                dtype=np.float64,
            )
    return built


def _as_strings(values: list[str] | npt.NDArray[np.float64]) -> list[str]:
    if isinstance(values, list):
        return values
    return [str(value) for value in values]


def _inject_nulls(config: UseCaseConfig, columns: dict[str, Column], rows: int, seed: int) -> None:
    """Blank out a realistic slice of a couple of numeric feature columns, in place."""
    numeric = [
        column.name
        for column in config.template.columns
        if column.role is ColumnRole.FEATURE and column.type in (ColumnType.INTEGER, ColumnType.FLOAT)
    ]
    for position, rate in NUMERIC_NULL_RATES.items():
        if position >= len(numeric):
            continue
        name = numeric[position]
        current = columns[name]
        values = current.values
        if isinstance(values, list):  # pragma: no cover - numeric columns are arrays
            continue
        mask = _rng(seed, config.id, name, "nulls").random(rows) < rate
        holed = values.astype(np.float64).copy()
        holed[mask] = np.nan
        columns[name] = Column(role=current.role, values=holed, dtype=current.dtype, signal=current.signal)


def _frame(
    config: UseCaseConfig,
    columns: Mapping[str, Column],
    target: npt.NDArray[np.int64] | None,
) -> pd.DataFrame:
    data: dict[str, list[str] | npt.NDArray[np.float64]] = {}
    dtypes: dict[str, str] = {}
    labels = target_labels(config)
    for template_column in config.template.columns:
        if template_column.role is ColumnRole.TARGET:
            if target is None:
                continue
            if labels.dtype == "Int64":
                data[template_column.name] = np.where(
                    target == 1, float(labels.positive), float(labels.negative)
                )
            else:
                positive, negative = str(labels.positive), str(labels.negative)
                data[template_column.name] = [positive if flag else negative for flag in target]
            dtypes[template_column.name] = labels.dtype
            continue
        built = columns[template_column.name]
        data[template_column.name] = built.values
        dtypes[template_column.name] = built.dtype
    frame = pd.DataFrame(data)
    return frame.astype(dtypes)


# ---------------------------------------------------------------------------
# Broken variants
# ---------------------------------------------------------------------------
def _first_feature(config: UseCaseConfig) -> TemplateColumn:
    features = config.template.by_role(ColumnRole.FEATURE)
    if not features:  # pragma: no cover - every shipped template has features
        raise ValueError(f"{config.id} has no feature columns")
    return features[0]


def _first_numeric_feature(config: UseCaseConfig) -> TemplateColumn:
    for column in config.template.by_role(ColumnRole.FEATURE):
        if column.type in (ColumnType.INTEGER, ColumnType.FLOAT):
            return column
    return _first_feature(config)  # pragma: no cover - every shipped template has a numeric feature


def _primary_key_name(config: UseCaseConfig) -> str:
    key = config.template.primary_key
    if key is None:  # pragma: no cover - the config schema requires exactly one
        raise ValueError(f"{config.id} has no primary key column")
    return key.name


def _target_name(config: UseCaseConfig) -> str:
    return _target_column(config).name


def _apply_variant(frame: pd.DataFrame, config: UseCaseConfig, spec: GenerationSpec) -> pd.DataFrame:
    variant = spec.variant
    rows = len(frame)
    seed = spec.seed
    labels = target_labels(config)
    if variant in (CLEAN, SCORING):
        return frame
    if not variant_applies(variant, config):
        return frame  # nothing in this template to break; see `variant_applies`
    if variant == "duplicate_keys":
        key = _primary_key_name(config)
        repeated = min(50, max(rows - 1, 0))
        frame.loc[1:repeated, key] = frame.at[0, key]
        return frame
    if variant == "null_keys":
        key = _primary_key_name(config)
        holes = _rng(seed, config.id, "null_keys").choice(rows, size=min(20, rows), replace=False)
        frame.loc[list(holes), key] = None
        return frame
    if variant == "leaky_column":
        return _add_leaky_column(frame, config, seed)
    if variant == "too_few_positives":
        target = _target_name(config)
        positives = frame.index[frame[target] == labels.positive].tolist()
        frame.loc[positives[150:], target] = labels.negative
        return frame
    if variant == "too_few_rows":
        return frame  # the row count and positive rate are set by the Variant overrides
    if variant == "constant_target":
        frame[_target_name(config)] = labels.positive
        return frame
    if variant == "non_binary_target":
        target = _target_name(config)
        negatives = frame.index[frame[target] == labels.negative].tolist()
        frame.loc[negatives[:120], target] = labels.third
        return frame
    if variant == "constant_column":
        frame[CONSTANT_COLUMN] = "crm_export"
        return frame
    if variant == "high_null_column":
        values = _rng(seed, config.id, "high_null").integers(0, 11, size=rows).astype(np.float64)
        values[_rng(seed, config.id, "high_null_mask").random(rows) < 0.82] = np.nan
        frame[HIGH_NULL_COLUMN] = values
        return frame.astype({HIGH_NULL_COLUMN: "Int64"})
    if variant == "pii_column":
        return _add_pii_columns(frame, config, seed)
    if variant == "id_like_column":
        frame[ID_LIKE_COLUMN] = [
            f"REF-{_seed_for(seed, config.id, index) & 0xFFFFFFFF:08x}" for index in range(rows)
        ]
        return frame
    if variant == "unparseable_time":
        return _break_time_column(frame, config)
    if variant == "renamed_column":
        name = _first_feature(config).name
        return frame.rename(columns={name: f"{name}_v2"})
    if variant == "missing_column":
        return frame.drop(columns=[_first_feature(config).name])
    if variant == "type_changed_column":
        return _change_column_type(frame, config, seed)
    raise ValueError(f"unhandled variant {variant!r}")  # pragma: no cover


def _add_leaky_column(frame: pd.DataFrame, config: UseCaseConfig, seed: int) -> pd.DataFrame:
    """A post-outcome score that all but gives the answer away (single-feature AUC ~0.99).

    Rounded to two decimals on purpose: a continuous column with a distinct value per row would
    also trip HIGH_CARDINALITY_ID_LIKE, and each variant must trigger exactly one code.
    """
    rows = len(frame)
    positive = target_labels(config).positive
    target = (frame[_target_name(config)] == positive).to_numpy(dtype="float64")
    rng = _rng(seed, config.id, "leak")
    noise = rng.normal(0.0, 0.03, size=rows)
    muddled = rng.random(rows) < 0.015
    noise[muddled] = rng.normal(0.0, 0.35, size=int(muddled.sum()))
    frame[LEAKY_COLUMN] = np.round(np.clip(target + noise, -0.2, 1.2), 2)
    return frame


def _add_pii_columns(frame: pd.DataFrame, config: UseCaseConfig, seed: int) -> pd.DataFrame:
    """Obviously fake contact details: RFC 2606 `.invalid` domain, NANP 555-555 fictional exchange.

    Drawn from a pool of rows/4 shared billing contacts so the columns are unmistakably PII without
    also looking like a per-row identifier.
    """
    rows = len(frame)
    pool = max(rows // 4, 1)
    picks = _rng(seed, config.id, "pii").integers(0, pool, size=rows)
    frame[PII_EMAIL_COLUMN] = [f"billing.contact{int(pick):05d}@example.invalid" for pick in picks]
    frame[PII_PHONE_COLUMN] = [f"+1-555-555-{int(pick) % 10000:04d}" for pick in picks]
    return frame


def _break_time_column(frame: pd.DataFrame, config: UseCaseConfig) -> pd.DataFrame:
    times = config.template.by_role(ColumnRole.TIME)
    if not times:  # pragma: no cover - `_apply_variant` skips the variant when there is no time column
        raise ValueError(f"{config.id} has no time column to break")
    name = times[0].name
    labels: list[str] = []
    for value in frame[name].astype("string").tolist():
        parsed = date.fromisoformat(str(value)[:10])
        labels.append(f"FY{parsed.strftime('%y')}-W{parsed.isocalendar().week:02d}")
    frame[name] = labels
    return frame


def _change_column_type(frame: pd.DataFrame, config: UseCaseConfig, seed: int) -> pd.DataFrame:
    """A numeric column that arrives as text because blanks were typed out upstream.

    The sentinel is ``unknown`` rather than ``N/A`` on purpose: ``N/A`` is in pandas' default
    ``na_values``, so a reader would silently turn the column back into a float and the type change
    would vanish before the validator ever saw it.
    """
    name = _first_numeric_feature(config).name
    rows = len(frame)
    unknown = _rng(seed, config.id, "type_change").random(rows) < 0.05
    rendered = frame[name].astype("string").fillna(TYPE_CHANGE_SENTINEL).tolist()
    frame[name] = [
        TYPE_CHANGE_SENTINEL if flag else str(value) for flag, value in zip(unknown, rendered, strict=True)
    ]
    return frame


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def predictive_use_case_ids(config_root: Path | None = None) -> tuple[str, ...]:
    """Every use case this generator can serve: trainable in Phase 1 with a binary target.

    Includes the two hybrids (`rca`, `win-back-campaign`), which train as predictive in Phase 1.
    """
    return tuple(
        sorted(
            use_case.id
            for use_case in load_all_use_cases(config_root).values()
            if use_case.trainable_in_phase_1 and use_case.template.by_role(ColumnRole.TARGET)
        )
    )


def generate(spec: GenerationSpec) -> pd.DataFrame:
    """Build the synthetic frame this spec describes. Pure: same spec, same frame, every time.

    A variant the use case's template has no column for (`variant_applies`) is skipped, and the
    frame comes back clean, so every use case is generable for every variant.
    """
    config = load_use_case(spec.use_case_id, spec.config_root)
    rows = spec.effective_rows
    columns = _build_columns(config, rows, spec.seed)
    target = _target_values(config, columns, rows, spec.seed, spec.effective_positive_rate)
    _inject_nulls(config, columns, rows, spec.seed)
    frame = _frame(config, columns, target if spec.spec_variant.has_target else None)
    return _apply_variant(frame, config, spec)


def write_csv(spec: GenerationSpec, path: Path | None = None) -> Path:
    """Write the frame as UTF-8 CSV and return the path. Defaults to a temp directory (never git)."""
    destination = default_path(spec) if path is None else path
    destination.parent.mkdir(parents=True, exist_ok=True)
    generate(spec).to_csv(destination, index=False, lineterminator="\n", encoding="utf-8")
    return destination


def default_output_dir() -> Path:
    """Where generated CSVs go unless a caller says otherwise: outside the checkout, so never staged."""
    return Path(tempfile.gettempdir()) / "marketing-ai-synthetic"


def default_path(spec: GenerationSpec) -> Path:
    return default_output_dir() / spec.file_name


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="make_data",
        description="Regenerate the synthetic sample CSVs used by the tests (plan §10).",
    )
    parser.add_argument("--use-case", action="append", dest="use_cases", metavar="ID", help="repeatable")
    parser.add_argument("--variant", action="append", dest="variants", metavar="NAME", help="repeatable")
    parser.add_argument("--rows", type=int, default=DEFAULT_ROWS)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--positive-rate", type=float, default=DEFAULT_POSITIVE_RATE)
    parser.add_argument("--out-dir", type=Path, default=None, help=f"default: {default_output_dir()}")
    parser.add_argument("--config-root", type=Path, default=None)
    parser.add_argument("--list-variants", action="store_true", help="print the variant table and exit")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    if args.list_variants:
        width = max(len(name) for name in VARIANTS)
        for name, code in VARIANTS.items():
            print(f"{name:<{width}}  {code or '-':<26}  {VARIANT_SPECS[name].description}")
        return 0
    use_cases = tuple(args.use_cases) if args.use_cases else predictive_use_case_ids(args.config_root)
    variants = tuple(args.variants) if args.variants else tuple(VARIANTS)
    out_dir: Path = args.out_dir if args.out_dir is not None else default_output_dir()
    for use_case_id in use_cases:
        applicable = variants_for(use_case_id, args.config_root)
        for variant in variants:
            if variant not in applicable:
                print(f"{use_case_id}: skipping {variant} (this template has no column to break)")
                continue
            spec = GenerationSpec(
                use_case_id=use_case_id,
                rows=args.rows,
                seed=args.seed,
                variant=variant,
                positive_rate=args.positive_rate,
                config_root=args.config_root,
            )
            written = write_csv(spec, out_dir / spec.file_name)
            print(f"{written}  ({math.trunc(written.stat().st_size / 1024)} KiB)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
