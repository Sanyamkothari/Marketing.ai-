"""`GET /campaigns/{id}/plan-preview`: the planner's points on a campaign's own population (M93 x M94).

The "Plan the test" slider steps through these points only (DEC-1204), so every number here must be
one `engine.measurement.planner` computed on the campaign's realised counts: the preview equals
`power_preview` called with the assignment's population, the campaign's realised share is one of
the points (the slider starts on it), the expected rate is the request's or else the registered
plan's, and nothing is invented when no rate is known. A plan frozen after it carries the planner's
own power for its arms, so the card and the plan never disagree.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from api.routes.campaigns import PREVIEW_SHARES
from engine.measurement.campaign import ASSIGNMENT_FILENAME, INTENDED_COLUMN, campaign_key, read_frame
from engine.measurement.plan import realised_population
from engine.measurement.planner import (
    REASON_BASE_RATE_UNKNOWN,
    PowerPreviewRequest,
    achieved_power,
    arm_sizes,
    power_preview,
)
from engine.storage import LocalStorage
from tests.integration.measurement.support import TARGET, ok, propensity_run, upload

pytestmark = pytest.mark.integration

RUN_ID = "r_20261007_94p00001"


@dataclass(frozen=True)
class World:
    client: TestClient
    storage: LocalStorage
    outcomes: str


@pytest.fixture(scope="module")
def world(config_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[World]:
    data_dir = tmp_path_factory.mktemp("plan-preview") / "data"
    data_dir.mkdir()
    storage = LocalStorage(data_dir)
    seeded = propensity_run(storage, RUN_ID, rows=6_000)
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as client:
        yield World(client, storage, upload(client, seeded.outcomes))


def _campaign(world: World) -> str:
    created = ok(world.client.post("/campaigns", json={"run_id": RUN_ID}), 201)
    campaign_id = str(created["campaign"]["campaign_id"])
    ok(world.client.post(f"/campaigns/{campaign_id}/outcomes", json={"upload_id": world.outcomes}))
    return campaign_id


def _preview(world: World, campaign_id: str, **params: Any) -> Any:
    return world.client.get(f"/campaigns/{campaign_id}/plan-preview", params=params)


def test_the_preview_is_the_planner_on_the_assignments_own_counts(world: World) -> None:
    campaign_id = _campaign(world)
    body = ok(_preview(world, campaign_id, base_rate=0.1, value_per_conversion=500))
    realised = realised_population(
        read_frame(world.storage, campaign_key(campaign_id, ASSIGNMENT_FILENAME)),
        intended_column=INTENDED_COLUMN,
    )
    assert (body["eligible"], body["n_treat"], body["n_holdout"]) == (
        realised.population_rows,
        realised.n_treat,
        realised.n_holdout,
    )
    assert body["holdout_fraction"] == realised.holdout_fraction
    assert (body["base_rate"], body["base_rate_source"], body["direction"]) == (0.1, "request", "up")
    shares = tuple(sorted({*PREVIEW_SHARES, realised.holdout_fraction}))
    expected = power_preview(
        PowerPreviewRequest(
            eligible=realised.population_rows,
            base_rate=0.1,
            holdout_shares=shares,
            value_per_conversion=500,
            direction="up",
        )
    )
    assert body["points"] == [point.model_dump(mode="json") for point in expected.points]
    assert body["basis"] == expected.basis and body["reason"] is None
    current = body["points"][body["current_index"]]
    assert current["holdout_share"] == realised.holdout_fraction
    assert (current["n_treat"], current["n_control"]) == (realised.n_treat, realised.n_holdout)
    assert (current["n_treat"], current["n_control"]) == arm_sizes(
        realised.population_rows, current["holdout_share"]
    )


def test_the_shares_asked_for_are_the_only_points(world: World) -> None:
    campaign_id = _campaign(world)
    body = ok(_preview(world, campaign_id, holdout=[0.2, 0.05], base_rate=0.1))
    assert [point["holdout_share"] for point in body["points"]] == [0.05, 0.2]
    assert body["current_index"] is None or body["points"][body["current_index"]]["holdout_share"] == 0.05
    refused = _preview(world, campaign_id, holdout=[0.7])
    assert refused.status_code == 422, refused.text
    assert (refused.json()["detail"]["code"], refused.json()["detail"]["path"]) == (
        "CAMPAIGN_INVALID",
        "holdout",
    )


def test_without_a_rate_no_effect_size_is_invented(world: World) -> None:
    body = ok(_preview(world, _campaign(world)))
    assert body["base_rate"] is None and body["base_rate_source"] is None
    assert body["points"], "the arms are still counted"
    assert all(point["mde_pp"] is None for point in body["points"])
    assert all(REASON_BASE_RATE_UNKNOWN in point["reason"] for point in body["points"])


def test_the_registered_plans_rate_is_used_and_its_power_is_the_planners(world: World) -> None:
    campaign_id = _campaign(world)
    plan_body = {
        "metric": "reactivated within 90 days",
        "outcome_column": TARGET,
        "analysis_date": "2026-08-15",
        "mde_pp": 2.0,
        "base_rate": 0.12,
    }
    plan = ok(world.client.post(f"/campaigns/{campaign_id}/plan", json=plan_body), 201)
    body = ok(_preview(world, campaign_id))
    assert (body["base_rate"], body["base_rate_source"]) == (0.12, "plan")
    assert ok(_preview(world, campaign_id, base_rate=0.2))["base_rate_source"] == "request"
    expected = achieved_power(plan["n_treat"], plan["n_holdout"], 0.12, 0.02, alpha=plan["alpha"])
    assert expected.power is not None
    assert plan["achieved_power"] == round(expected.power, 6)


def test_an_unknown_campaign_has_no_preview(world: World) -> None:
    response = _preview(world, "c_20260101_00000000")
    assert response.status_code == 404 and response.json()["detail"]["code"] == "CAMPAIGN_NOT_FOUND"
