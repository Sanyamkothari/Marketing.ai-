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
(`campaign_verdict_for` returns none for it).

**`as_of` always comes from the caller.** `utc_now()` lives in the route, never here, so the same
inputs give the same report on any day.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from engine.measurement.campaign import as_scores_frame
from engine.measurement.plan import TestPlanChangedError, is_early_look, plan_differences, realised_population

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    import pandas as pd

    from engine.config import PrimaryKey
    from engine.measurement.plan import TestPlan
    from engine.uplift.contracts import IncrementalityReport
    from engine.uplift.measure import CampaignVerdict

__all__ = ["EARLY_LOOK_PREFIX", "campaign_verdict_for", "measure_campaign"]

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
) -> IncrementalityReport:
    """Measure a campaign: `measure_incrementality` on `assignment`, checked against `plan`.

    `assignment` is a run's scores or a campaign's `assignment.parquet`. With no `plan` the report is
    exactly `measure_incrementality`'s. With one, a measurement that differs from it raises
    `TestPlanChangedError` (a `ValueError`) before anything is computed; otherwise the report carries
    `test_plan_hash` and, before the plan's analysis date, `early_look` with its summary saying so.
    `covariate_column` is compared with the plan now and used by M102's adjusted estimate. Raises
    `ValueError` for anything `measure_incrementality` refuses.
    """
    from engine.uplift.incrementality import measure_incrementality

    frame = as_scores_frame(assignment)
    if plan is not None:
        differences = plan_differences(
            plan,
            realised=realised_population(frame, intended_column=intended_column, bands=bands),
            outcome_column=outcome_column,
            positive_label=positive_label,
            outcome_window_days=outcome_window_days,
            covariate_column=covariate_column,
        )
        if differences:
            raise TestPlanChangedError(differences)
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
    )
    if plan is None:
        return report
    early = is_early_look(plan, as_of)
    update: dict[str, object] = {"test_plan_hash": plan.plan_hash, "early_look": early}
    if early:
        when = f"{plan.analysis_date.day} {plan.analysis_date:%b %Y}"
        update["summary"] = (
            f"{EARLY_LOOK_PREFIX}, before the planned analysis date of {when}: not a final result. "
            f"{report.summary}"
        )
    return report.model_copy(update=update)


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
    return campaign_verdict(report, outcome_is_good=outcome_is_good, outcome_label=outcome_label)
