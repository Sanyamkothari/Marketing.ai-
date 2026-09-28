"""`numbers_grounded` (Plan G §8.5): every number the helper writes must come from its evidence.

Plan §2.1 rule 5 - nothing fabricated - holds for generated text only if something checks it. Here
the check is arithmetic, not a judge: collect every number the session's tool results, proposals and
the person's own message contain, and refuse a reply that states any other.

Formatting is forgiven, invention is not. `4.2%` matches a rate of `0.042`, `3,000` matches `3000`,
and a value rounded for reading (`12.5` for `12.4837`) matches within half a unit of its last digit.
Numbers inside quotes are names, not claims (`'ad_ctr_90d'`), and are skipped. `0` and `1` are
allowed anywhere: "turn yes into 1 and no into 0" is not a statistic.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any, Final

__all__ = ["grounded_numbers", "numbers_in", "ungrounded_numbers"]

_QUOTED: Final[re.Pattern[str]] = re.compile(r"'[^']*'|\"[^\"]*\"|`[^`]*`")
_NUMBER: Final[re.Pattern[str]] = re.compile(r"(?<![\w.])[-+]?\d[\d,]*(?:\.\d+)?%?(?![\w])")
_ALWAYS: Final[frozenset[float]] = frozenset({0.0, 1.0})


def _parse(token: str) -> tuple[float, int, bool] | None:
    """(value, decimals shown, was a percent) of one number token."""
    percent = token.endswith("%")
    raw = token.rstrip("%").replace(",", "")
    try:
        value = float(raw)
    except ValueError:
        return None
    decimals = len(raw.split(".", 1)[1]) if "." in raw else 0
    return value, decimals, percent


def numbers_in(text: str) -> list[str]:
    """The number tokens of `text`, outside quotes, as written."""
    return _NUMBER.findall(_QUOTED.sub(" ", text))


def _walk(value: Any, out: set[float]) -> None:
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, (int, float)):
        out.add(float(value))
        out.add(float(value) * 100.0)  # a rate shown as a percent
    elif isinstance(value, str):
        for token in numbers_in(value) + _NUMBER.findall(value):
            parsed = _parse(token)
            if parsed is not None:
                out.add(parsed[0])
                if parsed[2]:
                    out.add(parsed[0] / 100.0)
    elif isinstance(value, dict):
        for item in value.values():
            _walk(item, out)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _walk(item, out)


def grounded_numbers(sources: Iterable[Any]) -> frozenset[float]:
    """Every number in `sources` (JSON-like values and strings), plus each value times 100."""
    out: set[float] = set()
    for source in sources:
        _walk(source, out)
    return frozenset(out)


def ungrounded_numbers(text: str, grounded: frozenset[float]) -> list[str]:
    """The number tokens in `text` that no grounded value explains, in order."""
    missing: list[str] = []
    for token in numbers_in(text):
        parsed = _parse(token)
        if parsed is None:
            continue
        value, decimals, percent = parsed
        candidates = {value, value / 100.0} if percent else {value}
        if candidates & _ALWAYS:
            continue
        tolerance = 0.5 * 10.0 ** (-decimals) + 1e-9
        explained = any(
            abs(candidate - known) <= tolerance or abs(candidate - known) <= abs(known) * 0.005
            for candidate in candidates
            for known in grounded
        )
        if not explained:
            missing.append(token)
    return missing
