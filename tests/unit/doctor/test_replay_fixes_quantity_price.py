"""Found in the replay on an invoice log: quantity x price, the SQL answer's size, one data_check per subject,
one ledger finding for a plan-level stop however many trials said it."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pandas as pd
import pytest

from engine.access.roles import Principal, Role
from engine.agent.contracts import AgentConfidence, ProposalSource, ProposalState
from engine.doctor.case import CaseItem
from engine.doctor.diagnose import LoadedFile
from engine.doctor.registry import DataToolContext, DataToolError, data_registry
from engine.doctor.subjects import item_subject
from engine.doctor.tools_sql import MAX_OUTPUT_CHARS

PRINCIPAL = Principal(user_id="u", username="u", roles=frozenset({Role.ANALYST}), kind="user")
NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)


def _log() -> pd.DataFrame:
    return pd.DataFrame(
        [
            ("C1", "A1", "2025-06-20", 2, 3.0),
            ("C1", "A2", "2025-03-01", 1, 10.0),
            ("C2", "B1", "2025-06-01", 4, 2.5),
            ("C2", "B2", "2025-07-20", 9, 9.0),  # after the as-of date
        ],
        columns=["Customer", "Invoice", "InvoiceDate", "Quantity", "UnitPrice"],
    )


def _ask(**more: Any) -> dict[str, Any]:
    file = LoadedFile(upload_id="up-1", file_name="log.csv", frame=_log(), profile=cast(Any, None))
    ctx = DataToolContext(files=(file,), caller="ai", principal=PRINCIPAL)
    given = {
        "upload_id": "up-1",
        "entity_key": "Customer",
        "time_column": "InvoiceDate",
        "as_of": "2025-06-30",
        "order_column": "Invoice",
        "preset": "rfm_plus",
        **more,
    }
    return dict(data_registry().call("propose_features", given, ctx).output)


def test_quantity_and_price_make_the_amount() -> None:
    out = _ask(quantity_column="Quantity", price_column="UnitPrice")
    spend = {f["name"]: f for f in out["features"]}["spend_365d"]
    assert spend["name"] == "spend_365d"
    assert "Quantity x UnitPrice" in out["sentence"] or "Quantity x UnitPrice" in " ".join(out["notes"])


def test_missing_amount_says_to_give_quantity_and_price() -> None:
    with pytest.raises(DataToolError) as caught:
        _ask()
    assert caught.value.code == "FEATURE_NEEDS_COLUMN"
    assert "quantity_column" in caught.value.message and "price_column" in caught.value.message


def test_quantity_without_price_or_with_an_amount_is_refused() -> None:
    with pytest.raises(DataToolError) as one:
        _ask(quantity_column="Quantity")
    assert one.value.code == "FEATURE_NEEDS_COLUMN"
    with pytest.raises(DataToolError) as both:
        _ask(quantity_column="Quantity", price_column="UnitPrice", amount_column="Quantity")
    assert both.value.code == "FEATURE_NOT_VALID"


def test_propose_sql_step_has_its_own_output_budget() -> None:
    tool = data_registry().get("propose_sql_step")
    assert tool.max_output_bytes == MAX_OUTPUT_CHARS and MAX_OUTPUT_CHARS > 5_856


def _check(code: str, rows: int, file: str = "online.csv") -> CaseItem:
    payload: dict[str, Any] = {
        "upload_id": None,
        "code": code,
        "level": "flag",
        "column": None,
        "numbers": {"rows": rows, "file": file},
    }
    return CaseItem(
        item_id=f"data_check.base.{rows}",
        kind="data_check",
        title=f"{rows} rows are copies",
        source=ProposalSource.RULES,
        confidence=AgentConfidence.CHECK,
        state=ProposalState.PENDING,
        payload=payload,
        payload_digest=str(rows),
        proposed_at=NOW,
    )


def test_two_measurements_of_one_data_check_share_a_subject() -> None:
    a, b = _check("DUPLICATE_ROWS", 1771), _check("DUPLICATE_ROWS", 1700)
    assert item_subject(a) == item_subject(b) == "data_check@DUPLICATE_ROWS:online.csv"
    assert item_subject(_check("DUPLICATE_ROWS", 5, "other.csv")) != item_subject(a)
    assert item_subject(_check("REPEATED_HEADER_ROWS", 5)) != item_subject(a)
