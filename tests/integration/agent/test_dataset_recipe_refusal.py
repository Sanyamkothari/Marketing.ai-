"""A model trained on Guided-prepared data scores a built dataset prepared by its recipe (Plan L M155).

DEC-1006 prepares every file a recipe model scores with the model's own recipe. Until M155 only uploads were
prepared (`replay_for_scoring`), and a built dataset - `POST /runs` with `dataset_id`, or a scheduled score
firing - was refused with `RECIPE_DATASET_UNSUPPORTED` (DEC-1044). M155 closes that (DEC-1553, DEC-1554): the
recipe runs on the dataset's frame into `datasets/<id>/prepared_<identity>.parquet`, the run still names the
dataset, and its job reads the prepared rows. Every rule of the recipe still holds: another ID column is
`RECIPE_ROLES_MISMATCH`, and a recipe that cannot run on the dataset is a clear 409 (a firing failure with the
same code), never a 500 and never an unprepared score.

This file's first two tests asserted the refusal until M155; they now assert the replay (the change is
intentional and recorded in DEC-1553).
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from api.routes import runs
from api.routes.agent_recipes import write_derived_upload
from api.routes.uploads import load_upload
from engine.agent.contracts import (
    DATA_RECIPE_FILENAME,
    RECIPE_RECEIPT_FILENAME,
    DataRecipe,
    RecipeReceipt,
    RecipeStep,
    RecipeStepKind,
    recipe_hash,
)
from engine.config import ColumnType, Metric, ProblemType, RunMode, get_catalog, load_use_case, resolve_config
from engine.contracts import FeatureSchema, FeatureSchemaColumn, ModelStatus, ModelVersion, RunRecord
from engine.jobs import CancelToken
from engine.onboarding.datasets import DATASET_FRAME_FILENAME, dataset_key
from engine.onboarding.specs import DatasetManifest
from engine.registry import REGISTRY_FILENAME, LocalModelRegistry
from engine.runs import job_spec_key, read_job_spec
from engine.scheduling.firing import FiringError, start_dataset_run
from engine.storage import LocalStorage, run_key
from engine.utils.time import utc_now
from tests.fixtures.raw.make_raw import RawTables, make_raw
from tests.integration.test_api_onboarding_replay import build, new_client, onboard
from tests.integration.test_runs_from_dataset import USE_CASE, _build
from tests.unit.production.scheduling_support import RecordingJobs

pytestmark = pytest.mark.integration

KEY = "entity_key"
"""A built dataset's key column: the standard name the mapping gives the customer list's ID."""


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str]:
    root = tmp_path_factory.mktemp("dataset-recipe")
    _, dataset_id, report = _build(root)
    assert report.passed
    return root, dataset_id


def _steps(column: str = "region") -> tuple[RecipeStep, ...]:
    return (
        RecipeStep(
            order=1,
            kind=RecipeStepKind.MAP_VALUES,
            column=column,
            params={"mapping": {"North": "Northern"}},
        ),
    )


def _recipe(*, primary_key: str = KEY, column: str = "region") -> DataRecipe:
    steps = _steps(column)
    return DataRecipe(
        recipe_id="rec1",
        use_case_id=USE_CASE,
        source_fingerprint="f",
        steps=steps,
        recipe_hash=recipe_hash(steps),
        primary_key=primary_key,
        created_at=utc_now(),
    )


def _seed(data_dir: Path, run_id: str, model_id: str, *, recipe: DataRecipe | None) -> ModelVersion:
    version = ModelVersion(
        model_id=model_id,
        use_case_id=USE_CASE,
        version=1,
        run_id=run_id,
        created_at=utc_now(),
        status=ModelStatus.CHAMPION,
        metric=Metric.ROC_AUC,
        metric_label="ROC-AUC",
        test_score=0.8,
        validation_score=0.8,
        model_display_name="WeightedEnsemble_L2",
        schema_key=run_key(run_id, "schema.json"),
        run_config_key=run_key(run_id, "run_config.json"),
        predictor_key=run_key(run_id, "model"),
        engine_version="0.1.0",
        autogluon_version="1.6.3",
    )
    LocalModelRegistry(data_dir / REGISTRY_FILENAME).register(version)
    storage = LocalStorage(data_dir)
    storage.write_model(
        version.schema_key,
        FeatureSchema(
            use_case_id=USE_CASE,
            model_version_id=model_id,
            primary_key=KEY,
            target="churned",
            problem_type=ProblemType.BINARY_CLASSIFICATION,
            columns=(FeatureSchemaColumn(name="events_90d", inferred_type=ColumnType.INTEGER),),
            row_count_at_fit=1_000,
            created_at=utc_now(),
        ),
    )
    if recipe is not None:
        storage.write_model(run_key(run_id, DATA_RECIPE_FILENAME), recipe)
    return version


def _fresh_registry(data_dir: Path) -> None:
    if (data_dir / REGISTRY_FILENAME).exists():
        (data_dir / REGISTRY_FILENAME).unlink()


def _frame(storage: LocalStorage, key: str) -> pd.DataFrame:
    return pd.read_parquet(io.BytesIO(storage.read_bytes(key)))


def _post_score(root: Path, dataset_id: str) -> Any:
    app = create_app(data_dir=root / "data")
    app.state.jobs = RecordingJobs()
    return TestClient(app).post(
        "/runs", json={"use_case": USE_CASE, "mode": "score", "dataset_id": dataset_id}
    )


def _fire(root: Path, dataset_id: str, version: ModelVersion) -> RunRecord:
    storage = LocalStorage(root / "data")
    manifest = storage.read_model(dataset_key(dataset_id, "dataset_manifest.json"), DatasetManifest)
    return start_dataset_run(
        storage=storage,
        registry=LocalModelRegistry(root / "data" / REGISTRY_FILENAME),
        jobs=RecordingJobs(),
        resolved=resolve_config(USE_CASE, {}),
        catalog=get_catalog(None),
        manifest=manifest,
        mode=RunMode.SCORE,
        version=version,
        client_tag=None,
    )


def _assert_scored_prepared(storage: LocalStorage, run_id: str, dataset_id: str) -> None:
    record = storage.read_model(run_key(run_id, "run.json"), RunRecord)
    assert record.dataset_id == dataset_id and record.upload_id is None  # the lineage is the dataset's
    spec = read_job_spec(storage, job_spec_key(run_id))
    assert spec.upload_key.startswith(f"datasets/{dataset_id}/prepared_")
    built_rows = _frame(storage, dataset_key(dataset_id, DATASET_FRAME_FILENAME))
    prepared = _frame(storage, spec.upload_key)
    assert len(prepared) == len(built_rows)
    assert "North" in set(built_rows["region"]) and "North" not in set(prepared["region"])
    assert (prepared["region"] == "Northern").sum() == (built_rows["region"] == "North").sum()
    assert storage.read_model(run_key(run_id, DATA_RECIPE_FILENAME), DataRecipe).recipe_hash == recipe_hash(
        _steps()
    )
    receipt = storage.read_model(run_key(run_id, RECIPE_RECEIPT_FILENAME), RecipeReceipt)
    assert receipt.upload_id == dataset_id and receipt.rows_out == len(built_rows)


def test_post_runs_scores_a_dataset_prepared_by_the_models_recipe(built: tuple[Path, str]) -> None:
    root, dataset_id = built
    _fresh_registry(root / "data")
    _seed(root / "data", "r_guided", "m_guided", recipe=_recipe())
    response = _post_score(root, dataset_id)
    assert response.status_code == 202, response.text
    _assert_scored_prepared(LocalStorage(root / "data"), response.json()["run_id"], dataset_id)


def test_a_scheduled_score_firing_prepares_the_dataset_with_the_models_recipe(
    built: tuple[Path, str],
) -> None:
    root, dataset_id = built
    _fresh_registry(root / "data")
    version = _seed(root / "data", "r_guided", "m_guided", recipe=_recipe())
    record = _fire(root, dataset_id, version)
    _assert_scored_prepared(LocalStorage(root / "data"), record.run_id, dataset_id)


def test_the_dataset_itself_is_never_changed(built: tuple[Path, str]) -> None:
    root, dataset_id = built
    storage = LocalStorage(root / "data")
    names = ("dataset.parquet", "dataset_manifest.json", "sample.json")
    before = {name: storage.read_bytes(dataset_key(dataset_id, name)) for name in names}
    _fresh_registry(root / "data")
    _seed(root / "data", "r_guided", "m_guided", recipe=_recipe())
    assert _post_score(root, dataset_id).status_code == 202
    assert {name: storage.read_bytes(dataset_key(dataset_id, name)) for name in names} == before


def test_a_recipe_that_cannot_run_on_the_dataset_is_a_409_naming_the_column(built: tuple[Path, str]) -> None:
    root, dataset_id = built
    _fresh_registry(root / "data")
    _seed(root / "data", "r_guided", "m_guided", recipe=_recipe(column="city"))
    response = _post_score(root, dataset_id)
    assert response.status_code == 409, response.text
    (check,) = response.json()["validation"]["checks"]
    assert (check["code"], check["column"]) == ("RECIPE_COLUMN_MISSING", "city")


def test_a_firing_whose_recipe_cannot_run_fails_with_the_recipes_code(built: tuple[Path, str]) -> None:
    root, dataset_id = built
    _fresh_registry(root / "data")
    version = _seed(root / "data", "r_guided", "m_guided", recipe=_recipe(column="city"))
    with pytest.raises(FiringError) as caught:
        _fire(root, dataset_id, version)
    assert caught.value.code == "RECIPE_COLUMN_MISSING"


def test_a_recipe_for_another_id_column_is_refused_before_anything_is_prepared(
    built: tuple[Path, str],
) -> None:
    root, dataset_id = built
    _fresh_registry(root / "data")
    version = _seed(root / "data", "r_guided", "m_guided", recipe=_recipe(primary_key="customer_id"))
    storage = LocalStorage(root / "data")
    prepared_before = [key for key in storage.list_keys(f"datasets/{dataset_id}/") if "/prepared_" in key]
    for key in prepared_before:
        storage.delete(key)
    response = _post_score(root, dataset_id)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "RECIPE_ROLES_MISMATCH"
    with pytest.raises(FiringError) as caught:
        _fire(root, dataset_id, version)
    assert caught.value.code == "RECIPE_ROLES_MISMATCH"
    assert not [key for key in storage.list_keys(f"datasets/{dataset_id}/") if "/prepared_" in key]


def test_a_model_without_a_recipe_scores_the_dataset_as_built(built: tuple[Path, str]) -> None:
    root, dataset_id = built
    _fresh_registry(root / "data")
    _seed(root / "data", "r_plain", "m_plain", recipe=None)
    response = _post_score(root, dataset_id)
    assert response.status_code == 202, response.text
    storage = LocalStorage(root / "data")
    spec = read_job_spec(storage, job_spec_key(response.json()["run_id"]))
    assert spec.upload_key == dataset_key(dataset_id, DATASET_FRAME_FILENAME)
    assert not storage.exists(run_key(response.json()["run_id"], DATA_RECIPE_FILENAME))


# ---------------------------------------------------------------------------
# End to end through the APIs: onboarding builds, Guided setup prepares, training, scoring next month
# ---------------------------------------------------------------------------
@pytest.fixture
def api(
    config_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, Path]]:
    """Real onboarding, builds, recipes and validation; a run's job does nothing, so nothing trains."""

    def idle(*_args: Any, **_kwargs: Any) -> Any:
        def job(cancel: CancelToken) -> None:
            del cancel

        return job

    monkeypatch.setattr(runs, "build_job_fn", idle)
    data_dir = tmp_path / "data"
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as client:
        yield client, data_dir


def test_a_dataset_built_through_onboarding_is_scored_by_a_model_trained_on_a_guided_upload(
    api: tuple[TestClient, Path], tmp_path: Path, config_root: Path
) -> None:
    client, data_dir = api
    storage = LocalStorage(data_dir)
    config = load_use_case(USE_CASE, config_root)
    target = str(config.target.column)

    # This month's tables, onboarded through the API with one row per customer, built for scoring.
    raw: RawTables = make_raw(tmp_path / "raw", customers=80, usage_rows=400)
    client_id = new_client(client, "Doctor Telecom")
    spec_id, _ = onboard(client, client_id, raw, snapshot_spec={"mode": "single"})
    scoring = build(client, client_id, spec_id, "score")
    assert scoring["status"]["state"] == "done", scoring
    dataset_id = scoring["manifest"]["dataset_id"]
    (key,) = scoring["manifest"]["primary_key"]
    as_built = _frame(storage, dataset_key(dataset_id, DATASET_FRAME_FILENAME))
    first_region = str(as_built["region"].dropna().iloc[0])

    # A history the person sent as a file - the same columns and the outcome - prepared by Guided setup.
    rng = np.random.default_rng(155)
    history = as_built.sample(n=1_500, replace=True, random_state=155).reset_index(drop=True)
    history[key] = np.arange(5_000_000, 5_001_500)
    history[target] = rng.integers(0, 2, size=len(history))
    sent = client.post(
        "/uploads",
        files={"file": ("history.csv", history.to_csv(index=False).encode(), "text/csv")},
        data={"use_case": USE_CASE, "mode": "train"},
    )
    assert sent.status_code == 201, sent.text
    steps = (
        RecipeStep(
            order=1,
            kind=RecipeStepKind.MAP_VALUES,
            column="region",
            params={"mapping": {first_region: f"{first_region} zone"}},
        ),
    )
    recipe = DataRecipe(
        recipe_id="rec_guided",
        use_case_id=USE_CASE,
        source_fingerprint="f",
        steps=steps,
        recipe_hash=recipe_hash(steps),
        primary_key=key,
        target=target,
        created_at=utc_now(),
    )
    derived = write_derived_upload(storage, config, load_upload(storage, sent.json()["upload_id"]), recipe)
    trained = client.post(
        "/runs",
        json={
            "use_case": USE_CASE,
            "mode": "train",
            "upload_id": derived.record.upload_id,
            "primary_key": key,
            "target": target,
        },
    )
    assert trained.status_code == 202, trained.text
    run_id = trained.json()["run_id"]
    assert storage.exists(run_key(run_id, DATA_RECIPE_FILENAME))  # the training run carries its recipe
    # Training itself is AutoGluon's (minutes); the version it would register is recorded here instead.
    profile = {column.name: column for column in derived.profile.columns}
    version = ModelVersion(
        model_id="m_guided_api",
        use_case_id=USE_CASE,
        version=1,
        run_id=run_id,
        created_at=utc_now(),
        status=ModelStatus.CHAMPION,
        metric=Metric.ROC_AUC,
        metric_label="ROC-AUC",
        test_score=0.8,
        validation_score=0.8,
        model_display_name="WeightedEnsemble_L2",
        schema_key=run_key(run_id, "schema.json"),
        run_config_key=run_key(run_id, "run_config.json"),
        predictor_key=run_key(run_id, "model"),
        engine_version="0.1.0",
        autogluon_version="1.6.3",
    )
    storage.write_model(
        version.schema_key,
        FeatureSchema(
            use_case_id=USE_CASE,
            model_version_id=version.model_id,
            primary_key=key,
            target=target,
            problem_type=ProblemType.BINARY_CLASSIFICATION,
            columns=tuple(
                FeatureSchemaColumn(name=name, inferred_type=column.inferred_type)
                for name, column in profile.items()
                if name not in {key, target}
            ),
            row_count_at_fit=derived.record.row_count,
            created_at=utc_now(),
        ),
    )
    LocalModelRegistry(data_dir / REGISTRY_FILENAME).register(version)

    # Scoring the built dataset with that model: prepared by its recipe, still traced to the dataset.
    scored = client.post(
        "/runs",
        json={"use_case": USE_CASE, "mode": "score", "dataset_id": dataset_id, "client_id": client_id},
    )
    assert scored.status_code == 202, scored.text
    record = storage.read_model(run_key(scored.json()["run_id"], "run.json"), RunRecord)
    assert (record.dataset_id, record.client_id) == (dataset_id, client_id)
    assert record.model_version_id == version.model_id
    prepared = _frame(storage, read_job_spec(storage, job_spec_key(record.run_id)).upload_key)
    assert len(prepared) == len(as_built)
    changed = (prepared["region"] == f"{first_region} zone").sum()
    assert changed == (as_built["region"] == first_region).sum() > 0
    assert first_region not in set(prepared["region"])
