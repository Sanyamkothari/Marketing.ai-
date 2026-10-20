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

Everything random comes from one generator seeded by `(ctx.seed, customer_id)` (plus the trigger
for an event-triggered decision), so a run repeats exactly given its seed, and customers do not share
draws.

The weekly cap counts sends this policy has scheduled but that have not gone out yet. A send chosen
today may be timed for tomorrow evening, after the next sweep has already read the customer's state,
so `contacts_7d` alone would let a second and third send through. The policy remembers each ACT
decision's `send_at` until the customer's state shows a contact at or after it (the SEND was
folded) or until it is a week old, and adds those pending sends to `contacts_7d` before the rules and
the attention price see the state.

Two optional parts make the policy smarter per person and per channel (both off by default, so the
agent factory decides when to switch them on):

* `reading_hours` (a `timing.ReadingHourModel`): each decision takes one Thompson draw of the
  customer's reading hours and times every bid at the best allowed hour of that draw. The draw uses
  its own seeded generator, so it never changes the auction's draws.
* `pacer` (a `pacing.Pacer`): before the breaker would trip a stop rule, a channel's bids pay a
  surcharge that rises with its pressure, and at full pressure the channel is held (`pacing` in
  `blocked_by`). The surcharge is charged like the attention price, and a sent decision logs the
  price plus its surcharge as `attention_price`, which is what that bid had to beat.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

import numpy as np

from autonomy.contracts import (
    Bid,
    Channel,
    CustomerState,
    Decision,
    DecisionContext,
    DecisionKind,
    Group,
    Reward,
    stable_id,
)
from autonomy.decide import explain, rules
from autonomy.decide.pacing import Pacer
from autonomy.decide.timing import DEFAULT_HOUR, ReadingHourModel, choose_send_time
from autonomy.interfaces import Bidder, Policy

DEFAULT_EXPLORATION_FLOOR = 0.05
DEFAULT_MC_DRAWS = 1000
DEFAULT_MAX_OPTIONS = 10
LOGGED_BIDS = 5
SWEEP = "sweep"
"""The trigger of the daily sweep; event-triggered decisions name their own (e.g. `cart:<event_id>`)."""
_WEEK = timedelta(days=7)

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
    hours are unknown. `plausible_z` (None: off) keeps only options whose net could beat doing
    nothing within that many standard deviations; the rest are logged as considered but neither
    sampled nor explored. `min_gap` (None: off) holds a new message while another is scheduled or
    within that long of the last one (`rules.too_soon`). `reading_hours` (None: off) times each
    send by a Thompson draw of the customer's reading hours. `pacer` (None: off) slows a channel
    down, then holds it, as the goal nears a stop rule (see the module docstring).

    `decide(state, ctx, trigger=None)`: `trigger` names what prompted the decision when it is not the
    daily sweep (a cart, a reply). The decision id is fixed by (customer, `ctx.now`, trigger), so
    deciding again for the same moment and trigger gives the same id (and replaces the earlier
    decision's pending send), while a cart and a reply in the same minute get two ids.
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
        plausible_z: float | None = None,
        min_gap: timedelta | None = None,
        reading_hours: ReadingHourModel | None = None,
        pacer: Pacer | None = None,
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
        self.plausible_z = plausible_z
        self.min_gap = min_gap
        self.reading_hours = reading_hours
        self.pacer = pacer
        # customer -> {decision_id: send_at} for ACT decisions whose SEND is not in the state yet
        self._scheduled: dict[str, dict[str, datetime]] = {}

    # --- decide -----------------------------------------------------------------------------------

    def decide(self, state: CustomerState, ctx: DecisionContext, *, trigger: str | None = None) -> Decision:
        if state.group is Group.BASELINE:
            if self.baseline_policy is not None:
                return self.baseline_policy.decide(state, ctx)
            return self._nothing(
                state, ctx, "Left alone: baseline group, and no baseline rule is set.", trigger=trigger
            )
        if state.group is Group.HOLDOUT:
            held = self._nothing(state, ctx, "-", (rules.HOLDOUT,), trigger=trigger)
            return held.model_copy(update={"reason": explain.reason(held, [])})

        decision_id = self.decision_id(state.customer_id, ctx.now, trigger)
        gap = rules.OK
        if self.min_gap is not None:
            # For the gap only sends still to come, or due within the gap, are pending. One due
            # longer ago that the state still does not show never went out (the customer opted out
            # first, say); the cap keeps counting it for a week, but it must not hold every other
            # message for that week.
            self.pending_sends(state, ctx.now)  # drops what was folded or is a week old
            since = ctx.now - self.min_gap
            upcoming = sum(
                1
                for did, at in self._scheduled.get(state.customer_id, {}).items()
                if did != decision_id and at > since
            )
            gap = rules.too_soon(state, ctx.now, self.min_gap, upcoming)
        state = self.with_pending(state, ctx.now, ignore=decision_id)
        rng_key = state.customer_id if trigger in (None, SWEEP) else f"{state.customer_id}\x1f{trigger}"
        hour_scores: tuple[float, ...] | None = None
        if self.reading_hours is not None:
            # One draw per decision, shared by every bid, from its own generator.
            hour_scores = self.reading_hours.sample_profile(
                state.customer_id, state.segment, seeded_rng(ctx.seed, f"{rng_key}\x1freading")
            )
        # A bid whose value or uncertainty is NaN or infinite is a bidder bug, not an opportunity:
        # numpy's argmax treats NaN as the largest value, so one such bid would always be sent.
        times: dict[tuple[str, datetime], datetime | None] = {}
        offered = [
            self._timed(b, state, ctx, times, hour_scores)
            for bidder in self.bidders
            for b in bidder.bids(state, ctx)
            if math.isfinite(b.expected_net) and math.isfinite(b.uncertainty)
        ]
        survivors: list[Bid] = []
        blocked: list[str] = []
        for bid in offered:
            check = rules.check_bid(bid, state, ctx)
            if check.allowed and not gap.allowed:
                check = gap
            if check.allowed:
                survivors.append(bid)
            else:
                blocked.append(check.code)
        price = self._price(state)
        extra: dict[tuple[str, Channel], float] = {}
        if self.pacer is not None:
            survivors = self._paced(survivors, ctx, price, extra, blocked)

        def cost(b: Bid) -> float:
            return price + extra.get((b.goal_id, b.action.channel), 0.0)

        # Keep the near-best options only: exploring a long tail of poor bids wastes the floor.
        survivors = sorted(survivors, key=lambda b: b.expected_net - cost(b), reverse=True)[
            : self.max_options
        ]
        options = survivors
        if self.plausible_z is not None:
            # Near-best only: an option whose net is below "do nothing" even at the optimistic end
            # of its spread is not explored either, so the floor is not spent on known losers.
            z = self.plausible_z
            options = [b for b in survivors if b.expected_net - cost(b) + z * b.uncertainty > 0]
        means = [0.0] + [b.expected_net - cost(b) for b in options]  # option 0 is "do nothing"
        sds = [0.0] + [b.uncertainty for b in options]
        pick = thompson_with_floor(means, sds, self._floor(ctx), seeded_rng(ctx.seed, rng_key), self.mc_draws)

        chosen = options[pick.index - 1] if pick.index > 0 else None
        draft = Decision(
            decision_id=decision_id,
            customer_id=state.customer_id,
            made_at=ctx.now,
            kind=DecisionKind.ACT if chosen is not None else DecisionKind.NOTHING,
            action=chosen.action if chosen is not None else None,
            group=state.group,
            probability=pick.probability,
            expected_net=chosen.expected_net if chosen is not None else 0.0,
            attention_price=cost(chosen) if chosen is not None else price,
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
            surcharges=extra,
            base_price=price,
        )
        self._remember(draft)
        return draft.model_copy(update={"reason": sentence})

    def decision_id(self, customer_id: str, now: datetime, trigger: str | None = None) -> str:
        """The id of the decision for this customer, moment and trigger (None: the daily sweep)."""
        if trigger is None or trigger == SWEEP:
            return stable_id("dec", customer_id, now.isoformat(), self.version)
        return stable_id("dec", customer_id, now.isoformat(), self.version, trigger)

    # --- sends scheduled but not yet in the state ------------------------------------------------

    def pending_sends(self, state: CustomerState, now: datetime, *, ignore: str | None = None) -> int:
        """Sends this policy scheduled for the customer that the state does not show yet.

        A pending send is dropped once the state has a contact at or after its `send_at` (the SEND
        was folded in) or once it is more than a week old (it no longer counts towards the cap even
        if it never went out). `ignore` leaves out one decision id: the decision being re-made.
        """
        pending = self._scheduled.get(state.customer_id)
        if not pending:
            return 0
        folded = state.last_contact_at
        week_ago = now - _WEEK
        keep = {did: at for did, at in pending.items() if at > week_ago and (folded is None or at > folded)}
        if keep:
            self._scheduled[state.customer_id] = keep
        else:
            del self._scheduled[state.customer_id]
        return sum(1 for did in keep if did != ignore)

    def with_pending(
        self, state: CustomerState, now: datetime, *, ignore: str | None = None
    ) -> CustomerState:
        """The state with pending sends added to `contacts_7d`, so the cap and the price see them."""
        extra = self.pending_sends(state, now, ignore=ignore)
        if not extra:
            return state
        return state.model_copy(
            update={"contacts_7d": state.contacts_7d + extra, "contacts_30d": state.contacts_30d + extra}
        )

    def forget_pending(self, decision_ids: Sequence[str]) -> None:
        """Drop scheduled sends that will never go out (rejected, cancelled), so they stop counting."""
        drop = set(decision_ids)
        for cid in list(self._scheduled):
            kept = {d: at for d, at in self._scheduled[cid].items() if d not in drop}
            if kept:
                self._scheduled[cid] = kept
            else:
                del self._scheduled[cid]

    def _remember(self, decision: Decision) -> None:
        pending = self._scheduled.get(decision.customer_id)
        if decision.action is not None:
            self._scheduled.setdefault(decision.customer_id, {})[
                decision.decision_id
            ] = decision.action.send_at
        elif pending is not None and decision.decision_id in pending:
            del pending[decision.decision_id]
            if not pending:
                del self._scheduled[decision.customer_id]

    def _timed(
        self,
        bid: Bid,
        state: CustomerState,
        ctx: DecisionContext,
        cache: dict[tuple[str, datetime], datetime | None] | None = None,
        hour_scores: Sequence[float] | None = None,
    ) -> Bid:
        """The bid with its send time set: the customer's best allowed hour, never before now.

        A bidder may ask for a later start (a follow-up due in two days); the hour is then chosen in
        the 24 hours after that. A bid for a goal with no brief keeps its time; the rules block it.
        Bids of one goal with the same start share one send time, so it is worked out once.
        `hour_scores` (one draw of the customer's reading hours) take precedence over their
        `active_hours` and the segment's profile.
        """
        earliest: datetime = max(ctx.now, bid.action.send_at)
        key = (bid.goal_id, earliest)
        if cache is not None and key in cache:
            send_at = cache[key]
        else:
            brief = rules.brief_for(bid.goal_id, ctx.briefs)
            send_at = (
                None
                if brief is None
                else choose_send_time(
                    state,
                    brief.envelope,
                    earliest,
                    fallback_hours=self.segment_hours.get(state.segment) if state.segment else None,
                    default_hour=self.default_hour,
                    hour_scores=hour_scores,
                )
            )
            if cache is not None:
                cache[key] = send_at
        if send_at is None or send_at == bid.action.send_at:
            return bid
        return bid.model_copy(update={"action": bid.action.model_copy(update={"send_at": send_at})})

    def _paced(
        self,
        bids: Sequence[Bid],
        ctx: DecisionContext,
        price: float,
        extra: dict[tuple[str, Channel], float],
        blocked: list[str],
    ) -> list[Bid]:
        """Bids that pacing lets through; fills `extra` with each (goal, channel)'s surcharge and
        adds `pacing` to `blocked` for each bid on a held channel."""
        pacer = self.pacer
        if pacer is None:
            return list(bids)
        kept: list[Bid] = []
        for bid in bids:
            channel = bid.action.channel
            pacer.observe_bid(channel, bid.expected_net, ctx.now)
            key = (bid.goal_id, channel)
            if key not in extra:
                brief = rules.brief_for(bid.goal_id, ctx.briefs)
                pressure = (
                    0.0 if brief is None else pacer.pressure(bid.goal_id, channel, brief.envelope, ctx.now)
                )
                extra[key] = pacer.surcharge(channel, pressure, price, ctx.now)
            if math.isinf(extra[key]):
                blocked.append(rules.PACING)
            else:
                kept.append(bid)
        for key in [k for k, v in extra.items() if math.isinf(v) or v <= 0]:
            del extra[key]
        return kept

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
        self,
        state: CustomerState,
        ctx: DecisionContext,
        reason: str,
        blocked_by: tuple[str, ...] = (),
        *,
        trigger: str | None = None,
    ) -> Decision:
        decision = Decision(
            decision_id=self.decision_id(state.customer_id, ctx.now, trigger),
            customer_id=state.customer_id,
            made_at=ctx.now,
            kind=DecisionKind.NOTHING,
            group=state.group,
            probability=1.0,
            reason=reason,
            blocked_by=blocked_by,
            policy_version=self.version,
        )
        self._remember(decision)
        return decision

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
