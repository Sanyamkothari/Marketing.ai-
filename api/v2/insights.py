"""What the marketer app needs to make PITCH.md's promises visible, counted from the stores.

Every number here is counted or summed from the decision log, the event log, customer state, the
workspace and the batch store; nothing is estimated that the system did not already estimate. The
plain sentences (`words`) are built by code from those counts, so a screen never has to word a
reason code itself, and never meets a bare code.

The models are new response bodies. The existing endpoints keep their contract shapes exactly
(their tests validate them with `extra="forbid"`), so each addition is its own endpoint: a day's
summary (`/v2/today`), the feed with delivery states (`/v2/activity`), a customer's insight bundle,
a plan's facts (audience size, hard rules, the editor's checks), the Shadow forecast, the stop
rules with their baselines, the data and channel status, sized opportunities, and a goal's text
read for its budget and period.

`Services` (in `api.v2.deps`) calls these functions; they take it as their first argument.
"""

from __future__ import annotations

import datetime as dt
import math
import re
import statistics
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime, time, timedelta, tzinfo
from itertools import pairwise
from typing import TYPE_CHECKING, Any, Literal
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, Field

from autonomy.contracts import (
    Alert,
    AudienceFilter,
    AutonomyLevel,
    Batch,
    BatchStatus,
    Brief,
    Channel,
    ContentItem,
    ContentStatus,
    Contract,
    CustomerState,
    Decision,
    DecisionKind,
    Envelope,
    Estimate,
    Event,
    EventType,
    Goal,
    GoalStatus,
    Group,
    StopMetric,
    Theme,
)

if TYPE_CHECKING:
    from api.v2.deps import Services

_TICK = timedelta(microseconds=1)
"""Store scans exclude `until`; adding this makes them include it."""

# --- response bodies ------------------------------------------------------------------------------


class ReasonCount(Contract):
    """One reason customers were left alone, with how many and the words for it."""

    code: str = Field(description="e.g. weekly_cap, holdout, not_worth_it (see LEFT_ALONE_WORDS)")
    words: str = Field(description="The reason in plain English, for a business owner")
    n: int = Field(ge=0)


class ReasonRef(Contract):
    code: str
    words: str


class Today(Contract):
    """One day's work for one goal (or the whole programme), counted over every decision that day."""

    goal_id: str | None = Field(description="None: every goal together")
    date: dt.date = Field(description="The day counted, in the business's time zone")
    as_of: AwareDatetime
    autonomy: AutonomyLevel | None = None
    sends_nothing: bool = Field(description="True in Shadow: the decisions were logged, nothing was sent")
    autonomy_since: AwareDatetime | None = Field(
        default=None, description="When the plan was last approved at its current level"
    )
    customers_considered: int = Field(ge=0, description="Customers the agent made a decision for that day")
    contacted: int = Field(ge=0, description="Decisions to contact someone (in Shadow: would have)")
    contacted_by_group: dict[str, int] = Field(
        default_factory=dict, description="agent / baseline (the simple rule) -> contacts"
    )
    left_alone: int = Field(ge=0)
    left_alone_by_reason: dict[str, int] = Field(default_factory=dict, description="code -> customers")
    reasons: list[ReasonCount] = Field(default_factory=list, description="The same, worded, most first")
    messages_sent: int = Field(ge=0, description="SEND events that day for this goal")
    delivered: int = Field(ge=0)
    purchases: int = Field(ge=0, description="Every purchase that day by the customers considered")
    revenue: float = Field(description="What those purchases were worth (all of it, not only the extra)")
    currency: str = "USD"
    spend_to_date: float | None = Field(
        default=None,
        description="Spent on this goal so far, as the runtime's budget ledger counts it (the cost of the messages sent; offer costs are counted on /v2/proof)",
    )
    budget: float | None = None
    budget_left: float | None = None
    last_run_at: AwareDatetime | None = Field(default=None, description="The latest decision for this goal")
    next_run_at: AwareDatetime | None = Field(
        default=None, description="The next daily run the runtime's schedule has; None without one"
    )
    run_overdue: bool = Field(default=False, description="True: that run is due and has not happened yet")
    learned: list[str] = Field(default_factory=list, description="Learning sections of the latest briefing")
    notes: list[str] = Field(default_factory=list)


class Delivery(Contract):
    """What really happened to an ACT decision."""

    state: Literal[
        "shadow", "held", "approved", "rejected", "queued", "sent", "delivered", "failed", "not_sent"
    ]
    words: str
    batch_id: str | None = None
    at: AwareDatetime | None = Field(default=None, description="When it was, or is planned to be, sent")


class ItemRef(Contract):
    item_id: str
    name: str | None = Field(default=None, description="From the product catalogue or the shop's events")


class ContentRef(Contract):
    content_id: str
    channel: Channel
    theme: Theme
    subject: str | None = None
    template_name: str | None = None
    asset_url: str | None = None


class ActivityRow(Contract):
    """One decision for the Live feed, with what happened to it and who it was for."""

    decision: Decision
    goal_id: str | None = Field(description="The goal it was made for (bid-less decisions included)")
    display_name: str | None = Field(default=None, description="First name only, when the data has one")
    contacts_7d: int | None = Field(default=None, description="Messages this customer got in the last 7 days")
    delivery: Delivery | None = Field(default=None, description="ACT decisions only")
    left_alone: ReasonRef | None = Field(default=None, description="NOTHING decisions only")
    item: ItemRef | None = None
    content: ContentRef | None = None


class Refill(Contract):
    item_id: str
    item_name: str | None = None
    days_until_due: float = Field(description="Negative: overdue by that many days")
    window_open: bool
    basis: str = Field(description="How it was worked out, in words")


class NextItem(Contract):
    item_id: str
    name: str | None = None
    reason: str
    decision_id: str | None = None


class CustomerInsight(Contract):
    """What the customer panel says about a person, in words, from their state and events."""

    customer_id: str
    as_of: AwareDatetime
    display_name: str | None = None
    timezone: str
    best_hour: int | None = Field(default=None, ge=0, le=23, description="Local hour they engage most")
    best_hour_words: str | None = None
    reads_counted: int = Field(ge=0, description="Engagements best_hour is counted from")
    offer_habit: Literal["full_price", "sometimes", "needs_offer", "unknown"]
    offer_habit_words: str
    purchases: int = Field(ge=0)
    purchases_with_offer: int = Field(ge=0)
    contacts_7d: int = Field(ge=0)
    refill: Refill | None = None
    next_items: list[NextItem] = Field(default_factory=list)
    items: dict[str, str | None] = Field(default_factory=dict, description="item_id -> name")
    deliveries: dict[str, Delivery] = Field(default_factory=dict, description="decision_id -> delivery")
    left_alone: dict[str, ReasonRef] = Field(default_factory=dict, description="decision_id -> reason")


class HardRule(Contract):
    code: str = Field(description="A rule code of autonomy/decide/rules.py")
    words: str
    value: str | float | int | None = None


EditorCheckName = Literal["brand_voice", "honesty", "no_invented_numbers", "unsubscribe", "channel_rules"]


class EditorCheck(Contract):
    check: EditorCheckName
    passed: bool | None = Field(description="None: not checked")
    note: str


class ContentReview(Contract):
    content_id: str
    status: ContentStatus
    reviewed_by: Literal["critic", "person"] | None = Field(
        description="Who last decided: the AI editor, a person, or nobody yet"
    )
    passed: bool | None = Field(description="Did it pass the editor? None: the editor has not checked it")
    checks: list[EditorCheck] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list, description="The editor's raw notes")


class PlanFacts(Contract):
    """What the Plan screen needs beyond the brief: how many people, the hard rules, the editor."""

    goal_id: str
    as_of: AwareDatetime
    audience_n: int = Field(ge=0, description="Customers who match the plan's audience today")
    agent_n: int = Field(ge=0)
    baseline_n: int = Field(ge=0, description="Of them, decided by the simple rule (for comparison)")
    holdout_n: int = Field(ge=0, description="Of them, people we never contact")
    audience_words: list[str] = Field(default_factory=list, description="Each filter in words")
    hard_rules: list[HardRule] = Field(default_factory=list)
    content: list[ContentReview] = Field(default_factory=list)
    editor_passed: int = Field(ge=0)
    editor_failed: int = Field(ge=0)
    not_reviewed: int = Field(ge=0)
    rejected_by_person: int = Field(ge=0)
    autonomy: AutonomyLevel
    autonomy_since: AwareDatetime | None = None


class Expected(Contract):
    """The agent's own forecast, summed from its decisions. A forecast, not proof."""

    goal_id: str
    as_of: AwareDatetime
    label: Literal["forecast"] = "forecast"
    since: AwareDatetime | None = Field(description="Decisions from here on are summed")
    contacts: int = Field(ge=0, description="Decisions to contact someone (in Shadow: would have)")
    with_forecast: int = Field(ge=0, description="Of them, decisions that carry a forecast (a chosen bid)")
    customers: int = Field(ge=0)
    expected_net: Estimate | None = Field(
        description="Sum of expected extra profit after costs, 95% range; None without any forecast"
    )
    expected_spend: float = Field(ge=0)
    horizon_days: int = Field(description="Each forecast is for this many days after the contact")
    currency: str = "USD"
    words: str


class StopRuleStatus(Contract):
    metric: StopMetric
    threshold: float
    action: str
    words: str
    current: float | None = Field(default=None, description="The last 7 days, every goal together")
    baseline: float | None = Field(default=None, description="The usual rate (unsubscribe_multiple only)")
    pause_at: float | None = Field(default=None, description="The rate that pauses, when known")
    judged: bool = Field(description="False: a number it needs is missing, so this rule is not checked")


class HealthLimits(Contract):
    goal_id: str | None
    as_of: AwareDatetime
    unsubscribe_baseline: float | None = Field(
        description="The usual unsubscribe rate the 3x rule compares with; None: not known yet"
    )
    rules: list[StopRuleStatus]


class DataSource(Contract):
    name: str
    kind: Literal["file", "shop", "database", "simulation", "other"]
    customers: int = Field(ge=0)
    orders: int = Field(ge=0)
    events: int = Field(ge=0)
    updated_at: AwareDatetime | None = None


class DataFix(Contract):
    what: str
    rows: int = Field(ge=0)


class DataQuestion(Contract):
    question_id: str
    text: str
    rows_affected: int = Field(ge=0)


class DataStatus(Contract):
    as_of: AwareDatetime
    customers: int = Field(ge=0)
    orders: int = Field(ge=0)
    products: int | None = Field(default=None, description="Distinct items in the order data; None: none")
    sources: list[DataSource] = Field(default_factory=list)
    fixes: list[DataFix] = Field(default_factory=list)
    problems: list[str] = Field(default_factory=list)
    questions: list[DataQuestion] = Field(default_factory=list)
    doctor_connected: bool = Field(description="False: the Data Doctor's report is not available here")


class ChannelStatus(Contract):
    channel: Channel
    mode: Literal["direct", "handoff"]
    ready: bool
    paused: bool
    paused_reason: str | None = None
    words: str


class SizedOpportunity(Contract):
    goal: Goal
    audience_n: int = Field(ge=0)
    reason: str


class GoalReading(Contract):
    """What the server reads from a goal's words: an amount and a period, when it names them."""

    text: str
    budget: float | None = None
    currency: str | None = None
    ends_at: AwareDatetime | None = None
    period: str | None = Field(default=None, description="The words it read the period from")
    words: str


class BatchBreakdown(Contract):
    batch_id: str
    by_channel: dict[str, int]
    by_offer: dict[str, int]
    offer_labels: dict[str, str] = Field(default_factory=dict)
    words: str


# --- shared helpers -------------------------------------------------------------------------------


def inner_runtime(services: Services) -> Any:
    """The `AutonomyRuntime` behind the API's recording wrapper, or None."""
    runtime = services.runtime
    return getattr(runtime, "inner", runtime)


def business_tz(services: Services) -> tzinfo:
    config = getattr(inner_runtime(services), "config", None)
    name = getattr(config, "timezone", "UTC")
    try:
        return ZoneInfo(name)
    except (KeyError, ValueError):
        return UTC


def latest_brief(services: Services, goal_id: str) -> Brief | None:
    briefs = services.workspace.briefs(goal_id)
    return max(briefs, key=lambda b: b.version) if briefs else None


def autonomy_since(services: Services, goal: Goal) -> datetime | None:
    """When the plan was approved at the current level (approving is how the level is set)."""
    if goal.status is GoalStatus.DRAFT:
        return None
    brief = latest_brief(services, goal.goal_id)
    return brief.approved_at if brief is not None else None


def goal_resolver(services: Services) -> Callable[[Decision], str | None]:
    """decision -> the goal it was made for: its action's, its best bid's, else its derived id's.

    The runtime derives a decision's id from its goal, so a bid-less "nothing" decision (every
    holdout customer, everyone the weekly cap stopped) can still be counted for its goal. With a
    single running goal, a decision nothing else names is that goal's.
    """
    goals = services.workspace.goals()
    ids = [g.goal_id for g in goals]
    live = [g.goal_id for g in goals if g.status is not GoalStatus.DRAFT]
    tz = business_tz(services)
    derive: Callable[..., str | None] | None
    try:
        from autonomy.runtime.service import _goal_of as derive
    except ImportError:  # the runtime is not installed: actions and bids only
        derive = None

    def resolve(d: Decision) -> str | None:
        gid: str | None = None
        if d.action is not None:
            gid = d.action.goal_id
        elif d.bids:
            gid = d.bids[0].goal_id
        elif derive is not None:
            try:
                gid = derive(d, ids, tz)
            except Exception:  # an id scheme this runtime does not know
                gid = None
        if gid is None and len(live) == 1:
            gid = live[0]
        return gid

    return resolve


def _codes(decision: Decision) -> set[str]:
    """The rule codes on a decision, with a channel suffix ('no_consent:whatsapp') dropped."""
    aliases = {"max_contacts_per_week": "weekly_cap", "preference": "no_discounts"}
    out: set[str] = set()
    for raw in decision.blocked_by:
        code, _, rest = raw.partition(":")
        if code == "preference" and rest != "no_discounts":
            continue
        out.add(aliases.get(code, code))
    return out


LEFT_ALONE_WORDS: dict[str, str] = {
    "holdout": "People we never contact, so we can measure the difference",
    "simple_rule": "In the comparison group, where a simple rule decides, and the rule left them alone",
    "not_worth_it": "Better off without a message: no message was worth the cost of bothering them",
    "no_effect": "A message would not change what they do",
    "testing_rest": "Left alone on purpose, to keep measuring what happens with no message",
    "nothing_to_offer": "No message in the plan fits them right now",
    "weekly_cap": "Already contacted as often as the weekly limit allows",
    "too_soon": "Their last message went out only hours ago",
    "open_complaint": "Has an open complaint",
    "snoozed": "Asked us to pause messages for now",
    "no_budget": "The budget is used up",
    "pacing": "Slowing down on a channel to stay under the complaint and unsubscribe limits",
    "frequency_plan": "Their 3-week plan waits: a message later is worth more than one today",
    "quiet_hours": "Every possible send time fell in quiet hours",
    "offer_needs_approval": "The only offers worth sending need your approval first",
    "no_discounts": "Asked for no discounts, and every option had one",
    "offer_not_allowed": "The only offers worth sending are not in your plan",
    "no_brief": "No approved plan covers them",
    "channel_paused": "The channel that reaches them is paused",
    "unsubscribed": "Unsubscribed",
    "no_consent": "Has not agreed to receive messages",
    "no_address": "We have no email address or phone number for them",
    "channel_not_allowed": "Your plan does not allow a channel that can reach them",
    "other": "Other rules",
}
"""Every reason code `left_alone_reason` can give, in words. `would_buy_anyway` is not one: telling
it apart from `no_effect` needs each customer's chance of buying without a message, which decide()
does not log yet."""

_BLOCK_ORDER: tuple[str, ...] = (
    "open_complaint",
    "snoozed",
    "weekly_cap",
    "too_soon",
    "no_budget",
    "pacing",
    "frequency_plan",
    "channel_paused",
    "quiet_hours",
    "offer_needs_approval",
    "no_discounts",
    "offer_not_allowed",
    "no_brief",
    "unsubscribed",
    "no_consent",
    "no_address",
    "channel_not_allowed",
)
"""Which rule names a block when several did, most binding first (as autonomy/decide/explain.py)."""


def left_alone_reason(decision: Decision) -> ReasonRef:
    """Why a NOTHING decision left the customer alone, as one code and its words."""
    codes = _codes(decision)
    if decision.group is Group.HOLDOUT or "holdout" in codes:
        code = "holdout"
    elif decision.group is Group.BASELINE:
        code = "simple_rule"
    elif decision.bids:
        best = max(decision.bids, key=lambda b: b.expected_net)
        if max(b.uplift for b in decision.bids) <= 0:
            code = "no_effect"
        elif best.expected_net - decision.attention_price <= 0:
            code = "not_worth_it"
        else:
            code = "testing_rest"
    elif codes:
        code = next((c for c in _BLOCK_ORDER if c in codes), "other")
    else:
        code = "nothing_to_offer"
    return ReasonRef(code=code, words=LEFT_ALONE_WORDS[code])


def _first_name(raw: object) -> str | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    return raw.strip().split()[0][:40]


def display_name(state: CustomerState | None, events: Iterable[Event] = ()) -> str | None:
    """A first name when the data has one (a preference, or a signup's `first_name`/`name`); never made up."""
    if state is not None:
        for key in ("first_name", "name"):
            name = _first_name(state.preferences.get(key))
            if name:
                return name
    for e in events:
        for key in ("first_name", "name"):
            name = _first_name(e.attrs.get(key))
            if name:
                return name
    return None


_ITEM_NAME_ATTRS = ("item_name", "product_name", "title", "description")


def item_name(services: Services, item_id: str, events: Iterable[Event] = ()) -> str | None:
    """The product's name from the catalogue, else from an event about it; None when nobody said."""
    name = services.catalogue.get(item_id)
    if name:
        return name
    for e in events:
        if e.item_id != item_id:
            continue
        for key in _ITEM_NAME_ATTRS:
            raw = e.attrs.get(key)
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
    return None


def _batch_index(services: Services) -> dict[str, Batch]:
    if services.batch_store is None:
        return {}
    return {i: b for b in services.batch_store.list_batches() for i in b.decision_ids}


_DELIVERY_WORDS = {
    "shadow": "Would have sent this (Shadow mode sends nothing)",
    "held": "Waiting for your approval",
    "approved": "Approved; it goes out on the next run",
    "rejected": "Not sent: the batch was turned down or ran out of time",
    "queued": "Scheduled to go out",
    "sent": "Sent",
    "delivered": "Delivered",
    "failed": "Could not be delivered",
    "not_sent": "Decided, but there is no record of it being sent",
}

_FAILED = frozenset({EventType.FAIL, EventType.BOUNCE})
_REACHED = frozenset({EventType.DELIVER, EventType.OPEN, EventType.CLICK, EventType.REPLY})


def delivery(
    decision: Decision,
    events: Sequence[Event],
    batch: Batch | None,
    goal: Goal | None,
    since: datetime | None,
    now: datetime,
) -> Delivery | None:
    """What happened to an ACT decision: from its events first, then its batch, then the goal's level."""
    if decision.action is None:
        return None
    send_at = decision.action.send_at
    types = {e.type for e in events}

    def made(state: str, at: datetime | None = send_at) -> Delivery:
        bid = batch.batch_id if batch is not None else None
        return Delivery(state=state, words=_DELIVERY_WORDS[state], batch_id=bid, at=at)  # type: ignore[arg-type]

    sent_at = min((e.ts for e in events if e.type is EventType.SEND), default=None)
    if types & _FAILED:
        return made("failed", sent_at or send_at)
    if types & _REACHED:
        return made("delivered", sent_at or send_at)
    if EventType.SEND in types:
        return made("sent", sent_at)
    if batch is not None:
        if batch.status is BatchStatus.PENDING:
            return made("held")
        if batch.status is BatchStatus.APPROVED:
            return made("approved")
        if batch.status is BatchStatus.REJECTED:
            return made("rejected")
    shadow = goal is not None and goal.autonomy is AutonomyLevel.SHADOW
    if shadow and (since is None or decision.made_at >= since) and batch is None:
        return made("shadow")
    if send_at > now:
        return made("queued")
    return made("not_sent")


# --- /v2/today ------------------------------------------------------------------------------------


def _day_bounds(day: dt.date, tz: tzinfo) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(0), tzinfo=tz).astimezone(UTC)
    return start, datetime.combine(day + timedelta(days=1), time(0), tzinfo=tz).astimezone(UTC)


def goal_spend(services: Services, goal_id: str, now: datetime) -> float | None:
    """Spent on a goal so far: the runtime's own ledger when there is one, else send costs from the log."""
    spend_fn = getattr(inner_runtime(services), "spend_fn", None)
    if callable(spend_fn):
        try:
            return round(float(spend_fn(goal_id, now)), 2)
        except Exception:  # a ledger that cannot answer: fall back to the log
            pass
    from api.v2.deps import SEND_COST

    total = 0.0
    for e in services.events.scan(until=now + _TICK):
        if e.type is not EventType.SEND or e.channel is None:
            continue
        gid = e.attrs.get("goal_id")
        if gid is None and e.decision_id is not None:
            d = services.decisions.get(e.decision_id)
            gid = d.action.goal_id if d is not None and d.action is not None else None
        if gid == goal_id:
            total += SEND_COST.get(e.channel, 0.0)
    return round(total, 2)


def _next_run(services: Services, now: datetime, last_run: datetime | None) -> datetime | None:
    """The first daily run the runtime's schedule has after the latest one (None without a runtime).

    It can be in the past: a run that is due but has not happened yet (the worker ticks shortly
    after it, and the demo only when ticked). `Today.run_overdue` says so.
    """
    runtime = inner_runtime(services)
    config = getattr(runtime, "config", None)
    hour = getattr(config, "sweep_hour", None)
    if not isinstance(hour, int):
        return None
    tz = business_tz(services)
    local = (last_run or now).astimezone(tz)
    planned = datetime.combine(local.date(), time(hour), tzinfo=tz)
    if planned <= local:
        planned = datetime.combine(local.date() + timedelta(days=1), time(hour), tzinfo=tz)
    return planned.astimezone(UTC)


def _learned(services: Services, goal_id: str | None) -> list[str]:
    """The learning sections of the latest briefing (the goal's, else the programme's)."""
    out: list[str] = []
    for gid in dict.fromkeys([goal_id, None]):
        briefing = services.briefing(gid)
        if briefing is None:
            continue
        out.extend(s.body for s in briefing.sections if "learn" in s.title.lower())
        if out:
            break
    return out


def today(services: Services, goal_id: str | None = None, day: dt.date | None = None) -> Today:
    """Count one day of decisions and events for a goal: the latest day it decided, unless `day` is given."""
    now = services.clock()
    tz = business_tz(services)
    goal = services.get_goal(goal_id) if goal_id is not None else None
    resolve = goal_resolver(services)
    mine: Callable[[Decision], bool] = (
        (lambda d: resolve(d) == goal_id) if goal_id is not None else (lambda d: True)  # noqa: ARG005
    )
    notes: list[str] = []
    last_run: datetime | None = None
    if day is None:
        latest = [d.made_at for d in services.decisions.scan(until=now + _TICK) if mine(d)]
        last_run = max(latest, default=None)
        day = (last_run or now).astimezone(tz).date()
        if last_run is None:
            notes.append("Nothing has been decided for this goal yet.")
    start, end = _day_bounds(day, tz)
    until = min(end, now + _TICK)
    rows = [d for d in services.decisions.scan(since=start, until=until) if mine(d)]
    if last_run is None and rows:
        last_run = max(d.made_at for d in rows)

    # One customer can have more than one decision in a day (a sweep, then a reply); the latest counts.
    latest_per: dict[str, Decision] = {}
    for d in sorted(rows, key=lambda d: (d.made_at, d.decision_id)):
        latest_per[d.customer_id] = d
    acted = {c for c, d in latest_per.items() if d.kind is DecisionKind.ACT and d.group is not Group.HOLDOUT}
    by_group = Counter(latest_per[c].group.value for c in acted)
    reasons: Counter[str] = Counter()
    for c, d in latest_per.items():
        if c not in acted:
            reasons[left_alone_reason(d).code] += 1

    decision_ids = {d.decision_id for d in rows}
    sent = delivered = purchases = 0
    revenue = 0.0
    for e in services.events.scan(since=start, until=until):
        if e.type is EventType.PURCHASE:
            if goal_id is None or e.customer_id in latest_per:
                purchases += 1
                revenue += e.value or 0.0
            continue
        if e.type not in (EventType.SEND, EventType.DELIVER):
            continue
        if goal_id is not None and e.attrs.get("goal_id") != goal_id:
            if e.decision_id is None:
                continue
            if e.decision_id not in decision_ids:
                earlier = services.decisions.get(e.decision_id)
                if earlier is None or resolve(earlier) != goal_id:
                    continue
        if e.type is EventType.SEND:
            sent += 1
        else:
            delivered += 1

    spend = budget = left = None
    autonomy = since = None
    currency = "USD"
    if goal is not None:
        autonomy, currency, since = goal.autonomy, goal.currency, autonomy_since(services, goal)
        brief = latest_brief(services, goal.goal_id)
        budget = min(goal.budget, brief.envelope.budget) if brief is not None else goal.budget
        spend = goal_spend(services, goal.goal_id, now)
        left = round(max(0.0, budget - spend), 2) if spend is not None else None
        if goal.autonomy is AutonomyLevel.SHADOW:
            notes.append("Shadow mode: these are the decisions it made; nothing was sent.")
    notes.append(
        "Purchases are every purchase by the customers it looked after that day, not only extra ones; "
        "the extra profit, measured against people we never contact, is on the Proof page."
    )
    ordered = sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))
    next_run = _next_run(services, now, last_run)
    return Today(
        goal_id=goal_id,
        date=day,
        as_of=now,
        autonomy=autonomy,
        sends_nothing=autonomy is AutonomyLevel.SHADOW,
        autonomy_since=since,
        customers_considered=len(latest_per),
        contacted=len(acted),
        contacted_by_group=dict(sorted(by_group.items())),
        left_alone=len(latest_per) - len(acted),
        left_alone_by_reason=dict(ordered),
        reasons=[ReasonCount(code=c, words=LEFT_ALONE_WORDS[c], n=n) for c, n in ordered],
        messages_sent=sent,
        delivered=delivered,
        purchases=purchases,
        revenue=round(revenue, 2),
        currency=currency,
        spend_to_date=spend,
        budget=budget,
        budget_left=left,
        last_run_at=last_run,
        next_run_at=next_run,
        run_overdue=next_run is not None and next_run <= now,
        learned=_learned(services, goal_id),
        notes=notes,
    )


# --- /v2/activity ---------------------------------------------------------------------------------


def _content_index(services: Services) -> dict[str, ContentItem]:
    return {c.content_id: c for c in services.workspace.content(approved_only=False)}


def _content_ref(item: ContentItem | None) -> ContentRef | None:
    if item is None:
        return None
    return ContentRef(
        content_id=item.content_id,
        channel=item.channel,
        theme=item.theme,
        subject=item.subject,
        template_name=item.template_name,
        asset_url=item.asset_url,
    )


def activity(
    services: Services, *, goal_id: str | None = None, customer_id: str | None = None, limit: int = 50
) -> list[ActivityRow]:
    """The latest decisions, newest first, each with its goal, delivery state, name and reason."""
    if goal_id is not None:
        services.get_goal(goal_id)
    now = services.clock()
    resolve = goal_resolver(services)
    rows: Sequence[Decision] = (
        services.decisions.for_customer(customer_id)
        if customer_id is not None
        else list(services.decisions.scan(until=now + _TICK))
    )
    if goal_id is not None:
        rows = [d for d in rows if resolve(d) == goal_id]
    newest = sorted(rows, key=lambda d: (d.made_at, d.decision_id), reverse=True)[: max(limit, 0)]
    goals = {g.goal_id: g for g in services.workspace.goals()}
    since = {gid: autonomy_since(services, g) for gid, g in goals.items()}
    batches = _batch_index(services)
    content = _content_index(services)
    states: dict[str, CustomerState | None] = {}
    names: dict[str, str | None] = {}
    out: list[ActivityRow] = []
    for d in newest:
        cid = d.customer_id
        if cid not in states:
            states[cid] = services.states.get(cid)
            names[cid] = display_name(states[cid], _profile_events(services, cid))
        gid = resolve(d)
        goal = goals.get(gid) if gid is not None else None
        item: ItemRef | None = None
        deliv: Delivery | None = None
        ref: ContentRef | None = None
        if d.action is not None:
            evs = services.events.for_decision(d.decision_id)
            deliv = delivery(d, evs, batches.get(d.decision_id), goal, since.get(gid or ""), now)
            ref = _content_ref(content.get(d.action.content_id or ""))
            if d.action.item_id:
                item = ItemRef(item_id=d.action.item_id, name=item_name(services, d.action.item_id))
        state = states[cid]
        out.append(
            ActivityRow(
                decision=d,
                goal_id=gid,
                display_name=names[cid],
                contacts_7d=state.contacts_7d if state is not None else None,
                delivery=deliv,
                left_alone=left_alone_reason(d) if d.kind is DecisionKind.NOTHING else None,
                item=item,
                content=ref,
            )
        )
    return out


def _profile_events(services: Services, customer_id: str) -> list[Event]:
    """The customer's own sign-up events, where a first name would be."""
    return [e for e in services.events.for_customer(customer_id) if e.type is EventType.SIGNUP]


# --- /v2/customers/{id}/insight -------------------------------------------------------------------

MIN_READS_FOR_BEST_HOUR = 3
"""Fewer engagements than this, and the panel says it does not know their hour yet."""


def _hour_words(hour: int) -> str:
    if hour == 0:
        return "midnight"
    if hour == 12:
        return "noon"
    return f"{hour % 12} {'am' if hour < 12 else 'pm'}"


def _offer_used(e: Event) -> bool:
    offer = e.attrs.get("offer_id")
    return isinstance(offer, str) and offer not in ("", "none")


def _offer_habit(purchases: int, with_offer: int) -> tuple[str, str]:
    if purchases == 0:
        return "unknown", "No purchases yet, so we do not know how they respond to offers."
    times = "once" if purchases == 1 else f"{purchases} times"
    if with_offer == 0:
        return "full_price", f"Always buys at full price (bought {times}, never with an offer)."
    share = with_offer / purchases
    used = f"{with_offer} of {purchases} purchases"
    if share >= 0.5:
        return "needs_offer", f"Usually buys with an offer ({used} used one)."
    return "sometimes", f"Sometimes buys with an offer ({used} used one)."


def _refill(services: Services, purchases: Sequence[Event], now: datetime) -> Refill | None:
    """The item they buy again and again, and when their own rhythm says they are due."""
    by_item: dict[str, list[datetime]] = {}
    for e in purchases:
        if e.item_id:
            by_item.setdefault(e.item_id, []).append(e.ts)
    repeats = {i: sorted(ts) for i, ts in by_item.items() if len(ts) >= 2}
    if not repeats:
        return None
    item_id, times = max(repeats.items(), key=lambda kv: (kv[1][-1], kv[0]))
    gaps = [(b - a).total_seconds() / 86_400 for a, b in pairwise(times)]
    gap = statistics.median(gaps)
    if gap <= 0:
        return None
    due = times[-1] + timedelta(days=gap)
    days = round((due - now).total_seconds() / 86_400, 1)
    window = max(3.0, 0.1 * gap)
    return Refill(
        item_id=item_id,
        item_name=item_name(services, item_id, purchases),
        days_until_due=days,
        window_open=days <= window,
        basis=f"Bought it {len(times)} times, about every {round(gap)} days; the last time was "
        f"{round((now - times[-1]).total_seconds() / 86_400)} days ago.",
    )


def customer_insight(services: Services, customer_id: str) -> CustomerInsight:
    from api.v2.deps import TIMELINE_LIMIT, NotFoundError

    state = services.states.get(customer_id)
    if state is None:
        raise NotFoundError(f"no customer {customer_id!r}")
    now = services.clock()
    events = services.events.for_customer(customer_id, until=now + _TICK)
    purchases = [e for e in events if e.type is EventType.PURCHASE]
    with_offer = sum(1 for e in purchases if _offer_used(e))
    habit, habit_words = _offer_habit(len(purchases), with_offer)

    counts = list(state.hour_counts)
    reads = sum(counts)
    best = best_words = None
    if len(counts) == 24 and reads >= MIN_READS_FOR_BEST_HOUR:
        best = max(range(24), key=lambda h: (counts[h], -h))
        best_words = f"Usually reads messages around {_hour_words(best)} (their time, {state.timezone})."

    decisions = sorted(
        services.decisions.for_customer(customer_id), key=lambda d: (d.made_at, d.decision_id), reverse=True
    )[:TIMELINE_LIMIT]
    goals = {g.goal_id: g for g in services.workspace.goals()}
    batches = _batch_index(services)
    resolve = goal_resolver(services)
    deliveries: dict[str, Delivery] = {}
    reasons: dict[str, ReasonRef] = {}
    next_items: dict[str, NextItem] = {}
    for d in decisions:
        if d.kind is DecisionKind.NOTHING:
            reasons[d.decision_id] = left_alone_reason(d)
            continue
        gid = resolve(d)
        goal = goals.get(gid) if gid is not None else None
        since = autonomy_since(services, goal) if goal is not None else None
        own = [e for e in events if e.decision_id == d.decision_id]
        found = delivery(d, own, batches.get(d.decision_id), goal, since, now)
        if found is not None:
            deliveries[d.decision_id] = found
        item = d.action.item_id if d.action is not None else None
        if item and item not in next_items and len(next_items) < 3:
            next_items[item] = NextItem(
                item_id=item,
                name=item_name(services, item, events),
                reason="Chosen for one of their recent messages",
                decision_id=d.decision_id,
            )
    refill = _refill(services, purchases, now)
    mentioned = {e.item_id for e in events if e.item_id} | set(next_items)
    return CustomerInsight(
        customer_id=customer_id,
        as_of=now,
        display_name=display_name(state, events),
        timezone=state.timezone,
        best_hour=best,
        best_hour_words=best_words,
        reads_counted=reads,
        offer_habit=habit,  # type: ignore[arg-type]
        offer_habit_words=habit_words,
        purchases=len(purchases),
        purchases_with_offer=with_offer,
        contacts_7d=state.contacts_7d,
        refill=refill,
        next_items=list(next_items.values()),
        items={i: item_name(services, i, events) for i in sorted(mentioned)},
        deliveries=deliveries,
        left_alone=reasons,
    )


# --- /v2/goals/{id}/plan --------------------------------------------------------------------------

_FIELD_WORDS = {
    "purchases": "Purchases",
    "last_purchase_at": "Last purchase",
    "last_seen_at": "Last visit",
    "first_seen_at": "Signed up",
    "revenue": "Total spent",
    "segment": "Segment",
    "top_categories": "Favourite categories",
}


def filter_words(f: AudienceFilter) -> str:
    """One audience filter as a person would say it."""
    v = f.value
    if f.field == "purchases" and isinstance(v, int | float):
        n = int(v)
        if f.op == "eq":
            return (
                "Has never bought"
                if n == 0
                else ("Bought exactly once" if n == 1 else f"Bought exactly {n} times")
            )
        if f.op == "gte":
            return "Has bought at least once" if n == 1 else f"Has bought at least {n} times"
        if f.op == "gt":
            return f"Has bought more than {n} times"
        if f.op in ("lt", "lte"):
            return f"Has bought {'fewer than' if f.op == 'lt' else 'at most'} {n} times"
    label = _FIELD_WORDS.get(f.field, f.field.replace("_", " ").replace(".", " ").capitalize())
    if f.op == "days_since_gt":
        return f"{label} more than {v:g} days ago" if isinstance(v, int | float) else label
    if f.op == "days_since_lt":
        return f"{label} in the last {v:g} days" if isinstance(v, int | float) else label
    ops = {
        "eq": "is",
        "ne": "is not",
        "gt": "more than",
        "gte": "at least",
        "lt": "less than",
        "lte": "at most",
        "in": "is one of",
        "not_in": "is none of",
    }
    if f.op == "is_null":
        return f"{label}: not known"
    if f.op == "not_null":
        return f"{label}: known"
    shown = ", ".join(map(str, v)) if isinstance(v, tuple) else str(v)
    return f"{label} {ops.get(f.op, f.op)} {shown}"


def _money(x: float, currency: str) -> str:
    symbol = {"USD": "$", "EUR": "€", "GBP": "£", "INR": "₹"}.get(currency.upper())
    amount = f"{x:,.0f}" if float(x).is_integer() else f"{x:,.2f}"
    return f"{symbol}{amount}" if symbol else f"{amount} {currency.upper()}"


def hard_rules(envelope: Envelope, currency: str) -> list[HardRule]:
    """The rules decide() never breaks, worded with this plan's values (codes of decide/rules.py)."""
    cap = envelope.max_contacts_per_week
    quiet = f"{envelope.quiet_start_hour:02d}:00-{envelope.quiet_end_hour:02d}:00"
    if envelope.quiet_start_hour == envelope.quiet_end_hour:
        quiet_words = "No quiet hours are set."
    else:
        quiet_words = (
            f"No messages between {envelope.quiet_start_hour:02d}:00 and {envelope.quiet_end_hour:02d}:00, "
            "in each person's own time zone."
        )
    return [
        HardRule(
            code="no_consent", words="Only messages people who agreed to hear from you on that channel."
        ),
        HardRule(code="unsubscribed", words="Never messages anyone who unsubscribed."),
        HardRule(code="quiet_hours", words=quiet_words, value=quiet),
        HardRule(
            code="weekly_cap",
            words=f"At most {cap} message{'s' if cap != 1 else ''} per person per week, across all goals.",
            value=cap,
        ),
        HardRule(
            code="no_budget",
            words=f"Stops spending when the budget of {_money(envelope.budget, currency)} is used up.",
            value=envelope.budget,
        ),
        HardRule(code="open_complaint", words="No messages to anyone with an open complaint."),
        HardRule(code="no_discounts", words="Never sends a discount to someone who asked for none."),
        HardRule(
            code="holdout",
            words=f"Never contacts the {envelope.holdout_share:.0%} of customers set aside for comparison.",
            value=envelope.holdout_share,
        ),
    ]


_JUDGE = re.compile(r"\b(brand_voice|honesty) (\d)(?: \(([^)]*)\))?")
_CHECK_NOTES = {
    "brand_voice": "Sounds like your brand",
    "honesty": "No false claims",
    "no_invented_numbers": "No made-up numbers or discounts",
    "unsubscribe": "Unsubscribe line present",
    "channel_rules": "Follows the channel's rules",
}


def _rule_check(problem: str) -> EditorCheckName:
    low = problem.lower()
    if "unsubscribe" in low or "opt-out" in low or "opt out" in low or "stop" in low.split():
        return "unsubscribe"
    if "figure" in low or "not an approved offer" in low or "{{offer_label}}" in low:
        return "no_invented_numbers"
    if "banned phrase" in low or "shouting" in low or "exclamation" in low:
        return "brand_voice"
    return "channel_rules"


def review_content(item: ContentItem, threshold: int = 3) -> ContentReview:
    """The AI editor's verdict on one message, as five checks, read from its `critic_notes`.

    The Critic (autonomy/agents/critic.py) writes `rule: ...` lines for every rule a message breaks
    (and then never asks the judge), or one `judge run N [...]: brand_voice 5 (...); honesty 4 (...)`
    line per judge run, then `approved` or `rejected: ...`. Notes in any other form are kept as
    they are, and the checks stay empty: nothing is read into them.
    """
    notes = list(item.critic_notes)
    rules = [n.removeprefix("rule:").strip() for n in notes if n.startswith("rule:")]
    judged = [n for n in notes if n.startswith("judge run")]
    decided = any(n.startswith(("approved", "rejected:")) for n in notes)
    by_critic = bool(rules or judged or decided)
    if not by_critic:
        person = item.status in (ContentStatus.REJECTED, ContentStatus.RETIRED)
        return ContentReview(
            content_id=item.content_id,
            status=item.status,
            reviewed_by="person" if person else None,
            passed=None,
            notes=notes,
        )
    failed: dict[str, list[str]] = {}
    for p in rules:
        failed.setdefault(_rule_check(p), []).append(p)
    checks: list[EditorCheck] = []
    for name in ("no_invented_numbers", "unsubscribe", "channel_rules"):
        if name in failed:
            checks.append(EditorCheck(check=name, passed=False, note="; ".join(failed[name])))
        elif name == "unsubscribe" and item.channel is not Channel.EMAIL:
            checks.append(
                EditorCheck(check="unsubscribe", passed=True, note="No unsubscribe line needed here")
            )
        else:
            checks.append(EditorCheck(check=name, passed=True, note=_CHECK_NOTES[name]))
    no_verdict = any("no valid verdict" in n for n in judged)
    for name in ("brand_voice", "honesty"):
        if name in failed:
            checks.append(EditorCheck(check=name, passed=False, note="; ".join(failed[name])))  # type: ignore[arg-type]
            continue
        scores = [(int(s), why) for run in judged for c, s, why in _JUDGE.findall(run) if c == name]
        if no_verdict:
            scores.append((0, "The editor gave no clear verdict"))
        if not scores:
            why = "Not scored: it failed a rule first" if rules else "Not scored"
            checks.append(EditorCheck(check=name, passed=None, note=why))  # type: ignore[arg-type]
            continue
        low, why = min(scores)
        note = why or _CHECK_NOTES[name]
        checks.append(EditorCheck(check=name, passed=low >= threshold, note=note))  # type: ignore[arg-type]
    passed: bool | None = all(c.passed is not False for c in checks) and not any(
        n.startswith("rejected:") for n in notes
    )
    if not rules and not decided:
        # The editor started and has not finished (the judge went down between its two runs, so the message
        # is held as a draft). That is not a pass, and the server refuses to approve it (`editor_passed`).
        passed = None
    # The editor approved it and a person turned it down afterwards.
    person = item.status is ContentStatus.REJECTED and passed is True
    return ContentReview(
        content_id=item.content_id,
        status=item.status,
        reviewed_by="person" if person else "critic",
        passed=passed,
        checks=checks,
        notes=notes,
    )


def _judge_threshold() -> int:
    try:
        from autonomy.agents.critic import JUDGE_THRESHOLD
    except ImportError:
        return 3
    return int(JUDGE_THRESHOLD)


def plan_facts(services: Services, goal_id: str) -> PlanFacts:
    from autonomy.decide.audience import matches

    goal = services.get_goal(goal_id)
    now = services.clock()
    brief = latest_brief(services, goal_id)
    groups: Counter[Group] = Counter()
    if brief is not None:
        for s in services.states.scan():
            if matches(s, brief.audience, now):
                groups[s.group] += 1
    threshold = _judge_threshold()
    reviews = [
        review_content(c, threshold) for c in services.workspace.content(goal_id=goal_id, approved_only=False)
    ]
    return PlanFacts(
        goal_id=goal_id,
        as_of=now,
        audience_n=sum(groups.values()),
        agent_n=groups[Group.AGENT],
        baseline_n=groups[Group.BASELINE],
        holdout_n=groups[Group.HOLDOUT],
        audience_words=[filter_words(f) for f in brief.audience.filters] if brief is not None else [],
        hard_rules=hard_rules(brief.envelope, goal.currency) if brief is not None else [],
        content=reviews,
        editor_passed=sum(1 for r in reviews if r.passed is True),
        editor_failed=sum(1 for r in reviews if r.passed is False),
        not_reviewed=sum(1 for r in reviews if r.passed is None),
        rejected_by_person=sum(
            1 for r in reviews if r.reviewed_by == "person" and r.status is ContentStatus.REJECTED
        ),
        autonomy=goal.autonomy,
        autonomy_since=autonomy_since(services, goal),
    )


# --- /v2/expected ---------------------------------------------------------------------------------


def expected(services: Services, goal_id: str, since: datetime | None = None) -> Expected:
    """Sum the agent's own forecast over its contact decisions for the goal since `since`.

    Each decision's forecast is its chosen bid's `expected_net` (extra profit after offer and
    channel costs over the plan's outcome window) with that bid's `uncertainty` as its standard
    deviation; the sum's 95% range treats them as independent. The simple-rule group is left out:
    its decisions carry no forecast.
    """
    goal = services.get_goal(goal_id)
    now = services.clock()
    start = since or autonomy_since(services, goal) or goal.starts_at
    brief = latest_brief(services, goal_id)
    horizon = brief.horizon_days if brief is not None else 14
    total = var = spend = 0.0
    contacts = forecasts = 0
    customers: set[str] = set()
    for d in services.decisions.scan(since=start, until=now + _TICK):
        if d.kind is not DecisionKind.ACT or d.action is None or d.action.goal_id != goal_id:
            continue
        if d.group is not Group.AGENT:
            continue
        chosen = next((b for b in d.bids if b.action == d.action), None)
        contacts += 1
        customers.add(d.customer_id)
        if chosen is None:  # a rule's decision, not the agent's maths: no forecast to add
            continue
        forecasts += 1
        total += chosen.expected_net
        var += chosen.uncertainty**2
        spend += max(chosen.offer_cost, 0.0) + max(chosen.channel_cost, 0.0)
    estimate = None
    if forecasts:
        half = 1.96 * math.sqrt(var)
        estimate = Estimate(point=round(total, 2), low=round(total - half, 2), high=round(total + half, 2))
    if estimate is None:
        words = (
            "No contacts decided yet, so there is no forecast."
            if not contacts
            else "These decisions carry no forecast of their own, so there is nothing to add up."
        )
    else:
        words = (
            f"Its own forecast, not proof: {forecasts} contacts expected to add "
            f"{_money(estimate.point, goal.currency)} (range {_money(estimate.low, goal.currency)} to "
            f"{_money(estimate.high, goal.currency)}) in extra profit over {horizon} days each."
        )
    return Expected(
        goal_id=goal_id,
        as_of=now,
        since=start,
        contacts=contacts,
        with_forecast=forecasts,
        customers=len(customers),
        expected_net=estimate,
        expected_spend=round(spend, 2),
        horizon_days=horizon,
        currency=goal.currency,
        words=words,
    )


# --- /v2/health/limits ----------------------------------------------------------------------------


def unsubscribe_baseline(services: Services) -> float | None:
    """The usual unsubscribe rate the breakers compare with, when the runtime was given one."""
    breakers = getattr(inner_runtime(services), "breakers_fn", None)
    baselines = getattr(breakers, "baselines", None)
    if isinstance(baselines, Mapping):
        value = baselines.get("unsubscribe_rate")
        if isinstance(value, int | float) and value > 0:
            return float(value)
    return None


def _pct(x: float) -> str:
    """0.0008 -> '0.08%', 0.95 -> '95%', 0.004 -> '0.4%'."""
    return f"{round(x * 100, 2):g}%"


def health_limits(services: Services, goal_id: str | None = None) -> HealthLimits:
    from autonomy.contracts import DEFAULT_STOP_RULES

    now = services.clock()
    rules = DEFAULT_STOP_RULES
    planned = spent = None
    if goal_id is not None:
        goal = services.get_goal(goal_id)
        brief = latest_brief(services, goal_id)
        if brief is not None:
            rules = brief.envelope.stop_rules
        spent = goal_spend(services, goal.goal_id, now)
        planned_fn = getattr(inner_runtime(services), "planned_fn", None)
        if callable(planned_fn):
            try:
                planned = float(planned_fn(goal_id, now))
            except Exception:
                planned = None
    health = services.health()
    base = unsubscribe_baseline(services)
    out: list[StopRuleStatus] = []
    for r in rules:
        what = "this goal" if r.action == "pause_goal" else "that channel"
        current = baseline = pause_at = None
        judged = True
        if r.metric is StopMetric.COMPLAINT_RATE:
            current, pause_at = health.complaint_rate, r.threshold
            words = f"Pauses {what} if complaints go above {_pct(r.threshold)} of delivered messages."
        elif r.metric is StopMetric.UNSUBSCRIBE_MULTIPLE:
            current, baseline = health.unsubscribe_rate, base
            if base is None:
                judged = False
                words = (
                    f"Pauses {what} if unsubscribes reach {r.threshold:g} times your usual rate. Your usual "
                    "rate is not known yet, so this check is not running."
                )
            else:
                pause_at = round(base * r.threshold, 6)
                words = (
                    f"Pauses {what} if unsubscribes reach {r.threshold:g} times your usual rate "
                    f"({_pct(base)}), that is {_pct(pause_at)}."
                )
        elif r.metric is StopMetric.DELIVERY_RATE_BELOW:
            current, pause_at = health.delivery_rate, r.threshold
            words = f"Pauses {what} if fewer than {_pct(r.threshold)} of messages are delivered."
        elif r.metric is StopMetric.SPEND_PACE:
            if spent is not None and planned:
                current = round(spent / planned, 3)
            judged = goal_id is not None and planned is not None
            words = f"Pauses {what} if spending runs more than {r.threshold - 1:.0%} ahead of plan."
        else:
            judged = Channel.WHATSAPP in services.adapters
            words = f"Pauses {what} if WhatsApp rates your message quality {'yellow' if r.threshold <= 1 else 'red'}."
        out.append(
            StopRuleStatus(
                metric=r.metric,
                threshold=r.threshold,
                action=r.action,
                words=words,
                current=current,
                baseline=baseline,
                pause_at=pause_at,
                judged=judged,
            )
        )
    return HealthLimits(goal_id=goal_id, as_of=now, unsubscribe_baseline=base, rules=out)


# --- /v2/data/status and /v2/channels -------------------------------------------------------------

_CUSTOMER_MADE = frozenset(
    {EventType.SIGNUP, EventType.VISIT, EventType.VIEW, EventType.CART, EventType.PURCHASE, EventType.REFUND}
)


def _source_kind(source: str) -> str:
    low = source.lower()
    if low in ("gym", "sim", "simulation", "demo", "fake"):
        return "simulation"
    if low in ("csv", "file", "upload", "xlsx", "excel", "online_retail", "uci"):
        return "file"
    if low in ("shopify", "woocommerce", "magento", "bigcommerce", "shop"):
        return "shop"
    if low in ("postgres", "mysql", "database", "db", "warehouse", "bigquery", "snowflake"):
        return "database"
    return "other"


def data_status(services: Services) -> DataStatus:
    """What customer data the system holds, by source, plus the Data Doctor's report when it has one."""
    now = services.clock()
    per: dict[str, dict[str, Any]] = {}
    items: set[str] = set()
    for e in services.events.scan(until=now + _TICK):
        if e.type not in _CUSTOMER_MADE:
            continue
        row = per.setdefault(e.source, {"customers": set(), "orders": 0, "events": 0, "updated_at": None})
        row["customers"].add(e.customer_id)
        row["events"] += 1
        if e.type is EventType.PURCHASE:
            row["orders"] += 1
            if e.item_id:
                items.add(e.item_id)
        row["updated_at"] = e.ts if row["updated_at"] is None else max(row["updated_at"], e.ts)
    sources = [
        DataSource(
            name=name,
            kind=_source_kind(name),  # type: ignore[arg-type]
            customers=len(row["customers"]),
            orders=row["orders"],
            events=row["events"],
            updated_at=row["updated_at"],
        )
        for name, row in sorted(per.items())
    ]
    report = services.data_doctor() if services.data_doctor is not None else None
    fixes = [DataFix.model_validate(f) for f in (report or {}).get("fixes", ())]
    questions = [DataQuestion.model_validate(q) for q in (report or {}).get("questions", ())]
    problems = [str(p) for p in (report or {}).get("problems", ())]
    return DataStatus(
        as_of=now,
        customers=services.states.count(),
        orders=sum(s.orders for s in sources),
        products=len(items) or None,
        sources=sources,
        fixes=fixes,
        problems=problems,
        questions=questions,
        doctor_connected=report is not None,
    )


_DIRECT = (Channel.EMAIL, Channel.WHATSAPP)
_HANDOFF = (Channel.SMS, Channel.WEBHOOK)
_CHANNEL_NAMES = {
    Channel.EMAIL: "Email",
    Channel.WHATSAPP: "WhatsApp",
    Channel.SMS: "SMS",
    Channel.WEBHOOK: "Your own tools",
}


def _paused_channels(services: Services) -> set[Channel]:
    runtime = inner_runtime(services)
    paused: set[Channel] = set(getattr(runtime, "paused_channels", ()) or ())
    dispatcher = getattr(runtime, "dispatcher", None)
    paused |= set(getattr(dispatcher, "paused_channels", ()) or ())
    return paused


def _pause_reason(alerts: Sequence[Alert], channel: Channel) -> str | None:
    name = channel.value.lower()
    for a in alerts:  # newest first
        if (a.auto_paused or a.code.startswith("breaker.")) and name in f"{a.code} {a.message}".lower():
            return a.message
    return None


def channels(services: Services) -> list[ChannelStatus]:
    paused = _paused_channels(services)
    alerts = services.alerts()
    out: list[ChannelStatus] = []
    for ch in (*_DIRECT, *_HANDOFF):
        direct = ch in _DIRECT
        ready = ch in services.adapters
        is_paused = ch in paused
        name = _CHANNEL_NAMES[ch]
        if is_paused:
            words = f"{name} is paused by a safety check."
        elif direct:
            words = f"{name}: Autonomy sends these itself." if ready else f"{name} is not set up yet."
        elif ch is Channel.WEBHOOK:
            words = (
                "Your own tools: Autonomy hands each decision to them, and they send it."
                if ready
                else "Your own tools can send through a hand-off; not set up yet."
            )
        else:
            words = (
                f"{name}: Autonomy hands each decision to your own tool, which sends it."
                if ready
                else f"{name} works through a hand-off to your own tool; not set up yet."
            )
        out.append(
            ChannelStatus(
                channel=ch,
                mode="direct" if direct else "handoff",
                ready=ready,
                paused=is_paused,
                paused_reason=(
                    (_pause_reason(alerts, ch) or "Paused by a safety check") if is_paused else None
                ),
                words=words,
            )
        )
    return out


# --- /v2/opportunities/sized ----------------------------------------------------------------------

_OPPORTUNITY_REASONS: dict[str, tuple[Callable[[CustomerState], bool], str]] = {
    "opp_repeat_purchase": (
        lambda s: s.purchases == 1,
        "They bought once and have not come back for a second order yet.",
    ),
    "opp_onboarding": (
        lambda s: s.purchases == 0,
        "They signed up but have never bought.",
    ),
}


def sized_opportunities(services: Services) -> list[SizedOpportunity]:
    """Each suggested goal with how many customers it is about and why, counted from state now."""
    from api.v2.deps import _AUDIENCES
    from autonomy.decide.audience import matches

    now = services.clock()
    states = list(services.states.scan())
    out: list[SizedOpportunity] = []
    for goal in services.opportunities():
        rule = _OPPORTUNITY_REASONS.get(goal.goal_id)
        if rule is not None:
            test, reason = rule
            n = sum(1 for s in states if test(s))
        else:
            audience = _AUDIENCES[goal.kind]
            n = sum(1 for s in states if matches(s, audience, now))
            reason = f"Customers who match: {audience.description.lower()}."
        out.append(SizedOpportunity(goal=goal, audience_n=n, reason=reason))
    return out


# --- POST /v2/goals/read --------------------------------------------------------------------------

_CURRENCY_WORDS = {
    "$": "USD",
    "usd": "USD",
    "dollar": "USD",
    "dollars": "USD",
    "€": "EUR",
    "eur": "EUR",
    "euro": "EUR",
    "euros": "EUR",
    "£": "GBP",
    "gbp": "GBP",
    "pound": "GBP",
    "pounds": "GBP",
    "₹": "INR",
    "rs": "INR",
    "rs.": "INR",
    "inr": "INR",
    "rupee": "INR",
    "rupees": "INR",
}
_NUM = r"(\d{1,3}(?:,\d{2,3})+|\d+(?:\.\d+)?)\s*(k|thousand|lakh|lakhs|m|million)?\b"
_AMOUNT_BEFORE = re.compile(r"(\$|€|£|₹|\brs\.?|\busd|\beur|\bgbp|\binr)\s?" + _NUM, re.IGNORECASE)
_AMOUNT_AFTER = re.compile(_NUM + r"\s*(usd|eur|gbp|inr|dollars?|euros?|pounds?|rupees?)\b", re.IGNORECASE)
_SCALE = {"k": 1e3, "thousand": 1e3, "lakh": 1e5, "lakhs": 1e5, "m": 1e6, "million": 1e6}
_PERIOD = re.compile(
    r"\b(this month|this week|this quarter|next month|(?:in|over|for|within|next) (\d{1,3}) (days?|weeks?|months?))\b",
    re.IGNORECASE,
)


def _amount(number: str, scale: str | None) -> float:
    value = float(number.replace(",", ""))
    return value * _SCALE.get((scale or "").lower(), 1.0)


def read_goal_text(text: str, now: datetime) -> GoalReading:
    """An amount ('$5,000', '5k dollars', '₹50,000') and a period ('this month', 'in 30 days')."""
    budget = currency = None
    m = _AMOUNT_BEFORE.search(text)
    if m:
        currency = _CURRENCY_WORDS.get(m.group(1).lower().strip())
        budget = _amount(m.group(2), m.group(3))
    else:
        m2 = _AMOUNT_AFTER.search(text)
        if m2:
            currency = _CURRENCY_WORDS.get(m2.group(3).lower())
            budget = _amount(m2.group(1), m2.group(2))
    ends_at = period = None
    p = _PERIOD.search(text)
    if p:
        period = p.group(1)
        low = period.lower()
        local = now.astimezone(UTC)
        if low == "this month":
            first = dt.date(local.year + local.month // 12, local.month % 12 + 1, 1)
            ends_at = datetime.combine(first, time(0), tzinfo=UTC)
        elif low == "next month":
            month = local.month + 2
            first = dt.date(local.year + (month - 1) // 12, (month - 1) % 12 + 1, 1)
            ends_at = datetime.combine(first, time(0), tzinfo=UTC)
        elif low == "this week":
            monday = local.date() + timedelta(days=7 - local.weekday())
            ends_at = datetime.combine(monday, time(0), tzinfo=UTC)
        elif low == "this quarter":
            q_month = ((local.month - 1) // 3 + 1) * 3 + 1
            first = dt.date(local.year + (q_month - 1) // 12, (q_month - 1) % 12 + 1, 1)
            ends_at = datetime.combine(first, time(0), tzinfo=UTC)
        else:
            n = int(p.group(2))
            unit = p.group(3).lower().rstrip("s")
            days = n * {"day": 1, "week": 7, "month": 30}[unit]
            ends_at = now + timedelta(days=days)
    parts: list[str] = []
    if budget is not None:
        parts.append(f"We read {_money(budget, currency or 'USD')} as your budget.")
    if period is not None:
        last = (ends_at - timedelta(seconds=1)) if ends_at is not None else None
        parts.append(f"We read '{period}' as the period, so it runs to {last:%d %b %Y}.")
    words = " ".join(parts) + (" Change it if that's wrong." if parts else "")
    return GoalReading(
        text=text,
        budget=budget,
        currency=currency,
        ends_at=ends_at,
        period=period,
        words=words or "No budget or period found in the words.",
    )


# --- /v2/batches/{id}/breakdown -------------------------------------------------------------------


def batch_breakdown(services: Services, batch: Batch) -> BatchBreakdown:
    by_channel: Counter[str] = Counter()
    by_offer: Counter[str] = Counter()
    for i in batch.decision_ids:
        d = services.decisions.get(i)
        if d is None or d.action is None:
            continue
        by_channel[d.action.channel.value] += 1
        by_offer[d.action.offer_id] += 1
    brief = latest_brief(services, batch.goal_id)
    labels = {o.offer_id: o.label for o in brief.envelope.offers} if brief is not None else {}
    labels.setdefault("none", "No offer")
    n = sum(by_channel.values())

    def listed(counts: Counter[str], name: Callable[[str], str]) -> str:
        return ", ".join(f"{v} {name(k)}" for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

    chan = {"email": "email", "whatsapp": "WhatsApp", "sms": "SMS", "webhook": "your own tool"}
    words = f"{n} customer{'s' if n != 1 else ''}"
    if n:
        words += f": {listed(by_channel, lambda k: 'by ' + chan.get(k, k))}"
        words += f"; {listed(by_offer, lambda k: 'with ' + labels.get(k, k).lower())}."
    else:
        words += "."
    return BatchBreakdown(
        batch_id=batch.batch_id,
        by_channel=dict(by_channel),
        by_offer=dict(by_offer),
        offer_labels={k: labels.get(k, k) for k in by_offer},
        words=words,
    )
