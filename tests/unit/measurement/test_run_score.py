"""Plan K M135 (DEC-1324): the scoring run's own prediction as the adjustment, for yes/no outcomes and amounts.

Every campaign here is the simulator's (`predicted_campaign`: a yes/no outcome whose chance depends on two
features, and a prediction of it fixed before the campaign), measured through the product's one path
(`measure_campaign`) against a plan frozen by `freeze_plan`, as the routes do. The API journey over a real
trained and scored run is `tests/integration/measurement/test_campaign_run_score.py`; the coverage of the
adjusted interval is the nightly `tests/statistical/test_run_score_coverage.py`.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from engine.measurement.campaign import INTENDED_COLUMN, build_assignment
from engine.measurement.continuous import CovariateNotBeforeCampaignError, adjusted_difference
from engine.measurement.measure import campaign_verdict_for, headline_estimate, measure_campaign
from engine.measurement.plan import (
    TestPlan,
    TestPlanChangedError,
    TestPlanInput,
    freeze_plan,
    plan_differences,
    plan_inputs,
    realised_population,
)
from engine.measurement.planner import achieved_power, mde_two_proportions
from engine.measurement.run_score import PREDICTION_COLUMN, RunPrediction, adjust_by_prediction
from engine.measurement.simulate import (
    KEY_COLUMN,
    OUTCOME_COLUMN,
    PREDICTED_COLUMN,
    multi_arm_campaign,
    predicted_campaign,
    revenue_campaign,
)
from engine.pilot.plain import jargon_in
from engine.uplift.incrementality import measure_incrementality
from engine.uplift.measure import VerdictKind

REGISTERED = datetime(2026, 3, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _quiet() -> Iterator[None]:
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


def _plan(assignment: pd.DataFrame, **extra: Any) -> TestPlan:
    decided = TestPlanInput.model_validate(
        {
            "metric": "converted within 30 days",
            "outcome_column": OUTCOME_COLUMN,
            "outcome_window_days": 30,
            "analysis_date": date(2026, 6, 1),
            **extra,
        }
    )
    return freeze_plan(
        decided,
        realised_population(assignment, intended_column=INTENDED_COLUMN),
        campaign_id="c_m135",
        registered_by="u_1",
        registered_at=REGISTERED,
    )


def _measured(sim: Any, assignment: pd.DataFrame, plan: TestPlan | None, **extra: Any) -> Any:
    return measure_campaign(
        assignment,
        sim.outcomes,
        intended_column=INTENDED_COLUMN,
        plan=plan,
        **{**sim.measure_kwargs, **extra},
    )


def _hand(sim: Any, prediction: pd.DataFrame) -> tuple[float, float, float, float]:
    """`adjusted_difference` on arrays joined by a pandas merge on the key - an independent computation."""
    joined = sim.scores[[KEY_COLUMN, "control_group"]].merge(sim.outcomes, on=KEY_COLUMN, how="inner")
    joined = joined.merge(prediction, on=KEY_COLUMN, how="left")
    x = joined[PREDICTION_COLUMN].to_numpy(dtype=float)
    x = np.where(np.isnan(x), np.nanmean(x), x)
    y = joined[OUTCOME_COLUMN].to_numpy(dtype=float)
    held = joined["control_group"].to_numpy(dtype=bool)
    result = adjusted_difference(y[~held], x[~held], y[held], x[held])
    assert result is not None
    return result.difference, result.low, result.high, result.variance_reduction


# ---------------------------------------------------------------------------
# The plan (acceptance 5 and the plan_hash rule)
# ---------------------------------------------------------------------------
def test_run_score_with_a_covariate_column_is_refused_with_a_clear_message() -> None:
    with pytest.raises(ValidationError, match="covariate_column must be left empty"):
        TestPlanInput(
            metric="churned",
            outcome_column="churned",
            analysis_date=date(2026, 6, 1),
            covariate_source="run_score",
            covariate_column="tenure",
        )
    with pytest.raises(ValidationError, match="name the covariate_column"):
        TestPlanInput(
            metric="churned",
            outcome_column="churned",
            analysis_date=date(2026, 6, 1),
            covariate_source="column",
        )


def test_a_yes_no_plan_may_expect_a_share_only_with_the_run_score() -> None:
    allowed = TestPlanInput(
        metric="churned",
        outcome_column="churned",
        analysis_date=date(2026, 6, 1),
        covariate_source="run_score",
        expected_rho2=0.1,
    )
    assert allowed.expected_rho2 == 0.1
    with pytest.raises(ValidationError, match="expected_rho2"):
        TestPlanInput(
            metric="churned", outcome_column="churned", analysis_date=date(2026, 6, 1), expected_rho2=0.1
        )
    amount = TestPlanInput(
        metric="revenue",
        outcome_column="revenue",
        outcome_kind="continuous",
        analysis_date=date(2026, 6, 1),
        covariate_source="run_score",
        expected_rho2=0.2,
        mde_value=2.0,
        outcome_sd=40.0,
    )
    assert amount.covariate_column is None


def test_an_unset_source_leaves_the_stored_plan_and_its_hash_as_they_were() -> None:
    sim = predicted_campaign(400, 0.2, 0.0, seed=1)
    assignment = build_assignment(sim.scores, primary_key=KEY_COLUMN)
    plain = _plan(assignment, mde_pp=2.0, base_rate=0.2)
    assert "covariate_source" not in plain.model_dump(mode="json")
    # Recorded on the commit before M135 from the same inputs: an old plan keeps its hash.
    assert plain.plan_hash == "e3d3b64c34b0577ad74a569673d22b3f6b2d085171a5f473f6962c1975811422"
    with_source = _plan(assignment, mde_pp=2.0, base_rate=0.2, covariate_source="run_score")
    assert with_source.plan_hash != plain.plan_hash
    assert plan_inputs(with_source).covariate_source == "run_score"


def test_a_change_of_source_is_a_plan_difference() -> None:
    sim = predicted_campaign(400, 0.2, 0.0, seed=2)
    assignment = build_assignment(sim.scores, primary_key=KEY_COLUMN)
    plan = _plan(assignment)
    realised = realised_population(assignment, intended_column=INTENDED_COLUMN)
    common: dict[str, Any] = {
        "realised": realised,
        "outcome_column": OUTCOME_COLUMN,
        "positive_label": None,
        "outcome_window_days": 30,
        "covariate_column": None,
    }
    assert plan_differences(plan, **common) == ()
    changed = plan_differences(plan, covariate_source="run_score", **common)
    assert [(d.field, d.planned, d.realised) for d in changed] == [("covariate_source", "none", "run_score")]
    registered = _plan(assignment, covariate_source="run_score")
    dropped = plan_differences(registered, **common)
    assert [(d.field, d.planned, d.realised) for d in dropped] == [("covariate_source", "run_score", "none")]


# ---------------------------------------------------------------------------
# Measuring (acceptance 1 in unit form, 2, 3, 4)
# ---------------------------------------------------------------------------
def test_a_yes_no_campaign_is_adjusted_by_the_runs_prediction_as_computed_by_hand() -> None:
    sim = predicted_campaign(6_000, 0.15, 0.03, seed=3)
    assignment = build_assignment(sim.scores, primary_key=KEY_COLUMN)
    plan = _plan(assignment, covariate_source="run_score")
    prediction = sim.run_prediction
    dropped = prediction.frame.iloc[::50]  # 120 scored customers have no prediction
    partial = RunPrediction(
        frame=prediction.frame.drop(index=dropped.index),
        run_id=prediction.run_id,
        column=prediction.column,
        predicted_at=prediction.predicted_at,
    )
    report = _measured(sim, assignment, plan, covariate_source="run_score", run_prediction=partial)
    assert report.covariate_source == "run_score" and report.covariate_column is None
    assert report.rows_covariate_missing == len(dropped)
    assert report.absolute_lift is not None and report.incremental_conversions is not None  # unadjusted kept
    assert report.adjusted_interval is not None and report.variance_reduction is not None
    value, low, high, reduction = _hand(sim, partial.frame)
    assert report.adjusted_lift == pytest.approx(value, abs=1e-12)
    assert report.adjusted_interval.ci_low == pytest.approx(low, abs=1e-12)
    assert report.adjusted_interval.ci_high == pytest.approx(high, abs=1e-12)
    assert report.variance_reduction == pytest.approx(reduction, abs=1e-12)
    assert report.variance_reduction > 0.05
    assert report.test_plan_hash == plan.plan_hash
    assert "Taking account of the chance of the outcome the scoring run predicted" in report.summary
    assert jargon_in(report.summary) == ()


def test_the_join_is_on_the_key_never_on_row_order() -> None:
    sim = predicted_campaign(3_000, 0.2, 0.02, seed=4)
    assignment = build_assignment(sim.scores, primary_key=KEY_COLUMN)
    plan = _plan(assignment, covariate_source="run_score")
    prediction = sim.run_prediction
    base = _measured(sim, assignment, plan, covariate_source="run_score", run_prediction=prediction)
    rng = np.random.default_rng(0)
    shuffled_outcomes = sim.outcomes.iloc[rng.permutation(len(sim.outcomes))].reset_index(drop=True)
    shuffled_prediction = RunPrediction(
        frame=prediction.frame.iloc[rng.permutation(len(prediction.frame))].reset_index(drop=True),
        run_id=prediction.run_id,
        column=prediction.column,
        predicted_at=prediction.predicted_at,
    )
    again = measure_campaign(
        assignment,
        shuffled_outcomes,
        intended_column=INTENDED_COLUMN,
        plan=plan,
        covariate_source="run_score",
        run_prediction=shuffled_prediction,
        **sim.measure_kwargs,
    )
    assert again.model_dump(exclude={"computed_at"}) == base.model_dump(exclude={"computed_at"})


def test_a_composite_key_is_joined_on_every_column() -> None:
    sim = predicted_campaign(2_000, 0.2, 0.02, seed=5)
    snapshot = np.where(np.arange(len(sim.scores)) % 2 == 0, "2026-01-31", "2026-02-28")
    scores = sim.scores.assign(snapshot_date=snapshot)
    outcomes = sim.outcomes.assign(snapshot_date=snapshot)
    # One customer id, two snapshots: the prediction must follow the snapshot as well as the id.
    ids = np.char.add("C", (np.arange(len(scores)) // 2).astype(str))
    scores[KEY_COLUMN] = ids
    outcomes[KEY_COLUMN] = ids
    key = [KEY_COLUMN, "snapshot_date"]
    frame = scores[[*key, PREDICTED_COLUMN]].rename(columns={PREDICTED_COLUMN: PREDICTION_COLUMN})
    prediction = RunPrediction(
        frame=frame, run_id="r", column=PREDICTED_COLUMN, predicted_at=sim.predicted_at
    )
    kwargs = {**sim.measure_kwargs, "primary_key": key}
    report = measure_incrementality(scores, outcomes, run_prediction=prediction, **kwargs)
    swapped = frame.assign(
        snapshot_date=frame["snapshot_date"].map({"2026-01-31": "2026-02-28", "2026-02-28": "2026-01-31"})
    )
    other = measure_incrementality(
        scores,
        outcomes,
        run_prediction=RunPrediction(frame=swapped, run_id="r", column="c", predicted_at=sim.predicted_at),
        **kwargs,
    )
    assert report.rows_covariate_missing == 0 and report.adjusted_lift is not None
    assert other.adjusted_lift != report.adjusted_lift, "the snapshot is part of the join"


def test_a_prediction_made_after_the_campaign_began_is_refused() -> None:
    sim = predicted_campaign(1_000, 0.2, 0.02, seed=6)
    assignment = build_assignment(sim.scores, primary_key=KEY_COLUMN)
    plan = _plan(assignment, covariate_source="run_score")
    start = sim.measure_kwargs["treatment_time"]
    late = RunPrediction(
        frame=sim.run_prediction.frame, run_id="r", column="c", predicted_at=start + timedelta(hours=2)
    )
    with pytest.raises(CovariateNotBeforeCampaignError, match="made after the campaign began"):
        _measured(sim, assignment, plan, covariate_source="run_score", run_prediction=late)
    # Before the campaign's start but after some customers' own treatment day: refused too.
    between = RunPrediction(
        frame=sim.run_prediction.frame, run_id="r", column="c", predicted_at=start - timedelta(days=45)
    )
    with pytest.raises(
        CovariateNotBeforeCampaignError, match="contacted before the day the scoring run finished"
    ):
        _measured(sim, assignment, plan, covariate_source="run_score", run_prediction=between)


def test_without_run_score_the_report_is_what_it_was() -> None:
    sim = predicted_campaign(2_000, 0.2, 0.02, seed=7)
    assignment = build_assignment(sim.scores, primary_key=KEY_COLUMN)
    no_plan = _measured(sim, assignment, None)
    plan = _plan(assignment)
    planned = _measured(sim, assignment, plan, run_prediction=sim.run_prediction)  # read, but not registered
    unadjusted = measure_incrementality(sim.scores, sim.outcomes, **sim.measure_kwargs)
    keys = set(no_plan.model_dump(mode="json"))
    assert keys == set(unadjusted.model_dump(mode="json"))
    assert not keys & {"covariate_source", "adjusted_interval", "rows_covariate_missing", "adjusted_lift"}
    assert no_plan.model_dump(exclude={"computed_at"}) == unadjusted.model_dump(exclude={"computed_at"})
    assert planned.model_dump(exclude={"computed_at", "test_plan_hash", "early_look"}) == no_plan.model_dump(
        exclude={"computed_at", "test_plan_hash", "early_look"}
    )


def test_run_score_needs_a_plan_that_registered_it() -> None:
    sim = predicted_campaign(1_000, 0.2, 0.02, seed=8)
    assignment = build_assignment(sim.scores, primary_key=KEY_COLUMN)
    with pytest.raises(TestPlanChangedError, match="No test plan is registered"):
        _measured(sim, assignment, None, covariate_source="run_score", run_prediction=sim.run_prediction)
    unregistered = _plan(assignment)
    with pytest.raises(TestPlanChangedError) as raised:
        _measured(
            sim, assignment, unregistered, covariate_source="run_score", run_prediction=sim.run_prediction
        )
    assert [d.field for d in raised.value.differences] == ["covariate_source"]


def test_run_score_is_refused_with_a_column_and_for_several_offers() -> None:
    sim = predicted_campaign(1_000, 0.2, 0.02, seed=9)
    assignment = build_assignment(sim.scores, primary_key=KEY_COLUMN)
    plan = _plan(assignment, covariate_source="run_score")
    with pytest.raises(ValueError, match="one thing at a time"):
        _measured(
            sim,
            assignment,
            plan,
            covariate_source="run_score",
            covariate_column="x",
            run_prediction=sim.run_prediction,
        )
    with pytest.raises(ValueError, match="was not read"):
        _measured(sim, assignment, plan, covariate_source="run_score")
    offers = multi_arm_campaign(600, 0.05, (0.02, 0.04), seed=22)
    with pytest.raises(ValueError, match="Several offers"):
        measure_campaign(
            offers.scores,
            offers.outcomes,
            plan=None,
            covariate_source="run_score",
            run_prediction=sim.run_prediction,
            **offers.measure_kwargs,
        )


def test_an_amount_is_adjusted_by_the_prediction_too() -> None:
    sim = revenue_campaign(4_000, 3.0, seed=10, rho=0.6)
    # The pre-campaign amount, written as a scoring run's prediction would be.
    frame = sim.outcomes[[KEY_COLUMN, "pre_revenue"]].rename(columns={"pre_revenue": PREDICTION_COLUMN})
    early = sim.measure_kwargs["treatment_time"] - timedelta(days=200)
    prediction = RunPrediction(frame=frame, run_id="r", column="pre_revenue", predicted_at=early)
    assignment = build_assignment(sim.scores, primary_key=KEY_COLUMN)
    plan = freeze_plan(
        TestPlanInput(
            metric="revenue",
            outcome_column="revenue",
            outcome_kind="continuous",
            outcome_window_days=30,
            analysis_date=date(2026, 6, 1),
            covariate_source="run_score",
        ),
        realised_population(assignment, intended_column=INTENDED_COLUMN),
        campaign_id="c_m135",
        registered_by="u_1",
        registered_at=REGISTERED,
    )
    report = measure_campaign(
        assignment,
        sim.outcomes,
        intended_column=INTENDED_COLUMN,
        plan=plan,
        covariate_source="run_score",
        run_prediction=prediction,
        **sim.measure_kwargs,
    )
    column = measure_incrementality(sim.scores, sim.outcomes, **sim.adjusted_kwargs)
    assert report.covariate_source == "run_score" and report.covariate_column is None
    assert report.adjusted_lift == pytest.approx(column.adjusted_lift, abs=1e-9)
    assert report.variance_reduction == pytest.approx(column.variance_reduction, abs=1e-9)
    assert "what the scoring run predicted" in report.summary and jargon_in(report.summary) == ()
    verdict = campaign_verdict_for(report, outcome_is_good=True)
    assert verdict is not None and "predicted for each customer before the campaign" in verdict.detail
    assert "None" not in verdict.detail


def test_the_verdict_and_headline_of_an_adjusted_yes_no_report_are_the_adjusted_rates() -> None:
    sim = predicted_campaign(8_000, 0.15, 0.04, seed=11)
    assignment = build_assignment(sim.scores, primary_key=KEY_COLUMN)
    plan = _plan(assignment, covariate_source="run_score")
    report = _measured(sim, assignment, plan, covariate_source="run_score", run_prediction=sim.run_prediction)
    estimate, adjusted = headline_estimate(report)
    assert adjusted and estimate == report.adjusted_interval
    verdict = campaign_verdict_for(report, outcome_is_good=True)
    assert verdict is not None and verdict.kind is VerdictKind.ADDED
    assert report.adjusted_lift is not None
    assert verdict.amount == round(abs(report.adjusted_lift * report.treated_rows))
    assert verdict.detail.endswith(
        "Adjusted for the chance of the outcome the scoring run predicted for each customer before the "
        "campaign, as the test plan registered."
    )
    assert jargon_in(verdict.detail) == ()


def test_a_useless_or_missing_prediction_is_reported_honestly() -> None:
    sim = predicted_campaign(3_000, 0.2, 0.02, seed=12, prediction="noise")
    assignment = build_assignment(sim.scores, primary_key=KEY_COLUMN)
    plan = _plan(assignment, covariate_source="run_score")
    report = _measured(sim, assignment, plan, covariate_source="run_score", run_prediction=sim.run_prediction)
    assert report.variance_reduction is not None and abs(report.variance_reduction) < 0.01  # negative allowed
    flat = RunPrediction(
        frame=sim.run_prediction.frame.assign(**{PREDICTION_COLUMN: 0.3}),
        run_id="r",
        column="c",
        predicted_at=sim.predicted_at,
    )
    same = _measured(sim, assignment, plan, covariate_source="run_score", run_prediction=flat)
    assert same.adjusted_interval is None and same.adjustment_note is not None
    assert (
        "the same for every measured customer" in same.adjustment_note
        and same.adjustment_note in same.summary
    )
    none = RunPrediction(
        frame=sim.run_prediction.frame.iloc[:0], run_id="r", column="c", predicted_at=sim.predicted_at
    )
    nothing = _measured(sim, assignment, plan, covariate_source="run_score", run_prediction=none)
    assert nothing.adjusted_interval is None and nothing.rows_covariate_missing == 3_000
    assert (
        nothing.adjustment_note is not None
        and "No measured customer has a prediction" in nothing.adjustment_note
    )


def test_adjust_by_prediction_counts_only_the_usable_customers() -> None:
    outcome = np.array([1, 0, 1, 0, 1, 0, np.nan, 1], dtype=float)
    prediction = np.array([0.9, 0.1, np.nan, 0.2, 0.8, np.nan, 0.5, 0.7])
    usable = ~np.isnan(outcome)
    treated = np.array([True, True, True, True, False, False, False, False])
    result = adjust_by_prediction(outcome, prediction, usable=usable, treated=treated)
    assert result.rows_missing == 2 and result.adjusted is not None


# ---------------------------------------------------------------------------
# The planner (item 4): the person's expected share shrinks the change the test must see
# ---------------------------------------------------------------------------
def test_the_planner_takes_the_expected_share_on_a_yes_no_outcome() -> None:
    plain = mde_two_proportions(9_000, 1_000, 0.1)
    assert mde_two_proportions(9_000, 1_000, 0.1, rho2=0.0) == plain
    shrunk = mde_two_proportions(9_000, 1_000, 0.1, rho2=0.19)
    assert plain.absolute is not None and shrunk.absolute is not None
    assert shrunk.absolute < plain.absolute
    assert achieved_power(9_000, 1_000, 0.1, 0.02, rho2=0.19).power > achieved_power(9_000, 1_000, 0.1, 0.02).power  # type: ignore[operator]
    with pytest.raises(ValueError, match="rho2"):
        achieved_power(9_000, 1_000, 0.1, 0.02, rho2=1.0)
    sim = predicted_campaign(400, 0.2, 0.0, seed=13)
    assignment = build_assignment(sim.scores, primary_key=KEY_COLUMN)
    without = _plan(assignment, mde_pp=5.0, base_rate=0.2, covariate_source="run_score")
    expected = _plan(assignment, mde_pp=5.0, base_rate=0.2, covariate_source="run_score", expected_rho2=0.3)
    assert expected.achieved_power is not None and without.achieved_power is not None
    assert expected.achieved_power > without.achieved_power
