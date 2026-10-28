"""Lessons memory (roadmap 2.4): short, general rules the AI learned from a case, kept only when the owner approves.

After a case reaches ready, the AI may propose a lesson (`propose_lesson`): one sentence a later case can use, with no
dataset number, no cell text and no client name. A lesson starts `proposed`; only the owner makes it `approved` or
`rejected` (`POST /doctor/lessons/{id}/approve|reject`). `read_guide("lessons")` shows approved lessons only
(`engine.doctor.guides`), so a lesson nobody approved never reaches a prompt.

The text is checked by code, not trusted: a lesson holds no digit (a rule id such as `R4` is allowed), no quote, no
address or link, and is at most `MAX_WORDS` words, so it cannot carry a count, a date, a value or a name out of a
case. At most `MAX_APPROVED` lessons are approved at once, which keeps the guide under the guides' word limit; the
owner retires one (`POST /doctor/lessons/{id}/retire`) to make room. The store is one small JSON document, `doctor_lessons/lessons.json`, holding the
deployment's lessons (the deployment is the client). It holds no rows and no customer id.
"""

from __future__ import annotations

import hashlib
import re
from typing import Final, Literal

from pydantic import AwareDatetime, Field

from engine.config import StrictBase
from engine.storage import Storage, StorageError
from engine.utils.time import utc_now

__all__ = [
    "LESSONS_KEY",
    "MAX_APPROVED",
    "MAX_PROPOSED",
    "MAX_WORDS",
    "Lesson",
    "LessonError",
    "LessonStore",
    "approved_lessons",
    "check_lesson",
]

LESSONS_KEY: Final[str] = "doctor_lessons/lessons.json"
MAX_WORDS: Final[int] = 18
MAX_APPROVED: Final[int] = 8
MAX_PROPOSED: Final[int] = 20
"""Waiting for the owner at most; a further proposal is refused until one is decided."""

_RULE_ID: Final = re.compile(r"\b[RQ]\d{1,2}\b")
_REFUSED: Final = re.compile(r"\d|[\"'`“”‘’<>@{}\[\]]|https?:|www\.", flags=re.IGNORECASE)


class LessonError(Exception):
    """A lesson that cannot be kept or changed; `code` is the API's error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


class Lesson(StrictBase):
    lesson_id: str
    text: str = Field(description="One general rule, at most `MAX_WORDS` words, no number, quote or name.")
    status: Literal["proposed", "approved", "rejected"] = "proposed"
    case_id: str | None = Field(default=None, description="The case it was learned in.")
    proposed_at: AwareDatetime
    decided_at: AwareDatetime | None = None
    decided_by: str | None = None


class _Document(StrictBase):
    schema_version: Literal[1] = 1
    lessons: tuple[Lesson, ...] = ()


def check_lesson(text: object) -> str:
    """The lesson as one clean sentence, or `LessonError` (`LESSON_REFUSED`) saying why not."""
    clean = " ".join(str(text or "").split())
    words = len(clean.split())
    if not clean or words > MAX_WORDS:
        raise LessonError("LESSON_REFUSED", f"A lesson is one sentence of 1 to {MAX_WORDS} words.")
    if _REFUSED.search(_RULE_ID.sub("", clean)):
        raise LessonError(
            "LESSON_REFUSED",
            "A lesson is a general rule: no number, quote, address or link, and no value from the data.",
        )
    return clean


def _id_of(text: str) -> str:
    return "ls_" + hashlib.sha256(text.casefold().encode("utf-8")).hexdigest()[:12]


class LessonStore:
    """The deployment's lessons, over the artefact store."""

    def __init__(self, storage: Storage) -> None:
        self._storage = storage

    def all(self) -> tuple[Lesson, ...]:
        try:
            if not self._storage.exists(LESSONS_KEY):
                return ()
            return self._storage.read_model(LESSONS_KEY, _Document).lessons
        except (StorageError, ValueError):
            return ()

    def _save(self, lessons: tuple[Lesson, ...]) -> None:
        self._storage.write_model(LESSONS_KEY, _Document(lessons=lessons))

    def propose(self, text: object, case_id: str | None = None) -> Lesson:
        """Keep a checked lesson as `proposed`. The same sentence again is the lesson already there."""
        clean = check_lesson(text)
        lessons = self.all()
        lesson_id = _id_of(clean)
        for existing in lessons:
            if existing.lesson_id == lesson_id:
                return existing
        if sum(1 for item in lessons if item.status == "proposed") >= MAX_PROPOSED:
            raise LessonError("LESSON_QUEUE_FULL", "Too many lessons wait for the owner; none was added.")
        lesson = Lesson(lesson_id=lesson_id, text=clean, case_id=case_id, proposed_at=utc_now())
        self._save((*lessons, lesson))
        return lesson

    def decide(self, lesson_id: str, approve: bool, by: str) -> Lesson:
        """The owner's answer. Approving past `MAX_APPROVED` is refused; a decided lesson is not decided twice."""
        lessons = self.all()
        found = next((item for item in lessons if item.lesson_id == lesson_id), None)
        if found is None:
            raise LessonError("LESSON_NOT_FOUND", "There is no such lesson.")
        if found.status != "proposed":
            raise LessonError("LESSON_DECIDED", f"This lesson is already {found.status}.")
        if approve and sum(1 for item in lessons if item.status == "approved") >= MAX_APPROVED:
            raise LessonError(
                "LESSON_CAP", f"At most {MAX_APPROVED} lessons are kept; reject or retire one first."
            )
        decided = found.model_copy(
            update={
                "status": "approved" if approve else "rejected",
                "decided_at": utc_now(),
                "decided_by": by,
            }
        )
        self._save(tuple(decided if item.lesson_id == lesson_id else item for item in lessons))
        return decided

    def retire(self, lesson_id: str, by: str) -> Lesson:
        """The owner takes an approved lesson back: it becomes `rejected`, leaves the guide and makes room."""
        lessons = self.all()
        found = next((item for item in lessons if item.lesson_id == lesson_id), None)
        if found is None:
            raise LessonError("LESSON_NOT_FOUND", "There is no such lesson.")
        if found.status != "approved":
            raise LessonError(
                "LESSON_NOT_APPROVED", f"Only an approved lesson can be retired; this one is {found.status}."
            )
        retired = found.model_copy(update={"status": "rejected", "decided_at": utc_now(), "decided_by": by})
        self._save(tuple(retired if item.lesson_id == lesson_id else item for item in lessons))
        return retired


def approved_lessons(storage: Storage | None) -> tuple[str, ...]:
    """The text of the approved lessons, oldest first; nothing without a store."""
    if storage is None:
        return ()
    return tuple(item.text for item in LessonStore(storage).all() if item.status == "approved")
