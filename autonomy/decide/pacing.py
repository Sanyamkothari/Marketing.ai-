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
* 1, which blocks the channel (rule code `pacing`), when one more such event could trip the rule.

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
