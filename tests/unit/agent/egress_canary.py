"""The canary: fake personal data planted in every kind of cell and header, and a runner that puts a
whole chat session through the real `chat_turn`, capturing every prompt that would reach the model.

Everything planted here is obviously fake (`example.test`, the 4111 test card, 192.0.2.0/24). The
tests assert that none of it can be found in any prompt, in any stored `ChatMessage` or in any
`sent` preview - see `test_egress_canary.py` and `tests/integration/agent/test_egress_sessions.py`.
"""

from __future__ import annotations

import dataclasses
import itertools
import json
import re
from collections.abc import Callable, Iterable, Sequence
from typing import Any

import pandas as pd

from engine.agent.config import DataAccess
from engine.agent.contracts import AgentSession, ChatMessage, ChatRole
from engine.agent.egress import RESULT_KEYS, SAFE_KEYS, cap_sent
from engine.agent.loop import TurnResult, chat_turn
from engine.agent.session import start_session
from engine.agent.tools import TOOLS, AgentContext, Tool, ToolKind
from engine.config import BudgetConfig, LlmConfig
from engine.generative.budget import Meter
from engine.generative.guardrails import Guardrails, load_policy
from engine.llm import FakeLLMClient, FakeLLMMode, _helper_sections
from engine.utils.time import utc_now
from tests.unit.agent.helpers import context_for

EMAIL = "jane.roe@example.test"
PHONE = "+91 98765 43210"
PAN = "ABCDE1234F"
CARD = "4111 1111 1111 1111"
AADHAAR = "2345 6789 0123"
IBAN = "IN12 3456 7890"
IP = "192.0.2.44"
URL = "https://example.test/u?token=abc123def456ghi789"
TOKEN = (
    "zk_live_51HxAbCdEf123456789012345"  # secret-scan: allow (a fake key the masking tests plant on purpose)
)
NAME = "Jane Roe"
ADDRESS = "221B Baker Street"
HEADER_EMAIL = EMAIL  # a column whose *name* is personal data
HEADER_NUMBER = "123456789012"  # a column whose name is twelve digits
HEADER_PHONE = "+91 98765 43210 alt"

ID_BASE = 100_000_000_000
"""Customer ids are twelve digits (`100000000000`, `100000000007`, ...): an ID column is full of numbers."""

HARD_SECRETS: tuple[str, ...] = (
    EMAIL,
    "example.test",
    PHONE,
    "98765 43210",
    "9876543210",
    "+9198765",
    PAN,
    CARD,
    "4111 1111",
    "4111111111111111",
    AADHAAR,
    "2345 6789",
    "234567890123",
    IBAN,
    "3456 7890",
    IP,
    "192.0.2",
    URL,
    "abc123def456ghi789",
    TOKEN,
    "51HxAbCdEf",
    HEADER_NUMBER,
    str(ID_BASE),
    str(ID_BASE + 7),
    str(ID_BASE + 14),
    "joe.b@",
    "5500 0000 0000 0004",
    "3782 822463 10005",
    "2001:db8:",
)
"""Values a pattern can catch: they must be absent from everything in every mode."""

CELL_WORDS: tuple[str, ...] = ("Jane", "Roe", "Baker", "delivery to", "Please email", "Ring ")
"""Words that only appear in cells: no pattern can catch them, so only `always_hide_columns` or
`summaries_only` may keep them out of a prompt."""

HIDE_ALL_TEXT: tuple[str, ...] = ("notes", "feedback", "region_contact", "label", "type", "code")
"""The innocent-looking columns that hold names and addresses: what a person would put in `always_hide_columns`."""


def _cycle(items: Sequence[str], rows: int) -> list[str]:
    return [items[i % len(items)] for i in range(rows)]


def canary_frame(rows: int = 1_500) -> pd.DataFrame:
    """A file with fake personal data in an obviously personal column, in innocent-looking ones, in
    headers, and in the ID column."""
    ids = [str(ID_BASE + 7 * i) for i in range(rows)]
    return pd.DataFrame(
        {
            "customer_id": ids,
            "contact_email": _cycle([EMAIL, "joe.b@example.test", "ann.lee@example.test"], rows),
            "phone_number": _cycle([PHONE, "+91 91234 56789", "+91 99887 76655"], rows),
            "pan_number": _cycle([PAN, "PQRST5678U", "LMNOP4321Z"], rows),
            "name": _cycle([NAME, "Joe Bloggs", "Ann Lee"], rows),
            "notes": _cycle(
                [
                    f"{NAME} called from {PHONE} about order 12",
                    f"Please email {EMAIL}; card {CARD} was declined",
                    f"Aadhaar {AADHAAR} and IBAN {IBAN} were on the form",
                    f"Logged in from {IP} then opened {URL}",
                    f"Key {TOKEN} pasted into chat by {NAME} of {ADDRESS}",
                    "Great service, thanks",
                ],
                rows,
            ),
            "feedback": _cycle(
                [
                    f"{NAME} said the delivery to {ADDRESS} was late",
                    f"Ring {PHONE}",
                    "No comment",
                    f"{NAME} <{EMAIL}>",
                ],
                rows,
            ),
            "region_contact": _cycle([f"{NAME} <{EMAIL}>", "North", f"call {PHONE}", "South"], rows),
            "card_hint": _cycle([CARD, "5500 0000 0000 0004", "3782 822463 10005"], rows),
            "ip_address": _cycle([IP, "2001:db8:0:0:0:8a2e:370:7334", "198.51.100.7"], rows),
            "tracking_link": _cycle([URL, "https://example.test/o/1?session=zzzz9999yyyy"], rows),
            "api_key": _cycle([TOKEN, "pk_test_4eC39HqLyjWDarjtT1zdp7dc"], rows),
            "bank_ref": _cycle([IBAN, "GB82 WEST 1234 5698 7654 32"], rows),
            "label": _cycle([f"{NAME} (VIP)", "standard"], rows),
            "type": _cycle([f"{NAME} type", "standard"], rows),
            "code": _cycle([IBAN, "A-100"], rows),
            HEADER_EMAIL: _cycle(["x", "y", "z"], rows),
            HEADER_NUMBER: [i % 97 for i in range(rows)],
            HEADER_PHONE: _cycle(["a", "b"], rows),
            "monthly_spend": [round(20 + (i * 37) % 900 + (i % 7) / 10, 2) for i in range(rows)],
            "signup_date": [f"2024-{1 + i % 12:02d}-{1 + i % 27:02d}" for i in range(rows)],
            "converted_30d": [1 if (i * 13) % 10 < 3 else 0 for i in range(rows)],
        }
    )


def canary_context(
    *,
    mode: str = "masked_data",
    always_hide: Iterable[str] = (),
    rows: int = 1_500,
    frame: pd.DataFrame | None = None,
) -> AgentContext:
    """A context whose agent config has the given data-access mode and hidden columns, and room for a long chat."""
    ctx = context_for(canary_frame(rows) if frame is None else frame, target="converted_30d")
    agent = ctx.config.agent.model_copy(
        update={
            "ai_data_access": DataAccess(mode),
            "always_hide_columns": tuple(always_hide),
            "max_llm_calls_per_session": 500,
            "max_tool_steps_per_turn": 20,
        }
    )
    return dataclasses.replace(ctx, config=ctx.config.model_copy(update={"agent": agent}))


# ---------------------------------------------------------------------------
# Every tool, with arguments that name every column
# ---------------------------------------------------------------------------
def _filler(name: str, annotation: Any, columns: Sequence[str]) -> Any:
    if name in {"primary_key"}:
        return "customer_id"
    if name in {"target"}:
        return "converted_30d"
    text = str(annotation)
    if "str" in text:
        return EMAIL  # a search text: the model asking about a value it saw
    if "int" in text:
        return 3
    if "bool" in text:
        return True
    raise ValueError(f"the canary does not know how to fill {name}: {annotation}")


def _max_items(info: Any) -> int | None:
    """The most items a list argument accepts (`max_length` in its field metadata), if it says."""
    for meta in info.metadata:
        limit = getattr(meta, "max_length", None)
        if limit:
            return int(limit)
    return None


def arg_sets(tool: Tool, columns: Sequence[str]) -> list[dict[str, Any]]:
    """Calls that put every column through `tool`; a tool whose arguments it cannot fill is an error."""
    fields = tool.args_model.model_fields
    base: dict[str, Any] = {}
    for name, info in fields.items():
        if name in {"column", "columns", "left", "right"}:
            continue
        if info.is_required():
            base[name] = _filler(name, info.annotation, columns)
    if "column" in fields:
        return [{**base, "column": column} for column in columns]
    if {"left", "right"} <= fields.keys():
        # Two different columns (the same one twice is refused): every neighbouring pair, and the far ends.
        pairs = [*itertools.pairwise(columns), (columns[0], columns[-1])]
        return [{**base, "left": left, "right": right} for left, right in pairs]
    if "columns" in fields:
        limit = _max_items(fields["columns"]) or len(columns)
        pairs = [list(pair) for pair in itertools.pairwise(columns)]
        groups = [list(columns[start : start + limit]) for start in range(0, len(columns), limit)]
        return [{**base, "columns": pair} for pair in pairs] + [
            {**base, "columns": group} for group in groups
        ]
    variants = [base]
    if {"primary_key", "target"} <= fields.keys():
        variants.append({"primary_key": "customer_id", "target": "converted_30d"})
    if "offset" in fields:
        variants.append({"offset": 10})
    return variants


def every_call(columns: Sequence[str], skip: Iterable[str] = ()) -> list[dict[str, Any]]:
    """One action per (tool, arguments) the registry can be called with."""
    skipped = set(skip)
    actions: list[dict[str, Any]] = []
    for tool in TOOLS.values():
        if tool.kind is not ToolKind.READ or tool.name in skipped:
            continue
        actions.extend({"action": tool.name, "args": args} for args in arg_sets(tool, columns))
    return actions


class ToolingClient(FakeLLMClient):
    """A `FakeLLMClient` that makes a scripted list of tool calls, `per_turn` of them, then replies.

    It records every prompt in `calls` like the real fake, so a test reads exactly what a model would
    have been sent.
    """

    def __init__(self, actions: Iterable[dict[str, Any]], per_turn: int = 6) -> None:
        super().__init__(mode=FakeLLMMode.GROUNDED)
        self.queue = list(actions)
        self.per_turn = per_turn

    def _body(self, system: str, user: str) -> str:
        _, results, _ = _helper_sections(user)
        if self.queue and len(results) < self.per_turn:
            return json.dumps(self.queue.pop(0))
        return json.dumps({"action": "reply", "text": "I looked at the file.", "evidence_ids": []})

    @property
    def sent_text(self) -> list[str]:
        """Every prompt, system and user, exactly as the client received it."""
        return [f"{call.system}\n{call.prompt}" for call in self.calls if call.kind == "complete"]


def guardrails() -> Guardrails:
    return Guardrails(load_policy())


def meter_for(client: FakeLLMClient) -> Meter:
    return Meter(
        client,
        job_id="canary",
        llm=LlmConfig(),
        budget=BudgetConfig(cache=False, max_calls_per_run=100_000, max_cost_usd_per_run=1_000.0),
    )


def after(session: AgentSession, message: str, turn: TurnResult) -> AgentSession:
    """The session as `POST …/messages` leaves it after one turn."""
    asked = ChatMessage(role=ChatRole.USER, text=message, created_at=utc_now())
    return session.model_copy(
        update={
            "transcript": cap_sent((*session.transcript, asked, turn.reply)),
            "tool_results": (*session.tool_results, *turn.tool_results),
            "proposals": (*session.proposals, *turn.proposals),
            "llm_calls": session.llm_calls + turn.llm_calls,
        }
    )


def run_session(
    ctx: AgentContext,
    client: ToolingClient,
    *,
    message: str = "Please look at every column.",
    started: AgentSession | None = None,
    max_turns: int = 200,
    on_turn: Callable[[TurnResult], None] | None = None,
) -> tuple[AgentSession, list[TurnResult]]:
    """Whole chat turns through the real `chat_turn` until the client has made every call."""
    session = started if started is not None else start_session(ctx, session_id="canary")
    meter = meter_for(client)
    turns: list[TurnResult] = []
    for _ in range(max_turns):
        if not client.queue:
            break
        turn = chat_turn(ctx, session, message, meter=meter, guardrails=guardrails())
        turns.append(turn)
        if on_turn is not None:
            on_turn(turn)
        session = after(session, message, turn)
    return session, turns


def leaks(text: str, secrets: Iterable[str], *, exact: bool = False) -> list[str]:
    """The planted values found in `text`: as written, without separators, and (unless `exact`) in any case."""
    folded = text if exact else text.casefold()
    joined = re.sub(r"(?<=\d)[ \-](?=\d)", "", folded)  # `4111 1111` and `4111-1111` read as one number
    found: list[str] = []
    for secret in secrets:
        plain = secret if exact else secret.casefold()
        compact = re.sub(r"(?<=\d)[ \-](?=\d)", "", plain)
        if plain in folded or (len(compact) >= 9 and compact in joined):
            found.append(secret)
    return found


def stored_text(session: AgentSession) -> str:
    """Everything a Viewer reads of the chat: each message, its turn log and what it says was sent."""
    return "\n".join(message.model_dump_json() for message in session.transcript)


def stray_keys(value: Any, columns: Iterable[str] = ()) -> list[str]:
    """Every dict key in a JSON-like `value` that is not a field name of ours (`egress.RESULT_KEYS`,
    `SAFE_KEYS`) or a column of the file. A tool that returns `{cell value: count}` has one per cell: the
    gate cannot tell such a key from a label, so no tool may do it (review finding 3)."""
    known = {str(c) for c in columns} | RESULT_KEYS | SAFE_KEYS
    found: list[str] = []

    def walk(item: Any) -> None:
        if isinstance(item, dict):
            for key, inner in item.items():
                if str(key) not in known:
                    found.append(str(key))
                walk(inner)
        elif isinstance(item, (list, tuple)):
            for inner in item:
                walk(inner)

    walk(value)
    return found
