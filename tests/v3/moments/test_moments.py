"""Each moment fires and fades; the calendar, the configuration and the combination rule."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import pytest

from autonomy.contracts import CustomerState, ItemViews
from autonomy.decide.moments import (
    CONFIG_DIR,
    SIGNALS,
    ActiveMoment,
    Calendar,
    Festival,
    MomentBook,
    MomentsConfig,
    MomentSpec,
    PriceChange,
    load_calendar,
    load_config,
    price_drops,
    register_moment,
)

NOW = datetime(2026, 9, 9, 12, tzinfo=UTC)  # a Wednesday: no festival, three weeks from a payday
BOOK = MomentBook.load()


def state(**kw: object) -> CustomerState:
    base: dict[str, object] = {"customer_id": "c1", "as_of": NOW, "timezone": "Asia/Kolkata", "country": "IN"}
    base.update(kw)
    return CustomerState.model_validate(base)


def days(n: float) -> timedelta:
    return timedelta(days=n)


def lift_of(name: str, st: CustomerState, at: datetime = NOW, book: MomentBook = BOOK) -> float:
    found = {m.name: m for m in book.active(st, at)}
    return found[name].lift if name in found else 0.0


def multiplier(name: str) -> float:
    return BOOK.config.moments[name].multiplier


# --- the files ---------------------------------------------------------------------------------


def test_every_configured_moment_has_a_signal_and_the_default_calendar_is_there() -> None:
    config = load_config()
    assert set(config.moments) == set(SIGNALS)
    assert config.default_country == "IN"
    assert "IN" in BOOK.calendars
    assert BOOK.calendars["IN"].timezone == "Asia/Kolkata"


def test_a_country_without_a_file_has_no_calendar() -> None:
    assert load_calendar("zz") is None
    assert load_calendar("in", CONFIG_DIR) is not None


def test_an_unknown_moment_in_the_config_is_refused() -> None:
    spec = MomentSpec(label="x", multiplier=1.2)
    with pytest.raises(ValueError, match="no signal"):
        MomentBook(MomentsConfig(moments={"nonsense": spec}))


def test_a_typo_in_a_setting_is_refused() -> None:
    with pytest.raises(ValueError):
        MomentSpec.model_validate({"label": "x", "multiplier": 1.2, "half_life": 1})


# --- the calendar --------------------------------------------------------------------------------


def test_payday_age_counts_days_since_the_first_or_the_last_day_of_the_month() -> None:
    cal = BOOK.calendars["IN"]
    assert cal.payday_age(date(2026, 11, 1)) == 0
    assert cal.payday_age(date(2026, 11, 4)) == 3
    assert cal.payday_age(date(2026, 10, 31)) == 0
    assert cal.payday_age(date(2026, 11, 29)) == 28
    assert cal.payday_age(date(2027, 1, 1)) == 0  # the new year's first, the last of December before it
    assert cal.payday_age(date(2027, 2, 28)) == 0  # February's last day
    assert cal.payday_age(date(2027, 3, 2)) == 1


def test_festival_windows_run_from_lead_to_tail() -> None:
    cal = BOOK.calendars["IN"]

    def on(day: date) -> dict[str, int]:
        return {w.festival.id: w.days_to for w in cal.festivals_on(day)}

    assert on(date(2026, 10, 28)) == {}
    assert on(date(2026, 10, 29)) == {"diwali": 10}
    assert on(date(2026, 11, 8)) == {"diwali": 0}
    assert on(date(2026, 11, 10)) == {"diwali": -2}
    assert on(date(2026, 11, 11)) == {}
    # A fixed day comes back every year.
    assert on(date(2026, 8, 12)) == {"independence_day_sale": 3}
    assert "independence_day_sale" in on(date(2031, 8, 16))


def test_a_window_can_cross_the_new_year() -> None:
    cal = Calendar(
        country="xx",
        festivals=(Festival(id="ny", name="New Year", fixed="01-01", lead_days=5, tail_days=2),),
    )
    assert cal.country == "XX"
    assert cal.festivals_on(date(2026, 12, 27))[0].days_to == 5
    assert cal.festivals_on(date(2026, 12, 26)) == ()
    assert cal.festivals_on(date(2027, 1, 3))[0].days_to == -2
    assert cal.festivals_on(date(2027, 1, 4)) == ()


def test_a_festival_needs_a_day_and_a_real_one() -> None:
    with pytest.raises(ValueError, match="neither"):
        Festival(id="x", name="X")
    with pytest.raises(ValueError, match="MM-DD"):
        Festival(id="x", name="X", fixed="13-01")
    leap = Festival(id="x", name="X", fixed="02-29")
    assert leap.days_in(2027) == ()
    assert leap.days_in(2028) == (date(2028, 2, 29),)


def test_the_shipped_calendar_lists_the_launch_festivals() -> None:
    ids = {f.id for f in BOOK.calendars["IN"].festivals}
    assert {
        "diwali",
        "holi",
        "eid_al_fitr",
        "raksha_bandhan",
        "independence_day_sale",
        "year_end_sale",
    } <= ids


# --- viewed twice --------------------------------------------------------------------------------


def test_viewed_twice_fires_and_fades_by_half_a_day_then_ends() -> None:
    views = {"shoe": ItemViews(count=2, last_at=NOW)}
    full = multiplier("viewed_twice") - 1.0
    assert lift_of("viewed_twice", state(viewed_items=views)) == pytest.approx(full)
    assert lift_of("viewed_twice", state(viewed_items=views), NOW + days(1)) == pytest.approx(full / 2)
    assert lift_of("viewed_twice", state(viewed_items=views), NOW + days(2)) == pytest.approx(full / 4)
    assert lift_of("viewed_twice", state(viewed_items=views), NOW + days(2.01)) == 0.0


def test_one_view_is_not_a_moment_and_buying_since_ends_it() -> None:
    once = {"shoe": ItemViews(count=1, last_at=NOW)}
    assert lift_of("viewed_twice", state(viewed_items=once)) == 0.0
    twice = {"shoe": ItemViews(count=2, last_at=NOW - days(1))}
    assert lift_of("viewed_twice", state(viewed_items=twice)) > 0
    assert lift_of("viewed_twice", state(viewed_items=twice, last_purchase_at=NOW - days(0.5))) == 0.0


def test_viewed_twice_names_the_product_in_its_reason() -> None:
    views = {"shoe": ItemViews(count=3, last_at=NOW - days(1))}
    [reason] = [m.reason for m in BOOK.active(state(viewed_items=views), NOW) if m.name == "viewed_twice"]
    assert reason == "Looked at shoe 3 times, last a day ago"


# --- cart left -----------------------------------------------------------------------------------


def test_cart_left_fires_fades_and_closes_on_a_purchase() -> None:
    full = multiplier("cart_left") - 1.0
    cart = NOW - days(1)
    assert lift_of("cart_left", state(last_cart_at=NOW)) == pytest.approx(full)
    assert lift_of("cart_left", state(last_cart_at=cart)) == pytest.approx(full / 2)
    assert lift_of("cart_left", state(last_cart_at=NOW - days(3))) == pytest.approx(full / 8)
    assert lift_of("cart_left", state(last_cart_at=NOW - days(3.5))) == 0.0
    assert lift_of("cart_left", state(last_cart_at=cart, last_purchase_at=NOW - days(0.2))) == 0.0
    assert lift_of("cart_left", state(last_cart_at=cart, last_purchase_at=NOW - days(5))) > 0


def test_a_cart_in_the_future_is_not_a_moment() -> None:
    assert lift_of("cart_left", state(last_cart_at=NOW + days(1))) == 0.0


# --- refill due ----------------------------------------------------------------------------------


def test_refill_due_from_the_catalogue_scores() -> None:
    open_window = state(scores={"refill_window_open": 1.0, "refill_p14": 0.30, "refill_due_days": -2.0})
    # The share of the raise follows the same shrunk curve as refill_boost: p / (p + 0.10).
    assert lift_of("refill_due", open_window) == pytest.approx((multiplier("refill_due") - 1.0) * 0.75)
    closed = state(scores={"refill_window_open": 0.0, "refill_p14": 0.30})
    assert lift_of("refill_due", closed) == 0.0
    [reason] = [m.reason for m in BOOK.active(open_window, NOW) if m.name == "refill_due"]
    assert reason == "Refill about 2 days overdue"


def test_refill_due_from_the_customers_own_rhythm() -> None:
    base = {"purchases": 4, "purchase_gap_days": 30.0}
    full = multiplier("refill_due") - 1.0
    # 28 days since the last purchase: due in two days, inside the three-day lead.
    near = state(last_purchase_at=NOW - days(28), **base)
    assert lift_of("refill_due", near) == pytest.approx(full)
    assert [m.reason for m in BOOK.active(near, NOW) if m.name == "refill_due"] == [
        "Refill due in about 2 days"
    ]
    assert lift_of("refill_due", state(last_purchase_at=NOW - days(20), **base)) == 0.0  # too early
    assert lift_of("refill_due", state(last_purchase_at=NOW - days(36), **base)) == pytest.approx(full)
    assert lift_of("refill_due", state(last_purchase_at=NOW - days(38), **base)) == 0.0  # the window closed
    young = state(last_purchase_at=NOW - days(28), purchases=2, purchase_gap_days=30.0)
    assert lift_of("refill_due", young) == 0.0  # two purchases do not make a rhythm


# --- price drop ----------------------------------------------------------------------------------


def book_with_drop(item: str, old: float, new: float, at: datetime) -> MomentBook:
    return BOOK.with_price_changes({item: PriceChange(item, at, old, new)})


def test_price_drop_on_a_viewed_item_fires_scales_with_the_drop_and_fades() -> None:
    views = {"shoe": ItemViews(count=1, last_at=NOW - days(2))}
    st = state(viewed_items=views)
    full = multiplier("price_drop") - 1.0
    big = book_with_drop("shoe", 100.0, 70.0, NOW)  # 30% is past the "full" 20%
    assert lift_of("price_drop", st, NOW, big) == pytest.approx(full)
    assert lift_of("price_drop", st, NOW + days(2), big) == pytest.approx(full / 2)  # half-life of two days
    assert lift_of("price_drop", st, NOW + days(3.1), big) == 0.0
    modest = book_with_drop("shoe", 100.0, 90.0, NOW)  # 10% is half the "full" 20%
    assert lift_of("price_drop", st, NOW, modest) == pytest.approx(full / 2)
    [reason] = [m.reason for m in big.active(st, NOW) if m.name == "price_drop"]
    assert reason == "The price of shoe dropped 30% just now, after they looked at it"


def test_price_drop_needs_a_view_before_it_a_real_drop_and_no_purchase_since() -> None:
    drop = book_with_drop("shoe", 100.0, 70.0, NOW)
    seen_after = state(viewed_items={"shoe": ItemViews(count=1, last_at=NOW + days(0.1))})
    assert lift_of("price_drop", seen_after, NOW + days(0.2), drop) == 0.0  # they saw the new price
    other = state(viewed_items={"hat": ItemViews(count=1, last_at=NOW - days(1))})
    assert lift_of("price_drop", other, NOW, drop) == 0.0
    tiny = book_with_drop("shoe", 100.0, 98.0, NOW)
    seen = state(viewed_items={"shoe": ItemViews(count=1, last_at=NOW - days(1))})
    assert lift_of("price_drop", seen, NOW, tiny) == 0.0
    rise = book_with_drop("shoe", 100.0, 120.0, NOW)
    assert lift_of("price_drop", seen, NOW, rise) == 0.0
    bought = state(
        viewed_items={"shoe": ItemViews(count=1, last_at=NOW - days(1))}, last_purchase_at=NOW + days(0.1)
    )
    assert lift_of("price_drop", bought, NOW + days(0.2), drop) == 0.0


def test_price_drops_finds_the_items_that_got_cheaper() -> None:
    found = price_drops({"a": 10.0, "b": 10.0, "c": 10.0}, {"a": 8.0, "b": 12.0, "d": 1.0}, NOW)
    assert set(found) == {"a"}
    assert found["a"].drop_share == pytest.approx(0.2)


# --- back after a long absence --------------------------------------------------------------------


def test_back_after_absence_fires_fades_and_needs_a_long_gap() -> None:
    full = multiplier("back_after_absence") - 1.0
    st = state(last_return_at=NOW, last_return_gap_days=45.0)
    assert lift_of("back_after_absence", st) == pytest.approx(full)
    assert lift_of("back_after_absence", st, NOW + days(1)) == pytest.approx(full / 2)
    assert lift_of("back_after_absence", st, NOW + days(2.5)) == 0.0
    short = state(last_return_at=NOW, last_return_gap_days=10.0)
    assert lift_of("back_after_absence", short) == 0.0
    [reason] = [m.reason for m in BOOK.active(st, NOW) if m.name == "back_after_absence"]
    assert reason == "Back just now after 45 days away"


# --- payday and festival --------------------------------------------------------------------------


def test_payday_fires_on_the_day_fades_and_ends_after_three_days() -> None:
    full = multiplier("payday") - 1.0
    first = datetime(2026, 9, 1, 6, tzinfo=UTC)
    assert lift_of("payday", state(), first) == pytest.approx(full)
    assert lift_of("payday", state(), first + days(1)) == pytest.approx(full * 0.5 ** (1 / 1.5))
    assert lift_of("payday", state(), first + days(3)) == pytest.approx(full / 4)
    assert lift_of("payday", state(), first + days(4)) == 0.0
    [reason] = [m.reason for m in BOOK.active(state(), first + days(2)) if m.name == "payday"]
    assert reason == "Payday was 2 days ago"


def test_the_calendar_day_is_the_customers_local_day() -> None:
    full = multiplier("payday") - 1.0
    # 19:00 UTC on 31 August is 00:30 on 1 September in Kolkata: payday, whatever UTC says. And
    # 19:00 UTC on 1 September is already 2 September there: a day later, so a faded raise.
    assert lift_of("payday", state(), datetime(2026, 8, 31, 19, tzinfo=UTC)) == pytest.approx(full)
    assert lift_of("payday", state(), datetime(2026, 9, 1, 19, tzinfo=UTC)) == pytest.approx(
        full * 0.5 ** (1 / 1.5)
    )


def test_a_customer_with_no_zone_in_india_is_read_in_the_calendars_zone() -> None:
    plain = state(timezone="UTC")
    full = multiplier("payday") - 1.0
    assert lift_of("payday", plain, datetime(2026, 9, 1, 19, tzinfo=UTC)) == pytest.approx(
        full * 0.5 ** (1 / 1.5)
    )


def test_festival_fires_in_its_window_with_its_own_multiplier() -> None:
    st = state()
    diwali = datetime(2026, 11, 8, 12, tzinfo=UTC)
    assert lift_of("festival", st, diwali) == pytest.approx(0.7)
    assert lift_of("festival", st, diwali - days(10)) == pytest.approx(0.7)  # flat through the window
    assert lift_of("festival", st, diwali - days(11)) == 0.0
    assert lift_of("festival", st, diwali + days(3)) == 0.0
    reasons = {
        d: [m.reason for m in BOOK.active(st, diwali + days(d)) if m.name == "festival"]
        for d in (-3, -1, 0, 1)
    }
    assert reasons == {
        -3: ["Diwali is in 3 days"],
        -1: ["Diwali is tomorrow"],
        0: ["It is Diwali"],
        1: ["Diwali was yesterday"],
    }


def test_the_biggest_festival_wins_when_two_overlap() -> None:
    # 28 December: the year-end sale (1.35) is on and Christmas (1.25, lead five days, no tail) is not.
    when = datetime(2026, 12, 28, 6, tzinfo=UTC)
    assert lift_of("festival", state(), when) == pytest.approx(0.35)
    # 25 December: Christmas alone. From the 26th the year-end sale (two days of lead) is on as well.
    assert lift_of("festival", state(), datetime(2026, 12, 25, 6, tzinfo=UTC)) == pytest.approx(0.25)
    assert lift_of("festival", state(), datetime(2026, 12, 26, 6, tzinfo=UTC)) == pytest.approx(0.35)


def test_a_country_with_no_calendar_gets_no_payday_or_festival() -> None:
    abroad = state(country="US", timezone="America/New_York")
    diwali = datetime(2026, 11, 8, 12, tzinfo=UTC)
    assert BOOK.active(abroad, diwali) == ()
    assert BOOK.active(abroad, datetime(2026, 9, 1, 12, tzinfo=UTC)) == ()


def test_a_customer_with_no_country_is_read_with_the_default_calendar() -> None:
    unknown = state(country=None)
    assert lift_of("festival", unknown, datetime(2026, 11, 8, 12, tzinfo=UTC)) > 0


# --- putting them together -----------------------------------------------------------------------


def test_nothing_live_means_a_factor_of_exactly_one() -> None:
    assert BOOK.factor(state(), NOW) == 1.0
    assert BOOK.active(state(), NOW) == ()
    assert BOOK.reasons(state(), NOW) == ()


def test_the_strongest_counts_in_full_and_the_rest_at_half() -> None:
    st = state(last_cart_at=NOW, viewed_items={"shoe": ItemViews(count=2, last_at=NOW)})
    cart = multiplier("cart_left") - 1.0
    viewed = multiplier("viewed_twice") - 1.0
    assert BOOK.factor(st, NOW) == pytest.approx(1.0 + cart + 0.5 * viewed)
    assert BOOK.reasons(st, NOW)[0].startswith("Left a cart")


def test_the_total_is_capped() -> None:
    cfg = MomentsConfig.model_validate(
        {
            "combine": {"rest_weight": 1.0, "max_multiplier": 1.5},
            "moments": {
                "cart_left": {"label": "c", "multiplier": 1.4},
                "viewed_twice": {"label": "v", "multiplier": 1.4},
            },
        }
    )
    book = MomentBook(cfg)
    st = state(last_cart_at=NOW, viewed_items={"shoe": ItemViews(count=2, last_at=NOW)})
    assert book.factor(st, NOW) == 1.5


def test_a_moment_can_be_switched_off_or_skipped() -> None:
    st = state(last_cart_at=NOW)
    assert BOOK.factor(st, NOW, skip={"cart_left"}) == 1.0
    cfg = load_config()
    off = cfg.model_copy(
        update={
            "moments": {
                **cfg.moments,
                "cart_left": cfg.moments["cart_left"].model_copy(update={"enabled": False}),
            }
        }
    )
    assert MomentBook(off, BOOK.calendars.values()).factor(st, NOW) == 1.0


@pytest.fixture
def restore_registry() -> Iterator[None]:
    saved = dict(SIGNALS)
    yield
    SIGNALS.clear()
    SIGNALS.update(saved)


def test_a_new_moment_can_be_registered(restore_registry: None) -> None:
    def vip(st: CustomerState, spec: MomentSpec, ctx: object) -> ActiveMoment | None:
        return (
            ActiveMoment("vip_day", spec.label, spec.multiplier - 1.0, "A VIP") if st.purchases > 9 else None
        )

    register_moment("vip_day", vip)
    with pytest.raises(ValueError, match="already registered"):
        register_moment("vip_day", vip)
    cfg = MomentsConfig.model_validate({"moments": {"vip_day": {"label": "VIP", "multiplier": 1.2}}})
    assert MomentBook(cfg).factor(state(purchases=12), NOW) == pytest.approx(1.2)
    assert MomentBook(cfg).factor(state(purchases=1), NOW) == 1.0
