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

Three more optional hooks let a caller make contacting dearer and rarer (all off by default):

* `channel_price_fn(state, bids)`: the attention price per channel, given the customer's bids (an
  unsubscribe on WhatsApp risks a different future from one on email). A channel it leaves out pays
  `attention_price_fn`'s price.
* `budget_fn(state, bids, cost)`: the customer's weekly contact budget. When the messages of the
  last 7 days (pending sends included) have used it up, every option is held (`weekly_cap` in
  `blocked_by`: it is the customer's own weekly limit). A budget below 1 never holds the first
  message of a week; whether that one is worth sending is the price's and the auction's call.
* `margin_fn(state, bid)`: restraint under uncertainty. An option with a positive margin may win
  the Thompson part only if its net beats doing nothing by that margin (a lower-confidence-bound
  rule); it stays among the options the exploration floor picks from, so learning goes on. The
  logged probability counts this exactly: `floor / n` for every option, plus `(1 - floor) x` its
  share of Thompson wins among the options that pass.

One more optional hook is the v3 frequency plan (`plan_fn`, `frequency.FrequencyPlanner.gate`): a customer
whose best 3-week contact plan sends nothing today and a message later is held (`frequency_plan` in
`blocked_by`), after the weekly budget and before the draw. It can only hold, never release; a
journey's later steps and event-triggered decisions (a cart, a reply) skip it (docs/v3/FREQUENCY.md).

One more optional hook is the v3 channel journeys (`journeys`, a `journeys.JourneyRunner`; off by
default, and with it off nothing below changes). Each bid on a journey's first channel becomes one
option per journey, priced over the whole plan, and the chosen journey's chance is logged on the
decision (`journey_probability`). Later steps run before the auction in the daily sweep, are not
drawn again and pass every hard rule at their own time. See `autonomy.decide.journeys`.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
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
from autonomy.decide.calibration import scaled_net
from autonomy.decide.journeys import (
    EXPIRED,
    LEFT_AGENT_GROUP,
    NO_LONGER_PAYS,
    JourneyRun,
    JourneyRunner,
    TurnKind,
    Verdict,
    channel_name,
    journey_probability,
)
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
ChannelPrices = Callable[[CustomerState, Sequence[Bid]], Mapping[Channel, float]]
"""(state, the customer's bids) -> the attention price per channel."""
ContactBudget = Callable[[CustomerState, Sequence[Bid], Callable[[Bid], float]], int | None]
"""(state, bids, each bid's price) -> how many messages the customer should get in any 7 days."""
PlanGate = Callable[[CustomerState, Sequence[Bid], datetime, bool], str | None]
"""(state, the customer's bids, now, whether to record) -> a hold code when the customer's 3-week plan
waits today, else None (`frequency.FrequencyPlanner.gate`)."""
RestraintMargin = Callable[[CustomerState, Bid], float]
"""(state, bid) -> how far the bid's net must beat doing nothing to be sampled (0: plain Thompson)."""


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


@dataclass(frozen=True)
class Market:
    """One customer's options after pricing, as the auction sees them (`AutonomousPolicy.auction`).

    `survivors` are the bids that may go out, best net after their price first (at most
    `max_options`); `options` are those of them the auction samples or explores (all of them, unless
    `plausible_z` drops known losers); `eligible` says, per option, whether it may win the Thompson
    part (False when restraint holds it back). `price` is the base attention price, `prices` the
    per-channel prices and `surcharges` pacing's extra per (goal, channel). `held` lists the bids
    that passed the hard rules but that pacing (`pacing`) or the weekly budget (`weekly_cap`) held,
    with that code; `budget` is the customer's weekly budget when one was set.
    """

    survivors: list[Bid]
    options: list[Bid]
    eligible: list[bool]
    price: float
    prices: Mapping[Channel, float]
    surcharges: Mapping[tuple[str, Channel], float]
    budget: int | None = None
    held: list[tuple[Bid, str]] = field(default_factory=list)

    def channel_price(self, channel: Channel) -> float:
        """The attention price of a message on `channel`, before any pacing surcharge."""
        return self.prices.get(channel, self.price)

    def cost(self, bid: Bid) -> float:
        """What `bid` must beat: its channel's price plus its pacing surcharge."""
        return self.channel_price(bid.action.channel) + self.surcharges.get(
            (bid.goal_id, bid.action.channel), 0.0
        )


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
    eligible: Sequence[bool] | None = None,
) -> Pick:
    """Choose among options by Thompson sampling mixed with a uniform exploration floor.

    Ties go to the lowest index, so put the option you prefer on a tie (do nothing) first.
    `eligible` (None: all) marks the options that may win the Thompson part; the floor still picks
    uniformly among all of them, so an option held back by restraint keeps being tried now and then.
    The first option is always eligible, and marking every option eligible gives exactly the
    unmasked choice. Returns the chosen index, its logged probability and the whole distribution
    over options.
    """
    n = len(means)
    if n == 0 or len(sds) != n:
        raise ValueError("need one mean and one standard deviation per option, and at least one option")
    if eligible is not None and len(eligible) != n:
        raise ValueError("need one eligibility flag per option")
    if not 0 <= floor <= 1:
        raise ValueError("the exploration floor is a probability")
    if draws < 1:
        raise ValueError("need at least one Monte Carlo draw")
    if n == 1:
        return Pick(index=0)
    mean = np.asarray(means, dtype=float)
    sd = np.asarray(sds, dtype=float)
    # np.array copies, so setting the first flag below leaves the caller's flags as given.
    mask = np.ones(n, dtype=bool) if eligible is None else np.array(eligible, dtype=bool)
    mask[0] = True
    explore = bool(rng.random() < floor)
    uniform_pick = int(rng.integers(n))
    if not np.any(sd[mask] > 0):  # nothing uncertain: the sampling part is a plain argmax, exactly
        ts_pick = int(np.argmax(np.where(mask, mean, -np.inf)))
        ts_share = [1.0 if i == ts_pick else 0.0 for i in range(n)]
    else:
        samples = mean + sd * rng.standard_normal((draws, n))
        samples[:, ~mask] = -np.inf
        winners = np.argmax(samples, axis=1)
        ts_pick = int(winners[0])  # the real Thompson draw is the first of the Monte Carlo draws
        ts_share = [float(c) / draws for c in np.bincount(winners, minlength=n)]
    probs = tuple(floor / n + (1 - floor) * t for t in ts_share)
    index = uniform_pick if explore else ts_pick
    return Pick(index=index, probability=min(1.0, probs[index]), probabilities=probs, explored=explore)


def _unique(codes: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(c for c in codes if c))


def unreachable(state: CustomerState, ctx: DecisionContext) -> list[str]:
    """Why no brief's channel can reach this customer, when none can: per channel the briefs allow,
    the reach rule it fails (`unsubscribed`, `no_consent`, `no_address`, the order `check_bid` uses).

    Bidders offer nothing on a channel the customer cannot be reached on, so without this a customer
    who never gave consent was "left alone: no active goal has anything to offer", with an empty
    `blocked_by`; now the decision says it is consent. Empty when some allowed channel is open."""
    codes: list[str] = []
    for brief in ctx.briefs:
        for channel in brief.envelope.channels:
            for check in (
                rules.unsubscribed(state, channel),
                rules.consent(state, channel),
                rules.contactable(state, channel),
            ):
                if not check.allowed:
                    codes.append(check.code)
                    break
            else:
                return []
    return codes


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
    down, then holds it, as the goal nears a stop rule. `channel_price_fn`, `budget_fn` and
    `margin_fn` (None: off) price each channel apart, hold a customer whose weekly budget is spent
    and keep uncertain options out of the Thompson draw (see the module docstring). `greedy` (False: off) is
    an ablation: the final choice takes the option with the best net after price, with no Thompson draw (set
    `exploration_floor=0` with it for a policy that never explores; docs/v4/METHOD.md). Every option's spread
    still counts upstream (restraint, pruning).

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
        channel_price_fn: ChannelPrices | None = None,
        budget_fn: ContactBudget | None = None,
        margin_fn: RestraintMargin | None = None,
        journeys: JourneyRunner | None = None,
        plan_fn: PlanGate | None = None,
        greedy: bool = False,
        value_scale_fn: Callable[[], float] | None = None,
    ) -> None:
        if exploration_floor is not None and not 0 <= exploration_floor <= 1:
            raise ValueError("exploration_floor is a probability")
        if mc_draws < 1 or max_options < 1:
            raise ValueError("mc_draws and max_options must be at least 1")
        self.bidders = list(bidders)
        self.attention_price_fn = attention_price_fn
        self.exploration_floor = exploration_floor
        self.greedy = greedy
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
        self.channel_price_fn = channel_price_fn
        self.budget_fn = budget_fn
        self.margin_fn = margin_fn
        self.journeys = journeys
        self.plan_fn = plan_fn
        self.value_scale_fn = value_scale_fn
        # customer -> {decision_id: send_at} for ACT decisions whose SEND is not in the state yet
        self._scheduled: dict[str, dict[str, datetime]] = {}

    # --- decide -----------------------------------------------------------------------------------

    def decide(self, state: CustomerState, ctx: DecisionContext, *, trigger: str | None = None) -> Decision:
        if self.journeys is not None and state.group is not Group.AGENT:
            self.journeys.drop(state.customer_id, LEFT_AGENT_GROUP, ctx.now)
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
        journey_ended: str | None = None
        if self.journeys is not None:
            self.journeys.begin(state.customer_id, decision_id)
            step = self._journey_turn(state, ctx, decision_id, trigger, gap)
            if step is not None:
                return step
            journey_ended = self.journeys.ended(state.customer_id)
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
        scale = self._value_scale()
        offered = [
            self._timed(self._scaled(b, scale), state, ctx, times, hour_scores)
            for bidder in self.bidders
            for b in bidder.bids(state, ctx)
            if math.isfinite(b.expected_net) and math.isfinite(b.uncertainty)
        ]
        if self.journeys is not None:
            offered = self.journey_options(offered, state, ctx)
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
        if not offered:
            blocked.extend(unreachable(state, ctx))
        # v3 frequency: only the daily sweep is planned; a cart or a reply is answered when it comes.
        market = self.auction(state, ctx, survivors, blocked, plan=trigger in (None, SWEEP))
        survivors, options, cost = market.survivors, market.options, market.cost
        means = [0.0] + [b.expected_net - cost(b) for b in options]  # option 0 is "do nothing"
        sds = [0.0] + [0.0 if self.greedy else b.uncertainty for b in options]
        pick = thompson_with_floor(
            means,
            sds,
            self._floor(ctx),
            seeded_rng(ctx.seed, rng_key),
            self.mc_draws,
            eligible=[True, *market.eligible],
        )

        chosen = options[pick.index - 1] if pick.index > 0 else None
        if chosen is not None:
            paid, base_price = cost(chosen), market.channel_price(chosen.action.channel)
        else:
            # What the best option had to beat: its own channel's price.
            best = survivors[0] if survivors else None
            paid = base_price = market.channel_price(best.action.channel) if best else market.price
        journey_id = chosen.action.journey_id if chosen is not None else None
        draft = Decision(
            decision_id=decision_id,
            customer_id=state.customer_id,
            made_at=ctx.now,
            kind=DecisionKind.ACT if chosen is not None else DecisionKind.NOTHING,
            action=chosen.action if chosen is not None else None,
            group=state.group,
            probability=pick.probability,
            expected_net=chosen.expected_net if chosen is not None else 0.0,
            attention_price=paid,
            reason="-",
            bids=tuple(survivors[:LOGGED_BIDS]),
            blocked_by=_unique(blocked),
            policy_version=self.version,
            journey_id=journey_id,
            journey_probability=(
                journey_probability(options, pick.probabilities, journey_id) if journey_id else None
            ),
            journey_ended=journey_ended,
        )
        sentence = explain.reason(
            draft,
            survivors,
            content={c.content_id: c for c in ctx.content},
            offers={o.offer_id: o for b in ctx.briefs for o in b.envelope.offers},
            contacts_7d=state.contacts_7d,
            explored=pick.explored,
            currency=self.currency,
            surcharges=market.surcharges,
            base_price=base_price,
            prices={b.action.channel: market.channel_price(b.action.channel) for b in survivors},
            channels=state.channels,
            # options are best net first, so this says restraint held the best one out of the draw
            restrained=bool(market.eligible) and not market.eligible[0],
        )
        if journey_id is not None and self.journeys is not None:
            sentence = f"{sentence} Journey {journey_id}: {self.journeys.describe(journey_id)}."
        self._remember(draft)
        return draft.model_copy(update={"reason": sentence})

    # --- journeys: the steps after the first ------------------------------------------------------

    def _journey_turn(
        self,
        state: CustomerState,
        ctx: DecisionContext,
        decision_id: str,
        trigger: str | None,
        gap: rules.RuleResult,
    ) -> Decision | None:
        """The decision for a customer whose journey is in progress, or None to run the auction.

        Only the daily sweep carries journeys on (a cart or a reply is decided on its own). A step
        that is not due yet holds the customer (`hold_during_journey`). A due step is quoted afresh,
        timed, passed through every hard rule and the gap between messages, then through the same
        market as a first message (`auction`: attention price, pacing, weekly budget, restraint). A
        hold by pacing or the budget waits like a cap; a step that no longer beats its price, or
        that is more than `max_defer_days` overdue, ends the journey. A step that goes out is sent
        with probability 1.0 (the journey was the draw) and the first step's `journey_probability`."""
        runner = self.journeys
        if runner is None or trigger not in (None, SWEEP):
            return None
        turn = runner.advance(state, ctx)
        run, journey, step, due = turn.run, turn.journey, turn.step, turn.due_at
        if run is None or journey is None or turn.kind in (TurnKind.NONE, TurnKind.STOPPED):
            return None
        label = f"Journey {run.journey_id}"
        if turn.kind is TurnKind.WAIT:
            if not runner.settings.hold_during_journey or step is None or due is None:
                return None
            where = channel_name(step.channel)
            return self._journey_hold(
                state,
                ctx,
                decision_id,
                run,
                f"Waiting: {label} sends its next message, by {where},"
                f" on {due.astimezone(rules.customer_zone(state)):%d %b}.",
            )
        planned = runner.plan(turn, state, ctx, self.bidders)
        if isinstance(planned, str):
            return None
        bid = self._timed(self._scaled(planned, self._value_scale()), state, ctx)
        check = rules.check_bid(bid, state, ctx)
        if check.allowed and not gap.allowed:
            check = gap
        market: Market | None = None
        if check.allowed:
            # The same market as a first message: a hold by pacing or the weekly budget waits.
            market = self.auction(state, ctx, [bid], [], plan=False, rhythm=runner.follow_rhythm)
            if market.held:
                check = rules.RuleResult(False, market.held[0][1])
        if check.allowed and runner.late(turn, ctx.now):
            runner.stop(run, EXPIRED, ctx.now)
            return None
        if not check.allowed:
            verdict = runner.blocked(turn, check.code, ctx.now)
            if verdict is Verdict.STOP:
                return None
            what = "skips" if verdict is Verdict.SKIP else "holds"
            why = explain.BLOCK_SENTENCE.get(check.code, check.code).removeprefix("Left alone: ")
            return self._journey_hold(
                state, ctx, decision_id, run, f"{label} {what} its next message: {why}", (check.code,)
            )
        assert market is not None  # set whenever the step passed the rules
        price = market.cost(bid)
        restrained = bool(market.eligible) and not market.eligible[0]
        if runner.settings.require_positive_net and (not bid.expected_net - price > 0 or restrained):
            runner.stop(run, NO_LONGER_PAYS, ctx.now)
            return None
        draft = Decision(
            decision_id=decision_id,
            customer_id=state.customer_id,
            made_at=ctx.now,
            kind=DecisionKind.ACT,
            action=bid.action,
            group=state.group,
            probability=1.0,
            expected_net=bid.expected_net,
            attention_price=price,
            reason="-",
            bids=(bid,),
            policy_version=self.version,
            journey_id=run.journey_id,
            journey_probability=run.probability,
        )
        sentence = explain.reason(
            draft,
            [bid],
            content={c.content_id: c for c in ctx.content},
            offers={o.offer_id: o for b in ctx.briefs for o in b.envelope.offers},
            contacts_7d=state.contacts_7d,
            currency=self.currency,
            surcharges=market.surcharges,
            base_price=market.channel_price(bid.action.channel),
            prices={bid.action.channel: market.channel_price(bid.action.channel)},
            channels=state.channels,
        )
        self._remember(draft)
        return draft.model_copy(
            update={"reason": f"{sentence} Part of {label}: {runner.describe(run.journey_id)}."}
        )

    def journey_options(
        self, offered: Sequence[Bid], state: CustomerState, ctx: DecisionContext
    ) -> list[Bid]:
        """The bids the auction weighs once journeys are on: `JourneyRunner.expand`, less any journey that
        puts more messages in a 7-day span than the strictest weekly cap lets through (its later step could
        never go out, and the customer would be held for nothing until it expired)."""
        runner = self.journeys
        if runner is None:
            return list(offered)
        options = runner.expand(offered, state, self.journey_prices(state, offered), ctx.now)
        cap = rules.strictest_weekly_cap(ctx.briefs)
        if cap is None:
            return options
        return [
            b
            for b in options
            if b.action.journey_id is None or runner.messages_in_a_week(b.action.journey_id) <= cap
        ]

    def journey_prices(self, state: CustomerState, bids: Sequence[Bid]) -> Callable[[Channel], float]:
        """The attention price of one more message on each channel, as the auction would price the
        customer's single-message `bids`: what a journey's later step pays when it runs."""
        base = self._price(state)
        prices: dict[Channel, float] = {}
        if self.channel_price_fn is not None and bids:
            for channel, p in self.channel_price_fn(state, bids).items():
                prices[channel] = float(p) if p > 0 and math.isfinite(p) else 0.0
        return lambda channel: prices.get(channel, base)

    def _journey_hold(
        self,
        state: CustomerState,
        ctx: DecisionContext,
        decision_id: str,
        run: JourneyRun,
        reason: str,
        blocked_by: tuple[str, ...] = (),
    ) -> Decision:
        """Leave a customer mid-journey alone for this sweep, saying which journey and why."""
        decision = Decision(
            decision_id=decision_id,
            customer_id=state.customer_id,
            made_at=ctx.now,
            kind=DecisionKind.NOTHING,
            group=state.group,
            probability=1.0,
            reason=reason,
            blocked_by=blocked_by,
            policy_version=self.version,
            journey_id=run.journey_id,
            journey_probability=run.probability,
        )
        self._remember(decision)
        return decision

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
        if self.journeys is not None:
            self.journeys.forget(drop)
        for cid in list(self._scheduled):
            kept = {d: at for d, at in self._scheduled[cid].items() if d not in drop}
            if kept:
                self._scheduled[cid] = kept
            else:
                del self._scheduled[cid]

    def _remember(self, decision: Decision) -> None:
        if self.journeys is not None:
            self.journeys.record(decision)
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

    def auction(
        self,
        state: CustomerState,
        ctx: DecisionContext,
        survivors: Sequence[Bid],
        blocked: list[str],
        *,
        observe: bool = True,
        plan: bool = True,
        rhythm: bool = True,
    ) -> Market:
        """Price the bids that passed the hard rules and pick the ones the auction weighs.

        In order: the attention price per channel, pacing (which may hold a channel), the weekly
        budget (which may hold the customer), the frequency plan (which may hold the customer for a
        day), the best `max_options` by net after price, the
        plausible ones among them, and which of those restraint lets into the Thompson draw. Codes
        for what pacing or the budget held are appended to `blocked`. `observe=False` leaves the
        pacer's running reference alone, so an explanation changes nothing. `plan=False` skips the
        frequency plan's hold: a journey's later step follows its journey, not a new plan, and a
        decision prompted by an event (a cart, a reply) is answered when it comes. `rhythm=False` skips
        the weekly budget's hold (a journey's step, when `JourneyRunner.follow_rhythm` is off). The hard
        weekly cap (`rules.check_bid`) and the attention price still apply to the step.
        """
        price = self._price(state)
        prices: dict[Channel, float] = {}
        if self.channel_price_fn is not None and survivors:
            for channel, p in self.channel_price_fn(state, survivors).items():
                prices[channel] = float(p) if p > 0 and math.isfinite(p) else 0.0
        extra: dict[tuple[str, Channel], float] = {}
        bids = list(survivors)
        held: list[tuple[Bid, str]] = []
        if self.pacer is not None:
            bids = self._paced(bids, ctx, lambda ch: prices.get(ch, price), extra, blocked, observe=observe)
            kept = {id(b) for b in bids}
            held = [(b, rules.PACING) for b in survivors if id(b) not in kept]
        market = Market([], [], [], price, prices, extra)
        budget: int | None = None
        if rhythm and self.budget_fn is not None and bids:
            budget = self.budget_fn(state, bids, market.cost)
            if budget is not None and state.contacts_7d >= max(budget, 1):
                blocked.append(rules.WEEKLY_CAP)
                held.extend((b, rules.WEEKLY_CAP) for b in bids)
                bids = []
        if plan and self.plan_fn is not None and bids:
            # v3 frequency plans: a customer whose best 3-week plan waits is left alone today.
            code = self.plan_fn(state, bids, ctx.now, observe)
            if code is not None:
                blocked.append(code)
                held.extend((b, code) for b in bids)
                bids = []
        # Keep the near-best options only: exploring a long tail of poor bids wastes the floor.
        bids = sorted(bids, key=lambda b: b.expected_net - market.cost(b), reverse=True)[: self.max_options]
        options = bids
        if self.plausible_z is not None:
            # Near-best only: an option whose net is below "do nothing" even at the optimistic end
            # of its spread is not explored either, so the floor is not spent on known losers.
            z = self.plausible_z
            options = [b for b in bids if b.expected_net - market.cost(b) + z * b.uncertainty > 0]
        eligible: list[bool] = []
        for b in options:
            margin = self.margin_fn(state, b) if self.margin_fn is not None else 0.0
            eligible.append(margin <= 0 or b.expected_net - market.cost(b) > margin)
        return Market(bids, options, eligible, price, prices, extra, budget, held)

    def _paced(
        self,
        bids: Sequence[Bid],
        ctx: DecisionContext,
        price_of: Callable[[Channel], float],
        extra: dict[tuple[str, Channel], float],
        blocked: list[str],
        *,
        observe: bool = True,
    ) -> list[Bid]:
        """Bids that pacing lets through; fills `extra` with each (goal, channel)'s surcharge and
        adds `pacing` to `blocked` for each bid on a held channel."""
        pacer = self.pacer
        if pacer is None:
            return list(bids)
        kept: list[Bid] = []
        for bid in bids:
            channel = bid.action.channel
            if observe:
                pacer.observe_bid(channel, bid.expected_net, ctx.now)
            key = (bid.goal_id, channel)
            if key not in extra:
                brief = rules.brief_for(bid.goal_id, ctx.briefs)
                pressure = (
                    0.0 if brief is None else pacer.pressure(bid.goal_id, channel, brief.envelope, ctx.now)
                )
                extra[key] = pacer.surcharge(channel, pressure, price_of(channel), ctx.now)
            if math.isinf(extra[key]):
                blocked.append(rules.PACING)
            else:
                kept.append(bid)
        for key in [k for k, v in extra.items() if math.isinf(v) or v <= 0]:
            del extra[key]
        return kept

    def _value_scale(self) -> float:
        """The calibration factor (`autonomy.decide.calibration`): 1.0 when none is set or it is not a usable number."""
        if self.value_scale_fn is None:
            return 1.0
        scale = float(self.value_scale_fn())
        return scale if math.isfinite(scale) and scale > 0 else 1.0

    @staticmethod
    def _scaled(bid: Bid, scale: float) -> Bid:
        """The bid with its value part shrunk by the calibration factor and its costs left whole
        (`autonomy.decide.calibration.scaled_net`; its uncertainty is left as it is)."""
        if scale >= 1.0 or not math.isfinite(bid.expected_net):
            return bid
        net = scaled_net(bid, scale)
        return bid if net == bid.expected_net else bid.model_copy(update={"expected_net": net})

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
