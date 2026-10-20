"""A tiny fake world for tests of code that needs an `Environment`. The real simulator is the Gym (WF2).

Each customer has a daily chance of buying on their own, and a contact on a channel adds a fixed
extra chance for a few days. A contact also carries a small unsubscribe risk that grows with the
number of contacts in the last week. That is all; it exists so `decide()` and the loop can run.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

import numpy as np

from autonomy.contracts import Channel, Decision, DecisionKind, Event, EventType, stable_id

EFFECT_DAYS = 5


class FakeEnvironment:
    def __init__(self, n_customers: int = 200, start: datetime | None = None, history_days: int = 60) -> None:
        if start is None:
            raise ValueError("pass a timezone-aware start time; the fake world never reads the clock")
        self.n = n_customers
        self.start = start
        self.history_days = history_days
        self._now = start
        self._rng = np.random.default_rng(0)
        self._base = np.zeros(0)
        self._uplift: dict[Channel, np.ndarray] = {}
        self._boost_until: dict[int, datetime] = {}
        self._boost: dict[int, float] = {}
        self._unsub: set[tuple[int, Channel]] = set()
        self._recent: dict[int, list[datetime]] = {}
        self._pending: list[Event] = []

    def _cid(self, i: int) -> str:
        return f"c{i:05d}"

    def reset(self, seed: int) -> Sequence[Event]:
        self._rng = np.random.default_rng(seed)
        self._base = self._rng.uniform(0.002, 0.02, self.n)
        self._uplift = {
            Channel.EMAIL: self._rng.normal(0.01, 0.01, self.n),
            Channel.WHATSAPP: self._rng.normal(0.02, 0.02, self.n),
        }
        self._boost_until, self._boost, self._unsub, self._recent, self._pending = {}, {}, set(), {}, []
        history: list[Event] = []
        t0 = self.start - timedelta(days=self.history_days)
        for i in range(self.n):
            cid = self._cid(i)
            history.append(self._event(cid, EventType.SIGNUP, t0, None))
            history.append(self._event(cid, EventType.CONSENT_GRANTED, t0, None, Channel.EMAIL))
            if self._rng.random() < 0.5:
                history.append(self._event(cid, EventType.CONSENT_GRANTED, t0, None, Channel.WHATSAPP))
        self._now = t0
        history.extend(self._simulate_days(self.start))
        return sorted(history, key=lambda e: e.ts)

    def now(self) -> datetime:
        return self._now

    def act(self, decisions: Sequence[Decision]) -> None:
        for d in decisions:
            if d.kind is not DecisionKind.ACT or d.action is None:
                continue
            i = int(d.customer_id[1:])
            ch = d.action.channel
            if (i, ch) in self._unsub:
                continue
            at = max(d.action.send_at, self._now)
            self._pending.append(self._event(d.customer_id, EventType.SEND, at, d.decision_id, ch))
            self._pending.append(
                self._event(d.customer_id, EventType.DELIVER, at + timedelta(minutes=1), d.decision_id, ch)
            )
            recent = [x for x in self._recent.get(i, []) if x > at - timedelta(days=7)] + [at]
            self._recent[i] = recent
            if self._rng.random() < 0.003 * len(recent) ** 2:
                self._unsub.add((i, ch))
                self._pending.append(
                    self._event(
                        d.customer_id, EventType.UNSUBSCRIBE, at + timedelta(hours=2), d.decision_id, ch
                    )
                )
                continue
            lift = float(self._uplift.get(ch, np.zeros(self.n))[i])
            self._boost[i] = lift
            self._boost_until[i] = at + timedelta(days=EFFECT_DAYS)
            if self._rng.random() < 0.05 + max(lift, 0) * 3:
                self._pending.append(
                    self._event(d.customer_id, EventType.CLICK, at + timedelta(hours=1), d.decision_id, ch)
                )

    def advance(self, until: datetime) -> Sequence[Event]:
        events = self._simulate_days(until)
        due = [e for e in self._pending if e.ts < until]
        self._pending = [e for e in self._pending if e.ts >= until]
        return sorted(events + due, key=lambda e: e.ts)

    def _simulate_days(self, until: datetime) -> list[Event]:
        out: list[Event] = []
        while self._now + timedelta(days=1) <= until:
            day = self._now
            p = self._base.copy()
            for i, end in self._boost_until.items():
                if day < end:
                    p[i] = min(1.0, max(0.0, p[i] + self._boost[i]))
            buyers = np.nonzero(self._rng.random(self.n) < p)[0]
            for j in buyers.tolist():
                i = int(j)
                ts = day + timedelta(hours=int(self._rng.integers(8, 22)))
                value = float(round(self._rng.gamma(2.0, 30.0), 2))
                out.append(
                    Event(
                        event_id=stable_id("ev", "purchase", i, ts.isoformat()),
                        customer_id=self._cid(i),
                        type=EventType.PURCHASE,
                        ts=ts,
                        source="fake",
                        value=value,
                        margin=round(value * 0.4, 2),
                        category="general",
                    )
                )
            self._now = day + timedelta(days=1)
        return out

    def _event(
        self, cid: str, t: EventType, ts: datetime, decision_id: str | None, channel: Channel | None = None
    ) -> Event:
        return Event(
            event_id=stable_id("ev", t.value, cid, ts.isoformat(), decision_id, channel),
            customer_id=cid,
            type=t,
            ts=ts,
            source="fake",
            channel=channel,
            decision_id=decision_id,
        )
