"""Scheduling review fixes (DEC-775 … DEC-779), each written first as the failing reproduction.

* an EventBridge slot that was never delivered is reported when the next one arrives;
* a managed schedule whose push failed is pushed again at the next sync;
* an erasure flag is answered by an `on_drift` drift check that cannot measure drift;
* a scheduler-started training run never crowns a champion, whatever the use case says;
* two processes sweeping one backlog run one catch-up and send one alert;
* two settlers of one failed firing send one alert.
"""

from __future__ import annotations

import shutil
from datetime import timedelta
from pathlib import Path

import pytest

from engine.config import ResolvedConfig, Retraining
from engine.contracts import RunError
from engine.platform_db import sqlite_engine
from engine.runs import fail_run
from engine.scheduling.alerts import AlertKind, AlertQuery
from engine.scheduling.firing import ScheduleFirer
from engine.scheduling.retraining import RetrainingTarget, sync_retraining_schedules
from engine.scheduling.schedules import (
    FiringStatus,
    FiringTrigger,
    Schedule,
    ScheduleFiring,
    ScheduleKind,
    SqlScheduleStore,
    new_firing_id,
)
from engine.storage import run_key
from tests.unit.production.scheduling_support import (
    USE_CASE,
    MemoryFlags,
    World,
    make_world,
    register_champion,
)
from tests.unit.production.test_firing import training_frame
from tests.unit.production.test_schedule_service import T0, RecordingScheduler


@pytest.fixture
def world(tmp_path: Path, config_root: Path) -> World:
    return make_world(tmp_path, config_root)


def firings(world: World, status: FiringStatus) -> list[ScheduleFiring]:
    return list(world.store.list_firings(status=status))


# --- DEC-775 -------------------------------------------------------------------------------------
def test_a_slot_eventbridge_never_delivered_is_reported_when_the_next_one_arrives(world: World) -> None:
    schedule = world.schedule(ScheduleKind.DRIFT_CHECK)
    first = schedule.next_due_at
    assert first is not None
    second = schedule.next_slot_after(first)
    assert second is not None
    world.clock.now = second + timedelta(minutes=2)
    firer = ScheduleFirer(world.services())
    firer.fire_scheduled(schedule.schedule_id, scheduled_for=second)

    (missed,) = firings(world, FiringStatus.MISSED)
    assert missed.scheduled_for == first
    assert len(world.alerts.store.query(AlertQuery(kind=AlertKind.SCHEDULE_MISSED))) == 1
    assert world.store.get(schedule.schedule_id).next_due_at == schedule.next_slot_after(second)


def test_a_skipped_slot_still_inside_the_grace_is_left_for_the_sweep(world: World) -> None:
    schedule = world.schedule(ScheduleKind.DRIFT_CHECK, cron="*/10 * * * *")
    first = schedule.next_due_at
    assert first is not None
    second = schedule.next_slot_after(first)
    assert second is not None
    world.clock.now = second + timedelta(minutes=1)
    ScheduleFirer(world.services()).fire_scheduled(schedule.schedule_id, scheduled_for=second)
    assert firings(world, FiringStatus.MISSED) == [], "EventBridge may still deliver it"
    assert world.store.get(schedule.schedule_id).next_due_at == first


# --- DEC-776 -------------------------------------------------------------------------------------
class FailsOnce(RecordingScheduler):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def sync(self, schedule: Schedule) -> None:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("EventBridge said no")
        super().sync(schedule)


def test_a_managed_schedule_whose_push_failed_is_pushed_at_the_next_sync(tmp_path: Path) -> None:
    store = SqlScheduleStore(sqlite_engine(tmp_path / "platform.db"))
    scheduler = FailsOnce()
    wanted = [RetrainingTarget(client_id="c_demo_1", use_case_id=USE_CASE, retraining=Retraining.WEEKLY)]
    with pytest.raises(RuntimeError):
        sync_retraining_schedules(store, scheduler, wanted, now=T0)
    (row,) = store.list()
    sync_retraining_schedules(store, scheduler, wanted, now=T0)
    assert [schedule.schedule_id for schedule in scheduler.synced] == [row.schedule_id]


# --- DEC-777 -------------------------------------------------------------------------------------
def test_an_erasure_flag_is_answered_when_there_is_no_scored_data_to_measure(world: World) -> None:
    champion = register_champion(world, training_frame(world))
    world.flags = MemoryFlags(champion.model_id)
    firing = ScheduleFirer(world.services()).fire(world.schedule(ScheduleKind.DRIFT_CHECK))
    assert firing is not None
    assert (firing.status, firing.result_code) == (FiringStatus.RUNNING, "RETRAIN_FOR_ERASURE")
    assert firing.flagged_models == (champion.model_id,)
    assert world.jobs.submitted == [firing.run_id]


# --- DEC-778 -------------------------------------------------------------------------------------
def test_a_scheduled_retrain_waits_for_approval_even_when_the_use_case_does_not(
    tmp_path: Path, config_root: Path
) -> None:
    patched = tmp_path / "configs"
    shutil.copytree(config_root, patched)
    use_case = patched / "use_cases" / "telco_churn.yaml"
    use_case.write_text(use_case.read_text() + "\ngovernance:\n  approval_required: false\n")
    world = make_world(tmp_path / "w", patched)
    firing = ScheduleFirer(world.services()).fire(world.schedule(ScheduleKind.RETRAIN))
    assert firing is not None and firing.run_id is not None
    resolved = world.storage.read_model(run_key(firing.run_id, "run_config.json"), ResolvedConfig)
    assert resolved.config.governance.approval_required is True
    assert resolved.sources.get("governance.approval_required") == "override"


# --- DEC-779 -------------------------------------------------------------------------------------
def test_a_process_that_lost_the_newest_missed_slot_neither_catches_up_nor_alerts(world: World) -> None:
    schedule = world.schedule(ScheduleKind.DRIFT_CHECK)
    first = schedule.next_due_at
    assert first is not None
    slots = [first]
    for _ in range(2):
        following = schedule.next_slot_after(slots[-1])
        assert following is not None
        slots.append(following)
    world.clock.now = slots[-1] + timedelta(hours=3)
    for slot in slots[1:]:  # another process has already claimed the two newest slots
        world.store.claim_firing(
            ScheduleFiring(
                firing_id=new_firing_id(world.clock.now),
                schedule_id=schedule.schedule_id,
                client_id=schedule.client_id,
                use_case_id=schedule.use_case_id,
                kind=schedule.kind,
                trigger=FiringTrigger.SCHEDULED,
                status=FiringStatus.MISSED,
                scheduled_for=slot,
                error_code="SCHEDULE_MISSED",
                fired_at=world.clock.now,
                finished_at=world.clock.now,
            )
        )
    produced = ScheduleFirer(world.services()).run_due(
        schedule, now=world.clock.now, grace=timedelta(minutes=5)
    )
    assert [item.status for item in produced] == [FiringStatus.MISSED], "slot 1 only, and no catch-up"
    assert world.alerts.store.query(AlertQuery(kind=AlertKind.SCHEDULE_MISSED)) == ()


def test_two_settlers_of_one_failed_firing_send_one_alert(world: World) -> None:
    firer = ScheduleFirer(world.services())
    firing = firer.fire(world.schedule(ScheduleKind.RETRAIN))
    assert firing is not None and firing.run_id is not None
    fail_run(world.storage, firing.run_id, RunError(code="TRAINING_FAILED", message="x", stage=None))
    stale = world.store.get_firing(firing.firing_id)  # both settlers read it while it was running
    other = ScheduleFirer(world.services())
    assert firer._settle_one(stale) is not None
    assert other._settle_one(stale) is None
    assert len(world.alerts.store.query(AlertQuery(kind=AlertKind.SCHEDULED_JOB_FAILED))) == 1
