"""The report of a campaign measured on an amount (Plan J M102, DEC-1312).

`engine.uplift.incrementality.measure_incrementality(outcome_kind="continuous")` decides who is compared,
who is mature and which amounts are usable exactly as for a yes/no outcome, then hands the amounts here.
This module turns them into an `IncrementalityReport`: the means, Welch's difference and interval, the
adjusted estimate when a covariate was named, the skew warning and one plain summary. The statistics
are `engine.measurement.continuous`'s; nothing here computes a number of its own beyond sums and
counts.

On such a report the yes/no fields keep the meaning they can have: `treated_rows` and `control_rows`
are the customers measured, `treated_conversions` and `control_conversions` the customers whose amount
is above zero (who spent anything), and the rates, `absolute_lift` and `incremental_conversions` are
null - a rate of an amount means nothing. `relative_lift` is the difference over the held-back mean
(when that mean is above zero) and `p_value` is Welch's. Every M102 field is set.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

import numpy as np

from engine.measurement.continuous import (
    OUTCOME_SKEWED,
    AdjustedDifference,
    MeanDifference,
    adjusted_difference,
    mean_difference,
    skewed_arms,
)

if TYPE_CHECKING:
    from numpy.typing import NDArray

    from engine.uplift.contracts import IncrementalityReport, IncrementalityStatus

__all__ = ["amount_report", "amount_text"]

CONFIDENCE_LEVEL = 0.95


def amount_text(value: float) -> str:
    """An amount as the summary prints it: grouped digits and two decimals, `1,234.50`."""
    return f"{value:,.2f}"


def _signed(value: float) -> str:
    """A signed amount, `+12.30`; one that rounds to zero prints `+0.00`, never `-0.00`."""
    if round(value, 2) == 0.0:
        value = 0.0
    return f"{value:+,.2f}"


def amount_report(
    *,
    run_id: str,
    campaign_id: str | None,
    outcome_column: str,
    outcome_window_days: int | None,
    as_of: datetime,
    status: IncrementalityStatus,
    results_available_on: date | None,
    members: int,
    treated: NDArray[np.float64],
    control: NDArray[np.float64],
    covariate_column: str | None,
    treated_covariate: NDArray[np.float64] | None,
    control_covariate: NDArray[np.float64] | None,
    rows_covariate_missing: int | None,
    rows_immature: int,
    rows_without_outcome: int,
    rows_outside: int,
    has_control_group: bool,
) -> IncrementalityReport:
    """The report of a measured amount. `treated` / `control` are the usable amounts of each arm.

    With `covariate_column`, `treated_covariate` and `control_covariate` hold each usable customer's
    amount from before the campaign, an unknown one already set to the mean of the known
    (`rows_covariate_missing` of them); both None when no customer had a known one.
    """
    from engine.uplift.contracts import ConfidenceValue, IncrementalityReport, IncrementalityStatus

    measurable = status is IncrementalityStatus.MATURE
    compared = mean_difference(treated, control) if measurable else None
    adjusted: AdjustedDifference | None = None
    note: str | None = None
    if covariate_column is not None and compared is not None:
        if treated_covariate is None or control_covariate is None:
            note = (
                f"No measured customer has a known amount in {covariate_column!r} from before the campaign, "
                f"so nothing could be adjusted."
            )
        else:
            adjusted = adjusted_difference(treated, treated_covariate, control, control_covariate)
            if adjusted is None:
                note = (
                    f"{covariate_column!r} is the same for every measured customer, so it cannot narrow the "
                    f"range and the estimate is not adjusted."
                )
    warnings = (OUTCOME_SKEWED,) if compared is not None and skewed_arms(treated, control) else None

    def interval(value: float, low: float, high: float) -> ConfidenceValue:
        return ConfidenceValue(value=value, ci_low=low, ci_high=high, confidence_level=CONFIDENCE_LEVEL)

    summary = _summary(
        status=status,
        members=members,
        treated_rows=len(treated),
        control_rows=len(control),
        compared=compared,
        adjusted=adjusted,
        covariate_column=covariate_column,
        note=note,
        skewed=warnings is not None,
        rows_immature=rows_immature,
        results_available_on=results_available_on,
        outcome_window_days=outcome_window_days,
        has_control_group=has_control_group,
    )
    control_mean = compared.control_mean if compared is not None else None
    return IncrementalityReport(
        run_id=run_id,
        campaign_id=campaign_id,
        outcome_column=outcome_column,
        outcome_window_days=outcome_window_days,
        as_of=as_of,
        status=status,
        results_available_on=results_available_on,
        treated_rows=len(treated),
        treated_conversions=int(np.count_nonzero(treated > 0.0)),
        treated_rate=None,
        control_rows=len(control),
        control_conversions=int(np.count_nonzero(control > 0.0)),
        control_rate=None,
        absolute_lift=None,
        relative_lift=(
            compared.difference / control_mean
            if compared is not None and control_mean is not None and control_mean > 0.0
            else None
        ),
        incremental_conversions=None,
        p_value=compared.p_value if compared is not None else None,
        rows_immature=rows_immature,
        rows_without_outcome=rows_without_outcome,
        rows_suppressed_or_untreated=rows_outside,
        causal=has_control_group,
        summary=summary,
        computed_at=datetime.now(UTC),
        outcome_kind="continuous",
        treated_mean=_mean(treated) if measurable else None,
        control_mean=_mean(control) if measurable else None,
        mean_difference=compared.difference if compared is not None else None,
        mean_difference_ci=(
            interval(compared.difference, compared.low, compared.high) if compared is not None else None
        ),
        covariate_column=covariate_column,
        adjusted_lift=adjusted.difference if adjusted is not None else None,
        adjusted_interval=(
            interval(adjusted.difference, adjusted.low, adjusted.high) if adjusted is not None else None
        ),
        variance_reduction=adjusted.variance_reduction if adjusted is not None else None,
        rows_covariate_missing=rows_covariate_missing if covariate_column is not None else None,
        adjustment_note=note,
        outcome_warnings=warnings,
    )


def _mean(values: NDArray[np.float64]) -> float | None:
    return float(np.mean(values)) if len(values) else None


def _summary(
    *,
    status: IncrementalityStatus,
    members: int,
    treated_rows: int,
    control_rows: int,
    compared: MeanDifference | None,
    adjusted: AdjustedDifference | None,
    covariate_column: str | None,
    note: str | None,
    skewed: bool,
    rows_immature: int,
    results_available_on: date | None,
    outcome_window_days: int | None,
    has_control_group: bool,
) -> str:
    """One plain paragraph for the campaign page, as `measure_incrementality`'s sentence is for a rate."""
    from engine.uplift.contracts import IncrementalityStatus

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
    if compared is None:
        return (
            f"An average needs at least two customers in each group with a recorded amount; this file has "
            f"{treated_rows:,} contacted and {control_rows:,} held back, so the difference cannot be measured."
        )
    p_value = compared.p_value
    p_text = "—" if p_value is None else ("p < 0.001" if p_value < 0.001 else f"p = {p_value:.3f}")
    head = (
        f"Contacted customers averaged {amount_text(compared.treated_mean)} against "
        f"{amount_text(compared.control_mean)} for the control group"
    )
    excludes_zero = compared.low > 0.0 or compared.high < 0.0
    if excludes_zero:
        sentence = (
            f"{head}: a difference of {_signed(compared.difference)} per customer (95% CI "
            f"{_signed(compared.low)} to {_signed(compared.high)}; {p_text}), about "
            f"{abs(compared.difference) * treated_rows:,.0f} {'more' if compared.difference > 0 else 'less'} "
            f"in total across the contacted customers."
        )
    else:
        sentence = (
            f"{head}: a difference of {_signed(compared.difference)} per customer, but the 95% interval "
            f"({_signed(compared.low)} to {_signed(compared.high)}; {p_text}) includes zero, so the campaign "
            f"cannot be shown to have changed the amount."
        )
    if adjusted is not None:
        removed = max(adjusted.variance_reduction, 0.0)
        sentence += (
            f" Taking account of each customer's {covariate_column!r} from before the campaign, the "
            f"difference is {_signed(adjusted.difference)} per customer (95% CI {_signed(adjusted.low)} "
            f"to {_signed(adjusted.high)}); this removed {removed:.0%} of the chance variation."
        )
    elif note is not None:
        sentence += f" {note}"
    if skewed:
        sentence += (
            " A few very large amounts dominate these averages, so the range may be too narrow; more "
            "customers in each group would make it reliable."
        )
    if rows_immature:
        when = results_available_on.isoformat() if results_available_on is not None else "—"
        sentence += (
            f" {rows_immature} customers are still inside the outcome window and are not counted; "
            f"all results are in on {when}."
        )
    return sentence
