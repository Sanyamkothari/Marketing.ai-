"""Point in time: no event after `as_of` may change a profile. These tests plant future events."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from autonomy.contracts import Channel, CustomerState, Event, EventType
from autonomy.fakes import InMemoryEventStore, InMemoryStateStore
from autonomy.profile import ShopPrices, build_profiles, profile_for, training_rows
from tests.v3.profile.synthetic import customer_events, customer_id

AS_OF = datetime(2025, 3, 31, 12, tzinfo=UTC)


def _planted(cid: str, after: datetime) -> list[Event]:
    """One future event of every kind that feeds a feature, with loud values."""
    kinds: list[tuple[EventType, dict[str, object]]] = [
        (EventType.PURCHASE, {"value": 99_999.0, "margin": 5.0, "category": "future", "item_id": "future"}),
        (EventType.REFUND, {"value": 50_000.0}),
        (EventType.VIEW, {"item_id": "future"}),
        (EventType.CART, {}),
        (EventType.VISIT, {}),
        (EventType.SIGNUP, {"attrs": {"timezone": "Asia/Tokyo"}}),
        (EventType.SEND, {"channel": Channel.WHATSAPP}),
        (EventType.CLICK, {"channel": Channel.WHATSAPP}),
        (EventType.REPLY, {"channel": Channel.WHATSAPP}),
        (EventType.COMPLAINT, {"channel": Channel.EMAIL}),
        (EventType.BOUNCE, {"channel": Channel.EMAIL}),
        (EventType.UNSUBSCRIBE, {"channel": Channel.EMAIL, "attrs": {"reason": "reply_stop"}}),
        (EventType.CONSENT_GRANTED, {"channel": Channel.SMS}),
        (EventType.PREFERENCE, {"attrs": {"key": "no_discounts", "value": "true"}}),
        (
            EventType.PREFERENCE,
            {"attrs": {"key": "snooze_until", "value": (after + timedelta(days=400)).isoformat()}},
        ),
    ]
    return [
        Event(
            event_id=f"future-{i}",
            customer_id=cid,
            type=kind,
            ts=after + timedelta(days=1 + i, hours=i),
            source="plant",
            **fields,  # type: ignore[arg-type]
        )
        for i, (kind, fields) in enumerate(kinds)
    ]


def test_planted_future_events_never_change_a_profile() -> None:
    changed_somewhere = 0
    for i in range(60):
        events = customer_events(i)
        before = profile_for(events, AS_OF)
        after = profile_for([*events, *_planted(customer_id(i), AS_OF)], AS_OF)
        assert after == before, i
        # the same events do change the profile once the date moves past them
        later = profile_for([*events, *_planted(customer_id(i), AS_OF)], AS_OF + timedelta(days=60))
        changed_somewhere += later != before
    assert changed_somewhere == 60


def test_an_event_exactly_at_as_of_counts_and_one_a_moment_later_does_not() -> None:
    def purchase(ts: datetime) -> Event:
        return Event(event_id="p", customer_id="c", type=EventType.PURCHASE, ts=ts, source="t", value=10.0)

    assert profile_for([purchase(AS_OF)], AS_OF)["buy_orders_total"] == 1.0
    assert profile_for([purchase(AS_OF + timedelta(microseconds=1))], AS_OF)["buy_orders_total"] == 0.0


def test_the_shops_price_histogram_reads_no_future_order_either() -> None:
    events = [e for i in range(120) for e in customer_events(i)]
    future = [
        Event(
            event_id=f"big-{i}",
            customer_id="z",
            type=EventType.PURCHASE,
            ts=AS_OF + timedelta(days=i + 1),
            source="t",
            value=1e6,
        )
        for i in range(200)
    ]
    assert ShopPrices.until([*events, *future], AS_OF).orders == ShopPrices.until(events, AS_OF).orders
    mine = customer_events(3)
    a = profile_for(mine, AS_OF, shop=ShopPrices.until(events, AS_OF))
    b = profile_for(mine, AS_OF, shop=ShopPrices.until([*events, *future], AS_OF))
    assert a == b


def test_build_profiles_through_a_store_ignores_events_after_the_date() -> None:
    def run(with_future: bool) -> dict[str, dict[str, float]]:
        store, states = InMemoryEventStore(), InMemoryStateStore()
        for i in range(25):
            cid = customer_id(i)
            store.append(customer_events(i))
            if with_future:
                store.append(_planted(cid, AS_OF))
            states.put([CustomerState(customer_id=cid, as_of=AS_OF)])
        build_profiles(store, states, AS_OF, batch_size=10)
        return {s.customer_id: s.features for s in states.scan()}

    assert run(True) == run(False)


def test_training_rows_at_an_early_date_ignore_later_events() -> None:
    early = datetime(2024, 9, 30, tzinfo=UTC)
    events = [e for i in range(30) for e in customer_events(i)]
    planted = [e for i in range(30) for e in _planted(customer_id(i), early)]
    assert list(training_rows(events + planted, [early])) == list(training_rows(events, [early]))
