"""Did the campaign work? Treated rate minus control rate, with an honest interval (plan B §6, Stage D).

A scoring run acts on customers and, by Phase 1's design, holds a random control group back from
the eligible rows (`engine.stages.actions`). Once the outcomes are known, the difference between the
two arms' conversion rates is the campaign's *incremental* effect: the control group is what the
treated customers would have done without the contact. This module turns a run's `scores` and an
uploaded outcomes file into `incrementality_report.json`. It is a pure function of its inputs - no
storage, no network - so the API route and the tests call the same code.

**Who is compared.** Suppressed rows were never eligible for treatment *or* for the holdout, so they
are always left out. Of the rest:

* an uplift run passes `intended_column` (the scores' `intended_treatment`): the rows the policy
  selected plus the control rows it *would* have selected. Comparing treated and control inside that
  set compares like with like - comparing the treated persuadables with a control group of every
  segment would mix the policy's effect with the difference between segments;
* otherwise `bands` restricts both arms to the named bands (the same rule on both sides, so the
  comparison stays fair);
* otherwise every eligible row is compared: intent to treat.

Inside the population the treated arm is every row not in the control group and the control arm is
every row with `control_group` true.

**Maturity.** An outcome such as "reactivated within 90 days" is only known once 90 days have passed.
A row's treatment date is `outcomes[treatment_date_column]` (parsed as UTC) when given, else the run's
`treatment_time`; the row is mature iff `date + outcome_window_days ≤ as_of`. Immature rows are
excluded and counted, never guessed: counting a not-yet-converted customer as a non-converter would
bias the lift towards zero. With no mature row at all the report says when results will be available
instead of showing rates.

**Rows without a usable outcome** - no matching key in the outcomes file, a null outcome, or a
treatment date that cannot be read - are excluded and counted in `rows_without_outcome`. None of
them is filled in: a missing outcome is not a non-conversion.

**Statistics.** The difference of two proportions gets the Newcombe hybrid score interval
(Newcombe 1998, method 10): the two arms' Wilson score intervals `(l1, u1)`, `(l2, u2)` combine as

    L = d − sqrt((p1 − l1)² + (u2 − p2)²)      U = d + sqrt((u1 − p1)² + (p2 − l2)²)

with `d = p1 − p2`. Unlike the textbook Wald interval it stays inside `[-1, 1]` and keeps close to
its nominal coverage with small arms or rates near 0 - exactly the conditions of a small control
group and a rare conversion. The p-value is the pooled two-proportion z-test, two-sided, via
`math.erfc`; it is `None` when the pooled rate is 0 or 1 (both arms identical at the boundary), where
the statistic is 0/0 and no number would be honest. `relative_lift = d / control_rate` and
`incremental_conversions = d × treated_rows`, whose interval is the lift's interval scaled the same
way. The 95 % quantile is the exact normal quantile (`NormalDist().inv_cdf(0.975) ≈ 1.95996`).

`pandas` is imported inside the function bodies, never at module level, so `import engine` stays
fast.
"""

from __future__ import annotations

import math
import time
from datetime import UTC, datetime
from statistics import NormalDist
from typing import TYPE_CHECKING, Final

from engine.uplift.contracts import ConfidenceValue, IncrementalityReport, IncrementalityStatus
from engine.utils.logging import get_logger, log_stage

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

    import pandas as pd

__all__ = [
    "CONFIDENCE_LEVEL",
    "Z_95",
    "measure_incrementality",
    "newcombe_interval",
    "two_proportion_p_value",
    "wilson_interval",
]

_LOGGER = get_logger(__name__)

CONFIDENCE_LEVEL: Final[float] = 0.95
Z_95: Final[float] = NormalDist().inv_cdf(0.5 + CONFIDENCE_LEVEL / 2.0)
"""The two-sided 95 % standard normal quantile, ≈ 1.959964."""

_BAND_COLUMN: Final[str] = "band"
_CONTROL_COLUMN: Final[str] = "control_group"
_SUPPRESSED_COLUMN: Final[str] = "suppressed_reason"

_TRUE_TEXT: Final[frozenset[str]] = frozenset({"1", "true", "t", "yes", "y"})
_OUTCOME_TRUE: Final[frozenset[str]] = frozenset({"1", "true", "yes", "y"})
_OUTCOME_FALSE: Final[frozenset[str]] = frozenset({"0", "false", "no", "n"})
"""Outcome text accepted without a `positive_label` (plan B §6: 1/true/yes/y count as converted)."""


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------
def wilson_interval(successes: int, trials: int, *, z: float = Z_95) -> tuple[float, float]:
    """The Wilson score interval of a binomial proportion (no continuity correction).

    Raises `ValueError` for `trials <= 0` or `successes` outside `0..trials`: a proportion of nobody
    has no interval.
    """
    if trials <= 0 or not 0 <= successes <= trials:
        raise ValueError("A Wilson interval needs trials > 0 and 0 <= successes <= trials.")
    p = successes / trials
    z2 = z * z
    denominator = 1.0 + z2 / trials
    centre = p + z2 / (2.0 * trials)
    half = z * math.sqrt(p * (1.0 - p) / trials + z2 / (4.0 * trials * trials))
    low = max(0.0, (centre - half) / denominator)
    high = min(1.0, (centre + half) / denominator)
    return low, high


def newcombe_interval(x1: int, n1: int, x2: int, n2: int, *, z: float = Z_95) -> tuple[float, float, float]:
    """`(p1 − p2, lower, upper)`: the Newcombe hybrid score interval (1998, method 10).

    Arm 1 is the treated arm, arm 2 the control arm. Built from the two Wilson intervals as in the
    module docstring; Newcombe's own example 56/70 vs 48/80 gives 0.2000 (0.0524 to 0.3339).
    """
    l1, u1 = wilson_interval(x1, n1, z=z)
    l2, u2 = wilson_interval(x2, n2, z=z)
    p1, p2 = x1 / n1, x2 / n2
    difference = p1 - p2
    lower = difference - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2)
    upper = difference + math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2)
    return difference, lower, upper


def two_proportion_p_value(x1: int, n1: int, x2: int, n2: int) -> float | None:
    """Two-sided p-value of the pooled two-proportion z-test; `None` when it is undefined.

    `z = (p1 − p2) / sqrt(p̂(1 − p̂)(1/n1 + 1/n2))` with the pooled rate `p̂ = (x1 + x2)/(n1 + n2)`,
    and `p = erfc(|z| / √2)`. Undefined (None) when an arm is empty or `p̂` is 0 or 1.
    """
    if n1 <= 0 or n2 <= 0:
        return None
    pooled = (x1 + x2) / (n1 + n2)
    variance = pooled * (1.0 - pooled) * (1.0 / n1 + 1.0 / n2)
    if variance <= 0.0:
        return None
    statistic = (x1 / n1 - x2 / n2) / math.sqrt(variance)
    return math.erfc(abs(statistic) / math.sqrt(2.0))


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------
def measure_incrementality(
    scores: pd.DataFrame,
    outcomes: pd.DataFrame,
    *,
    run_id: str,
    primary_key: str,
    outcome_column: str,
    positive_label: str | None = None,
    intended_column: str | None = None,
    bands: Sequence[str] | None = None,
    treatment_time: datetime,
    treatment_date_column: str | None = None,
    outcome_window_days: int | None = None,
    as_of: datetime,
    campaign_id: str | None = None,
) -> IncrementalityReport:
    """Measure a scoring run's campaign: treated rate − control rate on the mature rows.

    See the module docstring for the population, maturity and statistics. Raises `ValueError` for a
    missing column, duplicate primary keys, a non-binary outcome or a negative outcome window; the
    messages carry counts and column names, never data values.
    """
    import pandas as pd

    started = time.perf_counter()
    _require_columns(scores, (primary_key, _CONTROL_COLUMN), what="The run's scores")
    _require_columns(outcomes, (primary_key, outcome_column), what="The outcomes file")
    if intended_column is not None:
        _require_columns(scores, (intended_column,), what="The run's scores")
    if intended_column is None and bands is not None:
        _require_columns(scores, (_BAND_COLUMN,), what="The run's scores")
    if treatment_date_column is not None:
        _require_columns(outcomes, (treatment_date_column,), what="The outcomes file")
    if outcome_window_days is not None and outcome_window_days < 0:
        raise ValueError("outcome_window_days cannot be negative.")
    as_of_utc = _aware(as_of)
    treatment_utc = _aware(treatment_time)

    score_keys = _keys(scores[primary_key])
    outcome_keys = _keys(outcomes[primary_key])
    _require_unique(score_keys, what="The run's scores")
    _require_unique(outcome_keys, what="The outcomes file")

    # --- population ---------------------------------------------------------------------------
    suppressed = _suppressed(scores[_SUPPRESSED_COLUMN]) if _SUPPRESSED_COLUMN in scores.columns else None
    eligible = pd.Series(True, index=scores.index) if suppressed is None else ~suppressed
    if intended_column is not None:
        in_population = eligible & _flag(scores[intended_column])
    elif bands is not None:
        wanted = {str(band) for band in bands}
        in_population = eligible & scores[_BAND_COLUMN].astype("string").isin(wanted).fillna(value=False)
    else:
        in_population = eligible
    population = in_population.to_numpy(dtype=bool)
    held_out = _flag(scores[_CONTROL_COLUMN]).to_numpy(dtype=bool)
    treated = ~held_out & population
    rows_outside = int((~population).sum())
    has_control_group = bool((held_out & eligible.to_numpy(dtype=bool)).any())

    # --- join the outcomes ---------------------------------------------------------------------
    converted = _coerce_outcome(outcomes[outcome_column], positive_label)
    by_key = pd.DataFrame({"converted": converted.to_numpy()}, index=pd.Index(outcome_keys.to_numpy()))
    if treatment_date_column is not None:
        by_key["date"] = _parse_dates(outcomes[treatment_date_column]).to_numpy()
    members = pd.DataFrame({"key": score_keys[population].to_numpy(), "treated": treated[population]})
    matched = members["key"].isin(by_key.index).to_numpy(dtype=bool)
    joined = by_key.reindex(members["key"].to_numpy())
    if treatment_date_column is not None:
        dates = pd.Series(pd.to_datetime(joined["date"].to_numpy(), errors="coerce", utc=True))
    else:
        dates = pd.Series(pd.Timestamp(treatment_utc), index=pd.RangeIndex(len(members)))
    date_ok = dates.notna().to_numpy()
    outcome_known = joined["converted"].notna().to_numpy()

    if outcome_window_days is None:
        mature = matched & date_ok
        ready_at = None
    else:
        window = pd.Timedelta(days=outcome_window_days)
        ready_at = dates + window
        mature = matched & date_ok & (ready_at <= pd.Timestamp(as_of_utc)).to_numpy()
    immature = matched & date_ok & ~mature
    usable = mature & outcome_known
    rows_without_outcome = int(len(members) - usable.sum() - immature.sum())
    rows_immature = int(immature.sum())

    results_available_on: date | None = None
    if ready_at is not None and rows_immature:
        results_available_on = ready_at[immature].max().date()

    arm = members["treated"].to_numpy(dtype=bool)
    values = joined["converted"].to_numpy()
    treated_rows = int((usable & arm).sum())
    control_rows = int((usable & ~arm).sum())
    treated_conversions = int(values[usable & arm].astype(bool).sum()) if treated_rows else 0
    control_conversions = int(values[usable & ~arm].astype(bool).sum()) if control_rows else 0

    status = (
        IncrementalityStatus.IMMATURE if rows_immature and not mature.any() else IncrementalityStatus.MATURE
    )
    measurable = status is IncrementalityStatus.MATURE
    treated_rate = treated_conversions / treated_rows if measurable and treated_rows else None
    control_rate = control_conversions / control_rows if measurable and control_rows else None

    absolute_lift: ConfidenceValue | None = None
    incremental: ConfidenceValue | None = None
    relative_lift: float | None = None
    p_value: float | None = None
    if treated_rate is not None and control_rate is not None:
        difference, low, high = newcombe_interval(
            treated_conversions, treated_rows, control_conversions, control_rows
        )
        absolute_lift = ConfidenceValue(
            value=difference, ci_low=low, ci_high=high, confidence_level=CONFIDENCE_LEVEL
        )
        incremental = ConfidenceValue(
            value=difference * treated_rows,
            ci_low=low * treated_rows,
            ci_high=high * treated_rows,
            confidence_level=CONFIDENCE_LEVEL,
        )
        relative_lift = difference / control_rate if control_rate > 0.0 else None
        p_value = two_proportion_p_value(treated_conversions, treated_rows, control_conversions, control_rows)

    summary = _summary(
        status=status,
        members=len(members),
        treated_rows=treated_rows,
        control_rows=control_rows,
        treated_rate=treated_rate,
        control_rate=control_rate,
        absolute_lift=absolute_lift,
        incremental=incremental,
        p_value=p_value,
        rows_immature=rows_immature,
        results_available_on=results_available_on,
        outcome_window_days=outcome_window_days,
        has_control_group=has_control_group,
    )
    report = IncrementalityReport(
        run_id=run_id,
        campaign_id=campaign_id,
        outcome_column=outcome_column,
        outcome_window_days=outcome_window_days,
        as_of=as_of_utc,
        status=status,
        results_available_on=results_available_on,
        treated_rows=treated_rows,
        treated_conversions=treated_conversions,
        treated_rate=treated_rate,
        control_rows=control_rows,
        control_conversions=control_conversions,
        control_rate=control_rate,
        absolute_lift=absolute_lift,
        relative_lift=relative_lift,
        incremental_conversions=incremental,
        p_value=p_value,
        rows_immature=rows_immature,
        rows_without_outcome=rows_without_outcome,
        rows_suppressed_or_untreated=rows_outside,
        causal=has_control_group,
        summary=summary,
        computed_at=datetime.now(UTC),
    )
    _LOGGER.info(
        "incrementality status=%s treated_rows=%d control_rows=%d rows_immature=%d "
        "rows_without_outcome=%d rows_outside=%d",
        status.value,
        treated_rows,
        control_rows,
        rows_immature,
        rows_without_outcome,
        rows_outside,
    )
    log_stage(_LOGGER, "incrementality", rows=len(scores), seconds=time.perf_counter() - started)
    return report


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _require_columns(frame: pd.DataFrame, columns: Sequence[str], *, what: str) -> None:
    missing = [column for column in columns if column not in frame.columns]
    if missing:
        raise ValueError(f"{what} has no column {', '.join(repr(column) for column in missing)}.")


def _require_unique(keys: pd.Series, *, what: str) -> None:
    duplicated = int(keys.duplicated().sum())
    if duplicated:
        raise ValueError(
            f"{what} repeats {duplicated} primary key value(s); each customer must appear once, "
            f"or their outcome would be counted twice."
        )


def _aware(moment: datetime) -> datetime:
    """`moment` in UTC; a naive datetime is read as UTC, like everywhere else in the engine."""
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def _parse_dates(values: pd.Series) -> pd.Series:
    """Treatment dates as UTC timestamps; unreadable values become NaT (counted, never guessed).

    ISO 8601 is parsed in one vectorised pass; only the values it could not read are retried one by
    one, so a large file of clean dates stays fast and a few odd formats still parse.
    """
    import pandas as pd

    parsed = pd.to_datetime(values, errors="coerce", utc=True, format="ISO8601")
    retry = parsed.isna() & values.notna()
    if retry.any():
        parsed[retry] = pd.to_datetime(values[retry], errors="coerce", utc=True, format="mixed")
    return parsed.reset_index(drop=True)


def _keys(values: pd.Series) -> pd.Series:
    """Primary keys as trimmed text, so `1` from one file matches `"1"` from the other."""
    return values.astype("string").str.strip().reset_index(drop=True)


def _flag(values: pd.Series) -> pd.Series:
    """A boolean series for a flag column (`control_group`, `intended_treatment`) read from JSON or CSV.

    Booleans pass through; 0/1 and `true/false/yes/no` text are read; nulls and anything else are
    False - a row not recorded as held out was not held out.
    """
    import pandas as pd

    if pd.api.types.is_bool_dtype(values.dtype):
        return values.fillna(value=False).astype(bool)
    numeric = pd.to_numeric(values, errors="coerce")
    from_number = numeric.notna() & (numeric != 0)
    text = values.astype("string").str.strip().str.lower()
    from_text = text.isin(_TRUE_TEXT).fillna(value=False)
    return (from_number | from_text).astype(bool)


def _suppressed(values: pd.Series) -> pd.Series:
    """True where a suppression reason is recorded (a null or blank cell means not suppressed)."""
    text = values.astype("string").str.strip()
    return (
        (text.notna() & (text != "") & ~text.str.lower().isin({"nan", "none", "null"}))
        .fillna(value=False)
        .astype(bool)
    )


def _coerce_outcome(values: pd.Series, positive_label: str | None) -> pd.Series:
    """The outcome as a nullable boolean series (null stays null); raises when it is not binary.

    With `positive_label`, a value converts iff its text equals the label (case-insensitive, an
    integral number written without its `.0`), and the column may hold at most two distinct values.
    Without it, `1/true/yes/y` convert and `0/false/no/n` do not; anything else is refused.
    """
    import pandas as pd

    text = values.astype("string").str.strip().str.lower()
    if not pd.api.types.is_bool_dtype(values.dtype):
        # 1.0 read from a CSV is the same answer as 1: integral numbers are written without `.0`.
        numeric = pd.to_numeric(values, errors="coerce").astype("float64")
        integral = numeric.notna() & (numeric == numeric.round()) & (numeric.abs() < 2**53)
        text = text.where(~integral, numeric.round().astype("Int64").astype("string"))
    present = text.notna() & (text != "")
    if positive_label is not None:
        distinct = int(text[present].nunique())
        if distinct > 2:
            raise ValueError(
                f"The outcome column holds {distinct} distinct values; an incrementality report needs "
                f"a binary outcome."
            )
        label = str(positive_label).strip().lower()
        try:
            as_number = float(label)
            if as_number.is_integer():
                label = str(int(as_number))
        except ValueError:
            pass
        converted = text == label
    else:
        known = text.isin(_OUTCOME_TRUE | _OUTCOME_FALSE).fillna(value=False)
        unknown = int((present & ~known).sum())
        if unknown:
            raise ValueError(
                f"{unknown} outcome value(s) are neither 1/true/yes/y nor 0/false/no/n; name the "
                f"converted value with positive_label."
            )
        converted = text.isin(_OUTCOME_TRUE)
    result: pd.Series = converted.astype("boolean").mask(~present.astype(bool))
    return result.reset_index(drop=True)


def _points(rate: float) -> str:
    """A difference of two rates in percentage points, signed: `+2.3 points`."""
    return f"{rate * 100:+.1f} points"


def _summary(
    *,
    status: IncrementalityStatus,
    members: int,
    treated_rows: int,
    control_rows: int,
    treated_rate: float | None,
    control_rate: float | None,
    absolute_lift: ConfidenceValue | None,
    incremental: ConfidenceValue | None,
    p_value: float | None,
    rows_immature: int,
    results_available_on: date | None,
    outcome_window_days: int | None,
    has_control_group: bool,
) -> str:
    """One plain-language sentence (two at most) for the Campaign results page."""
    if not has_control_group:
        return (
            "This run held no control group back, so there is nothing to compare the treated "
            "customers with and the campaign's effect cannot be measured."
        )
    if status is IncrementalityStatus.IMMATURE:
        when = results_available_on.isoformat() if results_available_on is not None else "—"
        return (
            f"Results available on {when}: the {outcome_window_days}-day outcome window has not "
            f"elapsed yet for any of the {rows_immature} treated and control customers."
        )
    if members == 0 or (treated_rows == 0 and control_rows == 0):
        return "No treated or control customer of this run has a usable outcome, so there is nothing to measure yet."
    if absolute_lift is None or treated_rate is None or control_rate is None or incremental is None:
        empty = "control" if control_rows == 0 else "treated"
        return f"No {empty} customer has a usable outcome yet, so the lift cannot be measured."
    low = absolute_lift.ci_low if absolute_lift.ci_low is not None else math.nan
    high = absolute_lift.ci_high if absolute_lift.ci_high is not None else math.nan
    p_text = "—" if p_value is None else ("p < 0.001" if p_value < 0.001 else f"p = {p_value:.3f}")
    rates = (
        f"Treated customers converted at {treated_rate:.1%} against {control_rate:.1%} for the "
        f"control group"
    )
    if absolute_lift.excludes_zero:
        direction = "extra" if absolute_lift.value > 0 else "fewer"
        sentence = (
            f"{rates}: a lift of {_points(absolute_lift.value)} (95% CI {_points(low)} to "
            f"{_points(high)}; {p_text}), about {abs(incremental.value):,.0f} {direction} conversions "
            f"caused by the campaign."
        )
    else:
        sentence = (
            f"{rates}: a difference of {_points(absolute_lift.value)}, but the 95% interval "
            f"({_points(low)} to {_points(high)}; {p_text}) includes zero, so the campaign cannot be "
            f"shown to have changed the outcome."
        )
    if rows_immature:
        when = results_available_on.isoformat() if results_available_on is not None else "—"
        sentence += (
            f" {rows_immature} customers are still inside the outcome window and are not counted; "
            f"all results are in on {when}."
        )
    return sentence
