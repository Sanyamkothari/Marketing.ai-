"""A model trained on Guided-prepared data cannot score a built dataset yet: it is refused by name.

DEC-1006 prepares every file a recipe model scores with the model's own recipe. Only uploads are
prepared (`replay_for_scoring`); a built dataset - `POST /runs` with `dataset_id`, or a scheduled
score firing - was validated and scored as built, so a recipe that merged spellings scored categories
the model never saw. Until a dataset can be prepared too, both refuse with `RECIPE_DATASET_UNSUPPORTED`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine.agent.contracts import DATA_RECIPE_FILENAME, DataRecipe, RecipeStep, RecipeStepKind, recipe_hash
from engine.config import ColumnType, Metric, ProblemType, RunMode, get_catalog, resolve_config
from engine.contracts import FeatureSchema, FeatureSchemaColumn, ModelStatus, ModelVersion
from engine.onboarding.datasets import dataset_key
from engine.onboarding.specs import DatasetManifest
from engine.registry import REGISTRY_FILENAME, LocalModelRegistry
from engine.scheduling.firing import FiringError, start_dataset_run
from engine.storage import LocalStorage, run_key
from engine.utils.time import utc_now
from tests.integration.test_runs_from_dataset import USE_CASE, _build
from tests.unit.production.scheduling_support import RecordingJobs

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str]:
    root = tmp_path_factory.mktemp("dataset-recipe")
    _, dataset_id, report = _build(root)
    assert report.passed
    return root, dataset_id


def _seed(data_dir: Path, run_id: str, model_id: str, *, recipe: bool, champion: bool) -> ModelVersion:
    version = ModelVersion(
        model_id=model_id,
        use_case_id=USE_CASE,
        version=1 if champion else 2,
        run_id=run_id,
        created_at=utc_now(),
        status=ModelStatus.CHAMPION if champion else ModelStatus.ARCHIVED,
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
            primary_key="customer_id",
            target="churned",
            problem_type=ProblemType.BINARY_CLASSIFICATION,
            columns=(FeatureSchemaColumn(name="events_90d", inferred_type=ColumnType.INTEGER),),
            row_count_at_fit=1_000,
            created_at=utc_now(),
        ),
    )
    if recipe:
        steps = (
            RecipeStep(order=1, kind=RecipeStepKind.NORMALISE_TEXT, column="region", params={"strip": True}),
        )
        storage.write_model(
            run_key(run_id, DATA_RECIPE_FILENAME),
            DataRecipe(
                recipe_id="rec1",
                use_case_id=USE_CASE,
                source_fingerprint="f",
                steps=steps,
                recipe_hash=recipe_hash(steps),
                primary_key="customer_id",
                created_at=utc_now(),
            ),
        )
    return version


@pytest.fixture
def seeded(built: tuple[Path, str]) -> tuple[Path, str, ModelVersion]:
    root, dataset_id = built
    data_dir = root / "data"
    if (data_dir / REGISTRY_FILENAME).exists():
        (data_dir / REGISTRY_FILENAME).unlink()
    version = _seed(data_dir, "r_guided", "m_guided", recipe=True, champion=True)
    return root, dataset_id, version


def test_post_runs_refuses_to_score_a_dataset_with_a_recipe_model(
    seeded: tuple[Path, str, ModelVersion],
) -> None:
    root, dataset_id, _ = seeded
    app = create_app(data_dir=root / "data")
    app.state.jobs = RecordingJobs()
    response = TestClient(app).post(
        "/runs", json={"use_case": USE_CASE, "mode": "score", "dataset_id": dataset_id}
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "RECIPE_DATASET_UNSUPPORTED"


def test_a_scheduled_score_firing_refuses_a_recipe_model(seeded: tuple[Path, str, ModelVersion]) -> None:
    root, dataset_id, version = seeded
    storage = LocalStorage(root / "data")
    manifest = storage.read_model(dataset_key(dataset_id, "dataset_manifest.json"), DatasetManifest)
    resolved = resolve_config(USE_CASE, {})
    with pytest.raises(FiringError) as caught:
        start_dataset_run(
            storage=storage,
            registry=LocalModelRegistry(root / "data" / REGISTRY_FILENAME),
            jobs=RecordingJobs(),
            resolved=resolved,
            catalog=get_catalog(None),
            manifest=manifest,
            mode=RunMode.SCORE,
            version=version,
            client_tag=None,
        )
    assert caught.value.code == "RECIPE_DATASET_UNSUPPORTED"
