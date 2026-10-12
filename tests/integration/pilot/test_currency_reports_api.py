"""Plan O M242 (DEC-1812): `GET /pilot/roi/{run_id}` notes the units and rates of the run's money columns when the Data
Doctor flow stored a money basis beside the run - never relabelling the rupee amounts worked out from the person's
inputs - and prints exactly what it printed before when it did not."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine import __version__
from engine.agent.contracts import RecipeStep, RecipeStepKind
from engine.config import ProblemType, RunMode
from engine.contracts import RunRecord, RunState
from engine.doctor.currency import money_basis, save_money_basis
from engine.runs import RUN_FILENAME
from engine.storage import LocalStorage, run_key
from engine.uplift.contracts import (
    INCREMENTALITY_FILENAME,
    ConfidenceValue,
    IncrementalityReport,
    IncrementalityStatus,
)
from engine.utils.time import utc_now

pytestmark = pytest.mark.integration

RUPEES = "r_20261012_dd000001"
DOLLARS = "r_20261012_dd000002"


@pytest.fixture(scope="module")
def client(config_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    data_dir = tmp_path_factory.mktemp("currency-api")
    storage = LocalStorage(data_dir)
    now = utc_now()
    for run_id in (RUPEES, DOLLARS):
        storage.write_model(
            run_key(run_id, RUN_FILENAME),
            RunRecord(
                run_id=run_id,
                use_case_id="telco-churn",
                use_case_name="Telco Customer Churn",
                mode=RunMode.SCORE,
                state=RunState.DONE,
                created_at=now - timedelta(days=90),
                finished_at=now - timedelta(days=90),
                file_name="scores.csv",
                primary_key="customer_id",
                problem_type=ProblemType.BINARY_CLASSIFICATION,
                model_choice="auto",
                engine_version=__version__,
            ),
        )
        storage.write_model(
            run_key(run_id, INCREMENTALITY_FILENAME),
            IncrementalityReport(
                run_id=run_id,
                outcome_column="retained_60d",
                outcome_window_days=60,
                as_of=now,
                status=IncrementalityStatus.MATURE,
                results_available_on=None,
                treated_rows=1000,
                treated_conversions=800,
                treated_rate=0.8,
                control_rows=100,
                control_conversions=74,
                control_rate=0.74,
                absolute_lift=ConfidenceValue(value=0.06, ci_low=0.03, ci_high=0.09),
                relative_lift=0.06 / 0.74,
                incremental_conversions=ConfidenceValue(value=60.0, ci_low=30.0, ci_high=90.0),
                p_value=0.1,
                rows_immature=0,
                rows_without_outcome=0,
                rows_suppressed_or_untreated=0,
                causal=True,
                summary="measured",
                computed_at=now,
            ),
        )
    steps = (
        RecipeStep(order=1, kind=RecipeStepKind.PARSE_NUMBER, column="bill_amount", params={"unit": "USD"}),
    )
    save_money_basis(storage, DOLLARS, money_basis(steps, workspace_currency="INR"))
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as test_client:
        for run_id in (RUPEES, DOLLARS):
            saved = test_client.put(
                f"/pilot/roi/{run_id}", json={"value_per_outcome": 40, "contact_cost": 0.02}
            )
            assert saved.status_code == 200, saved.text
        yield test_client


def test_rupee_inputs_stay_rupees_beside_a_dollar_basis(client: TestClient) -> None:
    """Review blocker (DEC-1812 (h)): the value inputs are rupees, so the amounts worked out from them stay in rupees
    when a column of the file is in US dollars; the dollar column is noted apart under "Currency"."""
    page = client.get(f"/pilot/roi/{DOLLARS}", params={"format": "html"})
    assert page.status_code == 200
    assert "Value in rupees" in page.text and "₹1,200 to ₹3,600" in page.text
    assert "is in US dollars, as the file gives it" in page.text
    assert "the file&#x27;s amounts are in US dollars. They are shown apart" in page.text
    assert "$" not in page.text and "Value in US dollars" not in page.text


def test_a_run_with_no_basis_prints_rupees_as_before(client: TestClient) -> None:
    page = client.get(f"/pilot/roi/{RUPEES}", params={"format": "html"})
    assert page.status_code == 200
    assert "Value in rupees" in page.text and "₹1,200 to ₹3,600" in page.text
    assert ">Currency<" not in page.text


def test_the_json_view_is_unchanged_by_a_basis(client: TestClient) -> None:
    rupees = client.get(f"/pilot/roi/{RUPEES}").json()
    dollars = client.get(f"/pilot/roi/{DOLLARS}").json()
    assert set(rupees) == set(dollars)
    assert rupees["net_value"] == dollars["net_value"]
