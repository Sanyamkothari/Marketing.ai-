"""When to send: the hour this person is most likely to pay attention, never in their quiet hours.

Plan-v2 section 3, step 5: use the person's own active hours if we know them, otherwise their
segment's (or the business's) profile, otherwise a plain default of 10:00 local time. The answer is
always inside the next 24 hours, never in the past, and never in the envelope's quiet hours in the
customer's own time zone.

Hours are walked in absolute time (UTC) and converted to local time one by one, so daylight-saving
changes and half-hour time zones (India, Newfoundland) come out right.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from autonomy.contracts import CustomerState, Envelope
from autonomy.decide.rules import customer_zone, is_quiet_hour

DEFAULT_HOUR = 10
HORIZON_HOURS = 24


def _profile(hours: Sequence[float] | None) -> tuple[float, ...] | None:
    """A usable 24-hour engagement profile, or None if it is missing, malformed or all zeros."""
    if hours is None or len(hours) != 24:
        return None
    if not any(h > 0 for h in hours):
        return None
    return tuple(float(h) for h in hours)


def candidate_times(state: CustomerState, envelope: Envelope, now: datetime) -> list[tuple[datetime, int]]:
    """Every allowed send moment in the next 24 hours, with its local hour, earliest first.

    The first candidate is `now` itself when the current local hour is allowed; the rest are the
    tops of the following local hours.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    zone = customer_zone(state)
    local_now = now.astimezone(zone)
    top = local_now.replace(minute=0, second=0, microsecond=0).astimezone(UTC)
    out: list[tuple[datetime, int]] = []
    for k in range(HORIZON_HOURS + 1):
        at = max(now, top + timedelta(hours=k))
        if at > now + timedelta(hours=HORIZON_HOURS):
            break
        hour = at.astimezone(zone).hour
        if is_quiet_hour(hour, envelope):
            continue
        out.append((at, hour))
    return out


def choose_send_time(
    state: CustomerState,
    envelope: Envelope,
    now: datetime,
    *,
    fallback_hours: Sequence[float] | None = None,
    default_hour: int = DEFAULT_HOUR,
) -> datetime:
    """The send time for this customer, returned in the same time zone as `now`.

    1. If the customer's `active_hours` are known, the allowed hour with the highest share wins.
    2. Else, if a segment or business profile is given (`fallback_hours`), the same with that.
       A profile whose only activity falls in quiet hours is skipped like a missing one.
    3. Else `default_hour` local; if that hour is quiet, the first allowed hour after it.
    Ties go to the earliest moment, so a good hour today beats the same hour tomorrow.
    """
    candidates = candidate_times(state, envelope, now)
    if not candidates:  # cannot happen: quiet hours always leave at least one hour of the day free
        raise ValueError("no allowed send time in the next 24 hours")
    for profile in (_profile(state.active_hours), _profile(fallback_hours)):
        if profile is not None:
            picked = _most_active(candidates, profile)
            if picked is not None:
                return picked.astimezone(now.tzinfo)
    best = min(candidates, key=lambda c: ((c[1] - default_hour) % 24, c[0].timestamp()))
    return best[0].astimezone(now.tzinfo)


def _most_active(candidates: Sequence[tuple[datetime, int]], profile: tuple[float, ...]) -> datetime | None:
    """The earliest candidate in the most active allowed hour; None if no allowed hour has activity
    (a profile active only in quiet hours tells us nothing usable)."""
    at, hour = max(candidates, key=lambda c: (profile[c[1]], -c[0].timestamp()))
    return at if profile[hour] > 0 else None
