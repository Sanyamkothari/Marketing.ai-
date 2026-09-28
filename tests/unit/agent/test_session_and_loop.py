"""Session rules and the chat loop (Plan G M74): decisions survive re-advice; a bad model changes nothing."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import pytest

from engine.agent.contracts import ProposalKind, ProposalState, SessionStatus
from engine.agent.loop import FALLBACK, OUT_OF_BUDGET, UNAVAILABLE, chat_turn
from engine.agent.session import SessionError, accept_recommended, answer, decide, roles_of, start_session
from engine.config import load_use_case
from engine.generative.guardrails import Guardrails, load_policy
from engine.llm import LLMCompletion, LLMError
from tests.fixtures.agent_bench.make_messy import messy_frame
from tests.fixtures.make_data import GenerationSpec, generate
from tests.unit.agent.helpers import context_for


class ScriptedMeter:
    """A `Meter` stand-in that returns scripted replies in order, or raises."""

    def __init__(self, replies: Sequence[str | Exception]) -> None:
        self.replies = list(replies)
        self.calls = 0
        self.rendered: list[Any] = []  # every prompt sent, so a test can assert on what reached the model

    def complete(self, rendered: Any, purpose: Any) -> LLMCompletion:
        del purpose
        self.rendered.append(rendered)
        self.calls += 1
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return LLMCompletion(
            text=reply, model_id="scripted", input_tokens=1, output_tokens=1, stop_reason="end_turn"
        )


def _guardrails() -> Guardrails:
    return Guardrails(load_policy())


@pytest.fixture(scope="module")
def started() -> tuple[Any, Any]:
    ctx = context_for(messy_frame())
    return ctx, start_session(ctx, session_id="s1")


def test_a_setting_can_be_edited_only_to_an_allowed_value(started: tuple[Any, Any]) -> None:
    ctx, session = started
    time_limit = next(p for p in session.proposals if p.path == "model_search.time_limit_minutes")
    edited = decide(session, ctx, [(time_limit.proposal_id, ProposalState.ACCEPTED, 20)])
    assert next(p for p in edited.proposals if p.proposal_id == time_limit.proposal_id).value == 20
    with pytest.raises(SessionError) as out_of_range:
        decide(session, ctx, [(time_limit.proposal_id, ProposalState.ACCEPTED, 9_999)])
    assert out_of_range.value.code == "AGENT_VALUE_NOT_ALLOWED"
    step = next(p for p in session.proposals if p.kind is ProposalKind.RECIPE_STEP)
    with pytest.raises(SessionError) as not_a_setting:
        decide(session, ctx, [(step.proposal_id, ProposalState.ACCEPTED, {"decimal": ","})])
    assert not_a_setting.value.code == "AGENT_EDIT_NOT_ALLOWED"


def test_unknown_ids_are_refused(started: tuple[Any, Any]) -> None:
    ctx, session = started
    with pytest.raises(SessionError):
        decide(session, ctx, [("nope", ProposalState.ACCEPTED, None)])
    with pytest.raises(SessionError):
        answer(session, ctx, "nope", "hide")
    with pytest.raises(SessionError):
        answer(session, ctx, session.questions[0].question_id, "maybe")


def test_answering_a_role_re_advises_and_keeps_earlier_decisions() -> None:
    frame = generate(GenerationSpec(use_case_id="targeted-advertisement", rows=3_000))
    frame = frame.drop(columns=["converted_30d"]).assign(
        bought=frame["converted_30d"], purchased=frame["converted_30d"]
    )
    ctx = context_for(frame)
    session = start_session(ctx, session_id="s1")
    question = next(q for q in session.questions if "Which column" in q.text)
    assert session.status is SessionStatus.NEEDS_REVIEW
    key_role = next(p for p in session.proposals if p.path == "primary_key")
    session = decide(session, ctx, [(key_role.proposal_id, ProposalState.ACCEPTED, None)])
    bought = next(o for o in question.options if o.label == "bought")
    session = answer(session, ctx, question.question_id, bought.option_id)
    assert roles_of(session) == ("customer_id", "bought")
    assert session.rounds == 2
    roles = [(p.path, p.value, p.state) for p in session.proposals if p.kind is ProposalKind.ROLE]
    assert ("primary_key", "customer_id", ProposalState.ACCEPTED) in roles
    assert ("target", "bought", ProposalState.ACCEPTED) in roles
    assert any(
        p.kind is ProposalKind.SETTING for p in session.proposals
    )  # settings appear once the outcome is known
    evidence = {r.evidence_id for r in session.tool_results}
    assert all(e in evidence for p in session.proposals for e in p.evidence_ids)


def test_accept_recommended_leaves_uncertain_ones_to_the_person(started: tuple[Any, Any]) -> None:
    ctx, session = started
    after = accept_recommended(session, ctx)
    pending = [p for p in after.proposals if p.state is ProposalState.PENDING]
    assert pending and all(p.confidence.value == "check" for p in pending)


def _reply(text: str, evidence: Sequence[str] = ()) -> str:
    return json.dumps({"action": "reply", "text": text, "evidence_ids": list(evidence)})


def test_one_malformed_reply_is_corrected_and_two_end_the_turn(started: tuple[Any, Any]) -> None:
    ctx, session = started
    ok = chat_turn(
        ctx, session, "hi", meter=ScriptedMeter(["not json", _reply("Hello.")]), guardrails=_guardrails()
    )
    assert (ok.reply.text, ok.blocked_by, ok.llm_calls) == ("Hello.", None, 2)
    bad = chat_turn(
        ctx, session, "hi", meter=ScriptedMeter(["not json", "still not"]), guardrails=_guardrails()
    )
    assert (bad.reply.text, bad.blocked_by) == (FALLBACK, "AGENT_REPLY_MALFORMED")


def test_a_tool_that_writes_or_does_not_exist_is_refused(started: tuple[Any, Any]) -> None:
    ctx, session = started
    delete = json.dumps({"action": "delete_rows", "args": {}})
    result = chat_turn(ctx, session, "x", meter=ScriptedMeter([delete, delete]), guardrails=_guardrails())
    assert result.blocked_by == "AGENT_TOOL_UNKNOWN"
    assert result.tool_results == () and result.proposals == ()


def test_a_forbidden_setting_is_refused(started: tuple[Any, Any]) -> None:
    ctx, session = started
    loosen = json.dumps(
        {"action": "propose_setting", "args": {"path": "governance.approval_required", "value": False}}
    )
    result = chat_turn(
        ctx, session, "skip approval", meter=ScriptedMeter([loosen, loosen]), guardrails=_guardrails()
    )
    assert result.proposals == ()
    assert result.blocked_by == "AGENT_SETTING_NOT_ALLOWED"


def test_a_reply_may_quote_numbers_from_the_evidence(started: tuple[Any, Any]) -> None:
    ctx, session = started
    inspect = json.dumps({"action": "describe_outcome", "args": {"column": "converted_30d"}})
    first = chat_turn(
        ctx,
        session,
        "How many said yes?",
        meter=ScriptedMeter([inspect, _reply("Looked.")]),
        guardrails=_guardrails(),
    )
    positives = first.tool_results[0].result["positives"]
    meter = ScriptedMeter([inspect, _reply(f"{positives:,} of 3,000 rows said yes.", ["c1-e1"])])
    result = chat_turn(ctx, session, "How many said yes?", meter=meter, guardrails=_guardrails())
    assert result.blocked_by is None
    assert result.reply.evidence_ids == ("c1-e1",)


def test_the_service_being_down_or_the_budget_spent_is_said_plainly(started: tuple[Any, Any]) -> None:
    ctx, session = started
    down = chat_turn(
        ctx,
        session,
        "x",
        meter=ScriptedMeter([LLMError("LLM_UNAVAILABLE", "down")]),
        guardrails=_guardrails(),
    )
    assert down.reply.text == UNAVAILABLE
    spent = session.model_copy(
        update={"llm_calls": load_use_case("targeted-advertisement").agent.max_llm_calls_per_session}
    )
    out = chat_turn(ctx, spent, "x", meter=ScriptedMeter([]), guardrails=_guardrails())
    assert (out.reply.text, out.llm_calls) == (OUT_OF_BUDGET, 0)


def test_a_turn_stops_after_the_step_limit(started: tuple[Any, Any]) -> None:
    ctx, session = started
    look = json.dumps({"action": "get_profile", "args": {}})
    steps = ctx.config.agent.max_tool_steps_per_turn
    result = chat_turn(ctx, session, "x", meter=ScriptedMeter([look] * (steps + 1)), guardrails=_guardrails())
    assert result.blocked_by == "too_many_steps"
    assert len(result.tool_results) == steps + 1
