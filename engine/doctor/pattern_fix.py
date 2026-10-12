"""New patterns, with permission (Plan O M245, DEC-1815; added at approval, [REQ] 11; the design §6.2, §7).

When Data Doctor's rules notice a value pattern no rule covers - `bilkul` / `nako` in a yes/no column, `nil` among
numbers - the AI may propose a fix. This module is everything between the AI's words and the person's yes:

* **A look at the column first** (:class:`PatternLook`). The rules read the column's spellings and counts (a look
  tool's result, cited by its evidence id) and only that goes to the AI, through the egress gate
  (`engine.agent.egress`): personal-data columns are never looked at, every spelling is masked as a cell value is,
  the rendered prompt is scanned again, and only spellings at least :data:`MIN_SHOWN` rows share are shown.
* **Only the fixed step kinds** (:data:`PATTERN_FIX_KINDS`). The AI answers with one step kind and its parameters
  (:class:`PatternSuggestion`), never code. A kind outside the list, or parameters the recipe refuses, is not a fix.
* **Tried on 1,000 rows** (:func:`try_pattern`): the step runs alone on a copy of the file's first rows; the result
  is shown as before -> after per spelling, with the rows it changed and the values it could not map.
* **O10's change-share guard and never ticked**: the PROPOSE tool `propose_pattern_fix` (`engine.doctor.tools_notice`)
  returns the trial as a pending, unticked proposal the data policy decides (`data.propose_pattern_fix`: grounded,
  tried, the change share, never ticked). Only the person's answer applies it, recorded with their user id; an
  approved fix counts towards a candidate rule (`engine.doctor.patterns`) and is remembered for the client as a
  standing answer (`engine.doctor.standing`).
* **"I can't do this safely yet"** (:func:`record_tool_request`): when the AI says no step kind can express the fix,
  or names one that is not allowed, nothing is proposed; the need is written down for the product team
  (`doctor_patterns/tool_requests.json`, deployment-wide like the patterns: the need in plain words, scrubbed, with
  counts, client ids and places - never a row or a person's id).

The AI is optional: with no AI service connected nothing here is called and the rules' own question stands.
Proposals are cached by the look's evidence id and the prompt version (the design §6.4), so reloading a case shows the
same suggestion and a model is asked once per look.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

import pandas as pd
from pydantic import AwareDatetime, Field, JsonValue, ValidationError

from engine.agent.config import AgentLevel, DataAccess
from engine.agent.contracts import RecipeStep, RecipeStepKind
from engine.config import StrictBase
from engine.doctor.case import payload_digest
from engine.storage import Storage, StorageError
from engine.utils.logging import get_logger

__all__ = [
    "CANNOT_SAFELY",
    "MIN_SHOWN",
    "PATTERN_FIX_KINDS",
    "PROMPT_NAME",
    "TOOL_REQUESTS_KEY",
    "BeforeAfter",
    "PatternAI",
    "PatternLook",
    "PatternSuggestion",
    "PatternTrial",
    "ToolRequest",
    "ToolRequests",
    "CachedSuggestion",
    "cache_key",
    "cached_suggestion",
    "parse_suggestion",
    "look_at",
    "meter_pattern_ai",
    "record_tool_request",
    "tool_requests",
    "try_pattern",
    "unusable_reason",
]

_LOG = get_logger(__name__)
_LOCK: Final = threading.Lock()

PATTERN_FIX_KINDS: Final[tuple[RecipeStepKind, ...]] = (
    RecipeStepKind.MAP_BOOLEAN,
    RecipeStepKind.MAP_VALUES,
    RecipeStepKind.SET_MISSING_TEXT,
    RecipeStepKind.NORMALISE_TEXT,
    RecipeStepKind.PARSE_NUMBER,
    RecipeStepKind.PARSE_DATE,
    RecipeStepKind.CONVERT_UNITS,
)
"""The step kinds an AI-proposed fix of a value pattern may use: each rewrites one column's values, row by row, from
frozen parameters. Never a row filter, a new column, an expression or a currency rate."""

CANNOT_SAFELY: Final[str] = "I can't do this safely yet"
MIN_SHOWN: Final[int] = 2
"""A spelling is shown to the AI only when at least this many rows hold it (a one-off value may name a person)."""
MAX_LOOK_VALUES: Final[int] = 40
MAX_PAIRS: Final[int] = 12
MAX_TEXT: Final[int] = 40
TOOL_REQUESTS_KEY: Final[str] = "doctor_patterns/tool_requests.json"
PROMPT_NAME: Final[str] = "data_pattern_fix"
PROMPT_VERSION: Final[str] = "data_pattern_fix.v1"


class PatternLook(StrictBase):
    """What the AI is shown of one column: its spellings and counts (masked), and what the rules noticed."""

    upload_id: str
    column: str
    detector: str = Field(description="The rules' finding (`UNKNOWN_BOOLEAN`, `UNREADABLE_VALUES`).")
    rows: int = Field(ge=0, description="Rows of the file.")
    non_empty: int = Field(ge=0)
    values: tuple[tuple[str, int], ...] = Field(
        default=(), description="Spellings held by at least `MIN_SHOWN` rows, most common first, with counts."
    )
    unknown: tuple[str, ...] = Field(default=(), description="The spellings no rule has a meaning for.")
    evidence_id: str | None = Field(default=None, description="The look tool's result this look cites.")


class PatternSuggestion(StrictBase):
    """The AI's answer: one step of a fixed kind with its parameters, or what it would need (`cannot`)."""

    kind: str | None = None
    params: dict[str, JsonValue] = Field(default_factory=dict)
    reason: str = ""
    cannot: str | None = Field(default=None, description="No step kind can do it: what is needed, in words.")


PatternAI = Callable[[PatternLook], "PatternSuggestion | None"]
"""The AI service's side: a suggestion for a look, or None when it gave none (no service, a budget or parse error)."""


class BeforeAfter(StrictBase):
    before: str
    after: str | None = Field(description="What the step makes of it; null = empty (not read).")
    rows: int = Field(ge=0)


class PatternTrial(StrictBase):
    """What the step did on the first rows of the file."""

    rows_tried: int = Field(ge=0)
    changed: int = Field(ge=0, description="Values it changed.")
    failed: int = Field(ge=0, description="Non-empty values it could not read.")
    out_of: int = Field(ge=0, description="Non-empty values of the column in the rows tried.")
    before_after: tuple[BeforeAfter, ...] = ()
    unmapped: tuple[str, ...] = Field(default=(), description="Values it could not map (masked, cut).")
    unmapped_rows: int = Field(default=0, ge=0)


def _text(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _empty(value: Any) -> bool:
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _cut(text: str) -> str:
    from engine.agent.formats import masked_cut

    return masked_cut(text, MAX_TEXT)


def look_at(
    frame: pd.DataFrame,
    column: str,
    *,
    upload_id: str,
    detector: str,
    unknown: Sequence[str] = (),
    evidence_id: str | None = None,
) -> PatternLook:
    """The look the AI is shown: the column's spellings at least :data:`MIN_SHOWN` rows share, with counts."""
    series = frame[column]
    present = series.dropna().map(_text)
    present = present[present != ""]
    counts = present.value_counts()
    shown = tuple((str(v), int(n)) for v, n in counts.items() if int(n) >= MIN_SHOWN)[:MAX_LOOK_VALUES]
    return PatternLook(
        upload_id=upload_id,
        column=column,
        detector=detector,
        rows=len(frame),
        non_empty=len(present),
        values=shown,
        unknown=tuple(unknown),
        evidence_id=evidence_id,
    )


def unusable_reason(suggestion: PatternSuggestion, column: str) -> str | None:
    """Why a suggestion cannot be tried as a fix (it then becomes a tool request), or None when it can."""
    if suggestion.cannot:
        return suggestion.cannot
    if suggestion.kind is None:
        return "The AI named no fix."
    allowed = {kind.value for kind in PATTERN_FIX_KINDS}
    if suggestion.kind not in allowed:
        return f"The AI asked for a '{suggestion.kind[:40]}' change, which is not one of the fixes it may suggest."
    from engine.agent.recipe import RecipeError, check_recipe

    try:
        step = RecipeStep(order=1, kind=RecipeStepKind(suggestion.kind), column=column, params=suggestion.params)
        check_recipe([step], columns=[column], primary_key=None, target=None, levels=tuple(AgentLevel))
    except (RecipeError, ValidationError, ValueError) as exc:
        return f"The AI's {suggestion.kind} step is not valid: {str(exc)[:160]}"
    return None


def try_pattern(
    frame: pd.DataFrame, column: str, kind: RecipeStepKind, params: Mapping[str, Any], *, trial_rows: int = 1_000
) -> PatternTrial:
    """Run the step alone on a copy of the first `trial_rows` rows; before -> after per spelling.

    Raises `RecipeError` when the step cannot run on the column at all."""
    from engine.agent.recipe import run_recipe

    sample = frame.head(trial_rows)
    step = RecipeStep(order=1, kind=kind, column=column, params=dict(params))
    run = run_recipe(
        sample,
        [step],
        upload_id="pattern-trial",
        primary_key=None,
        target=None,
        levels=tuple(AgentLevel),
        max_failure_pct=100.0,
    )
    receipt = run.receipt.steps[0]
    before = sample[column]
    after = run.frame[column]
    pairs: dict[str, tuple[str | None, int]] = {}
    unmapped: dict[str, int] = {}
    listed = _listed(kind, params)
    for old, new in zip(before.tolist(), after.tolist(), strict=True):
        if _empty(old):
            continue
        key = _text(old)
        if key == "":
            continue
        shown = None if _empty(new) else _text(new)
        known = pairs.get(key)
        pairs[key] = (shown, (known[1] if known else 0) + 1)
        not_read = shown is None and kind is not RecipeStepKind.SET_MISSING_TEXT
        not_listed = listed is not None and key.casefold() not in listed
        if not_read or not_listed:
            unmapped[key] = unmapped.get(key, 0) + 1
    ordered = sorted(pairs.items(), key=lambda item: (-item[1][1], item[0]))[:MAX_PAIRS]
    return PatternTrial(
        rows_tried=len(sample),
        changed=receipt.changed,
        failed=receipt.failed,
        out_of=int(before.notna().sum()),
        before_after=tuple(
            BeforeAfter(before=_cut(old), after=None if new is None else _cut(new), rows=n)
            for old, (new, n) in ordered
        ),
        unmapped=tuple(_cut(v) for v in sorted(unmapped, key=lambda v: (-unmapped[v], v))[:MAX_PAIRS]),
        unmapped_rows=sum(unmapped.values()),
    )


def _listed(kind: RecipeStepKind, params: Mapping[str, Any]) -> set[str] | None:
    """The spellings a mapping step lists (casefolded); None for a step that reads every value."""
    if kind is RecipeStepKind.MAP_BOOLEAN:
        values = [*params.get("true_values", []), *params.get("false_values", [])]
        return {str(v).strip().casefold() for v in values}
    if kind is RecipeStepKind.MAP_VALUES:
        mapping = params.get("mapping", {})
        return {str(k).strip().casefold() for k in mapping} | {str(v).strip().casefold() for v in mapping.values()}
    return None


# ---------------------------------------------------------------------------
# The AI service's side
# ---------------------------------------------------------------------------
def cache_key(client_id: str, look: PatternLook, signature: Sequence[str]) -> str:
    """`cases/<client_id>/notice_ai/<digest>.json`: per client, by the pattern - the detector, the column (case and
    punctuation aside) and the values it is about - and the prompt version (the design §6.4's evidence digest)."""
    from engine.doctor.case import CASE_PREFIX

    digest = payload_digest(
        {
            "detector": look.detector,
            "column": " ".join(look.column.split()).casefold(),
            "values": sorted(signature),
            "prompt": PROMPT_VERSION,
        }
    )
    return f"{CASE_PREFIX}{client_id}/notice_ai/{digest[:24]}.json"


class CachedSuggestion(StrictBase):
    suggestion: PatternSuggestion
    asked_at_rows: int = Field(default=0, ge=0, description="Rows of the file the look was made on.")


def cached_suggestion(
    storage: Storage | None,
    ai: PatternAI | None,
    look: PatternLook,
    *,
    client_id: str,
    signature: Sequence[str],
) -> PatternSuggestion | None:
    """The AI's suggestion for this client's pattern, asked once and kept (`notice_ai/<digest>.json`): a reload, and
    next month's file with the same pattern, show the same suggestion, even with no AI service connected then, so
    the client's standing answer to it still applies (the design §6.4). An answer the AI did not give is not kept."""
    key = cache_key(client_id, look, signature) if storage is not None else None
    if storage is not None and key is not None:
        try:
            if storage.exists(key):
                return storage.read_model(key, CachedSuggestion).suggestion
        except (StorageError, ValueError):
            pass
    if ai is None:
        return None
    suggestion = ai(look)
    if suggestion is not None and storage is not None and key is not None:
        try:
            storage.write_model(key, CachedSuggestion(suggestion=suggestion, asked_at_rows=look.rows))
        except (StorageError, OSError):
            _LOG.warning("pattern_fix.cache_write_failed")
    return suggestion


def _prompt_payload(look: PatternLook, mode: DataAccess) -> str:
    from engine.agent.egress import Egress, assert_clean

    gate = Egress.build([look.column], mode=mode)
    prepared = gate.prepare(
        {
            "column": look.column,
            "noticed": look.detector,
            "rows": look.rows,
            "non_empty": look.non_empty,
            "values": [{"value": value, "rows": rows} for value, rows in look.values],
            "unknown": list(look.unknown),
            "kinds": [kind.value for kind in PATTERN_FIX_KINDS],
        }
    )
    return assert_clean(json.dumps(prepared, ensure_ascii=False, sort_keys=True))[0]


def parse_suggestion(text: str) -> PatternSuggestion | None:
    """The AI's JSON answer as a suggestion; None when it is not one."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        raw = json.loads(text[start : end + 1])
        if not isinstance(raw, dict):
            return None
        params = raw.get("params") or {}
        return PatternSuggestion(
            kind=str(raw["kind"]) if isinstance(raw.get("kind"), str) else None,
            params=params if isinstance(params, dict) else {},
            reason=str(raw.get("reason") or "")[:300],
            cannot=str(raw["cannot"])[:300] if isinstance(raw.get("cannot"), str) and raw["cannot"] else None,
        )
    except (ValueError, ValidationError, TypeError):
        return None


def meter_pattern_ai(meter: Any, config_root: Path | None = None, *, mode: DataAccess = DataAccess.MASKED_DATA) -> PatternAI:
    """The AI's side over the metered AI service (`DATA_PATTERN_FIX`): the look, masked by the egress gate, in; one
    suggestion out. An AI service or budget error is no suggestion (the rules' question stands)."""
    from engine.generative.contracts import GenerativePurpose
    from engine.generative.prompts import load_prompt, render

    def suggest(look: PatternLook) -> PatternSuggestion | None:
        prompt = render(load_prompt(PROMPT_NAME, config_root), {"column_look": _prompt_payload(look, mode)})
        try:
            text = str(meter.complete(prompt, GenerativePurpose.DATA_PATTERN_FIX).text)
        except Exception:  # an AI service or budget error: no suggestion
            return None
        return parse_suggestion(text)

    return suggest


# ---------------------------------------------------------------------------
# Tool requests: what no step kind can do yet
# ---------------------------------------------------------------------------
class ToolRequest(StrictBase):
    request_id: str
    detector: str = Field(description="What the rules noticed.")
    need: str = Field(description="What the fix would need, in plain words (scrubbed).")
    count: int = Field(default=0, ge=0, description="Times it was needed.")
    places: tuple[str, ...] = Field(default=(), description="Cases it was needed in (most recent last).")
    clients: tuple[str, ...] = Field(default=(), description="Clients it was needed for.")
    first_at: AwareDatetime
    last_at: AwareDatetime


class ToolRequests(StrictBase):
    requests: tuple[ToolRequest, ...] = ()


def _read_requests(storage: Storage) -> ToolRequests:
    try:
        if storage.exists(TOOL_REQUESTS_KEY):
            return storage.read_model(TOOL_REQUESTS_KEY, ToolRequests)
    except (StorageError, ValueError):
        _LOG.warning("tool_requests.unreadable; starting a new list")
    return ToolRequests()


def record_tool_request(
    storage: Storage, *, detector: str, need: str, place: str, client_id: str, at: datetime
) -> ToolRequest | None:
    """Write down that a fix was needed that no step kind can do; once per place. Never raises."""
    from engine.agent.egress import scrub

    text = " ".join(scrub(need).split())[:300] or "A change no fix can make yet."
    request_id = "tr_" + payload_digest({"detector": detector, "need": text.casefold()})[:20]
    try:
        with _LOCK:
            document = _read_requests(storage)
            found = {r.request_id: r for r in document.requests}
            current = found.get(request_id) or ToolRequest(
                request_id=request_id, detector=detector, need=text, first_at=at, last_at=at
            )
            if place not in current.places:
                current = current.model_copy(
                    update={
                        "count": current.count + 1,
                        "places": (*current.places, place)[-200:],
                        "clients": tuple(sorted({*current.clients, client_id})),
                        "last_at": at,
                    }
                )
                found[request_id] = current
                storage.write_model(
                    TOOL_REQUESTS_KEY,
                    ToolRequests(requests=tuple(sorted(found.values(), key=lambda r: r.request_id))),
                )
            return current
    except (StorageError, OSError, ValueError) as exc:
        _LOG.warning("tool_requests.record_failed error=%s", type(exc).__name__)
        return None


def tool_requests(storage: Storage) -> tuple[ToolRequest, ...]:
    """Every need written down, most needed first (the product team's list)."""
    return tuple(sorted(_read_requests(storage).requests, key=lambda r: (-r.count, r.request_id)))


def share_pct(changed: int, out_of: int) -> float:
    return 0.0 if out_of <= 0 else round(changed / out_of * 100.0, 4)
