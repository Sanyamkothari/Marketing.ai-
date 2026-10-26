"""The registry: every feature is defined once, described, and has a finite default."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from autonomy.contracts import Event, EventType
from autonomy.profile import BY_NAME, FEATURES, NAMES, FeatureGroup, defaults, profile_for
from tests.v3.profile.synthetic import customer_events

AS_OF = datetime(2025, 6, 30, tzinfo=UTC)


def test_every_feature_has_a_description_and_a_finite_default() -> None:
    assert FEATURES
    for f in FEATURES:
        assert f.description.strip(), f.name
        assert len(f.description) <= 160, f"{f.name}: a description is one line"
        assert isinstance(f.default, float) and math.isfinite(f.default), f.name


def test_names_are_unique_and_follow_their_group() -> None:
    assert len(set(NAMES)) == len(NAMES) == len(BY_NAME)
    prefixes = {
        FeatureGroup.BUYING: "buy_",
        FeatureGroup.TREND: "trend_",
        FeatureGroup.TASTE: "taste_",
        FeatureGroup.PRICE: "price_",
        FeatureGroup.TIME: "time_",
        FeatureGroup.CHANNELS: "ch_",
        FeatureGroup.RESPONSES: "resp_",
        FeatureGroup.MOMENTS: "mom_",
    }
    for f in FEATURES:
        assert f.name.startswith(prefixes[f.group]), f.name


def test_all_eight_groups_are_present_and_the_size_is_about_fifty() -> None:
    assert {f.group for f in FEATURES} == set(FeatureGroup)
    assert 40 <= len(FEATURES) <= 60


def test_a_customer_with_no_events_gets_exactly_the_defaults() -> None:
    assert profile_for([], AS_OF) == defaults()


def test_the_profile_has_every_feature_and_only_finite_floats() -> None:
    for i in range(40):
        profile = profile_for(customer_events(i), AS_OF)
        assert list(profile) == list(NAMES)
        for name, value in profile.items():
            assert type(value) is float and math.isfinite(value), (i, name, value)


def test_a_non_finite_amount_does_not_leak_into_the_profile() -> None:
    odd = [
        Event(
            event_id=f"e{i}",
            customer_id="c",
            type=EventType.PURCHASE,
            ts=AS_OF - timedelta(days=10 * (i + 1)),
            source="t",
            value=value,
            margin=value,
        )
        for i, value in enumerate((float("nan"), float("inf"), 20.0))
    ]
    profile = profile_for(odd, AS_OF)
    assert all(math.isfinite(v) for v in profile.values())
    assert profile["buy_orders_total"] == 3.0
    assert profile["buy_net_revenue"] == 20.0


def test_naive_as_of_and_mixed_customers_are_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        profile_for([], datetime(2025, 6, 30))
    two = [
        Event(event_id=f"e{c}", customer_id=c, type=EventType.VISIT, ts=AS_OF - timedelta(days=1), source="t")
        for c in ("a", "b")
    ]
    with pytest.raises(ValueError, match="one customer"):
        profile_for(two, AS_OF)
