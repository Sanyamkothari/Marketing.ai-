"""The offer planner: spend the offer budget on the customers it pays most for, all at once.

The per-customer bidder decides one customer at a time, and a budget that is spent first-come,
first-served goes to whoever happens to be decided first. The planner looks at everyone together.
For every customer it takes the offers that would add profit over the plain message (after paying
for the offer), ranks the pairs by extra profit per unit of offer cost, and fills the budget from the
top. A cheap offer that works as well as a dear one wins (free shipping at 40 beats 20% off at 180 for
a customer who responds to both), and the budget goes where each unit buys the most.

* Sure things never get an offer. A customer who would buy anyway is paid the offer cost for nothing:
  their extra profit from any offer is below zero, so no offer of theirs is a candidate.
* One offer per customer. A customer's offers are walked up in steps (the cheapest that pays, then an
  upgrade only if the extra cost buys more profit per unit than the planner's other pairs), which is
  the greedy answer to "one option per customer, one shared budget".
* Never over budget: a step that does not fit is skipped, and so is every step above it.
* `OfferPlan.price` is the planner's budget price: the ratio of the best pair left out for lack of
  budget (0 when everything that pays fits). A customer who takes the offer with the best
  `extra profit - price x cost` makes the planner's choice without seeing anyone else
  (`choose_at_price`). Day to day the price is moved by `pacing.BudgetPrice` and charged by the
  bidder (`GoalBidder(budget_price=...)`).

The planner only reads the envelope: an offer the envelope does not list, or that waits for approval,
is not a candidate, and the plan never exceeds the envelope's budget. Nothing here is on unless a
caller builds a plan: the v2 bidder and policy do not use it.

Pure and deterministic: no clock, no randomness; ties break on customer id and offer id.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cached_property

from autonomy.contracts import NO_OFFER, Bid, CustomerState, Envelope, OfferKind, OfferOption
from autonomy.decide import rules
from autonomy.mandate.catalogue import offer_cost

SURE_THING = "sure_thing"
"""Every offer of this customer has negative (or no) extra profit: they would buy anyway."""
OVER_BUDGET = "over_budget"
"""This customer had an offer that pays, but the budget was spent on pairs with a better ratio."""
BELOW_MIN_RATIO = "below_min_ratio"
"""This customer had an offer that pays, but none earned `min_ratio` per unit of cost."""
NO_CANDIDATE = "no_candidate"
"""A customer of the `audience` with no candidate at all (no offer bid, or every offer was ruled out)."""

_EPS = 1e-12


@dataclass(frozen=True)
class OfferCandidate:
    """One customer-offer pair: what the offer adds in profit, and what it costs to give.

    `extra_profit` is over the best plain alternative and already net of the offer's cost, so it is
    negative for a sure thing. `cost` is the expected offer spend: the cost of one redemption times
    the chance this customer redeems it.
    """

    customer_id: str
    offer_id: str
    extra_profit: float
    cost: float

    def __post_init__(self) -> None:
        if not (math.isfinite(self.extra_profit) and math.isfinite(self.cost)) or self.cost < 0:
            raise ValueError("a candidate needs a finite extra profit and a finite cost >= 0")

    @property
    def ratio(self) -> float:
        """Extra profit per unit of cost; infinite for a free offer that pays."""
        if self.cost <= 0:
            return math.inf if self.extra_profit > 0 else -math.inf
        return self.extra_profit / self.cost


@dataclass(frozen=True)
class Assignment:
    """The offer one customer gets in the plan."""

    customer_id: str
    offer_id: str
    extra_profit: float
    cost: float


@dataclass(frozen=True)
class OfferPlan:
    """The result of `plan_offers`.

    `price` is the budget price (see the module docstring); `left_out` maps each customer who had an
    offer that pays but did not get one to `OVER_BUDGET` (or `BELOW_MIN_RATIO`), each with none that
    pays to `SURE_THING`, and each of the `audience` with no candidate to `NO_CANDIDATE`.
    """

    assignments: tuple[Assignment, ...]
    budget: float
    spent: float
    price: float
    left_out: Mapping[str, str] = field(default_factory=dict)

    @property
    def extra_profit(self) -> float:
        return sum(a.extra_profit for a in self.assignments)

    @cached_property
    def _by_customer(self) -> dict[str, str]:
        return {a.customer_id: a.offer_id for a in self.assignments}

    def offer_for(self, customer_id: str) -> str:
        """The planned offer id for a customer; "none" when they get none."""
        return self._by_customer.get(customer_id, NO_OFFER.offer_id)


@dataclass(frozen=True)
class _Step:
    """One upgrade on a customer's ladder: from the previous rung to `candidate`."""

    customer_id: str
    level: int
    candidate: OfferCandidate
    extra_cost: float
    extra_profit: float

    @property
    def ratio(self) -> float:
        return math.inf if self.extra_cost <= 0 else self.extra_profit / self.extra_cost


def offer_economics(
    *, uplift_extra: float, value: float, redemption_cost: float, p_redeem: float
) -> tuple[float, float]:
    """(extra profit, expected offer spend) of an offer over the plain message.

    `uplift_extra` is the offer's uplift minus the plain message's, `value` what one conversion is
    worth, `redemption_cost` the cost of one redemption and `p_redeem` the chance this customer
    redeems: organic chance plus uplift, because an offer is paid by everyone who buys while it is
    open, including those who would have bought anyway. A sure thing has `uplift_extra` near 0, so
    the spend makes its extra profit negative.
    """
    spend = redemption_cost * p_redeem
    return uplift_extra * value - spend, spend


def _bid_reaches(bid: Bid, state: CustomerState, envelope: Envelope) -> bool:
    """Whether the hard rules that depend on the offer and the channel let this bid through: the
    envelope lists the offer and it needs no approval, the customer takes discounts if it is one, and
    the channel can reach them. (Rules about the hour or the week are the daily loop's.)"""
    channel = bid.action.channel
    return all(
        r.allowed
        for r in (
            rules.channel_in_envelope(bid, envelope),
            rules.offer_in_envelope(bid, envelope),
            rules.offer_approved(bid, envelope),
            rules.no_discounts(bid, state, envelope),
            rules.unsubscribed(state, channel),
            rules.consent(state, channel),
            rules.contactable(state, channel),
        )
    )


def _plain_net(bid: Bid) -> float:
    """The bid's expected net without its urgency, in the same money as `Bid.offer_cost`."""
    return bid.expected_net / bid.urgency if bid.urgency > 0 else bid.expected_net


def candidates_from_bids(
    customer_id: str,
    bids: Iterable[Bid],
    *,
    state: CustomerState | None = None,
    envelope: Envelope | None = None,
) -> list[OfferCandidate]:
    """Candidates for one customer from the bids `GoalBidder.quote` returned, quoted with no budget price.

    The reference is the best plain bid (no offer), or doing nothing (0) when no plain bid is worth
    sending. Each offer is that offer's best bid, so the planner sees one candidate per offer. The
    bidder scales `expected_net` by the goal's urgency but not `offer_cost`, so the net is read
    without it: extra profit and cost are then in the same money, and the plan's price is a price per
    unit of offer cost. With the customer's `state` and the `envelope`, bids the hard rules would block
    (a discount to a customer who asked for none, a channel they cannot be reached on, an offer that
    waits for approval) are dropped first, so the plan never spends budget on them.
    """
    plain = 0.0
    best: dict[str, tuple[float, Bid]] = {}
    for bid in bids:
        if state is not None and envelope is not None and not _bid_reaches(bid, state, envelope):
            continue
        offer_id = bid.action.offer_id
        net = _plain_net(bid)
        if offer_id == NO_OFFER.offer_id:
            plain = max(plain, net)
        elif offer_id not in best or net > best[offer_id][0]:
            best[offer_id] = (net, bid)
    return [
        OfferCandidate(customer_id, offer_id, net - plain, bid.offer_cost)
        for offer_id, (net, bid) in sorted(best.items())
    ]


def _ladder(options: Sequence[OfferCandidate], min_ratio: float) -> list[OfferCandidate]:
    """A customer's efficient offers, cheapest first, with falling profit per extra unit of cost.

    This is the upper convex hull of (cost, extra profit) from "no offer" at (0, 0): an offer that
    costs more and earns no more than another is dropped, and so is one whose step up is worth less
    per unit than the step above it (the customer jumps straight to the better one).
    """
    pays = [o for o in options if o.extra_profit > _EPS and o.extra_profit >= min_ratio * o.cost]
    pays.sort(key=lambda o: (o.cost, -o.extra_profit, o.offer_id))
    hull: list[OfferCandidate] = []
    for o in pays:
        if hull and o.extra_profit <= hull[-1].extra_profit + _EPS:
            continue  # costs at least as much and earns no more
        while hull:
            prev_cost = hull[-2].cost if len(hull) > 1 else 0.0
            prev_profit = hull[-2].extra_profit if len(hull) > 1 else 0.0
            last = hull[-1]
            # Drop `last` when going from prev straight to `o` is at least as good per unit as the
            # step prev -> last: cross-multiplied so that equal costs never divide by zero.
            if (last.extra_profit - prev_profit) * (o.cost - prev_cost) <= (o.extra_profit - prev_profit) * (
                last.cost - prev_cost
            ):
                hull.pop()
            else:
                break
        hull.append(o)
    return hull


def plan_offers(
    candidates: Iterable[OfferCandidate],
    budget: float,
    *,
    envelope: Envelope | None = None,
    min_ratio: float = 0.0,
    audience: Iterable[str] = (),
) -> OfferPlan:
    """Fill `budget` with the customer-offer pairs that earn the most extra profit per unit of cost.

    Only pairs with positive extra profit are considered, and with `min_ratio` only those that earn
    at least `min_ratio` per unit of cost (0: any profit). With an `envelope`, offers it does not
    list or that need approval are dropped, and the budget is capped at the envelope's. A customer
    gets at most one offer. The plan never spends more than the budget. `audience` names every
    customer the plan is for: those with no candidate are reported in `left_out` as `NO_CANDIDATE`.
    """
    if not math.isfinite(budget) or budget < 0 or min_ratio < 0:
        raise ValueError("need a finite budget >= 0 and min_ratio >= 0")
    pool = list(candidates)
    if envelope is not None:
        budget = min(budget, envelope.budget)
        allowed = {
            o.offer_id for o in envelope.offers if not o.needs_approval and o.offer_id != NO_OFFER.offer_id
        }
        pool = [c for c in pool if c.offer_id in allowed]
    by_customer: dict[str, list[OfferCandidate]] = defaultdict(list)
    for c in pool:
        by_customer[c.customer_id].append(c)

    steps: list[_Step] = []
    left_out: dict[str, str] = {}
    for customer_id, options in by_customer.items():
        ladder = _ladder(options, min_ratio)
        if not ladder:
            pays = any(o.extra_profit > _EPS for o in options)
            left_out[customer_id] = BELOW_MIN_RATIO if pays else SURE_THING
            continue
        prev_cost = prev_profit = 0.0
        for level, c in enumerate(ladder):
            steps.append(_Step(customer_id, level, c, c.cost - prev_cost, c.extra_profit - prev_profit))
            prev_cost, prev_profit = c.cost, c.extra_profit
    steps.sort(key=lambda s: (-s.ratio, s.level, s.customer_id, s.candidate.offer_id))

    level_of: dict[str, int] = {}
    chosen: dict[str, OfferCandidate] = {}
    spent = 0.0
    price = 0.0
    for step in steps:
        if level_of.get(step.customer_id, 0) != step.level:
            continue  # a lower step of this customer did not fit, so neither does this one
        if spent + step.extra_cost <= budget:
            spent += step.extra_cost
            level_of[step.customer_id] = step.level + 1
            chosen[step.customer_id] = step.candidate
        elif price == 0.0:
            price = step.ratio
    assignments = tuple(
        Assignment(c.customer_id, c.offer_id, c.extra_profit, c.cost)
        for c in sorted(chosen.values(), key=lambda c: c.customer_id)
    )
    for customer_id in by_customer:
        if customer_id not in chosen:
            left_out.setdefault(customer_id, OVER_BUDGET)
    for customer_id in audience:
        if customer_id not in by_customer:
            left_out.setdefault(customer_id, NO_CANDIDATE)
    return OfferPlan(assignments, budget, spent, price, dict(sorted(left_out.items())))


def choose_at_price(options: Iterable[OfferCandidate], price: float) -> OfferCandidate | None:
    """One customer's choice when every unit of offer cost is charged `price`: the offer with the most
    `extra profit - price x cost`, or None when no offer is above zero.

    This is what a bidder charging the budget price does for a customer, and why a well-set price
    reproduces the plan. An offer with no extra profit is never chosen, so a sure thing gets none.
    """
    if price < 0:
        raise ValueError("a price cannot be negative")
    best: OfferCandidate | None = None
    best_score = 0.0
    for o in sorted(options, key=lambda o: (o.cost, o.offer_id)):
        if o.extra_profit <= _EPS:
            continue
        score = o.extra_profit - price * o.cost
        if score > best_score + _EPS:
            best, best_score = o, score
    return best


def basket_offer_cost(
    offer: OfferOption, state: CustomerState, default_basket: float | None = None
) -> float | None:
    """`GoalBidder`'s `offer_cost_fn` for the v3 cost model: what one redemption costs for this customer.

    A percent-off offer with no configured cost costs its percent of this customer's usual basket
    (their average order value). For a customer with no orders yet the shop's `default_basket` stands
    in; with neither known the offer cannot be priced and None is returned (the bidder then does not
    bid it), so a real discount is never counted as free. The other kinds do not depend on the
    customer.
    """
    basket = state.avg_order_value if state.avg_order_value > 0 else default_basket
    if (
        offer.kind is OfferKind.PERCENT_OFF
        and offer.cost_per_redemption <= 0
        and offer.amount > 0
        and (basket is None or basket <= 0)
    ):
        return None
    return offer_cost(offer, basket)


def basket_cost_fn(default_basket: float | None) -> Callable[[OfferOption, CustomerState], float | None]:
    """`basket_offer_cost` with the shop's typical order value as the fallback basket."""

    def cost(offer: OfferOption, state: CustomerState) -> float | None:
        return basket_offer_cost(offer, state, default_basket)

    return cost
