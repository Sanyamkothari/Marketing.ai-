"""Display formatting shared by the API bodies and the generated docs.

These three functions reproduce the reference prototype's ``fmtN``, ``fmtSize`` and ``nowStamp``,
rounding included: JavaScript's ``toFixed(1)`` and ``Math.round`` round a half *up* on the exact
binary value, where Python's ``round`` and ``format`` round it to even, so ``2500`` renders as
``"3K"`` and ``1.25 MB`` as ``"1.3 MB"`` here exactly as in the browser. The prototype stops at
``M`` and ``MB``; the ``B`` and ``GB`` branches are the one deliberate extension (DEC-040).

They format values that already exist; they never invent one. A caller with nothing to show renders
an em dash itself rather than asking for a formatted zero (plan §2.1 principle 5).
"""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

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

_TENTHS = Decimal("0.1")
_UNITS = Decimal(1)


def _to_fixed_1(value: float) -> str:
    """JavaScript ``value.toFixed(1)`` for a non-negative number: one decimal, a half rounding up.

    ``Decimal(value)`` is the exact binary value, so ``0.15`` (really ``0.1499…``) gives ``"0.1"`` and an
    exact ``1.25`` gives ``"1.3"``, as ``toFixed`` does.
    """
    return str(Decimal(value).quantize(_TENTHS, rounding=ROUND_HALF_UP))


def _js_round(value: float) -> int:
    """JavaScript ``Math.round(value)`` for a non-negative number: the nearest integer, a half rounding up."""
    return int(Decimal(value).quantize(_UNITS, rounding=ROUND_HALF_UP))


def humanise_count(n: int) -> str:
    """Render a row or record count the way the prototype's ``fmtN`` does.

    ``184000 -> "184K"``, ``2500 -> "3K"``, ``2400000 -> "2.4M"``: thousands are ``Math.round``-ed to a
    whole number, millions (and, beyond the prototype, billions) keep one ``toFixed(1)`` decimal, and
    anything under a thousand is printed as-is. A negative count is formatted by magnitude and signed.
    """
    sign = "-" if n < 0 else ""
    magnitude = abs(n)
    if magnitude >= 1_000_000_000:
        return f"{sign}{_to_fixed_1(magnitude / 1_000_000_000)}B"
    if magnitude >= 1_000_000:
        return f"{sign}{_to_fixed_1(magnitude / 1_000_000)}M"
    if magnitude >= 1_000:
        return f"{sign}{_js_round(magnitude / 1_000)}K"
    return f"{sign}{magnitude}"


def humanise_bytes(n: int) -> str:
    """Render a file size the way the prototype's ``fmtSize`` does.

    Binary units: whole bytes below 1 KB, then one ``toFixed(1)`` decimal for KB and MB
    (``1536 -> "1.5 KB"``) and, beyond the prototype, for GB. A negative size is formatted by
    magnitude and signed.
    """
    sign = "-" if n < 0 else ""
    magnitude = abs(n)
    if magnitude >= _GB:
        return f"{sign}{_to_fixed_1(magnitude / _GB)} GB"
    if magnitude >= _MB:
        return f"{sign}{_to_fixed_1(magnitude / _MB)} MB"
    if magnitude >= _KB:
        return f"{sign}{_to_fixed_1(magnitude / _KB)} KB"
    return f"{sign}{magnitude} B"


def humanise_datetime(dt: datetime) -> str:
    """Render a timestamp the way the prototype's ``nowStamp`` does: ``"14 Sep 2026, 10:20"``.

    The month abbreviation is spelled out here rather than taken from ``strftime('%b')``, so the
    output does not change with the machine's locale. The datetime is rendered in whatever timezone
    it carries; it is never converted.
    """
    return f"{dt.day:02d} {_MONTHS[dt.month - 1]} {dt.year:04d}, {dt.hour:02d}:{dt.minute:02d}"
