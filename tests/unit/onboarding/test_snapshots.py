"""Snapshot dates.

Every expected date below was written out by hand from the calendar before the code ran; nothing
here re-derives an expectation from the thing it is testing. The two years used throughout were
picked because 2023 and 2024 put a 28-day and a 29-day February next to each other, and because
2024-01-01 + 90 days is 2024-03-31 and 2024-12-31 - 60 days is 2024-11-01, both checkable by hand.
"""

from __future__ import annotations

import datetime

import pandas as pd

from engine.contracts import Severity
from engine.onboarding.snapshots import build_snapshot_frame, plan_snapshots, scoring_snapshot
from engine.onboarding.specs import SnapshotFrequency, SnapshotMode, SnapshotSpec

JAN_2023 = datetime.date(2023, 1, 1)
JAN_2024 = datetime.date(2024, 1, 1)
DEC_2024 = datetime.date(2024, 12, 31)

MONTH_ENDS = (
    "2023-01-31",
    "2023-02-28",
    "2023-03-31",
    "2023-04-30",
    "2023-05-31",
    "2023-06-30",
    "2023-07-31",
    "2023-08-31",
    "2023-09-30",
    "2023-10-31",
    "2023-11-30",
    "2023-12-31",
    "2024-01-31",
    "2024-02-29",
    "2024-03-31",
    "2024-04-30",
    "2024-05-31",
    "2024-06-30",
    "2024-07-31",
    "2024-08-31",
    "2024-09-30",
    "2024-10-31",
    "2024-11-30",
    "2024-12-31",
)


def periodic(min_history_days=0, **overrides) -> SnapshotSpec:
    return SnapshotSpec(mode=SnapshotMode.PERIODIC, min_history_days=min_history_days, **overrides)


def iso(dates) -> list[str]:
    return [day.isoformat() for day in dates]


def test_month_ends_are_the_last_day_of_each_month_across_a_leap_and_a_non_leap_february():
    plan = plan_snapshots(
        periodic(max_snapshots=24), first_event=JAN_2023, last_event=DEC_2024, horizon_days=0
    )

    assert iso(plan.dates) == list(MONTH_ENDS)
    assert plan.checks == ()


def test_the_default_range_leaves_room_for_the_history_and_the_outcome_window():
    plan = plan_snapshots(
        periodic(min_history_days=90), first_event=JAN_2024, last_event=DEC_2024, horizon_days=60
    )

    assert iso(plan.dates) == [
        "2024-03-31",
        "2024-04-30",
        "2024-05-31",
        "2024-06-30",
        "2024-07-31",
        "2024-08-31",
        "2024-09-30",
        "2024-10-31",
    ]


def test_weekly_snapshots_are_seven_days_apart_and_anchored_on_the_end_date():
    plan = plan_snapshots(
        periodic(
            frequency=SnapshotFrequency.WEEKLY,
            start=datetime.date(2024, 3, 1),
            end=datetime.date(2024, 4, 10),
        ),
        first_event=JAN_2024,
        last_event=DEC_2024,
        horizon_days=0,
    )

    assert iso(plan.dates) == [
        "2024-03-06",
        "2024-03-13",
        "2024-03-20",
        "2024-03-27",
        "2024-04-03",
        "2024-04-10",
    ]


def test_the_cap_keeps_the_most_recent_snapshots():
    plan = plan_snapshots(
        periodic(max_snapshots=3), first_event=JAN_2023, last_event=DEC_2024, horizon_days=0
    )

    assert iso(plan.dates) == ["2024-10-31", "2024-11-30", "2024-12-31"]


def test_a_span_shorter_than_the_history_plus_the_outcome_window_is_an_error():
    plan = plan_snapshots(
        periodic(min_history_days=90),
        first_event=JAN_2024,
        last_event=datetime.date(2024, 4, 10),
        horizon_days=60,
    )

    assert plan.dates == ()
    (check,) = plan.checks
    assert check.code == "TOO_LITTLE_HISTORY"
    assert check.severity is Severity.ERROR
    assert "at least 150 days of history" in check.message
    assert check.details["span_days"] == 100


def test_a_requested_date_outside_the_data_is_an_error_and_is_still_reported_back():
    pick = datetime.date(2025, 6, 1)
    plan = plan_snapshots(
        SnapshotSpec(mode=SnapshotMode.SINGLE, min_history_days=0, end=pick),
        first_event=JAN_2024,
        last_event=DEC_2024,
        horizon_days=0,
    )

    assert plan.dates == (pick,)
    (check,) = plan.checks
    assert check.code == "SNAPSHOT_OUTSIDE_DATA_RANGE"
    assert check.severity is Severity.ERROR
    assert "2025-06-01" in check.message


def test_a_single_snapshot_defaults_to_the_last_event():
    plan = plan_snapshots(
        SnapshotSpec(mode=SnapshotMode.SINGLE, min_history_days=0),
        first_event=JAN_2024,
        last_event=DEC_2024,
        horizon_days=0,
    )

    assert plan.dates == (DEC_2024,)
    assert plan.checks == ()


def test_scoring_stands_at_the_end_of_the_new_data_whatever_the_training_spec_said():
    training = periodic(frequency=SnapshotFrequency.WEEKLY, max_snapshots=24)

    assert scoring_snapshot(training, last_event=DEC_2024) == (DEC_2024,)
    pick = datetime.date(2024, 11, 15)
    assert scoring_snapshot(training.model_copy(update={"end": pick}), last_event=DEC_2024) == (pick,)


def test_an_entity_appears_only_at_snapshots_on_or_after_its_signup():
    entities = pd.DataFrame(
        {
            "entity_key": ["A", "B", "C"],
            "signup_date": pd.to_datetime(["2024-01-15", "2024-03-20", None]),
            "region": ["north", "south", "east"],
        }
    )
    dates = (datetime.date(2024, 1, 31), datetime.date(2024, 2, 29), datetime.date(2024, 3, 31))

    frame = build_snapshot_frame(entities, dates, signup_column="signup_date")

    assert list(frame.columns) == ["entity_key", "snapshot_date"]
    assert list(zip(frame["entity_key"], iso(frame["snapshot_date"].dt.date), strict=True)) == [
        ("A", "2024-01-31"),
        ("A", "2024-02-29"),
        ("A", "2024-03-31"),
        ("B", "2024-03-31"),
    ]


def test_without_a_signup_column_every_entity_appears_at_every_snapshot():
    entities = pd.DataFrame({"entity_key": ["A", "B", "C"]})
    dates = (datetime.date(2024, 1, 31), datetime.date(2024, 2, 29))

    frame = build_snapshot_frame(entities, dates, signup_column=None)

    assert len(frame) == 6
    assert frame["snapshot_date"].nunique() == 2
