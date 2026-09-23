"""Schedule operations the API calls, and `monitoring.retraining` made live (DEC-765, DEC-767)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from engine.access.roles import SYSTEM_SCHEDULER
from engine.config import Retraining
from engine.platform_db import sqlite_engine
from engine.scheduling.retraining import (
    MANAGED_BY_RETRAINING,
    CallableRetrainFlags,
    RetrainingTarget,
    managed_schedule_id,
    retraining_targets,
    sync_retraining_schedules,
)
from engine.scheduling.schedules import (
    CadencePreset,
    Schedule,
    ScheduleError,
    ScheduleKind,
    ScheduleParameters,
    SqlScheduleStore,
)
from engine.scheduling.service import ERROR_STATUS, create_schedule, delete_schedule, update_schedule
from tests.unit.production.scheduling_support import USE_CASE, World, make_world

T0 = datetime(2026, 9, 1, tzinfo=UTC)


class RecordingScheduler:
    """A `Scheduler` that records what it was told, and can be made to fail."""

    backend = "recording"

    def __init__(self, *, fail: bool = False) -> None:
        self.synced: list[Schedule] = []
        self.removed: list[str] = []
        self.fail = fail

    def sync(self, schedule: Schedule) -> None:
        if self.fail:
            raise RuntimeError("EventBridge said no")
        self.synced.append(schedule)

    def remove(self, schedule_id: str) -> None:
        if self.fail:
            raise RuntimeError("EventBridge said no")
        self.removed.append(schedule_id)

    def start(self) -> None:
        """Nothing to start."""

    def stop(self) -> None:
        """Nothing to stop."""


@pytest.fixture
def store(tmp_path: Path) -> SqlScheduleStore:
    return SqlScheduleStore(sqlite_engine(tmp_path / "platform.db"))


def create(
    store: SqlScheduleStore, scheduler: RecordingScheduler, config_root: Path, **fields: object
) -> Schedule:
    values: dict[str, object] = {
        "config_root": config_root,
        "client_id": "c_demo_1",
        "use_case_id": USE_CASE,
        "kind": ScheduleKind.DRIFT_CHECK,
        "cadence": "weekly",
        "created_by": "u_analyst",
        "now": T0,
    }
    values.update(fields)
    return create_schedule(store, scheduler, **values)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# create / update / delete
# ---------------------------------------------------------------------------
def test_create_stores_computes_the_next_slot_and_registers(
    store: SqlScheduleStore, config_root: Path
) -> None:
    scheduler = RecordingScheduler()
    made = create(store, scheduler, config_root)
    assert made.preset is CadencePreset.WEEKLY
    assert made.cron == "0 2 * * 1"
    assert made.timezone == "Asia/Kolkata"
    # The first Monday 02:00 IST after 1 Sep 2026 (a Tuesday) is 7 Sep, i.e. 6 Sep 20:30 UTC.
    assert made.next_due_at == datetime(2026, 9, 6, 20, 30, tzinfo=UTC)
    assert store.get(made.schedule_id) == made
    assert scheduler.synced == [made]


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        ({"use_case_id": "no-such-use-case"}, "USE_CASE_NOT_FOUND"),
        ({"cadence": "every tuesday"}, "CRON_INVALID"),
        ({"cadence": "0 2 1 * 1"}, "CRON_NOT_PORTABLE"),
        ({"timezone": "Mars/Base"}, "TIMEZONE_UNKNOWN"),
        ({"kind": ScheduleKind.SCORE}, "SCHEDULE_INVALID"),
    ],
)
def test_create_refusals_carry_codes_the_api_maps(
    store: SqlScheduleStore, config_root: Path, fields: dict[str, object], code: str
) -> None:
    with pytest.raises(ScheduleError) as caught:
        create(store, RecordingScheduler(), config_root, **fields)
    assert caught.value.code == code
    assert ERROR_STATUS[code] in (404, 422)
    assert store.list() == ()


def test_a_recipe_must_belong_to_the_client_and_the_use_case(tmp_path: Path, config_root: Path) -> None:
    world: World = make_world(tmp_path, config_root)
    scheduler = RecordingScheduler()
    good = create(
        world.store,
        scheduler,
        config_root,
        client_id=world.client_id,
        kind=ScheduleKind.SCORE,
        parameters=ScheduleParameters(onboarding_spec_id=world.spec.spec_id),
        client_store=world.client_store,
    )
    assert good.parameters.onboarding_spec_id == world.spec.spec_id
    with pytest.raises(ScheduleError) as caught:
        create(
            world.store,
            scheduler,
            config_root,
            client_id="c_someone_else",
            kind=ScheduleKind.SCORE,
            parameters=ScheduleParameters(onboarding_spec_id=world.spec.spec_id),
            client_store=world.client_store,
        )
    assert caught.value.code == "ONBOARDING_SPEC_NOT_FOUND"


def test_a_create_the_scheduler_refuses_leaves_no_row(store: SqlScheduleStore, config_root: Path) -> None:
    with pytest.raises(ScheduleError) as caught:
        create(store, RecordingScheduler(fail=True), config_root)
    assert caught.value.code == "SCHEDULER_SYNC_FAILED"
    assert store.list() == ()


def test_update_recomputes_the_next_slot_and_disabling_clears_it(
    store: SqlScheduleStore, config_root: Path
) -> None:
    scheduler = RecordingScheduler()
    made = create(store, scheduler, config_root)
    changed = update_schedule(
        store, scheduler, made.schedule_id, cadence="30 6 * * *", timezone="UTC", now=T0
    )
    assert (changed.cron, changed.preset, changed.timezone) == ("30 6 * * *", None, "UTC")
    assert changed.next_due_at == datetime(2026, 9, 1, 6, 30, tzinfo=UTC)
    paused = update_schedule(store, scheduler, made.schedule_id, enabled=False, now=T0)
    assert paused.next_due_at is None
    resumed = update_schedule(
        store, scheduler, made.schedule_id, enabled=True, now=datetime(2026, 9, 3, tzinfo=UTC)
    )
    assert resumed.next_due_at == datetime(2026, 9, 3, 6, 30, tzinfo=UTC), "no backlog from the paused days"
    assert [item.enabled for item in scheduler.synced] == [True, True, False, True]


def test_delete_removes_the_registration_first(store: SqlScheduleStore, config_root: Path) -> None:
    made = create(store, RecordingScheduler(), config_root)
    with pytest.raises(ScheduleError):
        delete_schedule(store, RecordingScheduler(fail=True), made.schedule_id)
    assert store.get(made.schedule_id) == made, "kept, so deleting again retries"
    scheduler = RecordingScheduler()
    delete_schedule(store, scheduler, made.schedule_id)
    assert scheduler.removed == [made.schedule_id]
    assert store.list() == ()


# ---------------------------------------------------------------------------
# monitoring.retraining (DEC-767)
# ---------------------------------------------------------------------------
def target(setting: Retraining, client_id: str | None = "c_demo_1") -> RetrainingTarget:
    return RetrainingTarget(client_id=client_id, use_case_id=USE_CASE, retraining=setting)


def managed(store: SqlScheduleStore) -> list[tuple[str, str | None]]:
    return sorted(
        (schedule.kind.value, schedule.preset.value if schedule.preset else None)
        for schedule in store.list(managed_by=MANAGED_BY_RETRAINING)
    )


def test_weekly_and_monthly_keep_one_retrain_schedule_in_step(store: SqlScheduleStore) -> None:
    scheduler = RecordingScheduler()
    result = sync_retraining_schedules(store, scheduler, [target(Retraining.WEEKLY)], now=T0)
    assert managed(store) == [("retrain", "weekly")]
    (made,) = store.list()
    assert made.schedule_id == managed_schedule_id(ScheduleKind.RETRAIN, "c_demo_1", USE_CASE)
    assert made.created_by == SYSTEM_SCHEDULER.user_id
    assert result.created == (made.schedule_id,)
    again = sync_retraining_schedules(store, scheduler, [target(Retraining.WEEKLY)], now=T0)
    assert (again.created, again.updated, again.removed) == ((), (), ()), "idempotent"
    monthly = sync_retraining_schedules(store, scheduler, [target(Retraining.MONTHLY)], now=T0)
    assert monthly.updated == (made.schedule_id,)
    assert managed(store) == [("retrain", "monthly")]
    assert store.get(made.schedule_id).next_due_at == datetime(2026, 9, 30, 20, 30, tzinfo=UTC)


def test_on_drift_keeps_a_drift_check_and_manual_removes_everything_managed(store: SqlScheduleStore) -> None:
    scheduler = RecordingScheduler()
    sync_retraining_schedules(store, scheduler, [target(Retraining.WEEKLY)], now=T0)
    result = sync_retraining_schedules(store, scheduler, [target(Retraining.ON_DRIFT)], now=T0)
    assert managed(store) == [("drift_check", "weekly")]
    assert len(result.removed) == 1 and len(result.created) == 1
    manual = sync_retraining_schedules(store, scheduler, [target(Retraining.MANUAL)], now=T0)
    assert managed(store) == []
    assert len(manual.removed) == 1
    assert set(scheduler.removed) == set(result.removed) | set(manual.removed)


def test_a_persons_own_schedules_are_never_touched(store: SqlScheduleStore, config_root: Path) -> None:
    mine = create(store, RecordingScheduler(), config_root, kind=ScheduleKind.RETRAIN)
    sync_retraining_schedules(store, RecordingScheduler(), [target(Retraining.MANUAL)], now=T0)
    assert store.get(mine.schedule_id) == mine


def test_managed_schedules_can_be_paused_but_not_redefined_or_deleted(store: SqlScheduleStore) -> None:
    scheduler = RecordingScheduler()
    sync_retraining_schedules(store, scheduler, [target(Retraining.WEEKLY)], now=T0)
    (made,) = store.list()
    assert update_schedule(store, scheduler, made.schedule_id, enabled=False, now=T0).enabled is False
    for attempt in (
        lambda: update_schedule(store, scheduler, made.schedule_id, cadence="daily"),
        lambda: delete_schedule(store, scheduler, made.schedule_id),
    ):
        with pytest.raises(ScheduleError) as caught:
            attempt()
        assert caught.value.code == "SCHEDULE_MANAGED"


def test_each_client_gets_its_own_managed_schedule(store: SqlScheduleStore) -> None:
    targets = [
        target(Retraining.WEEKLY, "c_a_1"),
        target(Retraining.WEEKLY, "c_b_1"),
        target(Retraining.WEEKLY, None),
    ]
    sync_retraining_schedules(store, RecordingScheduler(), targets, now=T0)
    assert sorted(schedule.client_id or "" for schedule in store.list()) == ["", "c_a_1", "c_b_1"]


def test_targets_are_every_client_with_a_training_recipe(tmp_path: Path, config_root: Path) -> None:
    world = make_world(tmp_path, config_root)
    world.client_store.create_client("No Recipe Co", "telecom")
    assert retraining_targets(world.client_store, config_root) == (
        RetrainingTarget(world.client_id, USE_CASE, Retraining.ON_DRIFT),
    )
    assert retraining_targets(None, config_root) == ()


def test_callable_flags_adapt_two_functions() -> None:
    cleared: list[str] = []
    flags = CallableRetrainFlags(list_flagged=lambda: ("m_a_1",), clear_one=lambda m: cleared.append(m) or 1)
    assert flags.flagged() == ("m_a_1",)
    assert flags.clear("m_a_1") == 1
    assert cleared == ["m_a_1"]


def test_the_privacy_adapter_reads_and_clears_m48s_flags(tmp_path: Path) -> None:
    """The wiring to M48's `engine.privacy.erasure` functions, on a real `model_retrain_flag` table."""
    from sqlmodel import Session

    from engine.privacy.tables import ModelRetrainFlagRow, create_privacy_tables
    from engine.scheduling.retraining import privacy_retrain_flags

    engine = sqlite_engine(tmp_path / "platform.db")
    flags = privacy_retrain_flags(engine)
    assert flags.flagged() == ()
    create_privacy_tables(engine)
    with Session(engine) as session:
        session.add(
            ModelRetrainFlagRow(model_id=f"m_{USE_CASE}_1", request_id="er_1", reason="r", created_at=T0)
        )
        session.commit()
    assert flags.flagged() == (f"m_{USE_CASE}_1",)
    assert flags.clear(f"m_{USE_CASE}_1") == 1
    assert flags.flagged() == ()
