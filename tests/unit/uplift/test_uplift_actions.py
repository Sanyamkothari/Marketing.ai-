"""`engine.uplift.actions.apply_uplift_actions`: segments instead of bands, Phase 1's rules reused.

Suppression and the control group must be *the same* as Phase 1's for the same run id - they are
checked against `engine.stages.actions.apply_actions` itself, not against a copy of its rules.
Sleeping dogs must never be treated, under any configuration. `intended_treatment` must mark the
selected rows and exactly the control rows the policy would have selected.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from engine.config import UseCaseConfig, load_use_case_document
from engine.stages.actions import (
    APPLIED_RULES_ATTR,
    CONTROL_ACTION,
    SKIPPED_RULES_ATTR,
    SUPPRESSED_ACTION,
    apply_actions,
)
from engine.uplift.actions import (
    BELOW_COST_ACTION,
    INTENDED_TREATMENT_COLUMN,
    OVER_BUDGET_ACTION,
    SEGMENT_COLUMN,
    TREAT_ACTION,
    apply_uplift_actions,
)
from engine.uplift.contracts import (
    SEGMENT_ACTIONS,
    SEGMENT_LABELS,
    ConfidenceValue,
    PolicyRecommendation,
    PolicyStopReason,
    Segment,
    SegmentThresholds,
)
from tests.fixtures.make_uplift_data import make_uplift_data

RUN_ID = "r_20260921_aaaaaaaa"
NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)

THRESHOLDS = SegmentThresholds(
    persuadable_min_uplift=0.02,
    sleeping_dog_max_uplift=-0.01,
    sure_thing_min_probability=0.40,
    sure_thing_from_base_rate=False,
)


def use_case(
    *,
    control_fraction: float = 0.10,
    opt_out: bool = True,
    policy: dict[str, object] | None = None,
) -> UseCaseConfig:
    """`targeted-advertisement` with an uplift policy, opt-out on, recency off, template emptied."""
    document = copy.deepcopy(load_use_case_document("targeted-advertisement"))
    document["template"] = {"columns": []}
    document["actions"]["control_group_fraction"] = control_fraction
    document["actions"]["suppression"]["suppress_opted_out"] = opt_out
    document["actions"]["suppression"]["suppress_recently_contacted"] = False
    document["uplift"] = {"policy": policy or {}}
    return UseCaseConfig.model_validate(document)


def scored(rows: int = 400, *, seed: int = 5, opt_out_every: int = 10) -> pd.DataFrame:
    """A scored uplift frame with every segment represented and some opted-out customers."""
    rng = np.random.default_rng(seed)
    uplift = np.round(rng.uniform(-0.3, 0.4, size=rows), 4)
    p_control = np.round(rng.uniform(0.01, 0.8, size=rows), 4)
    return pd.DataFrame(
        {
            "customer_id": [f"C-{index:05d}" for index in range(rows)],
            "uplift": uplift,
            "p_treated": np.clip(p_control + uplift, 0.0, 1.0),
            "p_control": p_control,
            "marketing_opt_in": [index % opt_out_every != 0 for index in range(rows)],
        }
    )


def run(
    frame: pd.DataFrame, config: UseCaseConfig, **kwargs: object
) -> tuple[pd.DataFrame, PolicyRecommendation]:
    return apply_uplift_actions(
        frame,
        config,
        run_id=RUN_ID,
        primary_key="customer_id",
        causal=True,
        thresholds=THRESHOLDS,
        now=NOW,
        **kwargs,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Phase 1 rules reused
# ---------------------------------------------------------------------------
def test_suppression_and_control_are_exactly_phase_1s_for_the_same_run_id() -> None:
    config = use_case()
    frame = scored()
    result, _ = run(frame, config)
    phase1 = apply_actions(
        frame.assign(propensity=0.5), config, run_id=RUN_ID, primary_key="customer_id", now=NOW
    )
    assert result["control_group"].tolist() == phase1["control_group"].tolist()
    assert result["suppressed_reason"].tolist() == phase1["suppressed_reason"].tolist()
    assert int(result["control_group"].sum()) == 36  # 10% of the 360 non-opted-out rows
    assert result.attrs[APPLIED_RULES_ATTR] == phase1.attrs[APPLIED_RULES_ATTR]
    assert result.attrs[SKIPPED_RULES_ATTR] == phase1.attrs[SKIPPED_RULES_ATTR]


def test_suppressed_and_control_rows_keep_phase_1s_actions() -> None:
    result, _ = run(scored(), use_case())
    suppressed = result["suppressed_reason"].notna()
    assert (result.loc[suppressed, "action"] == SUPPRESSED_ACTION).all()
    assert (result.loc[result["control_group"], "action"] == CONTROL_ACTION).all()
    assert not (suppressed & result["control_group"]).any()


def test_the_input_frame_and_its_own_score_column_are_left_alone() -> None:
    frame = scored().assign(propensity=0.123)
    before = frame.copy()
    result, _ = run(frame, use_case())
    pd.testing.assert_frame_equal(frame, before)
    assert (result["propensity"] == 0.123).all()
    for column in ("customer_id", "uplift", "p_treated", "p_control", "marketing_opt_in"):
        assert result[column].tolist() == frame[column].tolist()


# ---------------------------------------------------------------------------
# Segments, bands and actions
# ---------------------------------------------------------------------------
def test_band_is_the_segment_label_and_segment_is_the_segment_value() -> None:
    result, _ = run(scored(), use_case())
    expected = np.where(
        result["uplift"] >= 0.02,
        "persuadable",
        np.where(
            result["uplift"] <= -0.01,
            "sleeping_dog",
            np.where(result["p_control"] >= 0.4, "sure_thing", "lost_cause"),
        ),
    )
    assert result[SEGMENT_COLUMN].tolist() == expected.tolist()
    labels = {segment.value: label for segment, label in SEGMENT_LABELS.items()}
    assert result["band"].tolist() == [labels[value] for value in expected]
    assert {type(value) for value in result[SEGMENT_COLUMN]} == {str}


def test_every_eligible_row_gets_its_segments_action_and_only_selected_persuadables_are_treated() -> None:
    result, recommendation = run(scored(), use_case(policy={"budget_contacts": 20}))
    eligible = result["suppressed_reason"].isna() & ~result["control_group"]
    treated = result["action"] == TREAT_ACTION
    assert int(treated.sum()) == 20 == recommendation.contacts_recommended
    assert recommendation.stop_reason is PolicyStopReason.BUDGET
    assert (result.loc[treated, SEGMENT_COLUMN] == "persuadable").all()
    assert (eligible[treated]).all()

    persuadable = result[SEGMENT_COLUMN] == "persuadable"
    over = eligible & persuadable & ~treated
    assert (result.loc[over, "action"] == OVER_BUDGET_ACTION).all()
    assert result.loc[treated, "uplift"].min() >= result.loc[over, "uplift"].max()
    for segment in (Segment.SURE_THING, Segment.LOST_CAUSE, Segment.SLEEPING_DOG):
        rows = eligible & (result[SEGMENT_COLUMN] == segment.value)
        assert rows.any()
        assert (result.loc[rows, "action"] == SEGMENT_ACTIONS[segment]).all()


def test_persuadables_that_do_not_pay_are_labelled_below_cost() -> None:
    # 100 × uplift ≥ 10 needs uplift ≥ 0.10; with a budget of 20 some rows are over budget as well.
    config = use_case(policy={"budget_contacts": 20, "cost_per_contact": 10.0, "value_per_conversion": 100.0})
    result, recommendation = run(scored(), config)
    eligible = result["suppressed_reason"].isna() & ~result["control_group"]
    passed_over = eligible & (result[SEGMENT_COLUMN] == "persuadable") & (result["action"] != TREAT_ACTION)
    below = passed_over & (result["uplift"] * 100.0 < 10.0)
    assert below.any() and (passed_over & ~below).any()
    assert (result.loc[below, "action"] == BELOW_COST_ACTION).all()
    assert (result.loc[passed_over & ~below, "action"] == OVER_BUDGET_ACTION).all()
    assert recommendation.stop_reason is PolicyStopReason.BUDGET


@pytest.mark.parametrize(
    "policy",
    [
        {},
        {"budget_contacts": 10**9},
        {"budget_contacts": 10**9, "cost_per_contact": 0.0, "value_per_conversion": 10**6},
    ],
)
def test_sleeping_dogs_are_never_treated_whatever_the_configuration(policy: dict[str, object]) -> None:
    result, _ = run(scored(), use_case(control_fraction=0.0, opt_out=False, policy=policy))
    sleeping = result[SEGMENT_COLUMN] == "sleeping_dog"
    assert sleeping.sum() > 50
    assert not (result.loc[sleeping, "action"] == TREAT_ACTION).any()
    assert not result.loc[sleeping, INTENDED_TREATMENT_COLUMN].any()
    assert (result.loc[sleeping, "action"] == SEGMENT_ACTIONS[Segment.SLEEPING_DOG]).all()


def test_with_no_limits_every_eligible_persuadable_is_treated() -> None:
    result, recommendation = run(scored(), use_case())
    eligible = result["suppressed_reason"].isna() & ~result["control_group"]
    persuadable = result[SEGMENT_COLUMN] == "persuadable"
    assert ((result["action"] == TREAT_ACTION) == (eligible & persuadable)).all()
    assert recommendation.stop_reason is PolicyStopReason.ALL_PERSUADABLES
    assert recommendation.eligible_persuadables == int((eligible & persuadable).sum())


# ---------------------------------------------------------------------------
# intended_treatment
# ---------------------------------------------------------------------------
def test_intended_treatment_is_the_selected_rows_plus_the_control_rows_that_would_have_been() -> None:
    result, _ = run(scored(1_000), use_case(control_fraction=0.3, policy={"budget_contacts": 60}))
    treated = result["action"] == TREAT_ACTION
    lowest = result.loc[treated, "uplift"].min()
    would_have_been = (
        result["control_group"] & (result[SEGMENT_COLUMN] == "persuadable") & (result["uplift"] >= lowest)
    )
    assert would_have_been.any()
    assert (result[INTENDED_TREATMENT_COLUMN] == (treated | would_have_been)).all()
    assert result[INTENDED_TREATMENT_COLUMN].dtype == bool
    # No suppressed row is ever intended, and no control row below the cut.
    assert not result.loc[result["suppressed_reason"].notna(), INTENDED_TREATMENT_COLUMN].any()
    below_cut = result["control_group"] & (result["uplift"] < lowest)
    assert not result.loc[below_cut, INTENDED_TREATMENT_COLUMN].any()


def test_with_nobody_selected_nobody_is_intended() -> None:
    frame = scored()
    frame["uplift"] = -frame["uplift"].abs() - 0.02  # everyone a sleeping dog
    result, recommendation = run(frame, use_case())
    assert recommendation.contacts_recommended == 0
    assert recommendation.stop_reason is PolicyStopReason.NO_PERSUADABLES
    assert not result[INTENDED_TREATMENT_COLUMN].any()
    assert not (result["action"] == TREAT_ACTION).any()


# ---------------------------------------------------------------------------
# The recommendation
# ---------------------------------------------------------------------------
def test_the_recommendation_is_computed_on_the_scored_rows_and_carries_the_causal_flag() -> None:
    frame = scored()
    result, recommendation = apply_uplift_actions(
        frame,
        use_case(policy={"budget_contacts": 10, "cost_per_contact": 2.0, "value_per_conversion": 40.0}),
        run_id=RUN_ID,
        primary_key="customer_id",
        causal=False,
        thresholds=THRESHOLDS,
        now=NOW,
        observed_top_share=lambda fraction: ConfidenceValue(value=0.2, ci_low=0.1, ci_high=0.3),
    )
    assert recommendation.computed_on == "scored"
    assert recommendation.causal is False
    assert recommendation.run_id == RUN_ID
    assert recommendation.rows == len(frame)
    assert recommendation.contacts_recommended == 10
    expected = recommendation.expected_incremental_conversions
    assert expected is not None and expected.value == pytest.approx(2.0)
    assert recommendation.expected_cost == pytest.approx(20.0)
    assert recommendation.expected_value == pytest.approx(80.0)
    assert recommendation.expected_net_value == pytest.approx(60.0)
    treated = result["action"] == TREAT_ACTION
    assert recommendation.predicted_incremental_conversions == pytest.approx(
        result.loc[treated, "uplift"].sum()
    )


def test_custom_prediction_column_names_are_honoured() -> None:
    frame = scored().rename(columns={"uplift": "u", "p_treated": "pt", "p_control": "pc"})
    result, _ = run(frame, use_case(), prediction_columns=("u", "pt", "pc"))
    assert "uplift" not in result.columns
    assert (result.loc[result["action"] == TREAT_ACTION, "u"] >= 0.02).all()


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------
def test_a_missing_prediction_column_is_refused() -> None:
    with pytest.raises(ValueError, match="'p_control'"):
        run(scored().drop(columns="p_control"), use_case())


def test_a_missing_prediction_value_is_refused_with_counts_only() -> None:
    frame = scored()
    frame["uplift"] = frame["uplift"].astype(object)
    frame.loc[3, "uplift"] = np.nan
    frame.loc[7, "uplift"] = "n/a"
    with pytest.raises(ValueError, match="2 of 400 rows") as error:
        run(frame, use_case())
    assert "n/a" not in str(error.value)


# ---------------------------------------------------------------------------
# On planted data
# ---------------------------------------------------------------------------
def test_on_planted_data_the_true_sleeping_dogs_are_never_treated() -> None:
    dataset = make_uplift_data(5_000, seed=11)
    truth = dataset.truth
    frame = pd.DataFrame(
        {
            "customer_id": truth["customer_id"],
            "uplift": truth["true_uplift"],
            "p_treated": truth["p_treated"],
            "p_control": truth["p_control"],
            "marketing_opt_in": True,
        }
    )
    result, recommendation = run(frame, use_case(policy={"budget_contacts": 10**6}))
    true_sleeping = truth["true_segment"].to_numpy() == "sleeping_dog"
    assert true_sleeping.sum() > 100
    assert not (result["action"].to_numpy()[true_sleeping] == TREAT_ACTION).any()
    treated = result["action"] == TREAT_ACTION
    assert set(truth.loc[treated.to_numpy(), "true_segment"]) <= {"persuadable", "sure_thing"}
    assert recommendation.contacts_recommended == int(treated.sum())
