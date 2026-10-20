"""Cross-client priors: the pooling safety rules, blending, seeding, and both stores.

The store tests run against the in-memory fake, SQLite, a JSON directory and Postgres. The Postgres
run needs `MARKETING_AI_TEST_DATABASE_URL` (CI sets it) and skips with that reason when it is absent;
it gets a schema of its own, dropped afterwards.
"""

from __future__ import annotations

import math
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from autonomy import interfaces as ifc
from autonomy.contracts import MIN_CLIENTS_FOR_SHARED_PRIOR, Prior, PriorSource
from autonomy.fakes.stores import InMemoryPriorStore
from autonomy.priors import (
    MAX_CLIENT_WEIGHT,
    ClientStat,
    JsonPriorStore,
    NotEnoughClientsError,
    SqlPriorStore,
    blend,
    capped_weights,
    create_all,
    hash_client,
    pool,
    seed_from,
)

DB_ENV = "MARKETING_AI_TEST_DATABASE_URL"
NOW = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
KEY = "theme_lift/win_back/education"
SALT = "test-salt"


def stat(client: str, n: int, key: str = KEY, **values: float) -> ClientStat:
    return ClientStat(key=key, client_hash=hash_client(client, SALT), n_observations=n, values=values)


def five_clients() -> list[ClientStat]:
    return [stat(f"client-{i}", 100, relative_lift=0.1 * (i + 1)) for i in range(5)]


# --- pooling: the safety rules ----------------------------------------------------------------


def test_refuses_fewer_than_five_clients() -> None:
    assert MIN_CLIENTS_FOR_SHARED_PRIOR == 5
    with pytest.raises(NotEnoughClientsError, match="4 client"):
        pool(five_clients()[:4], KEY, NOW)


def test_five_clients_make_a_prior_without_identities() -> None:
    prior = pool(five_clients(), KEY, NOW)
    assert prior.source is PriorSource.CLIENTS
    assert prior.n_clients == 5 and prior.n_observations == 500
    assert prior.values == {"relative_lift": pytest.approx(0.3)}  # equal sizes: plain mean
    assert prior.updated_at == NOW
    assert set(prior.model_dump()) == {"key", "source", "n_clients", "n_observations", "values", "updated_at"}
    dumped = prior.model_dump_json()
    assert all(s.client_hash not in dumped for s in five_clients())


def test_one_client_split_into_many_stats_still_counts_once() -> None:
    stats = [*five_clients()[:4], *(stat("client-0", 10, relative_lift=0.9) for _ in range(3))]
    with pytest.raises(NotEnoughClientsError):
        pool(stats, KEY, NOW)


def test_empty_and_other_key_stats_do_not_count() -> None:
    stats = [
        *five_clients()[:4],
        stat("client-9", 0, relative_lift=0.5),
        stat("client-8", 50, key="other", x=1.0),
    ]
    with pytest.raises(NotEnoughClientsError):
        pool(stats, KEY, NOW)


def test_a_value_from_fewer_than_five_clients_is_dropped() -> None:
    stats = [*five_clients(), stat("client-0", 100, key=KEY, secret=7.0)]
    prior = pool(stats, KEY, NOW)
    assert set(prior.values) == {"relative_lift"}
    only_rare = [stat(f"c{i}", 10, rare=1.0) if i < 2 else stat(f"c{i}", 10, other=1.0) for i in range(5)]
    with pytest.raises(NotEnoughClientsError, match="no value"):
        pool(only_rare, KEY, NOW)


def test_order_of_stats_does_not_change_the_prior() -> None:
    stats = [stat(f"client-{i}", 10 * (i + 1) ** 2, relative_lift=0.05 * i, cost=1.0 + i) for i in range(7)]
    assert pool(stats, KEY, NOW) == pool(list(reversed(stats)), KEY, NOW)


def test_client_stat_refuses_names_and_customer_fields() -> None:
    with pytest.raises(ValidationError, match="hex digest"):
        ClientStat(key=KEY, client_hash="Acme Retail Ltd", n_observations=10, values={"x": 1.0})
    with pytest.raises(ValidationError):
        ClientStat.model_validate(
            {
                "key": KEY,
                "client_hash": hash_client("a", SALT),
                "n_observations": 1,
                "values": {},
                "customer_id": "c1",
            }
        )
    with pytest.raises(ValidationError, match="non-finite"):
        stat("a", 10, x=math.nan)
    with pytest.raises(ValueError, match="salt"):
        hash_client("a", "")
    assert hash_client("a", SALT) == hash_client("a", SALT) != hash_client("a", "other-salt")


# --- pooling: weights -------------------------------------------------------------------------


def test_weights_follow_sqrt_n_when_no_one_is_over_the_cap() -> None:
    weights = capped_weights([100, 100, 100, 100, 100, 400])
    assert sum(weights) == pytest.approx(1.0)
    assert weights[5] == pytest.approx(2 * weights[0])  # sqrt(400) = 2 * sqrt(100)


def test_no_client_weighs_more_than_thirty_percent() -> None:
    weights = capped_weights([1_000_000, 10, 10, 10, 10])
    assert MAX_CLIENT_WEIGHT == 0.30
    assert weights[0] == pytest.approx(0.30)
    assert sum(weights) == pytest.approx(1.0)
    assert weights[1:] == pytest.approx([0.175] * 4)
    many = capped_weights([10_000, 9_000, 1, 1, 1, 1, 1, 1])
    assert max(many) <= 0.30 + 1e-12 and sum(many) == pytest.approx(1.0)


def test_a_huge_client_cannot_dominate_the_pooled_value() -> None:
    stats = [
        stat("whale", 1_000_000, relative_lift=1.0),
        *(stat(f"c{i}", 10, relative_lift=0.0) for i in range(4)),
    ]
    prior = pool(stats, KEY, NOW)
    assert prior.values["relative_lift"] == pytest.approx(0.30)


def test_pool_weights_clients_by_sqrt_n_and_merges_a_clients_stats_by_n() -> None:
    # sqrt sizes 15, 10, 10, 10, 10: the big client weighs 15/55, under the cap.
    stats = [stat("big", 225, relative_lift=1.0), *(stat(f"c{i}", 100, relative_lift=0.0) for i in range(4))]
    assert pool(stats, KEY, NOW).values["relative_lift"] == pytest.approx(15 / 55)
    # client-0 reports 0.0 over 30 observations and 1.0 over 70: one client, value 0.7, n = 100.
    split = [
        stat("client-0", 30, relative_lift=0.0),
        stat("client-0", 70, relative_lift=1.0),
        *(stat(f"c{i}", 100, relative_lift=0.0) for i in range(4)),
    ]
    prior = pool(split, KEY, NOW)
    assert prior.n_clients == 5 and prior.n_observations == 500
    assert prior.values["relative_lift"] == pytest.approx(0.7 / 5)


def test_cap_needs_enough_clients() -> None:
    with pytest.raises(ValueError, match="cannot each stay under"):
        capped_weights([10, 10, 10])
    with pytest.raises(ValueError, match="positive"):
        capped_weights([10, 0, 10, 10, 10])


# --- blend and seed ---------------------------------------------------------------------------


def test_blend_moves_from_prior_to_local_as_local_data_grows() -> None:
    prior, local = {"rate": 0.10}, {"rate": 0.30}
    assert blend(prior, local, local_n=0, prior_strength=50) == {"rate": pytest.approx(0.10)}
    assert blend(prior, local, local_n=50, prior_strength=50) == {"rate": pytest.approx(0.20)}
    path = [blend(prior, local, local_n=n, prior_strength=50)["rate"] for n in (0, 10, 100, 1_000, 100_000)]
    assert path == sorted(path)
    assert path[-1] == pytest.approx(0.30, abs=1e-3)


def test_blend_passes_one_sided_values_through() -> None:
    out = blend({"a": 1.0, "b": 2.0}, {"b": 4.0, "c": 3.0}, local_n=10, prior_strength=10)
    assert out == {"a": 1.0, "b": pytest.approx(3.0), "c": 3.0}


def test_blend_rejects_bad_inputs() -> None:
    with pytest.raises(ValueError):
        blend({"a": 1.0}, {"a": 1.0}, local_n=-1, prior_strength=10)
    with pytest.raises(ValueError):
        blend({"a": 1.0}, {"a": 1.0}, local_n=1, prior_strength=0)
    with pytest.raises(ValueError):
        blend({"a": math.inf}, {"a": 1.0}, local_n=1, prior_strength=1)


def test_seed_from_gym_and_public_but_never_clients() -> None:
    gym = seed_from(PriorSource.GYM, {"half_life_days": 9.0}, "fatigue/win_back", NOW, n_observations=12_000)
    assert gym.source is PriorSource.GYM and gym.n_clients == 0 and gym.n_observations == 12_000
    public = seed_from(PriorSource.PUBLIC, {"peak_hour": 19.0}, "send_hour/email", NOW)
    assert public.values == {"peak_hour": 19.0}
    with pytest.raises(ValueError, match="pool"):
        seed_from(PriorSource.CLIENTS, {"x": 1.0}, "k", NOW)
    with pytest.raises(ValueError):
        seed_from(PriorSource.GYM, {}, "k", NOW)


# --- stores -----------------------------------------------------------------------------------


@pytest.fixture
def postgres_engine() -> Iterator[Engine]:
    """An engine whose tables live in a fresh schema, dropped when the test ends."""
    url = os.environ.get(DB_ENV, "").strip()
    if not url:
        pytest.skip(f"{DB_ENV} is not set, so the Postgres variant cannot run (CI sets it)")
    base = create_engine(url)
    schema = f"v2prior_{uuid.uuid4().hex[:12]}"
    with base.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        yield base.execution_options(schema_translate_map={None: schema})
    finally:
        with base.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        base.dispose()


@pytest.fixture(params=["fake", "sqlite", "json", "postgres"])
def prior_store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Any]:
    if request.param == "fake":
        yield InMemoryPriorStore()
    elif request.param == "json":
        yield JsonPriorStore(tmp_path / "priors")
    elif request.param == "sqlite":
        engine = create_engine(f"sqlite:///{tmp_path / 'priors.db'}")
        create_all(engine)
        create_all(engine)  # safe twice
        try:
            yield SqlPriorStore(engine)
        finally:
            engine.dispose()
    else:
        engine = request.getfixturevalue("postgres_engine")
        create_all(engine)
        yield SqlPriorStore(engine)


def test_store_round_trips_and_replaces(prior_store: Any) -> None:
    assert isinstance(prior_store, ifc.PriorStore)
    assert prior_store.get(KEY) is None and prior_store.keys() == []

    pooled = pool(five_clients(), KEY, NOW)
    prior_store.put(pooled)
    assert prior_store.get(KEY) == pooled

    ist = timezone(timedelta(hours=5, minutes=30))
    newer = seed_from(
        PriorSource.GYM, {"relative_lift": 0.2, "n_eff": 3.0}, KEY, datetime(2026, 10, 2, tzinfo=ist)
    )
    prior_store.put(newer)
    back = prior_store.get(KEY)
    assert back == newer and back.updated_at == newer.updated_at


def test_store_keys_filter_by_prefix_in_code_point_order(prior_store: Any) -> None:
    keys = ["send_hour/email", "fatigue/win_back", "fatigue/Win_back", "fatigue_x", "Fatigue/a", "fatigue/é"]
    for k in keys:
        prior_store.put(seed_from(PriorSource.PUBLIC, {"v": 1.0}, k, NOW))
    assert prior_store.keys() == sorted(keys)
    assert prior_store.keys("fatigue/") == ["fatigue/Win_back", "fatigue/win_back", "fatigue/é"]
    assert prior_store.keys("nothing") == []
    assert all(prior_store.get(k) is not None and prior_store.get(k).key == k for k in keys)


def test_sql_and_json_stores_refuse_non_finite_values(tmp_path: Path) -> None:
    bad = Prior(key="k", source=PriorSource.GYM, values={"x": math.nan}, updated_at=NOW)
    engine = create_engine(f"sqlite:///{tmp_path / 'p.db'}")
    create_all(engine)
    for store in (SqlPriorStore(engine), JsonPriorStore(tmp_path / "j")):
        with pytest.raises(ValueError, match="non-finite"):
            store.put(bad)
        assert store.keys() == []
    engine.dispose()


def test_sql_store_refuses_keys_longer_than_its_column(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'p.db'}")
    create_all(engine)
    store = SqlPriorStore(engine)
    with pytest.raises(ValueError, match="at most 200"):
        store.put(seed_from(PriorSource.GYM, {"x": 1.0}, "k" * 201, NOW))
    store.put(seed_from(PriorSource.GYM, {"x": 1.0}, "k" * 200, NOW))
    assert store.keys() == ["k" * 200]
    engine.dispose()


def test_json_store_files_are_readable_and_survive_a_new_instance(tmp_path: Path) -> None:
    first = JsonPriorStore(tmp_path / "priors")
    prior = seed_from(PriorSource.GYM, {"half_life_days": 9.0}, "fatigue/win_back", NOW)
    first.put(prior)
    path = first.path_for("fatigue/win_back")
    assert path.name.startswith("fatigue_win_back--") and '"half_life_days": 9.0' in path.read_text()
    assert first.path_for("a/b") != first.path_for("a_b")
    assert JsonPriorStore(tmp_path / "priors").get("fatigue/win_back") == prior
    assert not list((tmp_path / "priors").glob(".tmp-*"))
