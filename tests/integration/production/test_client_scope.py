"""One deployment serves one client (security review 2, H-3): outside `env=local`, a `client_id` other than
`Settings.client_id` is refused on every route, whether it arrives in the path, the query, a JSON body or a form.

The exploit these tests replay: an Analyst of client A, on a deployment that also holds client B, names B
(`GET /cases/x?client_id=B`, `GET /clients/B/sources`, `POST /campaign-runs {"client_id": "B"}`) or lists
`GET /campaign-runs` and sees B's runs. On a laptop (`env=local`) nothing changes.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes.campaign_runs import get_runner
from api.routes.clients import get_client_store
from engine.access.roles import Role
from engine.campaign_run import CampaignRunStore
from tests.integration.production.access_support import bearer, local_app, make_user

pytestmark = pytest.mark.integration

MINE = "c_acme_1"
OTHER = "c_other_1"
ALL_ROLES = (Role.VIEWER, Role.ANALYST, Role.APPROVER, Role.ADMIN)


def _signed_in(tmp_path: Path, **extra: Any) -> tuple[FastAPI, dict[str, str]]:
    app = local_app(tmp_path, **extra)
    return app, bearer(app, make_user(app, "pat", ALL_ROLES))


@pytest.fixture
def pilot(tmp_path: Path) -> Iterator[tuple[TestClient, dict[str, str], FastAPI]]:
    app, headers = _signed_in(
        tmp_path,
        env="prod",
        client_id=MINE,
        privacy_salt="x" * 32,
        cors_origins=("https://pilot.example.com",),
    )
    with TestClient(app) as client:
        yield client, headers, app


def _request(app: FastAPI) -> Any:
    from starlette.requests import Request

    return Request({"type": "http", "app": app, "headers": [], "method": "GET"})


def _code(response: Any) -> str | None:
    detail = response.json().get("detail")
    return detail.get("code") if isinstance(detail, dict) else None


def test_a_foreign_client_in_the_query_is_refused_and_the_deployments_own_is_not(
    pilot: tuple[TestClient, dict[str, str], FastAPI],
) -> None:
    client, headers, _ = pilot
    foreign = client.get(f"/datasets?client_id={OTHER}", headers=headers)
    assert foreign.status_code == 403 and _code(foreign) == "CLIENT_NOT_ALLOWED"
    mixed = client.get(f"/runs?client_id={MINE}&client_id={OTHER}", headers=headers)
    assert mixed.status_code == 403 and _code(mixed) == "CLIENT_NOT_ALLOWED"
    for own in (MINE, "default"):
        assert _code(client.get(f"/datasets?client_id={own}", headers=headers)) != "CLIENT_NOT_ALLOWED"
    assert _code(client.get("/datasets", headers=headers)) != "CLIENT_NOT_ALLOWED"


def test_a_foreign_client_in_the_path_is_refused(pilot: tuple[TestClient, dict[str, str], FastAPI]) -> None:
    client, headers, _ = pilot
    for path in (
        f"/clients/{OTHER}",
        f"/clients/{OTHER}/sources",
        f"/clients/{OTHER}/mappings",
    ):
        response = client.get(path, headers=headers)
        assert response.status_code == 403 and _code(response) == "CLIENT_NOT_ALLOWED", path
    assert client.get(f"/clients/{MINE}/sources", headers=headers).status_code != 403


def test_a_foreign_client_in_a_json_body_is_refused_before_the_route_runs(
    pilot: tuple[TestClient, dict[str, str], FastAPI],
) -> None:
    client, headers, _ = pilot
    body = {"use_case": "targeted-advertisement", "client_id": OTHER}
    refused = client.post("/campaign-runs", json=body, headers=headers)
    assert refused.status_code == 403 and _code(refused) == "CLIENT_NOT_ALLOWED"
    own = client.post("/campaign-runs", json={**body, "client_id": MINE}, headers=headers)
    assert _code(own) != "CLIENT_NOT_ALLOWED"
    none = client.post("/campaign-runs", json={**body, "client_id": None}, headers=headers)
    assert _code(none) != "CLIENT_NOT_ALLOWED"


JSON_SPELLINGS = (
    "application/json",
    "application/json; charset=utf-8",
    " application/json",
    "application/vnd.api+json",
    "application/merge-patch+json",
    "APPLICATION/JSON",
)


@pytest.mark.parametrize("content_type", JSON_SPELLINGS)
def test_a_foreign_client_in_a_body_is_refused_whatever_json_type_the_caller_claims(
    pilot: tuple[TestClient, dict[str, str], FastAPI], content_type: str
) -> None:
    """FastAPI parses `application/*+json` and a padded ` application/json` too; the check must not be keyed on the header."""
    client, headers, _ = pilot
    headers = {**headers, "Content-Type": content_type}
    for path, body in (
        ("/datasets", {"client_id": OTHER, "spec_id": "s_x"}),
        ("/campaign-runs", {"use_case": "targeted-advertisement", "client_id": OTHER}),
    ):
        refused = client.post(path, content=json.dumps(body), headers=headers)
        assert refused.status_code == 403 and _code(refused) == "CLIENT_NOT_ALLOWED", (path, content_type)
    own = client.post("/datasets", content=json.dumps({"client_id": MINE, "spec_id": "s_x"}), headers=headers)
    assert _code(own) != "CLIENT_NOT_ALLOWED", content_type


def test_a_foreign_client_in_a_body_with_no_content_type_is_refused(
    pilot: tuple[TestClient, dict[str, str], FastAPI],
) -> None:
    client, headers, _ = pilot
    body = json.dumps({"client_id": OTHER, "spec_id": "s_x"})
    response = client.post("/datasets", content=body, headers={**headers, "Content-Type": ""})
    assert response.status_code == 403 and _code(response) == "CLIENT_NOT_ALLOWED"


def test_a_foreign_client_in_a_form_field_is_refused(
    pilot: tuple[TestClient, dict[str, str], FastAPI],
) -> None:
    client, headers, _ = pilot
    response = client.post(
        "/privacy/consent/imports",
        headers=headers,
        data={"client_id": OTHER},
        files={"file": ("consent.csv", b"principal_id,purpose,status,recorded_at\n", "text/csv")},
    )
    assert response.status_code == 403 and _code(response) == "CLIENT_NOT_ALLOWED"


def test_the_check_comes_after_sign_in(pilot: tuple[TestClient, dict[str, str], FastAPI]) -> None:
    client, _, _ = pilot
    assert client.get(f"/datasets?client_id={OTHER}").status_code == 401


def test_a_laptop_is_unchanged(tmp_path: Path) -> None:
    app, headers = _signed_in(tmp_path, env="local", client_id=MINE)
    with TestClient(app) as client:
        assert _code(client.get(f"/datasets?client_id={OTHER}", headers=headers)) != "CLIENT_NOT_ALLOWED"
        assert client.get(f"/clients/{OTHER}/sources", headers=headers).status_code != 403


def test_a_deployment_without_a_client_id_is_unchanged(tmp_path: Path) -> None:
    app, headers = _signed_in(
        tmp_path, env="prod", privacy_salt="x" * 32, cors_origins=("https://pilot.example.com",)
    )
    with TestClient(app) as client:
        assert _code(client.get(f"/datasets?client_id={OTHER}", headers=headers)) != "CLIENT_NOT_ALLOWED"


def test_the_deployment_registers_its_one_client_under_its_own_id(
    pilot: tuple[TestClient, dict[str, str], FastAPI],
) -> None:
    client, headers, _ = pilot
    created = client.post("/clients", json={"name": "Acme Telecom", "industry": "telecom"}, headers=headers)
    assert created.status_code == 201, created.text
    assert created.json()["client_id"] == MINE  # not c_acme_telecom_1: it must equal Settings.client_id
    again = client.post("/clients", json={"name": "Other Co", "industry": "telecom"}, headers=headers)
    assert again.status_code == 201 and again.json()["client_id"] == MINE  # never a second client
    assert [c["client_id"] for c in client.get("/clients", headers=headers).json()["clients"]] == [MINE]


def test_the_runbook_step_names_the_client_the_page_already_created_silently(
    pilot: tuple[TestClient, dict[str, str], FastAPI],
) -> None:
    """The UI calls `POST /clients/default` on load (named after the id); `POST /clients` must then set the real name,
    and a store that holds some other client must not make the deployment's own impossible to create."""
    client, headers, app = pilot
    assert client.post("/clients/default", headers=headers).json()["name"] == MINE
    created = client.post("/clients", json={"name": "Acme Telecom", "industry": "telecom"}, headers=headers)
    assert created.status_code == 201 and created.json()["client_id"] == MINE
    (record,) = client.get("/clients", headers=headers).json()["clients"]
    assert record["name"] == "Acme Telecom" and record["industry"] == "telecom"
    store = get_client_store(_request(app))
    store.create_client("Demo", "telecom")  # another client left in the store (for example c_demo)
    assert (
        client.post(
            "/clients", json={"name": "Acme Telecom", "industry": "telecom"}, headers=headers
        ).status_code
        == 201
    )
    assert [c["client_id"] for c in client.get("/clients", headers=headers).json()["clients"]] == [MINE]


def test_the_silent_default_client_is_the_deployments_own(
    pilot: tuple[TestClient, dict[str, str], FastAPI],
) -> None:
    client, headers, _ = pilot
    response = client.post("/clients/default", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["client_id"] == MINE


def test_the_campaign_run_list_and_reads_hold_only_this_clients_runs(
    pilot: tuple[TestClient, dict[str, str], FastAPI],
) -> None:
    client, headers, app = pilot
    store: CampaignRunStore = get_runner(app).store
    kept = {
        "mine": store.create(use_case_id="targeted-advertisement", client_id=MINE, state="done"),
        "unnamed": store.create(use_case_id="targeted-advertisement", state="done"),
    }
    foreign = store.create(use_case_id="targeted-advertisement", client_id=OTHER, state="done")
    listed = client.get("/campaign-runs", headers=headers)
    assert listed.status_code == 200, listed.text
    ids = {r["record"]["campaign_run_id"] for r in listed.json()["campaign_runs"]}
    assert ids == {r.campaign_run_id for r in kept.values()}
    for path in ("", "/top-customers"):
        response = client.get(f"/campaign-runs/{foreign.campaign_run_id}{path}", headers=headers)
        assert response.status_code == 404 and _code(response) == "CAMPAIGN_RUN_NOT_FOUND", path
    assert client.get(f"/campaign-runs/{kept['mine'].campaign_run_id}", headers=headers).status_code == 200


def test_cancel_and_restart_of_a_foreign_campaign_run_are_not_found_and_change_nothing(
    pilot: tuple[TestClient, dict[str, str], FastAPI],
) -> None:
    client, headers, app = pilot
    runner = get_runner(app)
    foreign = runner.store.create(
        use_case_id="targeted-advertisement", client_id=OTHER, state="stopped", error=None
    )
    running = runner.store.create(use_case_id="targeted-advertisement", client_id=OTHER, state="running")
    cancelled = client.post(f"/campaign-runs/{running.campaign_run_id}/cancel", headers=headers)
    assert cancelled.status_code == 404 and _code(cancelled) == "CAMPAIGN_RUN_NOT_FOUND"
    assert runner.store.get(running.campaign_run_id).state == "running"  # type: ignore[union-attr]
    before = len(runner.store.list(limit=200))
    restarted = client.post(f"/campaign-runs/{foreign.campaign_run_id}/restart", headers=headers)
    assert restarted.status_code == 404 and _code(restarted) == "CAMPAIGN_RUN_NOT_FOUND"
    assert len(runner.store.list(limit=200)) == before  # no run was started for the other client


def test_the_campaign_run_list_keeps_this_clients_runs_behind_many_foreign_ones(
    pilot: tuple[TestClient, dict[str, str], FastAPI],
) -> None:
    client, headers, app = pilot
    store: CampaignRunStore = get_runner(app).store
    mine = store.create(use_case_id="targeted-advertisement", client_id=MINE, state="done")
    for _ in range(205):
        store.create(use_case_id="targeted-advertisement", client_id=OTHER, state="done")
    listed = client.get("/campaign-runs?limit=3", headers=headers)
    ids = [r["record"]["campaign_run_id"] for r in listed.json()["campaign_runs"]]
    assert ids == [mine.campaign_run_id]


def test_a_non_local_deployment_without_a_client_id_says_so_at_startup(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    app, _ = _signed_in(
        tmp_path, env="prod", privacy_salt="x" * 32, cors_origins=("https://pilot.example.com",)
    )
    with caplog.at_level("WARNING", logger="api.access"), TestClient(app):
        pass
    assert any("CLIENT_ID" in r.getMessage() and "any client id" in r.getMessage() for r in caplog.records)
