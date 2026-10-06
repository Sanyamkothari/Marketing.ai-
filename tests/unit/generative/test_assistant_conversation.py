"""The assistant as a conversation (Plan I, DEC-1280 … DEC-1289).

Four additions, each proved from the fake client's own call log or from the artefact, never from a
claim the code makes about itself:

**A follow-up is rewritten before it is searched, and only a follow-up.** With history, exactly one
`assistant_condense` call is made and metered, retrieval embeds what it returned, and the answer
prompt still carries the question as asked. Without history no such call exists - which is what
keeps a first question, and every graded one, at the cost it had before. A rewrite that fails is
searched as asked and the reason is on the answer; it never fails the answer.

**An answer the faithfulness check refuses is written again, a bounded number of times.** The count
is `guardrails.retries` from the policy, each retry carries the stricter instruction, and a block by
any other rule is not retried because the stricter instruction cannot change it.

**Confidence is a rule over measured signals.** `confidence_for` is tested as a table, and the flow
is shown to hand it the real values.

**Starter questions are reference questions the index passed.** Nothing else qualifies.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime

import pytest

from engine.generative.assistant import (
    CONDENSE_MAX_CHARS,
    HIGH_FAITHFULNESS,
    STRONG_MARGIN,
    WEAK_MARGIN,
    Turn,
    answer,
    confidence_for,
    suggested_questions,
)
from engine.generative.contracts import (
    AssistantAnswer,
    Citation,
    Confidence,
    GenerativePurpose,
    RagEval,
    RagEvalQuestion,
)
from engine.generative.errors import BUDGET_EXCEEDED, MODEL_OUTPUT_MALFORMED, GenerativeError
from engine.generative.evaluation import aggregate
from engine.generative.guardrails import GuardrailPolicy, Guardrails, load_policy
from engine.llm import FakeLLMClient, FakeLLMMode, LLMCompletion, LLMError
from tests.unit.generative.test_assistant import (
    ANSWERED,
    DISJOINT,
    FLOOR,
    REFUSAL,
    KnowledgeIndex,
    answering,
    ask,
    completes,
    knowledge_index,
    meter_for,
    use_case,
)

__all__ = ["knowledge_index"]  # the shared, module-scoped index, re-used rather than rebuilt

FOLLOW_UP = "And how much does it cost?"
HISTORY = (
    Turn(role="user", text=ANSWERED),
    Turn(role="assistant", text="Usually within four hours of the verification call."),
)


class Scripted(FakeLLMClient):
    """The grounded fake, with chosen replies for the rewrite or the judge, and a log of both.

    `condense` is what the rewrite call returns (a string), or an `LLMError` it raises instead.
    `judge_scores` are the faithfulness scores returned in order; once used up the fake's own
    verdict (a pass) is returned.
    """

    def __init__(
        self,
        *,
        condense: str | LLMError | None = None,
        judge_scores: Sequence[float] = (),
    ) -> None:
        super().__init__(mode=FakeLLMMode.GROUNDED)
        self._condense_reply = condense
        self._scores = list(judge_scores)

    def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        model_id: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.0,
    ) -> LLMCompletion:
        real = super().complete(
            prompt, system=system, model_id=model_id, max_tokens=max_tokens, temperature=temperature
        )
        if "FOLLOW-UP QUESTION:" in prompt and self._condense_reply is not None:
            if isinstance(self._condense_reply, LLMError):
                raise self._condense_reply
            return real.model_copy(update={"text": self._condense_reply})
        judging = "GENERATED TEXT" in prompt or "TEXT TO CHECK" in prompt
        if judging and self._scores:
            score = self._scores.pop(0)
            verdict = {"score": score, "supported": 1, "unsupported": 0, "reason": "scripted"}
            return real.model_copy(update={"text": json.dumps(verdict)})
        return real


def run(
    index: KnowledgeIndex,
    question: str,
    client: FakeLLMClient,
    *,
    history: tuple[Turn, ...] = (),
    policy: GuardrailPolicy | None = None,
) -> AssistantAnswer:
    meter = meter_for(client)
    return answer(
        question,
        index_id=index.index_id,
        use_case=use_case(),
        store=index.store,
        meter=meter,
        guardrails=Guardrails(policy or load_policy(), meter=meter),
        history=history,
    )


def condense_calls(client: FakeLLMClient) -> list[str]:
    return [call.prompt for call in completes(client) if "FOLLOW-UP QUESTION:" in call.prompt]


def embedded(client: FakeLLMClient) -> list[str]:
    return [text for call in client.calls if call.kind == "embed" for text in call.texts]


# ---------------------------------------------------------------------------
# Rewriting a follow-up
# ---------------------------------------------------------------------------
def test_a_first_question_makes_no_rewrite_call_and_is_searched_as_asked(
    knowledge_index: KnowledgeIndex,
) -> None:
    client, meter, result = ask(knowledge_index, ANSWERED)
    assert condense_calls(client) == []
    assert GenerativePurpose.ASSISTANT_CONDENSE not in {u.purpose for u in meter.usage().by_purpose}
    assert embedded(client) == [ANSWERED]
    assert result.searched_for is None and result.condense_error is None


def test_a_follow_up_is_rewritten_once_metered_and_searched_with_the_rewrite(
    knowledge_index: KnowledgeIndex,
) -> None:
    client, meter, result = ask(knowledge_index, FOLLOW_UP, history=HISTORY)
    assert len(condense_calls(client)) == 1
    usage = {u.purpose: u.calls for u in meter.usage().by_purpose}
    assert usage[GenerativePurpose.ASSISTANT_CONDENSE] == 1
    assert result.searched_for and result.searched_for != FOLLOW_UP
    assert ANSWERED in result.searched_for, "the fake carries the earlier question's words"
    assert embedded(client) == [result.searched_for], "retrieval searched with the rewrite"
    prompt = answering(client)[0].prompt
    assert f"Question: {FOLLOW_UP}" in prompt, "the answer prompt keeps the question as asked"
    assert "Earlier in this conversation" in prompt
    assert result.question == FOLLOW_UP
    assert result.condense_error is None


def test_the_rewrite_sees_the_conversation_and_the_follow_up(knowledge_index: KnowledgeIndex) -> None:
    client, _, _ = ask(knowledge_index, FOLLOW_UP, history=HISTORY)
    (prompt,) = condense_calls(client)
    assert ANSWERED in prompt and HISTORY[1]["text"] in prompt and FOLLOW_UP in prompt


@pytest.mark.parametrize(
    "reply",
    [
        "Sure! The standalone question is about prices.",
        json.dumps({"question": ""}),
        json.dumps({"something": "else"}),
        json.dumps({"question": "word " * (CONDENSE_MAX_CHARS // 4)}),
    ],
)
def test_an_unreadable_rewrite_falls_back_to_the_question_as_asked_and_says_so(
    knowledge_index: KnowledgeIndex, reply: str
) -> None:
    client = Scripted(condense=reply)
    result = run(knowledge_index, ANSWERED, client, history=HISTORY)
    assert result.condense_error == MODEL_OUTPUT_MALFORMED
    assert result.searched_for is None
    assert embedded(client) == [ANSWERED]
    assert not result.refused, "a failed rewrite is never a failed answer"


def test_a_rewrite_the_ai_service_failed_falls_back_and_records_its_code(
    knowledge_index: KnowledgeIndex,
) -> None:
    client = Scripted(condense=LLMError("LLM_UNAVAILABLE", "The AI service could not be reached."))
    result = run(knowledge_index, ANSWERED, client, history=HISTORY)
    assert result.condense_error == "LLM_UNAVAILABLE"
    assert result.searched_for is None
    assert not result.refused


def test_running_out_of_budget_on_the_rewrite_is_not_hidden_as_a_fallback(
    knowledge_index: KnowledgeIndex,
) -> None:
    from engine.config import BudgetConfig, LlmConfig
    from engine.generative.budget import Meter

    client = FakeLLMClient(mode=FakeLLMMode.GROUNDED)
    meter = Meter(
        client, job_id="x_20260101_abcdef01", llm=LlmConfig(), budget=BudgetConfig(max_calls_per_run=1)
    )
    with pytest.raises(GenerativeError) as caught:
        answer(
            FOLLOW_UP,
            index_id=knowledge_index.index_id,
            use_case=use_case(),
            store=knowledge_index.store,
            meter=meter,
            guardrails=Guardrails(load_policy(), meter=meter),
            history=(*HISTORY, *HISTORY),
        )
    assert caught.value.code == BUDGET_EXCEEDED


def test_a_floor_refusal_after_a_rewrite_still_says_what_was_searched(
    knowledge_index: KnowledgeIndex,
) -> None:
    client = Scripted(condense=json.dumps({"question": DISJOINT}))
    result = run(knowledge_index, "and who painted it?", client, history=HISTORY)
    assert result.refused and result.called_model is False
    assert result.searched_for == DISJOINT
    assert result.confidence is None and result.attempts == 0


# ---------------------------------------------------------------------------
# Writing an unfaithful answer again
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("retries", [0, 1, 2, 3])
def test_an_answer_the_faithfulness_check_keeps_refusing_is_tried_retries_plus_one_times(
    knowledge_index: KnowledgeIndex, retries: int
) -> None:
    policy = load_policy()
    policy = GuardrailPolicy(**{**policy.__dict__, "retries": retries})
    client = FakeLLMClient(mode=FakeLLMMode.FAILING_JUDGE)
    result = run(knowledge_index, ANSWERED, client, policy=policy)
    assert len(answering(client)) == retries + 1
    assert result.attempts == retries + 1
    assert len(result.retried_after) == retries
    assert all(check.rule == "judge_faithfulness" for check in result.retried_after)
    assert result.refused and result.answer == REFUSAL and result.citations == ()
    assert result.confidence is None


def test_the_shipped_policy_retries_twice() -> None:
    assert load_policy().retries == 2


def test_a_retry_carries_the_stricter_instruction_and_the_first_attempt_does_not(
    knowledge_index: KnowledgeIndex,
) -> None:
    client = FakeLLMClient(mode=FakeLLMMode.FAILING_JUDGE)
    run(knowledge_index, ANSWERED, client)
    prompts = [call.system for call in answering(client)]
    assert "answered again" not in prompts[0]
    assert "attempt 2" in prompts[1] and "attempt 3" in prompts[2]


def test_a_retry_that_passes_is_returned_and_marked_low_confidence(
    knowledge_index: KnowledgeIndex,
) -> None:
    client = Scripted(judge_scores=[0.2])
    result = run(knowledge_index, ANSWERED, client)
    assert len(answering(client)) == 2, "stops at the first answer that passes"
    assert result.attempts == 2
    assert not result.refused and result.citations
    assert [check.rule for check in result.retried_after] == ["judge_faithfulness"]
    assert result.confidence is not None
    assert result.confidence.level is Confidence.LOW
    assert result.confidence.retried is True


def test_a_block_by_another_rule_is_not_retried(knowledge_index: KnowledgeIndex) -> None:
    client = FakeLLMClient(mode=FakeLLMMode.BANNED)
    result = run(knowledge_index, ANSWERED, client)
    assert len(answering(client)) == 1
    assert result.attempts == 1 and result.retried_after == ()
    assert result.refused


def test_an_answer_that_passes_first_time_is_one_attempt(knowledge_index: KnowledgeIndex) -> None:
    client, _, result = ask(knowledge_index, ANSWERED)
    assert len(answering(client)) == 1
    assert result.attempts == 1 and result.retried_after == ()


# ---------------------------------------------------------------------------
# Confidence
# ---------------------------------------------------------------------------
def cite(quote: str = "within four hours") -> Citation:
    return Citation(chunk_id="c-1", document="d.md", section="s", quote=quote, similarity=0.5)


STRONG = FLOOR + STRONG_MARGIN + 0.01
FAIR = FLOOR + (WEAK_MARGIN + STRONG_MARGIN) / 2
WEAK = FLOOR + WEAK_MARGIN / 2


@pytest.mark.parametrize(
    ("top", "citations", "faithfulness", "retried", "expected"),
    [
        (STRONG, [cite()], 1.0, False, Confidence.HIGH),
        (STRONG, [cite()], HIGH_FAITHFULNESS, False, Confidence.HIGH),
        (STRONG, [cite()], HIGH_FAITHFULNESS - 0.01, False, Confidence.MEDIUM),
        (STRONG, [cite()], None, False, Confidence.MEDIUM),
        (FAIR, [cite()], 1.0, False, Confidence.MEDIUM),
        (WEAK, [cite()], 1.0, False, Confidence.LOW),
        (STRONG, [cite("")], 1.0, False, Confidence.LOW),
        (STRONG, [], 1.0, False, Confidence.LOW),
        (STRONG, [cite()], 1.0, True, Confidence.LOW),
    ],
)
def test_the_confidence_rule(
    top: float,
    citations: list[Citation],
    faithfulness: float | None,
    retried: bool,
    expected: Confidence,
) -> None:
    result = confidence_for(
        refused=False,
        top_similarity=top,
        floor=FLOOR,
        citations=citations,
        faithfulness=faithfulness,
        retried=retried,
    )
    assert result is not None
    assert result.level is expected
    assert result.reasons, "every level says why"
    assert result.verified_citations == sum(1 for c in citations if c.quote)
    assert result.retried is retried


def test_a_refusal_has_no_confidence() -> None:
    assert (
        confidence_for(
            refused=True, top_similarity=0.9, floor=FLOOR, citations=[cite()], faithfulness=1.0, retried=False
        )
        is None
    )


def test_the_answer_carries_the_signals_it_was_rated_on(knowledge_index: KnowledgeIndex) -> None:
    _, _, result = ask(knowledge_index, ANSWERED)
    assert result.confidence is not None
    signals = result.confidence
    assert signals.faithfulness == 1.0, "the fake judge's score, read off the check that ran"
    assert signals.floor == FLOOR
    assert signals.verified_citations == sum(1 for c in result.citations if c.quote)
    assert signals.top_similarity >= max(c.similarity for c in result.citations)


def test_an_unjudged_answer_is_never_high_confidence(knowledge_index: KnowledgeIndex) -> None:
    _, _, result = ask(knowledge_index, ANSWERED, judged=False)
    assert result.confidence is not None
    assert result.confidence.faithfulness is None
    assert result.confidence.level is not Confidence.HIGH


def test_a_refusal_the_model_made_has_no_confidence(knowledge_index: KnowledgeIndex) -> None:
    _, _, result = ask(knowledge_index, ANSWERED, mode=FakeLLMMode.REFUSING)
    assert result.refused and result.confidence is None


# ---------------------------------------------------------------------------
# Starter questions
# ---------------------------------------------------------------------------
def row(question: str, **overrides: object) -> RagEvalQuestion:
    values: dict[str, object] = {
        "question": question,
        "answer": "An answer.",
        "refused": False,
        "expect_refusal": False,
        "faithfulness": 1.0,
        "correctness": 1.0,
        "passed": True,
        "latency_ms": 1,
    }
    values.update(overrides)
    return RagEvalQuestion(**values)  # type: ignore[arg-type]


def graded(*rows: RagEvalQuestion) -> RagEval:
    return RagEval(
        index_id="i",
        reference_set_fingerprint="f",
        questions=rows,
        aggregates=aggregate(rows, pass_threshold=0.75),
        prompt_versions={},
        graded_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_starter_questions_are_only_the_reference_rows_that_passed_and_were_answered() -> None:
    result = suggested_questions(
        graded(
            row("Failed one?", passed=False),
            row("Should refuse?", expect_refusal=True, refused=True),
            row("Refused wrongly?", refused=True),
            row("Errored?", refused=None, error_code="LLM_UNAVAILABLE"),
            row("Good one?"),
        )
    )
    assert result == ("Good one?",)


def test_starter_questions_are_at_most_four_best_supported_first_each_once() -> None:
    rows = [row(f"Question {n}?", faithfulness=n / 10) for n in range(1, 8)]
    rows.append(row("question 7?", faithfulness=0.7))
    result = suggested_questions(graded(*rows))
    assert result == ("Question 7?", "Question 6?", "Question 5?", "Question 4?")


def test_an_index_never_graded_has_no_starter_questions() -> None:
    assert suggested_questions(None) == ()
    assert suggested_questions(graded()) == ()
