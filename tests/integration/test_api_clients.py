"""`api.routes.clients` and `api.routes.sources` end to end through `TestClient` (Phase 2 plan §9, M12).

Neither router is mounted by `api.main.create_app` yet - `PARALLEL_WORK_PROTOCOL.md` §3 keeps
`api/main.py` a file this branch may not touch, and the task says so plainly: registering the router
is the orchestrator's job, not this branch's. So the fixtures below assemble the smallest app that
serves these routes itself: `ConfigRootDep`/`StorageDep` read `app.state.config_root`/`data_dir`
exactly as `api.main.create_app` sets them, and the one `ConfigError` a bare app would otherwise let
through unhandled (`read_standard_schema` on a planned or unknown use case) is caught by importing
`api.main`'s own handler rather than reimplementing its status-code table.

`engine.onboarding.sources` is landing in parallel and did not exist when this module was written
(see `api/routes/sources.py`'s module docstring for the signature it is called against), so every
test that needs `api.routes.sources` goes through the `full_client` fixture, which skips - not fails
- collection until that module exists. The client-only tests, and `GET
/use-cases/{id}/standard-schema` (this branch's, defined in `clients.py` because `use_cases.py` is
not ours to edit), have no such dependency and always run.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.main import config_error_handler
from api.routes.clients import router as clients_router
from engine.config import (
    ConfigError,
    FeatureDef,
    LabelDefinition,
    RoleCatalogue,
    StandardSchemaConfig,
)
from engine.onboarding.specs import ClientRecord
from engine.storage import LocalStorage

pytestmark = pytest.mark.integration

TELCO_CHURN = "telco-churn"
PLANNED_ID = "ai-onboarding-assistant"
UNKNOWN_ID = "not-a-use-case"

ENTITY_CSV = "customer_id,name,signup_date\nC-1,Ann,2026-01-01\nC-2,Bea,2026-01-02\nC-3,Cid,2026-01-03\n"
EVENT_CSV = (
    "customer_id,complaint_date,channel\n"
    "C-1,2026-02-01,phone\n"
    "C-2,2026-02-03,email\n"
    "C-1,2026-02-10,phone\n"
    "C-9,2026-02-11,email\n"
)

EVENT_KEY_COVERAGE = 0.75
"""What `EVENT_CSV.customer_id` against `ENTITY_CSV.customer_id` has to come to, counted by hand:
four non-null event keys, of which C-1, C-2 and C-1 are in the entity table and C-9 is not - 3/4.

Every coverage assertion below is against this hand-counted number rather than against "some float
between 0 and 1", because a badge that renders whatever the API happens to send is exactly the thing
these tests exist to catch: `0.75` can only come from a real join, where a range check also passes
when nothing was measured at all, when the wrong pair of columns was compared, or when a stale
number from a deleted source was left on the record."""


# ---------------------------------------------------------------------------
# Fixtures: the smallest app that serves these two routers (see module docstring)
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
def client_app(config_root: Path, data_dir: Path) -> FastAPI:
    app = _bare_app(config_root, data_dir)
    app.include_router(clients_router)
    return app


@pytest.fixture
def client(client_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(client_app) as test_client:
        yield test_client


@pytest.fixture
def full_app(config_root: Path, data_dir: Path) -> FastAPI:
    """`client_app` plus the sources router - skipped, not failed, until its dependency exists."""
    pytest.importorskip("engine.onboarding.sources")
    from api.routes.sources import router as sources_router

    app = _bare_app(config_root, data_dir)
    app.include_router(clients_router)
    app.include_router(sources_router)
    return app


@pytest.fixture
def full_client(full_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(full_app) as test_client:
        yield test_client


@pytest.fixture
def storage(data_dir: Path) -> LocalStorage:
    return LocalStorage(data_dir)


def small_limits_config_root(
    config_root: Path,
    destination: Path,
    *,
    max_sources: int | None = None,
    max_source_rows: int | None = None,
) -> Path:
    """A copy of `configs/` with `onboarding.limits` shrunk, for the two limit tests below."""
    copied = destination / "configs"
    shutil.copytree(config_root, copied)
    engine_yaml = copied / "engine.yaml"
    text = engine_yaml.read_text()
    if max_sources is not None:
        text = text.replace("max_sources: 10", f"max_sources: {max_sources}")
    if max_source_rows is not None:
        text = text.replace("max_source_rows: 50000000", f"max_source_rows: {max_source_rows}")
    engine_yaml.write_text(text)
    return copied


def create_client_via_api(
    client: TestClient, *, name: str = "Acme Water Co", industry: str = "telecom", notes: str = ""
) -> dict[str, Any]:
    response = client.post("/clients", json={"name": name, "industry": industry, "notes": notes})
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def upload_source(
    client: TestClient,
    client_id: str,
    payload: bytes = ENTITY_CSV.encode(),
    *,
    name: str = "customers.csv",
    role: str | None = None,
) -> Any:
    data = {} if role is None else {"role": role}
    return client.post(
        f"/clients/{client_id}/sources", files={"file": (name, payload, "text/csv")}, data=data
    )


# ---------------------------------------------------------------------------
# POST /clients, GET /clients, GET /clients/{id}
# ---------------------------------------------------------------------------
def test_create_client_returns_201_with_a_stable_id(client: TestClient) -> None:
    body = create_client_via_api(client)
    assert body["client_id"].startswith("c_acme_water_co_")


def test_two_clients_of_the_same_name_get_distinct_ids(client: TestClient) -> None:
    first = create_client_via_api(client, name="Repeat Co")
    second = create_client_via_api(client, name="Repeat Co")
    assert first["client_id"] != second["client_id"]


def test_list_clients_is_newest_first(client: TestClient) -> None:
    older = create_client_via_api(client, name="Repeat Co")
    newer = create_client_via_api(client, name="Repeat Co")
    response = client.get("/clients")
    assert response.status_code == 200, response.text
    ids = [row["client_id"] for row in response.json()["clients"]]
    assert ids[:2] == [newer["client_id"], older["client_id"]]


def test_read_client_round_trips_every_field(client: TestClient) -> None:
    created = create_client_via_api(client, name="Acme", industry="telecom", notes="pilot account")
    response = client.get(f"/clients/{created['client_id']}")
    assert response.status_code == 200, response.text
    record = ClientRecord.model_validate(response.json())
    assert record.client_id == created["client_id"]
    assert record.name == "Acme"
    assert record.industry == "telecom"
    assert record.notes == "pilot account"


def test_read_an_unknown_client_is_404(client: TestClient) -> None:
    response = client.get("/clients/c_does_not_exist_1")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "CLIENT_NOT_FOUND"


def test_every_error_body_is_the_m1_envelope(client: TestClient) -> None:
    detail = client.get("/clients/c_missing_1").json()["detail"]
    assert set(detail) == {"code", "message", "path"}
    assert isinstance(detail["message"], str) and detail["message"]


# ---------------------------------------------------------------------------
# GET /use-cases/{id}/standard-schema
# ---------------------------------------------------------------------------
def test_standard_schema_returns_the_merged_configs_onboarding_vocabulary(client: TestClient) -> None:
    """Four keys, each the engine's own document rather than a shape this route invented.

    The blocks are validated back into the real models to prove it: they all inherit `StrictBase`,
    which forbids unknown keys, so a response that had quietly grown a field of its own - or dropped
    one the mapping UI is generated from - would fail here rather than at the far end in the browser.
    """
    response = client.get(f"/use-cases/{TELCO_CHURN}/standard-schema")
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"standard_schema", "suggested_features", "label", "roles"}

    schema = StandardSchemaConfig.model_validate(body["standard_schema"])
    features = [FeatureDef.model_validate(row) for row in body["suggested_features"]]
    catalogue = RoleCatalogue.model_validate(body["roles"])
    assert schema.columns, "telco-churn's M8 config declares standard columns"
    assert features, "...and the feature library the mapping screen offers"
    assert catalogue.entity_role == "entity"
    # a use case with no label says so with a null, never with a stand-in label (house rule 2)
    if body["label"] is not None:
        LabelDefinition.model_validate(body["label"])


def test_standard_schema_of_an_unknown_use_case_is_404(client: TestClient) -> None:
    response = client.get(f"/use-cases/{UNKNOWN_ID}/standard-schema")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "USE_CASE_NOT_FOUND"


def test_standard_schema_of_a_planned_use_case_is_404(client: TestClient) -> None:
    response = client.get(f"/use-cases/{PLANNED_ID}/standard-schema")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "USE_CASE_PLANNED"


# ---------------------------------------------------------------------------
# POST /clients/{id}/sources
# ---------------------------------------------------------------------------
def test_create_source_profiles_the_file_and_leaves_role_unconfirmed(
    full_client: TestClient, storage: LocalStorage
) -> None:
    client_id = create_client_via_api(full_client)["client_id"]
    response = upload_source(full_client, client_id)
    assert response.status_code == 201, response.text
    assert response.headers["Location"] == f"/clients/{client_id}/sources/{response.json()['source_id']}"
    body = response.json()
    profile = body["profile"]
    assert profile["source_id"] == body["source_id"]
    assert profile["client_id"] == client_id
    assert profile["role"] is None
    assert profile["profile"]["row_count"] == 3
    assert isinstance(profile["role_candidates"], list)
    assert isinstance(profile["key_candidates"], list)
    stored = set(storage.list_keys(f"clients/{client_id}/sources/{body['source_id']}/"))
    assert stored == {
        f"clients/{client_id}/sources/{body['source_id']}/raw.csv",
        f"clients/{client_id}/sources/{body['source_id']}/profile.json",
    }


def test_create_source_with_an_explicit_role_confirms_it(full_client: TestClient) -> None:
    client_id = create_client_via_api(full_client)["client_id"]
    response = upload_source(full_client, client_id, role="entity")
    assert response.status_code == 201, response.text
    assert response.json()["profile"]["role"] == "entity"
    assert response.json()["profile"]["role_decided_by"] == "user"


def test_create_source_with_an_unknown_role_is_409(full_client: TestClient) -> None:
    client_id = create_client_via_api(full_client)["client_id"]
    response = upload_source(full_client, client_id, role="not-a-role")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "ROLE_UNKNOWN"


def test_create_source_of_an_unknown_client_is_404_before_any_byte_is_stored(
    full_client: TestClient, storage: LocalStorage
) -> None:
    response = upload_source(full_client, "c_does_not_exist_1")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "CLIENT_NOT_FOUND"
    assert storage.list_keys("clients/") == ()


def test_create_source_of_an_unsupported_format_is_415_and_stores_nothing(
    full_client: TestClient, storage: LocalStorage
) -> None:
    client_id = create_client_via_api(full_client)["client_id"]
    response = upload_source(full_client, client_id, b"not a table", name="report.docx")
    assert response.status_code == 415
    assert response.json()["detail"]["code"] == "UPLOAD_UNSUPPORTED_FORMAT"
    assert storage.list_keys(f"clients/{client_id}/sources/") == ()


def test_too_many_sources_is_409(config_root: Path, tmp_path: Path, data_dir: Path) -> None:
    pytest.importorskip("engine.onboarding.sources")
    from api.routes.sources import router as sources_router

    root = small_limits_config_root(config_root, tmp_path, max_sources=1)
    app = _bare_app(root, data_dir)
    app.include_router(clients_router)
    app.include_router(sources_router)
    with TestClient(app) as test_client:
        client_id = create_client_via_api(test_client)["client_id"]
        first = upload_source(test_client, client_id)
        assert first.status_code == 201, first.text
        second = upload_source(test_client, client_id, name="more.csv")
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "TOO_MANY_SOURCES"


def test_source_too_large_is_409_and_leaves_no_orphan(
    config_root: Path, tmp_path: Path, data_dir: Path
) -> None:
    pytest.importorskip("engine.onboarding.sources")
    from api.routes.sources import router as sources_router

    root = small_limits_config_root(config_root, tmp_path, max_source_rows=1)
    app = _bare_app(root, data_dir)
    app.include_router(clients_router)
    app.include_router(sources_router)
    with TestClient(app) as test_client:
        client_id = create_client_via_api(test_client)["client_id"]
        response = upload_source(test_client, client_id)  # ENTITY_CSV has 3 data rows, above the cap of 1
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "SOURCE_TOO_LARGE"
    assert LocalStorage(data_dir).list_keys(f"clients/{client_id}/sources/") == ()


def test_too_many_sources_names_the_count_it_measured_not_the_limit(
    config_root: Path, tmp_path: Path, data_dir: Path
) -> None:
    """House rule 2 reaches error messages: the count in the message is the client's real one.

    Lowering `max_sources` under a client that already holds more than the new limit is the case
    that tells the two apart - a message built from the limit would tell this user they hold one
    source when they hold two, and send them looking for the one they would have to delete.
    """
    pytest.importorskip("engine.onboarding.sources")
    from api.routes.sources import router as sources_router

    def app_for(root: Path) -> FastAPI:
        app = _bare_app(root, data_dir)
        app.include_router(clients_router)
        app.include_router(sources_router)
        return app

    with TestClient(app_for(config_root)) as generous:
        client_id = create_client_via_api(generous)["client_id"]
        for name in ("customers.csv", "complaints.csv"):
            assert upload_source(generous, client_id, name=name).status_code == 201

    lowered = small_limits_config_root(config_root, tmp_path / "lowered", max_sources=1)
    with TestClient(app_for(lowered)) as strict:
        refused = upload_source(strict, client_id, name="more.csv")

    assert refused.status_code == 409
    detail = refused.json()["detail"]
    assert detail["code"] == "TOO_MANY_SOURCES"
    assert "2 sources" in detail["message"], detail["message"]
    assert "allows 1" in detail["message"], detail["message"]


def test_a_failure_after_the_row_is_written_leaves_no_half_registered_source(
    full_client: TestClient, storage: LocalStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`uploads.py`'s promise, kept for a source: the last step of `create_source` is made to fail,
    and afterwards the client has neither a listed source nor a file left behind - a registry row
    whose profile never landed would 404 every later `GET /clients/{id}/sources` for this client."""
    from api.routes import sources as sources_module

    client_id = create_client_via_api(full_client)["client_id"]

    def explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("the store went away mid-upload")

    monkeypatch.setattr(sources_module, "resync_join_coverage", explode)
    with pytest.raises(RuntimeError):
        upload_source(full_client, client_id)

    monkeypatch.undo()
    assert full_client.get(f"/clients/{client_id}/sources").json()["sources"] == []
    assert storage.list_keys(f"clients/{client_id}/sources/") == ()


# ---------------------------------------------------------------------------
# GET /clients/{id}/sources
# ---------------------------------------------------------------------------
def test_list_sources_returns_specs_and_profiles_by_id(full_client: TestClient) -> None:
    client_id = create_client_via_api(full_client)["client_id"]
    created = upload_source(full_client, client_id).json()
    response = full_client.get(f"/clients/{client_id}/sources")
    assert response.status_code == 200, response.text
    body = response.json()
    assert [row["source_id"] for row in body["sources"]] == [created["source_id"]]
    assert set(body["profiles"]) == {created["source_id"]}
    assert body["profiles"][created["source_id"]]["source_id"] == created["source_id"]


def test_list_sources_of_an_unknown_client_is_404(full_client: TestClient) -> None:
    response = full_client.get("/clients/c_does_not_exist_1/sources")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "CLIENT_NOT_FOUND"


# ---------------------------------------------------------------------------
# PATCH /clients/{id}/sources/{sid}
# ---------------------------------------------------------------------------
def test_patch_confirms_a_role_and_rejects_an_unknown_one(full_client: TestClient) -> None:
    client_id = create_client_via_api(full_client)["client_id"]
    source_id = upload_source(full_client, client_id).json()["source_id"]

    ok = full_client.patch(f"/clients/{client_id}/sources/{source_id}", json={"role": "entity"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["role"] == "entity"

    bad = full_client.patch(f"/clients/{client_id}/sources/{source_id}", json={"role": "not-a-role"})
    assert bad.status_code == 409
    assert bad.json()["detail"]["code"] == "ROLE_UNKNOWN"
    # the rejected PATCH must not have overwritten the confirmed role
    assert full_client.get(f"/clients/{client_id}/sources").json()["sources"][0]["role"] == "entity"


def test_patch_role_of_an_unknown_source_is_404(full_client: TestClient) -> None:
    client_id = create_client_via_api(full_client)["client_id"]
    response = full_client.patch(f"/clients/{client_id}/sources/src_missing", json={"role": "entity"})
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "SOURCE_NOT_FOUND"


def coverage_of(profile: dict[str, Any], column: str) -> float | None:
    """The stored coverage of one key candidate, by column name."""
    return next(k["coverage"] for k in profile["key_candidates"] if k["column"] == column)


def test_patch_confirms_the_role_on_the_profile_as_well_as_the_spec(full_client: TestClient) -> None:
    """The role is on both documents `GET /clients/{id}/sources` serves, and the mapping screen reads
    the profile - so a spec that says "entity" beside a profile that says "not settled yet" would
    show the user their own confirmed answer as still open."""
    client_id = create_client_via_api(full_client)["client_id"]
    source_id = upload_source(full_client, client_id).json()["source_id"]

    full_client.patch(f"/clients/{client_id}/sources/{source_id}", json={"role": "entity"})

    body = full_client.get(f"/clients/{client_id}/sources").json()
    assert body["sources"][0]["role"] == "entity"
    assert body["profiles"][source_id]["role"] == "entity"
    assert body["profiles"][source_id]["role_decided_by"] == "user"


def test_confirming_the_entity_role_measures_join_coverage_on_other_sources(full_client: TestClient) -> None:
    """The scenario the task calls out by name: an event source uploaded first, an entity source
    confirmed second, and the coverage badge on the *first* source's key candidates becomes real."""
    client_id = create_client_via_api(full_client)["client_id"]
    event_id = upload_source(full_client, client_id, EVENT_CSV.encode(), name="complaints.csv").json()[
        "source_id"
    ]
    entity_id = upload_source(full_client, client_id, name="customers.csv").json()["source_id"]
    before = full_client.get(f"/clients/{client_id}/sources").json()["profiles"]
    assert coverage_of(before[event_id], "customer_id") is None, "nothing to measure against yet"

    full_client.patch(f"/clients/{client_id}/sources/{entity_id}", json={"role": "entity"})

    profiles = full_client.get(f"/clients/{client_id}/sources").json()["profiles"]
    assert coverage_of(profiles[event_id], "customer_id") == EVENT_KEY_COVERAGE
    # a column that is not a join key is measured too, and honestly: no complaint date is a customer id
    assert coverage_of(profiles[event_id], "complaint_date") == 0.0
    # the entity source is never measured against itself
    for candidate in profiles[entity_id]["key_candidates"]:
        assert candidate["coverage"] is None


def test_uploading_against_a_confirmed_entity_measures_coverage_on_the_new_source(
    full_client: TestClient,
) -> None:
    """The other direction: the entity is already confirmed, so the source being uploaded now comes
    back with its coverage already measured - and the `201` body says what the list will say."""
    client_id = create_client_via_api(full_client)["client_id"]
    upload_source(full_client, client_id, name="customers.csv", role="entity")

    created = upload_source(full_client, client_id, EVENT_CSV.encode(), name="complaints.csv").json()
    assert coverage_of(created["profile"], "customer_id") == EVENT_KEY_COVERAGE

    listed = full_client.get(f"/clients/{client_id}/sources").json()["profiles"]
    assert listed[created["source_id"]] == created["profile"]


def test_deleting_the_entity_source_clears_the_coverage_it_was_measured_against(
    full_client: TestClient,
) -> None:
    """A measurement outlives neither the table it was measured against nor the badge's honesty:
    with the entity source gone there is nothing to have covered, so the number goes back to null
    and the UI renders an em dash."""
    client_id = create_client_via_api(full_client)["client_id"]
    entity_id = upload_source(full_client, client_id, name="customers.csv", role="entity").json()["source_id"]
    event_id = upload_source(full_client, client_id, EVENT_CSV.encode(), name="complaints.csv").json()[
        "source_id"
    ]
    profiles = full_client.get(f"/clients/{client_id}/sources").json()["profiles"]
    assert coverage_of(profiles[event_id], "customer_id") == EVENT_KEY_COVERAGE

    assert full_client.delete(f"/clients/{client_id}/sources/{entity_id}").status_code == 204

    profiles = full_client.get(f"/clients/{client_id}/sources").json()["profiles"]
    assert coverage_of(profiles[event_id], "customer_id") is None


def test_moving_the_role_away_from_entity_clears_the_coverage_it_measured(
    full_client: TestClient,
) -> None:
    """The same stale measurement, reached by re-labelling the entity table instead of deleting it."""
    client_id = create_client_via_api(full_client)["client_id"]
    entity_id = upload_source(full_client, client_id, name="customers.csv", role="entity").json()["source_id"]
    event_id = upload_source(full_client, client_id, EVENT_CSV.encode(), name="complaints.csv").json()[
        "source_id"
    ]
    assert (
        coverage_of(
            full_client.get(f"/clients/{client_id}/sources").json()["profiles"][event_id], "customer_id"
        )
        == EVENT_KEY_COVERAGE
    )

    moved = full_client.patch(f"/clients/{client_id}/sources/{entity_id}", json={"role": "complaints"})
    assert moved.status_code == 200, moved.text

    profiles = full_client.get(f"/clients/{client_id}/sources").json()["profiles"]
    assert coverage_of(profiles[event_id], "customer_id") is None


# ---------------------------------------------------------------------------
# DELETE /clients/{id}/sources/{sid}
# ---------------------------------------------------------------------------
def test_delete_source_removes_it_and_is_not_found_afterwards(
    full_client: TestClient, storage: LocalStorage
) -> None:
    client_id = create_client_via_api(full_client)["client_id"]
    source_id = upload_source(full_client, client_id).json()["source_id"]

    deleted = full_client.delete(f"/clients/{client_id}/sources/{source_id}")
    assert deleted.status_code == 204

    assert full_client.get(f"/clients/{client_id}/sources").json()["sources"] == []
    assert storage.list_keys(f"clients/{client_id}/sources/{source_id}/") == ()

    again = full_client.delete(f"/clients/{client_id}/sources/{source_id}")
    assert again.status_code == 404
    assert again.json()["detail"]["code"] == "SOURCE_NOT_FOUND"
