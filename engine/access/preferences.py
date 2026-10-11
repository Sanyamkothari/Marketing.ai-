"""A signed-in person's own display choices (Plan M M180, DEC-1610): today one, "Show technical details".

The choice changes what the screens show, never what a person may do: roles alone decide that (`engine.access.roles`).
It is remembered per user so it follows the person to another browser; with sign-in off there is no person to
remember it for, and the browser keeps it instead (`ui/technical.js`).

**Where it is kept.** In the platform database's small key/value table, `platform_setting`
(`engine.platform_db.PlatformSettingRow`), under the key `user_preferences:<user_id>`, as a JSON object. No table and
no column are added, so there is no migration and nothing to back-fill: a user with no row has made no choice, and the
answer is `None` ("not chosen"), which the UI reads as the layout's default. The value holds a user id (already in
the audit trail) and booleans; never a name, a password or anything about a customer.

An unreadable stored value is treated as "not chosen" rather than an error: a display preference must never stop a
person from signing in or reading `GET /auth/me`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from typing import Final

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from engine.platform_db import PLATFORM_SETTING_TABLE, PlatformSettingRow, create_tables
from engine.utils.time import utc_now

__all__ = ["PREFERENCE_KEY_PREFIX", "SqlPreferenceStore", "UserPreferences", "preference_key"]

PREFERENCE_KEY_PREFIX: Final[str] = "user_preferences:"
"""The `platform_setting` key of one user's preferences is this prefix and the user id."""


class UserPreferences(BaseModel):
    """One person's display choices. `None` means "not chosen": the UI then uses its layout's default."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    show_technical: bool | None = Field(
        default=None,
        description='"Show technical details": true shows technical pages and sections, false hides them; '
        "null when the person has not chosen.",
    )


def preference_key(user_id: str) -> str:
    """The `platform_setting` key that holds `user_id`'s preferences."""
    return f"{PREFERENCE_KEY_PREFIX}{user_id}"


class SqlPreferenceStore:
    """Per-user preferences in `platform_setting` (no schema change; see the module docstring)."""

    def __init__(self, engine: Engine, *, clock: Callable[[], datetime] = utc_now) -> None:
        self._engine = engine
        self._clock = clock
        create_tables(engine, (PLATFORM_SETTING_TABLE,))

    def get(self, user_id: str) -> UserPreferences:
        """The person's preferences; every field `None` when nothing was saved or it cannot be read."""
        with Session(self._engine) as session:
            row = session.get(PlatformSettingRow, preference_key(user_id))
            raw = None if row is None else row.value
        if raw is None:
            return UserPreferences()
        try:
            data = json.loads(raw)
            return UserPreferences.model_validate(data if isinstance(data, dict) else {})
        except ValueError:
            return UserPreferences()

    def set_show_technical(self, user_id: str, value: bool) -> UserPreferences:
        """Save "Show technical details" for this person, keeping any other choice they made; the result."""
        current = self.get(user_id)
        updated = current.model_copy(update={"show_technical": bool(value)})
        self._put(user_id, updated)
        return updated

    def _put(self, user_id: str, preferences: UserPreferences) -> None:
        key = preference_key(user_id)
        value = json.dumps(preferences.model_dump(), sort_keys=True)
        at = self._clock()
        with Session(self._engine) as session:
            row = session.get(PlatformSettingRow, key)
            if row is None:
                session.add(PlatformSettingRow(key=key, value=value, updated_at=at))
            else:
                row.value = value
                row.updated_at = at
                session.add(row)
            try:
                session.commit()
            except IntegrityError:  # the same person saved from two tabs at once: the later one wins
                session.rollback()
                existing = session.get(PlatformSettingRow, key)
                if existing is None:
                    raise
                existing.value = value
                existing.updated_at = at
                session.add(existing)
                session.commit()
