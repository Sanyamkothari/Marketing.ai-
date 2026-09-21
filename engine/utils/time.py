"""Timezone-aware clock helpers.

Every timestamp the engine writes is UTC and timezone-aware; every timestamp it reads back is
returned timezone-aware too, so artefact round-trips never silently drop the offset.
"""

from __future__ import annotations

from datetime import UTC, datetime

__all__ = ["parse_iso", "to_iso", "utc_now"]


def utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def to_iso(dt: datetime) -> str:
    """Render ``dt`` as an ISO-8601 string that always carries an offset.

    A naive datetime is interpreted as UTC rather than rejected, so callers that build a timestamp
    from a filesystem mtime or a parsed CSV value still produce an unambiguous artefact value.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


def parse_iso(s: str) -> datetime:
    """Parse an ISO-8601 string into a timezone-aware datetime.

    A string without an offset is interpreted as UTC, which makes ``parse_iso(to_iso(dt))`` an
    identity for every datetime ``to_iso`` accepts.
    """
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt
