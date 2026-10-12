"""Plan G's runtime through Data Doctor's registry (Plan O M232, DEC-1802), behind `data_doctor.unified`.

With the switch on, the rules advisor and the chat call every tool through the registry. What it proves: Plan G's
22 and the 27 Hinglish golden digests are byte-identical through the registry (and the registry was really
called); a whole chat session sends the model byte-identical prompts and stores the same results either way; and
a Viewer's chat is refused on each tool call, an Analyst's is not.

The switch is M230's `data_doctor.unified`; until it is merged a configuration has no such section, so these
tests switch it on where it is read (`engine.doctor.registry.unified_on`).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

from engine.access.roles import Principal, Role
from engine.agent.contracts import AgentSession
from engine.agent.loop import TurnResult, chat_turn
from engine.agent.session import start_session
from engine.doctor import registry as registry_module
from engine.doctor.registry import DataToolRegistry
from tests.fixtures.agent_bench.cases import CASES, EXPECTED, HEADER_HELP_CASES, HINGLISH_PREFIX, run_case
from tests.unit.agent.egress_canary import (
    ToolingClient,
    after,
    canary_context,
    every_call,
    guardrails,
    meter_for,
)

ORIGINAL = sorted(name for name in CASES if name not in HEADER_HELP_CASES)
HINGLISH = sorted(name for name in CASES if name.startswith(HINGLISH_PREFIX))


@pytest.fixture
def unified(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, int]]:
    """`data_doctor.unified` on, and a count of the calls that went through the registry."""
    counts = {"session_call": 0, "call": 0}
    real_session_call = DataToolRegistry.session_call
    real_call = DataToolRegistry.call

    def session_call(self: DataToolRegistry, *args: Any, **kwargs: Any) -> Any:
        counts["session_call"] += 1
        return real_session_call(self, *args, **kwargs)

    def call(self: DataToolRegistry, *args: Any, **kwargs: Any) -> Any:
        counts["call"] += 1
        return real_call(self, *args, **kwargs)

    monkeypatch.setattr(registry_module, "unified_on", lambda _config: True)
    monkeypatch.setattr(DataToolRegistry, "session_call", session_call)
    monkeypatch.setattr(DataToolRegistry, "call", call)
    yield counts


def test_there_are_22_original_and_27_hinglish_cases() -> None:
    assert len(ORIGINAL) == 22 and len(HINGLISH) == 27


@pytest.mark.parametrize("name", [*ORIGINAL, *HINGLISH])
def test_the_golden_digests_are_byte_identical_through_the_registry(
    name: str, unified: dict[str, int]
) -> None:
    expected = json.loads(EXPECTED.read_text(encoding="utf-8"))[name]
    assert json.dumps(run_case(name), sort_keys=True) == json.dumps(expected, sort_keys=True)
    assert unified["session_call"] > 0, "the advisor's tools went through the registry"


# ---------------------------------------------------------------------------
# The chat
# ---------------------------------------------------------------------------
def _scrubbed(session: AgentSession) -> dict[str, Any]:
    """The session as stored, without the times the clock wrote."""
    data = json.loads(session.model_dump_json())

    def walk(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                k: walk(v) for k, v in value.items() if k not in {"created_at", "updated_at", "started_at"}
            }
        if isinstance(value, list):
            return [walk(v) for v in value]
        return value

    return walk(data)  # type: ignore[no-any-return]


ANALYST = Principal(user_id="u-analyst", username="analyst", roles=frozenset({Role.ANALYST}), kind="user")


def _chat(mode: str, steps: list[dict[str, Any]]) -> tuple[list[str], dict[str, Any]]:
    """Whole chat turns, as `egress_canary.run_session` runs them, asked by an Analyst (as the route passes)."""
    ctx = canary_context(mode=mode, rows=600)
    client = ToolingClient(steps)
    session = start_session(ctx, session_id="canary")
    meter = meter_for(client)
    message = "Please look at every column."
    for _ in range(200):
        if not client.queue:
            break
        turn = chat_turn(ctx, session, message, meter=meter, guardrails=guardrails(), principal=ANALYST)
        session = after(session, message, turn)
    return client.sent_text, _scrubbed(session)


def _proposals(columns: list[str]) -> list[dict[str, Any]]:
    """A look at a column, then a fix of it citing that look, then a setting; the turn's ids are predictable."""
    steps: list[dict[str, Any]] = []
    for turn, column in enumerate(columns, start=1):
        steps.extend(
            [
                {"action": "value_counts", "args": {"column": column}},
                {
                    "action": "propose_step",
                    "args": {
                        "kind": "normalise_text",
                        "column": column,
                        "evidence_ids": [f"c{turn}-e1"],
                        "reason": "Tidy it.",
                        "extra_key_plan_g_ignores": True,
                    },
                },
                {
                    "action": "propose_setting",
                    "args": {"path": "split.test_fraction", "value": 0.2, "reason": "More."},
                },
                {"action": "edit_by_hand", "args": {"kind": "drop_column", "column": column}},
            ]
        )
    return steps


@pytest.mark.parametrize("mode", ["masked_data", "summaries_only"])
def test_a_whole_chat_is_byte_identical_through_the_registry(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    columns = [str(c) for c in canary_context(rows=600).frame.columns]
    steps = [*every_call(columns[:8]), *_proposals(["notes", "region_contact", "label"])]
    off_prompts, off_session = _chat(mode, steps)
    counts = {"n": 0}
    real = DataToolRegistry.session_call

    def counted(self: DataToolRegistry, *args: Any, **kwargs: Any) -> Any:
        counts["n"] += 1
        return real(self, *args, **kwargs)

    monkeypatch.setattr(registry_module, "unified_on", lambda _config: True)
    monkeypatch.setattr(DataToolRegistry, "session_call", counted)
    on_prompts, on_session = _chat(mode, steps)
    assert counts["n"] > 0
    assert on_prompts == off_prompts, "the model is sent exactly what it was sent before"
    assert on_session == off_session, "the session stores exactly what it stored before"
    assert any(p["source"] == "ai" for p in on_session["proposals"]), "a chat suggestion went through"


def _turn(principal: Principal, steps: list[dict[str, Any]]) -> TurnResult:
    ctx = canary_context(rows=600)
    session = start_session(ctx, session_id="viewer")
    client = ToolingClient(steps, per_turn=len(steps))
    return chat_turn(
        ctx, session, "Look please.", meter=meter_for(client), guardrails=guardrails(), principal=principal
    )


def _person(*roles: Role) -> Principal:
    return Principal(user_id="u-1", username="u", roles=frozenset(roles), kind="user")


def test_a_viewers_chat_is_refused_on_each_tool_call(unified: dict[str, int]) -> None:
    steps = [
        {"action": "value_counts", "args": {"column": "monthly_spend"}},
        {"action": "propose_setting", "args": {"path": "split.test_fraction", "value": 0.2}},
    ]
    turn = _turn(_person(Role.VIEWER), steps)
    assert turn.reply.turn is not None
    # Each call is refused on its own; the second refusal ends the turn, as any second refusal does.
    assert turn.reply.turn.error_codes == ("TOOL_ROLE_REQUIRED", "TOOL_ROLE_REQUIRED")
    assert turn.blocked_by == "TOOL_ROLE_REQUIRED"
    assert turn.tool_results == () and turn.proposals == ()
    allowed = _turn(_person(Role.ANALYST), steps[:1])
    assert [r.tool for r in allowed.tool_results] == ["value_counts"]


def test_a_viewer_cannot_make_a_chat_suggestion(unified: dict[str, int]) -> None:
    steps = [{"action": "propose_setting", "args": {"path": "split.test_fraction", "value": 0.2}}]
    refused = _turn(_person(Role.VIEWER), steps)
    assert refused.proposals == ()
    assert refused.reply.turn is not None and "TOOL_ROLE_REQUIRED" in refused.reply.turn.error_codes
    made = _turn(_person(Role.ANALYST), steps)
    assert [p.path for p in made.proposals] == ["split.test_fraction"]
    assert unified["call"] >= 2, "the suggestions went through the registry's call"


def test_with_the_switch_off_the_registry_is_not_reached(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the registry was reached with the switch off")

    monkeypatch.setattr(DataToolRegistry, "session_call", refuse)
    monkeypatch.setattr(DataToolRegistry, "call", refuse)
    steps = [
        {"action": "value_counts", "args": {"column": "monthly_spend"}},
        {"action": "propose_setting", "args": {"path": "split.test_fraction", "value": 0.2}},
    ]
    turn = _turn(_person(Role.VIEWER), steps)  # the route checks the role; the chat does not, as before
    assert [r.tool for r in turn.tool_results] == ["value_counts", "propose_setting"]
    assert run_case("clean") == json.loads(EXPECTED.read_text(encoding="utf-8"))["clean"]
