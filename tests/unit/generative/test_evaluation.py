"""`engine.generative.evaluation`: grading arithmetic, the deterministic checks, and one real run.

**The score under the fake proves nothing about quality, and no test here asks it to.** DEC-218 and
DEC-219 are explicit that a lexical fake's retrieval and a scripted judge cannot be trusted to
produce a *correct* verdict, only to produce *a* verdict - so what is proved below is arithmetic on
hand-built results, the deterministic checks in isolation, and one small end-to-end run whose
assertions are about shape (three questions in, three questions out, the counts add up, the artefact
reads back) rather than about whether the fake got anything right. The one quality-shaped fact this
module trusts is the same one `test_assistant.py` already earns for the same reason: a question with
no vocabulary in common with the corpus is refused by the similarity floor before a model is ever
called, which is arithmetic on cosine similarity rather than a judgement of anything.

**`_retrieval_hit` is proved at the seam DEC-217 warns about.** A chunk's `doc_id` is a document's
stem, and a reference set's `source_doc` carries the extension the upload template ships it with;
comparing the two raw strings is a hit rate of zero dressed up as a bug in retrieval, which is
exactly the mistake the module docstring says has already happened once. The tests below hold the
extension and the stem apart on purpose, and separately hold the n-gram overlap ratio at its
boundary, because a threshold that is not tested on both sides of itself is not really tested.

**`_verdict` is proved as the thing that decides `passed`, not through a full evaluation run.**
Refusal correctness, a retrieval miss and an unfaithful answer are three different reasons a row can
fail, and each is asserted as the *first* reason for a row built to trigger it and nothing else -
which is the only way to know the precedence is what the docstring claims it is rather than an
accident of which check happened to run last.
"""

from __future__ import annotations

import csv
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from engine.config import BudgetConfig, LlmConfig, RagConfig, UseCaseConfig, load_use_case
from engine.generative.assistant import ANSWER_PROMPT
from engine.generative.budget import Meter
from engine.generative.contracts import RAG_EVAL_FILENAME, Chunk, RagEval, RagEvalQuestion
from engine.generative.errors import REFERENCE_SET_INVALID, GenerativeError
from engine.generative.evaluation import (
    CORRECTNESS_PROMPT,
    FAITHFULNESS_PROMPT,
    NGRAM_SIZE,
    REFUSAL_MISMATCH,
    RETRIEVAL_HIT_OVERLAP,
    RETRIEVAL_MISS,
    UNFAITHFUL,
    _faithfulness_threshold,
    _read_reference_set,
    _ReferenceRow,
    _retrieval_hit,
    _verdict,
    aggregate,
    evaluate,
    ngram_overlap,
)
from engine.generative.guardrails import (
    GuardrailAction,
    GuardrailPolicy,
    Guardrails,
    JudgeRule,
    load_policy,
)
from engine.generative.index import build_index
from engine.generative.retrieval import Retrieved
from engine.generative.vectorstore import LocalVectorStore, Match, VectorStore
from engine.llm import GroundedFakeLLMClient
from engine.storage import LocalStorage, index_key
from engine.utils.ids import new_index_id
from tests.fixtures.make_docs import build_knowledge_base

REFERENCE_SET_CONFIG = load_use_case("ai-onboarding-assistant").generative.reference_set


def chunk(text: str, *, doc_id: str = "faq_activation", ordinal: int = 0) -> Chunk:
    """A chunk carrying only what `_retrieval_hit` reads: its document stem and its text."""
    return Chunk(
        chunk_id=f"{doc_id}-{ordinal:05d}",
        doc_id=doc_id,
        document=f"{doc_id}.md",
        section="A section",
        ordinal=ordinal,
        tokens=len(text.split()),
        text=text,
    )


def retrieved(*matches: Match) -> Retrieved:
    """A hand-built retrieval result; only `.matches` is read by anything under test here."""
    return Retrieved(matches=matches, considered=len(matches), above_floor=len(matches), floor=0.0)


# ---------------------------------------------------------------------------
# The n-gram overlap ratio, at its boundary
# ---------------------------------------------------------------------------
TEN_WORDS = "alpha bravo charlie delta echo foxtrot golf hotel india juliet"


def test_ngram_overlap_of_identical_text_is_one() -> None:
    assert ngram_overlap(TEN_WORDS, TEN_WORDS) == pytest.approx(1.0)


def test_ngram_overlap_is_zero_when_the_reference_has_no_words_to_overlap_with() -> None:
    assert ngram_overlap("", "alpha bravo charlie") == 0.0
    assert ngram_overlap("   ", "alpha bravo charlie") == 0.0


def test_ngram_overlap_is_a_share_of_the_references_words_not_the_candidates() -> None:
    """A candidate many times longer than the reference is not diluted by everything else it says."""
    candidate = f"{TEN_WORDS} " + " ".join(f"filler{n}" for n in range(200))
    assert ngram_overlap(TEN_WORDS, candidate) == pytest.approx(1.0)


def test_ngram_overlap_exactly_on_the_retrieval_hit_boundary_counts_as_a_hit() -> None:
    """Three of the reference's ten words shared is exactly `RETRIEVAL_HIT_OVERLAP`, and `>=` keeps it."""
    candidate = "alpha bravo charlie unrelated words that share nothing else"
    overlap = ngram_overlap(TEN_WORDS, candidate)
    assert overlap == pytest.approx(0.3)
    assert overlap >= RETRIEVAL_HIT_OVERLAP


def test_ngram_overlap_one_word_short_of_the_boundary_does_not_count_as_a_hit() -> None:
    candidate = "alpha bravo unrelated words that share nothing else"
    overlap = ngram_overlap(TEN_WORDS, candidate)
    assert overlap == pytest.approx(0.2)
    assert overlap < RETRIEVAL_HIT_OVERLAP


def test_the_shipped_n_is_single_words_so_a_paraphrase_is_not_penalised_for_reordering() -> None:
    """A reference answer is the client's own words, not a quotation, so the module owns n=1."""
    assert NGRAM_SIZE == 1
    reordered = " ".join(reversed(TEN_WORDS.split()))
    assert ngram_overlap(TEN_WORDS, reordered) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# The stem-vs-extension comparison `_retrieval_hit` is built from
# ---------------------------------------------------------------------------
def test_a_source_doc_with_its_extension_still_matches_a_chunks_bare_stem() -> None:
    """DEC-217's own warning: comparing the two strings raw would never match, and has bitten once."""
    hit = _retrieval_hit(
        retrieved(Match(chunk=chunk(TEN_WORDS, doc_id="faq_activation"), similarity=0.9)),
        "faq_activation.md",
        TEN_WORDS,
    )
    assert hit is True


def test_a_chunk_from_a_different_document_is_not_a_hit_even_with_full_text_overlap() -> None:
    """Document identity is a hard requirement, not a tie-breaker the overlap can buy back."""
    hit = _retrieval_hit(
        retrieved(Match(chunk=chunk(TEN_WORDS, doc_id="plans_prepaid"), similarity=0.9)),
        "faq_activation.md",
        TEN_WORDS,
    )
    assert hit is False


def test_a_chunk_from_the_right_document_with_no_shared_words_is_not_a_hit() -> None:
    """Document identity alone is not proof that the retrieved *passage* is the one that answers it."""
    hit = _retrieval_hit(
        retrieved(Match(chunk=chunk("nothing about that here", doc_id="faq_activation"), similarity=0.9)),
        "faq_activation.md",
        TEN_WORDS,
    )
    assert hit is False


def test_a_hit_from_any_one_of_several_retrieved_chunks_is_enough() -> None:
    hit = _retrieval_hit(
        retrieved(
            Match(
                chunk=chunk("unrelated to the reference", doc_id="faq_activation", ordinal=0), similarity=0.9
            ),
            Match(chunk=chunk(TEN_WORDS, doc_id="faq_activation", ordinal=1), similarity=0.5),
        ),
        "faq_activation.md",
        TEN_WORDS,
    )
    assert hit is True


def test_no_retrieved_matches_is_never_a_hit() -> None:
    assert _retrieval_hit(retrieved(), "faq_activation.md", TEN_WORDS) is False


# ---------------------------------------------------------------------------
# The verdict: refusal correctness first, then retrieval, then faithfulness
# ---------------------------------------------------------------------------
def row(**overrides: object) -> _ReferenceRow:
    values: dict[str, object] = {
        "question": "does it matter for this check",
        "expect_refusal": False,
        "source_doc": "faq_activation.md",
        "reference_answer": "Usually within four hours.",
    }
    values.update(overrides)
    return _ReferenceRow(**values)  # type: ignore[arg-type]


def test_a_row_that_correctly_refuses_passes_with_no_failure() -> None:
    assert _verdict(row(expect_refusal=True), True, None, None, faithfulness_threshold=0.8) == (True, None)


def test_a_row_that_answers_when_it_should_have_refused_fails_on_refusal_alone() -> None:
    """A perfect retrieval and a perfect faithfulness score do not save a row that should have refused."""
    verdict = _verdict(row(expect_refusal=True), False, True, 1.0, faithfulness_threshold=0.8)
    assert verdict == (False, REFUSAL_MISMATCH)


def test_a_row_that_refuses_when_it_should_have_answered_fails_on_refusal_alone() -> None:
    assert _verdict(row(expect_refusal=False), True, None, None, faithfulness_threshold=0.8) == (
        False,
        REFUSAL_MISMATCH,
    )


def test_an_answerable_row_that_missed_retrieval_fails_before_faithfulness_is_even_asked() -> None:
    """A high faithfulness score is passed in and ignored: retrieval is checked first and it lost."""
    verdict = _verdict(row(), False, False, 0.99, faithfulness_threshold=0.8)
    assert verdict == (False, RETRIEVAL_MISS)


def test_a_row_with_no_source_doc_to_check_is_not_penalised_for_a_missing_retrieval_verdict() -> None:
    """`retrieval_hit=None` means there was nothing to check, which is not the same as missing it."""
    assert _verdict(row(source_doc=""), False, None, 0.95, faithfulness_threshold=0.8) == (True, None)


def test_an_answerable_row_below_the_faithfulness_threshold_fails_as_unfaithful() -> None:
    assert _verdict(row(), False, True, 0.5, faithfulness_threshold=0.8) == (False, UNFAITHFUL)


def test_a_faithfulness_score_exactly_on_the_threshold_passes() -> None:
    assert _verdict(row(), False, True, 0.8, faithfulness_threshold=0.8) == (True, None)


def test_a_row_that_clears_every_check_passes_with_no_failure_named() -> None:
    assert _verdict(row(), False, True, 0.95, faithfulness_threshold=0.8) == (True, None)


# ---------------------------------------------------------------------------
# Aggregating: the empty and all-refusal edges, and the arithmetic in between
# ---------------------------------------------------------------------------
def eval_question(**overrides: object) -> RagEvalQuestion:
    values: dict[str, object] = {
        "question": "q",
        "answer": "a",
        "refused": False,
        "expect_refusal": False,
        "retrieval_hit": True,
        "faithfulness": 0.9,
        "correctness": 0.8,
        "passed": True,
        "failure": None,
        "latency_ms": 10,
        "cost_estimate_usd": None,
    }
    values.update(overrides)
    return RagEvalQuestion(**values)  # type: ignore[arg-type]


def test_aggregate_arithmetic_over_a_hand_built_set_of_results() -> None:
    questions = (
        eval_question(passed=True, retrieval_hit=True, faithfulness=0.9, correctness=0.8),
        eval_question(
            passed=False, retrieval_hit=False, faithfulness=0.4, correctness=0.5, failure=RETRIEVAL_MISS
        ),
        eval_question(
            passed=True,
            expect_refusal=True,
            refused=True,
            retrieval_hit=None,
            faithfulness=None,
            correctness=None,
        ),
    )
    aggregates = aggregate(questions, pass_threshold=0.6)
    assert aggregates.questions == 3
    assert aggregates.passed == 2
    assert aggregates.pass_rate == pytest.approx(2 / 3)
    assert aggregates.pass_threshold == 0.6
    assert aggregates.meets_threshold is True
    assert aggregates.retrieval_hit_rate == pytest.approx(0.5)
    assert aggregates.mean_faithfulness == pytest.approx((0.9 + 0.4) / 2)
    assert aggregates.mean_correctness == pytest.approx((0.8 + 0.5) / 2)
    assert aggregates.refusal_accuracy == pytest.approx(1.0)


def test_aggregate_of_no_questions_has_a_zero_pass_rate_and_nothing_else_to_report() -> None:
    aggregates = aggregate((), pass_threshold=0.75)
    assert (aggregates.questions, aggregates.passed) == (0, 0)
    assert aggregates.pass_rate == 0.0
    assert aggregates.meets_threshold is False
    assert aggregates.retrieval_hit_rate is None
    assert aggregates.mean_faithfulness is None
    assert aggregates.mean_correctness is None
    assert aggregates.refusal_accuracy is None


def test_aggregate_when_every_row_correctly_refuses_has_no_retrieval_or_judge_rate() -> None:
    """A reference set of nothing but refusals is real: `retrieval_hit_rate` has no rows to average."""
    questions = tuple(
        eval_question(
            expect_refusal=True,
            refused=True,
            passed=True,
            failure=None,
            retrieval_hit=None,
            faithfulness=None,
            correctness=None,
        )
        for _ in range(4)
    )
    aggregates = aggregate(questions, pass_threshold=0.9)
    assert (aggregates.questions, aggregates.passed) == (4, 4)
    assert aggregates.pass_rate == 1.0
    assert aggregates.meets_threshold is True
    assert aggregates.retrieval_hit_rate is None
    assert aggregates.mean_faithfulness is None
    assert aggregates.mean_correctness is None
    assert aggregates.refusal_accuracy == 1.0


def test_meets_threshold_is_the_pass_rate_compared_against_the_configured_bar() -> None:
    questions = (eval_question(passed=True), eval_question(passed=False))
    assert aggregate(questions, pass_threshold=0.5).meets_threshold is True
    assert aggregate(questions, pass_threshold=0.51).meets_threshold is False


# ---------------------------------------------------------------------------
# The reference set: read, or a coded failure naming what is wrong with it
# ---------------------------------------------------------------------------
def test_a_well_formed_reference_set_parses_every_row_and_normalises_the_refusal_flag(tmp_path: Path) -> None:
    path = tmp_path / "reference.csv"
    path.write_text(
        "question,expect_refusal,source_doc,reference_answer\n"
        "How long?,false,faq_activation.md,Four hours.\n"
        "Share price?,TRUE,,\n",
        encoding="utf-8",
    )
    rows = _read_reference_set(path, REFERENCE_SET_CONFIG)
    assert [entry.question for entry in rows] == ["How long?", "Share price?"]
    assert [entry.expect_refusal for entry in rows] == [False, True]
    assert rows[0].source_doc == "faq_activation.md"
    assert rows[1].source_doc == ""
    assert rows[1].reference_answer == ""


def test_a_reference_set_missing_a_configured_column_raises_the_coded_error(tmp_path: Path) -> None:
    path = tmp_path / "reference.csv"
    path.write_text("question,expect_refusal,reference_answer\nq,false,a\n", encoding="utf-8")
    with pytest.raises(GenerativeError) as error:
        _read_reference_set(path, REFERENCE_SET_CONFIG)
    assert error.value.code == REFERENCE_SET_INVALID
    assert "source_doc" in error.value.message


def test_a_reference_set_file_that_does_not_exist_raises_the_same_coded_error(tmp_path: Path) -> None:
    with pytest.raises(GenerativeError) as error:
        _read_reference_set(tmp_path / "missing.csv", REFERENCE_SET_CONFIG)
    assert error.value.code == REFERENCE_SET_INVALID


def test_an_empty_reference_set_file_raises_the_same_coded_error(tmp_path: Path) -> None:
    path = tmp_path / "reference.csv"
    path.write_bytes(b"")
    with pytest.raises(GenerativeError) as error:
        _read_reference_set(path, REFERENCE_SET_CONFIG)
    assert error.value.code == REFERENCE_SET_INVALID


# ---------------------------------------------------------------------------
# One small end-to-end run: a real index, a real reference set, the real assistant
# ---------------------------------------------------------------------------
STEMS: tuple[str, ...] = ("faq_activation", "faq_billing", "plans_prepaid")
"""Three of the fourteen corpus documents - the flow is the same and the fixture costs a fifth of it."""

FLOOR = 0.15
"""This module's own similarity floor (DEC-218): the shipped 0.25 is calibrated for a real embedding
model and means something else under the lexical fake."""

ANSWERED = "How long does a new SIM take to activate?"
LATE_FEE_QUESTION = "What is the late payment fee?"
DISJOINT = "Who painted the ceiling of the Sistine Chapel?"
"""A question with no vocabulary in common with a telecom corpus - the one off-topic question a
lexical fake can be trusted to refuse (DEC-219)."""

BASE: UseCaseConfig = load_use_case("ai-onboarding-assistant")


def rag(**overrides: object) -> RagConfig:
    settings: dict[str, object] = {"top_k": 4, "min_similarity": FLOOR, "mmr_lambda": 0.7}
    settings.update(overrides)
    return RagConfig(**settings)


def small_use_case() -> UseCaseConfig:
    return BASE.model_copy(update={"generative": BASE.generative.model_copy(update={"rag": rag()})})


def meter_for(client: GroundedFakeLLMClient) -> Meter:
    return Meter(client, job_id="x_20260101_evalcase", llm=LlmConfig(), budget=BudgetConfig(cache=False))


def write_reference_csv(path: Path, rows: Sequence[tuple[str, str, str, str]]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["question", "expect_refusal", "source_doc", "reference_answer"])
        writer.writerows(rows)
    return path


@dataclass(frozen=True)
class GradedFixture:
    index_id: str
    storage: LocalStorage
    store: VectorStore
    reference_set_path: Path


@pytest.fixture(scope="module")
def graded_fixture(tmp_path_factory: pytest.TempPathFactory) -> GradedFixture:
    """A real three-document index and a small reference set naming two of its documents."""
    root = tmp_path_factory.mktemp("evaluation")
    paths = build_knowledge_base(root / "docs", stems=STEMS)
    storage = LocalStorage(root / "data")
    store = LocalVectorStore(storage)
    index_id = new_index_id()
    build_index(
        list(paths),
        index_id=index_id,
        use_case=BASE,
        storage=storage,
        store=store,
        meter=meter_for(GroundedFakeLLMClient()),
    )
    reference_set_path = write_reference_csv(
        root / "reference.csv",
        [
            (ANSWERED, "false", "faq_activation.md", "Usually within four hours of the verification call."),
            (
                LATE_FEE_QUESTION,
                "false",
                "faq_billing.md",
                "Rs 100 or 2% of the outstanding amount, whichever is higher.",
            ),
            (DISJOINT, "true", "", ""),
        ],
    )
    return GradedFixture(
        index_id=index_id, storage=storage, store=store, reference_set_path=reference_set_path
    )


def test_grading_a_small_index_produces_an_artefact_whose_counts_add_up(
    graded_fixture: GradedFixture,
) -> None:
    meter = meter_for(GroundedFakeLLMClient())
    guardrails = Guardrails(load_policy(), meter=meter)

    result = evaluate(
        index_id=graded_fixture.index_id,
        use_case=small_use_case(),
        reference_set_path=graded_fixture.reference_set_path,
        storage=graded_fixture.storage,
        store=graded_fixture.store,
        meter=meter,
        guardrails=guardrails,
    )

    stored = graded_fixture.storage.read_model(index_key(graded_fixture.index_id, RAG_EVAL_FILENAME), RagEval)
    assert stored == result

    assert result.index_id == graded_fixture.index_id
    assert len(result.questions) == 3
    assert {question.question for question in result.questions} == {ANSWERED, LATE_FEE_QUESTION, DISJOINT}
    assert result.aggregates.questions == 3
    assert result.aggregates.passed == sum(1 for question in result.questions if question.passed)
    assert result.aggregates.pass_rate == pytest.approx(result.aggregates.passed / 3)
    assert result.aggregates == aggregate(
        result.questions, pass_threshold=small_use_case().generative.reference_set.pass_threshold
    )
    assert all(question.latency_ms >= 0 for question in result.questions)
    assert all(
        question.cost_estimate_usd is None for question in result.questions
    ), "the fake model has no row in an empty price table, so every row's cost is honestly unknown"
    assert set(result.prompt_versions) == {ANSWER_PROMPT, FAITHFULNESS_PROMPT, CORRECTNESS_PROMPT}
    assert result.reference_set_fingerprint.startswith("sha256-document:")

    disjoint = next(question for question in result.questions if question.question == DISJOINT)
    assert disjoint.refused is True
    assert disjoint.retrieval_hit is None
    assert disjoint.faithfulness is None
    assert disjoint.correctness is None
    assert disjoint.passed is True
    assert disjoint.failure is None


def test_evaluating_the_same_index_twice_does_not_grow_its_stored_files(
    graded_fixture: GradedFixture,
) -> None:
    """`rag_eval.json` is written once and overwritten, not appended, however many times grading runs."""
    before = sorted(p for p in graded_fixture.storage.root.rglob("*") if p.is_file())
    meter = meter_for(GroundedFakeLLMClient())
    evaluate(
        index_id=graded_fixture.index_id,
        use_case=small_use_case(),
        reference_set_path=graded_fixture.reference_set_path,
        storage=graded_fixture.storage,
        store=graded_fixture.store,
        meter=meter,
        guardrails=Guardrails(load_policy(), meter=meter),
    )
    after = sorted(p for p in graded_fixture.storage.root.rglob("*") if p.is_file())
    assert after == before


# ---------------------------------------------------------------------------
# The faithfulness bar, and the rows it is not allowed to be applied to
# ---------------------------------------------------------------------------
def test_the_faithfulness_bar_is_the_one_the_guardrail_policy_sets() -> None:
    policy = GuardrailPolicy(judges={"faithfulness": JudgeRule(0.8, GuardrailAction.BLOCK)})
    assert _faithfulness_threshold(Guardrails(policy)) == pytest.approx(0.8)


def test_a_deployment_that_configures_no_faithfulness_judge_gets_no_faithfulness_bar() -> None:
    """A bar invented here would fail every answerable row against a number nobody chose."""
    assert _faithfulness_threshold(Guardrails(GuardrailPolicy())) == 0.0


def test_a_faithfulness_judge_switched_off_is_off_rather_than_a_bar_that_still_fails_rows() -> None:
    policy = GuardrailPolicy(judges={"faithfulness": JudgeRule(0.8, GuardrailAction.OFF)})
    assert _faithfulness_threshold(Guardrails(policy)) == 0.0


def test_an_unset_faithfulness_bar_cannot_fail_a_row_whatever_the_judge_scored() -> None:
    """`_verdict` is where the bar is spent, so the 0.0 has to mean "nothing fails" there too."""
    assert _verdict(row(), False, True, 0.0, faithfulness_threshold=0.0) == (True, None)


def test_a_row_the_reference_set_says_should_refuse_gets_no_retrieval_verdict(
    graded_fixture: GradedFixture, tmp_path: Path
) -> None:
    """A question nobody wanted retrieval for is never folded into the retrieval hit rate."""
    reference_set_path = write_reference_csv(
        tmp_path / "refusals.csv", [(DISJOINT, "true", "faq_activation.md", "Nothing to say about it.")]
    )
    meter = meter_for(GroundedFakeLLMClient())

    result = evaluate(
        index_id=graded_fixture.index_id,
        use_case=small_use_case(),
        reference_set_path=reference_set_path,
        storage=graded_fixture.storage,
        store=graded_fixture.store,
        meter=meter,
        guardrails=Guardrails(load_policy(), meter=meter),
    )

    (question,) = result.questions
    assert question.refused is True
    assert question.passed is True
    assert question.retrieval_hit is None
    assert result.aggregates.retrieval_hit_rate is None


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_a_non_finite_judge_score_grades_as_the_worst_answer(literal: str) -> None:
    """`min(1.0, nan)` is 1.0, so the clamp alone wrote a fabricated mean into `rag_eval.json`.

    `_parse_score` mirrors `guardrails._parse_verdict`, and it has to mirror this half too: a reply
    nobody can read scores as the worst answer, never as a perfect one, because the mean of these
    is what `meets_threshold` is decided on.
    """
    from engine.generative.evaluation import _parse_score

    assert _parse_score(f'{{"score": {literal}}}') == 0.0


def test_a_readable_score_is_untouched() -> None:
    from engine.generative.evaluation import _parse_score

    assert _parse_score('{"score": 0.82}') == 0.82
    assert _parse_score('{"score": 4}') == 1.0
    assert _parse_score("not json at all") == 0.0
