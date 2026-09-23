"""Retraining through the API yields a challenger, never a champion, while approval is required (slow).

Plan section 4: "retraining yields a challenger, never an automatic champion". The engine-level test
(`test_scheduling_flow.py`) settles it for the firer; this one settles it for the product a person
uses: an Analyst schedules and fires a retrain through the API, AutoGluon really fits the model the
firing's dataset feeds it, the pipeline's register stage decides, and the version waits for an
Approver. The Analyst who scheduled it cannot approve it; the Approver can, through the same API.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from engine.jobs import ThreadJobRunner
from tests.integration.production.schedules_support import Api, build_api, code_of
from tests.integration.production.test_scheduling_flow import FAST_SEARCH
from tests.unit.production.scheduling_support import USE_CASE

pytestmark = [pytest.mark.slow, pytest.mark.integration]


@pytest.fixture
def api(tmp_path: Path, config_root: Path) -> Api:
    patched = tmp_path / "configs"
    shutil.copytree(config_root, patched)
    use_case = patched / "use_cases" / "telco_churn.yaml"
    use_case.write_text(use_case.read_text() + FAST_SEARCH)
    return build_api(tmp_path / "w", patched)


def test_a_retrain_fired_through_the_api_waits_for_an_approver(api: Api) -> None:
    jobs = ThreadJobRunner(max_workers=1)
    api.app.state.jobs = jobs
    try:
        made = api.client.post(
            "/schedules",
            json={
                "use_case_id": USE_CASE,
                "kind": "retrain",
                "cadence": "monthly",
                "client_id": api.world.client_id,
            },
            headers=api.as_("analyst"),
        )
        assert made.status_code == 201, made.text
        schedule_id = made.json()["schedule_id"]
        fired = api.client.post(f"/schedules/{schedule_id}/fire", headers=api.as_("analyst")).json()
        assert (fired["status"], fired["result_code"]) == ("running", "TRAINING_STARTED"), fired
        jobs.wait(fired["run_id"], timeout=900)

        (settled,) = api.client.get(f"/schedules/{schedule_id}/firings", headers=api.as_("viewer")).json()[
            "firings"
        ]
        assert settled["status"] == "succeeded", settled["error_code"]
        assert settled["result_code"] == "MODEL_PENDING_APPROVAL"
        versions = api.client.get("/models", params={"use_case": USE_CASE}, headers=api.as_("viewer")).json()[
            "versions"
        ]
        assert [(item["version"]["status"], item["is_champion"]) for item in versions] == [
            ("pending_approval", False)
        ], "never an automatic champion"
        model_id = versions[0]["version"]["model_id"]

        refused = api.client.post(
            f"/models/{model_id}/approve", json={"approved_by": "asha"}, headers=api.as_("analyst")
        )
        assert refused.status_code == 403 and code_of(refused) == "ROLE_REQUIRED"
        approved = api.client.post(
            f"/models/{model_id}/approve", json={"approved_by": "priya"}, headers=api.as_("approver")
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["is_champion"] is True
    finally:
        jobs.shutdown(wait=True)
