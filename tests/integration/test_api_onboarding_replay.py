"""Score mode's API, end to end through `TestClient` on the raw fixture tables (Plan A M35).

The browser journey (`test_onboarding_acceptance.py`) walks the happy path at a test size and needs
a browser and AutoGluon; this module holds the same three endpoints to their contracts in seconds,
including the paths a journey never takes:

* `POST /clients/default` - the header's "Demo" client, created once and only once;
* `POST /clients/{id}/onboarding-specs/{sid}/replay` - this month's files through last month's
  recipe: nothing to review when the columns are the same, the mapping step reopened for exactly
  the file that lost a column (and closed again by the user's saved answer), a missing table named,
  and the recipe's own files refused;
* `GET /datasets/{id}/lineage` - where a built dataset came from, card by card.

Every table is `tests/fixtures/raw/make_raw.py`'s, and every recipe is built the way the screens
build one: each file's first-ranked role confirmed, each suggested mapping saved as suggested, the
use case's suggested features and label kept.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from tests.fixtures.raw.make_raw import RawTables, make_next_month, make_raw

pytestmark = pytest.mark.integration

USE_CASE = "telco-churn"
CUSTOMERS = 80
USAGE_ROWS = 400
TABLES = ("customers", "bills", "complaints", "activity")
BUILD_TIMEOUT_S = 120.0
MAPPING_FIELDS = (
    "client_id",
    "source_id",
    "use_case",
    "role",
    "columns",
    "unmapped_source",
    "missing_required",
    "value_maps",
)


@pytest.fixture(scope="module")
def raw(tmp_path_factory: pytest.TempPathFactory) -> RawTables:
    return make_raw(tmp_path_factory.mktemp("raw"), customers=CUSTOMERS, usage_rows=USAGE_ROWS)


@pytest.fixture(scope="module")
def next_month(raw: RawTables, tmp_path_factory: pytest.TempPathFactory) -> RawTables:
    return make_next_month(raw, tmp_path_factory.mktemp("next_month"))


@pytest.fixture(scope="module")
def client(config_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    """One app for the module; every test that uploads makes a client of its own, so no test's
    files count against another's `max_sources`."""
    data_dir = tmp_path_factory.mktemp("data")
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def recipe(client: TestClient, raw: RawTables) -> tuple[str, str, list[str]]:
    """One onboarded client, read and never written by the tests that only check a refusal."""
    client_id = new_client(client, "Refusal Telecom")
    spec_id, source_ids = onboard(client, client_id, raw)
    return client_id, spec_id, source_ids


def ok(response: Any, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json()


def upload(client: TestClient, client_id: str, path: Path, payload: bytes | None = None) -> str:
    body = payload if payload is not None else path.read_bytes()
    created = ok(
        client.post(f"/clients/{client_id}/sources", files={"file": (path.name, body, "text/csv")}), 201
    )
    source_id: str = created["source_id"]
    return source_id


def onboard(client: TestClient, client_id: str, tables: RawTables) -> tuple[str, list[str]]:
    """Every table uploaded, its proposed role confirmed and its suggested mapping saved; the
    use case's suggested features and label kept. Returns the saved recipe and its file ids."""
    source_ids = [upload(client, client_id, getattr(tables, name)) for name in TABLES]
    for source_id in source_ids:
        profile = ok(client.get(f"/clients/{client_id}/sources"))["profiles"][source_id]
        role = profile["role_candidates"][0]["role"]
        ok(client.patch(f"/clients/{client_id}/sources/{source_id}", json={"role": role}))
    roles = {s["source_id"]: s["role"] for s in ok(client.get(f"/clients/{client_id}/sources"))["sources"]}
    mapping_ids = []
    for source_id in source_ids:
        suggested = ok(
            client.post(
                f"/clients/{client_id}/mappings/suggest", json={"source_id": source_id, "use_case": USE_CASE}
            )
        )
        body = {field: suggested[field] for field in MAPPING_FIELDS}
        ok(client.put(f"/clients/{client_id}/mappings/{suggested['mapping_id']}", json=body))
        mapping_ids.append(suggested["mapping_id"])
    schema = ok(client.get(f"/use-cases/{USE_CASE}/standard-schema"))
    entity = next(source_id for source_id, role in roles.items() if role == "entity")
    spec = ok(
        client.post(
            f"/clients/{client_id}/onboarding-specs",
            json={
                "use_case": USE_CASE,
                "entity_source_id": entity,
                "event_source_ids": [source_id for source_id in source_ids if source_id != entity],
                "mapping_ids": sorted(mapping_ids),
                "feature_spec": {
                    "features": [f for f in schema["suggested_features"] if f["role"] in set(roles.values())]
                },
                "label_spec": schema["label"],
                "snapshot_spec": {},
            },
        ),
        201,
    )
    spec_id: str = spec["spec_id"]
    return spec_id, source_ids


def build(client: TestClient, client_id: str, spec_id: str, mode: str) -> dict[str, Any]:
    created = ok(
        client.post("/datasets", json={"client_id": client_id, "spec_id": spec_id, "mode": mode}), 202
    )
    deadline = time.monotonic() + BUILD_TIMEOUT_S
    while time.monotonic() < deadline:
        dataset: dict[str, Any] = ok(client.get(f"/datasets/{created['dataset_id']}"))
        if dataset["status"]["state"] in ("done", "failed", "cancelled"):
            return dataset
        time.sleep(0.2)
    pytest.fail(f"the {mode} build of {spec_id} never finished")


def replay(
    client: TestClient,
    client_id: str,
    spec_id: str,
    source_ids: list[str],
    mapping_ids: list[str] | None = None,
) -> Any:
    return client.post(
        f"/clients/{client_id}/onboarding-specs/{spec_id}/replay",
        json={"source_ids": source_ids, "mapping_ids": mapping_ids or []},
    )


def new_client(client: TestClient, name: str = "Replay Telecom") -> str:
    client_id: str = ok(client.post("/clients", json={"name": name, "industry": "telecom"}), 201)["client_id"]
    return client_id


# ---------------------------------------------------------------------------
# POST /clients/default
# ---------------------------------------------------------------------------
def test_the_default_client_is_demo_and_is_created_only_once(client: TestClient) -> None:
    first = ok(client.post("/clients/default"))
    second = ok(client.post("/clients/default"))
    assert first == second
    assert first["name"] == "Demo"
    assert first["industry"] == "telecom"
    listed = [row["client_id"] for row in ok(client.get("/clients"))["clients"]]
    assert listed.count(first["client_id"]) == 1


# ---------------------------------------------------------------------------
# POST /clients/{id}/onboarding-specs/{sid}/replay
# ---------------------------------------------------------------------------
def test_next_months_tables_replay_the_recipe_with_nothing_to_review(
    client: TestClient, raw: RawTables, next_month: RawTables
) -> None:
    client_id = new_client(client)
    spec_id, old_ids = onboard(client, client_id, raw)
    new_ids = [upload(client, client_id, getattr(next_month, name)) for name in TABLES]

    answer = ok(replay(client, client_id, spec_id, new_ids))

    assert answer["spec_id"] and answer["spec_id"] != spec_id
    assert not answer["unmatched"] and not answer["unused_source_ids"]
    assert sorted(entry["source_id"] for entry in answer["sources"]) == sorted(new_ids)
    assert sorted(entry["replaces_source_id"] for entry in answer["sources"]) == sorted(old_ids)
    assert all(not entry["missing_columns"] for entry in answer["sources"])
    assert not [check for check in answer["checks"] if check["severity"] == "error"]

    # The recipe is last month's, re-pointed: the same features, label and snapshot rule.
    specs = {s["spec_id"]: s for s in ok(client.get(f"/clients/{client_id}/onboarding-specs"))["specs"]}
    old, new = specs[spec_id], specs[answer["spec_id"]]
    for part in ("feature_spec", "label_spec", "snapshot_spec"):
        assert new[part] == old[part], part
    assert sorted([new["entity_source_id"], *new["event_source_ids"]]) == sorted(new_ids)

    # Each new file carries the role the recipe gave the file it replaces.
    roles = {s["source_id"]: s["role"] for s in ok(client.get(f"/clients/{client_id}/sources"))["sources"]}
    for entry in answer["sources"]:
        assert roles[entry["source_id"]] == roles[entry["replaces_source_id"]] == entry["role"]


def test_the_replayed_recipe_builds_a_scoring_dataset_whose_lineage_names_this_months_files(
    client: TestClient, raw: RawTables, next_month: RawTables
) -> None:
    client_id = new_client(client)
    spec_id, _ = onboard(client, client_id, raw)
    new_ids = [upload(client, client_id, getattr(next_month, name)) for name in TABLES]
    replayed = ok(replay(client, client_id, spec_id, new_ids))["spec_id"]

    dataset = build(client, client_id, replayed, "score")

    assert dataset["status"]["state"] == "done", dataset["status"]
    manifest = dataset["manifest"]
    assert manifest["target"] is None
    assert manifest["primary_key"] == ["entity_key", "snapshot_date"]
    assert len(manifest["snapshot_dates"]) == 1
    assert manifest["n_entities"] == CUSTOMERS

    lineage = ok(client.get(f"/datasets/{manifest['dataset_id']}/lineage"))

    assert sorted(node["id"] for node in lineage["sources"]) == sorted(new_ids)
    assert sorted(node["id"] for node in lineage["mappings"]) == sorted(manifest["mapping_hashes"])
    assert lineage["spec"]["id"] == replayed
    assert lineage["dataset"]["id"] == manifest["dataset_id"]
    assert lineage["dataset"]["parents"] == [replayed]
    assert all(node["detail"] for node in (*lineage["sources"], *lineage["mappings"]))


def test_a_file_that_lost_a_column_reopens_only_its_own_mapping(
    client: TestClient, raw: RawTables, next_month: RawTables
) -> None:
    client_id = new_client(client)
    spec_id, _ = onboard(client, client_id, raw)
    complaints = pd.read_csv(next_month.complaints, dtype=str, keep_default_na=False)
    renamed = complaints.rename(columns={"SEVERITY": "SEV_LEVEL"}).to_csv(index=False).encode()
    new_ids = [
        upload(client, client_id, getattr(next_month, name), renamed if name == "complaints" else None)
        for name in TABLES
    ]

    answer = ok(replay(client, client_id, spec_id, new_ids))

    assert answer["spec_id"] is None
    reopened = [entry for entry in answer["sources"] if entry["missing_columns"]]
    assert [(entry["file_name"], entry["missing_columns"]) for entry in reopened] == [
        ("complaints.csv", ["SEVERITY"])
    ]
    assert "SEVERITY" in reopened[0]["message"]

    # The reopened file's mapping is saved without the lost column, for the mapping step to edit.
    mappings = {m["mapping_id"]: m for m in ok(client.get(f"/clients/{client_id}/mappings"))["mappings"]}
    saved = mappings[reopened[0]["mapping_id"]]
    assert "SEVERITY" not in [column["source"] for column in saved["columns"]]
    assert "SEV_LEVEL" in saved["unmapped_source"]

    # The user points the renamed column at what SEVERITY meant, saves, and the replay stands on it.
    fixed = {field: saved[field] for field in MAPPING_FIELDS}
    fixed["columns"] = [
        *saved["columns"],
        {
            "source": "SEV_LEVEL",
            "standard": "severity",
            "confidence": 1.0,
            "decided_by": "user",
            "transform": None,
        },
    ]
    fixed["unmapped_source"] = [name for name in saved["unmapped_source"] if name != "SEV_LEVEL"]
    ok(client.put(f"/clients/{client_id}/mappings/{saved['mapping_id']}", json=fixed))
    settled = ok(replay(client, client_id, spec_id, new_ids, [saved["mapping_id"]]))

    assert settled["spec_id"]
    entry = next(e for e in settled["sources"] if e["mapping_id"] == saved["mapping_id"])
    assert entry["settled_by_user"] and not entry["missing_columns"]

    # A "settled" mapping must be for one of the files being replayed - last month's is not.
    old_mapping = next(m for m in mappings.values() if m["source_id"] not in new_ids)["mapping_id"]
    refused = replay(client, client_id, spec_id, new_ids, [old_mapping])
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "MAPPING_NOT_FOR_SPEC"


def test_a_table_the_recipe_reads_but_this_month_lacks_is_named(
    client: TestClient, raw: RawTables, next_month: RawTables
) -> None:
    client_id = new_client(client)
    spec_id, _ = onboard(client, client_id, raw)
    new_ids = [upload(client, client_id, getattr(next_month, name)) for name in TABLES if name != "activity"]

    answer = ok(replay(client, client_id, spec_id, new_ids))

    assert answer["spec_id"] is None
    assert [(u["file_name"], u["role"]) for u in answer["unmatched"]] == [("activity.csv", "activity")]
    assert "activity.csv" in answer["unmatched"][0]["message"]


def test_replaying_onto_the_recipes_own_files_is_refused(
    client: TestClient, recipe: tuple[str, str, list[str]]
) -> None:
    client_id, spec_id, old_ids = recipe
    response = replay(client, client_id, spec_id, old_ids)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "REPLAY_SOURCE_ALREADY_IN_RECIPE"


def test_replaying_a_recipe_of_another_client_is_not_found(
    client: TestClient, recipe: tuple[str, str, list[str]]
) -> None:
    _, spec_id, old_ids = recipe
    stranger = new_client(client, "Stranger Telecom")
    response = replay(client, stranger, spec_id, old_ids)
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "ONBOARDING_SPEC_NOT_FOUND"


# ---------------------------------------------------------------------------
# GET /datasets/{id}/lineage
# ---------------------------------------------------------------------------
def test_the_lineage_of_an_unknown_dataset_is_not_found(client: TestClient) -> None:
    response = client.get("/datasets/ds_nope/lineage")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "DATASET_NOT_FOUND"
