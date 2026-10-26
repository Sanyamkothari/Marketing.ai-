"""The order-log path's detector and Q1 (v3 phase 0, B2)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from engine.config import load_use_case
from engine.doctor.order_log import (
    CUSTOMER_COLUMN_CODE,
    id_tier,
    name_words,
    order_log_enabled,
    order_log_reading,
)
from tests.fixtures.order_log import order_log_frame
from tests.unit.doctor.order_log_support import loaded


def small_log() -> pd.DataFrame:
    """Ten lines: three shoppers, two lines of no shopper, six orders."""
    return pd.DataFrame(
        {
            "InvoiceNo": ["A1", "A1", "A2", "A3", "A3", "A4", "A5", "A5", "A6", "A6"],
            "Quantity": [1, 2, 1, 5, 1, 1, 3, 1, 2, 2],
            "InvoiceDate": pd.date_range("2011-01-03", periods=10, freq="D").strftime("%Y-%m-%d"),
            "CustomerID": [1.0, 1.0, 1.0, 2.0, 2.0, np.nan, np.nan, 3.0, 3.0, 3.0],
        }
    )


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("name", "words"),
    [
        ("CustomerID", ("customer", "id")),
        ("InvoiceNo", ("invoice", "no")),
        ("customer_id", ("customer", "id")),
        ("Customer ID", ("customer", "id")),
        ("CUSTOMERID", ("customer", "id")),
        ("INVOICENO", ("invoice", "no")),
        ("StockCode", ("stock", "code")),
        ("PAID", ("paid",)),
        ("cust-no", ("cust", "no")),
        ("OrderNo2", ("order", "no", "2")),
    ],
)
def test_names_are_split_at_capitals_and_separators(name: str, words: tuple[str, ...]) -> None:
    assert name_words(name) == words


@pytest.mark.parametrize(
    ("name", "tier"),
    [
        ("CustomerID", 0),
        ("cust_no", 0),
        ("Customer", 0),
        ("CLIENT_CODE_ID", 0),
        ("id", 1),
        ("row_key", 1),
        ("InvoiceNo", 2),
        ("OrderID", 2),
        ("bill_number", 2),
        ("StockCode", None),
        ("CustomerName", None),
        ("PAID", None),
        ("UnitPrice", None),
    ],
)
def test_a_name_ranks_as_a_shopper_another_id_or_an_order_number(name: str, tier: int | None) -> None:
    assert id_tier(name) == tier


# ---------------------------------------------------------------------------
# The detector
# ---------------------------------------------------------------------------
def test_the_detector_reads_the_uci_shaped_log(config_root: Path) -> None:
    frame = order_log_frame()
    config = load_use_case("retail-win-back", config_root)
    reading = order_log_reading(loaded(frame, "online-retail.csv", config_root), config)
    assert reading is not None
    assert reading.date_column == "InvoiceDate"
    assert reading.rows == len(frame) and not reading.sampled
    # The shopper first although a quarter of its cells are blank: ranked by name before blank share.
    assert [c.column for c in reading.candidates] == ["CustomerID", "InvoiceNo"]
    customer, invoice = reading.candidates
    assert customer.distinct == frame["CustomerID"].nunique()
    assert customer.blank_rows == int(frame["CustomerID"].isna().sum())
    assert customer.rows == len(frame)
    assert invoice.distinct == frame["InvoiceNo"].nunique() and invoice.blank_rows == 0
    assert reading.customer_candidates == (customer,)
    assert reading.order_number == invoice and invoice.looks_like_order_number
    # StockCode repeats too but is a product code, not an ID of a person or an order.
    assert reading.column("StockCode") is None


def test_the_shopper_outranks_a_fuller_order_number_and_blank_share_orders_equals(config_root: Path) -> None:
    frame = small_log().rename(columns={"InvoiceNo": "OrderID"})
    frame["AccountId"] = ["a", "a", "b", "b", "c", "c", "d", "d", "e", "e"]
    frame["MemberID"] = [None, None, "m", "m", "m", "n", "n", "n", "o", "o"]
    reading = order_log_reading(loaded(frame, "log.csv", config_root))
    assert reading is not None
    # Shopper-like names first, the fuller one first among them (ties keep the file's order); the order number
    # last although it is never blank.
    assert [c.column for c in reading.candidates] == ["AccountId", "CustomerID", "MemberID", "OrderID"]


def test_a_file_that_is_not_an_order_log_is_not_read_as_one(config_root: Path) -> None:
    config = load_use_case("retail-win-back", config_root)
    log = small_log()

    def reads(frame: pd.DataFrame, **kwargs: object) -> bool:
        return order_log_reading(loaded(frame, "f.csv", config_root), config, **kwargs) is not None  # type: ignore[arg-type]

    assert reads(log)
    # One row per customer: the ID does not repeat.
    assert not reads(
        log.drop_duplicates("CustomerID").assign(InvoiceNo=lambda f: [f"I{i}" for i in range(len(f))])
    )
    # No date.
    assert not reads(log.drop(columns="InvoiceDate"))
    # Only an order number repeats: nobody is named.
    assert not reads(log.drop(columns="CustomerID"))
    # The goal's outcome is in the file: it is not waiting for one.
    assert not reads(log.assign(reactivated_90d=[0, 1] * 5))
    # Mostly blank: the column names nobody.
    assert not reads(log.assign(CustomerID=[1.0] + [np.nan] * 9))
    # A repeating column whose name is not an ID's.
    assert not reads(
        log.rename(columns={"CustomerID": "Country", "InvoiceNo": "StockCode"}).assign(Country="x")
    )


def test_a_date_the_caller_already_has_is_used(config_root: Path) -> None:
    config = load_use_case("retail-win-back", config_root)
    reading = order_log_reading(
        loaded(small_log(), "f.csv", config_root), config, date_columns=["InvoiceDate"]
    )
    assert reading is not None and reading.date_column == "InvoiceDate"
    assert order_log_reading(loaded(small_log(), "f.csv", config_root), config, date_columns=[]) is None


def test_the_path_needs_both_switches(config_root: Path) -> None:
    config = load_use_case("retail-win-back", config_root)
    assert not order_log_enabled(config)
    only_unified = config.model_copy(
        update={"data_doctor": config.data_doctor.model_copy(update={"unified": True})}
    )
    assert not order_log_enabled(only_unified)
    both = config.model_copy(
        update={"data_doctor": config.data_doctor.model_copy(update={"unified": True, "order_log": True})}
    )
    assert order_log_enabled(both)


# ---------------------------------------------------------------------------
# Q1
# ---------------------------------------------------------------------------
def test_q1_names_the_counts_and_recommends_nothing(config_root: Path) -> None:
    from engine.doctor.order_log import customer_question

    reading = order_log_reading(loaded(small_log(), "orders.csv", config_root, "u_9"))
    assert reading is not None
    question, effects = customer_question(reading)
    assert question.text == (
        "Each row of orders.csv is one line of an order, not one shopper. Which column names the shopper?\n"
        "• 'CustomerID': 3 different values, 2.7 lines each on average, blank on 2 lines (20.0%).\n"
        "• 'InvoiceNo': 6 different values, 1.7 lines each on average, never blank. "
        "This looks like the order number."
    )
    assert question.code == CUSTOMER_COLUMN_CODE
    assert question.blocking is True and question.recommended is None
    assert question.upload_id == "u_9" and question.answer is None
    assert [o.label for o in question.options] == [
        "CustomerID",
        "InvoiceNo",
        "None of these: this file is not one row per order line",
    ]
    assert [e.option_id for e in effects] == ["c0", "c1", "none"]
    assert effects[0].kind == "order_log_customer"
    assert effects[0].payload == {"upload_id": "u_9", "column": "CustomerID"}
    assert effects[2].kind is None  # "None of these" adds nothing: the data request stays
    assert question.options[2].effect_digest is None and question.options[0].effect_digest is not None
    # The same file and goal always ask the same question; another goal asks its own.
    again, _ = customer_question(reading)
    other, _ = customer_question(reading, goal_id="win-back-campaign")
    assert again.question_id == question.question_id != other.question_id


def test_q1_says_when_the_counts_are_of_a_part_of_the_file(config_root: Path) -> None:
    from engine.doctor.order_log import customer_question

    reading = order_log_reading(loaded(small_log(), "orders.csv", config_root, file_rows=500))
    assert reading is not None and reading.sampled and reading.rows == 500
    question, _ = customer_question(reading)
    assert question.text.endswith("Counted on the rows read of 500.")
