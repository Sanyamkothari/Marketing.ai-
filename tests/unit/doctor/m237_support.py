"""Shared fixtures for the M237 unit tests: a small diagnosed set of files, and a builder that records calls."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from engine.doctor.contracts import (
    ColumnDrift,
    ColumnRename,
    Diagnosis,
    FileDiagnosis,
    FileLink,
    FileStack,
)

CUSTOMERS = 120


def file(upload_id: str, name: str, frame: pd.DataFrame, kind: str | None, **extra: Any) -> FileDiagnosis:
    values: dict[str, Any] = {
        "upload_id": upload_id,
        "file_name": name,
        "rows": len(frame),
        "columns": len(frame.columns),
        "kind": kind,
        "kind_label": kind or "unknown",
        "kind_reason": f"read as {kind}",
        "id_column": None,
        "rows_per_id": None,
        "date_columns": (),
        "personal_columns": 0,
        "fixable_columns": 0,
        "notes": (),
    }
    values.update(extra)
    return FileDiagnosis(**values)


def frames() -> dict[str, pd.DataFrame]:
    customers = pd.DataFrame(
        {
            "customer_id": [f"CUST-{n:05d}" for n in range(1, CUSTOMERS + 1)],
            "email": [f"person{n}@example.com" for n in range(1, CUSTOMERS + 1)],
        }
    )

    def bills(month: int, amount: str = "amount") -> pd.DataFrame:
        rows = [
            {"customer_id": n, "invoice_date": f"2025-{month:02d}-{day:02d}", amount: 100 + n}
            for n in range(1, CUSTOMERS + 1)
            for day in (3, 28)
        ]
        overlap = [
            {"customer_id": n, "invoice_date": f"2025-{month - 1:02d}-28", amount: 100 + n}
            for n in range(1, CUSTOMERS + 1)
            if month > 1
        ]
        return pd.DataFrame(overlap + rows)

    tickets = pd.DataFrame(
        {
            "cust_no": [f"98765{n:05d}" for n in range(1, 101)],
            "opened_at": ["2025-02-01"] * 100,
        }
    )
    return {
        "u_list": customers,
        "u_jan": bills(1),
        "u_feb": bills(2),
        "u_mar": bills(3, amount="bill_amount"),
        "u_phones": tickets,
    }


def diagnosis(loaded: Mapping[str, pd.DataFrame]) -> Diagnosis:
    files = (
        file("u_list", "customers.csv", loaded["u_list"], "entity", id_column="customer_id"),
        file(
            "u_jan",
            "bills_01.csv",
            loaded["u_jan"],
            "bills",
            id_column="customer_id",
            date_columns=("invoice_date",),
        ),
        file(
            "u_feb",
            "bills_02.csv",
            loaded["u_feb"],
            "bills",
            id_column="customer_id",
            date_columns=("invoice_date",),
        ),
        file(
            "u_mar",
            "bills_03.csv",
            loaded["u_mar"],
            "bills",
            id_column="customer_id",
            date_columns=("invoice_date",),
        ),
        file(
            "u_phones",
            "calls.csv",
            loaded["u_phones"],
            "complaints",
            id_column="cust_no",
            date_columns=("opened_at",),
        ),
    )
    stack = FileStack(
        upload_ids=("u_jan", "u_feb", "u_mar"),
        file_names=("bills_01.csv", "bills_02.csv", "bills_03.csv"),
        kind="bills",
        columns=("customer_id", "invoice_date"),
        rows=sum(len(loaded[u]) for u in ("u_jan", "u_feb", "u_mar")),
        drift=(
            ColumnDrift(
                upload_id="u_mar",
                file_name="bills_03.csv",
                renamed=(ColumnRename(before="amount", after="bill_amount"),),
            ),
        ),
        summary="",
    )
    link = FileLink(
        upload_id="u_jan",
        file_name="bills_01.csv",
        column="customer_id",
        list_upload_id="u_list",
        list_file_name="customers.csv",
        list_column="customer_id",
        coverage=0.0,
        summary="",
    )
    return Diagnosis(files=files, use_cases=(), links=(link,), stacks=(stack,))


@dataclass
class RecordingBuilder:
    """A `BuilderPort` that keeps what it was sent."""

    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    data: dict[str, pd.DataFrame] = field(default_factory=dict)

    def add_source(self, *, file_name: str, data: bytes, role: str) -> str:
        import io

        source_id = f"s{len(self.data) + 1}"
        self.data[source_id] = pd.read_csv(io.BytesIO(data), dtype=str)
        self.calls.append(("add_source", {"file_name": file_name, "role": role}))
        return source_id

    def suggest_mapping(self, *, source_id: str, use_case: str) -> Mapping[str, Any]:
        self.calls.append(("suggest_mapping", {"source_id": source_id}))
        return {
            "mapping_id": f"m_{source_id}",
            "columns": [
                {"source": "customer_id", "standard": "entity_key", "decided_by": "auto", "confidence": 0.9},
                {"source": "region", "standard": "region", "decided_by": "auto", "confidence": 0.9},
            ],
        }

    def save_mapping(
        self,
        *,
        mapping_id: str,
        source_id: str,
        use_case: str,
        role: str,
        columns: Sequence[Mapping[str, Any]],
    ) -> str:
        self.calls.append(("save_mapping", {"mapping_id": mapping_id, "columns": [dict(c) for c in columns]}))
        return mapping_id

    def save_spec(self, body: Mapping[str, Any]) -> str:
        self.calls.append(("save_spec", dict(body)))
        return "spec_1"

    def build(self, *, spec_id: str, mode: str) -> str:
        self.calls.append(("build", {"spec_id": spec_id, "mode": mode}))
        return "ds_1"
