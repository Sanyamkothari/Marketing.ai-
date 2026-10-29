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
from engine.jobs import CancelToken
from tests.fixtures.agent_bench.make_messy import messy_frame

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
