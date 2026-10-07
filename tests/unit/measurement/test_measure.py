"""`measure_campaign`: the one measurement path is `measure_incrementality`, plus the registered plan."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from engine.measurement.campaign import INTENDED_COLUMN, build_assignment
from engine.measurement.measure import EARLY_LOOK_PREFIX, campaign_verdict_for, measure_campaign
from engine.measurement.plan import (
    RealisedPopulation,
    TestPlan,
    TestPlanChangedError,
    TestPlanInput,
    freeze_plan,
    realised_population,
)
from engine.uplift.contracts import IncrementalityReport
from engine.uplift.incrementality import measure_incrementality
from engine.uplift.measure import VerdictKind

SENT = datetime(2026, 5, 1, tzinfo=UTC)
AS_OF = datetime(2026, 9, 1, tzinfo=UTC)
ROWS = 4_000


def scores_and_outcomes(*, uplift: bool = False, seed: int = 3) -> tuple[pd.DataFrame, pd.DataFrame]:
    """A scored list with a random 20 % holdout, some suppressed rows and a real treatment effect."""
    rng = np.random.default_rng(seed)
    keys = [f"C{index:05d}" for index in range(ROWS)]
    control = rng.random(ROWS) < 0.2
    suppressed = rng.random(ROWS) < 0.05
    bands = rng.choice(["High", "Medium", "Low"], size=ROWS)
    scores = pd.DataFrame(
        {
            "customer_id": keys,
            "band": bands,
            "suppressed_reason": np.where(suppressed, "opted_out", None),
            "control_group": control & ~suppressed,
        }
    )
    if uplift:
        scores["intended_treatment"] = bands != "Low"
    treated = ~control & ~suppressed
    converted = rng.random(ROWS) < np.where(treated, 0.14, 0.10)
    outcomes = pd.DataFrame({"customer_id": keys, "converted": converted.astype(int)})
    return scores, outcomes


def common(**extra: object) -> dict[str, object]:
    return {
        "run_id": "r_1",
        "primary_key": "customer_id",
        "outcome_column": "converted",
        "treatment_time": SENT,
        "outcome_window_days": 90,
        "as_of": AS_OF,
        **extra,
    }


def same(left: IncrementalityReport, right: IncrementalityReport) -> None:
    assert left.model_dump(exclude={"computed_at"}) == right.model_dump(exclude={"computed_at"})


@pytest.mark.parametrize("bands", [None, ("High", "Medium")])
def test_without_a_plan_it_is_measure_incrementality_exactly(bands: tuple[str, ...] | None) -> None:
    scores, outcomes = scores_and_outcomes()
    same(
        measure_campaign(scores, outcomes, bands=bands, **common()),  # type: ignore[arg-type]
        measure_incrementality(scores, outcomes, bands=bands, **common()),  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(("uplift", "bands"), [(False, None), (False, ("High",)), (True, None)])
def test_an_assignment_measures_the_same_as_the_scores_it_came_from(
    uplift: bool, bands: tuple[str, ...] | None
) -> None:
    scores, outcomes = scores_and_outcomes(uplift=uplift)
    assignment = build_assignment(scores, primary_key="customer_id", bands=bands)
    from_campaign = measure_campaign(assignment, outcomes, intended_column=INTENDED_COLUMN, **common())  # type: ignore[arg-type]
    from_run = measure_incrementality(
        scores,
        outcomes,
        intended_column="intended_treatment" if uplift else None,
        bands=bands,
        **common(),  # type: ignore[arg-type]
    )
    same(from_campaign, from_run)
    assert from_campaign.treated_rows > 0 and from_campaign.control_rows > 0


def plan_for(assignment: pd.DataFrame, **extra: object) -> TestPlan:
    decided = TestPlanInput.model_validate(
        {
            "metric": "converted within 90 days",
            "outcome_column": "converted",
            "outcome_window_days": 90,
            "analysis_date": date(2026, 8, 15),
            **extra,
        }
    )
    return freeze_plan(
        decided,
        realised_population(assignment, intended_column=INTENDED_COLUMN),
        campaign_id="c_1",
        registered_by="u_1",
        registered_at=SENT,
    )


def test_a_plan_stamps_its_hash_and_an_early_read_is_an_early_look() -> None:
    scores, outcomes = scores_and_outcomes()
    assignment = build_assignment(scores, primary_key="customer_id")
    plan = plan_for(assignment, analysis_date=date(2026, 9, 2))
    early = measure_campaign(assignment, outcomes, intended_column=INTENDED_COLUMN, plan=plan, **common())  # type: ignore[arg-type]
    unplanned = measure_campaign(assignment, outcomes, intended_column=INTENDED_COLUMN, **common())  # type: ignore[arg-type]
    assert early.test_plan_hash == plan.plan_hash and early.early_look is True
    assert early.summary.startswith(
        f"{EARLY_LOOK_PREFIX}, before the planned analysis date of 2 Sep 2026: not a final result. "
    )
    # the counts so far, and when they were read - never the conclusion the final sentence ends with
    assert "caused by the campaign" in unplanned.summary, "the conclusion an early look must leave out"
    for conclusion in ("caused by the campaign", "cannot be shown", "lift of", "95% CI", "p ="):
        assert conclusion not in early.summary, conclusion
    assert f"{unplanned.treated_rate:.1%} of {unplanned.treated_rows:,} contacted" in early.summary
    assert f"{unplanned.control_rate:.1%} of {unplanned.control_rows:,} held-back" in early.summary
    assert "as of 1 Sep 2026" in early.summary and early.summary.endswith("read on 2 Sep 2026.")
    assert early.absolute_lift == unplanned.absolute_lift, "an early look shows the same numbers"
    assert campaign_verdict_for(early, outcome_is_good=True) is None, "but no verdict"
    on_the_day = measure_campaign(
        assignment,
        outcomes,
        intended_column=INTENDED_COLUMN,
        plan=plan,
        **common(as_of=datetime(2026, 9, 2, tzinfo=UTC)),  # type: ignore[arg-type]
    )
    assert on_the_day.early_look is False and on_the_day.summary == unplanned.summary
    verdict = campaign_verdict_for(on_the_day, outcome_is_good=True)
    assert verdict is not None and verdict.kind is VerdictKind.ADDED


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"outcome_window_days": 60}, "outcome_window_days"),
        ({"covariate_column": "tenure"}, "covariate_column"),
        ({"outcome_column": "converted_2"}, "outcome_column"),
        ({"positive_label": "yes"}, "positive_label"),
    ],
)
def test_measuring_differently_from_the_plan_is_refused(change: dict[str, object], field: str) -> None:
    scores, outcomes = scores_and_outcomes()
    outcomes["converted_2"] = outcomes["converted"]
    assignment = build_assignment(scores, primary_key="customer_id")
    plan = plan_for(assignment)
    with pytest.raises(TestPlanChangedError) as raised:
        measure_campaign(
            assignment,
            outcomes,
            intended_column=INTENDED_COLUMN,
            plan=plan,
            **common(**change),  # type: ignore[arg-type]
        )
    assert [difference.field for difference in raised.value.differences] == [field]
    assert isinstance(raised.value, ValueError) and "amend the plan" in str(raised.value)


def test_a_changed_holdout_or_population_is_caught() -> None:
    scores, outcomes = scores_and_outcomes()
    plan = plan_for(build_assignment(scores, primary_key="customer_id"))
    moved = scores.copy()
    moved.loc[moved.index[:400], "control_group"] = False  # a different holdout than was planned
    with pytest.raises(TestPlanChangedError) as raised:
        measure_campaign(
            build_assignment(moved, primary_key="customer_id"),
            outcomes,
            intended_column=INTENDED_COLUMN,
            plan=plan,
            **common(),  # type: ignore[arg-type]
        )
    assert {"holdout_fraction"} <= {d.field for d in raised.value.differences}
    narrower = build_assignment(scores, primary_key="customer_id", bands=["High"])
    with pytest.raises(TestPlanChangedError) as raised:
        measure_campaign(narrower, outcomes, intended_column=INTENDED_COLUMN, plan=plan, **common())  # type: ignore[arg-type]
    assert "population_rows" in {d.field for d in raised.value.differences}


def test_the_realised_population_follows_the_measurement_rule() -> None:
    scores, _ = scores_and_outcomes()
    population = realised_population(scores)
    eligible = scores["suppressed_reason"].isna()
    assert population == RealisedPopulation(
        population_rows=int(eligible.sum()),
        n_treat=int((eligible & ~scores["control_group"]).sum()),
        n_holdout=int((eligible & scores["control_group"]).sum()),
    )
    assert (
        realised_population(
            build_assignment(scores, primary_key="customer_id"), intended_column=INTENDED_COLUMN
        )
        == population
    )


def test_as_of_is_the_callers() -> None:
    scores, outcomes = scores_and_outcomes()
    later = measure_campaign(scores, outcomes, **common(as_of=SENT + timedelta(days=30)))  # type: ignore[arg-type]
    assert later.status.value == "immature" and later.as_of == SENT + timedelta(days=30)
