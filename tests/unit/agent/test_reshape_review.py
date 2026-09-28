"""Level 3 combine: the defects the Plan G adversarial review confirmed, each pinned by its repro."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest

from engine.agent import reshape
from engine.agent.advisor import advise
from engine.agent.contracts import SessionStatus
from engine.agent.reshape import CombineError, CombineSpec, combine_rows, plan_combine
from engine.config import load_use_case
from engine.stages import ingest
from tests.fixtures.agent_bench.make_multirow import multirow_frame
from tests.unit.agent.helpers import context_for

KEY = "customer_id"
TARGET = "reactivated_90d"


def _features(*items: tuple[str, str, str | None]) -> list[dict[str, str | None]]:
    return [{"name": name, "function": function, "column": column} for name, function, column in items]


def _params(time_column: str, snapshot_column: str | None, *features: Any, **extra: Any) -> dict[str, Any]:
    return {
        "time_column": time_column,
        "snapshot_column": snapshot_column,
        "dayfirst": False,
        "outcome": extra.pop("outcome", None),
        "features": _features(*features),
        **extra,
    }


def _profile(frame: pd.DataFrame) -> Any:
    return ingest.profile_dataset(
        frame,
        load_use_case("retail-win-back"),
        upload_id="u",
        file_name="f.csv",
        file_format="csv",
        file_size_bytes=1,
        delimiter=",",
        encoding="utf-8",
    )


def _plan(frame: pd.DataFrame, **kwargs: Any) -> dict[str, Any]:
    options: dict[str, Any] = {
        "key": KEY,
        "time_column": "order_date",
        "snapshot_column": "snapshot_date",
        "outcome": TARGET,
    }
    options.update(kwargs)
    return plan_combine(frame, _profile(frame), **options)


# ---------------------------------------------------------------------------
# days_since_last over another date column never reads a date after the snapshot
# ---------------------------------------------------------------------------
def test_a_date_after_the_snapshot_does_not_hide_the_dates_known_before_it() -> None:
    frame = pd.DataFrame(
        {
            "cid": ["a", "a", "b"],
            "order_date": ["2024-01-10", "2024-02-10", "2024-02-01"],
            "snapshot_date": ["2024-03-01"] * 3,
            "refund_date": ["2024-01-20", "2024-03-15", None],  # a's second refund came after the snapshot
        }
    )
    params = _params(
        "order_date",
        "snapshot_date",
        ("row_count", "count", None),
        ("refund_date_days_since_last", "days_since_last", "refund_date"),
    )
    out = combine_rows(frame, key="cid", params=params, max_failure_pct=5.0, leak_check="narrow").frame
    days = out.set_index("cid")["refund_date_days_since_last"]
    assert (
        days["a"] == (pd.Timestamp("2024-03-01") - pd.Timestamp("2024-01-20")).days
    )  # the refund known then
    assert pd.isna(days["b"])
    # On a training file the column is refused outright: whether it is empty could give the answer away.
    with pytest.raises(CombineError) as caught:
        combine_rows(frame, key="cid", params=params, max_failure_pct=5.0, leak_check="full")
    assert (caught.value.code, caught.value.column) == ("RECIPE_STEP_INVALID", "refund_date")
    assert "FUTURE_EVENTS_LEAKED" in caught.value.message


def test_a_date_column_written_after_the_snapshot_is_left_out_of_the_plan() -> None:
    frame = multirow_frame()
    frame["last_order_date"] = frame.groupby(KEY)["order_date"].transform("max")  # overwritten later
    frame["first_seen"] = frame.groupby(KEY)["order_date"].transform("min")  # known at the snapshot
    params = _plan(frame)
    columns = {f["column"] for f in params["features"]}
    assert "last_order_date" not in columns
    assert "first_seen" in columns
    out = combine_rows(frame, key=KEY, params=params, max_failure_pct=5.0, leak_check="full").frame
    # No combined column is blank for exactly the shoppers who came back.
    for name in out.columns:
        blank = out[name].isna()
        if blank.any():
            assert not blank.equals(out[TARGET].astype(bool)), name


# ---------------------------------------------------------------------------
# Day/month order per column
# ---------------------------------------------------------------------------
def test_a_snapshot_column_whose_values_prove_day_first_is_read_day_first() -> None:
    frame = pd.DataFrame(
        {
            "cid": ["a", "a", "b"],
            "order_date": ["2011-09-05", "2011-09-20", "2011-09-01"],
            "snapshot_date": ["10/09/2011", "10/09/2011", "25/09/2011"],  # 25 proves the day comes first
        }
    )
    params = _plan(frame, key="cid", outcome=None)
    assert params["dayfirst"] == {"order_date": True, "snapshot_date": True}
    out = combine_rows(frame, key="cid", params=params, max_failure_pct=5.0).frame.set_index("cid")
    assert out.loc["a", "snapshot_date"] == pd.Timestamp("2011-09-10")
    assert out.loc["a", "row_count"] == 1  # the 20 September order is after the snapshot


def test_each_date_column_keeps_the_order_its_own_values_prove() -> None:
    frame = pd.DataFrame(
        {
            "cid": ["a", "a", "b"],
            "order_date": ["25/08/2011", "03/09/2011", "01/09/2011"],  # day first
            "snapshot_date": ["09/05/2011", "09/05/2011", "09/15/2011"],  # month first
        }
    )
    params = _plan(frame, key="cid", outcome=None)
    assert params["dayfirst"] == {"order_date": True, "snapshot_date": False}
    out = combine_rows(frame, key="cid", params=params, max_failure_pct=5.0).frame.set_index("cid")
    assert out.loc["a", "snapshot_date"] == pd.Timestamp("2011-09-05")
    assert out.loc["a", "row_count"] == 2  # 25 August and 3 September, both before 5 September


def test_the_plan_decides_the_order_from_every_date_column() -> None:
    frame = multirow_frame(200)
    frame["snapshot_date"] = "10/09/2011"
    frame["signup"] = "25/08/2010"  # proves day first; the ISO order dates prove nothing
    params = _plan(frame)
    assert params["dayfirst"] == {"order_date": True, "snapshot_date": True, "signup": True}
    out = combine_rows(frame, key=KEY, params=params, max_failure_pct=5.0).frame
    assert (out["snapshot_date"] == pd.Timestamp("2011-09-10")).all()
    assert out["order_value_max"].max() < 5_000  # no order after 10 September counted


def test_a_date_column_that_proves_nothing_takes_the_order_the_file_proves() -> None:
    frame = pd.DataFrame(
        {
            "cid": ["a", "a", "b"],
            "order_date": ["25/08/2011", "03/09/2011", "01/09/2011"],  # proves day first
            "signup": ["03/04/2010", "03/04/2010", "05/06/2010"],  # proves nothing on its own
        }
    )
    params = _plan(frame, key="cid", snapshot_column=None, outcome=None)
    assert params["dayfirst"] == {"order_date": True, "signup": True}
    signup = {"name": "signup_days_since_last", "function": "days_since_last", "column": "signup"}
    assert signup in params["features"]
    out = combine_rows(frame, key="cid", params=params, max_failure_pct=5.0).frame.set_index("cid")
    expected = (pd.Timestamp("2011-09-03") - pd.Timestamp("2010-04-03")).days  # 3 April, not 4 March
    assert out.loc["a", "signup_days_since_last"] == expected


def test_a_date_column_whose_order_nothing_in_the_file_proves_is_left_out() -> None:
    frame = pd.DataFrame(
        {
            "cid": ["a", "a", "b"],
            "order_date": ["2011-08-25", "2011-09-03", "2011-09-01"],
            "signup": ["03/04/2010", "03/04/2010", "05/06/2010"],
        }
    )
    params = _plan(frame, key="cid", snapshot_column=None, outcome=None)
    assert params["dayfirst"] == {"order_date": None}
    assert "signup" not in {f["column"] for f in params["features"]}


# ---------------------------------------------------------------------------
# Entity-wise: an entity's output depends only on its own rows and the frozen parameters (DEC-1023)
# ---------------------------------------------------------------------------
def test_an_entitys_dates_do_not_depend_on_other_entities() -> None:
    frame = pd.DataFrame(
        {
            "cid": ["a", "a", "b"],
            "order_date": ["2024-03-10", "2024-03-20", "2024-03-01"],
            "snapshot_date": ["04/03/2024", "04/03/2024", "04/25/2024"],  # b's value reads month first
        }
    )
    params = _params("order_date", "snapshot_date", ("row_count", "count", None), dayfirst=True)
    whole = combine_rows(frame, key="cid", params=params, max_failure_pct=5.0).frame.set_index("cid")
    alone = combine_rows(frame.iloc[:2], key="cid", params=params, max_failure_pct=5.0).frame
    pd.testing.assert_frame_equal(whole.loc[["a"]], alone.set_index("cid"))
    assert whole.loc["a", "snapshot_date"] == pd.Timestamp("2024-03-04")  # the frozen order
    assert whole.loc["a", "row_count"] == 0
    assert whole.loc["b", "snapshot_date"] == pd.Timestamp("2024-04-25")  # the only reading it has


def test_an_entitys_key_does_not_depend_on_other_entities() -> None:
    frame = pd.DataFrame(
        {
            "cid": pd.Series([7, 8, "7"], dtype=object),
            "order_date": ["2024-01-08", "2024-01-09", "2024-01-25"],
        }
    )
    params = _params("order_date", None, ("row_count", "count", None))
    whole = combine_rows(frame, key="cid", params=params, max_failure_pct=5.0).frame
    alone = combine_rows(frame.iloc[[1]], key="cid", params=params, max_failure_pct=5.0).frame
    assert whole["cid"].tolist() == ["7", "8"]
    assert alone["cid"].tolist() == ["8"]


def test_a_snapshot_whose_day_month_order_nothing_proves_is_not_guessed() -> None:
    frame = multirow_frame()
    frame["snapshot_date"] = "10/09/2011"
    frame.loc[0, "snapshot_date"] = "unknown"  # the format detector skips the column
    ctx = context_for(frame, "retail-win-back")
    first = advise(ctx)
    step = next(
        o.proposal.step
        for q in first.questions
        for o in q.options
        if o.option_id == "combine" and o.proposal is not None
    )
    assert step is not None and step.params["dayfirst"] == {"order_date": None, "snapshot_date": None}
    with pytest.raises(CombineError) as caught:
        combine_rows(frame, key=KEY, params=step.params, max_failure_pct=5.0)
    assert (caught.value.code, caught.value.column) == ("RECIPE_VALUES_UNCONVERTED", "snapshot_date")
    second = advise(ctx, primary_key=KEY, target=TARGET, combine=step, id_prefix="a2-")
    assert second.status is SessionStatus.STOPPED
    assert second.stop_reason is not None and "snapshot_date" in second.stop_reason


# ---------------------------------------------------------------------------
# Rows with no readable snapshot are counted
# ---------------------------------------------------------------------------
def test_rows_without_a_readable_snapshot_are_counted_and_stop_above_the_limit() -> None:
    frame = pd.DataFrame(
        {
            "cid": ["a", "a", "b", "b", "c"],
            "order_date": ["2024-02-01"] * 5,
            "as_of": ["2024-03-01", "2024-03-01", None, None, "2024-03-01"],
        }
    )
    params = _params("order_date", "as_of", ("row_count", "count", None))
    with pytest.raises(CombineError) as caught:
        combine_rows(frame, key="cid", params=params, max_failure_pct=5.0, leak_check="narrow")
    assert caught.value.code == "RECIPE_VALUES_UNCONVERTED"
    tolerant = combine_rows(frame, key="cid", params=params, max_failure_pct=50.0)
    assert (tolerant.failed, tolerant.entities) == (2, 2)
    with pytest.raises(CombineError):
        combine_rows(frame.assign(as_of=None), key="cid", params=params, max_failure_pct=5.0)


def test_a_snapshot_on_some_rows_of_an_entity_dates_all_of_its_rows() -> None:
    frame = pd.DataFrame(
        {
            "cid": ["a", "a", "a", "b", "b"],
            "order_date": ["2024-01-01", "2024-01-15", "2024-02-01", "2024-01-05", "2024-02-10"],
            "as_of": [None, None, "2024-03-01", None, "2024-03-01"],  # filled on the last row only
            "amt": [1.0, 2.0, 3.0, 4.0, 5.0],
        }
    )
    params = _params("order_date", "as_of", ("row_count", "count", None), ("amt_sum", "sum", "amt"))
    result = combine_rows(frame, key="cid", params=params, max_failure_pct=5.0, leak_check="full")
    out = result.frame.set_index("cid")
    assert result.failed == 0
    assert out["row_count"].to_dict() == {"a": 3, "b": 2}
    assert out["amt_sum"].to_dict() == {"a": 6.0, "b": 9.0}


# ---------------------------------------------------------------------------
# Dates outside pandas' nanosecond range
# ---------------------------------------------------------------------------
def test_a_sentinel_date_outside_the_nanosecond_range_is_an_empty_date() -> None:
    frame = pd.DataFrame(
        {
            "cid": ["a", "a", "b"],
            "order_date": pd.Series(
                np.array(["2024-01-10", "2024-02-10", "2024-02-01"], dtype="datetime64[us]")
            ),
            "snapshot_date": pd.Series(np.array(["2024-03-01"] * 3, dtype="datetime64[us]")),
            "contract_end": pd.Series(
                np.array(["2024-02-15", "9999-12-31", "9999-12-31"], dtype="datetime64[us]")
            ),
        }
    )
    params = _params(
        "order_date",
        "snapshot_date",
        ("row_count", "count", None),
        ("contract_end_days_since_last", "days_since_last", "contract_end"),
    )
    out = combine_rows(frame, key="cid", params=params, max_failure_pct=5.0, leak_check="full").frame
    days = out.set_index("cid")["contract_end_days_since_last"]
    assert days["a"] == (pd.Timestamp("2024-03-01") - pd.Timestamp("2024-02-15")).days
    assert pd.isna(days["b"])
    late = frame.assign(snapshot_date=frame["contract_end"])
    with pytest.raises(CombineError) as caught:  # an unreadable snapshot is counted, never a crash
        combine_rows(late, key="cid", params=params, max_failure_pct=5.0)
    assert caught.value.code == "RECIPE_VALUES_UNCONVERTED"


# ---------------------------------------------------------------------------
# Keys of mixed types (a CSV read in chunks)
# ---------------------------------------------------------------------------
def test_an_id_written_as_a_number_and_as_text_is_one_entity() -> None:
    frame = pd.DataFrame(
        {
            "cid": pd.Series([7, 7, 8, "7", "7", "GUEST"], dtype=object),
            "order_date": [
                "2024-01-08",
                "2024-01-20",
                "2024-01-09",
                "2024-01-25",
                "2024-02-02",
                "2024-02-01",
            ],
            "amt": [10.0, 20.0, 5.0, 30.0, 40.0, 1.0],
            "churned": [1, 1, 0, 1, 1, 0],
        }
    )
    params = _params(
        "order_date",
        None,
        ("row_count", "count", None),
        ("amt_sum", "sum", "amt"),
        outcome="churned",
    )
    out = combine_rows(frame, key="cid", params=params, max_failure_pct=5.0, leak_check="full").frame
    assert out["cid"].is_unique
    assert len(out) == 3
    seven = out[out["cid"].astype(str) == "7"].iloc[0]
    assert (seven["row_count"], seven["amt_sum"], seven["churned"]) == (4, 100.0, 1)


# ---------------------------------------------------------------------------
# Carried consent columns keep any header
# ---------------------------------------------------------------------------
def test_a_carried_column_keeps_a_header_with_punctuation() -> None:
    frame = multirow_frame(50)
    frame["Email opt-out (Y/N)"] = "N"
    params = _plan(frame, carry=["Email opt-out (Y/N)"])
    assert {"name": "Email opt-out (Y/N)", "function": "latest", "column": "Email opt-out (Y/N)"} in params[
        "features"
    ]
    CombineSpec.from_params(KEY, params)
    out = combine_rows(frame, key=KEY, params=params, max_failure_pct=5.0).frame
    assert (out["Email opt-out (Y/N)"] == "N").all()
    with pytest.raises(CombineError):  # a name the plan did not carry still has to be a plain name
        CombineSpec.from_params(
            KEY, {**params, "features": _features(("x (y)", "latest", "Email opt-out (Y/N)"))}
        )


# ---------------------------------------------------------------------------
# Dates with UTC offsets
# ---------------------------------------------------------------------------
def test_dates_with_different_utc_offsets_are_compared_in_utc() -> None:
    frame = pd.DataFrame(
        {
            "cid": ["a", "a", "b"],
            "event_at": ["2024-01-01T06:00:00Z", "2024-01-01T09:00:00Z", "2024-01-01T06:00:00Z"],
            "snapshot_date": ["2024-01-01T12:00:00+05:00"] * 3,  # 07:00 UTC
            "amount": [10.0, 1000.0, 5.0],
        }
    )
    params = _params(
        "event_at", "snapshot_date", ("row_count", "count", None), ("amount_sum", "sum", "amount")
    )
    out = combine_rows(frame, key="cid", params=params, max_failure_pct=5.0, leak_check="full").frame
    a = out.set_index("cid").loc["a"]
    assert (a["row_count"], a["amount_sum"]) == (1, 10.0)
    assert a["snapshot_date"] == pd.Timestamp("2024-01-01 07:00")


def test_time_zone_aware_columns_are_compared_in_utc() -> None:
    frame = pd.DataFrame(
        {
            "cid": ["a", "a"],
            "event_at": pd.to_datetime(["2024-01-01T06:00:00Z", "2024-01-01T09:00:00Z"]),
            "snapshot_date": pd.to_datetime(["2024-01-01T12:00:00+05:00"] * 2).tz_convert("Asia/Karachi"),
            "amount": [10.0, 1000.0],
        }
    )
    params = _params("event_at", "snapshot_date", ("amount_sum", "sum", "amount"))
    out = combine_rows(frame, key="cid", params=params, max_failure_pct=5.0).frame
    assert out.loc[0, "amount_sum"] == 10.0


def test_a_zoned_date_that_cannot_be_read_in_utc_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    def unreadable(texts: pd.Series[Any], dayfirst: bool) -> pd.Series[Any]:
        return pd.Series(pd.NaT, index=texts.index, dtype="datetime64[ns]")

    monkeypatch.setattr(reshape, "_utc", unreadable)
    values = reshape._dates(pd.Series(["2024-01-01T12:00:00+05:00", "2024-01-02"]), False, "at")
    assert pd.isna(values[0])  # never read at its clock time, 12:00
    assert values[1] == pd.Timestamp("2024-01-02")


# ---------------------------------------------------------------------------
# Same-date ties: `latest` and the outcome read the same row
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("flip", [False, True])
def test_latest_and_the_outcome_break_same_date_ties_by_the_later_row(flip: bool) -> None:
    frame = pd.DataFrame(
        {
            "cid": ["a", "a", "a", "b"],
            "order_date": ["2024-01-01", "2024-02-01", "2024-02-01", "2024-01-05"],
            "plan_tier": ["free", "basic", "pro", "basic"],
            "spend": [1.0, 2.0, 3.0, 4.0],
            "churned": [0, 0, 1, 0],
        }
    )
    if flip:
        frame = frame.iloc[[0, 2, 1, 3]].reset_index(drop=True)
    params = _params(
        "order_date",
        None,
        ("plan_tier_latest", "latest", "plan_tier"),
        ("spend_latest", "latest", "spend"),
        ("spend_sum", "sum", "spend"),
        outcome="churned",
    )
    a = combine_rows(frame, key="cid", params=params, max_failure_pct=5.0, leak_check="full").frame.iloc[0]
    last = frame[frame["cid"] == "a"].iloc[-1]  # the later of the two 1 February rows
    assert (a["plan_tier_latest"], a["spend_latest"], a["churned"]) == (
        last["plan_tier"],
        last["spend"],
        last["churned"],
    )
    assert a["spend_sum"] == 6.0


@pytest.mark.parametrize("flip", [False, True])
def test_a_same_date_tie_goes_to_the_later_snapshot_like_the_outcome(flip: bool) -> None:
    frame = pd.DataFrame(
        {
            "cid": ["a", "a"],
            "order_date": ["2024-02-01", "2024-02-01"],
            "snapshot_date": ["2024-03-02", "2024-03-01"],
            "tier": ["x", "y"],
            "churned": [1, 0],
        }
    )
    if flip:
        frame = frame.iloc[::-1].reset_index(drop=True)
    params = _params("order_date", "snapshot_date", ("tier_latest", "latest", "tier"), outcome="churned")
    a = combine_rows(frame, key="cid", params=params, max_failure_pct=5.0, leak_check="full").frame.iloc[0]
    assert (a["tier_latest"], a["churned"]) == ("x", 1)  # both from the 2 March snapshot row


# ---------------------------------------------------------------------------
# The frozen per-column order is checked
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "dayfirst",
    [{"order_date": "yes"}, {"elsewhere": True}, {1: True}, "yes"],
    ids=["value", "column", "key", "text"],
)
def test_a_per_column_order_names_the_steps_dates_with_true_false_or_null(dayfirst: Any) -> None:
    params = _params("order_date", "snapshot_date", ("row_count", "count", None), dayfirst=dayfirst)
    with pytest.raises(CombineError) as caught:
        CombineSpec.from_params("cid", params)
    assert caught.value.code == "RECIPE_STEP_INVALID"
    spec = CombineSpec.from_params("cid", {**params, "dayfirst": {"order_date": True, "snapshot_date": None}})
    assert (spec.order("order_date"), spec.order("snapshot_date"), spec.order("other")) == (True, None, None)
