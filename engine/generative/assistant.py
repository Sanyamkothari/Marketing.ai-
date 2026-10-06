"""Answering one question from one index: retrieve, ground, check, or refuse.

The flow is short and its order is the substance:

1. **Embed the question and retrieve.** `retrieval` applies the similarity floor and the MMR
   de-duplication; what comes back is everything the model will be allowed to see.
2. **If nothing survived the floor, refuse - without calling a model.** That is the single most
   important line in this module. A question the documents do not answer costs one embedding and
   nothing else, the refusal is the operator's configured sentence rather than a model's improvised
   one, and no generation can hallucinate an answer that was never asked for.
3. **Otherwise render the prompt with numbered extracts, and call once.** The numbering is what a
   citation refers to, so the model cites a position rather than inventing a filename.
4. **Parse, and drop any citation that points nowhere, and any quote the chunk does not contain.**
   A model that cites extract 7 when six were supplied has said something about a document that was
   not in front of it; the claim survives, the false citation does not, and the answer is marked for
   the guardrails. A quote is checked the same way and for the same reason: the words shown under a
   real document, a real heading and a real chunk id have to be that chunk's own words, or the
   citation's whole purpose - letting a reader check the claim - is served by something invented.
5. **Check, then return.** The faithfulness judge is given exactly the extracts as its source, so
   "is every claim supported?" is asked against the same text the prompt was. An answer the judge
   refuses is written again with a stricter instruction, up to `guardrails.retries` times, before
   the refusal is returned (DEC-1281); every attempt is a metered call.

Plan I added the conversation around this (DEC-1280 … DEC-1289): a follow-up asked with earlier
turns is first rewritten to stand alone, and that rewrite is what step 1 searches with (`condense`;
no call at all without history); every answered question carries a confidence label worked out from
measured signals only (`confidence_for`); and an empty chat offers starter questions taken from the
reference rows the index passed (`suggested_questions`).

Nothing here writes a file. An answer is a response body and a row of `rag_eval.json`, and the
caller decides which; keeping the flow free of storage is what lets the evaluation run it a hundred
times without a hundred artefacts.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from engine.config import UseCaseConfig
from engine.generative.budget import Meter
from engine.generative.contracts import (
    AnswerConfidence,
    AssistantAnswer,
    Citation,
    Confidence,
    GenerativePurpose,
    GuardrailCheck,
    GuardrailOutcome,
    JudgeScore,
    RagEval,
)
from engine.generative.errors import MODEL_OUTPUT_MALFORMED
from engine.generative.guardrails import CheckContext, Guardrails
from engine.generative.prompts import load_prompt, render
from engine.generative.rerank import Reranker
from engine.generative.retrieval import Retrieved, retrieve
from engine.generative.vectorstore import Match, VectorStore
from engine.llm import LLMError
from engine.utils.logging import get_logger, log_failure

__all__ = [
    "ANSWER_PROMPT",
    "CONDENSE_MAX_CHARS",
    "CONDENSE_PROMPT",
    "FAITHFULNESS_RULE",
    "HIGH_FAITHFULNESS",
    "HISTORY_TURNS",
    "QUOTE_WORDS",
    "STRONG_MARGIN",
    "SUGGESTED_QUESTIONS",
    "UNKNOWN_CITATION",
    "UNSUPPORTED_QUOTE",
    "WEAK_MARGIN",
    "Turn",
    "answer",
    "condense",
    "confidence_for",
    "extracts_for",
    "suggested_questions",
]

_LOGGER = get_logger(__name__)

ANSWER_PROMPT: Final[str] = "assistant_answer"
HISTORY_TURNS: Final[int] = 6
"""How many earlier turns the prompt carries. Six is three exchanges - enough for "and the other one?"

Nothing is stored server-side: the client sends the conversation it has, and the assistant answers
this question with that context and forgets it again. A longer window would cost tokens on every
question to serve the rare conversation that needs it.
"""

CONDENSE_PROMPT: Final[str] = "assistant_condense"
CONDENSE_MAX_CHARS: Final[int] = 600
"""The longest rewritten question kept. The prompt asks for at most 60 words; a reply far past that
is not a question any more, so it is treated as unreadable and the question is searched as asked."""

FAITHFULNESS_RULE: Final[str] = GenerativePurpose.JUDGE_FAITHFULNESS.value
"""The rule name a failed faithfulness check is recorded under, and the one block that is retried."""

HIGH_FAITHFULNESS: Final[float] = 0.90
"""The faithfulness score at or above which an answer can be called high confidence."""

STRONG_MARGIN: Final[float] = 0.15
"""How far above the floor the closest passage must be for high confidence (DEC-1284)."""

WEAK_MARGIN: Final[float] = 0.05
"""Closer to the floor than this, and the closest passage makes the answer low confidence."""

SUGGESTED_QUESTIONS: Final[int] = 4
"""Starter questions shown on an empty chat, at most."""

QUOTE_WORDS: Final[int] = 25
"""The longest quote a citation may carry, trimmed here rather than trusted from the model."""

UNKNOWN_CITATION: Final[str] = "UNKNOWN_CITATION"
"""Recorded when a model cited an extract number nobody supplied. The citation is dropped."""

UNSUPPORTED_QUOTE: Final[str] = "UNSUPPORTED_QUOTE"
"""Recorded when a quote is not in the chunk it was attributed to. The quote is dropped, not the
citation: the chunk id, the document, the heading and the similarity are the engine's own facts and
stay true, and only the words the model put between quotation marks are taken away (DEC-226)."""

_CODE_FENCE: Final[re.Pattern[str]] = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


class Turn(dict[str, str]):
    """One earlier exchange, as the client sends it: `{"role": ..., "text": ...}`.

    A `dict` subclass rather than a model because it crosses the API boundary as JSON and is handed
    straight to a template; validating it into a frozen object and back would buy nothing.
    """


def extracts_for(matches: Sequence[Match]) -> tuple[dict[str, str], ...]:
    """The numbered extracts a prompt renders, in retrieval order.

    The prompt numbers them by position, so this order *is* the citation vocabulary: extract 1 is
    the first element here and nothing else.
    """
    return tuple(
        {"document": match.chunk.document, "section": match.chunk.section, "text": match.chunk.text}
        for match in matches
    )


def answer(
    question: str,
    *,
    index_id: str,
    use_case: UseCaseConfig,
    store: VectorStore,
    meter: Meter,
    guardrails: Guardrails,
    history: Sequence[Mapping[str, str]] = (),
    config_root: Path | None = None,
    reranker: Reranker | None = None,
) -> AssistantAnswer:
    """Answer `question` from `index_id`, or refuse because the documents do not answer it.

    With `history` (earlier turns, oldest first) a follow-up is first rewritten to stand alone and
    that is what is searched for (`condense`, DEC-1280); the answer prompt still sees the question
    as asked and the conversation. Without history - which includes every graded question - no
    rewriting call is made at all.
    """
    started = time.monotonic()
    rag = use_case.generative.rag
    turns = list(history)[-HISTORY_TURNS:]
    searched, condense_error = condense(question, turns, meter=meter, config_root=config_root)
    (question_vector,) = meter.embed([searched])
    found = retrieve(store, index_id, question_vector, config=rag, question_text=searched, reranker=reranker)
    searched_for = searched if searched != question else None

    if found.empty:
        return _refusal(question, found, started, rag.refusal_message, config_root).model_copy(
            update={"searched_for": searched_for, "condense_error": condense_error}
        )

    prompt = load_prompt(ANSWER_PROMPT, config_root)
    context = CheckContext(
        target=question[:80],
        expected_language=None if rag.answer_language == "auto" else rag.answer_language,
        source="\n\n".join(match.chunk.text for match in found.matches),
        judges=("faithfulness",),
    )
    retried_after: list[GuardrailCheck] = []
    attempts = 0
    while True:
        attempts += 1
        rendered = render(
            prompt,
            {
                "question": question,
                "chunks": extracts_for(found.matches),
                "refusal_message": rag.refusal_message,
                "answer_language": rag.answer_language,
                "history": turns,
                "strict": attempts > 1,
                "attempt": attempts,
            },
        )
        completion = meter.complete(rendered, GenerativePurpose.ASSISTANT_ANSWER)
        text, refused, citations, unknown = _parse(
            completion.text, found.matches, question=question, refusal=rag.refusal_message
        )
        result = guardrails.check(text, context)
        checks = (*unknown, *result.checks)
        if result.passed or result.blocked_by != FAITHFULNESS_RULE or attempts > guardrails.retries:
            break
        # Only an unfaithful answer is written again: the stricter instruction is a fix for claims
        # the extracts do not support, and would change nothing about a phone number or a banned
        # phrase, which a deterministic rule blocks the same way every time (DEC-1281).
        retried_after.extend(check for check in result.checks if check.rule == FAITHFULNESS_RULE)
        _LOGGER.info("assistant.retry attempt=%d rule=%s", attempts, result.blocked_by)

    common: dict[str, Any] = {
        "question": question,
        "retrieved": len(found.matches),
        "called_model": True,
        "prompt_version": prompt.version,
        "guardrails": checks,
        "searched_for": searched_for,
        "condense_error": condense_error,
        "attempts": attempts,
        "retried_after": tuple(retried_after),
    }
    if not result.passed:
        _LOGGER.info("assistant.blocked rule=%s attempts=%d", result.blocked_by, attempts)
        return AssistantAnswer(
            answer=rag.refusal_message,
            refused=True,
            citations=(),
            latency_ms=_elapsed(started),
            **common,
        )

    kept = () if refused else citations
    return AssistantAnswer(
        answer=text,
        refused=refused,
        citations=kept,
        latency_ms=_elapsed(started),
        confidence=confidence_for(
            refused=refused,
            top_similarity=max(match.similarity for match in found.matches),
            floor=rag.min_similarity,
            citations=kept,
            faithfulness=_faithfulness(result.judge_scores),
            retried=attempts > 1,
        ),
        **common,
    )


def condense(
    question: str,
    history: Sequence[Mapping[str, str]],
    *,
    meter: Meter,
    config_root: Path | None = None,
) -> tuple[str, str | None]:
    """The question to search with, and why rewriting failed (an error code) or `None`.

    With no history the question is returned as it is, and **no call is made**: a first question,
    and every question a grading run asks, costs exactly what it cost before (DEC-1280). With history
    one call to the generating model rewrites the follow-up to stand alone - "and for business
    customers?" becomes a question retrieval can match. When that call fails (the AI service
    refused or was unreachable) or its reply cannot be read, the question is searched as asked and
    the reason is returned, so the answer says it; a failed rewrite is never a failed answer. Running
    out of budget is not a failed rewrite: it would stop the answer's own call too, so it is raised.
    """
    if not history:
        return question, None
    prompt = load_prompt(CONDENSE_PROMPT, config_root)
    rendered = render(prompt, {"question": question, "history": list(history)[-HISTORY_TURNS:]})
    try:
        completion = meter.complete(rendered, GenerativePurpose.ASSISTANT_CONDENSE)
    except LLMError as exc:
        log_failure(_LOGGER, "assistant.condense", exc)
        return question, exc.code
    payload = _json(completion.text)
    standalone = " ".join(str(payload.get("question") or "").split()) if payload else ""
    if not standalone or len(standalone) > CONDENSE_MAX_CHARS:
        _LOGGER.info("assistant.condense_unreadable chars=%d", len(standalone))
        return question, MODEL_OUTPUT_MALFORMED
    return standalone, None


def confidence_for(
    *,
    refused: bool,
    top_similarity: float,
    floor: float,
    citations: Sequence[Citation],
    faithfulness: float | None,
    retried: bool,
) -> AnswerConfidence | None:
    """High, medium or low for one answer, from four measured signals and nothing else (DEC-1284).

    A refusal has no confidence: it claims nothing. Otherwise:

    * **Low** when any of: the first answer failed the faithfulness check and had to be rewritten;
      no citation carries a quote found in its passage; or the closest passage cleared the floor by
      less than `WEAK_MARGIN`.
    * **High** when all of: no retry; the faithfulness check ran and scored at least
      `HIGH_FAITHFULNESS`; at least one citation carries a checked quote; and the closest passage
      cleared the floor by at least `STRONG_MARGIN`.
    * **Medium** otherwise.

    Similarity is read against the floor rather than as a bare number because the floor is
    calibrated per embedding model (DEC-218): 0.4 is a strong match for one model and a weak one for
    another, while "well above the point this index stops answering" means the same for both.
    """
    if refused:
        return None
    verified = sum(1 for citation in citations if citation.quote)
    margin = top_similarity - floor
    signals: dict[str, Any] = {
        "top_similarity": round(top_similarity, 4),
        "floor": floor,
        "verified_citations": verified,
        "faithfulness": None if faithfulness is None else round(faithfulness, 4),
        "retried": retried,
    }
    low: list[str] = []
    if retried:
        low.append("The first answer did not pass the check against the documents and was rewritten.")
    if verified == 0:
        low.append("No citation carries a quote found in its passage.")
    if margin < WEAK_MARGIN:
        low.append("The closest passage was only just similar enough to be used.")
    if low:
        return AnswerConfidence(level=Confidence.LOW, reasons=tuple(low), **signals)
    missing: list[str] = []
    if faithfulness is None:
        missing.append("The answer was not checked against the documents.")
    elif faithfulness < HIGH_FAITHFULNESS:
        missing.append(
            f"The check against the documents scored {faithfulness:.2f}, below {HIGH_FAITHFULNESS:.2f}."
        )
    if margin < STRONG_MARGIN:
        missing.append("The closest passage was a fair match rather than a strong one.")
    if missing or faithfulness is None:
        return AnswerConfidence(level=Confidence.MEDIUM, reasons=tuple(missing), **signals)
    return AnswerConfidence(
        level=Confidence.HIGH,
        reasons=(
            f"The check against the documents scored {faithfulness:.2f}.",
            f"{verified} citation{'s' if verified != 1 else ''} with a quote found in the passage.",
            "The closest passage was a strong match.",
        ),
        **signals,
    )


def suggested_questions(rag_eval: RagEval | None, limit: int = SUGGESTED_QUESTIONS) -> tuple[str, ...]:
    """Up to `limit` starter questions, each one this index was graded on and answered well.

    Taken only from the reference set's own rows - never written by a model - and only from rows
    that passed, were meant to be answered (not refused), were answered, and did not error: a
    starter that leads to a refusal or a wrong answer would teach a new user the wrong thing about
    the assistant. Best-supported first (faithfulness, then correctness), each question once.
    """
    if rag_eval is None or limit < 1:
        return ()
    rows = [
        row
        for row in rag_eval.questions
        if row.passed and not row.expect_refusal and row.refused is False and row.error_code is None
    ]
    rows.sort(key=lambda row: (-(row.faithfulness or 0.0), -(row.correctness or 0.0)))
    chosen: list[str] = []
    seen: set[str] = set()
    for row in rows:
        text = " ".join(row.question.split())
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            chosen.append(text)
        if len(chosen) == limit:
            break
    return tuple(chosen)


def _refusal(
    question: str,
    found: Retrieved,
    started: float,
    message: str,
    config_root: Path | None,
) -> AssistantAnswer:
    """The answer when nothing passed the floor: the configured sentence, and no model call.

    `called_model=False` is what a cost screen reads to explain a question that cost an embedding
    and nothing else, and what a test asserts to prove the floor really does come first.
    """
    _LOGGER.info("assistant.refused considered=%d above_floor=%d", found.considered, found.above_floor)
    return AssistantAnswer(
        question=question,
        answer=message,
        refused=True,
        citations=(),
        retrieved=0,
        called_model=False,
        prompt_version=load_prompt(ANSWER_PROMPT, config_root).version,
        latency_ms=_elapsed(started),
        guardrails=(),
    )


def _faithfulness(scores: Sequence[JudgeScore]) -> float | None:
    """The faithfulness judge's score among `scores`, or `None` when it did not run."""
    return next(
        (score.score for score in scores if score.purpose is GenerativePurpose.JUDGE_FAITHFULNESS),
        None,
    )


def _parse(
    raw: str,
    matches: Sequence[Match],
    *,
    question: str,
    refusal: str,
) -> tuple[str, bool, tuple[Citation, ...], tuple[GuardrailCheck, ...]]:
    """The model's answer, its refusal flag, its citations, and a check for every one it invented.

    A malformed reply is treated as a refusal rather than as a failure: the model said something
    the contract cannot read, and showing a customer an unparsed blob would be worse than saying
    the documents do not cover it. So the reply itself is *dropped* and the operator's refusal
    sentence is returned in its place - returning the blob under `refused=True` would satisfy the
    flag while still putting the thing on the screen, which is the outcome this paragraph exists
    to prevent. The raw reply is not lost: it is what the model was metered for, and the guardrail
    check records that the parse is what failed.

    `question` is what every check here is targeted at, matching what `Guardrails.check` targets
    for the checks it appends to the same tuple. A caller reading `answer.guardrails` is looking at
    one list, and a `target` that means the question in one row and the answer in the next cannot
    be read at all.

    Two things are checked against what was actually supplied rather than read off the reply: the
    extract number, and the quote. Everything else on a `Citation` - the chunk id, the document,
    the heading, the similarity - is the engine's own and is copied from the match, so a fabricated
    quote would be the one invented thing in a row of otherwise real provenance, which is the worst
    place for it (DEC-226).
    """
    payload = _json(raw)
    if payload is None:
        return (
            refusal,
            True,
            (),
            (
                GuardrailCheck(
                    target=question[:60],
                    rule=MODEL_OUTPUT_MALFORMED,
                    outcome=GuardrailOutcome.BLOCKED,
                    detail="the reply was not a JSON object, so it was dropped for the refusal",
                ),
            ),
        )
    text = str(payload.get("answer", "")).strip()
    refused = bool(payload.get("refused", False))
    citations: list[Citation] = []
    checks: list[GuardrailCheck] = []
    for entry in payload.get("citations", []) or []:
        if not isinstance(entry, dict):
            checks.append(_unknown_citation(question))
            continue
        number = _number(entry.get("chunk"))
        if number is None or not 1 <= number <= len(matches):
            checks.append(_unknown_citation(question))
            continue
        match = matches[number - 1]
        quote, invented = _verified_quote(entry.get("quote", ""), match.chunk.text)
        if invented:
            checks.append(_unsupported_quote(question))
        citations.append(
            Citation(
                chunk_id=match.chunk.chunk_id,
                document=match.chunk.document,
                section=match.chunk.section,
                quote=quote,
                similarity=round(match.similarity, 4),
            )
        )
    return text, refused, tuple(citations), tuple(checks)


def _verified_quote(claimed: object, chunk_text: str) -> tuple[str, bool]:
    """The quote a citation may carry, and whether the model claimed one the chunk does not contain.

    Trimming to `QUOTE_WORDS` bounds how much is shown and says nothing about where the words came
    from, so the trimmed quote is looked for in the cited chunk before it is allowed to appear under
    that chunk's id. Whitespace and case are flattened on both sides first: a chunk reaches a prompt
    through a template and comes back inside a JSON string, so a passage copied faithfully can still
    arrive re-wrapped or with its first letter changed, and refusing those would throw away quotes
    that are exactly as checkable as the ones that survive. A quote found that way is the chunk's own
    words; anything else is words nobody wrote.

    A reply that quoted nothing is not the same failure and gets no check: the model claimed no
    words, so it invented none, and a citation with an empty quote shows a reader nothing rather than
    showing them something false. `null` is one of the ways a model writes that, so it is read as an
    absent quote rather than stringified into the word it spells.
    """
    quote = "" if claimed is None else " ".join(str(claimed).split()[:QUOTE_WORDS])
    if not quote:
        return "", False
    if _flattened(quote) in _flattened(chunk_text):
        return quote, False
    return "", True


def _flattened(text: str) -> str:
    """`text` with its case and its runs of whitespace taken out, for comparing one passage to another."""
    return " ".join(text.lower().split())


def _unknown_citation(question: str) -> GuardrailCheck:
    """The check recorded for a citation that cannot be resolved to a supplied extract.

    One entry not naming a number and one naming a number nobody supplied are the same failure -
    a claim that pointed somewhere the evidence pack does not reach - so both go through this one
    place rather than building the same `GuardrailCheck` twice.
    """
    return GuardrailCheck(
        target=question[:60],
        rule=UNKNOWN_CITATION,
        outcome=GuardrailOutcome.WARNED,
        detail="a citation pointed at an extract that was not supplied",
    )


def _unsupported_quote(question: str) -> GuardrailCheck:
    """The check recorded for a quote the cited chunk does not contain.

    `WARNED` rather than `BLOCKED`, because nothing was refused: the answer stands, the citation
    stands, and what was dropped is the one part of the row the model made up.
    """
    return GuardrailCheck(
        target=question[:60],
        rule=UNSUPPORTED_QUOTE,
        outcome=GuardrailOutcome.WARNED,
        detail="a citation quoted words the cited extract does not contain, so the quote was dropped",
    )


def _json(raw: str) -> dict[str, Any] | None:
    """The reply as an object, or `None` when it is not one.

    A code fence is stripped first: a model asked for bare JSON supplies one often enough that
    refusing over it would throw away good answers, and stripping it changes no content.
    """
    try:
        parsed = json.loads(_CODE_FENCE.sub("", raw).strip())
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _number(value: object) -> int | None:
    """An extract number from whatever the model put there, or `None` if it is not one.

    `{"chunk": 2.0}` is as much extract 2 as `{"chunk": 2}` or `{"chunk": "2"}` - a model writing
    JSON has no reason to prefer one spelling of a whole number - so this reads the value as a
    float first and only then asks whether it names a whole extract. `1.5` fails that question and
    is dropped exactly as a word or a stray sign would be.
    """
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else None


def _elapsed(started: float) -> int:
    """Milliseconds since `started`, which is what the artefact records."""
    return int((time.monotonic() - started) * 1000)
