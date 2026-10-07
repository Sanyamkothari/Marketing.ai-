"""A campaign's explore slice comes from its run's `holdout_assignment.parquet` (M92 x M94 x M95).

`scores.*` never carries the explore flag (DEC-1302 (c)); an engaged run writes it, with its probability,
in `holdout_assignment.parquet`. Through the product's own API, over a Phase 1 propensity run written
into the store with that file beside its scores:

* `POST /campaigns` joins `explore` and `explore_probability` onto `assignment.parquet` by the key, so
  the campaign's `counts.explore` is the run's explore slice, not 0;
* `GET /campaigns/{id}/plan-preview` reports that slice as `n_explore` and prices it with
  `planner.cost_of_explore`, whether the campaign is measured on every eligible customer or on its
  treat bands (the slice lies outside the selection, so a band campaign still paid for it);
* a run that wrote no such file (a default run) explores nobody.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine.config import load_use_case
from engine.holdout.assign import HOLDOUT_ASSIGNMENT_FILENAME
from engine.measurement.campaign import ASSIGNMENT_FILENAME, campaign_key, read_frame
from engine.measurement.planner import cost_of_explore
from engine.storage import LocalStorage, run_key
from tests.integration.measurement.support import PRIMARY_KEY, USE_CASE, ok, propensity_run

pytestmark = pytest.mark.integration

ENGAGED_RUN = "r_20261007_95e00001"
DEFAULT_RUN = "r_20261007_95e00002"
EXPLORE_FRACTION = 0.05
CONTACT_COST = 10.0
OFFER_COST = 50.0


@dataclass(frozen=True)
class World:
    client: TestClient
    storage: LocalStorage
    table: pd.DataFrame
    """The engaged run's `holdout_assignment.parquet`."""


def _holdout_table(scores: pd.DataFrame) -> pd.DataFrame:
    """The table an engaged run under `scope: run` writes: members are its control group, and every
    twentieth eligible, unselected (lowest band), non-member customer is explored."""
    lowest = load_use_case(USE_CASE).actions.bands[-1].name
    eligible = scores["suppressed_reason"].isna()
    member = scores["control_group"].astype(bool)
    selected = scores["band"].astype(str) != lowest
    candidate = eligible & ~selected & ~member
    explore = candidate & (pd.Series(range(len(scores.index)), index=scores.index) % 20 == 0)
    treated = (eligible & selected & ~member) | explore
    return pd.DataFrame(
        {
            PRIMARY_KEY: scores[PRIMARY_KEY],
            "holdout_member": member,
            "explore": explore,
            "explore_probability": candidate.astype(float) * EXPLORE_FRACTION,
            "treated": treated,
            "treatment_probability": treated.astype(float) * 0.9,
        }
    ).sample(
        frac=1.0, random_state=3
    )  # the join is on the key, never on the row order


@pytest.fixture(scope="module")
def world(config_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[World]:
    data_dir = tmp_path_factory.mktemp("campaign-explore") / "data"
    data_dir.mkdir()
    storage = LocalStorage(data_dir)
    engaged = propensity_run(storage, ENGAGED_RUN, rows=6_000)
    propensity_run(storage, DEFAULT_RUN, rows=2_000, seed=12)
    table = _holdout_table(engaged.scores)
    buffer = io.BytesIO()
    table.to_parquet(buffer, index=False)
    storage.write_bytes(run_key(ENGAGED_RUN, HOLDOUT_ASSIGNMENT_FILENAME), buffer.getvalue())
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as client:
        yield World(client, storage, table)


def _preview(world: World, campaign_id: str) -> dict[str, object]:
    params = {"base_rate": 0.1, "contact_cost": CONTACT_COST, "offer_cost": OFFER_COST}
    body: dict[str, object] = ok(world.client.get(f"/campaigns/{campaign_id}/plan-preview", params=params))
    return body


def test_the_assignment_takes_the_explore_flag_and_probability_from_the_run(world: World) -> None:
    explored = int(world.table["explore"].sum())
    assert explored > 0, "the fixture explores somebody"
    created = ok(world.client.post("/campaigns", json={"run_id": ENGAGED_RUN}), 201)
    campaign_id = str(created["campaign"]["campaign_id"])
    assert created["campaign"]["counts"]["explore"] == explored

    assignment = read_frame(world.storage, campaign_key(campaign_id, ASSIGNMENT_FILENAME))
    expected = world.table.set_index(PRIMARY_KEY)
    joined = assignment.set_index(PRIMARY_KEY)
    assert joined["explore"].equals(expected.loc[joined.index, "explore"].astype(bool))
    assert (
        joined["explore_probability"].tolist() == expected.loc[joined.index, "explore_probability"].tolist()
    )

    preview = _preview(world, campaign_id)
    assert preview["n_explore"] == explored
    cost = cost_of_explore(explored, CONTACT_COST, OFFER_COST).amount
    assert cost is not None and cost.low == explored * CONTACT_COST
    points = preview["points"]
    assert isinstance(points, list) and points
    assert all(point["cost_of_explore"] == cost.model_dump(mode="json") for point in points)


def test_a_band_campaign_still_counts_and_prices_the_slice_outside_its_selection(world: World) -> None:
    explored = int(world.table["explore"].sum())
    created = ok(
        world.client.post("/campaigns", json={"run_id": ENGAGED_RUN, "bands": ["High", "Medium"]}), 201
    )
    campaign_id = str(created["campaign"]["campaign_id"])
    preview = _preview(world, campaign_id)
    assert (
        preview["n_explore"] == explored
    ), "the explore slice lies outside the treat bands, and was still sent"
    cost = cost_of_explore(explored, CONTACT_COST, OFFER_COST).amount
    assert cost is not None
    points = preview["points"]
    assert isinstance(points, list)
    assert all(point["cost_of_explore"] == cost.model_dump(mode="json") for point in points)


def test_a_run_without_a_holdout_assignment_explores_nobody(world: World) -> None:
    created = ok(world.client.post("/campaigns", json={"run_id": DEFAULT_RUN}), 201)
    campaign_id = str(created["campaign"]["campaign_id"])
    assert created["campaign"]["counts"]["explore"] == 0
    assignment = read_frame(world.storage, campaign_key(campaign_id, ASSIGNMENT_FILENAME))
    assert "explore" not in assignment.columns
    assert _preview(world, campaign_id)["n_explore"] == 0
