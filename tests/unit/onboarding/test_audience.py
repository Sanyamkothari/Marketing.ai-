"""The lapsed audience on the spine (v3 phase 0, B5): who has a row at a snapshot, and that it is point in time.

A shopper has a row at a snapshot only with an event in the audience's table on or before it and none in the
`no_event_days` days up to and including it. Only events the point-in-time clause admits are read, so an event after
the snapshot never keeps or drops a row.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import duckdb
import pandas as pd
import pytest

from engine.onboarding.audience import apply_audience, compile_audience_query
from engine.onboarding.features import POINT_IN_TIME_MARKER, SNAPSHOT_VIEW
from engine.onboarding.specs import AudienceSpec, FeatureSpec, OnboardingSpec, SnapshotDefinition, recipe_hash

DAYS = 90


def spine(*keys: str, snapshots: tuple[str, ...] = ("2011-08-31",)) -> pd.DataFrame:
    rows = [(key, pd.Timestamp(day)) for key in keys for day in snapshots]
    return pd.DataFrame(rows, columns=["entity_key", "snapshot_date"])


def events(rows: dict[str, list[str]]) -> pd.DataFrame:
    flat = [(key, pd.Timestamp(day)) for key, days in rows.items() for day in days]
    return pd.DataFrame(flat, columns=["entity_key", "event_time"])


def kept(frame: pd.DataFrame, snapshots: pd.DataFrame, *, inclusive: bool = True) -> list[tuple[str, str]]:
    con = duckdb.connect()
    try:
        con.register("orders", frame)
        out = apply_audience(con, snapshots, "orders", DAYS, inclusive=inclusive)
    finally:
        con.close()
    return [(row.entity_key, row.snapshot_date.strftime("%Y-%m-%d")) for row in out.itertuples(index=False)]


def test_a_shopper_with_an_order_and_none_in_the_last_days_is_in() -> None:
    log = events({"lapsed": ["2011-03-01", "2011-05-01"], "active": ["2011-03-01", "2011-08-01"]})
    assert kept(log, spine("lapsed", "active")) == [("lapsed", "2011-08-31")]


def test_the_boundary_is_the_whole_days_before_the_snapshot() -> None:
    # 31 Aug less 90 days is 2 Jun 00:00: an order at that instant is in the lapsed set, a day later is not.
    log = events({"on_the_line": ["2011-06-02"], "a_day_late": ["2011-06-03"]})
    assert kept(log, spine("on_the_line", "a_day_late")) == [("on_the_line", "2011-08-31")]


def test_a_shopper_with_no_earlier_order_is_not_lapsed_they_have_not_started() -> None:
    log = events({"first_today": ["2011-08-31"], "never": []})
    assert kept(log, spine("first_today", "never")) == []


def test_the_rows_keep_their_order_and_the_audience_is_decided_per_snapshot() -> None:
    log = events({"a": ["2011-01-10"], "b": ["2011-01-10", "2011-05-15"], "c": ["2011-06-20"]})
    snapshots = spine("a", "b", "c", snapshots=("2011-04-30", "2011-08-31"))
    # 30 Apr: `a` and `b` last ordered on 10 Jan (110 days), `c` has not started. 31 Aug: `b` last ordered on
    # 15 May (108 days), `c` on 20 Jun (72 days: not lapsed).
    assert kept(log, snapshots) == [
        ("a", "2011-04-30"),
        ("a", "2011-08-31"),
        ("b", "2011-04-30"),
        ("b", "2011-08-31"),
    ]


def test_an_event_after_the_snapshot_never_changes_a_row() -> None:
    """Point in time: the same snapshot with and without later events keeps the same rows."""
    base = {"lapsed": ["2011-03-01"], "active": ["2011-08-20"], "quiet": ["2011-02-01"]}
    later = {
        "lapsed": ["2011-09-15", "2011-10-01"],  # would make them "active" if the future leaked
        "active": [],
        "quiet": ["2011-12-01"],
    }
    snapshots = spine("lapsed", "active", "quiet")
    without = kept(events(base), snapshots)
    with_future = kept(events({key: base[key] + later[key] for key in base}), snapshots)
    assert without == with_future == [("lapsed", "2011-08-31"), ("quiet", "2011-08-31")]


def test_a_shopper_whose_only_orders_are_in_the_future_is_not_in() -> None:
    assert kept(events({"late": ["2011-09-20", "2011-10-20"]}), spine("late")) == []


def test_the_inclusive_flag_moves_no_row() -> None:
    """It only puts an event stamped at the snapshot's own instant on one side of the clause; such an event is
    inside the last 90 days either way."""
    log = events({"x": ["2011-03-01"], "y": ["2011-08-31"], "z": ["2011-06-02"]})
    snapshots = spine("x", "y", "z")
    assert kept(log, snapshots, inclusive=True) == kept(log, snapshots, inclusive=False)


def test_the_view_is_left_registered_for_the_audience_only() -> None:
    con = duckdb.connect()
    try:
        con.register("orders", events({"a": ["2011-01-01"], "b": ["2011-08-30"]}))
        out = apply_audience(con, spine("a", "b"), "orders", DAYS, inclusive=True)
        left = con.execute(f'SELECT entity_key FROM "{SNAPSHOT_VIEW}"').df()
    finally:
        con.close()
    assert list(out["entity_key"]) == ["a"] and list(left["entity_key"]) == ["a"]


def test_the_query_carries_the_point_in_time_guard() -> None:
    sql = compile_audience_query("orders", DAYS, inclusive=True)
    assert (
        POINT_IN_TIME_MARKER in sql and "HAVING MAX(e.event_time) <= s.snapshot_date - INTERVAL 90 DAY" in sql
    )


@pytest.mark.parametrize("role", ["orders; DROP TABLE x", "o r", '"orders"', ""])
def test_a_role_must_be_a_plain_name(role: str) -> None:
    with pytest.raises(ValueError, match="plain name"):
        compile_audience_query(role, DAYS, inclusive=True)


def test_the_days_are_a_whole_number_of_at_least_one() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        compile_audience_query("orders", 0, inclusive=True)


def _spec(audience: AudienceSpec | None) -> OnboardingSpec:
    return OnboardingSpec(
        spec_id="spec_1",
        client_id="cl_1",
        use_case="retail-win-back",
        entity_source_id="src_1",
        mapping_ids=("map_1",),
        feature_spec=FeatureSpec(features=()),
        label_spec=None,
        snapshot_spec=SnapshotDefinition(),
        audience=audience,
        created_at=datetime(2026, 10, 26, tzinfo=UTC),
    )


def test_an_unset_audience_leaves_the_document_alone_and_a_set_one_is_in_the_recipe_hash() -> None:
    plain = _spec(None)
    assert "audience" not in json.loads(plain.model_dump_json())
    ruled = _spec(AudienceSpec(role="orders", no_event_days=90))
    written = json.loads(ruled.model_dump_json())["audience"]
    assert (written["role"], written["no_event_days"]) == ("orders", 90)
    assert recipe_hash(plain, ()) != recipe_hash(ruled, ())
    assert recipe_hash(ruled, ()) != recipe_hash(_spec(AudienceSpec(role="orders", no_event_days=60)), ())
