"""M108 review fix (DEC-1318): a schedule's firing passes the run cost gate, and its run is watched.

The review's major finding: `POST /runs` refused a capped run until a person confirmed it and watched
it afterwards, but a scheduled firing called `create_run` and `jobs.submit` directly, so an unattended
scoring run of a capped use case was never refused and nobody was looking at its cost. These tests fire
a real schedule (the real recipe rebuild, dataset checks and `create_run`, as `test_firing.py` does) on a
deployment described by `Settings(job_backend="sagemaker", ...)` with the checkout's own price list, a
cap in the services' configuration, and a controlled clock for the run's cost.
"""

from __future__ import annotations

import shutil
import threading
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from engine.aws import run_cost
from engine.aws.prices import PRICES_FILENAME, SECONDS_PER_HOUR, load_price_table
from engine.config import RunMode, load_use_case
from engine.contracts import RunRecord, RunState
from engine.onboarding.datasets import dataset_key
from engine.runs import update_run
from engine.scheduling.firing import ScheduleFirer, build_dataset_from_spec
from engine.scheduling.schedules import FiringStatus, ScheduleKind
from engine.storage import run_key
from engine.utils.time import utc_now
from tests.fixtures.settings import sagemaker_settings
from tests.unit.production.scheduling_support import (
    USE_CASE,
    RecordingJobs,
    World,
    make_world,
    register_champion,
)

REGION = "ap-south-1"
SCORE_INSTANCE = "ml.m5.xlarge"
LIMIT_S = 7200


class CancellableJobs(RecordingJobs):
    """Records submissions as the scheduler's stand-in does, and accepts a cancel."""

    def __init__(self) -> None:
        super().__init__()
        self.cancelled: list[str] = []

    def cancel(self, job_id: str) -> bool:
        self.cancelled.append(job_id)
        return True


def deployment() -> Any:
    return sagemaker_settings(
        sagemaker_instance_type="ml.m5.2xlarge",
        sagemaker_processing_instance_type=SCORE_INSTANCE,
        sagemaker_max_runtime_seconds=LIMIT_S,
    )


def ceiling_usd(config_root: Path) -> float:
    table = load_price_table(config_root / PRICES_FILENAME)
    assert table is not None
    rate = table.rate(component="processing", instance_type=SCORE_INSTANCE, region=REGION)
    assert rate is not None
    return LIMIT_S * rate.usd_per_hour / SECONDS_PER_HOUR


def capped_root(config_root: Path, target: Path, cap: float | None) -> Path:
    shutil.copytree(config_root, target)
    if cap is not None:
        path = target / "engine.yaml"
        text = path.read_text(encoding="utf-8")
        assert "max_run_cost_usd: null" in text
        path.write_text(
            text.replace("max_run_cost_usd: null", f"max_run_cost_usd: {cap}", 1), encoding="utf-8"
        )
    return target


def fire_score(world: World, jobs: CancellableJobs, settings: Any) -> Any:
    config = load_use_case(USE_CASE)
    manifest = build_dataset_from_spec(
        world.spec, config=config, mode=RunMode.TRAIN, client_store=world.client_store, storage=world.storage
    )
    frame = pd.read_parquet(world.storage.local_path(dataset_key(manifest.dataset_id, "dataset.parquet")))
    register_champion(world, frame)
    services = world.services(jobs=jobs, settings=settings)
    return ScheduleFirer(services).fire(world.schedule(ScheduleKind.SCORE))


def wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def watchers() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith("cost-watch-") and t.is_alive()]


def test_a_scheduled_run_that_needs_confirmation_is_refused_and_leaves_no_run(
    tmp_path: Path, config_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MARKETING_AI_CONFIG_DIR", raising=False)
    root = capped_root(config_root, tmp_path / "configs", round(ceiling_usd(config_root) / 2, 4))
    world = make_world(tmp_path / "w", root)
    jobs = CancellableJobs()
    firing = fire_score(world, jobs, deployment())
    assert firing is not None and firing.status is FiringStatus.FAILED, firing
    assert firing.error_code == "RUN_COST_NEEDS_CONFIRMATION"
    assert firing.run_id is None and jobs.submitted == [], "a refused firing starts nothing"
    assert not [key for key in world.storage.list_keys("runs/") if key.endswith("/run.json")]


def test_a_scheduled_run_that_cannot_be_priced_is_refused_too(
    tmp_path: Path, config_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MARKETING_AI_CONFIG_DIR", raising=False)
    root = capped_root(config_root, tmp_path / "configs", 5.0)
    (root / PRICES_FILENAME).unlink()
    world = make_world(tmp_path / "w", root)
    firing = fire_score(world, CancellableJobs(), deployment())
    assert firing is not None and firing.error_code == "RUN_COST_NEEDS_CONFIRMATION"


def test_a_scheduled_capped_run_past_the_cap_is_stopped_without_anyone_polling(
    tmp_path: Path, config_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MARKETING_AI_CONFIG_DIR", raising=False)
    monkeypatch.setattr(run_cost, "COST_WATCH_INTERVAL_S", 0.02)
    cap = round(ceiling_usd(config_root) * 1.5, 4)  # above the estimate, so the firing starts
    root = capped_root(config_root, tmp_path / "configs", cap)
    world = make_world(tmp_path / "w", root)
    jobs = CancellableJobs()
    firing = fire_score(world, jobs, deployment())
    assert firing is not None and firing.status is FiringStatus.RUNNING, firing
    run_id = firing.run_id
    assert run_id is not None
    started = utc_now()
    update_run(world.storage, run_id, state=RunState.RUNNING, started_at=started)

    monkeypatch.setattr(run_cost, "utc_now", lambda: started + timedelta(seconds=LIMIT_S * 1.5 + 600))
    assert wait_for(
        lambda: world.storage.read_model(run_key(run_id, "run.json"), RunRecord).state is RunState.CANCELLED
    )
    error = world.storage.read_model(run_key(run_id, "run.json"), RunRecord).error
    assert error is not None and error.code == "RUN_COST_CAP_REACHED"
    assert run_id in jobs.cancelled
    assert wait_for(lambda: not watchers()), "the watcher ends with the run"


def test_a_scheduled_run_under_the_cap_is_left_running(
    tmp_path: Path, config_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MARKETING_AI_CONFIG_DIR", raising=False)
    monkeypatch.setattr(run_cost, "COST_WATCH_INTERVAL_S", 0.02)
    root = capped_root(config_root, tmp_path / "configs", round(ceiling_usd(config_root) * 3, 4))
    world = make_world(tmp_path / "w", root)
    jobs = CancellableJobs()
    firing = fire_score(world, jobs, deployment())
    assert firing is not None and firing.status is FiringStatus.RUNNING and firing.run_id is not None
    started = utc_now()
    update_run(world.storage, firing.run_id, state=RunState.RUNNING, started_at=started)
    monkeypatch.setattr(run_cost, "utc_now", lambda: started + timedelta(seconds=LIMIT_S))
    time.sleep(0.3)
    record = world.storage.read_model(run_key(firing.run_id, "run.json"), RunRecord)
    assert record.state is RunState.RUNNING and jobs.cancelled == []
    update_run(world.storage, firing.run_id, state=RunState.DONE, finished_at=started)  # lets the watcher end
    assert wait_for(lambda: not watchers())


@pytest.mark.parametrize("capped", [False, True])
def test_without_a_cap_or_without_settings_a_firing_is_exactly_as_before(
    tmp_path: Path, config_root: Path, monkeypatch: pytest.MonkeyPatch, capped: bool
) -> None:
    monkeypatch.delenv("MARKETING_AI_CONFIG_DIR", raising=False)
    root = capped_root(config_root, tmp_path / "configs", 0.0001 if capped else None)
    world = make_world(tmp_path / "w", root)
    before = len(watchers())
    firing = fire_score(world, CancellableJobs(), None if capped else deployment())
    assert firing is not None and firing.status is FiringStatus.RUNNING, firing
    assert len(watchers()) == before, "no cap or no settings: no watcher, no refusal"


def test_a_local_deployment_with_a_cap_is_not_gated_or_watched(
    tmp_path: Path, config_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.fixtures.settings import local_settings

    monkeypatch.delenv("MARKETING_AI_CONFIG_DIR", raising=False)
    root = capped_root(config_root, tmp_path / "configs", 0.0001)
    world = make_world(tmp_path / "w", root)
    before = len(watchers())
    firing = fire_score(world, CancellableJobs(), local_settings())
    assert firing is not None and firing.status is FiringStatus.RUNNING, firing
    assert len(watchers()) == before
