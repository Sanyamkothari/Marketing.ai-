"""Plan E's routes, without a trained model: help, the kit, feedback, demo mode off, the refusals."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.access_policy import all_policies
from api.main import create_app
from engine.contracts import CHECK_CODES

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def client(config_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    data_dir = tmp_path_factory.mktemp("pilot-api")
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as test_client:
        yield test_client


def test_help_carries_every_check_code_and_the_glossary(client: TestClient) -> None:
    body = client.get("/pilot/help").json()
    assert set(body["codes"]) >= CHECK_CODES
    assert "control group" in body["terms"]
    assert "actions.control_group_fraction" in body["settings"]


def test_the_data_request_comes_as_markdown_and_as_json(client: TestClient, repo_root: Path) -> None:
    markdown = client.get("/pilot/data-request")
    assert markdown.status_code == 200 and markdown.headers["content-type"].startswith("text/markdown")
    assert markdown.text == (repo_root / "docs" / "pilot" / "DATA_REQUEST.md").read_text(encoding="utf-8")
    facts = client.get("/pilot/data-request", params={"format": "json"}).json()
    assert facts["tables"][0]["role"] == "entity"
    churn = client.get("/pilot/data-request", params={"use_case": "telco-churn", "format": "json"}).json()
    assert [u["id"] for u in churn["use_cases"]] == ["telco-churn"]


def test_an_unknown_use_case_is_404(client: TestClient) -> None:
    response = client.get("/pilot/data-request", params={"use_case": "no-such-use-case"})
    assert response.status_code == 404 and response.json()["detail"]["code"] == "USE_CASE_NOT_FOUND"


def test_a_template_is_the_committed_header_row(client: TestClient, repo_root: Path) -> None:
    response = client.get("/pilot/templates/bills")
    assert response.status_code == 200
    assert response.text == (repo_root / "docs/pilot/templates/bills_template.csv").read_text(
        encoding="utf-8"
    )
    assert 'filename="bills_template.csv"' in response.headers["content-disposition"]
    assert client.get("/pilot/templates/orders").status_code == 404


def test_reports_of_things_that_do_not_exist_are_404_not_empty_pages(client: TestClient) -> None:
    assert client.get("/pilot/readiness/ds_missing").status_code == 404
    assert client.get("/pilot/results", params={"use_case": "telco-churn"}).status_code == 404
    assert client.get("/pilot/results").status_code == 422
    assert client.get("/pilot/roi/r_missing").status_code == 404
    assert client.put("/pilot/roi/r_missing", json={"value_per_outcome": 10}).status_code == 404


def test_demo_mode_is_off_by_default_and_hands_out_nothing(client: TestClient) -> None:
    body = client.get("/pilot/demo").json()
    assert body == {"demo_mode": False, "seeded": False, "manifest": None, "how_to_seed": ""}
    assert client.get("/pilot/demo/raw/clean").status_code == 404


def test_demo_mode_on_without_a_seed_says_how_to_seed(
    config_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The environment, not `create_app(settings=...)`: an explicit Settings also configures the
    # process's logging, which the logging tests elsewhere in the suite rely on nobody doing.
    monkeypatch.setenv("MARKETING_AI_DEMO_MODE", "true")
    monkeypatch.setenv("MARKETING_AI_DATA_DIR", str(tmp_path))
    app = create_app(config_root=config_root, data_dir=tmp_path)
    with TestClient(app) as test_client:
        body = test_client.get("/pilot/demo").json()
    assert body["demo_mode"] is True and body["seeded"] is False and body["how_to_seed"] == "make demo-seed"


def test_feedback_is_stored_masked_and_exported(client: TestClient) -> None:
    created = client.post(
        "/pilot/feedback",
        json={"screen": "#/pilot", "category": "confusing", "text": "reach me at asha@example.invalid"},
    )
    assert created.status_code == 201, created.text
    assert "email" in created.json()["redacted"]
    exported = client.get("/pilot/feedback/export")
    assert exported.status_code == 200
    rows = list(csv.DictReader(io.StringIO(exported.text)))
    assert any(row["feedback_id"] == created.json()["feedback_id"] for row in rows)
    assert "asha@example.invalid" not in exported.text
    assert client.get("/pilot/feedback/export", params={"format": "jsonl"}).status_code == 200


def test_feedback_names_a_route_never_page_content(client: TestClient) -> None:
    response = client.post("/pilot/feedback", json={"screen": "<b>1234</b>", "category": "idea", "text": ""})
    assert response.status_code == 422


def test_every_pilot_route_has_a_role_and_writes_are_not_open_to_everyone() -> None:
    policies = {key: policy for key, policy in all_policies().items() if key[1].startswith("/pilot/")}
    assert len(policies) == 11
    assert policies[("PUT", "/pilot/roi/{run_id}")].role.value == "analyst"
    assert policies[("GET", "/pilot/feedback/export")].audit_reads
    assert all(policy.role is not None for policy in policies.values())


def test_a_run_id_that_is_not_a_key_is_404_not_500(client: TestClient) -> None:
    for run_id in ("%2E%2E", "a%5Cb"):
        response = client.put(f"/pilot/roi/{run_id}", json={"value_per_outcome": 1})
        assert response.status_code == 404, response.text


def test_who_entered_the_values_is_who_is_signed_in(client: TestClient, tmp_path: Path) -> None:
    from engine import __version__
    from engine.config import ProblemType, RunMode
    from engine.contracts import RunRecord, RunState
    from engine.runs import RUN_FILENAME
    from engine.storage import LocalStorage, run_key
    from engine.utils.time import utc_now

    data_dir = Path(client.app.state.data_dir)
    now = utc_now()
    LocalStorage(data_dir).write_model(
        run_key("r_20260923_bb000001", RUN_FILENAME),
        RunRecord(
            run_id="r_20260923_bb000001",
            use_case_id="telco-churn",
            use_case_name="Telco Customer Churn",
            mode=RunMode.SCORE,
            state=RunState.DONE,
            created_at=now,
            finished_at=now,
            file_name="scores.csv",
            primary_key="customer_id",
            problem_type=ProblemType.BINARY_CLASSIFICATION,
            model_choice="auto",
            engine_version=__version__,
        ),
    )
    body = {"value_per_outcome": 100, "entered_by": "Somebody Else"}
    saved = client.put("/pilot/roi/r_20260923_bb000001", json=body).json()
    assert saved["inputs"]["entered_by"] != "Somebody Else"
    assert client.put("/pilot/roi/r_20260923_bb000001", json={"offer_cost": 1}).status_code == 422
