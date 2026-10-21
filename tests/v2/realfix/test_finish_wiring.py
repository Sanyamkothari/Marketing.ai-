"""The fix wave wired together: the warm start's compact history comes back as the events the uplift
model reads (`HistoryDigest.as_events`)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from autonomy.contracts import Channel, Event, EventType
from autonomy.learning.calibration import DigestEvent, HistoryDigest
from autonomy.uplift.features import behaviour_from_events

T0 = datetime(2024, 3, 1, 9, tzinfo=UTC)


def _ev(i: int, kind: EventType, at: datetime, **kw: object) -> Event:
    return Event(event_id=f"e{i}", customer_id="a", type=kind, ts=at, source="t", **kw)  # type: ignore[arg-type]


def test_digest_gives_back_what_the_uplift_model_reads() -> None:
    raw = [
        _ev(1, EventType.VIEW, T0),
        _ev(2, EventType.CONSENT_GRANTED, T0 + timedelta(days=1), channel=Channel.EMAIL),
        _ev(3, EventType.PURCHASE, T0 + timedelta(days=2), value=40.0, margin=12.0),
        _ev(4, EventType.PURCHASE, T0 + timedelta(days=5, microseconds=7), value=10.0),
        _ev(5, EventType.REFUND, T0 + timedelta(days=6), value=4.0),
        _ev(6, EventType.SEND, T0 + timedelta(days=8), channel=Channel.EMAIL),
        _ev(7, EventType.UNSUBSCRIBE, T0 + timedelta(days=9), channel=Channel.EMAIL),
    ]
    digest = HistoryDigest()
    digest.extend(raw)
    digest.extend(raw)  # handed twice: counted once
    kept = {"a": [e for e in raw if e.type in (EventType.SEND, EventType.UNSUBSCRIBE)]}
    back = digest.as_events(kept)["a"]

    def of(kind: EventType) -> list[Event | DigestEvent]:
        return [e for e in back if e.type is kind]

    assert [(e.ts, e.value, e.margin) for e in of(EventType.PURCHASE)] == [
        (T0 + timedelta(days=2), 40.0, 12.0),
        (T0 + timedelta(days=5, microseconds=7), 10.0, None),
    ]
    assert [(e.ts, e.value) for e in of(EventType.REFUND)] == [(T0 + timedelta(days=6), 4.0)]
    assert [(e.ts, e.channel) for e in of(EventType.CONSENT_GRANTED)] == [
        (T0 + timedelta(days=1), Channel.EMAIL)
    ]
    assert of(EventType.CONSENT_REVOKED) == []  # the unsubscribe is in `kept` already
    assert len(of(EventType.SEND)) == 1 and len(of(EventType.UNSUBSCRIBE)) == 1
    assert len({e.event_id for e in back}) == len(back)
    assert all(isinstance(e, DigestEvent) for e in of(EventType.PURCHASE))  # compact, not Events
    for at in (T0 + timedelta(days=3), T0 + timedelta(days=7), T0 + timedelta(days=30)):
        assert behaviour_from_events(back, at) == behaviour_from_events(raw, at)
