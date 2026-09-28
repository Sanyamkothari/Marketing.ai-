"""`engine.uplift.measure` (Plan H M83): who gets step 4, the plain verdict, the learning floor and
the experiment file an uplift model learns from."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pandas as pd
import pytest

from engine.config import load_all_use_cases
from engine.uplift.config import UpliftConfig
from engine.uplift.contracts import ConfidenceValue, IncrementalityReport, IncrementalityStatus
from engine.uplift.measure import (
    VerdictKind,
    build_experiment_frame,
    campaign_verdict,
    detect_outcome_column,
    learn_readiness,
    measure_offered,
    treatment_column_for,
)

OPERATIONAL = {"order-fulfillment", "fault-prediction"}


def test_every_use_case_that_contacts_customers_gets_step_4_and_the_operational_ones_do_not() -> None:
    offered = {uc_id for uc_id, config in load_all_use_cases().items() if measure_offered(config)}
    everything = set(load_all_use_cases())
    assert offered == everything - OPERATIONAL - {"ai-onboarding-assistant"}
    assert {"targeted-advertisement", "telco-churn", "win-back-campaign", "payment-propensity"} <= offered


def test_step_4_is_decided_by_configuration_not_by_id() -> None:
    config = load_all_use_cases()["telco-churn"]
    no_holdout = config.model_copy(
        update={"actions": config.actions.model_copy(update={"control_group_fraction": 0.0})}
    )
    assert measure_offered(config) and not measure_offered(no_holdout)
    ops = load_all_use_cases()["order-fulfillment"]
    contacting = ops.model_copy(
        update={"actions": ops.actions.model_copy(update={"contacts_customers": True})}
    )
    assert not measure_offered(ops) and measure_offered(contacting)


def _report(**fields: object) -> IncrementalityReport:
    base: dict[str, object] = {
        "run_id": "r1",
        "outcome_column": "converted",
        "outcome_window_days": 30,
        "as_of": datetime(2026, 9, 1, tzinfo=UTC),
        "status": IncrementalityStatus.MATURE,
        "results_available_on": None,
        "treated_rows": 9000,
        "treated_conversions": 1080,
        "treated_rate": 0.12,
        "control_rows": 1000,
        "control_conversions": 100,
        "control_rate": 0.10,
        "absolute_lift": ConfidenceValue(value=0.02, ci_low=0.001, ci_high=0.039),
        "relative_lift": 0.2,
        "incremental_conversions": ConfidenceValue(value=180.0, ci_low=9.0, ci_high=351.0),
        "p_value": 0.04,
        "rows_immature": 0,
        "rows_without_outcome": 0,
        "rows_suppressed_or_untreated": 0,
        "causal": True,
        "summary": "…",
        "computed_at": datetime(2026, 9, 1, tzinfo=UTC),
    }
    return IncrementalityReport.model_validate({**base, **fields})


def test_a_clear_gain_is_the_number_the_report_measured() -> None:
    verdict = campaign_verdict(_report(), outcome_is_good=True, outcome_label="Bought within 30 days")
    assert verdict.kind is VerdictKind.ADDED
    assert verdict.headline == "The campaign added about 180 conversions"
    assert verdict.detail == "Likely between 9 and 351. Counted: Bought within 30 days."
    assert (verdict.amount, verdict.likely_low, verdict.likely_high) == (180, 9, 351)


def test_a_churn_campaign_that_lowered_churn_prevented_cases() -> None:
    report = _report(
        absolute_lift=ConfidenceValue(value=-0.02, ci_low=-0.039, ci_high=-0.001),
        incremental_conversions=ConfidenceValue(value=-180.0, ci_low=-351.0, ci_high=-9.0),
    )
    verdict = campaign_verdict(report, outcome_is_good=False)
    assert verdict.kind is VerdictKind.PREVENTED
    assert verdict.headline == "The campaign prevented about 180 cases"
    assert (verdict.likely_low, verdict.likely_high) == (9, 351)
    harmed = campaign_verdict(report, outcome_is_good=True)
    assert harmed.kind is VerdictKind.HARMED and harmed.headline == "The campaign cost about 180 conversions"


def test_an_interval_that_includes_zero_is_no_clear_effect_and_no_number() -> None:
    report = _report(
        absolute_lift=ConfidenceValue(value=0.005, ci_low=-0.01, ci_high=0.02),
        incremental_conversions=ConfidenceValue(value=45.0, ci_low=-90.0, ci_high=180.0),
    )
    verdict = campaign_verdict(report, outcome_is_good=True)
    assert verdict.kind is VerdictKind.NO_CLEAR_EFFECT
    assert verdict.headline == "No clear effect yet" and verdict.amount is None


def test_too_early_no_control_and_nothing_matched() -> None:
    early = _report(
        status=IncrementalityStatus.IMMATURE,
        results_available_on=date(2026, 7, 30),
        treated_rate=None,
        control_rate=None,
        absolute_lift=None,
        incremental_conversions=None,
        relative_lift=None,
        p_value=None,
        rows_immature=10_000,
    )
    verdict = campaign_verdict(early, outcome_is_good=True)
    assert verdict.kind is VerdictKind.TOO_EARLY and verdict.headline == "Outcome window not over yet"
    assert verdict.detail.endswith("on or after 30 Jul 2026.") and verdict.results_on == date(2026, 7, 30)
    assert campaign_verdict(_report(causal=False), outcome_is_good=True).kind is VerdictKind.NO_CONTROL
    empty = _report(
        treated_rows=0,
        treated_conversions=0,
        control_rows=0,
        control_conversions=0,
        treated_rate=None,
        control_rate=None,
        absolute_lift=None,
        incremental_conversions=None,
    )
    assert campaign_verdict(empty, outcome_is_good=True).kind is VerdictKind.NOTHING_MATCHED


def test_learning_needs_both_groups_above_the_uplift_floors() -> None:
    floors = UpliftConfig()
    assert learn_readiness(None, floors).reason == "Measure the campaign first."
    assert learn_readiness(_report(), floors).ready is True
    small = learn_readiness(_report(control_rows=400, control_conversions=40), floors)
    assert small.ready is False
    assert small.reason.startswith("To learn who to contact next time, each group needs at least 1,000")
    assert "400 held back (40 responded)" in small.reason
    assert learn_readiness(_report(rows_immature=5), floors).ready is False


def test_the_outcome_column_is_the_use_case_s_own_else_the_only_other_one() -> None:
    assert (
        detect_outcome_column(["id", "x", "converted"], primary_key="id", target_column="converted")
        == "converted"
    )
    assert detect_outcome_column(["id", "bought"], primary_key="id", target_column="converted") == "bought"
    with pytest.raises(ValueError, match="only the customer id"):
        detect_outcome_column(["id"], primary_key="id", target_column="converted")
    with pytest.raises(ValueError, match="Keep only the customer id"):
        detect_outcome_column(["id", "a", "b"], primary_key="id", target_column="converted")
    with pytest.raises(ValueError, match="same customer id column"):
        detect_outcome_column(["cust", "converted"], primary_key="id", target_column="converted")


def test_the_experiment_file_is_the_input_with_treatment_and_outcome() -> None:
    inputs = pd.DataFrame({"id": [1, 2, 3, 4, 5], "tenure": [10, 20, 30, 40, 50], "converted": [None] * 5})
    scores = pd.DataFrame(
        {
            "id": ["1", "2", "3", "4", "5"],
            "suppressed_reason": [None, "opted_out", None, None, None],
            "control_group": [False, False, True, False, True],
        }
    )
    outcomes = pd.DataFrame({"id": [5, 4, 3, 2, 1], "bought": ["yes", None, "no", "yes", "no"]})
    frame = build_experiment_frame(
        inputs,
        scores,
        outcomes,
        primary_key="id",
        outcome_column="bought",
        positive_label=None,
        target_column="converted",
        treatment_column=treatment_column_for(list(inputs.columns)),
    )
    # 2 was suppressed (neither arm); 4 has no known outcome; nothing is guessed.
    assert frame["id"].tolist() == [1, 3, 5]
    assert frame["contacted"].tolist() == [1, 0, 0]
    assert frame["converted"].tolist() == [0, 0, 1]
    assert frame["tenure"].tolist() == [10, 30, 50]
    assert treatment_column_for(["contacted", "contacted_2"]) == "contacted_3"


def test_an_uplift_run_learns_only_inside_the_customers_its_policy_intended_to_treat() -> None:
    inputs = pd.DataFrame({"id": ["a", "b", "c"], "x": [1, 2, 3]})
    scores = pd.DataFrame(
        {
            "id": ["a", "b", "c"],
            "control_group": [False, True, False],
            "intended_treatment": [True, True, False],
        }
    )
    outcomes = pd.DataFrame({"id": ["a", "b", "c"], "y": [1, 0, 1]})
    frame = build_experiment_frame(
        inputs,
        scores,
        outcomes,
        primary_key="id",
        outcome_column="y",
        positive_label=None,
        target_column="y",
        treatment_column="contacted",
    )
    assert frame["id"].tolist() == ["a", "b"]
    assert frame["contacted"].tolist() == [1, 0]
