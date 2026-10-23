"""Plan G review fixes through the API: a malformed model reply is answered, stored and metered."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine import ai_service
from engine.agent.contracts import AgentSession
from engine.agent.loop import UNAVAILABLE
from engine.generative.contracts import LlmUsageReport
from engine.llm import FakeLLMClient, FakeLLMMode, LLMCompletion
from engine.llm_http import PROBLEMS, LLMHttpError
from engine.storage import LocalStorage, upload_key
from tests.fixtures.agent_bench.make_messy import messy_frame

pytestmark = pytest.mark.integration

USE_CASE = "targeted-advertisement"


class _NotAList(FakeLLMClient):
    """Replies with `evidence_ids` as a number: the shape that used to crash the turn with a 500."""

    def complete(self, prompt: str, **kwargs: Any) -> LLMCompletion:
        completion = super().complete(prompt, **kwargs)
        text = json.dumps({"action": "reply", "text": "Nothing to add.", "evidence_ids": 5})
        return completion.model_copy(update={"text": text})


class _KeyExpired(FakeLLMClient):
    """A connected AI service whose key has expired."""

    def complete(self, prompt: str, **kwargs: Any) -> LLMCompletion:
        raise LLMHttpError("key_expired", status=401)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def client(config_root: Path, data_dir: Path) -> Iterator[TestClient]:
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as test_client:
        yield test_client


def test_a_malformed_model_reply_is_answered_and_metered(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _NotAList(mode=FakeLLMMode.GROUNDED)
    monkeypatch.setattr(ai_service, "build_client", lambda *_a, **_k: fake)
    response = client.post(
        "/uploads",
        files={"file": ("history.csv", messy_frame().to_csv(index=False).encode(), "text/csv")},
        data={"use_case": USE_CASE, "mode": "train"},
    )
    assert response.status_code == 201, response.text
    upload_id = response.json()["upload_id"]
    client.post(f"/uploads/{upload_id}/agent-session", json={"use_case": USE_CASE})
    for _ in range(2):
        reply = client.post(f"/uploads/{upload_id}/agent-session/messages", json={"text": "hi"})
        assert reply.status_code == 200, reply.text
        assert reply.json()["session"]["transcript"][-1]["text"] == "Nothing to add."
    storage = LocalStorage(data_dir)
    session = storage.read_model(upload_key(upload_id, "agent/agent_session.json"), AgentSession)
    usage = storage.read_model(upload_key(upload_id, "agent/llm_usage.json"), LlmUsageReport)
    assert session.llm_calls == len(fake.calls) == usage.totals.calls == 2
    assert len(session.transcript) == 4
    assert session.transcript[-1].evidence_ids == ()


def test_a_chat_the_ai_service_could_not_answer_carries_why_in_the_reply_and_its_turn_log(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ai_service, "build_client", lambda *_a, **_k: _KeyExpired(mode=FakeLLMMode.GROUNDED))
    response = client.post(
        "/uploads",
        files={"file": ("history.csv", messy_frame().to_csv(index=False).encode(), "text/csv")},
        data={"use_case": USE_CASE, "mode": "train"},
    )
    assert response.status_code == 201, response.text
    upload_id = response.json()["upload_id"]
    client.post(f"/uploads/{upload_id}/agent-session", json={"use_case": USE_CASE})
    reply = client.post(f"/uploads/{upload_id}/agent-session/messages", json={"text": "hi"})
    assert reply.status_code == 200, reply.text
    sentence, fix = PROBLEMS["key_expired"]
    message = reply.json()["session"]["transcript"][-1]
    assert message["text"] == f"{UNAVAILABLE} {sentence} {fix}"
    assert message["turn"]["blocked_by"] == "unavailable"
    assert message["turn"]["error_codes"] == ["LLM_UNAVAILABLE", "LLM_KEY_EXPIRED"]
