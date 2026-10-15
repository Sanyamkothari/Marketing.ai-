"""The few best customers of a contact list, for the plain results summary of a finished campaign run.

The contact list (`treat_list.csv`) is already sorted best first with each customer's score
(`engine.decide.treat_list`), so the top of the summary is the first rows marked "contact". This reads them by
column name from the CSV text and stops after `limit`, so a long list is never held in memory as a table.
"""

from __future__ import annotations

import csv
import io
from typing import Final

from engine.contracts import Artefact

__all__ = ["TopCustomer", "top_customers"]

_KEY_END: Final[str] = "use_case"  # the customer's key columns come first, up to this one
_TREATED_YES: Final[frozenset[str]] = frozenset({"1", "true", "True", "TRUE"})


class TopCustomer(Artefact):
    """One row of the summary's short list: who, how high the score is, and what they would get."""

    customer: str
    score: float | None = None
    priority: str | None = None
    offer: str | None = None
    channel: str | None = None
    reason: str | None = None


def _text(row: dict[str, str], *names: str) -> str | None:
    for name in names:
        value = (row.get(name) or "").strip()
        if value:
            return value
    return None


def _score(row: dict[str, str]) -> float | None:
    value = _text(row, "score")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def top_customers(csv_bytes: bytes, limit: int = 10) -> list[TopCustomer]:
    """The first `limit` customers to contact, in the list's own (best first) order. Empty when none is marked."""
    reader = csv.DictReader(io.TextIOWrapper(io.BytesIO(csv_bytes), encoding="utf-8-sig", newline=""))
    header = list(reader.fieldnames or [])
    keys = header[: header.index(_KEY_END)] if _KEY_END in header else header[:1]
    found: list[TopCustomer] = []
    for row in reader:
        if limit <= 0 or len(found) >= limit:
            break
        if (row.get("treat") or "").strip() not in _TREATED_YES:
            continue
        found.append(
            TopCustomer(
                customer=" / ".join(str(row.get(name) or "").strip() for name in keys) or "(no id)",
                score=_score(row),
                priority=_text(row, "band", "segment"),
                offer=_text(row, "offer"),
                channel=_text(row, "channel"),
                reason=_text(row, "reason_1"),
            )
        )
    return found
