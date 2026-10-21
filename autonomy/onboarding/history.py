"""One pass over a shop's events and customer states: what onboarding needs to know about its past.

`ShopHistory.build(events, states, now)` keeps, per customer, the purchases (time, value, margin,
item, discount), the messages they received, their cart and product-view moments, and whether we
may contact them now. Opportunities (`opportunities.py`) and learned limits (`limits.py`) read only
this, so the store is scanned once however many proposals are sized. Events after `now` are ignored.

What "a message they received" means: a `DELIVER` event (Klaviyo's Received Email / Received SMS, or
our own channel's delivery receipt). Where a source reports sends but no deliveries, its `SEND`s are
counted instead, so a shop is never read as silent because one tool lacks delivery receipts. Each is
marked as ours (it carries a decision id) or the shop's own (their campaigns and flows).
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from autonomy.contracts import Channel, CustomerState, Event, EventType

__all__ = [
    "MESSAGE_CHANNELS",
    "Delivery",
    "Purchase",
    "ShopHistory",
    "quantile",
]

MESSAGE_CHANNELS: tuple[Channel, ...] = (Channel.EMAIL, Channel.SMS)
"""The channels a connected shop sends through (its Klaviyo): onboarding sizes reach on these."""


@dataclass(frozen=True)
class Purchase:
    ts: datetime
    value: float
    margin: float | None
    item_id: str | None
    category: str | None
    discount: float | None
    """Money taken off the order (Shopify `total_discounts`, or `offer_cost` on an offer purchase);
    None when the source does not say."""
    with_offer: bool
    """Whether the order used a discount code (`attrs.offer_id`)."""


@dataclass(frozen=True)
class Delivery:
    ts: datetime
    channel: Channel
    ours: bool


def quantile(values: Sequence[float], q: float) -> float:
    """The q-quantile by linear interpolation (values need not be sorted); NaN for no values."""
    if not values:
        return math.nan
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo = math.floor(pos)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        if isinstance(value, str):
            try:
                parsed = float(value)
            except ValueError:
                return None
            return parsed if math.isfinite(parsed) else None
        return None
    return float(value) if math.isfinite(float(value)) else None


def _discount(e: Event) -> float | None:
    """Money taken off an order: Shopify's `discounts`, else the offer's cost an offer purchase carries."""
    for key in ("discounts", "offer_cost"):
        amount = _number(e.attrs.get(key))
        if amount is not None:
            return amount
    return None


@dataclass
class ShopHistory:
    """What happened in one shop up to `now`, per customer. Build it with `ShopHistory.build`."""

    now: datetime
    purchases: dict[str, list[Purchase]] = field(default_factory=dict)
    deliveries: dict[str, list[Delivery]] = field(default_factory=dict)
    carts: dict[str, list[datetime]] = field(default_factory=dict)
    views: dict[str, list[datetime]] = field(default_factory=dict)
    states: dict[str, CustomerState] = field(default_factory=dict)
    sources: set[str] = field(default_factory=set)
    currencies: dict[str, int] = field(default_factory=dict)
    """Purchases per currency code their source reported (Shopify's `attrs.currency`)."""

    @classmethod
    def build(cls, events: Iterable[Event], states: Iterable[CustomerState], now: datetime) -> ShopHistory:
        purchases: dict[str, list[Purchase]] = defaultdict(list)
        delivered: dict[str, list[Delivery]] = defaultdict(list)
        sent: dict[str, list[Delivery]] = defaultdict(list)
        sources_with_delivers: set[tuple[str, Channel]] = set()
        send_sources: dict[str, list[tuple[str, Channel]]] = defaultdict(list)
        carts: dict[str, list[datetime]] = defaultdict(list)
        views: dict[str, list[datetime]] = defaultdict(list)
        sources: set[str] = set()
        currencies: dict[str, int] = defaultdict(int)
        for e in events:
            if e.ts > now:
                continue
            sources.add(e.source)
            if e.type is EventType.PURCHASE and e.value is not None:
                code = e.attrs.get("currency")
                if isinstance(code, str) and len(code) == 3:
                    currencies[code.upper()] += 1
                purchases[e.customer_id].append(
                    Purchase(
                        ts=e.ts,
                        value=float(e.value),
                        margin=e.margin,
                        item_id=e.item_id,
                        category=e.category,
                        discount=_discount(e),
                        with_offer=bool(e.attrs.get("offer_id")),
                    )
                )
            elif e.type is EventType.DELIVER and e.channel is not None:
                delivered[e.customer_id].append(Delivery(e.ts, e.channel, e.decision_id is not None))
                sources_with_delivers.add((e.source, e.channel))
            elif e.type is EventType.SEND and e.channel is not None:
                sent[e.customer_id].append(Delivery(e.ts, e.channel, e.decision_id is not None))
                send_sources[e.customer_id].append((e.source, e.channel))
            elif e.type is EventType.CART:
                carts[e.customer_id].append(e.ts)
            elif e.type is EventType.VIEW:
                views[e.customer_id].append(e.ts)
        # A send counts as received only when its source reports no deliveries on that channel.
        for customer, rows in sent.items():
            keyed = send_sources[customer]
            for d, key in zip(rows, keyed, strict=True):
                if key not in sources_with_delivers:
                    delivered[customer].append(d)
        return cls(
            now=now,
            purchases={c: sorted(p, key=lambda x: x.ts) for c, p in purchases.items()},
            deliveries={c: sorted(d, key=lambda x: x.ts) for c, d in delivered.items()},
            carts={c: sorted(t) for c, t in carts.items()},
            views={c: sorted(t) for c, t in views.items()},
            states={s.customer_id: s for s in states},
            sources=sources,
            currencies=dict(currencies),
        )

    def currency(self, default: str = "USD") -> str:
        """The currency most purchases were reported in; `default` when no source said."""
        if not self.currencies:
            return default
        return max(sorted(self.currencies), key=lambda c: self.currencies[c])

    # --- who --------------------------------------------------------------------------------------

    def customers(self) -> list[str]:
        return sorted(set(self.purchases) | set(self.states))

    def reachable_channel(self, customer_id: str) -> Channel | None:
        """The channel we would message this customer on now (email first), or None."""
        state = self.states.get(customer_id)
        if state is None or state.open_complaint:
            return None
        for channel in MESSAGE_CHANNELS:
            stats = state.channels.get(channel)
            if stats is not None and stats.contactable:
                return channel
        return None

    # --- purchases --------------------------------------------------------------------------------

    def bought_by(self, customer_id: str, at: datetime) -> list[Purchase]:
        return [p for p in self.purchases.get(customer_id, ()) if p.ts <= at]

    def bought_between(self, customer_id: str, after: datetime, until: datetime) -> bool:
        return any(after < p.ts <= until for p in self.purchases.get(customer_id, ()))

    def all_purchases(self) -> list[Purchase]:
        return [p for rows in self.purchases.values() for p in rows]

    def first_purchase_at(self) -> datetime | None:
        firsts = [rows[0].ts for rows in self.purchases.values() if rows]
        return min(firsts) if firsts else None

    def history_days(self) -> float:
        first = self.first_purchase_at()
        return 0.0 if first is None else (self.now - first).total_seconds() / 86_400.0

    def buyers(self) -> int:
        return sum(1 for rows in self.purchases.values() if rows)

    def order_value(self, customer_id: str) -> float | None:
        rows = self.purchases.get(customer_id)
        return statistics.fmean(p.value for p in rows) if rows else None

    def mean_order_value(self) -> float | None:
        values = [p.value for p in self.all_purchases()]
        return statistics.fmean(values) if values else None

    def second_purchase_gaps(self) -> list[float]:
        """Days from each customer's first purchase to their second (distinct days only)."""
        gaps: list[float] = []
        for rows in self.purchases.values():
            days = sorted({p.ts.date() for p in rows})
            if len(days) >= 2:
                gaps.append(float((days[1] - days[0]).days))
        return gaps

    # --- messages ---------------------------------------------------------------------------------

    def received_since(self, since: datetime) -> Mapping[str, list[Delivery]]:
        return {c: kept for c, rows in self.deliveries.items() if (kept := [d for d in rows if d.ts > since])}

    def engagement_hours(self) -> list[int]:
        """24 counts of opens and clicks by the customer's local hour, summed over every state."""
        total = [0] * 24
        for state in self.states.values():
            for hour, n in enumerate(state.hour_counts[:24]):
                total[hour] += n
        return total

    def window(self, days: float) -> datetime:
        return self.now - timedelta(days=days)
