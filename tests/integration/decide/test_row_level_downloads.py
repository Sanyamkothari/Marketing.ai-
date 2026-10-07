"""Row-level downloads are Analyst-only and audited once sign-in is on (Plan J M91, defect e).

`GET /runs/{run_id}/artefacts/{name}` served `scores.csv`, `scores.parquet` and `copy_messages.csv`
to a Viewer, and so did the two routes built on it (`GET /runs/{run_id}/scores.csv` and
`GET /runs/{run_id}/copy_messages.csv`). A Viewer is the role for *seeing results*; a file with one
row per customer is a copy of customer data, which is what the Analyst role and the audit trail are
for.

Which routes serve a row-level file is not taken from a hand-kept list: every GET route the live app
serves is called, as an Analyst, against a run whose row-level files carry a unique marker, and a
route "serves" a file when its answer is that file. Every route found that way must refuse a Viewer.
The row-level files themselves are the ones `configs/privacy.yaml` names
(`retention.row_level_run_artefacts`), the registry every new row-level artefact joins (Plan J §3.2).
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine.access.roles import Role
from engine.audit.events import AuditEvent, AuditQuery
from engine.config import ProblemType, RunMode
from engine.contracts import RunRecord, RunState
from engine.privacy.config import load_privacy_config
from engine.storage import LocalStorage, run_key
from engine.utils.time import utc_now
from tests.integration.production.access_support import (
    LiveRoute,
    audit_log_at,
    bearer,
    live_routes,
    local_app,
    make_user,
)

pytestmark = pytest.mark.integration

RUN_ID = "r-m91-rows"
GENERIC = "/runs/{run_id}/artefacts/{name}"
ROW_LEVEL: tuple[str, ...] = tuple(load_privacy_config().retention.row_level_run_artefacts)
"""Every run file that holds one row per customer, as the privacy configuration declares them."""

KNOWN_ROW_LEVEL_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {(GENERIC, name) for name in ROW_LEVEL}
    | {("/runs/{run_id}/scores.csv", "scores.csv"), ("/runs/{run_id}/copy_messages.csv", "copy_messages.csv")}
)
"""The routes known today to serve a row-level file. The walk must find at least these, which proves
it works; anything else it finds is held to the same rule."""


def _payload(name: str) -> bytes:
    """A small file of `name`'s kind whose one customer id is a marker no other file contains."""
    marker = f"M91-MARKER-{name}"
    if name.endswith(".parquet"):
        buffer = io.BytesIO()
        pd.DataFrame({"entity_key": [marker], "score": [0.5]}).to_parquet(buffer, index=False)
        return buffer.getvalue()
    return f"entity_key,score\n{marker},0.5\n".encode()


PAYLOADS: dict[str, bytes] = {name: _payload(name) for name in ROW_LEVEL}


def seed_run(root: Path) -> None:
    """A finished scoring run holding every row-level file; its `run.json` is the non-row-level report."""
    storage = LocalStorage(root)
    storage.write_model(
        run_key(RUN_ID, "run.json"),
        RunRecord(
            run_id=RUN_ID,
            use_case_id="targeted-advertisement",
            use_case_name="Targeted Advertisement",
            mode=RunMode.SCORE,
            state=RunState.DONE,
            created_at=utc_now(),
            upload_id="u_m91",
            file_name="score.csv",
            primary_key="entity_key",
            problem_type=ProblemType.BINARY_CLASSIFICATION,
            model_choice="automl",
            engine_version="0.1.0",
        ),
    )
    for name, payload in PAYLOADS.items():
        storage.write_bytes(run_key(RUN_ID, name), payload)


@dataclass(frozen=True)
class Download:
    """One route that answered with one row-level file."""

    route: LiveRoute
    name: str

    @property
    def id(self) -> str:
        return f"{self.route.path} -> {self.name}"

    def url(self) -> str:
        url = self.route.path
        for param in self.route.route.dependant.path_params:
            value = {"run_id": RUN_ID, "name": self.name}.get(param.name, "does-not-exist")
            url = url.replace("{" + param.name + "}", value)
        return url


def _serves(content: bytes, name: str) -> bool:
    return content == PAYLOADS[name] or f"M91-MARKER-{name}".encode() in content


@dataclass
class World:
    app: FastAPI
    client: TestClient
    root: Path
    users: dict[str, str]
    downloads: list[Download]

    def headers(self, who: str) -> dict[str, str]:
        return bearer(self.app, self.users[who])

    def events(self) -> dict[str, AuditEvent]:
        return {event.event_id: event for event in audit_log_at(self.root).query(AuditQuery(limit=10000))}


def _walk(client: TestClient, app: FastAPI, headers: dict[str, str]) -> list[Download]:
    """Every (GET route, row-level file) whose answer, to an Analyst, is that file."""
    found: list[Download] = []
    for route in live_routes(app):
        if route.method != "GET":
            continue
        params = {param.name for param in route.route.dependant.path_params}
        for name in ROW_LEVEL if "name" in params else (ROW_LEVEL[0],):
            candidate = Download(route, name)
            response = client.get(candidate.url(), headers=headers)
            hits = [n for n in ROW_LEVEL if response.status_code == 200 and _serves(response.content, n)]
            for hit in hits:
                if all((d.route.path, d.name) != (route.path, hit) for d in found):
                    found.append(Download(route, hit))
    return found


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> World:
    root = tmp_path_factory.mktemp("m91-rows")
    app = local_app(root)
    seed_run(root)
    users = {
        "viewer": make_user(app, "viewer", [Role.VIEWER]),
        "analyst": make_user(app, "analyst", [Role.ANALYST]),
        "all-but-analyst": make_user(app, "all-but-analyst", [r for r in Role if r is not Role.ANALYST]),
    }
    client = TestClient(app, raise_server_exceptions=False)
    downloads = _walk(client, app, bearer(app, users["analyst"]))
    return World(app=app, client=client, root=root, users=users, downloads=downloads)


def test_the_walk_finds_every_route_known_to_serve_customer_rows(world: World) -> None:
    found = {(download.route.path, download.name) for download in world.downloads}
    assert sorted(KNOWN_ROW_LEVEL_ROUTES - found) == []


def test_a_viewer_is_refused_every_row_level_download(world: World) -> None:
    served_to_viewer = []
    for download in world.downloads:
        response = world.client.get(download.url(), headers=world.headers("viewer"))
        if response.status_code != 403 or response.json()["detail"]["code"] != "ROLE_REQUIRED":
            served_to_viewer.append((download.id, response.status_code))
            continue
        message = response.json()["detail"]["message"]
        assert message.startswith("Only an Analyst can "), message
    assert served_to_viewer == []


def test_no_other_role_stands_in_for_analyst(world: World) -> None:
    """Approver and Admin do not imply Analyst (DEC-703); together they still may not take the rows."""
    statuses = {
        download.id: world.client.get(download.url(), headers=world.headers("all-but-analyst")).status_code
        for download in world.downloads
    }
    assert {key: status for key, status in statuses.items() if status != 403} == {}


def test_each_analyst_download_writes_exactly_one_audit_event(world: World) -> None:
    analyst = world.users["analyst"]
    for download in world.downloads:
        before = world.events()
        response = world.client.get(download.url(), headers=world.headers("analyst"))
        assert response.status_code == 200 and _serves(response.content, download.name), download.id
        new = [event for key, event in world.events().items() if key not in before]
        assert len(new) == 1, (download.id, [event.action for event in new])
        event = new[0]
        assert (event.actor_id, event.outcome, event.object_id) == (analyst, "success", RUN_ID), download.id
        assert event.details["route"] == download.route.path


def test_a_generic_route_download_is_recorded_as_customer_rows(world: World) -> None:
    """The generic route's own action cannot tell `scores.csv` from `run.json`; a row-level read says so."""
    for name in ROW_LEVEL:
        before = world.events()
        world.client.get(f"/runs/{RUN_ID}/artefacts/{name}", headers=world.headers("analyst"))
        (event,) = [event for key, event in world.events().items() if key not in before]
        assert event.action == "runs.customer_rows_download", name


def test_a_viewers_refusal_is_audited_once_as_denied(world: World) -> None:
    viewer = world.users["viewer"]
    for download in world.downloads:
        before = world.events()
        world.client.get(download.url(), headers=world.headers("viewer"))
        new = [event for key, event in world.events().items() if key not in before]
        assert [(event.actor_id, event.outcome) for event in new] == [(viewer, "denied")], download.id
        if download.route.path == GENERIC:  # refused by the route, not by the policy table: it says why
            assert new[0].details["reason_code"] == "ROW_LEVEL_DOWNLOAD_REFUSED", download.id
            assert new[0].action == "runs.customer_rows_download", download.id


def test_a_viewer_still_reads_a_run_report(world: World) -> None:
    """Non-row-level artefacts keep their policy: a Viewer sees results."""
    response = world.client.get(f"/runs/{RUN_ID}/artefacts/run.json", headers=world.headers("viewer"))
    assert response.status_code == 200
    assert response.json()["run_id"] == RUN_ID


def test_with_sign_in_off_nothing_changes(tmp_path: Path) -> None:
    """The local operator holds every role: every row-level download answers as it always did."""
    app = local_app(tmp_path, auth_mode="off")
    seed_run(tmp_path)
    client = TestClient(app, raise_server_exceptions=False)
    for path, name in sorted(KNOWN_ROW_LEVEL_ROUTES):
        url = path.replace("{run_id}", RUN_ID).replace("{name}", name)
        before = {event.event_id for event in audit_log_at(tmp_path).query(AuditQuery(limit=10000))}
        response = client.get(url)
        assert response.status_code == 200, (url, response.text)
        assert response.content == PAYLOADS[name], url
        new = [e for e in audit_log_at(tmp_path).query(AuditQuery(limit=10000)) if e.event_id not in before]
        assert len(new) == 1, url
        expected = {
            GENERIC: "runs.artefact_download",
            "/runs/{run_id}/scores.csv": "runs.scores_download",
            "/runs/{run_id}/copy_messages.csv": "copy.messages_download",
        }[path]
        assert (new[0].action, new[0].actor_id, new[0].outcome) == (expected, "local-operator", "success")


def test_every_tabular_file_the_artefact_route_serves_is_classified() -> None:
    """A new table the route whitelists must be declared row-level or shown to hold no customer rows."""
    from api.access_policy import ROW_LEVEL_ARTEFACTS
    from engine.contracts import TABULAR_SCHEMAS
    from engine.generative.contracts import GENERATIVE_TABULAR_SCHEMAS

    # A knowledge index's own tables: passages of the client's documents, never one row per customer.
    not_customer_rows = {"chunks.parquet", "embeddings.parquet"}
    tables = set(TABULAR_SCHEMAS) | set(GENERATIVE_TABULAR_SCHEMAS)
    assert sorted(tables - ROW_LEVEL_ARTEFACTS - not_customer_rows) == []
    assert sorted(set(ROW_LEVEL) - ROW_LEVEL_ARTEFACTS) == []
