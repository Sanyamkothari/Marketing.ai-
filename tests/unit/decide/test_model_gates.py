"""Plan J M96: the Approver's advisory checks (`engine.model_gates`, `ApprovalItem.checks`).

The checks read what the training run measured; they never approve, reject or block anything, the
frozen champion rule is not consulted, and a propensity model has none. On an equal-effect hold-out the
beats-risk check fails with "does not beat risk ranking"; an evaluation written before M96 is "not
measured" (`passed: null`), never a made-up pass. Fails on the code before M96 (no `engine.model_gates`,
no `ApprovalItem.checks`).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from engine.access.roles import LOCAL_OPERATOR
from engine.approvals import pending_approvals
from engine.config import Metric
from engine.contracts import ModelStatus, ModelVersion
from engine.measurement.compare import fold_auuc_not_computed
from engine.model_gates import (
    UPLIFT_MISCALIBRATED,
    UPLIFT_UNSTABLE_ACROSS_FOLDS,
    approval_checks,
    uplift_checks,
)
from engine.platform_db import sqlite_engine
from engine.registry import LocalModelRegistry
from engine.storage import LocalStorage, run_key
from engine.uplift.contracts import UPLIFT_NOT_BETTER_THAN_RISK, UpliftEvaluation
from engine.uplift.metrics import (
    BaselineInput,
    calibration_by_decile,
    compare_with_baselines,
    evaluate_uplift,
)

RUN_ID = "r_20261008_0d960002"
NOW = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)


def evaluation(effect: str = "equal", *, with_m96: bool = True) -> UpliftEvaluation:
    rng = np.random.default_rng(31)
    rows = 6_000
    risk = 0.1 + 0.4 * rng.random(rows)
    tau = np.full(rows, 0.05) if effect == "equal" else 0.25 * rng.random(rows)
    t = rng.integers(0, 2, rows)
    y = (rng.random(rows) < risk + t * tau).astype(int)
    pred = rng.random(rows) if effect == "equal" else tau
    result, _ = evaluate_uplift(
        pred,
        t,
        y,
        run_id=RUN_ID,
        learner="x_learner",  # type: ignore[arg-type]
        base_model="lightgbm",  # type: ignore[arg-type]
        bootstrap_samples=100,
        seed=5,
        causal=True,
        now=NOW,
    )
    if not with_m96:
        return result
    return result.model_copy(
        update={
            "baseline_comparison": compare_with_baselines(
                pred, t, y, [BaselineInput("p_control", risk)], samples=100, seed=5
            ),
            "calibration_by_decile": calibration_by_decile(pred, t, y, samples=100, seed=5),
            "fold_auuc": fold_auuc_not_computed(
                folds=5, reason="Off by default.", estimated_refit_seconds=40.0
            ),
        }
    )


def version(
    metric: Metric = Metric.AUUC, *, status: ModelStatus = ModelStatus.PENDING_APPROVAL
) -> ModelVersion:
    return ModelVersion(
        model_id="m_uplift_1",
        use_case_id="win-back-campaign",
        version=1,
        run_id=RUN_ID,
        created_at=NOW,
        status=status,
        metric=metric,
        metric_label=metric.value,
        test_score=0.01,
        model_display_name="X-learner (LightGBM)",
        schema_key="runs/r/schema.json",
        run_config_key="runs/r/run_config.json",
        predictor_key=f"runs/{RUN_ID}/model",
        engine_version="0",
        autogluon_version="0",
    )


def test_on_an_equal_effect_the_screen_says_does_not_beat_risk_ranking() -> None:
    checks = {check.code: check for check in uplift_checks(evaluation("equal"))}
    risk = checks[UPLIFT_NOT_BETTER_THAN_RISK]
    assert risk.passed is False
    assert "does not beat risk ranking" in risk.message
    assert "includes zero" in risk.message


def test_a_model_that_beats_risk_passes_and_the_order_is_fixed() -> None:
    checks = uplift_checks(evaluation("heterogeneous"))
    assert [check.code for check in checks] == [
        UPLIFT_NOT_BETTER_THAN_RISK,
        UPLIFT_UNSTABLE_ACROSS_FOLDS,
        UPLIFT_MISCALIBRATED,
    ]
    assert checks[0].passed is True
    assert checks[2].passed is True


def test_a_check_that_is_off_is_not_measured_and_says_what_it_would_cost() -> None:
    stability = uplift_checks(evaluation("equal"))[1]
    assert stability.passed is None
    assert "Off by default." in stability.message
    assert "about 40 seconds" in stability.message


def test_an_evaluation_from_before_the_checks_is_not_measured_never_passed() -> None:
    checks = uplift_checks(evaluation(with_m96=False))
    assert [check.passed for check in checks] == [None, None, None]
    assert all(check.message.startswith("Not checked") for check in checks)


def test_a_propensity_model_has_no_checks(tmp_path: Path) -> None:
    assert approval_checks(LocalStorage(tmp_path), version(Metric.ROC_AUC)) == ()


def test_the_checks_are_read_from_the_training_run_with_the_equal_budget_sentence(tmp_path: Path) -> None:
    from engine.measurement.compare import cross_fit, equal_budget_comparison
    from engine.measurement.simulate import uplift_population
    from tests.statistical.risk_comparison import linear_risk_fitter, linear_uplift_fitter

    storage = LocalStorage(tmp_path)
    storage.write_model(run_key(RUN_ID, "uplift_evaluation.json"), evaluation("equal"))
    population = uplift_population(800, seed=1, effect="null")
    cross = cross_fit(
        population.features,
        population.t,
        population.y,
        folds=5,
        seed=1,
        fit_uplift=linear_uplift_fitter,
        fit_risk=linear_risk_fitter,
        scheme="ring",
    )
    comparison = equal_budget_comparison(
        t=population.t,
        y=population.y,
        propensity=population.propensity,
        cross=cross,
        top_share=0.2,
        cost_per_contact=None,
        run_id=RUN_ID,
        causal=True,
        propensity_source="recorded",
    )
    storage.write_model(run_key(RUN_ID, "risk_comparison.json"), comparison)
    checks = approval_checks(storage, version())
    assert checks[0].code == UPLIFT_NOT_BETTER_THAN_RISK
    assert checks[0].message.endswith(comparison.summary)
    missing = approval_checks(LocalStorage(tmp_path / "empty"), version())
    assert [(check.code, check.passed) for check in missing] == [(UPLIFT_NOT_BETTER_THAN_RISK, None)]


def test_the_approvals_list_carries_the_checks_and_leaves_the_decision_alone(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path / "data")
    storage.write_model(run_key(RUN_ID, "uplift_evaluation.json"), evaluation("equal"))
    registry = LocalModelRegistry(tmp_path / "registry.db")
    registry.register(version())
    items = pending_approvals(
        storage,
        registry,
        sqlite_engine(tmp_path / "platform.db"),
        LOCAL_OPERATOR,
        may_approve=True,
        role_reason=None,
    )
    assert len(items) == 1
    item = items[0]
    assert item.can_decide is True and item.blocked_reason is None  # advisory: nothing is blocked
    assert item.checks[0].code == UPLIFT_NOT_BETTER_THAN_RISK
    assert item.checks[0].passed is False
    dumped = item.model_dump(mode="json")["checks"][0]
    assert set(dumped) == {"code", "passed", "message"}
