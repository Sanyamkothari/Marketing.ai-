"""The order-log path's customer list and roll-up (v3 phase 0, B3 and B4).

Every expected figure is worked out here from the log with plain loops, not with the roll-up's own pandas calls.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from engine.doctor.order_log import (
    BLANK_CUSTOMER_CODE,
    MIXED_ORDERS_CODE,
    blank_customer_question,
    derive_customer_list,
    loss_finding,
    mixed_orders_question,
    order_log_reading,
)
from engine.doctor.order_roll_up import (
    LogColumns,
    OrderLogError,
    find_log_columns,
    list_matches_log,
    roll_up,
)
from tests.fixtures.order_log import order_log_frame
from tests.unit.doctor.order_log_support import loaded

UCI = LogColumns(
    customer="CustomerID",
    order="InvoiceNo",
    date="InvoiceDate",
    quantity="Quantity",
    price="UnitPrice",
    product="StockCode",
    country="Country",
)


def moment(text: str) -> datetime:
    return datetime.strptime(text, "%m/%d/%Y %H:%M")


def shopper_text(value: float) -> str:
    return str(int(value))


# ---------------------------------------------------------------------------
# B3: the derived customer list
# ---------------------------------------------------------------------------
def test_the_list_is_the_distinct_shoppers_written_as_a_person_reads_them() -> None:
    log = order_log_frame()
    derived = derive_customer_list(
        log, "CustomerID", "InvoiceDate", order_column="InvoiceNo", quantity_column="Quantity"
    )
    expected = {shopper_text(v) for v in log["CustomerID"].dropna().unique()}
    ids = list(derived.frame["CustomerID"])
    assert set(ids) == expected and len(ids) == len(expected) == 150
    assert not any("." in text for text in ids), "12346.0 is 12346"
    assert list_matches_log(derived.frame, log, "CustomerID")
    assert not list_matches_log(derived.frame.iloc[1:], log, "CustomerID")


def test_the_first_order_date_is_the_day_of_the_first_line_with_a_quantity_above_zero() -> None:
    log = order_log_frame()
    derived = derive_customer_list(log, "CustomerID", "InvoiceDate", quantity_column="Quantity")
    first: dict[str, datetime] = {}
    for row in log.itertuples(index=False):
        if pd.isna(row.CustomerID) or row.Quantity <= 0:
            continue
        key, when = shopper_text(row.CustomerID), moment(row.InvoiceDate)
        first[key] = min(when, first.get(key, when))
    got = dict(zip(derived.frame["CustomerID"], derived.frame["signup_date"], strict=True))
    assert got == {key: pd.Timestamp(when.date()) for key, when in first.items()}
    assert derived.no_order_shoppers == 0


def test_a_shopper_with_only_returns_stays_on_the_list_with_no_date() -> None:
    log = pd.DataFrame(
        {
            "CustomerID": [1.0, 1.0, 2.0],
            "InvoiceNo": ["A1", "A2", "C3"],
            "Quantity": [4, 1, -2],
            "InvoiceDate": ["1/5/2011 9:00", "2/6/2011 9:00", "3/7/2011 9:00"],
        }
    )
    derived = derive_customer_list(log, "CustomerID", "InvoiceDate", quantity_column="Quantity")
    assert list(derived.frame["CustomerID"]) == ["1", "2"]
    assert derived.frame["signup_date"].tolist()[0] == pd.Timestamp("2011-01-05")
    assert pd.isna(derived.frame["signup_date"].tolist()[1]) and derived.no_order_shoppers == 1
    assert list_matches_log(derived.frame, log, "CustomerID"), "the list is still every ID the log has"


def test_only_exact_rules_merge_ids_so_case_and_leading_zeros_stay_apart() -> None:
    log = pd.DataFrame(
        {
            "Shopper": ["a", "A", " a", "007", "7", 7, 7.0, "007", None, "  "],
            "Date": ["2011-01-01"] * 10,
        }
    )
    derived = derive_customer_list(log, "Shopper", "Date")
    # 7 and 7.0 are one number written two ways (key_text); everything else differs and stays two shoppers.
    assert sorted(derived.frame["Shopper"]) == sorted(["a", "A", " a", "007", "7"])
    assert derived.blank_lines == 2


def test_blank_lines_and_the_orders_they_belong_to_are_counted() -> None:
    log = order_log_frame()
    derived = derive_customer_list(
        log, "CustomerID", "InvoiceDate", order_column="InvoiceNo", quantity_column="Quantity"
    )
    blank = log[log["CustomerID"].isna()]
    assert derived.blank_lines == len(blank) == int(log["CustomerID"].isna().sum())
    assert derived.lines == len(log)
    assert derived.blank_orders == blank["InvoiceNo"].nunique()
    assert derived.mixed_orders == 0, "guest checkouts are whole orders"
    mixed = derive_customer_list(
        order_log_frame(mixed_orders=3), "CustomerID", "InvoiceDate", order_column="InvoiceNo"
    )
    assert mixed.mixed_orders == 3


def test_the_loss_guard_asks_over_five_percent_and_not_under(config_root: object) -> None:
    log = order_log_frame()
    reading = order_log_reading(loaded(log, "online-retail.csv", config_root))  # type: ignore[arg-type]
    assert reading is not None
    derived = derive_customer_list(log, "CustomerID", "InvoiceDate", order_column="InvoiceNo")
    finding = loss_finding(derived)
    assert finding is not None and finding.code == "PLAN_LOSES_TOO_MUCH" and finding.level == "stop"
    assert finding.count == derived.blank_lines
    few = derive_customer_list(order_log_frame(guest_share=0.03), "CustomerID", "InvoiceDate")
    assert few.blank_lines / few.lines < 0.05 and loss_finding(few) is None
    none = derive_customer_list(log.dropna(subset=["CustomerID"]), "CustomerID", "InvoiceDate")
    assert loss_finding(none) is None
    assert blank_customer_question(reading, none) is None


def test_q2_names_the_lines_the_orders_and_ties_the_yes_to_the_guard(config_root: object) -> None:
    log = pd.DataFrame(
        {
            "InvoiceNo": ["A1", "A1", "A2", "A3", "A3", "A4", "A5", "A5", "A6", "A6"],
            "InvoiceDate": pd.date_range("2011-01-03", periods=10, freq="D").strftime("%Y-%m-%d"),
            "CustomerID": [1.0, 1.0, 1.0, 2.0, 2.0, np.nan, np.nan, 3.0, 3.0, 3.0],
        }
    )
    reading = order_log_reading(loaded(log, "orders.csv", config_root), None)  # type: ignore[arg-type]
    assert reading is not None
    derived = derive_customer_list(log, "CustomerID", "InvoiceDate", order_column="InvoiceNo")
    asked = blank_customer_question(reading, derived)
    assert asked is not None
    question, effects, finding = asked
    assert question.text == (
        "2 of 10 lines (20.0%) have no 'CustomerID', 1 order, likely guest checkouts. "
        "Nobody can be counted for them. Leave them out? That is more than the 5% that needs your yes."
    )
    assert question.code == BLANK_CUSTOMER_CODE and question.blocking and question.recommended is None
    assert [o.label for o in question.options] == [
        "Leave them out",
        "Stop: I will get the shopper for these orders",
    ]
    leave, stop = effects
    assert leave.kind == "order_log_blank_customers" and stop.kind is None
    assert finding is not None and leave.payload["finding_key"] == finding.key
    assert question.options[0].rows_changed == 2 and question.options[1].safe
    # The yes is tied to this count: another count is another finding, so the old yes does not carry over.
    other = blank_customer_question(reading, derive_customer_list(log.iloc[:6], "CustomerID", "InvoiceDate"))
    assert other is not None and other[2] is not None and other[2].key != finding.key
    # Order A5 has a line with a shopper and a line without: asked, never assumed.
    mixed = mixed_orders_question(reading, derived)
    assert mixed is not None
    assert mixed[0].text == (
        "1 order in 'InvoiceNo' has some lines with a 'CustomerID' and some without. "
        "Keep the lines that have a 'CustomerID' and leave out the others?"
    )
    assert mixed[0].code == MIXED_ORDERS_CODE and mixed[0].blocking
    assert (
        mixed_orders_question(
            reading, derive_customer_list(log.iloc[:5], "CustomerID", "InvoiceDate", order_column="InvoiceNo")
        )
        is None
    )


# ---------------------------------------------------------------------------
# B4: the roll-up
# ---------------------------------------------------------------------------
def test_the_columns_of_the_uci_log_are_found_by_name() -> None:
    assert (
        find_log_columns(order_log_frame(), customer="CustomerID", order="InvoiceNo", date="InvoiceDate")
        == UCI
    )
    bare = find_log_columns(
        pd.DataFrame(columns=["Cust", "Bill", "When", "Description"]),
        customer="Cust",
        order="Bill",
        date="When",
    )
    assert (bare.quantity, bare.price, bare.product, bare.country) == (None, None, None, None)
    assert (
        "Description"
        not in find_log_columns(
            order_log_frame(), customer="CustomerID", order="InvoiceNo", date="InvoiceDate"
        ).used()
    )


def expected_orders(log: pd.DataFrame) -> dict[str, dict[str, object]]:
    """One entry per order number with a line of quantity above zero and a shopper: the shopper, the earliest
    timestamp, the value and the units, summed line by line."""
    found: dict[str, dict[str, object]] = {}
    for row in log.itertuples(index=False):
        if pd.isna(row.CustomerID) or row.Quantity <= 0:
            continue
        order = found.setdefault(
            row.InvoiceNo,
            {
                "shopper": shopper_text(row.CustomerID),
                "at": moment(row.InvoiceDate),
                "value": 0.0,
                "units": 0,
            },
        )
        order["at"] = min(order["at"], moment(row.InvoiceDate))  # type: ignore[type-var]
        order["value"] += row.Quantity * max(row.UnitPrice, 0.0)  # type: ignore[operator]
        order["units"] += row.Quantity  # type: ignore[operator]
    return found


def test_orders_are_one_row_per_order_with_the_earliest_timestamp_and_the_summed_value() -> None:
    log = order_log_frame()
    rolled = roll_up(log, UCI)
    expected = expected_orders(log)
    assert rolled.counts.orders == len(rolled.orders) == len(expected)
    by_shopper_and_time = sorted(
        (str(o["shopper"]), pd.Timestamp(o["at"]), round(float(o["value"]), 2), int(o["units"]))  # type: ignore[arg-type]
        for o in expected.values()
    )
    got = sorted(
        (str(r.CustomerID), r.event_time, round(r.order_value, 2), int(r.quantity))
        for r in rolled.orders.itertuples(index=False)
    )
    assert got == by_shopper_and_time
    assert rolled.counts.two_timestamp_orders == 2, "two orders were stamped a minute apart"
    two = log.groupby("InvoiceNo")["InvoiceDate"].nunique()
    for order in two[two > 1].index:
        if str(order).startswith("C"):
            continue
        lines = log[log["InvoiceNo"] == order]
        assert pd.Timestamp(expected[str(order)]["at"]) == min(moment(t) for t in lines["InvoiceDate"])  # type: ignore[arg-type]


def test_a_price_of_zero_adds_nothing_and_is_counted() -> None:
    log = order_log_frame()
    rolled = roll_up(log, UCI)
    unpriced = int(((log["Quantity"] > 0) & (log["UnitPrice"] <= 0) & log["CustomerID"].notna()).sum())
    assert rolled.counts.unpriced_lines == unpriced > 0
    assert (rolled.orders["order_value"] >= 0).all()


def test_returns_are_by_quantity_not_by_the_c_in_the_invoice_number() -> None:
    log = order_log_frame()
    rolled = roll_up(log, UCI)
    negative = log[log["Quantity"] < 0]
    named = negative[negative["CustomerID"].notna()]
    assert rolled.counts.return_lines == len(named)
    assert rolled.counts.return_orders == named["InvoiceNo"].nunique() == len(rolled.returns)
    assert (
        rolled.counts.cancelled_return_orders
        == named.loc[named["InvoiceNo"].str.startswith("C"), "InvoiceNo"].nunique()
        > 0
    )
    assert rolled.counts.return_lines_without_c == int((~named["InvoiceNo"].str.startswith("C")).sum()) == 3
    assert (rolled.returns["quantity"] > 0).all(), "returned units are a positive number"
    assert int(rolled.returns["quantity"].sum()) == int(-named["Quantity"].sum())
    # A line is an order whatever its invoice number looks like: no 'C' needed to be a return.
    assert not set(rolled.orders["order_month"]) - {t.strftime("%Y-%m") for t in rolled.orders["event_time"]}


def test_every_line_is_accounted_for_once() -> None:
    log = order_log_frame(mixed_orders=2)
    counts = roll_up(log, UCI).counts
    left = (
        counts.lines_without_shopper
        + counts.lines_without_order
        + counts.lines_unreadable_date
        + counts.lines_unreadable_quantity
    )
    assert counts.lines == len(log) == left + counts.order_lines + counts.return_lines + counts.zero_lines
    assert counts.lines_without_shopper == int(log["CustomerID"].isna().sum())


def test_lines_with_no_order_a_bad_date_or_a_bad_quantity_are_left_out_and_counted() -> None:
    log = order_log_frame().head(200).copy().astype({"Quantity": object})
    log.loc[log.index[:3], "InvoiceNo"] = None
    log.loc[log.index[3:5], "InvoiceDate"] = "not a date"
    log.loc[log.index[5:6], "Quantity"] = "lots"
    named = log["CustomerID"].notna()
    counts = roll_up(log, UCI).counts
    assert counts.lines_without_order == int(named.iloc[:3].sum())
    assert counts.lines_unreadable_date == int(named.iloc[3:5].sum())
    assert counts.lines_unreadable_quantity == int(named.iloc[5:6].sum())


def test_the_days_since_a_shoppers_previous_order_use_only_their_earlier_orders() -> None:
    log = order_log_frame()
    rolled = roll_up(log, UCI)
    for _shopper, group in rolled.orders.groupby("CustomerID"):
        ordered = group.sort_values("event_time")
        gaps = ordered["event_time"].dt.normalize().diff().dt.days.tolist()
        assert pd.isna(gaps[0]) and ordered["days_since_previous_order"].isna().iloc[0]
        assert ordered["days_since_previous_order"].tolist()[1:] == gaps[1:]
    # A later order never changes an earlier row: cut the log at a date and the rows before it are the same.
    cut = pd.Timestamp("2011-06-01")
    early = log[[moment(t) < cut for t in log["InvoiceDate"]]]
    again = roll_up(early, UCI).orders
    full = rolled.orders[rolled.orders["event_time"] < cut]
    pd.testing.assert_frame_equal(
        again.sort_values(["CustomerID", "event_time"]).reset_index(drop=True),
        full.sort_values(["CustomerID", "event_time"]).reset_index(drop=True),
    )


def test_order_lines_carry_their_orders_timestamp_so_an_order_is_never_split() -> None:
    log = order_log_frame()
    rolled = roll_up(log, UCI)
    assert len(rolled.order_lines) == rolled.counts.order_lines
    per_order = rolled.order_lines.groupby(["CustomerID", "event_time"]).size()
    expected = expected_orders(log)
    assert len(per_order) == len(expected)
    assert set(rolled.order_lines.columns) == {
        "CustomerID",
        "event_time",
        "product_code",
        "quantity",
        "line_value",
    }


def test_the_roll_up_is_deterministic() -> None:
    log = order_log_frame()
    first, second = roll_up(log, UCI), roll_up(log.copy(), UCI)
    for name, frame in first.tables().items():
        pd.testing.assert_frame_equal(frame, second.tables()[name])
    pd.testing.assert_frame_equal(first.customers, second.customers)
    assert first.counts == second.counts


def test_one_order_with_two_shoppers_stops_the_roll_up() -> None:
    log = order_log_frame()
    order = log.loc[log["CustomerID"].notna(), "InvoiceNo"].iloc[0]
    line = log.index[log["InvoiceNo"] == order][0]
    log.loc[line, "CustomerID"] = 99999.0
    with pytest.raises(OrderLogError) as raised:
        roll_up(log, UCI)
    assert (
        raised.value.code == "BUILD_PLAN_INCOMPLETE" and "more than one 'CustomerID'" in raised.value.message
    )


def test_no_order_number_is_a_question_not_a_guess() -> None:
    with pytest.raises(OrderLogError) as raised:
        roll_up(order_log_frame(), LogColumns(customer="CustomerID", order=None, date="InvoiceDate"))
    assert (
        raised.value.code == "BUILD_PLAN_INCOMPLETE" and "No column names the order" in raised.value.message
    )


def test_a_log_without_price_or_product_rolls_up_without_them() -> None:
    log = order_log_frame().drop(columns=["UnitPrice", "StockCode", "Country", "Description"])
    columns = find_log_columns(log, customer="CustomerID", order="InvoiceNo", date="InvoiceDate")
    rolled = roll_up(log, columns)
    assert "order_value" not in rolled.orders.columns and "country" not in rolled.orders.columns
    assert set(rolled.order_lines.columns) == {"CustomerID", "event_time", "quantity"}


# ---------------------------------------------------------------------------
# Mixed spellings, time zones, a blank column, copies, the independent list check
# ---------------------------------------------------------------------------
TINY = LogColumns(customer="Cust", order="Inv", date="When", quantity="Qty", price="Price")


def tiny(customers: list[object], orders: list[object], dates: list[object]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Inv": pd.Series(orders, dtype=object),
            "Cust": pd.Series(customers, dtype=object),
            "When": pd.Series(dates, dtype=object),
            "Qty": [1] * len(customers),
            "Price": [2.0] * len(customers),
        }
    )


def test_one_shopper_written_as_a_number_and_as_text_is_one_shopper_in_every_table() -> None:
    log = tiny(
        [7, "7", 7.0, "8"], ["1", "1", "2", "3"], ["2011-01-03", "2011-01-03", "2011-01-03", "2011-01-04"]
    )
    rolled = roll_up(log, TINY)
    assert list(rolled.customers["Cust"]) == ["7", "8"]
    assert list(rolled.orders["Cust"]) == ["7", "7", "8"]
    # Order 2 is on the same day as order 1, by the same shopper: 0 days since the previous one, not a blank.
    assert list(rolled.orders["days_since_previous_order"].fillna(-1)) == [-1, 0, -1]
    assert rolled.counts.orders == 3 and rolled.counts.shoppers == 2
    assert list_matches_log(rolled.customers, log, "Cust", rolled.tables().values())


def test_one_order_number_written_as_a_number_and_as_text_is_one_order() -> None:
    log = tiny(
        ["7", "7", "8"], [536365, "536365", 536366], ["2011-01-03 08:00", "2011-01-03 08:05", "2011-01-04"]
    )
    rolled = roll_up(log, TINY)
    assert rolled.counts.orders == 2 and rolled.counts.two_timestamp_orders == 1
    assert list(rolled.orders["quantity"]) == [2, 1]


def test_mixed_spellings_of_one_shopper_on_one_order_are_not_two_shoppers() -> None:
    log = tiny([7, "7", "7"], ["1", "1", "2"], ["2011-01-03", "2011-01-03", "2011-02-03"])
    rolled = roll_up(log, TINY)  # before the fix: BUILD_PLAN_INCOMPLETE, "more than one 'Cust'"
    assert rolled.counts.orders == 2 and rolled.counts.shoppers == 1


@pytest.mark.parametrize(
    "dates",
    [
        ["2011-01-01T23:30:00+05:30", "2011-01-02 01:00", "2011-01-03 10:00", "not a date"],
        ["2011-01-01T23:30:00+05:30", "2011-01-02T01:00:00+00:00", "2011-01-03T10:00:00+00:00", "not a date"],
        ["2011-01-01T23:30:00+05:30", "2011-01-02T06:30:00+05:30", "2011-01-03T15:30:00+05:30", "not a date"],
        ["2011-01-01 18:00", "2011-01-02 01:00", "2011-01-03 10:00", "not a date"],
    ],
    ids=["aware-and-naive", "mixed-offsets", "one-offset", "naive"],
)
def test_times_with_a_zone_are_read_in_utc_without_one(dates: list[str]) -> None:
    from engine.doctor.outcomes import snapshot_rows

    log = tiny(["7", "7", "8", "8"], ["1", "2", "3", "4"], dates)
    rolled = roll_up(log, TINY)
    assert rolled.counts.lines_unreadable_date == 1 and rolled.counts.orders == 3
    times = rolled.orders["event_time"]
    assert times.dt.tz is None and times.is_monotonic_increasing
    assert list(times) == [
        pd.Timestamp("2011-01-01 18:00"),  # 23:30 at +05:30 is 18:00 UTC
        pd.Timestamp("2011-01-02 01:00"),
        pd.Timestamp("2011-01-03 10:00"),
    ]
    signup = rolled.customers.set_index("Cust")["signup_date"]
    assert signup.to_dict() == {"7": pd.Timestamp("2011-01-01"), "8": pd.Timestamp("2011-01-03")}
    rows = snapshot_rows(rolled.customers["Cust"], [datetime(2011, 1, 2).date()], signup)
    assert list(rows["entity_key"]) == ["7"]


def test_a_shopper_column_blank_on_every_line_is_a_clean_refusal() -> None:
    log = tiny([None, None], ["1", "2"], ["2011-01-03", "2011-01-04"])
    derived = derive_customer_list(log, "Cust", "When", order_column="Inv", quantity_column="Qty")
    assert derived.frame.empty and derived.blank_lines == 2 and derived.blank_orders == 2
    with pytest.raises(OrderLogError) as raised:
        roll_up(log, TINY)
    assert raised.value.code == "BUILD_PLAN_INCOMPLETE" and "No line names a shopper" in raised.value.message


def test_exact_copies_of_a_line_are_kept_and_counted() -> None:
    log = tiny(
        ["7", "7", "7", "8"], ["1", "1", "1", "2"], ["2011-01-03", "2011-01-03", "2011-01-03", "2011-01-04"]
    )
    log.loc[2, "Qty"] = 5  # same order, another quantity: not a copy
    rolled = roll_up(log, TINY)
    assert rolled.counts.duplicate_lines == 1
    assert list(rolled.orders["quantity"]) == [7, 1], "the copy is summed, as the log has it"


def test_the_list_check_reads_the_log_on_its_own() -> None:
    log = tiny([7, "7", "8"], ["1", "1", "2"], ["2011-01-03", "2011-01-03", "2011-01-04"])
    rolled = roll_up(log, TINY)
    customers = rolled.customers
    assert list_matches_log(customers, log, "Cust", rolled.tables().values())
    twice = pd.concat([customers, customers.iloc[:1]], ignore_index=True)
    assert not list_matches_log(twice, log, "Cust"), "nobody twice"
    split = pd.DataFrame({"Cust": ["7", "7.0", "8"]})
    assert not list_matches_log(split, log, "Cust"), "nobody added"
    stranger = rolled.orders.assign(Cust=["7", "9"])
    assert not list_matches_log(customers, log, "Cust", [stranger]), "every shopper of a table is on the list"
