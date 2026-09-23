"""The local scheduler, driven one tick at a time by a fake clock (DEC-762, DEC-763, DEC-764).

The firer is the real one; the schedules are drift checks on a use case with no champion yet, which
fire, succeed with `NO_CHAMPION` and touch nothing else - so these tests are about *when* things fire
and what is recorded, not about what a firing does (that is `test_firing.py`).
"""

from __future__ import annotations

import time
from datetime import timedelta
from pathlib import Path

import pytest

from engine.access.roles import SYSTEM_SCHEDULER
from engine.scheduling.alerts import AlertKind, AlertQuery
from engine.scheduling.firing import ScheduleFirer
from engine.scheduling.scheduler import LocalScheduler, NullScheduler, build_scheduler
from engine.scheduling.schedules import FiringStatus, FiringTrigger, ScheduleKind
from engine.settings import Settings
from tests.unit.production.scheduling_support import T0, World, make_world


@pytest.fixture
def world(tmp_path: Path, config_root: Path) -> World:
    return make_world(tmp_path, config_root)


def hourly(world: World) -> LocalScheduler:
    """An hourly UTC drift check due at T0+1h, and a scheduler over it ticking every 60 s."""
    world.schedule(ScheduleKind.DRIFT_CHECK, cron="0 * * * *", timezone="UTC")
    return LocalScheduler(world.store, ScheduleFirer(world.services()), tick_seconds=60, clock=world.clock)


def statuses(world: World) -> list[tuple[str, str]]:
    return sorted(
        (firing.trigger.value, firing.status.value) for firing in world.store.list_firings(limit=1000)
    )


def test_nothing_fires_before_the_slot(world: World) -> None:
    scheduler = hourly(world)
    world.clock.advance(minutes=59)
    assert scheduler.tick() == ()


def test_the_due_slot_fires_once_as_scheduled(world: World) -> None:
    scheduler = hourly(world)
    world.clock.advance(hours=1, seconds=5)
    (fired,) = scheduler.tick()
    assert fired.trigger is FiringTrigger.SCHEDULED
    assert fired.scheduled_for == T0 + timedelta(hours=1)
    assert fired.status is FiringStatus.SUCCEEDED
    assert fired.result_code == "NO_CHAMPION"
    assert world.store.get("sch_drift_check").next_due_at == T0 + timedelta(hours=2)


def test_ticking_again_never_fires_the_same_slot_twice(world: World) -> None:
    scheduler = hourly(world)
    world.clock.advance(hours=1, seconds=5)
    scheduler.tick()
    world.clock.advance(seconds=30)
    assert scheduler.tick() == ()
    assert len(world.store.list_firings()) == 1


def test_a_second_scheduler_on_the_same_table_cannot_fire_the_slot_again(world: World) -> None:
    """Two replicas, one table: the slot claim is what stops the second (DEC-763)."""
    first = hourly(world)
    second = LocalScheduler(world.store, ScheduleFirer(world.services()), tick_seconds=60, clock=world.clock)
    world.clock.advance(hours=1, seconds=5)
    stale = world.store.get("sch_drift_check")  # read before the first moved next_due_at on
    first.tick()
    assert ScheduleFirer(world.services()).run_due(stale, now=world.clock(), grace=timedelta(minutes=3)) == ()
    assert second.tick() == ()
    assert len(world.store.list_firings()) == 1


def test_consecutive_slots_each_fire(world: World) -> None:
    scheduler = hourly(world)
    for hour in (1, 2, 3):
        world.clock.now = T0 + timedelta(hours=hour, seconds=10)
        assert len(scheduler.tick()) == 1
    assert statuses(world) == [("scheduled", "succeeded")] * 3


def test_slots_that_passed_while_nothing_ran_are_missed_alerted_and_caught_up_once(world: World) -> None:
    scheduler = hourly(world)
    world.clock.now = T0 + timedelta(hours=3, minutes=30)  # slots 1h, 2h, 3h passed unseen
    produced = scheduler.tick()
    assert len(produced) == 4
    assert statuses(world) == [("catch_up", "succeeded")] + [("scheduled", "missed")] * 3
    missed = world.store.list_firings(status=FiringStatus.MISSED)
    assert {firing.scheduled_for for firing in missed} == {T0 + timedelta(hours=h) for h in (1, 2, 3)}
    assert all(firing.error_code == "SCHEDULE_MISSED" for firing in missed)
    (alert,) = world.alerts.store.query(AlertQuery(kind=AlertKind.SCHEDULE_MISSED))
    assert alert.schedule_id == "sch_drift_check"
    assert "missed 3 run(s)" in alert.message
    actions = [event.action for event in world.audit.events]
    assert actions.count("schedules.missed") == 1
    assert actions.count("schedules.fire") == 1
    assert all(event.actor_id == SYSTEM_SCHEDULER.user_id for event in world.audit.events)
    assert world.store.get("sch_drift_check").next_due_at == T0 + timedelta(hours=4)
    # And the next tick does not treat those slots as due again.
    world.clock.advance(minutes=1)
    assert scheduler.tick() == ()


def test_a_disabled_schedule_does_not_fire_or_miss(world: World) -> None:
    scheduler = hourly(world)
    world.store.save(world.store.get("sch_drift_check").model_copy(update={"enabled": False}))
    world.clock.advance(hours=5)
    assert scheduler.tick() == ()
    assert world.store.list_firings() == ()


def test_one_broken_schedule_does_not_stop_the_others(world: World) -> None:
    class Exploding(ScheduleFirer):
        def run_due(self, schedule, *, now, grace):  # type: ignore[no-untyped-def]
            if schedule.schedule_id == "sch_bad":
                raise RuntimeError("boom")
            return super().run_due(schedule, now=now, grace=grace)

    world.schedule(ScheduleKind.DRIFT_CHECK, schedule_id="sch_bad", cron="0 * * * *", timezone="UTC")
    world.schedule(ScheduleKind.DRIFT_CHECK, schedule_id="sch_good", cron="0 * * * *", timezone="UTC")
    scheduler = LocalScheduler(world.store, Exploding(world.services()), tick_seconds=60, clock=world.clock)
    world.clock.advance(hours=1, seconds=1)
    (fired,) = scheduler.tick()
    assert fired.schedule_id == "sch_good"


def test_the_thread_ticks_and_stops(world: World) -> None:
    world.clock.advance(hours=1, seconds=1)
    scheduler = hourly(world)
    due = T0 + timedelta(hours=1)
    world.store.save(world.store.get("sch_drift_check").model_copy(update={"next_due_at": due}))
    scheduler.start()
    try:
        assert scheduler.running
        deadline = time.monotonic() + 10
        while not world.store.list_firings() and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        scheduler.stop()
    assert not scheduler.running
    assert len(world.store.list_firings()) == 1


def test_build_scheduler_picks_the_backend(world: World) -> None:
    firer = ScheduleFirer(world.services())
    assert isinstance(build_scheduler(Settings(), store=world.store), NullScheduler)
    local = build_scheduler(
        Settings(scheduler_backend="local", scheduler_tick_seconds=5), store=world.store, firer=firer
    )
    assert isinstance(local, LocalScheduler)
    with pytest.raises(ValueError, match="firer"):
        build_scheduler(Settings(scheduler_backend="local"), store=world.store)
