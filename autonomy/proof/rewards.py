"""Rewards: what each decision earned, once its outcome window has closed.

A decision made at `made_at` is judged on the window `[made_at, made_at + horizon_days)`. When that
window has closed (its end is at or before `now`), the decision gets a `Reward`:

* **converted / revenue / margin** come from the customer's PURCHASE events inside the window. A
  purchase with no recorded margin counts `value x costs.margin_rate`.
* **cost** is the channel cost if the message was actually sent (a SEND or DELIVER event carries the
  decision's id; an ACT decision that never went out, as in shadow mode, cost nothing) plus the
  offer's cost when a purchase used the decision's offer (the purchase's `attrs["offer_id"]` matches
  the action's `offer_id`). A redemption is charged once, to one decision, however many windows it
  falls in.
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

Refunds are not subtracted yet: a refund rarely names the purchase it reverses.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

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

__all__ = ["Attribution", "Costs", "build_rewards", "proof_rewards"]

Attribution = Literal["window", "last_touch"]

_YES_NO_OBJECTIVES = frozenset(
    {Objective.CONVERSION, Objective.RETENTION, Objective.LEAD, Objective.ACTIVATION}
)


@dataclass(frozen=True)
class Costs:
    """What things cost, in the goal's currency.

    `channel`: cost of one message per channel. `offer`: cost per redemption per offer id.
    `margin_rate`: share of a purchase's value counted as margin when the event has no margin; the
    default 1.0 treats unknown margin as revenue, so set it from the business when you know it.
    `value_per_conversion`: what one conversion is worth, for yes/no objectives.
    """

    channel: Mapping[Channel, float] = field(default_factory=dict)
    offer: Mapping[str, float] = field(default_factory=dict)
    margin_rate: float = 1.0
    value_per_conversion: float | None = None

    @classmethod
    def from_brief(
        cls,
        brief: Brief,
        *,
        channel: Mapping[Channel, float] | None = None,
        margin_rate: float = 1.0,
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

    converted: bool = False
    revenue: float = 0.0
    margin: float = 0.0
    offer_cost: float = 0.0
    unsubscribed: bool = False
    complained: bool = False


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
        events = sorted(events_by_customer.get(customer_id, ()), key=lambda e: e.ts)
        tallies = _tally(mine, events, horizon, costs, attribution, cutoff)
        sent = {
            e.decision_id
            for e in events
            if e.decision_id is not None and e.type in (EventType.SEND, EventType.DELIVER)
        }
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

    Purchases at or after `cutoff` (when given) are not counted at all.
    """
    tallies = {d.decision_id: _Tally() for d in mine}
    for e in events:
        covering = [d for d in mine if _covers(d, e.ts, horizon)]
        if e.type is EventType.PURCHASE:
            if cutoff is not None and e.ts >= cutoff:
                continue
            _purchase(e, covering, tallies, costs, attribution)
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
) -> None:
    if not covering:
        return
    value = float(e.value or 0.0)
    margin = float(e.margin) if e.margin is not None else value * costs.margin_rate
    used = e.attrs.get("offer_id")
    used_offer = str(used) if used is not None and str(used) != "none" else None
    acts = [d for d in covering if d.kind is DecisionKind.ACT]
    with_offer = [d for d in acts if used_offer is not None and _offer_of(d) == used_offer]

    # The one decision that "owns" this purchase: it pays the offer cost, and under last touch it
    # is the only one credited with the money.
    owner = _named(e, covering) or _latest(with_offer) or _latest(acts) or _latest(covering)
    if owner is None:  # pragma: no cover - covering is non-empty
        return
    credited = [owner] if attribution == "last_touch" else list(covering)
    for d in credited:
        t = tallies[d.decision_id]
        t.converted = True
        t.revenue += value
        t.margin += margin
    if used_offer is not None and _offer_of(owner) == used_offer:
        tallies[owner.decision_id].offer_cost += float(costs.offer.get(used_offer, 0.0))


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
        cost=cost,
        net=net,
        unsubscribed=t.unsubscribed,
        complained=t.complained,
    )
