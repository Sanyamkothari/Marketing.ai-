"""Level 3 through the API (Plan G M76): an order log is combined on Approve, trains, and next month's
order log is combined the same way before it is scored."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from api.routes import runs
from api.routes.uploads import load_upload_profile
from engine.agent.contracts import DATA_RECIPE_FILENAME, RECIPE_RECEIPT_FILENAME, DataRecipe, RecipeReceipt
from engine.config import Metric, ProblemType
from engine.contracts import FeatureSchema, FeatureSchemaColumn, ModelStatus, ModelVersion, RunRecord
from engine.jobs import CancelToken
from engine.registry import REGISTRY_FILENAME, LocalModelRegistry
from engine.storage import LocalStorage, run_key, upload_key
from engine.utils.time import utc_now
from tests.fixtures.agent_bench.make_multirow import multirow_frame

pytestmark = pytest.mark.integration

USE_CASE = "retail-win-back"
KEY = "customer_id"
TARGET = "reactivated_90d"


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


def _upload(client: TestClient, frame: pd.DataFrame, mode: str = "train") -> str:
    response = client.post(
        "/uploads",
        files={"file": ("orders.csv", frame.to_csv(index=False).encode(), "text/csv")},
        data={"use_case": USE_CASE, "mode": mode},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["upload_id"])


def _session(response: Any) -> dict[str, Any]:
    assert response.status_code in {200, 201}, response.text
    return dict(response.json()["session"])


def _seed_model(data_dir: Path, run_id: str, trained_on: str) -> ModelVersion:
    """Register the run's model as the champion, with a schema read from the upload it trained on."""
    storage = LocalStorage(data_dir)
    version = ModelVersion(
        model_id="m_1",
        use_case_id=USE_CASE,
        version=1,
        run_id=run_id,
        created_at=utc_now(),
        status=ModelStatus.CHAMPION,
        metric=Metric.PR_AUC,
        metric_label="PR-AUC",
        test_score=0.6,
        validation_score=0.6,
        model_display_name="WeightedEnsemble_L2",
        schema_key=run_key(run_id, "schema.json"),
        run_config_key=run_key(run_id, "run_config.json"),
        predictor_key=run_key(run_id, "model"),
        engine_version="0.1.0",
        autogluon_version="1.6.3",
    )
    LocalModelRegistry(data_dir / REGISTRY_FILENAME).register(version)
    columns = {c.name: c for c in load_upload_profile(storage, trained_on).columns}
    storage.write_model(
        version.schema_key,
        FeatureSchema(
            use_case_id=USE_CASE,
            model_version_id=version.model_id,
            primary_key=KEY,
            target=TARGET,
            problem_type=ProblemType.BINARY_CLASSIFICATION,
            columns=tuple(
                FeatureSchemaColumn(name=name, inferred_type=columns[name].inferred_type)
                for name in ("row_count", "order_value_sum", "items_mean", "country_latest")
            ),
            row_count_at_fit=columns[KEY].distinct_count,
            created_at=utc_now(),
        ),
    )
    return version


def test_an_order_log_is_combined_trained_on_and_replayed_for_scoring(
    client: TestClient, data_dir: Path
) -> None:
    storage = LocalStorage(data_dir)
    orders = multirow_frame()
    upload_id = _upload(client, orders)

    session = _session(client.post(f"/uploads/{upload_id}/agent-session", json={"use_case": USE_CASE}))
    assert session["status"] == "needs_review"
    (question,) = [q for q in session["questions"] if q["answer"] is None]
    assert (
        question["text"]
        == "Each shopper appears on 5.1 rows on average. Combine them into one row per shopper?"
    )
    blocked = client.post(f"/uploads/{upload_id}/agent-session/apply")
    assert blocked.json()["detail"]["code"] == "AGENT_UNDECIDED"

    session = _session(
        client.post(
            f"/uploads/{upload_id}/agent-session/answers",
            json={"question_id": question["question_id"], "option_id": "combine"},
        )
    )
    assert session["stop_reason"] is None
    session = _session(
        client.post(f"/uploads/{upload_id}/agent-session/decisions", json={"accept_recommended": True})
    )
    pending = [p["proposal_id"] for p in session["proposals"] if p["state"] == "pending"]
    session = _session(
        client.post(
            f"/uploads/{upload_id}/agent-session/decisions",
            json={"decisions": [{"proposal_id": pid, "state": "rejected"} for pid in pending]},
        )
    )
    assert session["status"] == "ready"
    assert "Combine the rows into one row per shopper" in session["summary"]["decisions"]

    preview = client.post(f"/uploads/{upload_id}/agent-session/preview").json()
    assert "order_value_sum" in preview["columns_after"]
    assert preview["receipt"]["steps"][0]["leak_check"] is None  # a preview runs no leak check

    applied = client.post(f"/uploads/{upload_id}/agent-session/apply")
    assert applied.status_code == 200, applied.text
    body = applied.json()
    shoppers = orders[KEY].nunique()
    assert (body["primary_key"], body["target"]) == (KEY, TARGET)
    assert (body["receipt"]["rows_in"], body["receipt"]["rows_out"]) == (len(orders), shoppers)
    (combine,) = [s for s in body["receipt"]["steps"] if s["kind"] == "combine_rows"]
    assert combine["leak_check"].startswith(f"Full future-data check: {shoppers:,} of {shoppers:,}")
    prepared = load_upload_profile(storage, body["upload_id"])
    assert prepared.row_count == shoppers
    assert {c.name: c for c in prepared.columns}[KEY].is_unique  # passes the ID check

    run = client.post(
        "/runs",
        json={
            "use_case": USE_CASE,
            "mode": "train",
            "upload_id": body["upload_id"],
            "primary_key": body["primary_key"],
            "target": body["target"],
            "overrides": body["overrides"],
        },
    )
    assert run.status_code == 202, run.text
    run_id = run.json()["run_id"]
    recipe = storage.read_model(run_key(run_id, DATA_RECIPE_FILENAME), DataRecipe)
    assert [s.kind.value for s in recipe.steps] == ["combine_rows"]

    # Next month: the model trained by that run scores a new order log, combined the same way.
    _seed_model(data_dir, run_id, body["upload_id"])
    next_month = multirow_frame(400, scoring=True)
    scoring_id = _upload(client, next_month, mode="score")
    scored = client.post(
        "/runs", json={"use_case": USE_CASE, "mode": "score", "upload_id": scoring_id, "primary_key": KEY}
    )
    assert scored.status_code == 202, scored.text
    record = storage.read_model(run_key(scored.json()["run_id"], "run.json"), RunRecord)
    assert record.upload_id is not None and record.upload_id != scoring_id  # the combined copy is scored
    combined = load_upload_profile(storage, record.upload_id)
    assert combined.row_count == next_month[KEY].nunique() == 400
    names = {c.name for c in combined.columns}
    assert names == {c.name for c in prepared.columns} - {TARGET}  # the same columns as training
    receipt = storage.read_model(upload_key(record.upload_id, RECIPE_RECEIPT_FILENAME), RecipeReceipt)
    assert receipt.steps[0].leak_check is not None and receipt.steps[0].leak_check.startswith("Narrow")
    replayed = storage.read_model(upload_key(record.upload_id, DATA_RECIPE_FILENAME), DataRecipe)
    assert replayed.recipe_hash == recipe.recipe_hash


def _combine_and_approve(client: TestClient, upload_id: str) -> dict[str, Any]:
    """Answer Combine, accept what the helper is sure of, reject the rest, Approve."""
    session = _session(client.post(f"/uploads/{upload_id}/agent-session", json={"use_case": USE_CASE}))
    (question,) = session["questions"]
    client.post(
        f"/uploads/{upload_id}/agent-session/answers",
        json={"question_id": question["question_id"], "option_id": "combine"},
    )
    session = _session(
        client.post(f"/uploads/{upload_id}/agent-session/decisions", json={"accept_recommended": True})
    )
    pending = [p["proposal_id"] for p in session["proposals"] if p["state"] == "pending"]
    client.post(
        f"/uploads/{upload_id}/agent-session/decisions",
        json={"decisions": [{"proposal_id": pid, "state": "rejected"} for pid in pending]},
    )
    applied = client.post(f"/uploads/{upload_id}/agent-session/apply")
    assert applied.status_code == 200, applied.text
    return dict(applied.json())


def _train(client: TestClient, body: dict[str, Any]) -> str:
    run = client.post(
        "/runs",
        json={
            "use_case": USE_CASE,
            "mode": "train",
            "upload_id": body["upload_id"],
            "primary_key": KEY,
            "target": TARGET,
            "overrides": body["overrides"],
        },
    )
    assert run.status_code == 202, run.text
    return str(run.json()["run_id"])


def test_a_scoring_session_reads_the_combined_rows(client: TestClient, data_dir: Path) -> None:
    """Guided setup on next month's order log: the model's recipe combines it before anything is said."""
    body = _combine_and_approve(client, _upload(client, multirow_frame()))
    _seed_model(data_dir, _train(client, body), body["upload_id"])
    scoring_id = _upload(client, multirow_frame(400, scoring=True), mode="score")
    scoring = _session(client.post(f"/uploads/{scoring_id}/agent-session", json={"use_case": USE_CASE}))
    assert scoring["stop_reason"] is None
    assert scoring["status"] == "needs_review"
    assert [(p["path"], p["value"]) for p in scoring["proposals"]] == [("primary_key", KEY)]
    assert scoring["questions"] == []  # a scoring file is never asked to combine: the model's recipe did


def test_a_changed_order_log_stops_scoring_with_the_missing_column_named(
    client: TestClient, data_dir: Path
) -> None:
    body = _combine_and_approve(client, _upload(client, multirow_frame()))
    _seed_model(data_dir, _train(client, body), body["upload_id"])
    renamed = multirow_frame(400, scoring=True).rename(columns={"order_value": "value"})
    scoring_id = _upload(client, renamed, mode="score")
    scored = client.post(
        "/runs", json={"use_case": USE_CASE, "mode": "score", "upload_id": scoring_id, "primary_key": KEY}
    )
    assert scored.status_code == 409, scored.text
    (check,) = scored.json()["validation"]["checks"]
    assert (check["code"], check["column"]) == ("RECIPE_COLUMN_MISSING", "order_value")
