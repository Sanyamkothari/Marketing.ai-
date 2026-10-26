"""The shop's own order values up to a date, so a customer's price level is read against the shop.

"Premium or budget" only means something inside one shop: 40 is a big basket in one and a small one
in another. `ShopPrices` keeps the shop's order values in a histogram with log-spaced bins (about 12%
wide), so memory stays a few hundred counters however many orders there are, and answers one
question: `rank(v)`, the share of the shop's orders worth no more than `v`.

Point in time: `ShopPrices.until(events, as_of)` adds only purchases with `ts <= as_of`;
`ShopPrices.at(events, as_ofs)` does the same for several dates in one pass over the events.
"""

from __future__ import annotations

import math
from bisect import bisect_left, insort
from collections.abc import Iterable, Sequence
from datetime import datetime

from autonomy.contracts import Event, EventType

__all__ = ["MIN_SHOP_ORDERS", "ShopPrices"]

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
        """The shop's order values from purchases at or before `as_of`."""
        shop = cls()
        for e in events:
            if e.type is EventType.PURCHASE and e.value is not None and e.ts <= as_of:
                shop.add(e.value)
        return shop

    @classmethod
    def at(cls, events: Iterable[Event], as_ofs: Sequence[datetime]) -> dict[datetime, ShopPrices]:
        """One `ShopPrices` per date, from one pass over `events` (any order)."""
        dates = sorted(set(as_ofs))
        slices = [cls() for _ in dates]
        for e in events:
            if e.type is not EventType.PURCHASE or e.value is None:
                continue
            i = bisect_left(dates, e.ts)  # the first date at or after the purchase
            if i < len(dates):
                slices[i].add(e.value)
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
