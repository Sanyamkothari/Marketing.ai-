"""What the Data Doctor screen reads, through the API (Plan O M247, DEC-1817).

`GET /cases/{case_id}/screen` and `GET /data-doctor/cases` on a real upload and a real visit, their policies and
refusals; `GET /auth/me` says the screen is on.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.access_policy import all_policies
from api.main import create_app
from api.routes import case_screen
from engine.access.roles import Role
from tests.fixtures.make_data import GenerationSpec, generate
from tests.integration.doctor_wait import settled
from tests.integration.production.access_support import bearer, local_app, make_user

GOAL = "targeted-advertisement"


@pytest.fixture
def client(config_root: Path, tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(config_root=config_root, data_dir=tmp_path / "data")) as test_client:
        yield test_client


def _upload(client: TestClient) -> str:
    frame = generate(GenerationSpec(use_case_id=GOAL, rows=1_500, seed=3))
    buffer = io.StringIO()
    frame.to_csv(buffer, index=False)
    response = client.post(
        "/uploads",
        files={"file": ("customers.csv", buffer.getvalue().encode(), "text/csv")},
        data={"use_case": GOAL, "mode": "train"},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["upload_id"])


def _code(response: Any) -> str:
    return str(response.json()["detail"]["code"])


def test_the_screen_follows_a_visit_from_a_file_to_a_goal(client: TestClient) -> None:
    empty = client.post("/cases", json={"title": "Check"}).json()
    screen = client.get(f"/cases/{empty['case_id']}/screen").json()
    assert screen["view"]["verdict"]["key"] == "needs_files" and screen["case"]["case_id"] == empty["case_id"]
    assert [e["kind"] for e in screen["timeline"]] == ["case.created"]

    case = client.post("/cases", json={"upload_ids": [_upload(client)]}).json()
    case = client.post(f"/cases/{case['case_id']}/visit", json={}).json()
    screen = client.get(f"/cases/{case['case_id']}/screen").json()
    assert screen["case"] == case, "the case as GET /cases/{id} answers it"
    view = screen["view"]
    assert view["mode"] == "generic" and view["missing_for"] == case["visit"]["missing_for"]
    assert case["state"] == "approve" and view["verdict"]["key"] == "needs_approval"
    shown = view["preview"]["files"][0]  # the data panel: the first rows before and after, masked
    assert shown["file_name"] == "customers.csv" and shown["total_rows"] == 1_500
    assert len(shown["before"]) == 10 == len(shown["after"]) and shown["note"] is None
    assert set(shown["columns"][0]) == {"name", "changed", "personal"}
    assert all(len(row) == len(shown["columns"]) for row in shown["before"] + shown["after"])
    case = settled(client, case["case_id"])  # Approve waits for the check of the whole file
    approved = client.post(
        f"/cases/{case['case_id']}/approval",
        json={"version": case["version"], "sentence": view["approval_sentence"]},
    )
    assert approved.status_code == 200, "the sentence the screen shows is the one Approve takes"

    chosen = client.post(
        f"/cases/{case['case_id']}/goal", json={"version": approved.json()["version"], "goal_id": GOAL}
    )
    assert chosen.status_code == 200, chosen.text
    screen = client.get(f"/cases/{case['case_id']}/screen").json()
    assert screen["view"]["mode"] == "goal" and screen["view"]["goal_id"] == GOAL
    consent = [a for a in screen["view"]["approve_class"] if a["role"] == "approver"]
    assert consent and consent[0]["why"] == "it decides who may be contacted" and not consent[0]["confirmed"]

    listed = client.get("/data-doctor/cases", params={"goal_id": GOAL}).json()["cases"]
    assert [c["case_id"] for c in listed] == [case["case_id"]] and listed[0]["files"] == ["customers.csv"]
    every = client.get("/data-doctor/cases").json()["cases"]
    assert [c["case_id"] for c in every] == [case["case_id"], empty["case_id"]]


def test_refusals(client: TestClient) -> None:
    missing = client.get("/cases/case_0000000000000000/screen")
    assert missing.status_code == 404 and _code(missing) == "CASE_NOT_FOUND"
    case = client.post("/cases", json={}).json()
    other = client.get(f"/cases/{case['case_id']}/screen", params={"client_id": "c_other"})
    assert (
        other.status_code == 404 and _code(other) == "CASE_NOT_FOUND"
    ), "another client's case is never named"
    assert client.get("/data-doctor/cases", params={"goal_id": "Not A Goal!"}).status_code == 422


def test_auth_me_says_the_screen_is_on(client: TestClient) -> None:
    assert client.get("/auth/me").json()["data_doctor"] is True


def test_the_routes_have_their_policies(client: TestClient) -> None:
    policies = all_policies()
    for key in case_screen.POLICIES:
        assert policies[key].role is Role.VIEWER, key
    paths = set(client.app.openapi()["paths"])  # type: ignore[attr-defined]
    assert {path for _method, path in case_screen.POLICIES} <= paths


def test_a_viewer_reads_the_screen(tmp_path: Path) -> None:
    app = local_app(tmp_path)
    viewer = make_user(app, "vera", [Role.VIEWER])
    analyst = make_user(app, "asha", [Role.ANALYST])
    with TestClient(app, raise_server_exceptions=False) as client:
        case = client.post("/cases", json={}, headers=bearer(app, analyst)).json()
        read = client.get(f"/cases/{case['case_id']}/screen", headers=bearer(app, viewer))
        assert read.status_code == 200 and read.json()["view"]["verdict"]["key"] == "needs_files"
        assert client.get("/data-doctor/cases", headers=bearer(app, viewer)).status_code == 200


def test_the_screen_names_the_goal_and_says_who_may_agree(tmp_path: Path) -> None:
    """M247 review: the sentence Approve records names the goal as the screens do, and each change that needs its
    own yes says whether the person reading may give it - a viewer is not offered a yes the server refuses."""
    app = local_app(tmp_path)
    viewer = make_user(app, "vera", [Role.VIEWER])
    analyst = make_user(app, "asha", [Role.ANALYST])
    approver = make_user(app, "ravi", [Role.APPROVER])
    frame = generate(GenerationSpec(use_case_id=GOAL, rows=1_500, seed=3))
    buffer = io.StringIO()
    frame.to_csv(buffer, index=False)
    with TestClient(app, raise_server_exceptions=False) as client:
        as_asha = bearer(app, analyst)
        upload = client.post(
            "/uploads",
            files={"file": ("customers.csv", buffer.getvalue().encode(), "text/csv")},
            data={"use_case": GOAL, "mode": "train"},
            headers=as_asha,
        )
        assert upload.status_code == 201, upload.text
        case = client.post(
            "/cases", json={"upload_ids": [upload.json()["upload_id"]]}, headers=as_asha
        ).json()
        case = client.post(f"/cases/{case['case_id']}/visit", json={}, headers=as_asha).json()
        case = client.post(
            f"/cases/{case['case_id']}/goal",
            json={"version": case["version"], "goal_id": GOAL},
            headers=as_asha,
        ).json()

        def consent(who: Any) -> dict[str, Any]:
            screen = client.get(f"/cases/{case['case_id']}/screen", headers=bearer(app, who)).json()
            return next(a for a in screen["view"]["approve_class"] if a["role"] == "approver")

        assert (consent(viewer)["can_agree"], consent(viewer)["cannot_agree"]) == (False, "role")
        assert (consent(analyst)["can_agree"], consent(analyst)["cannot_agree"]) == (False, "role")
        assert consent(approver)["can_agree"] is True and consent(approver)["cannot_agree"] is None
        screen = client.get(f"/cases/{case['case_id']}/screen", headers=as_asha).json()
        assert screen["case"]["state"] == "approve", screen["case"]["state"]
        sentence = screen["view"]["approval_sentence"]
        assert "“Find customers likely to buy”" in sentence and GOAL not in sentence

        # Cell values (masked) are an Analyst's, as on `agent-session/preview` and `/rows`: a Viewer's screen holds none.
        assert screen["view"]["preview"]["files"][0]["before"]
        seen = client.get(f"/cases/{case['case_id']}/screen", headers=bearer(app, viewer)).json()
        assert seen["view"]["preview"] is None
        assert all("samples" not in step for step in (seen["view"]["ledger"] or {}).get("steps", []))
