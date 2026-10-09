"""Every campaign route M94 adds has its role, and a caller without it is refused (plan M46, DEC-716).

Reads are Viewer (every role implies it, DEC-703), so without a sign-in they are `401
AUTH_REQUIRED`; every write - creating a campaign, its outcomes, measuring it, registering or amending
its test plan - is Analyst, so a principal holding every *other* role is `403 ROLE_REQUIRED` before
the route runs, and the refusal is audited. No route puts a customer id in its path (DEC-746).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.access_policy import MUTATING_METHODS, policy_for
from engine.access.roles import Role
from engine.audit.events import AuditQuery
from tests.integration.production.access_support import (
    audit_log_at,
    bearer,
    live_routes,
    local_app,
    make_user,
)

pytestmark = pytest.mark.integration

ROUTES: dict[tuple[str, str], Role] = {
    ("POST", "/campaigns"): Role.ANALYST,
    ("GET", "/campaigns"): Role.VIEWER,
    ("GET", "/campaigns/{campaign_id}"): Role.VIEWER,
    ("POST", "/campaigns/{campaign_id}/outcomes"): Role.ANALYST,
    ("POST", "/campaigns/{campaign_id}/measure"): Role.ANALYST,
    ("GET", "/campaigns/{campaign_id}/plan"): Role.VIEWER,
    ("POST", "/campaigns/{campaign_id}/plan"): Role.ANALYST,
    ("POST", "/campaigns/{campaign_id}/plan/amendments"): Role.ANALYST,
    ("GET", "/campaigns/{campaign_id}/plan-preview"): Role.VIEWER,  # computed at M94's integration
    # Plan J M103 (DEC-1313): audit another tool's campaign, the programme readout, who was contacted.
    ("POST", "/campaigns/audit"): Role.ANALYST,
    ("POST", "/campaigns/programme"): Role.ANALYST,
    ("POST", "/campaigns/{campaign_id}/contacts"): Role.ANALYST,
    # Plan J M105 (DEC-1315): what needs attention and the value proven to date, no customer row.
    ("GET", "/campaigns/summary"): Role.VIEWER,
}
WRITES = [key for key, role in ROUTES.items() if key[0] in MUTATING_METHODS]
READS = [key for key in ROUTES if key[0] not in MUTATING_METHODS]


@pytest.fixture(scope="module")
def signed_in(tmp_path_factory: pytest.TempPathFactory) -> tuple[TestClient, FastAPI, Path]:
    tmp_path: Path = tmp_path_factory.mktemp("campaign-access")
    app = local_app(tmp_path)
    return TestClient(app, raise_server_exceptions=False), app, tmp_path


def _path(template: str) -> str:
    return template.replace("{campaign_id}", "c_20261007_00000001")


def test_every_campaign_route_is_served_and_has_its_policy() -> None:
    served = {(route.method, route.path) for route in live_routes() if route.path.startswith("/campaigns")}
    assert served == set(ROUTES)
    for (method, path), role in ROUTES.items():
        policy = policy_for(method, path)
        assert policy is not None and policy.role is role, (method, path)
        assert policy.action.startswith("campaigns.") and policy.purpose
        if "{campaign_id}" in path:
            assert (policy.object_type, policy.object_param) == ("campaign", "campaign_id")
        assert not policy.audit_reads, "no campaign route hands out customer rows"


@pytest.mark.parametrize(("method", "template"), WRITES, ids=lambda v: str(v))
def test_every_role_but_analyst_is_refused_a_write(
    signed_in: tuple[TestClient, FastAPI, Path], method: str, template: str
) -> None:
    client, app, tmp_path = signed_in
    name = "no-analyst-" + "-".join(part.strip("{}") for part in template.split("/") if part)
    user = make_user(app, name[:64], [role for role in Role if role is not Role.ANALYST])
    before = audit_log_at(tmp_path).count(AuditQuery(outcome="denied"))
    response = client.request(method, _path(template), json={}, headers=bearer(app, user))
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "ROLE_REQUIRED"
    assert audit_log_at(tmp_path).count(AuditQuery(outcome="denied")) == before + 1


@pytest.mark.parametrize(("method", "template"), READS, ids=lambda v: str(v))
def test_a_read_needs_a_sign_in(
    signed_in: tuple[TestClient, FastAPI, Path], method: str, template: str
) -> None:
    client, _, _ = signed_in
    response = client.request(method, _path(template))
    assert response.status_code == 401, response.text
    assert response.json()["detail"]["code"] == "AUTH_REQUIRED"


def test_a_viewer_may_read_the_campaign_list(signed_in: tuple[TestClient, FastAPI, Path]) -> None:
    client, app, _ = signed_in
    viewer = make_user(app, "campaign-viewer", [Role.VIEWER])
    response = client.get("/campaigns", headers=bearer(app, viewer))
    assert response.status_code == 200, response.text
    assert response.json() == {"campaigns": []}


def test_no_route_hands_out_a_campaigns_customer_rows(signed_in: tuple[TestClient, FastAPI, Path]) -> None:
    """M91's row-level rule (`ROW_LEVEL_ARTEFACTS`, enforced in `read_artefact`) guards the run files a
    route serves. A campaign's row-level files (`assignment.parquet`, `outcomes.parquet`) are served by
    no route at all: no campaign route takes a file name, and the run artefact route does not know them,
    so no run id reaches them. Erasure and retention cover them (`test_campaign_privacy.py`)."""
    from engine.measurement.campaign import ROW_LEVEL_CAMPAIGN_FILES

    for route in live_routes():
        if route.path.startswith("/campaigns"):
            assert "{name}" not in route.path and "{path" not in route.path, route.path
    client, app, _ = signed_in
    admin = make_user(app, "campaign-rows-admin", list(Role))
    for name in ROW_LEVEL_CAMPAIGN_FILES:
        response = client.get(f"/runs/r_20261007_00000001/artefacts/{name}", headers=bearer(app, admin))
        assert response.status_code == 404, response.text
        assert response.json()["detail"]["code"] == "ARTEFACT_UNKNOWN"
