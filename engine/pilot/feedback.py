"""The in-app feedback loop (Plan E M64, DEC-911).

Every screen carries a feedback button. What a person types is stored in the platform's own
storage - the local artefact root on a laptop, the bucket on a deployment - one small JSON file per
entry under `pilot/feedback/`, and exported for the team as `pilot_feedback.csv` or `.jsonl`.

"No data values": the entry holds the screen's route (ids, never cell values), a category, and the
person's words - which pass through the platform's one redaction (`engine.pii.redact_text`), so a
phone number or e-mail address typed into a comment is stored as a marker naming its kind. The
POST is audited by the access middleware like every write; the audit row carries the entry's id
and a hash of it, never the text (DEC-705).
"""

from __future__ import annotations

import csv
import io
import json
import re
import secrets
from datetime import datetime
from typing import TYPE_CHECKING, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:
    from engine.storage import Storage

__all__ = [
    "FEEDBACK_PREFIX",
    "FeedbackCategory",
    "FeedbackEntry",
    "export_csv",
    "export_jsonl",
    "list_feedback",
    "new_feedback",
    "save_feedback",
]

FEEDBACK_PREFIX: Final[str] = "pilot/feedback/"
MAX_TEXT: Final[int] = 1000
MAX_SCREEN: Final[int] = 200

FeedbackCategory = Literal["confusing", "wrong", "idea", "praise", "other"]

_SCREEN: Final[re.Pattern[str]] = re.compile(r"^[#/A-Za-z0-9_\-.:~]*$")
"""A route: hash, slashes, ids. No query string, so no filter value can ride in on it."""


class FeedbackEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    feedback_id: str
    created_at: datetime
    screen: str = Field(max_length=MAX_SCREEN)
    category: FeedbackCategory
    text: str = Field(max_length=MAX_TEXT)
    actor_id: str = Field(description="Who gave it, as the audit log names them.")
    demo: bool = Field(default=False, description="Given while the demo environment was on.")
    redacted: tuple[str, ...] = Field(
        default=(), description="Kinds of personal detail masked out of the text."
    )

    @field_validator("screen")
    @classmethod
    def _route_only(cls, value: str) -> str:
        if not _SCREEN.fullmatch(value):
            raise ValueError("screen must be the page's route: letters, digits and #/_-.:~ only")
        return value


def new_feedback(
    *, screen: str, category: FeedbackCategory, text: str, actor_id: str, demo: bool, now: datetime
) -> FeedbackEntry:
    """A new entry with the text redacted and trimmed. Raises `ValueError` for a bad screen."""
    from engine.pii import redact_text

    cleaned, kinds = redact_text(text.strip()[:MAX_TEXT])
    return FeedbackEntry(
        feedback_id=f"fb_{now.strftime('%Y%m%dT%H%M%S')}_{secrets.token_hex(4)}",
        created_at=now,
        screen=screen.split("?", 1)[0],
        category=category,
        text=cleaned[:MAX_TEXT],
        actor_id=actor_id,
        demo=demo,
        redacted=tuple(sorted(set(kinds))),
    )


def save_feedback(storage: Storage, entry: FeedbackEntry) -> str:
    key = f"{FEEDBACK_PREFIX}{entry.feedback_id}.json"
    storage.write_model(key, entry)
    return key


def list_feedback(storage: Storage) -> tuple[FeedbackEntry, ...]:
    """Every entry, oldest first. An unreadable file is skipped, never guessed at."""
    entries: list[FeedbackEntry] = []
    for key in storage.list_keys(FEEDBACK_PREFIX):
        if not key.endswith(".json"):
            continue
        try:
            entries.append(storage.read_model(key, FeedbackEntry))
        except Exception:
            continue
    return tuple(sorted(entries, key=lambda e: (e.created_at, e.feedback_id)))


EXPORT_COLUMNS: Final[tuple[str, ...]] = (
    "feedback_id",
    "created_at",
    "screen",
    "category",
    "text",
    "actor_id",
    "demo",
    "redacted",
)


def _formula_safe(value: str) -> str:
    """A cell a spreadsheet would run as a formula is prefixed with a quote (as the audit export does)."""
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


def export_csv(entries: tuple[FeedbackEntry, ...]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(EXPORT_COLUMNS)
    for entry in entries:
        writer.writerow(
            [
                entry.feedback_id,
                entry.created_at.isoformat(),
                _formula_safe(entry.screen),
                entry.category,
                _formula_safe(entry.text),
                _formula_safe(entry.actor_id),
                str(entry.demo).lower(),
                " ".join(entry.redacted),
            ]
        )
    return buffer.getvalue()


def export_jsonl(entries: tuple[FeedbackEntry, ...]) -> str:
    return "".join(json.dumps(entry.model_dump(mode="json"), sort_keys=True) + "\n" for entry in entries)
