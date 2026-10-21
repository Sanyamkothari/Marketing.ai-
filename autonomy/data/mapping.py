"""How a flat business table becomes our `Event`s.

A client's export is one row per thing that happened (an order line, a send, a signup), with its own
column names, date format and words for event types. `EventMapping` says which column is which, once,
in data (it loads from YAML or JSON as easily as from Python), and `map_frame` applies it.

Four rules shape `map_frame`:

- **Bad rows are collected, not fatal.** A real export always has a few rows with no customer, a date
  nobody can read or an amount of "N/A". Each becomes a `MappingProblem` that names the row, the column
  and what was wrong, and the rest of the file still loads. Only a mapping that cannot apply to the
  table at all (a named column is missing) raises `MappingError`, because then every row would fail
  for the same reason.
- **Nothing ambiguous is guessed.** A date such as 01/12/2010 (1 December or 12 January?), an amount
  such as 2.550 (2.55 or 2,550?), a file holding two currencies, an id such as `0` or `unknown`, a
  number saved as 1.23457E+11: each is either settled by evidence elsewhere in the same file, by an
  explicit option on the mapping, or left out as a problem that says which option settles it. Never
  silently read one way.
- **Asking first.** `map_frame(..., ask=True)` (what the Data Doctor's route uses) looks at the whole
  table before mapping and, when something can only be settled by the person who knows the file,
  raises `MappingQuestionsError` with plain questions, each naming the mapping option that answers it:
  the time zone of naive times, the day/month order when the data contradict the format, whether
  exact repeated lines are real, whether leading zeros matter, which currency, which separators.
  `inspect_frame` returns the same questions without raising. Without `ask` (programmatic callers
  that wrote their mapping for a known feed), two old defaults stay: naive times are read in
  `timezone` (UTC unless set) and exact repeated lines are kept, each as its own event.
- **Event ids are stable.** An event's id is `stable_id("evt", source, <key>)`, so loading the same
  file twice yields the same ids and the append-only event store drops the repeats. The key is the
  `row_key` columns when given. Without them it is the event's **content** (customer, type, time,
  amount, item, category, channel, decision) plus an occurrence counter for identical lines, never the
  row's position: a re-export that lists new orders first neither drops them nor counts old ones
  twice.

Pure and deterministic: no clock reads, no randomness, no I/O.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from typing import Any, Final, Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd
from pydantic import AwareDatetime, Field, ValidationError, field_validator, model_validator

from autonomy.contracts import Channel, Contract, Event, EventType, stable_id
from autonomy.contracts.events import AttrValue

__all__ = [
    "DEFAULT_ID_PLACEHOLDERS",
    "EventMapping",
    "FrameInspection",
    "MappingError",
    "MappingProblem",
    "MappingQuestion",
    "MappingQuestionsError",
    "ProblemCode",
    "QuestionCode",
    "customer_key",
    "inspect_frame",
    "map_frame",
    "problem_counts",
]

EVENT_ID_PREFIX = "evt"

DEFAULT_ID_PLACEHOLDERS: Final[tuple[str, ...]] = (
    "0",
    "-1",
    "-",
    "--",
    "?",
    "n/a",
    "na",
    "#n/a",
    "none",
    "null",
    "nil",
    "nan",
    "undefined",
    "unknown",
    "guest",
    "anonymous",
    "anon",
    "no id",
    "noid",
    "missing",
    "not available",
    "test",
    "total",
    "totals",
    "grand total",
    "subtotal",
    "sum",
    "all",
)
"""Ids that name nobody: a guest checkout, an unknown buyer, a totals line. Compared case-insensitively.
A row with one is left out as `placeholder_customer`, never merged into one giant "customer"."""

PLACEHOLDER_DAYS: Final[frozenset[tuple[int, int, int]]] = frozenset(
    {(1900, 1, 1), (1970, 1, 1), (1899, 12, 30), (1899, 12, 31), (1, 1, 1)}
)
"""Dates systems write when they have none (at midnight): Excel's and Unix's zero, 0001-01-01."""

HEAVY_ID_SHARE: Final[float] = 0.2
"""`ask` mode: one id on at least this share of rows, and five times the next biggest, is questioned."""
HEAVY_ID_RATIO: Final[float] = 5.0
HEAVY_ID_MIN_ROWS: Final[int] = 50
DATE_DENSITY_CONFIDENCE: Final[float] = 0.75
"""`ask` mode: the other day/month reading must be this much likelier (by date density) to ask."""
INSPECT_SAMPLE: Final[int] = 50_000
"""Distinct cells read per column when inspecting; enough to see a convention, cheap on big files. A
column with more is sampled evenly over the whole file, not just its first rows. The sample only sets
the evidence (and the counts a question quotes); each row is still checked on its own."""

_INTEGRAL_TEXT = re.compile(r"^-?\d+\.0+$")
"""An integer key that went through a float column on its way here, such as "17850.0"."""

_GROUPED = re.compile(r"^-?\d{1,3}(,\d{3})+(\.\d+)?$")
"""A number with comma thousands groups, such as "1,234" or "-12,345.60"."""

_SCIENTIFIC_ID = re.compile(r"^[+-]?\d(\.\d+)?[eE][+-]\d{1,3}$")
"""An id a spreadsheet saved as a float in scientific notation (1.23457E+11): its digits are gone. The
exponent's sign is required, as spreadsheets and pandas write it, so a hex id such as 12345e6789 is
not mistaken for one."""

_EMAIL_ID = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_SEPARATOR_AMBIGUOUS = re.compile(r"^[+-]?[1-9]\d{0,2}[.,]\d{3}$")
"""One separator then exactly three digits: 2.550 is 2.55 in one convention and 2,550 in the other."""

_DAY_MONTH = re.compile(r"%d|%m")


class ProblemCode:
    """The reasons a row can be left out. Plain strings, so a summary can count them."""

    MISSING_CUSTOMER = "missing_customer"
    PLACEHOLDER_CUSTOMER = "placeholder_customer"
    """The id is a placeholder (`0`, `unknown`, `guest`, `TOTAL`...), not a person."""
    BAD_CUSTOMER_ID = "bad_customer_id"
    """The id cannot be trusted as written (scientific notation; two spellings of one number)."""
    BAD_TIMESTAMP = "bad_timestamp"
    AMBIGUOUS_DATE = "ambiguous_date"
    """Both day/month orders give a valid date and the file does not settle which it uses."""
    UNKNOWN_EVENT_TYPE = "unknown_event_type"
    BAD_NUMBER = "bad_number"
    AMBIGUOUS_NUMBER = "ambiguous_number"
    """2.550 or 1,234: one separator and three digits, and the file does not settle the convention."""
    MIXED_CURRENCY = "mixed_currency"
    """The row's amount is in another currency than the business's, and no rate was given."""
    BAD_CHANNEL = "bad_channel"
    DUPLICATE_ROW = "duplicate_row"
    INVALID_EVENT = "invalid_event"
    EXCLUDED = "excluded"
    """Used by loaders for rows they leave out on purpose (a guest checkout, a postage adjustment)."""


class QuestionCode:
    """What `inspect_frame` can ask. Plain strings."""

    TIMEZONE = "timezone"
    DATE_ORDER = "date_order"
    DUPLICATES = "duplicates"
    LEADING_ZEROS = "id_leading_zeros"
    HEAVY_ID = "heavy_id"
    SEPARATORS = "separators"
    CURRENCY = "currency"


class MappingError(ValueError):
    """The mapping cannot apply to this table at all, for example because a column it names is missing."""


class MappingProblem(Contract):
    """One row that did not become an event, and why. Never carries the row's values (they may be personal)."""

    row: str = Field(description="The row key: the `row_key` values, or the row's index label")
    code: str = Field(description="One of `ProblemCode`")
    column: str | None = Field(default=None, description="The column at fault, when one is")
    message: str


class MappingQuestion(Contract):
    """Something only the person who knows the file can settle, in plain words. Never carries an id."""

    code: str = Field(description="One of `QuestionCode`")
    option: str = Field(description="The `EventMapping` field whose value answers it")
    column: str | None = None
    question: str
    choices: tuple[str, ...] = Field(description="Values `option` can take to answer it")
    rows: int = Field(description="How many rows the answer changes")
    confidence: float | None = Field(
        default=None, description="How sure the file alone makes the likeliest answer (0-1), when known"
    )


class FrameInspection(Contract):
    """What a look at the whole table found before mapping: questions to ask, and what it settled."""

    questions: tuple[MappingQuestion, ...] = ()
    inferred: tuple[str, ...] = Field(
        default=(), description="Plain sentences: what the file itself settled, and from what evidence"
    )


class MappingQuestionsError(MappingError):
    """`map_frame(..., ask=True)` found things it will not guess. Each question names its option."""

    def __init__(self, questions: Sequence[MappingQuestion]) -> None:
        self.questions = tuple(questions)
        lines = [f"{q.question} (set {q.option}: {' or '.join(q.choices)})" for q in self.questions]
        super().__init__("Before this file can be loaded: " + " ".join(lines))


Separator = Literal[",", ".", " ", "'", "none"]


class EventMapping(Contract):
    """Which column of a flat table is which part of an `Event`, and how its cells are written.

    Every field naming a column holds the column's name in the table. The event type is either one
    constant (`event_type`, e.g. every row of an orders export is a purchase) or read from a column
    (`event_type_column`) through `event_type_values`, which maps the table's own words ("order",
    "Cancelled") to our types; a value not in the map is tried as one of our type names, so a table
    that already says "purchase" needs no map.

    The options after the columns say how the file writes things. Each defaults to "settle it from
    the file, or ask": see the module docstring.
    """

    customer_id: str = Field(description="Column holding the customer's id")
    timestamp: str = Field(description="Column holding when it happened")
    timestamp_format: str | None = Field(
        default=None,
        description="A `strptime` format such as '%m/%d/%Y %H:%M'; None reads ISO 8601",
    )
    timezone: str = Field(
        default="UTC",
        description="IANA zone for timestamps that carry no offset of their own. `ask` mode asks "
        "when it is not given and the file has naive times",
    )
    date_order: Literal["day_first", "month_first"] | None = Field(
        default=None,
        description="Confirms the day/month order. With timestamp_format it must agree with it (and "
        "stops the check that the data agree); without one, common d/m/Y or m/d/Y shapes are read",
    )
    not_before: AwareDatetime | None = Field(
        default=None, description="Earliest plausible time; earlier rows are bad_timestamp"
    )
    not_after: AwareDatetime | None = Field(
        default=None,
        description="Latest plausible time (the export's moment); later rows are bad_timestamp",
    )

    event_type: EventType | None = Field(default=None, description="The same type for every row")
    event_type_column: str | None = None
    event_type_values: dict[str, EventType] = Field(
        default_factory=dict, description="The table's word for a type -> our type (case-insensitive)"
    )

    value_column: str | None = Field(default=None, description="Amount, gross, in the business currency")
    quantity_column: str | None = Field(
        default=None, description="With `unit_price_column`: value = quantity x unit price"
    )
    unit_price_column: str | None = None
    negative_as: EventType | None = Field(
        default=None,
        description="A row whose value is negative becomes this type with the absolute value "
        "(e.g. REFUND for a returned order line). Without it, a REFUND row's negative value is "
        "made positive, and a negative purchase is a bad_number problem",
    )
    margin_column: str | None = None
    category_column: str | None = None
    item_column: str | None = None
    decimal: Literal[".", ","] | None = Field(
        default=None,
        description="Decimal separator of numbers written as text; None settles it from the file "
        "(2,55 can only be a decimal comma) and leaves 2.550-like cells out when it cannot",
    )
    thousands: Separator | None = Field(
        default=None, description="Thousands separator; needs `decimal`. Default: the other of . and ,"
    )
    currency: str | None = Field(
        default=None,
        description="ISO 4217 code of the amounts, or of the business currency when `currency_rates` "
        "converts others into it",
    )
    currency_column: str | None = Field(
        default=None,
        description="Column holding each row's currency code. An attr named 'currency' is read the "
        "same way when this is not given",
    )
    currency_rates: dict[str, float] = Field(
        default_factory=dict,
        description="Currency code -> how many units of `currency` one unit is worth. Rows in a "
        "currency without a rate are left out (mixed_currency), never added as if the same money",
    )

    channel: Channel | None = Field(default=None, description="The same channel for every row")
    channel_column: str | None = Field(default=None, description="Values are channel names, any case")
    decision_id_column: str | None = None

    attrs: dict[str, str] = Field(
        default_factory=dict, description="Event attr name -> column; empty cells are left out"
    )
    fixed_attrs: dict[str, AttrValue] = Field(
        default_factory=dict, description="Attrs every event gets, e.g. which file it came from"
    )
    row_key: tuple[str, ...] = Field(
        default=(), description="Columns that identify a row; empty means the event's own content"
    )
    duplicates: Literal["keep", "drop"] | None = Field(
        default=None,
        description="Exact repeated lines (no row_key): 'keep' (real repeats, e.g. the same item "
        "scanned twice) or 'drop' (the file was exported or pasted twice). `ask` mode asks",
    )
    id_placeholders: tuple[str, ...] | None = Field(
        default=None,
        description="Ids that name nobody, any case; None uses DEFAULT_ID_PLACEHOLDERS, () none",
    )
    id_leading_zeros: Literal["keep", "strip"] | None = Field(
        default=None,
        description="'strip' reads 0012431 as 12431 (what a spreadsheet or pandas makes of it in "
        "another file); 'keep' keeps the zeros. None keeps them, and reports 007 next to 7",
    )
    allow_heavy_ids: tuple[str, ...] = Field(
        default=(), description="Ids confirmed as real although they hold a large share of rows"
    )

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown time zone {value!r}") from exc
        return value

    @field_validator("event_type_values")
    @classmethod
    def _fold_case(cls, value: dict[str, EventType]) -> dict[str, EventType]:
        return {k.strip().casefold(): v for k, v in value.items()}

    @field_validator("currency")
    @classmethod
    def _currency_code(cls, value: str | None) -> str | None:
        return None if value is None else _currency_code(value)

    @field_validator("currency_rates")
    @classmethod
    def _rates(cls, value: dict[str, float]) -> dict[str, float]:
        out: dict[str, float] = {}
        for code, rate in value.items():
            if not (math.isfinite(rate) and rate > 0):
                raise ValueError(f"the rate for {code!r} must be a positive number")
            out[_currency_code(code)] = float(rate)
        return out

    @model_validator(mode="after")
    def _consistent(self) -> EventMapping:
        if (self.event_type is None) == (self.event_type_column is None):
            raise ValueError("give exactly one of event_type (a constant) and event_type_column")
        if self.event_type_values and self.event_type_column is None:
            raise ValueError("event_type_values needs event_type_column")
        if (self.quantity_column is None) != (self.unit_price_column is None):
            raise ValueError("quantity_column and unit_price_column go together")
        if self.value_column is not None and self.quantity_column is not None:
            raise ValueError("give value_column or quantity_column + unit_price_column, not both")
        if self.channel is not None and self.channel_column is not None:
            raise ValueError("give channel (a constant) or channel_column, not both")
        if self.thousands is not None and self.decimal is None:
            raise ValueError("thousands needs decimal")
        if self.thousands is not None and self.thousands == self.decimal:
            raise ValueError("decimal and thousands must differ")
        if self.currency_rates and self.currency is None:
            raise ValueError("currency_rates needs currency (the currency they convert into)")
        if self.not_before is not None and self.not_after is not None and self.not_before >= self.not_after:
            raise ValueError("not_before must be earlier than not_after")
        if self.date_order is not None and self.timestamp_format is not None:
            order = _format_order(self.timestamp_format)
            if order is not None and order != self.date_order:
                raise ValueError(
                    f"timestamp_format reads {order.replace('_', ' ')} but date_order says "
                    f"{self.date_order.replace('_', ' ')}"
                )
        return self

    def currency_source(self) -> str | None:
        """The column holding each row's currency: `currency_column`, else the attr named 'currency'."""
        return self.currency_column or self.attrs.get("currency")

    def columns(self) -> tuple[str, ...]:
        """Every column this mapping reads, in a stable order, each once."""
        named = (
            self.customer_id,
            self.timestamp,
            self.event_type_column,
            self.value_column,
            self.quantity_column,
            self.unit_price_column,
            self.margin_column,
            self.category_column,
            self.item_column,
            self.channel_column,
            self.decision_id_column,
            self.currency_column,
            *self.attrs.values(),
            *self.row_key,
        )
        return tuple(dict.fromkeys(c for c in named if c is not None))


class _RowError(Exception):
    """Internal: one row failed; carries what the `MappingProblem` will say."""

    def __init__(self, code: str, message: str, column: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.column = column
        self.message = message


# ---------------------------------------------------------------------------
# What the whole table says, before any row is mapped
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _DateEvidence:
    applies: bool
    format_only: int
    """Rows whose text reads only in the format's order (e.g. 13/01 under %d/%m)."""
    swapped_only: int
    ambiguous: frozenset[str]
    """Texts both orders read, as different dates."""
    ambiguous_rows: int
    swapped_confidence: float
    """How much likelier the other order is, by how densely each reading packs the dates (0-1)."""


@dataclass(frozen=True)
class _NumberEvidence:
    decimal: Literal[".", ","]
    settled: bool
    """The convention is known (given, or one-sided evidence); else every 2.550-like cell is a problem."""
    settled_by: str
    ambiguous: frozenset[str]
    ambiguous_rows: int
    columns: tuple[str, ...]


@dataclass(frozen=True)
class _CurrencyEvidence:
    column: str | None
    counts: tuple[tuple[str, int], ...]
    """Each currency code and its rows, most first."""
    business: str | None
    """The currency amounts are stored in: the mapping's, else the file's only (or most common) one."""


@dataclass(frozen=True)
class _IdEvidence:
    collisions: frozenset[str]
    """Raw ids that are another raw id with leading zeros added or removed (007 and 7)."""
    leading_zero_rows: int
    heavy: tuple[str, int, int] | None
    """(id, its rows, the next biggest id's rows) when one id holds an implausible share."""


@dataclass(frozen=True)
class _Plan:
    dates: _DateEvidence
    numbers: _NumberEvidence
    currency: _CurrencyEvidence
    ids: _IdEvidence
    placeholders: frozenset[str]
    zero_is_placeholder: bool
    """An all-zero id (0, 000) names nobody: on with the default placeholders, off with the mapping's own."""
    exact_repeats: tuple[bool, ...]
    naive_times: bool


def inspect_frame(frame: pd.DataFrame, mapping: EventMapping) -> FrameInspection:
    """Look at the whole table and return what must be asked, and what the file itself settled.

    Raises `MappingError` when a column the mapping names is missing. Never reads the clock.
    """
    _require_columns(frame, mapping)
    plan = _plan(frame, mapping)
    return FrameInspection(questions=tuple(_questions(plan, mapping, len(frame))), inferred=_inferred(plan))


def map_frame(
    frame: pd.DataFrame, mapping: EventMapping, source: str, *, ask: bool = False
) -> tuple[list[Event], list[MappingProblem]]:
    """Turn every row of `frame` into an `Event`; return the events and the rows that could not be.

    Raises `MappingError` when a column the mapping names is not in the frame, and, with `ask`,
    `MappingQuestionsError` when `inspect_frame` has questions. Events come out in row order. A second row
    with the same `row_key` is reported as `duplicate_row` and left out, because it would get the same
    event id and the store would silently drop it anyway.
    """
    _require_columns(frame, mapping)
    # Finding exact repeats hashes every row; only asking and duplicates='drop' use them.
    plan = _plan(frame, mapping, repeats=ask or mapping.duplicates == "drop")
    if ask:
        questions = _questions(plan, mapping, len(frame))
        if questions:
            raise MappingQuestionsError(questions)
    zone = ZoneInfo(mapping.timezone)
    events: list[Event] = []
    problems: list[MappingProblem] = []
    seen: set[str] = set()
    occurrences: Counter[tuple[str, ...]] = Counter()
    records = frame.loc[:, list(mapping.columns())].to_dict("records")
    for position, (label, record) in enumerate(zip(frame.index, records, strict=True)):
        key = _row_key(label, record, mapping)
        try:
            if mapping.duplicates == "drop" and plan.exact_repeats[position]:
                raise _RowError(
                    ProblemCode.DUPLICATE_ROW, "an exact copy of an earlier row (duplicates='drop')"
                )
            fields = _fields(record, mapping, source, zone, plan)
            if mapping.row_key:
                event_id = stable_id(EVENT_ID_PREFIX, source, *key)
                if event_id in seen:
                    raise _RowError(ProblemCode.DUPLICATE_ROW, "another row has the same row key")
            else:
                content = _content(fields)
                occurrences[content] += 1
                event_id = stable_id(EVENT_ID_PREFIX, source, *content, occurrences[content])
            event = _build(event_id, fields)
        except _RowError as err:
            problems.append(
                MappingProblem(row=_describe(key), code=err.code, column=err.column, message=err.message)
            )
            continue
        seen.add(event_id)
        events.append(event)
    return events, problems


def problem_counts(problems: Iterable[MappingProblem]) -> dict[str, int]:
    """How many rows were left out for each reason, most common first."""
    return dict(Counter(p.code for p in problems).most_common())


def _require_columns(frame: pd.DataFrame, mapping: EventMapping) -> None:
    missing = [c for c in mapping.columns() if c not in frame.columns]
    if missing:
        raise MappingError(
            f"the table has no column {', '.join(repr(c) for c in missing)}; "
            f"it has {', '.join(repr(str(c)) for c in frame.columns)}"
        )


def _plan(frame: pd.DataFrame, mapping: EventMapping, *, repeats: bool = True) -> _Plan:
    placeholders = frozenset(
        p.strip().lower()
        for p in (DEFAULT_ID_PLACEHOLDERS if mapping.id_placeholders is None else mapping.id_placeholders)
    )
    exact: tuple[bool, ...]
    if mapping.row_key or len(frame) == 0 or not repeats:
        exact = (False,) * len(frame)
    else:
        mapped = frame.loc[:, list(mapping.columns())]
        try:
            flags = mapped.duplicated(keep="first")
        except TypeError:  # a nested dict or list in a cell
            flags = mapped.map(_hashable).duplicated(keep="first")
        exact = tuple(bool(x) for x in flags)
    return _Plan(
        dates=_date_evidence(frame[mapping.timestamp], mapping),
        numbers=_number_evidence(frame, mapping),
        currency=_currency_evidence(frame, mapping),
        ids=_id_evidence(frame[mapping.customer_id], mapping, placeholders),
        placeholders=placeholders,
        zero_is_placeholder=mapping.id_placeholders is None,
        exact_repeats=exact,
        naive_times=_has_naive_times(frame[mapping.timestamp], mapping),
    )


def _questions(plan: _Plan, mapping: EventMapping, rows: int) -> list[MappingQuestion]:
    asked: list[MappingQuestion] = []
    given = mapping.model_fields_set
    if plan.naive_times and "timezone" not in given:
        asked.append(
            MappingQuestion(
                code=QuestionCode.TIMEZONE,
                option="timezone",
                column=mapping.timestamp,
                question=(
                    f"The times in '{mapping.timestamp}' carry no time zone. In which zone were they "
                    "recorded? Reading them as UTC would move every event by the zone's offset."
                ),
                choices=("an IANA zone such as 'Europe/London'", "'UTC'"),
                rows=rows,
            )
        )
    d = plan.dates
    if d.applies and mapping.date_order is None:
        fmt = mapping.timestamp_format or ""
        reads = _format_order(fmt) or "month_first"
        other = "day_first" if reads == "month_first" else "month_first"
        soft = d.swapped_only == 0 and d.format_only == 0 and d.ambiguous_rows > 0
        contradicted = (d.swapped_only > 0 and d.format_only == 0) or (
            soft and d.swapped_confidence >= DATE_DENSITY_CONFIDENCE
        )
        mixed = d.swapped_only > 0 and d.format_only > 0
        unsettled = soft and 1 - DATE_DENSITY_CONFIDENCE < d.swapped_confidence < DATE_DENSITY_CONFIDENCE
        if contradicted or mixed or unsettled:
            if unsettled:
                why = (
                    "every date in the file reads both ways (no day over 12, too few days to see a "
                    "pattern), so nothing in it settles which"
                )
                confidence = round(1 - d.swapped_confidence, 3)
            elif mixed:
                why = (
                    f"{d.format_only:,} rows read only {_words(reads)} and {d.swapped_only:,} only "
                    f"{_words(other)}, so the file mixes both orders"
                )
                confidence = None
            elif d.swapped_only:
                why = (
                    f"{d.swapped_only:,} rows can only be read {_words(other)} (a 'month' over 12), "
                    f"and none only {_words(reads)}"
                )
                confidence = 1.0
            else:
                why = (
                    f"read {_words(reads)}, the dates scatter over the year; read {_words(other)} they "
                    "fall on consecutive days, as orders do"
                )
                confidence = round(d.swapped_confidence, 3)
            asked.append(
                MappingQuestion(
                    code=QuestionCode.DATE_ORDER,
                    option="date_order",
                    column=mapping.timestamp,
                    question=(
                        f"Is '{mapping.timestamp}' written day first (01/12 = 1 December) or month "
                        f"first (01/12 = 12 January)? The format {fmt!r} says {_words(reads)}, but "
                        f"{why}. {d.ambiguous_rows:,} rows would change date."
                    ),
                    choices=(f"'{other}' (and fix timestamp_format)", f"'{reads}'"),
                    rows=d.ambiguous_rows + d.swapped_only,
                    confidence=confidence,
                )
            )
    if not mapping.row_key and mapping.duplicates is None:
        repeats = sum(plan.exact_repeats)
        if repeats:
            asked.append(
                MappingQuestion(
                    code=QuestionCode.DUPLICATES,
                    option="duplicates",
                    question=(
                        f"{repeats:,} rows are exact copies of an earlier row in every mapped column. "
                        "Are they real repeated lines (the same item bought twice in one order) or "
                        "the same rows exported or pasted twice?"
                    ),
                    choices=("'keep' (real lines)", "'drop' (copies; or give row_key columns)"),
                    rows=repeats,
                )
            )
    ids = plan.ids
    if mapping.id_leading_zeros is None and ids.leading_zero_rows:
        clash = (
            f" {len(ids.collisions):,} ids appear both with and without the zeros." if ids.collisions else ""
        )
        asked.append(
            MappingQuestion(
                code=QuestionCode.LEADING_ZEROS,
                option="id_leading_zeros",
                column=mapping.customer_id,
                question=(
                    f"{ids.leading_zero_rows:,} rows have an id that starts with 0. Spreadsheets and "
                    "pandas drop those zeros, so the same customer can arrive as 12431 from another "
                    f"file and never match 0012431.{clash} Should the zeros be kept or dropped?"
                ),
                choices=("'strip' (match files that lost the zeros)", "'keep' (every file keeps them)"),
                rows=ids.leading_zero_rows,
            )
        )
    if ids.heavy is not None:
        _cid, n, nxt = ids.heavy
        asked.append(
            MappingQuestion(
                code=QuestionCode.HEAVY_ID,
                option="id_placeholders",
                column=mapping.customer_id,
                question=(
                    f"One id is on {n:,} of {rows:,} rows ({n / max(rows, 1):.0%}); the next biggest "
                    f"is on {nxt:,}. Is it a real customer, or a stand-in for guests or unknown "
                    "buyers that would merge many people into one?"
                ),
                choices=("add it to id_placeholders (leave its rows out)", "add it to allow_heavy_ids"),
                rows=n,
            )
        )
    nums = plan.numbers
    if nums.ambiguous_rows:
        asked.append(
            MappingQuestion(
                code=QuestionCode.SEPARATORS,
                option="decimal",
                column=", ".join(nums.columns),
                question=(
                    f"{nums.ambiguous_rows:,} amounts look like 2.550 or 1,234: one separator and three "
                    "digits, and nothing else in the file says which convention it uses. Is the "
                    "separator a decimal point (2.550 = 2.55) or a thousands separator (2.550 = 2550)?"
                ),
                choices=("'.' (decimal point; thousands ',')", "',' (decimal comma; thousands '.')"),
                rows=nums.ambiguous_rows,
            )
        )
    cur = plan.currency
    if cur.column is not None and cur.counts:
        foreign = [
            (code, n)
            for code, n in cur.counts
            if code != mapping.currency and code not in mapping.currency_rates
        ]
        # One currency in the file is the business's own unless the mapping names another one.
        if foreign and (len(cur.counts) > 1 or mapping.currency is not None):
            listed = ", ".join(f"{code} ({n:,} rows)" for code, n in cur.counts)
            found = (
                f"The amounts are in several currencies: {listed}. Adding them would mix different money."
                if len(cur.counts) > 1
                else f"Every amount is in {listed}, but the mapping says the business currency is "
                f"{mapping.currency}."
            )
            asked.append(
                MappingQuestion(
                    code=QuestionCode.CURRENCY,
                    option="currency",
                    column=cur.column,
                    question=(
                        f"{found} Which is the business currency, and at what rate should the "
                        "others convert into it? Rows in a currency without a rate are left out."
                    ),
                    choices=(
                        "currency='GBP' with currency_rates={'BRL': 0.16}",
                        "load each currency separately",
                    ),
                    rows=sum(n for code, n in cur.counts if code != cur.business),
                )
            )
    return asked


def _inferred(plan: _Plan) -> tuple[str, ...]:
    notes: list[str] = []
    if plan.numbers.settled_by:
        notes.append(plan.numbers.settled_by)
    d = plan.dates
    if d.applies and d.format_only and not d.swapped_only:
        notes.append(
            f"The day/month order matches the format: {d.format_only:,} rows can be read no other way."
        )
    if plan.currency.column is not None and len(plan.currency.counts) == 1:
        notes.append(f"Every amount is in {plan.currency.counts[0][0]}.")
    return tuple(notes)


def _words(order: str) -> str:
    return "day first" if order == "day_first" else "month first"


# --- dates ------------------------------------------------------------------------------------------
def _format_order(fmt: str) -> Literal["day_first", "month_first"] | None:
    found = _DAY_MONTH.findall(fmt)
    if "%d" in found and "%m" in found:
        return "day_first" if found.index("%d") < found.index("%m") else "month_first"
    return None


def _swap_day_month(fmt: str) -> str:
    return _DAY_MONTH.sub(lambda m: "%m" if m.group(0) == "%d" else "%d", fmt)


def _formats(mapping: EventMapping) -> tuple[str, ...]:
    """The strptime formats a timestamp may be in; empty means ISO 8601."""
    if mapping.timestamp_format is not None:
        return (mapping.timestamp_format,)
    if mapping.date_order is None:
        return ()
    date = "%d{s}%m{s}%Y" if mapping.date_order == "day_first" else "%m{s}%d{s}%Y"
    out: list[str] = []
    for sep in ("/", ".", "-"):
        for clock in ("", " %H:%M", " %H:%M:%S", "T%H:%M", "T%H:%M:%S"):
            out.append(date.format(s=sep) + clock)
    return tuple(out)


def _value_counts(series: pd.Series[Any]) -> Iterable[tuple[Any, int]]:
    """Distinct cells and their rows; cells that cannot be hashed (a nested dict) are counted as text."""
    try:
        return [(cell, int(n)) for cell, n in series.value_counts(dropna=True, sort=False).items()]
    except TypeError:
        return [
            (cell, int(n)) for cell, n in series.map(_hashable).value_counts(dropna=True, sort=False).items()
        ]


def _hashable(cell: object) -> object:
    return repr(cell) if isinstance(cell, dict | list | set) else cell


def _text_cells(series: pd.Series[Any]) -> Counter[str]:
    """Distinct non-empty text cells and their rows; at most `INSPECT_SAMPLE`, spread over the file."""
    if not (pd.api.types.is_object_dtype(series.dtype) or pd.api.types.is_string_dtype(series.dtype)):
        return Counter()  # a numeric or datetime column holds no text to read either way
    cells = [
        (cell.strip(), int(n)) for cell, n in _value_counts(series) if isinstance(cell, str) and cell.strip()
    ]
    if len(cells) > INSPECT_SAMPLE:
        # Distinct cells come in order of first appearance: an even spread sees the whole file (a
        # sorted export's later months too), not only its first rows. Deterministic.
        step = len(cells) / INSPECT_SAMPLE
        cells = [cells[int(i * step)] for i in range(INSPECT_SAMPLE)]
    counts: Counter[str] = Counter()
    for text, n in cells:
        counts[text] += n
    return counts


def _strptime(text: str, fmt: str) -> datetime | None:
    try:
        return datetime.strptime(text, fmt)
    except ValueError:
        return None


def _density(dates: Iterable[datetime]) -> float | None:
    days = sorted({d.date() for d in dates})
    if len(days) < 3:
        return None
    return len(days) / ((days[-1] - days[0]).days + 1)


def _date_evidence(series: pd.Series[Any], mapping: EventMapping) -> _DateEvidence:
    none = _DateEvidence(False, 0, 0, frozenset(), 0, 0.0)
    fmt = mapping.timestamp_format
    if fmt is None or _format_order(fmt) is None:
        return none
    swapped = _swap_day_month(fmt)
    format_only = swapped_only = ambiguous_rows = 0
    ambiguous: set[str] = set()
    as_format: list[datetime] = []
    as_swapped: list[datetime] = []
    for text, n in _text_cells(series).items():
        a, b = _strptime(text, fmt), _strptime(text, swapped)
        if a is not None and b is not None:
            as_format.append(a)
            as_swapped.append(b)
            if a != b:
                ambiguous.add(text)
                ambiguous_rows += n
        elif a is not None:
            format_only += n
        elif b is not None:
            swapped_only += n
    da, db = _density(as_format), _density(as_swapped)
    confidence = db / (da + db) if da is not None and db is not None and da + db > 0 else 0.5
    return _DateEvidence(True, format_only, swapped_only, frozenset(ambiguous), ambiguous_rows, confidence)


def _has_naive_times(series: pd.Series[Any], mapping: EventMapping) -> bool:
    formats = _formats(mapping)
    looked = 0
    for cell in series:
        cell = _plain(cell)
        if _is_missing(cell):
            continue
        looked += 1
        if looked > 500:
            break
        if isinstance(cell, datetime):
            if cell.tzinfo is None:
                return True
            continue
        parsed = _parse_time(str(cell).strip(), formats, mapping.timestamp_format is None)
        if parsed is not None and parsed.tzinfo is None:
            return True
    return False


def _parse_time(text: str, formats: tuple[str, ...], iso_first: bool = True) -> datetime | None:
    """`text` read in the first format that fits; ISO 8601 is tried first unless a format was given."""
    if iso_first:
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            if not formats:
                return None
    for fmt in formats:
        parsed = _strptime(text, fmt)
        if parsed is not None:
            return parsed
    return None


# --- numbers ----------------------------------------------------------------------------------------
_POINT_GROUPED = re.compile(r"^[+-]?(\d+|\d{1,3}(,\d{3})+)(\.\d+)?$")
_COMMA_GROUPED = re.compile(r"^[+-]?(\d+|\d{1,3}(\.\d{3})+)(,\d+)?$")


def _number_columns(mapping: EventMapping) -> tuple[str, ...]:
    named = (mapping.value_column, mapping.quantity_column, mapping.unit_price_column, mapping.margin_column)
    return tuple(dict.fromkeys(c for c in named if c is not None))


def _number_evidence(frame: pd.DataFrame, mapping: EventMapping) -> _NumberEvidence:
    """Which decimal separator the file's numbers use, from every number column at once."""
    columns = _number_columns(mapping)
    if mapping.decimal is not None:
        return _NumberEvidence(mapping.decimal, True, "", frozenset(), 0, columns)
    point = comma = 0
    ambiguous: Counter[str] = Counter()
    for column in columns:
        for text, n in _text_cells(frame[column]).items():
            if "," not in text and "." not in text:
                continue
            if _SEPARATOR_AMBIGUOUS.match(text):
                ambiguous[text] += n
                continue
            as_point, as_comma = bool(_POINT_GROUPED.match(text)), bool(_COMMA_GROUPED.match(text))
            if as_point and not as_comma:
                point += n
            elif as_comma and not as_point:
                comma += n
    if comma and not point:
        note = (
            f"Amounts use a decimal comma: {comma:,} cells (such as 2,55 or 1.234,50) can be read no "
            "other way, and none reads as a decimal point."
        )
        return _NumberEvidence(",", True, note, frozenset(), 0, columns)
    if point and not comma:
        return _NumberEvidence(".", True, "", frozenset(), 0, columns)
    # Nothing settles it (or the file mixes both): a 2.550 is not read either way.
    return _NumberEvidence(".", False, "", frozenset(ambiguous), sum(ambiguous.values()), columns)


def _parse_number(text: str, decimal: Literal[".", ","], thousands: Separator | None) -> float:
    """A number written as text, in the given convention; ValueError when it is not one."""
    text = text.strip().replace(" ", " ")
    if decimal == "." and thousands is None:
        # The historical reading: comma thousands only as whole groups ("1,234.50").
        if _GROUPED.match(text):
            text = text.replace(",", "")
        return float(text)
    sep = thousands if thousands is not None else ("." if decimal == "," else ",")
    if sep in (" ", "'"):
        text = text.replace(sep, "")
    elif sep != "none" and sep in text:
        group = re.escape(sep)
        if not re.match(rf"^[+-]?\d{{1,3}}({group}\d{{3}})+({re.escape(decimal)}\d+)?$", text):
            raise ValueError("bad thousands groups")
        text = text.replace(sep, "")
    other = "," if decimal == "." else "."
    if other in text:
        raise ValueError("the other separator")
    return float(text.replace(decimal, "."))


# --- currency ---------------------------------------------------------------------------------------
def _currency_code(value: str) -> str:
    code = value.strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", code):
        raise ValueError(f"{value!r} is not a three-letter currency code")
    return code


def _currency_evidence(frame: pd.DataFrame, mapping: EventMapping) -> _CurrencyEvidence:
    column = mapping.currency_source()
    if column is None:
        return _CurrencyEvidence(None, (), mapping.currency)
    counts: Counter[str] = Counter()
    for cell, n in _value_counts(frame[column]):
        text = str(_plain(cell)).strip().upper()
        if text:
            counts[text] += int(n)
    ranked = tuple(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
    business = mapping.currency or (ranked[0][0] if ranked else None)
    return _CurrencyEvidence(column, ranked, business)


# --- ids --------------------------------------------------------------------------------------------
def _id_evidence(series: pd.Series[Any], mapping: EventMapping, placeholders: frozenset[str]) -> _IdEvidence:
    counts: Counter[str] = Counter()
    for cell, n in _value_counts(series):
        text = _id_text(cell)
        if text:
            counts[text] += int(n)
    by_number: dict[str, set[str]] = {}
    zero_rows = 0
    for text, n in counts.items():
        if text.isdigit() and text.strip("0"):
            by_number.setdefault(text.lstrip("0"), set()).add(text)
            if text.startswith("0"):
                zero_rows += n
    collisions = frozenset(raw for group in by_number.values() if len(group) > 1 for raw in group)
    canon: Counter[str] = Counter()
    for text, n in counts.items():
        key = _canonical_id(text, placeholders, mapping.id_leading_zeros, mapping.id_placeholders is None)
        if isinstance(key, str):
            canon[key] += n
    heavy: tuple[str, int, int] | None = None
    total = sum(canon.values())
    top = canon.most_common(2)
    if top and total >= HEAVY_ID_MIN_ROWS:
        cid, n = top[0]
        nxt = top[1][1] if len(top) > 1 else 0
        if (
            n >= HEAVY_ID_SHARE * total
            and n >= HEAVY_ID_RATIO * max(nxt, 1)
            and cid not in mapping.allow_heavy_ids
        ):
            heavy = (cid, n, nxt)
    return _IdEvidence(collisions, zero_rows, heavy)


def _id_text(cell: object) -> str | None:
    """The id cell as text, before any judgement: a whole float loses its ".0"."""
    cell = _plain(cell)
    if _is_missing(cell):
        return None
    if isinstance(cell, float):
        return str(int(cell)) if cell.is_integer() else repr(cell)
    text = str(cell).strip()
    if _INTEGRAL_TEXT.match(text):
        text = text.split(".", 1)[0]
    return text or None


def _canonical_id(
    text: str, placeholders: frozenset[str], zeros: str | None, zero_is_placeholder: bool = True
) -> str | _RowError:
    """The customer id for a cell's text, or why the cell names nobody we can trust."""
    if _SCIENTIFIC_ID.match(text):
        return _RowError(
            ProblemCode.BAD_CUSTOMER_ID,
            "the id is a number in scientific notation (as a spreadsheet saves a long id), so its "
            "last digits are lost; export the column as text",
        )
    if text.lower() in placeholders or (zero_is_placeholder and text.isdigit() and not text.strip("0")):
        return _RowError(
            ProblemCode.PLACEHOLDER_CUSTOMER,
            "the id is a placeholder (such as 0, unknown, guest or a totals line), not a person; "
            "left out rather than merged into one customer",
        )
    if _EMAIL_ID.match(text):
        return text.lower()
    if zeros == "strip" and text.isdigit():
        return text.lstrip("0")
    return text


def customer_key(cell: object) -> str | None:
    """The id as text, or None when the cell names nobody.

    A whole float (12346.0, or "12346.0" read as text) loses its ".0"; an e-mail is lower-cased; a
    placeholder (`DEFAULT_ID_PLACEHOLDERS`, or all zeros) and an id in scientific notation are None.
    Leading zeros are kept ("007" stays "007").
    """
    text = _id_text(cell)
    if text is None:
        return None
    key = _canonical_id(text, frozenset(DEFAULT_ID_PLACEHOLDERS), None)
    return key if isinstance(key, str) else None


# ---------------------------------------------------------------------------
# One row
# ---------------------------------------------------------------------------
def _fields(
    record: Mapping[Hashable, Any], mapping: EventMapping, source: str, zone: tzinfo, plan: _Plan
) -> dict[str, Any]:
    customer = _customer(record, mapping, plan)
    ts = _timestamp(record[mapping.timestamp], mapping, zone, plan)
    kind = _event_type(record, mapping)
    value = _value(record, mapping, plan)
    if value is not None and value < 0:
        if mapping.negative_as is not None:
            kind, value = mapping.negative_as, -value
        elif kind is EventType.REFUND:
            # Many exports write a refund as a negative amount. A refund's value is the positive
            # amount returned (the fold subtracts it), so a negative one would *add* to revenue.
            value = -value
        elif kind is EventType.PURCHASE:
            raise _RowError(
                ProblemCode.BAD_NUMBER,
                "a purchase with a negative amount; set negative_as (e.g. 'refund') if it is a return",
                mapping.value_column or mapping.unit_price_column,
            )
    margin = _number(record, mapping.margin_column, plan.numbers.decimal, mapping.thousands, plan)
    attrs = _attrs(record, mapping)
    currency = _currency(record, mapping, plan)
    if currency is not None:
        code, rate = currency
        if rate != 1.0:
            if value is not None:
                attrs["original_value"] = value
                value = value * rate
            if margin is not None:
                margin = margin * rate
            attrs["original_currency"] = code
        attrs["currency"] = plan.currency.business or code
    return {
        "customer_id": customer,
        "type": kind,
        "ts": ts,
        "source": source,
        "channel": _channel(record, mapping),
        "decision_id": _text(record, mapping.decision_id_column),
        "value": value,
        "margin": margin,
        "category": _text(record, mapping.category_column),
        "item_id": _text(record, mapping.item_column),
        "attrs": attrs,
    }


def _content(fields: Mapping[str, Any]) -> tuple[str, ...]:
    """What makes two rows the same event, as text: the event id is built from it, not the row's place."""
    ts = cast(datetime, fields["ts"]).astimezone(UTC).isoformat()
    value = fields["value"]
    channel = fields["channel"]
    return (
        "content",
        str(fields["customer_id"]),
        cast(EventType, fields["type"]).value,
        ts,
        "" if value is None else repr(float(value)),
        fields["item_id"] or "",
        fields["category"] or "",
        "" if channel is None else cast(Channel, channel).value,
        fields["decision_id"] or "",
    )


def _build(event_id: str, fields: Mapping[str, Any]) -> Event:
    try:
        return Event(event_id=event_id, **fields)
    except ValidationError as exc:
        reasons = "; ".join(str(e.get("msg", "")) for e in exc.errors())
        raise _RowError(ProblemCode.INVALID_EVENT, reasons) from None


def _customer(record: Mapping[Hashable, Any], mapping: EventMapping, plan: _Plan) -> str:
    column = mapping.customer_id
    text = _id_text(record[column])
    if text is None:
        raise _RowError(ProblemCode.MISSING_CUSTOMER, "no customer id", column)
    key = _canonical_id(text, plan.placeholders, mapping.id_leading_zeros, plan.zero_is_placeholder)
    if isinstance(key, _RowError):
        key.column = column
        raise key
    if mapping.id_leading_zeros is None and text in plan.ids.collisions:
        raise _RowError(
            ProblemCode.BAD_CUSTOMER_ID,
            "this id also appears in the file with leading zeros added or removed (like 007 and 7); "
            "set id_leading_zeros to say whether they are one customer",
            column,
        )
    return key


def _timestamp(cell: object, mapping: EventMapping, zone: tzinfo, plan: _Plan) -> datetime:
    column = mapping.timestamp
    cell = _plain(cell)
    if _is_missing(cell):
        raise _RowError(ProblemCode.BAD_TIMESTAMP, "no timestamp", column)
    if isinstance(cell, pd.Timestamp):
        parsed: datetime = cell.to_pydatetime()
    elif isinstance(cell, datetime):
        parsed = cell
    else:
        text = str(cell).strip()
        d = plan.dates
        if d.swapped_only and mapping.date_order is None and _reads_both_ways(text, mapping.timestamp_format):
            # Other rows only read with day and month the other way round from the format, so the
            # format is not settled for this one either: report it rather than pick a reading.
            raise _RowError(
                ProblemCode.AMBIGUOUS_DATE,
                "this date reads as two different days (day first or month first), and other rows "
                "only read the other way round from the format; set date_order",
                column,
            )
        found = _parse_time(text, _formats(mapping), mapping.timestamp_format is None)
        if found is None:
            expected = mapping.timestamp_format or (
                f"{_words(mapping.date_order)} dates" if mapping.date_order else "ISO 8601"
            )
            hint = ""
            fmt = mapping.timestamp_format
            if d.swapped_only and fmt is not None and _strptime(text, _swap_day_month(fmt)) is not None:
                hint = "; it reads with day and month the other way round (set date_order)"
            raise _RowError(
                ProblemCode.BAD_TIMESTAMP, f"cannot read the timestamp as {expected}{hint}", column
            )
        parsed = found
    wall = parsed
    midnight = wall.time() == datetime.min.time()
    if ((wall.year, wall.month, wall.day) in PLACEHOLDER_DAYS and midnight) or wall.year >= 9999:
        raise _RowError(
            ProblemCode.BAD_TIMESTAMP,
            "a placeholder date (such as 1900-01-01 or 1970-01-01 at midnight), not a real time",
            column,
        )
    aware = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=zone)
    if mapping.not_after is not None and aware > mapping.not_after:
        two_digit = "%y" in (mapping.timestamp_format or "")
        raise _RowError(
            ProblemCode.BAD_TIMESTAMP,
            "later than the latest plausible time (not_after)"
            + ("; a two-digit year read in the wrong century? use %Y" if two_digit else ""),
            column,
        )
    if mapping.not_before is not None and aware < mapping.not_before:
        raise _RowError(
            ProblemCode.BAD_TIMESTAMP, "earlier than the earliest plausible time (not_before)", column
        )
    return aware


def _reads_both_ways(text: str, fmt: str | None) -> bool:
    """`text` is a valid date with day and month either way round, and the two are different days.

    Checked on the row itself, never looked up in the inspection's sample, so a big file's rows
    beyond the sample are not read one way silently."""
    if fmt is None:
        return False
    a = _strptime(text, fmt)
    if a is None:
        return False
    b = _strptime(text, _swap_day_month(fmt))
    return b is not None and a != b


def _event_type(record: Mapping[Hashable, Any], mapping: EventMapping) -> EventType:
    if mapping.event_type is not None:
        return mapping.event_type
    column = mapping.event_type_column
    assert column is not None  # the model validator guarantees one of the two
    # `_text` turns a code that went through a float column (1.0, beside an empty cell) back into "1".
    word = (_text(record, column) or "").casefold()
    if not word:
        raise _RowError(ProblemCode.UNKNOWN_EVENT_TYPE, "no event type", column)
    if word in mapping.event_type_values:
        return mapping.event_type_values[word]
    try:
        return EventType(word)
    except ValueError:
        raise _RowError(
            ProblemCode.UNKNOWN_EVENT_TYPE, "the event type is not in the mapping's value map", column
        ) from None


def _value(record: Mapping[Hashable, Any], mapping: EventMapping, plan: _Plan) -> float | None:
    decimal, thousands = plan.numbers.decimal, mapping.thousands
    if mapping.quantity_column is not None and mapping.unit_price_column is not None:
        quantity = _number(record, mapping.quantity_column, decimal, thousands, plan)
        price = _number(record, mapping.unit_price_column, decimal, thousands, plan)
        if quantity is None or price is None:
            return None
        return quantity * price
    return _number(record, mapping.value_column, decimal, thousands, plan)


def _currency(record: Mapping[Hashable, Any], mapping: EventMapping, plan: _Plan) -> tuple[str, float] | None:
    """The row's currency and the rate into the business currency; None when no currency is known."""
    cur = plan.currency
    if cur.column is None:
        return (mapping.currency, 1.0) if mapping.currency is not None else None
    cell = _plain(record[cur.column])
    code = None if _is_missing(cell) else str(cell).strip().upper()
    if code is None or code == cur.business:
        return (cur.business, 1.0) if cur.business is not None else None
    if code in mapping.currency_rates:
        return code, mapping.currency_rates[code]
    raise _RowError(
        ProblemCode.MIXED_CURRENCY,
        f"this amount is in another currency than {cur.business}; give currency and currency_rates to "
        "convert it, or load it separately (amounts in different currencies are never added)",
        cur.column,
    )


def _channel(record: Mapping[Hashable, Any], mapping: EventMapping) -> Channel | None:
    if mapping.channel_column is None:
        return mapping.channel
    raw = record[mapping.channel_column]
    if _is_missing(raw):
        return None
    try:
        return Channel(str(raw).strip().casefold())
    except ValueError:
        raise _RowError(
            ProblemCode.BAD_CHANNEL,
            f"not a channel; expected one of {', '.join(c.value for c in Channel)}",
            mapping.channel_column,
        ) from None


def _attrs(record: Mapping[Hashable, Any], mapping: EventMapping) -> dict[str, AttrValue]:
    attrs: dict[str, AttrValue] = dict(mapping.fixed_attrs)
    for name, column in mapping.attrs.items():
        cell = _plain(record[column])
        if _is_missing(cell):
            continue
        if isinstance(cell, str | bool | int | float):
            attrs[name] = cell.strip() if isinstance(cell, str) else cell
        else:
            attrs[name] = str(cell)
    return attrs


def _number(
    record: Mapping[Hashable, Any],
    column: str | None,
    decimal: Literal[".", ","] = ".",
    thousands: Separator | None = None,
    plan: _Plan | None = None,
) -> float | None:
    """The cell as a finite float; None when the column is unmapped or the cell is empty."""
    if column is None:
        return None
    cell = _plain(record[column])
    if _is_missing(cell):
        return None
    if isinstance(cell, bool):
        raise _RowError(ProblemCode.BAD_NUMBER, "expected a number, found true/false", column)
    if (
        isinstance(cell, str)
        and plan is not None
        and not plan.numbers.settled
        and _SEPARATOR_AMBIGUOUS.match(cell.strip())
    ):
        raise _RowError(
            ProblemCode.AMBIGUOUS_NUMBER,
            "one separator then three digits (like 2.550): a decimal point or a thousands separator? "
            "Nothing else in the file says; set decimal",
            column,
        )
    try:
        number = (
            _parse_number(cell, decimal, thousands) if isinstance(cell, str) else float(cell)  # type: ignore[arg-type]
        )
    except (TypeError, ValueError):
        hint = ""
        if isinstance(cell, str) and decimal == "." and re.match(r"^[+-]?\d+,\d{1,2}$", cell.strip()):
            hint = " (a decimal comma? set decimal=',')"
        raise _RowError(ProblemCode.BAD_NUMBER, f"expected a number{hint}", column) from None
    if not math.isfinite(number):
        raise _RowError(ProblemCode.BAD_NUMBER, "the number is not finite", column)
    return number


def _text(record: Mapping[Hashable, Any], column: str | None) -> str | None:
    if column is None:
        return None
    cell = _plain(record[column])
    if _is_missing(cell):
        return None
    if isinstance(cell, float) and cell.is_integer():
        return str(int(cell))
    return str(cell).strip() or None


def _plain(cell: object) -> object:
    """A numpy scalar as the Python value it holds, so pydantic and isinstance see ordinary types."""
    item = getattr(cell, "item", None)
    if callable(item) and not isinstance(cell, str | bytes | pd.Timestamp):
        try:
            return item()
        except (TypeError, ValueError):
            return cell
    return cell


def _is_missing(cell: object) -> bool:
    if cell is None:
        return True
    if isinstance(cell, str):
        return not cell.strip()
    try:
        return bool(pd.isna(cast(Any, cell)))  # a scalar here; the cast picks pandas' scalar overload
    except (TypeError, ValueError):
        return False


def _row_key(label: Hashable, record: Mapping[Hashable, Any], mapping: EventMapping) -> tuple[str, ...]:
    if not mapping.row_key:
        return (str(label),)
    return tuple(_text(record, c) or "" for c in mapping.row_key)


def _describe(key: tuple[str, ...]) -> str:
    return "/".join(key)
