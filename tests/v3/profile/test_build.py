"""Batching, training rows and the runtime's nightly hook all give the same numbers as `profile_for`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from autonomy.contracts import AutonomyLevel, CustomerState
from autonomy.fakes import InMemoryEventStore, InMemoryStateStore
from autonomy.profile import (
    NAMES,
    ShopPrices,
    build_profiles,
    profile_for,
    training_rows,
    training_rows_from_store,
)
from autonomy.runtime.service import RuntimeConfig
from tests.v2.runtime.test_service import CUSTOMERS, SWEEP, world
from tests.v3.profile.synthetic import customer_events, customer_id, events

AS_OF = datetime(2025, 9, 30, tzinfo=UTC)
N = 150
MONTH_ENDS = [datetime(2024, 12, 31, tzinfo=UTC), datetime(2025, 3, 31, tzinfo=UTC), AS_OF]


def _stores(n: int = N) -> tuple[InMemoryEventStore, InMemoryStateStore]:
    store, states = InMemoryEventStore(), InMemoryStateStore()
    store.append(events(n))
    states.put(
        [
            CustomerState(customer_id=customer_id(i), as_of=AS_OF, revenue=float(i), purchases=i % 7)
            for i in range(n)
        ]
    )
    return store, states


def _features(states: InMemoryStateStore) -> dict[str, dict[str, float]]:
    return {s.customer_id: s.features for s in states.scan()}


def test_build_profiles_fills_features_and_the_date_and_touches_nothing_else() -> None:
    store, states = _stores()
    before = {s.customer_id: s for s in states.scan()}
    assert build_profiles(store, states, AS_OF) == N
    for s in states.scan():
        assert s.features_as_of == AS_OF
        assert list(s.features) == list(NAMES)
        assert s.model_copy(update={"features": {}, "features_as_of": None}) == before[s.customer_id]


def test_batching_gives_identical_results_to_one_batch() -> None:
    reference_store, reference_states = _stores()
    build_profiles(reference_store, reference_states, AS_OF, batch_size=10**6)
    expected = _features(reference_states)
    for size in (1, 7, 64, N - 1, N, N + 1):
        store, states = _stores()
        assert build_profiles(store, states, AS_OF, batch_size=size) == N
        assert _features(states) == expected, size


def test_build_profiles_matches_profile_for_with_the_shops_price_level() -> None:
    store, states = _stores()
    build_profiles(store, states, AS_OF, batch_size=32)
    shop = ShopPrices.until(store.scan(), AS_OF)
    assert shop.known
    for i in (0, 5, 77, 140):
        got = states.get(customer_id(i))
        assert got is not None
        assert got.features == profile_for(customer_events(i), AS_OF, shop=shop)
    levels = [s.features["price_level"] for s in states.scan() if s.features["price_level_known"]]
    assert levels and min(levels) < 0.35 and max(levels) > 0.65


def test_price_context_can_be_switched_off() -> None:
    store, states = _stores()
    build_profiles(store, states, AS_OF, price_context=False)
    assert {s.features["price_level"] for s in states.scan()} == {0.5}
    assert {s.features["price_level_known"] for s in states.scan()} == {0.0}


def test_a_customer_without_events_gets_the_defaults_and_the_date() -> None:
    store, states = InMemoryEventStore(), InMemoryStateStore()
    states.put([CustomerState(customer_id="ghost", as_of=AS_OF)])
    assert build_profiles(store, states, AS_OF) == 1
    ghost = states.get("ghost")
    assert ghost is not None and ghost.features_as_of == AS_OF
    assert ghost.features == profile_for([], AS_OF)


def test_build_profiles_rejects_bad_arguments() -> None:
    store, states = _stores(3)
    with pytest.raises(ValueError, match="batch_size"):
        build_profiles(store, states, AS_OF, batch_size=0)
    with pytest.raises(ValueError, match="timezone-aware"):
        build_profiles(store, states, datetime(2025, 9, 30))


def test_training_rows_equal_profile_for_at_each_as_of() -> None:
    everything = list(events(N))
    rows = list(training_rows(everything, MONTH_ENDS))
    assert rows
    shops = ShopPrices.at(everything, MONTH_ENDS)
    seen: set[tuple[str, datetime]] = set()
    for row in rows:
        mine = customer_events(int(row.customer_id[1:]))
        assert row.features == profile_for(mine, row.as_of, shop=shops[row.as_of])
        assert row.features == profile_for(mine, row.as_of, shop=ShopPrices.until(everything, row.as_of))
        seen.add((row.customer_id, row.as_of))
    assert len(seen) == len(rows)  # one row per customer per date
    for i in range(N):
        mine = customer_events(i)
        for d in MONTH_ENDS:
            assert ((customer_id(i), d) in seen) == (mine[0].ts <= d), (i, d)


def test_training_rows_are_in_customer_then_date_order_and_do_not_depend_on_event_order() -> None:
    everything = list(events(40))
    rows = list(training_rows(everything, MONTH_ENDS))
    assert [(r.customer_id, r.as_of) for r in rows] == sorted((r.customer_id, r.as_of) for r in rows)
    assert list(training_rows(reversed(everything), MONTH_ENDS[::-1])) == rows


def test_training_rows_from_the_store_equal_the_in_memory_rows() -> None:
    store, _ = _stores()
    ids = [customer_id(i) for i in range(N)]
    assert list(training_rows_from_store(store, ids, MONTH_ENDS)) == list(
        training_rows(events(N), MONTH_ENDS)
    )


def test_training_rows_agree_with_the_nightly_profile_on_the_same_date() -> None:
    """A model trained on a month-end row and the agent fed tonight's profile see the same numbers."""
    store, states = _stores()
    build_profiles(store, states, AS_OF)
    tonight = _features(states)
    rows = {r.customer_id: r.features for r in training_rows(events(N), [AS_OF])}
    assert rows == {cid: f for cid, f in tonight.items() if cid in rows}
    assert len(rows) > 100


def test_training_rows_need_a_date() -> None:
    with pytest.raises(ValueError, match="at least one"):
        list(training_rows([], []))


# --- the runtime's nightly hook ---------------------------------------------------------------------


def test_the_first_tick_gives_every_state_a_fresh_profile_before_deciding() -> None:
    w = world(autonomy=AutonomyLevel.SHADOW)
    report = w.runtime.tick(SWEEP)
    assert report.rebuilt and not any(a.code == "profile.error" for a in report.alerts)
    assert w.states.count() == len(CUSTOMERS)
    for s in w.states.scan():
        assert s.features_as_of == SWEEP
        assert s.features == profile_for(w.events.for_customer(s.customer_id), SWEEP, shop=_shop(w.events))
        assert s.features["buy_orders_total"] == 1.0


def test_an_ordinary_tick_keeps_the_last_profile_and_a_new_night_refreshes_it() -> None:
    w = world(autonomy=AutonomyLevel.SHADOW)
    w.runtime.tick(SWEEP)
    w.runtime.tick(SWEEP + timedelta(minutes=5))
    assert {s.features_as_of for s in w.states.scan()} == {SWEEP}
    night = SWEEP + timedelta(hours=16)
    w.runtime.tick(night)
    assert {s.features_as_of for s in w.states.scan()} == {night}


def test_the_profile_can_be_turned_off() -> None:
    w = world(autonomy=AutonomyLevel.SHADOW, config=RuntimeConfig(nightly_profile=False))
    w.runtime.tick(SWEEP)
    assert all(not s.features and s.features_as_of is None for s in w.states.scan())


def test_a_profile_failure_is_an_alert_not_a_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_: object, **__: object) -> int:
        raise RuntimeError("disk")

    monkeypatch.setattr("autonomy.runtime.service.build_profiles", boom)
    w = world(autonomy=AutonomyLevel.SHADOW)
    report = w.runtime.tick(SWEEP)
    assert [a.code for a in report.alerts if a.code == "profile.error"] == ["profile.error"]
    assert "RuntimeError" in next(a.message for a in report.alerts if a.code == "profile.error")
    assert len(report.decisions) == len(CUSTOMERS)


def _shop(store: InMemoryEventStore) -> ShopPrices:
    return ShopPrices.until(store.scan(), SWEEP)
