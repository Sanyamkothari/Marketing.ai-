"""Labels and censoring.

Every expected number below was worked out by hand from the fixture at the top of the file and
written down before the code ran; nothing here re-derives an expectation from the thing it is
testing. The payment history is small enough to check with a pencil:

    A  15th of every month, Jan-Jun, 200 each
    B  10 Jan only, 50                          - stops paying immediately
    C  20 Jan 50, 20 Mar 150, 20 May 50
    D  5th of every month, Jan-Jun, 80 each
    E  25 Feb 90, 25 Apr 90, 25 Jun 300         - the last event in the table
    F  8 Jan 100, 8 Feb 50, 8 Mar 100, 8 Apr 50 - stops after April
    G  never pays at all

The extract therefore ends on 25 Jun 2024, and a 60-day outcome window is complete only for a
snapshot date on or before 26 Apr 2024.
"""

from __future__ import annotations

import datetime

import duckdb
import pandas as pd
import pytest

from engine.onboarding.labels import LabelError, build_labels, compile_label_query
from engine.onboarding.specs import LabelSpec, LabelType, SnapshotMode

PAYMENTS: tuple[tuple[str, str, float], ...] = (
    ("A", "2024-01-15", 200.0),
    ("A", "2024-02-15", 200.0),
    ("A", "2024-03-15", 200.0),
    ("A", "2024-04-15", 200.0),
    ("A", "2024-05-15", 200.0),
    ("A", "2024-06-15", 200.0),
    ("B", "2024-01-10", 50.0),
    ("C", "2024-01-20", 50.0),
    ("C", "2024-03-20", 150.0),
    ("C", "2024-05-20", 50.0),
    ("D", "2024-01-05", 80.0),
    ("D", "2024-02-05", 80.0),
    ("D", "2024-03-05", 80.0),
    ("D", "2024-04-05", 80.0),
    ("D", "2024-05-05", 80.0),
    ("D", "2024-06-05", 80.0),
    ("E", "2024-02-25", 90.0),
    ("E", "2024-04-25", 90.0),
    ("E", "2024-06-25", 300.0),
    ("F", "2024-01-08", 100.0),
    ("F", "2024-02-08", 50.0),
    ("F", "2024-03-08", 100.0),
    ("F", "2024-04-08", 50.0),
)
ENTITIES = ("A", "B", "C", "D", "E", "F", "G")
SNAPSHOT_DATES = ("2024-01-31", "2024-02-29", "2024-03-31", "2024-04-30", "2024-05-31")

CHURN = LabelSpec(name="churn", type=LabelType.EVENT_ABSENCE, role="payments", horizon_days=60)


def connection(payments=PAYMENTS, role="payments"):
    frame = pd.DataFrame(list(payments), columns=["entity_key", "event_time", "amount"])
    frame["event_time"] = pd.to_datetime(frame["event_time"])
    con = duckdb.connect()
    con.register(role, frame)
    return con


def snapshot_frame(dates=SNAPSHOT_DATES, entities=ENTITIES):
    return pd.DataFrame(
        [(entity, pd.Timestamp(date)) for date in dates for entity in entities],
        columns=["entity_key", "snapshot_date"],
    )


def labels_at(result, date):
    """The label value per entity at one snapshot date, as a plain dict."""
    rows = result.frame[result.frame["snapshot_date"] == pd.Timestamp(date)]
    return dict(zip(rows["entity_key"], rows[result.frame.columns[-1]], strict=True))


def build(spec, con=None, snapshots=None, **overrides):
    settings = {
        "drop_censored": True,
        "mapped_roles": {"payments"},
        "mode": SnapshotMode.PERIODIC,
        "inclusive": True,
    }
    settings.update(overrides)
    return build_labels(
        con or connection(),
        spec,
        snapshot_frame() if snapshots is None else snapshots,
        **settings,
    )


def stat_for(result, date):
    return next(stat for stat in result.per_snapshot if stat.date == datetime.date.fromisoformat(date))


def codes(result):
    return [check.code for check in result.checks]


# ---------------------------------------------------------------------------
# The four label types
# ---------------------------------------------------------------------------
def test_event_absence_is_positive_for_the_entities_with_no_payment_in_the_window() -> None:
    """Churn at 31 Jan: the window is 1 Feb - 31 Mar, where only B and G pay nothing."""
    assert labels_at(build(CHURN), "2024-01-31") == {
        "A": 0,
        "B": 1,
        "C": 0,
        "D": 0,
        "E": 0,
        "F": 0,
        "G": 1,
    }


def test_event_presence_is_the_other_half_of_the_same_window() -> None:
    """The same 1 Feb - 31 Mar window, so exactly the five entities churn does not name."""
    spec = LabelSpec(name="paid_60d", type=LabelType.EVENT_PRESENCE, role="payments", horizon_days=60)
    assert labels_at(build(spec), "2024-01-31") == {
        "A": 1,
        "B": 0,
        "C": 1,
        "D": 1,
        "E": 1,
        "F": 1,
        "G": 0,
    }


def test_value_threshold_over_any_event_needs_one_payment_under_a_hundred() -> None:
    """1 Feb - 31 Mar: D pays 80, E pays 90, F pays 50 on 8 Feb; A, C pay more and B, G not at all."""
    spec = LabelSpec(
        name="small_payment",
        type=LabelType.VALUE_THRESHOLD,
        role="payments",
        horizon_days=60,
        expression="amount < 100",
        any_event=True,
    )
    assert labels_at(build(spec), "2024-01-31") == {
        "A": 0,
        "B": 0,
        "C": 0,
        "D": 1,
        "E": 1,
        "F": 1,
        "G": 0,
    }


def test_value_threshold_over_all_events_is_null_when_there_were_no_events() -> None:
    """F's 8 Mar payment of 100 breaks its run, and "every payment" says nothing about B and G.

    The vacuous truth is the trap: B and G paid nothing at all in the window, and answering 1
    because zero out of zero payments were small would mark every dormant customer a positive.
    """
    spec = LabelSpec(
        name="all_payments_small",
        type=LabelType.VALUE_THRESHOLD,
        role="payments",
        horizon_days=60,
        expression="amount < 100",
        any_event=False,
    )
    values = labels_at(build(spec), "2024-01-31")
    assert values["D"] == 1
    assert values["E"] == 1
    assert values["A"] == 0
    assert values["C"] == 0
    assert values["F"] == 0
    assert pd.isna(values["B"])
    assert pd.isna(values["G"])


def test_a_column_label_reads_the_entity_table_straight_through() -> None:
    entity = pd.DataFrame({"entity_key": ["A", "B", "C"], "cancelled": [0, 1, 0]})
    con = duckdb.connect()
    con.register("entity", entity)
    spec = LabelSpec(name="cancelled", type=LabelType.COLUMN, column="cancelled")

    result = build(
        spec,
        con=con,
        snapshots=snapshot_frame(dates=("2024-06-30",), entities=("A", "B", "C")),
        mapped_roles={"entity"},
        mode=SnapshotMode.SINGLE,
    )

    assert labels_at(result, "2024-06-30") == {"A": 0, "B": 1, "C": 0}
    assert stat_for(result, "2024-06-30").positive_rate == pytest.approx(1 / 3, abs=1e-4)
    assert not result.checks


# ---------------------------------------------------------------------------
# Censoring: the bug that makes an event_absence model look excellent and be worthless
# ---------------------------------------------------------------------------
def test_censored_snapshots_are_dropped_and_do_not_inflate_the_last_kept_positive_rate() -> None:
    """The extract ends 25 Jun, so a 60-day window is only complete up to 26 Apr.

    30 Apr and 31 May are therefore unobservable, and both read high: at 30 Apr F looks churned
    because its 8 Apr payment was its last one *in the file*, and at 31 May C joins it. Keeping
    them would take the churn rate from 2 in 7 to 4 in 7 purely because the data stops.
    """
    result = build(CHURN)

    kept = sorted(result.frame["snapshot_date"].dt.date.unique())
    assert kept == [datetime.date(2024, 1, 31), datetime.date(2024, 2, 29), datetime.date(2024, 3, 31)]

    assert [stat.censored for stat in result.per_snapshot] == [False, False, False, True, True]
    assert stat_for(result, "2024-03-31").positive_rate == pytest.approx(2 / 7, abs=1e-4)
    assert stat_for(result, "2024-04-30").positive_rate == pytest.approx(3 / 7, abs=1e-4)
    assert stat_for(result, "2024-05-31").positive_rate == pytest.approx(4 / 7, abs=1e-4)

    censored = [check for check in result.checks if check.code == "LABEL_HORIZON_CENSORED"]
    assert len(censored) == 1
    assert censored[0].message == (
        "2 of 5 snapshot dates were dropped because the outcome window is not complete yet."
    )
    assert censored[0].details["data_end"] == "2024-06-25"


def test_keeping_censored_snapshots_is_what_the_drop_flag_turns_off() -> None:
    """Without the drop, every snapshot survives - and the last one reads 4 in 7 instead of 2."""
    result = build(CHURN, drop_censored=False)

    assert len(result.frame) == len(ENTITIES) * len(SNAPSHOT_DATES)
    assert "LABEL_HORIZON_CENSORED" not in codes(result)
    assert [stat.censored for stat in result.per_snapshot] == [False, False, False, True, True]


# ---------------------------------------------------------------------------
# Point in time, from the other side: only the future window may move a label
# ---------------------------------------------------------------------------
def test_events_at_or_before_a_snapshot_date_never_change_its_label() -> None:
    """G's payment falls on exactly 31 Jan, which belongs to the past half of that boundary."""
    before = (
        *PAYMENTS,
        *((entity, "2023-12-01", 10.0) for entity in ENTITIES),
        ("G", "2024-01-31", 10.0),
    )

    baseline = labels_at(build(CHURN), "2024-01-31")
    assert labels_at(build(CHURN, con=connection(before)), "2024-01-31") == baseline


def test_an_event_at_the_snapshot_instant_moves_into_the_window_when_it_is_not_inclusive() -> None:
    """The mirror of the test above: the boundary belongs to one half or the other, never neither."""
    payments = (*PAYMENTS, ("G", "2024-01-31", 10.0))

    result = build(CHURN, con=connection(payments), inclusive=False)

    assert labels_at(result, "2024-01-31")["G"] == 0


def test_an_event_inside_the_horizon_does_change_the_label() -> None:
    """B pays once on 15 Mar: inside the windows opening 31 Jan and 29 Feb, outside the 31 Mar one."""
    payments = (*PAYMENTS, ("B", "2024-03-15", 70.0))
    result = build(CHURN, con=connection(payments))

    assert labels_at(result, "2024-01-31")["B"] == 0
    assert labels_at(result, "2024-02-29")["B"] == 0
    assert labels_at(result, "2024-03-31")["B"] == 1


def test_a_filter_on_the_event_narrows_which_events_count() -> None:
    """Churn against payments over 100 only: D's 80s and F's 50 stop counting as activity."""
    spec = LabelSpec(
        name="churn_large",
        type=LabelType.EVENT_ABSENCE,
        role="payments",
        horizon_days=60,
        where={"column": "amount", "op": "gt", "value": 100},
    )
    assert labels_at(build(spec), "2024-01-31") == {
        "A": 0,
        "B": 1,
        "C": 0,
        "D": 1,
        "E": 1,
        "F": 1,
        "G": 1,
    }


# ---------------------------------------------------------------------------
# The refusals
# ---------------------------------------------------------------------------
def test_a_column_label_is_refused_under_periodic_snapshots() -> None:
    """One stored flag cannot say what the outcome was at each of several past dates."""
    entity = pd.DataFrame({"entity_key": list(ENTITIES), "cancelled": [0, 1, 0, 0, 1, 0, 0]})
    con = duckdb.connect()
    con.register("entity", entity)
    spec = LabelSpec(name="cancelled", type=LabelType.COLUMN, column="cancelled")

    result = build(spec, con=con, mapped_roles={"entity"}, mode=SnapshotMode.PERIODIC)

    assert codes(result) == ["ENTITY_ATTRIBUTES_NOT_TIME_VERSIONED"]
    assert result.checks[0].severity == "error"
    assert result.frame.empty
    assert result.per_snapshot == ()


def test_a_label_whose_role_is_not_mapped_is_an_error_not_a_column_of_zeroes() -> None:
    spec = LabelSpec(name="complained", type=LabelType.EVENT_PRESENCE, role="complaints", horizon_days=30)

    result = build(spec, mapped_roles={"payments"})

    assert codes(result) == ["LABEL_ROLE_MISSING"]
    assert result.checks[0].severity == "error"
    assert "complaints" in result.checks[0].message
    assert result.frame.empty


def test_a_degenerate_snapshot_is_reported_and_dropped() -> None:
    """At 1 Jan both entities pay within 10 days; at 3 Jan only X does."""
    con = connection((("X", "2024-01-05", 10.0), ("X", "2024-01-15", 10.0), ("Y", "2024-01-02", 10.0)))
    spec = LabelSpec(name="paid_10d", type=LabelType.EVENT_PRESENCE, role="payments", horizon_days=10)

    result = build(
        spec, con=con, snapshots=snapshot_frame(dates=("2024-01-01", "2024-01-03"), entities=("X", "Y"))
    )

    assert codes(result) == ["LABEL_DEGENERATE_SNAPSHOT"]
    assert result.checks[0].message.startswith("Every one of the 2 rows at 2024-01-01")
    assert stat_for(result, "2024-01-01").dropped_reason == "every row has the same outcome"
    assert stat_for(result, "2024-01-03").dropped_reason is None
    assert sorted(result.frame["snapshot_date"].dt.date.unique()) == [datetime.date(2024, 1, 3)]


def test_an_expression_is_whitelisted_rather_than_pasted_into_the_query() -> None:
    """A value_threshold expression is the one place a user's text decides an outcome."""
    for expression in ("__import__('os').system('ls')", "amount.__class__", "1; DROP TABLE payments"):
        spec = LabelSpec(
            name="threshold",
            type=LabelType.VALUE_THRESHOLD,
            role="payments",
            horizon_days=30,
            expression=expression,
        )
        with pytest.raises(LabelError) as raised:
            compile_label_query(spec, inclusive=True)
        assert raised.value.code.startswith("LABEL_EXPRESSION_")
