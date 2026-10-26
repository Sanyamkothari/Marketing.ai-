"""What one customer's history says, read once and in time order.

`gather` reads a customer's events a single time and keeps only the small lists the features need
(orders, refunds, views, carts, per-channel counts). Each registry feature is then a short function of
a `Facts`. This keeps the cost per customer at one pass over the events, not one per feature.

**Point in time.** `gather(events, as_of)` drops every event with `ts > as_of` before anything else is
read. An event stamped exactly at `as_of` is kept, as it is everywhere in the runtime (`e.ts <= now`).
This is the only place events are read, so it is the only place that has to be right.

Conventions follow `autonomy.data.folder`:

- time zone: the last valid `timezone` attr on an event the customer made themselves (signup, visit,
  view, cart, purchase), else UTC. Hours, weekdays and month days are the customer's local ones;
- orders: purchases of one customer at the same instant that name the same order
  (`attrs["order_id"]`, else `attrs["invoice_no"]`, else nothing) are one order, their values and
  margins summed. A log with one PURCHASE per invoice line (`ONLINE_RETAIL_LINE_MAPPING`) is read as
  one order per invoice, not one per line (`autonomy.profile.shop.order_key`);
- amounts: `Event.value` in the business currency. A purchase adds, a refund subtracts its size
  (`abs(value)`, as the event mapper does), so a refund stored as a negative number still subtracts. A
  value that is not a finite number counts as 0. Currencies are never converted here;
- an offer: a purchase whose `attrs["offer_id"]` is set (not empty, not False). The offer named
  "none" is the agent's way of saying "no offer", so it does not count (the spend ledger agrees);
- engagement is a click or a reply. Opens are counted but never mean engagement: mail privacy proxies
  open messages on the reader's behalf.

Pure and deterministic: no clock reads, no randomness, no I/O. Events may arrive in any order and may
repeat an `event_id` (the first copy is used; `training_rows` and the stores drop repeats the same way).
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import cached_property, lru_cache
from zoneinfo import ZoneInfo

from autonomy.contracts import NO_OFFER, Channel, Event, EventType
from autonomy.profile.shop import ShopPrices, order_key

__all__ = ["ChannelCounts", "Facts", "Order", "gather"]

SECONDS_PER_DAY = 86_400.0

_SELF_MADE = frozenset(
    {EventType.SIGNUP, EventType.VISIT, EventType.VIEW, EventType.CART, EventType.PURCHASE}
)
"""Events the customer made themselves: only these tell us where they live."""

_SEEN = _SELF_MADE
"""Events that show the customer was around (the folder's `last_seen_at`)."""

SNOOZE_KEY = "snooze_until"
"""The preference a "not now" reply leaves behind (`autonomy.agents.conversation`)."""

STOP_REASON = "reply_stop"
"""The reason on the UNSUBSCRIBE that a "stop" reply leaves behind."""


@lru_cache(maxsize=512)
def _zone(name: str) -> ZoneInfo | None:
    """The zone for an IANA name, or None if the name is not one. Cached: lookups hit the disk."""
    try:
        return ZoneInfo(name)
    except (KeyError, ValueError, OSError):
        return None


def days_between(earlier: datetime, later: datetime) -> float:
    """Whole and fractional days from `earlier` to `later`."""
    return (later - earlier).total_seconds() / SECONDS_PER_DAY


@dataclass(frozen=True, slots=True)
class Order:
    """One order: one purchase event, or the purchase lines that share an instant and an order id."""

    ts: datetime
    value: float
    margin: float | None
    """The summed margin, or None unless every line reports one."""
    categories: tuple[str, ...]
    """The distinct categories of its lines, sorted (empty if no line is tagged)."""
    items: tuple[str, ...]
    """The distinct items of its lines, sorted (empty if no line is tagged)."""
    offered: bool
    """Whether any line had an offer applied."""


def _order(lines: list[Event]) -> Order:
    """One `Order` from its purchase lines (all at the same instant)."""
    margins = [e.margin for e in lines]
    known = [m for m in margins if m is not None and math.isfinite(m)]
    return Order(
        ts=lines[0].ts,
        value=sum(_amount(e.value) for e in lines),
        margin=sum(known) if len(known) == len(margins) else None,
        categories=tuple(sorted({e.category for e in lines if e.category})),
        items=tuple(sorted({e.item_id for e in lines if e.item_id})),
        offered=any(_has_offer(e) for e in lines),
    )


@dataclass(slots=True)
class ChannelCounts:
    """What happened on one channel, up to `as_of`."""

    sends: int = 0
    delivers: int = 0
    opens: int = 0
    clicks: int = 0
    replies: int = 0
    bounces: int = 0
    complaints: int = 0
    blocks: int = 0
    address_known: bool = False
    consent: bool = False
    unsubscribed: bool = False

    @property
    def contactable(self) -> bool:
        return self.address_known and self.consent and not self.unsubscribed


def _amount(value: float | None) -> float:
    return value if value is not None and math.isfinite(value) else 0.0


def _has_offer(event: Event) -> bool:
    value = event.attrs.get("offer_id")
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return bool(value.strip()) and value.strip() != NO_OFFER.offer_id
    return True


def _text(event: Event, key: str) -> str | None:
    value = event.attrs.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


NO_END = datetime.max.replace(tzinfo=UTC)
"""A snooze whose end cannot be read: when we cannot tell, the customer asked us to wait."""


def _snooze_end(raw: str, zone: ZoneInfo | None) -> datetime | None:
    """When a `snooze_until` value ends, read as `autonomy.decide.rules.snooze_until` reads it.

    Empty means no snooze; a value that cannot be read is a snooze with no end; a time without a zone
    is on the customer's own clock.
    """
    text = raw.strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return NO_END
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=zone or UTC)


@dataclass
class Facts:
    """One customer's history up to `as_of`. Build it with `gather`."""

    customer_id: str
    as_of: datetime
    shop: ShopPrices | None = None
    timezone: str = "UTC"
    first_ts: datetime | None = None
    last_seen_ts: datetime | None = None
    orders: list[Order] = field(default_factory=list)
    refunds: list[tuple[datetime, float]] = field(default_factory=list)
    views: list[tuple[datetime, str | None]] = field(default_factory=list)
    carts: list[datetime] = field(default_factory=list)
    visits: list[datetime] = field(default_factory=list)
    sends: list[datetime] = field(default_factory=list)
    engagements: list[datetime] = field(default_factory=list)
    channels: dict[Channel, ChannelCounts] = field(default_factory=dict)
    preferences: dict[str, str] = field(default_factory=dict)
    snoozed_until: datetime | None = None
    stop_reply_ids: set[str] = field(default_factory=set)
    """The replies behind `reply_stop` unsubscribes: one stop reply unsubscribes every channel."""
    not_now_replies: int = 0
    replies: int = 0

    # --- derived once, used by many features -----------------------------------------------------

    @cached_property
    def zone(self) -> ZoneInfo | None:
        return _zone(self.timezone)

    def local(self, ts: datetime) -> datetime:
        zone = self.zone
        return ts.astimezone(zone) if zone is not None else ts.astimezone(UTC)

    @cached_property
    def order_gaps(self) -> list[float]:
        """Days between consecutive orders, oldest first."""
        o = self.orders
        return [days_between(o[i - 1].ts, o[i].ts) for i in range(1, len(o))]

    @cached_property
    def mean_gap(self) -> float:
        """The usual number of days between orders; 0 when there are fewer than two orders."""
        gaps = self.order_gaps
        return sum(gaps) / len(gaps) if gaps else 0.0

    @cached_property
    def gap_known(self) -> bool:
        """Whether the customer ordered on at least two different local days, so a gap means something."""
        return len({self.local(o.ts).date() for o in self.orders}) >= 2 and self.mean_gap > 0

    @property
    def stop_replies(self) -> int:
        return len(self.stop_reply_ids)

    @cached_property
    def last_order_ts(self) -> datetime | None:
        return self.orders[-1].ts if self.orders else None

    @cached_property
    def days_since_last_order(self) -> float:
        last = self.last_order_ts
        return max(days_between(last, self.as_of), 0.0) if last is not None else 0.0

    def since(self, days: float) -> datetime:
        return self.as_of - timedelta(days=days)

    def orders_in(self, newer_than: datetime, not_after: datetime | None = None) -> list[Order]:
        """Orders after `newer_than` and, if given, at or before `not_after`."""
        cap = self.as_of if not_after is None else not_after
        return [o for o in self.orders if newer_than < o.ts <= cap]

    @cached_property
    def gross_spend(self) -> float:
        return sum(o.value for o in self.orders)

    @cached_property
    def refunded(self) -> float:
        """Money refunded, as a positive amount."""
        return sum(v for _, v in self.refunds)

    @cached_property
    def mean_order_value(self) -> float:
        return self.gross_spend / len(self.orders) if self.orders else 0.0

    @cached_property
    def engagement_hours(self) -> list[int]:
        """Local hour (0-23) of each click and reply."""
        return [self.local(ts).hour for ts in self.engagements]

    @cached_property
    def last_engaged_ts(self) -> datetime | None:
        return self.engagements[-1] if self.engagements else None

    def total(self, name: str) -> int:
        """The sum over channels of one `ChannelCounts` field."""
        return sum(getattr(c, name) for c in self.channels.values())


def gather(events: Iterable[Event], as_of: datetime, shop: ShopPrices | None = None) -> Facts:
    """Read one customer's events up to and including `as_of` into a `Facts`.

    `shop` is the shop's order values up to `as_of` (see `ShopPrices`), for the price level.

    Raises `ValueError` if `as_of` has no time zone or if the events belong to more than one customer.
    """
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    kept: dict[str, Event] = {}
    for e in events:
        if e.ts <= as_of:
            kept.setdefault(e.event_id, e)
    ordered = sorted(kept.values(), key=lambda e: (e.ts, e.event_id))
    customers = {e.customer_id for e in ordered}
    if len(customers) > 1:
        raise ValueError(f"profile_for takes one customer's events, got {sorted(customers)[:3]}")
    facts = Facts(customer_id=next(iter(customers), ""), as_of=as_of, shop=shop)
    channels = facts.channels
    lines: dict[tuple[datetime, str], list[Event]] = {}
    snooze: str | None = None
    for e in ordered:
        t = e.type
        if facts.first_ts is None:
            facts.first_ts = e.ts
        if t in _SELF_MADE:
            tz = _text(e, "timezone")
            if tz is not None and _zone(tz) is not None:
                facts.timezone = tz
        if t in _SEEN:
            facts.last_seen_ts = e.ts
        if t is EventType.PURCHASE:
            lines.setdefault((e.ts, order_key(e)), []).append(e)
        elif t is EventType.REFUND:
            facts.refunds.append((e.ts, abs(_amount(e.value))))
        elif t is EventType.VIEW:
            facts.views.append((e.ts, e.item_id or None))
        elif t is EventType.CART:
            facts.carts.append(e.ts)
        elif t is EventType.VISIT:
            facts.visits.append(e.ts)
        elif t is EventType.PREFERENCE:
            key, value = e.attrs.get("key"), e.attrs.get("value")
            if isinstance(key, str) and value is not None:
                facts.preferences[key] = str(value)
                if key == SNOOZE_KEY:
                    snooze = str(value)
                    facts.not_now_replies += bool(snooze.strip())  # an empty value clears a snooze
        elif e.channel is not None:
            _channel_event(facts, channels.setdefault(e.channel, ChannelCounts()), e)
    facts.orders = [_order(group) for group in lines.values()]  # inserted in time order
    if snooze is not None:
        facts.snoozed_until = _snooze_end(snooze, facts.zone)
    return facts


def _channel_event(facts: Facts, c: ChannelCounts, e: Event) -> None:
    t = e.type
    if t is EventType.SEND:
        c.sends += 1
        facts.sends.append(e.ts)
    elif t is EventType.DELIVER:
        c.delivers += 1
    elif t is EventType.OPEN:
        c.opens += 1
    elif t is EventType.CLICK:
        c.clicks += 1
        facts.engagements.append(e.ts)
    elif t is EventType.REPLY:
        c.replies += 1
        facts.replies += 1
        facts.engagements.append(e.ts)
    elif t is EventType.BOUNCE:
        c.bounces += 1
    elif t is EventType.BLOCK:
        c.blocks += 1
    elif t is EventType.UNSUBSCRIBE:
        c.unsubscribed = True
        if _text(e, "reason") == STOP_REASON:
            reply = e.attrs.get("reply_event_id")
            facts.stop_reply_ids.add(reply if isinstance(reply, str) and reply else e.event_id)
    elif t is EventType.COMPLAINT:
        c.complaints += 1
        c.unsubscribed = True
    elif t is EventType.CONSENT_GRANTED:
        c.consent = True
        c.address_known = True
        c.unsubscribed = False
    elif t is EventType.CONSENT_REVOKED:
        c.consent = False
