"""The canary through the real API (Plan G §7.6): upload a file full of fake personal data, chat with
the helper until every tool has been called on every column, and search everything that was sent to
the model, returned to the person and written to the session file.

`tests/unit/agent/test_egress_canary.py` runs the same file through `chat_turn` directly; this one
adds the routes, the stored session file and the response models (`sent`, `data_access`, `third_party`).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from api.routes import runs
from api.routes.uploads import use_case_config
from engine import ai_service
from engine.agent.config import DataAccess
from engine.agent.egress import EGRESS_LATE_MASK, MAX_SENT_ITEMS, MAX_SENT_SESSION_CHARS
from engine.config import UseCaseConfig
from engine.jobs import CancelToken
from engine.storage import LocalStorage
from tests.unit.agent.egress_canary import (
    CELL_WORDS,
    HARD_SECRETS,
    HIDE_ALL_TEXT,
    ToolingClient,
    canary_frame,
    every_call,
    leaks,
)

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


def _configure(monkeypatch: pytest.MonkeyPatch, mode: str, always_hide: tuple[str, ...]) -> None:
    real = use_case_config

    def with_agent(use_case_id: str, root: Path) -> UseCaseConfig:
        config = real(use_case_id, root)
        agent = config.agent.model_copy(
            update={
                "ai_data_access": DataAccess(mode),
                "always_hide_columns": always_hide,
                "max_llm_calls_per_session": 500,
                "max_tool_steps_per_turn": 20,
            }
        )
        return config.model_copy(update={"agent": agent})

    monkeypatch.setattr("api.routes.agent.use_case_config", with_agent)


def _chat_everything(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, mode: str, always_hide: tuple[str, ...]
) -> tuple[str, ToolingClient, list[dict[str, Any]]]:
    _configure(monkeypatch, mode, always_hide)
    frame = canary_frame(1_200)
    fake = ToolingClient(every_call([str(c) for c in frame.columns]))
    monkeypatch.setattr(ai_service, "build_client", lambda *_a, **_k: fake)
    uploaded = client.post(
        "/uploads",
        files={"file": ("history.csv", frame.to_csv(index=False).encode(), "text/csv")},
        data={"use_case": USE_CASE, "mode": "train"},
    )
    assert uploaded.status_code == 201, uploaded.text
    upload_id = str(uploaded.json()["upload_id"])
    started = client.post(f"/uploads/{upload_id}/agent-session", json={"use_case": USE_CASE})
    assert started.status_code == 201, started.text
    responses: list[dict[str, Any]] = []
    for _ in range(100):
        if not fake.queue:
            break
        reply = client.post(
            f"/uploads/{upload_id}/agent-session/messages", json={"text": "Please look at every column."}
        )
        assert reply.status_code == 200, reply.text
        responses.append(reply.json())
    assert not fake.queue
    return upload_id, fake, responses


@pytest.mark.parametrize(
    ("mode", "always_hide"),
    [("masked_data", ()), ("masked_data", HIDE_ALL_TEXT), ("summaries_only", ())],
    ids=["masked", "hidden", "summaries"],
)
def test_nothing_planted_reaches_a_prompt_a_response_or_the_session_file(
    client: TestClient,
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    always_hide: tuple[str, ...],
) -> None:
    upload_id, fake, responses = _chat_everything(client, monkeypatch, mode, always_hide)
    prompts = "\n".join(fake.sent_text)
    assert len(fake.calls) > 100

    # What the model was sent.
    assert leaks(prompts, HARD_SECRETS) == []
    # What the person (or a Viewer) is sent back for every message, transcript and record of `sent` alike.
    last = responses[-1]["session"]
    transcript = json.dumps(last["transcript"])
    assert leaks(transcript, HARD_SECRETS) == []
    # What is written to the session file on disk.
    storage = LocalStorage(data_dir)
    key = f"uploads/{upload_id}/agent/agent_session.json"
    stored = json.loads(storage.read_bytes(key))
    assert leaks(json.dumps(stored["transcript"]), HARD_SECRETS) == []
    if mode == "summaries_only" or always_hide:
        assert leaks(prompts, CELL_WORDS, exact=True) == []
        assert leaks(json.dumps(stored["transcript"]), CELL_WORDS, exact=True) == []

    # The gate did the work; the last check never had to.
    codes = [c for m in stored["transcript"] for c in (m.get("turn") or {}).get("error_codes", [])]
    assert EGRESS_LATE_MASK not in codes


def test_the_response_says_what_was_sent_and_how(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    upload_id, _, responses = _chat_everything(client, monkeypatch, "summaries_only", ())
    body = responses[0]
    assert body["chat"]["data_access"] == "summaries_only"
    assert body["chat"]["third_party"] is False
    agent_messages = [m for m in body["session"]["transcript"] if m["role"] == "agent"]
    assert agent_messages
    sent = agent_messages[-1]["sent"]
    assert 0 < len(sent) <= MAX_SENT_ITEMS
    for item in sent:
        assert set(item) == {"tool", "args", "preview", "chars", "mode"}
        assert item["mode"] == "summaries_only"
        assert (
            isinstance(item["args"], dict)
            and isinstance(item["chars"], int)
            and len(item["preview"]) <= 1_500
        )
    user_messages = [m for m in body["session"]["transcript"] if m["role"] == "user"]
    assert all(m["sent"] == [] for m in user_messages)
    everything = client.get(f"/uploads/{upload_id}/agent-session").json()
    kept = [
        i["preview"]
        for m in everything["session"]["transcript"]
        for i in m["sent"]
        if not i["preview"].startswith("[not kept")
    ]
    assert sum(map(len, kept)) <= MAX_SENT_SESSION_CHARS
    assert everything["chat"]["data_access"] == "summaries_only"


def test_a_session_saved_before_this_change_still_loads(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upload_id, _, _ = _chat_everything(client, monkeypatch, "masked_data", ())
    storage = LocalStorage(data_dir)
    key = f"uploads/{upload_id}/agent/agent_session.json"
    stored = json.loads(storage.read_bytes(key))
    for message in stored["transcript"]:
        message.pop("sent", None)
    storage.write_bytes(key, json.dumps(stored).encode())
    old = client.get(f"/uploads/{upload_id}/agent-session")
    assert old.status_code == 200, old.text
    assert all(m["sent"] == [] for m in old.json()["session"]["transcript"])


def test_the_openapi_document_describes_the_new_fields(client: TestClient) -> None:
    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    assert set(schemas["SentItem"]["properties"]) == {"tool", "args", "preview", "chars", "mode"}
    assert "sent" in schemas["ChatMessage"]["properties"]
    assert {"data_access", "third_party"} <= set(schemas["ChatAvailability"]["properties"])
