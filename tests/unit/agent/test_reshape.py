"""Level 3 (Plan G M76): many rows per entity become one, point in time, the same way every time."""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from engine.agent.config import AgentLevel
from engine.agent.contracts import RecipeStep, RecipeStepKind
from engine.agent.recipe import RecipeError, check_recipe, run_recipe
from engine.agent.reshape import CombineError, CombineSpec, choose_dates, combine_rows, plan_combine
from engine.config import load_use_case
from engine.stages import ingest
from tests.fixtures.agent_bench.make_multirow import multirow_frame

LEVELS = (AgentLevel.CLEAN, AgentLevel.DERIVE, AgentLevel.RESHAPE)
KEY = "customer_id"
TARGET = "came_back"


def _orders() -> pd.DataFrame:
    """Two customers; C2 has an order after its snapshot, which must never count."""
    return pd.DataFrame(
        {
            KEY: ["C1", "C2", "C1", "C2", "C2", "C1"],
            "snapshot_date": ["2024-03-01"] * 6,
            "order_date": [
                "2024-01-10",
                "2024-02-01",
                "2024-02-20",
                "2024-02-25",
                "2024-04-15",
                "2024-01-01",
            ],
            "amount": [10.0, 20.0, 30.0, 40.0, 999.0, 5.0],
            "channel": ["web", "shop", "app", "web", "app", "web"],
            "refund_date": [None, None, "2024-02-21", None, "2024-04-20", None],
            TARGET: [0, 1, 0, 1, 1, 0],
        }
    )


def _features(*items: tuple[str, str, str | None]) -> list[dict[str, str | None]]:
    return [{"name": name, "function": function, "column": column} for name, function, column in items]


_PARAMS: dict[str, Any] = {
    "time_column": "order_date",
    "snapshot_column": "snapshot_date",
    "dayfirst": False,
    "outcome": TARGET,
    "features": _features(
        ("row_count", "count", None),
        ("order_date_days_since_first", "days_since_first", None),
        ("order_date_days_since_last", "days_since_last", None),
        ("amount_sum", "sum", "amount"),
        ("amount_mean", "mean", "amount"),
        ("amount_max", "max", "amount"),
        ("amount_latest", "latest", "amount"),
        ("channel_latest", "latest", "channel"),
        ("channel_nunique", "nunique", "channel"),
        ("refund_date_days_since_last", "days_since_last", "refund_date"),
    ),
}


def _combine(frame: pd.DataFrame, params: dict[str, Any] | None = None, **kwargs: Any) -> Any:
    return combine_rows(frame, key=KEY, params=params or _PARAMS, max_failure_pct=5.0, **kwargs)


def test_an_event_after_the_snapshot_never_counts() -> None:
    out = _combine(_orders()).frame.set_index(KEY)
    c2 = out.loc["C2"]
    assert c2["row_count"] == 2  # the 2024-04-15 order is after the 2024-03-01 snapshot
    assert c2["amount_sum"] == 60.0
    assert c2["amount_max"] == 40.0
    assert c2["amount_latest"] == 40.0
    assert c2["channel_latest"] == "web"
    assert c2["channel_nunique"] == 2
    assert c2["order_date_days_since_last"] == (pd.Timestamp("2024-03-01") - pd.Timestamp("2024-02-25")).days
    assert pd.isna(c2["refund_date_days_since_last"])  # the only refund date is after the snapshot
    c1 = out.loc["C1"]
    assert (c1["row_count"], c1["amount_sum"], c1["amount_latest"]) == (3, 45.0, 30.0)
    assert c1["order_date_days_since_first"] == (pd.Timestamp("2024-03-01") - pd.Timestamp("2024-01-01")).days
    assert c1["refund_date_days_since_last"] == 9


def test_one_row_per_entity_with_the_snapshot_and_the_outcome() -> None:
    result = _combine(_orders())
    out = result.frame
    assert list(out[KEY]) == ["C1", "C2"]
    assert out[KEY].is_unique
    assert list(out.columns) == [KEY, "snapshot_date", *(f["name"] for f in _PARAMS["features"]), TARGET]
    assert (result.rows_in, result.entities, result.failed) == (6, 2, 0)
    assert out["snapshot_date"].tolist() == [pd.Timestamp("2024-03-01")] * 2
    assert out[TARGET].tolist() == [0, 1]
    assert out[TARGET].dtype == _orders()[TARGET].dtype


def test_the_same_rows_in_any_order_give_the_same_frame() -> None:
    first = _combine(_orders()).frame
    again = _combine(_orders()).frame
    shuffled = _combine(_orders().sample(frac=1.0, random_state=3).reset_index(drop=True)).frame
    pd.testing.assert_frame_equal(first, again)
    pd.testing.assert_frame_equal(first, shuffled)


def test_the_outcome_is_read_from_the_latest_row() -> None:
    frame = pd.DataFrame(
        {
            KEY: ["A", "A", "A", "B", "B"],
            "snap": ["2024-01-31", "2024-02-29", "2024-02-29", "2024-01-31", "2024-02-29"],
            "amount": [1.0, 2.0, 3.0, 4.0, 5.0],
            TARGET: [1, 0, 1, 1, None],
        }
    )
    params = {
        "time_column": "snap",
        "snapshot_column": None,
        "outcome": TARGET,
        "features": _features(("row_count", "count", None), ("amount_sum", "sum", "amount")),
    }
    out = _combine(frame, params).frame.set_index(KEY)
    # A: two rows on its latest date - the later one in the file wins. B: its latest row's outcome is
    # empty, and an earlier row's outcome is about an earlier date, so B's outcome stays empty.
    assert out.loc["A", TARGET] == 1
    assert pd.isna(out.loc["B", TARGET])
    assert out.loc["A", "row_count"] == 3  # one date column: every row is on or before the latest


def test_a_scoring_file_without_the_outcome_is_combined_without_it() -> None:
    out = _combine(_orders().drop(columns=[TARGET])).frame
    assert TARGET not in out.columns
    assert len(out) == 2


def test_rows_without_an_id_or_a_date_are_counted_and_stop_above_the_limit() -> None:
    frame = _orders()
    frame.loc[0, "order_date"] = "someday"
    with pytest.raises(CombineError) as caught:
        _combine(frame)
    assert caught.value.code == "RECIPE_VALUES_UNCONVERTED"
    tolerant = combine_rows(frame, key=KEY, params=_PARAMS, max_failure_pct=50.0)
    assert tolerant.failed == 1
    assert tolerant.frame.set_index(KEY).loc["C1", "row_count"] == 2


def test_a_missing_column_is_named() -> None:
    with pytest.raises(CombineError) as caught:
        _combine(_orders().drop(columns=["amount"]))
    assert (caught.value.code, caught.value.column) == ("RECIPE_COLUMN_MISSING", "amount")


def test_text_where_numbers_are_added_up_stops_the_step() -> None:
    frame = _orders()
    frame["amount"] = ["ten", "twenty", "30", "40", "999", "5"]
    with pytest.raises(CombineError) as caught:
        _combine(frame)
    assert caught.value.code == "RECIPE_VALUES_UNCONVERTED"


def test_the_full_leak_check_runs_and_says_so() -> None:
    result = _combine(_orders(), leak_check="full")
    assert result.leak_check is not None
    assert result.leak_check.startswith("Full future-data check: 2 of 2 combined rows")
    assert _combine(_orders()).leak_check is None


@pytest.mark.parametrize(
    "change",
    [
        {"features": []},
        {"features": _features(("x", "sideways", "amount"))},
        {"features": _features(("x", "sum", None))},
        {"features": _features(("x", "count", "amount"))},
        {"features": _features(("x", "sum", TARGET))},
        {"features": _features(("x", "sum", "amount"), ("x", "max", "amount"))},
        {"dayfirst": "yes"},
        {"time_column": ""},
        {"extra": 1},
    ],
)
def test_bad_parameters_are_refused(change: dict[str, Any]) -> None:
    with pytest.raises(CombineError) as caught:
        CombineSpec.from_params(KEY, {**_PARAMS, **change})
    assert caught.value.code == "RECIPE_STEP_INVALID"


def _profile(frame: pd.DataFrame, use_case: str = "retail-win-back") -> Any:
    return ingest.profile_dataset(
        frame,
        load_use_case(use_case),
        upload_id="u",
        file_name="f.csv",
        file_format="csv",
        file_size_bytes=1,
        delimiter=",",
        encoding="utf-8",
    )


def test_the_plan_chooses_aggregations_by_type() -> None:
    frame = _orders().assign(
        email=[f"p{i}@example.com" for i in range(6)], opt_out=[0, 1, 0, 1, 1, 0], invoice=list("abcdef")
    )
    profile = _profile(frame)
    dates = choose_dates(profile, exclude=[KEY, TARGET], snapshot_hint=None)
    assert dates == ("order_date", "snapshot_date")
    params = plan_combine(
        frame,
        profile,
        key=KEY,
        time_column="order_date",
        snapshot_column="snapshot_date",
        outcome=TARGET,
        carry=["opt_out"],
    )
    made = {(f["name"], f["function"], f["column"]) for f in params["features"]}
    assert ("row_count", "count", None) in made
    assert ("order_date_days_since_last", "days_since_last", None) in made
    assert {("amount_sum", "sum", "amount"), ("amount_latest", "latest", "amount")} <= made
    assert {("channel_latest", "latest", "channel"), ("channel_nunique", "nunique", "channel")} <= made
    assert ("refund_date_days_since_last", "days_since_last", "refund_date") in made
    assert ("opt_out", "latest", "opt_out") in made  # a suppression column keeps its name
    assert not any(f["column"] == "email" for f in params["features"])  # personal data is left out
    assert not any(f["column"] in {KEY, TARGET, "order_date", "snapshot_date"} for f in params["features"])
    names = [f["name"] for f in params["features"]]
    assert len(names) == len(set(names))
    CombineSpec.from_params(KEY, params)  # the plan is a valid step


def test_one_date_column_is_its_own_snapshot() -> None:
    frame = _orders().drop(columns=["snapshot_date", "refund_date"])
    profile = _profile(frame)
    assert choose_dates(profile, exclude=[KEY, TARGET], snapshot_hint=None) == ("order_date", None)
    params = plan_combine(
        frame, profile, key=KEY, time_column="order_date", snapshot_column=None, outcome=TARGET
    )
    assert "order_date_days_since_last" not in {f["name"] for f in params["features"]}  # always 0
    out = _combine(frame, params).frame.set_index(KEY)
    assert out.loc["C2", "row_count"] == 3  # the latest order is the snapshot, so all three count
    assert out.loc["C2", "order_date"] == pd.Timestamp("2024-04-15")
    assert choose_dates(_profile(frame.drop(columns=["order_date"])), exclude=[], snapshot_hint=None) is None


# ---------------------------------------------------------------------------
# Through the recipe engine
# ---------------------------------------------------------------------------
def _combine_step(order: int = 1, **params: Any) -> RecipeStep:
    return RecipeStep(order=order, kind=RecipeStepKind.COMBINE_ROWS, column=KEY, params={**_PARAMS, **params})


def _check(steps: tuple[RecipeStep, ...], **kwargs: Any) -> None:
    options: dict[str, Any] = {
        "columns": list(_orders().columns),
        "primary_key": KEY,
        "target": TARGET,
        "levels": LEVELS,
    }
    options.update(kwargs)
    check_recipe(steps, **options)


def test_the_recipe_runs_parsing_first_then_combines() -> None:
    frame = _orders().assign(amount=["₹10", "₹20", "₹30", "₹40", "₹999", "₹5"])
    steps = (
        RecipeStep(order=1, kind=RecipeStepKind.PARSE_NUMBER, column="amount", params={"decimal": "."}),
        _combine_step(2),
        RecipeStep(order=3, kind=RecipeStepKind.DROP_COLUMN, column="channel_latest"),
    )
    run = run_recipe(
        frame,
        steps,
        upload_id="u1",
        primary_key=KEY,
        target=TARGET,
        levels=LEVELS,
        max_failure_pct=5.0,
        leak_check="full",
    )
    assert run.frame.set_index(KEY).loc["C2", "amount_sum"] == 60.0
    assert "channel_latest" not in run.frame.columns
    assert (run.receipt.rows_in, run.receipt.rows_out) == (6, 2)
    combine = run.receipt.steps[1]
    assert (combine.kind, combine.rows, combine.changed, combine.failed) == (
        RecipeStepKind.COMBINE_ROWS,
        6,
        2,
        0,
    )
    assert combine.leak_check is not None and combine.leak_check.startswith("Full")
    assert run.receipt.steps[0].leak_check is None


def test_combining_needs_the_reshape_level() -> None:
    with pytest.raises(RecipeError) as caught:
        _check((_combine_step(),), levels=(AgentLevel.CLEAN, AgentLevel.DERIVE))
    assert caught.value.code == "RECIPE_STEP_INVALID"
    _check((_combine_step(),))


@pytest.mark.parametrize(
    ("steps", "code"),
    [
        ((_combine_step(1), _combine_step(2)), "RECIPE_STEP_INVALID"),  # only once
        (
            (
                _combine_step(1),
                RecipeStep(order=2, kind=RecipeStepKind.PARSE_NUMBER, column="amount_sum", params={}),
            ),
            "RECIPE_STEP_INVALID",  # parsing comes before combining
        ),
        ((_combine_step(outcome="amount"),), "RECIPE_STEP_INVALID"),  # the outcome is the target only
        ((_combine_step(features=_features(("gone", "sum", "discount"))),), "RECIPE_COLUMN_MISSING"),
    ],
)
def test_unsafe_combines_are_refused(steps: tuple[RecipeStep, ...], code: str) -> None:
    with pytest.raises(RecipeError) as caught:
        _check(steps)
    assert caught.value.code == code


def test_a_combine_keyed_on_another_column_is_refused() -> None:
    step = RecipeStep(order=1, kind=RecipeStepKind.COMBINE_ROWS, column="channel", params=_PARAMS)
    with pytest.raises(RecipeError) as caught:
        _check((step,))
    assert caught.value.code == "RECIPE_STEP_INVALID"


def test_after_combining_the_combined_columns_are_the_ones_available() -> None:
    drop_old = RecipeStep(order=2, kind=RecipeStepKind.DROP_COLUMN, column="amount")
    with pytest.raises(RecipeError) as caught:
        _check((_combine_step(1), drop_old))
    assert caught.value.code == "RECIPE_COLUMN_MISSING"
    _check((_combine_step(1), RecipeStep(order=2, kind=RecipeStepKind.DROP_COLUMN, column="amount_max")))


def test_the_benchmark_log_combines_to_one_row_per_shopper_without_the_later_orders() -> None:
    frame = multirow_frame(300)
    profile = _profile(frame)
    params = plan_combine(
        frame,
        profile,
        key=KEY,
        time_column="order_date",
        snapshot_column="snapshot_date",
        outcome="reactivated_90d",
    )
    out = combine_rows(frame, key=KEY, params=params, max_failure_pct=5.0, leak_check="full").frame
    assert len(out) == frame[KEY].nunique() == 300
    assert (out["order_date_days_since_last"] >= 0).all()
    before = frame[pd.to_datetime(frame["order_date"]) <= pd.to_datetime(frame["snapshot_date"])]
    assert (
        out.set_index(KEY)["row_count"].sort_index().tolist()
        == before.groupby(KEY).size().sort_index().tolist()
    )
    assert out["order_value_max"].max() < 5_000  # every post-snapshot order is worth over 5,000
