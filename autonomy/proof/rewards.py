"""Rewards: what each decision earned, once its outcome window has closed.

A decision made at `made_at` is judged on the window `[made_at, made_at + horizon_days)`. When that
window has closed (its end is at or before `now`), the decision gets a `Reward`:

* **converted / revenue / margin** come from the customer's PURCHASE events inside the window, less
  the refunds and voids that reverse them inside the same window (see "Refunds" below). A purchase
  with no recorded margin counts `value x` the shop's margin rate (`Costs.margin_rate`), and the
  part of a reward's margin that came from that assumption is in `Reward.margin_assumed`.
* **cost** is the channel cost if the message was actually sent (a SEND or DELIVER event carries the
  decision's id; an ACT decision that never went out, as in shadow mode, cost nothing) plus the
  offer's cost when a purchase used the decision's offer (the purchase's `attrs["offer_id"]` matches
  the action's `offer_id`). A redemption is charged once, to one decision, however many windows it
  falls in. A SEND does **not** carry the channel cost when the message then failed and was never
  delivered (`sent_decisions`): a FAIL for the decision at or after its last SEND and no DELIVER.
  That is how a hand-off the customer's own tool never delivered reads (Klaviyo: a FAIL with
  `reason=not_delivered_by_tool`, from the delivery watch, or Klaviyo's own Dropped Email). A
  BOUNCE or BLOCK keeps its cost: the provider sent the message and bills it (SES charges bounced
  mail, carriers charge undelivered SMS), and leaving it out would overstate profit. A provider
  failure that a later retry got past is followed by a SEND, so it does not count against it.

**Intent to treat.** Who is agent, baseline or holdout is fixed when the customer is assigned
(`autonomy.proof.split`) and every decision keeps its group, whatever happened to the message.
A decision whose message was not delivered is still an agent (or baseline) decision: its reward
keeps the purchases in its window, and only the channel cost it never incurred is dropped.
Moving undelivered customers out of the agent group, or counting only the delivered ones, would
compare the customers a tool chose to deliver to (often the more engaged) against a random
holdout, and overstate the lift. The comparison therefore measures the effect of deciding to
contact, including the share of messages the tool drops; learning, which needs the effect of a
message received, reads the same events and leaves undelivered sends out
(`autonomy.learning.observations`).
* **net** depends on the objective: margin - cost for gross profit, revenue - cost for revenue, and
  for yes/no objectives (conversion, retention, lead, activation) `converted x value_per_conversion
  - cost` when a value per conversion is given, else just 1.0 or 0.0.
* **unsubscribed / complained** are set from UNSUBSCRIBE / COMPLAINT events that carry the decision's
  id, or, with no id, that fall in the window of the latest ACT decision before them.

"Do nothing" decisions get rewards too, with no cost and no goal. These are "virtual" rewards: what
the customer did when we left them alone. They are the untreated side of every comparison, and the
whole of the holdout group's record.

**Two ways to count a purchase** (`attribution`):

* `"window"` (the default): a purchase counts for *every* decision whose window holds it. This is the
  right reward for learning and for off-policy evaluation: each decision is judged on what happened
  in the horizon after it, whatever came later. Daily decisions with a 14-day horizon mean one
  purchase appears in up to 14 rewards, so **never sum these rewards per customer**.
* `"last_touch"`: each purchase counts for exactly one decision: the one it names in `decision_id`,
  else the latest ACT decision whose window holds it and whose offer it used, else the latest ACT
  decision, else the latest decision of any kind. Summing these rewards per customer gives the
  customer's purchases over the period without double counting, which is what `proof_summary`
  needs. Use `proof_rewards()`.

  Last touch only counts purchases made before `now - horizon_days`, the same cutoff for every
  customer. Without it the measured period would depend on the group: an agent customer's purchase
  goes to an earlier ACT decision whose window has already closed, while a holdout customer's goes
  to that day's "do nothing", whose window is still open. Agent customers would then have more days
  of purchases counted than holdout customers, and the proof would show money the agent never made.
  Any purchase before the cutoff belongs to a decision whose window has closed, so nothing is lost
  either.

**Refunds.** A REFUND event reverses a purchase of the same customer: the one whose `attrs["order_id"]`
it names (Shopify refunds and voids both do), else, when it names none, the customer's latest earlier
purchase with exactly the refund's amount left to refund, else the latest earlier purchase that still has
money left (a partial refund naming no order can still land on the wrong purchase). Every PURCHASE of the customer is a candidate, including
one that no decision was credited with (made before the first decision, outside every window, or left
out by the last-touch cutoff): a refund of such a purchase then finds it and changes nothing, rather
than reversing an earlier purchase that was earned. An order sent as several PURCHASE lines with the
same `order_id` is reversed across all its lines, each in proportion to what is left of it. The amount
is the size of the refund's `value`, whatever its sign (some sources write refunds as negative values).
The refunded share of a purchase's value comes off the revenue, and the same share of its margin comes
off the margin, of every decision that was credited with that purchase, **if the refund falls inside
that decision's window**. A refund after the window has closed is not netted: the window was judged on
what was known by its end. A REFUND naming no purchase we hold (an order from before the history) is
ignored. A purchase refunded in full no longer counts as a conversion for those decisions. A purchase
and a refund at the same instant are taken purchase first.

*Cash on delivery and return to origin (RTO).* A COD parcel the customer refuses comes back as a voided
or cancelled order, which the connectors already turn into a REFUND naming the order, so it is netted
like any refund. A REFUND whose `attrs` say `rto` (or `return_to_origin`) is true, or whose `reason` is
`rto` or `return_to_origin`, reverses the whole of the order whatever amount it carries. These attrs are a
convention for a loader or courier feed to set; no connector sets them yet. The lost
shipping on such a parcel is not charged separately: it is part of the shop's contribution margin
(`Costs.margin_rate`), which is meant to be after shipping, gateway fees and RTO.

An offer's cost is still charged when its order is later refunded: the discount was given.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final, Literal

from autonomy.contracts import (
    Brief,
    Channel,
    Decision,
    DecisionKind,
    Event,
    EventType,
    Objective,
    Reward,
)

__all__ = [
    "Attribution",
    "Costs",
    "build_rewards",
    "proof_rewards",
    "sent_decisions",
    "undelivered_decisions",
]

Attribution = Literal["window", "last_touch"]

_YES_NO_OBJECTIVES = frozenset(
    {Objective.CONVERSION, Objective.RETENTION, Objective.LEAD, Objective.ACTIVATION}
)


@dataclass(frozen=True)
class Costs:
    """What things cost, in the goal's currency.

    `channel`: cost of one message per channel. `offer`: cost per redemption per offer id.
    `margin_rate`: the shop's contribution margin (after shipping, gateway fees and RTO), the share
    of a purchase's value counted as margin when the event has no margin of its own. None means the
    shop has not said: purchases without a margin then count at their full value
    (`margin_share` is 1.0) and the proof says so. It is one setting for the whole shop
    (`autonomy.runtime.settings.RuntimeSettings.margin_rate`), the same one the agent's value model
    starts from. `value_per_conversion`: what one conversion is worth, for yes/no objectives.
    """

    channel: Mapping[Channel, float] = field(default_factory=dict)
    offer: Mapping[str, float] = field(default_factory=dict)
    margin_rate: float | None = None
    value_per_conversion: float | None = None

    @property
    def margin_share(self) -> float:
        """The share of a purchase's value counted as margin when it has none: the shop's rate, else 1.0."""
        return 1.0 if self.margin_rate is None else self.margin_rate

    @classmethod
    def from_brief(
        cls,
        brief: Brief,
        *,
        channel: Mapping[Channel, float] | None = None,
        margin_rate: float | None = None,
        value_per_conversion: float | None = None,
    ) -> Costs:
        """Offer costs from the brief's envelope (`cost_per_redemption`), channel costs as given."""
        return cls(
            channel=dict(channel or {}),
            offer={o.offer_id: o.cost_per_redemption for o in brief.envelope.offers},
            margin_rate=margin_rate,
            value_per_conversion=value_per_conversion,
        )


@dataclass
class _Tally:
    """The running totals for one decision while purchases are handed out."""

    live_orders: int = 0
    """Purchases credited here and not refunded in full inside the window."""
    revenue: float = 0.0
    margin: float = 0.0
    margin_assumed: float = 0.0
    offer_cost: float = 0.0
    unsubscribed: bool = False
    complained: bool = False

    @property
    def converted(self) -> bool:
        return self.live_orders > 0


@dataclass
class _Order:
    """One purchase (one line of an order), kept so that a later refund can reverse it.

    `credited` lists the decisions it was credited to; it is empty for a purchase no decision was
    credited with, which is kept only so that its refund finds it."""

    order_id: str | None
    ts: datetime
    value: float
    margin: float
    margin_assumed: float
    credited: list[Decision]
    refunded: float = 0.0
    gone: bool = False
    """Refunded in full: its conversion has been taken back."""

    @property
    def left(self) -> float:
        return 0.0 if self.gone else max(self.value - self.refunded, 0.0)


def build_rewards(
    decisions: Iterable[Decision],
    events_by_customer: Mapping[str, Sequence[Event]],
    horizon_days: int,
    objective: Objective,
    costs: Costs,
    *,
    now: datetime | None = None,
    attribution: Attribution = "window",
) -> list[Reward]:
    """One `Reward` per decision whose window has closed by `now`, in (window end, decision id) order.

    `now` is passed in, never read from the clock. When it is None, the latest event seen stands in
    for it: a window counts as closed only once the data reach past its end.
    """
    if horizon_days < 1:
        raise ValueError("horizon_days must be at least 1")
    horizon = timedelta(days=horizon_days)
    if now is None:
        seen = [e.ts for evs in events_by_customer.values() for e in evs]
        if not seen:
            return []
        now = max(seen)
    # Last touch measures every customer over the same period (see the module docstring).
    cutoff = now - horizon if attribution == "last_touch" else None

    by_customer: dict[str, list[Decision]] = {}
    seen_ids: set[str] = set()
    for d in decisions:
        if d.decision_id in seen_ids:  # a decision logged twice is still one decision
            continue
        seen_ids.add(d.decision_id)
        by_customer.setdefault(d.customer_id, []).append(d)

    out: list[Reward] = []
    for customer_id, mine in by_customer.items():
        mine.sort(key=lambda d: (d.made_at, d.decision_id))
        # A purchase and its refund at the same instant: the purchase first.
        events = sorted(
            events_by_customer.get(customer_id, ()),
            key=lambda e: (e.ts, e.type is EventType.REFUND),
        )
        tallies = _tally(mine, events, horizon, costs, attribution, cutoff)
        sent = sent_decisions(events)
        for d in mine:
            end = d.made_at + horizon
            if end > now:
                continue
            out.append(_reward(d, end, tallies[d.decision_id], d.decision_id in sent, objective, costs))
    out.sort(key=lambda r: (r.window_end, r.decision_id))
    return out


def proof_rewards(
    decisions: Iterable[Decision],
    events_by_customer: Mapping[str, Sequence[Event]],
    horizon_days: int,
    objective: Objective,
    costs: Costs,
    *,
    now: datetime | None = None,
) -> list[Reward]:
    """`build_rewards` with last-touch attribution: every purchase counted once, ready to sum.

    Only purchases before `now - horizon_days` count, for every customer alike. Pass
    `proof_summary` the same `now`; an earlier one brings back a group-dependent period.
    """
    return build_rewards(
        decisions, events_by_customer, horizon_days, objective, costs, now=now, attribution="last_touch"
    )


_UNDELIVERED: frozenset[EventType] = frozenset({EventType.FAIL, EventType.BOUNCE, EventType.BLOCK})
"""Outcomes that say a message did not reach the person (the same set learning uses)."""
_UNBILLED: frozenset[EventType] = frozenset({EventType.FAIL})
"""Outcomes that say the message never left (no provider bills it). A bounce or block was sent."""


def undelivered_decisions(
    events: Iterable[Event], types: frozenset[EventType] = _UNDELIVERED
) -> frozenset[str]:
    """Decisions whose message was reported undelivered after it was sent, and never delivered.

    An outcome in `types` (default FAIL, BOUNCE or BLOCK) at or after the decision's latest SEND,
    with no DELIVER at all: the customer's tool took the hand-off and did not deliver it
    (`not_delivered_by_tool`), or the provider dropped or bounced it. A FAIL before a later SEND is
    a retry that got through."""
    last_send: dict[str, datetime] = {}
    delivered: set[str] = set()
    failed: list[tuple[str, datetime]] = []
    for e in events:
        if e.decision_id is None:
            continue
        if e.type is EventType.SEND:
            seen = last_send.get(e.decision_id)
            last_send[e.decision_id] = e.ts if seen is None else max(seen, e.ts)
        elif e.type is EventType.DELIVER:
            delivered.add(e.decision_id)
        elif e.type in types:
            failed.append((e.decision_id, e.ts))
    return frozenset(
        d for d, ts in failed if d not in delivered and (d not in last_send or ts >= last_send[d])
    )


def sent_decisions(events: Iterable[Event]) -> frozenset[str]:
    """Decisions whose message counts as sent (and so carries its channel cost): a SEND or DELIVER,
    unless it then failed and was never delivered. A bounced or blocked message was sent and billed,
    so it stays sent here (`undelivered_decisions` with its default types also lists those)."""
    listed = list(events)
    went = {
        e.decision_id
        for e in listed
        if e.decision_id is not None and e.type in (EventType.SEND, EventType.DELIVER)
    }
    return frozenset(went - undelivered_decisions(listed, _UNBILLED))


def _covers(d: Decision, ts: datetime, horizon: timedelta) -> bool:
    return d.made_at <= ts < d.made_at + horizon


def _offer_of(d: Decision) -> str | None:
    if d.action is None or d.action.offer_id == "none":
        return None
    return d.action.offer_id


def _tally(
    mine: Sequence[Decision],
    events: Sequence[Event],
    horizon: timedelta,
    costs: Costs,
    attribution: Attribution,
    cutoff: datetime | None,
) -> dict[str, _Tally]:
    """Hand each purchase, unsubscribe and complaint to the decisions it belongs to.

    Purchases at or after `cutoff` (when given) are not counted at all. Every purchase is still kept in
    `orders`, so that a refund of an uncounted purchase finds it instead of reversing a counted one.
    """
    tallies = {d.decision_id: _Tally() for d in mine}
    orders: list[_Order] = []
    for e in events:
        covering = [d for d in mine if _covers(d, e.ts, horizon)]
        if e.type is EventType.PURCHASE:
            counted = cutoff is None or e.ts < cutoff
            orders.append(_purchase(e, covering if counted else (), tallies, costs, attribution))
        elif e.type is EventType.REFUND:
            _refund(e, orders, tallies, horizon)
        elif e.type in (EventType.UNSUBSCRIBE, EventType.COMPLAINT):
            owner = _named(e, mine) or _latest(d for d in covering if d.kind is DecisionKind.ACT)
            if owner is None:
                continue
            if e.type is EventType.UNSUBSCRIBE:
                tallies[owner.decision_id].unsubscribed = True
            else:
                tallies[owner.decision_id].complained = True
    return tallies


def _purchase(
    e: Event,
    covering: Sequence[Decision],
    tallies: dict[str, _Tally],
    costs: Costs,
    attribution: Attribution,
) -> _Order:
    """Credit one purchase to its decisions and return the `_Order` that records it (credited to no
    decision when no window holds it)."""
    value = float(e.value or 0.0)
    assumed = 0.0
    if e.margin is not None:
        margin = float(e.margin)
    else:
        margin = assumed = value * costs.margin_share
    used = e.attrs.get("offer_id")
    used_offer = str(used) if used is not None and str(used) != "none" else None
    acts = [d for d in covering if d.kind is DecisionKind.ACT]
    with_offer = [d for d in acts if used_offer is not None and _offer_of(d) == used_offer]

    # The one decision that "owns" this purchase: it pays the offer cost, and under last touch it
    # is the only one credited with the money.
    owner = _named(e, covering) or _latest(with_offer) or _latest(acts) or _latest(covering)
    credited: list[Decision] = []
    if owner is not None:
        credited = [owner] if attribution == "last_touch" else list(covering)
    for d in credited:
        t = tallies[d.decision_id]
        t.live_orders += 1
        t.revenue += value
        t.margin += margin
        t.margin_assumed += assumed
    if owner is not None and used_offer is not None and _offer_of(owner) == used_offer:
        tallies[owner.decision_id].offer_cost += float(costs.offer.get(used_offer, 0.0))
    named = e.attrs.get("order_id")
    return _Order(
        order_id=str(named) if named is not None else None,
        ts=e.ts,
        value=value,
        margin=margin,
        margin_assumed=assumed,
        credited=credited,
    )


_RTO_REASONS: Final[frozenset[str]] = frozenset({"rto", "return_to_origin"})
_CENT: Final[float] = 0.005
"""How close an unnamed refund's amount must be to what is left of a purchase to be taken as its refund."""


def _is_rto(e: Event) -> bool:
    """Whether a REFUND says the parcel came back undelivered, so the whole order is reversed."""
    for key in ("rto", "return_to_origin"):
        flag = e.attrs.get(key)
        if flag is True or str(flag).lower() in ("true", "1", "yes"):
            return True
    return str(e.attrs.get("reason") or "").lower() in _RTO_REASONS


def _refund(e: Event, orders: Sequence[_Order], tallies: dict[str, _Tally], horizon: timedelta) -> None:
    """Take a refund off the purchase it reverses (all the lines of a named order), for every decision
    whose window it falls in."""
    asked = abs(float(e.value or 0.0))
    named = e.attrs.get("order_id")
    if named is not None:
        lines = [o for o in orders if o.order_id == str(named)]
    else:
        earlier = [o for o in orders if o.ts <= e.ts and o.left > 0]
        exact = [o for o in earlier if math.isclose(o.left, asked, abs_tol=_CENT)]
        lines = (exact or earlier)[-1:]
    lines = [o for o in lines if o.value > 0 and o.left > 0]
    left = sum(o.left for o in lines)
    if left <= 0:
        return
    amount = left if _is_rto(e) else min(asked, left)
    if amount <= 0:
        return
    parts = [(o, amount * o.left / left) for o in lines]
    for order, part in parts:
        _reverse(order, part, e.ts, tallies, horizon)


def _reverse(
    order: _Order, amount: float, at: datetime, tallies: dict[str, _Tally], horizon: timedelta
) -> None:
    """Take `amount` off one purchase line, for the credited decisions whose window holds `at`."""
    order.refunded += amount
    share = amount / order.value
    whole = order.refunded >= order.value - 1e-9
    if whole:
        order.gone = True
    for d in order.credited:
        if not _covers(d, at, horizon):
            continue  # the window had closed: it was judged on what was known by its end
        t = tallies[d.decision_id]
        t.revenue -= amount
        t.margin -= order.margin * share
        t.margin_assumed -= order.margin_assumed * share
        if whole:
            t.live_orders -= 1


def _named(e: Event, candidates: Iterable[Decision]) -> Decision | None:
    if e.decision_id is None:
        return None
    return next((d for d in candidates if d.decision_id == e.decision_id), None)


def _latest(candidates: Iterable[Decision]) -> Decision | None:
    best: Decision | None = None
    for d in candidates:
        if best is None or (d.made_at, d.decision_id) > (best.made_at, best.decision_id):
            best = d
    return best


def _reward(d: Decision, end: datetime, t: _Tally, sent: bool, objective: Objective, costs: Costs) -> Reward:
    channel_cost = 0.0
    if d.action is not None and sent:
        channel_cost = float(costs.channel.get(d.action.channel, 0.0))
    cost = channel_cost + t.offer_cost
    if objective is Objective.GROSS_PROFIT:
        net = t.margin - cost
    elif objective is Objective.REVENUE:
        net = t.revenue - cost
    elif objective in _YES_NO_OBJECTIVES and costs.value_per_conversion is not None:
        net = (costs.value_per_conversion if t.converted else 0.0) - cost
    else:
        net = 1.0 if t.converted else 0.0
    return Reward(
        decision_id=d.decision_id,
        customer_id=d.customer_id,
        goal_id=d.action.goal_id if d.action is not None else None,
        window_end=end,
        converted=t.converted,
        revenue=t.revenue,
        margin=t.margin,
        margin_assumed=t.margin_assumed,
        cost=cost,
        net=net,
        unsubscribed=t.unsubscribed,
        complained=t.complained,
    )
