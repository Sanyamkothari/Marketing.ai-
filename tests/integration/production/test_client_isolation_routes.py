"""Client isolation inside one deployment: plan M51, the half that does not wait for decision P4.

The default deployment model is single-tenant - one deployment per client, in that client's own AWS
account - so the boundary between two *customers of Minfy* is the account, and nothing in this file
tests it. What this file tests is the boundary one deployment can still be asked to keep: Phase 2's
`ClientStore` models several clients inside one data directory (a Minfy laptop onboarding two
prospects, a client with two business units, a dev deployment used for demos), and a request that
names client A must never read, change or silently consume client B's sources, mappings, recipes,
datasets, runs or models.

Every path the M51 audit found is a test here, written to fail first, and then one of three things:

* **It holds** - the test passes, and `docs/CLIENT_ISOLATION.md` cites it as the evidence. Phase 2
  built most of the onboarding API this way on purpose: a cross-client id answers the same 404 as an
  unknown one (`api.routes.sources.load_source`, `api.routes.mappings.load_mapping`,
  `api.routes.datasets.load_spec`), and a run refuses another client's dataset with a 409.
* **It was a small, real gap and is closed** - `GET /runs?client_id=` (the filter
  `RunRecord.client_id` was added for and `GET /datasets?client_id=` already had).
* **It is structural** - the model registry, the global-id artefact routes, the storage key layout
  and the knowledge index have no client dimension at all. Closing any of them is a design change
  (a registry key, a key-layout migration, a principal bound to a client) whose shape depends on
  decision P4, so the test is `xfail(strict=True)`: it fails today, and the day someone closes the
  path the strict marker fails the suite until the marker is removed, so the document and the code
  cannot drift apart. `raises=AssertionError` keeps a broken fixture from hiding as an expected
  failure; a precondition that does not hold calls `pytest.fail` instead of asserting.

Everything runs through `create_app(data_dir=tmp_path)` - the served app, with the Phase 4b access
hook and audit middleware in place (sign-in off: the local operator) - on the same onboarding
fixtures `tests/integration/test_api_datasets.py` uses, built once per module because a real
dataset build takes seconds and every test here only reads what the fixture built (a refused write
changes nothing, which several tests assert).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.main import create_app
from engine.config import ColumnType, Metric, ProblemType, RunMode
from engine.contracts import FeatureSchema, FeatureSchemaColumn, ModelStatus, ModelVersion, RunRecord
from engine.registry import REGISTRY_FILENAME, LocalModelRegistry
from engine.storage import LocalStorage, run_key
from engine.utils.time import utc_now
from tests.fixtures.make_run import RunSpec, write_run
from tests.integration.test_api_clients import TELCO_CHURN, upload_source
from tests.integration.test_api_datasets import (
    ENTITY_CSV,
    _buildable_client,
    _create_spec,
    _poll_until_finished,
    _suggest_mapping,
)

pytestmark = pytest.mark.integration

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]

BUILD_TIMEOUT_S: Final[float] = 90.0
"""As `test_api_datasets.BUILD_TIMEOUT_S`: a timeout must mean "never finished", not "slow machine"."""

M51_STRUCTURAL: Final[str] = "M51, waits on decision P4 (docs/CLIENT_ISOLATION.md section 4)"
"""The prefix of every xfail reason here, so `pytest -rx` lists the open M51 items together."""


@dataclass(frozen=True)
class Onboarded:
    """One client of the fixture deployment, onboarded through the API up to a finished dataset."""

    client_id: str
    entity_source_id: str
    complaints_source_id: str
    entity_mapping_id: str
    complaints_mapping_id: str
    spec_id: str
    dataset_id: str
    run_id: str
    """A finished scoring run of this client's dataset (fabricated: `tests.fixtures.make_run`)."""


@dataclass(frozen=True)
class World:
    """One deployment's data directory holding two clients, and the served app over it."""

    app: FastAPI
    http: TestClient
    data_dir: Path
    a: Onboarded
    b: Onboarded

    @property
    def storage(self) -> LocalStorage:
        return LocalStorage(self.data_dir)


# ---------------------------------------------------------------------------
# The fixture deployment
# ---------------------------------------------------------------------------
def _built_dataset(http: TestClient, ctx: dict[str, str], spec_id: str) -> str:
    response = http.post(
        "/datasets", json={"client_id": ctx["client_id"], "spec_id": spec_id, "mode": "score"}
    )
    assert response.status_code == 202, response.text
    dataset_id: str = response.json()["dataset_id"]
    final = _poll_until_finished(http, dataset_id, timeout=BUILD_TIMEOUT_S)
    assert final["status"]["state"] == "done", final["status"]
    return dataset_id


def _run_of(storage: LocalStorage, *, client_id: str, dataset_id: str, seed: int, mode: RunMode) -> str:
    """A finished run whose `run.json` says it read `client_id`'s `dataset_id`.

    `make_run` fabricates a complete, contract-valid run directory in milliseconds; only its lineage
    is rewritten here, through `RunRecord.model_validate` so the "exactly one of upload_id and
    dataset_id" rule is checked rather than bypassed. What a run *read* is irrelevant to every test
    that uses one: they are about which client a stored run is attributed to and who can reach it.
    """
    run_id = write_run(storage, RunSpec(use_case_id=TELCO_CHURN, mode=mode, rows=60, seed=seed))
    key = run_key(run_id, "run.json")
    record = storage.read_model(key, RunRecord)
    rewritten = RunRecord.model_validate(
        {**record.model_dump(), "upload_id": None, "dataset_id": dataset_id, "client_id": client_id}
    )
    storage.write_model(key, rewritten)
    return run_id


def _onboard(http: TestClient, storage: LocalStorage, *, seed: int) -> Onboarded:
    ctx = _buildable_client(http)
    spec_id: str = _create_spec(http, ctx)["spec_id"]
    dataset_id = _built_dataset(http, ctx, spec_id)
    return Onboarded(
        client_id=ctx["client_id"],
        entity_source_id=ctx["entity_source_id"],
        complaints_source_id=ctx["complaints_source_id"],
        entity_mapping_id=ctx["entity_mapping_id"],
        complaints_mapping_id=ctx["complaints_mapping_id"],
        spec_id=spec_id,
        dataset_id=dataset_id,
        run_id=_run_of(
            storage, client_id=ctx["client_id"], dataset_id=dataset_id, seed=seed, mode=RunMode.SCORE
        ),
    )


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory, config_root: Path) -> Iterator[World]:
    data_dir = tmp_path_factory.mktemp("client_isolation") / "data"
    data_dir.mkdir()
    storage = LocalStorage(data_dir)
    app = create_app(config_root=config_root, data_dir=data_dir)
    with TestClient(app) as http:
        a = _onboard(http, storage, seed=1)
        b = _onboard(http, storage, seed=2)
        assert a.client_id != b.client_id
        yield World(app=app, http=http, data_dir=data_dir, a=a, b=b)


def _code(response: Any) -> str:
    code: str = response.json()["detail"]["code"]
    return code


def _given(condition: bool, what: str) -> None:
    """A precondition of an xfail test: `pytest.fail`, never `assert`, so it cannot pass as the xfail."""
    if not condition:
        pytest.fail(f"precondition does not hold: {what}")


def _seed_version(
    world: World, *, model_id: str, version: int, status: ModelStatus, trained_by: Onboarded
) -> ModelVersion:
    """A registered model version whose training run read `trained_by`'s dataset.

    Registered on a second `LocalModelRegistry` over the app's own `registry.db`, which is what
    `tests/integration/test_api_runs.py::seed_model` does and `LocalModelRegistry` documents as safe.
    The saved feature schema is one column the fixture datasets really carry, so a scoring request
    against either client's dataset passes validation and reaches the model resolution under test.
    """
    run_id = _run_of(
        world.storage,
        client_id=trained_by.client_id,
        dataset_id=trained_by.dataset_id,
        seed=100 + version,
        mode=RunMode.TRAIN,
    )
    model = ModelVersion(
        model_id=model_id,
        use_case_id=TELCO_CHURN,
        version=version,
        run_id=run_id,
        created_at=utc_now(),
        status=status,
        metric=Metric.ROC_AUC,
        metric_label="ROC-AUC",
        test_score=0.8,
        validation_score=0.8,
        model_display_name="Synthetic ensemble",
        schema_key=run_key(run_id, "schema.json"),
        run_config_key=run_key(run_id, "run_config.json"),
        predictor_key=run_key(run_id, "model"),
        engine_version="0.1.0",
        autogluon_version="1.6.3",
    )
    LocalModelRegistry(world.data_dir / REGISTRY_FILENAME).register(model)
    world.storage.write_model(
        model.schema_key,
        FeatureSchema(
            use_case_id=TELCO_CHURN,
            model_version_id=model_id,
            primary_key="entity_key",
            target="churn_next_60d",
            problem_type=ProblemType.BINARY_CLASSIFICATION,
            columns=(FeatureSchemaColumn(name="complaints_90d", inferred_type=ColumnType.INTEGER),),
            row_count_at_fit=1_000,
            created_at=utc_now(),
        ),
    )
    return model


# ===========================================================================
# 1. Paths that hold: a request naming client A cannot reach client B's records
# ===========================================================================
def test_a_clients_source_list_holds_only_its_own_sources(world: World) -> None:
    listed = world.http.get(f"/clients/{world.a.client_id}/sources")
    assert listed.status_code == 200, listed.text
    ids = {row["source_id"] for row in listed.json()["sources"]}
    assert ids == {world.a.entity_source_id, world.a.complaints_source_id}


@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
def test_another_clients_source_cannot_be_changed_or_deleted_through_this_client(
    world: World, method: str
) -> None:
    """The same 404 an unknown id gets: which ids another client holds is not this client's to learn."""
    path = f"/clients/{world.a.client_id}/sources/{world.b.complaints_source_id}"
    response = world.http.request(method, path, json={"role": "entity"} if method == "PATCH" else None)
    assert response.status_code == 404, response.text
    assert _code(response) == "SOURCE_NOT_FOUND"

    theirs = world.http.get(f"/clients/{world.b.client_id}/sources").json()["sources"]
    roles = {row["source_id"]: row["role"] for row in theirs}
    assert roles.get(world.b.complaints_source_id) == "complaints", "B's source was changed or removed"


def test_a_mapping_cannot_be_suggested_from_another_clients_source(world: World) -> None:
    response = world.http.post(
        f"/clients/{world.a.client_id}/mappings/suggest",
        json={"source_id": world.b.entity_source_id, "use_case": TELCO_CHURN},
    )
    assert response.status_code == 404, response.text
    assert _code(response) == "SOURCE_NOT_FOUND"


def test_another_clients_mapping_cannot_be_overwritten_through_this_client(world: World) -> None:
    """`save_mapping` upserts, so a save that did not check the stored owner would move B's mapping."""
    suggestion = _suggest_mapping(world.http, world.a.client_id, world.a.entity_source_id)
    response = world.http.put(
        f"/clients/{world.a.client_id}/mappings/{world.b.entity_mapping_id}",
        json={
            "client_id": world.a.client_id,
            "source_id": world.a.entity_source_id,
            "use_case": TELCO_CHURN,
            "role": suggestion["role"],
            "columns": suggestion["columns"],
        },
    )
    assert response.status_code == 404, response.text
    assert _code(response) == "MAPPING_NOT_FOUND"
    theirs = world.http.get(f"/clients/{world.b.client_id}/mappings").json()["mappings"]
    assert world.b.entity_mapping_id in {row["mapping_id"] for row in theirs}


@pytest.mark.parametrize("collection", ["mappings", "onboarding-specs"])
def test_a_clients_mapping_and_recipe_lists_hold_only_its_own(world: World, collection: str) -> None:
    listed = world.http.get(f"/clients/{world.a.client_id}/{collection}")
    assert listed.status_code == 200, listed.text
    rows = listed.json()["mappings" if collection == "mappings" else "specs"]
    assert rows, "the fixture saved at least one for every client"
    assert {row["client_id"] for row in rows} == {world.a.client_id}


@pytest.mark.parametrize(
    ("borrowed", "code"),
    [("entity_source_id", "SOURCE_NOT_FOUND"), ("complaints_mapping_id", "MAPPING_NOT_FOUND")],
)
def test_a_recipe_cannot_name_another_clients_source_or_mapping(
    world: World, borrowed: str, code: str
) -> None:
    body: dict[str, Any] = {
        "use_case": TELCO_CHURN,
        "entity_source_id": world.a.entity_source_id,
        "event_source_ids": [world.a.complaints_source_id],
        "mapping_ids": [world.a.entity_mapping_id, world.a.complaints_mapping_id],
        "feature_spec": {
            "features": [
                {"name": "complaints_90d", "role": "complaints", "function": "count", "window_days": 90}
            ]
        },
        "label_spec": None,
        "snapshot_spec": {"mode": "single"},
    }
    if borrowed == "entity_source_id":
        body["entity_source_id"] = world.b.entity_source_id
    else:
        body["mapping_ids"] = [world.a.entity_mapping_id, world.b.complaints_mapping_id]
    response = world.http.post(f"/clients/{world.a.client_id}/onboarding-specs", json=body)
    assert response.status_code == 404, response.text
    assert _code(response) == code


def test_another_clients_recipe_cannot_be_previewed_or_built_through_this_client(world: World) -> None:
    preview = world.http.post(f"/clients/{world.a.client_id}/onboarding-specs/{world.b.spec_id}/preview")
    assert preview.status_code == 404, preview.text
    assert _code(preview) == "ONBOARDING_SPEC_NOT_FOUND"

    build = world.http.post(
        "/datasets", json={"client_id": world.a.client_id, "spec_id": world.b.spec_id, "mode": "score"}
    )
    assert build.status_code == 404, build.text
    assert _code(build) == "ONBOARDING_SPEC_NOT_FOUND"


def test_a_dataset_list_narrowed_to_a_client_holds_only_its_datasets(world: World) -> None:
    listed = world.http.get("/datasets", params={"client_id": world.a.client_id})
    assert listed.status_code == 200, listed.text
    assert [row["dataset_id"] for row in listed.json()["datasets"]] == [world.a.dataset_id]


def test_a_run_for_one_client_cannot_read_another_clients_dataset(world: World) -> None:
    """The task's own example: B's dataset id in a run for A. Refused before anything is written."""
    before = set(world.storage.list_keys("runs/"))
    response = world.http.post(
        "/runs",
        json={
            "use_case": TELCO_CHURN,
            "mode": "score",
            "dataset_id": world.b.dataset_id,
            "client_id": world.a.client_id,
        },
    )
    assert response.status_code == 409, response.text
    assert _code(response) == "DATASET_CLIENT_MISMATCH"
    assert set(world.storage.list_keys("runs/")) == before, "a refused run left a run directory behind"


def test_a_clients_raw_sources_live_under_its_own_prefix(world: World) -> None:
    """Sources are the one artefact already keyed by client (`clients/<c>/sources/<s>/`)."""
    for onboarded in (world.a, world.b):
        for source_id in (onboarded.entity_source_id, onboarded.complaints_source_id):
            keys = [key for key in world.storage.list_keys("") if source_id in key]
            assert keys, f"no artefact of source {source_id}"
            assert all(key.startswith(f"clients/{onboarded.client_id}/sources/{source_id}/") for key in keys)


def test_the_served_api_keeps_no_completion_cache_to_share_between_clients() -> None:
    """A tripwire, not an isolation proof. `engine.generative.cache.CompletionCache` is keyed by
    prompt content only, with no client in the key or the directory, so wired into the served app it
    would be one cache for every client of a deployment (a hit reveals that another client sent the
    same prompt; an erasure has to find entries across clients). Today nothing outside the cache and
    budget modules constructs one - the `Meter` every route builds keeps `NullCache` - so there is
    nothing to share. Whoever wires it must namespace its root per client (`llm_cache/<client_id>/`)
    and extend this test; docs/CLIENT_ISOLATION.md section 3 records the requirement."""
    allowed = {Path("engine/generative/cache.py"), Path("engine/generative/budget.py")}
    constructing = sorted(
        str(path.relative_to(REPO_ROOT))
        for top in ("api", "engine", "scripts")
        for path in (REPO_ROOT / top).rglob("*.py")
        if path.relative_to(REPO_ROOT) not in allowed and re.search(r"\bCompletionCache\(", path.read_text())
    )
    assert constructing == []


# ===========================================================================
# 2. The small gap closed by M51: run history narrowed to one client
# ===========================================================================
def test_run_history_narrowed_to_a_client_holds_only_its_runs(world: World) -> None:
    """Failed before M51: `GET /runs` ignored `client_id` and listed B's run under A.

    Every listed run is checked rather than an exact set, because other tests in this module may
    start runs for A; B's run must never be among them.
    """
    listed = world.http.get("/runs", params={"client_id": world.a.client_id, "limit": 100})
    assert listed.status_code == 200, listed.text
    runs = listed.json()["runs"]
    assert world.a.run_id in {row["run_id"] for row in runs}
    assert world.b.run_id not in {row["run_id"] for row in runs}
    assert {row["client_id"] for row in runs} == {world.a.client_id}


def test_run_history_without_a_client_still_lists_every_run(world: World) -> None:
    """The filter is opt-in: the Phase 1 call, with no `client_id`, answers exactly as before."""
    listed = world.http.get("/runs", params={"limit": 100})
    assert listed.status_code == 200, listed.text
    assert {world.a.run_id, world.b.run_id} <= {row["run_id"] for row in listed.json()["runs"]}


# ===========================================================================
# 3. Structural paths: open M51 items, strict xfails
# ===========================================================================
GLOBAL_ID_READS: Final[tuple[tuple[str, str], ...]] = (
    ("dataset", "/datasets/{dataset_id}"),
    ("dataset", "/datasets/{dataset_id}/report"),
    ("dataset", "/datasets/{dataset_id}/sample"),
    ("dataset", "/datasets/{dataset_id}/features.sql"),
    ("run", "/runs/{run_id}"),
    ("run", "/runs/{run_id}/artefacts/run.json"),
    ("run", "/runs/{run_id}/scores.csv"),
)


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        f"{M51_STRUCTURAL}: datasets and runs are addressed by a deployment-wide id and these routes "
        "take no client, so any caller holding B's id reads B's artefact whatever client it works for"
    ),
)
@pytest.mark.parametrize(("kind", "template"), GLOBAL_ID_READS, ids=[path for _, path in GLOBAL_ID_READS])
def test_a_global_id_read_naming_another_client_is_refused(world: World, kind: str, template: str) -> None:
    """The read `GET /datasets?client_id=` and `GET /runs?client_id=` already scope, by id instead.

    The request names client A the only way these routes could be told a client today - the same
    `client_id` query the list routes take - and asks for B's artefact. Under single-tenant every
    principal of a deployment belongs to its one client, so this is by design there; under SaaS the
    client comes from the principal instead, and the cross-tenant suite of docs/CLIENT_ISOLATION.md
    section 6.6 replaces this test.
    """
    path = template.format(dataset_id=world.b.dataset_id, run_id=world.b.run_id)
    _given(world.http.get(path).status_code == 200, f"{path} answers for its own client")
    response = world.http.get(path, params={"client_id": world.a.client_id})
    assert response.status_code == 404, f"{kind} of client B served to a request naming client A"


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        f"{M51_STRUCTURAL}: a model version records no client and the champion is one per use case, "
        "so a champion trained on client B's data scores client A's dataset"
    ),
)
def test_scoring_a_clients_dataset_never_uses_a_champion_trained_on_another_clients_data(
    world: World,
) -> None:
    """The one structural path that mixes data rather than merely showing it.

    `engine.registry`'s champion rule is frozen (PARALLEL_WORK_PROTOCOL.md section 3) and keyed by
    use case; `resolve_model_version` takes no client. A deployment holding two clients therefore
    shares one champion between them, and the scoring run below would start on B's model - its
    predictions, its drift baseline, its explanations - over A's customers. The fix is a registry
    keyed by (client, use case), or refusing a champion whose training run's client differs.
    """
    champion = _seed_version(
        world, model_id="m_isolation_champion_b", version=1, status=ModelStatus.CHAMPION, trained_by=world.b
    )
    trained_on = world.storage.read_model(run_key(champion.run_id, "run.json"), RunRecord)
    _given(trained_on.client_id == world.b.client_id, "the champion's training run belongs to client B")

    response = world.http.post(
        "/runs",
        json={
            "use_case": TELCO_CHURN,
            "mode": "score",
            "dataset_id": world.a.dataset_id,
            "client_id": world.a.client_id,
        },
    )
    assert response.status_code != 202, "a scoring run for client A started on client B's champion"


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=f"{M51_STRUCTURAL}: `ModelVersion` has no client and `GET /models` takes no client filter",
)
def test_model_versions_listed_for_a_client_exclude_another_clients(world: World) -> None:
    model = _seed_version(
        world, model_id="m_isolation_candidate_b", version=2, status=ModelStatus.CANDIDATE, trained_by=world.b
    )
    _given(
        model.model_id
        in {row["version"]["model_id"] for row in world.http.get("/models").json()["versions"]},
        "the seeded version is listed at all",
    )
    listed = world.http.get("/models", params={"use_case": TELCO_CHURN, "client_id": world.a.client_id})
    assert listed.status_code == 200, listed.text
    assert model.model_id not in {row["version"]["model_id"] for row in listed.json()["versions"]}


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        f"{M51_STRUCTURAL}: only `clients/<c>/sources/` is keyed by client; `datasets/`, `runs/`, "
        "`uploads/`, `models/`, `indexes/` and `llm_cache/` are not, so no S3 prefix policy, per-client "
        "lifecycle rule or per-client KMS key can be written against them"
    ),
)
def test_every_artefact_of_a_client_lives_under_its_prefix(world: World) -> None:
    keys = world.storage.list_keys("")
    for onboarded in (world.a, world.b):
        owned = [key for key in keys if onboarded.dataset_id in key or onboarded.run_id in key]
        _given(bool(owned), f"client {onboarded.client_id} has dataset and run artefacts")
        outside = [key for key in owned if not key.startswith(f"clients/{onboarded.client_id}/")]
        assert outside == [], f"{len(outside)} artefacts of {onboarded.client_id} outside its prefix"


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        f"{M51_STRUCTURAL}: `DocIndexManifest.client_id` exists but `POST /use-cases/{{id}}/indexes` "
        "cannot set it, and the champion index is one per use case - every client's assistant answers "
        "from the same documents"
    ),
)
def test_a_knowledge_index_can_be_built_for_one_client(world: World) -> None:
    schema = world.app.openapi()
    body = schema["paths"]["/use-cases/{use_case_id}/indexes"]["post"]["requestBody"]
    reference: str = body["content"]["multipart/form-data"]["schema"]["$ref"]
    fields = schema["components"]["schemas"][reference.rsplit("/", 1)[-1]]["properties"]
    _given("documents" in fields, "the index-build form is the one this test reads")
    assert "client_id" in fields


def test_a_source_upload_for_an_unknown_client_is_refused(world: World) -> None:
    """Phase 1's upload path is the deployment's single-client path by design, not an M51 item.

    `POST /uploads` has no client and a run of an upload records none (`RunRecord.client_id` is
    null); the consent gate falls back to `Settings.client_id`. Under single-tenant that is the one
    client the deployment serves. What *is* client-scoped is Phase 2's source upload, which this
    checks still refuses an unknown client - the onboarding entry point cannot create an orphan.
    """
    response = upload_source(
        world.http, "c_nobody_1", ENTITY_CSV.encode(), name="customers.csv", role="entity"
    )
    assert response.status_code == 404, response.text
    assert _code(response) == "CLIENT_NOT_FOUND"
