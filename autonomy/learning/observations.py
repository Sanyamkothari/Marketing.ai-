"""Turn what happened into the observations each model's `update()` expects.

Three kinds of observation, one per model in `autonomy.decide.models`:

* `ResponseObservation`: did the customer buy within the horizon?
  - **Treated**: an ACT decision that was actually sent (a SEND or DELIVER event carries its id)
    and not reported undelivered (a FAIL, BOUNCE or BLOCK for it with no DELIVER: the message never
    reached the customer, so counting it would pull the treated rate towards the control rate),
    judged on `[send time, send time + horizon)`, keyed by the customer's segment and the action's
    labels `channel|offer_id|theme`. The theme comes from the content library (`content_by_id`),
    because decisions carry a content id, not a theme. A window counts once it has closed
    (`send time + horizon <= now`). Overlapping sends can each claim the same purchase, like the
    `'window'` attribution in `autonomy.proof.rewards`.
  - **Control** (`action_key=None`): customers we left alone, judged on `[made_at, made_at + horizon)`.
    By default these come **only from the holdout**, which is assigned at random. The agent's own
    "do nothing" choices are not random: it leaves alone the customers it judged least likely to
    respond, so their purchase rate is lower than the rate a contacted customer would have had
    without the message, and counting them would inflate every uplift. They can be added with
    `agent_nothing_ipw=True`, weighted by the inverse of the probability the policy had of doing
    nothing (clipped, then self-normalised so the weights sum to the number of decisions). That
    corrects for the policy's randomisation only: a decision taken with probability 1 (no bids, out
    of the audience) still counts at face value, so the estimate stays biased towards whoever the
    agent never targets. A control window with any send to the customer in it is skipped.
    In the Gym, holdout customers never reach `decide()`, so no holdout decisions exist. Pass their
    ids as `holdout_customers` and each night closes one virtual daily window per holdout customer.
* `FatigueObservation`: for each delivered send with a closed window, how many sends the customer had in the
  7 days before it, and whether an UNSUBSCRIBE or COMPLAINT followed (one carrying the decision's id,
  or one without an id before the next send and inside the window).
* `ValueObservation`: every PURCHASE event up to `now`, with its value and margin.

**Logged history.** A client hands over months of past campaigns before day 1. `logged_sends` reads
them back as `LoggedSend`s (channel, offer, theme, whether delivered and clicked, and the
`logged_probability` a randomised campaign recorded for each send), and `ipw_weights` /
`weighted_rate` turn them into rates that undo the campaigns' targeting: a send that had a 10%
chance of going out stands for ten customers like it. The agent warm-starts its click model from
them (`autonomy.runtime.agent.AgentPolicy`).

**Contact evidence.** `contact_evidence` keeps, per customer, what they did around messages: days
inside and outside the week after a delivered message and the purchases in each, clicks, stops and
direct visits, recency-weighted when asked. `autonomy.learning.update` feeds it to the contact-harm
model (`autonomy.decide.bidders.ContactHarm`), which learns who buys less after a message.

**Overlapping control windows.** Daily do-nothing decisions with a 14-day horizon put one purchase
in up to 14 control observations. That does not bias the rate, but it would make the control
posterior about 14 times too sure of itself. With `overlap_weighting=True` (the default) a control
window is weighted by the share of it that does not overlap the customer's previous control window,
so a customer observed daily for D days counts as about D / horizon independent windows.

Everything here is pure: no clock, no randomness, nothing mutated. The caller (`update.py`) passes in
what has already been learned and applies the result.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Callable, Container, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from datetime import datetime, timedelta

from autonomy.contracts import Channel, ContentItem, Decision, DecisionKind, Event, EventType, Group
from autonomy.decide.models import FatigueObservation, ResponseObservation, action_key
from autonomy.learning.bundle import ValueObservation

SegmentOf = Callable[[str], str | None] | Mapping[str, str | None]
"""How to find a customer's segment: a function or a dict. Decisions do not carry it."""

DEFAULT_TREATED_GROUPS: frozenset[Group] = frozenset({Group.AGENT, Group.BASELINE})
"""Sends from the agent and from the baseline rule both teach what a contact does."""

DEFAULT_IPW_CLIP = 20.0
"""Largest inverse-propensity weight before normalising: one lucky do-nothing cannot dominate."""

_WEEK = timedelta(days=7)
_DAY = timedelta(days=1)
_SENT_TYPES = frozenset({EventType.SEND, EventType.DELIVER})
_STOP_TYPES = frozenset({EventType.UNSUBSCRIBE, EventType.COMPLAINT})
_UNDELIVERED_TYPES = frozenset({EventType.FAIL, EventType.BOUNCE, EventType.BLOCK})
"""A message that did not reach the customer. A WhatsApp BLOCK means they had already opted out, so it
says nothing about what this message did and is not counted as an unsubscribe caused by it."""


@dataclass
class ObservationSet:
    """Observations ready for the models, plus what to remember so they are not made twice."""

    response: list[ResponseObservation] = field(default_factory=list)
    fatigue: list[FatigueObservation] = field(default_factory=list)
    value: list[ValueObservation] = field(default_factory=list)
    decisions_closed: list[str] = field(default_factory=list)
    """Decision ids now learned from (or skipped for good, e.g. never sent)."""
    purchases_seen: list[str] = field(default_factory=list)
    holdout_windows: list[str] = field(default_factory=list)
    control_starts: dict[str, datetime] = field(default_factory=dict)
    """Latest control window start per customer, for the next overlap weight."""
    holdout_anchor: datetime | None = None
    counts: dict[str, int] = field(default_factory=dict)

    def count(self, name: str, n: int = 1) -> None:
        self.counts[name] = self.counts.get(name, 0) + n

    def merge(self, other: ObservationSet) -> None:
        self.response.extend(other.response)
        self.fatigue.extend(other.fatigue)
        self.value.extend(other.value)
        self.decisions_closed.extend(other.decisions_closed)
        self.purchases_seen.extend(other.purchases_seen)
        self.holdout_windows.extend(other.holdout_windows)
        self.control_starts.update(other.control_starts)
        if other.holdout_anchor is not None:
            self.holdout_anchor = other.holdout_anchor
        for name, n in other.counts.items():
            self.count(name, n)


def segment_lookup(segment_of: SegmentOf | None) -> Callable[[str], str | None]:
    """A function from customer id to segment (None, the pooled segment, when unknown)."""
    if segment_of is None:
        return lambda _cid: None
    if callable(segment_of):
        return segment_of
    table = segment_of
    return lambda cid: table.get(cid)


def theme_of(content_id: str | None, content_by_id: Mapping[str, ContentItem]) -> str | None:
    """The theme label of a content item, or None when the item is unknown."""
    if content_id is None:
        return None
    item = content_by_id.get(content_id)
    return None if item is None else item.theme.value


def format_of(content_id: str | None, content_by_id: Mapping[str, ContentItem]) -> str | None:
    """The format of a content item (`text`, `text_image`, ...), or None when the item is unknown.

    It goes into the action key beside the theme, so an image send is learned as its own option
    (see `autonomy.decide.models.action_key`); plain text keeps the three-part key."""
    if content_id is None:
        return None
    item = content_by_id.get(content_id)
    return None if item is None else item.format.value


class Timeline:
    """One customer's events, sorted, with the lookups learning needs."""

    def __init__(self, events: Iterable[Event]) -> None:
        ordered = sorted(events, key=lambda e: (e.ts, e.event_id))
        self.first_ts: datetime | None = ordered[0].ts if ordered else None
        self.purchases: list[datetime] = [e.ts for e in ordered if e.type is EventType.PURCHASE]
        sends = [e for e in ordered if e.type is EventType.SEND]
        self.send_times: list[datetime] = [e.ts for e in sends]
        self.sent_at: dict[str, datetime] = {}
        for e in ordered:
            if e.type in _SENT_TYPES and e.decision_id is not None:
                self.sent_at.setdefault(e.decision_id, e.ts)
        self.stops: list[Event] = [e for e in ordered if e.type in _STOP_TYPES]
        # decisions reported as failed, bounced or blocked and never delivered
        delivered = {e.decision_id for e in ordered if e.type is EventType.DELIVER}
        self.undelivered: frozenset[str] = frozenset(
            e.decision_id
            for e in ordered
            if e.type in _UNDELIVERED_TYPES and e.decision_id is not None and e.decision_id not in delivered
        )

    def bought(self, start: datetime, end: datetime) -> bool:
        """Any purchase in `[start, end)`."""
        i = bisect.bisect_left(self.purchases, start)
        return i < len(self.purchases) and self.purchases[i] < end

    def sends_between(self, start: datetime, end: datetime) -> int:
        """SEND events in `[start, end)`."""
        return bisect.bisect_left(self.send_times, end) - bisect.bisect_left(self.send_times, start)

    def next_send_after(self, ts: datetime) -> datetime | None:
        i = bisect.bisect_right(self.send_times, ts)
        return self.send_times[i] if i < len(self.send_times) else None

    def stopped(self, decision_id: str, start: datetime, end: datetime) -> bool:
        """An unsubscribe or complaint caused by this send: it names the decision, or it names none
        and falls after the send, before the next send and inside the window."""
        nxt = self.next_send_after(start)
        until = end if nxt is None else min(end, nxt)
        for e in self.stops:
            if e.decision_id == decision_id:
                return True
            if e.decision_id is None and start <= e.ts < until:
                return True
        return False


def timelines(events_by_customer: Mapping[str, Sequence[Event]]) -> Callable[[str], Timeline | None]:
    """A memoised `customer_id -> Timeline`; None for a customer the mapping does not include."""
    cache: dict[str, Timeline] = {}

    def get(customer_id: str) -> Timeline | None:
        found = cache.get(customer_id)
        if found is not None:
            return found
        events = events_by_customer.get(customer_id)
        if events is None:
            return None
        made = Timeline(events)
        cache[customer_id] = made
        return made

    return get


def overlap_weight(start: datetime, previous: datetime | None, horizon: timedelta) -> float:
    """Share of a control window not covered by the customer's previous one (1 if none)."""
    if previous is None:
        return 1.0
    gap = abs((start - previous).total_seconds())
    return min(1.0, gap / horizon.total_seconds())


def decision_observations(
    decisions: Iterable[Decision],
    events_by_customer: Mapping[str, Sequence[Event]],
    content_by_id: Mapping[str, ContentItem],
    now: datetime,
    horizon_days: int,
    *,
    learned: Container[str] = frozenset(),
    last_control_start: Mapping[str, datetime] | None = None,
    segment_of: SegmentOf | None = None,
    treated_groups: frozenset[Group] = DEFAULT_TREATED_GROUPS,
    agent_nothing_ipw: bool = False,
    ipw_clip: float = DEFAULT_IPW_CLIP,
    overlap_weighting: bool = True,
    require_send: bool = True,
    timeline_of: Callable[[str], Timeline | None] | None = None,
) -> ObservationSet:
    """Treated, control and fatigue observations from decisions whose window has closed by `now`.

    Decisions in `learned`, decisions whose window is still open, and decisions of customers missing
    from `events_by_customer` (pass an empty list for a customer with no events) are left for later.
    With `require_send=False`, an ACT decision with no SEND/DELIVER event is taken as sent at its
    `send_at` (for logs without send events); it then gives no fatigue observation.
    """
    if horizon_days < 1:
        raise ValueError("horizon_days must be at least 1")
    horizon = timedelta(days=horizon_days)
    segment = segment_lookup(segment_of)
    timeline = timeline_of or timelines(events_by_customer)
    starts: dict[str, datetime] = dict(last_control_start or {})
    out = ObservationSet()
    # (decision, start, converted, raw weight) for control candidates, weighted after the pass
    holdout: list[tuple[Decision, datetime, bool]] = []
    agent: list[tuple[Decision, datetime, bool, float]] = []

    unique: dict[str, Decision] = {}
    for d in decisions:
        unique.setdefault(d.decision_id, d)
    ordered = sorted(unique.values(), key=lambda d: (d.customer_id, d.made_at, d.decision_id))

    for d in ordered:
        if d.decision_id in learned:
            out.count("already_learned")
            continue
        if d.kind is DecisionKind.NOTHING:
            usable = d.group is Group.HOLDOUT or (d.group is Group.AGENT and agent_nothing_ipw)
            if not usable:
                continue
        elif d.group not in treated_groups:
            continue
        tl = timeline(d.customer_id)
        if tl is None:
            out.count("no_events")
            continue

        if d.kind is DecisionKind.NOTHING:
            end = d.made_at + horizon
            if end > now:
                out.count("still_open")
                continue
            out.decisions_closed.append(d.decision_id)
            if tl.sends_between(d.made_at, end) > 0:
                out.count("contaminated")
                continue
            converted = tl.bought(d.made_at, end)
            if d.group is Group.HOLDOUT:
                holdout.append((d, d.made_at, converted))
            else:
                agent.append((d, d.made_at, converted, min(1.0 / d.probability, ipw_clip)))
            continue

        action = d.action
        assert action is not None  # an ACT decision always has one (contract validator)
        sent = tl.sent_at.get(d.decision_id)
        start = sent if sent is not None else action.send_at
        end = start + horizon
        if end > now:
            out.count("still_open")
            continue
        out.decisions_closed.append(d.decision_id)
        if sent is None and require_send:
            out.count("not_sent")
            continue
        if d.decision_id in tl.undelivered:
            out.count("not_delivered")
            continue
        theme = theme_of(action.content_id, content_by_id)
        if theme is None:
            out.count("unknown_content")
        else:
            out.response.append(
                ResponseObservation(
                    segment=segment(d.customer_id),
                    action_key=action_key(
                        action.channel, action.offer_id, theme, format_of(action.content_id, content_by_id)
                    ),
                    converted=tl.bought(start, end),
                )
            )
            out.count("treated")
        if sent is not None:
            out.fatigue.append(
                FatigueObservation(
                    contacts_7d=tl.sends_between(sent - _WEEK, sent),
                    unsubscribed=tl.stopped(d.decision_id, sent, end),
                )
            )
            out.count("fatigue")

    for d, start, converted in holdout:
        w = overlap_weight(start, starts.get(d.customer_id), horizon) if overlap_weighting else 1.0
        starts[d.customer_id] = max(start, starts.get(d.customer_id, start))
        out.control_starts[d.customer_id] = starts[d.customer_id]
        out.response.append(ResponseObservation(segment(d.customer_id), None, converted, w))
        out.count("control_holdout")

    if agent:
        total = sum(raw for *_, raw in agent)
        scale = len(agent) / total if total > 0 else 0.0
        for d, start, converted, raw in agent:
            w = raw * scale
            if overlap_weighting:
                w *= overlap_weight(start, starts.get(d.customer_id), horizon)
            starts[d.customer_id] = max(start, starts.get(d.customer_id, start))
            out.control_starts[d.customer_id] = starts[d.customer_id]
            out.response.append(ResponseObservation(segment(d.customer_id), None, converted, w))
            out.count("control_agent_ipw")
    return out


def holdout_anchors(now: datetime, horizon_days: int, last_anchor: datetime | None) -> list[datetime]:
    """Starts of the virtual holdout windows that close by `now`: one a day after `last_anchor`, or
    just `now - horizon` the first time (nothing is backfilled before learning starts)."""
    latest = now - timedelta(days=horizon_days)
    if last_anchor is None:
        return [latest]
    out: list[datetime] = []
    t = last_anchor + _DAY
    while t <= latest:
        out.append(t)
        t += _DAY
    return out


def holdout_observations(
    holdout_customers: Iterable[str],
    events_by_customer: Mapping[str, Sequence[Event]],
    now: datetime,
    horizon_days: int,
    *,
    learned: Container[str] = frozenset(),
    last_anchor: datetime | None = None,
    last_control_start: Mapping[str, datetime] | None = None,
    skip_customers: Container[str] = frozenset(),
    segment_of: SegmentOf | None = None,
    overlap_weighting: bool = True,
    timeline_of: Callable[[str], Timeline | None] | None = None,
) -> ObservationSet:
    """Control observations for holdout customers who have no logged decisions (the Gym).

    Each night closes the windows `[anchor, anchor + horizon)` for the anchors `holdout_anchors`
    gives. A customer counts only from the first anchor after their first event (so a customer who
    did not exist yet is not counted as "did not buy"), and a window with a send in it is skipped.
    `skip_customers` are holdout customers whose do-nothing decisions are logged and so already
    counted by `decision_observations`.
    """
    if horizon_days < 1:
        raise ValueError("horizon_days must be at least 1")
    horizon = timedelta(days=horizon_days)
    segment = segment_lookup(segment_of)
    timeline = timeline_of or timelines(events_by_customer)
    starts: dict[str, datetime] = dict(last_control_start or {})
    anchors = holdout_anchors(now, horizon_days, last_anchor)
    out = ObservationSet(holdout_anchor=anchors[-1] if anchors else last_anchor)
    for cid in sorted(set(holdout_customers)):
        if cid in skip_customers:
            continue
        tl = timeline(cid)
        if tl is None or tl.first_ts is None:
            out.count("no_events")
            continue
        for anchor in anchors:
            key = f"{cid}@{anchor.isoformat()}"
            if key in learned:
                out.count("already_learned")
                continue
            if tl.first_ts > anchor:
                continue
            out.holdout_windows.append(key)
            end = anchor + horizon
            if tl.sends_between(anchor, end) > 0:
                out.count("contaminated")
                continue
            w = overlap_weight(anchor, starts.get(cid), horizon) if overlap_weighting else 1.0
            starts[cid] = max(anchor, starts.get(cid, anchor))
            out.control_starts[cid] = starts[cid]
            out.response.append(ResponseObservation(segment(cid), None, tl.bought(anchor, end), w))
            out.count("control_holdout")
    return out


def value_observations(
    events_by_customer: Mapping[str, Sequence[Event]],
    now: datetime,
    *,
    learned: Container[str] = frozenset(),
) -> ObservationSet:
    """One `ValueObservation` per PURCHASE event at or before `now` not yet learned from."""
    out = ObservationSet()
    seen: set[str] = set()
    for cid in sorted(events_by_customer):
        for e in events_by_customer[cid]:
            if e.type is not EventType.PURCHASE or e.ts > now or e.value is None:
                continue
            if e.event_id in learned or e.event_id in seen:
                continue
            seen.add(e.event_id)
            out.purchases_seen.append(e.event_id)
            out.value.append(ValueObservation(value=e.value, margin=e.margin))
            out.count("value")
    return out


# --- logged history: past campaigns handed over with the client's data ---------------------------

LOGGED_PROBABILITY = "logged_probability"
"""The attr a logged send carries when the campaign that sent it was randomised: the chance it had of
going to this customer. It is what makes the history usable for learning without its targeting bias."""
LOGGED_CLICK_WINDOW = timedelta(days=3)
"""A click without a decision id is credited to the last send on its channel at most this long before."""
DEFAULT_LOGGED_IPW_CLIP = 20.0
"""Largest inverse-propensity weight for a logged send, so one send with a tiny propensity cannot
carry a whole action's estimate."""


@dataclass(frozen=True)
class LoggedSend:
    """One message from the client's past campaigns, as the history shows it."""

    customer_id: str
    sent_at: datetime
    channel: Channel
    offer_id: str
    theme: str | None
    """From the content library when the content id is known, else the send's `theme` attr."""
    propensity: float | None
    """`attrs["logged_probability"]` when the campaign logged it (a number in (0, 1]), else None."""
    delivered: bool
    clicked: bool
    click_at: datetime | None
    decision_id: str | None
    format: str | None = None
    """From the content library when the content id is known, else the send's `format` attr; None
    (plain text) for campaigns that never recorded one."""
    campaign_id: str | None = None
    """The send's `campaign_id` attr, when the history names its campaign."""

    def action_key(self) -> str:
        return action_key(self.channel, self.offer_id, self.theme or "*", self.format)


def _probability(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    p = float(value)
    return p if 0 < p <= 1 else None


def logged_sends(
    events_by_customer: Mapping[str, Sequence[Event]],
    content_by_id: Mapping[str, ContentItem],
    *,
    exclude: Container[str] = frozenset(),
) -> list[LoggedSend]:
    """Every past send in the history, with whether it was delivered and clicked.

    A SEND event is one message (its `decision_id`, when there is one, ties it to its DELIVER, FAIL
    and CLICK events). Without a decision id, a FAIL or BOUNCE on the same channel within five minutes
    marks it undelivered and the first CLICK on the same channel within `LOGGED_CLICK_WINDOW`, before
    the next send on that channel, is its click. Sends whose decision id is in `exclude` (the agent's
    own) are left out. Sorted by customer, then send time, so callers repeat exactly.
    """
    out: list[LoggedSend] = []
    for cid in sorted(events_by_customer):
        ordered = sorted(events_by_customer[cid], key=lambda e: (e.ts, e.event_id))
        sends = [e for e in ordered if e.type is EventType.SEND and e.channel is not None]
        if not sends:
            continue
        failed: set[str] = set()
        delivered_ids: set[str] = set()
        clicks: dict[str, datetime] = {}
        loose_fail: list[Event] = []
        loose_clicks: list[Event] = []
        for e in ordered:
            if e.type is EventType.DELIVER and e.decision_id is not None:
                delivered_ids.add(e.decision_id)
            elif e.type in _UNDELIVERED_TYPES:
                if e.decision_id is not None:
                    failed.add(e.decision_id)
                else:
                    loose_fail.append(e)
            elif e.type is EventType.CLICK:
                if e.decision_id is not None:
                    clicks.setdefault(e.decision_id, e.ts)
                else:
                    loose_clicks.append(e)
        used: set[str] = set()
        for k, s in enumerate(sends):
            if s.decision_id is not None and s.decision_id in exclude:
                continue
            channel = s.channel
            assert channel is not None  # filtered above
            offer = s.attrs.get("offer_id")
            content = s.attrs.get("content_id")
            theme = theme_of(content if isinstance(content, str) else None, content_by_id)
            if theme is None:
                raw = s.attrs.get("theme")
                theme = raw if isinstance(raw, str) else None
            fmt = format_of(content if isinstance(content, str) else None, content_by_id)
            if fmt is None:
                raw_fmt = s.attrs.get("format")
                fmt = raw_fmt if isinstance(raw_fmt, str) else None
            did = s.decision_id
            if did is not None:
                delivered = did in delivered_ids or did not in failed
                click_at = clicks.get(did)
            else:
                delivered = not any(
                    f.channel is channel and timedelta(0) <= f.ts - s.ts <= timedelta(minutes=5)
                    for f in loose_fail
                )
                nxt = next((t.ts for t in sends[k + 1 :] if t.channel is channel), None)
                until = s.ts + LOGGED_CLICK_WINDOW if nxt is None else min(nxt, s.ts + LOGGED_CLICK_WINDOW)
                click_at = None
                for c in loose_clicks:
                    if c.event_id not in used and c.channel in (channel, None) and s.ts <= c.ts < until:
                        used.add(c.event_id)
                        click_at = c.ts
                        break
            out.append(
                LoggedSend(
                    customer_id=cid,
                    sent_at=s.ts,
                    channel=channel,
                    offer_id=offer if isinstance(offer, str) else "none",
                    theme=theme,
                    propensity=_probability(s.attrs.get(LOGGED_PROBABILITY)),
                    delivered=delivered,
                    clicked=delivered and click_at is not None,
                    click_at=click_at if delivered else None,
                    decision_id=did,
                    format=fmt,
                    campaign_id=campaign if isinstance(campaign := s.attrs.get("campaign_id"), str) else None,
                )
            )
    return out


def ipw_weights(
    propensities: Sequence[float | None], *, clip: float = DEFAULT_LOGGED_IPW_CLIP
) -> list[float]:
    """Inverse-propensity weights 1 / p, clipped at `clip`. A send without a logged propensity gets
    weight 1: we cannot correct its targeting, so it counts as if it had been sent to everyone."""
    return [1.0 if p is None else min(1.0 / p, clip) for p in propensities]


def weighted_rate(outcomes: Sequence[bool], weights: Sequence[float]) -> tuple[float, float]:
    """(self-normalised weighted rate, effective sample size) of yes/no outcomes.

    The rate is the Hajek estimator sum(w y) / sum(w); the effective size (sum w)^2 / sum(w^2) is how
    many unweighted observations it is worth, so a prior built from it is not overconfident."""
    sw = sum(weights)
    if sw <= 0:
        return 0.0, 0.0
    rate = sum(w for y, w in zip(outcomes, weights, strict=True) if y) / sw
    return rate, sw * sw / sum(w * w for w in weights)


# --- contact evidence: what each customer did around messages, for the contact-harm model ---------

DIRECT_VISIT_GAP = timedelta(days=1)
"""A visit within a day of the customer's last click came through the message; any other visit is
"direct" (they came on their own)."""
_EVENT_ORDER: Mapping[EventType, int] = {EventType.PURCHASE: 0}
"""At one timestamp a purchase is taken to come before a send: a message cannot cause a purchase
made at the moment it went out."""


@dataclass
class ContactEvidence:
    """One customer's record around messages, summed from their events up to `until`.

    Time is split into **exposed** days (inside `[send, send + horizon)` of a delivered message) and
    **open** days (no message working on them), with the purchases made in each. A customer whose
    purchases fall off after messages, against their own buying on open days, is someone contact
    harms. Offer windows are the exposed days in which an offer from a message was open.

    The clues a real CRM holds sit beside it: delivered messages and clicks (someone who never clicks
    after many messages), unsubscribes and complaints on any channel, visits and the "direct" ones
    (not within `DIRECT_VISIT_GAP` of a click), and purchases within `LOGGED_CLICK_WINDOW` of a click
    (message-attributed buying).

    It is an accumulator: `contact_evidence` adds the events in `[until, now)` and returns a new one,
    so a nightly caller that keeps only recent events still counts each moment once. Events that
    arrive with a timestamp before `until` are not counted.

    **Before and after the agent started** (`split_at`). Once the agent runs, its customers are
    messaged most weeks, so most of their exposed days fall after the start and most of their open
    days before it. A season that lowers everyone's buying since the start would then read as
    messages costing them buying. The holdout (people we never contact) shows how buying moved on
    its own; the `post_*` counts, the part of the record from `split_at` on, let the harm model
    count those days at that level. The exposed, open and total counts above still cover the whole
    record.
    """

    since: datetime | None = None
    """The first moment counted (the customer's first event)."""
    until: datetime | None = None
    """Everything before this has been counted."""
    exposed_until: datetime | None = None
    offer_until: datetime | None = None
    last_click_at: datetime | None = None
    exposed_days: float = 0.0
    exposed_purchases: float = 0.0
    offer_days: float = 0.0
    """The part of `exposed_days` with an offer open (from a message carrying one)."""
    offer_purchases: float = 0.0
    open_days: float = 0.0
    open_purchases: float = 0.0
    sends: float = 0.0
    """Messages that reached the customer (SEND events not reported failed, bounced or blocked)."""
    offer_sends: float = 0.0
    clicks: float = 0.0
    stops: float = 0.0
    """Unsubscribes and complaints, on any channel."""
    visits: float = 0.0
    direct_visits: float = 0.0
    message_purchases: float = 0.0
    """Purchases within `LOGGED_CLICK_WINDOW` of a click: buying a message can be credited with."""
    sends_since_click: int = 0
    """Messages delivered since the customer's last click (all of them if they never clicked), not
    weighted by recency: a long run of messages with no answer, whether they never engaged or have
    stopped engaging since they were contacted more."""
    split_at: datetime | None = None
    """Where the `post_*` counts begin (the agent's first decision); None: nothing split off yet."""
    post_days: float = 0.0
    post_exposed_days: float = 0.0
    post_purchases: float = 0.0
    post_exposed_purchases: float = 0.0

    @property
    def days(self) -> float:
        """Days observed (`since` to `until`)."""
        return self.exposed_days + self.open_days

    @property
    def purchases(self) -> float:
        return self.exposed_purchases + self.open_purchases


def _offer_of(event: Event, content_by_id: Mapping[str, ContentItem]) -> str:
    offer = event.attrs.get("offer_id")
    if isinstance(offer, str):
        return offer
    content = event.attrs.get("content_id")
    item = content_by_id.get(content) if isinstance(content, str) else None
    return item.offer_id if item is not None else "none"


def contact_evidence(
    events: Sequence[Event],
    now: datetime,
    horizon_days: int,
    previous: ContactEvidence | None = None,
    *,
    content_by_id: Mapping[str, ContentItem] | None = None,
    no_offer: str = "none",
    data_start: datetime | None = None,
    half_life_days: float | None = None,
    split_at: datetime | None = None,
) -> ContactEvidence:
    """`previous` plus the events in `[previous.until, now)` (all events before `now` the first time).

    The first time, counting starts at the customer's first event or `data_start`, whichever is
    later: an import of six months of orders for customers who signed up years ago would otherwise
    count years of "open days without a purchase" and make every message look like it sells. When
    the customer's record opens with a purchase (their first order is why they are on file), that
    purchase is not counted: it is the selection, not their buying.

    With `half_life_days`, every count and every day is weighted by how recent it is (a record that
    old counts half): people change, and someone who has stopped answering messages over the last
    quarter is better described by that quarter than by the year before. The weights are those at
    `now`; what was counted before is decayed to `now` first, so the result does not depend on how
    often it is called.

    `events` may be the customer's whole log or only what is still in memory, as long as it holds
    every event since `previous.until`. A send's window is `horizon_days` long, as in learning. A
    SEND whose decision is reported FAIL, BOUNCE or BLOCK (and never DELIVER) did not reach the
    customer and exposes nothing. Pure: `previous` is not changed.

    `split_at` (the agent's first decision) starts the `post_*` counts. It is kept on the record the
    first time it is given, and only moments from then on, not yet counted, are split off: a record
    counted before the agent started keeps its history as history.
    """
    if horizon_days < 1:
        raise ValueError("horizon_days must be at least 1")
    if half_life_days is not None and half_life_days <= 0:
        raise ValueError("half_life_days must be positive")
    horizon = timedelta(days=horizon_days)
    library = content_by_id or {}
    acc = ContactEvidence() if previous is None else replace(previous)
    if acc.split_at is None and split_at is not None:
        acc.split_at = split_at
    split = acc.split_at
    start = acc.until
    batch = sorted(
        (e for e in events if e.ts < now and (start is None or e.ts >= start)),
        key=lambda e: (e.ts, _EVENT_ORDER.get(e.type, 1), e.event_id),
    )
    if start is None:
        if not batch:
            return acc
        first = batch[0].ts
        start = first if data_start is None else max(first, data_start)
        batch = [e for e in batch if e.ts >= start]
        if start == first and any(e.type is EventType.PURCHASE and e.ts == start for e in batch):
            # A record that opens with a purchase (an order import) is on file *because* of it:
            # counting it would add one open-day purchase to every such customer and make
            # messages look as if they put everyone off. Counting starts just after it.
            batch = [e for e in batch if not (e.type is EventType.PURCHASE and e.ts == start)]
        acc.since = start
    if now <= start:
        return acc
    rate = math.log(2.0) / half_life_days if half_life_days is not None else 0.0

    def weight(ts: datetime) -> float:
        """An event's weight at `now`."""
        return math.exp(-rate * max((now - ts).total_seconds(), 0.0) / 86_400.0)

    def days(a: datetime, b: datetime) -> float:
        """Weighted days in `[a, b)`."""
        span = max((b - a).total_seconds(), 0.0) / 86_400.0
        if span <= 0:
            return 0.0
        if rate == 0:
            return span
        return (weight(b) - weight(a)) / rate

    if previous is not None and rate > 0:
        old = weight(start)
        for f in fields(acc):
            if f.type in ("float", float):
                setattr(acc, f.name, getattr(acc, f.name) * old)
    delivered = {e.decision_id for e in batch if e.type is EventType.DELIVER and e.decision_id is not None}
    failed = {
        e.decision_id
        for e in batch
        if e.type in _UNDELIVERED_TYPES and e.decision_id is not None and e.decision_id not in delivered
    }
    clicked: set[str] = set()
    t = start

    def advance(to: datetime) -> None:
        total = days(t, to)
        exposed = (
            days(t, min(to, acc.exposed_until))
            if acc.exposed_until is not None and acc.exposed_until > t
            else 0.0
        )
        offer = (
            days(t, min(to, acc.offer_until)) if acc.offer_until is not None and acc.offer_until > t else 0.0
        )
        acc.exposed_days += exposed
        acc.open_days += max(total - exposed, 0.0)
        acc.offer_days += min(offer, exposed)
        if split is not None and to > split:
            # the part of [t, to) from the agent's start on
            a = max(t, split)
            acc.post_days += days(a, to)
            if acc.exposed_until is not None and acc.exposed_until > a:
                acc.post_exposed_days += days(a, min(to, acc.exposed_until))

    for e in batch:
        advance(e.ts)
        t = e.ts
        w = weight(e.ts)
        if e.type is EventType.SEND:
            if e.decision_id is not None and e.decision_id in failed:
                continue
            acc.sends += w
            acc.sends_since_click += 1
            end = e.ts + horizon
            acc.exposed_until = end if acc.exposed_until is None else max(acc.exposed_until, end)
            if _offer_of(e, library) != no_offer:
                acc.offer_sends += w
                acc.offer_until = end if acc.offer_until is None else max(acc.offer_until, end)
        elif e.type is EventType.PURCHASE:
            exposed_now = acc.exposed_until is not None and e.ts < acc.exposed_until
            if exposed_now:
                acc.exposed_purchases += w
                if acc.offer_until is not None and e.ts < acc.offer_until:
                    acc.offer_purchases += w
            else:
                acc.open_purchases += w
            if split is not None and e.ts >= split:
                acc.post_purchases += w
                if exposed_now:
                    acc.post_exposed_purchases += w
            if acc.last_click_at is not None and e.ts - acc.last_click_at <= LOGGED_CLICK_WINDOW:
                acc.message_purchases += w
        elif e.type is EventType.CLICK:
            if e.decision_id is not None:
                if e.decision_id in clicked:
                    continue
                clicked.add(e.decision_id)
            acc.clicks += w
            acc.last_click_at = e.ts
            acc.sends_since_click = 0
        elif e.type in _STOP_TYPES:
            acc.stops += w
        elif e.type is EventType.VISIT:
            acc.visits += w
            if acc.last_click_at is None or e.ts - acc.last_click_at > DIRECT_VISIT_GAP:
                acc.direct_visits += w
    advance(now)
    acc.until = now
    return acc


def contact_evidence_all(
    events_by_customer: Mapping[str, Sequence[Event]],
    now: datetime,
    horizon_days: int,
    previous: Mapping[str, ContactEvidence] | None = None,
    *,
    content_by_id: Mapping[str, ContentItem] | None = None,
    data_start: datetime | None = None,
    half_life_days: float | None = None,
    split_at: datetime | None = None,
) -> dict[str, ContactEvidence]:
    """`contact_evidence` for every customer in `events_by_customer` (sorted, so runs repeat).

    Only the customers given are returned; the caller merges them into what it keeps. `data_start`
    is where the business's records begin and `split_at` where the agent started (see
    `contact_evidence`)."""
    known = previous or {}
    out: dict[str, ContactEvidence] = {}
    for cid in sorted(events_by_customer):
        found = contact_evidence(
            events_by_customer[cid],
            now,
            horizon_days,
            known.get(cid),
            content_by_id=content_by_id,
            data_start=data_start,
            half_life_days=half_life_days,
            split_at=split_at,
        )
        if found.until is not None:
            out[cid] = found
    return out
