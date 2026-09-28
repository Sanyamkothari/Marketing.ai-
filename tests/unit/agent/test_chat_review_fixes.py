"""Plan G review fixes for the chat: each defect the adversarial review confirmed, pinned by a test."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import pytest

from engine.agent import loop
from engine.agent.grounding import evidence_numbers, grounded_numbers, ungrounded_numbers
from engine.agent.loop import FALLBACK, chat_turn
from engine.agent.session import start_session
from engine.agent.tools import call_tool
from engine.generative.guardrails import Guardrails, load_policy
from tests.fixtures.agent_bench.make_messy import messy_frame
from tests.unit.agent.helpers import context_for, synthetic
from tests.unit.agent.test_session_and_loop import ScriptedMeter


def _guardrails() -> Guardrails:
    return Guardrails(load_policy())


def _act(action: str, **args: Any) -> str:
    return json.dumps({"action": action, "args": args})


def _reply(text: str, evidence: Any = ()) -> str:
    return json.dumps({"action": "reply", "text": text, "evidence_ids": evidence})


@pytest.fixture(scope="module")
def started() -> tuple[Any, Any]:
    ctx = context_for(messy_frame())
    return ctx, start_session(ctx, session_id="s1")


def _turn(started: tuple[Any, Any], replies: Sequence[str], message: str = "hi") -> Any:
    ctx, session = started
    return chat_turn(ctx, session, message, meter=ScriptedMeter(replies), guardrails=_guardrails())


# ---------------------------------------------------------------------------
# 1. A malformed reply never crashes the turn, and its model calls are counted
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("evidence", [5, True, 1.5, "c1-e1", {"a": 1}])
def test_evidence_ids_that_are_not_a_list_count_as_no_evidence(
    started: tuple[Any, Any], evidence: Any
) -> None:
    turn = _turn(started, [_reply("Nothing to add.", evidence)])
    assert turn.reply.text == "Nothing to add."
    assert turn.reply.evidence_ids == ()
    assert turn.llm_calls == 1


def test_a_deeply_nested_reply_is_malformed_not_a_crash(started: tuple[Any, Any]) -> None:
    deep = "[" * 5_000 + "]" * 5_000
    deep_args = '{"action":"get_profile","args":' + '{"a":' * 3_000 + "1" + "}" * 3_000 + "}"
    turn = _turn(started, [deep, deep_args])
    assert turn.reply.text.startswith(FALLBACK)
    assert turn.blocked_by == "AGENT_REPLY_MALFORMED"
    assert turn.llm_calls == 2
    assert turn.reply.turn is not None and turn.reply.turn.llm_calls == 2


def test_an_unexpected_error_in_a_step_ends_the_turn_and_keeps_the_call_count(
    started: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("a tool bug")

    monkeypatch.setattr(loop, "call_tool", broken)
    turn = _turn(started, [_act("describe_outcome", column="converted_30d")])
    assert turn.reply.text.startswith(FALLBACK)
    assert turn.blocked_by == "AGENT_TURN_FAILED"
    assert turn.llm_calls == 1
    assert turn.reply.turn is not None and turn.reply.turn.error_codes == ("AGENT_TURN_FAILED",)


# ---------------------------------------------------------------------------
# 2. A cell is masked before it is cut, so no half of an email or phone number survives
# ---------------------------------------------------------------------------
NAMES = ("priya.sharma", "rahul.verma", "anita.desai", "john.smith", "maria.garcia")


def test_cells_are_masked_before_they_are_cut() -> None:
    frame = synthetic(rows=400)
    frame["notes"] = [
        f"Customer note: please follow up with the client at their address {NAMES[i % 5]}@example.com"
        for i in range(len(frame))
    ]
    frame["calls"] = [
        f"Called on the weekend {i % 5}, asked for a call back at +91 98765 4321{i % 5}"
        for i in range(len(frame))
    ]
    ctx = context_for(frame, target="converted_30d")
    for column in ("notes", "calls"):
        profile = next(c for c in ctx.profile.columns if c.name == column)
        assert not profile.pii_kinds and profile.free_text_pii_kinds  # inspected, values shown
        result = call_tool(ctx, "inspect_column", {"column": column}, evidence_id="e1").result
        relation = result["relation_to_outcome"]
        assert isinstance(relation, dict)
        shown = [
            *result["examples"],
            *(item["value"] for item in result["top_values"]),
            *(item["bucket"] for item in relation["buckets"]),
        ]
        assert shown
        for text in shown:
            assert "@" not in text, text
            assert "98765" not in text and "+91" not in text, text
            assert not any(name in text for name in NAMES), text


# ---------------------------------------------------------------------------
# 3. A number the model chose as an argument is never evidence
# ---------------------------------------------------------------------------
def test_a_number_passed_as_a_tool_argument_does_not_ground_a_reply(started: tuple[Any, Any]) -> None:
    turn = _turn(started, [_act("get_profile", offset=73), _reply("Churn in your file is 73%.")])
    assert turn.blocked_by == "numbers_grounded"
    assert turn.reply.text.startswith(FALLBACK)


def test_a_number_passed_as_an_argument_in_an_earlier_turn_does_not_ground_a_later_reply(
    started: tuple[Any, Any],
) -> None:
    ctx, session = started
    first = _turn(started, [_act("get_profile", offset=73), _reply("Done.")])
    later = session.model_copy(
        update={
            "tool_results": (*session.tool_results, *first.tool_results),
            "transcript": (first.reply, first.reply),
        }
    )
    turn = chat_turn(
        ctx,
        later,
        "what is churn?",
        meter=ScriptedMeter([_reply("Churn is 73%.")]),
        guardrails=_guardrails(),
    )
    assert turn.blocked_by == "numbers_grounded"


def test_evidence_numbers_drop_what_the_arguments_supplied() -> None:
    numbers = evidence_numbers(
        {"columns_offset": 73, "rows": 3000, "message": "Test share 0.35 is too large."},
        {"offset": 73, "overrides": {"split.test_size": 0.35}},
    )
    assert ungrounded_numbers("There are 3,000 rows.", numbers) == []
    for claim in ("Churn is 73%.", "Churn is 0.73.", "The share is 35%.", "It is 0.35."):
        assert ungrounded_numbers(claim, numbers), claim


# ---------------------------------------------------------------------------
# 4. Leading-dot decimals, glued percents and percent forms of 0 and 1 are claims
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "Your conversion rate is .73 overall.",
        "Churn is .5% here.",
        "Churn is 100%.",
        "Only 1% convert.",
        "Churn is73% here.",
    ],
)
def test_leading_dot_glued_and_percent_numbers_are_checked(text: str) -> None:
    grounded = grounded_numbers([{"rows": 3000, "rate": 0.042}])
    assert ungrounded_numbers(text, grounded, names=("ad_ctr_90d",)), text


@pytest.mark.parametrize(
    "text",
    [
        "Turn yes into 1 and no into 0.",
        "The 1st and 2nd suggestions.",
        "Look at ad_ctr_90d and Q3 figures.",
        "The rate is .042 of 3,000 rows.",
        "4.2% of rows.",
    ],
)
def test_bare_zero_one_names_and_grounded_decimals_still_pass(text: str) -> None:
    grounded = grounded_numbers([{"rows": 3000, "rate": 0.042}])
    assert ungrounded_numbers(text, grounded, names=("ad_ctr_90d",)) == [], text


def test_a_grounded_leading_dot_decimal_in_evidence_grounds_a_reply() -> None:
    grounded = grounded_numbers(["The share is .35 of the file."])
    assert ungrounded_numbers("It is 35%.", grounded) == []


# ---------------------------------------------------------------------------
# 5. The turn log records only known action names, never model-written text
# ---------------------------------------------------------------------------
def test_an_unknown_action_is_logged_as_unknown_not_verbatim(started: tuple[Any, Any]) -> None:
    turn = _turn(
        started,
        [json.dumps({"action": "priya.sharma@example.com +91 98765 43210"}), _reply("Nothing to add.")],
    )
    log = turn.reply.turn
    assert log is not None
    assert log.tools == ("unknown", "reply")
    assert log.error_codes == ("AGENT_TOOL_UNKNOWN",)
    assert "priya" not in json.dumps(log.model_dump())
