"""The feature registry: every feature of the customer profile, defined once.

A feature is a name, a group, a one-line description (shown in "why" explanations), a default, and a
function of `Facts`. `@feature(...)` registers it; `FEATURES` is the ordered list, and nothing else in
the package names a feature. The nightly profile and the slow models' training rows both call
`compute_all`, so what a model learned from and what the agent feeds it cannot drift apart.

**Defaults.** A feature with nothing to measure returns its `default`, never NaN, never a guess
dressed up as data. Where "no data" and "a real zero" mean different things, an indicator feature
(`buy_ordered`, `trend_gap_known`, `price_level_known`, ...) says which. A default is the value
documented in `docs/v3/PROFILE.md`; most are 0, and the exceptions are the ones where 0 would read as a
claim: a neutral ratio is 1, a price level is 0.5, an offer sensitivity is the folder's prior.

**Windows.** "30 days" means after `as_of - 30 days` and up to `as_of`, so an event at exactly
`as_of` counts and one at exactly 30 days ago does not.

**Time.** Weekdays, hours and month days are the customer's local ones (see `autonomy.profile.facts`).
"""

from __future__ import annotations

import calendar
import math
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from autonomy.contracts import Channel
from autonomy.data.folder import OFFER_PRIOR, OFFER_PSEUDO_COUNT
from autonomy.profile.facts import Facts, days_between

__all__ = [
    "BY_NAME",
    "FEATURES",
    "NAMES",
    "Feature",
    "FeatureGroup",
    "compute_all",
    "defaults",
]


class FeatureGroup(StrEnum):
    BUYING = "buying"
    TREND = "trend"
    TASTE = "taste"
    PRICE = "price"
    TIME = "time"
    CHANNELS = "channels"
    RESPONSES = "responses"
    MOMENTS = "moments"


@dataclass(frozen=True, slots=True)
class Feature:
    name: str
    group: FeatureGroup
    description: str
    default: float
    compute: Callable[[Facts], float]


_REGISTRY: list[Feature] = []

RATIO_CAP = 10.0
"""Ratios against a customer's own usual (a gap, an order value) stop at this, so one odd customer
cannot dominate a model's scale."""

RECENT_DAYS = 30
QUARTER_DAYS = 90
YEAR_DAYS = 365


def feature(
    name: str, group: FeatureGroup, description: str, *, default: float = 0.0
) -> Callable[[Callable[[Facts], float]], Callable[[Facts], float]]:
    """Register a feature. The name must start with its group (`buy_` for buying, ...) and be new."""

    def register(fn: Callable[[Facts], float]) -> Callable[[Facts], float]:
        if any(f.name == name for f in _REGISTRY):
            raise ValueError(f"feature {name!r} is defined twice")
        if not description.strip() or not math.isfinite(default):
            raise ValueError(f"feature {name!r} needs a description and a finite default")
        _REGISTRY.append(Feature(name, group, description, default, fn))
        return fn

    return register


def _share(part: float, whole: float) -> float:
    """`part / whole` kept in [0, 1]; 0 when `whole` is not positive."""
    return min(max(part / whole, 0.0), 1.0) if whole > 0 else 0.0


def _capped(x: float) -> float:
    return min(x, RATIO_CAP)


def _momentum(recent: float, before: float) -> float:
    """-1 when everything happened in the earlier window, +1 when everything is recent, 0 when even."""
    total = recent + before
    return (recent - before) / total if total > 0 else 0.0


# --- buying ---------------------------------------------------------------------------------------


@feature("buy_orders_30d", FeatureGroup.BUYING, "Orders placed in the last 30 days")
def _orders_30d(f: Facts) -> float:
    return float(len(f.orders_in(f.since(RECENT_DAYS))))


@feature("buy_orders_90d", FeatureGroup.BUYING, "Orders placed in the last 90 days")
def _orders_90d(f: Facts) -> float:
    return float(len(f.orders_in(f.since(QUARTER_DAYS))))


@feature("buy_orders_365d", FeatureGroup.BUYING, "Orders placed in the last 365 days")
def _orders_365d(f: Facts) -> float:
    return float(len(f.orders_in(f.since(YEAR_DAYS))))


@feature("buy_orders_total", FeatureGroup.BUYING, "Orders placed in the customer's whole history")
def _orders_total(f: Facts) -> float:
    return float(len(f.orders))


@feature("buy_net_revenue", FeatureGroup.BUYING, "Money spent in the whole history, minus refunds")
def _net_revenue(f: Facts) -> float:
    return f.gross_spend - f.refunded


@feature("buy_avg_order_value", FeatureGroup.BUYING, "Average value of one order (0 if no order yet)")
def _aov(f: Facts) -> float:
    return f.mean_order_value


@feature(
    "buy_margin_rate",
    FeatureGroup.BUYING,
    "Share of order value kept as margin, over orders with a value that report margin (0 if none do)",
)
def _margin_rate(f: Facts) -> float:
    with_margin = [o for o in f.orders if o.margin is not None and o.value > 0]
    return _share(sum(o.margin or 0.0 for o in with_margin), sum(o.value for o in with_margin))


@feature("buy_active_months", FeatureGroup.BUYING, "Calendar months in which the customer ordered")
def _active_months(f: Facts) -> float:
    return float(len({(f.local(o.ts).year, f.local(o.ts).month) for o in f.orders}))


@feature("buy_ordered", FeatureGroup.BUYING, "1 if the customer has ever ordered, else 0")
def _ordered(f: Facts) -> float:
    return 1.0 if f.orders else 0.0


@feature(
    "buy_days_since_last_order",
    FeatureGroup.BUYING,
    "Days since the last order (0 if no order yet; see buy_ordered)",
)
def _days_since_order(f: Facts) -> float:
    return f.days_since_last_order


@feature(
    "buy_tenure_days",
    FeatureGroup.BUYING,
    "Days since the first thing we saw the customer do (0 if nothing yet)",
)
def _tenure(f: Facts) -> float:
    return max(days_between(f.first_ts, f.as_of), 0.0) if f.first_ts is not None else 0.0


# --- trend ----------------------------------------------------------------------------------------


@feature(
    "trend_gap_known",
    FeatureGroup.TREND,
    "1 if the customer has ordered at least twice on different days, else 0",
)
def _gap_known(f: Facts) -> float:
    return 1.0 if f.gap_known else 0.0


@feature(
    "trend_avg_gap_days", FeatureGroup.TREND, "Usual number of days between orders (0 unless trend_gap_known)"
)
def _avg_gap(f: Facts) -> float:
    return f.mean_gap if f.gap_known else 0.0


@feature(
    "trend_last_gap_ratio",
    FeatureGroup.TREND,
    "Latest gap between orders against the earlier gaps: below 1 buying faster, above 1 slower (1 if under 3 orders)",
    default=1.0,
)
def _last_gap_ratio(f: Facts) -> float:
    gaps = f.order_gaps
    if len(gaps) < 2:
        return 1.0
    earlier = sum(gaps[:-1]) / (len(gaps) - 1)
    return _capped(gaps[-1] / earlier) if earlier > 0 else 1.0


@feature(
    "trend_overdue_ratio",
    FeatureGroup.TREND,
    "Days since the last order against the usual gap: above 1 means later than usual (0 unless trend_gap_known)",
)
def _overdue_ratio(f: Facts) -> float:
    return _capped(f.days_since_last_order / f.mean_gap) if f.gap_known else 0.0


@feature(
    "trend_order_momentum",
    FeatureGroup.TREND,
    "Orders in the last 90 days against the 90 before, from -1 (slowing) to 1 (speeding up)",
)
def _order_momentum(f: Facts) -> float:
    recent = len(f.orders_in(f.since(QUARTER_DAYS)))
    before = len(f.orders_in(f.since(2 * QUARTER_DAYS), f.since(QUARTER_DAYS)))
    return _momentum(recent, before)


# --- taste ----------------------------------------------------------------------------------------


def _category_counts(f: Facts) -> Counter[str]:
    """For each category, the orders with at least one line in it."""
    return Counter(c for o in f.orders for c in o.categories)


@feature("taste_distinct_categories", FeatureGroup.TASTE, "Different product categories bought")
def _distinct_categories(f: Facts) -> float:
    return float(len(_category_counts(f)))


@feature(
    "taste_top_category_share",
    FeatureGroup.TASTE,
    "Share of category-tagged orders that are in the favourite category (0 if none tagged)",
)
def _top_category_share(f: Facts) -> float:
    counts = _category_counts(f)
    return _share(max(counts.values(), default=0), sum(1 for o in f.orders if o.categories))


@feature(
    "taste_repeat_item_share",
    FeatureGroup.TASTE,
    "Share of item-tagged orders that re-buy an item bought in an earlier order (0 if none tagged)",
)
def _repeat_item_share(f: Facts) -> float:
    seen: set[str] = set()
    tagged = repeats = 0
    for o in f.orders:
        if not o.items:
            continue
        tagged += 1
        repeats += any(i in seen for i in o.items)
        seen.update(o.items)
    return _share(repeats, tagged)


@feature(
    "taste_new_category_share_90d",
    FeatureGroup.TASTE,
    "Share of the last 90 days' tagged orders with a category not bought before then",
)
def _new_category_share(f: Facts) -> float:
    cut = f.since(QUARTER_DAYS)
    known = {c for o in f.orders if o.ts <= cut for c in o.categories}
    recent = [o for o in f.orders_in(cut) if o.categories]
    return _share(sum(any(c not in known for c in o.categories) for o in recent), len(recent))


# --- price ----------------------------------------------------------------------------------------


@feature(
    "price_level_known",
    FeatureGroup.PRICE,
    "1 if the shop has enough orders to read a price level against, and the customer has spent money",
)
def _price_known(f: Facts) -> float:
    return 1.0 if f.shop is not None and f.shop.known and f.mean_order_value > 0 else 0.0


@feature(
    "price_level",
    FeatureGroup.PRICE,
    "Where the customer's average order sits among the shop's orders: 0 cheapest, 1 priciest (0.5 unless price_level_known)",
    default=0.5,
)
def _price_level(f: Facts) -> float:
    if f.shop is None or not f.shop.known or f.mean_order_value <= 0:
        return 0.5
    return f.shop.rank(f.mean_order_value)


@feature(
    "price_offer_sensitivity",
    FeatureGroup.PRICE,
    "How much the customer needs an offer to buy, 0 to 1: offer share pulled toward 0.3 until there are enough orders",
    default=OFFER_PRIOR,
)
def _offer_sensitivity(f: Facts) -> float:
    prior_mass = OFFER_PRIOR * OFFER_PSEUDO_COUNT
    return (sum(o.offered for o in f.orders) + prior_mass) / (len(f.orders) + OFFER_PSEUDO_COUNT)


def _after_offer(f: Facts) -> list[bool]:
    """For each order that follows an order with an offer: whether it was at full price."""
    return [not f.orders[i].offered for i in range(1, len(f.orders)) if f.orders[i - 1].offered]


@feature(
    "price_full_after_offer_known",
    FeatureGroup.PRICE,
    "1 if an order with an offer was followed by another order, else 0",
)
def _full_after_known(f: Facts) -> float:
    return 1.0 if _after_offer(f) else 0.0


@feature(
    "price_full_after_offer_share",
    FeatureGroup.PRICE,
    "Share of orders right after an offer order that were at full price (0 unless known)",
)
def _full_after_offer(f: Facts) -> float:
    after = _after_offer(f)
    return _share(sum(after), len(after))


# --- time -----------------------------------------------------------------------------------------


EVENING_HOURS = range(18, 24)
"""Local hours counted as evening."""


@feature(
    "time_engaged_known",
    FeatureGroup.TIME,
    "1 if the customer has ever clicked or replied, so the hours below mean something, else 0",
)
def _engaged_known(f: Facts) -> float:
    return 1.0 if f.engagements else 0.0


@feature(
    "time_peak_hour",
    FeatureGroup.TIME,
    "Local hour of day (0 to 23) with the most clicks and replies (0 unless time_engaged_known)",
)
def _peak_hour(f: Facts) -> float:
    counts = Counter(f.engagement_hours)
    # Ties go to the earlier hour, so the answer does not depend on the order events arrived in.
    return float(min(counts, key=lambda h: (-counts[h], h))) if counts else 0.0


@feature(
    "time_evening_share",
    FeatureGroup.TIME,
    "Share of clicks and replies between 18:00 and 23:59 local time (0 unless time_engaged_known)",
)
def _evening_share(f: Facts) -> float:
    return _share(sum(h in EVENING_HOURS for h in f.engagement_hours), len(f.engagement_hours))


@feature(
    "time_weekend_order_share",
    FeatureGroup.TIME,
    "Share of orders placed on a Saturday or Sunday, local time (0 if no order yet)",
)
def _weekend_share(f: Facts) -> float:
    return _share(sum(f.local(o.ts).weekday() >= 5 for o in f.orders), len(f.orders))


@feature(
    "time_payday_order_share",
    FeatureGroup.TIME,
    "Share of orders placed on the 1st or 2nd, or the last two days, of a month, local time (0 if no order yet)",
)
def _payday_share(f: Facts) -> float:
    paydays = 0
    for o in f.orders:
        d = f.local(o.ts)
        last = calendar.monthrange(d.year, d.month)[1]
        paydays += d.day <= 2 or d.day >= last - 1
    return _share(paydays, len(f.orders))


# --- channels -------------------------------------------------------------------------------------


def _rate(f: Facts, field: str) -> float:
    return _share(f.total(field), f.total("sends"))


def _channel_engagement(f: Facts, channel: Channel) -> float:
    c = f.channels.get(channel)
    return _share(c.clicks + c.replies, c.sends) if c is not None else 0.0


@feature(
    "ch_open_rate",
    FeatureGroup.CHANNELS,
    "Share of messages sent that were opened; weak, as mail apps open messages by themselves (0 if none sent)",
)
def _open_rate(f: Facts) -> float:
    return _rate(f, "opens")


@feature("ch_click_rate", FeatureGroup.CHANNELS, "Share of messages sent that were clicked (0 if none sent)")
def _click_rate(f: Facts) -> float:
    return _rate(f, "clicks")


@feature("ch_reply_rate", FeatureGroup.CHANNELS, "Share of messages sent that were answered (0 if none sent)")
def _reply_rate(f: Facts) -> float:
    return _rate(f, "replies")


@feature(
    "ch_email_engage_rate",
    FeatureGroup.CHANNELS,
    "Share of emails sent that were clicked or answered (0 if none sent)",
)
def _email_engage(f: Facts) -> float:
    return _channel_engagement(f, Channel.EMAIL)


@feature(
    "ch_whatsapp_engage_rate",
    FeatureGroup.CHANNELS,
    "Share of WhatsApp messages sent that were clicked or answered (0 if none sent)",
)
def _whatsapp_engage(f: Facts) -> float:
    return _channel_engagement(f, Channel.WHATSAPP)


@feature(
    "ch_engaged", FeatureGroup.CHANNELS, "1 if the customer has ever clicked or replied to a message, else 0"
)
def _engaged(f: Facts) -> float:
    return 1.0 if f.last_engaged_ts is not None else 0.0


@feature(
    "ch_days_since_engaged",
    FeatureGroup.CHANNELS,
    "Days since the customer last clicked or replied (0 if never; see ch_engaged)",
)
def _days_since_engaged(f: Facts) -> float:
    last = f.last_engaged_ts
    return max(days_between(last, f.as_of), 0.0) if last is not None else 0.0


@feature(
    "ch_contactable_channels",
    FeatureGroup.CHANNELS,
    "Channels we may message: address known, consent given, not unsubscribed",
)
def _contactable(f: Facts) -> float:
    return float(sum(1 for c in f.channels.values() if c.contactable))


# --- responses ------------------------------------------------------------------------------------


@feature("resp_stop_replies", FeatureGroup.RESPONSES, "Replies that asked us to stop")
def _stop_replies(f: Facts) -> float:
    return float(f.stop_replies)


@feature("resp_not_now_replies", FeatureGroup.RESPONSES, "Replies that said 'not now'")
def _not_now_replies(f: Facts) -> float:
    return float(f.not_now_replies)


@feature(
    "resp_snoozed", FeatureGroup.RESPONSES, "1 if a 'not now' reply still holds, so we should wait, else 0"
)
def _snoozed(f: Facts) -> float:
    return 1.0 if f.snoozed_until is not None and f.snoozed_until > f.as_of else 0.0


@feature("resp_complaints", FeatureGroup.RESPONSES, "Complaints the customer has made about our messages")
def _complaints(f: Facts) -> float:
    return float(f.total("complaints"))


@feature("resp_returns", FeatureGroup.RESPONSES, "Refunds issued to the customer")
def _returns(f: Facts) -> float:
    return float(len(f.refunds))


@feature(
    "resp_refund_value_share",
    FeatureGroup.RESPONSES,
    "Refunded money as a share of money spent, at most 1 (0 if no order yet)",
)
def _refund_share(f: Facts) -> float:
    return _share(f.refunded, f.gross_spend)


@feature(
    "resp_no_discounts", FeatureGroup.RESPONSES, "1 if the customer said they do not want discounts, else 0"
)
def _no_discounts(f: Facts) -> float:
    return 1.0 if f.preferences.get("no_discounts", "").strip().lower() in {"true", "1", "yes"} else 0.0


# --- moments --------------------------------------------------------------------------------------


@feature("mom_viewed", FeatureGroup.MOMENTS, "1 if the customer has ever viewed a product, else 0")
def _viewed(f: Facts) -> float:
    return 1.0 if f.views else 0.0


@feature(
    "mom_days_since_view",
    FeatureGroup.MOMENTS,
    "Days since the customer last viewed a product (0 if never; see mom_viewed)",
)
def _days_since_view(f: Facts) -> float:
    return max(days_between(f.views[-1][0], f.as_of), 0.0) if f.views else 0.0


@feature(
    "mom_viewed_not_bought",
    FeatureGroup.MOMENTS,
    "Different products viewed in the last 30 days and never bought",
)
def _viewed_not_bought(f: Facts) -> float:
    cut = f.since(RECENT_DAYS)
    bought = {i for o in f.orders for i in o.items}
    return float(len({item for ts, item in f.views if ts > cut and item and item not in bought}))


@feature("mom_carted", FeatureGroup.MOMENTS, "1 if the customer has ever added to a cart, else 0")
def _carted(f: Facts) -> float:
    return 1.0 if f.carts else 0.0


@feature(
    "mom_days_since_cart",
    FeatureGroup.MOMENTS,
    "Days since the customer last added to a cart (0 if never; see mom_carted)",
)
def _days_since_cart(f: Facts) -> float:
    return max(days_between(f.carts[-1], f.as_of), 0.0) if f.carts else 0.0


@feature(
    "mom_cart_open",
    FeatureGroup.MOMENTS,
    "1 if the last cart came after the last order (something may be waiting to buy), else 0",
)
def _cart_open(f: Facts) -> float:
    if not f.carts:
        return 0.0
    last = f.last_order_ts
    return 1.0 if last is None or f.carts[-1] > last else 0.0


@feature(
    "mom_refill_due_days",
    FeatureGroup.MOMENTS,
    "Days until the next order is due by the customer's usual gap; negative if overdue (0 unless trend_gap_known)",
)
def _refill_due(f: Facts) -> float:
    return f.mean_gap - f.days_since_last_order if f.gap_known else 0.0


# --- the list -------------------------------------------------------------------------------------

FEATURES: tuple[Feature, ...] = tuple(_REGISTRY)
"""Every feature, in the order the profile is written."""

BY_NAME: dict[str, Feature] = {f.name: f for f in FEATURES}

NAMES: tuple[str, ...] = tuple(f.name for f in FEATURES)

_GROUP_PREFIX: dict[FeatureGroup, str] = {
    FeatureGroup.BUYING: "buy_",
    FeatureGroup.TREND: "trend_",
    FeatureGroup.TASTE: "taste_",
    FeatureGroup.PRICE: "price_",
    FeatureGroup.TIME: "time_",
    FeatureGroup.CHANNELS: "ch_",
    FeatureGroup.RESPONSES: "resp_",
    FeatureGroup.MOMENTS: "mom_",
}


def _check_registry() -> None:
    for f in FEATURES:
        if not f.name.startswith(_GROUP_PREFIX[f.group]):
            raise ValueError(f"feature {f.name!r} is in group {f.group.value} but lacks its prefix")


_check_registry()


def defaults() -> dict[str, float]:
    """Each feature's value when there is nothing to measure."""
    return {f.name: f.default for f in FEATURES}


def compute_all(facts: Facts) -> dict[str, float]:
    """Every feature for one customer. A value that comes out as NaN or infinite becomes the default."""
    out: dict[str, float] = {}
    for f in FEATURES:
        value = float(f.compute(facts))
        out[f.name] = value if math.isfinite(value) else f.default
    return out
