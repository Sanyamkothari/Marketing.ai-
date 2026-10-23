"""A model that reasons, a key that expires, and the connection test - end to end, on real bytes.

Everything here runs through the shipped path with nothing stubbed between the browser and the wire: the
Data Doctor's chat route builds its guarded meter from the saved AI service, which builds the real
`OpenAICompatibleClient`, which talks to a fake service on a real local socket (`tests/unit/ai_service/server.py`).
The unit tests pin each piece (the allowance a purpose is given, the kind an empty answer is, the codes a turn
logs); these pin that the pieces meet, because the original failure sat in the joins:

- A reasoning model spent the 800 tokens a Data Doctor turn was allowed on thinking and answered with nothing
  (`finish_reason: length`), the connection test (16 tokens, "no text is fine") called that connected, and the
  person met "the AI service is not answering" with no way to tell why.
- An expired key was the same sentence as a server that was down.

Each test names what reverting its fix would do, in the places it says "without ...".
"""

from __future__ import annotations

import io
import logging
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from api.main import create_app
from engine.agent.loop import UNAVAILABLE
from engine.ai_service import TEST_MAX_TOKENS
from engine.config import LlmConfig
from engine.doctor import case as doctor_case
from engine.llm_http import PROBLEMS
from engine.settings import Settings
from tests.fixtures.make_data import GenerationSpec, generate
from tests.unit.ai_service.server import Reply, Sent, chat_reply, serve

pytestmark = pytest.mark.integration

GOAL = "targeted-advertisement"
KEY = "zk-test-key-0123456789abcdef"
THINKING_TOKENS = 800
"""What a reasoning model spends thinking before it writes: the ordinary allowance is all of it."""
DEFAULT_ALLOWANCE = LlmConfig().data_agent_max_output_tokens
ANSWER = "I have looked at your file and I am ready for your questions."
PROVIDER_WORDS = "API key expired. Please renew."


def _stopped_while_thinking(allowance: int) -> Reply:
    """An answer with no text: the whole allowance went on thinking, and the provider billed for it."""
    return Reply(
        200,
        {
            "choices": [{"message": {"role": "assistant", "content": ""}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 40, "completion_tokens": allowance},
        },
    )


def _allowance_sent(sent: Sent) -> int:
    body = sent.body
    return int(body.get("max_completion_tokens") or body["max_tokens"])


def _reasoning_model(sent: Sent) -> Reply:
    """A model that needs more than 800 tokens to get as far as writing: below that, it only thinks."""
    allowance = _allowance_sent(sent)
    if allowance <= THINKING_TOKENS:
        return _stopped_while_thinking(allowance)
    return chat_reply(ANSWER, prompt_tokens=40, completion_tokens=THINKING_TOKENS + 12)


@pytest.fixture(autouse=True)
def _unified(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor_case, "data_doctor_unified", lambda root=None: True)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


def _configs(tmp_path: Path, source: Path, *, allowance: int) -> Path:
    """A copy of `configs/` whose `defaults.generative.llm.data_agent_max_output_tokens` is `allowance`.

    That is where an operator sets it: every use case file inherits the engine's defaults, and the Data Doctor
    meters its calls with the first use case's.
    """
    root = tmp_path / "configs"
    shutil.copytree(source, root)
    path = root / "engine.yaml"
    text = path.read_text(encoding="utf-8")
    shipped = f"data_agent_max_output_tokens: {DEFAULT_ALLOWANCE}"
    assert text.count(shipped) == 1, "engine.yaml no longer spells the setting the way this test edits it"
    path.write_text(text.replace(shipped, f"data_agent_max_output_tokens: {allowance}"), encoding="utf-8")
    return root


def _app(config_root: Path, data_dir: Path) -> TestClient:
    settings = Settings(connections_key=SecretStr("F" * 40), allow_fake_ai=False)
    return TestClient(create_app(config_root=config_root, data_dir=data_dir, settings=settings))


def _connect(client: TestClient, url: str, *, model: str = "local-m") -> None:
    saved = client.put(
        "/ai-service/product",
        json={"provider": "openai_compatible", "model": model, "base_url": url + "/v1", "api_key": KEY},
    )
    assert saved.status_code == 200, saved.text


def _open_case(client: TestClient) -> dict[str, Any]:
    """A case with one file, its blocking questions answered and the client's yes to the AI given."""
    buffer = io.StringIO()
    generate(GenerationSpec(use_case_id=GOAL, rows=600, seed=3)).to_csv(buffer, index=False)
    uploaded = client.post(
        "/uploads",
        files={"file": ("customers.csv", buffer.getvalue().encode(), "text/csv")},
        data={"use_case": GOAL, "mode": "train"},
    )
    assert uploaded.status_code == 201, uploaded.text
    case = client.post("/cases", json={"upload_ids": [uploaded.json()["upload_id"]]}).json()
    case = client.post(f"/cases/{case['case_id']}/visit", json={}).json()
    while case["state"] == "ask":
        pending = next(q for q in case["questions"] if q["blocking"] and q["answer"] is None)
        answered = client.post(
            f"/cases/{case['case_id']}/answers",
            json={"version": case["version"], "question_id": pending["question_id"], "option_id": "c0"},
        )
        assert answered.status_code == 200, answered.text
        case = answered.json()
    assert client.post(f"/cases/{case['case_id']}/ai-consent", json={}).status_code == 200
    return dict(case)


def _chat(client: TestClient, case: dict[str, Any], message: str = "Hello") -> dict[str, Any]:
    response = client.post(
        f"/cases/{case['case_id']}/chat", json={"version": case["version"], "message": message}
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


def _agent_events(client: TestClient, case_id: str) -> list[dict[str, Any]]:
    events = client.get(f"/cases/{case_id}/timeline").json()["events"]
    return [e for e in events if e["kind"] == "chat.message" and e["details"].get("role") == "agent"]


class _Log(logging.Handler):
    """Everything logged while it is attached, formatted the way a log file would show it (traceback and all)."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(logging.Formatter().format(record))


@pytest.fixture
def log() -> Iterator[_Log]:
    """Attached after the app is made: `create_app` configures the process's logging and would drop an earlier one."""
    handler = _Log()
    root = logging.getLogger()
    level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        root.removeHandler(handler)
        root.setLevel(level)


# --- a model that reasons, in the Data Doctor's chat ---------------------------------------------------------
def test_a_reasoning_model_answers_a_doctor_turn_because_the_turn_is_given_the_data_agents_allowance(
    config_root: Path, data_dir: Path
) -> None:
    """The original failure: with 800 tokens this model writes nothing; the Data Doctor now asks with 4096.

    Without the data agent's own allowance (the meter sending `max_output_tokens`), the fake service sees 800,
    answers with nothing, and the turn is `unavailable` - the assertions on the reply and on `blocked_by` fail.
    """
    assert LlmConfig().max_output_tokens <= THINKING_TOKENS < DEFAULT_ALLOWANCE, "the scenario needs this gap"
    with serve(_reasoning_model) as server, _app(config_root, data_dir) as client:
        _connect(client, server.url)
        case = _open_case(client)
        chatted = _chat(client, case)
        reply = chatted["chat"][-1]
        assert reply["role"] == "agent" and reply["text"] == ANSWER
        assert not reply["text"].startswith(UNAVAILABLE)
        assert "blocked_by" not in reply and "error_codes" not in reply, "left out while the AI answered"
        assert server.requests, "the AI service was asked"
        assert {_allowance_sent(r) for r in server.requests} == {DEFAULT_ALLOWANCE}
        (event,) = _agent_events(client, case["case_id"])
        assert event["details"]["ai"] is True and "blocked_by" not in event["details"]
        assert chatted["counters"]["model_calls"] == len(server.requests)


def test_with_the_allowance_set_to_the_old_800_the_same_turn_fails_and_says_why_and_how_to_fix_it(
    config_root: Path, data_dir: Path, tmp_path: Path
) -> None:
    """An operator who keeps the old allowance gets a reason, not "not answering": and raising it cures the turn.

    Without the `out_of_room` kind (the empty answer left a bare `LLM_REFUSED`), the codes lack
    `LLM_OUT_OF_ROOM` and the reply lacks the sentence; without the codes reaching the chat entry, the response
    carries neither `blocked_by` nor `error_codes`; without counting the call, `model_calls` stays 0.
    """
    sentence, fix = PROBLEMS["out_of_room"]
    small = _configs(tmp_path, config_root, allowance=THINKING_TOKENS)
    with serve(_reasoning_model) as server, _app(small, data_dir) as client:
        _connect(client, server.url)
        case = _open_case(client)
        chatted = _chat(client, case)
        reply = chatted["chat"][-1]
        assert reply["text"] == f"{UNAVAILABLE} {sentence} {fix}"
        assert reply["blocked_by"] == "unavailable"
        assert {"LLM_REFUSED", "LLM_OUT_OF_ROOM"} <= set(reply["error_codes"])
        asked = len(server.requests)
        assert asked >= 1 and {_allowance_sent(r) for r in server.requests} == {THINKING_TOKENS}
        assert chatted["counters"]["model_calls"] == asked, "each was billed, so each counts against the case"
        assert chatted["counters"]["tokens"] == asked * (
            40 + THINKING_TOKENS
        ), "from what the provider reported"
        (event,) = _agent_events(client, case["case_id"])
        assert event["details"]["blocked_by"] == "unavailable"
        assert "LLM_OUT_OF_ROOM" in event["details"]["error_codes"].split(",")
        assert PROVIDER_WORDS not in client.get(f"/cases/{case['case_id']}").text
    # The fix it printed names the setting; setting it is all it takes.
    assert "generative.llm.data_agent_max_output_tokens" in fix
    roomy = _configs(tmp_path / "raised", config_root, allowance=DEFAULT_ALLOWANCE)
    with serve(_reasoning_model) as server, _app(roomy, data_dir / "raised") as client:
        _connect(client, server.url)
        assert _chat(client, _open_case(client))["chat"][-1]["text"] == ANSWER


# --- an expired key, in the Data Doctor's chat ---------------------------------------------------------------
def test_an_expired_key_is_told_apart_from_an_unreachable_service_and_the_providers_words_stay_out(
    config_root: Path, data_dir: Path, log: _Log
) -> None:
    """HTTP 401 with "expired" in the body: our sentence and fix for it, our codes, and none of the provider's words.

    Without the body being read for the cause the kind is `key_rejected` (the codes end `LLM_KEY_REJECTED` and the
    sentence says "rejected"); without the codes reaching the entry the response has none; and a reply that quoted
    the provider would carry "renew".
    """
    sentence, fix = PROBLEMS["key_expired"]
    body = {"error": {"message": PROVIDER_WORDS}}
    with serve(lambda _: Reply(401, body)) as server, _app(config_root, data_dir) as client:
        log.lines.clear()
        _connect(client, server.url)
        case = _open_case(client)
        chatted = _chat(client, case)
        reply = chatted["chat"][-1]
        assert reply["text"] == f"{UNAVAILABLE} {sentence} {fix}"
        assert reply["blocked_by"] == "unavailable"
        assert reply["error_codes"] == ["LLM_UNAVAILABLE", "LLM_KEY_EXPIRED"]
        failed = [line for line in log.lines if "the AI call failed" in line]
        assert (
            len(server.requests) == len(failed) >= 1
        ), "each call that failed was one request: none was retried"
        assert chatted["counters"]["model_calls"] == 0, "nothing was billed"
        (event,) = _agent_events(client, case["case_id"])
        assert event["details"]["blocked_by"] == "unavailable"
        assert event["details"]["error_codes"] == "LLM_UNAVAILABLE,LLM_KEY_EXPIRED"
        everything = "\n".join(
            [
                str(chatted),
                client.get(f"/cases/{case['case_id']}").text,
                client.get(f"/cases/{case['case_id']}/timeline").text,
                *log.lines,
            ]
        )
    assert any("LLM_KEY_EXPIRED" in line for line in log.lines), "the failure was logged, by our code"
    for words in ("Please renew", "API key expired", KEY):
        assert words not in everything, f"{words!r} reached a person or a log"


# --- the connection test ----------------------------------------------------------------------------------
def test_the_connection_test_of_a_service_that_only_thinks_fails_with_the_out_of_room_sentence_and_its_own_fix(
    config_root: Path, data_dir: Path
) -> None:
    """An empty answer stopped by the token ceiling is not "Connected": the sentence is the client's, the fix the test's.

    Without `out_of_room` the sentence is "answered with no text"; without the test's own fix it is the client's
    "raise generative.llm.data_agent_max_output_tokens", which cannot help the test (it always sends the default).
    """
    sentence, client_fix = PROBLEMS["out_of_room"]
    with (
        serve(lambda sent: _stopped_while_thinking(_allowance_sent(sent))) as server,
        _app(config_root, data_dir) as client,
    ):
        _connect(client, server.url)
        result = client.post("/ai-service/product/test")
        saved = client.get("/ai-service").json()["slots"]["product"]
    body = result.json()
    assert result.status_code == 200 and body["ok"] is False
    assert body["message"] == sentence and "answered with no text" not in body["message"]
    assert body["fix"] != client_fix and "without extended reasoning" in body["fix"]
    assert f"{TEST_MAX_TOKENS} tokens" in body["fix"] and "not to this test" in body["fix"]
    assert len(server.requests) == 1 and _allowance_sent(server.requests[0]) == TEST_MAX_TOKENS
    assert saved["last_test"]["ok"] is False and saved["last_test"]["message"] == sentence
    assert saved["connected"] is True, "a failed test is shown; it does not disconnect"
    assert KEY not in result.text


def test_the_connection_test_of_a_reasoning_model_passes_because_it_asks_with_room_to_think(
    config_root: Path, data_dir: Path
) -> None:
    """The same service as the chat's: empty at 800, an answer at the data agent's allowance - and the test sends the latter.

    Without the test's own allowance (the old 16 tokens), the service answers with nothing and the test fails a
    service that the chat works with.
    """
    with serve(_reasoning_model) as server, _app(config_root, data_dir) as client:
        _connect(client, server.url)
        body = client.post("/ai-service/product/test").json()
    assert body["ok"] is True and "Connected" in body["message"]
    assert TEST_MAX_TOKENS == DEFAULT_ALLOWANCE and [_allowance_sent(r) for r in server.requests] == [
        TEST_MAX_TOKENS
    ]
