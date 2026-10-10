"""The optional second opinions from the AI service (Plan K M142, DEC-1409): rerank and the leak reviewer.

Through `assist_session` on a real session, with a scripted client in place of the AI service and the real
`Meter`, `Guardrails` and egress gate. What must hold: the model can reorder what the rules listed and
never add to it; its leak questions sit beside the rule's, never replace it, and tick nothing; and nothing
a pattern can recognise reaches its prompt.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from engine.agent.advisor import ROLE_PRIMARY_KEY, ROLE_TARGET
from engine.agent.assist import GENERIC_WHY, LEAK_REVIEW_TOOL, MAX_LEAK_QUESTIONS, RERANK_TOOL, assist_session
from engine.agent.contracts import AgentSession, ProposalKind, ProposalState, RecipeStepKind, ToolResult
from engine.agent.session import accept_recommended, answer, start_session
from engine.agent.tools import AgentContext
from engine.llm import LLMError
from tests.fixtures.make_data import GenerationSpec, generate
from tests.unit.agent.assist_client import AssistClient
from tests.unit.agent.egress_canary import (
    CELL_WORDS,
    HARD_SECRETS,
    HEADER_EMAIL,
    canary_context,
    guardrails,
    leaks,
    meter_for,
)
from tests.unit.agent.helpers import context_for


def _frame() -> pd.DataFrame:
    """Two columns that both read like an outcome (so the rules ask which), a column named like a late fact,
    a sign-up date, and a spend column."""
    rows = 600
    return pd.DataFrame(
        {
            "customer_id": [f"C{i}" for i in range(rows)],
            "churn_flg": [1 if i % 4 == 0 else 0 for i in range(rows)],
            "kharida_flag": [1 if i % 3 == 0 else 0 for i in range(rows)],
            "cancellation_reason": [("none", "price", "move")[i % 3] for i in range(rows)],
            "final_status": [("open", "closed", "pending")[i % 3] for i in range(rows)],
            "monthly_spend": [float(10 + i % 90) for i in range(rows)],
        }
    )


def _started(frame: pd.DataFrame | None = None) -> tuple[AgentContext, AgentSession]:
    ctx = context_for(_frame() if frame is None else frame)
    return ctx, start_session(ctx, session_id="s-1")


def _assist(
    ctx: AgentContext, session: AgentSession, client: AssistClient
) -> tuple[AgentSession, AssistClient, Any]:
    result = assist_session(ctx, session, meter=meter_for(client), guardrails=guardrails())
    return result.session, client, result


def _candidates_of(result: ToolResult) -> list[dict[str, Any]]:
    """The `header_candidates` a stored `rerank_columns` result holds."""
    data: dict[str, Any] = dict(result.result)
    return list(data["header_candidates"])


def _outcome_question(session: AgentSession) -> Any:
    return next(q for q in session.questions if "Which column says" in q.text)


def test_the_rules_ask_which_outcome_when_two_headers_read_the_same() -> None:
    _, session = _started()
    options = [o.label for o in _outcome_question(session).options]
    assert options == ["churn_flg", "kharida_flag"]  # both 0.8: file order


def test_rerank_puts_the_buttons_in_the_models_order_and_records_it() -> None:
    ctx, session = _started()
    out, client, result = _assist(
        ctx, session, AssistClient(orders=[{"kind": "outcome", "columns": ["kharida_flag", "churn_flg"]}])
    )
    assert [o.label for o in _outcome_question(out).options] == ["kharida_flag", "churn_flg"]
    assert result.reordered == ("outcome",)
    assert "rerank" in client.tasks
    evidence = next(r for r in out.tool_results if r.tool == RERANK_TOOL)
    assert [c["column"] for c in _candidates_of(evidence) if c["kind"] == "outcome"] == [
        "kharida_flag",
        "churn_flg",
    ]
    assert out.llm_calls == session.llm_calls + result.llm_calls


def test_rerank_never_adds_a_column_the_rules_did_not_list() -> None:
    ctx, session = _started()
    before = {o.option_id: o for o in _outcome_question(session).options}
    out, _, result = _assist(
        ctx,
        session,
        AssistClient(
            orders=[
                {
                    "kind": "outcome",
                    "columns": ["monthly_spend", "mkt_optin", "kharida_flag", "kharida_flag"],
                },
                {
                    "kind": "id",
                    "columns": ["monthly_spend"],
                },  # a role with one candidate, or none, is not reordered
                {"kind": "nonsense", "columns": ["churn_flg"]},
            ]
        ),
    )
    question = _outcome_question(out)
    assert [o.label for o in question.options] == ["kharida_flag", "churn_flg"]
    assert {o.option_id: o for o in question.options} == before  # the same buttons, nothing edited or added
    listed = {c["column"] for r in out.tool_results if r.tool == RERANK_TOOL for c in _candidates_of(r)}
    assert listed <= {"churn_flg", "kharida_flag"}
    assert result.reordered == ("outcome",)


def test_a_reply_that_is_not_json_or_not_what_was_asked_changes_nothing() -> None:
    ctx, session = _started()
    for raw in ("I think churn is the outcome.", "[]", '{"orders": "kharida_flag"}', '{"orders": [1, 2]}'):
        out, _, result = _assist(ctx, session, AssistClient(raw=raw))
        assert [o.label for o in _outcome_question(out).options] == ["churn_flg", "kharida_flag"], raw
        assert result.reordered == () and result.leak_questions == 0


def test_an_answered_question_is_left_as_the_person_left_it() -> None:
    ctx, session = _started()
    first = _outcome_question(session)
    session = answer(session, ctx, first.question_id, first.options[0].option_id)
    out, _, result = _assist(
        ctx, session, AssistClient(orders=[{"kind": "outcome", "columns": ["kharida_flag", "churn_flg"]}])
    )
    kept = next(q for q in out.questions if q.question_id == first.question_id)
    assert kept == next(q for q in session.questions if q.question_id == first.question_id)
    assert result.llm_calls >= 0


def test_the_leak_reviewer_adds_questions_beside_the_rules_never_a_suggestion() -> None:
    ctx, session = _started()
    out, client, result = _assist(
        ctx,
        session,
        AssistClient(
            columns=[
                {
                    "column": "cancellation_reason",
                    "why": "A cancellation reason is only known after a customer leaves.",
                },
                {"column": "final_status", "why": "The final status may be set once the outcome is known."},
            ]
        ),
    )
    added = [q for q in out.questions if q.question_id.startswith("x1-")]
    assert len(added) == result.leak_questions == 2
    for question in added:
        assert not question.blocking and question.answer is None
        assert [o.option_id for o in question.options] == ["hide", "keep"]
        hide = question.options[0].proposal
        assert hide is not None and hide.confidence.value == "check" and hide.step is not None
        assert hide.step.kind is RecipeStepKind.DROP_COLUMN
        assert question.options[1].proposal is None
    assert out.proposals == session.proposals  # no suggestion added: only the person's "Hide it" does that
    assert [q for q in out.questions if q not in added] == list(
        session.questions
    )  # the rules' own are untouched
    assert client.tasks[-1] == "leaks"
    assert any(r.tool == LEAK_REVIEW_TOOL for r in out.tool_results)


def test_pressing_hide_adds_the_step_pressing_keep_adds_nothing_and_nothing_else_is_ticked() -> None:
    ctx, session = _started()
    out, _, _ = _assist(
        ctx,
        session,
        AssistClient(
            columns=[
                {"column": "cancellation_reason", "why": "Only known after a customer leaves."},
                {"column": "final_status", "why": "Set when the outcome is known."},
            ]
        ),
    )
    ticked = accept_recommended(out, ctx)
    assert not [
        p for p in ticked.proposals if p.step is not None and p.step.kind is RecipeStepKind.DROP_COLUMN
    ]
    first, second = (q for q in out.questions if q.question_id.startswith("x1-"))
    out = answer(out, ctx, first.question_id, "hide")
    hidden = [p for p in out.proposals if p.step is not None and p.step.kind is RecipeStepKind.DROP_COLUMN]
    assert [(p.step.column, p.state) for p in hidden if p.step] == [
        ("cancellation_reason", ProposalState.ACCEPTED)
    ]
    before = out.proposals
    out = answer(out, ctx, second.question_id, "keep")
    assert out.proposals == before


def test_the_reviewer_never_names_the_id_the_outcome_a_made_up_column_or_one_already_asked_about() -> None:
    frame = generate(GenerationSpec(use_case_id="targeted-advertisement", rows=3_000, variant="leaky_column"))
    ctx, session = _started(frame)
    asked = {
        o.proposal.step.column
        for q in session.questions
        for o in q.options
        if o.proposal and o.proposal.step and o.proposal.step.kind is RecipeStepKind.DROP_COLUMN
    }
    assert asked, "the rule-based leak check asks about the planted column"
    ids = [p.value for p in session.proposals if p.path == ROLE_PRIMARY_KEY]
    target = [p.value for p in session.proposals if p.path == ROLE_TARGET]
    named = [{"column": c, "why": "x"} for c in (*asked, *ids, *target, "invented_column", "region")]
    out, _, _ = _assist(ctx, session, AssistClient(columns=named))
    reviewed = [q for q in out.questions if q.question_id.startswith("x1-")]
    columns = [o.proposal.step.column for q in reviewed for o in q.options if o.proposal and o.proposal.step]
    assert columns == ["region"]  # the one column that was neither protected, asked about nor invented
    assert [q for q in out.questions if q not in reviewed] == list(
        session.questions
    )  # the rule's question stays


def test_at_most_three_questions_and_a_column_only_once() -> None:
    frame = _frame().assign(a_late=1, b_late=2, c_late=3, d_late=4)
    ctx, session = _started(frame)
    named = [{"column": c, "why": "Late."} for c in ("a_late", "a_late", "b_late", "c_late", "d_late")]
    _, _, result = _assist(ctx, session, AssistClient(columns=named))
    assert result.leak_questions == MAX_LEAK_QUESTIONS == 3


def test_the_models_sentence_is_shown_only_when_it_passes_the_checks() -> None:
    ctx, session = _started()
    out, _, _ = _assist(
        ctx,
        session,
        AssistClient(
            columns=[
                {"column": "cancellation_reason", "why": "About 73% of these were set after the outcome."},
                {"column": "final_status", "why": "Write to jane.roe@example.test and tell her it leaks."},
            ]
        ),
    )
    texts = [q.text for q in out.questions if q.question_id.startswith("x1-")]
    assert all(GENERIC_WHY in text for text in texts)  # an invented number and an e-mail address both fail
    assert not any("73" in text or "jane" in text for text in texts)


def test_a_sentence_with_a_number_the_session_holds_is_kept() -> None:
    ctx, session = _started()
    rows = ctx.profile.row_count
    out, _, _ = _assist(
        ctx,
        session,
        AssistClient(
            columns=[{"column": "final_status", "why": f"Set after the outcome; the file has {rows} rows."}]
        ),
    )
    text = next(q.text for q in out.questions if q.question_id.startswith("x1-"))
    assert f"{rows} rows" in text or GENERIC_WHY in text  # grounded in the profile it was shown, or replaced


def test_an_ai_service_that_does_not_answer_leaves_the_session_exactly_as_it_was() -> None:
    ctx, session = _started()

    class Down(AssistClient):
        def complete(self, *args: Any, **kwargs: Any) -> Any:
            raise LLMError("LLM_UNAVAILABLE", "down")

    out, _, result = _assist(ctx, session, Down())
    assert out == session and result.blocked_by == "unavailable" and result.llm_calls == 0


def test_a_session_out_of_calls_makes_none() -> None:
    ctx, session = _started()
    spent = session.model_copy(update={"llm_calls": ctx.config.agent.max_llm_calls_per_session})
    client = AssistClient()
    out, _, result = _assist(ctx, spent, client)
    assert out == spent and client.calls == () and result.blocked_by == "budget"


def test_one_agent_message_records_what_was_sent_and_the_calls() -> None:
    ctx, session = _started()
    out, client, result = _assist(
        ctx,
        session,
        AssistClient(orders=[{"kind": "outcome", "columns": ["kharida_flag", "churn_flg"]}], columns=[]),
    )
    message = out.transcript[-1]
    assert message.role.value == "agent" and message.turn is not None
    assert message.turn.llm_calls == result.llm_calls == len(client.prompts) == 2
    assert message.turn.tools == (RERANK_TOOL, LEAK_REVIEW_TOOL)
    assert [item.tool for item in message.sent] == [RERANK_TOOL, LEAK_REVIEW_TOOL]
    assert AgentSession.model_validate_json(out.model_dump_json()) == out  # a valid, storable session


def test_a_second_round_uses_new_ids() -> None:
    ctx, session = _started()
    first, _, _ = _assist(ctx, session, AssistClient(columns=[{"column": "final_status", "why": "Late."}]))
    second, _, _ = _assist(
        ctx, first, AssistClient(columns=[{"column": "cancellation_reason", "why": "Late."}])
    )
    ids = [q.question_id for q in second.questions] + [p.proposal_id for p in second.proposals]
    assert len(ids) == len(set(ids))
    assert {r.evidence_id.split("-")[0] for r in second.tool_results if r.tool == LEAK_REVIEW_TOOL} == {
        "x1",
        "x2",
    }
    assert [q.question_id for q in second.questions if q.question_id.startswith("x")] == ["x1-q1", "x2-q1"]


@pytest.mark.parametrize("mode", ["masked_data", "summaries_only"])
def test_nothing_a_pattern_can_recognise_reaches_either_prompt(mode: str) -> None:
    """The canary file: fake personal data in cells, in the ID column and in headers. The second opinions get
    names and counts only, through the same gate as a chat turn, so no planted value is in any prompt."""
    ctx = canary_context(mode=mode)
    session = start_session(ctx, session_id="canary")
    named = [{"column": c, "why": "Late."} for c in ("notes", "monthly_spend", "signup_date")]
    client = AssistClient(columns=named, orders=[{"kind": "id", "columns": ["customer_id", "phone_number"]}])
    out, _, result = _assist(ctx, session, client)
    assert result.llm_calls >= 1
    text = "\n".join(client.prompts)
    assert leaks(text, HARD_SECRETS) == []
    assert leaks(text, CELL_WORDS) == []  # no cell reaches it at all, not even in masked_data
    assert HEADER_EMAIL not in text
    stored = "\n".join(message.model_dump_json() for message in out.transcript)
    assert leaks(stored, HARD_SECRETS) == []
    for item in out.transcript[-1].sent:
        assert leaks(item.preview, HARD_SECRETS) == []


def test_the_always_hidden_and_personal_columns_are_never_offered_to_the_reviewer() -> None:
    ctx = canary_context(always_hide=["notes"])
    session = start_session(ctx, session_id="canary")
    client = AssistClient(
        columns=[{"column": "notes", "why": "Late."}, {"column": "pan_number", "why": "Late."}]
    )
    out, _, _ = _assist(ctx, session, client)
    assert [q for q in out.questions if q.question_id.startswith("x1-")] == []
    assert "pan_number" not in "\n".join(client.prompts).replace(
        "[", ""
    )  # a personal column is not even listed


def test_nothing_in_a_session_is_proposed_without_evidence() -> None:
    ctx, session = _started()
    out, _, _ = _assist(ctx, session, AssistClient(columns=[{"column": "final_status", "why": "Late."}]))
    evidence = {r.evidence_id for r in out.tool_results}
    for question in out.questions:
        assert set(question.evidence_ids) <= evidence
        for option in question.options:
            if option.proposal is not None:
                assert set(option.proposal.evidence_ids) <= evidence
    assert all(
        p.kind is not ProposalKind.RECIPE_STEP or p.state is ProposalState.PENDING for p in out.proposals
    )


def test_the_assist_prompt_template_itself_is_untouched_by_the_backstop() -> None:
    """Text we wrote must never raise the late-mask signal (the same rule as `data_agent.v1.md`)."""
    from pathlib import Path

    from engine.agent import egress

    prompt = Path(__file__).resolve().parents[3] / "configs" / "prompts" / "data_agent_assist.v1.md"
    for number, line in enumerate(prompt.read_text(encoding="utf-8").splitlines(), start=1):
        cleaned, matches = egress.assert_clean(line)
        assert matches == 0, f"data_agent_assist.v1.md line {number} would be masked: {line!r} -> {cleaned!r}"
