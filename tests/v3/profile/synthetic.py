"""A deterministic, shop-like event stream for the profile tests and the scale bench.

Every customer's events come from `random.Random(f"{seed}:{index}")`, so a customer's history does not
depend on how many others are generated, and a million customers can be streamed without holding them.
The mix is plain on purpose (orders with categories, items, offers and margins; views and carts; sends,
deliveries, opens, clicks and replies on email and WhatsApp; some refunds, complaints, unsubscribes and
preferences). It makes no claim to look like any real shop.
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

from autonomy.contracts import Channel, Event, EventType

START = datetime(2024, 1, 1, tzinfo=UTC)
END = datetime(2025, 12, 31, tzinfo=UTC)
CATEGORIES = tuple(f"cat{i:02d}" for i in range(12))
ZONES = ("Asia/Kolkata", "Europe/London", "America/New_York", "UTC")


def customer_id(index: int) -> str:
    return f"c{index:08d}"


def customer_events(index: int, seed: int = 0) -> list[Event]:
    """One customer's events, in time order."""
    rnd = random.Random(f"{seed}:{index}")
    cid = customer_id(index)
    span = (END - START).total_seconds()
    first = START + timedelta(seconds=rnd.random() * span * 0.8)
    zone = rnd.choice(ZONES)
    out: list[Event] = []

    def add(kind: EventType, ts: datetime, **kw: object) -> None:
        out.append(
            Event(
                event_id=f"{cid}-{len(out)}",
                customer_id=cid,
                type=kind,
                ts=ts,
                source="synthetic",
                **kw,  # type: ignore[arg-type]
            )
        )

    add(EventType.SIGNUP, first, attrs={"timezone": zone, "country": "IN"})
    channels = [Channel.EMAIL] + ([Channel.WHATSAPP] if rnd.random() < 0.5 else [])
    for ch in channels:
        add(EventType.CONSENT_GRANTED, first, channel=ch)
    now = first
    for _ in range(rnd.choice((0, 0, 1, 1, 2, 3, 5, 8))):
        now += timedelta(days=rnd.expovariate(1 / 25), seconds=rnd.randrange(86_400))
        if now > END:
            break
        if rnd.random() < 0.7:
            add(EventType.VIEW, now - timedelta(minutes=20), item_id=f"item{rnd.randrange(60)}")
        if rnd.random() < 0.3:
            add(EventType.CART, now - timedelta(minutes=10), item_id=f"item{rnd.randrange(60)}")
        value = round(rnd.lognormvariate(3.5, 0.7), 2)
        offered = rnd.random() < 0.25
        add(
            EventType.PURCHASE,
            now,
            value=value,
            margin=round(value * 0.3, 2) if rnd.random() < 0.8 else None,
            category=rnd.choice(CATEGORIES[:5]) if rnd.random() < 0.9 else None,
            item_id=f"item{rnd.randrange(60)}",
            attrs={"offer_id": "welcome10"} if offered else {},
        )
        if rnd.random() < 0.06:
            add(EventType.REFUND, now + timedelta(days=3), value=round(value * 0.5, 2))
    sent = first
    for _ in range(rnd.choice((0, 2, 4, 6))):
        sent += timedelta(days=rnd.uniform(3, 20), seconds=rnd.randrange(86_400))
        ch = rnd.choice(channels)
        add(EventType.SEND, sent, channel=ch)
        if rnd.random() < 0.95:
            add(EventType.DELIVER, sent + timedelta(seconds=5), channel=ch)
            if rnd.random() < 0.4:
                add(EventType.OPEN, sent + timedelta(minutes=3), channel=ch)
            if rnd.random() < 0.1:
                add(EventType.CLICK, sent + timedelta(hours=rnd.uniform(0.1, 30)), channel=ch)
            if rnd.random() < 0.02:
                add(EventType.REPLY, sent + timedelta(hours=2), channel=ch, text="thanks")
        else:
            add(EventType.BOUNCE, sent + timedelta(seconds=9), channel=ch)
    if rnd.random() < 0.02:
        add(EventType.COMPLAINT, sent + timedelta(days=1), channel=channels[0])
    if rnd.random() < 0.03:
        add(EventType.PREFERENCE, sent + timedelta(days=2), attrs={"key": "no_discounts", "value": "true"})
    out.sort(key=lambda e: e.ts)
    return out


def events(customers: int, seed: int = 0) -> Iterator[Event]:
    """Every customer's events, customer after customer (not in global time order)."""
    for i in range(customers):
        yield from customer_events(i, seed)
