"""The schedule contract, its store, the one-claim-per-slot rule and the due-slot policy (DEC-763/764)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from engine.platform_db import sqlite_engine
from engine.scheduling.schedules import (
    MAX_MISSED_RECORDED,
    PRESET_CRON,
    CadencePreset,
    FiringStatus,
    FiringTrigger,
    Schedule,
    ScheduleError,
    ScheduleFiring,
    ScheduleKind,
    ScheduleParameters,
    SqlScheduleStore,
    cadence_cron,
    default_grace,
    due_slots,
    new_firing_id,
    new_schedule_id,
)

T0 = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
GRACE = timedelta(minutes=3)


def schedule(**fields: object) -> Schedule:
    values: dict[str, object] = {
        "schedule_id": "sch_test",
        "client_id": "c_demo_1",
        "use_case_id": "telco-churn",
        "kind": ScheduleKind.DRIFT_CHECK,
        "cron": "0 * * * *",
        "timezone": "UTC",
        "created_by": "u_test",
        "created_at": T0,
        "updated_at": T0,
        "next_due_at": T0 + timedelta(hours=1),
    }
    values.update(fields)
    return Schedule.model_validate(values)


@pytest.fixture
def store(tmp_path: Path) -> SqlScheduleStore:
    return SqlScheduleStore(sqlite_engine(tmp_path / "platform.db"))


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------
def test_presets_are_stored_as_the_cron_line_they_mean() -> None:
    assert cadence_cron("weekly") == (PRESET_CRON[CadencePreset.WEEKLY], CadencePreset.WEEKLY)
    assert cadence_cron(" Monthly ") == ("0 2 1 * *", CadencePreset.MONTHLY)
    assert cadence_cron("30 6 * * 1-5") == ("30 6 * * 1-5", None)


def test_the_default_timezone_is_kolkata() -> None:
    made = Schedule(
        schedule_id="sch_x",
        client_id=None,
        use_case_id="telco-churn",
        kind=ScheduleKind.RETRAIN,
        cron="0 2 * * *",
        created_by="u",
        created_at=T0,
        updated_at=T0,
    )
    assert made.timezone == "Asia/Kolkata"
    assert made.next_slot_after(T0) == datetime(2026, 9, 1, 20, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    ("fields", "fragment"),
    [
        ({"cron": "0 2 1 * 1"}, "not both"),
        ({"timezone": "Nowhere/Land"}, "timezone"),
        ({"kind": ScheduleKind.SCORE}, "needs an onboarding_spec_id or a dataset_id"),
        ({"parameters": ScheduleParameters(model_version_id="m_x_1")}, "only a score schedule"),
        (
            {
                "kind": ScheduleKind.SCORE,
                "parameters": ScheduleParameters(onboarding_spec_id="a", dataset_id="b"),
            },
            "not both",
        ),
    ],
)
def test_a_schedule_that_cannot_work_is_refused(fields: dict[str, object], fragment: str) -> None:
    with pytest.raises(ValidationError, match=fragment):
        schedule(**fields)


def test_ids_are_valid_eventbridge_names() -> None:
    import re

    for made in (new_schedule_id(), new_firing_id(T0)):
        assert re.fullmatch(r"[0-9A-Za-z_.-]{1,64}", made)


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------
def test_create_get_list_save_delete(store: SqlScheduleStore) -> None:
    made = store.create(schedule())
    assert store.get("sch_test") == made
    assert store.list(use_case_id="telco-churn") == (made,)
    assert store.list(client_id="c_other") == ()
    changed = store.save(made.model_copy(update={"enabled": False}))
    assert store.get("sch_test").enabled is False
    assert changed.enabled is False
    store.delete("sch_test")
    with pytest.raises(ScheduleError) as caught:
        store.get("sch_test")
    assert caught.value.code == "SCHEDULE_NOT_FOUND"


def test_a_duplicate_id_is_refused(store: SqlScheduleStore) -> None:
    store.create(schedule())
    with pytest.raises(ScheduleError) as caught:
        store.create(schedule())
    assert caught.value.code == "SCHEDULE_EXISTS"


def test_timestamps_come_back_aware_utc(store: SqlScheduleStore) -> None:
    store.create(schedule(next_due_at=datetime(2026, 9, 1, 5, 30, tzinfo=UTC)))
    back = store.get("sch_test")
    assert back.next_due_at == datetime(2026, 9, 1, 5, 30, tzinfo=UTC)
    assert back.created_at.tzinfo is not None


def firing(slot: datetime | None, **fields: object) -> ScheduleFiring:
    values: dict[str, object] = {
        "firing_id": new_firing_id(T0),
        "schedule_id": "sch_test",
        "client_id": "c_demo_1",
        "use_case_id": "telco-churn",
        "kind": ScheduleKind.DRIFT_CHECK,
        "trigger": FiringTrigger.SCHEDULED,
        "status": FiringStatus.RUNNING,
        "scheduled_for": slot,
        "fired_at": T0,
    }
    values.update(fields)
    return ScheduleFiring.model_validate(values)


def test_one_slot_is_claimed_once(store: SqlScheduleStore) -> None:
    """DEC-763: the unique slot index is what stops an EventBridge retry or a second replica."""
    slot = T0 + timedelta(hours=1)
    assert store.claim_firing(firing(slot)) is not None
    assert store.claim_firing(firing(slot)) is None
    assert store.claim_firing(firing(slot + timedelta(hours=1))) is not None


def test_manual_firings_have_no_slot_and_never_collide(store: SqlScheduleStore) -> None:
    for _ in range(3):
        assert store.claim_firing(firing(None, trigger=FiringTrigger.MANUAL)) is not None
    assert len(store.list_firings(schedule_id="sch_test")) == 3


def test_firings_list_newest_first_and_filter_by_status(store: SqlScheduleStore) -> None:
    first = store.claim_firing(firing(None, fired_at=T0))
    second = store.claim_firing(firing(None, fired_at=T0 + timedelta(minutes=1), status=FiringStatus.MISSED))
    assert first is not None and second is not None
    assert [item.firing_id for item in store.list_firings()] == [second.firing_id, first.firing_id]
    assert store.list_firings(status=FiringStatus.MISSED) == (second,)
    done = store.save_firing(
        first.model_copy(update={"status": FiringStatus.SUCCEEDED, "flagged_models": ("m_a_1",)})
    )
    assert store.get_firing(first.firing_id) == done


# ---------------------------------------------------------------------------
# The due-slot policy (DEC-764)
# ---------------------------------------------------------------------------
def test_nothing_is_due_before_the_slot() -> None:
    decision = due_slots(schedule(), T0 + timedelta(minutes=59), grace=GRACE)
    assert not decision.anything
    assert decision.next_due_at == T0 + timedelta(hours=1)


def test_the_due_slot_fires_on_time_within_the_grace() -> None:
    decision = due_slots(schedule(), T0 + timedelta(hours=1, minutes=2), grace=GRACE)
    assert decision.on_time == T0 + timedelta(hours=1)
    assert decision.missed == ()
    assert decision.catch_up is False
    assert decision.next_due_at == T0 + timedelta(hours=2)


def test_a_slot_older_than_the_grace_is_missed_and_caught_up_once() -> None:
    decision = due_slots(schedule(), T0 + timedelta(hours=1, minutes=10), grace=GRACE)
    assert decision.on_time is None
    assert decision.missed == (T0 + timedelta(hours=1),)
    assert decision.catch_up is True


def test_several_passed_slots_are_all_missed_with_one_catch_up() -> None:
    decision = due_slots(schedule(), T0 + timedelta(hours=4, minutes=1), grace=GRACE)
    assert decision.missed == tuple(T0 + timedelta(hours=hour) for hour in (1, 2, 3, 4))
    assert decision.on_time is None
    assert decision.catch_up is True
    assert decision.next_due_at == T0 + timedelta(hours=5)


def test_a_week_offline_records_a_bounded_number_of_misses() -> None:
    decision = due_slots(schedule(), T0 + timedelta(days=7), grace=GRACE)
    assert len(decision.missed) == MAX_MISSED_RECORDED
    assert decision.missed_truncated is True


def test_a_disabled_schedule_is_never_due() -> None:
    decision = due_slots(schedule(enabled=False), T0 + timedelta(days=2), grace=GRACE)
    assert not decision.anything


def test_the_default_grace_is_two_ticks_and_a_minute() -> None:
    assert default_grace(60) == timedelta(minutes=3)
