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

Approved lessons are put into the playbook prompt at build (`lessons_text`, under the guide index), so they act even
when the AI never reads the guide. Every write reads strictly (a failed read raises; it never rebuilds the document
from nothing) and holds a process lock, so concurrent proposals and approvals keep each other's changes.
"""

from __future__ import annotations

import hashlib
import re
import threading
from collections.abc import Callable, Iterable
from functools import cache
from typing import Final, Literal

from pydantic import AwareDatetime, Field

from engine.config import StrictBase
from engine.storage import Storage, StorageError
from engine.utils.time import utc_now

__all__ = [
    "LESSONS_KEY",
    "MAX_APPROVED",
    "MAX_LINE_CHARS",
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
MAX_LINE_CHARS: Final[int] = 160
MAX_APPROVED: Final[int] = 8
MAX_PROPOSED: Final[int] = 20
"""Waiting for the owner at most; a further proposal is refused until one is decided."""

_RULE_ID: Final = re.compile(r"\b[RQ]\d{1,2}\b")
_WORD: Final = re.compile(r"\w+")
_LOCK: Final[threading.Lock] = threading.Lock()
"""One read-modify-write of `LESSONS_KEY` at a time in this process (sync routes run in a threadpool)."""
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


@cache
def known_rule_ids() -> frozenset[str]:
    """The rule ids (`R4`) the shipped guides define and the question ids (`Q12`) of the catalogue."""
    from engine.doctor.guides import _folder
    from engine.doctor.questions import CATALOGUE

    found: set[str] = set(CATALOGUE)
    folder = _folder(None)
    for path in folder.glob("*.md") if folder.is_dir() else ():
        found.update(re.findall(r"\bR\d{1,2}\b", path.read_text(encoding="utf-8")))
    return frozenset(found)


def _phrases(case_words: Iterable[str]) -> tuple[tuple[str, ...], ...]:
    """Each name or value of the case as a run of case-folded words (a single letter or a stop word is no name)."""
    seen: set[tuple[str, ...]] = set()
    for raw in case_words:
        words = tuple(_WORD.findall(str(raw or "").casefold()))
        if not words or (len(words) == 1 and len(words[0]) < 3):
            continue
        seen.add(words)
    return tuple(sorted(seen))


def check_lesson(text: object, case_words: Iterable[str] = ()) -> str:
    """The lesson as one clean sentence, or `LessonError` (`LESSON_REFUSED`) saying why not.

    `case_words` are the case's column names, file names and profile top values (names only reach this check;
    nothing is logged): a lesson holding one of them, case-folded and as whole words, is refused, so a client
    name or a cell value cannot ride into every later prompt. Rule and question ids must exist (`known_rule_ids`).
    """
    clean = " ".join(str(text or "").split())
    words = len(clean.split())
    if not clean or words > MAX_WORDS or len(clean) > MAX_LINE_CHARS:
        raise LessonError(
            "LESSON_REFUSED",
            f"A lesson is one sentence of 1 to {MAX_WORDS} words and at most {MAX_LINE_CHARS} characters.",
        )
    unknown = sorted(set(_RULE_ID.findall(clean)) - known_rule_ids())
    if unknown:
        raise LessonError("LESSON_REFUSED", "A lesson names only rule and question ids the guides define.")
    if _REFUSED.search(_RULE_ID.sub("", clean)):
        raise LessonError(
            "LESSON_REFUSED",
            "A lesson is a general rule: no number, quote, address or link, and no value from the data.",
        )
    mine = tuple(_WORD.findall(clean.casefold()))
    for phrase in _phrases(case_words):
        size = len(phrase)
        if any(mine[i : i + size] == phrase for i in range(len(mine) - size + 1)):
            raise LessonError(
                "LESSON_REFUSED",
                "A lesson is a general rule: it names no column, file or value of this case.",
            )
    return clean


def _id_of(text: str) -> str:
    return "ls_" + hashlib.sha256(text.casefold().encode("utf-8")).hexdigest()[:12]


class LessonStore:
    """The deployment's lessons, over the artefact store."""

    def __init__(self, storage: Storage) -> None:
        self._storage = storage

    def all(self) -> tuple[Lesson, ...]:
        """For reading only (the guide, the GET route): a bad read is no lessons. Writers use `_read_strict`."""
        try:
            return self._read_strict()
        except (StorageError, ValueError):
            return ()

    def _read_strict(self) -> tuple[Lesson, ...]:
        """The stored lessons; a read that fails raises, so no writer builds a new document from nothing."""
        if not self._storage.exists(LESSONS_KEY):
            return ()
        return self._storage.read_model(LESSONS_KEY, _Document).lessons

    def _save(self, lessons: tuple[Lesson, ...]) -> None:
        self._storage.write_model(LESSONS_KEY, _Document(lessons=lessons))

    def _change(
        self, change: Callable[[tuple[Lesson, ...]], tuple[tuple[Lesson, ...] | None, Lesson]]
    ) -> Lesson:
        """Read, change and write the document under the lock; `change` returns (new lessons or None, result)."""
        with _LOCK:
            lessons, result = change(self._read_strict())
            if lessons is not None:
                self._save(lessons)
            return result

    def propose(self, text: object, case_id: str | None = None, case_words: Iterable[str] = ()) -> Lesson:
        """Keep a checked lesson as `proposed`. The same sentence again is the lesson already there."""
        clean = check_lesson(text, case_words)
        return self._change(lambda lessons: self._propose(lessons, clean, case_id))

    def _propose(
        self, lessons: tuple[Lesson, ...], clean: str, case_id: str | None
    ) -> tuple[tuple[Lesson, ...] | None, Lesson]:
        lesson_id = _id_of(clean)
        for existing in lessons:
            if existing.lesson_id == lesson_id:
                return None, existing
        if sum(1 for item in lessons if item.status == "proposed") >= MAX_PROPOSED:
            raise LessonError("LESSON_QUEUE_FULL", "Too many lessons wait for the owner; none was added.")
        lesson = Lesson(lesson_id=lesson_id, text=clean, case_id=case_id, proposed_at=utc_now())
        return (*lessons, lesson), lesson

    def decide(self, lesson_id: str, approve: bool, by: str) -> Lesson:
        """The owner's answer. Approving past `MAX_APPROVED` is refused; a decided lesson is not decided twice."""
        return self._change(lambda lessons: self._decide(lessons, lesson_id, approve, by))

    def _decide(
        self, lessons: tuple[Lesson, ...], lesson_id: str, approve: bool, by: str
    ) -> tuple[tuple[Lesson, ...] | None, Lesson]:
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
        return tuple(decided if item.lesson_id == lesson_id else item for item in lessons), decided

    def retire(self, lesson_id: str, by: str) -> Lesson:
        """The owner takes an approved lesson back: it becomes `rejected`, leaves the guide and makes room."""
        return self._change(lambda lessons: self._retire(lessons, lesson_id, by))

    def _retire(
        self, lessons: tuple[Lesson, ...], lesson_id: str, by: str
    ) -> tuple[tuple[Lesson, ...] | None, Lesson]:
        found = next((item for item in lessons if item.lesson_id == lesson_id), None)
        if found is None:
            raise LessonError("LESSON_NOT_FOUND", "There is no such lesson.")
        if found.status != "approved":
            raise LessonError(
                "LESSON_NOT_APPROVED", f"Only an approved lesson can be retired; this one is {found.status}."
            )
        retired = found.model_copy(update={"status": "rejected", "decided_at": utc_now(), "decided_by": by})
        return tuple(retired if item.lesson_id == lesson_id else item for item in lessons), retired


def approved_lessons(storage: Storage | None) -> tuple[str, ...]:
    """The text of the approved lessons, oldest first and at most `MAX_APPROVED`; nothing without a store."""
    if storage is None:
        return ()
    return tuple(item.text for item in LessonStore(storage).all() if item.status == "approved")[:MAX_APPROVED]


def lessons_text(lessons: Iterable[str]) -> str:
    """The approved lessons as the prompt's playbook lines (appended to the guide index), or "" with none."""
    lines = [f"- {item}" for item in lessons]
    return (
        (
            "Lessons the owner approved (apply them; `read_guide('lessons')` has the detail):\n"
            + "\n".join(lines)
        )
        if lines
        else ""
    )
