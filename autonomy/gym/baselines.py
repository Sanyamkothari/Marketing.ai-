"""The policies every agent is measured against: send-to-all, a sensible marketer's rule, and the oracle.

`ContactEveryone` and `RuleBaseline` see only what a real policy sees (customer state and the
decision context). `OraclePolicy` reads the Gym's planted truth and so marks the ceiling: the share
of its value a policy captures is the headline number on the scoreboard.

All three keep the hard rules a real deployment enforces (consent, the weekly cap, quiet hours), so
a gap between them and the agent is about judgement, not about rule-breaking.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Protocol, runtime_checkable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import numpy as np
import numpy.typing as npt

from autonomy.contracts import (
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
