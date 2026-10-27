"""The person asking in a chat test: the registry checks the role of whoever asks on every tool call."""

from __future__ import annotations

from typing import Final

from engine.access.roles import Principal, Role

ANALYST: Final[Principal] = Principal(
    user_id="u-analyst", username="analyst", roles=frozenset({Role.ANALYST}), kind="user"
)
