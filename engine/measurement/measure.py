"""The one measurement path (Plan J M94, DEC-1304 (c), (h)): every measured campaign result comes from here.

Three routes measured a campaign before M94 - `POST /runs/{id}/campaign-results`, `POST
/runs/{id}/measure` (which calls the first) and M49's `/outcomes` - and later features (the Proof Pack,
revenue outcomes, audits of other tools' campaigns) would each have added a fourth. Now
`measure_campaign` is the only function that turns an assignment and an outcomes file into an
`IncrementalityReport`:

* `api.routes.uplift.create_campaign_results` calls it with the run's scores, so `/campaign-results`
  and `/runs/{id}/measure` give the reports they always gave;
* `POST /campaigns/{id}/measure` calls it with the campaign's `assignment.parquet` and its registered
  test plan;
* M49's `/outcomes` stays model monitoring: `incrementality_input.json` is a descriptive input there,
  never presented as a campaign result.

**A wrapper, not a second computation.** The numbers are `engine.uplift.incrementality
.measure_incrementality`'s, unchanged: the same population rule, maturity rule, Newcombe interval and
p-value, under the same keyword names. What this function adds is the campaign's own frame (an
assignment is translated to the columns the computation reads, `engine.measurement.campaign
.as_scores_frame`) and the test plan: it refuses a measurement that differs from the plan in force
(`TestPlanChangedError`, `TEST_PLAN_CHANGED`), records the plan's hash on the report, and labels a read
before the plan's analysis date an **early look** - numbers, but no final verdict
(`campaign_verdict_for` returns none for it, and its summary states the rates so far without the
conclusion `measure_incrementality`'s sentence ends with).

**Each group's effect (Plan J M104, DEC-1314).** `measure_campaign_segments` reads the same campaign once per
band, predicted segment and offer, with the false-alarm guard of the Value Proof Pack's backfire check
(`engine.measurement.segments`); every route that stores a campaign report stores its
`segment_effects.json` beside it.

**The scoring run's prediction (Plan K M135, DEC-1324).** A plan registered with `covariate_source: run_score`
adjusts the outcome - yes/no or an amount - by the chance of the outcome the campaign's scoring run
predicted for each customer before the campaign (`engine.measurement.run_score`). The caller reads it
(`load_run_prediction`) and passes it as `run_prediction`; this function uses it only when the plan in force
registered it, refuses it with no plan or against a plan that did not (`TEST_PLAN_CHANGED`), and refuses it
for several offers. On a yes/no report the adjusted difference in rates is the headline (`headline_estimate`,
`campaign_verdict_for`), as the adjusted amount is on an amount's: it is the analysis the plan registered.

**`as_of` always comes from the caller.** `utc_now()` lives in the route, never here, so the same
inputs give the same report on any day.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from engine.measurement.campaign import as_scores_frame
from engine.measurement.plan import (
    PlanDifference,
    TestPlanChangedError,
    is_early_look,
    plan_differences,
    realised_population,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date, datetime

    import pandas as pd

    from engine.config import PrimaryKey
    from engine.measurement.continuous import OutcomeKind
    from engine.measurement.plan import TestPlan
    from engine.measurement.run_score import CovariateSource, RunPrediction
    from engine.measurement.segments import SegmentEffects
    from engine.uplift.contracts import ConfidenceValue, IncrementalityReport
    from engine.uplift.measure import CampaignVerdict

__all__ = [
    "EARLY_LOOK_PREFIX",
    "adjusted_by",
    "amount_verdict",
    "campaign_verdict_for",
    "early_look_summary",
    "headline_estimate",
    "measure_campaign",
    "measure_campaign_segments",
]

EARLY_LOOK_PREFIX = "Early look"


def measure_campaign(
    assignment: pd.DataFrame,
    outcomes: pd.DataFrame,
    *,
    run_id: str,
    primary_key: PrimaryKey,
    outcome_column: str,
    positive_label: str | None = None,
    intended_column: str | None = None,
    bands: Sequence[str] | None = None,
    treatment_time: datetime,
    treatment_date_column: str | None = None,
    outcome_window_days: int | None = None,
    as_of: datetime,
    campaign_id: str | None = None,
    plan: TestPlan | None = None,
    covariate_column: str | None = None,
    arm_column: str | None = None,
    arms: Sequence[str] | None = None,
    control_level: str | None = None,
    outcome_kind: OutcomeKind = "binary",
    covariate_date_column: str | None = None,
    combine_offers: bool = False,
    covariate_source: CovariateSource | None = None,
    run_prediction: RunPrediction | None = None,
) -> IncrementalityReport:
    """Measure a campaign: `measure_incrementality` on `assignment`, checked against `plan`.

    `assignment` is a run's scores or a campaign's `assignment.parquet`. With no `plan` the report is
    exactly `measure_incrementality`'s. With one, a measurement that differs from it raises
    `TestPlanChangedError` (a `ValueError`) before anything is computed; otherwise the report carries
    `test_plan_hash` and, before the plan's analysis date, `early_look` with a summary of the counts
    so far and no conclusion (`early_look_summary`).
    `covariate_column` is compared with the plan now and used by M102's adjusted estimate. Raises
    `ValueError` for anything `measure_incrementality` refuses.

    **Amounts and the adjusted estimate (Plan J M102, DEC-1312).** `outcome_kind="continuous"` measures
    an amount (`measure_incrementality`'s Welch difference in means); the plan's `outcome_kind` must
    match. `covariate_column` (with `covariate_date_column`, the date each value was measured up to)
    adjusts an amount by what each customer had before the campaign, and is used **only when the
    registered test plan named that covariate in advance**: with no plan, a covariate on an amount is
    refused with `TestPlanChangedError` (`TEST_PLAN_CHANGED`) just as a covariate different from the
    plan's is, because choosing the adjustment after seeing the outcomes is one more way to move the
    goalposts. A yes/no outcome never uses a covariate column (the scoring run's prediction is M135's,
    below), so with no plan it is ignored and the report is exactly as before M102. Several offers
    (`arm_column`) are measured on a yes/no outcome only: an amount there is refused with a plain
    `ValueError` (DEC-1312), since no nightly check covers per-offer amounts or their adjustment yet; a
    covariate on a yes/no several-offer campaign is ignored, as before.

    **Several offers (Plan J M100, DEC-668 (2)).** With `arm_column`, the column naming each treated
    customer's offer, every offer is measured against the shared control (`control_group`) by
    `measure_incrementality` on that offer's customers and the control's, and the report carries
    `arms` in `arms` order. `arms` (the configured offers, `uplift.treatment_levels[1:]`) and
    `control_level` (`uplift.treatment_levels[0]`) are then required (`ValueError` otherwise), so the
    report's own fields are the first configured offer's against the control (DEC-668 (3)) whatever
    the file's row order; a treated customer with no offer named is in no offer's comparison. Without
    `arm_column` nothing changes.

    **Every offer together (Plan J M104 fix, DEC-1314 (s)).** With `combine_offers` and two or more
    offers the report also carries `offers_combined`: every configured offer's customers pooled against
    the shared control (`engine.measurement.arms.combined_offers`), what the Value Proof Pack adds up.
    Off by default, so this function's report is byte for byte what it was; every route that stores a
    campaign report of several offers turns it on.

    **The scoring run's prediction (Plan K M135, DEC-1324).** `covariate_source="run_score"` adjusts the
    outcome, yes/no or an amount, by `run_prediction` (`engine.measurement.run_score.load_run_prediction`),
    with M102's estimator. Only when the registered plan says `run_score`: with no plan it is refused
    (`TestPlanChangedError`, as an unregistered covariate is), against a plan that did not register it it is
    a change of plan like any other. `covariate_column` must then be None, `run_prediction` must be given,
    and several offers are refused (`ValueError`), as M102 refuses an amount there.
    """
    from engine.uplift.incrementality import measure_incrementality

    frame = as_scores_frame(assignment)
    run_score = covariate_source == "run_score"
    if run_score and covariate_column is not None:
        raise ValueError(
            "Adjust by one thing at a time: the scoring run's prediction or a column from before the campaign."
        )
    if run_score and arm_column is not None:
        raise ValueError(
            "Several offers are measured without an adjustment: adjusting each offer by the scoring run's "
            "prediction is not offered yet. Measure the campaign without it, or register a plan that does not "
            "name it."
        )
    if plan is None and run_score:
        raise TestPlanChangedError(
            (PlanDifference(field="covariate_source", planned="none", realised="run_score"),), unplanned=True
        )
    if plan is None and covariate_column is not None and outcome_kind == "continuous":
        raise TestPlanChangedError(
            (PlanDifference(field="covariate_column", planned="none", realised=covariate_column),),
            unplanned=True,
        )
    if arm_column is not None and outcome_kind != "binary":
        raise ValueError(
            "Several offers are measured on a yes/no outcome only: measuring each offer on an amount, or "
            "adjusting it by an amount from before the campaign, is not offered yet. Measure the campaign "
            "as a whole on the amount, or each offer on a yes/no outcome."
        )
    if plan is not None:
        differences = plan_differences(
            plan,
            realised=realised_population(frame, intended_column=intended_column, bands=bands),
            outcome_column=outcome_column,
            positive_label=positive_label,
            outcome_window_days=outcome_window_days,
            covariate_column=covariate_column,
            outcome_kind=outcome_kind,
            covariate_source=covariate_source,
        )
        if differences:
            raise TestPlanChangedError(differences)
    if run_score and run_prediction is None:
        raise ValueError(
            "The scoring run's prediction was not read, so the planned adjustment cannot be made."
        )
    if arm_column is not None:
        from engine.measurement.arms import measure_arms

        report = measure_arms(
            frame,
            outcomes,
            arm_column=arm_column,
            arms=arms,
            control_level=control_level,
            run_id=run_id,
            primary_key=primary_key,
            outcome_column=outcome_column,
            positive_label=positive_label,
            intended_column=intended_column,
            bands=bands,
            treatment_time=treatment_time,
            treatment_date_column=treatment_date_column,
            outcome_window_days=outcome_window_days,
            as_of=as_of,
            campaign_id=campaign_id,
            combine_offers=combine_offers,
        )
    else:
        report = measure_incrementality(
            frame,
            outcomes,
            run_id=run_id,
            primary_key=primary_key,
            outcome_column=outcome_column,
            positive_label=positive_label,
            intended_column=intended_column,
            bands=bands,
            treatment_time=treatment_time,
            treatment_date_column=treatment_date_column,
            outcome_window_days=outcome_window_days,
            as_of=as_of,
            campaign_id=campaign_id,
            outcome_kind=outcome_kind,
            covariate_column=covariate_column,
            covariate_date_column=covariate_date_column,
            run_prediction=run_prediction if run_score else None,
        )
    if plan is None:
        return report
    early = is_early_look(plan, as_of)
    update: dict[str, object] = {"test_plan_hash": plan.plan_hash, "early_look": early}
    if early:
        update["summary"] = early_look_summary(report, plan, as_of)
    return report.model_copy(update=update)


def measure_campaign_segments(
    assignment: pd.DataFrame,
    outcomes: pd.DataFrame,
    report: IncrementalityReport,
    *,
    primary_key: PrimaryKey,
    outcome_column: str,
    positive_label: str | None = None,
    intended_column: str | None = None,
    treatment_time: datetime,
    treatment_date_column: str | None = None,
    offers: pd.Series[Any] | None = None,
    control_level: str | None = None,
) -> SegmentEffects:
    """Each group's effect beside `report`, measured as `measure_campaign` measured the campaign (Plan J M104).

    `campaigns/<id>/segment_effects.json` (`engine.measurement.segments`): the same population, maturity and
    interval rules on each band, predicted segment and offer, with the false-alarm guard of the Value Proof
    Pack's backfire check. `report` is the one `measure_campaign` just returned on the same `assignment` and
    `outcomes`; its window and `as_of` are used, so the groups are read exactly as the whole was. Every route
    that stores a campaign report stores this file beside it (DEC-1314).
    """
    from engine.measurement.segments import measure_segment_effects

    return measure_segment_effects(
        assignment,
        outcomes,
        report,
        primary_key=primary_key,
        outcome_column=outcome_column,
        positive_label=positive_label,
        intended_column=intended_column,
        treatment_time=treatment_time,
        treatment_date_column=treatment_date_column,
        outcome_window_days=report.outcome_window_days,
        as_of=report.as_of,
        offers=offers,
        control_level=control_level,
    )


def early_look_summary(report: IncrementalityReport, plan: TestPlan, as_of: datetime) -> str:
    """The summary of an early look: the rates and group sizes so far, and no conclusion.

    `measure_incrementality`'s own sentence ends with the verdict ("about N extra conversions caused
    by the campaign", "cannot be shown to have changed the outcome"); read before the planned date that
    is the peeking the plan exists to prevent, so an early look says only what was counted, and when.
    """
    from datetime import UTC

    moment = as_of.replace(tzinfo=UTC) if as_of.tzinfo is None else as_of.astimezone(UTC)
    head = (
        f"{EARLY_LOOK_PREFIX}, before the planned analysis date of {_day(plan.analysis_date)}: "
        f"not a final result."
    )
    if report.treated_mean is not None and report.control_mean is not None:  # an amount (Plan J M102)
        counted = (
            f"So far, as of {_day(moment.date())}, {report.treated_rows:,} contacted customers averaged "
            f"{report.treated_mean:,.2f} and {report.control_rows:,} held-back customers {report.control_mean:,.2f}."
        )
    elif report.treated_rate is None or report.control_rate is None:
        counted = (
            f"So far, as of {_day(moment.date())}, outcomes are in for {report.treated_rows:,} contacted "
            f"and {report.control_rows:,} held-back customers."
        )
    else:
        counted = (
            f"So far, as of {_day(moment.date())}, {report.treated_rate:.1%} of {report.treated_rows:,} "
            f"contacted customers and {report.control_rate:.1%} of {report.control_rows:,} held-back "
            f"customers had the outcome."
        )
    return f"{head} {counted} The result is read on {_day(plan.analysis_date)}."


def _day(value: date) -> str:
    return f"{value.day} {value:%b %Y}"


def campaign_verdict_for(
    report: IncrementalityReport, *, outcome_is_good: bool, outcome_label: str | None = None
) -> CampaignVerdict | None:
    """The plain verdict of `report` (`engine.uplift.measure.campaign_verdict`), or none for an early look.

    An early look is shown with its numbers and the label, never with "The campaign added about N":
    reading a verdict before the planned date is exactly the peeking the plan exists to prevent.
    """
    from engine.uplift.measure import campaign_verdict

    if report.early_look:
        return None
    amount = report.outcome_kind == "continuous" and report.causal and report.status.value == "mature"
    if amount and (report.treated_rows or report.control_rows):
        return amount_verdict(report, outcome_is_good=outcome_is_good, outcome_label=outcome_label)
    if report.outcome_kind != "continuous" and report.adjusted_interval is not None:
        return _adjusted_rate_verdict(report, outcome_is_good=outcome_is_good, outcome_label=outcome_label)
    return campaign_verdict(report, outcome_is_good=outcome_is_good, outcome_label=outcome_label)


def _adjusted_rate_verdict(
    report: IncrementalityReport, *, outcome_is_good: bool, outcome_label: str | None
) -> CampaignVerdict:
    """The verdict of a yes/no report adjusted by the scoring run's prediction (Plan K M135).

    `campaign_verdict`'s rules and words, read off the adjusted difference in rates (the analysis the plan
    registered) and the outcomes it implies among the contacted customers, with one sentence saying so.
    """
    from engine.uplift.contracts import ConfidenceValue
    from engine.uplift.measure import VerdictKind, campaign_verdict

    adjusted = report.adjusted_interval
    assert adjusted is not None  # the caller checked
    rows = report.treated_rows

    def scaled(value: float | None) -> float | None:
        return None if value is None else value * rows

    incremental = ConfidenceValue(
        value=adjusted.value * rows,
        ci_low=scaled(adjusted.ci_low),
        ci_high=scaled(adjusted.ci_high),
        confidence_level=adjusted.confidence_level,
    )
    read = report.model_copy(update={"absolute_lift": adjusted, "incremental_conversions": incremental})
    verdict = campaign_verdict(read, outcome_is_good=outcome_is_good, outcome_label=outcome_label)
    if verdict.kind not in {
        VerdictKind.ADDED,
        VerdictKind.PREVENTED,
        VerdictKind.HARMED,
        VerdictKind.NO_CLEAR_EFFECT,
    }:
        return verdict
    return verdict.model_copy(update={"detail": f"{verdict.detail} {adjusted_by(report)}"})


def adjusted_by(report: IncrementalityReport) -> str:
    """One sentence naming what an adjusted estimate took account of, as the plan registered it."""
    if report.covariate_source == "run_score":
        what = "what" if report.outcome_kind == "continuous" else "the chance of the outcome"
        return (
            f"Adjusted for {what} the scoring run predicted for each customer before the campaign, as the test "
            f"plan registered."
        )
    return f"Adjusted for each customer's {report.covariate_column!r} before the campaign."


def headline_estimate(report: IncrementalityReport) -> tuple[ConfidenceValue | None, bool]:
    """The per-customer difference a verdict or a value reads off a report, and whether it is adjusted.

    The adjusted estimate when the report has one: it was registered in the test plan before the outcomes
    were read, so it is the planned analysis, and it has the narrower range. Otherwise Welch's difference
    for an amount, and the Newcombe difference in rates for a yes/no outcome (Plan K M135: a yes/no report
    is adjusted only by the scoring run's prediction its plan registered).
    """
    if report.adjusted_interval is not None:
        return report.adjusted_interval, True
    if report.outcome_kind != "continuous":
        return report.absolute_lift, False
    return report.mean_difference_ci, False


def amount_verdict(
    report: IncrementalityReport, *, outcome_is_good: bool, outcome_label: str | None = None
) -> CampaignVerdict:
    """The plain verdict of a campaign measured on an amount (Plan J M102): the total it changed, in the
    amount's own unit, read off `headline_estimate` times the contacted customers."""
    from engine.uplift.measure import CampaignVerdict, VerdictKind

    estimate, adjusted = headline_estimate(report)
    if estimate is None or estimate.ci_low is None or estimate.ci_high is None:
        return CampaignVerdict(
            kind=VerdictKind.NOT_ENOUGH,
            headline="Not enough results yet",
            detail=(
                f"An average needs at least two contacted and two held-back customers with an amount; this "
                f"file has {report.treated_rows:,} contacted and {report.control_rows:,} held back."
            ),
        )
    if not estimate.excludes_zero:
        return CampaignVerdict(
            kind=VerdictKind.NO_CLEAR_EFFECT,
            headline="No clear effect yet",
            detail=(
                "Contacted and held-back customers had about the same amount, so the difference could be "
                "chance. The test can only show a change at or above its detectable effect, the smallest "
                "change a group of this size can see. A bigger group or a longer wait lowers it."
            ),
        )
    rows = report.treated_rows
    amount = round(abs(estimate.value * rows))
    low, high = sorted((round(abs(estimate.ci_low * rows)), round(abs(estimate.ci_high * rows))))
    what = outcome_label or report.outcome_column
    likely = f"Likely between {low:,} and {high:,} in total ({estimate.value:+,.2f} per contacted customer)."
    if adjusted:
        likely += f" {adjusted_by(report)}"
    went_up = estimate.value > 0
    if outcome_is_good and went_up:
        kind, headline = VerdictKind.ADDED, f"The campaign added about {amount:,} to {what}"
    elif not outcome_is_good and not went_up:
        kind, headline = VerdictKind.PREVENTED, f"The campaign cut {what} by about {amount:,}"
    elif outcome_is_good:
        kind, headline = VerdictKind.HARMED, f"The campaign cost about {amount:,} of {what}"
        likely = "Contacted customers did worse than the ones held back. " + likely
    else:
        kind, headline = VerdictKind.HARMED, f"The campaign raised {what} by about {amount:,}"
        likely = "Contacted customers did worse than the ones held back. " + likely
    return CampaignVerdict(
        kind=kind, headline=headline, detail=likely, amount=amount, likely_low=low, likely_high=high
    )
