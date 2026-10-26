"""Customer types: deterministic, bounded, built from the profile, and labelled in plain words."""

from __future__ import annotations

from datetime import timedelta

from autonomy.content.config import CopyConfig
from autonomy.content.types import BASE_NAMES, CustomerTypes, choose_k
from autonomy.contracts import CustomerState

from .synth import NOW, make_states


def test_number_of_types_is_bounded_by_the_config() -> None:
    cfg = CopyConfig()
    assert choose_k(10**6, cfg) == 300
    assert choose_k(2_000, cfg) == 100
    assert choose_k(1_000, cfg) == 50
    assert choose_k(500, cfg) == 50  # below the floor: members_per_type would give 25, min_types wins
    assert choose_k(100, cfg) == 20  # a small shop never gets types averaging under 5 customers
    assert choose_k(3, cfg) == 1


def test_fit_is_bounded_and_covers_every_customer() -> None:
    states = make_states(900)
    cfg = CopyConfig(min_types=10, max_types=30, members_per_type=20)
    types = CustomerTypes.fit(states, cfg)
    assert 1 <= len(types) <= cfg.max_types
    assert len(types) == choose_k(900, cfg) == 30
    ids = types.assign_many(states)
    assert set(ids) <= {t.type_id for t in types.types}
    assert sum(t.size for t in types.types) == len(states)
    counts = types.counts(states)
    assert sum(counts.values()) == len(states)
    assert all(t.label for t in types.types)
    assert len({t.label for t in types.types}) == len(types)  # labels are unique
    assert [t.size for t in types.types] == sorted((t.size for t in types.types), reverse=True)


def test_same_states_and_seed_give_the_same_types() -> None:
    states = make_states(600, seed=3)
    cfg = CopyConfig(min_types=8, max_types=20, seed=7)
    a, b = CustomerTypes.fit(states, cfg), CustomerTypes.fit(list(states), cfg)
    assert a.assign_many(states) == b.assign_many(states)
    assert [(t.type_id, t.label, t.size, t.traits) for t in a.types] == [
        (t.type_id, t.label, t.size, t.traits) for t in b.types
    ]


def test_a_large_base_is_fitted_on_a_sample_and_still_fully_assigned() -> None:
    states = make_states(600)
    cfg = CopyConfig(min_types=5, max_types=10, fit_sample=200)
    types = CustomerTypes.fit(states, cfg)
    assert sum(t.size for t in types.types) == 200  # the centres come from the sample...
    assert len(types.assign_many(states)) == 600  # ...and everyone is assigned
    again = CustomerTypes.fit(states, cfg)
    assert types.assign_many(states) == again.assign_many(states)


def test_labels_are_plain_words_about_where_the_type_sits() -> None:
    states = [s for s in make_states(300) if s.customer_id[1:].isdigit() and int(s.customer_id[1:]) % 3 != 2]
    types = CustomerTypes.fit(states, CopyConfig(min_types=2, max_types=2))
    assert len(types) == 2
    labels = " | ".join(t.label for t in types.types)
    assert "lapsed" in labels and "recent buyer" in labels
    assert "frequent buyer" in labels and "high spend" in labels
    assert "deal seeker" in labels and "pays full price" in labels
    for t in types.types:  # no customer data in what the writer is told
        assert not any(s.customer_id in t.note for s in states[:20])
        assert len(t.traits) == 5


def test_profile_features_separate_customers_the_base_set_cannot() -> None:
    base = {
        "customer_id": "x",
        "as_of": NOW,
        "last_purchase_at": NOW - timedelta(days=30),
        "first_seen_at": NOW - timedelta(days=300),
        "purchases": 3,
        "revenue": 100.0,
    }
    states = [
        CustomerState(**{**base, "customer_id": f"a{i}", "features": {"taste": 0.0 + i * 1e-3}})
        for i in range(30)
    ] + [
        CustomerState(**{**base, "customer_id": f"b{i}", "features": {"taste": 9.0 + i * 1e-3}})
        for i in range(30)
    ]
    cfg = CopyConfig(min_types=2, max_types=2)
    types = CustomerTypes.fit(states, cfg)
    ids = types.assign_many(states)
    assert types.profile_names == ("taste",)
    assert len(set(ids[:30])) == 1 and len(set(ids[30:])) == 1 and ids[0] != ids[30]
    # With the profile switched off the two groups are one: nothing else tells them apart.
    plain = CustomerTypes.fit(states, CopyConfig(min_types=2, max_types=2, max_profile_features=0))
    assert plain.profile_names == ()
    assert len(BASE_NAMES) == 9


def test_missing_profile_values_are_filled_not_fatal() -> None:
    states = make_states(60)
    sparse = [
        s.model_copy(update={"features": {} if i % 5 == 0 else s.features}) for i, s in enumerate(states)
    ]
    types = CustomerTypes.fit(sparse, CopyConfig(min_types=3, max_types=6))
    assert len(types.assign_many(sparse)) == 60
