"""A compact in-memory event store: the `EventStore` protocol at a small fraction of the memory.

`autonomy.fakes.InMemoryEventStore` keeps every `Event` object it is given. A pydantic `Event` costs
about 1.6 to 2.4 KB of peak memory (its `__dict__`, its fields-set `set`, a `datetime`, an empty
`attrs` dict and its strings), so a few million events fill a 16 GB machine (docs/v2/real_data,
family C). This store keeps the same information in columns instead:

* one `array` per field: the time as UTC microseconds, the type and channel as small enum codes,
  value and margin as doubles, a bit mask for which fields were set and which floats are None;
* every repeated string (customer id, source, category, item id, decision id) stored once in a
  pool and referred to by number; `attrs` dicts pooled the same way;
* the rare fields kept sparsely: reply `text`, a timestamp whose zone does not survive a round trip
  through UTC (a wall time in a daylight-saving gap), and any event this layout cannot hold, which
  is kept as the object it came in as.

Reads rebuild an `Event` on the way out with `Event.model_construct` (no validation: everything in
here was validated when it came in), with the same values and the same fields-set. `scan` streams:
it sorts row numbers once (numpy, stable, so equal times keep insertion order, as the fake does),
keeps the sorted order until the next append, and builds one `Event` at a time.

It behaves like the fake: a repeated `event_id` is ignored, `for_customer` / `scan` /
`for_decision` return events in time order with ties in insertion order, `since <= ts < until`.
The one difference: the fake compares datetimes, which for two times in the same `ZoneInfo` zone
means wall-clock order; this store orders by the instant. They differ only for times inside a
daylight-saving fold.
"""

from __future__ import annotations

from array import array
from collections.abc import Hashable, Iterable, Iterator
from datetime import UTC, datetime, timedelta, tzinfo
from math import copysign
from typing import Any

import numpy as np
import numpy.typing as npt

from autonomy.contracts import Channel, Event, EventType
from autonomy.contracts.events import AttrValue

__all__ = ["CompactEventStore"]

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICRO = timedelta(microseconds=1)
_SCAN_CHUNK = 4096
"""Row numbers turned into Python ints at a time while streaming a scan."""

_FIELDS: tuple[str, ...] = tuple(Event.model_fields)
_LAYOUT = frozenset(
    {
        "event_id",
        "customer_id",
        "type",
        "ts",
        "source",
        "channel",
        "decision_id",
        "value",
        "margin",
        "category",
        "item_id",
        "text",
        "attrs",
    }
)
COMPACT = frozenset(_FIELDS) == _LAYOUT
"""False if `Event` gained or lost a field since this layout was written; every event is then kept
whole (correct, but no saving) until the layout is updated. A test pins it to True."""

_BIT = {name: 1 << i for i, name in enumerate(_FIELDS)}
_VALUE_NONE = 1 << len(_FIELDS)
_MARGIN_NONE = 1 << (len(_FIELDS) + 1)
_SET_MASK = (1 << len(_FIELDS)) - 1

_TYPES: tuple[EventType, ...] = tuple(EventType)
_TYPE_CODE = {t: i for i, t in enumerate(_TYPES)}
_CHANNELS: tuple[Channel | None, ...] = (None, *Channel)
_CHANNEL_CODE: dict[Channel | None, int] = {c: i for i, c in enumerate(_CHANNELS)}

AttrItems = tuple[tuple[str, AttrValue], ...]

_SCALARS = frozenset({str, int, float, bool})
_set_attr = object.__setattr__


def _construct(values: dict[str, Any], fields_set: set[str]) -> Event:
    """`Event.model_construct(_fields_set=fields_set, **values)` for a complete `values`, minus the
    per-field default and alias handling it does not need here: about 0.8 µs instead of 5 µs.

    It sets the four slots pydantic's own `model_construct` sets for a model without private
    attributes, extras or a post-init hook; `_FAST` checks those preconditions (and that the
    result equals `model_construct`'s) once at import, and the store falls back otherwise.
    """
    event = Event.__new__(Event)
    _set_attr(event, "__dict__", values)
    _set_attr(event, "__pydantic_fields_set__", fields_set)
    _set_attr(event, "__pydantic_extra__", None)
    _set_attr(event, "__pydantic_private__", None)
    return event


def _fast_path_ok() -> bool:
    if (
        Event.__pydantic_post_init__
        or Event.__private_attributes__
        or Event.model_config.get("extra") == "allow"
    ):
        return False
    probe = Event(event_id="probe", customer_id="c", type=EventType.VIEW, ts=_EPOCH, source="s")
    values, fields_set = dict(probe.__dict__), set(probe.model_fields_set)
    slow = Event.model_construct(_fields_set=set(fields_set), **dict(values))
    fast = _construct(values, fields_set)
    return bool(
        fast == slow
        and fast.model_fields_set == slow.model_fields_set
        and fast.model_dump() == slow.model_dump()
        and getattr(fast, "__pydantic_extra__", 0) == getattr(slow, "__pydantic_extra__", 1)
        and getattr(fast, "__pydantic_private__", 0) == getattr(slow, "__pydantic_private__", 1)
    )


_FAST = _fast_path_ok()


def _micros(moment: datetime) -> int:
    """UTC microseconds since 1970 of an aware datetime (a naive one raises, as comparing would)."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise TypeError(f"can't compare offset-naive and offset-aware datetimes: {moment!r}")
    return (moment - _EPOCH) // _MICRO


def _from_micros(us: int, zone: tzinfo) -> datetime:
    utc = _EPOCH + timedelta(microseconds=us)
    return utc if zone is UTC else utc.astimezone(zone)


class _Pool:
    """Each distinct string once; code 0 is None."""

    __slots__ = ("_code", "values")

    def __init__(self) -> None:
        self.values: list[str | None] = [None]
        self._code: dict[str, int] = {}

    def code(self, value: str | None) -> int:
        if value is None:
            return 0
        found = self._code.get(value)
        if found is None:
            found = len(self.values)
            self._code[value] = found
            self.values.append(value)
        return found


class CompactEventStore:
    """The `EventStore` protocol in columns; see the module docstring."""

    def __init__(self) -> None:
        self._ids: list[str] = []
        self._seen: set[str] = set()
        self._strings = _Pool()
        self._ts = array("q")
        self._zone = array("H")
        self._zones: list[tzinfo] = [UTC]  # code 0
        self._zone_code: dict[Hashable, int] = {}
        self._customer = array("I")
        self._source = array("I")
        self._category = array("I")
        self._item = array("I")
        self._decision = array("I")
        self._type = array("B")
        self._channel = array("B")
        self._value = array("d")
        self._margin = array("d")
        self._flags = array("I")
        self._attrs = array("I")
        self._attr_items: list[AttrItems] = [()]
        self._attr_code: dict[tuple[tuple[str, type, AttrValue, float], ...], int] = {(): 0}
        self._text: dict[int, str] = {}
        self._odd_ts: dict[int, datetime] = {}
        self._whole: dict[int, Event] = {}
        self._by_customer: dict[str, array[int]] = {}
        self._by_decision: dict[str, array[int]] = {}
        self._fields_sets: dict[int, frozenset[str]] = {}
        self._sorted_n = 0
        self._order: npt.NDArray[np.int64] = np.empty(0, dtype=np.int64)
        self._sorted_ts: npt.NDArray[np.int64] = np.empty(0, dtype=np.int64)

    # --- writing ----------------------------------------------------------------------------------

    def append(self, events: Iterable[Event]) -> int:
        """Store events; return how many were new. A repeated `event_id` is ignored."""
        new = 0
        for e in events:
            if e.event_id in self._seen:
                continue
            self._seen.add(e.event_id)
            self._add(e)
            new += 1
        return new

    def _zone_of(self, moment: datetime) -> int | None:
        """The pool code of the moment's tzinfo; None if it cannot be pooled (kept whole then)."""
        zone = moment.tzinfo
        if zone is UTC:
            return 0
        assert zone is not None
        try:
            # Equal is not enough: timezone(timedelta(0), "Z") == UTC, but it prints differently.
            key: Hashable = (type(zone), zone, repr(zone))
            code = self._zone_code.get(key)
        except TypeError:  # an unhashable tzinfo
            return None
        if code is None:
            if len(self._zones) >= 0xFFFF:
                return None
            code = len(self._zones)
            self._zones.append(zone)
            self._zone_code[key] = code
        return code

    def _attrs_code(self, attrs: dict[str, AttrValue]) -> int:
        if not attrs:
            return 0
        items = tuple(attrs.items())
        # True == 1 == 1.0 hash alike, so the key carries each value's type; 0.0 == -0.0 too, so a
        # float also carries its sign (else {"x": -0.0} would read back as the {"x": 0.0} pooled first).
        key = tuple((k, type(v), v, copysign(1.0, v) if type(v) is float else 0.0) for k, v in items)
        code = self._attr_code.get(key)
        if code is None:
            code = len(self._attr_items)
            self._attr_items.append(items)
            self._attr_code[key] = code
        return code

    def _fits(self, e: Event) -> bool:
        d = e.__dict__
        return (
            COMPACT
            and len(d) == len(_FIELDS)
            and type(e.type) is EventType
            and (e.channel is None or type(e.channel) is Channel)
            and type(e.ts) is datetime  # not a subclass such as pandas' Timestamp
            and e.ts.tzinfo is not None
            and (e.value is None or type(e.value) is float)
            and (e.margin is None or type(e.margin) is float)
            and type(e.attrs) is dict
            and all(type(k) is str and type(v) in _SCALARS for k, v in e.attrs.items())
            and all(type(d[k]) is str or d[k] is None for k in ("decision_id", "category", "item_id"))
            and type(e.customer_id) is str
            and type(e.source) is str
            and (e.text is None or type(e.text) is str)
        )

    def _add(self, e: Event) -> None:
        row = len(self._ids)
        self._ids.append(e.event_id)
        customer = self._strings.code(e.customer_id)
        rows = self._by_customer.get(e.customer_id)
        if rows is None:
            # A new customer's id object is the one the pool just kept, so the key costs nothing more.
            rows = self._by_customer[e.customer_id] = array("I")
        rows.append(row)
        if e.decision_id:
            drows = self._by_decision.get(e.decision_id)
            if drows is None:
                drows = self._by_decision[e.decision_id] = array("I")
            drows.append(row)
        if not self._fits(e):
            # Kept whole; the columns still get the time (for sorting) and placeholders.
            self._whole[row] = e
            self._ts.append(_micros(e.ts))
            self._zone.append(0)
            for column in (self._source, self._category, self._item, self._decision, self._flags):
                column.append(0)
            self._customer.append(customer)
            self._type.append(0)
            self._channel.append(0)
            self._value.append(0.0)
            self._margin.append(0.0)
            self._attrs.append(0)
            return
        ts = e.ts
        us = (ts - _EPOCH) // _MICRO
        zone = self._zone_of(ts)
        if zone is None:
            self._odd_ts[row] = ts
            zone = 0
        elif ts.tzinfo is not UTC or ts.fold:
            try:
                back = _from_micros(us, self._zones[zone])
                same = back.replace(tzinfo=None) == ts.replace(tzinfo=None) and back.fold == ts.fold
            except OverflowError:  # e.g. 0001-01-01 at +05:00, whose instant in UTC is before year 1
                same = False
            if not same:
                self._odd_ts[row] = ts  # e.g. a wall time in a daylight-saving gap
        flags = 0
        for name in e.model_fields_set:
            flags |= _BIT[name]
        if e.value is None:
            flags |= _VALUE_NONE
        if e.margin is None:
            flags |= _MARGIN_NONE
        if e.text is not None:
            self._text[row] = e.text
        self._ts.append(us)
        self._zone.append(zone)
        self._customer.append(customer)
        self._source.append(self._strings.code(e.source))
        self._category.append(self._strings.code(e.category))
        self._item.append(self._strings.code(e.item_id))
        self._decision.append(self._strings.code(e.decision_id))
        self._type.append(_TYPE_CODE[e.type])
        self._channel.append(_CHANNEL_CODE[e.channel])
        self._value.append(0.0 if e.value is None else e.value)
        self._margin.append(0.0 if e.margin is None else e.margin)
        self._flags.append(flags)
        self._attrs.append(self._attrs_code(e.attrs))

    # --- reading ----------------------------------------------------------------------------------

    def _fields_set(self, flags: int) -> set[str]:
        names = self._fields_sets.get(flags & _SET_MASK)
        if names is None:
            names = frozenset(n for n in _FIELDS if flags & _BIT[n])
            self._fields_sets[flags & _SET_MASK] = names
        return set(names)  # a fresh set per event: pydantic keeps the one it is given

    def _event(self, row: int) -> Event:
        if self._whole:
            whole = self._whole.get(row)
            if whole is not None:
                return whole
        strings = self._strings.values
        flags = self._flags[row]
        ts = self._odd_ts.get(row) if self._odd_ts else None
        if ts is None:
            ts = _from_micros(self._ts[row], self._zones[self._zone[row]])
        values: dict[str, Any] = {
            "event_id": self._ids[row],
            "customer_id": strings[self._customer[row]],
            "type": _TYPES[self._type[row]],
            "ts": ts,
            "source": strings[self._source[row]],
            "channel": _CHANNELS[self._channel[row]],
            "decision_id": strings[self._decision[row]],
            "value": None if flags & _VALUE_NONE else self._value[row],
            "margin": None if flags & _MARGIN_NONE else self._margin[row],
            "category": strings[self._category[row]],
            "item_id": strings[self._item[row]],
            "text": self._text.get(row) if self._text else None,
            "attrs": dict(self._attr_items[self._attrs[row]]),
        }
        if _FAST:
            return _construct(values, self._fields_set(flags))
        return Event.model_construct(_fields_set=self._fields_set(flags), **values)

    def _window_rows(self, rows: Iterable[int], since: datetime | None, until: datetime | None) -> list[int]:
        ts = self._ts
        if since is None and until is None:
            picked = list(rows)
        else:
            lo = _micros(since) if since is not None else None
            hi = _micros(until) if until is not None else None
            picked = [r for r in rows if (lo is None or ts[r] >= lo) and (hi is None or ts[r] < hi)]
        picked.sort(key=ts.__getitem__)  # stable: equal times keep insertion order
        return picked

    def for_customer(
        self, customer_id: str, *, since: datetime | None = None, until: datetime | None = None
    ) -> list[Event]:
        """One customer's events in time order."""
        rows = self._by_customer.get(customer_id)
        if not rows:
            return []
        return [self._event(r) for r in self._window_rows(rows, since, until)]

    def for_decision(self, decision_id: str) -> list[Event]:
        rows = self._by_decision.get(decision_id)
        if not rows:
            return []
        return [self._event(r) for r in self._window_rows(rows, None, None)]

    def _sorted(self) -> tuple[npt.NDArray[np.int64], npt.NDArray[np.int64]]:
        """Row numbers in (time, insertion) order and the times in that order, cached until an append.

        Appends that arrive in time order (the usual case) extend the cache instead of re-sorting.
        Copies, never views, of the arrays: an `array` that exports a buffer cannot grow.
        """
        n = len(self._ts)
        if self._sorted_n == n:
            return self._order, self._sorted_ts
        k = self._sorted_n
        tail = np.array(self._ts[k:], dtype=np.int64)
        in_order = bool(np.all(tail[1:] >= tail[:-1])) and (k == 0 or tail[0] >= self._sorted_ts[-1])
        if in_order:
            order = np.concatenate([self._order, np.arange(k, n, dtype=np.int64)])
            sorted_ts = np.concatenate([self._sorted_ts, tail])
        else:
            every = np.array(self._ts, dtype=np.int64)
            order = np.argsort(every, kind="stable").astype(np.int64, copy=False)
            sorted_ts = every[order]
        self._order, self._sorted_ts, self._sorted_n = order, sorted_ts, n
        return order, sorted_ts

    def scan(self, *, since: datetime | None = None, until: datetime | None = None) -> Iterator[Event]:
        """All events in time order, streamed; a snapshot of the store as of the first event read."""
        order, sorted_ts = self._sorted()
        lo = 0 if since is None else int(np.searchsorted(sorted_ts, _micros(since), side="left"))
        hi = len(order) if until is None else int(np.searchsorted(sorted_ts, _micros(until), side="left"))
        for start in range(lo, hi, _SCAN_CHUNK):
            for row in order[start : min(hi, start + _SCAN_CHUNK)].tolist():
                yield self._event(row)

    def count(self) -> int:
        return len(self._ids)

    def __len__(self) -> int:
        return len(self._ids)
