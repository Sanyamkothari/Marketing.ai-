"""An order log's headers as clients write them: `CustomerID`, `InvoiceNo`, `StockCode` (the Data Doctor review, Phase A).

The UCI Online Retail log stopped with "No column holds a different value on every row" because the hint
`customer_id` did not meet the header `CustomerID`, and a pre-ticked fix would have turned the invoice numbers into
numbers, blanking the `C...` cancellations and adding the rest up after a combine.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from engine.agent.advisor import ROLE_PRIMARY_KEY, _looks_like_code, advise
from engine.agent.contracts import AgentConfidence, ProposalKind, RecipeStepKind
from tests.unit.agent.helpers import context_for


def _order_log(rows: int = 3000, customer: str = "CustomerID") -> pd.DataFrame:
    rng = np.random.default_rng(7)
    invoices = rng.integers(536000, 537500, rows).astype(str)
    invoices[:40] = [f"C{n}" for n in invoices[:40]]  # cancellations
    return pd.DataFrame(
        {
            "InvoiceNo": invoices,
            "StockCode": rng.integers(10000, 90000, rows).astype(str),
            "Quantity": rng.integers(1, 20, rows).astype(str),
            "InvoiceDate": (
                pd.Timestamp("2010-12-01") + pd.to_timedelta(rng.integers(0, 370, rows), unit="D")
            ).strftime("%Y-%m-%d"),
            "UnitPrice": (rng.random(rows) * 10).round(2).astype(str),
            customer: rng.integers(12000, 12600, rows).astype(str),
            "Country": rng.choice(["United Kingdom", "France", "Germany", "EIRE", "Spain"], rows),
        }
    )


def test_a_customer_id_spelt_another_way_is_the_key_to_check_not_a_missing_one() -> None:
    advice = advise(context_for(_order_log(), "retail-win-back"))
    key = next(p for p in advice.proposals if p.path == ROLE_PRIMARY_KEY)
    assert key.value == "CustomerID"
    assert key.confidence is AgentConfidence.CHECK  # an exact spelling would be "sure"
    assert "No column holds a different value on every row" not in (advice.stop_reason or "")


def test_the_exact_spelling_is_still_sure() -> None:
    advice = advise(context_for(_order_log(customer="customer_id"), "retail-win-back"))
    key = next(p for p in advice.proposals if p.path == ROLE_PRIMARY_KEY)
    assert (key.value, key.confidence) == ("customer_id", AgentConfidence.SURE)


def test_without_combining_the_stop_names_the_repeats_not_a_missing_id() -> None:
    advice = advise(context_for(_order_log(), "targeted-advertisement"))
    assert advice.stop_reason is not None
    assert "rows per customer on average" in advice.stop_reason


def test_invoice_numbers_and_stock_codes_are_never_turned_into_numbers() -> None:
    advice = advise(context_for(_order_log(), "retail-win-back"))
    parsed = {
        p.step.column
        for p in advice.proposals
        if p.kind is ProposalKind.RECIPE_STEP
        and p.step is not None
        and p.step.kind is RecipeStepKind.PARSE_NUMBER
    }
    assert "InvoiceNo" not in parsed and "StockCode" not in parsed
    assert {"Quantity", "UnitPrice"} <= parsed


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("InvoiceNo", True),
        ("StockCode", True),
        ("CustomerID", True),
        ("order_id", True),
        ("account_number", True),
        ("INVOICE_NO", True),
        ("amount_paid", False),
        ("is_valid", False),
        ("Quantity", False),
        ("spend_12m", False),
    ],
)
def test_a_code_is_named_by_the_last_word_of_its_header(name: str, code: bool) -> None:
    assert _looks_like_code(name) is code
