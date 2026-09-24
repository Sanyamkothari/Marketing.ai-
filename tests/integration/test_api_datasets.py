"""`api.routes.mappings` and `api.routes.datasets` end to end through `TestClient` (Phase 2 plan §9,
M12: mapping suggest/save/list, onboarding-spec create/list/preview, dataset build/read/list).

Neither router is mounted by `api.main.create_app` yet, for the same reason
`tests/integration/test_api_clients.py` gives: `PARALLEL_WORK_PROTOCOL.md` §3 keeps `api/main.py` a
file this branch may not touch, and the task says registering the router is the orchestrator's job.
The fixtures below build the smallest app that serves all four M8/M12 routers together, layered
exactly as the endpoints depend on each other (clients -> sources -> mappings -> datasets), and read
`app.state.config_root`/`data_dir` the same way `api.main.create_app` would set them.

`engine.onboarding.mapping`, `engine.onboarding.validate` and `engine.onboarding.build` have all
landed, so every test here runs for real against them rather than skipping; the `importorskip` guards
stay because the fixtures are layered and a module being rewritten beside this branch should skip
this file, not fail it.

Two tests stub `engine.onboarding.build.build_dataset` through `monkeypatch`, and only those two:
they are about what this route does when the build engine *stops without saying so*, which is a
state no real build can be asked to produce on demand and which - left unhandled - leaves the Build
screen polling "queued" for ever. Everything else, including the preview and the full build, runs
the real engine end to end. A third stubs it for one request only, to make a build fail and show that a
failed build does not stand in for the first build of its recipe (ruling R1, DEC-871); the build after
it is real.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.main import config_error_handler
from api.routes.clients import router as clients_router
from api.routes.datasets import PREVIEW_SAMPLE_ENTITIES
from engine.config import ConfigError
from engine.contracts import RunState
from engine.onboarding import build
from engine.onboarding.specs import BuildStage, BuildStatus
from engine.storage import LocalStorage
from engine.utils.time import utc_now
from tests.fixtures.planned import PLANNED_ID, planned_config_root
from tests.integration.test_api_clients import (
    TELCO_CHURN,
    UNKNOWN_ID,
    create_client_via_api,
    upload_source,
)

pytestmark = pytest.mark.integration

ENTITY_CSV = "customer_id,name,signup_date\nC-1,Ann,2026-01-01\nC-2,Bea,2026-01-02\nC-3,Cid,2026-01-03\n"
BUILD_TIMEOUT_S = 90.0
"""How long a poll waits for a build. Generous on purpose: a timeout here must mean "the job never
finished", never "this machine was slow", or the suite reports a route bug that is not there."""

EVENTS_START = date(2026, 1, 1)
"""First event date of the generated fixtures; the span after it is what `TOO_LITTLE_HISTORY` reads."""

COMPLAINTS_CSV = (
    "customer_id,complaint_date,category\n"
    "C-1,2026-02-01,billing\n"
    "C-2,2026-02-03,network\n"
    "C-1,2026-02-10,billing\n"
)


# ---------------------------------------------------------------------------
# Fixtures: the smallest app that serves all four M8/M12 routers together (see module docstring)
# ---------------------------------------------------------------------------
@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "data"
    directory.mkdir()
    return directory


def _bare_app(config_root: Path, data_dir: Path) -> FastAPI:
    app = FastAPI()
    app.state.config_root = config_root
    app.state.data_dir = data_dir
    app.add_exception_handler(ConfigError, config_error_handler)
    return app


@pytest.fixture
def sources_app(config_root: Path, data_dir: Path) -> FastAPI:
    """`clients` + `sources`, skipped until `engine.onboarding.sources` exists."""
    pytest.importorskip("engine.onboarding.sources")
    from api.routes.sources import router as sources_router

    app = _bare_app(config_root, data_dir)
    app.include_router(clients_router)
    app.include_router(sources_router)
    return app


@pytest.fixture
def mappings_app(sources_app: FastAPI) -> FastAPI:
    """`sources_app` + `mappings`, skipped until `engine.onboarding.mapping`/`validate` exist."""
    pytest.importorskip("engine.onboarding.mapping")
    pytest.importorskip("engine.onboarding.validate")
    from api.routes.mappings import router as mappings_router

    sources_app.include_router(mappings_router)
    return sources_app


@pytest.fixture
def full_app(mappings_app: FastAPI) -> FastAPI:
    """Every M8/M12 router. `engine.onboarding.build` is deliberately *not* required here - see the
    module docstring on why `api/routes/datasets.py` can be mounted and its `POST /datasets` job
    submitted before that module lands."""
    from api.routes.datasets import router as datasets_router

    mappings_app.include_router(datasets_router)
    return mappings_app


@pytest.fixture
def full_client(full_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(full_app) as test_client:
        yield test_client


@pytest.fixture
def storage(data_dir: Path) -> LocalStorage:
    return LocalStorage(data_dir)


# ---------------------------------------------------------------------------
# Test data: a client with a confirmed entity source and a confirmed complaints source
# ---------------------------------------------------------------------------
def _confirm_role(client: TestClient, client_id: str, source_id: str, role: str) -> dict[str, Any]:
    response = client.patch(f"/clients/{client_id}/sources/{source_id}", json={"role": role})
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _onboarded_client(client: TestClient) -> dict[str, str]:
    """A fresh client with two confirmed sources - `entity` and `complaints`, both roles
    `configs/roles.yaml` and `configs/use_cases/telco_churn.yaml` already recognise - so every
    mapping/spec test below has something real to suggest and save a mapping against."""
    client_id = create_client_via_api(client)["client_id"]
    entity = upload_source(client, client_id, ENTITY_CSV.encode(), name="customers.csv", role="entity")
    assert entity.status_code == 201, entity.text
    complaints = upload_source(client, client_id, COMPLAINTS_CSV.encode(), name="complaints.csv")
    assert complaints.status_code == 201, complaints.text
    complaints_id = complaints.json()["source_id"]
    _confirm_role(client, client_id, complaints_id, "complaints")
    return {
        "client_id": client_id,
        "entity_source_id": entity.json()["source_id"],
        "complaints_source_id": complaints_id,
    }


def _suggest_mapping(client: TestClient, client_id: str, source_id: str) -> dict[str, Any]:
    response = client.post(
        f"/clients/{client_id}/mappings/suggest", json={"source_id": source_id, "use_case": TELCO_CHURN}
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _save_mapping(client: TestClient, client_id: str, suggestion: dict[str, Any]) -> dict[str, Any]:
    response = client.put(
        f"/clients/{client_id}/mappings/{suggestion['mapping_id']}",
        json={
            "client_id": client_id,
            "source_id": suggestion["source_id"],
            "use_case": suggestion["use_case"],
            "role": suggestion["role"],
            "columns": suggestion["columns"],
            "unmapped_source": suggestion["unmapped_source"],
            "missing_required": suggestion["missing_required"],
            "value_maps": suggestion["value_maps"],
        },
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _big_csvs(entities: int = 1_200, span_days: int = 150) -> tuple[str, str]:
    """Two CSVs large enough for a build to *finish* rather than stop on Phase 1's own checks.

    The small fixtures above are right for every test about refusals, which never read a row; a
    build that reaches `done` needs more than a thousand rows and months of history, because
    `ROWS_TOO_FEW` and `TOO_LITTLE_HISTORY` are errors and an error means no dataset is written. The
    numbers come from the shipped config, not from taste: 1,000 rows and 90 days are what
    `configs/` asks for, and these clear both with room to spare rather than sitting on the line.
    """
    people = "".join(
        f"C-{index},Person{index},2025-0{index % 9 + 1}-01\n" for index in range(1, entities + 1)
    )
    events = "".join(
        f"C-{index},{EVENTS_START + timedelta(days=(index + offset * 37) % span_days)},billing\n"
        for index in range(1, entities + 1)
        for offset in range(3)
    )
    return f"customer_id,name,signup_date\n{people}", f"customer_id,complaint_date,category\n{events}"


BIG_ENTITY_CSV, BIG_COMPLAINTS_CSV = _big_csvs()


def _buildable_client(client: TestClient) -> dict[str, str]:
    """`_mapped_client`, on data a build can finish on. Used only by the tests that build for real."""
    client_id = create_client_via_api(client)["client_id"]
    entity = upload_source(client, client_id, BIG_ENTITY_CSV.encode(), name="customers.csv", role="entity")
    assert entity.status_code == 201, entity.text
    complaints = upload_source(client, client_id, BIG_COMPLAINTS_CSV.encode(), name="complaints.csv")
    assert complaints.status_code == 201, complaints.text
    complaints_id = complaints.json()["source_id"]
    _confirm_role(client, client_id, complaints_id, "complaints")
    ctx = {
        "client_id": client_id,
        "entity_source_id": entity.json()["source_id"],
        "complaints_source_id": complaints_id,
    }
    for key, source_id in (
        ("entity_mapping_id", ctx["entity_source_id"]),
        ("complaints_mapping_id", complaints_id),
    ):
        ctx[key] = _save_mapping(client, client_id, _suggest_mapping(client, client_id, source_id))[
            "mapping_id"
        ]
    return ctx


def _mapped_client(client: TestClient) -> dict[str, str]:
    """`_onboarded_client` plus a saved mapping for each of its two sources."""
    ctx = _onboarded_client(client)
    entity_suggestion = _suggest_mapping(client, ctx["client_id"], ctx["entity_source_id"])
    complaints_suggestion = _suggest_mapping(client, ctx["client_id"], ctx["complaints_source_id"])
    ctx["entity_mapping_id"] = _save_mapping(client, ctx["client_id"], entity_suggestion)["mapping_id"]
    ctx["complaints_mapping_id"] = _save_mapping(client, ctx["client_id"], complaints_suggestion)[
        "mapping_id"
    ]
    return ctx


def _create_spec(client: TestClient, ctx: dict[str, str]) -> dict[str, Any]:
    response = client.post(
        f"/clients/{ctx['client_id']}/onboarding-specs",
        json={
            "use_case": TELCO_CHURN,
            "entity_source_id": ctx["entity_source_id"],
            "event_source_ids": [ctx["complaints_source_id"]],
            "mapping_ids": [ctx["entity_mapping_id"], ctx["complaints_mapping_id"]],
            "feature_spec": {
                "features": [
                    {
                        "name": "complaints_90d",
                        "role": "complaints",
                        "function": "count",
                        "window_days": 90,
                    }
                ]
            },
            "label_spec": None,
            "snapshot_spec": {"mode": "single"},
        },
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


# ---------------------------------------------------------------------------
# POST /clients/{id}/mappings/suggest
# ---------------------------------------------------------------------------
def test_suggest_mapping_returns_non_empty_candidates(full_client: TestClient) -> None:
    ctx = _onboarded_client(full_client)
    suggestion = _suggest_mapping(full_client, ctx["client_id"], ctx["entity_source_id"])
    assert suggestion["source_id"] == ctx["entity_source_id"]
    assert suggestion["client_id"] == ctx["client_id"]
    assert suggestion["role"] == "entity"
    assert suggestion["hash"] == "", "a suggestion is unsaved, so it carries no stable hash yet"
    assert isinstance(suggestion["columns"], list)


def test_suggest_mapping_of_an_unknown_client_is_404(full_client: TestClient) -> None:
    response = full_client.post(
        "/clients/c_does_not_exist_1/mappings/suggest", json={"source_id": "src_x", "use_case": TELCO_CHURN}
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "CLIENT_NOT_FOUND"


def test_suggest_mapping_of_an_unknown_source_is_404(full_client: TestClient) -> None:
    client_id = create_client_via_api(full_client)["client_id"]
    response = full_client.post(
        f"/clients/{client_id}/mappings/suggest",
        json={"source_id": "src_does_not_exist", "use_case": TELCO_CHURN},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "SOURCE_NOT_FOUND"


def test_suggest_mapping_of_an_unknown_use_case_is_404(full_client: TestClient) -> None:
    ctx = _onboarded_client(full_client)
    response = full_client.post(
        f"/clients/{ctx['client_id']}/mappings/suggest",
        json={"source_id": ctx["entity_source_id"], "use_case": UNKNOWN_ID},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "USE_CASE_NOT_FOUND"


def test_suggest_mapping_of_a_planned_use_case_is_404(
    full_app: FastAPI, full_client: TestClient, tmp_path: Path
) -> None:
    ctx = _onboarded_client(full_client)
    # Nothing in the shipped configuration is planned any more (see tests/fixtures/planned.py).
    full_app.state.config_root = planned_config_root(tmp_path)
    response = full_client.post(
        f"/clients/{ctx['client_id']}/mappings/suggest",
        json={"source_id": ctx["entity_source_id"], "use_case": PLANNED_ID},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "USE_CASE_PLANNED"


def test_suggest_mapping_of_an_unconfirmed_role_is_409(full_client: TestClient) -> None:
    client_id = create_client_via_api(full_client)["client_id"]
    unconfirmed = upload_source(full_client, client_id, ENTITY_CSV.encode(), name="customers.csv")
    assert unconfirmed.status_code == 201, unconfirmed.text
    response = full_client.post(
        f"/clients/{client_id}/mappings/suggest",
        json={"source_id": unconfirmed.json()["source_id"], "use_case": TELCO_CHURN},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "SOURCE_ROLE_UNCONFIRMED"


# ---------------------------------------------------------------------------
# PUT /clients/{id}/mappings/{mid}
# ---------------------------------------------------------------------------
def test_save_mapping_returns_checks(full_client: TestClient) -> None:
    ctx = _onboarded_client(full_client)
    suggestion = _suggest_mapping(full_client, ctx["client_id"], ctx["entity_source_id"])
    saved = _save_mapping(full_client, ctx["client_id"], suggestion)
    assert saved["mapping_id"] == suggestion["mapping_id"]
    assert isinstance(saved["checks"], list)


def test_save_mapping_checks_every_mapping_of_the_use_case_not_only_the_one_just_saved(
    full_client: TestClient,
) -> None:
    """Whether an entity table has been mapped is a statement about the *set* of this client's
    mappings, so saving the complaints mapping alone must report `NO_ENTITY_SOURCE` and saving the
    entity mapping must clear it. A check run over only the mapping just written could report
    neither, and would tell a client whose other tables are fine that their data is broken."""
    ctx = _onboarded_client(full_client)
    complaints = _suggest_mapping(full_client, ctx["client_id"], ctx["complaints_source_id"])
    alone = _save_mapping(full_client, ctx["client_id"], complaints)
    assert "NO_ENTITY_SOURCE" in {check["code"] for check in alone["checks"]}

    entity = _suggest_mapping(full_client, ctx["client_id"], ctx["entity_source_id"])
    both = _save_mapping(full_client, ctx["client_id"], entity)
    assert "NO_ENTITY_SOURCE" not in {check["code"] for check in both["checks"]}


def test_every_check_a_save_returns_carries_a_code_a_message_and_a_suggestion(
    full_client: TestClient,
) -> None:
    """Plain-language failures (house rule 3): nothing reaches the mapping screen as a bare code."""
    ctx = _onboarded_client(full_client)
    complaints = _suggest_mapping(full_client, ctx["client_id"], ctx["complaints_source_id"])
    checks = _save_mapping(full_client, ctx["client_id"], complaints)["checks"]
    assert checks, "a client with no entity mapping has something to say"
    for check in checks:
        assert check["code"] and check["message"] and check["suggestion"]


def test_save_mapping_cannot_overwrite_another_clients_mapping(full_client: TestClient) -> None:
    """`LocalClientStore.save_mapping` upserts with `client_id = excluded.client_id`, so a save that
    did not check what an id already held would silently move another client's mapping - and its
    recipes - onto the caller's client. The id's existence is not this client's to learn either, so
    the answer is the same 404 `GET` would give."""
    ctx = _mapped_client(full_client)
    other_id = create_client_via_api(full_client, name="Other Co")["client_id"]
    other = upload_source(full_client, other_id, ENTITY_CSV.encode(), name="customers.csv", role="entity")
    assert other.status_code == 201, other.text

    response = full_client.put(
        f"/clients/{other_id}/mappings/{ctx['entity_mapping_id']}",
        json={
            "client_id": other_id,
            "source_id": other.json()["source_id"],
            "use_case": TELCO_CHURN,
            "role": "entity",
            "columns": [],
        },
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"]["code"] == "MAPPING_NOT_FOUND"

    still_theirs = full_client.get(f"/clients/{ctx['client_id']}/mappings")
    assert ctx["entity_mapping_id"] in {row["mapping_id"] for row in still_theirs.json()["mappings"]}
    assert full_client.get(f"/clients/{other_id}/mappings").json()["mappings"] == []


def test_save_mapping_cannot_repoint_an_existing_mapping_at_another_file(full_client: TestClient) -> None:
    """A recipe names a mapping, and the mapping names the file it reads; saving a second file over
    the first would change what every recipe using it builds from, without naming that recipe."""
    ctx = _mapped_client(full_client)
    response = full_client.put(
        f"/clients/{ctx['client_id']}/mappings/{ctx['entity_mapping_id']}",
        json={
            "client_id": ctx["client_id"],
            "source_id": ctx["complaints_source_id"],
            "use_case": TELCO_CHURN,
            "role": "complaints",
            "columns": [],
        },
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "MAPPING_SOURCE_MISMATCH"


def test_save_mapping_claiming_a_role_the_source_does_not_have_is_409(full_client: TestClient) -> None:
    ctx = _onboarded_client(full_client)
    suggestion = _suggest_mapping(full_client, ctx["client_id"], ctx["entity_source_id"])
    response = full_client.put(
        f"/clients/{ctx['client_id']}/mappings/{suggestion['mapping_id']}",
        json={
            "client_id": ctx["client_id"],
            "source_id": ctx["entity_source_id"],
            "use_case": TELCO_CHURN,
            "role": "complaints",
            "columns": [],
        },
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "MAPPING_ROLE_MISMATCH"


def test_save_mapping_of_an_unknown_use_case_is_404(full_client: TestClient) -> None:
    ctx = _onboarded_client(full_client)
    suggestion = _suggest_mapping(full_client, ctx["client_id"], ctx["entity_source_id"])
    response = full_client.put(
        f"/clients/{ctx['client_id']}/mappings/{suggestion['mapping_id']}",
        json={
            "client_id": ctx["client_id"],
            "source_id": ctx["entity_source_id"],
            "use_case": UNKNOWN_ID,
            "role": "entity",
            "columns": suggestion["columns"],
        },
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"]["code"] == "USE_CASE_NOT_FOUND"


def test_a_refused_save_writes_nothing(full_client: TestClient) -> None:
    """Every 404 a save can answer is resolved before `save_mapping` runs, so a refused save leaves
    no half-written mapping for the next screen to find."""
    ctx = _onboarded_client(full_client)
    suggestion = _suggest_mapping(full_client, ctx["client_id"], ctx["entity_source_id"])
    refused = full_client.put(
        f"/clients/{ctx['client_id']}/mappings/{suggestion['mapping_id']}",
        json={
            "client_id": ctx["client_id"],
            "source_id": ctx["entity_source_id"],
            "use_case": UNKNOWN_ID,
            "role": "entity",
            "columns": suggestion["columns"],
        },
    )
    assert refused.status_code == 404
    assert full_client.get(f"/clients/{ctx['client_id']}/mappings").json()["mappings"] == []


def test_save_mapping_persists_it_under_the_suggested_id(full_client: TestClient) -> None:
    ctx = _onboarded_client(full_client)
    suggestion = _suggest_mapping(full_client, ctx["client_id"], ctx["entity_source_id"])
    _save_mapping(full_client, ctx["client_id"], suggestion)
    listed = full_client.get(f"/clients/{ctx['client_id']}/mappings")
    assert listed.status_code == 200, listed.text
    ids = [row["mapping_id"] for row in listed.json()["mappings"]]
    assert ids == [suggestion["mapping_id"]]


def test_save_mapping_of_an_unknown_client_is_404(full_client: TestClient) -> None:
    ctx = _onboarded_client(full_client)
    suggestion = _suggest_mapping(full_client, ctx["client_id"], ctx["entity_source_id"])
    response = full_client.put(
        f"/clients/c_does_not_exist_1/mappings/{suggestion['mapping_id']}",
        json={
            "client_id": "c_does_not_exist_1",
            "source_id": suggestion["source_id"],
            "use_case": suggestion["use_case"],
            "role": suggestion["role"],
            "columns": suggestion["columns"],
        },
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "CLIENT_NOT_FOUND"


def test_save_mapping_naming_another_client_is_409(full_client: TestClient) -> None:
    ctx = _onboarded_client(full_client)
    other_client_id = create_client_via_api(full_client, name="Other Co")["client_id"]
    suggestion = _suggest_mapping(full_client, ctx["client_id"], ctx["entity_source_id"])
    response = full_client.put(
        f"/clients/{ctx['client_id']}/mappings/{suggestion['mapping_id']}",
        json={
            "client_id": other_client_id,
            "source_id": suggestion["source_id"],
            "use_case": suggestion["use_case"],
            "role": suggestion["role"],
            "columns": suggestion["columns"],
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "CLIENT_MISMATCH"


# ---------------------------------------------------------------------------
# GET /clients/{id}/mappings
# ---------------------------------------------------------------------------
def test_list_mappings_is_newest_first(full_client: TestClient) -> None:
    ctx = _mapped_client(full_client)
    response = full_client.get(f"/clients/{ctx['client_id']}/mappings")
    assert response.status_code == 200, response.text
    ids = {row["mapping_id"] for row in response.json()["mappings"]}
    assert ids == {ctx["entity_mapping_id"], ctx["complaints_mapping_id"]}


def test_list_mappings_narrows_by_use_case(full_client: TestClient) -> None:
    ctx = _mapped_client(full_client)
    response = full_client.get(f"/clients/{ctx['client_id']}/mappings", params={"use_case": UNKNOWN_ID})
    assert response.status_code == 200, response.text
    assert response.json()["mappings"] == []


def test_list_mappings_of_an_unknown_client_is_404(full_client: TestClient) -> None:
    response = full_client.get("/clients/c_does_not_exist_1/mappings")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "CLIENT_NOT_FOUND"


# ---------------------------------------------------------------------------
# POST /clients/{id}/onboarding-specs
# ---------------------------------------------------------------------------
def test_create_onboarding_spec_returns_checks(full_client: TestClient) -> None:
    ctx = _mapped_client(full_client)
    spec = _create_spec(full_client, ctx)
    assert spec["spec_id"]
    assert isinstance(spec["checks"], list)


def test_create_onboarding_spec_of_an_unknown_client_is_404(full_client: TestClient) -> None:
    response = full_client.post(
        "/clients/c_does_not_exist_1/onboarding-specs",
        json={
            "use_case": TELCO_CHURN,
            "entity_source_id": "src_x",
            "mapping_ids": [],
            "feature_spec": {"features": []},
            "snapshot_spec": {"mode": "single"},
        },
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "CLIENT_NOT_FOUND"


def test_create_onboarding_spec_naming_an_unknown_source_is_404(full_client: TestClient) -> None:
    client_id = create_client_via_api(full_client)["client_id"]
    response = full_client.post(
        f"/clients/{client_id}/onboarding-specs",
        json={
            "use_case": TELCO_CHURN,
            "entity_source_id": "src_does_not_exist",
            "mapping_ids": [],
            "feature_spec": {"features": []},
            "snapshot_spec": {"mode": "single"},
        },
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "SOURCE_NOT_FOUND"


def test_create_onboarding_spec_naming_an_unknown_mapping_is_404(full_client: TestClient) -> None:
    ctx = _onboarded_client(full_client)
    response = full_client.post(
        f"/clients/{ctx['client_id']}/onboarding-specs",
        json={
            "use_case": TELCO_CHURN,
            "entity_source_id": ctx["entity_source_id"],
            "mapping_ids": ["map_does_not_exist"],
            "feature_spec": {"features": []},
            "snapshot_spec": {"mode": "single"},
        },
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "MAPPING_NOT_FOUND"


def test_create_onboarding_spec_naming_a_mapping_for_another_file_is_409(full_client: TestClient) -> None:
    """`OnboardingSpec` documents `mapping_ids` as one per source; a mapping for a file the recipe
    never reads has nothing to be applied to, so it is refused rather than saved and ignored."""
    ctx = _mapped_client(full_client)
    response = full_client.post(
        f"/clients/{ctx['client_id']}/onboarding-specs",
        json={
            "use_case": TELCO_CHURN,
            "entity_source_id": ctx["entity_source_id"],
            "event_source_ids": [],
            "mapping_ids": [ctx["entity_mapping_id"], ctx["complaints_mapping_id"]],
            "feature_spec": {"features": []},
            "snapshot_spec": {"mode": "single"},
        },
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "MAPPING_NOT_FOR_SPEC"


# ---------------------------------------------------------------------------
# GET /clients/{id}/onboarding-specs
# ---------------------------------------------------------------------------
def test_list_onboarding_specs_is_newest_first(full_client: TestClient) -> None:
    ctx = _mapped_client(full_client)
    spec = _create_spec(full_client, ctx)
    response = full_client.get(f"/clients/{ctx['client_id']}/onboarding-specs")
    assert response.status_code == 200, response.text
    ids = [row["spec_id"] for row in response.json()["specs"]]
    assert ids == [spec["spec_id"]]


def test_list_onboarding_specs_of_an_unknown_client_is_404(full_client: TestClient) -> None:
    response = full_client.get("/clients/c_does_not_exist_1/onboarding-specs")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "CLIENT_NOT_FOUND"


# ---------------------------------------------------------------------------
# POST /clients/{id}/onboarding-specs/{sid}/preview
# ---------------------------------------------------------------------------
def test_preview_returns_rows_and_per_snapshot_stats(full_client: TestClient, storage: LocalStorage) -> None:
    """A real 200-entity build on the request thread: rows, one stat per snapshot, a null rate per
    feature, and every one of them measured by the build rather than shaped by this route."""
    ctx = _mapped_client(full_client)
    spec = _create_spec(full_client, ctx)
    response = full_client.post(f"/clients/{ctx['client_id']}/onboarding-specs/{spec['spec_id']}/preview")
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body["rows"], list)
    assert isinstance(body["per_snapshot"], list)
    assert isinstance(body["feature_null_rates"], dict)
    assert isinstance(body["checks"], list)
    assert all(isinstance(value, str) for row in body["rows"] for value in row.values())
    assert all(0.0 <= rate <= 1.0 for rate in body["feature_null_rates"].values())
    # A preview builds under a real dataset id and must take it with it when it goes, so no dataset
    # directory survives a preview - and `GET /datasets` never learns of one.
    assert storage.list_keys("datasets/") == ()
    listed = full_client.get("/datasets", params={"client_id": ctx["client_id"]})
    assert listed.status_code == 200, listed.text
    assert listed.json()["datasets"] == []


def test_preview_samples_rather_than_building_everything_and_answers_inside_its_budget(
    full_client: TestClient,
) -> None:
    """Plan §9, M12 gives the preview a budget - it exists so a user sees the effect of a window or a
    filter before committing, and a preview that takes as long as the build is no use. It reads
    `PREVIEW_SAMPLE_ENTITIES` entities out of a client with far more, so what is asserted is both
    halves of that: it is bounded by the sample, and it comes back inside the budget.

    The bound is deliberately looser than the plan's ten seconds, because a slow CI box must not read
    as a broken route; what it catches is the thing worth catching, a preview that quietly builds the
    whole dataset."""
    ctx = _buildable_client(full_client)
    spec = _create_spec(full_client, ctx)
    started = time.monotonic()
    response = full_client.post(f"/clients/{ctx['client_id']}/onboarding-specs/{spec['spec_id']}/preview")
    elapsed = time.monotonic() - started
    assert response.status_code == 200, response.text
    assert elapsed < 30.0, f"the preview took {elapsed:.1f}s; it is meant to sample, not build"
    entities = {row["entity_key"] for row in response.json()["rows"] if "entity_key" in row}
    assert entities, "a preview of a buildable recipe has rows"
    assert len(entities) <= PREVIEW_SAMPLE_ENTITIES


def test_preview_of_a_recipe_with_a_blocking_check_answers_200_with_the_checks_and_no_rows(
    full_client: TestClient,
) -> None:
    """The preview screen's whole job is showing a user what is wrong, so a structural error is a
    `200` carrying the checks - and emphatically not a `200` carrying a sample of something that was
    never built."""
    ctx = _onboarded_client(full_client)
    entity_suggestion = _suggest_mapping(full_client, ctx["client_id"], ctx["entity_source_id"])
    broken = full_client.put(
        f"/clients/{ctx['client_id']}/mappings/{entity_suggestion['mapping_id']}",
        json={
            "client_id": ctx["client_id"],
            "source_id": ctx["entity_source_id"],
            "use_case": TELCO_CHURN,
            "role": "entity",
            "columns": [],
        },
    )
    assert broken.status_code == 200, broken.text
    complaints = _suggest_mapping(full_client, ctx["client_id"], ctx["complaints_source_id"])
    ctx["entity_mapping_id"] = entity_suggestion["mapping_id"]
    ctx["complaints_mapping_id"] = _save_mapping(full_client, ctx["client_id"], complaints)["mapping_id"]
    spec = _create_spec(full_client, ctx)

    response = full_client.post(f"/clients/{ctx['client_id']}/onboarding-specs/{spec['spec_id']}/preview")
    assert response.status_code == 200, response.text
    body = response.json()
    assert any(check["severity"] == "error" and not check["acknowledged"] for check in body["checks"])
    assert body["rows"] == []
    assert body["per_snapshot"] == []
    assert body["feature_null_rates"] == {}


def test_preview_of_an_unknown_client_is_404(full_client: TestClient) -> None:
    response = full_client.post("/clients/c_does_not_exist_1/onboarding-specs/spec_x/preview")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "CLIENT_NOT_FOUND"


def test_preview_of_an_unknown_spec_is_404(full_client: TestClient) -> None:
    client_id = create_client_via_api(full_client)["client_id"]
    response = full_client.post(f"/clients/{client_id}/onboarding-specs/spec_does_not_exist/preview")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "ONBOARDING_SPEC_NOT_FOUND"


# ---------------------------------------------------------------------------
# POST /datasets
# ---------------------------------------------------------------------------
def _poll_until_finished(client: TestClient, dataset_id: str, *, timeout: float = 5.0) -> dict[str, Any]:
    """Poll `GET /datasets/{id}` until the build leaves `pending`/`running`, or `timeout` elapses."""
    deadline = time.monotonic() + timeout
    body: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = client.get(f"/datasets/{dataset_id}")
        assert response.status_code == 200, response.text
        body = response.json()
        if body["status"]["state"] not in {"pending", "running"}:
            return body
        time.sleep(0.05)
    return body


def test_create_dataset_with_a_deliberately_broken_spec_returns_409_with_the_codes(
    full_client: TestClient,
) -> None:
    """A mapping that maps nothing at all - not even the entity key - is about as broken as a recipe
    can be; whatever exact codes `run_onboarding_checks` names, at least one of them must be an
    unacknowledged error, and `POST /datasets` must refuse before anything is queued."""
    ctx = _onboarded_client(full_client)
    entity_suggestion = _suggest_mapping(full_client, ctx["client_id"], ctx["entity_source_id"])
    broken = full_client.put(
        f"/clients/{ctx['client_id']}/mappings/{entity_suggestion['mapping_id']}",
        json={
            "client_id": ctx["client_id"],
            "source_id": ctx["entity_source_id"],
            "use_case": TELCO_CHURN,
            "role": "entity",
            "columns": [],
        },
    )
    assert broken.status_code == 200, broken.text
    complaints_suggestion = _suggest_mapping(full_client, ctx["client_id"], ctx["complaints_source_id"])
    complaints_mapping_id = _save_mapping(full_client, ctx["client_id"], complaints_suggestion)["mapping_id"]
    ctx["entity_mapping_id"] = entity_suggestion["mapping_id"]
    ctx["complaints_mapping_id"] = complaints_mapping_id
    spec = _create_spec(full_client, ctx)

    response = full_client.post(
        "/datasets", json={"client_id": ctx["client_id"], "spec_id": spec["spec_id"], "mode": "train"}
    )
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["detail"]["code"] == "ONBOARDING_CHECKS_FAILED"
    assert body["checks"], "a mapping with no columns at all must fail at least one check"
    assert any(check["severity"] == "error" and not check["acknowledged"] for check in body["checks"])


def test_create_dataset_with_a_good_spec_returns_202_and_a_pollable_id(full_client: TestClient) -> None:
    """The whole path, on data a build can finish: `202` with a `Location`, a first poll that already
    finds something true, and a manifest once the job is done.

    Scoring mode because these two files carry no outcome to learn from, and a recipe with no
    `label_spec` is a scoring-only recipe by `OnboardingSpec`'s own definition - asking for `train`
    here would be asking the engine to derive a label this data cannot support, which is a fact about
    the fixture and not about the route.
    """
    ctx = _buildable_client(full_client)
    spec = _create_spec(full_client, ctx)
    response = full_client.post(
        "/datasets", json={"client_id": ctx["client_id"], "spec_id": spec["spec_id"], "mode": "score"}
    )
    assert response.status_code == 202, response.text
    dataset_id = response.json()["dataset_id"]
    assert response.headers["Location"] == f"/datasets/{dataset_id}"

    # The very first poll, microseconds after the 202, must already find something true to render.
    first = full_client.get(f"/datasets/{dataset_id}")
    assert first.status_code == 200, first.text
    assert first.json()["status"]["dataset_id"] == dataset_id
    assert first.json()["manifest"] is None, "nothing is built yet, so nothing is claimed"

    final = _poll_until_finished(full_client, dataset_id, timeout=BUILD_TIMEOUT_S)
    assert final["status"]["state"] == "done", final["status"]
    assert final["manifest"] is not None
    assert final["manifest"]["dataset_id"] == dataset_id
    assert final["manifest"]["spec_id"] == spec["spec_id"]
    assert final["manifest"]["n_rows"] > 0


def _built_leak_check(
    client: TestClient, ctx: dict[str, str], spec_id: str, **options: Any
) -> dict[str, Any]:
    """Build `spec_id` in score mode to `done` and return its report's `leak_check`, with the recipe
    hash its manifest recorded beside it."""
    response = client.post(
        "/datasets", json={"client_id": ctx["client_id"], "spec_id": spec_id, "mode": "score", **options}
    )
    assert response.status_code == 202, response.text
    dataset_id = response.json()["dataset_id"]
    final = _poll_until_finished(client, dataset_id, timeout=BUILD_TIMEOUT_S)
    assert final["status"]["state"] == "done", final["status"]
    report = client.get(f"/datasets/{dataset_id}/report")
    assert report.status_code == 200, report.text
    check: dict[str, Any] = report.json()["leak_check"]
    assert final["manifest"]["recipe_hash"] == check["recipe_hash"]
    return check


def test_the_first_build_of_a_recipe_runs_the_full_leak_check_and_later_ones_the_narrow(
    full_client: TestClient,
) -> None:
    """Ruling R1 (DEC-870, DEC-871) through the route: the first build of a recipe for a client is
    the full check whatever was asked, a later build of the same recipe is the narrowed default,
    `full_leak_check: true` asks for the full one again, and a second spec saying the same thing
    under a new id is the same recipe."""
    ctx = _buildable_client(full_client)
    spec_id = _create_spec(full_client, ctx)["spec_id"]

    first = _built_leak_check(full_client, ctx, spec_id)
    assert (first["scope"], first["reason"]) == ("full", "first_build_of_recipe")
    assert first["rows_probed"] == first["rows_total"] > 0
    assert first["summary"].startswith("Full future-data check")

    second = _built_leak_check(full_client, ctx, spec_id)
    assert (second["scope"], second["reason"]) == ("narrow", "default")
    assert second["recipe_hash"] == first["recipe_hash"]

    asked = _built_leak_check(full_client, ctx, spec_id, full_leak_check=True)
    assert (asked["scope"], asked["reason"]) == ("full", "option")
    assert asked["rows_probed"] == asked["rows_total"]

    same_recipe = _create_spec(full_client, ctx)["spec_id"]
    assert same_recipe != spec_id
    again = _built_leak_check(full_client, ctx, same_recipe)
    assert (again["scope"], again["reason"]) == ("narrow", "default")


def test_a_failed_build_does_not_count_as_the_first_build_of_its_recipe(
    full_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a registered dataset vouches for a recipe, and only a build that passed is registered."""
    ctx = _buildable_client(full_client)
    spec_id = _create_spec(full_client, ctx)["spec_id"]
    real = build.build_dataset
    seen: list[bool] = []

    def failing(**kwargs: Any) -> Any:
        seen.append(kwargs["first_build_of_recipe"])
        raise RuntimeError("the build stopped")

    monkeypatch.setattr(build, "build_dataset", failing)
    response = full_client.post(
        "/datasets", json={"client_id": ctx["client_id"], "spec_id": spec_id, "mode": "score"}
    )
    assert response.status_code == 202, response.text
    assert _poll_until_finished(full_client, response.json()["dataset_id"])["status"]["state"] == "failed"
    monkeypatch.setattr(build, "build_dataset", real)

    after = _built_leak_check(full_client, ctx, spec_id)
    assert seen == [True]
    assert (after["scope"], after["reason"]) == ("full", "first_build_of_recipe")


@pytest.mark.parametrize("subset", [[], ["src_anything"]])
def test_create_dataset_restricted_to_some_files_is_refused_not_silently_widened(
    full_client: TestClient, subset: list[str]
) -> None:
    """`engine.onboarding.build.build_dataset` reads every file the recipe names and hands all of
    them to `build_manifest`, which refuses a manifest that does not cover the recipe - so there is
    no narrowed build to run. The request says so rather than building the whole recipe under a name
    the caller would read as "only these files", and nothing is queued."""
    ctx = _mapped_client(full_client)
    spec = _create_spec(full_client, ctx)
    response = full_client.post(
        "/datasets",
        json={
            "client_id": ctx["client_id"],
            "spec_id": spec["spec_id"],
            "mode": "train",
            "source_ids": subset,
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "DATASET_SOURCE_SUBSET_NOT_AVAILABLE"
    assert full_client.get("/datasets", params={"client_id": ctx["client_id"]}).json()["datasets"] == []


# ---------------------------------------------------------------------------
# A build engine that stops without saying so
# ---------------------------------------------------------------------------
def _stub_build(monkeypatch: pytest.MonkeyPatch, build_dataset: Callable[..., Any]) -> None:
    """Replace `engine.onboarding.build.build_dataset` for one test.

    `api/routes/datasets.py` calls it through the module rather than by name, for the reason
    `api/routes/uploads.py` records about `engine.stages.ingest`: one seam to stub, and no test
    reaching into the route's own globals. `monkeypatch` puts the real function back afterwards.
    """
    monkeypatch.setattr(build, "build_dataset", build_dataset)


@pytest.fixture
def crashing_build_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """A `build_dataset` that dies before it writes anything at all."""

    def build_dataset(**_kwargs: Any) -> None:
        raise RuntimeError("customer C-0042 broke the join")

    _stub_build(monkeypatch, build_dataset)


@pytest.fixture
def self_reporting_build_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """A `build_dataset` that records its own failure on `build_status.json`, then raises."""

    def build_dataset(**kwargs: Any) -> None:
        spec = kwargs["spec"]
        registry = kwargs["registry"]
        dataset_id = kwargs["dataset_id"]
        registry.write_status(
            dataset_id,
            BuildStatus(
                dataset_id=dataset_id,
                client_id=spec.client_id,
                spec_id=spec.spec_id,
                state=RunState.FAILED,
                updated_at=utc_now(),
                stages=(
                    BuildStage(
                        key="apply_mappings",
                        title="Apply mappings",
                        group_label="Build",
                        state=RunState.FAILED,
                        detail="Two rows of the subscriber table share one id.",
                    ),
                ),
                progress_pct=0,
                detail="Two rows of the subscriber table share one id.",
                error="ENTITY_DUPLICATE_KEYS",
            ),
        )
        raise RuntimeError("stopped at apply_mappings")

    _stub_build(monkeypatch, build_dataset)


def _build_and_poll(client: TestClient, ctx: dict[str, str], *, mode: str = "train") -> dict[str, Any]:
    spec = _create_spec(client, ctx)
    created = client.post(
        "/datasets", json={"client_id": ctx["client_id"], "spec_id": spec["spec_id"], "mode": mode}
    )
    assert created.status_code == 202, created.text
    return _poll_until_finished(client, created.json()["dataset_id"], timeout=BUILD_TIMEOUT_S)


def test_a_build_that_dies_settles_as_failed_instead_of_polling_queued_for_ever(
    full_client: TestClient, crashing_build_engine: None
) -> None:
    """`POST /datasets` writes the `queued` status itself, so until the build takes that document
    over this route still owns it. A build engine that raises on its first line must not leave that
    "queued" standing as the last word - the Build screen would poll it until the user gave up."""
    final = _build_and_poll(full_client, _mapped_client(full_client))
    assert final["status"]["state"] == "failed"
    assert final["status"]["error"] == "DATASET_BUILD_FAILED"
    assert final["status"]["detail"]
    assert final["manifest"] is None
    # Nothing was measured, so nothing is claimed: no progress figure, no invented stage list.
    assert final["status"]["progress_pct"] == 0
    assert len(final["status"]["stages"]) == 1
    # And the engine's own exception text - which can quote a row - never reaches the screen.
    assert "C-0042" not in json.dumps(final)


def test_a_build_that_reported_its_own_failure_keeps_that_reason(
    full_client: TestClient, self_reporting_build_engine: None
) -> None:
    """The fallback above must not overwrite the better answer: a build that recorded the stage it
    failed at, and why, keeps both."""
    final = _build_and_poll(full_client, _mapped_client(full_client))
    assert final["status"]["state"] == "failed"
    assert final["status"]["error"] == "ENTITY_DUPLICATE_KEYS"
    assert final["status"]["stages"][0]["key"] == "apply_mappings"


def test_create_dataset_of_an_unknown_client_is_404(full_client: TestClient) -> None:
    response = full_client.post(
        "/datasets", json={"client_id": "c_does_not_exist_1", "spec_id": "spec_x", "mode": "train"}
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "CLIENT_NOT_FOUND"


def test_create_dataset_naming_an_unknown_spec_is_404(full_client: TestClient) -> None:
    client_id = create_client_via_api(full_client)["client_id"]
    response = full_client.post(
        "/datasets", json={"client_id": client_id, "spec_id": "spec_does_not_exist", "mode": "train"}
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "ONBOARDING_SPEC_NOT_FOUND"


# ---------------------------------------------------------------------------
# GET /datasets/{id}, /report, /sample, /features.sql
# ---------------------------------------------------------------------------
def test_read_an_unknown_dataset_is_404(full_client: TestClient) -> None:
    response = full_client.get("/datasets/ds_does_not_exist")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "DATASET_NOT_FOUND"


def test_read_report_of_an_unknown_dataset_is_404(full_client: TestClient) -> None:
    response = full_client.get("/datasets/ds_does_not_exist/report")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "DATASET_NOT_FOUND"


def test_read_sample_of_an_unknown_dataset_is_404(full_client: TestClient) -> None:
    response = full_client.get("/datasets/ds_does_not_exist/sample")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "DATASET_NOT_FOUND"


def test_read_features_sql_of_an_unknown_dataset_is_404(full_client: TestClient) -> None:
    response = full_client.get("/datasets/ds_does_not_exist/features.sql")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "DATASET_NOT_FOUND"


@pytest.mark.parametrize("artefact", ["report", "sample", "features.sql"])
def test_an_artefact_of_a_build_that_produced_nothing_is_409_not_404(
    full_client: TestClient, crashing_build_engine: None, artefact: str
) -> None:
    """A dataset whose build produced no artefacts still exists: it has a directory, a status and an
    id the Build screen is polling. Answering "no dataset with id ..." would tell a user that the
    build they are watching is not there - so the answer is a `409` that says what is actually true."""
    final = _build_and_poll(full_client, _mapped_client(full_client))
    dataset_id = final["status"]["dataset_id"]

    response = full_client.get(f"/datasets/{dataset_id}/{artefact}")
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "DATASET_NOT_BUILT"
    assert detail["message"]


def test_a_build_stopped_by_its_own_checks_still_serves_the_report_but_has_no_sample(
    full_client: TestClient,
) -> None:
    """ "A build whose checks found an error writes `build_report.json` and nothing else: there is no
    dataset, and the report is what the user reads" - `build_dataset`'s own words. So the report is a
    `200` and the sample, which was never produced, is the `409` rather than an empty list of rows
    that would read as "we built it and it came out empty"."""
    final = _build_and_poll(full_client, _mapped_client(full_client))
    dataset_id = final["status"]["dataset_id"]
    assert final["status"]["state"] == "failed"
    assert final["manifest"] is None

    report = full_client.get(f"/datasets/{dataset_id}/report")
    assert report.status_code == 200, report.text
    body = report.json()
    assert body["passed"] is False
    assert any(check["severity"] == "error" for check in body["checks"])

    sample = full_client.get(f"/datasets/{dataset_id}/sample")
    assert sample.status_code == 409, sample.text
    assert sample.json()["detail"]["code"] == "DATASET_NOT_BUILT"


def test_every_artefact_of_a_finished_build_is_served_from_what_the_build_wrote(
    full_client: TestClient,
) -> None:
    """The three artefact endpoints and the dataset list, on a build that actually finished. Each
    serves the document the build wrote - this route computes none of it - so the assertions are
    about shape and identity, which is all an API contract can honestly promise about numbers it did
    not produce."""
    ctx = _buildable_client(full_client)
    final = _build_and_poll(full_client, ctx, mode="score")
    assert final["status"]["state"] == "done", final["status"]
    dataset_id = final["status"]["dataset_id"]
    assert final["manifest"]["dataset_id"] == dataset_id

    report = full_client.get(f"/datasets/{dataset_id}/report")
    assert report.status_code == 200, report.text
    assert report.json()["dataset_id"] == dataset_id

    sample = full_client.get(f"/datasets/{dataset_id}/sample")
    assert sample.status_code == 200, sample.text
    rows = sample.json()["rows"]
    assert isinstance(rows, list)
    assert all(isinstance(value, str) for row in rows for value in row.values())

    features_sql = full_client.get(f"/datasets/{dataset_id}/features.sql")
    assert features_sql.status_code == 200, features_sql.text
    assert features_sql.headers["content-type"].startswith("text/plain")

    listed = full_client.get("/datasets", params={"client_id": ctx["client_id"]})
    assert listed.status_code == 200, listed.text
    assert dataset_id in {row["dataset_id"] for row in listed.json()["datasets"]}


# ---------------------------------------------------------------------------
# GET /datasets
# ---------------------------------------------------------------------------
def test_list_datasets_of_an_unknown_client_is_empty_not_404(full_client: TestClient) -> None:
    """Unlike every `/clients/{id}/...` route, `GET /datasets` names no client in the path - only a
    query filter - so an id nothing matches is an empty list, the same way `GET /runs?use_case=`
    answers an unknown use case with zero rows rather than a 404."""
    response = full_client.get("/datasets", params={"client_id": "c_does_not_exist_1"})
    assert response.status_code == 200, response.text
    assert response.json()["datasets"] == []


# ---------------------------------------------------------------------------
# Every error body is the M1 envelope
# ---------------------------------------------------------------------------
def test_every_error_body_is_the_m1_envelope(full_client: TestClient) -> None:
    detail = full_client.get("/datasets/ds_does_not_exist").json()["detail"]
    assert set(detail) == {"code", "message", "path"}
    assert isinstance(detail["message"], str) and detail["message"]
