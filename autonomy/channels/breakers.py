"""Circuit breakers: stop sending when customers or the budget show harm (plan-v2 section 6).

Each `StopRule` in a brief's envelope names a metric and a threshold. `evaluate()` measures every
metric from recent events and returns alerts plus what to pause. It is pure: events, baselines,
spend and `now` are passed in, so the Gym can check that a planted harm trips the breaker within
two batches.

How each metric is measured:

- `complaint_rate`: complaints (and WhatsApp blocks, the same "this is unwanted" signal) per
  delivery, per channel.
- `unsubscribe_multiple`: the unsubscribe rate per delivery, per channel, divided by the account's
  own baseline rate (`baselines["unsubscribe_rate/<channel>"]`, else `baselines["unsubscribe_rate"]`).
  No baseline, no verdict: we never guess one.
- `delivery_rate_below`: of the sends at least `delivery_grace` old (receipts lag), the share that
  got through; dispatcher failures count as attempts that did not. On a channel that reports
  delivery receipts, getting through means a DELIVER event. On one that does not (a webhook to the
  client's tool, or SES set up with bounce notices only), it means the provider accepted the send
  and no bounce came back, so a dead endpoint still trips the breaker while silence alone does not.
- `spend_pace`: spend so far divided by planned spend so far.
- `whatsapp_quality`: the latest quality rating Meta reported, read from WhatsApp events' attrs
  (`quality_rating` or `quality`: GREEN/YELLOW/RED or 0/1/2). 1 means yellow, 2 means red. Meta
  reports only changes, so the latest rating in `recent_events` stands whatever its age.

Rates are measured over two windows, the last batch (a day by default) and the last seven days,
and a rule trips if either crosses its threshold. The short window reacts fast to a bad batch; the
long one catches a slow trickle. Complaints are also measured over the last hour (`fast_window`):
a burst of complaints from one bad send should stop the next send, not wait for the day's tally.
In that hour only complaints about messages that went out in the same hour count (matched by
decision id): complaints lag their message by hours, so late complaints about yesterday's big send,
divided by the few messages of the last hour, would trip on nothing new. Small samples cannot trip
a breaker: a rate needs a minimum number of deliveries (or sends) and at least `min_count` bad
events, and spend pace needs a minimum planned spend. A false pause is cheap (a person resumes it); a missed one is not, so the guards are
kept modest.

When a rule says `pause_goal` but no goal is given, the offending channel is paused instead: a
tripped breaker always stops something it can name.

**Negative lift** (`negative_lift`) is a separate check, not a `StopMetric`: it reads the proof
(`autonomy.proof`), not the last week of events. Nightly, over a goal's closed outcome windows, it
compares the agent group with the no-contact holdout in money. When the whole 95% interval of
incremental profit sits below zero (with enough customers in both groups), the goal is losing money
against doing nothing and is paused, with alert code `proof.negative_lift`. The check is one-sided:
it only ever stops a goal, it never declares a win, and a goal whose interval still spans zero runs on.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal, NamedTuple

from autonomy.contracts import (
    Alert,
    Brief,
    Channel,
    Decision,
    Event,
    EventType,
    StopMetric,
    StopRule,
    stable_id,
)
from autonomy.proof.estimate import MIN_GROUP, proof_summary
from autonomy.proof.rewards import Costs, proof_rewards

NEGATIVE_LIFT_CODE = "proof.negative_lift"
"""The alert code of the negative-lift breaker (it is not a `StopMetric`: contracts stay as they are)."""

COMPLAINT_TYPES: frozenset[EventType] = frozenset({EventType.COMPLAINT, EventType.BLOCK})
QUALITY_ATTR_KEYS: tuple[str, ...] = ("quality_rating", "quality")
"""Where a WhatsApp adapter puts Meta's phone-number quality rating on an event's attrs."""

_QUALITY_LEVELS: dict[str, int] = {"green": 0, "high": 0, "yellow": 1, "medium": 1, "red": 2, "low": 2}


@dataclass(frozen=True)
class Guards:
    """Minimum volumes below which a metric is not judged, and the windows it is measured over."""

    min_deliveries: int = 100
    """Deliveries (or sends, for a channel without receipts) before a complaint or unsubscribe rate counts."""
    min_count: int = 2
    """Bad events needed to trip a rate: one complaint alone never pauses anything."""
    min_sends: int = 100
    """Sends old enough to judge before the delivery rate counts."""
    min_planned_spend: float = 25.0
    """Planned spend to date, in the goal's currency, before spend pace counts."""
    batch_window: timedelta = timedelta(days=1)
    long_window: timedelta = timedelta(days=7)
    fast_window: timedelta | None = timedelta(hours=1)
    """The extra, short window complaint rates are also judged over; None turns it off."""
    delivery_grace: timedelta = timedelta(hours=1)


DEFAULT_GUARDS = Guards()


class BreakerResult(NamedTuple):
    alerts: list[Alert]
    paused_goals: set[str]
    paused_channels: set[Channel]


@dataclass
class _Counts:
    sends: int = 0
    delivers: int = 0
    complaints: int = 0
    unsubscribes: int = 0

    @property
    def deliveries(self) -> int:
        """The rate denominator: deliveries, or sends for a channel that never reports deliveries."""
        return self.delivers if self.delivers else self.sends


@dataclass
class _Trip:
    metric: StopMetric
    channel: Channel | None
    message: str


@dataclass
class _Breaker:
    """Collects the trips for one call of `evaluate()`."""

    goal_id: str | None
    now: datetime
    trips: list[tuple[StopRule, _Trip]] = field(default_factory=list)

    def result(self) -> BreakerResult:
        alerts: list[Alert] = []
        goals: set[str] = set()
        channels: set[Channel] = set()
        for rule, trip in self.trips:
            if rule.action == "pause_goal" and self.goal_id is not None:
                goals.add(self.goal_id)
                paused = True
                what = "Paused the goal."
            elif trip.channel is not None:
                channels.add(trip.channel)
                paused = True
                what = f"Paused {trip.channel.value}."
            else:
                paused = False
                what = "Nothing to pause automatically; please review."
            alerts.append(
                Alert(
                    alert_id=stable_id(
                        "alert",
                        "breaker",
                        trip.metric.value,
                        self.goal_id,
                        trip.channel,
                        self.now.isoformat(),
                    ),
                    at=self.now,
                    goal_id=self.goal_id,
                    severity="critical",
                    code=f"breaker.{trip.metric.value}",
                    message=f"{trip.message} {what}",
                    auto_paused=paused,
                )
            )
        return BreakerResult(alerts=alerts, paused_goals=goals, paused_channels=channels)


def _counts(events: Iterable[Event], since: datetime, now: datetime) -> dict[Channel, _Counts]:
    out: dict[Channel, _Counts] = defaultdict(_Counts)
    for e in events:
        if e.channel is None or not since < e.ts <= now:
            continue
        if e.decision_id is None and quality_level(e) is not None:
            continue  # an account-level quality report, not a message
        c = out[e.channel]
        if e.type is EventType.SEND:
            c.sends += 1
        elif e.type is EventType.DELIVER:
            c.delivers += 1
        elif e.type in COMPLAINT_TYPES:
            c.complaints += 1
        elif e.type is EventType.UNSUBSCRIBE:
            c.unsubscribes += 1
    return out


def _windows(guards: Guards) -> tuple[tuple[str, timedelta], ...]:
    return (("the last batch", guards.batch_window), ("the last 7 days", guards.long_window))


def _complaint_windows(guards: Guards) -> tuple[tuple[str, timedelta, bool], ...]:
    """(label, span, same-window messages only): the batch and 7-day windows, then the last hour.

    The hour comes last so a harm the longer windows already report keeps their wording; it only
    adds trips they would miss. It counts only complaints about messages sent in that hour (see
    `_same_window_complaints`)."""
    out = tuple((label, span, False) for label, span in _windows(guards))
    if guards.fast_window is None:
        return out
    return (*out, ("the last hour", guards.fast_window, True))


def _same_window_complaints(events: Sequence[Event], since: datetime, now: datetime) -> list[Event]:
    """The events, less complaints whose message did not go out (SEND or DELIVER) in (since, now].

    A complaint with no decision id cannot be matched, so it is left to the longer windows."""
    messages = {
        e.decision_id
        for e in events
        if e.decision_id is not None and e.type in (EventType.SEND, EventType.DELIVER) and since < e.ts <= now
    }
    return [e for e in events if e.type not in COMPLAINT_TYPES or e.decision_id in messages]


def _pct(x: float) -> str:
    return f"{x * 100:.2f}%"


def _complaint_rate(rule: StopRule, events: Sequence[Event], now: datetime, guards: Guards) -> list[_Trip]:
    trips: dict[Channel, _Trip] = {}
    for label, span, same_window in _complaint_windows(guards):
        since = now - span
        counts = _counts(_same_window_complaints(events, since, now) if same_window else events, since, now)
        for ch in sorted(counts):
            c = counts[ch]
            if ch in trips or c.deliveries < guards.min_deliveries or c.complaints < guards.min_count:
                continue
            rate = c.complaints / c.deliveries
            if rate > rule.threshold:
                trips[ch] = _Trip(
                    StopMetric.COMPLAINT_RATE,
                    ch,
                    f"{ch.value.capitalize()} complaints reached {_pct(rate)} of deliveries in {label} "
                    f"({c.complaints} of {c.deliveries}), above the {_pct(rule.threshold)} limit.",
                )
    return list(trips.values())


def _baseline(baselines: Mapping[str, float], channel: Channel) -> float | None:
    value = baselines.get(f"unsubscribe_rate/{channel.value}", baselines.get("unsubscribe_rate"))
    return value if value is not None and value > 0 else None


def _unsubscribe_multiple(
    rule: StopRule, events: Sequence[Event], baselines: Mapping[str, float], now: datetime, guards: Guards
) -> list[_Trip]:
    trips: dict[Channel, _Trip] = {}
    for label, span in _windows(guards):
        counts = _counts(events, now - span, now)
        for ch in sorted(counts):
            c = counts[ch]
            base = _baseline(baselines, ch)
            if (
                ch in trips
                or base is None
                or c.deliveries < guards.min_deliveries
                or c.unsubscribes < guards.min_count
            ):
                continue
            rate = c.unsubscribes / c.deliveries
            multiple = rate / base
            if multiple > rule.threshold:
                trips[ch] = _Trip(
                    StopMetric.UNSUBSCRIBE_MULTIPLE,
                    ch,
                    f"{ch.value.capitalize()} unsubscribes ran at {multiple:.1f}x the usual rate in {label} "
                    f"({_pct(rate)} vs {_pct(base)}; {c.unsubscribes} of {c.deliveries}), "
                    f"above the {rule.threshold:g}x limit.",
                )
    return list(trips.values())


def _delivery_rate(rule: StopRule, events: Sequence[Event], now: datetime, guards: Guards) -> list[_Trip]:
    trips: dict[Channel, _Trip] = {}
    cutoff = now - guards.delivery_grace
    for label, span in _windows(guards):
        since = now - span
        attempted: dict[Channel, set[str]] = defaultdict(set)
        accepted: set[tuple[Channel, str]] = set()
        delivered: set[tuple[Channel, str]] = set()
        bounced: set[tuple[Channel, str]] = set()
        has_receipts: set[Channel] = set()
        for e in events:
            if e.channel is None or not since < e.ts <= now:
                continue
            if e.decision_id is None and quality_level(e) is not None:
                continue  # an account-level quality report, not a message
            key = e.decision_id or e.event_id
            if e.type in (EventType.SEND, EventType.FAIL) and e.ts <= cutoff:
                attempted[e.channel].add(key)
            if e.type is EventType.SEND:
                accepted.add((e.channel, key))
            elif e.type is EventType.DELIVER:
                delivered.add((e.channel, key))
                has_receipts.add(e.channel)
            elif e.type is EventType.BOUNCE:
                bounced.add((e.channel, key))
        for ch in sorted(attempted):
            sent = attempted[ch]
            if ch in trips or len(sent) < guards.min_sends:
                continue
            if ch in has_receipts:
                ok = sum(1 for key in sent if (ch, key) in delivered)
            else:
                ok = sum(1 for key in sent if (ch, key) in accepted and (ch, key) not in bounced)
            rate = ok / len(sent)
            if rate < rule.threshold:
                trips[ch] = _Trip(
                    StopMetric.DELIVERY_RATE_BELOW,
                    ch,
                    f"Only {_pct(rate)} of {ch.value} messages were delivered in {label} "
                    f"({ok} of {len(sent)}), below the {_pct(rule.threshold)} floor.",
                )
    return list(trips.values())


def _spend_pace(rule: StopRule, spend: float, planned_spend: float, guards: Guards) -> list[_Trip]:
    if planned_spend < guards.min_planned_spend:
        if planned_spend <= 0 and spend >= guards.min_planned_spend:
            return [_Trip(StopMetric.SPEND_PACE, None, f"Spent {spend:,.2f} with nothing planned yet.")]
        return []
    pace = spend / planned_spend
    if pace <= rule.threshold:
        return []
    return [
        _Trip(
            StopMetric.SPEND_PACE,
            None,
            f"Spend is running at {pace:.2f}x plan ({spend:,.2f} vs {planned_spend:,.2f} planned so far), "
            f"above the {rule.threshold:g}x limit.",
        )
    ]


def quality_level(event: Event) -> int | None:
    """Meta's quality rating on an event as 0 (green), 1 (yellow) or 2 (red); None if it carries none."""
    for key in QUALITY_ATTR_KEYS:
        raw = event.attrs.get(key)
        if raw is None or isinstance(raw, bool):
            continue
        if isinstance(raw, int | float):
            return int(raw)
        level = _QUALITY_LEVELS.get(raw.strip().lower())
        if level is not None:
            return level
    return None


def _whatsapp_quality(rule: StopRule, events: Sequence[Event], now: datetime) -> list[_Trip]:
    # Meta reports the rating only when it changes, so the latest report stands however old it is.
    rated = [
        (e.ts, level)
        for e in events
        if e.channel is Channel.WHATSAPP and e.ts <= now and (level := quality_level(e)) is not None
    ]
    if not rated:
        return []
    _, level = max(rated, key=lambda r: r[0])
    if level < rule.threshold:
        return []
    name = {0: "green", 1: "yellow", 2: "red"}.get(level, str(level))
    return [
        _Trip(
            StopMetric.WHATSAPP_QUALITY,
            Channel.WHATSAPP,
            f"WhatsApp rated our number's quality {name}.",
        )
    ]


def evaluate(
    stop_rules: Sequence[StopRule],
    recent_events: Sequence[Event],
    baselines: Mapping[str, float],
    spend: float,
    planned_spend: float,
    now: datetime,
    *,
    goal_id: str | None = None,
    guards: Guards = DEFAULT_GUARDS,
) -> BreakerResult:
    """Check every stop rule against recent events; return alerts and what to pause.

    Pass one goal's events, spend and plan with its `goal_id` (see `evaluate_goals`), or the whole
    programme's with `goal_id=None`. Events after `now` are ignored.
    """
    breaker = _Breaker(goal_id=goal_id, now=now)
    for rule in stop_rules:
        if rule.metric is StopMetric.COMPLAINT_RATE:
            trips = _complaint_rate(rule, recent_events, now, guards)
        elif rule.metric is StopMetric.UNSUBSCRIBE_MULTIPLE:
            trips = _unsubscribe_multiple(rule, recent_events, baselines, now, guards)
        elif rule.metric is StopMetric.DELIVERY_RATE_BELOW:
            trips = _delivery_rate(rule, recent_events, now, guards)
        elif rule.metric is StopMetric.SPEND_PACE:
            trips = _spend_pace(rule, spend, planned_spend, guards)
        else:
            trips = _whatsapp_quality(rule, recent_events, now)
        breaker.trips.extend((rule, t) for t in trips)
    return breaker.result()


def split_by_goal(events: Iterable[Event]) -> dict[str | None, list[Event]]:
    """Group events by goal, using the `goal_id` the dispatcher writes on each SEND event's attrs.

    Events of a decision we never saw sent land under None. Account-level WhatsApp quality events
    (no decision) are copied to every goal, because a poor rating affects every goal on the number.
    """
    rows = list(events)
    goal_of: dict[str, str] = {}
    for e in rows:
        g = e.attrs.get("goal_id")
        if e.type is EventType.SEND and e.decision_id and isinstance(g, str):
            goal_of[e.decision_id] = g
    out: dict[str | None, list[Event]] = defaultdict(list)
    shared: list[Event] = []
    for e in rows:
        if e.decision_id is None and quality_level(e) is not None:
            shared.append(e)
            continue
        out[goal_of.get(e.decision_id) if e.decision_id else None].append(e)
    for goal, bucket in out.items():
        if goal is not None:
            bucket.extend(shared)
    out.setdefault(None, []).extend(shared)
    return dict(out)


def evaluate_goals(
    briefs: Sequence[Brief],
    recent_events: Sequence[Event],
    baselines: Mapping[str, float],
    spend: Mapping[str, float],
    planned_spend: Mapping[str, float],
    now: datetime,
    *,
    guards: Guards = DEFAULT_GUARDS,
) -> BreakerResult:
    """Run `evaluate()` once per brief with that goal's own stop rules, events and spend, and merge."""
    by_goal = split_by_goal(recent_events)
    # A goal with no events of its own still sees account-level quality reports, but never the
    # unattributed events of other goals (callbacks whose SEND fell outside `recent_events`).
    shared = [e for e in by_goal.get(None, []) if e.decision_id is None and quality_level(e) is not None]
    merged = BreakerResult(alerts=[], paused_goals=set(), paused_channels=set())
    for brief in briefs:
        events = by_goal.get(brief.goal_id, shared)
        result = evaluate(
            brief.envelope.stop_rules,
            events,
            baselines,
            spend.get(brief.goal_id, 0.0),
            planned_spend.get(brief.goal_id, 0.0),
            now,
            goal_id=brief.goal_id,
            guards=guards,
        )
        merged.alerts.extend(result.alerts)
        merged.paused_goals.update(result.paused_goals)
        merged.paused_channels.update(result.paused_channels)
    return merged


# --- negative lift: the proof says the goal loses money -------------------------------------------


@dataclass(frozen=True)
class LiftGuards:
    """When the negative-lift breaker may judge a goal, and which bound it reads.

    `min_group` measured customers in both the agent group and the holdout (the proof's own floor),
    and at least `min_closed` closed outcome windows in all, before anything is judged.

    `bound` is the end of the 95% interval of incremental profit that must be below zero to trip:
    "high" (the default) trips only when the whole interval is below zero, i.e. we are confident the
    goal earns less than leaving its customers alone. "low" trips as soon as the interval reaches
    below zero, which pauses every goal that has not yet proved a profit; it is there for a business
    that wants proof before spend, and is not the default.
    """

    min_group: int = MIN_GROUP
    min_closed: int = 100
    bound: Literal["high", "low"] = "high"


DEFAULT_LIFT_GUARDS = LiftGuards()


def negative_lift(
    brief: Brief,
    decisions: Sequence[Decision],
    events_by_customer: Mapping[str, Sequence[Event]],
    now: datetime,
    *,
    costs: Costs | None = None,
    guards: LiftGuards = DEFAULT_LIFT_GUARDS,
) -> BreakerResult:
    """Pause the brief's goal when its proof shows it losing money against the holdout.

    `decisions` are the goal's own decisions (holdout "do nothing" ones included) and
    `events_by_customer` those customers' events; rewards are built with last-touch attribution
    (`proof_rewards`) as of `now`, so only closed outcome windows count. `costs` prices messages and
    offers (default: offer costs from the brief, no channel cost, unknown margin counted as revenue,
    which flatters the goal: a harm detector should not trip on our own guesses).

    One-sided: the result pauses a goal or does nothing; it never reports a win. Below the guards'
    minimum sample, or when the proof refuses to put a number on the goal, nothing is judged.
    """
    rewards = proof_rewards(
        decisions,
        events_by_customer,
        brief.horizon_days,
        brief.objective,
        costs or Costs.from_brief(brief),
        now=now,
    )
    empty = BreakerResult(alerts=[], paused_goals=set(), paused_channels=set())
    if len(rewards) < guards.min_closed:
        return empty
    summary = proof_summary(rewards, decisions, now, brief.goal_id, min_group=guards.min_group)
    profit = summary.incremental_profit
    if profit is None:
        return empty
    bound = profit.high if guards.bound == "high" else profit.low
    if bound >= 0:
        return empty
    which = "upper" if guards.bound == "high" else "lower"
    message = (
        f"Against the no-contact holdout, this goal's incremental profit is {profit.point:,.2f} "
        f"(95% interval {profit.low:,.2f} to {profit.high:,.2f}; {summary.agent_n:,} agent and "
        f"{summary.holdout_n:,} holdout customers over closed outcome windows). Its {which} bound is "
        "below zero, so it is losing money compared with doing nothing. Paused the goal."
    )
    alert = Alert(
        alert_id=stable_id("alert", "breaker", NEGATIVE_LIFT_CODE, brief.goal_id, now.isoformat()),
        at=now,
        goal_id=brief.goal_id,
        severity="critical",
        code=NEGATIVE_LIFT_CODE,
        message=message,
        auto_paused=True,
    )
    return BreakerResult(alerts=[alert], paused_goals={brief.goal_id}, paused_channels=set())
