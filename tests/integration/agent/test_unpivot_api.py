"""Plan L M156 through the API: a month-per-column sheet is turned into rows, combined, trained on, and next
month's sheet is prepared the same way before it is scored."""

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
from tests.fixtures.agent_bench.make_wide import wide_frame

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
        files={"file": ("sheet.csv", frame.to_csv(index=False).encode(), "text/csv")},
        data={"use_case": USE_CASE, "mode": mode},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["upload_id"])


def _session(response: Any) -> dict[str, Any]:
    assert response.status_code in {200, 201}, response.text
    return dict(response.json()["session"])


def _answer(client: TestClient, upload_id: str, session: dict[str, Any], option: str) -> dict[str, Any]:
    (question,) = [
        q
        for q in session["questions"]
        if q["answer"] is None and any(o["option_id"] == option for o in q["options"])
    ]
    return _session(
        client.post(
            f"/uploads/{upload_id}/agent-session/answers",
            json={"question_id": question["question_id"], "option_id": option},
        )
    )


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
                for name in ("spend_sum", "spend_latest", "orders_mean", "country")
            ),
            row_count_at_fit=columns[KEY].distinct_count,
            created_at=utc_now(),
        ),
    )
    return version


def _approve(client: TestClient, upload_id: str) -> dict[str, Any]:
    """Turn into rows, combine, accept what the helper is sure of, reject the rest, Approve."""
    session = _session(client.post(f"/uploads/{upload_id}/agent-session", json={"use_case": USE_CASE}))
    session = _answer(client, upload_id, session, "unpivot")
    session = _answer(client, upload_id, session, "combine")
    session = _session(
        client.post(f"/uploads/{upload_id}/agent-session/decisions", json={"accept_recommended": True})
    )
    pending = [p["proposal_id"] for p in session["proposals"] if p["state"] == "pending"]
    client.post(
        f"/uploads/{upload_id}/agent-session/decisions",
        json={"decisions": [{"proposal_id": pid, "state": "rejected"} for pid in pending]},
    )
    client.post(f"/uploads/{upload_id}/agent-session/preview")  # Approve checks that the preview was shown
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


def test_a_wide_sheet_is_turned_into_rows_trained_on_and_replayed_for_scoring(
    client: TestClient, data_dir: Path
) -> None:
    storage = LocalStorage(data_dir)
    sheet = wide_frame()
    upload_id = _upload(client, sheet)

    session = _session(client.post(f"/uploads/{upload_id}/agent-session", json={"use_case": USE_CASE}))
    assert session["status"] == "needs_review"
    (question,) = [q for q in session["questions"] if q["answer"] is None]
    assert question["text"] == (
        "Your sheet has one column per month for spend and orders. Turn them into rows (one row per shopper "
        "per month)?"
    )
    assert [o["label"] for o in question["options"]] == ["Turn into rows (recommended)", "Keep as columns"]
    blocked = client.post(f"/uploads/{upload_id}/agent-session/apply")
    assert blocked.json()["detail"]["code"] == "AGENT_UNDECIDED"

    session = _answer(client, upload_id, session, "unpivot")
    assert session["stop_reason"] is None
    (combine,) = [q for q in session["questions"] if q["answer"] is None]
    assert (
        combine["text"] == "Each shopper appears on 6 rows on average. Combine them into one row per shopper?"
    )
    session = _answer(client, upload_id, session, "combine")
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
    assert "Turn the month columns of spend and orders into rows" in session["summary"]["decisions"]

    preview = client.post(f"/uploads/{upload_id}/agent-session/preview")
    assert preview.status_code == 200, preview.text
    shown = preview.json()
    assert "2024_01_spend" in shown["columns_before"]
    assert {"spend_sum", "orders_latest", "country", TARGET} <= set(shown["columns_after"])
    unpivoted = shown["receipt"]["steps"][0]
    assert (unpivoted["kind"], unpivoted["rows"], unpivoted["changed"], unpivoted["failed"]) == (
        "unpivot",
        1_000,
        6_000,
        0,
    )

    applied = client.post(f"/uploads/{upload_id}/agent-session/apply")
    assert applied.status_code == 200, applied.text
    body = applied.json()
    shoppers = len(sheet)
    assert (body["primary_key"], body["target"]) == (KEY, TARGET)
    assert (body["receipt"]["rows_in"], body["receipt"]["rows_out"]) == (shoppers, shoppers)
    turned, combined = body["receipt"]["steps"]
    assert (turned["kind"], turned["rows"], turned["changed"], turned["failed"]) == (
        "unpivot",
        shoppers,
        shoppers * 6,
        0,
    )
    assert combined["kind"] == "combine_rows" and combined["rows"] == shoppers * 6
    assert combined["leak_check"].startswith(f"Full future-data check: {shoppers:,} of {shoppers:,}")
    prepared = load_upload_profile(storage, body["upload_id"])
    assert prepared.row_count == shoppers
    assert {c.name: c for c in prepared.columns}[KEY].is_unique

    run_id = _train(client, body)
    recipe = storage.read_model(run_key(run_id, DATA_RECIPE_FILENAME), DataRecipe)
    assert [s.kind.value for s in recipe.steps] == ["unpivot", "combine_rows"]

    # Next month: the same sheet for other shoppers, without the outcome, prepared the same way.
    _seed_model(data_dir, run_id, body["upload_id"])
    next_month = wide_frame(400, scoring=True)
    scoring_id = _upload(client, next_month, mode="score")
    scored = client.post(
        "/runs", json={"use_case": USE_CASE, "mode": "score", "upload_id": scoring_id, "primary_key": KEY}
    )
    assert scored.status_code == 202, scored.text
    record = storage.read_model(run_key(scored.json()["run_id"], "run.json"), RunRecord)
    assert record.upload_id is not None and record.upload_id != scoring_id
    replayed_profile = load_upload_profile(storage, record.upload_id)
    assert replayed_profile.row_count == 400
    assert {c.name for c in replayed_profile.columns} == {c.name for c in prepared.columns} - {TARGET}
    receipt = storage.read_model(upload_key(record.upload_id, RECIPE_RECEIPT_FILENAME), RecipeReceipt)
    assert (receipt.steps[0].kind.value, receipt.steps[0].changed) == ("unpivot", 2_400)
    assert receipt.steps[1].leak_check is not None and receipt.steps[1].leak_check.startswith("Narrow")
    replayed = storage.read_model(upload_key(record.upload_id, DATA_RECIPE_FILENAME), DataRecipe)
    assert replayed.recipe_hash == recipe.recipe_hash

    # Guided setup on next month's sheet reads the combined rows: nothing to ask.
    scoring = _session(client.post(f"/uploads/{scoring_id}/agent-session", json={"use_case": USE_CASE}))
    assert scoring["stop_reason"] is None and scoring["questions"] == []


def test_a_sheet_that_gained_a_month_is_refused_rather_than_scored_on_part_of_it(
    client: TestClient, data_dir: Path
) -> None:
    body = _approve(client, _upload(client, wide_frame()))
    _seed_model(data_dir, _train(client, body), body["upload_id"])
    grown = _upload(client, wide_frame(400, scoring=True, extra_month=True), mode="score")
    scored = client.post(
        "/runs", json={"use_case": USE_CASE, "mode": "score", "upload_id": grown, "primary_key": KEY}
    )
    assert scored.status_code == 409, scored.text
    (check,) = scored.json()["validation"]["checks"]
    assert check["code"] == "RECIPE_VALUES_UNCONVERTED"
    assert check["column"] in {"2024_07_spend", "2024_07_orders"}
    # A month the sheet no longer has stops with the missing column named.
    shrunk = wide_frame(400, scoring=True).drop(columns=["2024_01_spend"])
    scored = client.post(
        "/runs",
        json={
            "use_case": USE_CASE,
            "mode": "score",
            "upload_id": _upload(client, shrunk, "score"),
            "primary_key": KEY,
        },
    )
    assert scored.status_code == 409, scored.text
    (check,) = scored.json()["validation"]["checks"]
    assert (check["code"], check["column"]) == ("RECIPE_COLUMN_MISSING", "2024_01_spend")


def test_keep_as_columns_trains_on_the_sheet_as_it_is(client: TestClient) -> None:
    upload_id = _upload(client, wide_frame())
    session = _session(client.post(f"/uploads/{upload_id}/agent-session", json={"use_case": USE_CASE}))
    session = _answer(client, upload_id, session, "keep")
    assert session["stop_reason"] is None
    assert not any(q["answer"] is None and q["blocking"] for q in session["questions"])
    assert not any(p["step"] and p["step"]["kind"] == "unpivot" for p in session["proposals"])
