"""What needs a person right now: one builder per kind, each reading the system's own state.

`collect(sources, now)` returns the current `Condition`s; `Inbox.sync` turns them into items. Each
builder reads *state* where state exists (is the goal still paused, does the connection still need
reconnecting, is Autopilot still offered), so its item clears by itself; alerts are only the evidence
of what started it. A builder that fails is skipped and named in `Collected.errors`: one broken
source never empties the inbox of the others.

| Builder | Reads | Condition holds while |
|---|---|---|
| `safety_stops` | breaker alerts (30 days), the goal's status, paused channels | the goal (or channel) is still paused |
| `losing` | negative-lift alerts; Autopilot's automatic step back to Approve | the goal is paused by it, or still in Approve after it |
| `connections` | the connector store; Klaviyo setup alerts | the connection needs reconnecting / was uninstalled; no Klaviyo delivery since the setup alert |
| `stale_data` | open `connector.stale` incidents (critical) | decisions have been held 6 hours or more |
| `watchdog` | `DataWatchdog.findings` (hold and notice) | the finding is still there and nobody vouched for it |
| `limits` | mandate alerts, or the mandate itself | the goal cannot act for want of an active, signed mandate |
| `beyond_mandate` | the last 7 days of decisions | the agent keeps choosing what the mandate does not allow (20+ held, 5%+ of its contacts) |
| `autopilot` | `AutopilotGuard.offers` | the goal has earned Autopilot and waits for one click |
| `replies` | `reply.needs_human` / `reply.error` alerts since a person last read them | one aggregated item for all unread replies |
| `privacy` | open privacy incidents a day old | the request is not completed |
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from autonomy.contracts import (
    Alert,
    AutonomyLevel,
    Channel,
    DecisionKind,
    Estimate,
    EventType,
    Goal,
    GoalStatus,
)
from autonomy.inbox.items import Button, Condition, InboxKind
from autonomy.interfaces import DecisionLog, EventStore, Workspace

__all__ = [
    "BEYOND_CODES",
    "Collected",
    "InboxSources",
    "collect",
]

STALE_PERSIST = timedelta(hours=6)
PRIVACY_PERSIST = timedelta(hours=24)
DATA_CLEAR = timedelta(hours=1)
"""A data condition must stay gone this long before its item resolves (no flicker)."""
OFFER_CLEAR = timedelta(days=1)
"""An Autopilot offer stays a day after the bar dips (it is judged once a day), so it does not flicker."""
REPLY_WINDOW = timedelta(days=30)
SAFETY_WINDOW = timedelta(days=30)
LIMITS_RECENT = timedelta(hours=26)
"""Mandate alerts are raised once a day while the goal cannot act; older than this, it can again."""
BEYOND_WINDOW = timedelta(days=7)
BEYOND_MIN = 20
BEYOND_SHARE = 0.05

BEYOND_CODES: dict[str, str] = {
    "mandate_offer_not_approved": "offer",
    "mandate_margin_floor": "offer",
    "mandate_channel_not_allowed": "channel",
    "mandate_template_not_approved": "message",
    "mandate_budget_ceiling": "budget",
    "mandate_daily_spend": "budget",
}
"""Codes on a contact decision the mandate held back, grouped by what a person would approve."""

_FAMILY_WORDS: dict[str, tuple[str, str]] = {
    "offer": ("an offer your limits do not allow", "Review the offer"),
    "channel": ("a channel your limits do not include", "Review the channel"),
    "message": ("messages your limits have not approved", "Review the messages"),
    "budget": ("more than your budget allows", "Review the budget"),
}

LIMIT_CODES: frozenset[str] = frozenset(
    {"mandate.missing", "mandate.not_active", "mandate.unverified", "mandate.error"}
)
REPLY_CODES: frozenset[str] = frozenset({"reply.needs_human", "reply.error"})
NEGATIVE_LIFT = "proof.negative_lift"
STALE = "connector.stale"
PRIVACY = "connector.privacy_request"
KLAVIYO_SETUP = "connector.klaviyo_setup"
PERSON_DISCONNECT = "disconnected by a person"
"""`autonomy.connectors.sync.DISCONNECTED_BY_PERSON`."""

PLATFORMS: dict[str, str] = {"shopify": "Shopify", "klaviyo": "Klaviyo"}


class AlertReader(Protocol):
    def list_alerts(
        self, *, goal_id: str | None = None, since: datetime | None = None, limit: int | None = None
    ) -> list[Alert]: ...


class Guard(Protocol):
    """What the inbox reads from `autonomy.runtime.promotion.AutopilotGuard`."""

    def offers(self, now: datetime) -> Sequence[Any]: ...

    def demotion(self, goal_id: str, now: datetime | None = None) -> Any: ...


class Watchdog(Protocol):
    def findings(self, now: datetime) -> Sequence[Any]: ...


@dataclass
class InboxSources:
    """Everything the builders read. Only `business_id` and `workspace` are required; a source left
    out turns its builder off."""

    business_id: str
    workspace: Workspace
    alerts: AlertReader | None = None
    decisions: DecisionLog | None = None
    events: EventStore | None = None
    connectors: Any = None
    """An `autonomy.connectors.store.ConnectorStore` (connections and incidents)."""
    guard: Guard | None = None
    watchdog: Watchdog | None = None
    paused_channels: Callable[[], Iterable[Channel]] | None = None
    mandate_active: Callable[[str, datetime], bool | None] | None = None
    """goal_id, now -> whether its mandate is active (None: not governed). Without it, alerts decide."""
    handled_at: Callable[[str], datetime | None] | None = None
    """cause -> when a person last resolved it (`Inbox.handled_at`)."""
    app_url: str = "/v2/app/"


@dataclass
class Collected:
    conditions: list[Condition] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    cleared: set[str] = field(default_factory=set)
    """Causes whose condition is decisively gone (an Autopilot offer once the goal left the level it
    was offered at): their items resolve at once, without waiting out `clear_after`."""


def collect(sources: InboxSources, now: datetime) -> Collected:
    out = Collected()
    goals = {g.goal_id: g for g in sources.workspace.goals()}
    builders: list[tuple[str, Callable[[], Iterable[Condition]]]] = [
        ("safety_stops", lambda: safety_stops(sources, goals, now)),
        ("losing", lambda: losing(sources, goals, now)),
        ("connections", lambda: connections(sources, now)),
        ("stale_data", lambda: stale_data(sources, now)),
        ("watchdog", lambda: watchdog(sources, now)),
        ("limits", lambda: limits(sources, goals, now)),
        ("beyond_mandate", lambda: beyond_mandate(sources, goals, now)),
        ("autopilot", lambda: autopilot(sources, goals, now)),
        ("replies", lambda: replies(sources, now)),
        ("privacy", lambda: privacy(sources, now)),
    ]
    for name, build in builders:
        try:
            out.conditions.extend(build())
        except Exception as err:  # one broken source never hides the others
            out.errors.append(f"{name}: {type(err).__name__}: {err}"[:300])
    offerable = (AutonomyLevel.SHADOW, AutonomyLevel.APPROVE)
    out.cleared = {
        f"autopilot:{g.goal_id}"
        for g in goals.values()
        if g.status is not GoalStatus.ACTIVE or g.autonomy not in offerable
    }
    return out


# --- helpers ----------------------------------------------------------------------------------------


def _alerts(sources: InboxSources, since: datetime, codes: Callable[[str], bool]) -> list[Alert]:
    if sources.alerts is None:
        return []
    rows = sources.alerts.list_alerts(since=since)
    return sorted((a for a in rows if codes(a.code)), key=lambda a: (a.at, a.alert_id))


_PLAIN: tuple[tuple[str, str], ...] = (
    ("the no-contact holdout", "the people we never contact"),
    ("no-contact holdout", "people we never contact"),
    ("holdout customers", "people we never contact"),
    ("the holdout", "the people we never contact"),
    ("holdout", "people we never contact"),
)


def _plain(text: str) -> str:
    """Alert words as a shop owner reads them: "people we never contact", never "holdout"."""
    for old, new in _PLAIN:
        text = text.replace(old, new).replace(old.capitalize(), new.capitalize())
    return text


def _week(at: datetime) -> str:
    """The ISO week: a condition that clears and comes back a week later is a new instance."""
    iso = at.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _plan(sources: InboxSources, goal_id: str) -> str:
    return f"{sources.app_url}#/plan/{goal_id}"


def _goal_name(goal: Goal) -> str:
    """The goal in two or three words: its kind's name, else its own words, shortened."""
    named = _KIND_NAMES.get(goal.kind.value)
    if named is not None:
        return named
    text = goal.text.strip().rstrip(".")
    return text if len(text) <= 40 else text[:39].rstrip() + "\u2026"


_KIND_NAMES: dict[str, str] = {
    "win_back": "Win-back",
    "repeat_purchase": "Refill reminders",
    "cart_recovery": "Cart recovery",
    "cross_sell": "Cross-sell",
    "onboarding": "Onboarding",
    "retention": "Retention",
}


def _metric_words(code: str) -> str:
    return {
        "breaker.complaint_rate": "complaints went over the limit",
        "breaker.unsubscribe_multiple": "unsubscribes rose well above normal",
        "breaker.delivery_rate_below": "too many messages were not delivered",
        "breaker.spend_pace": "spend ran ahead of plan",
        "breaker.whatsapp_quality": "WhatsApp marked the number's quality down",
    }.get(code, "a safety limit tripped")


# --- the builders -----------------------------------------------------------------------------------


def _pause_cause(sources: InboxSources, goal_id: str) -> Alert | None:
    """The latest alert that paused this goal itself (a breaker's, or the negative-lift breaker's).

    Read over the goal's whole alert history, not a window: a paused goal stays paused until a person
    resumes it, however long ago the breaker tripped."""
    if sources.alerts is None:
        return None
    for a in sources.alerts.list_alerts(goal_id=goal_id, limit=500):  # newest first
        if a.goal_id != goal_id or not a.auto_paused:
            continue
        if a.code == NEGATIVE_LIFT or (a.code.startswith("breaker.") and "Paused the goal" in a.message):
            return a
    return None


def safety_stops(sources: InboxSources, goals: dict[str, Goal], now: datetime) -> list[Condition]:
    out: list[Condition] = []
    for gid, goal in sorted(goals.items()):
        if goal.status is not GoalStatus.PAUSED:
            continue
        a = _pause_cause(sources, gid)
        if a is None or a.code == NEGATIVE_LIFT:
            continue
        out.append(
            Condition(
                cause=f"safety_stop:{gid}",
                kind=InboxKind.SAFETY_STOP,
                goal_id=gid,
                sentence=f"{_goal_name(goal)} is paused: {_metric_words(a.code)}.",
                action="Resume it in Approve, so you see each batch before it goes out.",
                button=Button(label="Resume in Approve", action="resume_in_approve", goal_id=gid),
                detail=_plain(a.message),
                fingerprint=a.alert_id,
                since=a.at,
                refs=(a.alert_id,),
            )
        )
    by_channel: dict[str, Alert] = {}
    for a in _alerts(sources, now - SAFETY_WINDOW, lambda c: c.startswith("breaker.")):
        goal_ok = (
            a.goal_id is not None and a.goal_id in goals and goals[a.goal_id].status is GoalStatus.ACTIVE
        )
        if a.auto_paused and "Paused " in a.message and goal_ok:
            by_channel[f"{a.code}:{a.goal_id}"] = a
    paused = None if sources.paused_channels is None else {c.value for c in sources.paused_channels()}
    for a in by_channel.values():
        channel = _paused_channel(a.message)
        if channel is None or (paused is not None and channel not in paused):
            continue
        out.append(
            Condition(
                cause=f"safety_stop:channel:{channel}",
                kind=InboxKind.SAFETY_STOP,
                goal_id=a.goal_id,
                sentence=f"{channel.title()} is paused: {_metric_words(a.code)}.",
                action=f"Resume {channel} once the cause is fixed.",
                button=Button(label=f"Resume {channel}", action="resume_channel", channel=channel),
                detail=_plain(a.message),
                fingerprint=a.alert_id,
                since=a.at,
                refs=(a.alert_id,),
            )
        )
    return out


def _paused_channel(message: str) -> str | None:
    marker = "Paused "
    if marker not in message:
        return None
    word = message.split(marker, 1)[1].split(".", 1)[0].strip()
    if word == "the goal":
        return None
    return word if word in {c.value for c in Channel} else None


def losing(sources: InboxSources, goals: dict[str, Goal], now: datetime) -> list[Condition]:
    out: list[Condition] = []
    for gid, goal in sorted(goals.items()):
        a = _pause_cause(sources, gid) if goal.status is GoalStatus.PAUSED else None
        if a is None or a.code != NEGATIVE_LIFT:
            continue
        out.append(
            Condition(
                cause=f"losing:{goal.goal_id}",
                kind=InboxKind.LOSING,
                goal_id=goal.goal_id,
                sentence=f"{_goal_name(goal)} was losing money against the people we never contact, so it is paused.",
                action="Review the plan before it runs again.",
                button=Button(
                    label="Review the plan",
                    action="open",
                    target=_plan(sources, goal.goal_id),
                    goal_id=goal.goal_id,
                ),
                detail=_plain(a.message),
                fingerprint=a.alert_id,
                since=a.at,
                refs=(a.alert_id,),
            )
        )
    if sources.guard is None:
        return out
    for goal in goals.values():
        change = sources.guard.demotion(goal.goal_id, now)
        if change is None or goal.status is not GoalStatus.ACTIVE:
            continue
        reason = str(getattr(change, "reason", ""))
        evidence = dict(getattr(change, "evidence", {}) or {})
        money: Estimate | None = None
        if {"incremental_profit", "incremental_profit_low", "incremental_profit_high"} <= set(evidence):
            money = Estimate(
                point=evidence["incremental_profit"],
                low=evidence["incremental_profit_low"],
                high=evidence["incremental_profit_high"],
            )
        if reason == "losing":
            cause, kind = f"losing:{goal.goal_id}", InboxKind.LOSING
            sentence = f"{_goal_name(goal)} was losing money against the people we never contact, so Autopilot is off."
            action = "Pause the goal, or keep approving each batch yourself."
            button = Button(label="Pause this goal", action="pause_goal", goal_id=goal.goal_id)
        else:
            cause, kind = f"safety_stop:{goal.goal_id}", InboxKind.SAFETY_STOP
            sentence = f"{_goal_name(goal)} dropped to Approve: {_metric_words(reason.removeprefix('safety_stop:'))}."
            action = "Approve each batch yourself until it is fixed."
            # A button that closes it: an `open` link would leave the item up until the goal earned
            # Autopilot again (weeks), with nothing the person could press to say they have seen it.
            button = Button(label="Keep in Approve", action="mark_handled", goal_id=goal.goal_id)
        out.append(
            Condition(
                cause=cause,
                kind=kind,
                goal_id=goal.goal_id,
                sentence=sentence,
                action=action,
                button=button,
                detail=str(getattr(change, "words", "")),
                money=money,
                currency=goal.currency if money is not None else None,
                fingerprint=str(getattr(change, "entry_hash", "")),
                since=getattr(change, "at", now),
                refs=(str(getattr(change, "entry_hash", "")),),
            )
        )
    return out


def connections(sources: InboxSources, now: datetime) -> list[Condition]:
    store = sources.connectors
    if store is None:
        return []
    out: list[Condition] = []
    connect = f"{sources.app_url}#/connect"
    for c in store.connections(business_id=sources.business_id):
        status = getattr(c.status, "value", str(c.status))
        platform = PLATFORMS.get(c.kind, c.kind.title())
        if status == "needs_reconnect":
            sentence = f"{platform} ({c.label}) needs reconnecting; decisions wait until it is."
        elif status == "disconnected" and c.status_reason != PERSON_DISCONNECT:
            sentence = (
                f"The Autonomy app was removed from {platform} ({c.label}); decisions wait until it is back."
            )
        else:
            continue
        out.append(
            Condition(
                cause=f"reconnect:{c.connection_id}",
                kind=InboxKind.RECONNECT,
                sentence=sentence,
                action=f"Reconnect {platform}; it takes about a minute.",
                button=Button(
                    label=f"Reconnect {platform}",
                    action="open",
                    target=connect,
                    connection_id=c.connection_id,
                ),
                detail=c.status_reason or "",
                fingerprint=f"{status}:{c.updated_at.isoformat()}",
                since=c.updated_at,
            )
        )
    setup = _alerts(sources, now - SAFETY_WINDOW, lambda code: code == KLAVIYO_SETUP)
    latest: dict[str, Alert] = {}
    for a in setup:
        latest[a.message.split(")", 1)[0]] = a  # one per connection label ("Klaviyo (label)")
    active = {
        c.label: c
        for c in store.connections(business_id=sources.business_id)
        if c.kind == "klaviyo" and getattr(c.status, "value", str(c.status)) == "active"
    }
    for a in latest.values():
        conn = next((c for label, c in active.items() if f"({label})" in a.message), None)
        if conn is None or _delivered_since(sources, a.at, now):
            continue
        out.append(
            Condition(
                cause=f"setup:{conn.connection_id}",
                kind=InboxKind.RECONNECT,
                sentence="Klaviyo needs one step from you before Autonomy can send through it.",
                action="Follow the steps on the Connect page; it checks itself afterwards.",
                button=Button(
                    label="Open the steps", action="open", target=connect, connection_id=conn.connection_id
                ),
                detail=_plain(a.message),
                fingerprint=a.alert_id,
                since=a.at,
                refs=(a.alert_id,),
            )
        )
    return out


def _delivered_since(sources: InboxSources, since: datetime, now: datetime, cap: int = 20_000) -> bool:
    """True when a Klaviyo delivery arrived after `since`: the send setup works."""
    if sources.events is None:
        return False
    for i, e in enumerate(sources.events.scan(since=since, until=now)):
        if e.type is EventType.DELIVER and e.source == "klaviyo":
            return True
        if i >= cap:
            break
    return False


def stale_data(sources: InboxSources, now: datetime) -> list[Condition]:
    store = sources.connectors
    if store is None:
        return []
    out: list[Condition] = []
    labels = {c.connection_id: c for c in store.connections(business_id=sources.business_id)}
    for incident in store.open_incidents():
        conn = labels.get(incident.connection_id)
        if conn is None or not incident.code.startswith(STALE) or incident.severity != "critical":
            continue
        platform = PLATFORMS.get(conn.kind, conn.kind.title())
        hours = max(0, int((now - incident.opened_at).total_seconds() // 3600))
        out.append(
            Condition(
                cause=f"stale:{conn.connection_id}",
                kind=InboxKind.DATA,
                sentence=f"{platform} data has been out of date for {hours} hours, so decisions are on hold.",
                action=f"Sync {platform} now; if it fails again, reconnect it.",
                button=Button(label="Sync now", action="sync_now", connection_id=conn.connection_id),
                detail=_plain(incident.message),
                fingerprint=f"{incident.code}:{incident.opened_at.isoformat()}",
                since=incident.opened_at,
                persist_after=STALE_PERSIST,
                clear_after=DATA_CLEAR,
                refs=(incident.alert_id,),
            )
        )
    return out


def watchdog(sources: InboxSources, now: datetime) -> list[Condition]:
    if sources.watchdog is None:
        return []
    out: list[Condition] = []
    for f in sources.watchdog.findings(now):
        if f.level == "note":
            continue
        quiet = f.code in ("missing_day", "row_drop")
        out.append(
            Condition(
                cause=f"data:{f.code}:{f.scope}",
                kind=InboxKind.DATA,
                sentence=f.sentence,
                action=(
                    "If the shop really was quiet, say so and decisions go on."
                    if quiet
                    else "Check it in your platform, then confirm the data is right."
                ),
                button=Button(
                    label="It was a quiet day" if quiet else "The data is right", action="acknowledge"
                ),
                detail=f.detail,
                fingerprint=f.fingerprint,
                since=f.since,
                clear_after=DATA_CLEAR,
            )
        )
    return out


def limits(sources: InboxSources, goals: dict[str, Goal], now: datetime) -> list[Condition]:
    out: list[Condition] = []
    acting = [
        g for g in goals.values() if g.status is GoalStatus.ACTIVE and g.autonomy is not AutonomyLevel.SHADOW
    ]
    recent = {a.goal_id: a for a in _alerts(sources, now - LIMITS_RECENT, lambda c: c in LIMIT_CODES)}
    for goal in acting:
        active: bool | None = None
        if sources.mandate_active is not None:
            active = sources.mandate_active(goal.goal_id, now)
        alert = recent.get(goal.goal_id)
        if active is True or (active is None and alert is None):
            continue
        out.append(
            Condition(
                cause=f"limits:{goal.goal_id}",
                kind=InboxKind.APPROVAL,
                goal_id=goal.goal_id,
                sentence=f"{_goal_name(goal)} is not acting: its signed limits are missing or expired.",
                action="Sign the limits again; nothing goes out until then.",
                button=Button(
                    label="Sign the limits",
                    action="open",
                    target=_plan(sources, goal.goal_id),
                    goal_id=goal.goal_id,
                ),
                detail="" if alert is None else _plain(alert.message),
                fingerprint=f"limits:{goal.goal_id}:{_week(alert.at if alert is not None else now)}",
                since=alert.at if alert is not None else now,
                refs=() if alert is None else (alert.alert_id,),
            )
        )
    return out


def beyond_mandate(sources: InboxSources, goals: dict[str, Goal], now: datetime) -> list[Condition]:
    if sources.decisions is None:
        return []
    since = now - BEYOND_WINDOW
    contacts: Counter[str] = Counter()
    held: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for d in sources.decisions.scan(since=since, until=now):
        if d.kind is not DecisionKind.ACT or d.action is None:
            continue
        gid = d.action.goal_id
        contacts[gid] += 1
        families = {BEYOND_CODES[c] for c in d.blocked_by if c in BEYOND_CODES}
        for fam in families:
            held[(gid, fam)].append(d)
    out: list[Condition] = []
    for (gid, fam), rows in sorted(held.items()):
        goal = goals.get(gid)
        if goal is None or goal.status is not GoalStatus.ACTIVE:
            continue
        n = len(rows)
        if n < BEYOND_MIN or n < BEYOND_SHARE * contacts[gid]:
            continue
        total = var = 0.0
        priced = 0
        for d in rows:
            bid = next((b for b in d.bids if b.action == d.action), None)
            if bid is not None:
                total += bid.expected_net
                var += bid.uncertainty**2
                priced += 1
        money = None
        if priced:
            half = 1.96 * math.sqrt(var)
            money = Estimate(point=round(total, 2), low=round(total - half, 2), high=round(total + half, 2))
        what, label = _FAMILY_WORDS[fam]
        first = min(d.made_at for d in rows)
        out.append(
            Condition(
                cause=f"beyond:{gid}:{fam}",
                kind=InboxKind.APPROVAL,
                goal_id=gid,
                sentence=f"{_goal_name(goal)} chose {what} {n} times this week; those messages were held.",
                action="Review it: widen the limits if it is right for the shop, or leave them as they are.",
                button=Button(label=label, action="open", target=_plan(sources, gid), goal_id=gid),
                detail=(
                    f"{n} of its {contacts[gid]} contacts in the last 7 days went beyond the signed limits and "
                    "were not sent. "
                    + (
                        f"Its own forecast for them, not proof: {money.point:,.2f} {goal.currency} "
                        f"({money.low:,.2f} to {money.high:,.2f}). "
                        if money is not None
                        else ""
                    )
                    + "Widening the limits needs a new signed version; nothing changes until then."
                ),
                money=money,
                currency=goal.currency if money is not None else None,
                fingerprint=f"{fam}:{_week(first)}",
                since=first,
            )
        )
    return out


def autopilot(sources: InboxSources, goals: dict[str, Goal], now: datetime) -> list[Condition]:
    if sources.guard is None:
        return []
    out: list[Condition] = []
    for r in sources.guard.offers(now):
        goal = goals.get(r.goal_id)
        if goal is None:
            continue
        out.append(
            Condition(
                cause=f"autopilot:{goal.goal_id}",
                kind=InboxKind.APPROVAL,
                goal_id=goal.goal_id,
                sentence=r.sentence,
                action="Turn on Autopilot: it sends inside your limits and drops back to Approve on its own if proof turns.",
                button=Button(label="Turn on Autopilot", action="turn_on_autopilot", goal_id=goal.goal_id),
                detail=r.detail,
                money=r.forecast,
                currency=goal.currency,
                fingerprint=f"{goal.autonomy.value}:{r.since.isoformat() if r.since else ''}:{_week(r.as_of)}",
                since=r.as_of,
                clear_after=OFFER_CLEAR,
            )
        )
    return out


def replies(sources: InboxSources, now: datetime) -> list[Condition]:
    handled = sources.handled_at("replies") if sources.handled_at is not None else None
    since = now - REPLY_WINDOW if handled is None else max(handled, now - REPLY_WINDOW)
    rows = [
        a for a in _alerts(sources, since, lambda c: c in REPLY_CODES) if handled is None or a.at > handled
    ]
    if not rows:
        return []
    complaints = sum(1 for a in rows if "(complaint)" in a.message)
    people = len({a.message.split(" replied", 1)[0] for a in rows})
    who = "1 customer" if people == 1 else f"{people} customers"
    extra = ""
    if complaints:
        extra = ", including a complaint" if complaints == 1 else f", including {complaints} complaints"
    latest = rows[-1]
    return [
        Condition(
            cause="replies",
            kind=InboxKind.CUSTOMER,
            sentence=f"{who} replied and need{'s' if people == 1 else ''} a person{extra}.",
            action="Read them and answer in your own tool; mark them read here.",
            button=Button(label="Mark as read", action="mark_handled"),
            detail=_plain(" ".join(a.message for a in rows[-5:])),
            fingerprint=latest.alert_id,
            since=rows[0].at,
            refs=tuple(a.alert_id for a in rows[-20:]),
        )
    ]


def privacy(sources: InboxSources, now: datetime) -> list[Condition]:  # noqa: ARG001
    store = sources.connectors
    if store is None:
        return []
    mine = {c.connection_id for c in store.connections(business_id=sources.business_id)}
    out: list[Condition] = []
    for incident in store.open_incidents():
        if incident.connection_id not in mine or not incident.code.startswith(PRIVACY):
            continue
        out.append(
            Condition(
                cause=f"privacy:{incident.code}",
                kind=InboxKind.PRIVACY,
                sentence="A customer's privacy request is waiting; it must be completed within 30 days.",
                action="Open it, send the export (or confirm the deletion), then mark it done.",
                button=Button(
                    label="Mark as done", action="mark_handled", connection_id=incident.connection_id
                ),
                detail=_plain(incident.message),
                fingerprint=incident.code,
                since=incident.opened_at,
                persist_after=PRIVACY_PERSIST,
                refs=(incident.alert_id,),
            )
        )
    return out
