"""Pacing: slow a channel down before the brief's stop rules would pause the goal.

The breaker (in the runtime) judges each brief's stop rules nightly on the last week of sends and
pauses the whole goal when a rule is clearly crossed, until a person looks. That is the right last
resort, but a costly one: a goal that complains a little too much on WhatsApp loses its email sends
too, for the rest of the run. Pacing acts earlier and more gently. It reads the same week of
outcomes and gives every (goal, channel) a pressure in [0, 1]:

* 0 while the goal is far from tripping a rule;
* rising as one more complaint (or unsubscribe) would bring the breaker's own test close to
  tripping, weighted by how much this channel's rate pushes the goal towards the limit (a channel
  well under the limit dilutes the goal's rate rather than raising it);
* 1, which blocks the channel (rule code `pacing`), when one more such event could trip the rule;
* with `step_aside=True`, back to 0 once the goal is clearly over the limit (`in_breach`):
  slowing down then only holds the goal just short of the breaker's stop, and the sending resumes
  as the week rolls on, so a real storm would trickle on for weeks instead of being stopped (seen
  in the Gym's complaint storm, docs/v2/CONTACT_LESS.md). Off by default, which keeps the
  original behaviour: hold the channel near the trip point however far over the limit it is.

The policy turns a pressure below 1 into a surcharge on that channel's bids, on top of the attention
price: `reference x p / (1 - p)`, where the reference is what a typical message on the channel is
expected to add (learned from the bids the policy sees, frozen per day so every decision of one sweep
pays the same). At p = 0.5 a message must be worth about one typical message more to go; as p nears
1 only the very best ones go. So the least valuable sends are shed first, and the rate falls back
under the limit instead of tripping it.

The test mirrors the breaker's: the lower end of a one-sided interval (`z`, 3.09 = 99.9% by
default) on the rate, compared with the rule's threshold, and at least `min_events` events. Keep
`z`, `min_events` and `window` equal to the breaker's.

Only the complaint-rate and unsubscribe-multiple rules are paced. Delivery-rate rules are about list
quality (fewer sends do not fix a bad address), spend pace is the budget's job, and WhatsApp quality
ratings are not in the outcomes this sees.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta

from autonomy.contracts import Channel, Envelope, Event, EventType, StopMetric

PACING_WINDOW = timedelta(days=7)
PACING_Z = 3.09
"""One-sided 99.9%, the breaker's bar."""
PACING_MIN_EVENTS = 5
"""The breaker never trips on fewer complaints (or unsubscribes) than this."""
LOOKAHEAD = 1.0
"""Pressure is judged as if this many more events had already arrived: the breaker runs at night, so
by the time today's rate crosses the limit it is too late to slow down."""
PRESSURE_START = 0.5
"""How close to tripping (as a share of the way) the goal must be before any pressure is felt."""
CHANNEL_PSEUDO_SENDS = 200.0
"""A channel's own rate is shrunk towards its goal's, worth this many deliveries."""
REFERENCE_DECAY = 0.3
"""Weight of the latest day in the reference value of a message (an exponential average over days)."""

_TRACKED: frozenset[EventType] = frozenset({EventType.DELIVER, EventType.COMPLAINT, EventType.UNSUBSCRIBE})


def wilson_low(hits: float, n: float, z: float = PACING_Z) -> float:
    """Lower end of the Wilson interval for a rate of `hits` in `n`; 0 with no trials.

    The rate is capped at 1: in a rolling window a late complaint can belong to a delivery that has
    already left it, so hits can exceed n.
    """
    if n <= 0:
        return 0.0
    p = min(hits / n, 1.0)
    z2 = z * z
    centre = p + z2 / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    return max(0.0, (centre - margin) / (1 + z2 / n))


@dataclass
class _Counts:
    delivered: float = 0.0
    complaints: float = 0.0
    unsubscribes: float = 0.0

    def hits(self, metric: StopMetric) -> float:
        return self.complaints if metric is StopMetric.COMPLAINT_RATE else self.unsubscribes


@dataclass(frozen=True)
class _Outcome:
    at: datetime
    goal_id: str
    channel: Channel
    kind: EventType


class Pacer:
    """Per-channel pressure against the brief's stop rules, from the last week of outcomes.

    Feed it `record` (or `record_event`) for the agent's own deliveries, complaints and unsubscribes,
    each with the goal of the decision that caused it, and `set_unsubscribe_baseline` once the goal's
    usual unsubscribe rate is known (the breaker measures it; until then the unsubscribe rule is not
    paced). The policy calls `pressure` and `surcharge` while deciding, and `observe_bid` with the
    bids it sees so the surcharge is on the scale of a typical message.

    Deterministic: time is always passed in; nothing reads the clock. `now` is expected not to go
    backwards between calls: outcomes older than the window are dropped for good.
    """

    def __init__(
        self,
        *,
        window: timedelta = PACING_WINDOW,
        z: float = PACING_Z,
        min_events: int = PACING_MIN_EVENTS,
        lookahead: float = LOOKAHEAD,
        start: float = PRESSURE_START,
        step_aside: bool = False,
    ) -> None:
        if window <= timedelta(0) or z < 0 or min_events < 1 or lookahead < 0:
            raise ValueError("need a positive window, z >= 0, min_events >= 1 and lookahead >= 0")
        if not 0 <= start < 1:
            raise ValueError("start is a share of the way to tripping, in [0, 1)")
        self.window = window
        self.z = z
        self.min_events = min_events
        self.lookahead = lookahead
        self.start = start
        self.step_aside = step_aside
        self._outcomes: deque[_Outcome] = deque()
        self._unsub_baseline: dict[str, float] = {}
        self._counted_at: datetime | None = None
        self._goal: dict[str, _Counts] = {}
        self._channel: dict[tuple[str, Channel], _Counts] = {}
        # the reference value of a message per channel: today's sums, and the frozen value
        self._today: dict[Channel, list[float]] = {}
        self._reference: dict[Channel, float] = {}
        self._rolled_at: datetime | None = None

    # --- feeding ----------------------------------------------------------------------------------

    def record(self, goal_id: str, channel: Channel, kind: EventType, at: datetime) -> None:
        """One outcome of the agent's own message; types other than deliver, complaint and
        unsubscribe are ignored."""
        if kind not in _TRACKED:
            return
        if at.tzinfo is None:
            raise ValueError("times must be timezone-aware")
        self._outcomes.append(_Outcome(at, goal_id, channel, kind))
        self._counted_at = None

    def record_event(self, event: Event, goal_id: str) -> None:
        """`record` from an event (outbound events always carry their channel)."""
        if event.channel is not None:
            self.record(goal_id, event.channel, event.type, event.ts)

    def reset(self, goal_id: str) -> None:
        """Forget a goal's outcomes and baseline: a new brief version starts afresh, as the breaker's
        `resume` does, so pacing does not keep braking on complaints the breaker no longer counts."""
        self._outcomes = deque(o for o in self._outcomes if o.goal_id != goal_id)
        self._unsub_baseline.pop(goal_id, None)
        self._counted_at = None

    def set_unsubscribe_baseline(self, goal_id: str, rate: float | None) -> None:
        """The goal's usual unsubscribe rate per delivery, for the "N times the usual" rule."""
        if rate is None or rate <= 0 or not math.isfinite(rate):
            self._unsub_baseline.pop(goal_id, None)
        else:
            self._unsub_baseline[goal_id] = rate

    def observe_bid(self, channel: Channel, expected_net: float, now: datetime) -> None:
        """A bid the policy is weighing; its value feeds tomorrow's reference for the channel."""
        self._roll(now)
        if expected_net > 0 and math.isfinite(expected_net):
            sums = self._today.setdefault(channel, [0.0, 0.0])
            sums[0] += expected_net
            sums[1] += 1.0

    def _roll(self, now: datetime) -> None:
        """Once a day, fold the bids seen since the last roll into the frozen reference."""
        if self._rolled_at is not None and now - self._rolled_at < timedelta(days=1):
            return
        for channel, (total, n) in self._today.items():
            if n <= 0:
                continue
            mean = total / n
            old = self._reference.get(channel)
            self._reference[channel] = (
                mean if old is None else (1 - REFERENCE_DECAY) * old + REFERENCE_DECAY * mean
            )
        self._today = {}
        self._rolled_at = now

    def reference(self, channel: Channel) -> float:
        """What a typical message on this channel was expected to add, as of the last daily roll."""
        return self._reference.get(channel, 0.0)

    # --- reading ----------------------------------------------------------------------------------

    def _count(self, now: datetime) -> None:
        if self._counted_at == now:
            return
        since = now - self.window
        while self._outcomes and self._outcomes[0].at < since:
            self._outcomes.popleft()
        self._goal = {}
        self._channel = {}
        for o in self._outcomes:
            # Outcomes need not arrive in time order (a late complaint is recorded after newer
            # deliveries), so the front pop above can leave old ones behind it: skip them here.
            if o.at < since or o.at > now:
                continue
            for counts in (
                self._goal.setdefault(o.goal_id, _Counts()),
                self._channel.setdefault((o.goal_id, o.channel), _Counts()),
            ):
                if o.kind is EventType.DELIVER:
                    counts.delivered += 1.0
                elif o.kind is EventType.COMPLAINT:
                    counts.complaints += 1.0
                else:
                    counts.unsubscribes += 1.0
        self._counted_at = now

    def counts(self, goal_id: str, channel: Channel | None, now: datetime) -> tuple[float, float, float]:
        """(deliveries, complaints, unsubscribes) in the window, for the goal or one of its channels."""
        self._count(now)
        c = self._goal.get(goal_id) if channel is None else self._channel.get((goal_id, channel))
        return (0.0, 0.0, 0.0) if c is None else (c.delivered, c.complaints, c.unsubscribes)

    def closeness(self, goal_id: str, metric: StopMetric, threshold: float, now: datetime) -> float:
        """How close the goal is to tripping one rate rule, as a share of the way (1 = one more
        event could trip it): the smaller of the interval test and the event-count test, both judged
        with `lookahead` more events."""
        if threshold <= 0:
            return 0.0
        self._count(now)
        goal = self._goal.get(goal_id)
        if goal is None or goal.delivered <= 0:
            return 0.0
        hits = goal.hits(metric) + self.lookahead
        interval = wilson_low(hits, goal.delivered, self.z) / threshold
        count = hits / self.min_events
        return min(interval, count)

    def in_breach(self, goal_id: str, metric: StopMetric, threshold: float, now: datetime) -> bool:
        """Whether the goal is already clearly over the limit: the lower end of the interval on its
        rate this week, with no lookahead, is above the threshold. Pacing is for a goal that could
        still be under the limit; one that is plainly over it should reach the breaker's stop, not
        be held just short of it and let go again when the week rolls on."""
        if threshold <= 0:
            return False
        self._count(now)
        goal = self._goal.get(goal_id)
        if goal is None or goal.delivered <= 0:
            return False
        return wilson_low(goal.hits(metric), goal.delivered, self.z) > threshold

    def relevance(
        self, goal_id: str, channel: Channel, metric: StopMetric, threshold: float, now: datetime
    ) -> float:
        """How much this channel pushes the goal towards the limit, in [0, 1]: its own rate (shrunk
        towards the goal's) as a share of the threshold."""
        self._count(now)
        goal = self._goal.get(goal_id)
        if goal is None or goal.delivered <= 0 or threshold <= 0:
            return 0.0
        goal_rate = goal.hits(metric) / goal.delivered
        own = self._channel.get((goal_id, channel), _Counts())
        rate = (own.hits(metric) + CHANNEL_PSEUDO_SENDS * goal_rate) / (own.delivered + CHANNEL_PSEUDO_SENDS)
        return min(1.0, rate / threshold)

    def _ramp(self, closeness: float) -> float:
        return min(1.0, max(0.0, (closeness - self.start) / (1.0 - self.start)))

    def pressure(self, goal_id: str, channel: Channel, envelope: Envelope, now: datetime) -> float:
        """The channel's pressure in [0, 1] under this goal's envelope; 1 means hold the channel."""
        worst = 0.0
        for rule in envelope.stop_rules:
            if rule.metric is StopMetric.COMPLAINT_RATE:
                threshold = rule.threshold
            elif rule.metric is StopMetric.UNSUBSCRIBE_MULTIPLE:
                base = self._unsub_baseline.get(goal_id)
                if base is None:
                    continue
                threshold = rule.threshold * base
            else:
                continue
            if self.step_aside and self.in_breach(goal_id, rule.metric, threshold, now):
                continue
            ramp = self._ramp(self.closeness(goal_id, rule.metric, threshold, now))
            if ramp <= 0:
                continue
            worst = max(worst, ramp * self.relevance(goal_id, channel, rule.metric, threshold, now))
        return worst

    def surcharge(self, channel: Channel, pressure: float, price: float, now: datetime) -> float:
        """The extra a message on this channel must be worth: `reference x p / (1 - p)`, infinite at
        p = 1. The reference is the channel's typical message value, or the attention price when that
        is larger (or nothing has been learned yet)."""
        if pressure <= 0:
            return 0.0
        if pressure >= 1:
            return math.inf
        self._roll(now)
        scale = max(self.reference(channel), price, 0.0)
        return scale * pressure / (1.0 - pressure)


# --- the budget price for offers (v3) -------------------------------------------------------------

BUDGET_PRICE_GAIN = 0.5
"""How hard one day's miss moves the price: the price is multiplied by `exp(gain x miss)`."""
BUDGET_PRICE_FLOOR = 0.01
"""Below this the budget is not binding and the price reads 0 (the offers that pay all fit)."""
BUDGET_PRICE_CEILING = 1000.0
"""What the price reads once the budget is spent: high enough that no offer is worth its cost."""
BUDGET_PRICE_MAX_MISS = 3.0
"""A day that spends more than this many times its target counts as this much over, no more."""


class BudgetPrice:
    """The price of one unit of offer money, moved day by day so spend keeps to the plan.

    The planner (`autonomy.decide.planner`) fills the budget with the offers that earn the most extra
    profit per unit of cost. The last pair that fits has a ratio of extra profit to cost, and the first
    that does not fit has a smaller one: any ratio between them is the budget's shadow price (the
    Lagrange multiplier). A bid that pays this price per unit of its offer cost (`GoalBidder`'s
    `budget_price`) takes the offer exactly when its ratio is above the price, so each customer's
    decision lands on the same side of the line as the plan, without seeing anyone else.

    The best price is not known in advance (it depends on who turns up), so it is learned. Each day
    `update` is told what was spent. The target for the day is what is left of the budget divided by
    the days left, so a miss is made up over the days that remain. Spending above target raises the
    price, below it lowers it, by a multiple that grows with the miss. `initial` is where it starts;
    `planner.OfferPlan.price` on a sample of the audience is a good start.

    The goal's budget also pays for the messages. `update(..., other_spend=...)` takes the day's
    channel spend: it counts against the budget, and its daily average (before any day has run,
    `channel_plan_per_day`) is set aside for every day left, so the price aims the offers at the
    budget net of the planned channel spend rather than at the whole budget.

    Deterministic, with no clock: the caller says when a day ends. The object is callable and returns
    the current price, so it can be handed to a bidder directly.
    """

    def __init__(
        self,
        budget: float,
        days: int,
        *,
        initial: float = 0.5,
        gain: float = BUDGET_PRICE_GAIN,
        floor: float = BUDGET_PRICE_FLOOR,
        ceiling: float = BUDGET_PRICE_CEILING,
        max_miss: float = BUDGET_PRICE_MAX_MISS,
        channel_plan_per_day: float = 0.0,
    ) -> None:
        if not math.isfinite(budget) or budget < 0 or days < 1:
            raise ValueError("need a finite budget >= 0 and at least one day")
        if not math.isfinite(channel_plan_per_day) or channel_plan_per_day < 0:
            raise ValueError("the planned channel spend must be finite and >= 0")
        if gain <= 0 or not 0 < floor < ceiling or max_miss <= 0 or initial < 0:
            raise ValueError("need gain > 0, 0 < floor < ceiling, max_miss > 0 and initial >= 0")
        self.budget = budget
        self.days = days
        self.gain = gain
        self.floor = floor
        self.ceiling = ceiling
        self.max_miss = max_miss
        self._level = min(max(initial, floor), ceiling)
        self.channel_plan_per_day = channel_plan_per_day
        self._spent = 0.0
        self._other = 0.0
        self._days_done = 0

    @property
    def spent(self) -> float:
        """Offer spend so far."""
        return self._spent

    @property
    def other_spent(self) -> float:
        """Channel (and other non-offer) spend so far."""
        return self._other

    @property
    def remaining(self) -> float:
        """What is left for offers: the budget less all spend so far and the channel spend set aside
        for the days left."""
        rate = self._other / self._days_done if self._days_done else self.channel_plan_per_day
        reserve = rate * max(self.days - self._days_done, 0)
        return max(self.budget - self._spent - self._other - reserve, 0.0)

    @property
    def days_left(self) -> int:
        return max(self.days - self._days_done, 0)

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0 or self.days_left <= 0

    @property
    def daily_target(self) -> float:
        """What today should spend: the budget left over the days left; 0 when none is left."""
        return 0.0 if self.exhausted else self.remaining / self.days_left

    @property
    def price(self) -> float:
        """The price now: 0 when the budget is not binding, the ceiling when it is spent."""
        if self.exhausted:
            return self.ceiling
        return 0.0 if self._level <= self.floor else self._level

    def __call__(self) -> float:
        return self.price

    def extend(self, days: int) -> None:
        """Plan `days` more days: what is left of the budget is paced over them (a goal that outlives its plan)."""
        if days < 1:
            raise ValueError("extend by at least one day")
        self.days += days

    def update(self, spent_today: float, *, other_spend: float = 0.0) -> float:
        """Close a day that spent `spent_today` on offers (and `other_spend` on messages), and return
        tomorrow's price."""
        if (
            not (math.isfinite(spent_today) and math.isfinite(other_spend))
            or min(spent_today, other_spend) < 0
        ):
            raise ValueError("spend cannot be negative or infinite")
        target = self.daily_target
        if target > 0:
            miss = min(spent_today / target - 1.0, self.max_miss)
            self._level = min(max(self._level * math.exp(self.gain * miss), self.floor), self.ceiling)
        self._spent += spent_today
        self._other += other_spend
        self._days_done += 1
        return self.price


@dataclass(frozen=True)
class OfferPricing:
    """Opt-in settings for the budget price in the agent's daily loop (`AgentPolicy(offer_pricing=...)`).

    Each goal gets a `BudgetPrice` over its envelope's budget, spread over `days`. `initial` is where
    the price starts (`planner.OfferPlan.price` on a sample is a good start). `channel_plan_per_day`
    is the channel spend to set aside per day before any day has run. With `default_basket` set, a
    percent-off offer with no configured cost is priced at its percent of the customer's basket, or of
    `default_basket` for a customer with no orders (`planner.basket_offer_cost`); None leaves the
    offer's own `cost_per_redemption`, as in v2.

    `renew` (off by default): when the plan's `days` have run and budget is left, plan another `days`
    for what is left, instead of pricing every offer at the ceiling from then on. A goal with no end
    date outlives its first `days`; the v3 preset turns this on (docs/v3/PRESET.md).
    """

    days: int = 30
    initial: float = 0.5
    gain: float = BUDGET_PRICE_GAIN
    channel_plan_per_day: float = 0.0
    default_basket: float | None = None
    renew: bool = False

    def __post_init__(self) -> None:
        if self.days < 1 or self.initial < 0 or self.gain <= 0 or self.channel_plan_per_day < 0:
            raise ValueError("need days >= 1, initial >= 0, gain > 0 and channel_plan_per_day >= 0")
        if self.default_basket is not None and not self.default_basket > 0:
            raise ValueError("default_basket must be > 0 when given")

    def price_for(self, budget: float) -> BudgetPrice:
        return BudgetPrice(
            budget,
            self.days,
            initial=self.initial,
            gain=self.gain,
            channel_plan_per_day=self.channel_plan_per_day,
        )
