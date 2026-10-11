"""A real journey on the library's Hillstrom e-mail file: a warning card becomes an approved, carried-out plan
(Plan K M139 acceptance), every step through the product's public routes.

The deployment is the repository's configuration with three choices a client makes: Plan K switched on, the
e-mail channel configured (the public file carries no consent column, so everyone may be e-mailed), and the
outcome window the file's own definition gives (a purchase within two weeks). On the committed sample:

1. a propensity model is trained (`POST /uploads`, `POST /runs`) and the file scored twice (`POST /runs`);
2. the first list goes out as a campaign (`POST /campaigns`) with a test plan too small to see the effect it
   asks for (`POST /campaigns/{id}/plan`), so the Results page warns `PLAN_UNDERPOWERED`
   (`GET /campaigns/summary`);
3. "Draft a plan" on that card (`POST /agentic/plans`) drafts a plan for the second list, pending a person;
4. another person approves it (`POST /agentic/proposals/{id}/approve`): the second run's treat list is built,
   its campaign opened with the holdout in force and the proposed test plan frozen before any outcome exists.

No language model takes part (no AI service is connected): the narrative is the rules-only summary.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import SecretStr

from api.main import create_app
from engine.agentic.ledger import signer_for_data_dir, verify_ledger
from engine.platform_db import PLATFORM_DB_FILENAME, sqlite_engine
from engine.settings import Settings

pytestmark = [pytest.mark.integration, pytest.mark.slow]

REPO = Path(__file__).resolve().parents[2]
SAMPLE = REPO / "library" / "hillstrom-email" / "sample.csv"
USE_CASE = "hillstrom-email"
OUTCOMES = ["segment", "visit", "spend", "conversion"]
RISK_OVERRIDES: dict[str, Any] = {
    "model_search.strategy": "fast",
    "model_search.time_limit_minutes": 10,
    "model_search.tuning_trials": 5,
    "model_search.candidates": ["LightGBM"],
    "governance.approval_required": False,
    "validation.min_positive": 20,
}


def _deployment(target: Path) -> Path:
    shutil.copytree(REPO / "configs", target)
    (target / "agentic" / "agentic.yaml").write_text("agentic:\n  enabled: true\n", encoding="utf-8")
    path = target / "use_cases" / "hillstrom_email.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["actions"]["suppression"]["channels"] = {"email": {}}
    document["uplift"]["outcome_window_days"] = 14
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return target


def _ok(response: Any, *codes: int) -> Any:
    assert response.status_code in (codes or (200,)), response.text[:2000]
    return response.json()


def _upload(client: TestClient, frame: pd.DataFrame, mode: str) -> str:
    payload = frame.to_csv(index=False, lineterminator="\n").encode("utf-8")
    body = _ok(
        client.post(
            "/uploads",
            files={"file": (f"{mode}.csv", payload, "text/csv")},
            data={"use_case": USE_CASE, "mode": mode},
        ),
        201,
    )
    return str(body["upload_id"])


def _finish(client: TestClient, run_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 900
    while True:
        record = _ok(client.get(f"/runs/{run_id}"))["run"]
        if record["state"] in {"done", "failed", "cancelled"}:
            assert record["state"] == "done", record.get("error")
            return dict(record)
        assert time.monotonic() < deadline, f"run {run_id} did not finish"
        time.sleep(0.5)


def _run(client: TestClient, body: dict[str, Any]) -> dict[str, Any]:
    started = _ok(client.post("/runs", json=body), 202)
    return _finish(client, str(started["run_id"]))


def test_a_hillstrom_warning_card_becomes_an_approved_plan_carried_out(tmp_path: Path) -> None:
    root = _deployment(tmp_path / "configs")
    data = tmp_path / "data"
    frame = pd.read_csv(SAMPLE)
    settings = Settings(connections_key=SecretStr("F" * 40), allow_fake_ai=False)
    with TestClient(create_app(config_root=root, data_dir=data, settings=settings)) as client:
        trained = _run(
            client,
            {
                "use_case": USE_CASE,
                "mode": "train",
                "upload_id": _upload(client, frame.drop(columns=["segment", "visit", "spend"]), "train"),
                "primary_key": ["customer_id"],
                "target": "conversion",
                "overrides": RISK_OVERRIDES,
            },
        )
        features = frame.drop(columns=OUTCOMES)
        score = {"use_case": USE_CASE, "mode": "score", "primary_key": ["customer_id"]}
        first = _run(
            client,
            {
                **score,
                "upload_id": _upload(client, features, "score"),
                "model_version_id": trained["model_version_id"],
            },
        )

        # The first list goes out, planned on a test far too small for the effect it asks for.
        opened = _ok(client.post("/campaigns", json={"run_id": first["run_id"]}), 201)["campaign"]
        plan = {
            "metric": "bought within two weeks of the e-mail",
            "outcome_column": "conversion",
            "analysis_date": "2030-01-01",
            "mde_pp": 0.1,
            "base_rate": 0.006,
        }
        _ok(client.post(f"/campaigns/{opened['campaign_id']}/plan", json=plan), 201)
        cards = _ok(client.get("/campaigns/summary"))["cards"]
        card = next(c for c in cards if c["code"] == "PLAN_UNDERPOWERED")
        assert card["campaign_id"] == opened["campaign_id"]

        second = _run(
            client,
            {
                **score,
                "upload_id": _upload(client, features, "score"),
                "model_version_id": trained["model_version_id"],
            },
        )

        # "Draft a plan" on the card: a plan for the newest list, waiting for a person.
        drafted = _ok(
            client.post(
                "/agentic/plans", json={"card": {"code": card["code"], "campaign_id": card["campaign_id"]}}
            ),
            201,
        )
        proposal = drafted["proposal"]
        payload = proposal["payload"]
        assert proposal["status"] == "pending" and drafted["plan"]["step"] == "INTERRUPT"
        assert payload["run_id"] == second["run_id"] and payload["channel"] == "email"
        assert payload["objective"] == "larger_test" and payload["holdout"]["fraction"] == 0.5
        assert payload["test_plan"]["outcome_window_days"] == 14
        assert proposal["narrative"]["source"] == "rules"
        assert _ok(client.get("/campaigns", params={"run_id": second["run_id"]}))["campaigns"] == []
        # The test is sized on everyone the campaign will compare - the eligible rows and the held-back half -
        # not on the eligible rows alone (which would take the holdout off twice).
        evidence = drafted["plan"]["evidence"]
        contact = evidence["check_contactable"]["output"]
        (point,) = evidence["plan_test"]["output"]["points"]
        scored = _ok(client.get(f"/runs/{second['run_id']}/artefacts/scoring_summary.json"))
        assert contact["held_back_rows"] == scored["control_group_rows"] > 0
        assert point["n_treat"] + point["n_control"] == contact["eligible_rows"] + contact["held_back_rows"]
        suppressed = sum(int(item["rows"]) for item in scored["suppressed"])
        assert drafted["plan"]["size"]["population_rows"] == scored["rows_scored"] - suppressed

        receipt = _ok(
            client.post(f"/agentic/proposals/{proposal['proposal_id']}/approve", json={"reason": "Yes."})
        )
        assert receipt["status"] == "approved"

        summary = client.get(f"/runs/{second['run_id']}/artefacts/treat_list_summary.json")
        assert summary.status_code == 200 and summary.json()["total_rows"] == len(features)
        (campaign,) = _ok(client.get("/campaigns", params={"run_id": second["run_id"]}))["campaigns"]
        frozen = _ok(client.get(f"/campaigns/{campaign['campaign_id']}/plan"))["plan"]
        assert frozen["plan_hash"] == receipt["result_ref"] == campaign["test_plan_hash"]
        assert frozen["outcome_column"] == "conversion" and frozen["holdout_fraction"] > 0.4
        assert campaign["status"] == "live" and campaign["outcomes"] is None, "frozen before any outcome"
    assert verify_ledger(
        sqlite_engine(data / PLATFORM_DB_FILENAME), signer_for_data_dir(data, create=False)
    ).ok
