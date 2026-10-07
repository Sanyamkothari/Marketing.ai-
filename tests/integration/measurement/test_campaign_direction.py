"""The campaign page and step 4 read one report the same way round (Plan J M94, DEC-1304 (i)).

A churn campaign exists to make its outcome rarer (`configs/pilot/value.yaml` `outcomes_to_prevent`).
Step 4 (`api.routes.measure._view`) takes a column *found* in the outcomes file to be the use case's own
outcome, whatever the file calls it, and judges a column the person *named* by its own name; the
campaign view does exactly the same, and passes the same outcome words. Otherwise a churn file whose
only column is `churned` would read "prevented" on one page and "harmed" on the other.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine import __version__
from engine.config import ProblemType, RunMode
from engine.contracts import RunRecord, RunState
from engine.runs import RUN_FILENAME
from engine.stages import export
from engine.storage import LocalStorage, run_key
from tests.integration.measurement.support import ok

pytestmark = pytest.mark.integration

USE_CASE = "telco-churn"
KEY = "customerID"
RUN_ID = "r_20261007_94000021"
SENT = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
AS_OF = SENT + timedelta(days=120)
ROWS = 4_000


def _churn_run(storage: LocalStorage) -> pd.DataFrame:
    """A telco-churn scoring run whose retention campaign cut churn from about 30 % to about 15 %."""
    rng = np.random.default_rng(21)
    keys = [f"T-{index:05d}" for index in range(ROWS)]
    control = rng.random(ROWS) < 0.2
    scores = pd.DataFrame(
        {
            KEY: keys,
            "band": rng.choice(["High", "Medium"], size=ROWS),
            "suppressed_reason": [None] * ROWS,
            "control_group": control,
        }
    )
    buffer = io.BytesIO()
    scores.to_parquet(buffer, index=False)
    storage.write_bytes(run_key(RUN_ID, export.SCORES_PARQUET), buffer.getvalue())
    storage.write_model(
        run_key(RUN_ID, RUN_FILENAME),
        RunRecord(
            run_id=RUN_ID,
            use_case_id=USE_CASE,
            use_case_name="Telco churn",
            mode=RunMode.SCORE,
            state=RunState.DONE,
            created_at=SENT - timedelta(minutes=5),
            started_at=SENT - timedelta(minutes=4),
            finished_at=SENT,
            upload_id="u_94000021",
            file_name="telco.csv",
            row_count=ROWS,
            primary_key=KEY,
            problem_type=ProblemType.BINARY_CLASSIFICATION,
            model_choice="auto",
            model_version_id=f"m_{USE_CASE}_1",
            best_model="LightGBM",
            engine_version=__version__,
        ),
    )
    churned = rng.random(ROWS) < np.where(control, 0.30, 0.15)
    return pd.DataFrame({KEY: keys, "churned": churned.astype(int)})


def _upload(client: TestClient, frame: pd.DataFrame) -> str:
    payload = frame.to_csv(index=False, lineterminator="\n").encode()
    response = client.post(
        "/uploads",
        files={"file": ("churn_outcomes.csv", payload, "text/csv")},
        data={"use_case": USE_CASE, "mode": "score"},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["upload_id"])


@pytest.mark.parametrize("named", [False, True])
def test_the_campaign_page_and_step_4_agree_on_which_way_round(
    config_root: Path, tmp_path: Path, named: bool
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    storage = LocalStorage(data_dir)
    outcomes = _churn_run(storage)
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as client:
        upload_id = _upload(client, outcomes)
        column: dict[str, Any] = {"outcome_column": "churned"} if named else {}
        step4 = ok(
            client.post(
                f"/runs/{RUN_ID}/measure",
                json={"upload_id": upload_id, "as_of": AS_OF.isoformat(), **column},
            )
        )
        created = ok(client.post("/campaigns", json={"run_id": RUN_ID}), 201)
        campaign_id = created["campaign"]["campaign_id"]
        ok(client.post(f"/campaigns/{campaign_id}/outcomes", json={"upload_id": upload_id, **column}))
        view = ok(client.post(f"/campaigns/{campaign_id}/measure", json={"as_of": AS_OF.isoformat()}))
    assert view["campaign"]["outcomes"]["outcome_column"] == "churned"
    assert view["campaign"]["outcomes"]["outcome_named"] is named
    # a found column is the use case's own outcome (churn: one to prevent); a named one is judged by name
    assert step4["outcome_is_good"] is named
    assert view["outcome_is_good"] is step4["outcome_is_good"]
    assert view["verdict"]["kind"] == step4["verdict"]["kind"]
    assert view["verdict"]["kind"] == ("harmed" if named else "prevented")
    assert view["verdict"] == step4["verdict"], "the same headline, in the same outcome words"
