"""The autonomous policy: one customer in, one decision out, with its probability and a reason.

This is `decide()` from plan-v2 section 3, built only from maths (no LLM):

1. The three-way split comes first. Baseline customers go to the simple baseline rule; holdout
   customers are left alone with probability 1, because they are how we prove value.
2. Every goal's `Bidder` offers bids. Each bid gets its send time (the customer's best hour, never in
   quiet hours), then the hard rules remove what may not go out; their codes go in `blocked_by`.
3. Attention has a price (learned fatigue: unsubscribe risk x future value). It is subtracted from
   every acting bid, and "do nothing" joins as an option worth exactly 0, so a bid must beat it.
4. Thompson sampling chooses: each option's net value is drawn from Normal(mean, uncertainty) and the
   highest draw wins. With probability `exploration_floor` the choice is instead uniform over the
   options, so every option keeps getting tried.
5. The probability of the chosen option is logged, because off-policy evaluation and proof need it:

       p(option) = floor / n  +  (1 - floor) x P(option has the highest draw)

   The first term is exact. The second is estimated from `mc_draws` seeded Monte Carlo draws, and
   the draw that actually chose the option is one of them, so a chosen option never logs 0. With the
   default 1,000 draws the standard error of that share is at most 1.6 percentage points.

Everything random comes from one generator seeded by `(ctx.seed, customer_id)`, so a run repeats
exactly given its seed, and customers do not share draws.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

import numpy as np

from autonomy.contracts import (
    Bid,
    CustomerState,
    Decision,
    DecisionContext,
    DecisionKind,
    Group,
    Reward,
    stable_id,
)
from autonomy.decide import explain, rules
from autonomy.decide.timing import DEFAULT_HOUR, choose_send_time
from autonomy.interfaces import Bidder, Policy

DEFAULT_EXPLORATION_FLOOR = 0.05
DEFAULT_MC_DRAWS = 1000
DEFAULT_MAX_OPTIONS = 10
LOGGED_BIDS = 5

AttentionPrice = Callable[[CustomerState], float]


@runtime_checkable
class _Learns(Protocol):
    def learn(self, rewards: Sequence[Reward], decisions: Mapping[str, Decision]) -> None: ...


@runtime_checkable
class _Updates(Protocol):
    def update(self, rewards: Sequence[Reward], decisions: Mapping[str, Decision]) -> None: ...


@dataclass(frozen=True)
class Pick:
    """The outcome of one Thompson-with-floor choice over n options."""

    index: int
    probability: float = 1.0
    probabilities: tuple[float, ...] = (1.0,)
    explored: bool = False


def seeded_rng(seed: int, customer_id: str) -> np.random.Generator:
    """One generator per (seed, customer): repeatable runs, and no draws shared between customers."""
    digest = hashlib.sha256(customer_id.encode()).digest()
    return np.random.default_rng([seed & 0xFFFF_FFFF_FFFF_FFFF, int.from_bytes(digest[:8], "big")])


def thompson_with_floor(
    means: Sequence[float],
    sds: Sequence[float],
    floor: float,
    rng: np.random.Generator,
    draws: int = DEFAULT_MC_DRAWS,
) -> Pick:
    """Choose among options by Thompson sampling mixed with a uniform exploration floor.

    Ties go to the lowest index, so put the option you prefer on a tie (do nothing) first.
    Returns the chosen index, its logged probability and the whole distribution over options.
    """
    n = len(means)
    if n == 0 or len(sds) != n:
        raise ValueError("need one mean and one standard deviation per option, and at least one option")
    if not 0 <= floor <= 1:
        raise ValueError("the exploration floor is a probability")
    if draws < 1:
        raise ValueError("need at least one Monte Carlo draw")
    if n == 1:
        return Pick(index=0)
    mean = np.asarray(means, dtype=float)
    sd = np.asarray(sds, dtype=float)
    explore = bool(rng.random() < floor)
    uniform_pick = int(rng.integers(n))
    if not np.any(sd > 0):  # nothing uncertain: the sampling part is a plain argmax, exactly
        ts_pick = int(np.argmax(mean))
        ts_share = [1.0 if i == ts_pick else 0.0 for i in range(n)]
    else:
        samples = mean + sd * rng.standard_normal((draws, n))
        winners = np.argmax(samples, axis=1)
        ts_pick = int(winners[0])  # the real Thompson draw is the first of the Monte Carlo draws
        ts_share = [float(c) / draws for c in np.bincount(winners, minlength=n)]
    probs = tuple(floor / n + (1 - floor) * t for t in ts_share)
    index = uniform_pick if explore else ts_pick
    return Pick(index=index, probability=min(1.0, probs[index]), probabilities=probs, explored=explore)


def _unique(codes: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(c for c in codes if c))


class AutonomousPolicy:
    """`Policy` for the agent group, coded only against `Bidder` and the decision contracts.

    `attention_price_fn(state)` is what the next message costs this customer in expected lost future
    value (e.g. `FatigueModel.price`); None means attention is free and only the hard cap applies.
    `exploration_floor` overrides the envelopes' floors; None uses the largest floor among the
    active briefs. `baseline_policy` decides for the baseline group; None leaves them alone.
    `segment_hours` maps a segment to a 24-hour activity profile, used when a customer's own active
    hours are unknown.
    """

    def __init__(
        self,
        bidders: Sequence[Bidder],
        attention_price_fn: AttentionPrice | None = None,
        exploration_floor: float | None = None,
        baseline_policy: Policy | None = None,
        *,
        mc_draws: int = DEFAULT_MC_DRAWS,
        max_options: int = DEFAULT_MAX_OPTIONS,
        segment_hours: Mapping[str, Sequence[float]] | None = None,
        default_hour: int = DEFAULT_HOUR,
        currency: str = "$",
        version: str = "autonomy-v1",
    ) -> None:
        if exploration_floor is not None and not 0 <= exploration_floor <= 1:
            raise ValueError("exploration_floor is a probability")
        if mc_draws < 1 or max_options < 1:
            raise ValueError("mc_draws and max_options must be at least 1")
        self.bidders = list(bidders)
        self.attention_price_fn = attention_price_fn
        self.exploration_floor = exploration_floor
        self.baseline_policy = baseline_policy
        self.mc_draws = mc_draws
        self.max_options = max_options
        self.segment_hours: dict[str, Sequence[float]] = dict(segment_hours) if segment_hours else {}
        self.default_hour = default_hour
        self.currency = currency
        self.version = version

    # --- decide -----------------------------------------------------------------------------------

    def decide(self, state: CustomerState, ctx: DecisionContext) -> Decision:
        if state.group is Group.BASELINE:
            if self.baseline_policy is not None:
                return self.baseline_policy.decide(state, ctx)
            return self._nothing(state, ctx, "Left alone: baseline group, and no baseline rule is set.")
        if state.group is Group.HOLDOUT:
            held = self._nothing(state, ctx, "-", (rules.HOLDOUT,))
            return held.model_copy(update={"reason": explain.reason(held, [])})

        # A bid whose value or uncertainty is NaN or infinite is a bidder bug, not an opportunity:
        # numpy's argmax treats NaN as the largest value, so one such bid would always be sent.
        offered = [
            self._timed(b, state, ctx)
            for bidder in self.bidders
            for b in bidder.bids(state, ctx)
            if math.isfinite(b.expected_net) and math.isfinite(b.uncertainty)
        ]
        survivors: list[Bid] = []
        blocked: list[str] = []
        for bid in offered:
            check = rules.check_bid(bid, state, ctx)
            if check.allowed:
                survivors.append(bid)
            else:
                blocked.append(check.code)
        # Keep the near-best options only: exploring a long tail of poor bids wastes the floor.
        survivors = sorted(survivors, key=lambda b: b.expected_net, reverse=True)[: self.max_options]

        price = self._price(state)
        means = [0.0] + [b.expected_net - price for b in survivors]  # option 0 is "do nothing"
        sds = [0.0] + [b.uncertainty for b in survivors]
        pick = thompson_with_floor(
            means, sds, self._floor(ctx), seeded_rng(ctx.seed, state.customer_id), self.mc_draws
        )

        chosen = survivors[pick.index - 1] if pick.index > 0 else None
        draft = Decision(
            decision_id=stable_id("dec", state.customer_id, ctx.now.isoformat(), self.version),
            customer_id=state.customer_id,
            made_at=ctx.now,
            kind=DecisionKind.ACT if chosen is not None else DecisionKind.NOTHING,
            action=chosen.action if chosen is not None else None,
            group=state.group,
            probability=pick.probability,
            expected_net=chosen.expected_net if chosen is not None else 0.0,
            attention_price=price,
            reason="-",
            bids=tuple(survivors[:LOGGED_BIDS]),
            blocked_by=_unique(blocked),
            policy_version=self.version,
        )
        sentence = explain.reason(
            draft,
            survivors,
            content={c.content_id: c for c in ctx.content},
            offers={o.offer_id: o for b in ctx.briefs for o in b.envelope.offers},
            contacts_7d=state.contacts_7d,
            explored=pick.explored,
            currency=self.currency,
        )
        return draft.model_copy(update={"reason": sentence})

    def _timed(self, bid: Bid, state: CustomerState, ctx: DecisionContext) -> Bid:
        """The bid with its send time set: the customer's best allowed hour, never before now.

        A bidder may ask for a later start (a follow-up due in two days); the hour is then chosen in
        the 24 hours after that. A bid for a goal with no brief keeps its time; the rules block it.
        """
        brief = rules.brief_for(bid.goal_id, ctx.briefs)
        if brief is None:
            return bid
        earliest: datetime = max(ctx.now, bid.action.send_at)
        send_at = choose_send_time(
            state,
            brief.envelope,
            earliest,
            fallback_hours=self.segment_hours.get(state.segment) if state.segment else None,
            default_hour=self.default_hour,
        )
        if send_at == bid.action.send_at:
            return bid
        return bid.model_copy(update={"action": bid.action.model_copy(update={"send_at": send_at})})

    def _price(self, state: CustomerState) -> float:
        if self.attention_price_fn is None:
            return 0.0
        price = float(self.attention_price_fn(state))
        return price if price > 0 and math.isfinite(price) else 0.0

    def _floor(self, ctx: DecisionContext) -> float:
        if self.exploration_floor is not None:
            return self.exploration_floor
        floors = [b.envelope.exploration_floor for b in ctx.briefs]
        return max(floors) if floors else DEFAULT_EXPLORATION_FLOOR

    def _nothing(
        self, state: CustomerState, ctx: DecisionContext, reason: str, blocked_by: tuple[str, ...] = ()
    ) -> Decision:
        return Decision(
            decision_id=stable_id("dec", state.customer_id, ctx.now.isoformat(), self.version),
            customer_id=state.customer_id,
            made_at=ctx.now,
            kind=DecisionKind.NOTHING,
            group=state.group,
            probability=1.0,
            reason=reason,
            blocked_by=blocked_by,
            policy_version=self.version,
        )

    # --- learn ------------------------------------------------------------------------------------

    def learn(self, rewards: Sequence[Reward], decisions: Mapping[str, Decision]) -> None:
        """Hand closed outcomes to each bidder that can learn, then to the baseline rule.

        A bidder gets the rewards of its own goal plus those of "do nothing" decisions (no goal),
        which are its untreated comparison. Bidders expose `learn(rewards, decisions)` or
        `update(rewards, decisions)`; turning rewards into model observations is the bidder's job,
        since only it knows the features its models use. Bidders with neither are skipped.
        """
        for bidder in self.bidders:
            mine = [r for r in rewards if _goal_of(r, decisions) in (bidder.goal_id, None)]
            if not mine:
                continue
            if isinstance(bidder, _Learns):
                bidder.learn(mine, decisions)
            elif isinstance(bidder, _Updates):
                bidder.update(mine, decisions)
        if self.baseline_policy is not None:
            self.baseline_policy.learn(rewards, decisions)


def _goal_of(reward: Reward, decisions: Mapping[str, Decision]) -> str | None:
    if reward.goal_id is not None:
        return reward.goal_id
    decision = decisions.get(reward.decision_id)
    if decision is None or decision.action is None:
        return None
    return decision.action.goal_id
