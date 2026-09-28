"""Guided setup through the API (Plan G M74): suggest, decide, preview, approve, then the usual Run."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from api.routes import agent as agent_routes
from api.routes import runs
from engine.agent.contracts import DATA_RECIPE_FILENAME
from engine.jobs import CancelToken
from engine.llm import FakeLLMClient, FakeLLMMode
from engine.storage import LocalStorage, run_key
from tests.fixtures.agent_bench.make_messy import messy_frame
from tests.fixtures.make_data import GenerationSpec, generate
from tests.integration.agent.test_recipe_runs import _messy, _recipe, _seed_model

pytestmark = pytest.mark.integration

USE_CASE = "targeted-advertisement"


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


def _upload(client: TestClient, frame: Any, mode: str = "train") -> str:
    response = client.post(
        "/uploads",
        files={"file": ("history.csv", frame.to_csv(index=False).encode(), "text/csv")},
        data={"use_case": USE_CASE, "mode": mode},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["upload_id"])


def _start(client: TestClient, upload_id: str, use_case: str = USE_CASE) -> Any:
    return client.post(f"/uploads/{upload_id}/agent-session", json={"use_case": use_case})


def _session(response: Any) -> dict[str, Any]:
    assert response.status_code in {200, 201}, response.text
    return dict(response.json()["session"])


def _decide_everything(client: TestClient, upload_id: str) -> dict[str, Any]:
    """Accept what the helper is sure of, hide the leaky column, reject the rest."""
    session = _session(
        client.post(f"/uploads/{upload_id}/agent-session/decisions", json={"accept_recommended": True})
    )
    for question in session["questions"]:
        if question["answer"] is None:
            session = _session(
                client.post(
                    f"/uploads/{upload_id}/agent-session/answers",
                    json={
                        "question_id": question["question_id"],
                        "option_id": question["options"][0]["option_id"],
                    },
                )
            )
    pending = [p["proposal_id"] for p in session["proposals"] if p["state"] == "pending"]
    return _session(
        client.post(
            f"/uploads/{upload_id}/agent-session/decisions",
            json={"decisions": [{"proposal_id": pid, "state": "rejected"} for pid in pending]},
        )
    )


def test_starting_suggests_and_changes_nothing(client: TestClient, data_dir: Path) -> None:
    upload_id = _upload(client, messy_frame())
    storage = LocalStorage(data_dir)
    before = sorted(storage.list_keys("uploads/"))
    response = _start(client, upload_id)
    assert response.status_code == 201
    session = _session(response)
    assert session["status"] == "needs_review"
    assert session["agent_name"] == "Targeted Advertisement helper"
    assert any(p["title"] == "Turn 'monthly_spend' into numbers" for p in session["proposals"])
    assert all(p["state"] == "pending" for p in session["proposals"])
    assert response.json()["chat"]["backend"] == "fake"
    after = sorted(storage.list_keys("uploads/"))
    assert after == sorted([*before, f"uploads/{upload_id}/agent/agent_session.json"])
    assert client.get(f"/uploads/{upload_id}/agent-session").json()["session"] == session


def test_nothing_is_approved_while_anything_is_undecided(client: TestClient) -> None:
    upload_id = _upload(client, messy_frame())
    _start(client, upload_id)
    response = client.post(f"/uploads/{upload_id}/agent-session/apply")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "AGENT_UNDECIDED"


def test_approve_prepares_a_copy_that_trains(client: TestClient, data_dir: Path) -> None:
    upload_id = _upload(client, messy_frame())
    _start(client, upload_id)
    session = _decide_everything(client, upload_id)
    assert session["status"] == "ready"
    assert "Hide 'campaign_result_score'" in session["summary"]["decisions"]

    preview = client.post(f"/uploads/{upload_id}/agent-session/preview").json()
    assert "campaign_result_score" in preview["columns_before"]
    assert "campaign_result_score" not in preview["columns_after"]
    email = preview["columns_before"].index("contact_email")
    assert {row[email] for row in preview["rows_before"]} == {"[personal data]"}
    spend = preview["columns_after"].index("monthly_spend")
    assert all(float(row[spend]) > 0 for row in preview["rows_after"])

    storage = LocalStorage(data_dir)
    original = {
        key: storage.read_bytes(key)
        for key in storage.list_keys(f"uploads/{upload_id}/")
        if "/agent/" not in key
    }
    applied = client.post(f"/uploads/{upload_id}/agent-session/apply")
    assert applied.status_code == 200, applied.text
    body = applied.json()
    assert body["upload_id"] != upload_id
    assert (body["primary_key"], body["target"]) == ("customer_id", "converted_30d")
    assert body["receipt"]["rows_in"] == body["receipt"]["rows_out"] == 3_000
    assert {k: storage.read_bytes(k) for k in original} == original  # the upload a person sent is untouched

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
    assert storage.exists(run_key(run.json()["run_id"], DATA_RECIPE_FILENAME))

    again = client.post(f"/uploads/{upload_id}/agent-session/apply")
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "AGENT_SESSION_APPLIED"


def test_a_file_that_cannot_work_stops_and_cannot_be_approved(client: TestClient) -> None:
    upload_id = _upload(client, generate(GenerationSpec(use_case_id=USE_CASE, rows=900)))
    session = _session(_start(client, upload_id))
    assert session["status"] == "stopped"
    assert "At least" in session["stop_reason"]  # the first check that cannot pass, in its own words
    response = client.post(f"/uploads/{upload_id}/agent-session/apply")
    assert response.json()["detail"]["code"] == "AGENT_SESSION_STOPPED"


def test_chat_answers_from_the_data_and_suggests_settings_to_approve(client: TestClient) -> None:
    upload_id = _upload(client, messy_frame())
    _start(client, upload_id)
    session = _session(
        client.post(
            f"/uploads/{upload_id}/agent-session/messages", json={"text": "What about 'monthly_spend'?"}
        )
    )
    assert [m["role"] for m in session["transcript"]] == ["user", "agent"]
    assert "different values" in session["transcript"][-1]["text"]
    assert session["llm_calls"] == 2
    before = len(session["proposals"])
    session = _session(
        client.post(f"/uploads/{upload_id}/agent-session/messages", json={"text": "Make training faster"})
    )
    new = session["proposals"][before:]
    assert [(p["path"], p["value"], p["state"]) for p in new] == [
        ("model_search.strategy", "fast", "pending")
    ]


def test_an_invented_number_never_reaches_the_screen(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        agent_routes, "build_client", lambda *_a, **_k: FakeLLMClient(mode=FakeLLMMode.UNGROUNDED)
    )
    upload_id = _upload(client, messy_frame())
    _start(client, upload_id)
    session = _session(
        client.post(f"/uploads/{upload_id}/agent-session/messages", json={"text": "How many will buy?"})
    )
    reply = session["transcript"][-1]["text"]
    assert "98,765" not in reply
    assert reply.startswith("I could not answer that")


def test_a_use_case_without_a_helper_is_refused(client: TestClient) -> None:
    upload_id = _upload(client, messy_frame())
    response = _start(client, upload_id, use_case="ai-onboarding-assistant")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "AGENT_NOT_AVAILABLE"


def test_reading_a_session_that_was_never_started_is_404(client: TestClient) -> None:
    upload_id = _upload(client, messy_frame(rows=300))
    assert client.get(f"/uploads/{upload_id}/agent-session").status_code == 404


def test_scoring_setup_stops_when_the_models_recipe_does_not_fit(client: TestClient, data_dir: Path) -> None:
    _seed_model(data_dir, _recipe(_messy(), target=None))
    fits = _upload(client, _messy("scoring", rows=600), mode="score")
    session = _session(_start(client, fits))
    assert session["status"] == "needs_review"
    assert [p["path"] for p in session["proposals"]] == ["primary_key"]
    broken = _upload(client, _messy("scoring", rows=600).rename(columns={"ad_ctr_90d": "ctr"}), mode="score")
    stopped = _session(_start(client, broken))
    assert stopped["status"] == "stopped"
    assert "ad_ctr_90d" in stopped["stop_reason"]
