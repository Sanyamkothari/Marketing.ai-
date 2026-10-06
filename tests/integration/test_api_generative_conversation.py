"""`POST /indexes/{id}/ask` as a conversation, and its errors (Plan I, DEC-1280 … DEC-1289).

The request may carry the conversation so far; its shape and size are checked before anything is
asked of a model. A follow-up is rewritten and the rewrite is reported; a provider failure is a
plain coded error, never a `500`; and an index's detail offers starter questions taken from the
reference rows it passed.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from api.schemas import ASK_HISTORY_ANSWER_CHARS, ASK_HISTORY_MAX_TURNS, ASK_HISTORY_QUESTION_CHARS
from engine.generative.errors import PROMPT_NOT_FOUND, generative_error
from engine.llm import LLMError
from engine.llm_http import PROBLEMS, LLMHttpError

pytestmark = pytest.mark.integration

USE_CASE = "ai-onboarding-assistant"
QUESTION = "What is the late payment fee?"
INTERNAL = "KeyError: 'choices' at /srv/app/engine/secret_module.py line 42"


@pytest.fixture(scope="module")
def client(config_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    data_dir = tmp_path_factory.mktemp("conversation") / "data"
    data_dir.mkdir()
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def index_id(client: TestClient) -> str:
    response = client.post(
        f"/use-cases/{USE_CASE}/indexes",
        data={
            "use_sample_documents": "true",
            "use_sample_questions": "true",
            "model_choice": "__automl__",
            "overrides": "{}",
        },
    )
    assert response.status_code == 202, response.text
    built = str(response.json()["index_id"])
    deadline = time.monotonic() + 30
    while client.get(f"/indexes/{built}").json()["status"]["state"] not in ("done", "failed"):
        assert time.monotonic() < deadline
        time.sleep(0.02)
    return built


def _calls(client: TestClient, index_id: str, purpose: str) -> int:
    usage: dict[str, Any] = client.get(f"/indexes/{index_id}").json()["llm_usage"]
    return next((row["calls"] for row in usage["by_purpose"] if row["purpose"] == purpose), 0)


def test_a_first_question_needs_no_history_and_makes_no_rewrite(client: TestClient, index_id: str) -> None:
    before = _calls(client, index_id, "assistant_condense")
    body = client.post(f"/indexes/{index_id}/ask", json={"question": QUESTION}).json()
    assert body["searched_for"] is None and body["attempts"] == 1
    assert body["confidence"]["level"] in ("high", "medium", "low")
    assert _calls(client, index_id, "assistant_condense") == before


def test_a_follow_up_is_rewritten_metered_and_reported(client: TestClient, index_id: str) -> None:
    before = _calls(client, index_id, "assistant_condense")
    history = [{"question": QUESTION, "answer": "Rs 100 or 2% of the outstanding amount."}]
    response = client.post(
        f"/indexes/{index_id}/ask", json={"question": "And when is it charged?", "history": history}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["question"] == "And when is it charged?"
    assert body["searched_for"] and QUESTION in body["searched_for"]
    assert _calls(client, index_id, "assistant_condense") == before + 1


@pytest.mark.parametrize(
    "history",
    [
        [{"question": "", "answer": "x"}],
        [{"question": "q"}],
        [{"question": "q", "answer": "a", "role": "user"}],
        [{"question": "q" * (ASK_HISTORY_QUESTION_CHARS + 1), "answer": "a"}],
        [{"question": "q", "answer": "a" * (ASK_HISTORY_ANSWER_CHARS + 1)}],
        [{"question": "q", "answer": "a"}] * (ASK_HISTORY_MAX_TURNS + 1),
        "not a list",
    ],
)
def test_history_of_the_wrong_shape_or_size_is_refused_before_any_call(
    client: TestClient, index_id: str, history: object
) -> None:
    before = client.get(f"/indexes/{index_id}").json()["llm_usage"]
    response = client.post(f"/indexes/{index_id}/ask", json={"question": QUESTION, "history": history})
    assert response.status_code == 422, response.text
    assert client.get(f"/indexes/{index_id}").json()["llm_usage"] == before


def test_history_at_the_limit_is_accepted(client: TestClient, index_id: str) -> None:
    history = [{"question": "q" * ASK_HISTORY_QUESTION_CHARS, "answer": ""}] * ASK_HISTORY_MAX_TURNS
    response = client.post(f"/indexes/{index_id}/ask", json={"question": QUESTION, "history": history})
    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    ("raised", "status", "code", "message"),
    [
        (LLMHttpError("key_rejected"), 503, "LLM_UNAVAILABLE", PROBLEMS["key_rejected"][0]),
        (LLMError("LLM_REFUSED", "The AI service refused this request."), 409, "LLM_REFUSED", None),
        (LLMError("LLM_TOO_LONG", "The question is too long for the model."), 422, "LLM_TOO_LONG", None),
        (
            LLMError("RERANKER_NOT_INSTALLED", "The passage reranker is not installed on this server."),
            503,
            "RERANKER_NOT_INSTALLED",
            None,
        ),
        (generative_error(PROMPT_NOT_FOUND, name="assistant_answer"), 503, PROMPT_NOT_FOUND, None),
    ],
)
def test_a_failure_while_answering_is_a_plain_coded_error_and_never_a_500(
    client: TestClient,
    index_id: str,
    monkeypatch: pytest.MonkeyPatch,
    raised: Exception,
    status: int,
    code: str,
    message: str | None,
) -> None:
    from api.routes import generative

    def failing(*args: object, **kwargs: object) -> None:
        raise raised

    monkeypatch.setattr(generative, "answer", failing)
    response = client.post(f"/indexes/{index_id}/ask", json={"question": QUESTION})
    assert response.status_code == status, response.text
    detail = response.json()["detail"]
    assert detail["code"] == code
    assert detail["message"] == (message or getattr(raised, "message", ""))
    assert INTERNAL not in response.text


def test_the_index_detail_offers_starter_questions_it_passed(client: TestClient, index_id: str) -> None:
    detail = client.get(f"/indexes/{index_id}").json()
    suggested = detail["suggested_questions"]
    assert 0 < len(suggested) <= 4
    passed = {
        row["question"]
        for row in detail["rag_eval"]["questions"]
        if row["passed"] and not row["expect_refusal"] and row["refused"] is False
    }
    assert set(suggested) <= passed
