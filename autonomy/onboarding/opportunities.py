"""Goal proposals sized in money, from the shop's own history (docs/v2/ZERO_TOUCH.md, item 1).

After a connector backfill, `scan_opportunities(events, states, now)` looks for the opportunities
that pay most often and sizes each one:

| Opportunity | Who is in it now | Goal kind |
|---|---|---|
| `opp_refill_due` | a product they buy again and again is due (refill window open now or within 30 days; `autonomy.catalog` cycle model) | repeat purchase |
| `opp_first_to_second` | bought once, and are inside the stretch where the shop's second orders happen (10th to 80th percentile of its own first-to-second gaps) | repeat purchase |
| `opp_win_back` | past the point by which 90% of people like them have come back, by their own repeat curve (order-level cycle model, shrunk to the shop's curve), less than a year ago | win-back |
| `opp_vip_at_risk` | top 20% of the shop's buyers by spend, overdue on their own curve but not yet lapsed | retention |
| `opp_cart` | left a cart (or, without cart events, viewed a product) in the last 7 days and has not bought since; only when such events exist | cart recovery |

**The money.** For each one, over the next 30 days, if each reachable person gets one message:

    extra orders  = reached x lift
    extra profit  = extra orders x their average order value x margin  -  reached x 2 x message cost

* `reached` is the people in it we may contact now (consent on email or SMS), less the 10% we never
  contact (they are what proves the effect later, so they are left out of the money).
* `lift` is the documented lift of one message (`priors.MESSAGE_LIFT`, Hillstrom: +0.45 points,
  0.23 to 0.67, in 14 days), capped at the same relative rise on the group's own purchase rate, and
  raised for refills by the documented refill boost. The group's own rate is **measured on the
  shop's history**: the same rule applied 30, 60 and 90 days ago, and the share who bought in the
  30 days after (pooled; Wilson 95% interval sized on the distinct people, since one person can be
  in several look-backs). Its low and high ends feed the range. The lift's cap compares this rate,
  restated over 14 days, with the test's 14-day base.
* order value is the group's own (95% interval of the mean); margin is the shop's own when its
  orders carry margins, else the documented 40% default, flagged, and the shop is asked once.
* two messages per person are costed (the starting plan's message and one follow-up), whatever
  the follow-up adds is not counted.

The range multiplies the low ends together and the high ends together, so it is wider than any one
source of doubt: a person sees how sure the number is, never a bare point.

**Never a number the data cannot support.** Sizing needs `MIN_HISTORY_DAYS` days of orders from
`MIN_BUYERS` buyers, and each opportunity at least `MIN_BASE_SAMPLE` people in its look-backs; below
that the proposal keeps its audience count and says "Not enough history yet" with what is missing,
and carries no money. Refill needs products bought again by at least 3 customers; cart and browse
need cart or product-view events from a connected tool, and are left out (and said so) without them.

Ranked by expected profit; the best one with enough history is preselected. Pure maths: no LLM,
no clock (time is passed in), no randomness.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final

import pandas as pd
from pydantic import Field

from autonomy.catalog.cycle import CycleEstimate, CycleModel
from autonomy.catalog.products import PURCHASE_COLUMNS, Catalogue, to_days
from autonomy.contracts import (
    Audience,
    AudienceFilter,
    Channel,
    CustomerState,
    Estimate,
    Event,
    Goal,
    GoalKind,
)
from autonomy.contracts.base import Contract
from autonomy.onboarding.history import ShopHistory, quantile
from autonomy.onboarding.priors import (
    CHANNEL_COSTS,
    DEFAULT_MARGIN_RATE,
    HOLDOUT_SHARE,
    MESSAGE_LIFT,
    UNSUBSCRIBE_PRIOR,
    CitedNumber,
    lift_for,
    refill_factor,
)

__all__ = [
    "LOOKBACK_DAYS",
    "MIN_BASE_SAMPLE",
    "MIN_BUYERS",
    "MIN_HISTORY_DAYS",
    "Basis",
    "MarginReading",
    "OpportunityScan",
    "Proposal",
    "margin_reading",
    "proposal_goal",
    "scan_opportunities",
]

MIN_HISTORY_DAYS: Final[float] = 60.0
"""Days of orders needed before anything is sized: one 30-day look-back plus its 30-day outcome."""
MIN_BUYERS: Final[int] = 30
"""Customers who bought, needed before anything is sized."""
MIN_BASE_SAMPLE: Final[int] = 30
"""People an opportunity's look-backs must hold before its own purchase rate is trusted."""
MIN_GAP_SAMPLE: Final[int] = 20
"""Second orders needed to set the first-to-second window from the shop's own gaps."""
MIN_MARGIN_COVERAGE: Final[float] = 0.5
"""Share of orders that must carry a margin before the shop's own margin is used."""
OUTCOME_DAYS: Final[int] = 30
LOOKBACK_DAYS: Final[tuple[int, ...]] = (30, 60, 90)
CART_DAYS: Final[int] = 7
MAX_LAPSE_DAYS: Final[float] = 365.0
"""Customers gone longer than this are left out of win-back: a message a year later is not this plan."""
LAPSE_Z: Final[float] = 1.2816
"""The 90th percentile of a log-normal gap is exp(mu + 1.2816 sigma)."""
VIP_SHARE: Final[float] = 0.2
"""The top fifth of buyers by spend count as VIPs."""
FIRST_TO_SECOND_QUANTILES: Final[tuple[float, float]] = (0.1, 0.8)
MESSAGES_COSTED: Final[int] = 2
"""The starting plan sends one message and at most one follow-up (`api.v2.deps.template_strategist`)."""
Z95: Final[float] = 1.96


# --- what comes back --------------------------------------------------------------------------------


class Basis(Contract):
    """One number a proposal stands on, and where it came from."""

    label: str
    value: str
    source: str
    assumed: bool = Field(default=False, description="A documented default, not the shop's own data")


class MarginReading(Contract):
    rate: float = Field(ge=0, le=1)
    source: str
    from_data: bool
    orders_with_margin: int = 0
    orders: int = 0


class Proposal(Contract):
    opportunity_id: str
    kind: GoalKind
    title: str
    why: str = Field(description="One sentence")
    audience_now: int = Field(ge=0, description="People who qualify today")
    reachable_now: int = Field(ge=0, description="Of them, people we may message (consent on email or SMS)")
    reached_in_month: int = Field(
        ge=0,
        description="People the money counts: reachable over the next 30 "
        "days, less the share we never contact",
    )
    monthly_profit: Estimate | None = Field(
        default=None, description="Expected extra profit over the next 30 days, with its range"
    )
    extra_orders: Estimate | None = None
    message_cost: float | None = Field(default=None, description="What the messages cost at most")
    contacts_lost: float | None = Field(
        default=None, description="Expected unsubscribes and complaints, at the documented 0.5% a message"
    )
    base_rate: Estimate | None = Field(
        default=None, description="Share of people like them who bought within 30 days, measured"
    )
    base_sample: int = 0
    enough_history: bool
    not_enough: str | None = Field(
        default=None, description="'Not enough history yet: ...' when it has no money"
    )
    basis: tuple[Basis, ...] = ()
    audience: Audience
    goal_text: str
    budget: float = Field(ge=0, description="The plan's 30-day ceiling: its messages at most")
    currency: str
    already_running: bool = False
    preselected: bool = False
    rank: int = 0


class OpportunityScan(Contract):
    as_of: datetime
    currency: str
    proposals: tuple[Proposal, ...]
    preselected_id: str | None
    history_days: float
    buyers: int
    orders: int
    enough_history: bool
    not_enough: str | None = None
    margin: MarginReading
    skipped: tuple[str, ...] = Field(default=(), description="Opportunities left out, and why")
    notes: tuple[str, ...] = ()


# --- the shop's margin --------------------------------------------------------------------------------


def margin_reading(history: ShopHistory, given: float | None = None) -> MarginReading:
    """The margin rate to size with: the shop's answer, its orders' margins, else the default (flagged)."""
    rows = history.all_purchases()
    if given is not None:
        return MarginReading(rate=given, source="your answer", from_data=True, orders=len(rows))
    known = [p for p in rows if p.margin is not None and p.value > 0]
    if rows and len(known) >= MIN_MARGIN_COVERAGE * len(rows):
        revenue = sum(p.value for p in known)
        margin = sum(float(p.margin or 0.0) for p in known)
        if revenue > 0:
            rate = min(max(margin / revenue, 0.0), 1.0)
            return MarginReading(
                rate=round(rate, 4),
                source="your orders' margins",
                from_data=True,
                orders_with_margin=len(known),
                orders=len(rows),
            )
    return MarginReading(
        rate=DEFAULT_MARGIN_RATE,
        source="assumed: the decision engine's default (autonomy/decide/models.py ValueModel); "
        "tell us your margin",
        from_data=False,
        orders_with_margin=len(known),
        orders=len(rows),
    )


# --- models fitted at a moment, shared by every opportunity -------------------------------------------


@dataclass
class _Context:
    history: ShopHistory
    frame: pd.DataFrame
    _orders: dict[datetime, CycleModel] = field(default_factory=dict)
    _items: dict[datetime, tuple[CycleModel, Catalogue]] = field(default_factory=dict)
    _order_est: dict[datetime, dict[str, CycleEstimate]] = field(default_factory=dict)

    @classmethod
    def of(cls, history: ShopHistory) -> _Context:
        rows = [
            (c, p.item_id, p.category, to_days(p.ts), p.value, math.nan)
            for c, ps in sorted(history.purchases.items())
            for p in ps
        ]
        frame = pd.DataFrame(rows, columns=list(PURCHASE_COLUMNS))
        frame["day"] = frame["day"].astype(float)
        return cls(history=history, frame=frame)

    def order_model(self, at: datetime) -> CycleModel:
        if at not in self._orders:
            self._orders[at] = CycleModel.fit(self.frame, at, level="order")
        return self._orders[at]

    def order_estimates(self, at: datetime) -> dict[str, CycleEstimate]:
        """Every buyer's order-level cycle at `at`, from their purchases up to then."""
        if at not in self._order_est:
            model = self.order_model(at)
            out: dict[str, CycleEstimate] = {}
            for customer in sorted(self.history.purchases):
                times = [p.ts for p in self.history.bought_by(customer, at)]
                if not times:
                    continue
                est = model.estimate("*", times, at, pace=model.pace_of(times, at))
                if est is not None:
                    out[customer] = est
            self._order_est[at] = out
        return self._order_est[at]

    def item_model(self, at: datetime) -> tuple[CycleModel, Catalogue]:
        if at not in self._items:
            seen = self.frame[self.frame["day"] <= to_days(at)]
            catalogue = Catalogue.infer(seen)
            self._items[at] = (CycleModel.fit(seen, at, level="item", catalogue=catalogue), catalogue)
        return self._items[at]


def _p90_days(est: CycleEstimate) -> float:
    return math.exp(est.mu + LAPSE_Z * est.sigma)


# --- the opportunities -------------------------------------------------------------------------------

Members = dict[str, float | None]
"""customer_id -> the refill chance (p14) for refills, None for the others."""


@dataclass(frozen=True)
class _Spec:
    opportunity_id: str
    kind: GoalKind
    title: str
    members: Callable[[_Context, datetime], Members]
    upcoming: Callable[[_Context], Members] | None
    """Who joins over the next 30 days (refills coming due); None: the people in it now."""
    audience: Callable[[_Context], Audience]
    why: Callable[[int, int, Estimate | None, _Context], str]
    """(people in it now, people in it over the next 30 days, their measured buying rate) -> one sentence."""
    lift_note: str = ""


def _refill_members(ctx: _Context, at: datetime, *, ahead_days: float = 0.0) -> Members:
    model, catalogue = ctx.item_model(at)
    items = set(catalogue.replenishable_items())
    if not items:
        return {}
    out: Members = {}
    for customer in sorted(ctx.history.purchases):
        bought = ctx.history.bought_by(customer, at)
        times: dict[str, list[datetime]] = {}
        for p in bought:
            if p.item_id is not None and p.item_id in items:
                times.setdefault(p.item_id, []).append(p.ts)
        if not times:
            continue
        pace = model.pace_of([p.ts for p in bought], at)
        best: float | None = None
        for item in sorted(times):
            est = model.estimate(item, times[item], at, group=catalogue.category_of(item), pace=pace)
            if est is None:
                continue
            due = est.window_open or (
                ahead_days > 0 and at < est.window_start_at <= at + timedelta(days=ahead_days)
            )
            if due and (best is None or est.p14 > best):
                best = est.p14
        if best is not None:
            out[customer] = best
    return out


def _first_window(ctx: _Context) -> tuple[float, float, bool]:
    """Days since the first order during which second orders happen: (from, to, from the shop's data)."""
    gaps = ctx.history.second_purchase_gaps()
    if len(gaps) >= MIN_GAP_SAMPLE:
        lo, hi = FIRST_TO_SECOND_QUANTILES
        return max(1.0, round(quantile(gaps, lo))), max(2.0, round(quantile(gaps, hi))), True
    horizon = ctx.order_model(ctx.history.now).config.repeat_horizon_days
    return 1.0, float(horizon), False


def _first_members(ctx: _Context, at: datetime) -> Members:
    lo, hi, _ = _first_window(ctx)
    out: Members = {}
    for customer in sorted(ctx.history.purchases):
        bought = ctx.history.bought_by(customer, at)
        if len({p.ts.date() for p in bought}) != 1:
            continue
        age = (at - bought[-1].ts).total_seconds() / 86_400.0
        if lo <= age <= hi:
            out[customer] = None
    return out


def _lapsed(est: CycleEstimate) -> bool:
    return _p90_days(est) < est.elapsed_days <= MAX_LAPSE_DAYS


def _winback_members(ctx: _Context, at: datetime) -> Members:
    return {c: None for c, est in ctx.order_estimates(at).items() if _lapsed(est)}


def _vip_threshold(ctx: _Context, at: datetime) -> float:
    spends = [sum(p.value for p in ctx.history.bought_by(c, at)) for c in ctx.history.purchases]
    spends = [s for s in spends if s > 0]
    return quantile(spends, 1.0 - VIP_SHARE) if spends else math.inf


def _vip_members(ctx: _Context, at: datetime) -> Members:
    threshold = _vip_threshold(ctx, at)
    out: Members = {}
    for customer, est in ctx.order_estimates(at).items():
        bought = ctx.history.bought_by(customer, at)
        if len({p.ts.date() for p in bought}) < 2 or sum(p.value for p in bought) < threshold:
            continue
        if est.median_interval_days < est.elapsed_days <= _p90_days(est):
            out[customer] = None
    return out


def _intent_source(ctx: _Context) -> dict[str, list[datetime]]:
    return ctx.history.carts if any(ctx.history.carts.values()) else ctx.history.views


def _cart_members(ctx: _Context, at: datetime, *, days: float = CART_DAYS) -> Members:
    out: Members = {}
    since = at - timedelta(days=days)
    for customer, moments in sorted(_intent_source(ctx).items()):
        recent = [t for t in moments if since < t <= at]
        if recent and not ctx.history.bought_between(customer, recent[-1], at):
            out[customer] = None
    return out


def _days(value: float) -> int:
    return round(value)


def _refill_audience(ctx: _Context) -> Audience:  # noqa: ARG001
    return Audience(
        description="Due to reorder a product they buy again and again",
        filters=(AudienceFilter(field="scores.refill_window_open", op="gte", value=0.5),),
    )


def _first_audience(ctx: _Context) -> Audience:
    lo, hi, _ = _first_window(ctx)
    return Audience(
        description=f"Bought once, {_days(lo)} to {_days(hi)} days ago",
        filters=(
            AudienceFilter(field="purchases", op="eq", value=1),
            AudienceFilter(field="last_purchase_at", op="days_since_gt", value=_days(lo)),
            AudienceFilter(field="last_purchase_at", op="days_since_lt", value=_days(hi)),
        ),
    )


def _shop_p90(ctx: _Context) -> float:
    shop = ctx.order_model(ctx.history.now).shop
    return math.exp(shop.mu + LAPSE_Z * math.sqrt(shop.sigma**2 + shop.tau**2))


def _winback_audience(ctx: _Context) -> Audience:
    days = min(_days(_shop_p90(ctx)), int(MAX_LAPSE_DAYS) - 1)
    return Audience(
        description=f"Bought before, nothing for {days} to {int(MAX_LAPSE_DAYS)} days "
        "(past the point by which 90% of your returning customers come back)",
        filters=(
            AudienceFilter(field="purchases", op="gte", value=1),
            AudienceFilter(field="last_purchase_at", op="days_since_gt", value=days),
            AudienceFilter(field="last_purchase_at", op="days_since_lt", value=int(MAX_LAPSE_DAYS)),
        ),
    )


def _vip_audience(ctx: _Context) -> Audience:
    now = ctx.history.now
    threshold = _vip_threshold(ctx, now)
    median = math.exp(ctx.order_model(now).shop.mu)
    p90 = min(_shop_p90(ctx), MAX_LAPSE_DAYS)
    return Audience(
        description=f"Top fifth by spend (at least {threshold:,.0f}), quiet for longer than usual",
        filters=(
            AudienceFilter(field="purchases", op="gte", value=2),
            AudienceFilter(field="revenue", op="gte", value=round(threshold, 2)),
            AudienceFilter(field="last_purchase_at", op="days_since_gt", value=_days(median)),
            AudienceFilter(
                field="last_purchase_at", op="days_since_lt", value=max(_days(p90), _days(median) + 1)
            ),
        ),
    )


def _cart_audience(ctx: _Context) -> Audience:  # noqa: ARG001
    return Audience(
        description="Active on the site in the last 3 days",
        filters=(AudienceFilter(field="last_seen_at", op="days_since_lt", value=3),),
    )


def _pct(x: float) -> str:
    return f"{x:.0%}" if x >= 0.1 else f"{x:.1%}"


def _why_refill(n: int, month: int, base: Estimate | None, ctx: _Context) -> str:  # noqa: ARG001
    rate = f"; {_pct(base.point)} of people like them reordered within a month" if base else ""
    more = f", and {month - n:,} more come due within 30 days" if month > n else ""
    return f"{n:,} customers are due to reorder a product they buy again and again{more}{rate}."


def _why_first(n: int, month: int, base: Estimate | None, ctx: _Context) -> str:  # noqa: ARG001
    lo, hi, _ = _first_window(ctx)
    rate = f", and only {_pct(base.point)} of them usually do within a month" if base else ""
    return (
        f"{n:,} customers bought once {_days(lo)} to {_days(hi)} days ago, when your second orders "
        f"happen{rate}."
    )


def _why_winback(n: int, month: int, base: Estimate | None, ctx: _Context) -> str:  # noqa: ARG001
    rate = f"; {_pct(base.point)} of people like them came back within a month" if base else ""
    return f"{n:,} customers have gone longer than their own buying rhythm says they should{rate}."


def _why_vip(n: int, month: int, base: Estimate | None, ctx: _Context) -> str:  # noqa: ARG001
    rate = f"; {_pct(base.point)} of people like them bought within a month" if base else ""
    return f"{n:,} of your top-spending customers are overdue for their next order{rate}."


def _why_cart(n: int, month: int, base: Estimate | None, ctx: _Context) -> str:
    what = "left a cart" if any(ctx.history.carts.values()) else "looked at products"
    rate = f"; {_pct(base.point)} of people like them bought within a month" if base else ""
    month_words = f" ({month:,} in the last 30 days)" if month > n else ""
    return f"{n:,} people {what} in the last week{month_words} and have not bought since{rate}."


_SPECS: Final[tuple[_Spec, ...]] = (
    _Spec(
        "opp_refill_due",
        GoalKind.REPEAT_PURCHASE,
        "Refill reminders",
        _refill_members,
        lambda ctx: _refill_members(ctx, ctx.history.now, ahead_days=OUTCOME_DAYS),
        _refill_audience,
        _why_refill,
        lift_note="raised by the documented refill boost",
    ),
    _Spec(
        "opp_first_to_second",
        GoalKind.REPEAT_PURCHASE,
        "Second purchase",
        _first_members,
        None,
        _first_audience,
        _why_first,
    ),
    _Spec(
        "opp_win_back",
        GoalKind.WIN_BACK,
        "Win back lapsed customers",
        _winback_members,
        None,
        _winback_audience,
        _why_winback,
    ),
    _Spec(
        "opp_vip_at_risk",
        GoalKind.RETENTION,
        "Keep VIPs who are slowing",
        _vip_members,
        None,
        _vip_audience,
        _why_vip,
    ),
    _Spec(
        "opp_cart",
        GoalKind.CART_RECOVERY,
        "Cart and browse follow-up",
        _cart_members,
        lambda ctx: _cart_members(ctx, ctx.history.now, days=OUTCOME_DAYS),
        _cart_audience,
        _why_cart,
    ),
)


# --- maths -------------------------------------------------------------------------------------------


def _wilson(k: int, n: int, effective: int | None = None) -> Estimate:
    """The share k/n with its Wilson 95% interval, computed as if from `effective` independent
    observations (default n); fewer makes the interval wider, never the point different."""
    p = k / n
    m = n if effective is None else max(1, min(effective, n))
    denom = 1 + Z95**2 / m
    centre = (p + Z95**2 / (2 * m)) / denom
    half = Z95 * math.sqrt(p * (1 - p) / m + Z95**2 / (4 * m * m)) / denom
    return Estimate(point=p, low=max(0.0, min(p, centre - half)), high=min(1.0, max(p, centre + half)))


def _base_rate(ctx: _Context, spec: _Spec) -> tuple[Estimate | None, int, int]:
    """The share of people in this opportunity who bought within 30 days, the same rule applied at
    each look-back; (rate, distinct people, look-backs used).

    The look-backs are pooled for the point, but one person can be in the group at several of them,
    and their outcomes are not independent. So the interval and the sample size count each person
    once (the pooled successes scaled to that many people): an interval as wide as the distinct
    people justify, never narrower because the same customers were counted three times."""
    first = ctx.history.first_purchase_at()
    if first is None:
        return None, 0, 0
    k = n = used = 0
    people: set[str] = set()
    for days in LOOKBACK_DAYS:
        cut = ctx.history.now - timedelta(days=days)
        if (cut - first).days < OUTCOME_DAYS:
            continue  # no history to judge anyone by at this look-back
        members = spec.members(ctx, cut)
        used += 1
        n += len(members)
        people.update(members)
        k += sum(1 for c in members if ctx.history.bought_between(c, cut, cut + timedelta(days=OUTCOME_DAYS)))
    if n == 0:
        return None, 0, used
    return _wilson(k, n, len(people)), len(people), used


def _order_value(ctx: _Context, people: Iterable[str]) -> Estimate | None:
    values = [v for c in people if (v := ctx.history.order_value(c)) is not None]
    if not values:
        shop = ctx.history.mean_order_value()
        return None if shop is None else Estimate(point=shop, low=shop, high=shop)
    mean = statistics.fmean(values)
    if len(values) < 2:
        return Estimate(point=mean, low=mean, high=mean)
    half = Z95 * statistics.stdev(values) / math.sqrt(len(values))
    return Estimate(point=mean, low=max(0.0, mean - half), high=mean + half)


def _money(x: float, currency: str) -> str:
    symbol = {"USD": "$", "GBP": "£", "EUR": "€", "AUD": "A$", "CAD": "C$", "INR": "₹"}.get(currency)
    return f"{symbol}{x:,.0f}" if symbol else f"{x:,.0f} {currency}"


def _round_estimate(e: Estimate, digits: int = 2) -> Estimate:
    lo, pt, hi = round(e.low, digits), round(e.point, digits), round(e.high, digits)
    return Estimate(point=pt, low=min(lo, pt), high=max(hi, pt))


# --- the scan ----------------------------------------------------------------------------------------


def _not_enough_shop(history: ShopHistory) -> str | None:
    days, buyers = history.history_days(), history.buyers()
    if days >= MIN_HISTORY_DAYS and buyers >= MIN_BUYERS:
        return None
    return (
        f"Not enough history yet: {days:.0f} days of orders from {buyers:,} customers; sizing needs "
        f"{MIN_HISTORY_DAYS:.0f} days and {MIN_BUYERS} customers who bought."
    )


def _size(
    spec: _Spec,
    ctx: _Context,
    *,
    currency: str,
    margin: MarginReading,
    shop_gap: str | None,
    taken: frozenset[GoalKind],
    channel_costs: Mapping[Channel, float],
    lift_prior: CitedNumber,
) -> Proposal:
    h = ctx.history
    now_members = spec.members(ctx, h.now)
    month_members = spec.upcoming(ctx) if spec.upcoming is not None else now_members
    reachable_now = [c for c in now_members if h.reachable_channel(c) is not None]
    reachable_month = sorted(c for c in month_members if h.reachable_channel(c) is not None)
    reached = round(len(reachable_month) * (1.0 - HOLDOUT_SHARE))
    audience = spec.audience(ctx)
    title = spec.title
    basis: list[Basis] = [
        Basis(
            label="People we never contact",
            value=f"{HOLDOUT_SHARE:.0%} are left out of the money: they are what proves the effect",
            source="the default share of people we never contact (autonomy/contracts Envelope)",
            assumed=True,
        )
    ]

    def out(**extra: object) -> Proposal:
        fields: dict[str, object] = {
            "opportunity_id": spec.opportunity_id,
            "kind": spec.kind,
            "title": title,
            "audience_now": len(now_members),
            "reachable_now": len(reachable_now),
            "reached_in_month": reached,
            "audience": audience,
            "currency": currency,
            "already_running": spec.kind in taken,
            "basis": tuple(basis),
        }
        fields.update(extra)
        return Proposal.model_validate(fields)

    base, n_base, used = _base_rate(ctx, spec) if shop_gap is None else (None, 0, 0)
    why = spec.why(len(now_members), len(month_members), base if n_base >= MIN_BASE_SAMPLE else None, ctx)
    goal_text = f"{title}: {len(now_members):,} customers qualify today"
    gap = shop_gap
    if (
        gap is None
        and spec.opportunity_id == "opp_refill_due"
        and not ctx.item_model(h.now)[1].replenishable_items()
    ):
        gap = (
            "Not enough history yet: no product has been bought again by at least 3 customers, "
            "so there is no refill cycle to time reminders by."
        )
    if gap is None and n_base < MIN_BASE_SAMPLE:
        gap = (
            f"Not enough history yet: {n_base} people matched this group in your past "
            f"{used} look-back{'s' if used != 1 else ''}; we need {MIN_BASE_SAMPLE} to trust its buying rate."
        )
    if gap is None and reached == 0:
        gap = "Nobody we may message is in this group right now."
    unpriced = sorted(
        {(h.reachable_channel(c) or Channel.EMAIL).value for c in reachable_month}
        - {c.value for c in channel_costs}
    )
    if gap is None and unpriced:
        # Never a dollar-sized default in another currency: no price, no budget (docs/v3/CURRENCY.md).
        gap = (
            f"No {currency} price per message for {', '.join(unpriced)} yet: set the shop's channel prices "
            "(AUTONOMY_CHANNEL_COSTS) to size this goal's budget."
        )
    if gap is not None or base is None:
        return out(
            why=why, enough_history=False, not_enough=gap, goal_text=goal_text, budget=0.0, base_sample=n_base
        )

    # --- money ---
    basis.append(
        Basis(
            label="Your buying rate",
            value=f"{_pct(base.point)} ({_pct(base.low)} to {_pct(base.high)}) of {n_base:,} people like them "
            f"bought within 30 days ({used} look-back{'s' if used != 1 else ''})",
            source="your order history",
        )
    )
    boost = 1.0
    if spec.opportunity_id == "opp_refill_due":
        p14s = [p for c in reachable_month if (p := month_members.get(c)) is not None]
        boost = refill_factor(statistics.fmean(p14s)) if p14s else 1.0
        basis.append(
            Basis(
                label="Refill boost",
                value=f"x{boost:.2f} on the message's lift while the refill window is open",
                source="autonomy/decide/bidders.py REFILL_LIFT; docs/v2/CATALOG.md",
                assumed=True,
            )
        )
    lift = Estimate(
        point=lift_for(base.point, lift_prior.point, base_days=OUTCOME_DAYS) * boost,
        low=lift_for(base.low, lift_prior.low, base_days=OUTCOME_DAYS) * boost,
        high=lift_for(base.high, lift_prior.high, base_days=OUTCOME_DAYS) * boost,
    )
    capped = lift.point < lift_prior.point * boost - 1e-12
    basis.append(
        Basis(
            label="What one message adds",
            value=f"+{lift.point * 100:.2f} points ({lift.low * 100:.2f} to {lift.high * 100:.2f}) to the chance "
            "of a purchase"
            + (", capped at the test's relative rise because this group buys rarely" if capped else ""),
            source=lift_prior.cite(),
            assumed=True,
        )
    )
    aov = _order_value(ctx, reachable_month)
    if aov is None:
        return out(
            why=why,
            enough_history=False,
            not_enough="Not enough history yet: no order values to size this group by.",
            goal_text=goal_text,
            budget=0.0,
            base_sample=n_base,
        )
    basis.append(
        Basis(
            label="Their average order",
            value=f"{_money(aov.point, currency)} ({_money(aov.low, currency)} to {_money(aov.high, currency)})",
            source="your order history",
        )
    )
    basis.append(
        Basis(label="Margin", value=f"{margin.rate:.0%}", source=margin.source, assumed=not margin.from_data)
    )
    per_message = statistics.fmean(
        channel_costs.get(h.reachable_channel(c) or Channel.EMAIL, 0.0) for c in reachable_month
    )
    cost = reached * MESSAGES_COSTED * per_message
    basis.append(
        Basis(
            label="Message cost",
            value=f"{MESSAGES_COSTED} messages each at about {per_message:.3f} {currency}",
            source=(
                "autonomy/decide/bidders.py DEFAULT_CHANNEL_COSTS"
                if dict(channel_costs) == dict(CHANNEL_COSTS)
                else "the shop's channel prices"
            ),
            assumed=True,
        )
    )
    orders = Estimate(point=reached * lift.point, low=reached * lift.low, high=reached * lift.high)
    profit = Estimate(
        point=orders.point * aov.point * margin.rate - cost,
        low=orders.low * aov.low * margin.rate - cost,
        high=orders.high * aov.high * margin.rate - cost,
    )
    profit = _round_estimate(profit)
    budget = float(max(1, math.ceil(cost)))
    goal_text = (
        f"{title} for {len(reachable_month):,} customers{' in the next 30 days' if spec.upcoming else ''}: about {_money(profit.point, currency)} extra profit "
        f"a month ({_money(profit.low, currency)} to {_money(profit.high, currency)})"
    )
    return out(
        why=why,
        monthly_profit=profit,
        extra_orders=_round_estimate(orders, 2),
        message_cost=round(cost, 2),
        contacts_lost=round(reached * MESSAGES_COSTED * UNSUBSCRIBE_PRIOR, 1),
        base_rate=_round_estimate(base, 4),
        base_sample=n_base,
        enough_history=True,
        goal_text=goal_text,
        budget=budget,
    )


def scan_opportunities(
    events: Iterable[Event],
    states: Iterable[CustomerState],
    now: datetime,
    *,
    currency: str | None = None,
    margin_rate: float | None = None,
    taken: frozenset[GoalKind] = frozenset(),
    channel_costs: Mapping[Channel, float] = CHANNEL_COSTS,
    lift_prior: CitedNumber = MESSAGE_LIFT,
    history: ShopHistory | None = None,
    unreachable: Mapping[str, str] | None = None,
) -> OpportunityScan:
    """Every opportunity in the shop's history, sized and ranked; see the module docstring.

    `margin_rate` is the shop's own answer when it gave one. `taken` marks goal kinds already
    running (not preselected). `unreachable` maps an opportunity id to why this deployment cannot
    reach its audience yet (e.g. refill reminders without refill scores on each customer): it is
    still sized and shown, never preselected, and the reason is a note. Pass a prebuilt `history`
    to share one pass with `learn_limits`.
    """
    blocked = dict(unreachable or {})
    h = history if history is not None else ShopHistory.build(events, states, now)
    money = currency or h.currency()
    margin = margin_reading(h, margin_rate)
    shop_gap = _not_enough_shop(h)
    ctx = _Context.of(h)
    skipped: list[str] = []
    proposals: list[Proposal] = []
    for spec in _SPECS:
        if spec.opportunity_id == "opp_cart" and not any(h.carts.values()) and not any(h.views.values()):
            skipped.append(
                "Cart and browse follow-up: no cart or product-view events from your tools yet "
                "(Klaviyo's Added to Cart and Viewed Product bring them)."
            )
            continue
        proposals.append(
            _size(
                spec,
                ctx,
                currency=money,
                margin=margin,
                shop_gap=shop_gap,
                taken=taken,
                channel_costs=channel_costs,
                lift_prior=lift_prior,
            )
        )
    ranked = sorted(
        proposals,
        key=lambda p: (
            0 if p.monthly_profit is not None else 1,
            -(p.monthly_profit.point if p.monthly_profit is not None else 0.0),
            -p.audience_now,
            p.opportunity_id,
        ),
    )
    best = next(
        (
            p.opportunity_id
            for p in ranked
            if p.monthly_profit is not None
            and p.monthly_profit.point > 0
            and not p.already_running
            and p.opportunity_id not in blocked
        ),
        None,
    )
    final = tuple(
        p.model_copy(update={"rank": i + 1, "preselected": p.opportunity_id == best})
        for i, p in enumerate(ranked)
    )
    notes: list[str] = [
        "Money is extra profit over the next 30 days if each person gets one message. The range puts "
        "the low ends of every input together, and the high ends together, so it is wider than any one "
        "95% interval; people can be in more than one group, so the groups do not add up."
    ]
    if not margin.from_data:
        notes.append(f"Margin assumed at {margin.rate:.0%}; tell us yours and every number updates.")
    notes.extend(blocked[p.opportunity_id] for p in final if p.opportunity_id in blocked)
    return OpportunityScan(
        as_of=now,
        currency=money,
        proposals=final,
        preselected_id=best,
        history_days=round(h.history_days(), 1),
        buyers=h.buyers(),
        orders=len(h.all_purchases()),
        enough_history=shop_gap is None,
        not_enough=shop_gap,
        margin=margin,
        skipped=tuple(skipped),
        notes=tuple(notes),
    )


def proposal_goal(proposal: Proposal, business_id: str, now: datetime) -> Goal:
    """A draft goal for a proposal (nothing runs until it is approved)."""
    return Goal(
        goal_id=proposal.opportunity_id,
        business_id=business_id,
        text=proposal.goal_text,
        kind=proposal.kind,
        budget=proposal.budget,
        currency=proposal.currency,
        starts_at=now,
        created_at=now,
    )
