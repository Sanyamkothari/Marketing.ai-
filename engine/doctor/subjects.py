"""What a proposal is about: its subject (F5 of `docs/plans/DATA_DOCTOR_ROOT_CAUSES.md`).

Two proposals with one subject are two answers to one matter, and only one of them can stand: `outcome@goal` (which
column says the outcome), `sql@name` (the query that makes the table of that name), `key@file` (which column names
each row of the file), `fix@column:kind` (how a column is read), `question@CODE:column` (a question about a
matter), `data_check@code` (a doubt about a matter), `setting@goal:name` (the value of one of a goal's settings) and
`withdraw@item` (taking one item out of the plan). The reconcile store (`engine.doctor.reconcile`) adds, supersedes
or takes back proposals by subject.

The subject is derived from `kind` and `payload`, never typed in by a producer, so a case stored before this module
existed has the same subjects as one written now (`CaseItem.subject` is a cache of this, filled when a proposal
is stored). A proposal this module has no matter for gets its own id as the subject: it is never taken to compete
with another. Names only: nothing here reads a cell.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from engine.agentic.canonical import canonical_bytes, sha256_hex
from engine.doctor.case import CaseDoubt, CaseItem, CaseQuestion

__all__ = [
    "SETTING_KIND",
    "WITHDRAW_KIND",
    "doubt_subject",
    "item_subject",
    "options_digest",
    "question_subject",
]

WITHDRAW_KIND: Final[str] = "withdraw"
"""The item kind that asks the person to take another item out of the plan (`payload.item_id`, with its consequence)."""
SETTING_KIND: Final[str] = "goal_setting"
"""The item kind that sets one of a goal's settings (`payload.goal_id`, `setting`, `value`)."""

_GOAL_KINDS: Final[Mapping[str, str]] = {
    "outcome_column": "outcome",
    "as_of_column": "as_of",
    "consent_column": "consent",
}
"""Item kinds that decide one thing for a goal: one column is the outcome, the date, the consent column."""

_SETTERS: Final[frozenset[str]] = frozenset(
    {
        "parse_number",
        "parse_date",
        "map_boolean",
        "normalise_text",
        "convert_units",
        "convert_currency",
        "normalise_key",
    }
)
"""Typed fixes that say how one column is read: a column is read one way, so a second reading of it competes with
the first. Other steps (leaving rows out, correcting cells, filling) add up, so each is its own matter."""


def _digest(value: Any) -> str:
    return sha256_hex(canonical_bytes(value))[:12]


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def item_subject(item: CaseItem) -> str:
    """The matter `item` is about; `item.subject` when it was stored."""
    if item.subject:
        return item.subject
    payload = item.payload
    goal = item.goal_id or "base"
    if item.kind in _GOAL_KINDS:
        return f"{_GOAL_KINDS[item.kind]}@{goal}"
    if item.kind == SETTING_KIND:
        return f"setting@{_text(payload.get('goal_id')) or goal}:{_text(payload.get('setting')) or '-'}"
    if item.kind == WITHDRAW_KIND:
        return f"withdraw@{_text(payload.get('item_id')) or item.item_id}"
    if item.kind == "key":
        return f"key@{item.upload_id or _text(payload.get('upload_id')) or 'case'}"
    if item.kind == "data_check" and _text(payload.get("code")):
        numbers = payload.get("numbers")
        named = numbers.get("file") if isinstance(numbers, dict) else None
        where = _text(payload.get("upload_id")) or _text(named) or "case"
        return f"data_check@{payload['code']}:{where}" + (
            f":{payload['column']}" if _text(payload.get("column")) else ""
        )
    step = payload.get("step")
    if isinstance(step, dict):
        kind = str(step.get("kind"))
        given = step.get("params")
        params: dict[str, Any] = given if isinstance(given, dict) else {}
        if kind == "sql_query":
            name = _text(params.get("output_name")) or _text(step.get("column"))
            if name:
                return f"sql@{name}"
        column = _text(step.get("column"))
        where = f"#{item.upload_id}" if item.upload_id else ""
        if column and kind in _SETTERS:
            return f"fix@{column}:{kind}{where}"
        # Every other step adds to the plan: what it does (without its wording) is what it is.
        body = {k: v for k, v in step.items() if k not in {"reason", "order", "proposal_id", "decided_by"}}
        return f"step@{kind}:{_digest([item.upload_id, body])}"
    return f"{item.kind}@{item.item_id}"


def question_subject(question: CaseQuestion) -> str:
    """`question@CODE:column`, with the file and the goal it is about."""
    where = f"#{question.upload_id}" if question.upload_id else ""
    goal = f"/{question.goal_id}" if question.goal_id else ""
    return f"question@{question.code}:{question.column or '-'}{where}{goal}"


def doubt_subject(doubt: CaseDoubt) -> str:
    """`data_check@code`: a rules' doubt is named `d.<code>.<goal>`; any other doubt is about its own text."""
    if doubt.doubt_id.startswith("d.") and doubt.doubt_id.count(".") >= 1:
        return f"data_check@{doubt.doubt_id[2:]}"
    return f"data_check@{doubt.item_id or doubt.upload_id or 'case'}:{doubt.doubt_id}"


def options_digest(question: CaseQuestion) -> str:
    """What a question offers, whatever the order of its buttons: a question whose options changed is another
    question to the person who has not answered it."""
    offered = sorted(
        (o.label, o.effect_digest or "", o.value_kind or "", bool(o.safe)) for o in question.options
    )
    return _digest(offered)
