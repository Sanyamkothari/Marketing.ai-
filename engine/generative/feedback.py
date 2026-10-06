"""Thumbs up or down on one answer of the assistant, and turning the "down"s into test questions.

A reference set is only as good as the questions in it, and the questions people actually ask - and
were let down by - are the best ones a deployment will ever get. This module stores a person's
verdict on one answer and hands the thumbs-down questions back as rows of a reference-set file, so
the next grading includes them (DEC-1272, DEC-1273).

**One file per entry, never rewritten.** An entry is written once to
`indexes/{index_id}/feedback/{feedback_id}.json` and never edited, the way the pilot's feedback
button stores its entries (`engine.pilot.feedback`, DEC-911): there is no read-modify-write of a
shared file, so two people pressing the button at once cannot lose each other's entry, and the
`Storage` protocol needs no append operation it does not have. Deleting the index deletes them.

**Nothing personal beyond the question.** The question, the answer and the comment each pass
through the platform's one redaction (`engine.pii.redact_text`) before they are stored, so an e-mail
address or phone number typed into a comment - or into the question itself - is kept as a marker
naming its kind, never as the value. Who pressed the button is not stored here at all: the access
middleware's audit row already records the person against the entry id (DEC-705), and a second copy
of that would be a second place a name could leak from.

**A reference answer is never invented.** `thumbs_down_csv` writes the question and leaves
`reference_answer`, `expect_refusal` and `source_doc` empty. Whatever the assistant said was judged
wrong by a person; copying it, or asking a model for a better one, would put an unchecked answer into
the very file that is supposed to check answers. A person fills those cells in.
"""

from __future__ import annotations

import csv
import io
import secrets
from datetime import datetime
from typing import TYPE_CHECKING, Final, Literal

from pydantic import AwareDatetime, Field

from engine.contracts import Artefact
from engine.storage import StorageError, index_key

if TYPE_CHECKING:
    from collections.abc import Sequence

    from engine.config import ReferenceSetConfig
    from engine.storage import Storage

__all__ = [
    "FEEDBACK_DIR",
    "MAX_ANSWER",
    "MAX_COMMENT",
    "MAX_QUESTION",
    "AnswerFeedback",
    "FeedbackRating",
    "feedback_prefix",
    "list_feedback",
    "new_feedback",
    "reference_set_columns",
    "save_feedback",
    "thumbs_down_csv",
]

FEEDBACK_DIR: Final[str] = "feedback"
MAX_QUESTION: Final[int] = 2000
MAX_ANSWER: Final[int] = 8000
MAX_COMMENT: Final[int] = 1000
MAX_CITED: Final[int] = 20

FeedbackRating = Literal["up", "down"]


class AnswerFeedback(Artefact):
    """One person's verdict on one answer: `indexes/{index_id}/feedback/{feedback_id}.json`."""

    feedback_id: str = Field(description="Id of this entry, sortable by when it was given.")
    index_id: str = Field(description="Index whose answer was rated.")
    rating: FeedbackRating = Field(description="`up` when the answer helped, `down` when it did not.")
    question: str = Field(description="The question as asked, with contact details masked.")
    answer: str = Field(default="", description="The answer that was rated, with contact details masked.")
    refused: bool = Field(default=False, description="Whether the rated answer was a refusal.")
    cited_chunk_ids: tuple[str, ...] = Field(
        default=(), description="Chunks the rated answer cited, strongest first."
    )
    comment: str = Field(default="", description="What the person added, with contact details masked.")
    redacted: tuple[str, ...] = Field(
        default=(), description="Kinds of personal detail masked out of the question, answer or comment."
    )
    created_at: AwareDatetime = Field(description="UTC time the feedback was given.")


def feedback_prefix(index_id: str) -> str:
    """The storage prefix every feedback entry of `index_id` sits under, with its trailing slash."""
    return index_key(index_id, FEEDBACK_DIR) + "/"


def new_feedback(
    *,
    index_id: str,
    rating: FeedbackRating,
    question: str,
    answer: str,
    refused: bool,
    cited_chunk_ids: Sequence[str],
    comment: str,
    now: datetime,
) -> AnswerFeedback:
    """A new entry with every free-text field redacted and trimmed to its limit."""
    from engine.pii import redact_text

    kinds: set[str] = set()

    def clean(text: str, limit: int) -> str:
        redacted, found = redact_text(text.strip()[:limit])
        kinds.update(found)
        return redacted[:limit]

    return AnswerFeedback(
        feedback_id=f"fb_{now.strftime('%Y%m%dT%H%M%S%f')}_{secrets.token_hex(4)}",
        index_id=index_id,
        rating=rating,
        question=clean(question, MAX_QUESTION),
        answer=clean(answer, MAX_ANSWER),
        refused=refused,
        cited_chunk_ids=tuple(cited_chunk_ids)[:MAX_CITED],
        comment=clean(comment, MAX_COMMENT),
        redacted=tuple(sorted(kinds)),
        created_at=now,
    )


def save_feedback(storage: Storage, entry: AnswerFeedback) -> str:
    """Write `entry` to its own file and return the key. Never overwrites: the id is fresh."""
    key = f"{feedback_prefix(entry.index_id)}{entry.feedback_id}.json"
    storage.write_model(key, entry)
    return key


def list_feedback(storage: Storage, index_id: str) -> tuple[AnswerFeedback, ...]:
    """Every entry for `index_id`, oldest first. A file that will not read is skipped, never guessed at."""
    entries: list[AnswerFeedback] = []
    for key in storage.list_keys(feedback_prefix(index_id)):
        if not key.endswith(".json"):
            continue
        try:
            entries.append(storage.read_model(key, AnswerFeedback))
        except (StorageError, ValueError):
            continue
    return tuple(sorted(entries, key=lambda entry: (entry.created_at, entry.feedback_id)))


def reference_set_columns(reference_set: ReferenceSetConfig) -> tuple[str, str, str, str]:
    """The columns of a reference-set file, in the order the bundled sample set uses them."""
    from engine.generative.evaluation import SOURCE_DOC_COLUMN

    return (
        reference_set.question_column,
        reference_set.refusal_column,
        SOURCE_DOC_COLUMN,
        reference_set.reference_column,
    )


def _formula_safe(value: str) -> str:
    """A cell a spreadsheet would run as a formula gets a leading quote (as every export here does)."""
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


def _same_question(text: str) -> str:
    return " ".join(text.lower().split())


def thumbs_down_csv(entries: Sequence[AnswerFeedback], reference_set: ReferenceSetConfig) -> str:
    """Every thumbs-down question once, as rows of a reference-set file with the answer left empty.

    Questions are de-duplicated ignoring case and spacing and kept in the order they were first
    rated down. Only the question column is filled; the other three are for a person to complete.
    """
    seen: set[str] = set()
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(reference_set_columns(reference_set))
    for entry in entries:
        if entry.rating != "down" or not entry.question:
            continue
        key = _same_question(entry.question)
        if key in seen:
            continue
        seen.add(key)
        writer.writerow([_formula_safe(entry.question), "", "", ""])
    return buffer.getvalue()
