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
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from autonomy.contracts import NO_OFFER, Bid, CustomerState, Envelope, OfferOption
from autonomy.mandate.catalogue import offer_cost

SURE_THING = "sure_thing"
"""Every offer of this customer has negative (or no) extra profit: they would buy anyway."""
OVER_BUDGET = "over_budget"
"""This customer had an offer that pays, but the budget was spent on pairs with a better ratio."""

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
    offer that pays but did not get one to `OVER_BUDGET`, and each with none that pays to `SURE_THING`.
    """

    assignments: tuple[Assignment, ...]
    budget: float
    spent: float
    price: float
    left_out: Mapping[str, str] = field(default_factory=dict)

    @property
    def extra_profit(self) -> float:
        return sum(a.extra_profit for a in self.assignments)

    def offer_for(self, customer_id: str) -> str:
        """The planned offer id for a customer; "none" when they get none."""
        for a in self.assignments:
            if a.customer_id == customer_id:
                return a.offer_id
        return NO_OFFER.offer_id


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


def candidates_from_bids(customer_id: str, bids: Iterable[Bid]) -> list[OfferCandidate]:
    """Candidates for one customer from the bids `GoalBidder.quote` returned, quoted with no budget price.

    The reference is the best plain bid (no offer), or doing nothing (0) when no plain bid is worth
    sending. Each offer is that offer's best bid, so the planner sees one candidate per offer.
    """
    plain = 0.0
    best: dict[str, Bid] = {}
    for bid in bids:
        offer_id = bid.action.offer_id
        if offer_id == NO_OFFER.offer_id:
            plain = max(plain, bid.expected_net)
        elif offer_id not in best or bid.expected_net > best[offer_id].expected_net:
            best[offer_id] = bid
    return [
        OfferCandidate(customer_id, offer_id, bid.expected_net - plain, bid.offer_cost)
        for offer_id, bid in sorted(best.items())
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
) -> OfferPlan:
    """Fill `budget` with the customer-offer pairs that earn the most extra profit per unit of cost.

    Only pairs with positive extra profit are considered, and with `min_ratio` only those that earn
    at least `min_ratio` per unit of cost (0: any profit). With an `envelope`, offers it does not
    list or that need approval are dropped, and the budget is capped at the envelope's. A customer
    gets at most one offer. The plan never spends more than the budget.
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
            left_out[customer_id] = SURE_THING
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


def basket_offer_cost(offer: OfferOption, state: CustomerState) -> float:
    """`GoalBidder`'s `offer_cost_fn` for the v3 cost model: what one redemption costs for this customer.

    A percent-off offer costs its percent of this customer's usual basket (their average order value;
    unknown: the configured cost, or 0). The other kinds do not depend on the customer.
    """
    basket = state.avg_order_value
    return offer_cost(offer, basket if basket > 0 else None)
