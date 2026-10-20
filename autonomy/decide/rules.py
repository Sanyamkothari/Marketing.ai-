"""Hard rules: the things the agent may never do, however good a bid looks.

These are never learned and always on (plan-v2 section 3, step 3). Each rule is a small pure
function that looks at one candidate `Bid` and answers `(allowed, reason_code)`. The reason code is
what ends up in `Decision.blocked_by`, and `explain.reason()` turns it into plain English, so the
codes are a small fixed vocabulary (the constants below) rather than free text.

Rules check the bid as it will actually go out: the policy sets each bid's send time first, so the
quiet-hours rule tests the real send time in the customer's own time zone.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import NamedTuple
from zoneinfo import ZoneInfo

from autonomy.contracts import Bid, Brief, Channel, CustomerState, DecisionContext, Envelope, Group

# The reason codes, in the order `check_bid` tries them. The first failing rule names the block.
HOLDOUT = "holdout"
NO_BRIEF = "no_brief"
CHANNEL_NOT_ALLOWED = "channel_not_allowed"
OFFER_NOT_ALLOWED = "offer_not_allowed"
UNSUBSCRIBED = "unsubscribed"
NO_CONSENT = "no_consent"
NO_ADDRESS = "no_address"
OPEN_COMPLAINT = "open_complaint"
WEEKLY_CAP = "weekly_cap"
NO_BUDGET = "no_budget"
QUIET_HOURS = "quiet_hours"

ALL_CODES: tuple[str, ...] = (
    HOLDOUT,
    NO_BRIEF,
    CHANNEL_NOT_ALLOWED,
    OFFER_NOT_ALLOWED,
    UNSUBSCRIBED,
    NO_CONSENT,
    NO_ADDRESS,
    OPEN_COMPLAINT,
    WEEKLY_CAP,
    NO_BUDGET,
    QUIET_HOURS,
)

# Codes that mean "this channel cannot reach this person", as opposed to "not now".
REACH_CODES: frozenset[str] = frozenset({CHANNEL_NOT_ALLOWED, UNSUBSCRIBED, NO_CONSENT, NO_ADDRESS})


class RuleResult(NamedTuple):
    """`allowed` is True when the rule lets the bid through; `code` is empty then."""

    allowed: bool
    code: str


OK = RuleResult(True, "")


def _blocked(code: str) -> RuleResult:
    return RuleResult(False, code)


def customer_zone(state: CustomerState) -> ZoneInfo:
    """The customer's time zone. An unknown or malformed name falls back to UTC.

    The state folder should only ever store valid IANA names, so the fallback is a last resort that
    keeps one bad row from stopping a sweep, not a policy.
    """
    try:
        return ZoneInfo(state.timezone)
    except (KeyError, ValueError):
        return ZoneInfo("UTC")


def is_quiet_hour(local_hour: int, envelope: Envelope) -> bool:
    """Quiet hours run from `quiet_start_hour` up to (not including) `quiet_end_hour`, local time.

    They may wrap midnight (21 -> 9 means 21:00-08:59). Equal start and end means no quiet hours.
    """
    start, end = envelope.quiet_start_hour, envelope.quiet_end_hour
    if start == end:
        return False
    if start < end:
        return start <= local_hour < end
    return local_hour >= start or local_hour < end


# --- the rules ------------------------------------------------------------------------------------


def holdout(state: CustomerState) -> RuleResult:
    """The holdout group is never contacted by us: it is how we prove value."""
    return _blocked(HOLDOUT) if state.group is Group.HOLDOUT else OK


def channel_in_envelope(bid: Bid, envelope: Envelope) -> RuleResult:
    """The brief's envelope lists the channels the agent may use for this goal."""
    return OK if bid.action.channel in envelope.channels else _blocked(CHANNEL_NOT_ALLOWED)


def offer_in_envelope(bid: Bid, envelope: Envelope) -> RuleResult:
    """Only offers the marketer approved in the envelope may go out. 'none' is always allowed."""
    if bid.action.offer_id == "none" or any(o.offer_id == bid.action.offer_id for o in envelope.offers):
        return OK
    return _blocked(OFFER_NOT_ALLOWED)


def unsubscribed(state: CustomerState, channel: Channel) -> RuleResult:
    """An unsubscribe on a channel is final for that channel."""
    stats = state.channels.get(channel)
    return _blocked(UNSUBSCRIBED) if stats is not None and stats.unsubscribed else OK


def consent(state: CustomerState, channel: Channel) -> RuleResult:
    """We need the customer's consent on this specific channel."""
    stats = state.channels.get(channel)
    return OK if stats is not None and stats.consent else _blocked(NO_CONSENT)


def contactable(state: CustomerState, channel: Channel) -> RuleResult:
    """We need an address (email, phone, ...) for the channel. Consent and unsubscribes are their own
    rules, so this one only adds the address check that `ChannelStats.contactable` also requires."""
    stats = state.channels.get(channel)
    return OK if stats is not None and stats.contactable else _blocked(NO_ADDRESS)


def open_complaint(state: CustomerState) -> RuleResult:
    """While a complaint is open, nobody markets to this customer on any channel."""
    return _blocked(OPEN_COMPLAINT) if state.open_complaint else OK


def weekly_cap(state: CustomerState, max_contacts_per_week: int) -> RuleResult:
    """The hard backstop behind the attention price: at most N contacts in 7 days, across goals."""
    return _blocked(WEEKLY_CAP) if state.contacts_7d >= max_contacts_per_week else OK


def budget_left(bid: Bid, left_by_goal: Mapping[str, float]) -> RuleResult:
    """The goal must still have money for this bid's offer and channel cost.

    A goal missing from `budget_left` is not being tracked by the caller, so it is not blocked here;
    the runtime always fills it for live goals.
    """
    left = left_by_goal.get(bid.goal_id)
    if left is None:
        return OK
    cost = bid.offer_cost + bid.channel_cost
    return _blocked(NO_BUDGET) if left <= 0 or left < cost else OK


def quiet_hours(state: CustomerState, envelope: Envelope, send_at: datetime) -> RuleResult:
    """No message may land in the customer's local quiet hours."""
    local_hour = send_at.astimezone(customer_zone(state)).hour
    return _blocked(QUIET_HOURS) if is_quiet_hour(local_hour, envelope) else OK


# --- putting them together ------------------------------------------------------------------------


def brief_for(goal_id: str, briefs: Sequence[Brief]) -> Brief | None:
    """The brief of a goal, if it is active (the context only carries approved, active briefs)."""
    return next((b for b in briefs if b.goal_id == goal_id), None)


def strictest_weekly_cap(briefs: Sequence[Brief]) -> int | None:
    """The weekly cap is per customer across all goals, so the strictest active envelope wins."""
    caps = [b.envelope.max_contacts_per_week for b in briefs]
    return min(caps) if caps else None


def check_bid(bid: Bid, state: CustomerState, ctx: DecisionContext) -> RuleResult:
    """Run every hard rule on one bid, cheapest and most fundamental first; the first block wins."""
    first = holdout(state)
    if not first.allowed:
        return first
    brief = brief_for(bid.goal_id, ctx.briefs)
    if brief is None:
        return _blocked(NO_BRIEF)
    envelope = brief.envelope
    channel = bid.action.channel
    cap = strictest_weekly_cap(ctx.briefs)
    checks: tuple[RuleResult, ...] = (
        channel_in_envelope(bid, envelope),
        offer_in_envelope(bid, envelope),
        unsubscribed(state, channel),
        consent(state, channel),
        contactable(state, channel),
        open_complaint(state),
        weekly_cap(state, envelope.max_contacts_per_week if cap is None else cap),
        budget_left(bid, ctx.budget_left),
        quiet_hours(state, envelope, bid.action.send_at),
    )
    return next((c for c in checks if not c.allowed), OK)
