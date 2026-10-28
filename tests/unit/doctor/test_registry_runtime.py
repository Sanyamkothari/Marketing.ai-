"""Plan G's runtime through Data Doctor's registry (Plan O M232, DEC-1802).

The rules advisor and the chat call every tool through the registry. What it proves: Plan G's 22 and the 27
Hinglish golden digests are byte-identical through the registry (and the registry was really called); a chat
suggestion reaches Plan G's handler exactly as the model sent it; and a Viewer's chat is refused on each tool
call, an Analyst's is not.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from typing import Any

import pytest

from engine.access.roles import Principal, Role
from engine.agent import loop as loop_module
from engine.agent.loop import TurnResult, chat_turn
from engine.agent.session import start_session
from engine.doctor.registry import DataToolRegistry
from tests.fixtures.agent_bench.cases import CASES, EXPECTED, HEADER_HELP_CASES, HINGLISH_PREFIX, run_case
from tests.unit.agent.egress_canary import (
    ToolingClient,
    canary_context,
    guardrails,
    meter_for,
)

ORIGINAL = sorted(name for name in CASES if name not in HEADER_HELP_CASES)
HINGLISH = sorted(name for name in CASES if name.startswith(HINGLISH_PREFIX))


@pytest.fixture
def through_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, int]]:
    """A count of the calls that went through the registry."""
    counts = {"session_call": 0, "call": 0}
    real_session_call = DataToolRegistry.session_call
    real_call = DataToolRegistry.call

    def session_call(self: DataToolRegistry, *args: Any, **kwargs: Any) -> Any:
        counts["session_call"] += 1
        return real_session_call(self, *args, **kwargs)

    def call(self: DataToolRegistry, *args: Any, **kwargs: Any) -> Any:
        counts["call"] += 1
        return real_call(self, *args, **kwargs)

    monkeypatch.setattr(DataToolRegistry, "session_call", session_call)
    monkeypatch.setattr(DataToolRegistry, "call", call)
    yield counts


def test_there_are_22_original_and_27_hinglish_cases() -> None:
    assert len(ORIGINAL) == 22 and len(HINGLISH) == 27


@pytest.mark.parametrize("name", [*ORIGINAL, *HINGLISH])
def test_the_golden_digests_are_byte_identical_through_the_registry(
    name: str, through_registry: dict[str, int]
) -> None:
    expected = json.loads(EXPECTED.read_text(encoding="utf-8"))[name]
    assert json.dumps(run_case(name), sort_keys=True) == json.dumps(expected, sort_keys=True)
    assert through_registry["session_call"] > 0, "the advisor's tools went through the registry"


# ---------------------------------------------------------------------------
# The chat
# ---------------------------------------------------------------------------
ANALYST = Principal(user_id="u-analyst", username="analyst", roles=frozenset({Role.ANALYST}), kind="user")


# ---------------------------------------------------------------------------
# A suggestion reaches Plan G's handler exactly as the model sent it (review fixes)
# ---------------------------------------------------------------------------
REGION = "region_contact"
"""A column the canary does not mark as personal: 'North', 'South' and two planted values, 150 rows each."""
LOOK = {"action": "value_counts", "args": {"column": REGION}}


def _step(kind: str, params: dict[str, Any], **more: Any) -> dict[str, Any]:
    args = {"kind": kind, "column": REGION, "params": params, "evidence_ids": ["c1-e1"], **more}
    return {"action": "propose_step", "args": args}


PADDED: dict[str, dict[str, Any]] = {
    "padded setting path": {
        "action": "propose_setting",
        "args": {"path": " split.test_fraction ", "value": 0.2, "reason": " More. "},
    },
    "padded missing-value tokens": _step("set_missing_text", {"values": [" ", "North "]}, reason=" x "),
    "padded mapping key": _step("map_values", {"mapping": {" North ": "N"}}),
    "padded mapping value": _step("map_values", {"mapping": {"North": " N "}}),
    "one-space separator": _step("split_column", {"separator": " "}, new_column="region_part"),
    "padded column": {
        "action": "propose_step",
        "args": {"kind": "normalise_text", "column": f" {REGION} ", "evidence_ids": ["c1-e1"]},
    },
    "padded kind": _step(" map_values ", {"mapping": {"North": "N"}}),
    "400 spellings": _step(
        "map_values", {"mapping": {"North": "N", **{f"spelling {i:03d} " * 3: f"s{i}" for i in range(400)}}}
    ),
}
"""Suggestions whose strings have spaces around them, and one as large as Plan G accepts (MAX_MAPPINGS=500)."""


def _plain(models: Any) -> Any:
    """Models as stored, without the times the clock wrote."""
    return _scrubbed_value(json.loads(json.dumps([m.model_dump(mode="json") for m in models])))


def _scrubbed_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: _scrubbed_value(v)
            for k, v in value.items()
            if k not in {"created_at", "updated_at", "started_at"}
        }
    if isinstance(value, list):
        return [_scrubbed_value(v) for v in value]
    return value


def _suggested(step: dict[str, Any]) -> dict[str, Any]:
    """One turn: a look at the column, then `step`; what Plan G's handler received and what the turn made."""
    seen.clear()
    turn = _turn(ANALYST, [LOOK, step])
    assert turn.reply.turn is not None
    return {
        "handler_args": copy.deepcopy(seen),
        "error_codes": turn.reply.turn.error_codes,
        "results": _plain(turn.tool_results),
        "proposals": _plain(turn.proposals),
    }


seen: list[dict[str, Any]] = []


@pytest.fixture
def spied(monkeypatch: pytest.MonkeyPatch) -> None:
    """Record the arguments Plan G's two chat handlers receive, switch on or off."""
    real_setting, real_step = loop_module._propose, loop_module._propose_step

    def setting(ctx: Any, args: dict[str, Any], *rest: Any) -> Any:
        seen.append(copy.deepcopy(args))
        return real_setting(ctx, args, *rest)

    def step(ctx: Any, session: Any, args: dict[str, Any], *rest: Any) -> Any:
        seen.append(copy.deepcopy(args))
        return real_step(ctx, session, args, *rest)

    monkeypatch.setattr(loop_module, "_propose", setting)
    monkeypatch.setattr(loop_module, "_propose_step", step)


@pytest.mark.parametrize("case", sorted(PADDED))
def test_a_suggestion_reaches_plan_g_exactly_as_the_model_sent_it(case: str, spied: None) -> None:
    made = _suggested(PADDED[case])
    assert made["handler_args"] == [PADDED[case]["args"]], "Plan G's handler got the arguments as sent"


def test_the_padded_cases_really_differ_from_their_stripped_forms(spied: None) -> None:
    """Without the spaces, Plan G decides otherwise: so the test above would see a stripped argument."""
    padded = _suggested(PADDED["padded setting path"])
    stripped = _suggested(
        {
            "action": "propose_setting",
            "args": {"path": "split.test_fraction", "value": 0.2, "reason": "More."},
        }
    )
    assert padded["error_codes"] == ("AGENT_SETTING_NOT_ALLOWED",) and padded["proposals"] == []
    assert stripped["error_codes"] == () and len(stripped["proposals"]) == 1
    large = _suggested(PADDED["400 spellings"])
    assert large["error_codes"] == () and len(large["proposals"]) == 1, "Plan G accepts 401 spellings"


def test_a_refused_suggestion_leaves_nothing_behind(monkeypatch: pytest.MonkeyPatch) -> None:
    """The turn's registry binding keeps nothing from one call to the next, refused or not."""
    made: list[Any] = []
    real = loop_module._RegistryTurn.propose

    def propose(self: Any, name: str, args: dict[str, Any]) -> Any:
        try:
            return real(self, name, args)
        finally:
            made.append(list(self.made))

    monkeypatch.setattr(loop_module._RegistryTurn, "propose", propose)
    turn = _turn(ANALYST, [LOOK, PADDED["400 spellings"], PADDED["padded setting path"]])
    assert turn.reply.turn is not None and len(turn.proposals) == 1
    assert made == [[], []]


def _turn(principal: Principal, steps: list[dict[str, Any]]) -> TurnResult:
    ctx = canary_context(rows=600)
    session = start_session(ctx, session_id="viewer")
    client = ToolingClient(steps, per_turn=len(steps))
    return chat_turn(
        ctx, session, "Look please.", meter=meter_for(client), guardrails=guardrails(), principal=principal
    )


def _person(*roles: Role) -> Principal:
    return Principal(user_id="u-1", username="u", roles=frozenset(roles), kind="user")


def test_a_viewers_chat_is_refused_on_each_tool_call(through_registry: dict[str, int]) -> None:
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


def test_a_viewer_cannot_make_a_chat_suggestion(through_registry: dict[str, int]) -> None:
    steps = [{"action": "propose_setting", "args": {"path": "split.test_fraction", "value": 0.2}}]
    refused = _turn(_person(Role.VIEWER), steps)
    assert refused.proposals == ()
    assert refused.reply.turn is not None and "TOOL_ROLE_REQUIRED" in refused.reply.turn.error_codes
    made = _turn(_person(Role.ANALYST), steps)
    assert [p.path for p in made.proposals] == ["split.test_fraction"]
    assert through_registry["call"] >= 2, "the suggestions went through the registry's call"
