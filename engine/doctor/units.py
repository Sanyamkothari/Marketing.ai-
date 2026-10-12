"""Unit words in values, and numbers that look like they are in another unit (Plan O M238, DEC-1808 (e)).

`detect_units` is a READ tool with rules only. For a text column it reads each value the way the `convert_units`
step will (`engine.agent.value_steps._UNIT_VALUE`: a number, then a unit word) and sorts the units it finds
into families with frozen factors:

| Family | Units (any case) | Target |
|---|---|---|
| data | `KB`, `MB`, `GB`, `TB` (1024 apart) | the column's most common unit |
| time | `sec`/`s`, `min`, `hr`/`hrs`/`hour`, `day`/`days` | the column's most common unit |
| weight | `g`, `kg` | the column's most common unit |
| Indian scale | `K` (10³), `L`/`lac`/`lakh` (10⁵), `Cr`/`crore` (10⁷) | plain numbers (factor 1) |

What it proposes:

* **One family, every value with a unit** - a pending `convert_units` step, after the rules verify it: the step
  is run on the column and proposed only when no more than the use case's failure limit fails.
* **Units and plain numbers mixed** - one question: which unit are the plain numbers in? Each option is a
  `convert_units` step with `bare: as_target` and that unit as the target; "I don't know" adds nothing.
* **Two families in one column** (`2 GB` beside `3 kg`) or **`L` with nothing to say whether it is litres or
  lakh** - `UNIT_MISMATCH`, no proposal: the person says what the column holds.
* **A currency token** (`₹`, `Rs`, `$`, …) - reported (`currencies`), never converted here: a currency is changed
  only at a dated rate the person gives (decision O6, M242), and `₹2.5 L` is read by `parse_number` with Indian
  scale words (M241).

For a column of numbers it looks for a **magnitude jump**: most values near one size and a group near a size
about 10³, 10⁵ or 10⁷ times smaller or larger (thousands, lakhs, crores). That is a doubt (design §6.7), not a
fix: what would be done (leave the values as written, flag the column) and what changes if not.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping
from typing import Any, Final, Literal

import numpy as np
import pandas as pd
from pydantic import Field

from engine.agent.contracts import ProposalSource, RecipeStepKind
from engine.agent.formats import CURRENCY_TOKENS, MAX_EXAMPLES, masked_cut
from engine.agent.value_steps import _UNIT_VALUE, convert_units
from engine.config import StrictBase
from engine.doctor.row_contracts import Doubt, StepProposal

__all__ = [
    "UNIT_CODES",
    "UNIT_FAMILIES",
    "UnitCount",
    "UnitQuestion",
    "UnitReport",
    "detect_units",
]

UNIT_CODES: Final[frozenset[str]] = frozenset({"UNIT_MISMATCH"})

Family = Literal["data", "time", "weight", "indian_scale"]

UNIT_FAMILIES: Final[Mapping[Family, Mapping[str, float]]] = {
    "data": {"kb": 1.0, "mb": 1024.0, "gb": 1024.0**2, "tb": 1024.0**3},
    "time": {
        "s": 1.0, "sec": 1.0, "secs": 1.0, "second": 1.0, "seconds": 1.0,
        "min": 60.0, "mins": 60.0, "minute": 60.0, "minutes": 60.0,
        "hr": 3600.0, "hrs": 3600.0, "hour": 3600.0, "hours": 3600.0,
        "day": 86400.0, "days": 86400.0,
    },
    "weight": {"g": 1.0, "gm": 1.0, "gms": 1.0, "kg": 1000.0, "kgs": 1000.0},
    "indian_scale": {
        "k": 1e3, "l": 1e5, "lac": 1e5, "lacs": 1e5, "lakh": 1e5, "lakhs": 1e5,
        "cr": 1e7, "crs": 1e7, "crore": 1e7, "crores": 1e7,
    },
}  # fmt: skip
"""Each family's units in one base unit; the step's factors are these divided by the target's."""

_SCALE_WORDS: Final[frozenset[str]] = frozenset(UNIT_FAMILIES["indian_scale"]) - {"l", "k"}
_MIN_UNIT_SHARE: Final[float] = 0.5
_JUMPS: Final[Mapping[int, str]] = {3: "thousands", 5: "lakhs", 7: "crores"}
_MIN_JUMP_GROUP: Final[int] = 5
_MIN_JUMP_SHARE: Final[float] = 0.01


class UnitOption(StrictBase):
    option_id: str = Field(description="Stable within the question.")
    label: str = Field(description="Button text.")
    step: StepProposal | None = Field(default=None, description="The step it adds; null for 'I don't know'.")


class UnitQuestion(StrictBase):
    """One atomic question: which unit are the plain numbers in?"""

    code: str = Field(default="UNIT_MISMATCH", description="The detector.")
    text: str = Field(description="The question.")
    blocking: bool = Field(default=False, description="Non-blocking: leaving the column as is stays safe.")
    options: tuple[UnitOption, ...] = Field(description="One per unit found, then 'I don't know'.")


class UnitCount(StrictBase):
    unit: str = Field(description="A unit word, lower case.")
    count: int = Field(ge=0, description="Values written with it.")


class UnitReport(StrictBase):
    """What `detect_units` found in one column."""

    column: str = Field(description="The column.")
    non_empty: int = Field(ge=0, description="Non-empty values.")
    units: tuple[UnitCount, ...] = Field(
        default=(), description="Each unit word found (lower case) and its count."
    )
    families: tuple[Family, ...] = Field(default=(), description="The families those units belong to.")
    bare_numbers: int = Field(default=0, ge=0, description="Values that are a number with no unit.")
    unreadable: int = Field(default=0, ge=0, description="Values that are neither.")
    currencies: tuple[str, ...] = Field(
        default=(), description="Currency tokens found in values (never converted)."
    )
    finding: str | None = Field(
        default=None, description="`UNIT_MISMATCH` when the units disagree; else null."
    )
    sentence: str = Field(default="", description="What was found, in plain words, from the counts.")
    step: StepProposal | None = Field(default=None, description="A pending `convert_units` step, when sure.")
    question: UnitQuestion | None = Field(default=None, description="When plain numbers sit beside units.")
    doubt: Doubt | None = Field(default=None, description="A magnitude jump in a column of numbers.")
    examples: tuple[str, ...] = Field(default=(), description="Up to five masked unreadable examples.")


def _family(units: Mapping[str, int], currencies: tuple[str, ...]) -> tuple[list[Family], bool]:
    """(the families of `units`, whether an `L` stays ambiguous between litres and lakh)."""
    families: set[Family] = set()
    ambiguous = False
    for unit in units:
        hits = [name for name, table in UNIT_FAMILIES.items() if unit in table]
        families.update(hits)
    if "l" in units and not (set(units) & _SCALE_WORDS) and not currencies:
        ambiguous = True  # "2 L": litres or lakh? Nothing else in the column says
    return sorted(families), ambiguous


def _factors(family: Family, target: str, units: Mapping[str, int]) -> dict[str, float]:
    table = UNIT_FAMILIES[family]
    base = 1.0 if family == "indian_scale" else table[target]
    return {unit: table[unit] / base for unit in sorted(units)}


def _currencies(texts: pd.Series[Any]) -> tuple[str, ...]:
    found: list[str] = []
    for token in CURRENCY_TOKENS:
        # Case kept: "Rs" is a currency, the "rs" of "hrs" is not.
        if texts.str.contains(token, regex=False, case=True).any():
            found.append(token)
    return tuple(dict.fromkeys(found))


def _proposal(
    column: str,
    series: pd.Series[Any],
    units: dict[str, float],
    bare: str,
    max_failure_pct: float,
    title: str,
) -> StepProposal | None:
    outcome = convert_units(series, units=units, bare=bare)
    non_empty = int(series.notna().sum())
    if not non_empty or outcome.failed / non_empty * 100.0 > max_failure_pct:
        return None
    return StepProposal(
        kind=RecipeStepKind.CONVERT_UNITS,
        column=column,
        params={"units": [[unit, factor] for unit, factor in units.items()], "bare": bare},
        source=ProposalSource.RULES,
        title=title,
        reason=f"{outcome.changed:,} of {non_empty:,} values carry a unit; the rules checked every one converts.",
        changed=outcome.changed,
        out_of=non_empty,
        share_pct=round(outcome.changed / non_empty * 100.0, 4),
    )


def _jump(column: str, series: pd.Series[Any]) -> Doubt | None:
    numbers = pd.to_numeric(series, errors="coerce").dropna()
    positive = numbers[numbers > 0].to_numpy(dtype=float)
    if len(positive) < 2 * _MIN_JUMP_GROUP:
        return None
    logs = np.sort(np.log10(positive))
    gaps = np.diff(logs)
    cut = int(np.argmax(gaps))
    small, large = logs[: cut + 1], logs[cut + 1 :]
    if gaps[cut] < 2.0 or min(len(small), len(large)) < max(_MIN_JUMP_GROUP, _MIN_JUMP_SHARE * len(logs)):
        return None
    ratio = float(np.median(large) - np.median(small))
    power = min(_JUMPS, key=lambda p: abs(p - ratio))
    if abs(power - ratio) > math.log10(2.0):
        return None
    minor_small = len(small) < len(large)
    minor = len(small) if minor_small else len(large)
    near_major = 10 ** float(np.median(large if minor_small else small))
    near_minor = 10 ** float(np.median(small if minor_small else large))
    word = _JUMPS[power]
    return Doubt(
        code="UNIT_MISMATCH",
        column=column,
        because=(
            f"'{column}' has values near {near_major:,.4g} for most rows and near {near_minor:,.4g} for "
            f"{minor:,} rows, about 10^{power} apart, which could be the same amounts written in {word}."
        ),
        would_do=f"leave these {minor:,} values as written and flag the column",
        if_not=f"if they are in {word}, those {minor:,} values are 10^{power} times {'too small' if minor_small else 'too large'}",
    )


def detect_units(series: pd.Series[Any], *, column: str, max_failure_pct: float = 5.0) -> UnitReport:
    """Units in `series`'s values, and what to do about them (see the module docstring)."""
    present = series.dropna()
    non_empty = len(present)
    if pd.api.types.is_bool_dtype(series) or pd.api.types.is_datetime64_any_dtype(series):
        return UnitReport(column=column, non_empty=non_empty)
    if pd.api.types.is_numeric_dtype(series):
        doubt = _jump(column, series)
        return UnitReport(
            column=column,
            non_empty=non_empty,
            bare_numbers=non_empty,
            finding="UNIT_MISMATCH" if doubt else None,
            sentence=doubt.because if doubt else "",
            doubt=doubt,
        )
    texts = present.astype(str).str.strip()
    currencies = _currencies(texts)
    counts: Counter[str] = Counter()
    bare = 0
    unreadable: list[str] = []
    for text, n in Counter(texts.tolist()).items():
        match = _UNIT_VALUE.match(text)
        if match is None:
            unreadable += [text] * n
        elif match.group(2):
            counts[match.group(2).casefold()] += n
        else:
            bare += n
    units = dict(sorted(counts.items()))
    report: dict[str, Any] = {
        "column": column,
        "non_empty": non_empty,
        "units": tuple(UnitCount(unit=u, count=n) for u, n in units.items()),
        "bare_numbers": bare,
        "unreadable": len(unreadable),
        "currencies": currencies,
        "examples": tuple(masked_cut(t, 60) for t in list(dict.fromkeys(unreadable))[:MAX_EXAMPLES]),
    }
    known = {u: n for u, n in units.items() if any(u in t for t in UNIT_FAMILIES.values())}
    if not known or sum(known.values()) < _MIN_UNIT_SHARE * non_empty:
        if bare >= _MIN_UNIT_SHARE * non_empty and not units:
            doubt = _jump(column, pd.to_numeric(texts, errors="coerce"))
            if doubt is not None:
                report.update(finding="UNIT_MISMATCH", sentence=doubt.because, doubt=doubt)
        return UnitReport(**report)
    families, ambiguous_l = _family(known, currencies)
    report["families"] = tuple(families)
    if len(families) != 1 or ambiguous_l or set(units) - set(known):
        reason = (
            "'L' could mean litres or lakh, and nothing else in the column says which"
            if ambiguous_l
            else (
                f"the units belong to {len(families)} kinds of measure"
                if len(families) != 1
                else "some unit words are not ones the rules know"
            )
        )
        report.update(
            finding="UNIT_MISMATCH",
            sentence=f"'{column}' mixes units: {reason}. Say what the column holds; nothing is converted.",
        )
        return UnitReport(**report)
    family = families[0]
    target = max(known, key=lambda u: (known[u], u)) if family != "indian_scale" else ""
    factors = _factors(family, target or next(iter(known)), known)
    in_words = "plain numbers" if family == "indian_scale" else f"'{target.upper()}'"
    if bare == 0:
        step = _proposal(
            column, series, factors, "fail", max_failure_pct, f"Write every value of '{column}' in {in_words}"
        )
        report.update(
            step=step,
            sentence=f"{sum(known.values()):,} of {non_empty:,} values of '{column}' carry a unit ({', '.join(known)}).",
        )
        return UnitReport(**report)
    options: list[UnitOption] = []
    for unit in sorted(known, key=lambda u: (-known[u], u)):
        unit_factors = _factors(family, unit, known) if family != "indian_scale" else {**factors}
        if family == "indian_scale":
            # Plain numbers in lakh: every factor is relative to a lakh, and a plain number is kept as it is.
            unit_factors = {u: UNIT_FAMILIES[family][u] / UNIT_FAMILIES[family][unit] for u in known}
        step = _proposal(
            column, series, unit_factors, "as_target", max_failure_pct,
            f"Write every value of '{column}' in '{unit.upper()}', reading plain numbers as '{unit.upper()}'",
        )  # fmt: skip
        if step is not None:
            options.append(UnitOption(option_id=f"unit-{len(options) + 1}", label=unit.upper(), step=step))
    options.append(UnitOption(option_id="dont-know", label="I don't know", step=None))
    report.update(
        finding="UNIT_MISMATCH",
        sentence=f"{bare:,} of {non_empty:,} values of '{column}' are plain numbers beside values with a unit.",
        question=UnitQuestion(
            text=f"Which unit are the plain numbers in '{column}' written in?", options=tuple(options)
        ),
    )
    return UnitReport(**report)
