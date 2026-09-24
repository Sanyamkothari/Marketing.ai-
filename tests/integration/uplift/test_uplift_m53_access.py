"""The routes M53 changed keep their roles (plan M46: "every route has a role test").

M53 changed what four routes accept or answer - `POST /uplift/runs` takes a two-column key,
`POST /runs` refuses an uplift training run, `POST /runs/{id}/campaign-results` joins on every key
column, and `GET /runs/{id}/artefacts/{name}` whitelists the uplift artefacts - and added no route.
Each keeps its policy row; with sign-in on, a principal holding every role except the required one
is refused with `403 ROLE_REQUIRED` before the change is reached, and the Viewer-level artefact read
(which every role implies, DEC-703) refuses an anonymous caller with `401 AUTH_REQUIRED`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.access_policy import policy_for
from engine.access.roles import Role
from tests.integration.production.access_support import bearer, local_app, make_user

pytestmark = pytest.mark.integration

CHANGED_WRITES: tuple[tuple[str, str, str, dict[str, object]], ...] = (
    (
        "POST",
        "/uplift/runs",
        "/uplift/runs",
        {"use_case": "win-back-campaign", "upload_id": "u", "primary_key": ["a", "b"], "target": "y"},
    ),
    (
        "POST",
        "/runs",
        "/runs",
        {
            "use_case": "win-back-campaign",
            "upload_id": "u",
            "primary_key": "a",
            "overrides": {"problem_type": "uplift"},
        },
    ),
    (
        "POST",
        "/runs/{run_id}/campaign-results",
        "/runs/r-1/campaign-results",
        {"upload_id": "u", "outcome_column": "y"},
    ),
)


@pytest.fixture(scope="module")
def signed_in(tmp_path_factory: pytest.TempPathFactory) -> tuple[TestClient, object]:
    tmp_path: Path = tmp_path_factory.mktemp("m53-access")
    app = local_app(tmp_path)
    return TestClient(app, raise_server_exceptions=False), app


@pytest.mark.parametrize(("method", "template", "path", "body"), CHANGED_WRITES, ids=lambda v: str(v))
def test_every_role_but_the_required_one_is_refused(
    signed_in: tuple[TestClient, object], method: str, template: str, path: str, body: dict[str, object]
) -> None:
    client, app = signed_in
    policy = policy_for(method, template)
    assert policy is not None and policy.role is Role.ANALYST
    name = "all-but-analyst-" + "-".join(part.strip("{}") for part in template.split("/") if part)
    user = make_user(app, name[:64], [r for r in Role if r is not Role.ANALYST])  # type: ignore[arg-type]
    response = client.request(method, path, json=body, headers=bearer(app, user))  # type: ignore[arg-type]
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "ROLE_REQUIRED"


def test_the_uplift_artefact_read_needs_a_sign_in(signed_in: tuple[TestClient, object]) -> None:
    client, _ = signed_in
    policy = policy_for("GET", "/runs/{run_id}/artefacts/{name}")
    assert policy is not None and policy.role is Role.VIEWER
    response = client.get("/runs/r-1/artefacts/uplift_drift.json")
    assert response.status_code == 401, response.text
    assert response.json()["detail"]["code"] == "AUTH_REQUIRED"
