"""Next month's file changed: the rename is proposed, and a person's yes lets it through (Plan L #16, DEC-1597).

Month one: a billing export with `amount` (written `₹1,234.50`) is set up in Guided setup, which reads
`amount` as numbers, and a model is trained from it. Month two: the export calls the column `bill_amount`.

* Scoring: the model's saved steps stop with `RECIPE_COLUMN_MISSING`; the 409 and Guided setup both say
  "your file has 'bill_amount' where it had 'amount'". The person's yes makes a copy with the old name,
  and the copy scores. The file they sent, and the model's steps, are left as they are.
* Training: the file no longer has the remembered shape, so the same treatment is not offered; Guided
  setup proposes the rename, and the copy is offered last month's treatment, which then approves.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from api.routes import runs
from engine.agent.contracts import DATA_RECIPE_FILENAME, DataRecipe
from engine.config import ColumnType, Metric, ProblemType
from engine.contracts import FeatureSchema, FeatureSchemaColumn, ModelStatus, ModelVersion
from engine.doctor.renames import RENAMED_COPY_FILENAME
from engine.jobs import CancelToken
from engine.registry import REGISTRY_FILENAME, LocalModelRegistry
from engine.storage import LocalStorage, run_key, upload_key
from engine.utils.time import utc_now
from tests.fixtures.agent_bench.make_messy import messy_frame
from tests.integration.agent.test_api_session import _decide_everything

pytestmark = pytest.mark.integration

USE_CASE = "targeted-advertisement"
KEY = "customer_id"
TARGET = "converted_30d"


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def client(config_root: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Real ingest, sessions, recipes and validation; a training job does nothing."""

    def idle(*_args: Any, **_kwargs: Any) -> Any:
        def job(cancel: CancelToken) -> None:
            del cancel

        return job

    monkeypatch.setattr(runs, "build_job_fn", idle)
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as test_client:
        yield test_client


def month(seed: int, name: str, rows: int = 3_000) -> pd.DataFrame:
    return messy_frame(rows, seed=seed).rename(columns={"monthly_spend": name})


def upload(client: TestClient, frame: pd.DataFrame, *, mode: str = "train", name: str = "billing.csv") -> Any:
    response = client.post(
        "/uploads",
        files={"file": (name, frame.to_csv(index=False).encode(), "text/csv")},
        data={"use_case": USE_CASE, "mode": mode},
    )
    assert response.status_code == 201, response.text
    return response.json()


def approve_month_one(client: TestClient, data_dir: Path) -> tuple[str, DataRecipe]:
    """Month one through Guided setup; the prepared upload and its recipe, which reads `amount`."""
    upload_id = upload(client, month(7, "amount"), name="billing_sept.csv")["upload_id"]
    assert client.post(f"/uploads/{upload_id}/agent-session", json={"use_case": USE_CASE}).status_code == 201
    assert _decide_everything(client, upload_id)["status"] == "ready"
    applied = client.post(f"/uploads/{upload_id}/agent-session/apply")
    assert applied.status_code == 200, applied.text
    prepared = applied.json()["upload_id"]
    recipe = LocalStorage(data_dir).read_model(upload_key(prepared, DATA_RECIPE_FILENAME), DataRecipe)
    assert any(s.column == "amount" and s.kind.value == "parse_number" for s in recipe.steps)
    return prepared, recipe


def train_model(client: TestClient, data_dir: Path, prepared: str) -> str:
    """`POST /runs` from the prepared upload (the job does nothing), then the version that run registers."""
    response = client.post(
        "/runs",
        json={
            "use_case": USE_CASE,
            "mode": "train",
            "upload_id": prepared,
            "primary_key": KEY,
            "target": TARGET,
        },
    )
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
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
    LocalStorage(data_dir).write_model(
        version.schema_key,
        FeatureSchema(
            use_case_id=USE_CASE,
            model_version_id=version.model_id,
            primary_key=KEY,
            target=TARGET,
            problem_type=ProblemType.BINARY_CLASSIFICATION,
            columns=(FeatureSchemaColumn(name="amount", inferred_type=ColumnType.FLOAT),),
            row_count_at_fit=3_000,
            created_at=utc_now(),
        ),
    )
    return str(run_id)


def score(client: TestClient, upload_id: str) -> Any:
    return client.post(
        "/runs", json={"use_case": USE_CASE, "mode": "score", "upload_id": upload_id, "primary_key": KEY}
    )


def test_scoring_a_file_with_a_renamed_column_proposes_it_and_the_copy_scores(
    client: TestClient, data_dir: Path
) -> None:
    prepared, recipe = approve_month_one(client, data_dir)
    run_id = train_model(client, data_dir, prepared)
    october = month(11, "bill_amount", rows=800).drop(columns=[TARGET])
    upload_id = upload(client, october, mode="score", name="billing_oct.csv")["upload_id"]

    refused = score(client, upload_id)
    assert refused.status_code == 409, refused.text
    (check,) = refused.json()["validation"]["checks"]
    assert (check["code"], check["column"]) == ("RECIPE_COLUMN_MISSING", "amount")
    (proposed,) = check["details"]["suggested_renames"]
    assert (proposed["missing"], proposed["candidate"]) == ("amount", "bill_amount")
    assert "'bill_amount' where it had 'amount'" in check["suggestion"]

    dry = client.post(
        f"/uploads/{upload_id}/checks", json={"use_case": USE_CASE, "primary_key": KEY}
    )  # the dry run says the same
    assert dry.status_code == 200 and dry.json()["checks"][0]["details"]["suggested_renames"] == [proposed]

    started = client.post(f"/uploads/{upload_id}/agent-session", json={"use_case": USE_CASE})
    assert started.status_code == 201, started.text
    assert started.json()["session"]["status"] == "stopped"
    changes = started.json()["changes"]
    assert changes["basis"] == "model" and changes["source_file_name"] == "billing_sept.csv"
    assert [(r["missing"], r["candidate"]) for r in changes["suggested_renames"]] == [
        ("amount", "bill_amount")
    ]
    assert "Your file has 'bill_amount' where it had 'amount'" in changes["sentence"]

    copied = client.post(
        f"/uploads/{upload_id}/renamed-copy",
        json={"renames": [{"column": "bill_amount", "new_name": "amount"}]},
    )
    assert copied.status_code == 201, copied.text
    copy_id = copied.json()["upload_id"]
    assert copy_id != upload_id
    assert "amount" in copied.json()["profile"]["fingerprint"]["columns"]
    on_prepared = client.post(
        f"/uploads/{prepared}/renamed-copy", json={"renames": [{"column": "amount", "new_name": "amt"}]}
    )
    assert on_prepared.status_code == 409 and on_prepared.json()["detail"]["code"] == "DOCTOR_RENAME_PREPARED"
    storage = LocalStorage(data_dir)
    assert storage.exists(upload_key(copy_id, RENAMED_COPY_FILENAME))
    sent = client.get(f"/uploads/{upload_id}/profile").json()
    assert "bill_amount" in sent["fingerprint"]["columns"], "the file sent is left as it is"

    accepted = score(client, copy_id)
    assert accepted.status_code == 202, accepted.text
    saved = storage.read_model(run_key(run_id, DATA_RECIPE_FILENAME), DataRecipe)
    assert saved.recipe_hash == recipe.recipe_hash, "the model's steps are never edited"

    session = client.post(f"/uploads/{copy_id}/agent-session", json={"use_case": USE_CASE})
    assert session.status_code == 201 and session.json()["session"]["status"] != "stopped"
    assert session.json()["changes"] is None


def test_next_months_training_file_with_a_renamed_column_gets_last_months_treatment(
    client: TestClient, data_dir: Path
) -> None:
    _, first = approve_month_one(client, data_dir)
    october = upload(client, month(11, "bill_amount"), name="billing_oct.csv")
    assert october["treatment"] is None, "another shape: the same treatment is not offered as it is"

    started = client.post(f"/uploads/{october['upload_id']}/agent-session", json={"use_case": USE_CASE})
    assert started.status_code == 201
    changes = started.json()["changes"]
    assert changes["basis"] == "last_treatment" and changes["then_same_treatment"]
    assert [(r["missing"], r["candidate"]) for r in changes["suggested_renames"]] == [
        ("amount", "bill_amount")
    ]
    assert "This looks like billing_sept.csv" in changes["sentence"]
    proposals = started.json()["session"]["proposals"]
    assert all(p["state"] == "pending" for p in proposals), "nothing is ticked for the person"

    copied = client.post(
        f"/uploads/{october['upload_id']}/renamed-copy",
        json={"renames": [{"column": "bill_amount", "new_name": "amount"}]},
    )
    assert copied.status_code == 201, copied.text
    offer = copied.json()["treatment"]
    assert offer is not None and offer["kind"] == "same_shape" and offer["recipe_id"] == first.recipe_id

    copy_id = copied.json()["upload_id"]
    assert client.post(f"/uploads/{copy_id}/agent-session", json={"use_case": USE_CASE}).status_code == 201
    used = client.post(
        f"/uploads/{copy_id}/agent-session/same-treatment", json={"recipe_id": first.recipe_id}
    )
    assert used.status_code == 200, used.text
    assert _decide_everything(client, copy_id)["status"] == "ready"
    applied = client.post(f"/uploads/{copy_id}/agent-session/apply")
    assert applied.status_code == 200, applied.text


def test_a_renamed_copy_is_refused_for_an_unknown_column_or_a_taken_name(client: TestClient) -> None:
    upload_id = upload(client, month(7, "amount"))["upload_id"]
    for renames, code in (
        ([{"column": "nope", "new_name": "x"}], "DOCTOR_RENAME_UNKNOWN"),
        ([{"column": "amount", "new_name": "region"}], "DOCTOR_RENAME_INVALID"),
        (
            [{"column": "amount", "new_name": "a"}, {"column": "region", "new_name": "a"}],
            "DOCTOR_RENAME_INVALID",
        ),
    ):
        response = client.post(f"/uploads/{upload_id}/renamed-copy", json={"renames": renames})
        assert response.status_code == 422 and response.json()["detail"]["code"] == code, response.text
    assert client.post(f"/uploads/{upload_id}/renamed-copy", json={"renames": []}).status_code == 422


def test_new_spellings_in_a_merged_column_are_shown_not_applied(client: TestClient) -> None:
    """Month one merges a spelling by hand; month two (the same shape) has spellings nobody merged."""
    first = month(7, "amount")
    known = sorted(str(v).strip() for v in first["plan_tier"].dropna().unique())
    upload_id = upload(client, first, name="billing_sept.csv")["upload_id"]
    assert client.post(f"/uploads/{upload_id}/agent-session", json={"use_case": USE_CASE}).status_code == 201
    edit = {"kind": "map_values", "column": "plan_tier", "params": {"mapping": {known[0]: known[1]}}}
    assert client.post(f"/uploads/{upload_id}/agent-session/edits", json=edit).status_code == 200
    assert _decide_everything(client, upload_id)["status"] == "ready"
    assert client.post(f"/uploads/{upload_id}/agent-session/apply").status_code == 200

    second = month(11, "amount")
    second.loc[second.index % 50 == 0, "plan_tier"] = "Platinum Plus"
    october = upload(client, second, name="billing_oct.csv")
    assert october["treatment"] is not None, "the same shape"
    started = client.post(f"/uploads/{october['upload_id']}/agent-session", json={"use_case": USE_CASE})
    changes = started.json()["changes"]
    assert changes["basis"] == "last_treatment" and changes["suggested_renames"] == []
    (unseen,) = changes["unseen_values"]
    assert unseen["column"] == "plan_tier" and unseen["values"] == ["Platinum Plus"]
    assert unseen["rows"] == int((second.index % 50 == 0).sum())
    assert "New spellings in 'plan_tier'" in changes["sentence"]
