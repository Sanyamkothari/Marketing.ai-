"""Ask your data through the API (Plan I, DEC-1250 … DEC-1259): `GET/DELETE /uploads/{id}/ask` and
`POST /uploads/{id}/ask/messages`, against the recording fake model the agent tests use.

What it proves: a question opens a read-only explore session beside (never instead of) Guided setup's; a
reply whose turn grouped the rows comes back with that tool result's groups as a chart, and the chart's
numbers are the stored tool result's; a model that tries to suggest a setting is refused and nothing is
proposed; with no AI service connected the chat says so and a question is `409 AI_NOT_CONNECTED` with nothing
stored; and the routes keep the access rule of their neighbours.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from api.access_policy import policy_for
from api.main import create_app
from engine import ai_service
from engine.access.roles import Role
from engine.settings import Settings
from engine.storage import LocalStorage
from tests.unit.agent.egress_canary import ToolingClient

pytestmark = pytest.mark.integration

USE_CASE = "targeted-advertisement"


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def client(config_root: Path, data_dir: Path) -> Iterator[TestClient]:
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as test_client:
        yield test_client


def ask_frame(rows: int = 600) -> pd.DataFrame:
    """Customers in four regions with a yes/no outcome and a spend: what "churn rate by region" reads."""
    regions = ["North", "South", "East", "West"]
    return pd.DataFrame(
        {
            "customer_id": [f"C{i:05d}" for i in range(rows)],
            "region": [regions[i % 4] for i in range(rows)],
            "monthly_spend": [round(10 + (i * 37) % 500 + (i % 7) / 10, 2) for i in range(rows)],
            "tenure_months": [1 + (i * 13) % 60 for i in range(rows)],
            "converted_30d": [1 if (i * 7) % 10 < 3 + (i % 4) else 0 for i in range(rows)],
        }
    )


def upload(client: TestClient, frame: pd.DataFrame | None = None) -> str:
    response = client.post(
        "/uploads",
        files={
            "file": (
                "customers.csv",
                (ask_frame() if frame is None else frame).to_csv(index=False).encode(),
                "text/csv",
            )
        },
        data={"use_case": USE_CASE, "mode": "train"},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["upload_id"])


def _ok(response: Any, status: int = 200) -> dict[str, Any]:
    assert response.status_code == status, response.text
    return dict(response.json())


def scripted(
    monkeypatch: pytest.MonkeyPatch, actions: list[dict[str, Any]], per_turn: int = 6
) -> ToolingClient:
    fake = ToolingClient(actions, per_turn=per_turn)
    monkeypatch.setattr(ai_service, "build_client", lambda *_a, **_k: fake)
    return fake


def test_before_the_first_question_there_is_no_session_and_the_chat_is_on(client: TestClient) -> None:
    upload_id = upload(client)
    body = _ok(client.get(f"/uploads/{upload_id}/ask"))
    assert body["session"] is None and body["charts"] == []
    assert body["chat"]["available"] is True and body["chat"]["backend"] == "fake"


def test_a_question_answers_with_a_chart_read_from_the_tool_result(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upload_id = upload(client)
    scripted(
        monkeypatch, [{"action": "rate_by", "args": {"column": "region", "outcome_column": "converted_30d"}}]
    )
    body = _ok(client.post(f"/uploads/{upload_id}/ask/messages", json={"text": "Conversion rate by region"}))
    session = body["session"]
    assert session["explore"] is True and session["status"] == "ready"
    assert session["proposals"] == [] and session["questions"] == []
    assert [m["role"] for m in session["transcript"]] == ["user", "agent"]
    stored = next(r for r in session["tool_results"] if r["tool"] == "rate_by")
    [chart] = body["charts"]
    assert chart["message_index"] == 1 and chart["evidence_id"] == stored["evidence_id"]
    assert chart["function"] == "positive_rate" and chart["outcome_column"] == "converted_30d"
    assert [(b["label"], b["rows"], b["value"]) for b in chart["bars"]] == [
        (g["group"], g["rows"], g["positive_rate"]) for g in stored["result"]["groups"]
    ]
    assert {b["label"] for b in chart["bars"]} == {"North", "South", "East", "West"}
    # Its own file, beside Guided setup's (which was never started), and read back the same.
    keys = set(LocalStorage(data_dir).list_keys(f"uploads/{upload_id}/agent/"))
    assert f"uploads/{upload_id}/agent/explore_session.json" in keys
    assert f"uploads/{upload_id}/agent/agent_session.json" not in keys
    assert _ok(client.get(f"/uploads/{upload_id}/ask")) == body


def test_the_default_fake_model_groups_and_rates_by_quoted_columns(client: TestClient) -> None:
    upload_id = upload(client)
    body = _ok(
        client.post(f"/uploads/{upload_id}/ask/messages", json={"text": "Show 'converted_30d' by 'region'"})
    )
    assert (
        body["session"]["transcript"][-1]["text"]
        == "I grouped the rows by 'region'; the chart shows each group."
    )
    assert [c["function"] for c in body["charts"]] == ["positive_rate"]


def test_the_helper_cannot_suggest_a_setting_here(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    upload_id = upload(client)
    fake = scripted(
        monkeypatch,
        [
            {
                "action": "propose_setting",
                "args": {"path": "model_search.strategy", "value": "fast", "reason": "x"},
            }
        ],
    )
    body = _ok(client.post(f"/uploads/{upload_id}/ask/messages", json={"text": "Make training faster"}))
    session = body["session"]
    assert session["proposals"] == []
    assert "AGENT_EXPLORE_READ_ONLY" in session["transcript"][-1]["turn"]["error_codes"]
    assert all("model_search.strategy" not in text for text in fake.sent_text[:1])


def test_a_new_chat_deletes_the_old_one(client: TestClient, data_dir: Path) -> None:
    upload_id = upload(client)
    _ok(client.post(f"/uploads/{upload_id}/ask/messages", json={"text": "What is in the file?"}))
    body = _ok(client.delete(f"/uploads/{upload_id}/ask"))
    assert body["session"] is None
    assert not LocalStorage(data_dir).exists(f"uploads/{upload_id}/agent/explore_session.json")


def test_unknown_uploads_and_use_cases_without_a_helper_are_refused(client: TestClient) -> None:
    assert client.get("/uploads/u_missing/ask").status_code == 404
    response = client.post(
        "/uploads",
        files={"file": ("q.csv", b"question,answer\nhi,there\n", "text/csv")},
        data={"use_case": "ai-onboarding-assistant", "mode": "train"},
    )
    if response.status_code == 201:  # a generative use case may refuse the upload itself; either is fine
        upload_id = response.json()["upload_id"]
        assert client.get(f"/uploads/{upload_id}/ask").json()["detail"]["code"] == "AGENT_NOT_AVAILABLE"


def test_without_an_ai_service_the_chat_says_so_and_a_question_changes_nothing(
    config_root: Path, data_dir: Path
) -> None:
    settings = Settings(connections_key=SecretStr("F" * 40), allow_fake_ai=False)
    with TestClient(create_app(config_root=config_root, data_dir=data_dir, settings=settings)) as off:
        upload_id = upload(off)
        chat = _ok(off.get(f"/uploads/{upload_id}/ask"))["chat"]
        assert (chat["available"], chat["reason"]) == (False, "AI_NOT_CONNECTED")
        response = off.post(f"/uploads/{upload_id}/ask/messages", json={"text": "Churn rate by region"})
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "AI_NOT_CONNECTED"
        assert not LocalStorage(data_dir).exists(f"uploads/{upload_id}/agent/explore_session.json")


def test_the_routes_keep_their_neighbours_access_rule() -> None:
    read = policy_for("GET", "/uploads/{upload_id}/ask")
    ask = policy_for("POST", "/uploads/{upload_id}/ask/messages")
    clear = policy_for("DELETE", "/uploads/{upload_id}/ask")
    assert read is not None and read.role is Role.VIEWER
    assert (
        ask is not None and ask.role is policy_for("POST", "/uploads/{upload_id}/agent-session/messages").role
    )
    assert clear is not None and clear.role is Role.ANALYST
    assert json.loads(read.model_dump_json())["object_param"] == "upload_id"
