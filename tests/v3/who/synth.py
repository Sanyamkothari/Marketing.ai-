"""Small deterministic shops for the who-to-contact tests. Plain on purpose; no claim to look real."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from autonomy.contracts import Event, EventType

START = datetime(2025, 6, 1, tzinfo=UTC)
END = datetime(2026, 6, 30, 23, 59, tzinfo=UTC)


def purchase(cid: str, ts: datetime, value: float = 50.0, tag: str = "") -> Event:
    return Event(
        event_id=f"{cid}-p-{ts.isoformat()}{tag}",
        customer_id=cid,
        type=EventType.PURCHASE,
        ts=ts,
        source="test",
        value=value,
        margin=value * 0.4,
    )


def shop(n: int = 500, seed: int = 0, *, leavers: float = 0.4, end: datetime = END) -> list[Event]:
    """Customers who buy about every 20-40 days at their own order size. A share of them slow down
    (each gap 1.3 times the last) and then stop for good, so slowing and a long silence tell the
    leavers from the stayers."""
    rnd = random.Random(seed)
    out: list[Event] = []
    for i in range(n):
        cid = f"c{i:05d}"
        first = START + timedelta(days=rnd.random() * 200)
        gap = 20 + 20 * rnd.random()
        size = 30 + 90 * rnd.random()
        leaves = rnd.random() < leavers
        quits = first + timedelta(days=120 + 250 * rnd.random())
        slowing = False
        when = first
        out.append(
            Event(event_id=f"{cid}-s", customer_id=cid, type=EventType.SIGNUP, ts=first, source="test")
        )
        while True:
            when += timedelta(days=rnd.expovariate(1.0 / gap))
            if when > end:
                break
            if leaves:
                if when > quits:
                    break
                if when > quits - timedelta(days=100):
                    slowing = True
            if slowing:
                gap *= 1.3
            out.append(purchase(cid, when, size * (0.7 + 0.6 * rnd.random())))
    return out
