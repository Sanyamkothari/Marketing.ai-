"""`POST /measurement/power-preview` (Plan J M93): its access policy, its contract and its answers.

The router is mounted here on a fresh `create_app()` exactly as `api/main.py`'s PLAN-J block will
mount it (M90 adds that block; until then the integrator registers it). `include_router` after the
app is built still carries the global access dependency, so sign-in and roles are enforced as in the
served app.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.access_policy import MUTATING_METHODS, all_policies, refusal_message
from api.main import create_app
from api.routes.measurement import POLICIES
from api.routes.measurement import router as measurement_router
from engine.access.roles import Role
from engine.measurement.planner import PowerPreviewRequest, power_preview
from tests.integration.production.access_support import bearer, live_routes, local_app, make_user

pytestmark = pytest.mark.integration

KEY = ("POST", "/measurement/power-preview")
BODY: dict[str, Any] = {
    "eligible": 40_000,
    "base_rate": 0.04,
    "holdout_shares": [0.03, 0.05, 0.10, 0.15],
    "explore_share": 0.01,
    "value_per_conversion": 800,
    "contact_cost": 0.5,
    "offer_cost": 100,
}


def _mounted(app: FastAPI) -> FastAPI:
    app.include_router(measurement_router)
    return app


def test_the_route_has_a_well_formed_viewer_policy() -> None:
    policy = all_policies()[KEY]
    assert policy is POLICIES[KEY]
    assert policy.role is Role.VIEWER
    assert policy.action == "measurement.power_preview"
    assert not policy.audit_reads and policy.object_param is None
    assert KEY[0] in MUTATING_METHODS  # audited like every POST; it carries counts only
    assert refusal_message(policy) == "Only a Viewer can preview how big a test needs to be."


def test_every_route_of_the_router_has_a_policy() -> None:
    app = _mounted(create_app())
    served = {
        (route.method, route.path) for route in live_routes(app) if route.path.startswith("/measurement")
    }
    assert served == set(POLICIES)


def test_the_preview_answers_with_the_planners_numbers(tmp_path: Path) -> None:
    with TestClient(_mounted(create_app(data_dir=tmp_path))) as client:
        response = client.post("/measurement/power-preview", json=BODY)
    assert response.status_code == 200, response.text
    expected = power_preview(PowerPreviewRequest.model_validate(BODY))
    assert response.json() == expected.model_dump(mode="json")
    points = response.json()["points"]
    assert [point["holdout_share"] for point in points] == BODY["holdout_shares"]
    assert all(point["mde_pp"] is not None and point["reason"] is None for point in points)
    assert set(points[0]) == {
        "holdout_share",
        "n_treat",
        "n_control",
        "mde_pp",
        "cost_of_holdout",
        "cost_of_explore",
        "reason",
    }


def test_an_unknown_base_rate_is_null_with_a_reason(tmp_path: Path) -> None:
    with TestClient(_mounted(create_app(data_dir=tmp_path))) as client:
        response = client.post("/measurement/power-preview", json={**BODY, "base_rate": None})
    assert response.status_code == 200
    point = response.json()["points"][0]
    assert point["mde_pp"] is None and point["cost_of_holdout"] is None
    assert "base rate is not known" in point["reason"]


@pytest.mark.parametrize(
    "change",
    [
        {"holdout_shares": [0.7]},
        {"eligible": 0},
        {"base_rate": 1.5},
        {"explore_share": 0.5},
        {"customer_ids": ["C1"]},
    ],
)
def test_a_bad_body_is_refused(tmp_path: Path, change: dict[str, Any]) -> None:
    with TestClient(_mounted(create_app(data_dir=tmp_path))) as client:
        response = client.post("/measurement/power-preview", json={**BODY, **change})
    assert response.status_code == 422


def test_with_sign_in_on_a_viewer_may_preview_and_nobody_signed_out_may(tmp_path: Path) -> None:
    app = _mounted(local_app(tmp_path))
    viewer = make_user(app, "viewer", [Role.VIEWER])
    with TestClient(app) as client:
        refused = client.post("/measurement/power-preview", json=BODY)
        allowed = client.post("/measurement/power-preview", json=BODY, headers=bearer(app, viewer))
    assert refused.status_code == 401
    assert allowed.status_code == 200, allowed.text
