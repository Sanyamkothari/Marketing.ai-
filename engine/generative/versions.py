"""Index versions: what an update reused, how a grading reads, and what changed between two gradings.

An index is never edited in place. "Update documents" builds a *new* index from the previous one's
documents plus the added and replaced files, minus the removed ones, through
`engine.generative.index.build_index(previous=...)`, so every unchanged document keeps its passages
and vectors and only the new or changed ones are read and embedded (DEC-1274). The previous index
stays exactly as it was - it can still be asked, graded and compared - which is the same immutable
history a trained model has in the registry.

Everything here is a pure function of artefacts already written (`DocIndexManifest`, `RagEval`), so
the numbers a screen shows are computed once, on the server, and the screen computes nothing
(DEC-1276, DEC-1277).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from pydantic import Field

from engine.contracts import Artefact
from engine.generative.contracts import DocIndexManifest, RagEval, RagEvalQuestion

__all__ = [
    "INDEX_UPDATE_FILENAME",
    "IndexGrade",
    "IndexUpdate",
    "VerdictChange",
    "grade_of",
    "update_summary",
    "verdict_changes",
]

INDEX_UPDATE_FILENAME: Final[str] = "index_update.json"
"""Route bookkeeping beside a new index version, like `index_owner.json`: not a registered artefact."""


class IndexGrade(Artefact):
    """The grade card's numbers for one graded index, every one read off its `rag_eval.json`."""

    questions: int = Field(description="Questions graded.")
    passed: int = Field(description="Questions that passed.")
    pass_rate: float = Field(description="Share of questions that passed, 0 to 1.")
    pass_threshold: float = Field(description="Share the configuration required, 0 to 1.")
    meets_threshold: bool = Field(description="Whether the pass rate reached the threshold.")
    retrieval_hit_rate: float | None = Field(
        default=None, description="Share of answerable questions that found the right document; null if none."
    )
    mean_faithfulness: float | None = Field(
        default=None,
        description="Mean faithfulness over judged questions, 0 to 1; null when none was judged.",
    )
    mean_correctness: float | None = Field(
        default=None, description="Mean correctness over judged questions, 0 to 1; null when none was judged."
    )
    refusal_correct: int = Field(
        description="Questions where refusing or answering matched the reference set."
    )
    should_refuse: int = Field(description="Questions the reference set says should be refused.")
    refused_correctly: int = Field(description="Of those, how many the assistant did refuse.")
    reference_set_fingerprint: str = Field(description="Content hash of the reference file that graded it.")


class VerdictChange(Artefact):
    """One question that passed on one index and failed on the other."""

    question: str = Field(description="The question, as the reference set words it.")
    left_passed: bool = Field(description="Whether it passed on the left-hand index.")
    right_passed: bool = Field(description="Whether it passed on the right-hand index.")
    left_failure: str | None = Field(
        default=None, description="Which check failed on the left; null if it passed."
    )
    right_failure: str | None = Field(
        default=None, description="Which check failed on the right; null if it passed."
    )


class IndexUpdate(Artefact):
    """`index_update.json`: what one "Update documents" changed, and what it did not have to redo."""

    previous_index_id: str = Field(description="The index this version was built from; it is unchanged.")
    added: tuple[str, ...] = Field(default=(), description="Documents that were not in the previous index.")
    replaced: tuple[str, ...] = Field(default=(), description="Documents uploaded again under the same name.")
    removed: tuple[str, ...] = Field(default=(), description="Documents left out of this version.")
    reused: tuple[str, ...] = Field(
        default=(), description="Documents whose passages and vectors were carried over without re-reading."
    )
    chunks_reused: int = Field(description="Passages carried over with their vectors, at no embedding cost.")
    chunks_embedded: int = Field(description="Passages read and embedded for this version.")


def _refusal_counts(questions: Sequence[RagEvalQuestion]) -> tuple[int, int, int]:
    matched = sum(1 for q in questions if q.refused == q.expect_refusal)
    should = sum(1 for q in questions if q.expect_refusal)
    did = sum(1 for q in questions if q.expect_refusal and q.refused)
    return matched, should, did


def grade_of(rag_eval: RagEval) -> IndexGrade:
    """The grade card of one grading: its aggregates plus the refusal counts, counted not estimated."""
    agg = rag_eval.aggregates
    matched, should, did = _refusal_counts(rag_eval.questions)
    return IndexGrade(
        questions=agg.questions,
        passed=agg.passed,
        pass_rate=agg.pass_rate,
        pass_threshold=agg.pass_threshold,
        meets_threshold=agg.meets_threshold,
        retrieval_hit_rate=agg.retrieval_hit_rate,
        mean_faithfulness=agg.mean_faithfulness,
        mean_correctness=agg.mean_correctness,
        refusal_correct=matched,
        should_refuse=should,
        refused_correctly=did,
        reference_set_fingerprint=rag_eval.reference_set_fingerprint,
    )


def _key(question: str) -> str:
    return " ".join(question.lower().split())


def verdict_changes(left: RagEval, right: RagEval) -> tuple[VerdictChange, ...]:
    """Questions asked of both indexes whose pass/fail verdict differs, in the left grading's order.

    Questions are matched by their wording (ignoring case and spacing), because a reference set has
    no id column the engine reads; a question only one side was asked is not a change and is left out.
    """
    right_by_key = {_key(q.question): q for q in right.questions}
    changes: list[VerdictChange] = []
    seen: set[str] = set()
    for q in left.questions:
        key = _key(q.question)
        other = right_by_key.get(key)
        if other is None or key in seen or other.passed == q.passed:
            continue
        seen.add(key)
        changes.append(
            VerdictChange(
                question=q.question,
                left_passed=q.passed,
                right_passed=other.passed,
                left_failure=q.failure,
                right_failure=other.failure,
            )
        )
    return tuple(changes)


def update_summary(
    previous: DocIndexManifest,
    built: DocIndexManifest,
    *,
    added: Sequence[str],
    replaced: Sequence[str],
    removed: Sequence[str],
) -> IndexUpdate:
    """What an update reused: documents whose bytes and id both match the previous manifest's.

    The same identity `build_index`'s own reuse is keyed on - `(fingerprint, doc_id)` - so a document
    counted as reused here is exactly one whose passages and vectors the build carried over.
    """
    before = {(doc.fingerprint, doc.doc_id) for doc in previous.documents if doc.chunks}
    reused = [doc for doc in built.documents if doc.chunks and (doc.fingerprint, doc.doc_id) in before]
    chunks_reused = sum(doc.chunks for doc in reused)
    return IndexUpdate(
        previous_index_id=previous.index_id,
        added=tuple(sorted(added)),
        replaced=tuple(sorted(replaced)),
        removed=tuple(sorted(removed)),
        reused=tuple(sorted(doc.name for doc in reused)),
        chunks_reused=chunks_reused,
        chunks_embedded=built.total_chunks - chunks_reused,
    )
