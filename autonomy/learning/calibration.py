"""What a message is worth, measured on a past campaign's randomised control group.

The agent reads a message's value from clicks: `uplift = lift per click x click rate`. Clicks tell
messages apart, but not what they are worth: on real e-mail data a click (often just a visit) goes
with a small fraction of an extra purchase, and the agent's prior of a quarter purchase per click
made it value a Hillstrom e-mail about 10x too high (docs/v2/REAL_DATA_RESULTS.md). A past campaign
that was randomised says what its messages were really worth: the customers it sent to bought this
much more than the eligible customers it left out at random. This module finds such campaigns in
imported history and measures that difference; `autonomy.runtime.agent` turns it into the lift per
click (or, for a channel that logs no clicks, the lift per send).

**The product convention for a control group, events only.** Imported history never names a past
campaign's control group: the people it left out have no events. A campaign is read as randomised
over the customers it could have reached, and those it did not send to as its control, only when the
history itself says so:

1. Its sends carry `attrs["logged_probability"]` below 1 (the design's chance of what was sent).
   A campaign is the sends on one channel with one `attrs["campaign_id"]`, or, without one, on one
   UTC day. Each arm is one action (`channel|offer|theme[|format]`).
2. Within each arm the logged probability is the same for everyone (max/min at most
   `PROPENSITY_SPREAD`): assignment did not depend on the customer, so leaving someone out did not
   either. A campaign that sent more often to some customers (a targeted design) is not used: its
   left-out customers differ from its recipients, and their own chance of being left out is unknown.
3. The customers who could have received it and did not (known before it, reachable on its channel
   then, no other message from a day before it to the end of its window) are as many as the design
   implies: the implied number left out is `sum over arms (arm size / its probability) / arms - sent`,
   and the pool must be within `POOL_TOLERANCE` of it. A pool much larger means the campaign was
   aimed at a subset we cannot see; much smaller, that the history is incomplete.
4. Recipients and the left-out pool buy alike before the campaign (standardised difference of their
   purchase rates below `BALANCE_SMD`).

A past campaign exported with its holdout as decisions (`Group.HOLDOUT`, kind NOTHING, handed to the
policy before its first decision) names its control explicitly; then only rule 4 is checked.

The difference is measured on money (margin when the purchases carry it, else their value times the
business's margin rate), over the brief's proof window after the campaign (a campaign whose window
has not closed by `now` is not measured yet: a window cut short would read too little), with each
customer's purchase rate before it as a CUPED covariate (`cuped_adjust`). Pure maths over compact
data; no clock reads.
"""

from __future__ import annotations

import math
from array import array
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import MappingProxyType

from autonomy.contracts import Channel, Event, EventType
from autonomy.contracts.events import AttrValue
from autonomy.decide.models import cuped_adjust
from autonomy.learning.observations import LoggedSend

PROPENSITY_SPREAD = 1.05
"""Within one arm, the largest logged probability may be at most this many times the smallest."""
POOL_TOLERANCE = 0.2
"""The left-out pool must be within 20% of the number the design implies."""
BALANCE_SMD = 0.1
"""Largest standardised difference in pre-campaign purchase rate between recipients and the pool."""
MIN_ARM = 50
"""Customers an arm needs (and the control group needs) before its difference is used."""
CONTAMINATION_BEFORE = timedelta(days=1)
"""A left-out customer with any message from a day before the campaign to the end of its window is
not a clean control (another campaign reached them)."""

_OPEN, _CLOSED = True, False
_REACH_TYPES = {
    EventType.CONSENT_GRANTED: _OPEN,
    EventType.CONSENT_REVOKED: _CLOSED,
    EventType.UNSUBSCRIBE: _CLOSED,
    EventType.COMPLAINT: _CLOSED,
}
_NAN = float("nan")


def _ts(at: datetime) -> float:
    return at.timestamp()


_NO_ATTRS: Mapping[str, AttrValue] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class DigestEvent:
    """One purchase, refund, consent change or first sighting rebuilt from a `HistoryDigest`: the
    fields of an `Event` that a learner reading history needs (`autonomy.uplift.features.EventLike`),
    in about a tenth of the memory and time of building an `Event`."""

    event_id: str
    type: EventType
    ts: datetime
    value: float | None = None
    margin: float | None = None
    channel: Channel | None = None

    @property
    def decision_id(self) -> str | None:
        return None

    @property
    def attrs(self) -> Mapping[str, AttrValue]:
        return _NO_ATTRS


@dataclass
class HistoryDigest:
    """What the warm start keeps of imported history besides its outbound events, compactly.

    Purchases are three floats each (time, value, margin or NaN) in one `array` per customer, a few
    dozen bytes instead of an `Event`'s couple of kilobytes, so months of orders fit in memory while
    the history is streamed in. Events are counted once by id (a caller may hand the log twice).
    """

    purchases: dict[str, array[float]] = field(default_factory=dict)
    first_seen: dict[str, float] = field(default_factory=dict)
    reach: dict[tuple[str, str], list[tuple[float, bool]]] = field(default_factory=dict)
    """(customer, channel) -> (time, reachable after it) for consent, revocation and unsubscribe."""
    reach_channels: set[str] = field(default_factory=set)
    """Channels with any consent record: on the others reach is not checked."""
    send_times: dict[str, array[float]] = field(default_factory=dict)
    """Customer -> times of every message sent to them (any channel)."""
    _seen: set[int] = field(default_factory=set)
    data_start: float | None = None
    """The earliest purchase on file."""

    def add(self, e: Event) -> None:
        key = hash(e.event_id)
        if key in self._seen:
            return
        self._seen.add(key)
        t = _ts(e.ts)
        cid = e.customer_id
        first = self.first_seen.get(cid)
        if first is None or t < first:
            self.first_seen[cid] = t
        if e.type is EventType.PURCHASE:
            row = self.purchases.get(cid)
            if row is None:
                row = self.purchases[cid] = array("d")
            row.extend((t, float(e.value or 0.0), _NAN if e.margin is None else float(e.margin)))
            if self.data_start is None or t < self.data_start:
                self.data_start = t
        elif e.type is EventType.REFUND:
            row = self.purchases.get(cid)
            if row is None:
                row = self.purchases[cid] = array("d")
            row.extend((t, -float(e.value or 0.0), _NAN))
        elif e.type is EventType.SEND:
            sends = self.send_times.get(cid)
            if sends is None:
                sends = self.send_times[cid] = array("d")
            sends.append(t)
        if e.type in _REACH_TYPES and e.channel is not None:
            if e.type is not EventType.UNSUBSCRIBE and e.type is not EventType.COMPLAINT:
                self.reach_channels.add(e.channel.value)
            self.reach.setdefault((cid, e.channel.value), []).append((t, _REACH_TYPES[e.type]))

    def extend(self, events: Iterable[Event]) -> None:
        for e in events:
            self.add(e)

    def reachable(self, customer_id: str, channel: Channel, at: float) -> bool:
        """Reachable on `channel` just before `at`: the latest consent change before it opened it.
        A channel with no consent records anywhere counts everyone known as reachable."""
        if channel.value not in self.reach_channels:
            return True
        changes = self.reach.get((customer_id, channel.value))
        if not changes:
            return False
        state = False
        for t, opened in sorted(changes):
            if t >= at:
                break
            state = opened
        return state

    def money(self, customer_id: str, start: float, end: float, margin_rate: float | None) -> float:
        """Money from the customer's purchases in [start, end): margin when known, else value x
        `margin_rate` (None: the value itself, for a revenue objective)."""
        row = self.purchases.get(customer_id)
        if row is None:
            return 0.0
        total = 0.0
        for i in range(0, len(row), 3):
            t = row[i]
            if start <= t < end:
                value, margin = row[i + 1], row[i + 2]
                if margin_rate is None:
                    total += value
                elif math.isnan(margin):
                    total += value * margin_rate
                else:
                    total += margin
        return total

    def rate_before(self, customer_id: str, at: float) -> float:
        """Purchases per day before `at`, over what we can see of the customer (the CUPED covariate)."""
        row = self.purchases.get(customer_id)
        if row is None:
            return 0.0
        count = sum(1 for i in range(0, len(row), 3) if row[i] < at and row[i + 1] > 0)
        start = self.first_seen.get(customer_id, at)
        if self.data_start is not None:
            start = max(start, self.data_start)
        days = max((at - start) / 86_400.0, 30.0)
        return count / days

    def sent_between(self, customer_id: str, start: float, end: float) -> bool:
        sends = self.send_times.get(customer_id)
        return sends is not None and any(start <= t < end for t in sends)

    def as_events(self, kept: Mapping[str, Sequence[Event]]) -> dict[str, list[Event | DigestEvent]]:
        """The history as events again, for a learner that reads events (`autonomy.uplift`): the
        outbound events in `kept` (customer -> events kept whole) plus, rebuilt from the digest as
        compact `DigestEvent` records, each purchase (value and margin), each refund, each consent
        change and one VISIT at the time the customer was first seen. A refund of 0 comes back as a
        purchase of 0, and a consent closed by an unsubscribe or complaint in `kept` is not repeated.
        A record is about a tenth of an `Event`, so months of orders stay small at the warm start.
        """
        out: dict[str, list[Event | DigestEvent]] = {cid: list(evs) for cid, evs in kept.items()}
        closed_by_kept = {
            (e.customer_id, e.channel.value, _ts(e.ts))
            for evs in kept.values()
            for e in evs
            if e.channel is not None and (e.type is EventType.UNSUBSCRIBE or e.type is EventType.COMPLAINT)
        }
        n = 0

        def make(
            kind: EventType,
            t: float,
            value: float | None = None,
            margin: float | None = None,
            channel: Channel | None = None,
        ) -> DigestEvent:
            nonlocal n
            n += 1
            return DigestEvent(f"d{n}", kind, datetime.fromtimestamp(t, UTC), value, margin, channel)

        for cid, t in self.first_seen.items():
            out.setdefault(cid, []).append(make(EventType.VISIT, t))
        for cid, row in self.purchases.items():
            evs = out.setdefault(cid, [])
            for i in range(0, len(row), 3):
                t, value, margin = row[i], row[i + 1], row[i + 2]
                if value < 0:
                    evs.append(make(EventType.REFUND, t, -value))
                else:
                    evs.append(make(EventType.PURCHASE, t, value, None if math.isnan(margin) else margin))
        for (cid, ch), changes in self.reach.items():
            evs = out.setdefault(cid, [])
            channel = Channel(ch)
            for t, opened in changes:
                if opened:
                    evs.append(make(EventType.CONSENT_GRANTED, t, channel=channel))
                elif (cid, ch, t) not in closed_by_kept:
                    evs.append(make(EventType.CONSENT_REVOKED, t, channel=channel))
        return out


@dataclass(frozen=True)
class Arm:
    """One arm of a randomised past campaign: who got it, and the shared control group."""

    campaign: str
    channel: Channel
    action_key: str
    propensity: float
    start: datetime
    end: datetime
    treated: tuple[str, ...]
    control: tuple[str, ...]


@dataclass(frozen=True)
class Difference:
    """Extra money per customer in an arm over its control (CUPED-adjusted), and its standard error."""

    effect: float
    se: float
    n_treated: int
    n_control: int


def _campaign_key(s: LoggedSend) -> str:
    if s.campaign_id:
        return f"{s.channel.value}:{s.campaign_id}"
    return f"{s.channel.value}:{s.sent_at.astimezone(UTC).date().isoformat()}"


def _smd(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) < 2 or len(b) < 2:
        return math.inf
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    va = sum((x - ma) ** 2 for x in a) / (len(a) - 1)
    vb = sum((x - mb) ** 2 for x in b) / (len(b) - 1)
    pooled = math.sqrt((va + vb) / 2.0)
    if pooled <= 0:
        return 0.0 if ma == mb else math.inf
    return abs(ma - mb) / pooled


def randomised_arms(
    sends: Sequence[LoggedSend],
    digest: HistoryDigest,
    *,
    window: timedelta,
    now: datetime,
    customers: Iterable[str] | None = None,
    holdout: Mapping[str, datetime] | None = None,
) -> tuple[list[Arm], list[str]]:
    """The arms of past campaigns whose left-out customers are a randomised control (see the module
    docstring), and one plain sentence per campaign that was not used, saying why.

    `customers` are everyone the history knows (default: everyone in `digest`); `holdout` names a
    past campaign's explicit control (customer -> when it was decided)."""
    known = sorted(set(customers) if customers is not None else set(digest.first_seen))
    by_campaign: dict[str, list[LoggedSend]] = {}
    for s in sends:
        if s.propensity is not None and 0.0 < s.propensity < 1.0:
            by_campaign.setdefault(_campaign_key(s), []).append(s)
    arms: list[Arm] = []
    notes: list[str] = []
    for name in sorted(by_campaign):
        rows = by_campaign[name]
        channel = rows[0].channel
        start = min(s.sent_at for s in rows)
        end = start + window
        if end > now:
            # A window cut short counts only part of the purchases a message brings, with a standard
            # error as tight as a full one's: it would pin the value of a message too low.
            notes.append(f"{name}: its {window.days}-day window has not closed yet")
            continue
        first_arm: dict[str, str] = {}
        props: dict[str, list[float]] = {}
        for s in sorted(rows, key=lambda r: (r.sent_at, r.customer_id)):
            key = s.action_key()
            if s.customer_id in first_arm:
                continue
            first_arm[s.customer_id] = key
            assert s.propensity is not None
            props.setdefault(key, []).append(s.propensity)
        if any(max(p) > PROPENSITY_SPREAD * min(p) for p in props.values()):
            notes.append(f"{name}: sent to some customers more often than others (a targeted design)")
            continue
        t0, t1 = _ts(start), _ts(end)
        explicit = (
            sorted(
                c
                for c, at in holdout.items()
                if abs((at - start).total_seconds()) <= 86_400.0 and c not in first_arm
            )
            if holdout
            else []
        )
        if explicit:
            pool = explicit
        else:
            pool = [
                c
                for c in known
                if c not in first_arm
                and digest.first_seen.get(c, math.inf) < t0
                and digest.reachable(c, channel, t0)
            ]
            implied = sum(len(p) / (sum(p) / len(p)) for p in props.values()) / len(props) - len(first_arm)
            if implied <= 0 or not (
                1.0 / (1.0 + POOL_TOLERANCE) <= len(pool) / implied <= 1.0 + POOL_TOLERANCE
            ):
                notes.append(
                    f"{name}: {len(pool):,} customers could have been sent to and were not, against "
                    f"{max(implied, 0.0):,.0f} implied by its logged probabilities"
                )
                continue
        control = tuple(
            c for c in pool if not digest.sent_between(c, t0 - CONTAMINATION_BEFORE.total_seconds(), t1)
        )
        treated_all = sorted(first_arm)
        balance = _smd(
            [digest.rate_before(c, t0) for c in treated_all], [digest.rate_before(c, t0) for c in control]
        )
        if balance > BALANCE_SMD:
            notes.append(f"{name}: its recipients bought differently before it (difference {balance:.2f} sd)")
            continue
        if len(control) < MIN_ARM:
            notes.append(f"{name}: only {len(control)} clean control customers")
            continue
        for key in sorted(props):
            treated = tuple(c for c in treated_all if first_arm[c] == key)
            if len(treated) < MIN_ARM:
                continue
            p = props[key]
            arms.append(Arm(name, channel, key, sum(p) / len(p), start, end, treated, control))
    return arms, notes


def difference(
    y_treated: Sequence[float],
    y_control: Sequence[float],
    x_treated: Sequence[float],
    x_control: Sequence[float],
) -> Difference | None:
    """Mean outcome of the treated minus the control, CUPED-adjusted on `x` (theta fitted on both
    groups together, so the difference keeps its mean), with its standard error. None with fewer
    than two customers in a group."""
    n_t, n_c = len(y_treated), len(y_control)
    if n_t < 2 or n_c < 2:
        return None
    adjusted = cuped_adjust([*x_treated, *x_control], [*y_treated, *y_control])
    a, h = adjusted[:n_t], adjusted[n_t:]
    ma, mh = sum(a) / n_t, sum(h) / n_c
    va = sum((v - ma) ** 2 for v in a) / (n_t - 1)
    vh = sum((v - mh) ** 2 for v in h) / (n_c - 1)
    return Difference(ma - mh, math.sqrt(va / n_t + vh / n_c), n_t, n_c)


def combine(estimates: Iterable[tuple[float, float]]) -> tuple[float, float] | None:
    """Inverse-variance weighted mean of (estimate, standard error) pairs; None if none is usable."""
    usable = [(m, s) for m, s in estimates if s > 0 and math.isfinite(m) and math.isfinite(s)]
    if not usable:
        return None
    w = [1.0 / (s * s) for _m, s in usable]
    sw = sum(w)
    return sum(wi * m for wi, (m, _s) in zip(w, usable, strict=True)) / sw, math.sqrt(1.0 / sw)


def with_prior(prior: tuple[float, float], measured: tuple[float, float]) -> tuple[float, float]:
    """A prior (mean, sd) and a measurement (mean, se) combined by their precision, floored at 0."""
    k0, sd0 = prior
    k, se = measured
    if se <= 0:
        return max(k, 0.0), 0.0
    if sd0 <= 0:
        return max(k0, 0.0), 0.0
    w0, w = 1.0 / (sd0 * sd0), 1.0 / (se * se)
    return max((k0 * w0 + k * w) / (w0 + w), 0.0), math.sqrt(1.0 / (w0 + w))
