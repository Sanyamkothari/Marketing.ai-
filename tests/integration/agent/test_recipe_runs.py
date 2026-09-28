"""Recipes through uploads and runs (Plan G M72): a derived upload trains, and scoring replays the recipe."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from api.routes import runs
from api.routes.agent_recipes import write_derived_upload
from api.routes.uploads import load_upload, load_upload_profile
from engine.agent.contracts import (
    DATA_RECIPE_FILENAME,
    RECIPE_RECEIPT_FILENAME,
    DataRecipe,
    RecipeStep,
    RecipeStepKind,
    recipe_hash,
)
from engine.config import ColumnType, Metric, ProblemType, load_use_case
from engine.contracts import FeatureSchema, FeatureSchemaColumn, ModelStatus, ModelVersion, RunRecord
from engine.jobs import CancelToken
from engine.registry import REGISTRY_FILENAME, LocalModelRegistry
from engine.storage import LocalStorage, run_key
from engine.utils.time import utc_now
from tests.fixtures.make_data import GenerationSpec, generate

pytestmark = pytest.mark.integration

USE_CASE = "targeted-advertisement"
KEY = "customer_id"
TARGET = "converted_30d"


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def client(config_root: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Real ingest, recipes and validation; the job itself does nothing, so nothing trains."""

    def idle(*_args: Any, **_kwargs: Any) -> Any:
        def job(cancel: CancelToken) -> None:
            del cancel

        return job

    monkeypatch.setattr(runs, "build_job_fn", idle)
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as test_client:
        yield test_client


def _messy(variant: str = "clean", rows: int = 3_000) -> pd.DataFrame:
    frame = generate(GenerationSpec(use_case_id=USE_CASE, rows=rows, variant=variant))
    frame["ad_ctr_90d"] = [
        f"{value * 100:.1f}%" if pd.notna(value) else None for value in frame["ad_ctr_90d"]
    ]
    frame["region"] = [value.upper() if i % 3 == 0 else value for i, value in enumerate(frame["region"])]
    return frame


def _upload(client: TestClient, frame: pd.DataFrame, mode: str = "train") -> str:
    response = client.post(
        "/uploads",
        files={"file": ("history.csv", frame.to_csv(index=False).encode(), "text/csv")},
        data={"use_case": USE_CASE, "mode": mode},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["upload_id"])


def _recipe(frame: pd.DataFrame, *, target: str | None = TARGET) -> DataRecipe:
    merge = {value.upper(): value for value in frame["region"].dropna().unique() if value != value.upper()}
    steps = (
        RecipeStep(order=1, kind=RecipeStepKind.PARSE_NUMBER, column="ad_ctr_90d", params={"decimal": "."}),
        RecipeStep(
            order=2,
            kind=RecipeStepKind.NORMALISE_TEXT,
            column="region",
            params={"strip": True, "merge": merge},
        ),
    )
    return DataRecipe(
        recipe_id="rec1",
        use_case_id=USE_CASE,
        source_fingerprint="f",
        steps=steps,
        recipe_hash=recipe_hash(steps),
        primary_key=KEY,
        target=target,
        created_at=utc_now(),
    )


def _seed_model(data_dir: Path, recipe: DataRecipe | None) -> ModelVersion:
    run_id = "r_trained"
    version = ModelVersion(
        model_id="m_1",
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
            model_version_id=version.model_id,
            primary_key=KEY,
            target=TARGET,
            problem_type=ProblemType.BINARY_CLASSIFICATION,
            columns=(FeatureSchemaColumn(name="ad_ctr_90d", inferred_type=ColumnType.FLOAT),),
            row_count_at_fit=3_000,
            created_at=utc_now(),
        ),
    )
    if recipe is not None:
        storage.write_model(run_key(run_id, DATA_RECIPE_FILENAME), recipe)
    return version


def test_a_derived_upload_is_prepared_and_the_original_is_untouched(
    client: TestClient, data_dir: Path
) -> None:
    frame = _messy()
    upload_id = _upload(client, frame)
    storage = LocalStorage(data_dir)
    original = load_upload(storage, upload_id)
    before = {key: storage.read_bytes(key) for key in storage.list_keys(f"uploads/{upload_id}/")}
    config = load_use_case(USE_CASE)

    derived = write_derived_upload(storage, config, original, _recipe(frame))

    assert {key: storage.read_bytes(key) for key in storage.list_keys(f"uploads/{upload_id}/")} == before
    assert derived.record.upload_id != upload_id
    assert derived.record.mode is original.mode
    assert derived.record.row_count == original.row_count == 3_000
    columns = {
        column.name: column for column in load_upload_profile(storage, derived.record.upload_id).columns
    }
    assert columns["ad_ctr_90d"].inferred_type is ColumnType.FLOAT
    assert (
        columns["region"].distinct_count
        < {c.name: c for c in load_upload_profile(storage, upload_id).columns}["region"].distinct_count
    )
    assert storage.exists(f"uploads/{derived.record.upload_id}/{DATA_RECIPE_FILENAME}")
    assert storage.exists(f"uploads/{derived.record.upload_id}/{RECIPE_RECEIPT_FILENAME}")
    parse = derived.receipt.steps[0]
    assert (parse.changed, parse.failed) == (int(frame["ad_ctr_90d"].notna().sum()), 0)  # empties stay empty


def test_a_run_from_a_derived_upload_carries_its_recipe(client: TestClient, data_dir: Path) -> None:
    frame = _messy()
    storage = LocalStorage(data_dir)
    original = load_upload(storage, _upload(client, frame))
    derived = write_derived_upload(storage, load_use_case(USE_CASE), original, _recipe(frame))
    response = client.post(
        "/runs",
        json={
            "use_case": USE_CASE,
            "mode": "train",
            "upload_id": derived.record.upload_id,
            "primary_key": KEY,
            "target": TARGET,
        },
    )
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    saved = storage.read_model(run_key(run_id, DATA_RECIPE_FILENAME), DataRecipe)
    assert saved.recipe_hash == _recipe(frame).recipe_hash


def test_a_plain_run_carries_no_recipe(client: TestClient, data_dir: Path) -> None:
    upload_id = _upload(client, generate(GenerationSpec(use_case_id=USE_CASE, rows=3_000)))
    response = client.post(
        "/runs",
        json={
            "use_case": USE_CASE,
            "mode": "train",
            "upload_id": upload_id,
            "primary_key": KEY,
            "target": TARGET,
        },
    )
    assert response.status_code == 202, response.text
    assert not LocalStorage(data_dir).exists(run_key(response.json()["run_id"], DATA_RECIPE_FILENAME))


def _score(client: TestClient, upload_id: str) -> Any:
    return client.post(
        "/runs", json={"use_case": USE_CASE, "mode": "score", "upload_id": upload_id, "primary_key": KEY}
    )


def test_scoring_replays_the_models_recipe(client: TestClient, data_dir: Path) -> None:
    training = _messy()
    _seed_model(data_dir, _recipe(training, target=None))
    scoring = _messy("scoring", rows=800)
    upload_id = _upload(client, scoring, mode="score")
    response = _score(client, upload_id)
    assert response.status_code == 202, response.text
    storage = LocalStorage(data_dir)
    record = storage.read_model(run_key(response.json()["run_id"], "run.json"), RunRecord)
    assert record.upload_id != upload_id  # the run reads the prepared copy
    prepared = load_upload_profile(storage, str(record.upload_id))
    assert {c.name: c for c in prepared.columns}["ad_ctr_90d"].inferred_type is ColumnType.FLOAT
    assert prepared.row_count == 800


def test_a_scoring_file_missing_a_recipe_column_is_a_409_naming_it(
    client: TestClient, data_dir: Path
) -> None:
    _seed_model(data_dir, _recipe(_messy(), target=None))
    scoring = _messy("scoring", rows=800).rename(columns={"ad_ctr_90d": "ctr_90d"})
    response = _score(client, _upload(client, scoring, mode="score"))
    assert response.status_code == 409, response.text
    (check,) = response.json()["validation"]["checks"]
    assert (check["code"], check["column"]) == ("RECIPE_COLUMN_MISSING", "ad_ctr_90d")


def test_a_scoring_file_without_a_hidden_column_scores(client: TestClient, data_dir: Path) -> None:
    """A column Guided setup hid (here a post-outcome score) need not be in the scoring file: the
    drop step is skipped and says so in the receipt, instead of a 409 asking for the leaky column."""
    recipe = _recipe(_messy(), target=None)
    steps = (
        *recipe.steps,
        RecipeStep(order=3, kind=RecipeStepKind.DROP_COLUMN, column="campaign_result_score"),
    )
    _seed_model(data_dir, recipe.model_copy(update={"steps": steps, "recipe_hash": recipe_hash(steps)}))
    scoring = _messy("scoring", rows=800)
    assert "campaign_result_score" not in scoring.columns
    response = _score(client, _upload(client, scoring, mode="score"))
    assert response.status_code == 202, response.text


def test_a_scoring_file_whose_values_cannot_be_read_is_a_409(client: TestClient, data_dir: Path) -> None:
    _seed_model(data_dir, _recipe(_messy(), target=None))
    scoring = _messy("scoring", rows=800)
    scoring.loc[: len(scoring) // 5, "ad_ctr_90d"] = "unknown"
    response = _score(client, _upload(client, scoring, mode="score"))
    assert response.status_code == 409, response.text
    (check,) = response.json()["validation"]["checks"]
    assert check["code"] == "RECIPE_VALUES_UNCONVERTED"


def test_a_model_without_a_recipe_scores_the_file_as_sent(client: TestClient, data_dir: Path) -> None:
    _seed_model(data_dir, None)
    scoring = generate(GenerationSpec(use_case_id=USE_CASE, rows=800, variant="scoring"))
    upload_id = _upload(client, scoring, mode="score")
    response = _score(client, upload_id)
    assert response.status_code == 202, response.text
    record = LocalStorage(data_dir).read_model(run_key(response.json()["run_id"], "run.json"), RunRecord)
    assert record.upload_id == upload_id
