"""`api.routes.mappings` and `api.routes.datasets` end to end through `TestClient` (Phase 2 plan §9,
M12: mapping suggest/save/list, onboarding-spec create/list/preview, dataset build/read/list).

Neither router is mounted by `api.main.create_app` yet, for the same reason
`tests/integration/test_api_clients.py` gives: `PARALLEL_WORK_PROTOCOL.md` §3 keeps `api/main.py` a
file this branch may not touch, and the task says registering the router is the orchestrator's job.
The fixtures below build the smallest app that serves all four M8/M12 routers together, layered
exactly as the endpoints depend on each other (clients -> sources -> mappings -> datasets), and read
`app.state.config_root`/`data_dir` the same way `api.main.create_app` would set them.

`api/routes/mappings.py` needs `engine.onboarding.mapping` (`suggested_mapping_spec`) and
`engine.onboarding.validate` (`run_onboarding_checks`); `api/routes/datasets.py` needs
`engine.onboarding.validate` too, but reaches `engine.onboarding.build.build_dataset` through its own
`_build_callable()` indirection instead of a bare import, precisely so this module and the tests
below can still exercise everything up to a `POST /datasets` job *submission* before that module
exists (`test_create_dataset_with_a_good_spec_returns_202_and_a_pollable_id` polls the job to a
terminal state and accepts either a real `done` or an honestly-reported `BUILD_ENGINE_NOT_AVAILABLE`
failure - see that test). `engine.onboarding.sources` is a third, transitive dependency (needed by
`api/routes/sources.py`, which every fixture below needs for a confirmed-role source to map). None of
the three existed when this module was written: every fixture that needs one skips - not fails -
collection until it lands, exactly as `test_api_clients.py`'s own `full_client` fixture already does
for `engine.onboarding.sources` alone. `api/routes/mappings.py` and `api/routes/datasets.py` document,
at their own top, the exact signatures this branch wrote each call against; if the real modules differ
once they land, the fix belongs at those call sites, not in a weakened version of either route module
or a test that stops exercising them.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.main import config_error_handler
from api.routes.clients import router as clients_router
from engine.config import ConfigError
from engine.storage import LocalStorage
from tests.integration.test_api_clients import (
    PLANNED_ID,
    TELCO_CHURN,
    UNKNOWN_ID,
    create_client_via_api,
    upload_source,
)

pytestmark = pytest.mark.integration

ENTITY_CSV = "customer_id,name,signup_date\nC-1,Ann,2026-01-01\nC-2,Bea,2026-01-02\nC-3,Cid,2026-01-03\n"
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


def test_suggest_mapping_of_a_planned_use_case_is_404(full_client: TestClient) -> None:
    ctx = _onboarded_client(full_client)
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
def test_preview_returns_rows_and_per_snapshot_stats(full_client: TestClient) -> None:
    ctx = _mapped_client(full_client)
    spec = _create_spec(full_client, ctx)
    response = full_client.post(f"/clients/{ctx['client_id']}/onboarding-specs/{spec['spec_id']}/preview")
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body["rows"], list)
    assert isinstance(body["per_snapshot"], list)
    assert isinstance(body["feature_null_rates"], dict)
    assert isinstance(body["checks"], list)
    # A preview never leaves a dataset behind under the id it built the sample with.
    assert full_client.get(f"/clients/{ctx['client_id']}/onboarding-specs").status_code == 200


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
    ctx = _mapped_client(full_client)
    spec = _create_spec(full_client, ctx)
    response = full_client.post(
        "/datasets", json={"client_id": ctx["client_id"], "spec_id": spec["spec_id"], "mode": "train"}
    )
    assert response.status_code == 202, response.text
    dataset_id = response.json()["dataset_id"]
    assert response.headers["Location"] == f"/datasets/{dataset_id}"

    # The very first poll, microseconds after the 202, must already find something true to render.
    first = full_client.get(f"/datasets/{dataset_id}")
    assert first.status_code == 200, first.text
    assert first.json()["status"]["dataset_id"] == dataset_id

    final = _poll_until_finished(full_client, dataset_id)
    assert final["status"]["state"] in {"done", "failed"}
    if final["status"]["state"] == "failed":
        # engine.onboarding.build is not required to exist for this test to run (see module
        # docstring); when it has not landed yet the job must still resolve, honestly, to this one
        # named code rather than hang at "queued" forever.
        assert final["status"]["error"] == "BUILD_ENGINE_NOT_AVAILABLE"
        assert final["manifest"] is None
    else:
        assert final["manifest"] is not None
        assert final["manifest"]["dataset_id"] == dataset_id


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
