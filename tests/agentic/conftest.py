"""One fixture world for every Plan K scenario and route test (a real training and scoring run, built once),
and a campaign of it measured through the product's own routes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.agentic.scenarios.world import SCORE_ROWS, World, build_world
from tests.fixtures.make_uplift_data import make_winback_campaign, outcomes_for


@pytest.fixture(scope="session")
def agentic_world(config_root: Path, tmp_path_factory: pytest.TempPathFactory) -> World:
    return build_world(config_root, tmp_path_factory.mktemp("agentic-world"))


def upload_frame(client: TestClient, frame: Any, *, name: str = "outcomes.csv") -> str:
    """POST /uploads of a frame as CSV; the upload id."""
    payload = frame.to_csv(index=False, lineterminator="\n").encode()
    response = client.post(
        "/uploads",
        files={"file": (name, payload, "text/csv")},
        data={"use_case": "win-back-campaign", "mode": "score"},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["upload_id"])


@pytest.fixture(scope="session")
def measured(agentic_world: World) -> str:
    """A campaign of the world's run, every outcome in at once (a 0-day window), measured through the route."""
    world = agentic_world
    with world.client() as client:
        response = client.post("/campaigns", json={"run_id": world.run_id, "outcome_window_days": 0})
        assert response.status_code == 201, response.text
        campaign_id = str(response.json()["campaign"]["campaign_id"])
        dataset = make_winback_campaign(SCORE_ROWS, seed=11)
        outcomes = outcomes_for(dataset, treated_keys=set(dataset.frame["customer_id"].astype(str)), seed=5)
        upload_id = upload_frame(client, outcomes)
        added = client.post(f"/campaigns/{campaign_id}/outcomes", json={"upload_id": upload_id})
        assert added.status_code == 200, added.text
        report = client.post(f"/campaigns/{campaign_id}/measure", json={})
        assert report.status_code == 200, report.text
    return campaign_id
