"""Display formatting shared by the API bodies and the generated docs.

These three functions reproduce the reference prototype's ``fmtN``, ``fmtSize`` and ``nowStamp``.
They format values that already exist; they never invent one. A caller with nothing to show renders
an em dash itself rather than asking for a formatted zero (plan §2.1 principle 5).
"""

from __future__ import annotations

from datetime import datetime

__all__ = ["humanise_bytes", "humanise_count", "humanise_datetime"]

_MONTHS: tuple[str, ...] = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)

_KB = 1024
_MB = 1024 * 1024
_GB = 1024 * 1024 * 1024


def humanise_count(n: int) -> str:
    """Render a row or record count the way the prototype's ``fmtN`` does.

    ``184000 -> "184K"``, ``2400000 -> "2.4M"``; thousands are rounded to a whole number, millions
    and billions keep one decimal, and anything under a thousand is printed as-is.
    """
    sign = "-" if n < 0 else ""
    magnitude = abs(n)
    if magnitude >= 1_000_000_000:
        return f"{sign}{magnitude / 1_000_000_000:.1f}B"
    if magnitude >= 1_000_000:
        return f"{sign}{magnitude / 1_000_000:.1f}M"
    if magnitude >= 1_000:
        return f"{sign}{round(magnitude / 1_000)}K"
    return f"{sign}{magnitude}"


def humanise_bytes(n: int) -> str:
    """Render a file size the way the prototype's ``fmtSize`` does.

    Binary units: bytes below 1 KB, whole kilobytes below 1 MB, then one decimal for MB and GB.
    """
    sign = "-" if n < 0 else ""
    magnitude = abs(n)
    if magnitude >= _GB:
        return f"{sign}{magnitude / _GB:.1f} GB"
    if magnitude >= _MB:
        return f"{sign}{magnitude / _MB:.1f} MB"
    if magnitude >= _KB:
        return f"{sign}{round(magnitude / _KB)} KB"
    return f"{sign}{magnitude} B"


def humanise_datetime(dt: datetime) -> str:
    """Render a timestamp the way the prototype's ``nowStamp`` does: ``"14 Sep 2026, 10:20"``.

    The month abbreviation is spelled out here rather than taken from ``strftime('%b')``, so the
    output does not change with the machine's locale. The datetime is rendered in whatever timezone
    it carries; it is never converted.
    """
    return f"{dt.day:02d} {_MONTHS[dt.month - 1]} {dt.year:04d}, {dt.hour:02d}:{dt.minute:02d}"
