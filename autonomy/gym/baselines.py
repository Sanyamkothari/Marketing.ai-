"""The policies every agent is measured against, from send-to-all up to the oracle.

`ContactEveryone`, `RuleBaseline` and `BestFixedRule` see only what a real policy sees (customer
state and the decision context). `BestFixedRule` is the one to beat: "WhatsApp if they consented,
else email, no offer, at one fixed hour, as often as the cap allows" needs no learning at all and,
in the plain shop, already earns more than half of the oracle (docs/v2/GYM_RESULTS.md). An agent
that learns should do better than that.

Two policies read the Gym's planted truth and so mark ceilings, never targets for a real policy:

* `SegmentOracle` reads it only through the average of each observable segment (new, lapsed,
  light, medium, heavy buyer) x reachable channels: the best fixed action and hour per segment.
  That is what perfect knowledge at the level a business can see is worth: the realistic ceiling.
* `OraclePolicy` reads every customer's own truth: the full ceiling, which needs knowledge of
  hidden personas no policy can have within 90 days (docs/v2/GYM_BAR.md).

All of them keep the hard rules a real deployment enforces (consent, the weekly cap, quiet hours), so
a gap between them and the agent is about judgement, not about rule-breaking.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Protocol, runtime_checkable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import numpy as np
import numpy.typing as npt

from autonomy.contracts import (
    NO_OFFER,
    Action,
    Brief,
    Channel,
    ContentItem,
    ContentStatus,
    CustomerState,
    Decision,
    DecisionContext,
    DecisionKind,
    Envelope,
    Format,
    OfferKind,
    OfferOption,
    Reward,
    stable_id,
)
from autonomy.gym.api import GroundTruth

DEFAULT_SEND_HOUR = 10

# --- time helpers ------------------------------------------------------------------------------


@lru_cache(maxsize=256)
def _zone(tz: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def local_hour(ts: datetime, tz: str) -> int:
    """The hour on the customer's clock at `ts`; unknown time zones count as UTC."""
    return ts.astimezone(_zone(tz)).hour


def next_local_time(now: datetime, tz: str, hour: int) -> datetime:
    """The first moment at or after `now` when the customer's clock shows `hour`:00, in UTC."""
    local = now.astimezone(_zone(tz))
    candidate = local.replace(minute=0, second=0, microsecond=0) + timedelta(hours=(hour - local.hour) % 24)
    if candidate < local:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


def in_quiet_hours(hour: int, start: int, end: int) -> bool:
    """True if `hour` falls in quiet hours running from `start` to `end` (which may wrap midnight).
    Equal start and end means no quiet hours."""
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def is_quiet(hour: int, envelope: Envelope) -> bool:
    """True if a send at this local hour falls in the envelope's quiet hours."""
    return in_quiet_hours(hour, envelope.quiet_start_hour, envelope.quiet_end_hour)


def allowed_hours(envelope: Envelope) -> tuple[int, ...]:
    return tuple(h for h in range(24) if not is_quiet(h, envelope))


def send_hour(preferred: int, envelope: Envelope) -> int | None:
    """`preferred` if allowed, else the first hour after quiet hours end; None if every hour is quiet."""
    if not is_quiet(preferred, envelope):
        return preferred
    if not is_quiet(envelope.quiet_end_hour, envelope):
        return envelope.quiet_end_hour
    hours = allowed_hours(envelope)
    return hours[0] if hours else None


# --- shared pieces -----------------------------------------------------------------------------


def weekly_cap(briefs: Sequence[Brief]) -> int:
    """The per-customer weekly cap is across all goals, so the tightest brief wins."""
    return min((b.envelope.max_contacts_per_week for b in briefs), default=0)


def usable_content(content: Sequence[ContentItem], brief: Brief, channel: Channel) -> list[ContentItem]:
    """Approved items for this goal (or any goal) on this channel whose offer the envelope allows."""
    offers = {o.offer_id for o in brief.envelope.offers}
    return [
        c
        for c in content
        if c.channel is channel
        and c.status is ContentStatus.APPROVED
        and c.goal_id in (None, brief.goal_id)
        and c.offer_id in offers
    ]


def discount_size(offer: OfferOption) -> tuple[float, float]:
    """How generous an offer looks: what it costs the business, then its headline amount."""
    return (offer.cost_per_redemption, offer.amount)


def _decision(
    state: CustomerState,
    ctx: DecisionContext,
    version: str,
    reason: str,
    action: Action | None = None,
    expected_net: float = 0.0,
) -> Decision:
    return Decision(
        decision_id=stable_id("dec", state.customer_id, _iso(ctx.now), version),
        customer_id=state.customer_id,
        made_at=ctx.now,
        kind=DecisionKind.ACT if action is not None else DecisionKind.NOTHING,
        action=action,
        group=state.group,
        probability=1.0,
        expected_net=expected_net,
        reason=reason,
        policy_version=version,
    )


@lru_cache(maxsize=8)
def _iso(now: datetime) -> str:
    return now.isoformat()


class _Static:
    """Baselines do not learn."""

    def learn(self, rewards: Sequence[Reward], decisions: Mapping[str, Decision]) -> None:  # noqa: ARG002
        return None


# --- send to all -------------------------------------------------------------------------------


class ContactEveryone(_Static):
    """Send to everyone as often as the weekly cap allows: first consented channel, the most generous
    offer that channel has content for, at 10:00 on the customer's clock."""

    version = "contact-everyone-v1"

    def decide(self, state: CustomerState, ctx: DecisionContext) -> Decision:
        if state.contacts_7d >= weekly_cap(ctx.briefs):
            return _decision(state, ctx, self.version, "The weekly contact cap is reached.")
        reachable = state.contactable_channels()
        for brief in ctx.briefs:
            for ch in brief.envelope.channels:
                if ch not in reachable:
                    continue
                hour = send_hour(DEFAULT_SEND_HOUR, brief.envelope)
                if hour is None:
                    continue
                items = usable_content(ctx.content, brief, ch)
                by_id = {o.offer_id: o for o in brief.envelope.offers}
                content_id: str | None
                if items:
                    item = max(items, key=lambda c: discount_size(by_id[c.offer_id]))
                    offer_id, content_id, fmt = item.offer_id, item.content_id, item.format
                else:
                    offer = max(brief.envelope.offers, key=discount_size)
                    offer_id, content_id, fmt = offer.offer_id, None, Format.TEXT
                action = Action(
                    goal_id=brief.goal_id,
                    channel=ch,
                    offer_id=offer_id,
                    content_id=content_id,
                    format=fmt,
                    send_at=next_local_time(ctx.now, state.timezone, hour),
                )
                return _decision(
                    state,
                    ctx,
                    self.version,
                    f"Send to everyone: {ch.value} with the biggest offer ({offer_id}).",
                    action,
                )
        return _decision(state, ctx, self.version, "No allowed channel can reach this customer.")


# --- a sensible marketer's rule ----------------------------------------------------------------


class RuleBaseline(_Static):
    """What a careful marketer would set up by hand: email customers who have not bought in 60+ days,
    with 10% off, at most once every 14 days, at 10:00 on their clock."""

    version = "rule-baseline-v1"

    def __init__(self, lapsed_days: int = 60, gap_days: int = 14, percent_off: float = 10.0) -> None:
        self.lapsed_days = lapsed_days
        self.gap_days = gap_days
        self.percent_off = percent_off

    def _offer(self, envelope: Envelope) -> OfferOption | None:
        pct = [o for o in envelope.offers if o.kind is OfferKind.PERCENT_OFF]
        if not pct:
            return None
        return min(pct, key=lambda o: (abs(o.amount - self.percent_off), o.offer_id))

    def decide(self, state: CustomerState, ctx: DecisionContext) -> Decision:
        now = ctx.now
        if state.purchases < 1 or state.last_purchase_at is None:
            return _decision(state, ctx, self.version, "Never bought, so not a lapsed customer.")
        if now - state.last_purchase_at < timedelta(days=self.lapsed_days):
            return _decision(state, ctx, self.version, f"Bought in the last {self.lapsed_days} days.")
        if state.last_contact_at is not None and now - state.last_contact_at < timedelta(days=self.gap_days):
            return _decision(state, ctx, self.version, f"Contacted in the last {self.gap_days} days.")
        if state.open_complaint or state.contacts_7d >= weekly_cap(ctx.briefs):
            return _decision(state, ctx, self.version, "Complained, or the weekly cap is reached.")
        if Channel.EMAIL not in state.contactable_channels():
            return _decision(state, ctx, self.version, "No email consent.")
        for brief in ctx.briefs:
            if Channel.EMAIL not in brief.envelope.channels:
                continue
            hour = send_hour(DEFAULT_SEND_HOUR, brief.envelope)
            if hour is None:
                continue
            offer = self._offer(brief.envelope)
            offer_id = offer.offer_id if offer is not None else "none"
            items = usable_content(ctx.content, brief, Channel.EMAIL)
            item = next((c for c in items if c.offer_id == offer_id), None)
            action = Action(
                goal_id=brief.goal_id,
                channel=Channel.EMAIL,
                offer_id=offer_id,
                content_id=item.content_id if item is not None else None,
                send_at=next_local_time(now, state.timezone, hour),
            )
            return _decision(
                state,
                ctx,
                self.version,
                f"Lapsed {self.lapsed_days}+ days: email with {offer_id}.",
                action,
            )
        return _decision(state, ctx, self.version, "No brief allows email.")


# --- the oracle ----------------------------------------------------------------------------------


@runtime_checkable
class BatchTruth(Protocol):
    """An optional fast path a truth may offer: `expected_net` of one action for many customers,
    by row index. The Gym's `PlantedTruth` has it; the oracle uses it when present, because a
    per-customer call for every candidate every day is far too slow for 5,000 customers x 90 days."""

    def index(self, customer_id: str) -> int: ...

    def net(self, idx: npt.NDArray[np.int64], action: Action, now: datetime) -> npt.NDArray[np.float64]: ...


@dataclass(frozen=True)
class _Template:
    """One candidate without its send time: goal, channel, offer and content."""

    brief: Brief
    channel: Channel
    offer_id: str
    content_id: str | None
    format: Format

    def at(self, send_at: datetime) -> Action:
        return Action(
            goal_id=self.brief.goal_id,
            channel=self.channel,
            offer_id=self.offer_id,
            content_id=self.content_id,
            format=self.format,
            send_at=send_at,
        )


def _templates(ctx: DecisionContext) -> list[_Template]:
    """Every channel x offer x content combination the briefs allow, in a fixed order."""
    out: list[_Template] = []
    for brief in ctx.briefs:
        for ch in brief.envelope.channels:
            items = usable_content(ctx.content, brief, ch)
            if items:
                out.extend(_Template(brief, ch, c.offer_id, c.content_id, c.format) for c in items)
            else:
                out.extend(_Template(brief, ch, o.offer_id, None, Format.TEXT) for o in brief.envelope.offers)
    return out


class OraclePolicy(_Static):
    """Reads the planted truth and picks the best allowed action, at the customer's best hour.

    The candidates are every channel x offer x content item the envelope allows and the customer can
    receive, sent at the allowed local hour where the truth says a contact is worth most. The best
    hour is found once per customer (it is a fixed trait) by probing the truth across the allowed
    hours with the first candidate of each channel.

    With a `BatchTruth`, `prepare_day` (called by the scoreboard before the day's decisions) plans
    every customer in a few vectorised calls; `decide` then reads the plan. Without it, or for a
    customer the plan does not cover, `decide` asks the truth one customer at a time. Both paths
    choose the same action.
    """

    version = "oracle-v1"

    def __init__(self, truth: GroundTruth) -> None:
        self.truth = truth
        self._best_hour: dict[str, int] = {}
        self._plan: dict[str, tuple[Action, float] | None] = {}
        self._plan_at: datetime | None = None

    @staticmethod
    def _hours(ctx: DecisionContext) -> list[int]:
        return [h for h in range(24) if all(not is_quiet(h, b.envelope) for b in ctx.briefs)]

    def candidates(self, state: CustomerState, ctx: DecisionContext, hour: int) -> list[Action]:
        """Every allowed action for this customer if sent at `hour` on their clock."""
        reachable = state.contactable_channels()
        send_at = next_local_time(ctx.now, state.timezone, hour)
        return [
            t.at(send_at)
            for t in _templates(ctx)
            if t.channel in reachable and not is_quiet(hour, t.brief.envelope)
        ]

    def _hour(self, state: CustomerState, ctx: DecisionContext) -> int | None:
        cid = state.customer_id
        if cid in self._best_hour:
            return self._best_hour[cid]
        hours = self._hours(ctx)
        if not hours:
            return None
        best_h, best_v = hours[0], float("-inf")
        for h in hours:
            probe = self.candidates(state, ctx, h)
            if not probe:
                return None  # nothing reachable yet; try again on a later day
            firsts = {a.channel: a for a in reversed(probe)}.values()
            value = max(self.truth.expected_net(cid, a, ctx.now) for a in firsts)
            if value > best_v:
                best_h, best_v = h, value
        self._best_hour[cid] = best_h
        return best_h

    def prepare_day(self, states: Sequence[CustomerState], ctx: DecisionContext) -> None:
        """Plan the day for every customer in `states` at once, if the truth allows batch calls."""
        self._plan, self._plan_at = {}, None
        truth = self.truth
        if not isinstance(truth, BatchTruth):
            return
        hours, templates = self._hours(ctx), _templates(ctx)
        cap = weekly_cap(ctx.briefs)
        eligible = [s for s in states if s.contacts_7d < cap and s.contactable_channels()]
        if not hours or not templates:
            return
        probes = list({t.channel: t for t in reversed(templates)}.values())

        by_tz: dict[str, list[CustomerState]] = {}
        for s in eligible:
            if s.customer_id not in self._best_hour:
                by_tz.setdefault(s.timezone, []).append(s)
        for tz, group in by_tz.items():
            idx = np.array([truth.index(s.customer_id) for s in group], dtype=np.int64)
            # Probe only the channels each customer can receive, as `_hour` does, so both paths
            # settle on the same hour; a customer no probe reaches gets no hour (as in `_hour`).
            probe_reach = np.array(
                [[p.channel in s.contactable_channels() for s in group] for p in probes], dtype=np.bool_
            )
            best_v = np.full(len(group), -np.inf)
            best_h = np.full(len(group), hours[0], dtype=np.int64)
            for h in hours:
                send_at = next_local_time(ctx.now, tz, h)
                probed = np.vstack([truth.net(idx, p.at(send_at), ctx.now) for p in probes])
                v = np.max(np.where(probe_reach, probed, -np.inf), axis=0)
                better = v > best_v
                best_v = np.where(better, v, best_v)
                best_h = np.where(better, h, best_h)
            reached = probe_reach.any(axis=0).tolist()
            for s, h, ok in zip(group, best_h.tolist(), reached, strict=True):
                if ok:
                    self._best_hour[s.customer_id] = int(h)

        by_slot: dict[tuple[str, int], list[CustomerState]] = {}
        for s in eligible:
            hour = self._best_hour.get(s.customer_id)
            if hour is not None:  # no hour: `decide` falls back to the one-at-a-time path
                by_slot.setdefault((s.timezone, hour), []).append(s)
        for (tz, h), group in by_slot.items():
            send_at = next_local_time(ctx.now, tz, h)
            idx = np.array([truth.index(s.customer_id) for s in group], dtype=np.int64)
            actions = [t.at(send_at) for t in templates]
            values = np.vstack([truth.net(idx, a, ctx.now) for a in actions])
            reaches = [set(s.contactable_channels()) for s in group]
            allowed = np.array(
                [[t.channel in r and not is_quiet(h, t.brief.envelope) for r in reaches] for t in templates],
                dtype=np.bool_,
            )
            values = np.where(allowed, values, -np.inf)
            pick = np.argmax(values, axis=0)
            best = values[pick, np.arange(len(group))]
            for s, k, v in zip(group, pick.tolist(), best.tolist(), strict=True):
                self._plan[s.customer_id] = (actions[k], float(v)) if v > 0 else None
        self._plan_at = ctx.now

    def decide(self, state: CustomerState, ctx: DecisionContext) -> Decision:
        if state.contacts_7d >= weekly_cap(ctx.briefs):
            return _decision(state, ctx, self.version, "The weekly contact cap is reached.")
        if not state.contactable_channels():
            return _decision(state, ctx, self.version, "No allowed channel can reach this customer.")
        if self._plan_at == ctx.now and state.customer_id in self._plan:
            planned = self._plan[state.customer_id]
            best, value = planned if planned is not None else (None, 0.0)
        else:
            hour = self._hour(state, ctx)
            if hour is None:
                return _decision(state, ctx, self.version, "No allowed action for this customer.")
            candidates = self.candidates(state, ctx, hour)
            best = self.truth.best_action(state.customer_id, candidates, ctx.now) if candidates else None
            value = self.truth.expected_net(state.customer_id, best, ctx.now) if best is not None else 0.0
        if best is None:
            return _decision(state, ctx, self.version, "The truth says no contact beats doing nothing.")
        hour = local_hour(best.send_at, state.timezone)
        return _decision(
            state,
            ctx,
            self.version,
            f"The truth's best action: {best.channel.value} with {best.offer_id} at {hour}:00 local.",
            best,
            expected_net=value,
        )


# --- the best fixed rule -------------------------------------------------------------------------

BEST_FIXED_HOUR = 14
"""The fixed local send hour of `BestFixedRule`. Mid-afternoon is the hour a marketer with no data
would pick (inside every shop's opening hours, after lunch); GYM_RESULTS.md scored 14:00 to 18:00
within two points of each other, and 14:00 is the lower end, so the default is not fitted to the
Gym's planted best hours."""

MIN_GAP_HOURS = 6.0
"""No new message within this many hours of the last one. An unsubscribe arrives two to three hours
after the message that caused it; a decision made in between cannot see it yet, and a message it
schedules would then break the unsubscribe."""


def _gap_ok(state: CustomerState, now: datetime, gap_hours: float) -> bool:
    last = state.last_contact_at
    return last is None or now - last >= timedelta(hours=gap_hours)


def _in_audience(state: CustomerState, brief: Brief, now: datetime) -> bool:
    """True when the customer is in the brief's audience; an audience without filters is everyone.

    The filter rules live in `autonomy.decide.audience` and are imported only when a brief has
    filters, so the Gym's baselines load without the decide package."""
    if not brief.audience.filters:
        return True
    from autonomy.decide.audience import matches

    return matches(state, brief.audience, now)


class BestFixedRule(_Static):
    """WhatsApp if the customer consented, else email; no offer; at one fixed hour on the customer's
    clock (14:00 by default, moved out of quiet hours if need be); everyone in the audience, as often
    as the weekly cap allows.

    It never reads the truth: it is built without one and decides from the customer's state and the
    decision context alone. It is the strongest policy in the Gym that needs no learning, so an agent
    that learns has to beat it to be worth its complexity.

    Like any careful sender it keeps a minimum gap of `min_gap_hours` after the last message, holds a
    customer while a message it scheduled is still waiting to go out, and leaves alone a customer
    with an open complaint.
    """

    version = "best-fixed-rule-v1"

    def __init__(self, hour: int = BEST_FIXED_HOUR, *, min_gap_hours: float = MIN_GAP_HOURS) -> None:
        if not 0 <= hour < 24:
            raise ValueError(f"hour must be 0-23, not {hour}")
        self.hour = hour
        self.min_gap_hours = min_gap_hours
        self._scheduled: dict[str, datetime] = {}
        self._last_now: datetime | None = None

    def decide(self, state: CustomerState, ctx: DecisionContext) -> Decision:
        now = ctx.now
        cid = state.customer_id
        if self._last_now is not None and now < self._last_now:
            # Time ran backwards: the same instance is being reused for a new run (another seed).
            # Sends it scheduled in the old run must not hold customers in the new one.
            self._scheduled = {}
        self._last_now = now
        if state.open_complaint:
            return _decision(state, ctx, self.version, "They have an open complaint.")
        pending = self._scheduled.get(cid)
        if pending is not None and pending > now:
            return _decision(state, ctx, self.version, "A message is already scheduled.")
        if state.contacts_7d >= weekly_cap(ctx.briefs):
            return _decision(state, ctx, self.version, "The weekly contact cap is reached.")
        if not _gap_ok(state, now, self.min_gap_hours):
            return _decision(state, ctx, self.version, "Contacted too recently.")
        reachable = set(state.contactable_channels())
        for brief in ctx.briefs:
            if all(o.offer_id != NO_OFFER.offer_id for o in brief.envelope.offers):
                continue  # this goal's envelope does not allow a message without an offer
            allowed = brief.envelope.channels
            if Channel.WHATSAPP in allowed and Channel.WHATSAPP in reachable:
                ch = Channel.WHATSAPP
            elif Channel.EMAIL in allowed and Channel.EMAIL in reachable:
                ch = Channel.EMAIL
            else:
                continue
            hour = send_hour(self.hour, brief.envelope)
            if hour is None or not _in_audience(state, brief, now):
                continue
            item = next(
                (c for c in usable_content(ctx.content, brief, ch) if c.offer_id == NO_OFFER.offer_id),
                None,
            )
            action = Action(
                goal_id=brief.goal_id,
                channel=ch,
                offer_id=NO_OFFER.offer_id,
                content_id=item.content_id if item is not None else None,
                format=item.format if item is not None else Format.TEXT,
                send_at=next_local_time(now, state.timezone, hour),
            )
            self._scheduled[cid] = action.send_at
            return _decision(
                state,
                ctx,
                self.version,
                f"Fixed rule: {ch.value}, no offer, at {hour}:00 local.",
                action,
            )
        return _decision(state, ctx, self.version, "No allowed channel can reach this customer.")


# --- the segment oracle --------------------------------------------------------------------------

NEW_CUSTOMER_DAYS = 120
"""First seen this recently: "new" (no buying rhythm to read yet)."""
LAPSED_DAYS = 90
"""No purchase for this long, or none ever after the new period: "lapsed"."""
MIN_OBSERVED_DAYS = 30.0
DEFAULT_FREQUENCY_CUTS = (0.15, 0.35)
"""Purchases per 30 days splitting light / medium / heavy buyers until the terciles are fitted."""

OBSERVABLE_SEGMENTS: tuple[str, ...] = ("new", "lapsed", "light", "medium", "heavy")

SegmentKey = tuple[str, tuple[str, ...]]
"""(observable segment, sorted reachable channels): the unit `SegmentOracle` decides for."""


def purchase_frequency(state: CustomerState, now: datetime, data_start: datetime | None = None) -> float:
    """Purchases per 30 days since the customer was first seen or, when it is later, since the start
    of the business's purchase data (never over less than a month). The agent's segmenter does the
    same: a customer who signed up two years ago but whose orders were imported for six months only
    must not look three times lighter than a six-month customer with the same orders."""
    start = state.first_seen_at
    if data_start is not None and (start is None or start < data_start):
        start = data_start
    days = (now - start).total_seconds() / 86_400.0 if start is not None else MIN_OBSERVED_DAYS
    return state.purchases * 30.0 / max(days, MIN_OBSERVED_DAYS)


def observable_segment(
    state: CustomerState,
    now: datetime,
    cuts: tuple[float, float] = DEFAULT_FREQUENCY_CUTS,
    data_start: datetime | None = None,
) -> str:
    """The behaviour segment a business can see from its own data: tenure, recency and frequency.

    The same definitions the agent's segments use (new for 120 days, lapsed after 90 days without a
    purchase, light / medium / heavy by purchases per 30 days), so the segment oracle and the agent
    look at customers through the same window."""
    if state.first_seen_at is not None and now - state.first_seen_at <= timedelta(days=NEW_CUSTOMER_DAYS):
        return "new"
    last = state.last_purchase_at
    if state.purchases <= 0 or last is None or now - last > timedelta(days=LAPSED_DAYS):
        return "lapsed"
    f = purchase_frequency(state, now, data_start)
    if f >= cuts[1]:
        return "heavy"
    if f >= cuts[0]:
        return "medium"
    return "light"


def frequency_cuts(
    states: Sequence[CustomerState], now: datetime, data_start: datetime | None = None
) -> tuple[float, float]:
    """The terciles of purchase frequency among active, non-new buyers; the defaults if under 30."""
    active = sorted(
        purchase_frequency(s, now, data_start)
        for s in states
        if observable_segment(s, now) not in ("new", "lapsed")
    )
    if len(active) < 30:
        return DEFAULT_FREQUENCY_CUTS
    lo, hi = active[len(active) // 3], active[(2 * len(active)) // 3]
    return (lo, hi) if hi > lo else DEFAULT_FREQUENCY_CUTS


SegmentOf = Callable[[CustomerState, datetime], str]


class SegmentOracle(_Static):
    """The realistic ceiling: the best fixed action and hour per observable segment x reachable
    channels, chosen with the planted truth's average over the segment. Diagnostic only.

    Each morning (`prepare_day`) the customers who may be contacted today (consent, the cap, the
    gap after the last message, nothing still scheduled, no open complaint) are grouped by observable
    segment and the set of channels that can reach them. For each group the truth values every
    allowed channel x offer x content item at every allowed local hour for each member, and only the
    group's mean is used: the group gets the action and hour with the highest mean, or nothing when
    no mean is above zero. Every member of a group gets the same action at the same local hour, so
    nothing a member's own truth says beyond their segment's average can steer the choice. It never
    asks the truth for a persona, a sleeping-dog flag or a best action.

    `segment_of` replaces the default segments (for example to add an observable signal a scenario
    plants); by default it is `observable_segment` with frequency cuts fitted on the first day.
    `history_days` is how far back the business's purchase data goes before the first day (the
    scenario's history); frequency is then counted from there, as the agent counts it from its first
    imported order, rather than from a signup that may predate the data by a year.
    `decide` without a plan for today (no `prepare_day` call) does nothing: a per-customer answer
    would have to read that customer's own truth.
    """

    version = "segment-oracle-v1"

    def __init__(
        self,
        truth: GroundTruth,
        *,
        segment_of: SegmentOf | None = None,
        min_gap_hours: float = MIN_GAP_HOURS,
        history_days: int | None = None,
    ) -> None:
        self.truth = truth
        self.min_gap_hours = min_gap_hours
        self.history_days = history_days
        self._segment_of = segment_of
        self._cuts: tuple[float, float] | None = None
        self._data_start: datetime | None = None
        self._plan: dict[str, tuple[Action, float, SegmentKey]] = {}
        self._plan_at: datetime | None = None
        self._scheduled: dict[str, datetime] = {}
        self.choices: dict[SegmentKey, tuple[str, int, float] | None] = {}
        """Today's choice per group: (action label, local hour, mean true value), or None."""

    def segment(self, state: CustomerState, now: datetime) -> str:
        if self._segment_of is not None:
            return self._segment_of(state, now)
        return observable_segment(state, now, self._cuts or DEFAULT_FREQUENCY_CUTS, self._data_start)

    def key(self, state: CustomerState, now: datetime) -> SegmentKey:
        reach = tuple(sorted(c.value for c in state.contactable_channels()))
        return (self.segment(state, now), reach)

    def _eligible(self, state: CustomerState, now: datetime, cap: int) -> bool:
        pending = self._scheduled.get(state.customer_id)
        return (
            bool(state.contactable_channels())
            and not state.open_complaint
            and state.contacts_7d < cap
            and (pending is None or pending <= now)
            and _gap_ok(state, now, self.min_gap_hours)
        )

    def _values(
        self, states: Sequence[CustomerState], action: Action, now: datetime
    ) -> npt.NDArray[np.float64]:
        truth = self.truth
        if isinstance(truth, BatchTruth):
            idx = np.array([truth.index(s.customer_id) for s in states], dtype=np.int64)
            return np.asarray(truth.net(idx, action, now), dtype=np.float64)
        return np.array([truth.expected_net(s.customer_id, action, now) for s in states], dtype=np.float64)

    def prepare_day(self, states: Sequence[CustomerState], ctx: DecisionContext) -> None:
        """Choose today's action and hour for every group, and plan each eligible customer."""
        now = ctx.now
        if self._plan_at is not None and now < self._plan_at:
            # Time ran backwards: the same instance is being reused for a new run (another seed).
            self._scheduled, self._cuts, self._data_start = {}, None, None
        self._plan, self._plan_at, self.choices = {}, now, {}
        if self._cuts is None and self._segment_of is None:
            if self.history_days is not None:
                self._data_start = now - timedelta(days=self.history_days)
            self._cuts = frequency_cuts(states, now, self._data_start)
        cap = weekly_cap(ctx.briefs)
        eligible = [s for s in states if self._eligible(s, now, cap)]
        hours = [h for h in range(24) if all(not is_quiet(h, b.envelope) for b in ctx.briefs)]
        templates = _templates(ctx)
        if not eligible or not hours or not templates:
            return
        cands = [(t, h) for h in hours for t in templates if not is_quiet(h, t.brief.envelope)]
        values = np.zeros((len(cands), len(eligible)), dtype=np.float64)
        by_tz: dict[str, list[int]] = {}
        for k, s in enumerate(eligible):
            by_tz.setdefault(s.timezone, []).append(k)
        send_at: dict[tuple[str, int], datetime] = {}
        for tz, cols in by_tz.items():
            members = [eligible[k] for k in cols]
            col = np.array(cols, dtype=np.int64)
            for h in hours:
                send_at[(tz, h)] = next_local_time(now, tz, h)
            for r, (t, h) in enumerate(cands):
                values[r, col] = self._values(members, t.at(send_at[(tz, h)]), now)

        groups: dict[SegmentKey, list[int]] = {}
        for k, s in enumerate(eligible):
            groups.setdefault(self.key(s, now), []).append(k)
        for key, cols in sorted(groups.items(), key=lambda kv: kv[0]):
            reach = set(key[1])
            allowed = np.array([t.channel.value in reach for t, _ in cands], dtype=np.bool_)
            means = np.where(allowed, values[:, cols].mean(axis=1), -np.inf)
            r = int(np.argmax(means))
            best = float(means[r])
            if not best > 0:
                self.choices[key] = None
                continue
            t, h = cands[r]
            self.choices[key] = (f"{t.channel.value}/{t.offer_id}/{t.content_id}", h, best)
            for k in cols:
                s = eligible[k]
                self._plan[s.customer_id] = (t.at(send_at[(s.timezone, h)]), best, key)

    def decide(self, state: CustomerState, ctx: DecisionContext) -> Decision:
        if self._plan_at != ctx.now:
            return _decision(state, ctx, self.version, "No plan for today.")
        planned = self._plan.get(state.customer_id)
        if planned is None:
            return _decision(
                state,
                ctx,
                self.version,
                "Not eligible today, or nothing beats doing nothing for the segment.",
            )
        action, value, key = planned
        self._scheduled[state.customer_id] = action.send_at
        hour = local_hour(action.send_at, state.timezone)
        return _decision(
            state,
            ctx,
            self.version,
            f"Best fixed action for {key[0]} customers reachable by {', '.join(key[1])}: "
            f"{action.channel.value} with {action.offer_id} at {hour}:00 local.",
            action,
            expected_net=value,
        )
