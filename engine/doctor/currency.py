"""Currency (Plan O M242, the design §8.2 and [REQ] 7): money keeps its unit, foreign money is asked, mixed money stops.

"We configured it for ₹ but the client is from the US and we get $ in the file." Before M242, `parse_number` stripped
the sign and every report assumed rupees; a column holding ₹ and $ became one column of numbers. Now:

1. **Every money column keeps its unit.** :func:`check_currency` reads each column's currency signs and codes
   (`engine.agent.formats.currency_codes`) and its header words (`amount_usd`, `price (INR)`), counts only. The unit
   goes into the recipe as `parse_number`'s `unit` (`engine.agent.currency_steps`) and into the run's
   :class:`MoneyBasis`, which reports read instead of assuming ₹.
2. **A workspace currency setting** replaces the hard-coded rupee: `data_doctor.workspace_currency` (INR by default),
   or the client's confirmed locale profile (M241) - :func:`workspace_currency_for`.
3. **A different currency from the workspace's is a question** (`CURRENCY_FOREIGN`, ASK): "This file's `amount` is in
   US dollars. Report in US dollars, or convert to Indian rupees at a rate you give?" Reporting in dollars is
   :func:`report_in`; converting is :func:`propose_conversion` then :func:`approve_conversion`.
4. **Conversion needs an approved, dated rate from a person.** The proposal freezes `{from, to, rate, rate_date,
   source}`; the source is the person's own words ("RBI reference rate, 10 Oct 2026"). The approval is APPROVE-class
   (Analyst): the person sends back the exact sentence they read, and their id and the time are recorded
   (:class:`ApprovedRate`). **Nothing here fetches a rate**: this module has no network code at all.
5. **Mixed currencies in one column stop** (`CURRENCY_MIXED`, STOP) with one question: which column says which rows
   are which (a `currency` or `country` column), or do the signs say it for every row. The answer becomes a
   conversion of the rows in the other currency (:func:`answer_mixed`, then a rate per currency); amounts in
   different currencies are never added together (:func:`add_amounts` refuses, and so does the recipe).
6. **No unit is invented** (`CURRENCY_UNSTATED`, ASK): a money column with no sign, code or header word is asked
   once ("Is `amount` in Indian rupees?"); a unit the client confirmed (the locale profile's `money_units`) answers it.

Reports read a :class:`MoneyBasis` stored beside the run (`runs/<run_id>/money_basis.json`, written by the Data Doctor
flow only). :func:`report_money`
turns it into the lines a report shows under "Currency": one per column naming its unit and, when converted, the rate,
its date, its source and who approved it. A report's own amounts keep the currency they were worked out in - the value
view's are the person's value inputs (`RoiInputs.currency`, rupees) times measured counts - so a column of the file in
another currency is noted apart (:meth:`ReportMoney.lines_beside`), never used to relabel them. :func:`format_money`
(which for INR is `engine.pilot.roi.format_inr` exactly) writes an amount that really is in a currency.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Final, Literal

import pandas as pd
from pydantic import AwareDatetime, Field

from engine.agent.contracts import DecidedBy, RecipeStep, RecipeStepKind
from engine.agent.currency_steps import (
    CURRENCY_MIXED,
    MAX_RATE,
    MAX_ROW_VALUES,
    MAX_SOURCE_CHARS,
    is_currency_code,
)
from engine.agent.formats import MIN_CONVERT_SHARE, currency_codes, parse_numbers
from engine.config import StrictBase
from engine.doctor.indian_values import money_header
from engine.pilot.roi import format_inr
from engine.storage import Storage, StorageError
from engine.utils.time import utc_now

if TYPE_CHECKING:
    from engine.doctor.config import DataDoctorConfig
    from engine.doctor.locale import LocaleProfile

__all__ = [
    "APPROVAL_SENTENCE_CHANGED",
    "CURRENCY_FOREIGN",
    "CURRENCY_MIXED",
    "CURRENCY_RATE_INVALID",
    "CURRENCY_RATE_UNAPPROVED",
    "CURRENCY_UNSTATED",
    "MONEY_BASIS_FILENAME",
    "Amount",
    "ApprovedRate",
    "ColumnCurrency",
    "ConversionProposal",
    "CurrencyCheck",
    "CurrencyError",
    "CurrencyFinding",
    "CurrencyOption",
    "CurrencyQuestion",
    "MixedAnswer",
    "MoneyBasis",
    "MoneyColumn",
    "ReportMoney",
    "WorkspaceCurrency",
    "add_amounts",
    "answer_mixed",
    "approve_conversion",
    "check_currency",
    "currency_name",
    "format_money",
    "load_money_basis",
    "money_basis",
    "money_basis_for_campaign",
    "propose_conversion",
    "read_currencies",
    "report_in",
    "report_money",
    "save_money_basis",
    "suggest_spellings",
    "workspace_currency_for",
]

CURRENCY_FOREIGN: Final[str] = "CURRENCY_FOREIGN"
CURRENCY_UNSTATED: Final[str] = "CURRENCY_UNSTATED"
CURRENCY_RATE_INVALID: Final[str] = "CURRENCY_RATE_INVALID"
CURRENCY_RATE_UNAPPROVED: Final[str] = "CURRENCY_RATE_UNAPPROVED"
APPROVAL_SENTENCE_CHANGED: Final[str] = "APPROVAL_SENTENCE_CHANGED"
"""The design §6.6 exact-text rule, made general by M239; M242 raises it for a rate approval."""

MONEY_BASIS_FILENAME: Final[str] = "money_basis.json"
"""`runs/<run_id>/money_basis.json`: the unit of each money column of the run, and the rates used."""

MAX_DISTINCT: Final[int] = 20_000
"""Distinct values read per column: enough to see every currency of a million-row file, bounded."""
MAX_SELECTOR_VALUES: Final[int] = 20
"""A column that says which rows are which holds a handful of spellings (`INR`, `USD`, `IN`, `US`)."""

CURRENCY_NAMES: Final[dict[str, tuple[str, str]]] = {
    "INR": ("Indian rupees", "Indian rupee"),
    "USD": ("US dollars", "US dollar"),
    "EUR": ("euros", "euro"),
    "GBP": ("British pounds", "British pound"),
    "AED": ("UAE dirhams", "UAE dirham"),
    "SGD": ("Singapore dollars", "Singapore dollar"),
    "AUD": ("Australian dollars", "Australian dollar"),
    "CAD": ("Canadian dollars", "Canadian dollar"),
}
"""Plain names for the currencies a client of ours is likely to send; any other code is shown as its code."""

_SIGNS: Final[dict[str, str]] = {"USD": "$", "EUR": "€", "GBP": "£"}
"""The sign a report prints before an amount; INR is `format_inr`'s own; any other currency prints its code."""

_HEADER_UNITS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (re.compile(r"(?i)(?:^|[^a-z])(inr|rupees?|rs)(?:$|[^a-z])"), "INR"),
    (re.compile(r"(?i)(?:^|[^a-z])(usd|dollars?)(?:$|[^a-z])"), "USD"),
    (re.compile(r"(?i)(?:^|[^a-z])(eur|euros?)(?:$|[^a-z])"), "EUR"),
    (re.compile(r"(?i)(?:^|[^a-z])(gbp|pounds? sterling)(?:$|[^a-z])"), "GBP"),
)
"""A header word that names the column's currency: `amount_usd`, `Price (INR)`, `spend in dollars`."""
_SELECTOR_HEADER: Final[re.Pattern[str]] = re.compile(r"(?i)(currency|curr\b|ccy|country|market|region)")
"""A column that may say which rows are in which currency."""
_COUNTRY_CODES: Final[dict[str, str]] = {
    "in": "INR",
    "ind": "INR",
    "india": "INR",
    "us": "USD",
    "usa": "USD",
    "united states": "USD",
    "uk": "GBP",
    "gb": "GBP",
    "united kingdom": "GBP",
}
"""A country written in a selector column, and the currency it most likely means (the person confirms)."""

Severity = Literal["stop", "ask"]
FindingCode = Literal["CURRENCY_FOREIGN", "CURRENCY_MIXED", "CURRENCY_UNSTATED"]


class CurrencyError(Exception):
    """A coded refusal: a rate that is not valid, a changed sentence, an unapproved rate, amounts in two currencies."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def currency_name(code: str, *, one: bool = False) -> str:
    """`US dollars` (or `US dollar` with `one`); an unlisted code is shown as itself."""
    names = CURRENCY_NAMES.get(code)
    if names is None:
        return code
    return names[1] if one else names[0]


# ---------------------------------------------------------------------------
# The workspace currency
# ---------------------------------------------------------------------------
class WorkspaceCurrency(StrictBase):
    """The currency reports use for a client, and where that came from."""

    code: str
    source: Literal["client", "workspace", "default"] = Field(
        description="`client`: the client's confirmed locale profile; `workspace`: `data_doctor.workspace_currency`."
    )


def workspace_currency_for(
    config: DataDoctorConfig | None, profile: LocaleProfile | None = None
) -> WorkspaceCurrency:
    """The client's confirmed currency, else the workspace setting, else INR. A detected (unconfirmed) one never wins."""
    if profile is not None and profile.workspace_currency.source == "confirmed":
        code = str(profile.workspace_currency.value)
        if is_currency_code(code):
            return WorkspaceCurrency(code=code, source="client")
    if config is not None:
        return WorkspaceCurrency(
            code=config.workspace_currency,
            source="workspace" if config.workspace_currency != "INR" else "default",
        )
    return WorkspaceCurrency(code="INR", source="default")


# ---------------------------------------------------------------------------
# Reading a file's money columns (counts only)
# ---------------------------------------------------------------------------
class ColumnCurrency(StrictBase):
    """What one money column says about its currency: rows per currency, and the rows that name none."""

    column: str
    rows: dict[str, int] = Field(
        default_factory=dict, description="Currency code -> rows whose value names it."
    )
    several: int = Field(default=0, description="Rows whose one value names two currencies (`$12 (₹1,000)`).")
    unmarked: int = Field(default=0, description="Amounts that name no currency.")
    non_empty: int = 0
    header: str | None = Field(default=None, description="The currency the column's name says, if any.")

    @property
    def codes(self) -> frozenset[str]:
        """Every currency the column names, in its values or its header."""
        return frozenset(self.rows) | (frozenset({self.header}) if self.header else frozenset())


def _header_unit(name: str) -> str | None:
    found = {code for pattern, code in _HEADER_UNITS if pattern.search(name)}
    return next(iter(found)) if len(found) == 1 else None


def _counts(series: pd.Series[Any]) -> tuple[dict[str, int], int, int, int, list[str]]:
    """(rows per currency, rows naming several, unmarked amounts, non-empty rows, distinct texts) of a column."""
    present = series.dropna()
    rows: dict[str, int] = {}
    several = unmarked = non_empty = 0
    texts: list[str] = []
    table: dict[str, frozenset[str]] = {}
    for value in present.tolist():
        if isinstance(value, str):
            if not value.strip():
                continue
            non_empty += 1
            if value not in table:
                table[value] = currency_codes(value)
                if len(texts) < MAX_DISTINCT:
                    texts.append(value)
            codes = table[value]
            if len(codes) > 1:
                several += 1
            elif codes:
                code = next(iter(codes))
                rows[code] = rows.get(code, 0) + 1
            else:
                unmarked += 1
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            if isinstance(value, float) and math.isnan(value):
                continue
            non_empty += 1
            unmarked += 1
    return rows, several, unmarked, non_empty, texts


def _is_money(column: str, series: pd.Series[Any], rows: Mapping[str, int], texts: Sequence[str]) -> bool:
    """A column of amounts: numbers (or text that reads as numbers), with a currency sign or a money header."""
    if pd.api.types.is_bool_dtype(series):
        return False
    numeric = pd.api.types.is_numeric_dtype(series)
    if not numeric:
        if not texts:
            return False
        sample = pd.Series(list(texts), dtype=object)
        if parse_numbers(sample, indian_scale_words=True).changed < MIN_CONVERT_SHARE * len(sample):
            return False
    return bool(rows) or _header_unit(column) is not None or money_header(column)


def read_currencies(
    frame: pd.DataFrame,
    *,
    columns: Sequence[str] | None = None,
    primary_key: str | None = None,
    target: str | None = None,
) -> tuple[ColumnCurrency, ...]:
    """Each money column's currencies, counted. The ID and the outcome column are never read (Plan L §5.2)."""
    skip = {name for name in (primary_key, target) if name}
    names = [str(c) for c in frame.columns] if columns is None else [str(c) for c in columns]
    found: list[ColumnCurrency] = []
    for name in names:
        if name in skip or name not in frame.columns:
            continue
        series = frame[name]
        rows, several, unmarked, non_empty, texts = _counts(series)
        if not non_empty or not _is_money(name, series, rows, texts):
            continue
        found.append(
            ColumnCurrency(
                column=name,
                rows=dict(sorted(rows.items())),
                several=several,
                unmarked=unmarked,
                non_empty=non_empty,
                header=_header_unit(name),
            )
        )
    return tuple(found)


# ---------------------------------------------------------------------------
# Findings and their one question
# ---------------------------------------------------------------------------
class CurrencyOption(StrictBase):
    """One answer a person may give; never "add them up"."""

    key: str
    label: str


class CurrencyQuestion(StrictBase):
    text: str
    options: tuple[CurrencyOption, ...]
    blocking: bool = Field(
        description="True for a STOP: nothing is built from the column until it is answered."
    )


class CurrencyFinding(StrictBase):
    code: FindingCode
    column: str
    severity: Severity
    currencies: tuple[str, ...] = Field(description="The currencies the column names, sorted.")
    rows: dict[str, int] = Field(default_factory=dict)
    several: int = 0
    unmarked: int = 0
    candidates: tuple[str, ...] = Field(
        default=(), description="For CURRENCY_MIXED: columns that may say which rows are in which currency."
    )
    question: CurrencyQuestion


class CurrencyCheck(StrictBase):
    """`check_currency`'s answer: the columns whose unit is known, and one finding per column that needs a person."""

    workspace_currency: str
    units: dict[str, str] = Field(
        default_factory=dict, description="Column -> its one currency, for the columns that need no question."
    )
    findings: tuple[CurrencyFinding, ...] = ()
    columns: tuple[ColumnCurrency, ...] = ()

    @property
    def stopped(self) -> bool:
        """True while any column holds more than one currency: nothing is built until it is answered."""
        return any(f.severity == "stop" for f in self.findings)


def _plural(count: int, one: str, many: str | None = None) -> str:
    return f"{count:,} {one if count == 1 else (many or one + 's')}"


def _candidates(frame: pd.DataFrame, column: str, skip: set[str]) -> tuple[str, ...]:
    """Columns that may say each row's currency: a currency or country header, or values that all name a currency."""
    found: list[str] = []
    for name in (str(c) for c in frame.columns):
        if name == column or name in skip:
            continue
        series = frame[name].dropna()
        if series.empty:
            continue
        distinct = list(dict.fromkeys(str(v).strip() for v in series.head(100_000).tolist()))
        if len(distinct) > MAX_SELECTOR_VALUES or any(ch.isdigit() for v in distinct for ch in v):
            continue  # an amount column names currencies too; a selector names them without amounts
        named = all(
            currency_codes(v) or v.casefold() in _COUNTRY_CODES or v.upper() in CURRENCY_NAMES
            for v in distinct
        )
        if _SELECTOR_HEADER.search(name) or named:
            found.append(name)
    return tuple(found)


def _foreign(column: ColumnCurrency, code: str, workspace: str) -> CurrencyFinding:
    count = sum(column.rows.values()) + column.unmarked
    return CurrencyFinding(
        code="CURRENCY_FOREIGN",
        column=column.column,
        severity="ask",
        currencies=(code,),
        rows=column.rows,
        unmarked=column.unmarked,
        question=CurrencyQuestion(
            text=(
                f"This file's '{column.column}' is in {currency_name(code)} ({_plural(count, 'amount')}), and this "
                f"workspace reports in {currency_name(workspace)}. Report in {currency_name(code)}, or convert to "
                f"{currency_name(workspace)} at a rate you give?"
            ),
            options=(
                CurrencyOption(key=f"report_in:{code}", label=f"Report in {currency_name(code)}"),
                CurrencyOption(
                    key=f"convert:{code}:{workspace}",
                    label=f"Convert to {currency_name(workspace)} at a rate I give, with its date and source",
                ),
            ),
            blocking=False,
        ),
    )


def _mixed(column: ColumnCurrency, candidates: tuple[str, ...], codes: set[str]) -> CurrencyFinding:
    parts = [
        _plural(n, f"row in {currency_name(c)}", f"rows in {currency_name(c)}")
        for c, n in column.rows.items()
    ]
    if column.header and column.header not in column.rows:
        parts.append(f"a name that says {currency_name(column.header)}")
    for confirmed in sorted(codes - column.codes - {"mixed"}):
        parts.append(f"{currency_name(confirmed)} as you confirmed for this client")
    if column.unmarked:
        parts.append(_plural(column.unmarked, "row with no currency", "rows with no currency"))
    if column.several:
        parts.append(_plural(column.several, "value naming two currencies", "values naming two currencies"))
    options = [
        CurrencyOption(key=f"rows_by_column:{name}", label=f"'{name}' says which rows are in which currency")
        for name in candidates
    ]
    if not column.unmarked and not column.several and not column.header:
        options.append(CurrencyOption(key="rows_by_sign", label="The sign on each amount says its currency"))
    options.append(CurrencyOption(key="new_file", label="I will send a file with one currency per column"))
    return CurrencyFinding(
        code="CURRENCY_MIXED",
        column=column.column,
        severity="stop",
        currencies=tuple(sorted(codes - {"mixed"})),
        rows=column.rows,
        several=column.several,
        unmarked=column.unmarked,
        candidates=candidates,
        question=CurrencyQuestion(
            text=(
                f"'{column.column}' holds more than one currency ({'; '.join(parts)}). Amounts in different "
                "currencies are never added together, so nothing is built from this column until you say which "
                "rows are in which currency."
            ),
            options=tuple(options),
            blocking=True,
        ),
    )


def _unstated(column: ColumnCurrency, workspace: str) -> CurrencyFinding:
    return CurrencyFinding(
        code="CURRENCY_UNSTATED",
        column=column.column,
        severity="ask",
        currencies=(),
        unmarked=column.unmarked,
        question=CurrencyQuestion(
            text=(
                f"'{column.column}' looks like money but names no currency. Is it in {currency_name(workspace)}? "
                "We ask once for this client and remember the answer."
            ),
            options=(
                CurrencyOption(key=f"unit:{workspace}", label=f"Yes, {currency_name(workspace)}"),
                CurrencyOption(key="unit:other", label="Another currency (say which)"),
            ),
            blocking=False,
        ),
    )


def check_currency(
    frame: pd.DataFrame,
    *,
    workspace_currency: str = "INR",
    confirmed_units: Mapping[str, str] | None = None,
    columns: Sequence[str] | None = None,
    primary_key: str | None = None,
    target: str | None = None,
) -> CurrencyCheck:
    """Each money column's unit, or the one question it needs (#64's detector half; rules only, counts only).

    `confirmed_units` are the client's confirmed money units (the locale profile's `money_units` when `confirmed`):
    a column they name needs no `CURRENCY_UNSTATED` question, and values that contradict them are mixed.
    """
    if not is_currency_code(workspace_currency):
        raise ValueError(f"{workspace_currency!r} is not a currency code")
    confirmed = dict(confirmed_units or {})
    skip = {name for name in (primary_key, target) if name}
    found = read_currencies(frame, columns=columns, primary_key=primary_key, target=target)
    units: dict[str, str] = {}
    findings: list[CurrencyFinding] = []
    for column in found:
        codes = set(column.codes)
        held = confirmed.get(column.column)
        if held is not None and held != "mixed" and is_currency_code(held) and (codes - {held}):
            codes.add(held)  # the values contradict what the client confirmed: a question, never an overwrite
        if len(codes) > 1 or column.several or held == "mixed":
            findings.append(_mixed(column, _candidates(frame, column.column, skip), codes))
            continue
        code = next(iter(codes)) if codes else held if held and is_currency_code(held) else None
        if code is None:
            findings.append(_unstated(column, workspace_currency))
        elif code == workspace_currency:
            units[column.column] = code
        else:
            findings.append(_foreign(column, code, workspace_currency))
    return CurrencyCheck(
        workspace_currency=workspace_currency,
        units=units,
        findings=tuple(findings),
        columns=found,
    )


# ---------------------------------------------------------------------------
# Answers: report in the file's currency, or convert at an approved rate
# ---------------------------------------------------------------------------
def _parse_step(column: str, unit: str, order: int, params: Mapping[str, Any] | None) -> RecipeStep:
    base = {
        k: v
        for k, v in dict(params or {}).items()
        if k in {"decimal", "percent_to_fraction", "indian_scale_words"}
    }
    return RecipeStep(
        order=order,
        kind=RecipeStepKind.PARSE_NUMBER,
        column=column,
        params={**base, "unit": unit},
        reason=f"Read '{column}' as amounts in {currency_name(unit)}; a value in another currency stops the run.",
    )


def report_in(
    finding: CurrencyFinding,
    code: str,
    *,
    order: int = 1,
    text_column: bool = True,
    parse_params: Mapping[str, Any] | None = None,
) -> tuple[RecipeStep, ...]:
    """The answer "report in US dollars": a `parse_number` step recording the unit (none for a numeric column, whose
    unit the run's :class:`MoneyBasis` records). Nothing is converted."""
    if finding.code == "CURRENCY_MIXED":
        raise CurrencyError(
            CURRENCY_MIXED,
            f"'{finding.column}' holds more than one currency; it cannot be reported in one without converting.",
        )
    if not is_currency_code(code):
        raise ValueError(f"{code!r} is not a currency code")
    return (_parse_step(finding.column, code, order, parse_params),) if text_column else ()


class ApprovedRate(StrictBase):
    """A rate a person gave and approved, frozen: what every report that used it shows."""

    column: str
    from_currency: str
    to_currency: str
    rate: float = Field(gt=0, le=MAX_RATE, description="One `from_currency` is this many `to_currency`.")
    rate_date: date
    source: str = Field(min_length=1, max_length=MAX_SOURCE_CHARS)
    rows: str | dict[str, Any] = "all"
    sentence: str = Field(description="The exact sentence the person approved.")
    approved_by: str = Field(min_length=1)
    approved_at: AwareDatetime

    def step_params(self) -> dict[str, Any]:
        """The frozen parameters of the `convert_currency` step this rate is."""
        return _step_params(
            self.from_currency, self.to_currency, self.rate, self.rate_date, self.source, self.rows
        )


def _step_params(
    source_code: str, target: str, rate: float, rate_date: date, source: str, rows: str | dict[str, Any]
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "from": source_code,
        "to": target,
        "rate": rate,
        "rate_date": rate_date.isoformat(),
        "source": source,
    }
    if rows != "all":
        params["rows"] = rows
    return params


def _rate_text(rate: float) -> str:
    return f"{rate:,.6f}".rstrip("0").rstrip(".") if rate < 1 else f"{rate:,.4f}".rstrip("0").rstrip(".")


def _day(value: date) -> str:
    return f"{value.day} {value:%b %Y}"


def _rows_words(rows: str | Mapping[str, Any], source_code: str) -> str:
    if rows == "marked":
        return f"the amounts marked in {currency_name(source_code)}"
    if isinstance(rows, Mapping):
        values = ", ".join(str(v) for v in rows["values"])
        return f"the rows whose '{rows['column']}' is {values}"
    return "every amount"


class ConversionProposal(StrictBase):
    """A conversion waiting for an Analyst: the rate as the person gave it and the exact sentence to approve."""

    column: str
    from_currency: str
    to_currency: str
    rate: float
    rate_date: date
    source: str
    rows: str | dict[str, Any] = "all"
    sentence: str
    steps: tuple[RecipeStep, ...] = Field(
        description="Pending: the conversion, then the reading in the new unit."
    )


def propose_conversion(
    column: str,
    *,
    source_code: str,
    target: str,
    rate: float,
    rate_date: date | str,
    source: str,
    rows: str | Mapping[str, Any] = "all",
    order: int = 1,
    text_column: bool = True,
    parse_params: Mapping[str, Any] | None = None,
    today: date | None = None,
) -> ConversionProposal:
    """A pending `convert_currency` at the person's rate, with the sentence an Analyst approves word for word.

    Refused (`CURRENCY_RATE_INVALID`): a code that is not a currency, the same currency twice, a rate that is not a
    positive number, a date that is not a date or is in the future, an empty source. The rate is never looked up.
    """
    try:
        when = rate_date if isinstance(rate_date, date) else date.fromisoformat(str(rate_date))
    except ValueError as exc:
        raise CurrencyError(
            CURRENCY_RATE_INVALID, "The rate's date is not a date (write it YYYY-MM-DD)."
        ) from exc
    problems: list[str] = []
    if not (is_currency_code(source_code) and is_currency_code(target)):
        problems.append("name both currencies by their code (USD, INR)")
    elif source_code == target:
        problems.append("the two currencies are the same")
    if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not math.isfinite(rate):
        problems.append("the rate is a number")
    elif not 0 < rate <= MAX_RATE:
        problems.append(f"the rate is above 0 and at most {MAX_RATE:,.0f}")
    if when > (today or utc_now().date()):
        problems.append("the rate's date is in the future")
    if not source.strip() or len(source) > MAX_SOURCE_CHARS:
        problems.append(f"say where the rate comes from, in at most {MAX_SOURCE_CHARS} characters")
    frozen_rows: str | dict[str, Any] = dict(rows) if isinstance(rows, Mapping) else rows
    if isinstance(frozen_rows, dict):
        values = frozen_rows.get("values")
        if not (
            isinstance(frozen_rows.get("column"), str)
            and isinstance(values, list)
            and 0 < len(values) <= MAX_ROW_VALUES
        ):
            problems.append(f"name the column and 1 to {MAX_ROW_VALUES} of its values")
    elif frozen_rows not in {"all", "marked"}:
        problems.append("rows is all, marked, or a column with its values")
    if problems:
        raise CurrencyError(CURRENCY_RATE_INVALID, "The rate cannot be used: " + "; ".join(problems) + ".")
    rate = float(rate)
    source = source.strip()
    sentence = (
        f"In '{column}', convert {_rows_words(frozen_rows, source_code)} from {currency_name(source_code)} to "
        f"{currency_name(target)} at 1 {currency_name(source_code, one=True)} = {_rate_text(rate)} "
        f"{currency_name(target)}, the rate of {_day(when)}, from: {source}. Every converted amount changes, and "
        "every report that uses them shows this rate."
    )
    convert = RecipeStep(
        order=order,
        kind=RecipeStepKind.CONVERT_CURRENCY,
        column=column,
        params={
            **_step_params(source_code, target, rate, when, source, frozen_rows),
            **{
                k: v
                for k, v in dict(parse_params or {}).items()
                if k in {"decimal", "indian_scale_words"} and text_column
            },
        },
        reason=sentence,
        decided_by=DecidedBy.USER,
    )
    steps: tuple[RecipeStep, ...] = (convert,)
    if text_column:
        steps = (convert, _parse_step(column, target, order + 1, parse_params))
    return ConversionProposal(
        column=column,
        from_currency=source_code,
        to_currency=target,
        rate=rate,
        rate_date=when,
        source=source,
        rows=frozen_rows,
        sentence=sentence,
        steps=steps,
    )


def approve_conversion(
    proposal: ConversionProposal,
    *,
    sentence: str,
    approved_by: str,
    clock: Callable[[], datetime] = utc_now,
) -> ApprovedRate:
    """The Analyst's approval (APPROVE-class): recorded only when `sentence` is the proposal's, word for word.

    A different sentence records nothing (`APPROVAL_SENTENCE_CHANGED`): the person approves what they read.
    """
    if not approved_by.strip():
        raise CurrencyError(CURRENCY_RATE_UNAPPROVED, "A rate is approved by a named person.")
    if sentence != proposal.sentence:
        raise CurrencyError(
            APPROVAL_SENTENCE_CHANGED,
            "The sentence approved is not the one shown, so nothing was recorded. Read the new sentence and approve it.",
        )
    return ApprovedRate(
        column=proposal.column,
        from_currency=proposal.from_currency,
        to_currency=proposal.to_currency,
        rate=proposal.rate,
        rate_date=proposal.rate_date,
        source=proposal.source,
        rows=proposal.rows,
        sentence=proposal.sentence,
        approved_by=approved_by.strip(),
        approved_at=clock(),
    )


class MixedAnswer(StrictBase):
    """The answer to `CURRENCY_MIXED`: how to tell each row's currency, and the rate each foreign currency needs."""

    column: str
    rows: dict[str, str | dict[str, Any]] = Field(
        description="Currency -> the `rows` rule that picks its rows (`marked`, or a column with its spellings)."
    )
    to_convert: tuple[str, ...] = Field(
        description="The currencies a rate is needed for, to the workspace's."
    )
    keep: str = Field(description="The currency the column ends in: the workspace's.")


def answer_mixed(
    finding: CurrencyFinding,
    *,
    workspace_currency: str,
    by_sign: bool = False,
    rows_column: str | None = None,
    spellings: Mapping[str, Sequence[str]] | None = None,
) -> MixedAnswer:
    """Turn "which rows are which" into one `rows` rule per currency; each foreign one then needs an approved rate.

    `by_sign`: every amount carries its own sign (offered only then). `rows_column` with `spellings`
    (`{"USD": ["US", "USD"], "INR": ["IN"]}`): that column says each row's currency. The column ends in the
    workspace currency; nothing is added up before every other currency is converted at a person's rate.
    """
    if finding.code != "CURRENCY_MIXED":
        raise ValueError("answer_mixed answers a CURRENCY_MIXED finding")
    if by_sign == (rows_column is not None):
        raise CurrencyError(
            CURRENCY_MIXED, "Say either that the signs tell the rows apart, or which column does."
        )
    if by_sign:
        if finding.unmarked or finding.several:
            raise CurrencyError(
                CURRENCY_MIXED,
                f"{_plural(finding.unmarked + finding.several, 'amount')} in '{finding.column}' carry no single "
                "sign, so the signs cannot tell every row apart. Name the column that does.",
            )
        rules: dict[str, str | dict[str, Any]] = dict.fromkeys(finding.currencies, "marked")
    else:
        if rows_column not in finding.candidates:
            raise CurrencyError(
                CURRENCY_MIXED,
                f"'{rows_column}' is not a column that names a currency or a country for each row.",
            )
        given = {code: [s.strip() for s in values if s.strip()] for code, values in (spellings or {}).items()}
        missing = [c for c in finding.currencies if not given.get(c)]
        if missing or any(not is_currency_code(c) for c in given):
            raise CurrencyError(
                CURRENCY_MIXED,
                "Say which values of the column mean each currency: "
                + ", ".join(currency_name(c) for c in (missing or sorted(given)))
                + ".",
            )
        seen: dict[str, str] = {}
        for code, values in given.items():
            for value in values:
                if seen.setdefault(value.casefold(), code) != code:
                    raise CurrencyError(CURRENCY_MIXED, f"'{value}' cannot mean two currencies.")
        rules = {code: {"column": rows_column, "values": values} for code, values in sorted(given.items())}
    foreign = tuple(sorted(code for code in rules if code != workspace_currency))
    return MixedAnswer(column=finding.column, rows=rules, to_convert=foreign, keep=workspace_currency)


def suggest_spellings(frame: pd.DataFrame, rows_column: str) -> dict[str, list[str]]:
    """The spellings of a selector column grouped by the currency they most likely mean, for the person to confirm."""
    grouped: dict[str, list[str]] = {}
    for value in dict.fromkeys(str(v).strip() for v in frame[rows_column].dropna().tolist()):
        codes = currency_codes(value)
        code: str | None
        if len(codes) == 1:
            code = next(iter(codes))
        elif value.upper() in CURRENCY_NAMES:
            code = value.upper()
        else:
            code = _COUNTRY_CODES.get(value.casefold())
        if code is not None:
            grouped.setdefault(code, []).append(value)
    return grouped


# ---------------------------------------------------------------------------
# The money basis of a run, and what reports print from it
# ---------------------------------------------------------------------------
class MoneyColumn(StrictBase):
    column: str
    written_in: tuple[str, ...] = Field(description="The currencies the file wrote it in.")
    reported_in: str = Field(description="The one currency its amounts are in after the recipe.")
    rates: tuple[ApprovedRate, ...] = ()


class MoneyBasis(StrictBase):
    """The unit of every money column of a run, and every rate used: what reports read instead of assuming ₹."""

    schema_version: Literal[1] = 1
    workspace_currency: str
    columns: tuple[MoneyColumn, ...] = ()

    @property
    def report_currency(self) -> str:
        """The currency the run's amounts are in: one, when every money column agrees; else the workspace's."""
        reported = {c.reported_in for c in self.columns}
        return next(iter(reported)) if len(reported) == 1 else self.workspace_currency

    @property
    def basis_hash(self) -> str:
        """A hash of the units and rates (not who approved them): what the prescription (M243) records."""
        body = [
            {
                "column": c.column,
                "written_in": list(c.written_in),
                "reported_in": c.reported_in,
                "rates": [r.step_params() for r in c.rates],
            }
            for c in sorted(self.columns, key=lambda c: c.column)
        ]
        blob = json.dumps(
            {"workspace": self.workspace_currency, "columns": body}, sort_keys=True, separators=(",", ":")
        )
        return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def notes(self) -> tuple[str, ...]:
        """One plain line per money column (its unit), and one per rate (rate, date, source, who approved it)."""
        lines: list[str] = []
        for column in self.columns:
            if not column.rates:
                lines.append(
                    f"'{column.column}' is in {currency_name(column.reported_in)}, as the file gives it; "
                    "nothing was converted."
                )
            for rate in column.rates:
                lines.append(
                    f"'{column.column}': {_rows_words(rate.rows, rate.from_currency)} converted from "
                    f"{currency_name(rate.from_currency)} to {currency_name(rate.to_currency)} at 1 "
                    f"{currency_name(rate.from_currency, one=True)} = {_rate_text(rate.rate)} "
                    f"{currency_name(rate.to_currency)}, the rate of {_day(rate.rate_date)} "
                    f"(source: {rate.source}), approved by {rate.approved_by} on {_day(rate.approved_at.date())}."
                )
        reported = sorted({c.reported_in for c in self.columns})
        if len(reported) > 1:
            lines.append(
                "Amounts in "
                + " and ".join(currency_name(c) for c in reported)
                + " are shown apart and never added together."
            )
        return tuple(lines)


def _unit_of(step: RecipeStep) -> str | None:
    unit = dict(step.params).get("unit")
    return str(unit) if isinstance(unit, str) else None


def money_basis(
    steps: Sequence[RecipeStep],
    *,
    workspace_currency: str,
    rates: Sequence[ApprovedRate] = (),
    units: Mapping[str, str] | None = None,
) -> MoneyBasis:
    """The basis a recipe gives its run: each money column's unit from its steps (and `units` for numeric columns
    no step reads), each conversion matched to its approved rate. A conversion with no approval is refused
    (`CURRENCY_RATE_UNAPPROVED`): a report never shows a rate nobody approved."""
    written: dict[str, list[str]] = {}
    reported: dict[str, str] = {}
    used: dict[str, list[ApprovedRate]] = {}
    for column, unit in dict(units or {}).items():
        if not is_currency_code(unit):
            raise CurrencyError(
                CURRENCY_MIXED, f"'{column}' holds more than one currency; it has no one unit."
            )
        written.setdefault(column, []).append(unit)
        reported[column] = unit
    for step in steps:
        if step.kind is RecipeStepKind.CONVERT_CURRENCY:
            params = dict(step.params)
            match = next(
                (
                    rate
                    for rate in rates
                    if rate.column == step.column and rate.step_params() == _frozen(params)
                ),
                None,
            )
            if match is None:
                raise CurrencyError(
                    CURRENCY_RATE_UNAPPROVED,
                    f"The conversion of '{step.column}' from {params.get('from')} has no approved rate; it is "
                    "approved by an Analyst before any report uses it.",
                )
            written.setdefault(step.column, []).append(match.from_currency)
            reported[step.column] = match.to_currency
            used.setdefault(step.column, []).append(match)
        elif step.kind is RecipeStepKind.PARSE_NUMBER and _unit_of(step) is not None:
            unit = str(_unit_of(step))
            # The rows a conversion did not take (`marked`, or picked by a column) were written in this unit.
            if all(rate.rows == "all" for rate in used.get(step.column, [])) and step.column in used:
                pass
            elif unit not in written.setdefault(step.column, []):
                written[step.column].append(unit)
            reported[step.column] = unit
    return MoneyBasis(
        workspace_currency=workspace_currency,
        columns=tuple(
            MoneyColumn(
                column=column,
                written_in=tuple(sorted(set(written.get(column, [reported[column]])))),
                reported_in=reported[column],
                rates=tuple(used.get(column, ())),
            )
            for column in sorted(reported)
        ),
    )


def _frozen(params: Mapping[str, Any]) -> dict[str, Any]:
    """A step's rate parameters as `ApprovedRate.step_params` writes them (reading options left out)."""
    keep = {"from", "to", "rate", "rate_date", "source", "rows"}
    frozen = {k: v for k, v in params.items() if k in keep}
    if frozen.get("rows") == "all":
        frozen.pop("rows")
    if "rate" in frozen:
        frozen["rate"] = float(frozen["rate"])
    return frozen


def _basis_key(run_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_\-]{0,127}", run_id):
        raise ValueError(f"{run_id!r} is not a run id")
    return f"runs/{run_id}/{MONEY_BASIS_FILENAME}"


def save_money_basis(storage: Storage, run_id: str, basis: MoneyBasis) -> None:
    """Store the basis beside the run. Only the Data Doctor flow calls this."""
    storage.write_model(_basis_key(run_id), basis)


def load_money_basis(storage: Storage, run_id: str) -> MoneyBasis | None:
    """The run's basis, or None (no Data Doctor basis: the report prints as it always has)."""
    try:
        key = _basis_key(run_id)
        if not storage.exists(key):
            return None
        return storage.read_model(key, MoneyBasis)
    except (StorageError, ValueError):
        return None


def money_basis_for_campaign(storage: Storage, campaign_id: str) -> MoneyBasis | None:
    """The basis of a campaign's first run (the run the pack's value inputs belong to), or None."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_\-]{0,127}", campaign_id):
        return None
    try:
        key = f"campaigns/{campaign_id}/campaign.json"
        if not storage.exists(key):
            return None
        record = json.loads(storage.read_text(key))
    except (StorageError, ValueError):
        return None
    run_ids = record.get("run_ids") if isinstance(record, dict) else None
    if not isinstance(run_ids, list) or not run_ids or not isinstance(run_ids[0], str):
        return None
    try:
        return load_money_basis(storage, run_ids[0])
    except ValueError:  # pragma: no cover - load_money_basis already answers None
        return None


def _western(digits: str) -> str:
    groups: list[str] = []
    while len(digits) > 3:
        groups.insert(0, digits[-3:])
        digits = digits[:-3]
    return ",".join([digits, *groups]) if digits else ",".join(groups)


def format_money(amount: float, currency: str = "INR", *, decimals: int = 0) -> str:
    """An amount in its currency: INR is `format_inr` exactly (₹12,34,567 with lakh and crore); another currency
    has western grouping and its sign or code (`$3,578`, `€12.50`, `AED 1,200`)."""
    if currency == "INR":
        return format_inr(amount, decimals=decimals)
    if decimals < 0:
        raise ValueError(f"decimals must be 0 or more, got {decimals}.")
    negative = amount < 0
    if decimals:
        whole_text, fraction_text = f"{abs(amount):.{decimals}f}".split(".")
        body = f"{_western(whole_text)}.{fraction_text}"
    else:
        body = _western(str(round(abs(amount))))
    sign = _SIGNS.get(currency)
    text = f"{sign}{body}" if sign else f"{currency} {body}"
    return f"-{text}" if negative else text


@dataclass(frozen=True)
class ReportMoney:
    """What a report needs to print money: the currency, its name, and the unit and rate lines it must show."""

    currency: str
    notes: tuple[str, ...] = ()
    currencies: tuple[str, ...] = ()
    """Every currency the run's money columns are in after the recipe (one, or several shown apart)."""

    def lines_beside(self, inputs_currency: str) -> tuple[str, ...]:
        """The lines a report shows under "Currency" when the amounts it writes are in `inputs_currency` (the
        person's value inputs, `RoiInputs.currency`): the unit and rate lines, and - when a money column of the file
        is in another currency - one line saying the two are shown apart. The report's amounts keep the inputs'
        currency: a column of the file never relabels them, and they are never converted without an approved rate.
        """
        others = [c for c in (self.currencies or (self.currency,)) if c != inputs_currency]
        if not others:
            return self.notes
        return (
            *self.notes,
            f"The value amounts in this report are worked out from your inputs, which are in "
            f"{_money_words(inputs_currency)}; the file's amounts are in "
            + " and ".join(_money_words(c) for c in others)
            + ". They are shown apart: nothing here is converted or added together across currencies.",
        )

    @property
    def name(self) -> str:
        """`US dollars`; `rupees` for INR, the word every rupee report already uses."""
        return "rupees" if self.currency == "INR" else currency_name(self.currency)

    @property
    def one(self) -> str:
        """`US dollar` (as in "net value per US dollar spent"); `rupee` for INR."""
        return "rupee" if self.currency == "INR" else currency_name(self.currency, one=True)

    def format(self, amount: float, *, decimals: int = 0) -> str:
        return format_money(amount, self.currency, decimals=decimals)


def report_money(basis: MoneyBasis | None) -> ReportMoney | None:
    """What a report prints from a run's basis; None (print exactly as before) when there is no basis, or when
    every amount is in rupees as the file gave them - an INR workspace's reports stay byte-identical. A rate used,
    a foreign currency, or columns in two currencies always print."""
    if basis is None:
        return None
    plain_rupees = basis.report_currency == "INR" and all(
        c.reported_in == "INR" and not c.rates for c in basis.columns
    )
    if plain_rupees:
        return None
    return ReportMoney(
        currency=basis.report_currency,
        notes=basis.notes(),
        currencies=tuple(sorted({c.reported_in for c in basis.columns})),
    )


def _money_words(code: str) -> str:
    """`rupees` for INR, as every rupee report already says; else the currency's plain name (`US dollars`)."""
    return "rupees" if code == "INR" else currency_name(code)


# ---------------------------------------------------------------------------
# Never add two currencies
# ---------------------------------------------------------------------------
class Amount(StrictBase):
    value: float
    currency: str


def add_amounts(amounts: Sequence[Amount]) -> Amount:
    """The sum of amounts in one currency. Amounts in two currencies are refused (`CURRENCY_MIXED`), never added."""
    if not amounts:
        raise ValueError("nothing to add")
    currencies = sorted({a.currency for a in amounts})
    if len(currencies) > 1:
        raise CurrencyError(
            CURRENCY_MIXED,
            "Amounts in " + " and ".join(currency_name(c) for c in currencies) + " are never added together.",
        )
    return Amount(value=math.fsum(a.value for a in amounts), currency=currencies[0])
