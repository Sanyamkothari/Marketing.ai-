"""Values that cannot be true, by what the column means and what type it holds (Plan O M238, DEC-1808 (c)).

`impossible_values` is a READ tool with rules only. It reads a column's *meaning* from its name (the words of the
header, English and the common Hinglish spellings) and its *type* from its values, and reports what that meaning
rules out:

| Meaning (header words) | Type | Impossible | Proposed fix |
|---|---|---|---|
| an age (`age`, `umar`, `umr`) | numbers | below 0 or above 120 | `valid_range` 0–120 |
| a length of time (`tenure`, `vintage`, `duration`) | numbers | below 0 | `valid_range` minimum 0 |
| money (`amount`, `spend`, `price`, `revenue`, `bill`, …) | numbers | below 0 | `valid_range` minimum 0 |
| a count (`count`, `qty`, `orders`, `visits`, …) | numbers | below 0 | `valid_range` minimum 0 |
| a percentage (`pct`, `percent`) | numbers | below 0 or above 100 | `valid_range` 0–100 |
| any date | dates | year 2099 or later (a "no end" placeholder); before 1900 | `set_missing_text` with those spellings, when the column is still text |
| a date that has happened (not `expiry`, `renewal`, `due`, `next`, `end`, …) | dates | after the reference date | none: a doubt, the person decides |
| a birth date (`dob`, `birth`, `born`) | dates | more than 120 years before the reference date | `set_missing_text`, when still text |

Every proposal is `pending`, `check` and never ticked (`valid_range` is in `advisor.NEVER_TICKED_STEPS`; a value
is impossible only by a rule, and the rule may not fit this client). The ID and the outcome are never checked:
no step may change them. The reference date for "in the future" is an input (`as_of`), so the same file and
date always give the same answer. Examples are masked whole before they are cut (`formats.masked_cut`).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Final, Literal

import pandas as pd
from pydantic import Field

from engine.agent.contracts import ProposalSource, RecipeStepKind
from engine.agent.formats import MAX_EXAMPLES, date_order, masked_cut, order_unsettled, parse_dates
from engine.agent.value_steps import MAX_MISSING_WORDS
from engine.config import StrictBase
from engine.doctor.row_contracts import Doubt, StepProposal

__all__ = [
    "IMPOSSIBLE_CODES",
    "ImpossibleFinding",
    "ImpossibleReport",
    "column_words",
    "find_impossible_values",
]

IMPOSSIBLE_CODES: Final[frozenset[str]] = frozenset({"IMPOSSIBLE_VALUE"})

Meaning = Literal["age", "tenure", "amount", "count", "percent", "date", "birth_date"]
Rule = Literal[
    "below_minimum", "above_maximum", "future_date", "placeholder_date", "before_1900", "age_over_120"
]


@dataclass(frozen=True)
class _NumberMeaning:
    meaning: Meaning
    words: frozenset[str]
    minimum: float | None
    maximum: float | None
    label: str


_NUMBER_MEANINGS: Final[tuple[_NumberMeaning, ...]] = (
    _NumberMeaning("age", frozenset({"age", "umar", "umr", "umra", "aayu"}), 0.0, 120.0, "an age"),
    _NumberMeaning("percent", frozenset({"pct", "percent", "percentage", "pc"}), 0.0, 100.0, "a percentage"),
    _NumberMeaning("tenure", frozenset({"tenure", "vintage", "duration"}), 0.0, None, "a length of time"),
    _NumberMeaning(
        "amount",
        frozenset(
            {
                "amount", "amt", "spend", "spent", "spending", "price", "revenue", "sales", "cost", "fee", "fees",
                "bill", "billed", "paid", "payment", "income", "salary", "mrp", "kharcha", "rakam", "kimat",
            }
        ),
        0.0,
        None,
        "an amount of money",
    ),
    _NumberMeaning(
        "count",
        frozenset(
            {
                "count", "cnt", "qty", "quantity", "orders", "visits", "logins", "sessions", "transactions",
                "txns", "purchases", "calls", "clicks", "opens", "items",
            }
        ),
        0.0,
        None,
        "a count",
    ),
)  # fmt: skip
"""Checked in this order; a header that names two meanings (`age_pct`) takes the first."""

_FUTURE_ALLOWED: Final[frozenset[str]] = frozenset(
    {
        "expiry", "expires", "expire", "expiring", "renewal", "renew", "renews", "due", "next", "end", "ends",
        "till", "until", "valid", "validity", "scheduled", "planned", "maturity", "target", "forecast",
        "deadline", "upcoming", "promised", "eta",
    }
)  # fmt: skip
"""A header with one of these words may hold a date after the reference date."""

_BIRTH: Final[frozenset[str]] = frozenset({"dob", "birth", "birthday", "born", "birthdate", "janam"})
_PLACEHOLDER_YEAR: Final[int] = 2099
_EARLIEST: Final[pd.Timestamp] = pd.Timestamp("1900-01-01")
_MAX_AGE_YEARS: Final[int] = 120
_MIN_SHARE: Final[float] = 0.5
"""A column is read as numbers (or dates) only when at least half its non-empty values are."""
_YEAR: Final[re.Pattern[str]] = re.compile(r"(?<!\d)(\d{4})(?!\d)")
_CAMEL: Final[re.Pattern[str]] = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SPLIT: Final[re.Pattern[str]] = re.compile(r"[^0-9A-Za-zऀ-ॿ]+")


def column_words(name: str) -> frozenset[str]:
    """The words of a header, lower-cased: `DateOfBirth` and `date_of_birth` both give date, of, birth."""
    spaced = _CAMEL.sub(" ", str(name))
    return frozenset(word.casefold() for word in _SPLIT.split(spaced) if word)


class ImpossibleFinding(StrictBase):
    """One rule broken in one column."""

    column: str = Field(description="The column.")
    meaning: Meaning = Field(description="What the header says the column holds.")
    rule: Rule = Field(description="Which rule the values break.")
    count: int = Field(ge=0, description="Values that break it.")
    non_empty: int = Field(ge=0, description="Non-empty values in the column.")
    share_pct: float = Field(ge=0.0, le=100.0, description="`count` as a percentage of `non_empty`.")
    sentence: str = Field(description="What was found, in plain words, from the counts.")
    examples: tuple[str, ...] = Field(default=(), description="Up to five masked examples.")
    step: StepProposal | None = Field(
        default=None, description="The pending fix, when a fixed step can do it."
    )
    doubt: Doubt | None = Field(
        default=None, description="When no step can fix it safely: what the person decides."
    )


class ImpossibleReport(StrictBase):
    """What `impossible_values` found in a file."""

    as_of: str = Field(description="The reference date for 'in the future' (ISO).")
    columns_checked: int = Field(ge=0, description="Columns whose meaning or type a rule covers.")
    findings: tuple[ImpossibleFinding, ...] = Field(description="Every broken rule, in column order.")


def _examples(values: Iterable[Any]) -> tuple[str, ...]:
    out: list[str] = []
    for value in dict.fromkeys(str(v) for v in values):
        out.append(masked_cut(value, 60))
        if len(out) == MAX_EXAMPLES:
            break
    return tuple(out)


def _pct(part: int, whole: int) -> float:
    return 0.0 if whole <= 0 else round(part / whole * 100.0, 4)


def _number_findings(name: str, series: pd.Series[Any], meaning: _NumberMeaning) -> list[ImpossibleFinding]:
    present = series.dropna()
    if present.empty or pd.api.types.is_bool_dtype(series) or pd.api.types.is_datetime64_any_dtype(series):
        return []
    numbers = pd.to_numeric(present, errors="coerce")
    if numbers.notna().sum() < _MIN_SHARE * len(present):
        return []
    found: list[ImpossibleFinding] = []
    none = pd.Series(False, index=numbers.index)
    below = numbers < meaning.minimum if meaning.minimum is not None else none
    above = numbers > meaning.maximum if meaning.maximum is not None else none
    bad = int(below.sum()) + int(above.sum())
    if not bad:
        return []
    params: dict[str, Any] = {"minimum": meaning.minimum, "maximum": meaning.maximum}
    params = {k: (int(v) if float(v).is_integer() else v) for k, v in params.items() if v is not None}
    limits = " and ".join(
        part
        for part in (
            f"below {params['minimum']}" if "minimum" in params else "",
            f"above {params['maximum']}" if "maximum" in params else "",
        )
        if part
    )
    step = StepProposal(
        kind=RecipeStepKind.VALID_RANGE,
        column=name,
        params=params,
        source=ProposalSource.RULES,
        title=f"Empty the values of '{name}' {limits}",
        reason=f"'{name}' holds {meaning.label}, which cannot be {limits}; {bad:,} of {len(present):,} values are.",
        changed=bad,
        out_of=len(present),
        share_pct=_pct(bad, len(present)),
    )
    sides: tuple[tuple[Rule, pd.Series[Any]], ...] = (("below_minimum", below), ("above_maximum", above))
    for rule, mask in sides:
        count = int(mask.sum())
        if not count:
            continue
        side = f"below {params['minimum']}" if rule == "below_minimum" else f"above {params['maximum']}"
        found.append(
            ImpossibleFinding(
                column=name,
                meaning=meaning.meaning,
                rule=rule,
                count=count,
                non_empty=len(present),
                share_pct=_pct(count, len(present)),
                sentence=f"{count:,} of {len(present):,} values of '{name}' are {side}, which {meaning.label} cannot be.",
                examples=_examples(present[mask.to_numpy()]),
                step=step,
            )
        )
    return found


def _as_dates(series: pd.Series[Any]) -> tuple[pd.Series[Any], pd.Series[Any]] | None:
    """(dates, the text they were read from) for a date column or a text column of mostly dates, else None.

    Text is read once per distinct spelling and mapped back to the rows, so a million rows of a few thousand
    spellings cost a few thousand reads, and no column is skipped for having many spellings.
    """
    present = series.dropna()
    if present.empty or pd.api.types.is_bool_dtype(series):
        return None
    if pd.api.types.is_datetime64_any_dtype(series):
        values = present
        if getattr(values.dt, "tz", None) is not None:
            values = values.dt.tz_convert("UTC").dt.tz_localize(None)
        return values, present.astype(str)
    if pd.api.types.is_numeric_dtype(series):
        return None
    text = present.astype(str).str.strip()
    codes, spellings = pd.factorize(text)
    distinct = pd.Series(spellings, dtype=object)
    if not distinct.str.contains(r"\d{2}", regex=True).all():
        return None
    order = date_order(distinct)
    if order_unsettled(distinct):
        return None  # 03/04/2026 is March or April, or the column holds both orders: never read month first by default
    parsed_once = parse_dates(distinct, dayfirst=bool(order)).values
    parsed = pd.Series(parsed_once.to_numpy()[codes], index=text.index)
    if parsed.notna().sum() < _MIN_SHARE * len(text):
        return None
    return parsed, text


def _latest_years(text: pd.Series[Any]) -> pd.Series[Any]:
    """Each value's largest four-digit year as written (0 when none), read once per distinct spelling."""
    codes, spellings = pd.factorize(text)
    years = [max((int(y) for y in _YEAR.findall(str(s))), default=0) for s in spellings]
    return pd.Series(pd.array(years, dtype="int64").to_numpy()[codes], index=text.index)


def _date_findings(
    name: str, series: pd.Series[Any], words: frozenset[str], as_of: pd.Timestamp
) -> tuple[list[ImpossibleFinding], bool]:
    read = _as_dates(series)
    if read is None:
        return [], False
    dates, text = read
    present = int(series.notna().sum())
    is_text = not pd.api.types.is_datetime64_any_dtype(series)
    birth = bool(words & _BIRTH)
    meaning: Meaning = "birth_date" if birth else "date"
    # A year of 2099 or later is a placeholder; 9999 is beyond what a date can hold, so the text is read too.
    years = _latest_years(text)
    placeholder = (dates.dt.year >= _PLACEHOLDER_YEAR).fillna(False) | (years >= _PLACEHOLDER_YEAR)
    rules: list[tuple[Rule, pd.Series[Any], str]] = [
        (
            "placeholder_date",
            placeholder,
            f"in {_PLACEHOLDER_YEAR} or later, a common stand-in for 'no date'",
        ),
        ("before_1900", dates < _EARLIEST, "before 1900"),
    ]
    if birth:
        oldest = as_of - pd.DateOffset(years=_MAX_AGE_YEARS)
        rules.append(
            ("age_over_120", (dates < oldest) & (dates >= _EARLIEST), f"more than {_MAX_AGE_YEARS} years ago")
        )
    if birth or not (words & _FUTURE_ALLOWED):
        # The whole reference day has happened: a time later that day is not in the future.
        tomorrow = as_of.normalize() + pd.Timedelta(days=1)
        rules.append(("future_date", (dates >= tomorrow) & ~placeholder, f"after {as_of.date().isoformat()}"))
    found: list[ImpossibleFinding] = []
    for rule, mask, side in rules:
        mask = mask.fillna(False).astype(bool)
        count = int(mask.sum())
        if not count:
            continue
        spellings = list(dict.fromkeys(text[mask].tolist()))
        step: StepProposal | None = None
        doubt: Doubt | None = None
        sentence = f"{count:,} of {present:,} dates in '{name}' are {side}."
        if rule != "future_date" and is_text and len(spellings) <= MAX_MISSING_WORDS:
            step = StepProposal(
                kind=RecipeStepKind.SET_MISSING_TEXT,
                column=name,
                params={"values": spellings},
                source=ProposalSource.RULES,
                title=f"Treat {len(spellings)} spelling(s) in '{name}' as empty",
                reason=f"{count:,} of {present:,} dates in '{name}' are {side}, which cannot be a real date here.",
                changed=count,
                out_of=present,
                share_pct=_pct(count, present),
            )
        else:
            doubt = Doubt(
                code="IMPOSSIBLE_VALUE",
                column=name,
                because=(
                    f"{count:,} of {present:,} dates in '{name}' are {side}, which has not happened yet"
                    if rule == "future_date"
                    else f"{count:,} of {present:,} dates in '{name}' are {side}."
                ),
                would_do="leave these values as written and flag the column; you can leave such rows out yourself",
                if_not=f"if they are mistakes, {count:,} rows keep a date that cannot be right",
            )
        found.append(
            ImpossibleFinding(
                column=name,
                meaning=meaning,
                rule=rule,
                count=count,
                non_empty=present,
                share_pct=_pct(count, present),
                sentence=sentence,
                examples=_examples(spellings),
                step=step,
                doubt=doubt,
            )
        )
    return found, True


def find_impossible_values(
    frame: pd.DataFrame,
    *,
    as_of: date,
    primary_key: str | None,
    target: str | None,
    columns: Sequence[str] | None = None,
) -> ImpossibleReport:
    """Every impossible value in `frame` (or in `columns`), by each column's meaning and type."""
    reference = pd.Timestamp(as_of)
    protected = {name for name in (primary_key, target) if name}
    names = [str(c) for c in frame.columns] if columns is None else [str(c) for c in columns]
    findings: list[ImpossibleFinding] = []
    checked = 0
    for name in names:
        if name in protected or name not in frame.columns:
            continue
        series = frame[name]
        words = column_words(name)
        meaning = next((m for m in _NUMBER_MEANINGS if words & m.words), None)
        if meaning is not None and not (words & (_BIRTH | {"date", "dt"})):
            numbers = _number_findings(name, series, meaning)
            checked += 1
            findings += numbers
            continue
        dated, was_date = _date_findings(name, series, words, reference)
        checked += int(was_date)
        findings += dated
    return ImpossibleReport(
        as_of=reference.date().isoformat(), columns_checked=checked, findings=tuple(findings)
    )
