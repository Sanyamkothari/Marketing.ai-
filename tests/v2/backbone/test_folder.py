"""The production StateFolder and the rebuild / incremental paths that use it.

Property-style checks run over seeded random event streams (no hypothesis dependency): folding in
order equals rebuilding, and the folder agrees with SimpleStateFolder on every field the fake computes.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest

from autonomy.contracts import Channel, CustomerState, Event, EventType, Group
from autonomy.data import SegmentRules, StateFolder, apply_events, rebuild_states
from autonomy.data.folder import OFFER_PRIOR, OFFER_PSEUDO_COUNT
from autonomy.fakes.folder import SimpleStateFolder
from autonomy.fakes.stores import InMemoryEventStore, InMemoryStateStore
from autonomy.interfaces import StateFolder as StateFolderProtocol

T0 = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
CHANNELS = (Channel.EMAIL, Channel.WHATSAPP, Channel.SMS)
NEEDS_CHANNEL = {
    EventType.SEND,
    EventType.DELIVER,
    EventType.OPEN,
    EventType.CLICK,
    EventType.REPLY,
    EventType.BOUNCE,
    EventType.UNSUBSCRIBE,
    EventType.COMPLAINT,
    EventType.BLOCK,
    EventType.FAIL,
    EventType.CONSENT_GRANTED,
    EventType.CONSENT_REVOKED,
}
ZONES = ("America/New_York", "Asia/Kolkata", "Europe/Berlin", "Not/AZone")


def ev(n: int, cid: str, t: EventType, ts: datetime, **kw: object) -> Event:
    if t in NEEDS_CHANNEL and "channel" not in kw:
        kw["channel"] = Channel.EMAIL
    if t in (EventType.PURCHASE, EventType.REFUND) and "value" not in kw:
        kw["value"] = 10.0
    return Event(event_id=f"e{n}", customer_id=cid, type=t, ts=ts, source="test", **kw)  # type: ignore[arg-type]


def random_stream(seed: int, *, customers: int = 6, n: int = 400, profile_attrs: bool = False) -> list[Event]:
    """A time-ordered stream across a few customers and every event type."""
    rng = random.Random(seed)
    types = list(EventType)
    out: list[Event] = []
    ts = T0
    for i in range(n):
        ts += timedelta(minutes=rng.randint(1, 60 * 30))
        cid = f"c{rng.randrange(customers)}"
        t = rng.choice(types)
        kw: dict[str, object] = {}
        if t in NEEDS_CHANNEL:
            kw["channel"] = rng.choice(CHANNELS)
        if t is EventType.PURCHASE:
            kw["value"] = round(rng.uniform(5, 300), 2)
            kw["margin"] = round(rng.uniform(1, 50), 2) if rng.random() < 0.7 else None
            kw["category"] = rng.choice(["shoes", "bags", "hats", "socks", None])
            if rng.random() < 0.4:
                kw["attrs"] = {"offer_id": f"o{rng.randrange(3)}"}
        elif t is EventType.REFUND:
            kw["value"] = round(rng.uniform(5, 100), 2)
        elif t is EventType.PREFERENCE:
            kw["attrs"] = {"key": rng.choice(["no_discounts", "topic"]), "value": rng.choice(["true", "x"])}
        elif profile_attrs and t in (EventType.SIGNUP, EventType.VISIT) and rng.random() < 0.6:
            kw["attrs"] = {"timezone": rng.choice(ZONES), "country": rng.choice(["us", "IN", "Germany"])}
        out.append(ev(i, cid, t, ts, **kw))
    return out


def fold_all(folder: StateFolderProtocol, events: list[Event]) -> dict[str, CustomerState]:
    states: dict[str, CustomerState] = {}
    for e in events:
        states[e.customer_id] = folder.fold(states.get(e.customer_id), e)
    return states


def test_satisfies_the_protocol() -> None:
    assert isinstance(StateFolder(), StateFolderProtocol)


# --- agreement with the fake --------------------------------------------------------------------

NEW_FIELDS = {
    "offer_sensitivity",
    "segment",
    # moments (plan-v3 decision 2): views, carts, gaps and returns, which the fake does not keep
    "last_view_at",
    "last_cart_at",
    "viewed_items",
    "usual_gap_days",
    "purchase_gap_days",
    "last_return_at",
    "last_return_gap_days",
}


@pytest.mark.parametrize("seed", range(8))
def test_agrees_with_simple_folder_on_shared_fields(seed: int) -> None:
    events = random_stream(seed)
    ours, fake = fold_all(StateFolder(), events), fold_all(SimpleStateFolder(), events)
    assert ours.keys() == fake.keys()
    now = events[-1].ts + timedelta(days=3)
    for cid in ours:
        a = StateFolder().refresh(ours[cid], now).model_dump(exclude=NEW_FIELDS)
        b = SimpleStateFolder().refresh(fake[cid], now).model_dump(exclude=NEW_FIELDS)
        assert a == b, cid


@pytest.mark.parametrize("seed", range(4))
def test_agrees_with_simple_folder_apart_from_profile_when_zones_are_known(seed: int) -> None:
    events = random_stream(seed, profile_attrs=True)
    ours, fake = fold_all(StateFolder(), events), fold_all(SimpleStateFolder(), events)
    local = NEW_FIELDS | {"timezone", "country", "hour_counts", "active_hours"}
    for cid in ours:
        assert ours[cid].model_dump(exclude=local) == fake[cid].model_dump(exclude=local)
        # the histogram counts the same engagements, just on the customer's own clock
        assert sum(ours[cid].hour_counts) == sum(fake[cid].hour_counts)


# --- folding in order equals rebuilding ---------------------------------------------------------


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("seed", range(6))
def test_incremental_folding_equals_rebuild(seed: int, live: bool) -> None:
    events = random_stream(seed, profile_attrs=True)
    now = events[-1].ts + timedelta(days=2)
    folder = StateFolder()
    rng = random.Random(seed)

    incremental = InMemoryStateStore()
    i = 0
    while i < len(events):
        step = rng.randint(1, 60)
        chunk = events[i : i + step]
        # live: each chunk is applied and refreshed at the time it arrives, as the runtime will do
        at = chunk[-1].ts + timedelta(minutes=rng.randint(0, 30)) if live else None
        apply_events(chunk, incremental, folder, now=at, batch_size=rng.randint(1, 4))
        i += step

    log = InMemoryEventStore()
    log.append(events)
    rebuilt = InMemoryStateStore()
    written = rebuild_states(log, rebuilt, folder, now, batch_size=3)

    assert written == rebuilt.count() == incremental.count()
    for s in rebuilt.scan():
        assert folder.refresh(incremental.get(s.customer_id), now) == s  # type: ignore[arg-type]


def test_rebuild_is_deterministic_and_keeps_group_and_scores() -> None:
    events = random_stream(11)
    log = InMemoryEventStore()
    log.append(events)
    now = events[-1].ts
    store = InMemoryStateStore()
    rebuild_states(log, store, StateFolder(), now)
    first = list(store.scan())

    held = store.get("c0")
    assert held is not None
    store.put([held.model_copy(update={"group": Group.HOLDOUT, "scores": {"churn_risk": 0.4}})])
    rebuild_states(log, store, StateFolder(), now)
    after = store.get("c0")
    assert after is not None
    assert after.group is Group.HOLDOUT and after.scores == {"churn_risk": 0.4}
    assert [s for s in store.scan() if s.customer_id != "c0"] == [s for s in first if s.customer_id != "c0"]


def test_rebuild_rejects_unknown_keep_field() -> None:
    with pytest.raises(ValueError, match="not CustomerState fields"):
        rebuild_states(InMemoryEventStore(), InMemoryStateStore(), StateFolder(), T0, keep=("grup",))


def test_apply_events_counts_customers_and_refreshes_when_asked() -> None:
    store = InMemoryStateStore()
    events = [ev(i, f"c{i % 3}", EventType.SEND, T0 + timedelta(days=i)) for i in range(6)]
    now = T0 + timedelta(days=34)  # the 30-day window starts after day 4: days 0-4 age out, day 5 stays
    assert apply_events(events, store, StateFolder(), now=now, batch_size=2) == 3
    windows = {s.customer_id: (s.as_of, s.contacts_30d) for s in store.scan()}
    assert windows == {"c0": (now, 0), "c1": (now, 0), "c2": (now, 1)}


def test_late_event_does_not_move_timestamps_backwards() -> None:
    f = StateFolder()
    s = f.fold(None, ev(1, "c", EventType.VISIT, T0 + timedelta(days=5)))
    s = f.fold(s, ev(2, "c", EventType.VISIT, T0))
    assert s.last_seen_at == T0 + timedelta(days=5)
    assert s.first_seen_at == T0


def test_fold_rejects_another_customers_state() -> None:
    f = StateFolder()
    s = f.fold(None, ev(1, "a", EventType.VISIT, T0))
    with pytest.raises(ValueError):
        f.fold(s, ev(2, "b", EventType.VISIT, T0))


# --- profile: timezone, country, local active hours ---------------------------------------------


def test_timezone_and_country_from_signup_and_visit() -> None:
    f = StateFolder()
    s = f.fold(None, ev(1, "c", EventType.SIGNUP, T0, attrs={"timezone": "Asia/Kolkata", "country": "in"}))
    assert (s.timezone, s.country) == ("Asia/Kolkata", "IN")
    s = f.fold(s, ev(2, "c", EventType.VISIT, T0, attrs={"timezone": "Mars/Olympus", "country": "Germany"}))
    assert (s.timezone, s.country) == ("Asia/Kolkata", "Germany")  # unknown zone ignored
    s = f.fold(s, ev(3, "c", EventType.CLICK, T0, attrs={"timezone": "Europe/Berlin", "country": "DE"}))
    assert (s.timezone, s.country) == ("Asia/Kolkata", "Germany")  # outbound events never set the profile


def test_country_from_purchases_as_order_exports_carry_it() -> None:
    # The online-retail loaders put Country on every invoice and emit no signup event.
    f = StateFolder()
    s = f.fold(
        None, ev(1, "c", EventType.PURCHASE, T0, attrs={"invoice_no": "536365", "country": "United Kingdom"})
    )
    assert s.country == "United Kingdom"
    s = f.fold(s, ev(2, "c", EventType.CART, T0 + timedelta(hours=1), attrs={"timezone": "Europe/London"}))
    assert s.timezone == "Europe/London"


def test_active_hours_are_local_to_the_customer() -> None:
    f = StateFolder()
    s = f.fold(None, ev(1, "c", EventType.SIGNUP, T0, attrs={"timezone": "America/New_York"}))
    click_at = datetime(2026, 7, 1, 14, 0, tzinfo=UTC)  # 10:00 in New York (EDT, UTC-4)
    s = f.fold(s, ev(2, "c", EventType.CLICK, click_at))
    s = f.fold(s, ev(3, "c", EventType.REPLY, click_at + timedelta(hours=1)))
    assert s.hour_counts[10] == 1 and s.hour_counts[11] == 1
    assert sum(s.hour_counts) == 2
    assert s.active_hours[10] == pytest.approx(0.5)
    assert sum(s.active_hours) == pytest.approx(1.0)


def test_learning_the_zone_late_moves_earlier_engagement_to_local_hours() -> None:
    f = StateFolder()
    click_at = datetime(2026, 1, 10, 15, 0, tzinfo=UTC)
    s = f.fold(None, ev(1, "c", EventType.CLICK, click_at))
    assert s.hour_counts[15] == 1  # counted in UTC: zone unknown
    s = f.fold(s, ev(2, "c", EventType.VISIT, click_at, attrs={"timezone": "America/New_York"}))
    assert s.hour_counts[10] == 1 and sum(s.hour_counts) == 1  # EST, UTC-5
    assert s.active_hours[10] == 1.0


# --- offer sensitivity --------------------------------------------------------------------------


def expected_sensitivity(offers: int, purchases: int) -> float:
    return (offers + OFFER_PRIOR * OFFER_PSEUDO_COUNT) / (purchases + OFFER_PSEUDO_COUNT)


def test_offer_sensitivity_is_a_shrunk_share() -> None:
    f = StateFolder()
    s = f.fold(None, ev(0, "c", EventType.VISIT, T0))
    assert s.offer_sensitivity is None  # no purchases, no estimate
    pattern = [True, False, True, True, False, True, True]
    offers = 0
    for i, with_offer in enumerate(pattern, start=1):
        attrs: dict[str, str | int | float | bool] = {"offer_id": "spring10"} if with_offer else {}
        s = f.fold(s, ev(i, "c", EventType.PURCHASE, T0 + timedelta(hours=i), attrs=attrs))
        offers += with_offer
        assert s.offer_sensitivity == pytest.approx(expected_sensitivity(offers, i))
        assert f.offer_purchases(s) == offers
    assert s.offer_sensitivity == pytest.approx((5 + 0.9) / 10)


def test_one_full_price_order_stays_near_the_prior() -> None:
    f = StateFolder()
    s = f.fold(None, ev(1, "c", EventType.PURCHASE, T0))
    assert s.offer_sensitivity == pytest.approx(0.9 / 4)
    s = f.fold(None, ev(2, "d", EventType.PURCHASE, T0, attrs={"offer_id": ""}))
    assert s.offer_sensitivity == pytest.approx(0.9 / 4)  # an empty offer id is no offer


def test_offer_count_survives_many_purchases() -> None:
    f = StateFolder()
    s: CustomerState | None = None
    for i in range(2000):
        attrs: dict[str, str | int | float | bool] = {"offer_id": "x"} if i % 3 == 0 else {}
        s = f.fold(s, ev(i, "c", EventType.PURCHASE, T0 + timedelta(minutes=i), attrs=attrs))
    assert s is not None and f.offer_purchases(s) == 667


# --- channel responsiveness and contact windows -------------------------------------------------


def test_channel_counts_track_responsiveness() -> None:
    f = StateFolder()
    seq = [
        EventType.SEND,
        EventType.DELIVER,
        EventType.OPEN,
        EventType.CLICK,
        EventType.SEND,
        EventType.BOUNCE,
    ]
    s: CustomerState | None = None
    for i, t in enumerate(seq):
        s = f.fold(s, ev(i, "c", t, T0 + timedelta(hours=i), channel=Channel.WHATSAPP))
    assert s is not None
    stats = s.channels[Channel.WHATSAPP]
    assert (stats.sends, stats.delivers, stats.clicks, stats.bounces) == (2, 1, 1, 1)
    assert stats.last_engaged_at == T0 + timedelta(hours=3)


def test_refresh_keeps_contact_windows_correct() -> None:
    f = StateFolder()
    s: CustomerState | None = None
    for i, days in enumerate([0, 10, 20, 25, 28]):
        s = f.fold(s, ev(i, "c", EventType.SEND, T0 + timedelta(days=days)))
    assert s is not None
    assert (s.contacts_7d, s.contacts_30d) == (2, 5)
    later = f.refresh(s, T0 + timedelta(days=33))
    assert (later.contacts_7d, later.contacts_30d) == (1, 4)  # day 0 has aged out; only 28 within 7
    assert later.recent_contacts == tuple(T0 + timedelta(days=d) for d in (10, 20, 25, 28))
    gone = f.refresh(s, T0 + timedelta(days=60))
    assert (gone.contacts_7d, gone.contacts_30d, gone.recent_contacts) == (0, 0, ())


# --- segments -----------------------------------------------------------------------------------


def buyer(purchases: int, revenue: float, last: datetime) -> CustomerState:
    return CustomerState(
        customer_id="c", as_of=last, purchases=purchases, revenue=revenue, last_purchase_at=last
    )


@pytest.mark.parametrize(
    ("purchases", "revenue", "days_since", "label"),
    [
        (0, 0.0, 0, "prospect"),
        (1, 20.0, 0, "new"),
        (2, 40.0, 10, "active"),
        (5, 500.0, 10, "vip"),  # both VIP bars met exactly
        (5, 499.99, 10, "active"),
        (4, 9000.0, 10, "active"),
        (1, 20.0, 90, "new"),  # exactly 90 days: not yet lapsing
        (1, 20.0, 91, "lapsing"),
        (5, 900.0, 91, "lapsing"),  # recency beats VIP
        (2, 40.0, 180, "lapsing"),  # exactly 180 days: not yet lapsed
        (2, 40.0, 181, "lapsed"),
    ],
)
def test_segments_at_the_thresholds(purchases: int, revenue: float, days_since: int, label: str) -> None:
    last = T0
    state = buyer(purchases, revenue, last) if purchases else CustomerState(customer_id="c", as_of=T0)
    assert StateFolder().refresh(state, last + timedelta(days=days_since)).segment == label


def test_segment_moves_with_the_clock_and_custom_rules() -> None:
    f = StateFolder()
    s = f.fold(None, ev(1, "c", EventType.PURCHASE, T0, value=50.0))
    assert s.segment == "new"
    assert f.refresh(s, T0 + timedelta(days=200)).segment == "lapsed"
    strict = StateFolder(
        segments=SegmentRules(lapsing_after=timedelta(days=7), lapsed_after=timedelta(days=14))
    )
    assert strict.refresh(s, T0 + timedelta(days=8)).segment == "lapsing"
    with pytest.raises(ValueError):
        SegmentRules(lapsing_after=timedelta(days=30), lapsed_after=timedelta(days=10))


# --- in the reference loop ----------------------------------------------------------------------


def test_reference_loop_state_matches_a_rebuild_of_its_log() -> None:
    from autonomy.fakes import ContactEveryonePolicy, FakeEnvironment, InMemoryDecisionLog
    from autonomy.fakes.samples import sample_brief, sample_content
    from autonomy.loop import run_days

    start = datetime(2026, 10, 1, tzinfo=UTC)
    log, live, folder = InMemoryEventStore(), InMemoryStateStore(), StateFolder()
    run_days(
        env=FakeEnvironment(n_customers=30, start=start, history_days=10),
        policy=ContactEveryonePolicy(),
        events=log,
        states=live,
        decisions=InMemoryDecisionLog(),
        folder=folder,
        briefs=[sample_brief(start)],
        content=sample_content(start),
        days=5,
    )
    end = start + timedelta(days=6)
    rebuilt = InMemoryStateStore()
    rebuild_states(log, rebuilt, folder, end)
    assert rebuilt.count() == live.count() > 0
    assert any(s.contacts_7d for s in rebuilt.scan())
    for s in rebuilt.scan():
        current = live.get(s.customer_id)
        assert current is not None
        assert folder.refresh(current, end) == s
