"""Grading an index against a reference set: run the assistant, check each answer, write `rag_eval.json`.

`contracts.RagEvalQuestion`, `RagEvalAggregates` and `RagEval` already say exactly what a grading run
must produce, so this module's job is filling them. Four things are worth knowing before reading
further.

**Every question goes through the real assistant, not a shortcut around it.** `evaluate` calls
`engine.generative.assistant.answer` exactly as a caller of `POST /indexes/{id}/ask` would, so the
floor, the guardrails and the citation parsing are the same code a real question runs through. What
this module adds on top is grading: does the outcome match what the reference set says it should be.

**Retrieval is graded on its own, never read off the answer.** A refused or a guardrail-blocked
answer carries no citations, but the retrieval that fed it still happened and is still worth
grading - it is exactly the row a screen most wants to know "did we even find the document, or did
we find it and then say something unusable" about. So `evaluate` re-embeds the question and calls
`retrieval.retrieve` itself, over the same store and the same `RagConfig` the assistant used, rather
than reading `AssistantAnswer.citations`. The cost is one extra embedding per question over the
minimum possible; the benefit is that retrieval quality is measured independently of what the model
or a guardrail did with what it was given. A row the reference set says should be refused is the
exception and gets no verdict at all: it is not an answerable question, it has no document it ought
to have found, and folding it into `retrieval_hit_rate` would measure retrieval against rows nobody
wanted retrieval for - which is why `RagEvalQuestion.retrieval_hit` is null for one whether or not
its `source_doc` cell happens to be filled in.

**A retrieval hit is a document match confirmed by a text match.** Whether the right document was
retrieved is decided by comparing a chunk's `doc_id` - the document's stem, with no extension - to
the reference set's `source_doc`, which the upload template ships *with* one. The CSV and the chunk
disagree on this by design (DEC-217: a filename's extension is a word every chunk of that format
shares, so the embedding text never carries one), and comparing the two strings raw has already
produced a silently-zero hit rate once. Document identity alone is not proof that the *passage*
retrieved is the one the question needed, so a hit also requires the chunk's text to overlap the
reference answer by at least `RETRIEVAL_HIT_OVERLAP`: the document narrows which chunks count, the
overlap confirms one of them is plausibly about the right thing.

**Faithfulness and correctness are this module's own judge calls, not the assistant's.** `answer`
already runs a faithfulness check as a guardrail, but a guardrail only ever records passed, warned or
blocked plus a one-sentence reason - never the number a grading screen wants to average over a
reference set. `evaluate` renders `judge_faithfulness` and `judge_correctness` itself, through the
same `Meter`, so `RagEvalQuestion.faithfulness` and `.correctness` are real scores from 0 to 1.
Because the faithfulness prompt is rendered from the same source and the same generated text the
assistant's own guardrail would have used, the second call is usually a cache hit rather than a
second charge (`budget.cache` defaults on) - the redundancy costs nothing in the common case and is
honestly metered in the uncommon one.

CRITICAL, and the reason nothing below asserts a pass rate: under the fake backend, every score this
module produces is a smoke number proving the evaluation runs end to end, never a quality gate.
`GroundedFakeLLMClient`'s embeddings are hashed words with no semantics and its judges answer from a
script (DEC-218, DEC-219). The tests in this package prove arithmetic, plumbing and the deterministic
checks - never that the fake was retrieved from accurately, judged sensibly, or agreed with a
reference answer. A quality claim about this module is a Bedrock claim, made with a real embedding
model and a real judge, never with the fake.

`RagEvalQuestion.passed` is decided by up to three checks, in this order, and the first to fail is
what `.failure` names. **Refusal correctness** comes first because it is free and it is definitive: a
row that refused when it should have answered, or the reverse, has failed regardless of what either
text says, and nothing else about the row is even worth checking. **Retrieval** comes next, for a
row that correctly did not refuse: a hit, or no verdict at all when there was no `source_doc` to
check against. **Faithfulness** comes last, checked against the same threshold
`configs/guardrails.yaml` already sets for the assistant's own grounding guardrail, because "were the
claims grounded" is exactly that guardrail's question asked a second time with a number attached -
and not checked at all where that file sets no bar, for the reason `_faithfulness_threshold` gives.
`correctness` is deliberately not a fourth gate: nothing in this package's configuration sets a bar
for it, and inventing one here would be precisely the guessed number DEC-208 refuses to write down
for cost. It is still graded, reported on every row it can be, and averaged into
`RagEvalAggregates.mean_correctness` - a screen that wants to see it can.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

import pandas as pd

from engine.config import RagConfig, ReferenceSetConfig, UseCaseConfig
from engine.generative.assistant import ANSWER_PROMPT, answer
from engine.generative.budget import Meter
from engine.generative.chunking import fingerprint
from engine.generative.contracts import (
    RAG_EVAL_FILENAME,
    GenerativePurpose,
    RagEval,
    RagEvalAggregates,
    RagEvalQuestion,
)
from engine.generative.errors import REFERENCE_SET_INVALID, generative_error
from engine.generative.guardrails import GuardrailAction, Guardrails
from engine.generative.prompts import Prompt, load_prompt, prompt_versions, render
from engine.generative.retrieval import Retrieved, retrieve
from engine.generative.vectorstore import VectorStore
from engine.storage import Storage, index_key
from engine.utils.logging import get_logger, log_stage
from engine.utils.time import utc_now

__all__ = [
    "CORRECTNESS_PROMPT",
    "FAITHFULNESS_PROMPT",
    "NGRAM_SIZE",
    "REFUSAL_MISMATCH",
    "RETRIEVAL_HIT_OVERLAP",
    "RETRIEVAL_MISS",
    "SOURCE_DOC_COLUMN",
    "UNFAITHFUL",
    "aggregate",
    "evaluate",
    "ngram_overlap",
]

_LOGGER = get_logger(__name__)

FAITHFULNESS_PROMPT: Final[str] = GenerativePurpose.JUDGE_FAITHFULNESS.value
CORRECTNESS_PROMPT: Final[str] = GenerativePurpose.JUDGE_CORRECTNESS.value

SOURCE_DOC_COLUMN: Final[str] = "source_doc"
"""The reference set's document-identity column. Fixed rather than configurable.

`ReferenceSetConfig` lets a deployment rename the question, refusal and reference-answer columns,
because a client might reasonably call those something else. `source_doc` is this evaluation's own
bookkeeping column rather than a client-facing one - the template that ships it names it exactly
this, and nothing about it should vary by client.
"""

NGRAM_SIZE: Final[int] = 1
"""The n of the n-gram overlap `_retrieval_hit` checks: single words, not runs of them.

`reference_answer` is written in the client's own words, never quoted from the document - the
upload template says so explicitly - so a chunk that plainly discusses the right fact can share
almost no run of two or three consecutive words with it while still sharing most of its vocabulary.
Requiring longer runs would grade how closely the reference set happens to paraphrase the source
text rather than grade retrieval, which is the one thing this check exists to measure.
"""

RETRIEVAL_HIT_OVERLAP: Final[float] = 0.3
"""The share of the reference answer's words a retrieved chunk must contain to count as a hit.

Measured as containment - the reference answer's own word count is the denominator, never the union
with the chunk's - because a chunk is routinely several times longer than the sentence it answers,
and a symmetric overlap would shrink toward zero as the chunk grows however well it answers the
question. 0.3 is deliberately loose: three words in ten is not agreement, only "plausibly the same
subject", which is the bar retrieval is being asked to clear here. The faithfulness judge, which runs
afterwards and reads the whole chunk rather than a word-overlap ratio, is what actually judges
agreement.
"""

REFUSAL_MISMATCH: Final[str] = "refusal_mismatch"
"""The row's `failure`, when whether the assistant refused disagreed with `expect_refusal`."""

RETRIEVAL_MISS: Final[str] = "retrieval_miss"
"""The row's `failure`, when an answerable question did not retrieve a chunk from its `source_doc`."""

UNFAITHFUL: Final[str] = "unfaithful"
"""The row's `failure`, when the faithfulness judge scored the answer below its configured threshold."""

_WORD: Final[re.Pattern[str]] = re.compile(r"[a-z0-9']+")


@dataclass(frozen=True)
class _ReferenceRow:
    """One row of the reference set, columns read by their configured names and values normalised."""

    question: str
    expect_refusal: bool
    source_doc: str
    reference_answer: str


# ---------------------------------------------------------------------------
# The n-gram overlap `_retrieval_hit` is built from
# ---------------------------------------------------------------------------
def _ngrams(text: str, n: int) -> frozenset[tuple[str, ...]]:
    """The n-grams of `text`'s words, lower-cased; the empty set for text with fewer than `n` words."""
    words = _WORD.findall(text.lower())
    if len(words) < n:
        return frozenset()
    return frozenset(tuple(words[start : start + n]) for start in range(len(words) - n + 1))


def ngram_overlap(reference: str, candidate: str, *, n: int = NGRAM_SIZE) -> float:
    """How much of `reference`'s n-grams appear in `candidate`, 0 to 1; 0 when `reference` has none.

    Containment rather than Jaccard: the denominator is `reference`'s own n-gram count, so a long
    `candidate` that happens to contain every one of `reference`'s words scores 1.0 rather than being
    diluted by everything else `candidate` also says.
    """
    reference_grams = _ngrams(reference, n)
    if not reference_grams:
        return 0.0
    candidate_grams = _ngrams(candidate, n)
    return len(reference_grams & candidate_grams) / len(reference_grams)


def _stem(name: str) -> str:
    """`name` without its extension - the shape a chunk's `doc_id` is in and a `source_doc` cell is not."""
    return Path(name).stem


def _retrieval_hit(retrieval: Retrieved, source_doc: str, reference_answer: str) -> bool:
    """Whether retrieval found a chunk from `source_doc` whose text plausibly answers the question."""
    stem = _stem(source_doc)
    return any(
        match.chunk.doc_id == stem
        and ngram_overlap(reference_answer, match.chunk.text) >= RETRIEVAL_HIT_OVERLAP
        for match in retrieval.matches
    )


# ---------------------------------------------------------------------------
# The reference set
# ---------------------------------------------------------------------------
def _read_reference_set(path: Path, config: ReferenceSetConfig) -> tuple[_ReferenceRow, ...]:
    """The reference set at `path`, validated and parsed; `REFERENCE_SET_INVALID` names what is missing.

    An absent file and a file pandas cannot parse are treated the same as one with every configured
    column missing: from the caller's side "there is nothing usable here" is one failure with one
    fix - point `reference_set_path` at a real file with the configured columns - and two codes for
    it would only be two things to check for the same repair.
    """
    columns = (config.question_column, config.refusal_column, SOURCE_DOC_COLUMN, config.reference_column)
    frame = pd.DataFrame(columns=[])
    if path.is_file():
        try:
            frame = pd.read_csv(path, dtype=str, keep_default_na=False)
        except (pd.errors.ParserError, pd.errors.EmptyDataError):
            frame = pd.DataFrame(columns=[])
    missing = [column for column in columns if column not in frame.columns]
    if missing:
        raise generative_error(REFERENCE_SET_INVALID, column=missing[0])
    return tuple(
        _ReferenceRow(
            question=str(record[config.question_column]),
            expect_refusal=str(record[config.refusal_column]).strip().lower() == "true",
            source_doc=str(record[SOURCE_DOC_COLUMN]).strip(),
            reference_answer=str(record[config.reference_column]).strip(),
        )
        for record in frame.to_dict(orient="records")
    )


# ---------------------------------------------------------------------------
# Grading one row
# ---------------------------------------------------------------------------
def _retrieve(meter: Meter, store: VectorStore, index_id: str, question: str, rag: RagConfig) -> Retrieved:
    """The chunks retrieval would hand the assistant for `question`, worked out independently of it.

    A second embedding call rather than a shared one: `answer` returns citations, not matches, and
    citations are empty for a refused or a blocked row - exactly the rows a grading run most wants
    retrieval information about.
    """
    (vector,) = meter.embed([question])
    return retrieve(store, index_id, vector, config=rag)


def _parse_score(text: str) -> float:
    """The `score` field of a judge's JSON reply, or 0.0 for a reply that cannot be read as one.

    Mirrors how `guardrails._parse_verdict` reads the same shape: a verdict nobody can read is not a
    pass, so it scores as the worst answer rather than as a missing one.
    """
    try:
        payload = json.loads(text)
        score = float(payload["score"])
    except (ValueError, KeyError, TypeError):
        return 0.0
    return max(0.0, min(1.0, score))


def _judge_score(
    meter: Meter, prompt: Prompt, values: Mapping[str, object], purpose: GenerativePurpose
) -> float:
    """Render `prompt` from `values`, call `purpose` through `meter`, and return the clamped score."""
    completion = meter.judge(render(prompt, values), purpose)
    return _parse_score(completion.text)


def _cost_delta(before: float | None, after: float | None) -> float | None:
    """The cost this row added, or `None` when either snapshot could not be priced."""
    if before is None or after is None:
        return None
    return round(after - before, 6)


def _verdict(
    row: _ReferenceRow,
    refused: bool,
    retrieval_hit: bool | None,
    faithfulness: float | None,
    *,
    faithfulness_threshold: float,
) -> tuple[bool, str | None]:
    """Whether this row passed, and the first check that failed it when it did not.

    Refusal correctness is checked first because it is free and it is definitive: nothing else about
    a row matters if the assistant refused when it should have answered, or the reverse. Retrieval
    and faithfulness are checked only for a row that correctly did not refuse, in that order, because
    a faithfulness score with no evidence behind it is grading the wrong thing.
    """
    if refused != row.expect_refusal:
        return False, REFUSAL_MISMATCH
    if row.expect_refusal:
        return True, None
    if retrieval_hit is False:
        return False, RETRIEVAL_MISS
    if faithfulness is not None and faithfulness < faithfulness_threshold:
        return False, UNFAITHFUL
    return True, None


def _grade(
    row: _ReferenceRow,
    *,
    index_id: str,
    use_case: UseCaseConfig,
    store: VectorStore,
    meter: Meter,
    guardrails: Guardrails,
    config_root: Path | None,
    faithfulness_prompt: Prompt,
    correctness_prompt: Prompt,
    faithfulness_threshold: float,
) -> RagEvalQuestion:
    """One graded row: the real answer, this module's own retrieval and judge calls, and the verdict."""
    before = meter.cost_so_far
    result = answer(
        row.question,
        index_id=index_id,
        use_case=use_case,
        store=store,
        meter=meter,
        guardrails=guardrails,
        config_root=config_root,
    )
    gradeable = bool(row.source_doc) and not row.expect_refusal
    retrieval = (
        _retrieve(meter, store, index_id, row.question, use_case.generative.rag)
        if gradeable or not result.refused
        else None
    )
    retrieval_hit = (
        _retrieval_hit(retrieval, row.source_doc, row.reference_answer)
        if gradeable and retrieval is not None
        else None
    )
    faithfulness: float | None = None
    correctness: float | None = None
    if not result.refused:
        source = "\n\n".join(match.chunk.text for match in retrieval.matches) if retrieval is not None else ""
        faithfulness = _judge_score(
            meter,
            faithfulness_prompt,
            {"source": source, "generated": result.answer},
            GenerativePurpose.JUDGE_FAITHFULNESS,
        )
        if row.reference_answer:
            correctness = _judge_score(
                meter,
                correctness_prompt,
                {"question": row.question, "reference_answer": row.reference_answer, "answer": result.answer},
                GenerativePurpose.JUDGE_CORRECTNESS,
            )
    passed, failure = _verdict(
        row, result.refused, retrieval_hit, faithfulness, faithfulness_threshold=faithfulness_threshold
    )
    return RagEvalQuestion(
        question=row.question,
        answer=result.answer,
        refused=result.refused,
        expect_refusal=row.expect_refusal,
        retrieval_hit=retrieval_hit,
        faithfulness=faithfulness,
        correctness=correctness,
        passed=passed,
        failure=failure,
        latency_ms=result.latency_ms,
        cost_estimate_usd=_cost_delta(before, meter.cost_so_far),
    )


def _faithfulness_threshold(guardrails: Guardrails) -> float:
    """The bar a row's faithfulness must clear, or 0.0 when the configuration sets none.

    The bar is `configs/guardrails.yaml`'s, because "were the claims grounded" is the assistant's own
    faithfulness guardrail asked a second time with a number attached, and grading it against a
    second bar written here would be two answers to one question. A deployment that has no
    faithfulness judge, or has set its `on_fail` to `off`, has said what it wants: `0.0`, which no
    clamped score is below, so `UNFAITHFUL` cannot be the failure named on a row nobody set a bar
    for. The tempting alternative, failing closed at 1.0, would invent the strictest bar in the range
    for a check the operator switched off - exactly the guessed number DEC-208 refuses to write down
    for cost, and it would mark every answerable row `unfaithful` in a deployment whose
    `guardrails.yaml` is simply absent. The score itself is still judged and still reported; only the
    gate goes.
    """
    rule = guardrails.policy.judges.get("faithfulness")
    if rule is None or rule.on_fail is GuardrailAction.OFF:
        return 0.0
    return rule.threshold


def _worst_first(question: RagEvalQuestion) -> tuple[bool, float]:
    """Sort key putting a failed question first, and within a group the least-supported one first.

    A row with no judge score - any refusal, right or wrong - has nothing in it a reader could point
    at and improve, which is worse than a low real score, so it sorts as though it scored below zero.
    """
    scores = [score for score in (question.faithfulness, question.correctness) if score is not None]
    worst = min(scores) if scores else -1.0
    return (question.passed, worst)


# ---------------------------------------------------------------------------
# Aggregating and running the whole reference set
# ---------------------------------------------------------------------------
def aggregate(questions: Sequence[RagEvalQuestion], *, pass_threshold: float) -> RagEvalAggregates:
    """Counts and rates over `questions`, so a screen and a test both do the arithmetic once.

    Every rate is `None` rather than 0.0 when nothing fed it: an index graded on zero answerable
    questions has no retrieval hit rate to report, and reporting 0.0 would read as "it missed every
    one" rather than "there were none to check". `pass_rate` is the one exception, because it is
    always a share of `questions` and `questions` can be zero - an evaluation that graded nothing has
    not met any bar, which `0.0` says honestly.
    """
    total = len(questions)
    passed = sum(1 for question in questions if question.passed)
    hits = [hit for question in questions if (hit := question.retrieval_hit) is not None]
    faithfulness_scores = [score for question in questions if (score := question.faithfulness) is not None]
    correctness_scores = [score for question in questions if (score := question.correctness) is not None]
    refusal_matches = [question.refused == question.expect_refusal for question in questions]
    pass_rate = passed / total if total else 0.0
    return RagEvalAggregates(
        questions=total,
        passed=passed,
        pass_rate=pass_rate,
        pass_threshold=pass_threshold,
        meets_threshold=pass_rate >= pass_threshold,
        retrieval_hit_rate=(sum(hits) / len(hits)) if hits else None,
        mean_faithfulness=(
            (sum(faithfulness_scores) / len(faithfulness_scores)) if faithfulness_scores else None
        ),
        mean_correctness=(sum(correctness_scores) / len(correctness_scores)) if correctness_scores else None,
        refusal_accuracy=(sum(refusal_matches) / len(refusal_matches)) if refusal_matches else None,
    )


def evaluate(
    *,
    index_id: str,
    use_case: UseCaseConfig,
    reference_set_path: Path,
    storage: Storage,
    store: VectorStore,
    meter: Meter,
    guardrails: Guardrails,
    config_root: Path | None = None,
    now: datetime | None = None,
) -> RagEval:
    """Grade `index_id` against `reference_set_path`, write `rag_eval.json`, and return it.

    Every question runs through the real `assistant.answer`; nothing here approximates or replays
    it. What is graded is that outcome against the reference set's own claim about it - see the
    module docstring for what each check means and why `correctness` is reported but never gates
    `passed`.
    """
    started = time.monotonic()
    generative = use_case.generative
    rows = _read_reference_set(reference_set_path, generative.reference_set)
    faithfulness_prompt = load_prompt(FAITHFULNESS_PROMPT, config_root)
    correctness_prompt = load_prompt(CORRECTNESS_PROMPT, config_root)
    faithfulness_threshold = _faithfulness_threshold(guardrails)

    questions = [
        _grade(
            row,
            index_id=index_id,
            use_case=use_case,
            store=store,
            meter=meter,
            guardrails=guardrails,
            config_root=config_root,
            faithfulness_prompt=faithfulness_prompt,
            correctness_prompt=correctness_prompt,
            faithfulness_threshold=faithfulness_threshold,
        )
        for row in rows
    ]
    aggregates = aggregate(questions, pass_threshold=generative.reference_set.pass_threshold)
    graded_at = now if now is not None else utc_now()
    result = RagEval(
        index_id=index_id,
        reference_set_fingerprint=fingerprint(reference_set_path.read_bytes()),
        questions=tuple(sorted(questions, key=_worst_first)),
        aggregates=aggregates,
        prompt_versions=prompt_versions(
            (ANSWER_PROMPT, FAITHFULNESS_PROMPT, CORRECTNESS_PROMPT), config_root
        ),
        graded_at=graded_at,
    )
    storage.write_model(index_key(index_id, RAG_EVAL_FILENAME), result)
    log_stage(_LOGGER, "evaluation.graded", rows=len(questions), seconds=round(time.monotonic() - started, 3))
    return result
