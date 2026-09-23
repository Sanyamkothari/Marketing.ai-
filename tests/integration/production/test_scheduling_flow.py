"""M49 end to end, for real: scheduled retrain -> approval by a person -> scheduled score -> drift check.

Plan section 4's scheduler bullet, "retraining yields a challenger, never an automatic champion",
tested the only way that settles it: AutoGluon fits a model on a dataset a scheduled firing built,
the pipeline's own register stage decides, and the champion does not change until an Approver - a
person, never `SYSTEM_SCHEDULER` - approves. Then the approved champion scores a fresh build of the
recipe on a schedule, and a drift check reads that run's drift against the champion's baseline.

Slow (two one-minute trainings). The fast variants of every assertion are in
`tests/unit/production/test_firing.py`.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from engine.contracts import ModelStatus, RunRecord, RunState
from engine.jobs import ThreadJobRunner
from engine.scheduling.firing import ScheduleFirer
from engine.scheduling.schedules import FiringStatus, ScheduleFiring, ScheduleKind
from engine.storage import run_key
from tests.unit.production.scheduling_support import USE_CASE, MemoryFlags, World, make_world

pytestmark = [pytest.mark.slow, pytest.mark.integration]

FAST_SEARCH = "\nmodel_search:\n  time_limit_minutes: 1\n  strategy: fast\n  tuning_trials: 5\n"
"""The integration budget of `tests/integration/test_train_flow.py` (plan section 10), as a use-case
override: a firing resolves the use case's own configuration, so the budget has to live there."""


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory, config_root: Path) -> World:
    root = tmp_path_factory.mktemp("scheduling-flow")
    patched = root / "configs"
    shutil.copytree(config_root, patched)
    use_case = patched / "use_cases" / "telco_churn.yaml"
    use_case.write_text(use_case.read_text() + FAST_SEARCH)
    return make_world(root / "w", patched)


def run_to_the_end(
    world: World, jobs: ThreadJobRunner, firer: ScheduleFirer, firing: ScheduleFiring
) -> ScheduleFiring:
    assert firing.run_id is not None, (firing.error_code, firing.result_code)
    jobs.wait(firing.run_id, timeout=900)
    firer.settle()
    return world.store.get_firing(firing.firing_id)


def test_a_scheduled_retrain_waits_for_a_person_then_the_champion_scores_on_schedule(world: World) -> None:
    jobs = ThreadJobRunner(max_workers=1)
    world.flags = MemoryFlags(f"m_{USE_CASE}_0")
    firer = ScheduleFirer(world.services(jobs=jobs))
    try:
        # 1. The first retrain: no champion yet, and the rule would promote - approval is required.
        retrain = world.schedule(ScheduleKind.RETRAIN)
        first = run_to_the_end(world, jobs, firer, firer.fire(retrain) or pytest.fail("not fired"))
        assert first.status is FiringStatus.SUCCEEDED, first.error_code
        assert first.result_code == "MODEL_PENDING_APPROVAL"
        record = world.storage.read_model(run_key(first.run_id or "", "run.json"), RunRecord)
        assert record.state is RunState.DONE
        version = world.registry.get(record.model_version_id or "")
        assert version.status is ModelStatus.PENDING_APPROVAL
        assert world.registry.get_champion(USE_CASE) is None, "never an automatic champion"
        assert world.flags.cleared == [f"m_{USE_CASE}_0"]

        # 2. A person approves it.
        world.registry.approve(version.model_id, by="u_approver")
        champion = world.registry.get_champion(USE_CASE)
        assert champion is not None and champion.model_id == version.model_id

        # 3. A scheduled score of a fresh build, against that champion.
        score = run_to_the_end(
            world, jobs, firer, firer.fire(world.schedule(ScheduleKind.SCORE)) or pytest.fail()
        )
        assert score.status is FiringStatus.SUCCEEDED, score.error_code
        assert score.result_code == "SCORED"
        assert world.storage.exists(run_key(score.run_id or "", "scores.parquet"))

        # 4. The drift check reads that run's drift against the champion.
        drift = firer.fire(world.schedule(ScheduleKind.DRIFT_CHECK)) or pytest.fail()
        assert drift.result_code in {"DRIFT_OK", "DRIFT_ABOVE_THRESHOLD"}
        if drift.status is FiringStatus.RUNNING:  # drifted under on_drift: a retrain was started
            drift = run_to_the_end(world, jobs, firer, drift)

        # 5. Retraining with a champion in place still never replaces it on its own.
        second = run_to_the_end(
            world, jobs, firer, firer.fire(world.store.get(retrain.schedule_id)) or pytest.fail()
        )
        assert second.status is FiringStatus.SUCCEEDED, second.error_code
        assert second.result_code in {"MODEL_PENDING_APPROVAL", "MODEL_CANDIDATE"}
        current = world.registry.get_champion(USE_CASE)
        assert current is not None and current.model_id == champion.model_id
    finally:
        jobs.shutdown(wait=True)
