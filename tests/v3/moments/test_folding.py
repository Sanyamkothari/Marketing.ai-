"""The state folder keeps what moments need: views, carts, usual gaps and returns."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from autonomy.contracts import CustomerState, Event, EventType
from autonomy.data.folder import MAX_VIEWED_ITEMS, StateFolder

T0 = datetime(2026, 5, 1, 9, tzinfo=UTC)
FOLDER = StateFolder()


def ev(kind: EventType, at: datetime, n: int, item: str | None = None, **kw: object) -> Event:
    return Event.model_validate(
        {
            "event_id": f"e{n}",
            "customer_id": "c1",
            "type": kind,
            "ts": at,
            "source": "test",
            "item_id": item,
            **kw,
        }
    )


def fold(events: list[Event]) -> CustomerState:
    state: CustomerState | None = None
    for e in events:
        state = FOLDER.fold(state, e)
    assert state is not None
    return state


def test_new_fields_default_to_nothing() -> None:
    state = fold([ev(EventType.SIGNUP, T0, 1)])
    assert state.last_view_at is None
    assert state.last_cart_at is None
    assert state.viewed_items == {}
    assert state.usual_gap_days is None
    assert state.purchase_gap_days is None
    assert state.last_return_at is None


def test_views_count_per_product_and_keep_the_latest_time() -> None:
    state = fold(
        [
            ev(EventType.VIEW, T0, 1, "shoe"),
            ev(EventType.VIEW, T0 + timedelta(hours=1), 2, "shoe"),
            ev(EventType.VIEW, T0 + timedelta(hours=2), 3, "hat"),
        ]
    )
    assert state.viewed_items["shoe"].count == 2
    assert state.viewed_items["shoe"].last_at == T0 + timedelta(hours=1)
    assert state.viewed_items["hat"].count == 1
    assert state.last_view_at == T0 + timedelta(hours=2)


def test_a_view_without_a_product_sets_only_the_time() -> None:
    state = fold([ev(EventType.VIEW, T0, 1)])
    assert state.last_view_at == T0
    assert state.viewed_items == {}


def test_viewed_items_are_bounded_and_the_oldest_go_first() -> None:
    events = [
        ev(EventType.VIEW, T0 + timedelta(minutes=i), i, f"item{i}") for i in range(MAX_VIEWED_ITEMS + 3)
    ]
    state = fold(events)
    assert len(state.viewed_items) == MAX_VIEWED_ITEMS
    assert "item0" not in state.viewed_items
    assert f"item{MAX_VIEWED_ITEMS + 2}" in state.viewed_items


def test_cart_sets_last_cart_at_and_a_late_event_cannot_move_it_back() -> None:
    state = fold(
        [
            ev(EventType.CART, T0 + timedelta(days=2), 1, "shoe"),
            ev(EventType.CART, T0, 2, "shoe"),
        ]
    )
    assert state.last_cart_at == T0 + timedelta(days=2)


def test_a_purchase_of_a_viewed_product_drops_it_but_keeps_the_cart_time() -> None:
    state = fold(
        [
            ev(EventType.VIEW, T0, 1, "shoe"),
            ev(EventType.VIEW, T0, 2, "hat"),
            ev(EventType.CART, T0 + timedelta(minutes=5), 3, "shoe"),
            ev(EventType.PURCHASE, T0 + timedelta(minutes=9), 4, "shoe", value=50.0),
        ]
    )
    assert set(state.viewed_items) == {"hat"}
    assert state.last_cart_at == T0 + timedelta(minutes=5)
    assert state.last_purchase_at == T0 + timedelta(minutes=9)


def test_usual_gap_ignores_gaps_under_a_day_and_smooths_the_rest() -> None:
    state = fold(
        [
            ev(EventType.VISIT, T0, 1),
            ev(EventType.VISIT, T0 + timedelta(hours=5), 2),
            ev(EventType.VISIT, T0 + timedelta(days=4, hours=5), 3),
        ]
    )
    assert state.usual_gap_days == 4.0
    again = fold(
        [
            ev(EventType.VISIT, T0, 1),
            ev(EventType.VISIT, T0 + timedelta(days=4), 2),
            ev(EventType.VISIT, T0 + timedelta(days=6), 3),
        ]
    )
    assert again.usual_gap_days is not None
    assert abs(again.usual_gap_days - (0.7 * 4.0 + 0.3 * 2.0)) < 1e-9


def test_a_return_needs_a_long_gap_and_a_browsing_event() -> None:
    quiet = [
        ev(EventType.VISIT, T0, 1),
        ev(EventType.VISIT, T0 + timedelta(days=2), 2),
        ev(EventType.VISIT, T0 + timedelta(days=4), 3),
    ]
    assert fold(quiet).last_return_at is None
    back = T0 + timedelta(days=4 + 40)
    state = fold([*quiet, ev(EventType.VISIT, back, 4)])
    assert state.last_return_at == back
    assert state.last_return_gap_days == 40.0
    # The same gap closed by a purchase is not a return: they came to buy, not to browse.
    bought = fold([*quiet, ev(EventType.PURCHASE, back, 5, value=10.0)])
    assert bought.last_return_at is None
    # And a gap of two weeks is not a long absence for someone who is usually away for a month.
    monthly = [ev(EventType.VISIT, T0 + timedelta(days=30 * i), i) for i in range(4)]
    late = T0 + timedelta(days=90 + 15)
    assert fold([*monthly, ev(EventType.VISIT, late, 9)]).last_return_at != late


def test_purchase_gap_is_a_running_average_of_days_between_purchases() -> None:
    state = fold(
        [
            ev(EventType.PURCHASE, T0, 1, value=10.0),
            ev(EventType.PURCHASE, T0 + timedelta(days=30), 2, value=10.0),
            ev(EventType.PURCHASE, T0 + timedelta(days=70), 3, value=10.0),
        ]
    )
    assert state.purchase_gap_days is not None
    assert abs(state.purchase_gap_days - (0.7 * 30.0 + 0.3 * 40.0)) < 1e-9


def test_outbound_events_touch_none_of_it() -> None:
    state = fold(
        [ev(EventType.VIEW, T0, 1, "shoe"), ev(EventType.OPEN, T0 + timedelta(days=9), 2, channel="email")]
    )
    assert state.usual_gap_days is None
    assert state.last_return_at is None


def test_each_bought_product_keeps_its_own_rhythm_in_purchase_days() -> None:
    state = fold(
        [
            ev(EventType.PURCHASE, T0, 1, "cream", value=10.0),
            ev(EventType.PURCHASE, T0 + timedelta(minutes=1), 2, "cream", value=10.0),  # same order
            ev(EventType.PURCHASE, T0 + timedelta(days=5), 3, "soap", value=4.0),
            ev(EventType.PURCHASE, T0 + timedelta(days=30), 4, "cream", value=10.0),
            ev(EventType.PURCHASE, T0 + timedelta(days=70), 5, "cream", value=10.0),
        ]
    )
    cream = state.bought_items["cream"]
    assert cream.days == 3
    assert cream.last_at == T0 + timedelta(days=70)
    assert cream.gap_days is not None
    assert abs(cream.gap_days - (0.7 * 30.0 + 0.3 * 40.0)) < 1e-9
    assert state.bought_items["soap"].days == 1
    assert state.bought_items["soap"].gap_days is None
    # Five purchases, three gaps of a day or more between purchase days (5, 25 and 40 days).
    assert state.purchases == 5
    assert state.purchase_gaps == 3


def test_bought_items_are_bounded_and_a_late_purchase_only_adds_a_day() -> None:
    from autonomy.data.folder import MAX_BOUGHT_ITEMS

    events = [
        ev(EventType.PURCHASE, T0 + timedelta(days=k), k + 1, f"p{k}", value=1.0)
        for k in range(MAX_BOUGHT_ITEMS + 2)
    ]
    state = fold(events)
    assert len(state.bought_items) == MAX_BOUGHT_ITEMS
    assert "p0" not in state.bought_items and "p1" not in state.bought_items
    late = FOLDER.fold(state, ev(EventType.PURCHASE, T0, 99, "p9", value=1.0))
    p9 = late.bought_items["p9"]
    assert p9.days == 2
    assert p9.last_at == T0 + timedelta(days=MAX_BOUGHT_ITEMS + 1)
