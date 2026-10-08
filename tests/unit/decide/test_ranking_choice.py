"""Plan J M96 (J5): an uplift model that does not beat risk ranking does not rank the contact list.

The acceptance criteria, on planted data:

* **uplift = minus risk** (the effect grows with risk, the model ranks by minus risk): the contact
  list falls back to the approved propensity model's ranking - the same number of `Treat` rows,
  chosen by its score among eligible customers who are not predicted sleeping dogs - and says why,
  with `UPLIFT_NOT_BETTER_THAN_RISK`;
* **heterogeneous effect** (the model knows it): the list keeps the uplift ranking, unchanged;
* **no approved propensity model**: the list keeps the uplift ranking and says plainly that the model
  does not beat risk ranking and that there was nothing to fall back to;
* **a model trained before M96** (no `baseline_comparison`): nothing is decided, nothing changes.

The verdict is the hold-out comparison of `engine.uplift.metrics.compare_with_baselines`; the contact
list is `engine.uplift.actions.apply_uplift_actions` followed by `engine.decide.ranking.rerank_by_risk`,
as the uplift scoring flow runs them. All fail on the code before M96 (`engine.decide.ranking` did not
exist).
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from engine.config import Metric, UseCaseConfig, load_use_case_document
from engine.contracts import ModelStatus, ModelVersion
from engine.decide.ranking import (
    cannot_score,
    decide_ranking,
    last_approved_propensity,
    ranking_choice,
    rerank_by_risk,
)
from engine.registry import LocalModelRegistry
from engine.uplift.actions import (
    INTENDED_TREATMENT_COLUMN,
    OVER_BUDGET_ACTION,
    TREAT_ACTION,
    apply_uplift_actions,
    tiebreak_keys,
)
from engine.uplift.contracts import UPLIFT_NOT_BETTER_THAN_RISK, BaselineComparison, SegmentThresholds
from engine.uplift.metrics import BaselineInput, compare_with_baselines

RUN_ID = "r_20261008_0d960001"
NOW = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)
ROWS = 6_000
BUDGET = 60
THRESHOLDS = SegmentThresholds(
    persuadable_min_uplift=0.02,
    sleeping_dog_max_uplift=-0.01,
    sure_thing_min_probability=0.9,
    sure_thing_from_base_rate=False,
)


def use_case() -> UseCaseConfig:
    document = copy.deepcopy(load_use_case_document("targeted-advertisement"))
    document["template"] = {"columns": []}
    document["actions"]["control_group_fraction"] = 0.10
    document["actions"]["suppression"]["suppress_opted_out"] = True
    document["actions"]["suppression"]["suppress_recently_contacted"] = False
    document["uplift"] = {"policy": {"budget_contacts": BUDGET}}
    return UseCaseConfig.model_validate(document)


def propensity_version(model_id: str = "m_prop_1", *, version: int = 1) -> ModelVersion:
    return ModelVersion(
        model_id=model_id,
        use_case_id="targeted-advertisement",
        version=version,
        run_id="r_20261001_000000_prop",
        created_at=NOW - timedelta(days=30),
        status=ModelStatus.ARCHIVED,
        metric=Metric.ROC_AUC,
        metric_label="ROC AUC",
        test_score=0.8,
        model_display_name="LightGBM",
        schema_key="runs/r/schema.json",
        run_config_key="runs/r/run_config.json",
        predictor_key="runs/r/model",
        promoted_at=NOW - timedelta(days=29),
        engine_version="0",
        autogluon_version="0",
    )


def verdict(effect: str, model: str) -> BaselineComparison:
    """The training run's hold-out verdict for a model on a planted effect (see test_beats_risk.py)."""
    rng = np.random.default_rng(17)
    risk = 0.1 + 0.4 * rng.random(ROWS)
    tau = 0.5 * (risk - 0.1) if effect == "risk" else 0.25 * rng.random(ROWS)
    t = rng.integers(0, 2, ROWS)
    y = (rng.random(ROWS) < risk + t * tau).astype(int)
    pred = 0.35 - risk if model == "minus_risk" else tau
    return compare_with_baselines(
        pred,
        t,
        y,
        [BaselineInput("p_control", risk), BaselineInput("p_treated", risk + tau)],
        samples=200,
        seed=96,
    )


def scoring_frame(model: str, *, rows: int = 1_500, seed: int = 23) -> tuple[pd.DataFrame, np.ndarray]:
    """Customers to score: the model's uplift and the propensity model's score (their risk)."""
    rng = np.random.default_rng(seed)
    risk = 0.1 + 0.4 * rng.random(rows)
    uplift = 0.35 - risk if model == "minus_risk" else 0.3 * rng.random(rows) - 0.05
    frame = pd.DataFrame(
        {
            "customer_id": [f"C-{index:05d}" for index in range(rows)],
            "uplift": uplift,
            "p_treated": np.clip(risk + uplift, 0.0, 1.0),
            "p_control": risk,
            "marketing_opt_in": [index % 10 != 0 for index in range(rows)],
        }
    )
    return frame, risk


def contact_list(
    frame: pd.DataFrame,
    risk: np.ndarray,
    comparison: BaselineComparison | None,
    propensity: ModelVersion | None,
):
    decision = decide_ranking(comparison, propensity)
    acted, recommendation = apply_uplift_actions(
        frame,
        use_case(),
        run_id=RUN_ID,
        primary_key="customer_id",
        causal=True,
        thresholds=THRESHOLDS,
        now=NOW,
        observed_top_share=None,
    )
    uplift_list = acted
    if decision is not None and decision.falls_back:
        acted = rerank_by_risk(
            acted,
            risk,
            contacts=recommendation.contacts_recommended,
            tiebreak=tiebreak_keys(acted["customer_id"], run_id=RUN_ID),
        )
    return decision, acted, uplift_list, recommendation


def test_uplift_equal_to_minus_risk_falls_back_to_the_propensity_ranking_with_the_reason() -> None:
    comparison = verdict("risk", "minus_risk")
    assert comparison.beats_risk is False
    frame, risk = scoring_frame("minus_risk")
    decision, acted, uplift_list, recommendation = contact_list(frame, risk, comparison, propensity_version())
    assert decision is not None and decision.falls_back
    assert decision.code == UPLIFT_NOT_BETTER_THAN_RISK
    assert "does not beat risk ranking" in decision.reason
    assert "ranked by the approved propensity model (LightGBM, version 1)" in decision.reason

    treat = acted["action"].to_numpy() == TREAT_ACTION
    assert recommendation.contacts_recommended == BUDGET
    assert int(treat.sum()) == BUDGET, "equal budget: as many contacts as the uplift policy chose"
    eligible = acted["suppressed_reason"].isna().to_numpy() & ~acted["control_group"].to_numpy(dtype=bool)
    candidates = eligible & (acted["segment"].to_numpy() != "sleeping_dog")
    cut = np.sort(risk[candidates])[::-1][BUDGET - 1]
    assert (risk[treat] >= cut).all(), "the Treat rows are the riskiest eligible customers"
    assert not (acted["segment"].to_numpy()[treat] == "sleeping_dog").any()
    uplift_treat = uplift_list["action"].to_numpy() == TREAT_ACTION
    assert (treat != uplift_treat).any(), "the ranking really changed"
    # The uplift policy's choices that risk did not take are passed over for the budget.
    assert set(acted["action"].to_numpy()[uplift_treat & ~treat]) <= {OVER_BUDGET_ACTION}
    # Suppression and the control group are Phase 1's, untouched.
    for column in ("suppressed_reason", "control_group", "segment", "band"):
        pd.testing.assert_series_equal(acted[column], uplift_list[column])
    # The held-out customers the same ranking would have chosen are intended too.
    intended = acted[INTENDED_TREATMENT_COLUMN].to_numpy(dtype=bool)
    control = acted["control_group"].to_numpy(dtype=bool)
    assert (intended & ~control == treat).all()
    assert (risk[intended & control] >= cut - 1e-12).all()

    choice = ranking_choice(decision, run_id=RUN_ID, model_version_id="m_up", contacts=BUDGET, now=NOW)
    assert choice.ranking == "propensity_model" and choice.propensity_model_id == "m_prop_1"
    assert choice.code == UPLIFT_NOT_BETTER_THAN_RISK and choice.beats_risk is False


def test_a_heterogeneous_effect_keeps_the_uplift_ranking() -> None:
    comparison = verdict("heterogeneous", "true_effect")
    assert comparison.beats_risk is True
    frame, risk = scoring_frame("true_effect")
    decision, acted, uplift_list, _ = contact_list(frame, risk, comparison, propensity_version())
    assert decision is not None and decision.ranking == "uplift" and decision.code is None
    assert decision.reason.startswith("Ranked by predicted uplift.")
    pd.testing.assert_frame_equal(acted, uplift_list)


def test_without_an_approved_propensity_model_the_uplift_ranking_stays_with_the_warning() -> None:
    comparison = verdict("risk", "minus_risk")
    frame, risk = scoring_frame("minus_risk")
    decision, acted, uplift_list, _ = contact_list(frame, risk, comparison, None)
    assert decision is not None and decision.ranking == "uplift"
    assert decision.code == UPLIFT_NOT_BETTER_THAN_RISK
    assert "does not beat risk ranking" in decision.reason
    assert "no approved propensity model" in decision.reason
    pd.testing.assert_frame_equal(acted, uplift_list)


def test_a_model_trained_before_the_check_changes_nothing() -> None:
    frame, risk = scoring_frame("minus_risk")
    decision, acted, uplift_list, _ = contact_list(frame, risk, None, propensity_version())
    assert decision is None
    pd.testing.assert_frame_equal(acted, uplift_list)


def test_a_propensity_model_that_cannot_score_the_file_keeps_the_uplift_ranking_and_says_why() -> None:
    comparison = verdict("risk", "minus_risk")
    decision = decide_ranking(comparison, propensity_version())
    assert decision is not None
    kept = cannot_score(decision, propensity_version(), "it needs the column tenure_months")
    assert kept.ranking == "uplift" and kept.code == UPLIFT_NOT_BETTER_THAN_RISK
    assert kept.reason.startswith(comparison.summary)
    assert "could not score this file (it needs the column tenure_months)" in kept.reason


def test_a_sleeping_dog_is_never_treated_whatever_ranks_it() -> None:
    frame, risk = scoring_frame("minus_risk")
    acted, _ = apply_uplift_actions(
        frame,
        use_case(),
        run_id=RUN_ID,
        primary_key="customer_id",
        causal=True,
        thresholds=THRESHOLDS,
        now=NOW,
    )
    # A risk score that ranks the sleeping dogs first, and a budget larger than everyone else.
    sleeping = acted["segment"].to_numpy() == "sleeping_dog"
    reranked = rerank_by_risk(
        acted,
        np.where(sleeping, 2.0, risk),
        contacts=len(frame.index),
        tiebreak=tiebreak_keys(acted["customer_id"], run_id=RUN_ID),
    )
    assert not ((reranked["action"].to_numpy() == TREAT_ACTION) & sleeping).any()
    assert not (reranked[INTENDED_TREATMENT_COLUMN].to_numpy(dtype=bool) & sleeping).any()


# ---------------------------------------------------------------------------
# The last approved propensity model
# ---------------------------------------------------------------------------
def _register(
    registry: LocalModelRegistry, model_id: str, metric: Metric, promoted_days_ago: int | None
) -> None:
    version = propensity_version(model_id, version=registry.next_version("targeted-advertisement"))
    registry.register(
        version.model_copy(
            update={
                "metric": metric,
                "status": ModelStatus.ARCHIVED if promoted_days_ago is not None else ModelStatus.CANDIDATE,
                "promoted_at": None if promoted_days_ago is None else NOW - timedelta(days=promoted_days_ago),
            }
        )
    )


def test_the_last_approved_propensity_model_is_the_newest_promoted_classification_model(
    tmp_path: Path,
) -> None:
    registry = LocalModelRegistry(tmp_path / "registry.db")
    assert last_approved_propensity(registry, "targeted-advertisement") is None
    _register(registry, "m_old", Metric.ROC_AUC, promoted_days_ago=60)
    _register(registry, "m_last", Metric.PR_AUC, promoted_days_ago=20)
    _register(registry, "m_never", Metric.ROC_AUC, promoted_days_ago=None)  # a candidate, never in use
    _register(registry, "m_uplift", Metric.AUUC, promoted_days_ago=5)  # the uplift champion took the slot
    _register(registry, "m_rmse", Metric.RMSE, promoted_days_ago=1)  # not a propensity model
    chosen = last_approved_propensity(registry, "targeted-advertisement")
    assert chosen is not None and chosen.model_id == "m_last"
    assert last_approved_propensity(registry, "another-use-case") is None


@pytest.mark.parametrize("contacts", [0, 5])
def test_the_reranked_list_contacts_exactly_the_budget(contacts: int) -> None:
    frame, risk = scoring_frame("minus_risk", rows=300)
    acted, _ = apply_uplift_actions(
        frame,
        use_case(),
        run_id=RUN_ID,
        primary_key="customer_id",
        causal=True,
        thresholds=THRESHOLDS,
        now=NOW,
    )
    reranked = rerank_by_risk(
        acted, risk, contacts=contacts, tiebreak=tiebreak_keys(acted["customer_id"], run_id=RUN_ID)
    )
    assert int((reranked["action"].to_numpy() == TREAT_ACTION).sum()) == contacts
