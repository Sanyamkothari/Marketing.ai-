"""A query or a feature set may read a table another query built (`built:<name>`), as the look tools read `T1`.."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, cast

import pandas as pd
import pytest

from engine.access.roles import Principal, Role
from engine.agent.contracts import ProposalState
from engine.doctor.case import CaseItem
from engine.doctor.diagnose import LoadedFile
from engine.doctor.registry import DataToolContext, DataToolError, data_registry

PRINCIPAL = Principal(user_id="u", username="u", roles=frozenset({Role.ANALYST}), kind="user")
NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
AS_OF = date(2025, 6, 30)
ROWS = pd.DataFrame(
    {
        "Customer": ["C1", "C1", "C2", "C2", "C3"],
        "InvoiceDate": ["2025-06-20", "2025-03-01", "2025-06-01", "2025-05-01", "2025-06-10"],
        "Amount": [60.0, 50.0, 20.0, -5.0, 10.0],
    }
)
FIRST = LoadedFile(upload_id="up-1", file_name="invoices.csv", frame=ROWS, profile=cast(Any, None))


def _ctx(state: ProposalState = ProposalState.PENDING, **more: Any) -> DataToolContext:
    first = (
        data_registry()
        .call(
            "propose_sql_step",
            {
                "description": "Keep the paid invoices.",
                "sql": "SELECT * FROM t_invoices WHERE Amount > 0",
                "output_name": "paid",
                "upload_ids": ["up-1"],
            },
            DataToolContext(files=(FIRST,), caller="ai", principal=PRINCIPAL),
        )
        .output
    )
    item = CaseItem.model_validate(
        {
            "item_id": "q1",
            "kind": "recipe_step",
            "source": "ai",
            "confidence": "check",
            "state": state.value,
            "upload_id": None,
            "payload": {"step": first["step"]},
            "payload_digest": "q1",
            "proposed_at": NOW,
        }
    )
    return DataToolContext(
        files=(FIRST,), caller="ai", principal=PRINCIPAL, case_items=(item,), as_of=AS_OF, **more
    )


@pytest.mark.parametrize("state", [ProposalState.PENDING, ProposalState.ACCEPTED])
def test_a_query_reads_a_table_another_query_built(state: ProposalState) -> None:
    out = (
        data_registry()
        .call(
            "propose_sql_step",
            {
                "description": "Total per customer.",
                "sql": "SELECT Customer, sum(Amount) AS total FROM t_paid GROUP BY Customer",
                "output_name": "totals",
                "upload_ids": ["built:paid"],
            },
            _ctx(state),
        )
        .output
    )
    assert [(t["name"], t["rows"], t["columns"]) for t in out["inputs"]] == [("paid", 4, 3)]
    assert out["rows_out"] == 3 and out["columns"] == ["Customer", "total"]
    assert out["step"]["params"]["inputs"] == ["paid"] and "this_table" not in out["step"]["params"]


def test_a_query_over_a_table_that_is_not_built_is_refused() -> None:
    with pytest.raises(DataToolError) as caught:
        data_registry().call(
            "propose_sql_step",
            {
                "description": "x",
                "sql": "SELECT * FROM t_nope",
                "output_name": "x",
                "upload_ids": ["built:nope"],
            },
            _ctx(),
        )
    assert caught.value.status == 422


def test_features_are_built_on_a_built_table() -> None:
    out = (
        data_registry()
        .call(
            "propose_features",
            {
                "upload_id": "built:paid",
                "entity_key": "Customer",
                "time_column": "InvoiceDate",
                "preset": "rfm",
                "amount_column": "Amount",
            },
            _ctx(),
        )
        .output
    )
    # the refunded invoice is gone from the built table, so C2 has one invoice of 20
    assert out["rows_out"] == 3 and out["state"] == "pending" and out["ticked"] is False
    assert out["rows_in"] == 4 and [(t["name"], t["rows"]) for t in out["inputs"]] == [("paid", 4)]
    by_name = {f["name"]: f for f in out["features"]}
    # on the raw file C2 would have two invoices in 90 days and spend 15 (the median of 110, 15, 10)
    assert by_name["spend_365d"]["median"] == 20.0  # 110, 20, 10
    assert by_name["orders_90d"]["state"] == "constant"  # one paid invoice each in the last 90 days
