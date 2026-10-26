"""Hand-computed values for the features whose definition is easy to get subtly wrong."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest

from autonomy.contracts import Channel, Event, EventType
from autonomy.profile import ShopPrices, profile_for

AS_OF = datetime(2025, 6, 30, 12, tzinfo=UTC)


def ev(kind: EventType, days_ago: float, **kw: object) -> Event:
    n = ev.counter = getattr(ev, "counter", 0) + 1  # type: ignore[attr-defined]
    return Event(
        event_id=f"e{n}",
        customer_id="c",
        type=kind,
        ts=AS_OF - timedelta(days=days_ago),
        source="test",
        **kw,  # type: ignore[arg-type]
    )


def buy(days_ago: float, value: float = 10.0, **kw: object) -> Event:
    return ev(EventType.PURCHASE, days_ago, value=value, **kw)


def test_order_windows_are_open_at_the_far_end_and_closed_at_as_of() -> None:
    p = profile_for([buy(0), buy(29.99), buy(30), buy(89), buy(91), buy(364), buy(366)], AS_OF)
    assert p["buy_orders_30d"] == 2.0  # today and 29.99 days ago; exactly 30 days ago is out
    assert p["buy_orders_90d"] == 4.0
    assert p["buy_orders_365d"] == 6.0
    assert p["buy_orders_total"] == 7.0


def test_net_revenue_takes_refunds_off_and_agrees_with_the_state_folder() -> None:
    from autonomy.data.folder import StateFolder

    events = [buy(40, 30.0), buy(10, 20.0), ev(EventType.REFUND, 5, value=12.5)]
    folder = StateFolder()
    state = None
    for e in sorted(events, key=lambda x: x.ts):
        state = folder.fold(state, e)
    assert state is not None
    p = profile_for(events, AS_OF)
    assert p["buy_net_revenue"] == state.revenue == 37.5
    assert p["buy_avg_order_value"] == 25.0
    assert p["resp_returns"] == 1.0
    assert p["resp_refund_value_share"] == pytest.approx(0.25)
    assert p["price_offer_sensitivity"] == pytest.approx(state.offer_sensitivity or 0.0)


def test_gaps_overdue_and_refill_due() -> None:
    # orders 100, 70 and 40 days ago: two gaps of 30 days; last order 40 days ago
    p = profile_for([buy(100), buy(70), buy(40)], AS_OF)
    assert p["trend_gap_known"] == 1.0
    assert p["trend_avg_gap_days"] == pytest.approx(30.0)
    assert p["trend_last_gap_ratio"] == pytest.approx(1.0)
    assert p["trend_overdue_ratio"] == pytest.approx(40 / 30)
    assert p["mom_refill_due_days"] == pytest.approx(-10.0)  # ten days overdue
    speeding = profile_for([buy(100), buy(70), buy(60)], AS_OF)
    assert speeding["trend_last_gap_ratio"] == pytest.approx(10 / 30)


def test_a_single_order_has_no_cycle_and_says_so() -> None:
    p = profile_for([buy(10)], AS_OF)
    assert p["buy_ordered"] == 1.0 and p["trend_gap_known"] == 0.0
    assert p["trend_avg_gap_days"] == 0.0 and p["trend_overdue_ratio"] == 0.0
    assert p["trend_last_gap_ratio"] == 1.0 and p["mom_refill_due_days"] == 0.0


def test_order_momentum_compares_the_last_90_days_with_the_90_before() -> None:
    assert profile_for([buy(10), buy(20), buy(30), buy(120)], AS_OF)["trend_order_momentum"] == pytest.approx(
        0.5
    )
    assert profile_for([buy(120), buy(130)], AS_OF)["trend_order_momentum"] == -1.0
    assert profile_for([buy(10)], AS_OF)["trend_order_momentum"] == 1.0


def test_taste_features() -> None:
    p = profile_for(
        [
            buy(200, category="shoes", item_id="a"),
            buy(150, category="shoes", item_id="a"),
            buy(100, category="bags", item_id="b"),
            buy(10, category="hats", item_id="c"),
        ],
        AS_OF,
    )
    assert p["taste_distinct_categories"] == 3.0
    assert p["taste_top_category_share"] == 0.5
    assert p["taste_repeat_item_share"] == 0.25  # the second "a" re-buys
    assert p["taste_new_category_share_90d"] == 1.0  # hats were new inside the last 90 days


def test_offers_the_none_offer_is_not_an_offer_and_full_price_follows_an_offer() -> None:
    p = profile_for(
        [
            buy(90, attrs={"offer_id": "welcome10"}),
            buy(60),  # right after an offer, full price
            buy(40, attrs={"offer_id": "none"}),  # "none" is no offer
            buy(20, attrs={"offer_id": "spring"}),
            buy(10, attrs={"offer_id": "spring"}),  # right after an offer, with an offer
        ],
        AS_OF,
    )
    assert p["price_full_after_offer_known"] == 1.0
    assert p["price_full_after_offer_share"] == pytest.approx(0.5)  # of two orders after an offer, one full
    assert p["price_offer_sensitivity"] == pytest.approx((3 + 0.9) / (5 + 3))


def test_price_level_is_read_against_the_shop() -> None:
    shop = ShopPrices()
    for v in range(1, 201):
        shop.add(float(v))
    cheap = profile_for([buy(5, 10.0)], AS_OF, shop=shop)
    dear = profile_for([buy(5, 190.0)], AS_OF, shop=shop)
    assert cheap["price_level_known"] == dear["price_level_known"] == 1.0
    assert cheap["price_level"] < 0.12 and dear["price_level"] > 0.85
    assert profile_for([buy(5, 190.0)], AS_OF)["price_level"] == 0.5  # no shop, no claim
    tiny = ShopPrices()
    tiny.add(5.0)
    assert profile_for([buy(5)], AS_OF, shop=tiny)["price_level_known"] == 0.0


def test_hours_weekdays_and_paydays_are_in_the_customers_own_time_zone() -> None:
    # 2025-06-28 is a Saturday. 21:00 UTC on Saturday is 02:30 on Sunday in Kolkata, still the weekend,
    # 19:30 UTC on Friday is 01:00 on Saturday in Kolkata: a weekend order that is a weekday in UTC.
    friday_utc = datetime(2025, 6, 27, 19, 30, tzinfo=UTC)
    events = [
        Event(
            event_id="s",
            customer_id="c",
            type=EventType.SIGNUP,
            ts=friday_utc - timedelta(days=5),
            source="t",
            attrs={"timezone": "Asia/Kolkata"},
        ),
        Event(event_id="o", customer_id="c", type=EventType.PURCHASE, ts=friday_utc, source="t", value=5.0),
        Event(
            event_id="k",
            customer_id="c",
            type=EventType.CLICK,
            ts=friday_utc,
            source="t",
            channel=Channel.EMAIL,
        ),
    ]
    p = profile_for(events, AS_OF)
    assert p["time_weekend_order_share"] == 1.0
    assert p["time_peak_hour"] == 1.0  # 19:30 UTC = 01:00 in Kolkata
    assert p["time_evening_share"] == 0.0
    utc_only = profile_for([e for e in events if e.type is not EventType.SIGNUP], AS_OF)
    assert utc_only["time_weekend_order_share"] == 0.0 and utc_only["time_peak_hour"] == 19.0
    assert utc_only["time_evening_share"] == 1.0


def test_an_unknown_time_zone_name_is_ignored() -> None:
    odd = Event(
        event_id="s",
        customer_id="c",
        type=EventType.SIGNUP,
        ts=AS_OF - timedelta(days=3),
        source="t",
        attrs={"timezone": "Mars/Olympus"},
    )
    assert profile_for([odd, buy(1)], AS_OF)["buy_orders_total"] == 1.0


def test_payday_orders_are_the_first_two_and_last_two_days_of_a_month() -> None:
    days = [datetime(2025, 5, d, 9, tzinfo=UTC) for d in (1, 2, 3, 15, 30, 31)]
    orders = [
        Event(event_id=f"o{d.day}", customer_id="c", type=EventType.PURCHASE, ts=d, source="t", value=1.0)
        for d in days
    ]
    assert profile_for(orders, AS_OF)["time_payday_order_share"] == pytest.approx(4 / 6)


def test_channel_features() -> None:
    def msg(kind: EventType, hours: float, ch: Channel) -> Event:
        return ev(kind, 3 - hours / 24, channel=ch)

    events = [
        ev(EventType.CONSENT_GRANTED, 100, channel=Channel.EMAIL),
        ev(EventType.CONSENT_GRANTED, 100, channel=Channel.WHATSAPP),
        ev(EventType.UNSUBSCRIBE, 50, channel=Channel.WHATSAPP),
        *[msg(EventType.SEND, 0, Channel.EMAIL) for _ in range(4)],
        *[msg(EventType.OPEN, 1, Channel.EMAIL) for _ in range(2)],
        msg(EventType.CLICK, 2, Channel.EMAIL),
        msg(EventType.REPLY, 3, Channel.EMAIL),
        msg(EventType.SEND, 0, Channel.WHATSAPP),
    ]
    p = profile_for(events, AS_OF)
    assert p["ch_contactable_channels"] == 1.0  # email only; WhatsApp is unsubscribed
    assert p["ch_open_rate"] == pytest.approx(2 / 5)
    assert p["ch_click_rate"] == pytest.approx(1 / 5)
    assert p["ch_reply_rate"] == pytest.approx(1 / 5)
    assert p["ch_email_engage_rate"] == pytest.approx(2 / 4)
    assert p["ch_whatsapp_engage_rate"] == 0.0
    assert p["ch_engaged"] == 1.0 and p["ch_days_since_engaged"] == pytest.approx(3 - 3 / 24)


def test_opens_alone_are_not_engagement() -> None:
    p = profile_for(
        [ev(EventType.SEND, 4, channel=Channel.EMAIL), ev(EventType.OPEN, 4, channel=Channel.EMAIL)], AS_OF
    )
    assert p["ch_engaged"] == 0.0 and p["time_engaged_known"] == 0.0 and p["ch_open_rate"] == 1.0


def test_replies_stop_not_now_complaints_and_discount_preferences() -> None:
    snooze = (AS_OF + timedelta(days=10)).isoformat()
    expired = (AS_OF - timedelta(days=10)).isoformat()
    base = [
        ev(EventType.REPLY, 6, channel=Channel.EMAIL, text="not now"),
        ev(EventType.PREFERENCE, 6, channel=Channel.EMAIL, attrs={"key": "snooze_until", "value": expired}),
        ev(EventType.COMPLAINT, 5, channel=Channel.EMAIL),
        ev(EventType.UNSUBSCRIBE, 4, channel=Channel.EMAIL, attrs={"reason": "reply_stop"}),
        ev(EventType.PREFERENCE, 3, attrs={"key": "no_discounts", "value": "true"}),
    ]
    p = profile_for(base, AS_OF)
    assert p["resp_not_now_replies"] == 1.0 and p["resp_snoozed"] == 0.0
    assert p["resp_stop_replies"] == 1.0 and p["resp_complaints"] == 1.0
    assert p["resp_no_discounts"] == 1.0
    held = [*base, ev(EventType.PREFERENCE, 2, attrs={"key": "snooze_until", "value": snooze})]
    assert profile_for(held, AS_OF)["resp_snoozed"] == 1.0


def test_moments() -> None:
    p = profile_for(
        [
            ev(EventType.VIEW, 40, item_id="old"),
            ev(EventType.VIEW, 5, item_id="x"),
            ev(EventType.VIEW, 4, item_id="y"),
            ev(EventType.VIEW, 3, item_id="bought"),
            buy(2, item_id="bought"),
            ev(EventType.CART, 1),
        ],
        AS_OF,
    )
    assert p["mom_viewed"] == 1.0 and p["mom_days_since_view"] == pytest.approx(3.0)
    assert p["mom_viewed_not_bought"] == 2.0  # x and y; "old" is outside 30 days, "bought" was bought
    assert p["mom_carted"] == 1.0 and p["mom_days_since_cart"] == pytest.approx(1.0)
    assert p["mom_cart_open"] == 1.0
    assert profile_for([ev(EventType.CART, 3), buy(2)], AS_OF)["mom_cart_open"] == 0.0


def test_event_order_and_repeated_ids_do_not_matter() -> None:
    events = [buy(50, category="a"), buy(30, category="b"), ev(EventType.CART, 2), buy(10)]
    expected = profile_for(events, AS_OF)
    shuffled = list(events) * 2  # every id twice
    random.Random(1).shuffle(shuffled)
    assert profile_for(shuffled, AS_OF) == expected
