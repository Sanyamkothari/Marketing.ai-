"""Setup help for new sources through the API (Plan K M142, DEC-1409).

Hinglish and abbreviated headers read by the rules; the optional second opinion from the AI service
(`POST /uploads/{id}/agent-session/assist`); nothing applied without the person's tick.
"""

from __future__ import annotations

import json
import logging
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
from engine.agent.contracts import AgentSession, SessionStatus
from engine.settings import Settings
from engine.storage import LocalStorage
from tests.fixtures.agent_bench.make_hinglish import CASES, hinglish_frame
from tests.unit.agent.assist_client import AssistClient

pytestmark = pytest.mark.integration

USE_CASE = "targeted-advertisement"


@pytest.fixture(autouse=True)
def _restore_root_logging() -> Iterator[None]:
    """`create_app(settings=...)` configures the process's logging; a test is not a process."""
    root = logging.getLogger()
    handlers = list(root.handlers)
    state = [(handler, list(handler.filters), handler.formatter) for handler in handlers]
    level = root.level
    try:
        yield
    finally:
        root.handlers = handlers
        root.setLevel(level)
        for handler, filters, formatter in state:
            handler.filters = filters
            handler.setFormatter(formatter)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def client(config_root: Path, data_dir: Path) -> Iterator[TestClient]:
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as test_client:
        yield test_client


def _upload(client: TestClient, frame: pd.DataFrame) -> str:
    response = client.post(
        "/uploads",
        files={"file": ("export.csv", frame.to_csv(index=False).encode(), "text/csv")},
        data={"use_case": USE_CASE, "mode": "train"},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["upload_id"])


def _case(name: str) -> pd.DataFrame:
    return hinglish_frame(next(spec for spec in CASES if spec.name == name))


def _start(client: TestClient, upload_id: str) -> dict[str, Any]:
    response = client.post(f"/uploads/{upload_id}/agent-session", json={"use_case": USE_CASE})
    assert response.status_code == 201, response.text
    return dict(response.json()["session"])


def _two_outcomes() -> pd.DataFrame:
    rows = 800
    return pd.DataFrame(
        {
            "customer_id": [f"C{i}" for i in range(rows)],
            "churn_flg": [1 if i % 4 == 0 else 0 for i in range(rows)],
            "kharida_flag": [1 if i % 3 == 0 else 0 for i in range(rows)],
            "final_status": [("open", "closed", "pending")[i % 3] for i in range(rows)],
            "monthly_spend": [float(10 + i % 90) for i in range(rows)],
        }
    )


def test_a_hinglish_export_gets_check_suggestions_that_nobody_ticked(client: TestClient) -> None:
    """The ID and the outcome come from the headers (`grahak_id`, `kharida_30d`), each "please check"."""
    session = _start(client, _upload(client, _case("telco_hinglish")))
    roles = {p["path"]: p for p in session["proposals"] if p["kind"] == "role"}
    assert (roles["primary_key"]["value"], roles["primary_key"]["confidence"]) == ("grahak_id", "check")
    assert (roles["target"]["value"], roles["target"]["confidence"]) == ("kharida_30d", "check")
    assert roles["primary_key"]["from_headers"] is True and roles["target"]["from_headers"] is True
    assert all(p["state"] == "pending" for p in session["proposals"])
    # A suggestion that did not come from a header does not carry the flag at all (the stored session is unchanged).
    assert all(
        "from_headers" not in p for p in session["proposals"] if p["path"] not in {"primary_key", "target"}
    )


def test_accepting_the_recommended_suggestions_does_not_tick_a_header_read_column(
    client: TestClient,
) -> None:
    upload_id = _upload(client, _case("telco_hinglish"))
    _start(client, upload_id)
    response = client.post(f"/uploads/{upload_id}/agent-session/decisions", json={"accept_recommended": True})
    assert response.status_code == 200, response.text
    roles = [p for p in response.json()["session"]["proposals"] if p["kind"] == "role"]
    assert {p["state"] for p in roles} == {"pending"}  # both are the person's to tick
    assert response.json()["session"]["status"] == "needs_review"


def test_a_file_with_no_outcome_stops_with_a_lapse_draft_nothing_is_applied(
    client: TestClient, data_dir: Path
) -> None:
    upload_id = _upload(client, _case("lapse_no_outcome_telco"))
    session = _start(client, upload_id)
    assert session["status"] == "stopped"
    assert "A draft outcome to start from" in session["stop_reason"]
    draft = next(q for q in session["questions"] if "lapsed" in q["text"])
    assert draft["blocking"] is False and all(o["proposal"] is None for o in draft["options"])
    assert LocalStorage(data_dir).exists(f"uploads/{upload_id}/agent/agent_session.json")
    assert client.post(f"/uploads/{upload_id}/agent-session/apply").json()["detail"]["code"] == (
        "AGENT_SESSION_STOPPED"
    )


def test_the_second_opinion_adds_questions_and_a_new_order_and_changes_no_suggestion(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = AssistClient(
        orders=[{"kind": "outcome", "columns": ["kharida_flag", "churn_flg"]}],
        columns=[{"column": "final_status", "why": "A final status is only known once things end."}],
    )
    monkeypatch.setattr(ai_service, "build_client", lambda *_a, **_k: fake)
    upload_id = _upload(client, _two_outcomes())
    before = _start(client, upload_id)
    ask = next(q for q in before["questions"] if "Which column says" in q["text"])
    assert [o["label"] for o in ask["options"]] == ["churn_flg", "kharida_flag"]
    response = client.post(f"/uploads/{upload_id}/agent-session/assist")
    assert response.status_code == 200, response.text
    session = response.json()["session"]
    assert session["proposals"] == before["proposals"]  # nothing suggested, nothing ticked
    asked = next(q for q in session["questions"] if "Which column says" in q["text"])
    assert [o["label"] for o in asked["options"]] == ["kharida_flag", "churn_flg"]
    review = [q for q in session["questions"] if q["question_id"].startswith("x1-")]
    assert len(review) == 1 and review[0]["blocking"] is False
    assert [o["option_id"] for o in review[0]["options"]] == ["hide", "keep"]
    message = session["transcript"][-1]
    assert message["role"] == "agent" and message["turn"]["llm_calls"] == 2 == session["llm_calls"]
    assert [item["tool"] for item in message["sent"]] == ["rerank_columns", "review_leaks"]
    assert fake.tasks == ["rerank", "leaks"]
    assert LocalStorage(data_dir).exists(f"uploads/{upload_id}/agent/llm_usage.json")
    # Reading it back gives the same session.
    assert client.get(f"/uploads/{upload_id}/agent-session").json()["session"] == session


def test_pressing_hide_on_the_reviewers_question_is_the_only_way_a_column_gets_hidden(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = AssistClient(columns=[{"column": "final_status", "why": "Only known at the end."}])
    monkeypatch.setattr(ai_service, "build_client", lambda *_a, **_k: fake)
    upload_id = _upload(client, _two_outcomes())
    _start(client, upload_id)
    session = client.post(f"/uploads/{upload_id}/agent-session/assist").json()["session"]
    session = client.post(
        f"/uploads/{upload_id}/agent-session/decisions", json={"accept_recommended": True}
    ).json()["session"]
    assert not [p for p in session["proposals"] if p["kind"] == "recipe_step"]
    review = next(q for q in session["questions"] if q["question_id"].startswith("x1-"))
    answered = client.post(
        f"/uploads/{upload_id}/agent-session/answers",
        json={"question_id": review["question_id"], "option_id": "hide"},
    ).json()["session"]
    hidden = [p for p in answered["proposals"] if p["kind"] == "recipe_step"]
    assert [(p["step"]["kind"], p["step"]["column"], p["state"]) for p in hidden] == [
        ("drop_column", "final_status", "accepted")
    ]


def test_without_an_ai_service_the_second_opinion_is_refused_and_nothing_changes(
    config_root: Path, data_dir: Path
) -> None:
    settings = Settings(connections_key=SecretStr("F" * 40), allow_fake_ai=False)
    with TestClient(create_app(config_root=config_root, data_dir=data_dir, settings=settings)) as off:
        upload_id = _upload(off, _two_outcomes())
        before = _start(off, upload_id)
        response = off.post(f"/uploads/{upload_id}/agent-session/assist")
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "AI_NOT_CONNECTED"
        assert off.get(f"/uploads/{upload_id}/agent-session").json()["session"] == before


def test_a_setup_that_was_approved_cannot_be_second_guessed(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = AssistClient()
    monkeypatch.setattr(ai_service, "build_client", lambda *_a, **_k: fake)
    upload_id = _upload(client, _two_outcomes())
    _start(client, upload_id)
    storage = LocalStorage(data_dir)
    key = f"uploads/{upload_id}/agent/agent_session.json"
    stored = storage.read_model(key, AgentSession)
    storage.write_model(
        key, stored.model_copy(update={"status": SessionStatus.APPLIED, "applied_upload_id": "u-derived"})
    )
    response = client.post(f"/uploads/{upload_id}/agent-session/assist")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "AGENT_SESSION_APPLIED"
    assert fake.calls == ()  # refused before the AI service was reached
    assert client.post("/uploads/u-nope/agent-session/assist").status_code == 404


def test_the_route_keeps_the_chat_routes_access_rule() -> None:
    assist = policy_for("POST", "/uploads/{upload_id}/agent-session/assist")
    chat = policy_for("POST", "/uploads/{upload_id}/agent-session/messages")
    assert assist is not None and chat is not None
    assert assist.role is chat.role is Role.ANALYST
    assert json.loads(assist.model_dump_json())["object_param"] == "upload_id"
    assert assist.action == "agent.assist"
