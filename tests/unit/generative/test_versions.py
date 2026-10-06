"""Index versions (`engine.generative.versions`, DEC-1274, DEC-1276, DEC-1277): counted, never estimated."""

from __future__ import annotations

from datetime import UTC, datetime

from engine.generative.contracts import (
    ChunkConfig,
    DocIndexManifest,
    IndexedDocument,
    RagEval,
    RagEvalAggregates,
    RagEvalQuestion,
)
from engine.generative.versions import grade_of, update_summary, verdict_changes

WHEN = datetime(2026, 10, 6, tzinfo=UTC)


def _q(
    question: str, *, passed: bool, refused: bool = False, expect: bool = False, failure: str | None = None
) -> RagEvalQuestion:
    return RagEvalQuestion(
        question=question,
        answer="",
        refused=refused,
        expect_refusal=expect,
        passed=passed,
        failure=failure,
        latency_ms=1,
    )


def _eval(questions: tuple[RagEvalQuestion, ...], *, fingerprint: str = "f1") -> RagEval:
    passed = sum(q.passed for q in questions)
    return RagEval(
        index_id="x_1",
        reference_set_fingerprint=fingerprint,
        questions=questions,
        aggregates=RagEvalAggregates(
            questions=len(questions),
            passed=passed,
            pass_rate=passed / len(questions),
            pass_threshold=0.75,
            meets_threshold=passed / len(questions) >= 0.75,
            retrieval_hit_rate=0.5,
            mean_faithfulness=None,
            mean_correctness=0.25,
        ),
        graded_at=WHEN,
    )


def _doc(name: str, fingerprint: str, chunks: int) -> IndexedDocument:
    return IndexedDocument(
        doc_id=name.rsplit(".", 1)[0],
        name=name,
        media_type=name.rsplit(".", 1)[1],
        fingerprint=fingerprint,
        bytes=10,
        sections=1,
        chunks=chunks,
    )


def _manifest(index_id: str, *documents: IndexedDocument) -> DocIndexManifest:
    return DocIndexManifest(
        index_id=index_id,
        use_case_id="ai-onboarding-assistant",
        documents=documents,
        total_chunks=sum(doc.chunks for doc in documents),
        chunk_config=ChunkConfig(
            chunk_tokens=500, chunk_overlap=0.15, embedding_model_id="fake", dimensions=8
        ),
        built_at=WHEN,
        build_seconds=0.1,
    )


def test_the_grade_card_counts_refusals_and_keeps_an_absent_mean_absent() -> None:
    grade = grade_of(
        _eval(
            (
                _q("A?", passed=True),
                _q("B?", passed=True, refused=True, expect=True),
                _q("C?", passed=False, refused=False, expect=True, failure="refusal_mismatch"),
                _q("D?", passed=False, refused=True, expect=False, failure="refusal_mismatch"),
            )
        )
    )
    assert (grade.refusal_correct, grade.should_refuse, grade.refused_correctly) == (2, 2, 1)
    assert grade.mean_faithfulness is None
    assert (grade.retrieval_hit_rate, grade.mean_correctness) == (0.5, 0.25)
    assert (grade.questions, grade.passed, grade.pass_rate) == (4, 2, 0.5)


def test_only_questions_asked_of_both_whose_verdict_differs_are_changes() -> None:
    left = _eval(
        (
            _q("How do I port?", passed=False, failure="retrieval_miss"),
            _q("What is the fee?", passed=True),
            _q("Only on the left?", passed=True),
        )
    )
    right = _eval(
        (
            _q("how do i  PORT?", passed=True),
            _q("What is the fee?", passed=True),
            _q("Only on the right?", passed=False),
        )
    )
    changes = verdict_changes(left, right)
    assert [(c.question, c.left_passed, c.right_passed, c.left_failure) for c in changes] == [
        ("How do I port?", False, True, "retrieval_miss")
    ]


def test_an_update_counts_reuse_by_bytes_and_id_together() -> None:
    previous = _manifest("x_old", _doc("a.md", "fa", 3), _doc("b.md", "fb", 2), _doc("c.md", "fc", 4))
    built = _manifest(
        "x_new",
        _doc("a.md", "fa", 3),  # unchanged: reused
        _doc("b.md", "fb-edited", 2),  # replaced: re-embedded
        _doc("d.md", "fc", 4),  # c's bytes under another name: a different document, re-embedded
    )
    summary = update_summary(previous, built, added=["d.md"], replaced=["b.md"], removed=["c.md"])
    assert summary.previous_index_id == "x_old"
    assert summary.reused == ("a.md",)
    assert (summary.chunks_reused, summary.chunks_embedded) == (3, 6)
    assert (summary.added, summary.replaced, summary.removed) == (("d.md",), ("b.md",), ("c.md",))
