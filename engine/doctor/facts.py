"""Client facts: what a person tells Data Doctor about their business once, kept per client.

"About 50,000 customers", "monthly churn is about 2%", "the financial year starts in April", "IDs are 8 digits".
Each fact is typed, says who stated it and when, carries a tolerance, and can be revoked. The Doctor compares every
new file with these facts (`engine.doctor.fingerprint`): a file that is far from what the person said is the first
sign of an old file, the wrong export or a broken join, and it is reported in plain words, never passed over.

Stored at `cases/<client_id>/client_facts.json`, beside the client's cases and standing answers. A fact holds a
number or a short word, never a customer.

Tolerance. For `customer_count` it is the share the file may differ by before the Doctor flags it (default 0.3 = 30%);
for `outcome_rate` it is the factor (default 2 = half to double). Beyond twice that distance the Doctor stops.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Sequence
from enum import StrEnum
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, Field, model_validator

from engine.config import StrictBase
from engine.doctor.case import CASE_PREFIX, CLIENT_ID_PATTERN
from engine.storage import Storage, StorageError
from engine.utils.time import utc_now

__all__ = [
    "FACTS_FILENAME",
    "ClientFact",
    "ClientFacts",
    "FactError",
    "FactInput",
    "FactKind",
    "FactStore",
    "facts_key",
]

FACTS_FILENAME: Final[str] = "client_facts.json"
DEFAULT_COUNT_TOLERANCE: Final[float] = 0.3
DEFAULT_RATE_FACTOR: Final[float] = 2.0
_MONTHS: Final[tuple[str, ...]] = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)
_LOCK: Final = threading.Lock()


class FactKind(StrEnum):
    CUSTOMER_COUNT = "customer_count"
    OUTCOME_RATE = "outcome_rate"
    ID_LENGTH = "id_length"
    FISCAL_YEAR_START = "fiscal_year_start"
    TIMEZONE = "timezone"
    CURRENCY = "currency"


class FactError(ValueError):
    """A fact that cannot be stored, with a plain sentence for the person."""


def facts_key(client_id: str) -> str:
    return f"{CASE_PREFIX}{client_id}/{FACTS_FILENAME}"


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise FactError("Give a number.")
    text = str(value).strip().replace(",", "")
    try:
        return float(text[:-1]) / 100 if text.endswith("%") else float(text)
    except ValueError:
        raise FactError(f"“{value}” is not a number.") from None


def normalise(kind: FactKind, value: object) -> int | float | str:
    """The value as it is stored: a count, a share (0-1), a length, a month number or a word."""
    if kind is FactKind.CUSTOMER_COUNT:
        count = _number(value)
        if count < 1 or count != int(count):
            raise FactError("The number of customers must be a whole number above zero.")
        return int(count)
    if kind is FactKind.OUTCOME_RATE:
        rate = _number(value)
        if not 0 < rate < 1:
            raise FactError("Give the rate as a share between 0 and 1, or with a % sign (2% or 0.02).")
        return rate
    if kind is FactKind.ID_LENGTH:
        length = _number(value)
        if length < 1 or length > 64 or length != int(length):
            raise FactError("An ID length is a whole number from 1 to 64.")
        return int(length)
    if kind is FactKind.FISCAL_YEAR_START:
        text = str(value).strip().lower()
        if text in _MONTHS:
            return _MONTHS.index(text) + 1
        month = _number(value)
        if month != int(month) or not 1 <= month <= 12:
            raise FactError("Give the month the financial year starts in (April, or 4).")
        return int(month)
    text = str(value).strip()
    if kind is FactKind.TIMEZONE:
        try:
            ZoneInfo(text)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            raise FactError(f"“{text}” is not a time zone name (for example Asia/Kolkata).") from None
        return text
    if re.fullmatch(r"[A-Za-z]{3}", text) is None:
        raise FactError("Give the currency as its three-letter code (INR, USD).")
    return text.upper()


class FactInput(StrictBase):
    """One fact the person states (or takes back)."""

    kind: FactKind
    value: int | float | str | None = Field(default=None, description="What they said; null when revoking.")
    goal_id: str | None = Field(default=None, description="For an outcome rate: which goal it is about.")
    approximate: bool = Field(default=True, description="'About' (the default) or exactly.")
    tolerance: float | None = Field(
        default=None, gt=0, description="See the module note; null = the default."
    )
    note: str = Field(default="", max_length=300, description="Their words, if they want them kept.")
    revoke: bool = Field(default=False, description="Take this fact back.")

    @model_validator(mode="after")
    def _complete(self) -> FactInput:
        if not self.revoke and self.value is None:
            raise ValueError("A fact needs a value.")
        if self.goal_id is not None and self.kind is not FactKind.OUTCOME_RATE:
            raise ValueError("Only an outcome rate is about one goal.")
        return self


class ClientFact(StrictBase):
    fact_id: str = Field(description="`<kind>` or `<kind>:<goal>`: one live fact each.")
    kind: FactKind
    goal_id: str | None = None
    value: int | float | str
    approximate: bool = True
    tolerance: float | None = None
    note: str = ""
    set_by: str = Field(description="Who stated it (user id).")
    set_at: AwareDatetime
    revoked_by: str | None = None
    revoked_at: AwareDatetime | None = None

    def band(self) -> float:
        """The tolerance in force: the stated one, else the default for the kind."""
        if self.tolerance is not None:
            return self.tolerance
        return DEFAULT_RATE_FACTOR if self.kind is FactKind.OUTCOME_RATE else DEFAULT_COUNT_TOLERANCE


class ClientFacts(StrictBase):
    """`cases/<client_id>/client_facts.json`."""

    client_id: str
    facts: tuple[ClientFact, ...] = ()


class FactStore:
    """The client's facts. One writer at a time within a process; each write is whole-file."""

    def __init__(self, storage: Storage) -> None:
        self._storage = storage

    def _read(self, client_id: str) -> ClientFacts:
        key = facts_key(client_id)
        try:
            if not self._storage.exists(key):
                return ClientFacts(client_id=client_id)
            return self._storage.read_model(key, ClientFacts)
        except (StorageError, ValueError):
            return ClientFacts(client_id=client_id)

    def list(self, client_id: str, *, include_revoked: bool = False) -> tuple[ClientFact, ...]:
        if re.fullmatch(CLIENT_ID_PATTERN, client_id) is None:
            return ()
        facts = self._read(client_id).facts
        return facts if include_revoked else tuple(f for f in facts if f.revoked_at is None)

    def get(self, client_id: str, kind: FactKind, goal_id: str | None = None) -> ClientFact | None:
        """The live fact of this kind (and goal for an outcome rate; a rate for no goal applies to every goal)."""
        live = [f for f in self.list(client_id) if f.kind is kind]
        exact = next((f for f in live if f.goal_id == goal_id), None)
        return exact or next((f for f in live if f.goal_id is None), None)

    def put(self, client_id: str, inputs: Sequence[FactInput], *, actor: str) -> tuple[ClientFact, ...]:
        """State or revoke facts. Everything is checked first, so a bad entry changes nothing."""
        at = utc_now()
        prepared: list[tuple[FactInput, int | float | str | None]] = []
        for entry in inputs:
            prepared.append((entry, None if entry.revoke else normalise(entry.kind, entry.value)))
        with _LOCK:
            document = self._read(client_id)
            facts = list(document.facts)
            for entry, value in prepared:
                fact_id = entry.kind.value + (f":{entry.goal_id}" if entry.goal_id else "")
                # An earlier statement stays in the history, revoked by the new one (or by the person).
                facts = [
                    (
                        f.model_copy(update={"revoked_at": at, "revoked_by": actor})
                        if f.fact_id == fact_id and f.revoked_at is None
                        else f
                    )
                    for f in facts
                ]
                if value is None:
                    continue
                facts.append(
                    ClientFact(
                        fact_id=fact_id,
                        kind=entry.kind,
                        goal_id=entry.goal_id,
                        value=value,
                        approximate=entry.approximate,
                        tolerance=entry.tolerance,
                        note=entry.note,
                        set_by=actor,
                        set_at=at,
                    )
                )
            self._storage.write_model(
                facts_key(client_id), document.model_copy(update={"facts": tuple(facts)})
            )
        return self.list(client_id)
