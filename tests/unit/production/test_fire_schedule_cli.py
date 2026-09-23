"""`scripts/fire_schedule.py`: what an EventBridge target runs - fire, wait, settle, sweep (DEC-765).

The services are the test's (`services_factory`); everything else - argument handling, the slot, the
wait on a real `ThreadJobRunner`, the exit codes and the one JSON line - is the script's own.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from engine.jobs import ThreadJobRunner
from engine.scheduling.alerts import AlertKind, AlertQuery
from engine.scheduling.schedules import FiringStatus, ScheduleKind
from engine.settings import Settings
from scripts import fire_schedule
from tests.unit.production.scheduling_support import T0, World, make_world, register_champion
from tests.unit.production.test_firing import training_frame


@pytest.fixture(autouse=True)
def _local_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ("MARKETING_AI_METADATA_BACKEND", "MARKETING_AI_STORAGE_BACKEND", "MARKETING_AI_ENV"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MARKETING_AI_DATA_DIR", str(tmp_path / "unused"))
    # `main` configures the root logger as a real invocation should; in-process that would outlive
    # the test and change what tests/unit/test_logging_audit.py sees.
    monkeypatch.setattr(fire_schedule, "configure_logging", lambda *args, **kwargs: None)


@pytest.fixture
def world(tmp_path: Path, config_root: Path) -> World:
    return make_world(tmp_path, config_root)


def run(
    world: World, capsys: pytest.CaptureFixture[str], *argv: str, jobs: Any = None
) -> tuple[int, dict[str, Any]]:
    def factory(settings: Settings) -> tuple[Any, Any]:
        del settings
        return world.services(**({"jobs": jobs} if jobs is not None else {})), jobs or world.jobs

    code = fire_schedule.main(list(argv), services_factory=factory)
    out = capsys.readouterr().out.strip()
    return code, (json.loads(out.splitlines()[-1]) if out else {})


def test_a_slot_is_fired_once_and_moves_the_schedule_on(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    schedule = world.schedule(ScheduleKind.DRIFT_CHECK)
    slot = "2026-09-01T20:30:00Z"
    code, summary = run(world, capsys, "--schedule-id", schedule.schedule_id, "--scheduled-time", slot)
    assert code == fire_schedule.EXIT_OK
    assert summary["fired"]["status"] == "succeeded"
    assert summary["fired"]["trigger"] == "scheduled"
    assert summary["fired"]["result_code"] == "NO_CHAMPION"
    assert world.store.get(schedule.schedule_id).next_due_at == datetime(2026, 9, 2, 20, 30, tzinfo=UTC)
    # EventBridge retries the same slot: the claim makes the second invocation do nothing.
    code, summary = run(world, capsys, "--schedule-id", schedule.schedule_id, "--scheduled-time", slot)
    assert code == fire_schedule.EXIT_OK
    assert summary["fired"] is None
    assert len(world.store.list_firings()) == 1
    assert [event.action for event in world.audit.events] == ["schedules.fire"]


def test_an_unsubstituted_placeholder_is_a_manual_firing(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    schedule = world.schedule(ScheduleKind.DRIFT_CHECK)
    code, summary = run(
        world,
        capsys,
        "--schedule-id",
        schedule.schedule_id,
        "--scheduled-time",
        "<aws.scheduler.scheduled-time>",
    )
    assert code == fire_schedule.EXIT_OK
    assert summary["fired"]["trigger"] == "manual"


@pytest.mark.parametrize(
    ("argv", "code"),
    [
        ((), fire_schedule.EXIT_CONFIG),
        (("--schedule-id", "sch_x", "--scheduled-time", "next tuesday"), fire_schedule.EXIT_CONFIG),
        (("--schedule-id", "sch_nothing"), fire_schedule.EXIT_NOT_FOUND),
    ],
)
def test_arguments_and_unknown_schedules_are_refused(
    world: World, capsys: pytest.CaptureFixture[str], argv: tuple[str, ...], code: int
) -> None:
    assert run(world, capsys, *argv)[0] == code


def test_the_sweep_records_old_misses_and_leaves_a_slot_eventbridge_is_about_to_fire(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    late = world.schedule(ScheduleKind.DRIFT_CHECK, schedule_id="sch_late", cron="0 * * * *", timezone="UTC")
    both = world.schedule(ScheduleKind.DRIFT_CHECK, schedule_id="sch_both", cron="30 * * * *", timezone="UTC")
    assert (late.next_due_at, both.next_due_at) == (T0 + timedelta(hours=1), T0 + timedelta(minutes=30))
    # 01:40: sch_late's 01:00 is 40 minutes old; sch_both's 00:30 is 70 minutes old, its 01:30 only 10.
    world.clock.now = T0 + timedelta(hours=1, minutes=40)
    code, summary = run(world, capsys, "--sweep")
    assert code == fire_schedule.EXIT_OK
    assert summary["fired"] is None
    missed = {(f.schedule_id, f.scheduled_for) for f in world.store.list_firings(status=FiringStatus.MISSED)}
    assert missed == {("sch_late", T0 + timedelta(hours=1)), ("sch_both", T0 + timedelta(minutes=30))}
    catch_ups = [f.schedule_id for f in world.store.list_firings() if f.trigger.value == "catch_up"]
    assert catch_ups == ["sch_late"], "sch_both's 01:30 invocation is its catch-up"
    assert world.store.get("sch_both").next_due_at == T0 + timedelta(hours=1, minutes=30)
    # ... and when EventBridge's invocation for 01:30 arrives, it still fires.
    code, summary = run(
        world, capsys, "--schedule-id", "sch_both", "--scheduled-time", "2026-09-01T01:30:00Z"
    )
    assert summary["fired"]["status"] == "succeeded"
    assert world.store.get("sch_both").next_due_at == T0 + timedelta(hours=2, minutes=30)


def test_the_task_waits_for_its_own_run_and_reports_the_failure(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    """A real thread runner and the real score flow: the test champion was never fitted, so predict
    fails - and the task must still wait, settle the firing as failed, alert, and exit 1."""
    register_champion(world, training_frame(world))
    schedule = world.schedule(ScheduleKind.SCORE)
    jobs = ThreadJobRunner(max_workers=1)
    code, summary = run(world, capsys, "--schedule-id", schedule.schedule_id, jobs=jobs)
    assert code == fire_schedule.EXIT_FAILED
    assert summary["fired"]["status"] == "failed"
    assert summary["fired"]["error_code"]
    assert summary["fired"]["run_id"]
    (alert,) = world.alerts.store.query(AlertQuery(kind=AlertKind.SCHEDULED_JOB_FAILED))
    assert alert.run_id == summary["fired"]["run_id"]
