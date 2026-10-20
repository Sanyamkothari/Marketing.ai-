"""Every number a model writes must be a number code computed (plan-v2 section 2, the Analyst row).

An LLM is good at plain English and bad at arithmetic, and a confident wrong number in a briefing
is worse than no briefing. So agents hand the model a dict of numbers computed by code and let it
write words around them; `check_numbers` then reads every number back out of the text and checks
it against that dict. Anything it cannot match is returned, as written, so the caller can ask again
with the problems listed or fall back to a template written by code.

What counts as a number: integers, decimals, thousands separators (`1,900`), percentages (`12%`,
`12 percent`, `3 percentage points`), money with a symbol or code before it (`$1,235`, `EUR 40`),
and size suffixes (`1.2k`, `$3M`, `2.5 million`, `1bn`). Signs are ignored: the check is about the
size of a number, not its direction ("fell by $200" for -200 passes). Every digit counts, so dates,
ids and ordinals in the text are flagged too; the model is told not to write them.

How close is close enough: a number matches a value when the value, rounded to the precision the
number is written in, gives that number. `$1,235` matches 1234.56; `$1.2k` matches 1234.56;
`12%` matches 0.1249 (a rate) or 12.4 (a percent). Whole numbers ending in zeros may also be rounded
to those zeros (`1,900` for 1,873), but only within `rel_tol` of the value, so `2,000` does not
stand in for 1,873. A percentage also matches a value given as a fraction (`0.08%` for 0.0008).
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping

DEFAULT_REL_TOL = 0.05
"""How far a number rounded to its trailing zeros may sit from the true value, as a share of it."""

_SUFFIX_SCALE: dict[str, float] = {
    "k": 1e3,
    "thousand": 1e3,
    "m": 1e6,
    "mn": 1e6,
    "million": 1e6,
    "b": 1e9,
    "bn": 1e9,
    "billion": 1e9,
}
"""Size words and letters. A lone `m` is read as million, never minutes: if it is wrong the number is
flagged (safe), while reading `$5m` as 5 would let a wrong number through (unsafe)."""

_NUMBER = re.compile(
    r"(?P<cur>[$€£₹¥]\s?|\b(?:USD|EUR|GBP|INR|Rs\.?)\s?)?"
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)"
    r"(?:\s?(?P<suf>thousand|million|billion|mn|bn|[kKmMbB])(?![A-Za-z]))?"
    r"(?P<pct>\s?(?:%|percentage\s+points?\b|percent\b|per\s+cent\b|pct\b|pp\b))?",
    re.IGNORECASE,
)


def _reads_as(value: float, written: float, half_unit: float, coarse_half: float, rel_tol: float) -> bool:
    """True when `written` is `value` rounded to the precision it is written in (see the module doc)."""
    diff = abs(written - abs(value))
    slack = 1e-9 * max(1.0, abs(value))
    if diff <= half_unit + slack:
        return True
    return coarse_half > half_unit and diff <= coarse_half + slack and diff <= rel_tol * abs(value) + slack


def check_numbers(text: str, numbers: Mapping[str, float], *, rel_tol: float = DEFAULT_REL_TOL) -> list[str]:
    """The numbers in `text` that match no value in `numbers`, as written, each listed once.

    An empty list means every number in the text is backed by code. `numbers` maps a name to its
    value; the names are not used here, only the values (a model may round, but not invent).
    """
    values = [float(v) for v in numbers.values() if not isinstance(v, bool) and math.isfinite(float(v))]
    problems: list[str] = []
    for match in _NUMBER.finditer(text):
        raw = match.group("num")
        suffix = (match.group("suf") or "").lower()
        scale = _SUFFIX_SCALE.get(suffix, 1.0)
        plain = raw.replace(",", "")
        whole, _, decimals = plain.partition(".")
        written = float(plain) * scale
        half_unit = 0.5 * scale * 10.0 ** -len(decimals)
        trailing_zeros = len(whole) - len(whole.rstrip("0")) if not decimals and whole.strip("0") else 0
        coarse_half = 0.5 * scale * 10.0**trailing_zeros
        candidates = list(values)
        if match.group("pct"):
            candidates += [v * 100.0 for v in values]
        if not any(_reads_as(v, written, half_unit, coarse_half, rel_tol) for v in candidates):
            snippet = match.group(0).strip()
            if snippet not in problems:
                problems.append(snippet)
    return problems
