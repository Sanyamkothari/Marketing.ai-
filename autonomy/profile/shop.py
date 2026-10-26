"""The shop's own order values up to a date, so a customer's price level is read against the shop.

"Premium or budget" only means something inside one shop: 40 is a big basket in one and a small one
in another. `ShopPrices` keeps the shop's order values in a histogram with log-spaced bins (about 12%
wide), so memory stays a few hundred counters however many orders there are, and answers one
question: `rank(v)`, the share of the shop's orders worth no more than `v`.

Point in time: `ShopPrices.until(events, as_of)` adds only purchases with `ts <= as_of`;
`ShopPrices.at(events, as_ofs)` does the same for several dates in one pass over the events.

**Orders, not lines.** As in `autonomy.profile.facts`, purchases of one customer at the same instant with
the same `order_key` are one order. The roll-up holds only the purchases of the current instant, so it
needs the events in time order, as `EventStore.scan` gives them; within one instant it also drops a
repeated `event_id`. Given events out of time order, lines of one order that do not arrive together are
counted as separate orders (and repeats as orders), so `training_rows` sorts before it calls `at`.
"""

from __future__ import annotations

import math
from bisect import bisect_left, insort
from collections.abc import Iterable, Iterator, Sequence
from datetime import datetime

from autonomy.contracts import Event, EventType

__all__ = ["MIN_SHOP_ORDERS", "ORDER_ID_ATTRS", "ShopPrices", "order_key"]

ORDER_ID_ATTRS = ("order_id", "invoice_no")
"""Attrs that name the order a purchase line belongs to, first match wins (`invoice_no` is what the
online-retail loaders set)."""


def order_key(event: Event) -> str:
    """The order a purchase belongs to among the customer's purchases at the same instant ("" if unnamed)."""
    for name in ORDER_ID_ATTRS:
        value = event.attrs.get(name)
        if isinstance(value, bool) or value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _orders(events: Iterable[Event]) -> Iterator[tuple[datetime, float]]:
    """(instant, value) of each order in a time-ordered stream: lines of one order summed."""
    pending: dict[tuple[str, str], float] = {}
    seen: set[str] = set()
    instant: datetime | None = None
    for e in events:
        if e.type is not EventType.PURCHASE:
            continue
        if instant is not None and e.ts != instant:
            yield from ((instant, v) for v in pending.values())
            pending.clear()
            seen.clear()
        instant = e.ts
        if e.event_id in seen:
            continue
        seen.add(e.event_id)
        key = (e.customer_id, order_key(e))
        value = e.value if e.value is not None and math.isfinite(e.value) else 0.0
        pending[key] = pending.get(key, 0.0) + value
    if instant is not None:
        yield from ((instant, v) for v in pending.values())


MIN_SHOP_ORDERS = 50
"""Orders the shop needs before a price level means anything; below it `known` is false."""

_BINS_PER_NEPER = 8.0
"""Histogram resolution: bin width is a factor of e**(1/8), about 13%."""


class ShopPrices:
    """A histogram of order values. See the module docstring."""

    def __init__(self) -> None:
        self._counts: dict[int, int] = {}
        self._bins: list[int] = []
        self._before: list[int] = []
        self.orders = 0

    def add(self, value: float) -> None:
        """Count one order. A value that is not a positive finite number is ignored."""
        if not math.isfinite(value) or value <= 0.0:
            return
        b = math.floor(math.log(value) * _BINS_PER_NEPER)
        if b not in self._counts:
            insort(self._bins, b)
            self._counts[b] = 0
        self._counts[b] += 1
        self.orders += 1
        self._before = []

    @property
    def known(self) -> bool:
        return self.orders >= MIN_SHOP_ORDERS

    def rank(self, value: float) -> float:
        """The share of the shop's orders worth no more than `value`, in [0, 1]; 0.5 if unknown.

        Within the bin that holds `value`, half its orders are counted, so the answer moves smoothly.
        """
        if not self.known or not math.isfinite(value) or value <= 0.0:
            return 0.5
        if not self._before:
            run = 0
            for b in self._bins:
                self._before.append(run)
                run += self._counts[b]
        b = math.floor(math.log(value) * _BINS_PER_NEPER)
        i = bisect_left(self._bins, b)
        below = self._before[i] if i < len(self._bins) else self.orders
        here = self._counts[b] if i < len(self._bins) and self._bins[i] == b else 0
        return min(max((below + here / 2.0) / self.orders, 0.0), 1.0)

    @classmethod
    def until(cls, events: Iterable[Event], as_of: datetime) -> ShopPrices:
        """The shop's order values from purchases at or before `as_of` (events in time order)."""
        shop = cls()
        for _, value in _orders(e for e in events if e.ts <= as_of):
            shop.add(value)
        return shop

    @classmethod
    def at(cls, events: Iterable[Event], as_ofs: Sequence[datetime]) -> dict[datetime, ShopPrices]:
        """One `ShopPrices` per date, from one pass over `events` (in time order)."""
        dates = sorted(set(as_ofs))
        slices = [cls() for _ in dates]
        for ts, value in _orders(events):
            i = bisect_left(dates, ts)  # the first date at or after the order
            if i < len(dates):
                slices[i].add(value)
        out: dict[datetime, ShopPrices] = {}
        running = cls()
        for d, piece in zip(dates, slices, strict=True):
            running._merge(piece)
            out[d] = running.copy()
        return out

    def _merge(self, other: ShopPrices) -> None:
        for b, n in other._counts.items():
            if b not in self._counts:
                insort(self._bins, b)
                self._counts[b] = 0
            self._counts[b] += n
        self.orders += other.orders
        self._before = []

    def copy(self) -> ShopPrices:
        twin = ShopPrices()
        twin._merge(self)
        return twin
