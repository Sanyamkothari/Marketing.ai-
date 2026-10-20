"""How a flat business table becomes our `Event`s.

A client's export is one row per thing that happened (an order line, a send, a signup), with its own
column names, date format and words for event types. `EventMapping` says which column is which, once,
in data (it loads from YAML or JSON as easily as from Python), and `map_frame` applies it.

Two rules shape `map_frame`:

- **Bad rows are collected, not fatal.** A real export always has a few rows with no customer, a date
  nobody can read or an amount of "N/A". Each becomes a `MappingProblem` that names the row, the column
  and what was wrong, and the rest of the file still loads. Only a mapping that cannot apply to the
  table at all (a named column is missing) raises `MappingError`, because then every row would fail
  for the same reason.
- **Event ids are stable.** An event's id is `stable_id("evt", source, <row key>)`, so loading the same
  file twice yields the same ids and the append-only event store drops the repeats. The row key is the
  `row_key` columns when given, else the frame's index label (the row's position in the file as read).

Pure and deterministic: no clock reads, no randomness, no I/O.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Hashable, Iterable, Mapping
from datetime import datetime, tzinfo
from typing import Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd
from pydantic import Field, ValidationError, field_validator, model_validator

from autonomy.contracts import Channel, Contract, Event, EventType, stable_id
from autonomy.contracts.events import AttrValue

__all__ = [
    "EventMapping",
    "MappingError",
    "MappingProblem",
    "ProblemCode",
    "customer_key",
    "map_frame",
    "problem_counts",
]

EVENT_ID_PREFIX = "evt"

_INTEGRAL_TEXT = re.compile(r"^-?\d+\.0+$")
"""An integer key that went through a float column on its way here, such as "17850.0"."""

_GROUPED = re.compile(r"^-?\d{1,3}(,\d{3})+(\.\d+)?$")
"""A number with comma thousands groups, such as "1,234" or "-12,345.60"."""


class ProblemCode:
    """The reasons a row can be left out. Plain strings, so a summary can count them."""

    MISSING_CUSTOMER = "missing_customer"
    BAD_TIMESTAMP = "bad_timestamp"
    UNKNOWN_EVENT_TYPE = "unknown_event_type"
    BAD_NUMBER = "bad_number"
    BAD_CHANNEL = "bad_channel"
    DUPLICATE_ROW = "duplicate_row"
    INVALID_EVENT = "invalid_event"
    EXCLUDED = "excluded"
    """Used by loaders for rows they leave out on purpose (a guest checkout, a postage adjustment)."""


class MappingError(ValueError):
    """The mapping cannot apply to this table at all, for example because a column it names is missing."""


class MappingProblem(Contract):
    """One row that did not become an event, and why. Never carries the row's values (they may be personal)."""

    row: str = Field(description="The row key: the `row_key` values, or the row's index label")
    code: str = Field(description="One of `ProblemCode`")
    column: str | None = Field(default=None, description="The column at fault, when one is")
    message: str


class EventMapping(Contract):
    """Which column of a flat table is which part of an `Event`.

    Every field naming a column holds the column's name in the table. The event type is either one
    constant (`event_type`, e.g. every row of an orders export is a purchase) or read from a column
    (`event_type_column`) through `event_type_values`, which maps the table's own words ("order",
    "Cancelled") to our types; a value not in the map is tried as one of our type names, so a table
    that already says "purchase" needs no map.
    """

    customer_id: str = Field(description="Column holding the customer's id")
    timestamp: str = Field(description="Column holding when it happened")
    timestamp_format: str | None = Field(
        default=None,
        description="A `strptime` format such as '%m/%d/%Y %H:%M'; None reads ISO 8601",
    )
    timezone: str = Field(
        default="UTC", description="IANA zone for timestamps that carry no offset of their own"
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
        "still made positive, and any other negative value is kept as it is",
    )
    margin_column: str | None = None
    category_column: str | None = None
    item_column: str | None = None

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
        default=(), description="Columns that identify a row; empty means the frame's index label"
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
        return self

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


def map_frame(
    frame: pd.DataFrame, mapping: EventMapping, source: str
) -> tuple[list[Event], list[MappingProblem]]:
    """Turn every row of `frame` into an `Event`; return the events and the rows that could not be.

    Raises `MappingError` only when a column the mapping names is not in the frame. Events come out
    in row order. A second row with the same row key is reported as `duplicate_row` and left out,
    because it would get the same event id and the store would silently drop it anyway.
    """
    missing = [c for c in mapping.columns() if c not in frame.columns]
    if missing:
        raise MappingError(
            f"the table has no column {', '.join(repr(c) for c in missing)}; "
            f"it has {', '.join(repr(str(c)) for c in frame.columns)}"
        )
    zone = ZoneInfo(mapping.timezone)
    events: list[Event] = []
    problems: list[MappingProblem] = []
    seen: set[str] = set()
    records = frame.loc[:, list(mapping.columns())].to_dict("records")
    for label, record in zip(frame.index, records, strict=True):
        key = _row_key(label, record, mapping)
        try:
            event_id = stable_id(EVENT_ID_PREFIX, source, *key)
            if event_id in seen:
                raise _RowError(ProblemCode.DUPLICATE_ROW, "another row has the same row key")
            event = _event(record, mapping, source, event_id, zone)
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


# ---------------------------------------------------------------------------
# One row
# ---------------------------------------------------------------------------
def _event(
    record: Mapping[Hashable, Any], mapping: EventMapping, source: str, event_id: str, zone: tzinfo
) -> Event:
    customer = customer_key(record[mapping.customer_id])
    if customer is None:
        raise _RowError(ProblemCode.MISSING_CUSTOMER, "no customer id", mapping.customer_id)
    ts = _timestamp(record[mapping.timestamp], mapping, zone)
    kind = _event_type(record, mapping)
    value = _value(record, mapping)
    if value is not None and value < 0:
        if mapping.negative_as is not None:
            kind, value = mapping.negative_as, -value
        elif kind is EventType.REFUND:
            # Many exports write a refund as a negative amount. A refund's value is the positive
            # amount returned (the fold subtracts it), so a negative one would *add* to revenue.
            value = -value
    channel = _channel(record, mapping)
    try:
        return Event(
            event_id=event_id,
            customer_id=customer,
            type=kind,
            ts=ts,
            source=source,
            channel=channel,
            decision_id=_text(record, mapping.decision_id_column),
            value=value,
            margin=_number(record, mapping.margin_column),
            category=_text(record, mapping.category_column),
            item_id=_text(record, mapping.item_column),
            attrs=_attrs(record, mapping),
        )
    except ValidationError as exc:
        reasons = "; ".join(str(e.get("msg", "")) for e in exc.errors())
        raise _RowError(ProblemCode.INVALID_EVENT, reasons) from None


def customer_key(cell: object) -> str | None:
    """The id as text. A whole float (12346.0, or "12346.0" read as text) loses its ".0"."""
    cell = _plain(cell)
    if _is_missing(cell):
        return None
    if isinstance(cell, float):
        return str(int(cell)) if cell.is_integer() else repr(cell)
    text = str(cell).strip()
    if _INTEGRAL_TEXT.match(text):
        text = text.split(".", 1)[0]
    return text or None


def _timestamp(cell: object, mapping: EventMapping, zone: tzinfo) -> datetime:
    column = mapping.timestamp
    if _is_missing(cell):
        raise _RowError(ProblemCode.BAD_TIMESTAMP, "no timestamp", column)
    if isinstance(cell, pd.Timestamp):
        parsed: datetime = cell.to_pydatetime()
    elif isinstance(cell, datetime):
        parsed = cell
    else:
        text = str(cell).strip()
        try:
            if mapping.timestamp_format is not None:
                parsed = datetime.strptime(text, mapping.timestamp_format)
            else:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            expected = mapping.timestamp_format or "ISO 8601"
            raise _RowError(
                ProblemCode.BAD_TIMESTAMP, f"cannot read the timestamp as {expected}", column
            ) from None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=zone)


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


def _value(record: Mapping[Hashable, Any], mapping: EventMapping) -> float | None:
    if mapping.quantity_column is not None and mapping.unit_price_column is not None:
        quantity = _number(record, mapping.quantity_column)
        price = _number(record, mapping.unit_price_column)
        if quantity is None or price is None:
            return None
        return quantity * price
    return _number(record, mapping.value_column)


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


def _number(record: Mapping[Hashable, Any], column: str | None) -> float | None:
    """The cell as a finite float; None when the column is unmapped or the cell is empty."""
    if column is None:
        return None
    cell = _plain(record[column])
    if _is_missing(cell):
        return None
    if isinstance(cell, bool):
        raise _RowError(ProblemCode.BAD_NUMBER, "expected a number, found true/false", column)
    try:
        number = _parse_number(cell) if isinstance(cell, str) else float(cell)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise _RowError(ProblemCode.BAD_NUMBER, "expected a number", column) from None
    if not math.isfinite(number):
        raise _RowError(ProblemCode.BAD_NUMBER, "the number is not finite", column)
    return number


def _parse_number(text: str) -> float:
    """A number written as text. Commas are read only as thousands groups ("1,234.50"); a decimal
    comma ("12,5") is refused rather than misread as 125, so the row is reported, not silently wrong."""
    text = text.strip()
    if _GROUPED.match(text):
        text = text.replace(",", "")
    return float(text)


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
