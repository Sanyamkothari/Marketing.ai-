"""When to send: the hour this person is most likely to pay attention, never in their quiet hours.

Plan-v2 section 3, step 5: use the person's own reading hours if we know them, otherwise their
segment's (or the business's), otherwise a plain default of 10:00 local time. The answer is always
inside the next 24 hours, never in the past, and never in the envelope's quiet hours in the
customer's own time zone.

Hours are walked in absolute time (UTC) and converted to local time one by one, so daylight-saving
changes and half-hour time zones (India, Newfoundland) come out right.

`ReadingHourModel` learns reading hours from every click with its timing:

* a click minutes after a send says the send hour was a good one (the message was read when it
  arrived);
* a click hours after a send says the click hour is when this person reads, whenever we send.

Each customer's hours are a Dirichlet posterior over the 24 local hours, shrunk towards their
segment's hours, which are shrunk towards the business's, which start from a soft bump around the
default hour. `choose_send_time` can take one Thompson draw from that posterior, so a customer
whose hour is uncertain is tried at other plausible hours and one whose hour is known is sent at it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

import numpy as np
import numpy.typing as npt

from autonomy.contracts import CustomerState, Envelope
from autonomy.decide.rules import customer_zone, is_quiet_hour

DEFAULT_HOUR = 10
HORIZON_HOURS = 24
HOURS = 24

QUICK_CLICK = timedelta(hours=1)
"""A click within this long of the send credits the send hour: the message was read on arrival."""
LATE_CLICK = timedelta(hours=2)
"""A click this long after the send credits the click hour: that is when the person reads. Between
the two, the credit is split in proportion to the delay."""

CUSTOMER_STRENGTH = 6.0
"""How many clicks' worth the segment's hours are worth for one customer. Small on purpose: two or
three clicks at one hour should be enough to move a person to it."""
SEGMENT_STRENGTH = 40.0
"""How many clicks' worth the business's hours are worth for one segment."""
BUSINESS_STRENGTH = 24.0
"""How many clicks' worth the starting profile is worth for the business."""
RATE_STRENGTH = 20.0
"""Sends' worth of the business-wide quick-click rate that each send hour's own rate starts from."""

HOUR_KERNEL: tuple[float, ...] = (0.125, 0.25, 0.5, 1.0, 0.5, 0.25, 0.125)
"""Spreads an hour's share to its neighbours, halving with each hour away: someone who reads at
23:00 is better caught at 20:00 than at 10:00 when 21:00-09:00 is quiet."""


@lru_cache(maxsize=512)
def zone_named(name: str) -> ZoneInfo:
    """The IANA zone of this name; an unknown or malformed name falls back to UTC, as
    `rules.customer_zone` does, so one bad row cannot stop a sweep."""
    try:
        return ZoneInfo(name)
    except (KeyError, ValueError):
        return ZoneInfo("UTC")


def local_hour(at: datetime, tz: str) -> int:
    """The hour of `at` on the clock of time zone `tz`."""
    if at.tzinfo is None:
        raise ValueError("times must be timezone-aware")
    return at.astimezone(zone_named(tz)).hour


def smooth(shares: Sequence[float], kernel: Sequence[float] = HOUR_KERNEL) -> tuple[float, ...]:
    """Spread 24 hourly shares to neighbouring hours (around the clock) and renormalise to 1."""
    half = len(kernel) // 2
    out = [
        sum(w * shares[(h + d) % HOURS] for d, w in zip(range(-half, half + 1), kernel, strict=True))
        for h in range(HOURS)
    ]
    total = sum(out)
    return tuple(x / total for x in out) if total > 0 else tuple(1.0 / HOURS for _ in range(HOURS))


def default_profile(default_hour: int = DEFAULT_HOUR) -> tuple[float, ...]:
    """Where the business starts before any click: half flat, half a soft bump around the default
    hour. Flat enough that a few days of clicks move it, peaked enough that a brand-new business is
    not sent at random hours."""
    bump = smooth([1.0 if h == default_hour % HOURS else 0.0 for h in range(HOURS)])
    return tuple(0.5 / HOURS + 0.5 * b for b in bump)


def _normalised(values: Sequence[float]) -> tuple[float, ...] | None:
    if len(values) != HOURS or any(v < 0 for v in values):
        return None
    total = float(sum(values))
    return tuple(float(v) / total for v in values) if total > 0 else None


@dataclass
class _Pool:
    """A segment's or the business's click evidence, kept so our own send hours cannot feed back.

    Quick clicks pile up at whatever hour we happen to send, so counting them where they land would
    teach the agent its own habits. Instead the pool keeps sends and quick clicks per send hour and
    spreads its quick clicks over the hours in proportion to each hour's clicks per send. Late
    clicks go to the hour they happened.
    """

    late: list[float] = field(default_factory=lambda: [0.0] * HOURS)
    quick: list[float] = field(default_factory=lambda: [0.0] * HOURS)
    sends: list[float] = field(default_factory=lambda: [0.0] * HOURS)

    def evidence(self) -> list[float]:
        quick_total = sum(self.quick)
        sends_total = sum(self.sends)
        if quick_total <= 0:
            return list(self.late)
        if sends_total <= 0:
            weights = [1.0 / HOURS] * HOURS  # no send record: quick clicks say nothing about hours
        else:
            mean = quick_total / sends_total
            rates = [
                (q + RATE_STRENGTH * mean) / (s + RATE_STRENGTH)
                for q, s in zip(self.quick, self.sends, strict=True)
            ]
            total = sum(rates)
            weights = [r / total for r in rates]
        return [lt + quick_total * w for lt, w in zip(self.late, weights, strict=True)]


class ReadingHourModel:
    """Each customer's reading hours as a distribution over 24 local hours, learned from clicks.

    Feed it `observe_send` for each message (optional, but it keeps the segment and business hours
    from simply echoing the hours we send at) and `observe_click` for each message's first click;
    ask for `profile` (posterior mean), `sample_profile` (one Thompson draw), `best_hour` or
    `sample_hour`. Segments are labels the caller chooses; a click counts under the segment the
    customer had when it was observed.

    What it does not use: a send with no click. One unanswered message says little about the hour
    (most messages go unanswered at every hour), and the pools' clicks-per-send already carry it.

    Deterministic: it never reads the clock and draws only from the generator it is handed.
    """

    def __init__(
        self,
        *,
        default_hour: int = DEFAULT_HOUR,
        prior_hours: Sequence[float] | None = None,
        customer_strength: float = CUSTOMER_STRENGTH,
        segment_strength: float = SEGMENT_STRENGTH,
        business_strength: float = BUSINESS_STRENGTH,
        quick_click: timedelta = QUICK_CLICK,
        late_click: timedelta = LATE_CLICK,
    ) -> None:
        if min(customer_strength, segment_strength, business_strength) <= 0:
            raise ValueError("prior strengths must be positive")
        if not timedelta(0) <= quick_click <= late_click:
            raise ValueError("need 0 <= quick_click <= late_click")
        given = _normalised(prior_hours) if prior_hours is not None else None
        if prior_hours is not None and given is None:
            raise ValueError("prior_hours needs 24 non-negative values with a positive sum")
        start = default_profile(default_hour)
        # A pooled prior (e.g. a `send_hour/...` Prior) is mixed with a little of the default so no
        # hour has zero weight: a Dirichlet needs every parameter above zero.
        self.base: tuple[float, ...] = (
            start if given is None else tuple(0.9 * g + 0.1 * s for g, s in zip(given, start, strict=True))
        )
        self.customer_strength = customer_strength
        self.segment_strength = segment_strength
        self.business_strength = business_strength
        self.quick_click = quick_click
        self.late_click = late_click
        self._own: dict[str, list[float]] = {}
        self._segments: dict[str, _Pool] = {}
        self._business = _Pool()
        self._means: dict[str | None, tuple[float, ...]] = {}  # cache, cleared on every observation

    # --- observing --------------------------------------------------------------------------------

    def _pools(self, segment: str | None) -> list[_Pool]:
        pools = [self._business]
        if segment is not None:
            pools.append(self._segments.setdefault(segment, _Pool()))
        return pools

    def observe_send(self, customer_id: str, send_at: datetime, tz: str, segment: str | None = None) -> None:
        """A message went out at `send_at`: one more send at that local hour for the pools' rates.

        `customer_id` is accepted for symmetry with `observe_click`; a customer's own hours learn
        from clicks only (see the class docstring).
        """
        del customer_id
        hour = local_hour(send_at, tz)
        for pool in self._pools(segment):
            pool.sends[hour] += 1.0
        self._means.clear()

    def observe_click(
        self,
        customer_id: str,
        send_at: datetime,
        click_at: datetime,
        tz: str,
        segment: str | None = None,
        weight: float = 1.0,
    ) -> None:
        """A click on the message sent at `send_at`. Call it once per message (its first click).

        A quick click credits the local send hour, a late one the local click hour, and one in
        between is split by its delay. A click stamped before its send (clock skew) counts as quick.
        """
        if weight <= 0:
            return
        delay = max(click_at - send_at, timedelta(0))
        span = (self.late_click - self.quick_click).total_seconds()
        if delay <= self.quick_click:
            late_share = 0.0
        elif delay >= self.late_click or span <= 0:
            late_share = 1.0
        else:
            late_share = (delay - self.quick_click).total_seconds() / span
        send_hour = local_hour(send_at, tz)
        click_hour = local_hour(click_at, tz)
        own = self._own.setdefault(customer_id, [0.0] * HOURS)
        own[send_hour] += weight * (1.0 - late_share)
        own[click_hour] += weight * late_share
        for pool in self._pools(segment):
            pool.quick[send_hour] += weight * (1.0 - late_share)
            pool.late[click_hour] += weight * late_share
        self._means.clear()

    def forget(self, customer_id: str) -> None:
        """Drop one customer's own record (erasure); the pools keep only anonymous counts."""
        self._own.pop(customer_id, None)

    def clicks(self, customer_id: str) -> float:
        """How many clicks' worth of evidence this customer's own hours rest on."""
        return float(sum(self._own.get(customer_id, ())))

    # --- reading ----------------------------------------------------------------------------------

    def business_mean(self) -> tuple[float, ...]:
        """The business's hours: the starting profile updated with every customer's clicks."""
        cached = self._means.get(None)
        if cached is None:
            cached = _shrunk(self._business.evidence(), self.base, self.business_strength)
            self._means[None] = cached
        return cached

    def segment_mean(self, segment: str | None) -> tuple[float, ...]:
        """A segment's hours, shrunk towards the business's; the business's for an unknown segment."""
        if segment is None or segment not in self._segments:
            return self.business_mean()
        cached = self._means.get(segment)
        if cached is None:
            cached = _shrunk(self._segments[segment].evidence(), self.business_mean(), self.segment_strength)
            self._means[segment] = cached
        return cached

    def alpha(self, customer_id: str, segment: str | None = None) -> npt.NDArray[np.float64]:
        """The customer's Dirichlet parameters: the segment's hours as a prior plus their own clicks."""
        prior: npt.NDArray[np.float64] = np.asarray(self.segment_mean(segment), dtype=np.float64)
        prior = prior * self.customer_strength
        own = self._own.get(customer_id)
        return prior if own is None else prior + np.asarray(own, dtype=np.float64)

    def profile(self, customer_id: str, segment: str | None = None) -> tuple[float, ...]:
        """The customer's expected reading hours (posterior mean), spread to neighbouring hours."""
        a = self.alpha(customer_id, segment)
        return smooth([float(x) for x in a / a.sum()])

    def sample_profile(
        self, customer_id: str, segment: str | None, rng: np.random.Generator
    ) -> tuple[float, ...]:
        """One Thompson draw of the customer's reading hours, spread to neighbouring hours."""
        draw = rng.dirichlet(self.alpha(customer_id, segment))
        return smooth([float(x) for x in draw])

    def best_hour(
        self, customer_id: str, segment: str | None = None, allowed: Sequence[int] | None = None
    ) -> int:
        """The allowed local hour with the highest expected share (all 24 when `allowed` is None)."""
        return _argmax_hour(self.profile(customer_id, segment), allowed)

    def sample_hour(
        self,
        customer_id: str,
        segment: str | None,
        rng: np.random.Generator,
        allowed: Sequence[int] | None = None,
    ) -> int:
        """The allowed local hour that wins one Thompson draw (all 24 when `allowed` is None)."""
        return _argmax_hour(self.sample_profile(customer_id, segment, rng), allowed)


def _shrunk(evidence: Sequence[float], prior: Sequence[float], strength: float) -> tuple[float, ...]:
    total = strength + sum(evidence)
    return tuple((strength * p + e) / total for p, e in zip(prior, evidence, strict=True))


def _argmax_hour(shares: Sequence[float], allowed: Sequence[int] | None) -> int:
    hours = list(range(HOURS)) if allowed is None else [h % HOURS for h in allowed]
    if not hours:
        raise ValueError("no allowed hour")
    return max(hours, key=lambda h: (shares[h], -h))


def allowed_hours(envelope: Envelope) -> list[int]:
    """The local hours outside the envelope's quiet hours."""
    return [h for h in range(HOURS) if not is_quiet_hour(h, envelope)]


# --- choosing the moment ---------------------------------------------------------------------------


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
    hour_scores: Sequence[float] | None = None,
    reading: ReadingHourModel | None = None,
    rng: np.random.Generator | None = None,
) -> datetime:
    """The send time for this customer, returned in the same time zone as `now`.

    1. Learned hours: `hour_scores` (24 values, e.g. one draw the caller already took from a
       `ReadingHourModel`), else `reading`'s view of this customer: one Thompson draw when `rng` is
       given, the posterior mean otherwise. The allowed hour with the highest score wins.
    2. Else, if the customer's `active_hours` are known, the allowed hour with the highest share.
    3. Else, if a segment or business profile is given (`fallback_hours`), the same with that.
       A profile whose only activity falls in quiet hours is skipped like a missing one.
    4. Else `default_hour` local; if that hour is quiet, the first allowed hour after it.
    Ties go to the earliest moment, so a good hour today beats the same hour tomorrow.
    """
    candidates = candidate_times(state, envelope, now)
    if not candidates:  # cannot happen: quiet hours always leave at least one hour of the day free
        raise ValueError("no allowed send time in the next 24 hours")
    learned: Sequence[float] | None = hour_scores
    if learned is None and reading is not None:
        learned = (
            reading.profile(state.customer_id, state.segment)
            if rng is None
            else reading.sample_profile(state.customer_id, state.segment, rng)
        )
    for profile in (_profile(learned), _profile(state.active_hours), _profile(fallback_hours)):
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
