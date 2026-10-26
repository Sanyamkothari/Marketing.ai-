"""The production `StateFolder`: events in, one `CustomerState` per customer out.

It starts from `autonomy.fakes.folder.SimpleStateFolder` and agrees with it on every field the fake
computes, for events folded in time order (the tests hold it to that). On top it learns:

- `timezone` and `country` from the `timezone` / `country` attrs of any event the customer made
  themselves (signup, visit, view, cart, purchase), so `active_hours` is counted in the customer's
  own local hours, not UTC. Purchases count because order exports carry them there: the
  online-retail loaders put `country` on every invoice and have no signup event;
- `offer_sensitivity`: the share of purchases made with an offer (`attrs["offer_id"]`), shrunk toward
  a prior of 0.3 with a pseudo-count of 3, so one discounted order does not brand someone a deal hunter;
- moments (plan-v3 decision 2): `last_view_at`, `last_cart_at`, a bounded record of products viewed and
  not bought since (`viewed_items`, at most `MAX_VIEWED_ITEMS`), their usual gap between days of
  activity and between purchases, and the latest return after a gap much longer than usual
  (`last_return_at`). `autonomy.decide.moments` reads them. A purchase closes the cart and drops
  the bought product from `viewed_items`;
- `segment`: a plain RFM label (see `SegmentRules`), recomputed in `refresh()` because recency moves
  with the clock even when nothing happens.

Channel responsiveness lives in each channel's `ChannelStats` counts (sends, delivers, clicks,
replies, bounces, complaints), exactly as the fake keeps them.

Two deliberate differences from the fake, both invisible when events arrive in time order: the
"last ..." timestamps take the later of the old and new value, and `first_seen_at` the earlier, so a
late webhook cannot move them backwards. Opens are not counted as engagement: mail privacy proxies
open messages on the reader's behalf, so an open says little about the person.

Pure and deterministic: no clock reads, no randomness, no I/O.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

from autonomy.contracts import Channel, ChannelStats, CustomerState, Event, EventType, ItemViews

CONTACT_MEMORY = timedelta(days=30)
"""How far back `recent_contacts` reaches; it feeds contacts_7d and contacts_30d."""

OFFER_PRIOR = 0.3
"""Where offer_sensitivity starts: most shoppers buy at full price most of the time."""

OFFER_PSEUDO_COUNT = 3.0
"""How many purchases' worth of weight the prior carries."""

VIP_MIN_BUYERS = 50
"""Buyers the shop needs before its own VIP bars replace the fixed defaults."""

MAX_VIEWED_ITEMS = 8
"""How many viewed-but-not-bought products a customer's state keeps; the least recently viewed go first."""

GAP_SMOOTHING = 0.3
"""Weight of the newest gap in the running averages `usual_gap_days` and `purchase_gap_days`."""

RETURN_MIN_DAYS = 14.0
RETURN_RATIO = 3.0
"""A visit, view or cart counts as a return after a long absence when the gap before it is at least
`RETURN_MIN_DAYS` and at least `RETURN_RATIO` times the customer's usual gap."""

_RETURN_TYPES = frozenset({EventType.VISIT, EventType.VIEW, EventType.CART})

_SEEN_TYPES = frozenset(
    {EventType.SIGNUP, EventType.VISIT, EventType.VIEW, EventType.CART, EventType.PURCHASE}
)


@dataclass(frozen=True)
class SegmentRules:
    """The thresholds behind `CustomerState.segment`, checked in this order.

    1. no purchases yet                                  -> "prospect"
    2. last purchase more than `lapsed_after` ago        -> "lapsed"
    3. last purchase more than `lapsing_after` ago       -> "lapsing"
    4. at least `vip_min_purchases` purchases AND at
       least `vip_min_revenue` net revenue               -> "vip"
    5. at most `new_max_purchases` purchases             -> "new"
    6. otherwise                                         -> "active"

    Recency comes first on purpose: a big spender who has gone quiet is someone to win back, not
    someone to treat as a loyal regular. The bounds are strict ("more than 90 days"), so a customer
    whose last order was exactly 90 days ago is still active.

    The VIP bars are the shop's own, not fixed amounts: `fitted(states)` puts the revenue bar at the
    top `vip_share` of its buyers' revenue and the purchase bar at their median number of purchases
    (at least 2, at most `vip_min_purchases`), so a VIP is a VIP in roubles, yen or dollars, and in a
    shop where people buy weekly or yearly. A flat 500 made 88.5% of X5's rouble clients VIP. Until
    the rules are fitted (fewer than `VIP_MIN_BUYERS` buyers, or no states yet) the fixed defaults
    below apply.
    """

    lapsing_after: timedelta = timedelta(days=90)
    lapsed_after: timedelta = timedelta(days=180)
    vip_min_purchases: int = 5
    vip_min_revenue: float = 500.0
    new_max_purchases: int = 1
    vip_share: float = 0.1
    """The share of the shop's buyers (by revenue) the fitted revenue bar keeps above it."""

    def __post_init__(self) -> None:
        if self.lapsed_after < self.lapsing_after:
            raise ValueError("lapsed_after must be at least lapsing_after")
        if not 0.0 < self.vip_share < 1.0:
            raise ValueError("vip_share is a share, between 0 and 1")

    def fitted(self, states: Iterable[CustomerState]) -> SegmentRules:
        """These rules with the VIP bars set from the shop's own buyers (see the class docstring);
        unchanged when fewer than `VIP_MIN_BUYERS` customers have bought."""
        revenue: list[float] = []
        purchases: list[int] = []
        for st in states:
            if st.purchases > 0:
                revenue.append(st.revenue)
                purchases.append(st.purchases)
        if len(revenue) < VIP_MIN_BUYERS:
            return self
        revenue.sort()
        purchases.sort()
        bar = revenue[min(len(revenue) - 1, int(len(revenue) * (1.0 - self.vip_share)))]
        median = purchases[len(purchases) // 2]
        return replace(
            self,
            vip_min_revenue=max(bar, 0.0),
            vip_min_purchases=min(max(2, median), self.vip_min_purchases),
        )

    def label(self, state: CustomerState, now: datetime) -> str:
        if state.purchases <= 0 or state.last_purchase_at is None:
            return "prospect"
        since = now - state.last_purchase_at
        if since > self.lapsed_after:
            return "lapsed"
        if since > self.lapsing_after:
            return "lapsing"
        if state.purchases >= self.vip_min_purchases and state.revenue >= self.vip_min_revenue:
            return "vip"
        if state.purchases <= self.new_max_purchases:
            return "new"
        return "active"


@lru_cache(maxsize=512)
def _zone(name: str) -> ZoneInfo | None:
    """The zone for an IANA name, or None if the name is not one. Cached: lookups hit the disk."""
    try:
        return ZoneInfo(name)
    except (KeyError, ValueError, OSError):
        return None


def _local_hour(ts: datetime, tz: str) -> int:
    zone = _zone(tz)
    return ts.astimezone(zone).hour if zone is not None else ts.hour


def _offset_hours(ts: datetime, tz: str) -> int:
    zone = _zone(tz)
    if zone is None:
        return 0
    offset = ts.astimezone(zone).utcoffset()
    return round(offset.total_seconds() / 3600) if offset is not None else 0


def _later(a: datetime | None, b: datetime) -> datetime:
    return b if a is None or b > a else a


def _earlier(a: datetime | None, b: datetime) -> datetime:
    return b if a is None or b < a else a


def _text_attr(event: Event, key: str) -> str | None:
    value = event.attrs.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _has_offer(event: Event) -> bool:
    value = event.attrs.get("offer_id")
    if value is None or value is False:
        return False
    return not (isinstance(value, str) and not value.strip())


class StateFolder:
    """Folds one event at a time into a customer's state. See the module docstring for what it learns."""

    def __init__(
        self,
        *,
        segments: SegmentRules | None = None,
        offer_prior: float = OFFER_PRIOR,
        offer_pseudo_count: float = OFFER_PSEUDO_COUNT,
    ) -> None:
        if not 0.0 <= offer_prior <= 1.0:
            raise ValueError("offer_prior is a share, between 0 and 1")
        if offer_pseudo_count <= 0:
            raise ValueError("offer_pseudo_count must be positive")
        self.segments = segments or SegmentRules()
        self._base_segments = self.segments
        self._fit_vip = segments is None
        """Only the default rules follow the shop's own buyers: bars passed in are the operator's."""
        self.offer_prior = offer_prior
        self.offer_pseudo_count = offer_pseudo_count

    def fit_segments(self, states: Iterable[CustomerState]) -> SegmentRules:
        """Set the VIP bars from the shop's own customers (`SegmentRules.fitted`), always starting
        from the rules this folder was built with. Labels change at each state's next `refresh`.
        The runtime calls it after every full rebuild. Rules passed to the folder explicitly are
        kept as given (someone chose those bars). Returns the rules now in use."""
        if self._fit_vip:
            self.segments = self._base_segments.fitted(states)
        return self.segments

    # --- the protocol -------------------------------------------------------------------------

    def fold(self, state: CustomerState | None, event: Event) -> CustomerState:
        if state is not None and state.customer_id != event.customer_id:
            raise ValueError(f"event for {event.customer_id!r} folded into state of {state.customer_id!r}")
        s = state or CustomerState(customer_id=event.customer_id, as_of=event.ts, first_seen_at=event.ts)
        as_of = max(s.as_of, event.ts)
        upd: dict[str, object] = {
            "as_of": as_of,
            "version": s.version + 1,
            "first_seen_at": _earlier(s.first_seen_at, event.ts),
        }
        channels: dict[Channel, ChannelStats] = dict(s.channels)
        ch = event.channel
        stats = channels.get(ch, ChannelStats()) if ch else None
        t = event.type

        if t in _SEEN_TYPES:
            upd["last_seen_at"] = _later(s.last_seen_at, event.ts)
            upd.update(self._profile(s, event))
            upd.update(self._activity(s, event))

        if t is EventType.PURCHASE:
            upd.update(self._purchase(s, event))
        elif t is EventType.REFUND:
            upd["revenue"] = s.revenue - (event.value or 0.0)
        elif t is EventType.SEND and stats is not None:
            stats = stats.model_copy(
                update={"sends": stats.sends + 1, "last_send_at": _later(stats.last_send_at, event.ts)}
            )
            window_start = event.ts - CONTACT_MEMORY
            upd["recent_contacts"] = tuple(
                sorted(x for x in (*s.recent_contacts, event.ts) if x > window_start)
            )
            upd["last_contact_at"] = _later(s.last_contact_at, event.ts)
        elif t is EventType.DELIVER and stats is not None:
            stats = stats.model_copy(update={"delivers": stats.delivers + 1})
        elif t in (EventType.CLICK, EventType.REPLY) and stats is not None:
            field = "clicks" if t is EventType.CLICK else "replies"
            stats = stats.model_copy(
                update={
                    field: getattr(stats, field) + 1,
                    "last_engaged_at": _later(stats.last_engaged_at, event.ts),
                }
            )
            counts = list(s.hour_counts or (0,) * 24)
            counts[_local_hour(event.ts, s.timezone)] += 1
            upd.update(_hours(counts))
        elif t is EventType.BOUNCE and stats is not None:
            stats = stats.model_copy(update={"bounces": stats.bounces + 1})
        elif t is EventType.UNSUBSCRIBE and stats is not None:
            stats = stats.model_copy(update={"unsubscribed": True})
        elif t is EventType.COMPLAINT and stats is not None:
            stats = stats.model_copy(update={"complaints": stats.complaints + 1, "unsubscribed": True})
            upd["open_complaint"] = True
        elif t is EventType.CONSENT_GRANTED and stats is not None:
            stats = stats.model_copy(update={"consent": True, "address_known": True, "unsubscribed": False})
        elif t is EventType.CONSENT_REVOKED and stats is not None:
            stats = stats.model_copy(update={"consent": False})
        elif t is EventType.PREFERENCE:
            key, value = event.attrs.get("key"), event.attrs.get("value")
            if isinstance(key, str) and value is not None:
                upd["preferences"] = {**s.preferences, key: str(value)}

        if ch is not None and stats is not None:
            channels[ch] = stats
            upd["channels"] = channels
        return self.refresh(s.model_copy(update=upd), as_of)

    def refresh(self, state: CustomerState, now: datetime) -> CustomerState:
        """Trim contacts older than 30 days, recount the 7- and 30-day windows, relabel the segment."""
        recent = tuple(x for x in state.recent_contacts if x > now - CONTACT_MEMORY)
        week_start = now - timedelta(days=7)
        return state.model_copy(
            update={
                "recent_contacts": recent,
                "contacts_7d": sum(1 for x in recent if x > week_start),
                "contacts_30d": len(recent),
                "as_of": max(state.as_of, now),
                "segment": self.segments.label(state, now),
            }
        )

    # --- helpers ------------------------------------------------------------------------------

    def offer_purchases(self, state: CustomerState) -> int:
        """How many of this customer's purchases used an offer, read back from `offer_sensitivity`.

        `CustomerState` has no counter for it, but the shrunk estimate is an exact function of the
        count, so it inverts cleanly. A state with purchases but no estimate (built by another
        folder) is taken to have bought at full price.
        """
        if state.offer_sensitivity is None or state.purchases <= 0:
            return 0
        n = state.purchases
        k = round(
            state.offer_sensitivity * (n + self.offer_pseudo_count)
            - self.offer_prior * self.offer_pseudo_count
        )
        return min(max(k, 0), n)

    def _offer_sensitivity(self, offers: int, purchases: int) -> float:
        prior_mass = self.offer_prior * self.offer_pseudo_count
        return (offers + prior_mass) / (purchases + self.offer_pseudo_count)

    def _purchase(self, s: CustomerState, event: Event) -> dict[str, object]:
        purchases = s.purchases + 1
        offers = self.offer_purchases(s) + (1 if _has_offer(event) else 0)
        upd: dict[str, object] = {
            "purchases": purchases,
            "revenue": s.revenue + (event.value or 0.0),
            "margin": s.margin + (event.margin or 0.0),
            "last_purchase_at": _later(s.last_purchase_at, event.ts),
            "offer_sensitivity": self._offer_sensitivity(offers, purchases),
        }
        if event.category:
            counts = dict(s.category_counts)
            counts[event.category] = counts.get(event.category, 0) + 1
            upd["category_counts"] = counts
            upd["top_categories"] = tuple(sorted(counts, key=lambda c: (-counts[c], c))[:3])
        return upd

    def _activity(self, s: CustomerState, event: Event) -> dict[str, object]:
        """Views, carts, gaps and returns from one of the customer's own events.

        A gap is measured from the previous activity to this event and only when the event is the
        newest, so a late webhook moves nothing. Gaps shorter than a day are one visit, not an
        absence, and do not enter the usual gap.
        """
        upd: dict[str, object] = {}
        t, ts = event.type, event.ts
        if t is EventType.VIEW:
            upd["last_view_at"] = _later(s.last_view_at, ts)
            if event.item_id:
                upd["viewed_items"] = _viewed(s.viewed_items, event.item_id, ts)
        elif t is EventType.CART:
            upd["last_cart_at"] = _later(s.last_cart_at, ts)
        elif t is EventType.PURCHASE:
            if event.item_id and event.item_id in s.viewed_items:
                upd["viewed_items"] = {k: v for k, v in s.viewed_items.items() if k != event.item_id}
            before = s.last_purchase_at
            if before is not None and ts > before:
                days = (ts - before).total_seconds() / 86_400.0
                if days >= 1.0:
                    upd["purchase_gap_days"] = _smoothed(s.purchase_gap_days, days)
        previous = s.last_seen_at
        if previous is not None and ts > previous:
            days = (ts - previous).total_seconds() / 86_400.0
            if days >= 1.0:
                usual = s.usual_gap_days
                if t in _RETURN_TYPES and days >= max(RETURN_MIN_DAYS, RETURN_RATIO * (usual or 0.0)):
                    upd["last_return_at"] = _later(s.last_return_at, ts)
                    upd["last_return_gap_days"] = days
                upd["usual_gap_days"] = _smoothed(usual, days)
        return upd

    def _profile(self, s: CustomerState, event: Event) -> dict[str, object]:
        """Timezone and country from the customer's own event. An unknown zone name is ignored, not stored.

        Outbound events (send, click, ...) never set them: their attrs describe our message or the
        provider, not where the customer lives.
        """
        upd: dict[str, object] = {}
        country = _text_attr(event, "country")
        if country is not None:
            upd["country"] = country.upper() if len(country) == 2 else country
        tz = _text_attr(event, "timezone")
        if tz is not None and tz != s.timezone and _zone(tz) is not None:
            upd["timezone"] = tz
            if s.hour_counts:
                # Engagement already counted in the old zone's hours moves to the new zone's hours, so
                # the histogram stays one consistent clock. Whole hours: good enough for send timing.
                shift = _offset_hours(event.ts, tz) - _offset_hours(event.ts, s.timezone)
                if shift % 24:
                    old = s.hour_counts
                    upd.update(_hours([old[(h - shift) % 24] for h in range(24)]))
        return upd


def _smoothed(old: float | None, new: float) -> float:
    """A running average that starts at the first value and then leans `GAP_SMOOTHING` on the newest."""
    return new if old is None else (1.0 - GAP_SMOOTHING) * old + GAP_SMOOTHING * new


def _viewed(items: dict[str, ItemViews], item_id: str, ts: datetime) -> dict[str, ItemViews]:
    """`items` with one more view of `item_id`, trimmed to `MAX_VIEWED_ITEMS` (least recently viewed out)."""
    out = dict(items)
    known = out.get(item_id)
    out[item_id] = (
        ItemViews(count=1, last_at=ts)
        if known is None
        else ItemViews(count=known.count + 1, last_at=_later(known.last_at, ts))
    )
    while len(out) > MAX_VIEWED_ITEMS:
        oldest = min(out, key=lambda k: (out[k].last_at, out[k].count, k))
        del out[oldest]
    return out


def _hours(counts: list[int]) -> dict[str, object]:
    total = sum(counts)
    return {
        "hour_counts": tuple(counts),
        "active_hours": tuple(c / total for c in counts) if total else (),
    }
