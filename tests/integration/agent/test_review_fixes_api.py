"""Plan G adversarial review, API findings: each confirmed defect pinned by the repro that found it."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from api.routes import runs
from api.routes.agent_recipes import replay_for_scoring, write_derived_upload
from api.routes.uploads import load_upload
from engine.agent.config import AgentLevel
from engine.agent.contracts import DATA_RECIPE_FILENAME, DataRecipe, RecipeStep, RecipeStepKind, recipe_hash
from engine.agent.recipe import RecipeError
from engine.config import ColumnType, Metric, ProblemType, load_use_case
from engine.contracts import (
    FeatureSchema,
    FeatureSchemaColumn,
    ModelStatus,
    ModelVersion,
    RunRecord,
    ValidationReport,
)
from engine.jobs import CancelToken
from engine.registry import REGISTRY_FILENAME, LocalModelRegistry
from engine.storage import LocalStorage, run_key
from engine.utils.time import utc_now
from tests.integration.agent.test_recipe_runs import KEY, TARGET, USE_CASE, _messy, _recipe, _seed_model

pytestmark = pytest.mark.integration


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def client(config_root: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    def idle(*_args: Any, **_kwargs: Any) -> Any:
        def job(cancel: CancelToken) -> None:
            del cancel

        return job

    monkeypatch.setattr(runs, "build_job_fn", idle)
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as test_client:
        yield test_client


def _upload(client: TestClient, frame: pd.DataFrame, mode: str = "train") -> str:
    response = client.post(
        "/uploads",
        files={"file": ("history.csv", frame.to_csv(index=False).encode(), "text/csv")},
        data={"use_case": USE_CASE, "mode": mode},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["upload_id"])


def _score(client: TestClient, upload_id: str, model_version_id: str | None = None) -> Any:
    return client.post(
        "/runs",
        json={
            "use_case": USE_CASE,
            "mode": "score",
            "upload_id": upload_id,
            "primary_key": KEY,
            "model_version_id": model_version_id,
        },
    )


def _run_upload(data_dir: Path, response: Any) -> str:
    assert response.status_code == 202, response.text
    record = LocalStorage(data_dir).read_model(run_key(response.json()["run_id"], "run.json"), RunRecord)
    return str(record.upload_id)


def _seed_plain_version(data_dir: Path) -> ModelVersion:
    """Version 2, archived, trained on the file as sent: no recipe, and `region` as text."""
    run_id = "r_plain"
    version = ModelVersion(
        model_id="m_2",
        use_case_id=USE_CASE,
        version=2,
        run_id=run_id,
        created_at=utc_now(),
        status=ModelStatus.ARCHIVED,
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
    LocalStorage(data_dir).write_model(
        version.schema_key,
        FeatureSchema(
            use_case_id=USE_CASE,
            model_version_id=version.model_id,
            primary_key=KEY,
            target=TARGET,
            problem_type=ProblemType.BINARY_CLASSIFICATION,
            columns=(FeatureSchemaColumn(name="region", inferred_type=ColumnType.STRING),),
            row_count_at_fit=3_000,
            created_at=utc_now(),
        ),
    )
    return version


def _with_steps(recipe: DataRecipe, *extra: RecipeStep, **update: Any) -> DataRecipe:
    steps = (*recipe.steps, *extra)
    return DataRecipe.model_validate(
        {**recipe.model_dump(), **update, "steps": steps, "recipe_hash": recipe_hash(steps)}
    )


def _checks(report: dict[str, Any]) -> list[tuple[str, str | None]]:
    return [(check["code"], check["column"]) for check in report["checks"] if check["severity"] == "error"]


# --- a model with no recipe scores what the person sent, never another model's prepared copy ------


def test_a_model_without_a_recipe_scores_the_file_as_sent_not_a_prepared_copy(
    client: TestClient, data_dir: Path
) -> None:
    _seed_model(data_dir, _recipe(_messy(), target=None))  # the champion prepares its files
    _seed_plain_version(data_dir)  # version 2 was trained on files as sent
    original = _upload(client, _messy("scoring", rows=600), mode="score")
    prepared = _run_upload(data_dir, _score(client, original))  # the champion's prepared copy
    assert prepared != original
    assert _run_upload(data_dir, _score(client, prepared, "m_2")) == original


# --- Guided setup on a prepared training upload would save only its own steps ---------------------


def test_guided_setup_on_a_prepared_training_upload_is_refused(client: TestClient, data_dir: Path) -> None:
    storage = LocalStorage(data_dir)
    frame = _messy()
    original = load_upload(storage, _upload(client, frame))
    derived = write_derived_upload(storage, load_use_case(USE_CASE), original, _recipe(frame))
    response = client.post(f"/uploads/{derived.record.upload_id}/agent-session", json={"use_case": USE_CASE})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "AGENT_UPLOAD_PREPARED"


# --- the dry run prepares a scoring file exactly as Run does (DEC-1011) ---------------------------


def test_the_dry_run_prepares_a_scoring_file_with_the_models_recipe(
    client: TestClient, data_dir: Path
) -> None:
    _seed_model(data_dir, _recipe(_messy(), target=None))
    storage = LocalStorage(data_dir)
    fits = _upload(client, _messy("scoring", rows=600), mode="score")
    broken = _upload(
        client, _messy("scoring", rows=600).rename(columns={"ad_ctr_90d": "ctr_90d"}), mode="score"
    )
    before = sorted(storage.list_keys("uploads/"))
    body = {"use_case": USE_CASE, "primary_key": KEY}
    dry_fits = client.post(f"/uploads/{fits}/checks", json=body)
    dry_broken = client.post(f"/uploads/{broken}/checks", json=body)
    assert sorted(storage.list_keys("uploads/")) == before  # the dry run writes nothing
    assert dry_fits.status_code == dry_broken.status_code == 200
    assert dry_fits.json()["passed"] is True, _checks(dry_fits.json())
    assert _checks(dry_broken.json()) == [("RECIPE_COLUMN_MISSING", "ad_ctr_90d")]
    assert _score(client, fits).status_code == 202
    run_broken = _score(client, broken)
    assert run_broken.status_code == 409
    assert _checks(run_broken.json()["validation"]) == _checks(dry_broken.json())


# --- a session on a file the model's recipe already prepared uses it as it is ---------------------


def test_a_session_on_a_scoring_file_the_models_recipe_prepared_is_not_prepared_twice(
    client: TestClient, data_dir: Path
) -> None:
    hide = RecipeStep(order=3, kind=RecipeStepKind.DROP_COLUMN, column="last_contacted_at")
    _seed_model(data_dir, _with_steps(_recipe(_messy(), target=None), hide))
    prepared = _run_upload(
        data_dir, _score(client, _upload(client, _messy("scoring", rows=600), mode="score"))
    )
    assert _score(client, prepared).status_code == 202
    session = client.post(f"/uploads/{prepared}/agent-session", json={"use_case": USE_CASE})
    assert session.status_code == 201, session.text
    assert session.json()["session"]["status"] != "stopped", session.json()["session"]["stop_reason"]


# --- a column whose type changes between read chunks is written, not a 500 -----------------------


def _mixed_ids(rows: int) -> pd.DataFrame:
    """Numeric IDs for the first 100,000-row read chunk, then IDs with letters in them."""
    ids = [str(i) for i in range(rows - 3)] + ["C-1", "C-2", "C-3"]
    return pd.DataFrame(
        {
            KEY: ids,
            "ad_ctr_90d": ["1.5%" if i % 2 else "2.0%" for i in range(rows)],
            "region": ["NORTH" if i % 3 == 0 else "north" for i in range(rows)],
            TARGET: [i % 2 for i in range(rows)],
        }
    )


def test_a_column_whose_type_changes_between_read_chunks_is_prepared(
    client: TestClient, data_dir: Path
) -> None:
    storage = LocalStorage(data_dir)
    frame = _mixed_ids(100_010)
    upload = load_upload(storage, _upload(client, frame))
    recipe = _recipe(frame)
    derived = write_derived_upload(storage, load_use_case(USE_CASE), upload, recipe)
    written = pd.read_parquet(data_dir / derived.record.source_key)
    assert len(written) == 100_010
    assert written[KEY].tolist() == frame[KEY].tolist()  # the ID column's text, exactly


def test_a_prepared_file_that_cannot_be_written_is_a_coded_refusal(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pyarrow as pa

    storage = LocalStorage(data_dir)
    frame = _messy(rows=600)
    upload = load_upload(storage, _upload(client, frame))

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise pa.ArrowInvalid("Conversion failed for column customer_id with type object")

    monkeypatch.setattr(pd.DataFrame, "to_parquet", refuse)
    with pytest.raises(RecipeError) as caught:
        write_derived_upload(storage, load_use_case(USE_CASE), upload, _recipe(frame))
    assert caught.value.code == "RECIPE_STEP_INVALID"


# --- scoring from a built dataset cannot replay a recipe, so a recipe model refuses it ------------
# (tests/integration/agent/test_dataset_recipe_refusal.py builds the dataset)


# --- an upload prepared under other roles or limits is not reused ---------------------------------


def test_an_upload_prepared_with_the_same_steps_for_other_roles_is_prepared_again(
    client: TestClient, data_dir: Path
) -> None:
    storage = LocalStorage(data_dir)
    scoring = _messy("scoring", rows=600)
    original = load_upload(storage, _upload(client, scoring, mode="score"))
    other_roles = _with_steps(_recipe(scoring, target=None), primary_key=None)
    prepared = write_derived_upload(storage, load_use_case(USE_CASE), original, other_roles)
    _seed_model(data_dir, _recipe(scoring, target=None))  # same steps, same hash, other ID column
    assert other_roles.recipe_hash == _recipe(scoring, target=None).recipe_hash
    used = _run_upload(data_dir, _score(client, prepared.record.upload_id))
    assert used not in {prepared.record.upload_id, original.upload_id}


# --- POST /runs keeps a prepared training upload's roles -----------------------------------------


def test_training_on_a_prepared_upload_with_other_roles_is_refused(
    client: TestClient, data_dir: Path
) -> None:
    storage = LocalStorage(data_dir)
    frame = _messy()
    original = load_upload(storage, _upload(client, frame))
    derived = write_derived_upload(storage, load_use_case(USE_CASE), original, _recipe(frame))
    body = {"use_case": USE_CASE, "mode": "train", "upload_id": derived.record.upload_id}
    other_target = client.post("/runs", json={**body, "primary_key": KEY, "target": "marketing_opt_in"})
    assert other_target.status_code == 409, other_target.text
    assert other_target.json()["detail"]["code"] == "RECIPE_ROLES_MISMATCH"
    other_key = client.post("/runs", json={**body, "primary_key": "snapshot_date", "target": TARGET})
    assert other_key.status_code == 409, other_key.text
    assert other_key.json()["detail"]["code"] == "RECIPE_ROLES_MISMATCH"
    assert client.post("/runs", json={**body, "primary_key": KEY, "target": TARGET}).status_code == 202


# --- a replay runs under the levels and limit the recipe was approved with ------------------------


def test_scoring_replays_with_the_failure_limit_the_recipe_was_approved_with(
    client: TestClient, data_dir: Path
) -> None:
    _seed_model(data_dir, _recipe(_messy(), target=None).model_copy(update={"max_failure_pct": 50.0}))
    scoring = _messy("scoring", rows=800)
    scoring.loc[: len(scoring) // 5, "ad_ctr_90d"] = "unknown"  # 20%: over today's 5%, under the 50% approved
    assert _score(client, _upload(client, scoring, mode="score")).status_code == 202


def test_scoring_replays_with_the_levels_the_recipe_was_approved_with(
    client: TestClient, data_dir: Path
) -> None:
    storage = LocalStorage(data_dir)
    scoring = _messy("scoring", rows=600)
    derive = RecipeStep(
        order=3,
        kind=RecipeStepKind.DERIVE,
        column="tenure_months",
        new_column="tenure_years",
        params={"expression": "tenure_months / 12"},
    )
    recipe = _with_steps(_recipe(scoring, target=None), derive, levels=(AgentLevel.CLEAN, AgentLevel.DERIVE))
    version = _seed_model(data_dir, recipe)
    config = load_use_case(USE_CASE)
    clean_only = config.model_copy(
        update={"agent": config.agent.model_copy(update={"levels": (AgentLevel.CLEAN,)})}
    )
    upload = load_upload(storage, _upload(client, scoring, mode="score"))
    replayed = replay_for_scoring(storage, clean_only, upload, version)
    assert not isinstance(replayed, ValidationReport), replayed


def test_approve_records_the_levels_and_limit_on_the_recipe(client: TestClient, data_dir: Path) -> None:
    from tests.fixtures.agent_bench.make_messy import messy_frame

    upload_id = _upload(client, messy_frame())
    client.post(f"/uploads/{upload_id}/agent-session", json={"use_case": USE_CASE})
    session = client.post(
        f"/uploads/{upload_id}/agent-session/decisions", json={"accept_recommended": True}
    ).json()["session"]
    for question in session["questions"]:
        if question["answer"] is None:
            session = client.post(
                f"/uploads/{upload_id}/agent-session/answers",
                json={
                    "question_id": question["question_id"],
                    "option_id": question["options"][0]["option_id"],
                },
            ).json()["session"]
    pending = [p["proposal_id"] for p in session["proposals"] if p["state"] == "pending"]
    client.post(
        f"/uploads/{upload_id}/agent-session/decisions",
        json={"decisions": [{"proposal_id": pid, "state": "accepted"} for pid in pending]},
    )
    applied = client.post(f"/uploads/{upload_id}/agent-session/apply")
    assert applied.status_code == 200, applied.text
    saved = LocalStorage(data_dir).read_model(
        f"uploads/{applied.json()['upload_id']}/{DATA_RECIPE_FILENAME}", DataRecipe
    )
    agent = load_use_case(USE_CASE).agent
    assert saved.levels == agent.levels
    assert saved.max_failure_pct == agent.max_conversion_failure_pct
