"""New patterns, with permission, then rules (Plan O M246, DEC-1816; added at approval, [REQ] 11).

When the AI meets a value pattern no rule covers (`haa` / `nahi` in a yes/no column) it may propose a fix - one of
the fixed step kinds, grounded in a look at the column, tried on 1,000 rows, held to O10's change-share guard and
never ticked (M233's `data.recipe_step` rule set). Only the person's approval applies it. This module is the other
half: **when the same AI-proposed pattern is approved again and again, the product team hears of it** as a
candidate rule (a lexicon entry, a unit word), to be promoted into the deterministic rules by a reviewed change so
the AI is no longer needed for it. Nothing here changes a rule by itself.

A **pattern** is an approved AI step with its column left out: its kind and its parameters, normalised (spellings
case-folded and sorted), so `map_boolean {haa: yes, nahi: no}` on `active` in one client's file and on `is_member`
in another's are one pattern. Each approval is counted once per place it was approved in (a case or an upload),
with the client it came from. A pattern approved at least :data:`MIN_APPROVALS` times in at least
:data:`MIN_PLACES` places is a **candidate** (:func:`candidate_rules`); M248's promotion report reads them.

Stored at `doctor_patterns/approved.json` (deployment-wide, like the lexicons it feeds): kinds, parameters, counts,
client ids and case or upload ids - never a person's id and never a row. Recording never fails an approval.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any, Final

from pydantic import AwareDatetime, Field, JsonValue

from engine.config import StrictBase
from engine.doctor.case import payload_digest
from engine.storage import Storage, StorageError
from engine.utils.logging import get_logger

__all__ = [
    "MIN_APPROVALS",
    "MIN_PLACES",
    "PATTERNS_KEY",
    "ApprovedPattern",
    "ApprovedPatterns",
    "candidate_rules",
    "pattern_of",
    "record_approved",
]

_LOG = get_logger(__name__)
_LOCK: Final = threading.Lock()

PATTERNS_KEY: Final[str] = "doctor_patterns/approved.json"
MIN_APPROVALS: Final[int] = 3
MIN_PLACES: Final[int] = 2
MAX_PLACES: Final[int] = 200
"""Places kept per pattern (the count goes on; the oldest places are dropped from the list)."""


class ApprovedPattern(StrictBase):
    pattern_id: str
    kind: str = Field(description="The step kind.")
    params: dict[str, JsonValue] = Field(default_factory=dict, description="Its parameters, normalised.")
    approvals: int = Field(default=0, ge=0)
    places: tuple[str, ...] = Field(default=(), description="Cases or uploads it was approved in.")
    clients: tuple[str, ...] = Field(default=(), description="Clients it was approved for.")
    first_at: AwareDatetime
    last_at: AwareDatetime
    candidate: bool = Field(default=False, description="Approved often enough to become a rule (a review).")


class ApprovedPatterns(StrictBase):
    patterns: tuple[ApprovedPattern, ...] = ()


def _norm(value: Any) -> Any:
    if isinstance(value, str):
        return value.strip().casefold()
    if isinstance(value, list | tuple):
        items = [_norm(item) for item in value]
        if all(isinstance(item, str) for item in items):
            return sorted(set(items))
        return items
    if isinstance(value, Mapping):
        return {str(key).strip().casefold(): _norm(item) for key, item in sorted(value.items())}
    return value


def pattern_of(kind: str, params: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """(pattern id, normalised parameters) of a step, its column left out."""
    normalised = {str(key): _norm(value) for key, value in sorted(params.items())}
    return "pt_" + payload_digest({"kind": kind, "params": normalised})[:20], normalised


def _read(storage: Storage) -> ApprovedPatterns:
    try:
        if storage.exists(PATTERNS_KEY):
            return storage.read_model(PATTERNS_KEY, ApprovedPatterns)
    except (StorageError, ValueError):
        _LOG.warning("patterns.unreadable; starting a new list")
    return ApprovedPatterns()


def record_approved(
    storage: Storage,
    steps: Iterable[tuple[str, Mapping[str, Any]]],
    *,
    place: str,
    client_id: str,
    at: datetime,
) -> tuple[ApprovedPattern, ...]:
    """Count the approval of each AI-proposed step `(kind, params)` once for `place`; never raises."""
    wanted = [(*pattern_of(kind, params), kind) for kind, params in steps]
    if not wanted:
        return ()
    try:
        with _LOCK:
            document = _read(storage)
            patterns = {p.pattern_id: p for p in document.patterns}
            touched: list[ApprovedPattern] = []
            for pattern_id, params, kind in wanted:
                found = patterns.get(pattern_id) or ApprovedPattern(
                    pattern_id=pattern_id, kind=kind, params=params, first_at=at, last_at=at
                )
                if place in found.places:
                    touched.append(found)
                    continue
                places = (*found.places, place)[-MAX_PLACES:]
                clients = tuple(sorted({*found.clients, client_id}))
                approvals = found.approvals + 1
                updated = found.model_copy(
                    update={
                        "approvals": approvals,
                        "places": places,
                        "clients": clients,
                        "last_at": at,
                        "candidate": approvals >= MIN_APPROVALS and len(set(places)) >= MIN_PLACES,
                    }
                )
                patterns[pattern_id] = updated
                touched.append(updated)
            storage.write_model(
                PATTERNS_KEY,
                ApprovedPatterns(patterns=tuple(sorted(patterns.values(), key=lambda p: p.pattern_id))),
            )
            return tuple(touched)
    except (StorageError, OSError, ValueError) as exc:
        _LOG.warning("patterns.record_failed error=%s", type(exc).__name__)
        return ()


def candidate_rules(storage: Storage) -> tuple[ApprovedPattern, ...]:
    """The AI-proposed patterns approved often enough to become a deterministic rule, most approved first."""
    return tuple(
        sorted(
            (p for p in _read(storage).patterns if p.candidate),
            key=lambda p: (-p.approvals, p.pattern_id),
        )
    )
