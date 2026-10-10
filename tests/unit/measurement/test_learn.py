"""`engine.measurement.learn` (Plan J M106, DEC-1316): which rows a model learns from, and why.

The scored rows are Phase 1's own (`apply_actions` on win-back) and their assignment record is M92's
(`assignment_frame` with an explore share), so the frame is built from the artefacts a real scoring
run writes, row for row. What is pinned: only rows whose contact was randomised enter; in each group
the smaller side enters whole and the larger is cut to its size; every scored row is accounted for by
exactly one reason; the frame does not depend on row order; a cycle without an explore share is
refused unless everyone the list could contact was on it; an outcomes file whose coverage differs
between contacted and not-contacted customers (one covering only the hand-off, say) is refused after the
frame is built; a group whose recorded chances of contact disagree with its rule is refused; a
risk-ranked list has no calibration block, with the reason; a planted miscalibration is reported as
one; and the frame is built in linear time.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any, Final

import numpy as np
import pandas as pd
import pytest

from engine.config import load_use_case
from engine.holdout.assign import assignment_frame, selection_masks
from engine.holdout.spec import HoldoutAssignmentReport, HoldoutSpec
from engine.measurement.learn import (
    COVERAGE_TOLERANCE,
    GROUP_ON_LIST,
    GROUP_OUTSIDE_LIST,
    LEARN_CODES,
    LearnFrame,
    build_randomised_frame,
    delivery_from,
    frame_refusal,
    live_calibration,
    overlap_refusal,
)
from engine.measurement.reconcile import ContactReadout
from engine.pilot.plain import jargon_in
from engine.stages.actions import apply_actions

USE_CASE: Final[str] = "win-back-campaign"
KEY: Final[str] = "customer_id"
RUN_ID: Final[str] = "r_20261009_6c000001"
NOW: Final[datetime] = datetime(2026, 10, 9, tzinfo=UTC)
EXPLORE: Final[float] = 0.10


def _world(
    rows: int, *, explore: float = EXPLORE, seed: int = 3
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, Any]:
    """`(inputs, scores, assignment, outcomes, config)` of one win-back scoring run with an explore share."""
    config = load_use_case(USE_CASE)
    rng = np.random.default_rng(seed)
    inputs = pd.DataFrame(
        {
            KEY: [f"C{index:07d}" for index in range(rows)],
            "visits_30d": rng.poisson(4.0, rows),
            "marketing_opt_in": rng.random(rows) > 0.1,
        }
    )
    frame = inputs.copy()
    frame[config.actions.score_field] = rng.random(rows)
    scores = apply_actions(frame, config, run_id=RUN_ID, primary_key=KEY, now=NOW)
    assignment = assignment_frame(
        scores,
        config,
        primary_key=KEY,
        row_key=KEY,
        entity_key=None,
        run_id=RUN_ID,
        active=None,
        explore_fraction=explore,
    ).table
    outcomes = pd.DataFrame({KEY: inputs[KEY], "reactivated_90d": (rng.random(rows) < 0.2).astype(int)})
    return inputs, scores, assignment, outcomes, config


def _build(
    inputs: pd.DataFrame,
    scores: pd.DataFrame,
    assignment: pd.DataFrame,
    outcomes: pd.DataFrame,
    config: Any,
    *,
    holdout_fraction: float = 0.10,
) -> LearnFrame:
    return build_randomised_frame(
        inputs,
        scores,
        assignment,
        outcomes,
        config,
        primary_key=KEY,
        outcome_column="reactivated_90d",
        positive_label=None,
        target_column="reactivated_90d",
        treatment_column="contacted",
        run_id=RUN_ID,
        model_id="m_win-back-campaign_1",
        holdout_fraction=holdout_fraction,
        explore_fraction=EXPLORE,
        samples=50,
        now=NOW,
    )


@pytest.fixture(scope="module")
def world() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, Any]:
    return _world(20_000)


def test_the_code_set() -> None:
    assert {"LEARN_NO_OVERLAP"} == LEARN_CODES


def test_only_randomised_rows_enter_balanced_in_each_group(world: Any) -> None:
    inputs, scores, assignment, outcomes, config = world
    built = _build(inputs, scores, assignment, outcomes, config)
    frame = built.frame.set_index(KEY)
    table = assignment.set_index(KEY)
    probability = table.loc[frame.index, "treatment_probability"]
    assert ((probability > 0) & (probability < 1)).all()
    assert (frame["contacted"].to_numpy() == table.loc[frame.index, "treated"].to_numpy(dtype=int)).all()
    selected, _sleeping = selection_masks(scores, config)
    on_list = pd.Series(selected, index=scores[KEY]).loc[frame.index].to_numpy()
    for members in (on_list, ~on_list):
        assert int(frame["contacted"].to_numpy()[members].sum()) * 2 == int(members.sum())
    groups = {group.group: group for group in built.record.groups}
    assert set(groups) == {GROUP_ON_LIST, GROUP_OUTSIDE_LIST}
    assert groups[GROUP_ON_LIST].chance_of_contact == pytest.approx(0.9)
    assert groups[GROUP_OUTSIDE_LIST].chance_of_contact == pytest.approx(0.9 * EXPLORE)
    for group in groups.values():
        smaller = min(group.contacted, group.not_contacted)
        assert group.entered_contacted == group.entered_not_contacted == smaller
    assert built.record.contacted == built.record.not_contacted == len(frame.index) // 2
    # The frame keeps the input's own columns, plus the two the learner reads.
    assert list(built.frame.columns) == [*inputs.columns, "contacted", "reactivated_90d"]


def test_every_scored_row_is_accounted_for_once(world: Any) -> None:
    inputs, scores, assignment, outcomes, config = world
    # Some customers have no outcome, so that reason is counted too.
    built = _build(inputs, scores, assignment, outcomes.iloc[: len(outcomes.index) - 500], config)
    left = built.record.left_out
    accounted = (
        built.record.rows
        + left.not_eligible
        + left.never_contacted_by_the_rule
        + left.no_chance_of_contact
        + left.no_outcome
        + left.cut_to_balance
    )
    assert accounted == len(scores.index)
    assert left.not_eligible == int(scores["suppressed_reason"].notna().sum())
    assert left.no_outcome > 0
    assert left.never_contacted_by_the_rule == 0  # a propensity list predicts no one is put off
    for sentence in (built.record.summary, *built.record.notes):
        assert jargon_in(sentence) == (), sentence


def test_the_frame_does_not_depend_on_row_order(world: Any) -> None:
    inputs, scores, assignment, outcomes, config = world
    first = _build(inputs, scores, assignment, outcomes, config)
    order = np.random.default_rng(9).permutation(len(inputs.index))
    shuffled = _build(
        inputs.iloc[order],
        scores.iloc[order].reset_index(drop=True),
        assignment.iloc[order[::-1]],
        outcomes.iloc[order[::-1]],
        config,
    )
    left = first.frame.set_index(KEY).sort_index()
    right = shuffled.frame.set_index(KEY).sort_index()
    pd.testing.assert_frame_equal(left, right)
    assert first.record.left_out == shuffled.record.left_out


def test_a_risk_ranked_list_has_no_calibration_block_and_says_why(world: Any) -> None:
    built = _build(*world)
    assert built.record.calibration is None
    assert built.record.calibration_reason is not None and "risk score" in built.record.calibration_reason
    assert jargon_in(built.record.calibration_reason) == ()


def test_a_cycle_without_an_explore_share_has_no_one_outside_the_list_in_the_frame() -> None:
    inputs, scores, assignment, outcomes, config = _world(5_000, explore=0.0)
    built = _build(inputs, scores, assignment, outcomes, config)
    assert {group.group for group in built.record.groups} == {GROUP_ON_LIST}
    assert built.record.left_out.no_chance_of_contact > 0


def _report(explore: float, candidates: int) -> HoldoutAssignmentReport:
    return HoldoutAssignmentReport(
        run_id=RUN_ID,
        use_case_id=USE_CASE,
        spec=HoldoutSpec(scope="use_case", fraction=0.1, explore_fraction=explore),
        rows=1000,
        holdout_members=100,
        control_rows=90,
        explore_candidates=candidates,
        explore_rows=int(candidates * explore),
        created_at=NOW,
    )


def test_learning_is_refused_only_when_customers_outside_the_list_had_no_chance_of_contact() -> None:
    refusal = overlap_refusal(_report(0.0, 640))
    assert refusal is not None and refusal.startswith(
        "640 customers who could have been contacted were outside"
    )
    assert "explore" in refusal and jargon_in(refusal) == ()
    assert overlap_refusal(_report(0.05, 640)) is None
    assert (
        overlap_refusal(_report(0.0, 0)) is None
    ), "everyone the list could contact was on it: already randomised"


def test_a_planted_miscalibration_is_reported_and_a_calibrated_model_is_not() -> None:
    rng = np.random.default_rng(4)
    n = 40_000
    predicted = rng.uniform(0.0, 0.3, n)
    t = (rng.random(n) < 0.5).astype(int)
    base = 0.1

    def outcome(effect: np.ndarray) -> np.ndarray:
        return (rng.random(n) < base + t * effect).astype(int)

    good = live_calibration(
        predicted, t, outcome(predicted), samples=200, seed=1, source_run_id=RUN_ID, model_id="m"
    )
    assert good.matches is True
    wrong = live_calibration(
        predicted, t, outcome(0.3 - predicted), samples=200, seed=1, source_run_id=RUN_ID, model_id="m"
    )
    assert wrong.matches is False
    top = wrong.deciles[0]
    assert top.predicted > 0.25 and top.measured is not None and top.measured.ci_high is not None
    assert top.measured.ci_high < 0.1 and top.inside_range is False
    assert "do not match" in wrong.summary and jargon_in(wrong.summary) == ()
    assert "match what" in good.summary and jargon_in(good.summary) == ()


def test_the_contact_file_is_reported_and_never_replaces_the_treatment() -> None:
    readout = ContactReadout(
        campaign_id="cmp_1",
        contact_rows=900,
        unlisted_customers="unknown",
        treated_customers=800,
        treated_listed=800,
        treated_contacted=600,
        contact_rate=0.75,
        contact_rate_reason=None,
        holdout_customers=100,
        holdout_listed=100,
        holdout_contacted=4,
        contamination=0.04,
        contamination_reason=None,
        rate_difference=None,
        complier=None,
        complier_reason="not needed here",
        computed_at=NOW,
    )
    delivery = delivery_from([readout])
    assert delivery is not None and delivery.contact_rate == 0.75 and delivery.contamination == 0.04
    assert "75%" in delivery.sentence and "4%" in delivery.sentence
    assert jargon_in(delivery.sentence) == ()
    assert delivery_from([]) is None


def test_the_frame_is_built_in_linear_time() -> None:
    """200,000 scored customers; the 1M-row figure is about five times this (one hash per larger-side row)."""
    inputs, scores, assignment, outcomes, config = _world(200_000, seed=8)
    started = time.perf_counter()
    built = _build(inputs, scores, assignment, outcomes, config)
    seconds = time.perf_counter() - started
    assert built.record.rows > 0
    assert seconds < 10.0, f"{seconds:.1f}s for 200k rows"


# ---------------------------------------------------------------------------
# What the outcomes file leaves of the randomisation (review fix, DEC-1316)
# ---------------------------------------------------------------------------
CANDIDATES: Final[int] = 5_000
"""Any positive count: the cycle had customers outside its list it could explore."""


def _outside(built: LearnFrame) -> Any:
    return next(group for group in built.record.groups if group.group == GROUP_OUTSIDE_LIST)


def test_a_full_outcomes_file_is_not_refused(world: Any) -> None:
    built = _build(*world)
    for group in built.record.groups:
        assert group.outcome_coverage_contacted == group.outcome_coverage_not_contacted == 1.0
    assert frame_refusal(built.record, explore_candidates=CANDIDATES, min_rows=200) is None
    # A cycle with nobody outside its list to explore needs no one from there.
    assert frame_refusal(built.record, explore_candidates=0, min_rows=10**9) is None


def test_outcomes_for_the_hand_off_only_are_refused(world: Any) -> None:
    """The reviewer's probe: outcomes for the list, its control group and the explored customers only."""
    inputs, scores, assignment, outcomes, config = world
    selected, _sleeping = selection_masks(scores, config)
    treated = assignment.set_index(KEY).loc[scores[KEY], "treated"].to_numpy(dtype=bool)
    handed = set(scores[KEY][selected | treated])
    built = _build(inputs, scores, assignment, outcomes[outcomes[KEY].isin(handed)], config)
    outside = _outside(built)
    assert outside.contacted > 0 and outside.not_contacted == 0
    assert outside.entered_contacted == outside.entered_not_contacted == 0
    assert outside.outcome_coverage_contacted == 1.0 and outside.outcome_coverage_not_contacted == 0.0
    refusal = frame_refusal(built.record, explore_candidates=CANDIDATES, min_rows=200)
    assert refusal is not None and "outside the list" in refusal and "100%" in refusal and "0%" in refusal
    assert jargon_in(refusal) == (), refusal


def test_outcomes_missing_more_often_for_one_side_are_refused_and_even_gaps_are_not(world: Any) -> None:
    inputs, scores, assignment, outcomes, config = world
    selected, _sleeping = selection_masks(scores, config)
    table = assignment.set_index(KEY).loc[scores[KEY]]
    treated = table["treated"].to_numpy(dtype=bool)
    chance = table["treatment_probability"].to_numpy()
    outside_alone = scores[KEY][~selected & ~treated & (chance > 0) & (chance < 1)]
    rng = np.random.default_rng(12)
    dropped = set(outside_alone[rng.random(len(outside_alone.index)) < 0.30])
    uneven = _build(inputs, scores, assignment, outcomes[~outcomes[KEY].isin(dropped)], config)
    gap = _outside(uneven)
    assert gap.outcome_coverage_contacted == 1.0
    assert gap.outcome_coverage_not_contacted is not None and gap.outcome_coverage_not_contacted < 0.75
    refusal = frame_refusal(uneven.record, explore_candidates=CANDIDATES, min_rows=200)
    assert refusal is not None and "outside the list" in refusal and jargon_in(refusal) == ()

    # The same share missing at random, whoever was contacted: nothing depends on the contact.
    even = outcomes[rng.random(len(outcomes.index)) >= 0.05]
    built = _build(inputs, scores, assignment, even, config)
    for group in built.record.groups:
        assert (
            group.outcome_coverage_contacted is not None and group.outcome_coverage_not_contacted is not None
        )
        assert (
            abs(group.outcome_coverage_contacted - group.outcome_coverage_not_contacted) <= COVERAGE_TOLERANCE
        )
    assert frame_refusal(built.record, explore_candidates=CANDIDATES, min_rows=200) is None


def test_too_few_customers_outside_the_list_are_refused(world: Any) -> None:
    built = _build(*world)
    entered = _outside(built).entered_contacted
    assert frame_refusal(built.record, explore_candidates=CANDIDATES, min_rows=entered) is None
    refusal = frame_refusal(built.record, explore_candidates=CANDIDATES, min_rows=entered + 1)
    assert refusal is not None and refusal.startswith(f"Only {entered:,} contacted")
    assert jargon_in(refusal) == (), refusal


def test_a_group_whose_recorded_chances_disagree_with_its_rule_is_refused(world: Any) -> None:
    inputs, scores, assignment, outcomes, config = world
    selected, _sleeping = selection_masks(scores, config)
    on_list = assignment[KEY].isin(set(scores[KEY][selected]))
    drifted = assignment.copy()
    drifted.loc[drifted.index[on_list.to_numpy()][:10], "treatment_probability"] = 0.5
    with pytest.raises(ValueError, match="on the list"):
        _build(inputs, scores, drifted, outcomes, config)
    # Every chance alike, but not the one the run's settings give.
    with pytest.raises(ValueError, match="cannot be compared as one randomised group"):
        _build(inputs, scores, assignment, outcomes, config, holdout_fraction=0.20)
