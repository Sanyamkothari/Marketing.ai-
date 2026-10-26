"""The Data Doctor reads a file's ID by its name first, even with some blanks (the Data Doctor review, Phase A)."""

from __future__ import annotations

from engine.doctor.diagnose import _id_column
from engine.onboarding.specs import KeyCandidate


def _candidate(column: str, null_rate: float, rows_per_key: float = 1.0) -> KeyCandidate:
    return KeyCandidate(column=column, distinct_count=100, null_rate=null_rate, rows_per_key=rows_per_key)


def test_a_customer_id_with_a_few_blanks_beats_a_filled_amount_column() -> None:
    ranked = (_candidate("spend_12m", 0.0), _candidate("CustomerID", 0.03))
    assert _id_column(ranked) == ranked[1]


def test_an_order_log_is_about_its_customers_not_its_invoices() -> None:
    ranked = (_candidate("InvoiceNo", 0.0, 20.9), _candidate("CustomerID", 0.25, 124.0))
    assert _id_column(ranked) == ranked[1]


def test_a_mostly_blank_id_column_does_not_win() -> None:
    ranked = (_candidate("InvoiceNo", 0.0), _candidate("CustomerID", 0.8))
    assert _id_column(ranked) == ranked[0]


def test_without_an_id_named_column_the_ranking_stands() -> None:
    ranked = (_candidate("code", 0.0), _candidate("amount", 0.1))
    assert _id_column(ranked) == ranked[0]
    assert _id_column(()) is None


def test_a_product_code_is_never_read_as_lakh_or_crore() -> None:
    import pandas as pd

    from engine.doctor.indian_values import propose_indian_numbers

    codes = [
        "71053",
        "22752",
        "82494L",
        "21730",
        "84029K",
        "85123A",
    ] * 50  # as in UCI: 82494L is not 82,494 lakh
    frame = pd.DataFrame(
        {"StockCode": codes, "amount": ["2L", "3 lakh", "1.5 crore", "40000", "5L", "70000"] * 50}
    )
    proposed = {p.column for p in propose_indian_numbers(frame)}
    assert "StockCode" not in proposed and "amount" in proposed
