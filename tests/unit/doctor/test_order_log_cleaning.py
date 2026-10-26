"""No cleaning step turns the shopper or the order number of an order log into numbers (v3 phase 0, B3).

Today the advisor proposes "Turn 'InvoiceNo' into numbers", ticked as sure, which blanks every `C...` number; Phase A
taught it names such as `InvoiceNo` and `StockCode`. Here the guard goes by the person's choice, not by a name: the
column they chose as the shopper, and every column the file reads as an order number, are never read as numbers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd

from engine.agent.contracts import AgentConfidence, RecipeStep, RecipeStepKind
from engine.config import load_use_case
from engine.doctor import visit
from engine.doctor.cleaning import _clean
from engine.doctor.order_log import key_columns
from tests.unit.doctor.order_log_support import USE_CASE, loaded

NOW = datetime(2026, 10, 26, tzinfo=UTC)
ROWS = 600


def log() -> pd.DataFrame:
    """A log whose shopper and order columns are digits written as text, under names no code list knows."""
    rng = np.random.default_rng(5)
    return pd.DataFrame(
        {
            "Shopper": [f"{10_000 + int(n)}" for n in rng.integers(0, 120, ROWS)],
            "OrderNo2": [f"{500_000 + n // 3}" for n in range(ROWS)],
            "Basket": [f"{70_000 + n // 3}" for n in range(ROWS)],
            "Sent": pd.date_range("2011-01-03", periods=ROWS, freq="6h").strftime("%Y-%m-%d"),
            "Units": rng.integers(1, 20, ROWS),
        }
    )


def case_with(*items: Any) -> Any:
    return SimpleNamespace(items=list(items))


def chosen(upload_id: str, column: str) -> Any:
    return visit._item(
        "order_log_customer",
        {"upload_id": upload_id, "column": column},
        title=f"“{column}” names the shopper",
        upload_id=upload_id,
        confidence=AgentConfidence.SURE,
        evidence=(),
        now=NOW,
        ticked=True,
    )


def parsed_columns(items: list[Any]) -> set[str]:
    steps = [RecipeStep.model_validate(item.payload["step"]) for item in items if item.kind == "recipe_step"]
    return {step.column for step in steps if step.kind is RecipeStepKind.PARSE_NUMBER}


def test_the_chosen_shopper_and_the_order_numbers_are_never_read_as_numbers(config_root: Path) -> None:
    config = load_use_case(USE_CASE, config_root)
    file = loaded(log(), "orders.csv", config_root, "u_1")
    before, _ = _clean(case_with(), file, config, config_root, NOW)
    # Without a choice nothing is held: the advisor reads digits written as text as numbers (the control).
    assert {"Shopper", "OrderNo2", "Basket"} <= parsed_columns(before)
    after, asks = _clean(case_with(chosen("u_1", "Shopper")), file, config, config_root, NOW)
    held = parsed_columns(after)
    assert (
        "Shopper" not in held and "OrderNo2" not in held
    ), "the chosen shopper and the order number stay text"
    assert "Basket" in held, "a column nobody chose or named as an order number is cleaned as before"
    assert not [a for a in asks if a.column in {"Shopper", "OrderNo2"}]
    other = _clean(case_with(chosen("u_other", "Shopper")), file, config, config_root, NOW)[0]
    assert "Shopper" in parsed_columns(other), "a choice about another file holds nothing here"


def test_the_held_columns_are_the_choice_and_every_order_number() -> None:
    frame = log().assign(CustomerID=1)
    file = loaded(frame, "orders.csv", Path("configs"), "u_1")
    assert key_columns(file, "Shopper") == frozenset({"Shopper", "OrderNo2"})


def test_an_order_number_is_held_even_when_most_orders_have_one_line() -> None:
    frame = log().assign(InvoiceNo=[f"{536_000 + n}" for n in range(ROWS)])  # one line per order: no repeats
    file = loaded(frame, "orders.csv", Path("configs"), "u_1")
    assert {"InvoiceNo", "OrderNo2"} <= key_columns(file, "Shopper")
    assert "Basket" not in key_columns(file, "Shopper"), "a name that is not an order number's is not held"
