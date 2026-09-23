"""`engine.uplift.config`: which uplift settings a run may override, and which it may not.

The TREATMENT_NOT_RANDOM threshold decides whether a result may be called causal. A request that
could raise it would turn a targeted campaign into a "causal" one; acknowledging the finding is the
only way past it, and that marks every artefact not causal (plan B §4).
"""

from __future__ import annotations

import pytest

from engine.config import ConfigError, load_use_case, overridable_paths, resolve_config
from engine.uplift.config import (
    UPLIFT_OVERRIDABLE_PATHS,
    UPLIFT_RUN_LOCKED_PATHS,
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
