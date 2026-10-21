"""Limits learned from the shop's own history, confirmed once, signed as the mandate (ZERO_TOUCH item 2).

`learn_limits(history, now, proposal=...)` reads the shop's past and proposes each hard limit as
"what we found -> what we will use":

| Limit | Found in | Used as |
|---|---|---|
| How often to message | messages each customer received a week over the last 8 weeks (Klaviyo deliveries; our own sends once we run) | our cap per customer in any 7 days, on top of theirs: their own median a week rounded up, at least 1 and at most the documented starting cap of 2; 1 when they already send 2 or more, so we at most match what customers already get |
| Discount depth | Shopify orders' discounts over the last 12 months: the median share taken off a discounted order | the deepest offer we may ever make: their usual depth, rounded down to a whole percent. Offers stay off at the start unless the owner turns them on |
| Discount frequency | the share of orders that used a discount code | shown, not enforced: a mandate has no per-customer offer frequency field yet. With offers off nothing is offered at all |
| Margin | the orders' margins when at least half carry one; else the shop's answer, asked once; else the decision engine's 40% default, flagged | the margin money is sized with, and the margin floor for any offer: margin less the deepest offer |
| Quiet hours | customers' own time zones (Klaviyo profiles) and the local hours of their opens and clicks | no message from 21:00 to 09:00 in each customer's own zone (the documented default); the most common known zone for anyone whose zone is unknown |
| Budget | what the shop spends today: discounts given a month (Shopify) and messages a month (Klaviyo) | the chosen plan's 30-day cost at most: its messages (2 each), plus, only when offers are on, the offers everyone who buys would redeem, never above today's monthly discounts |

Where the data says nothing, the conservative default is used and the limit is flagged
(`from_data=False`), so a person sees exactly which limits are the shop's own and which are ours.

`mandate_from_limits(...)` turns the accepted terms into a `Mandate`, and `sign_as_owner(...)` signs
it. The two-person rule (`autonomy.mandate.SignaturePolicy`) stays the default. A one-person shop
can use the **single-owner path** (`SINGLE_OWNER_POLICY`): the owner signs alone as marketing lead,
and the mandate's signed `note` says so, so the ledger shows which path was used. It covers only
budgets at or below the finance threshold (10,000 in the mandate's currency): above it a second
person in a finance role must still sign, because one person can never fill both roles.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Final, Literal
from zoneinfo import ZoneInfo

from pydantic import Field

from autonomy.contracts import (
    NO_OFFER,
    Channel,
    OfferKind,
    OfferOption,
    stable_id,
)
from autonomy.contracts.base import Contract
from autonomy.mandate import (
    FrequencyCap,
    Mandate,
    MandateStatus,
    QuietHours,
    SignaturePolicy,
    SignerRole,
)
from autonomy.mandate.signing import KeyRing, Signer, sign_mandate, verify_mandate, with_signature
from autonomy.onboarding.history import MESSAGE_CHANNELS, ShopHistory, quantile
from autonomy.onboarding.opportunities import MESSAGES_COSTED, MarginReading, Proposal, margin_reading
from autonomy.onboarding.priors import CHANNEL_COSTS, DEFAULT_FREQUENCY, DEFAULT_QUIET

__all__ = [
    "CADENCE_WEEKS",
    "DISCOUNT_DAYS",
    "MIN_DISCOUNTED_ORDERS",
    "SINGLE_OWNER_NOTE",
    "SINGLE_OWNER_POLICY",
    "LearnedLimit",
    "LearnedLimits",
    "MandateTerms",
    "learn_limits",
    "mandate_from_limits",
    "offer_for",
    "sign_as_owner",
]

CADENCE_WEEKS: Final[int] = 8
DISCOUNT_DAYS: Final[int] = 365
SPEND_DAYS: Final[int] = 90
MIN_RECIPIENTS: Final[int] = 30
"""People who received messages before their cadence is trusted."""
MIN_DISCOUNTED_ORDERS: Final[int] = 20
"""Discounted orders before their usual depth is trusted."""
MIN_ZONED: Final[int] = 30
"""Customers with a known time zone before the fallback zone is taken from them."""
MANDATE_DAYS: Final[int] = 30

SINGLE_OWNER_POLICY: Final[SignaturePolicy] = SignaturePolicy(min_distinct_signers=1)
"""One person, signing as marketing lead, is enough up to the finance threshold (see the module)."""
SINGLE_OWNER_NOTE: Final[str] = (
    "Signed by the shop's sole owner under the single-owner path: one signer, budget at or under "
    "the finance threshold."
)
TWO_PERSON_NOTE: Final[str] = "Proposed from the shop's own history at setup; needs a second signer."

LimitKey = Literal["cadence", "discount_depth", "discount_frequency", "margin", "quiet_hours", "budget"]


class LearnedLimit(Contract):
    key: LimitKey
    label: str
    found: str = Field(description="What we found, one sentence")
    use: str = Field(description="What we will use, one sentence")
    description: str = Field(description="The longer explanation, for a hover or tap")
    source: str
    from_data: bool = Field(description="False: a conservative default is used, flagged")
    question: str | None = Field(default=None, description="Asked once when only the shop can answer")


class MandateTerms(Contract):
    """The limits as values: what `mandate_from_limits` signs."""

    max_touches: int = Field(ge=1)
    window_days: int = Field(default=7, ge=1, le=30)
    quiet_start: int = Field(ge=0, le=23)
    quiet_end: int = Field(ge=0, le=23)
    timezone: str
    margin_rate: float = Field(ge=0, le=1)
    margin_floor_pct: float = Field(ge=0, lt=1)
    max_offer_pct: float | None = Field(
        default=None, description="The deepest offer ever allowed; None: no offers"
    )
    offers_on: bool = False
    budget_ceiling: float = Field(ge=0)
    currency: str
    channels: tuple[Channel, ...]
    valid_days: int = MANDATE_DAYS


class LearnedLimits(Contract):
    as_of: datetime
    currency: str
    opportunity_id: str | None
    limits: tuple[LearnedLimit, ...]
    terms: MandateTerms
    questions: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


# --- each limit ----------------------------------------------------------------------------------------


def _cadence(h: ShopHistory) -> tuple[LearnedLimit, int]:
    received = h.received_since(h.window(CADENCE_WEEKS * 7))
    weekly = [len(rows) / CADENCE_WEEKS for rows in received.values()]
    ours = sum(1 for rows in received.values() for d in rows if d.ours)
    total = sum(len(rows) for rows in received.values())
    default = DEFAULT_FREQUENCY.max_touches
    if len(weekly) < MIN_RECIPIENTS:
        return (
            LearnedLimit(
                key="cadence",
                label="How often we message",
                found=f"Too little send history to read your rhythm ({len(weekly)} people received a message "
                f"in the last {CADENCE_WEEKS} weeks).",
                use=f"At most {default} messages a week per customer, the starting default.",
                description="We read how many messages each customer received a week from your email tool. "
                "Without enough of it we start at the documented default cap and you can lower it.",
                source="autonomy/mandate FrequencyCap default",
                from_data=False,
            ),
            default,
        )
    median = statistics.median(weekly)
    p90 = quantile(weekly, 0.9)
    # Ours on top of theirs: no more than they already send (rounded up), within 1..default; only 1
    # when they already send the default or more. (Their own Klaviyo messages arrive as deliveries,
    # which the mandate's frequency cap does not count, so this cap is on our messages alone.)
    cap = 1 if median >= default else min(default, max(1, math.ceil(median - 1e-9)))
    who = "yours and ours" if ours else "from your own campaigns and flows"
    return (
        LearnedLimit(
            key="cadence",
            label="How often we message",
            found=f"Customers received a median of {median:.1f} messages a week ({who}; 90% got at most "
            f"{p90:.1f}), across {len(weekly):,} people over {CADENCE_WEEKS} weeks.",
            use=f"At most {cap} message{'s' if cap != 1 else ''} of ours a week per customer, on top of yours.",
            description=f"Counted from {total:,} delivered messages. We add no more than you already send a "
            f"week (rounded up), at least 1 and at most the starting cap of {default}; and only 1 when you "
            f"already send {default} or more.",
            source="your email tool's delivery history",
            from_data=True,
        ),
        cap,
    )


def _discounts(h: ShopHistory) -> tuple[LearnedLimit, LearnedLimit, float | None]:
    since = h.window(DISCOUNT_DAYS)
    orders = [p for p in h.all_purchases() if p.ts > since]
    reported = [p for p in orders if p.discount is not None or p.with_offer]
    discounted = [p for p in orders if (p.discount or 0.0) > 0 or p.with_offer]
    depths = [
        p.discount / (p.value + p.discount)
        for p in discounted
        if p.discount is not None and p.discount > 0 and p.value + p.discount > 0
    ]
    if not reported or len(depths) < MIN_DISCOUNTED_ORDERS:
        if not reported:
            why = "Your order history does not say which orders had a discount."
        elif discounted and not depths:
            why = (
                f"{len(discounted):,} orders used a discount, but the orders do not say how much it took off."
            )
        else:
            why = f"Only {len(depths)} discounted orders in the last 12 months; we need {MIN_DISCOUNTED_ORDERS}."
        depth_limit = LearnedLimit(
            key="discount_depth",
            label="Discounts",
            found=why,
            use="No discounts: messages carry no offer until proof shows an offer pays.",
            description="We only allow offers as deep as the ones you already give. Without that history "
            "the plan runs with no offer at all, which is the conservative start.",
            source="default: no offers",
            from_data=False,
        )
        freq_limit = LearnedLimit(
            key="discount_frequency",
            label="How often you discount",
            found=(
                "Not enough discount history to read." if reported else "Not reported by your order history."
            ),
            use="No offers are sent, so there is nothing to limit yet.",
            description="A mandate has no per-customer offer frequency limit yet; with offers off none is made.",
            source="default: no offers",
            from_data=False,
        )
        return depth_limit, freq_limit, None
    median = statistics.median(depths)
    usual = math.floor(median * 100) / 100
    share = len(discounted) / len(orders)
    depth_limit = LearnedLimit(
        key="discount_depth",
        label="Discounts",
        found=f"Your discounted orders took a median {median:.0%} off ({len(depths):,} orders, last 12 months).",
        use=f"Never deeper than {usual:.0%}. Offers stay off at the start; turning them on is one switch.",
        description="The deepest offer the agent may ever make is your own usual depth. Even then it offers "
        "one only to people the evidence says need it, and the margin floor below still applies.",
        source="your orders' discounts",
        from_data=True,
    )
    freq_limit = LearnedLimit(
        key="discount_frequency",
        label="How often you discount",
        found=f"{share:.0%} of your {len(orders):,} orders in the last 12 months used a discount.",
        use="Shown for reference: the mandate cannot cap offers per customer yet, so offers start off.",
        description="This is not a hard limit: a mandate has no per-customer offer frequency field. The "
        "message cap and the agent's own rule (offer only when it pays for itself) are what hold it.",
        source="your orders' discounts",
        from_data=True,
    )
    return depth_limit, freq_limit, usual


def _margin(reading: MarginReading, depth: float | None) -> tuple[LearnedLimit, float]:
    floor = max(0.0, round(reading.rate - (depth or 0.0), 2)) if depth is not None else 0.0
    use_floor = (
        f"an offer may never take an order below a {floor:.0%} margin"
        if depth is not None
        else "no offers, so no margin floor is needed yet"
    )
    if reading.from_data:
        found = (
            f"{reading.rate:.0%} gross margin from your answer."
            if reading.source == "your answer"
            else f"{reading.rate:.0%} gross margin across {reading.orders_with_margin:,} orders that carry one."
        )
        return (
            LearnedLimit(
                key="margin",
                label="Margin",
                found=found,
                use=f"Size every goal at {reading.rate:.0%}; {use_floor}.",
                description="Margin turns extra orders into extra profit, and sets the floor any offer must "
                "respect: your margin less your usual discount.",
                source=reading.source,
                from_data=True,
            ),
            floor,
        )
    return (
        LearnedLimit(
            key="margin",
            label="Margin",
            found="Your orders do not carry product costs, so we cannot see your margin.",
            use=f"Assume {reading.rate:.0%} until you tell us; {use_floor}.",
            description="40% is the default the decision engine uses for a customer with no margin data. "
            "Answer once and every number on this page updates.",
            source=reading.source,
            from_data=False,
            question="What is your typical gross margin, as a percentage of the order value?",
        ),
        floor,
    )


def _quiet(h: ShopHistory) -> tuple[LearnedLimit, str]:
    zones: Counter[str] = Counter()
    for state in h.states.values():
        zone = state.timezone
        if zone and zone != "UTC":
            try:
                ZoneInfo(zone)
            except (KeyError, ValueError):
                continue
            zones[zone] += 1
    hours = h.engagement_hours()
    engaged = sum(hours)
    quiet = sum(n for hour, n in enumerate(hours) if DEFAULT_QUIET.is_quiet(hour))
    engaged_words = (
        f" {quiet / engaged:.0%} of their {engaged:,} opens and clicks fell in those hours."
        if engaged
        else ""
    )
    start, end = DEFAULT_QUIET.start, DEFAULT_QUIET.end
    known = sum(zones.values())
    total = len(h.states)
    if known >= MIN_ZONED:
        top, n = max(sorted(zones.items()), key=lambda kv: kv[1])
        return (
            LearnedLimit(
                key="quiet_hours",
                label="Quiet hours",
                found=f"{known:,} of {total:,} customers have a known time zone; the most common is {top} "
                f"({n / known:.0%}).",
                use=f"No messages from {start:02d}:00 to {end:02d}:00 in each customer's own time zone; {top} "
                "for anyone whose zone we do not know.",
                description="Time zones come from your Klaviyo profiles. The quiet window is the documented "
                f"default.{engaged_words}",
                source="your customers' profiles",
                from_data=True,
            ),
            top,
        )
    return (
        LearnedLimit(
            key="quiet_hours",
            label="Quiet hours",
            found=f"Only {known:,} of {total:,} customers have a known time zone.",
            use=f"No messages from {start:02d}:00 to {end:02d}:00, in UTC for anyone whose zone we do not know.",
            description="With few known time zones we fall back to UTC, the safe choice for a shop we cannot "
            f"place yet.{engaged_words}",
            source="default: autonomy/mandate QuietHours",
            from_data=False,
        ),
        DEFAULT_QUIET.timezone,
    )


def _channels(h: ShopHistory) -> tuple[Channel, ...]:
    found: set[Channel] = set()
    for state in h.states.values():
        for ch in MESSAGE_CHANNELS:
            stats = state.channels.get(ch)
            if stats is not None and stats.contactable:
                found.add(ch)
    return tuple(ch for ch in MESSAGE_CHANNELS if ch in found) or (Channel.EMAIL,)


def offer_for(depth: float, order_value: float) -> OfferOption:
    """The one offer the mandate may approve: the shop's usual percentage off, priced on its order."""
    pct = round(depth * 100)
    return OfferOption(
        offer_id=f"pct{pct}",
        label=f"{pct}% off",
        kind=OfferKind.PERCENT_OFF,
        amount=float(pct),
        cost_per_redemption=round(order_value * depth, 2),
    )


def _budget(
    h: ShopHistory,
    proposal: Proposal | None,
    currency: str,
    *,
    depth: float | None,
    offers_on: bool,
) -> tuple[LearnedLimit, float]:
    since = h.window(SPEND_DAYS)
    recent = [p for p in h.all_purchases() if p.ts > since]
    discount_month = sum(p.discount or 0.0 for p in recent) * 30 / SPEND_DAYS
    delivered = sum(len(rows) for rows in h.received_since(h.window(CADENCE_WEEKS * 7)).values())
    messages_month = delivered * 30 / (CADENCE_WEEKS * 7)
    email = CHANNEL_COSTS.get(Channel.EMAIL, 0.0)
    found = (
        f"About {discount_month:,.0f} {currency} a month in discounts and {messages_month:,.0f} messages a month "
        f"(last {SPEND_DAYS} days of orders, last {CADENCE_WEEKS} weeks of sends)."
    )
    if proposal is None or proposal.monthly_profit is None:
        return (
            LearnedLimit(
                key="budget",
                label="Budget",
                found=found,
                use="1 " + currency + " for the first 30 days until a goal is sized: enough to start Shadow, "
                "which sends nothing.",
                description="The budget is what the plan may spend in 30 days. Without a sized goal we keep "
                "it at the smallest amount.",
                source="default: smallest budget",
                from_data=False,
            ),
            1.0,
        )
    messages = proposal.message_cost or 0.0
    offers = 0.0
    if (
        offers_on
        and depth is not None
        and proposal.base_rate is not None
        and proposal.extra_orders is not None
    ):
        aov = h.mean_order_value() or 0.0
        buyers_high = proposal.reached_in_month * proposal.base_rate.high + proposal.extra_orders.high
        offers = min(buyers_high * aov * depth, discount_month)
    ceiling = float(max(1, math.ceil(messages + offers)))
    share = (
        f", {ceiling / discount_month:.0%} of what you give in discounts today" if discount_month > 0 else ""
    )
    offer_words = f" plus up to {offers:,.0f} {currency} of offers" if offers > 0 else " and no offers"
    return (
        LearnedLimit(
            key="budget",
            label="Budget",
            found=found,
            use=f"{ceiling:,.0f} {currency} for the first 30 days: {proposal.reached_in_month:,} people x "
            f"{MESSAGES_COSTED} messages{offer_words}{share}.",
            description=f"Messages are costed at about {email:.3f} {currency} per e-mail (documented default). "
            "Offers, when on, are budgeted for everyone who would buy and redeem them, and never above "
            "what you already give in discounts in a month.",
            source="your orders and sends, and the chosen goal",
            from_data=True,
        ),
        ceiling,
    )


def learn_limits(
    history: ShopHistory,
    now: datetime,
    *,
    proposal: Proposal | None = None,
    currency: str | None = None,
    margin_rate: float | None = None,
    offers_on: bool = False,
) -> LearnedLimits:
    """Each limit as found -> used, and the terms a mandate would sign (see the module docstring)."""
    money = currency or (proposal.currency if proposal is not None else history.currency())
    cadence, cap = _cadence(history)
    depth_limit, freq_limit, depth = _discounts(history)
    reading = margin_reading(history, margin_rate)
    margin, floor = _margin(reading, depth)
    quiet, zone = _quiet(history)
    allow = offers_on and depth is not None
    budget, ceiling = _budget(history, proposal, money, depth=depth, offers_on=allow)
    limits = (cadence, depth_limit, freq_limit, margin, quiet, budget)
    terms = MandateTerms(
        max_touches=cap,
        window_days=DEFAULT_FREQUENCY.window_days,
        quiet_start=DEFAULT_QUIET.start,
        quiet_end=DEFAULT_QUIET.end,
        timezone=zone,
        margin_rate=reading.rate,
        margin_floor_pct=floor,
        max_offer_pct=depth,
        offers_on=allow,
        budget_ceiling=ceiling,
        currency=money,
        channels=_channels(history),
    )
    notes: list[str] = []
    if offers_on and depth is None:
        notes.append(
            "Offers were asked for, but there is no discount history to set their depth; they stay off."
        )
    return LearnedLimits(
        as_of=now,
        currency=money,
        opportunity_id=proposal.opportunity_id if proposal is not None else None,
        limits=limits,
        terms=terms,
        questions=tuple(q for lim in limits if (q := lim.question) is not None),
        notes=tuple(notes),
    )


# --- the mandate ---------------------------------------------------------------------------------------


def mandate_from_limits(
    terms: MandateTerms,
    *,
    business_id: str,
    goal_ids: Sequence[str],
    now: datetime,
    created_by: str,
    offers: Sequence[OfferOption] = (),
    template_ids: Sequence[str] = (),
    single_owner: bool,
) -> Mandate:
    """A draft mandate holding exactly the accepted terms (sign it with `sign_as_owner`)."""
    return Mandate(
        mandate_id=stable_id("mandate", business_id, *goal_ids, now.isoformat()),
        business_id=business_id,
        goal_ids=tuple(goal_ids),
        budget_ceiling=terms.budget_ceiling,
        currency=terms.currency,
        margin_floor_pct=terms.margin_floor_pct,
        quiet_hours=QuietHours(start=terms.quiet_start, end=terms.quiet_end, timezone=terms.timezone),
        frequency_cap=FrequencyCap(max_touches=terms.max_touches, window_days=terms.window_days),
        channels=terms.channels,
        approved_template_ids=tuple(template_ids),
        approved_offer_ids=tuple(o.offer_id for o in offers if o.offer_id != NO_OFFER.offer_id),
        valid_from=now,
        valid_until=now + timedelta(days=terms.valid_days),
        status=MandateStatus.DRAFT,
        created_by=created_by,
        note=SINGLE_OWNER_NOTE if single_owner else TWO_PERSON_NOTE,
    )


def sign_as_owner(
    mandate: Mandate,
    signer: Signer,
    keys: KeyRing,
    *,
    owner_id: str,
    policy: SignaturePolicy,
    now: datetime,
) -> tuple[Mandate, list[str]]:
    """The owner's signature as marketing lead; (the mandate, what is still unmet under `policy`).

    The mandate is `active` once the policy is met, else `pending_signatures`."""
    signature = sign_mandate(mandate, signer, person_id=owner_id, role=SignerRole.MARKETING_LEAD, now=now)
    signed = with_signature(mandate, signature, policy=policy, keys=keys)
    return signed, verify_mandate(signed, policy, keys)
