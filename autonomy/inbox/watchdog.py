"""The Data Doctor as a quiet watchdog over connected data: silent unless the data should not be trusted.

The connector health checks (`autonomy.connectors.health`) say whether each sync *ran*: fresh,
history loaded, no error streak. A sync can run and still bring back the wrong data. This watchdog
reads what actually arrived (the event log, customer state and the identity links) against **the
shop's own normal**, and has three levels:

* **hold**: the data should not be trusted to act on. It feeds `HealthMonitor.ready_to_act` (the
  runtime's data gate), so nothing is decided or sent, and it is one inbox item (`data`).
* **notice**: the data is usable, but a person must look (one inbox item, no hold).
* **note**: worth a line in the weekly briefing; never a page.

**What it checks** (`WatchdogPolicy` holds every threshold):

| Check | How | Level |
|---|---|---|
| Missing days | A complete local day in the last 3 with **no** purchases (or other customer events) where the shop's normal makes zero all but impossible: Poisson P(0 \\| normal) < 1 in 10,000. The normal is the smaller of the median day of the last 28 and the median of the same weekday over the last 4 weeks; it needs at least 5 events a day, so a small shop is never held on a quiet day. Customers who bought that day would look lapsed. | hold (purchases), note (others) |
| Row-count drops | The same test for a day far below normal: under 20% of it is a hold for purchases, under 50% a note. | hold / note |
| History gaps | A zero day older than the last 3 (up to 90 days back). | note |
| Schema changes | Over the last 3 days against the 28 before (at least 30 events): the customer id changes form (digits to anything else, or back) -> hold, since every new event would be a new customer; a field present on 90% of purchases before is on fewer than half now -> note. | hold / note |
| Consent coverage | The share of reachable customers who can be contacted, once a day, against 7 days before: down 20% or more -> notice (the list or its sync shrank); up 30% or more -> hold (messages could reach people whose consent is not real). | notice / hold |
| Duplicate customers | From the identity links: Klaviyo profiles that match more than one Shopify customer, plus Shopify customers sharing an email. Over 5% of customers (and at least 50) -> notice: the same person can be counted in two proof groups and contacted twice. | notice |

A person can vouch for a finding ("it was a quiet day", "this data is right"): `acknowledge` stops
it holding or showing, for that exact finding only. Findings are recomputed at most once per
`refresh_every` (an hour); the log is scanned over 31 days for the recent checks and over 90 days
once a day for history gaps.
"""

from __future__ import annotations

import math
import statistics
import threading
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, Field
from sqlalchemy import Column, Float, MetaData, String, Table, func, select
from sqlalchemy.engine import Engine

from autonomy.contracts import Contract, EventType, stable_id
from autonomy.interfaces import EventStore, StateStore
from autonomy.store.schema import UtcDateTime
from autonomy.store.sql import upsert

__all__ = [
    "DataWatchdog",
    "Finding",
    "FindingCode",
    "InMemoryWatchdogStore",
    "Level",
    "SqlWatchdogStore",
    "WatchdogPolicy",
    "WatchdogStore",
    "poisson_cdf",
]

FindingCode = Literal[
    "missing_day", "row_drop", "history_gap", "schema_change", "consent_drop", "consent_jump", "duplicates"
]
Level = Literal["hold", "notice", "note"]

CUSTOMER_TYPES: frozenset[EventType] = frozenset(
    {
        EventType.SIGNUP,
        EventType.VISIT,
        EventType.VIEW,
        EventType.CART,
        EventType.PURCHASE,
        EventType.REFUND,
        EventType.CONSENT_GRANTED,
        EventType.CONSENT_REVOKED,
    }
)
"""What the shop's tools tell us customers did. Our own sends' events are not connected data."""

_TYPE_WORDS: dict[EventType, str] = {
    EventType.PURCHASE: "orders",
    EventType.SIGNUP: "new customers",
    EventType.VISIT: "visits",
    EventType.VIEW: "product views",
    EventType.CART: "carts",
    EventType.REFUND: "refunds",
    EventType.CONSENT_GRANTED: "consent updates",
    EventType.CONSENT_REVOKED: "unsubscribes",
}
_FIELDS: tuple[str, ...] = ("margin", "category", "item_id")


@dataclass(frozen=True)
class WatchdogPolicy:
    timezone: str = "UTC"
    normal_days: int = 28
    check_days: int = 3
    history_days: int = 90
    p_alarm: float = 1e-4
    drop_ratio: float = 0.5
    hold_ratio: float = 0.2
    min_normal: float = 5.0
    held_types: frozenset[EventType] = frozenset({EventType.PURCHASE})
    schema_min_events: int = 30
    schema_present: float = 0.9
    schema_lost: float = 0.5
    consent_min_customers: int = 200
    consent_window_days: int = 7
    consent_drop: float = 0.2
    consent_jump: float = 0.3
    consent_min_points: float = 0.02
    duplicate_share: float = 0.05
    duplicate_min: int = 50
    refresh_every: timedelta = timedelta(hours=1)


class Finding(Contract):
    code: FindingCode
    level: Level
    scope: str = Field(description="What it is about, e.g. 'shopify orders', 'consent', 'customers'")
    sentence: str
    detail: str = ""
    since: AwareDatetime = Field(description="The start of the first day affected")
    fingerprint: str = Field(description="This exact finding; acknowledging it silences only it")
    numbers: dict[str, float] = Field(default_factory=dict)


def poisson_cdf(x: int, lam: float) -> float:
    """P(X <= x) for X ~ Poisson(lam), summed in log space (exact for the small x tested here)."""
    if x < 0:
        return 0.0
    if lam <= 0:
        return 1.0
    log_lam = math.log(lam)
    total = 0.0
    for k in range(x + 1):
        total += math.exp(-lam + k * log_lam - math.lgamma(k + 1))
    return min(1.0, total)


# --- snapshots and acknowledgements -------------------------------------------------------------------


class WatchdogStore(Protocol):
    def put_snapshot(self, business_id: str, day: date, metric: str, value: float) -> None: ...

    def snapshot(self, business_id: str, day: date, metric: str) -> float | None:
        """The value recorded for `day`, else the latest one before it, else None."""
        ...

    def acknowledge(self, business_id: str, fingerprint: str, at: datetime, by: str) -> None: ...

    def acknowledged(self, business_id: str) -> frozenset[str]: ...


class InMemoryWatchdogStore:
    def __init__(self) -> None:
        self._snaps: dict[tuple[str, str], dict[date, float]] = defaultdict(dict)
        self._acks: dict[str, set[str]] = defaultdict(set)

    def put_snapshot(self, business_id: str, day: date, metric: str, value: float) -> None:
        self._snaps[(business_id, metric)][day] = value

    def snapshot(self, business_id: str, day: date, metric: str) -> float | None:
        days = [d for d in self._snaps.get((business_id, metric), {}) if d <= day]
        return self._snaps[(business_id, metric)][max(days)] if days else None

    def acknowledge(self, business_id: str, fingerprint: str, at: datetime, by: str) -> None:  # noqa: ARG002
        self._acks[business_id].add(fingerprint)

    def acknowledged(self, business_id: str) -> frozenset[str]:
        return frozenset(self._acks.get(business_id, set()))


watchdog_metadata = MetaData()

snapshot_table = Table(
    "watchdog_snapshot",
    watchdog_metadata,
    Column("business_id", String(200), primary_key=True),
    Column("metric", String(100), primary_key=True),
    Column("day", String(10), primary_key=True, doc="ISO date, local to the business"),
    Column("value", Float, nullable=False),
)

ack_table = Table(
    "watchdog_ack",
    watchdog_metadata,
    Column("business_id", String(200), primary_key=True),
    Column("fingerprint", String(200), primary_key=True),
    Column("at", UtcDateTime(), nullable=False),
    Column("by", String(200), nullable=False),
)


class SqlWatchdogStore:
    def __init__(self, engine: Engine, *, create: bool = True) -> None:
        self._engine = engine
        if create:
            watchdog_metadata.create_all(engine)

    def put_snapshot(self, business_id: str, day: date, metric: str, value: float) -> None:
        row = {"business_id": business_id, "metric": metric, "day": day.isoformat(), "value": float(value)}
        with self._engine.begin() as conn:
            upsert(conn, snapshot_table, [row], keep=())

    def snapshot(self, business_id: str, day: date, metric: str) -> float | None:
        t = snapshot_table.c
        query = (
            select(t.value)
            .where(t.business_id == business_id, t.metric == metric, t.day <= day.isoformat())
            .order_by(t.day.desc())
            .limit(1)
        )
        with self._engine.connect() as conn:
            value = conn.execute(query).scalar()
        return None if value is None else float(value)

    def acknowledge(self, business_id: str, fingerprint: str, at: datetime, by: str) -> None:
        row = {"business_id": business_id, "fingerprint": fingerprint, "at": at, "by": by}
        with self._engine.begin() as conn:
            upsert(conn, ack_table, [row], keep=())

    def acknowledged(self, business_id: str) -> frozenset[str]:
        t = ack_table.c
        with self._engine.connect() as conn:
            return frozenset(
                str(f)
                for f in conn.execute(select(t.fingerprint).where(t.business_id == business_id)).scalars()
            )


# --- the identity links -------------------------------------------------------------------------------


class IdentityCounts(Protocol):
    """What the duplicate check reads (`autonomy.connectors.identity.IdentityStore` fits)."""

    def summary(self, business_id: str) -> Any: ...


def shopify_shared_emails(identity: Any, business_id: str) -> int:
    """Shopify customers whose email another Shopify customer also has (beyond the first of each).

    Reads the identity store's own fact table (hashes only); 0 when the store has none."""
    engine = getattr(identity, "engine", None)
    if not isinstance(engine, Engine):
        return 0
    from autonomy.connectors.identity import SHOPIFY, fact_table

    f = fact_table.c
    shared = (
        select(func.count().label("n"))
        .where(f.business_id == business_id, f.source == SHOPIFY, f.email_hash.is_not(None))
        .group_by(f.email_hash)
        .subquery()
    )
    with engine.connect() as conn:
        extra = conn.execute(select(func.coalesce(func.sum(shared.c.n - 1), 0))).scalar()
    return int(extra or 0)


# --- the watchdog -----------------------------------------------------------------------------------


@dataclass
class _Scan:
    """Daily counts per (source, type), and field coverage for the held types, from one log scan."""

    counts: Counter[tuple[date, str, EventType]] = field(default_factory=Counter)
    first_day: dict[tuple[str, EventType], date] = field(default_factory=dict)
    shape: Counter[tuple[str, EventType, bool, bool]] = field(default_factory=Counter)
    """(source, type, recent?, digits-only id?) -> events."""
    fields: Counter[tuple[str, EventType, bool, str]] = field(default_factory=Counter)
    """(source, type, recent?, field) -> events carrying it; field '' counts all events."""


class DataWatchdog:
    """One business's watchdog. `findings(now)` is everything it sees; `holds(business_id, now)` the
    reasons to hold (what `HealthMonitor.ready_to_act` adds), after a person's acknowledgements."""

    def __init__(
        self,
        business_id: str,
        events: EventStore,
        *,
        states: StateStore | None = None,
        identity: IdentityCounts | None = None,
        store: WatchdogStore | None = None,
        policy: WatchdogPolicy | None = None,
    ) -> None:
        self.business_id = business_id
        self.events = events
        self.states = states
        self.identity = identity
        self.store: WatchdogStore = store if store is not None else InMemoryWatchdogStore()
        self.policy = policy or WatchdogPolicy()
        self._tz = ZoneInfo(self.policy.timezone)
        self._cache: tuple[datetime, list[Finding]] | None = None
        self._history: tuple[date, list[Finding]] | None = None
        self._snapshot_day: date | None = None
        self._lock = threading.Lock()

    # --- the answers -----------------------------------------------------------------------------

    def findings(self, now: datetime) -> list[Finding]:
        """Every current finding (acknowledged ones left out), worst first."""
        with self._lock:
            cached = self._cache
            if cached is not None and timedelta(0) <= now - cached[0] < self.policy.refresh_every:
                found = cached[1]
            else:
                found = self._compute(now)
                self._cache = (now, found)
        acked = self.store.acknowledged(self.business_id)
        order = {"hold": 0, "notice": 1, "note": 2}
        return sorted(
            (f for f in found if f.fingerprint not in acked), key=lambda f: (order[f.level], f.since, f.code)
        )

    def holds(self, business_id: str, now: datetime) -> list[str]:
        """Plain reasons the business must not act now (the `WatchdogCheck` health.py asks)."""
        if business_id != self.business_id:
            return []
        return [f.sentence for f in self.findings(now) if f.level == "hold"]

    def trusted(self, now: datetime) -> bool:
        return not self.holds(self.business_id, now)

    def acknowledge(self, fingerprint: str, now: datetime, *, by: str) -> None:
        """A person vouches for this finding: it no longer holds or shows.

        A consent finding is vouched for at its coverage: the same jump (or drop) is not raised again
        while it is in the 7-day comparison, even as the share moves a little, unless it moves further
        the same way. (Its fingerprint rounds the share, so without this a jump from 74% to 76% would
        be a new finding and hold every decision again right after the person said it is right.)"""
        found = next((f for f in self.findings(now) if f.fingerprint == fingerprint), None)
        self.store.acknowledge(self.business_id, fingerprint, now, by)
        if found is not None and found.code in ("consent_jump", "consent_drop"):
            today = now.astimezone(self._tz).date()
            self.store.put_snapshot(self.business_id, today, f"ack_{found.code}_cov", found.numbers["now"])
            self.store.put_snapshot(
                self.business_id, today, f"ack_{found.code}_day", float(today.toordinal())
            )

    def invalidate(self) -> None:
        """Forget the cached findings (new data arrived that should be judged now)."""
        with self._lock:
            self._cache = None
            self._history = None

    # --- the checks ------------------------------------------------------------------------------

    def _compute(self, now: datetime) -> list[Finding]:
        p = self.policy
        today = now.astimezone(self._tz).date()
        recent_start = today - timedelta(days=p.check_days)
        scan = self._scan(today - timedelta(days=p.check_days + p.normal_days), today, recent_start)
        found = list(self._gaps(scan, today, recent_days=True))
        found += self._schema(scan, recent_start)
        if self._history is None or self._history[0] != today:
            older = self._scan(
                today - timedelta(days=p.history_days + p.normal_days), recent_start, recent_start
            )
            self._history = (today, list(self._gaps(older, recent_start, recent_days=False)))
        found += self._history[1]
        found += self._consent(today)
        found += self._duplicates(now)
        return found

    def _scan(self, start: date, end: date, recent_start: date) -> _Scan:
        """Count connected customer events per local day in `[start, end)`."""
        out = _Scan()
        since = datetime.combine(start, time(0), tzinfo=self._tz)
        until = datetime.combine(end, time(0), tzinfo=self._tz)
        for e in self.events.scan(since=since, until=until):
            if e.decision_id is not None or e.type not in CUSTOMER_TYPES:
                continue
            day = e.ts.astimezone(self._tz).date()
            if not start <= day < end:
                continue
            out.counts[(day, e.source, e.type)] += 1
            key = (e.source, e.type)
            if key not in out.first_day or day < out.first_day[key]:
                out.first_day[key] = day
            if e.type in self.policy.held_types:
                recent = day >= recent_start
                out.shape[(e.source, e.type, recent, e.customer_id.isdigit())] += 1
                out.fields[(e.source, e.type, recent, "")] += 1
                for name in _FIELDS:
                    if getattr(e, name, None) is not None:
                        out.fields[(e.source, e.type, recent, name)] += 1
                for name in e.attrs:
                    out.fields[(e.source, e.type, recent, f"attrs.{name}")] += 1
        return out

    def _normal(self, scan: _Scan, day: date, source: str, kind: EventType) -> float | None:
        p = self.policy
        first = scan.first_day.get((source, kind))
        if first is None:
            return None
        window = [day - timedelta(days=i) for i in range(1, p.normal_days + 1)]
        window = [d for d in window if d >= first]
        if len(window) < 7:
            return None  # less than a week of this stream: no normal yet
        overall = statistics.median(scan.counts[(d, source, kind)] for d in window)
        weekly = [day - timedelta(days=7 * i) for i in range(1, 5)]
        same = [scan.counts[(d, source, kind)] for d in weekly if d >= first]
        normal = min(overall, statistics.median(same)) if len(same) == 4 else overall
        return float(normal)

    def _gaps(self, scan: _Scan, end: date, *, recent_days: bool) -> Iterable[Finding]:
        p = self.policy
        streams = sorted({(s, k) for (_, s, k) in scan.counts}, key=lambda sk: (sk[0], sk[1].value))
        days_back = p.check_days if recent_days else p.history_days
        for source, kind in streams:
            held = kind in p.held_types
            what = f"{source} {_TYPE_WORDS.get(kind, kind.value)}"
            bad: list[tuple[date, int, float, float]] = []
            for i in range(1, days_back + 1):
                day = end - timedelta(days=i)
                first = scan.first_day.get((source, kind))
                if first is None or day < first:
                    continue
                lam = self._normal(scan, day, source, kind)
                if lam is None or lam < p.min_normal:
                    continue
                x = scan.counts[(day, source, kind)]
                if x >= p.drop_ratio * lam:
                    continue
                pval = poisson_cdf(x, lam)
                if pval >= p.p_alarm:
                    continue
                bad.append((day, x, lam, pval))
            if not bad:
                continue
            bad.sort()
            if recent_days:
                for day, x, lam, pval in bad:
                    yield self._day_finding(day, x, lam, pval, what, held)
            else:
                zero = [b for b in bad if b[1] == 0]
                if zero:
                    days = [b[0] for b in zero]
                    yield Finding(
                        code="history_gap",
                        level="note",
                        scope=what,
                        sentence=f"{len(days)} older day{'s' if len(days) != 1 else ''} of {what} "
                        f"{'are' if len(days) != 1 else 'is'} missing, from {days[0]:%d %b}.",
                        detail="Days with none at all where the shop's normal makes that all but impossible. "
                        "Proof and learning read around them; a re-sync of history would fill them.",
                        since=datetime.combine(days[0], time(0), tzinfo=self._tz),
                        fingerprint=stable_id("wd", self.business_id, "history_gap", what, *days),
                        numbers={"days": float(len(days))},
                    )

    def _day_finding(self, day: date, x: int, lam: float, pval: float, what: str, held: bool) -> Finding:
        p = self.policy
        missing = x == 0
        severe = missing or x < p.hold_ratio * lam
        level: Level = "hold" if held and severe else "note"
        when = f"{day:%a %d %b}"
        if missing:
            sentence = f"No {what} arrived for {when}, against a usual {lam:.0f} a day."
        else:
            sentence = f"Only {x} {what} arrived for {when}, against a usual {lam:.0f} a day."
        if level == "hold":
            sentence += " Decisions wait until it fills in."
        return Finding(
            code="missing_day" if missing else "row_drop",
            level=level,
            scope=what,
            sentence=sentence,
            detail=(
                f"The usual day is the smaller of the median of the last {p.normal_days} days and the median of "
                f"the same weekday over the last 4 weeks. A day this low would happen less than once in "
                f"{1 / max(pval, 1e-300):,.0f} if nothing were wrong (Poisson). "
                + (
                    "Customers who bought that day would look as if they had not, so the agent could message "
                    "people who just ordered. If the shop really was closed or quiet, say so and it goes on."
                    if level == "hold"
                    else "Counted in the weekly briefing."
                )
            ),
            since=datetime.combine(day, time(0), tzinfo=self._tz),
            fingerprint=stable_id("wd", self.business_id, "day", what, day.isoformat()),
            numbers={"count": float(x), "usual": round(lam, 2), "p": pval},
        )

    def _schema(self, scan: _Scan, recent_start: date) -> list[Finding]:
        p = self.policy
        out: list[Finding] = []
        streams = sorted({(s, k) for (s, k, _, _) in scan.shape}, key=lambda sk: (sk[0], sk[1].value))
        for source, kind in streams:
            what = f"{source} {_TYPE_WORDS.get(kind, kind.value)}"
            base_n = scan.fields[(source, kind, False, "")]
            new_n = scan.fields[(source, kind, True, "")]
            if base_n < p.schema_min_events or new_n < p.schema_min_events:
                continue
            base_digits = scan.shape[(source, kind, False, True)] / base_n
            new_digits = scan.shape[(source, kind, True, True)] / new_n
            if (base_digits >= p.schema_present and new_digits < p.schema_lost) or (
                base_digits <= 1 - p.schema_present and new_digits > 1 - p.schema_lost
            ):
                out.append(
                    Finding(
                        code="schema_change",
                        level="hold",
                        scope=what,
                        sentence=f"The customer id on {what} changed form in the last {p.check_days} days. "
                        "Decisions wait until it is checked.",
                        detail=(
                            f"{base_digits:.0%} of {what} before carried a numeric customer id, {new_digits:.0%} now. "
                            "Every event under a new form of id would start a new customer: their history, "
                            "caps and proof group would be lost."
                        ),
                        since=datetime.combine(recent_start, time(0), tzinfo=self._tz),
                        fingerprint=stable_id("wd", self.business_id, "id_shape", what, round(new_digits, 1)),
                        numbers={"before": round(base_digits, 4), "now": round(new_digits, 4)},
                    )
                )
            names = sorted({n for (s, k, r, n) in scan.fields if s == source and k == kind and n})
            lost = [
                n
                for n in names
                if scan.fields[(source, kind, False, n)] / base_n >= p.schema_present
                and scan.fields[(source, kind, True, n)] / new_n < p.schema_lost
            ]
            if lost:
                out.append(
                    Finding(
                        code="schema_change",
                        level="note",
                        scope=what,
                        sentence=f"{what.capitalize()} stopped carrying {', '.join(lost)}.",
                        detail="Fields present on at least 90% of them before are on fewer than half now; "
                        "the platform's data may have changed. Decisions go on without them.",
                        since=datetime.combine(recent_start, time(0), tzinfo=self._tz),
                        fingerprint=stable_id("wd", self.business_id, "fields", what, *lost),
                        numbers={"fields": float(len(lost))},
                    )
                )
        return out

    def _consent(self, today: date) -> list[Finding]:
        p = self.policy
        if self.states is None:
            return []
        if self._snapshot_day != today:
            reachable = contactable = 0
            for s in self.states.scan():
                known = [c for c in s.channels.values() if c.address_known]
                if not known:
                    continue
                reachable += 1
                if any(c.contactable for c in known):
                    contactable += 1
            if reachable:
                self.store.put_snapshot(self.business_id, today, "consent_coverage", contactable / reachable)
                self.store.put_snapshot(self.business_id, today, "reachable", float(reachable))
            self._snapshot_day = today
        now_cov = self.store.snapshot(self.business_id, today, "consent_coverage")
        reach = self.store.snapshot(self.business_id, today, "reachable") or 0.0
        then_day = today - timedelta(days=p.consent_window_days)
        then_cov = self.store.snapshot(self.business_id, then_day, "consent_coverage")
        if now_cov is None or then_cov is None or then_cov <= 0 or reach < p.consent_min_customers:
            return []
        change = (now_cov - then_cov) / then_cov
        if abs(now_cov - then_cov) < p.consent_min_points:
            return []
        numbers = {"now": round(now_cov, 4), "before": round(then_cov, 4), "customers": reach}
        if change <= -p.consent_drop and not self._vouched("consent_drop", today, now_cov):
            return [
                Finding(
                    code="consent_drop",
                    level="notice",
                    scope="consent",
                    sentence=f"Customers who can be messaged fell from {then_cov:.0%} to {now_cov:.0%} in "
                    f"{p.consent_window_days} days.",
                    detail="A list that shrinks this fast is usually a consent sync or a list clean-up in the "
                    "email tool, not customers leaving. Autonomy keeps to the consent it sees, so it reaches "
                    "fewer people until it is checked.",
                    since=datetime.combine(then_day, time(0), tzinfo=self._tz),
                    fingerprint=stable_id("wd", self.business_id, "consent_drop", round(now_cov, 1)),
                    numbers=numbers,
                )
            ]
        if change >= p.consent_jump and not self._vouched("consent_jump", today, now_cov):
            return [
                Finding(
                    code="consent_jump",
                    level="hold",
                    scope="consent",
                    sentence=f"Customers who can be messaged jumped from {then_cov:.0%} to {now_cov:.0%} in "
                    f"{p.consent_window_days} days. Decisions wait until it is checked.",
                    detail="Consent does not grow this fast on its own; an import or a sync that marks everyone "
                    "subscribed would let messages reach people who never agreed. Check the email tool's "
                    "consent, then trust it here.",
                    since=datetime.combine(then_day, time(0), tzinfo=self._tz),
                    fingerprint=stable_id("wd", self.business_id, "consent_jump", round(now_cov, 1)),
                    numbers=numbers,
                )
            ]
        return []

    def _vouched(self, code: str, today: date, coverage: float) -> bool:
        """A person vouched for this consent change within the comparison window, and it has not moved
        further the same way since (by more than `consent_min_points`)."""
        p = self.policy
        day = self.store.snapshot(self.business_id, today, f"ack_{code}_day")
        at = self.store.snapshot(self.business_id, today, f"ack_{code}_cov")
        if day is None or at is None or today.toordinal() - int(day) > p.consent_window_days:
            return False
        if code == "consent_jump":
            return coverage <= at + p.consent_min_points
        return coverage >= at - p.consent_min_points

    def _duplicates(self, now: datetime) -> list[Finding]:
        p = self.policy
        if self.identity is None:
            return []
        summary = self.identity.summary(self.business_id)
        customers = int(getattr(summary, "shopify_customers", 0))
        conflicts = int(getattr(summary, "conflicts", 0))
        shared = shopify_shared_emails(self.identity, self.business_id)
        dupes = conflicts + shared
        if customers <= 0 or dupes < p.duplicate_min or dupes / customers < p.duplicate_share:
            return []
        share = dupes / customers
        return [
            Finding(
                code="duplicates",
                level="notice",
                scope="customers",
                sentence=f"About {share:.0%} of customers ({dupes:,}) appear twice across Shopify and Klaviyo.",
                detail=(
                    f"{conflicts:,} Klaviyo profiles match more than one Shopify customer and {shared:,} Shopify "
                    "customers share an email with another, so they are kept apart. The same person can then be "
                    "in two proof groups and reach the contact cap twice. Merging the duplicate customers in "
                    "Shopify clears it."
                ),
                since=now,
                fingerprint=stable_id("wd", self.business_id, "duplicates", math.floor(share * 20)),
                numbers={"duplicates": float(dupes), "customers": float(customers), "share": round(share, 4)},
            )
        ]
