"""Answer feedback (`engine.generative.feedback`, DEC-1272, DEC-1273): stored redacted, exported blank."""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime, timedelta
from pathlib import Path

from engine.config import ReferenceSetConfig
from engine.generative.feedback import (
    MAX_COMMENT,
    AnswerFeedback,
    FeedbackRating,
    feedback_prefix,
    list_feedback,
    new_feedback,
    reference_set_columns,
    save_feedback,
    thumbs_down_csv,
)
from engine.storage import LocalStorage

INDEX = "x_20261006_feedback"
NOW = datetime(2026, 10, 6, 9, 30, tzinfo=UTC)


def _entry(
    rating: FeedbackRating = "down",
    question: str = "How do I port my number?",
    *,
    comment: str = "",
    answer: str = "",
    at: datetime = NOW,
) -> AnswerFeedback:
    return new_feedback(
        index_id=INDEX,
        rating=rating,
        question=question,
        answer=answer,
        refused=False,
        cited_chunk_ids=("support_porting-00001",),
        comment=comment,
        now=at,
    )


def test_every_free_text_field_is_redacted_and_the_kinds_are_named() -> None:
    entry = _entry(
        question="My number is 9876543210, can I port it?",
        answer="Write to help@northwind.example.com for that.",
        comment="Reach me at asha.verma@example.com",
    )
    stored = entry.model_dump_json()
    for value in ("9876543210", "asha.verma@example.com", "help@northwind.example.com"):
        assert value not in stored, value
    assert "[REDACTED:phone]" in entry.question
    assert "[REDACTED:email]" in entry.comment and "[REDACTED:email]" in entry.answer
    assert entry.redacted == ("email", "phone")


def test_text_is_trimmed_to_its_limit_and_nothing_names_the_person() -> None:
    entry = _entry(comment="x" * (MAX_COMMENT + 50))
    assert len(entry.comment) == MAX_COMMENT
    assert "actor" not in AnswerFeedback.model_fields and "user_id" not in AnswerFeedback.model_fields


def test_entries_are_one_file_each_listed_oldest_first_and_a_broken_file_is_skipped(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    later = _entry("up", "What is the fee?", at=NOW + timedelta(minutes=1))
    earlier = _entry("down", "Is 5G included?")
    save_feedback(storage, later)
    save_feedback(storage, earlier)
    storage.write_text(f"{feedback_prefix(INDEX)}broken.json", "{not json")
    storage.write_text(f"{feedback_prefix(INDEX)}notes.txt", "ignored")
    assert [entry.feedback_id for entry in list_feedback(storage, INDEX)] == [
        earlier.feedback_id,
        later.feedback_id,
    ]
    assert list_feedback(storage, "x_other") == ()


def test_the_export_is_a_reference_set_with_only_the_question_filled_in() -> None:
    entries = (
        _entry("down", "How do I port my number?"),
        _entry("up", "What is the late fee?"),
        _entry("down", "how do I   PORT my number?"),
        _entry("down", "+91 is this a formula?"),
        _entry("down", "Is roaming free in Nepal?"),
    )
    rows = list(csv.reader(io.StringIO(thumbs_down_csv(entries, ReferenceSetConfig()))))
    assert rows == [
        ["question", "expect_refusal", "source_doc", "reference_answer"],
        ["How do I port my number?", "", "", ""],
        ["'+91 is this a formula?", "", "", ""],
        ["Is roaming free in Nepal?", "", "", ""],
    ]


def test_the_export_follows_the_use_cases_own_column_names() -> None:
    config = ReferenceSetConfig(question_column="q", reference_column="gold", refusal_column="refuse")
    assert reference_set_columns(config) == ("q", "refuse", "source_doc", "gold")
    assert thumbs_down_csv((), config) == "q,refuse,source_doc,gold\n"
