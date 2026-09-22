"""M12, the aggregation engine: the compiled SQL, the point-in-time guard, and the numbers.

The golden fixture below is small enough that every expected value in `_EXPECTED` was worked out on
paper from the event rows and written here as a literal. That is the point: expectations generated
by the code under test would agree with any bug the code has, and a leaking feature builder is
exactly the kind of bug that still produces numbers that look right.

`test_future_events_never_move_a_feature` is the suite's centre of gravity. It builds, appends
events dated after every snapshot, rebuilds and demands an identical frame - and then appends one
event *inside* a window and demands that the value moves, so the test cannot pass by the builder
being broken in the other direction.
"""

from __future__ import annotations

import duckdb
import numpy as np
import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from engine.config import load_use_case
from engine.onboarding.features import (
    POINT_IN_TIME_MARKER,
    FeatureError,
    assert_point_in_time,
    build_features,
    compile_feature_sql,
    compile_role_query,
    render_sql_file,
    suggested_features,
)
from engine.onboarding.specs import (
    AggFunction,
    FeatureDef,
    FeatureSpec,
    SubAggregation,
    WhereClause,
    WhereOp,
)

S1 = "2024-03-31"
S2 = "2024-06-30"

# (entity, event_time, category, resolved_time, severity)
_COMPLAINTS = [
    ("e01", "2023-09-15", "network", "2023-09-20", 1.0),
    ("e01", "2024-01-01", "billing", None, 2.0),
    ("e01", "2024-02-10", "billing", "2024-02-12", 3.0),
    ("e01", "2024-03-31", "network", None, 4.0),
    ("e01", "2024-05-20", "service", "2024-05-25", 5.0),
    ("e01", "2024-06-30", "billing", None, 6.0),
    ("e02", "2023-03-31", "billing", "2023-04-05", 9.0),
    # e03 never complains, so its row must survive the build as nulls rather than disappear.
]
# (entity, event_time, amount, status)
_BILLS = [
    ("e01", "2023-08-15", 50.0, "unpaid"),
    ("e01", "2024-01-15", 100.0, "paid"),
    ("e01", "2024-02-15", 200.0, "paid"),
    ("e01", "2024-03-15", 300.0, "unpaid"),
    ("e01", "2024-04-15", 400.0, "paid"),
    ("e01", "2024-05-15", 500.0, "paid"),
    ("e01", "2024-06-15", 600.0, "paid"),
    ("e02", "2023-03-31", 77.0, "paid"),
]
# (entity, signup_date)
_ENTITIES = [("e01", "2023-03-31"), ("e02", "2022-06-30"), ("e03", "2024-01-31")]

_FILLER = [f"e{index:02d}" for index in range(4, 11)]
_FILLER_COMPLAINT_DATES = ["2023-11-10", "2024-01-10", "2024-02-20", "2024-04-20", "2024-06-10"]
_FILLER_BILL_DATES = [
    "2023-12-20",
    "2024-01-20",
    "2024-02-20",
    "2024-03-20",
    "2024-04-20",
    "2024-05-20",
    "2024-06-20",
]

_FEATURES = (
    FeatureDef(name="complaints_all", role="complaints", function=AggFunction.COUNT),
    FeatureDef(name="complaints_30d", role="complaints", function=AggFunction.COUNT, window_days=30),
    FeatureDef(name="complaints_90d", role="complaints", function=AggFunction.COUNT, window_days=90),
    FeatureDef(name="complaints_180d", role="complaints", function=AggFunction.COUNT, window_days=180),
    FeatureDef(
        name="complaints_open_all",
        role="complaints",
        function=AggFunction.COUNT,
        where=WhereClause(column="resolved_time", op=WhereOp.IS_NULL),
    ),
    FeatureDef(
        name="complaint_categories", role="complaints", function=AggFunction.NUNIQUE, column="category"
    ),
    FeatureDef(
        name="latest_complaint_category",
        role="complaints",
        function=AggFunction.LATEST,
        column="category",
    ),
    FeatureDef(
        name="first_complaint_category", role="complaints", function=AggFunction.FIRST, column="category"
    ),
    FeatureDef(name="days_since_last_complaint", role="complaints", function=AggFunction.DAYS_SINCE_LAST),
    FeatureDef(name="days_since_first_complaint", role="complaints", function=AggFunction.DAYS_SINCE_FIRST),
    FeatureDef(name="has_complained", role="complaints", function=AggFunction.EXISTS),
    FeatureDef(name="bill_sum_all", role="bills", function=AggFunction.SUM, column="amount"),
    FeatureDef(
        name="bill_mean_90d", role="bills", function=AggFunction.MEAN, column="amount", window_days=90
    ),
    FeatureDef(name="bill_min_90d", role="bills", function=AggFunction.MIN, column="amount", window_days=90),
    FeatureDef(name="bill_max_90d", role="bills", function=AggFunction.MAX, column="amount", window_days=90),
    FeatureDef(name="bill_std_90d", role="bills", function=AggFunction.STD, column="amount", window_days=90),
    FeatureDef(
        name="unpaid_bills_all",
        role="bills",
        function=AggFunction.COUNT,
        where=WhereClause(column="status", op=WhereOp.EQ, value="unpaid"),
    ),
    FeatureDef(
        name="bill_trend_30d_vs_90d",
        role="bills",
        function=AggFunction.RATIO,
        of=SubAggregation(function=AggFunction.MEAN, column="amount", window_days=30),
        over=SubAggregation(function=AggFunction.MEAN, column="amount", window_days=90),
    ),
    FeatureDef(
        name="bill_share_180d",
        role="bills",
        function=AggFunction.RATIO,
        of=SubAggregation(function=AggFunction.COUNT, window_days=180),
        over=SubAggregation(function=AggFunction.COUNT),
    ),
    FeatureDef(
        name="tenure_months",
        role="entity",
        function=AggFunction.DERIVE,
        expression="months_between(snapshot_date, signup_date)",
    ),
)
SPEC = FeatureSpec(features=_FEATURES)

# Worked out by hand from the rows above. At 2024-03-31 the windows open after 2024-03-01 (30d),
# 2024-01-01 (90d) and 2023-10-03 (180d); at 2024-06-30 after 2024-05-31, 2024-04-01 and 2024-01-02.
_EXPECTED: dict[tuple[str, str], dict[str, object]] = {
    ("e01", S1): {
        "complaints_all": 4,
        "complaints_30d": 1,  # 2024-03-31 only
        "complaints_90d": 2,  # 2024-01-01 sits exactly on the edge and is excluded
        "complaints_180d": 3,
        "complaints_open_all": 2,
        "complaint_categories": 2,  # network, billing
        "latest_complaint_category": "network",
        "first_complaint_category": "network",
        "days_since_last_complaint": 0,  # complained on the snapshot date itself
        "days_since_first_complaint": 198,
        "has_complained": True,
        "bill_sum_all": 650.0,  # 50 + 100 + 200 + 300
        "bill_mean_90d": 200.0,
        "bill_min_90d": 100.0,
        "bill_max_90d": 300.0,
        "bill_std_90d": 100.0,
        "unpaid_bills_all": 2,
        "bill_trend_30d_vs_90d": 1.5,  # 300 / 200
        "bill_share_180d": 0.75,  # 3 of the 4 bills so far
        "tenure_months": 12.0,
    },
    ("e01", S2): {
        "complaints_all": 6,
        "complaints_30d": 1,
        "complaints_90d": 2,
        "complaints_180d": 4,
        "complaints_open_all": 3,
        "complaint_categories": 3,
        "latest_complaint_category": "billing",
        "first_complaint_category": "network",
        "days_since_last_complaint": 0,
        "days_since_first_complaint": 289,  # 198 + the 91 days from 2024-03-31
        "has_complained": True,
        "bill_sum_all": 2150.0,
        "bill_mean_90d": 500.0,  # 400, 500, 600
        "bill_min_90d": 400.0,
        "bill_max_90d": 600.0,
        "bill_std_90d": 100.0,
        "unpaid_bills_all": 2,
        "bill_trend_30d_vs_90d": 1.2,  # 600 / 500
        "bill_share_180d": pytest.approx(6 / 7),
        "tenure_months": 15.0,
    },
    ("e02", S1): {
        "complaints_all": 1,
        "complaints_30d": 0,
        "complaints_90d": 0,
        "complaints_180d": 0,
        "complaints_open_all": 0,
        "complaint_categories": 1,
        "latest_complaint_category": "billing",
        "first_complaint_category": "billing",
        "days_since_last_complaint": 366,  # 2023-03-31, across a leap day
        "days_since_first_complaint": 366,
        "has_complained": True,
        "bill_sum_all": 77.0,
        "bill_mean_90d": None,  # nothing in the window: never a fabricated 0
        "bill_min_90d": None,
        "bill_max_90d": None,
        "bill_std_90d": None,
        "unpaid_bills_all": 0,
        "bill_trend_30d_vs_90d": None,
        "bill_share_180d": 0.0,  # 0 of 1 is a measured zero, unlike e03's null
        "tenure_months": 21.0,
    },
    ("e02", S2): {
        "complaints_all": 1,
        "complaints_30d": 0,
        "complaints_90d": 0,
        "complaints_180d": 0,
        "complaints_open_all": 0,
        "complaint_categories": 1,
        "latest_complaint_category": "billing",
        "first_complaint_category": "billing",
        "days_since_last_complaint": 457,  # 366 + 91
        "days_since_first_complaint": 457,
        "has_complained": True,
        "bill_sum_all": 77.0,
        "bill_mean_90d": None,
        "bill_min_90d": None,
        "bill_max_90d": None,
        "bill_std_90d": None,
        "unpaid_bills_all": 0,
        "bill_trend_30d_vs_90d": None,
        "bill_share_180d": 0.0,
        "tenure_months": 24.0,
    },
    ("e03", S1): {
        "complaints_all": 0,
        "complaints_30d": 0,
        "complaints_90d": 0,
        "complaints_180d": 0,
        "complaints_open_all": 0,
        "complaint_categories": 0,
        "latest_complaint_category": None,
        "first_complaint_category": None,
        "days_since_last_complaint": None,
        "days_since_first_complaint": None,
        "has_complained": False,
        "bill_sum_all": None,
        "bill_mean_90d": None,
        "bill_min_90d": None,
        "bill_max_90d": None,
        "bill_std_90d": None,
        "unpaid_bills_all": 0,
        "bill_trend_30d_vs_90d": None,
        "bill_share_180d": None,  # 0 events over 0 events is null, not 0 and not an error
        "tenure_months": 2.0,
    },
    ("e03", S2): {
        "complaints_all": 0,
        "complaints_30d": 0,
        "complaints_90d": 0,
        "complaints_180d": 0,
        "complaints_open_all": 0,
        "complaint_categories": 0,
        "latest_complaint_category": None,
        "first_complaint_category": None,
        "days_since_last_complaint": None,
        "days_since_first_complaint": None,
        "has_complained": False,
        "bill_sum_all": None,
        "bill_mean_90d": None,
        "bill_min_90d": None,
        "bill_max_90d": None,
        "bill_std_90d": None,
        "unpaid_bills_all": 0,
        "bill_trend_30d_vs_90d": None,
        "bill_share_180d": None,
        "tenure_months": 5.0,
    },
}


def _rows(values: list[tuple[object, ...]]) -> str:
    def cell(value: object) -> str:
        if value is None:
            return "NULL"
        if isinstance(value, str):
            return f"'{value}'"
        return repr(value)

    return ", ".join("(" + ", ".join(cell(v) for v in row) + ")" for row in values)


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    """The golden fixture: 10 entities, ~100 events and two snapshot dates, in explicit DATE columns."""
    connection = duckdb.connect()
    connection.execute("CREATE TABLE snapshots(entity_key VARCHAR, snapshot_date DATE)")
    entities = [name for name, _ in _ENTITIES] + _FILLER
    connection.execute(
        "INSERT INTO snapshots VALUES " + _rows([(entity, date) for date in (S1, S2) for entity in entities])
    )
    connection.execute(
        "CREATE TABLE complaints(entity_key VARCHAR, event_time DATE, category VARCHAR, "
        "resolved_time DATE, severity DOUBLE)"
    )
    filler_complaints = [
        (entity, date, "network", date, 1.0) for entity in _FILLER for date in _FILLER_COMPLAINT_DATES
    ]
    connection.execute("INSERT INTO complaints VALUES " + _rows([*_COMPLAINTS, *filler_complaints]))
    connection.execute(
        "CREATE TABLE bills(entity_key VARCHAR, event_time DATE, amount DOUBLE, status VARCHAR)"
    )
    filler_bills = [(entity, date, 10.0, "paid") for entity in _FILLER for date in _FILLER_BILL_DATES]
    connection.execute("INSERT INTO bills VALUES " + _rows([*_BILLS, *filler_bills]))
    connection.execute("CREATE TABLE entity(entity_key VARCHAR, signup_date DATE)")
    connection.execute(
        "INSERT INTO entity VALUES " + _rows([*_ENTITIES, *((entity, "2023-01-31") for entity in _FILLER)])
    )
    yield connection
    connection.close()


def _cell(value: object) -> object:
    """One built value as plain Python, with every flavour of missing normalised to None."""
    if isinstance(value, str):
        return value
    if pd.isna(value):
        return None
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    return float(value)


def _built(connection: duckdb.DuckDBPyConnection, spec: FeatureSpec = SPEC) -> pd.DataFrame:
    frame = build_features(connection, spec, inclusive=True)
    frame = frame.assign(snapshot_date=frame["snapshot_date"].dt.strftime("%Y-%m-%d"))
    return frame.set_index(["entity_key", "snapshot_date"]).sort_index()


# ---------------------------------------------------------------------------
# 1. Golden values
# ---------------------------------------------------------------------------
def test_every_function_and_window_against_hand_computed_values(con) -> None:
    built = _built(con)
    assert len(built) == 20  # 10 entities x 2 snapshots: every entity keeps a row at every date
    for key, expected in _EXPECTED.items():
        assert set(expected) == set(SPEC.names), f"{key} does not cover every feature"
        for name, want in expected.items():
            assert _cell(built.loc[key, name]) == want, f"{name} at {key}"


# ---------------------------------------------------------------------------
# 2. The point-in-time guard, in both directions
# ---------------------------------------------------------------------------
def test_future_events_never_move_a_feature_but_past_ones_do(con) -> None:
    before = _built(con)

    con.execute("INSERT INTO complaints VALUES " + _rows([("e01", "2024-07-15", "billing", None, 7.0)]))
    con.execute("INSERT INTO bills VALUES " + _rows([("e02", "2024-12-01", 999.0, "unpaid")]))
    assert_frame_equal(before, _built(con))

    # The same test would pass if the builder simply returned constants, so now move a value that
    # must move: an event before the second snapshot and inside its 30-day window.
    con.execute("INSERT INTO complaints VALUES " + _rows([("e03", "2024-06-20", "service", None, 1.0)]))
    after = _built(con)
    assert _cell(after.loc[("e03", S2), "complaints_30d"]) == 1
    assert _cell(after.loc[("e03", S2), "has_complained"]) is True
    assert _cell(after.loc[("e03", S1), "complaints_30d"]) == 0  # still after the first snapshot


# ---------------------------------------------------------------------------
# 3. Every compiled query carries the guard, and the assertion is not vacuous
# ---------------------------------------------------------------------------
def test_every_compiled_query_carries_the_guard(con) -> None:
    queries = compile_feature_sql(SPEC, roles={"complaints", "bills", "entity"}, inclusive=True)
    assert set(queries) == {"complaints", "bills"}  # the derived entity feature is not SQL
    for sql in queries.values():
        assert POINT_IN_TIME_MARKER in sql
        assert_point_in_time(sql)

    stripped = queries["bills"].replace(f" AND e.event_time <= s.snapshot_date  {POINT_IN_TIME_MARKER}", "")
    with pytest.raises(FeatureError) as raised:
        assert_point_in_time(stripped)
    assert raised.value.code == "FEATURE_POINT_IN_TIME_GUARD_MISSING"


def test_inclusive_snapshot_time_chooses_the_comparison() -> None:
    features = (FeatureDef(name="complaints_all", role="complaints", function=AggFunction.COUNT),)
    assert "e.event_time <= s.snapshot_date" in compile_role_query("complaints", features, inclusive=True)
    assert "e.event_time < s.snapshot_date" in compile_role_query("complaints", features, inclusive=False)


def test_rendered_sql_file_is_runnable(con) -> None:
    queries = compile_feature_sql(SPEC, roles={"complaints", "bills"}, inclusive=True)
    con.execute(render_sql_file(queries))


# ---------------------------------------------------------------------------
# 4. Ratios and filters
# ---------------------------------------------------------------------------
def test_a_ratio_over_nothing_is_null_rather_than_zero_or_an_error(con) -> None:
    built = _built(con)
    # e03 has no bills at all, so the denominator really is 0 rather than merely small.
    assert _cell(built.loc[("e03", S1), "bill_share_180d"]) is None
    assert _cell(built.loc[("e02", S1), "bill_share_180d"]) == 0.0


@pytest.mark.parametrize(
    ("where", "expected"),
    [
        (WhereClause(column="category", op=WhereOp.EQ, value="billing"), 2),
        (WhereClause(column="category", op=WhereOp.NE, value="billing"), 2),
        (WhereClause(column="severity", op=WhereOp.GT, value=2.0), 2),
        (WhereClause(column="severity", op=WhereOp.GTE, value=2.0), 3),
        (WhereClause(column="severity", op=WhereOp.LT, value=2.0), 1),
        (WhereClause(column="severity", op=WhereOp.LTE, value=2.0), 2),
        (WhereClause(column="category", op=WhereOp.IN, value=["billing", "service"]), 2),
        (WhereClause(column="resolved_time", op=WhereOp.IS_NULL), 2),
        (WhereClause(column="resolved_time", op=WhereOp.NOT_NULL), 2),
    ],
)
def test_each_where_operator_selects_the_right_events(con, where, expected) -> None:
    spec = FeatureSpec(
        features=(FeatureDef(name="matched", role="complaints", function=AggFunction.COUNT, where=where),)
    )
    assert _cell(_built(con, spec).loc[("e01", S1), "matched"]) == expected


# ---------------------------------------------------------------------------
# 5. SQL safety
# ---------------------------------------------------------------------------
def test_an_injected_identifier_is_refused_and_an_injected_value_is_inert(con) -> None:
    injected = FeatureDef(
        name="injected",
        role="complaints",
        function=AggFunction.SUM,
        column='severity" FROM complaints; DROP TABLE snapshots; --',
    )
    with pytest.raises(FeatureError) as raised:
        compile_role_query("complaints", (injected,), inclusive=True)
    assert raised.value.code == "FEATURE_UNSAFE_IDENTIFIER"

    with pytest.raises(FeatureError):
        compile_role_query('complaints"; DROP TABLE snapshots; --', (injected,), inclusive=True)

    quoted = FeatureSpec(
        features=(
            FeatureDef(
                name="matched",
                role="complaints",
                function=AggFunction.COUNT,
                where=WhereClause(column="category", op=WhereOp.EQ, value="x'; DROP TABLE snapshots; --"),
            ),
        )
    )
    assert _cell(_built(con, quoted).loc[("e01", S1), "matched"]) == 0
    assert con.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 20


def test_a_derived_feature_is_not_compiled_into_a_role_query() -> None:
    derived = FeatureDef(
        name="tenure_months",
        role="entity",
        function=AggFunction.DERIVE,
        expression="months_between(snapshot_date, signup_date)",
    )
    with pytest.raises(FeatureError) as raised:
        compile_role_query("entity", (derived,), inclusive=True)
    assert raised.value.code == "FEATURE_DERIVE_NOT_SQL"


# ---------------------------------------------------------------------------
# suggested_features
# ---------------------------------------------------------------------------
_MAPPED = {
    "complaints": ["entity_key", "event_time"],
    "bills": ["entity_key", "event_time", "amount"],
}


def test_features_for_roles_the_client_did_not_map_are_left_out() -> None:
    names = {f.name for f in suggested_features(load_use_case("telco-churn"), _MAPPED)}
    assert "complaints_90d" in names  # role complaints, which was mapped
    assert "usage_mb_30d" not in names  # role usage, which was not
    assert "days_since_last_activity" not in names


def test_a_use_case_feature_wins_over_a_generated_one_of_the_same_name() -> None:
    config = load_use_case("telco-churn")
    named = FeatureDef(
        name="bills_mean_amount_90d",
        role="bills",
        function=AggFunction.MEAN,
        column="amount",
        window_days=90,
        description="The use case's own wording.",
    )
    config = config.model_copy(update={"suggested_features": (named,)})
    chosen = [f for f in suggested_features(config, _MAPPED) if f.name == "bills_mean_amount_90d"]
    assert [f.description for f in chosen] == ["The use case's own wording."]


def test_the_library_skips_functions_needing_a_column_the_role_lacks() -> None:
    features = suggested_features(load_use_case("telco-churn"), _MAPPED)
    generated = {f.name for f in features if f.role == "complaints"}
    windows = load_use_case("telco-churn").onboarding.features.library.windows_days
    assert {f"complaints_count_{w}d" for w in windows} <= generated
    assert "complaints_days_since_last" in generated  # generated once, not once per window
    assert not [f for f in features if f.role == "complaints" and f.column is not None]
    assert "bills_mean_amount_90d" in {f.name for f in features}


def test_two_builds_of_the_same_data_agree_only_to_a_tolerance() -> None:
    """Why the leak probe may not compare floats exactly.

    DuckDB aggregates in parallel, so the order it sums a column in is not fixed between runs. Two
    builds of identical, untouched tables therefore return floats that differ in their last bits -
    measured here, not assumed. `engine.onboarding.build._moved` compares floats to a tolerance
    because of this; an exact comparison made every float feature look like it had moved as soon as
    a build was large enough for DuckDB to use a second thread, and FUTURE_EVENTS_LEAKED is the one
    finding a user may never acknowledge, so large builds became impossible.

    If this test ever starts passing with `equals`, the tolerance in `_moved` can go - but only
    then, and only with a measurement like this one to show it.
    """
    import duckdb
    import numpy as np

    from engine.onboarding.build import _moved

    rng = np.random.default_rng(7)
    entities, events = 4_000, 400_000
    snapshots = pd.DataFrame(
        {
            "entity_key": np.repeat([f"E{i}" for i in range(entities)], 2),
            "snapshot_date": pd.to_datetime(np.tile(["2026-01-31", "2026-02-28"], entities)),
        }
    )
    bills = pd.DataFrame(
        {
            "entity_key": [f"E{i}" for i in rng.integers(0, entities, events)],
            "event_time": pd.to_datetime("2025-06-01")
            + pd.to_timedelta(rng.integers(0, 330, events), unit="D"),
            "amount": rng.normal(500.0, 180.0, events),
        }
    )
    spec = FeatureSpec(
        features=(
            FeatureDef(name="avg_bill", role="bills", function="mean", column="amount", window_days=180),
            FeatureDef(name="bills_180d", role="bills", function="count", window_days=180),
        )
    )
    con = duckdb.connect()
    try:
        con.register("snapshots", snapshots)
        con.register("bills", bills)
        first = build_features(con, spec, inclusive=True)
        second = build_features(con, spec, inclusive=True)
    finally:
        con.close()

    # The count is exact on both runs; only the float moves, and only in its last bits.
    assert first["bills_180d"].equals(second["bills_180d"])
    assert not _moved(first["avg_bill"], second["avg_bill"])
    assert not _moved(first["bills_180d"], second["bills_180d"])
    # And a real change is still a change: one extra event in a window moves the count by a whole 1.
    bumped = second["bills_180d"] + 1
    assert _moved(first["bills_180d"], bumped)
    assert _moved(first["avg_bill"], second["avg_bill"] * 1.0001)
