"""`engine.uplift.config`: which uplift settings a run may override, and which it may not.

The TREATMENT_NOT_RANDOM threshold decides whether a result may be called causal. A request that
could raise it would turn a targeted campaign into a "causal" one; acknowledging the finding is the
only way past it, and that marks every artefact not causal (plan B §4).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from engine.config import ConfigError, load_use_case, overridable_paths, resolve_config
from engine.uplift.config import (
    UPLIFT_OVERRIDABLE_PATHS,
    UPLIFT_RUN_LOCKED_PATHS,
    UpliftConfig,
    UpliftLearner,
    uplift_agent_editable_paths,
)

USE_CASE = "win-back-campaign"


def test_the_randomness_threshold_is_locked_and_not_agent_editable() -> None:
    assert {"uplift.randomness_auc_max"} == UPLIFT_RUN_LOCKED_PATHS
    editable = uplift_agent_editable_paths()
    assert all(path in editable and editable[path] is False for path in UPLIFT_RUN_LOCKED_PATHS)
    assert not UPLIFT_RUN_LOCKED_PATHS & UPLIFT_OVERRIDABLE_PATHS
    assert not UPLIFT_RUN_LOCKED_PATHS & overridable_paths(load_use_case(USE_CASE))


def test_every_other_uplift_leaf_stays_overridable_per_run() -> None:
    assert set(uplift_agent_editable_paths()) - UPLIFT_RUN_LOCKED_PATHS == UPLIFT_OVERRIDABLE_PATHS
    assert {"uplift.treatment_column", "uplift.min_arm_rows", "uplift.policy.budget_contacts"} <= (
        UPLIFT_OVERRIDABLE_PATHS
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"problem_type": "uplift", "uplift": {"randomness_auc_max": 1.0}},
        {"problem_type": "uplift", "uplift.randomness_auc_max": 0.99},
    ],
)
def test_a_run_override_of_the_randomness_threshold_is_refused(overrides: dict[str, object]) -> None:
    with pytest.raises(ConfigError) as error:
        resolve_config(USE_CASE, overrides)
    assert error.value.code == "OVERRIDE_UNKNOWN_PATH"
    assert error.value.path == "uplift.randomness_auc_max"


def test_the_arm_floors_can_still_be_set_for_one_run() -> None:
    resolved = resolve_config(USE_CASE, {"problem_type": "uplift", "uplift": {"min_arm_rows": 200}})
    assert resolved.config.uplift.min_arm_rows == 200
    assert resolved.config.uplift.randomness_auc_max == 0.60


# ---------------------------------------------------------------------------
# The learner's spelling (plan B §6 writes `s | t | x`; DEC-671)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("spelled", "stored"),
    [
        ("s", UpliftLearner.S_LEARNER),
        ("t", UpliftLearner.T_LEARNER),
        ("x", UpliftLearner.X_LEARNER),
        (" X ", UpliftLearner.X_LEARNER),
        ("t_learner", UpliftLearner.T_LEARNER),
    ],
)
def test_the_plans_short_learner_names_are_accepted(spelled: str, stored: UpliftLearner) -> None:
    assert UpliftConfig(learner=spelled).learner is stored  # type: ignore[arg-type]
    resolved = resolve_config(USE_CASE, {"problem_type": "uplift", "uplift": {"learner": spelled}})
    assert resolved.config.uplift.learner is stored


def test_the_default_learner_is_the_x_learner_and_an_unknown_one_is_refused() -> None:
    assert UpliftConfig().learner is UpliftLearner.X_LEARNER
    assert len(list(UpliftLearner)) == 3
    for wrong in ("y", "xl", "x-learner"):
        with pytest.raises(ValidationError):
            UpliftConfig(learner=wrong)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Plan K M137: the "safe to roll out" verdict's settings
# ---------------------------------------------------------------------------
def test_the_rollout_verdict_is_off_by_default_and_absent_from_a_default_config() -> None:
    evidence = UpliftConfig().evidence
    assert evidence.rollout_verdict is False and evidence.rollout_confidence == 0.95
    dumped = UpliftConfig().model_dump(mode="json")["evidence"]
    assert "rollout_verdict" not in dumped and "rollout_confidence" not in dumped
    switched = UpliftConfig.model_validate(
        {"evidence": {"risk_comparison": True, "rollout_verdict": True, "rollout_confidence": 0.9}}
    ).model_dump(mode="json")["evidence"]
    assert switched["rollout_verdict"] is True and switched["rollout_confidence"] == 0.9


def test_the_rollout_settings_are_not_agent_editable_like_the_other_evidence() -> None:
    editable = uplift_agent_editable_paths()
    assert editable["uplift.evidence.rollout_verdict"] is False
    assert editable["uplift.evidence.rollout_confidence"] is False
    assert editable["uplift.evidence.risk_comparison"] is False


@pytest.mark.parametrize("confidence", [0.79, 0.996, 1.0])
def test_the_rollout_confidence_stays_between_80_and_99_5_percent(confidence: float) -> None:
    with pytest.raises(ValidationError):
        UpliftConfig.model_validate(
            {"evidence": {"risk_comparison": True, "rollout_verdict": True, "rollout_confidence": confidence}}
        )


def test_the_rollout_verdict_needs_the_equal_budget_comparison() -> None:
    with pytest.raises(ValidationError, match="turn on risk_comparison"):
        UpliftConfig.model_validate({"evidence": {"rollout_verdict": True}})
