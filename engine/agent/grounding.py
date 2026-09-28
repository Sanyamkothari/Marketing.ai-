"""`numbers_grounded` (Plan G §8.5): every number the helper writes must come from its evidence.

Plan §2.1 rule 5 - nothing fabricated - holds for generated text only if something checks it. Here
the check is arithmetic, not a judge: collect every number the session's tool results, proposals and
the person's own message contain, and refuse a reply that states any other.

Formatting is forgiven, invention is not. `4.2%` matches a rate of `0.042`, `3,000` matches `3000`,
and a value rounded for reading (`12.5` for `12.4837`) matches within half a unit of its last digit.
A number written with a unit or a multiplier is still a number: `40k` must be grounded as 40 or
40,000, `3x` as 3, `1e5` as 100,000 (M77: these used to escape the check because a letter touched
the digits). A number inside quotes is skipped only when the quoted text is exactly the name of a
column of the file (`'ad_ctr_90d'`, `'Q3 2024 spend'`); any other quoted number is a claim like any
other (M77: `'73%'` and a number between two apostrophes, as in "it's 73% ... don't", used to pass).
`0` and `1` are allowed anywhere: "turn yes into 1 and no into 0" is not a statistic; nor is a small
ordinal ("the 2nd suggestion").
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any, Final

__all__ = ["grounded_numbers", "numbers_in", "ungrounded_numbers"]

_QUOTED: Final[re.Pattern[str]] = re.compile(r"'([^'\n]*)'|\"([^\"\n]*)\"|`([^`\n]*)`")
_NUMBER: Final[re.Pattern[str]] = re.compile(r"(?<![\w.])[-+]?\d[\d,]*(?:\.\d+)?%?[^\W_]*")
_SPLIT: Final[re.Pattern[str]] = re.compile(r"^([-+]?\d[\d,]*(?:\.\d+)?%?)([^\W_]*)$")
_EXPONENT: Final[re.Pattern[str]] = re.compile(r"^[eE][-+]?\d+$")
_MULTIPLIERS: Final[dict[str, float]] = {
    "k": 1e3,
    "m": 1e6,
    "mn": 1e6,
    "mm": 1e6,
    "b": 1e9,
    "bn": 1e9,
    "lakh": 1e5,
    "lakhs": 1e5,
    "cr": 1e7,
    "crore": 1e7,
    "crores": 1e7,
}
_ALWAYS: Final[frozenset[float]] = frozenset({0.0, 1.0})
_MAX_ORDINAL: Final[int] = 20


def _parse(token: str) -> tuple[tuple[tuple[float, float], ...], bool] | None:
    """((value, tolerance) for each thing the token may mean, whether it was a percent).

    The tolerance is half a unit of the last digit shown, scaled by a multiplier (`12.5k` is
    12,450 to 12,550); `12.5%` also means `0.125`.
    """
    match = _SPLIT.match(token)
    if match is None:
        return None
    number, suffix = match.group(1), match.group(2)
    percent = number.endswith("%")
    raw = number.rstrip("%").replace(",", "")
    try:
        value = float(raw)
    except ValueError:
        return None
    decimals = len(raw.split(".", 1)[1]) if "." in raw else 0
    half_unit = 0.5 * 10.0 ** (-decimals) + 1e-9
    if suffix and _EXPONENT.match(suffix):
        return ((float(raw + suffix), 1e-9),), percent
    meanings = [(value, half_unit)]
    multiplier = _MULTIPLIERS.get(suffix.casefold())
    if multiplier is not None:
        meanings.append((value * multiplier, half_unit * multiplier))
    if percent:
        meanings += [(v / 100.0, t / 100.0) for v, t in meanings]
    return tuple(meanings), percent


def _tokens(text: str) -> list[str]:
    return [match.group(0) for match in _NUMBER.finditer(text)]


def numbers_in(text: str, names: Iterable[str] = ()) -> list[str]:
    """The number tokens of `text` as written; a quoted column name is a name, not a number."""
    known = set(names)

    def unquote(match: re.Match[str]) -> str:
        inner = next(group for group in match.groups() if group is not None)
        return " " if inner in known else match.group(0)

    return _tokens(_QUOTED.sub(unquote, text))


def _walk(value: Any, out: set[float]) -> None:
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, (int, float)):
        out.add(float(value))
        out.add(float(value) * 100.0)  # a rate shown as a percent
    elif isinstance(value, str):
        for token in _tokens(value):
            parsed = _parse(token)
            if parsed is not None:
                out.update(number for number, _ in parsed[0])
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


def ungrounded_numbers(text: str, grounded: frozenset[float], names: Iterable[str] = ()) -> list[str]:
    """The number tokens in `text` that no grounded value explains, in order.

    `names` are the file's column names: a quoted span that is exactly one of them is skipped.
    """
    missing: list[str] = []
    for token in numbers_in(text, names):
        parsed = _parse(token)
        if parsed is None:
            continue
        meanings, _ = parsed
        if (
            len(meanings) <= 2
            and any(value in _ALWAYS for value, _ in meanings)
            and not _has_multiplier(token)
        ):
            continue
        if _is_small_ordinal(token):
            continue  # "the 2nd step" counts things on the screen; it states no measurement
        explained = any(
            abs(value - known) <= tolerance or abs(value - known) <= abs(known) * 0.005
            for value, tolerance in meanings
            for known in grounded
        )
        if not explained:
            missing.append(token)
    return missing


def _is_small_ordinal(token: str) -> bool:
    match = _SPLIT.match(token)
    if match is None or match.group(2).casefold() not in {"st", "nd", "rd", "th"}:
        return False
    number = match.group(1)
    return number.isdigit() and int(number) <= _MAX_ORDINAL


def _has_multiplier(token: str) -> bool:
    match = _SPLIT.match(token)
    return bool(match and match.group(2).casefold() in _MULTIPLIERS)
