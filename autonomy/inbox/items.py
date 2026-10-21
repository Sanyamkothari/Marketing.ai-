"""What an inbox item is, and where every alert the system raises goes (inbox, weekly briefing or ops).

**The inbox holds only what a person must act on** (docs/v2/ZERO_TOUCH.md, item 4):

| Kind | When | The one button |
|---|---|---|
| `safety_stop` | A circuit breaker paused a goal or a channel (complaints, unsubscribes, delivery, spend pace, WhatsApp quality) | Resume in Approve (paused) / Keep in Approve (Autopilot stepped back) |
| `losing` | Proof shows a goal losing money against the people we never contact (the negative-lift breaker, or Autopilot dropped back to Approve on it) | Pause this goal / Review the plan |
| `reconnect` | A platform needs reconnecting, the app was removed, or a one-time Klaviyo step only a person can do | Reconnect |
| `data` | The data cannot be trusted: decisions held for 6 hours on stale data, or a watchdog finding (missing days, a consent jump or drop, duplicate customers) | Sync now / Trust this data |
| `approval` | Something new or bigger than the mandate: the agent keeps choosing an offer, channel, message or budget the signed limits do not allow; the limits expired; or Shadow earned Autopilot | Review / Turn on Autopilot |
| `customer` | Customers replied and the Conversation agent handed them to a person (a complaint, a question, interest) | Mark as read |
| `privacy` | A privacy request a person must complete (open for a day) | Open the request |

Everything else goes to the weekly briefing (`BRIEFING`) or, for faults only an engineer can fix, to
the operator's log (`OPS`). `route_alert` says which, for every alert code the system raises today;
an unknown code goes to the briefing (never silent, never a page).

**One item per cause.** An item is keyed by its `cause` (for example `losing:<goal>` or
`reconnect:<connection>`), so the breaker's alert and Autopilot's automatic step back to Approve for
the same goal are one item, and a problem noticed by every hourly check is one item. Items are
driven by **conditions** (the goal is still paused, the connection still needs reconnecting), not by
alerts: an item resolves itself when its condition clears. See `autonomy.inbox.inbox`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field

from autonomy.contracts import Alert, Contract, Estimate

__all__ = [
    "BRIEFING",
    "INBOX",
    "OPS",
    "ROUTES",
    "Button",
    "ButtonAction",
    "Condition",
    "InboxItem",
    "InboxKind",
    "ResolvedBy",
    "Route",
    "kind_rank",
    "route_alert",
]


class InboxKind(StrEnum):
    SAFETY_STOP = "safety_stop"
    LOSING = "losing"
    RECONNECT = "reconnect"
    DATA = "data"
    APPROVAL = "approval"
    CUSTOMER = "customer"
    PRIVACY = "privacy"


_RANK: dict[InboxKind, int] = {
    InboxKind.SAFETY_STOP: 0,
    InboxKind.LOSING: 1,
    InboxKind.RECONNECT: 2,
    InboxKind.DATA: 3,
    InboxKind.CUSTOMER: 4,
    InboxKind.PRIVACY: 5,
    InboxKind.APPROVAL: 6,
}


def kind_rank(kind: InboxKind) -> int:
    """Display order: harm first, then what holds the business, then decisions to take."""
    return _RANK[kind]


ButtonAction = Literal[
    "open",  # go to `target` (a page of the app, or a platform's connect flow)
    "turn_on_autopilot",
    "pause_goal",
    "resume_in_approve",
    "resume_channel",
    "sync_now",
    "acknowledge",  # "trust this data" / "it was a quiet day": the person vouches for it
    "mark_handled",  # the person read the replies / completed the request
]
"""What the one button does. `open` navigates; the rest are `POST /v2/inbox/{item_id}/act`."""

ResolvedBy = Literal["cleared", "person"]


class Button(Contract):
    label: str = Field(description="Two to four words, e.g. 'Turn on Autopilot'")
    action: ButtonAction
    target: str | None = Field(default=None, description="For `open`: where to go")
    goal_id: str | None = None
    connection_id: str | None = None
    channel: str | None = None


class InboxItem(Contract):
    """One thing a person must act on. Resolved items stay, so a cause never repeats needlessly."""

    item_id: str
    business_id: str
    cause: str = Field(description="What it is about; one open item per cause")
    kind: InboxKind
    goal_id: str | None = None
    sentence: str = Field(description="One sentence: what happened and what it means")
    action: str = Field(description="The recommended action, in words")
    button: Button
    detail: str = Field(default="", description="The hover/tap description: why, numbers, method")
    money: Estimate | None = Field(default=None, description="Money at stake, always with its range")
    currency: str | None = None
    opened_at: AwareDatetime
    last_seen_at: AwareDatetime
    resolved_at: AwareDatetime | None = None
    resolved_by: ResolvedBy | None = None
    episode: int = Field(default=1, ge=1, description="1 for the first time this cause came up, then 2, ...")
    reopened: int = Field(default=0, ge=0, description="Times it came back within the cool-down")
    fingerprint: str = Field(description="The instance of the condition last seen (an alert id, a status)")
    handled_fingerprint: str | None = Field(
        default=None, description="The instance a person resolved; that instance never reopens"
    )
    missing_since: AwareDatetime | None = Field(
        default=None, description="When the condition was first not seen (it must stay gone to clear)"
    )
    clear_after_s: float = Field(default=0.0, ge=0)
    refs: tuple[str, ...] = Field(default=(), description="Evidence: alert ids, ledger entry hashes")

    @property
    def open(self) -> bool:
        return self.resolved_at is None


@dataclass(frozen=True)
class Condition:
    """A condition that, while it holds, means a person must act. Built fresh on every sync.

    `fingerprint` names the instance (the alert that started it, the connection's status change):
    once a person resolves an instance it never reopens, and a new instance after the cool-down is a
    new item. `since` is when the condition began; it opens an item only after `persist_after`
    (a sync delay that heals itself within the hour never reaches anyone). `clear_after` is how long
    it must be gone before the item resolves, so a condition that flickers does not open and close."""

    cause: str
    kind: InboxKind
    sentence: str
    action: str
    button: Button
    fingerprint: str
    since: datetime
    goal_id: str | None = None
    detail: str = ""
    money: Estimate | None = None
    currency: str | None = None
    persist_after: timedelta = timedelta(0)
    clear_after: timedelta = timedelta(0)
    refs: tuple[str, ...] = ()


# --- where alerts go ----------------------------------------------------------------------------

INBOX: Literal["inbox"] = "inbox"
BRIEFING: Literal["briefing"] = "briefing"
OPS: Literal["ops"] = "ops"
Destination = Literal["inbox", "briefing", "ops"]


@dataclass(frozen=True)
class Route:
    """Where an alert code goes. For `inbox`, the alert is evidence for a condition of `kind`; the
    item exists only while that condition holds. `topic` names the briefing line it is counted in."""

    destination: Destination
    topic: str
    kind: InboxKind | None = None
    note: str = ""


ROUTES: dict[str, Route] = {
    # Safety: a breaker paused a goal or a channel. The goal (or channel) stays paused until a person
    # resumes it, so this always needs a person.
    "breaker.complaint_rate": Route(INBOX, "safety", InboxKind.SAFETY_STOP),
    "breaker.unsubscribe_multiple": Route(INBOX, "safety", InboxKind.SAFETY_STOP),
    "breaker.delivery_rate_below": Route(INBOX, "safety", InboxKind.SAFETY_STOP),
    "breaker.spend_pace": Route(INBOX, "safety", InboxKind.SAFETY_STOP),
    "breaker.whatsapp_quality": Route(INBOX, "safety", InboxKind.SAFETY_STOP),
    "proof.negative_lift": Route(INBOX, "proof", InboxKind.LOSING),
    # Connections a person must fix.
    "connector.needs_reconnect": Route(INBOX, "connections", InboxKind.RECONNECT),
    "connector.uninstalled": Route(INBOX, "connections", InboxKind.RECONNECT),
    "connector.klaviyo_setup": Route(INBOX, "connections", InboxKind.RECONNECT),
    # Stale data holds decisions; most outages heal themselves, so only a hold of 6 hours or more
    # reaches the inbox (as `data`). A stale optional stream is a warning or an info note: briefing.
    "connector.stale": Route(
        INBOX, "data", InboxKind.DATA, "critical only, after 6 hours; warning/info: briefing"
    ),
    "connector.privacy_request": Route(INBOX, "privacy", InboxKind.PRIVACY, "after a day"),
    # Limits: a goal that cannot act because its mandate is missing, expired, revoked or unsigned;
    # or the agent keeps choosing what the mandate does not allow (counted from the decisions).
    "mandate.missing": Route(INBOX, "limits", InboxKind.APPROVAL),
    "mandate.not_active": Route(INBOX, "limits", InboxKind.APPROVAL),
    "mandate.unverified": Route(INBOX, "limits", InboxKind.APPROVAL),
    "mandate.error": Route(INBOX, "limits", InboxKind.APPROVAL),
    "mandate.violation": Route(
        INBOX, "limits", InboxKind.APPROVAL, "only when it keeps happening (see beyond_mandate)"
    ),
    # Customers a person should read.
    "reply.needs_human": Route(INBOX, "customers", InboxKind.CUSTOMER),
    "reply.error": Route(INBOX, "customers", InboxKind.CUSTOMER),
    # The weekly briefing: worth knowing, nothing to do now.
    "connector.row_drop": Route(BRIEFING, "data", note="the watchdog holds when data is really missing"),
    "connector.schema_drift": Route(
        BRIEFING, "data", note="the watchdog holds when the customer key changes"
    ),
    "connector.errors": Route(BRIEFING, "data", note="a stale stream raises its own hold"),
    "connector.webhooks": Route(BRIEFING, "data", note="the hourly catch-up poll covers missed webhooks"),
    "connector.stream_denied": Route(BRIEFING, "connections"),
    "connector.disconnect_followup": Route(BRIEFING, "connections"),
    "connector.klaviyo_unlinked": Route(BRIEFING, "connections"),
    "mandate.paced": Route(BRIEFING, "limits"),
    "mandate.store_unavailable": Route(BRIEFING, "limits"),
    "batch.expired": Route(BRIEFING, "approvals"),
    "weekly.refused": Route(BRIEFING, "plan"),
    "ingest.stale": Route(BRIEFING, "data"),
    "dispatch.refused": Route(BRIEFING, "sending"),
    "spend.unavailable": Route(BRIEFING, "sending"),
    "demo_data": Route(BRIEFING, "data"),
    "unsubscribes_up": Route(BRIEFING, "customers"),
    # Faults only an engineer can fix: the operator's log, never the shop owner's inbox.
    "runtime.hook_error": Route(OPS, "faults"),
    "data.gate_error": Route(OPS, "faults"),
    "ingest.unavailable": Route(OPS, "faults"),
    "breakers.error": Route(OPS, "faults"),
    "decide.error": Route(OPS, "faults"),
    "decide.wrong_goal": Route(OPS, "faults"),
    "enrich.error": Route(OPS, "faults"),
    "dispatch.missing": Route(OPS, "faults"),
    "dispatch.error": Route(OPS, "faults"),
    "nightly.error": Route(OPS, "faults"),
    "proof.error": Route(OPS, "faults"),
    "replay.error": Route(OPS, "faults"),
    "brief.split_mismatch": Route(OPS, "faults", note="our configuration, not the shop's"),
}
"""Every alert code raised today, and where it goes. Kept next to the kinds so a new code is one line."""

_UNKNOWN = Route(BRIEFING, "other", note="unknown code: counted in the briefing")


def route_alert(alert: Alert) -> Route:
    """Where this alert goes. Severity refines two codes: a stale *optional* stream (warning or info)
    is a briefing note, and an info-level alert never pages anyone."""
    route = ROUTES.get(alert.code)
    if route is None:
        if alert.code.startswith("breaker."):
            route = Route(INBOX, "safety", InboxKind.SAFETY_STOP)
        elif alert.code.startswith("connector.privacy_request"):
            route = ROUTES["connector.privacy_request"]
        else:
            return _UNKNOWN
    if route.destination == INBOX and alert.severity == "info":
        return Route(BRIEFING, route.topic, note="info level")
    if alert.code == "connector.stale" and alert.severity != "critical":
        return Route(BRIEFING, "data", note="an optional stream")
    return route
