"""Guided-setup chat (Product AI) and the generative routes (Deliverable AI) resolve their slot (DEC-1140)."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from api.main import create_app
from engine.config import RunMode
from engine.settings import Settings
from engine.storage import LocalStorage
from tests.fixtures.make_data import GenerationSpec, generate
from tests.fixtures.make_run import RunSpec, write_run
from tests.unit.ai_service.server import FakeService, chat_reply, message_reply, serve

pytestmark = pytest.mark.integration

KEY = "zk-test-key-0123456789abcdef"
RAG = "ai-onboarding-assistant"
USE_CASE = "targeted-advertisement"


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


def _client(config_root: Path, data_dir: Path, *, allow_fake: bool) -> TestClient:
    settings = Settings(connections_key=SecretStr("F" * 40), allow_fake_ai=allow_fake)
    return TestClient(create_app(config_root=config_root, data_dir=data_dir, settings=settings))


@pytest.fixture
def client(config_root: Path, data_dir: Path) -> Iterator[TestClient]:
    with _client(config_root, data_dir, allow_fake=False) as test_client:
        yield test_client


def _save(client: TestClient, slot: str, **body: Any) -> None:
    response = client.put(f"/ai-service/{slot}", json=body)
    assert response.status_code == 200, response.text


def _local(server: FakeService, **extra: Any) -> dict[str, Any]:
    return {"provider": "openai_compatible", "model": "local-m", "base_url": server.url + "/v1", **extra}


def _upload(client: TestClient) -> str:
    frame = generate(GenerationSpec(use_case_id=USE_CASE, rows=600))
    response = client.post(
        "/uploads",
        files={"file": ("history.csv", frame.to_csv(index=False).encode(), "text/csv")},
        data={"use_case": USE_CASE, "mode": "train"},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["upload_id"])


def _sample_index(client: TestClient) -> Any:
    return client.post(
        f"/use-cases/{RAG}/indexes",
        data={"use_sample_documents": "true", "model_choice": "__automl__", "overrides": "{}"},
    )


def _wait(client: TestClient, index_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        body = dict(client.get(f"/indexes/{index_id}").json())
        if body["status"]["state"] in ("done", "failed"):
            return body
        time.sleep(0.02)
    raise AssertionError("index build did not finish")


# --- Product AI: the Guided-setup chat -------------------------------------------------------------
def test_guided_setup_still_suggests_without_an_ai_service_but_the_chat_says_it_is_off(
    client: TestClient,
) -> None:
    upload_id = _upload(client)
    started = client.post(f"/uploads/{upload_id}/agent-session", json={"use_case": USE_CASE})
    assert started.status_code == 201, started.text
    body = started.json()
    assert body["session"]["proposals"] is not None and body["session"]["questions"] is not None
    assert body["chat"]["available"] is False and body["chat"]["reason"] == "AI_NOT_CONNECTED"
    assert (body["chat"]["backend"], body["chat"]["third_party"], body["chat"]["generation_model_id"]) == (
        "none",
        False,
        None,
    )
    assert client.get(f"/uploads/{upload_id}/agent-session").json()["chat"]["available"] is False


def test_the_chat_turn_is_409_not_connected_and_changes_nothing(client: TestClient) -> None:
    upload_id = _upload(client)
    client.post(f"/uploads/{upload_id}/agent-session", json={"use_case": USE_CASE})
    before = client.get(f"/uploads/{upload_id}/agent-session").json()["session"]
    response = client.post(
        f"/uploads/{upload_id}/agent-session/messages", json={"text": "what should I hide?"}
    )
    detail = response.json()["detail"]
    assert (
        response.status_code == 409 and detail["code"] == "AI_NOT_CONNECTED" and detail["slot"] == "product"
    )
    assert (
        detail["message"] == "No AI service is connected for Product AI." and "Connections" in detail["fix"]
    )
    assert client.get(f"/uploads/{upload_id}/agent-session").json()["session"] == before


def test_a_deliverable_service_does_not_switch_the_product_chat_on(client: TestClient) -> None:
    with serve(lambda _: chat_reply()) as server:
        _save(client, "deliverable", **_local(server))
        upload_id = _upload(client)
        chat = client.post(f"/uploads/{upload_id}/agent-session", json={"use_case": USE_CASE}).json()["chat"]
        assert chat["available"] is False
        response = client.post(f"/uploads/{upload_id}/agent-session/messages", json={"text": "hello"})
        assert response.status_code == 409
    assert server.requests == []


def test_with_a_product_service_the_chat_calls_it_and_reports_who_sees_the_prompts(
    client: TestClient,
) -> None:
    with serve(lambda _: chat_reply("Hide the id column.")) as server:
        _save(client, "product", **_local(server, api_key=KEY))
        upload_id = _upload(client)
        started = client.post(f"/uploads/{upload_id}/agent-session", json={"use_case": USE_CASE}).json()
        assert started["chat"] == {
            "available": True,
            "reason": None,
            "backend": "openai_compatible",
            "provider_label": "Other / local",
            "generation_model_id": "local-m",
            "data_access": started["chat"]["data_access"],
            "third_party": True,
        }
        response = client.post(
            f"/uploads/{upload_id}/agent-session/messages", json={"text": "what should I hide?"}
        )
    assert response.status_code == 200, response.text
    assert server.requests and server.requests[0].body["model"] == "local-m"
    assert server.requests[0].headers["authorization"] == f"Bearer {KEY}"
    assert KEY not in response.text


def test_a_saved_bedrock_is_not_third_party(client: TestClient) -> None:
    _save(client, "product", provider="bedrock", model="b-model", region="us-east-1")
    upload_id = _upload(client)
    chat = client.post(f"/uploads/{upload_id}/agent-session", json={"use_case": USE_CASE}).json()["chat"]
    assert (chat["available"], chat["backend"], chat["third_party"]) == (True, "bedrock", False)


def test_the_test_model_answers_only_when_a_test_allows_it(config_root: Path, tmp_path: Path) -> None:
    with _client(config_root, tmp_path / "d", allow_fake=True) as client:
        upload_id = _upload(client)
        chat = client.post(f"/uploads/{upload_id}/agent-session", json={"use_case": USE_CASE}).json()["chat"]
        assert (chat["available"], chat["backend"], chat["third_party"]) == (True, "fake", False)
        assert client.get("/ai-service").json()["slots"]["product"]["connected"] is False  # not a service


# --- Deliverable AI: the generative routes ---------------------------------------------------------
def test_every_generative_start_is_409_before_anything_is_written(
    client: TestClient, data_dir: Path, config_root: Path
) -> None:
    storage = LocalStorage(data_dir)
    rca = write_run(
        storage,
        RunSpec(
            use_case_id="rca",
            mode=RunMode.SCORE,
            rows=200,
            positive_rate=0.25,
            with_text=True,
            config_root=config_root,
        ),
    )
    copy = write_run(
        storage,
        RunSpec(
            use_case_id="win-back-campaign",
            mode=RunMode.SCORE,
            rows=200,
            positive_rate=0.3,
            config_root=config_root,
        ),
    )
    keys_before = set(storage.list_keys(""))  # the app's own audit database may also change: ignored below
    responses = [
        _sample_index(client),
        client.post(f"/runs/{rca}/root-cause", json={}),
        client.post(f"/runs/{copy}/campaign-copy", json={}),
    ]
    for response in responses:
        detail = response.json()["detail"]
        assert response.status_code == 409, response.text
        assert (detail["code"], detail["slot"]) == ("AI_NOT_CONNECTED", "deliverable")
        assert detail["message"] == "No AI service is connected for Deliverable AI."
    written = {k for k in storage.list_keys("") if not k.startswith("platform.db")} - keys_before
    assert written == set()  # no index, no status file, nothing half-made


def test_asking_and_grading_are_409_too(config_root: Path, data_dir: Path) -> None:
    with _client(config_root, data_dir, allow_fake=True) as fake:  # build one index with the test model
        response = _sample_index(fake)
        assert response.status_code == 202
        index_id = response.json()["index_id"]
        assert _wait(fake, index_id)["status"]["state"] == "done"
    with _client(config_root, data_dir, allow_fake=False) as client:
        asked = client.post(
            f"/indexes/{index_id}/ask", json={"question": "How long does a new SIM take to activate?"}
        )
        graded = client.post(f"/indexes/{index_id}/evaluate", data={"use_sample_questions": "true"})
    for response in (asked, graded):
        assert response.status_code == 409 and response.json()["detail"]["code"] == "AI_NOT_CONNECTED"


def test_a_service_without_embeddings_builds_an_index_by_keywords(client: TestClient) -> None:
    _save(client, "deliverable", provider="anthropic", api_key=KEY, model="cust-m")
    response = _sample_index(client)
    assert response.status_code == 202, response.text
    body = _wait(client, response.json()["index_id"])
    assert body["status"]["state"] == "done", body["status"]
    assert (body["llm"]["backend"], body["llm"]["generation_model_id"]) == ("external", "cust-m")
    assert body["llm"]["embedding_model_id"] == "keyword-hash-v1"
    assert (
        body["manifest"]["embedding_model_id"] == "keyword-hash-v1"
        if "embedding_model_id" in body["manifest"]
        else True
    )
    assert KEY not in str(body)


def test_the_deliverable_follows_the_product_service_until_it_has_its_own(client: TestClient) -> None:
    _save(client, "product", provider="anthropic", api_key=KEY, model="prod-m")
    body = _wait(client, _sample_index(client).json()["index_id"])
    assert body["status"]["state"] == "done" and body["llm"]["generation_model_id"] == "prod-m"
    _save(client, "deliverable", provider="anthropic", api_key=KEY, model="cust-m")
    body = _wait(client, _sample_index(client).json()["index_id"])
    assert body["llm"]["generation_model_id"] == "cust-m"


def test_a_question_is_answered_by_the_deliverable_service(client: TestClient) -> None:
    with serve(lambda _: message_reply("It costs Rs 299 a month.")) as server:
        _save(client, "deliverable", provider="anthropic", api_key=KEY, model="cust-m", base_url=server.url)
        index_id = _sample_index(client).json()["index_id"]
        assert _wait(client, index_id)["status"]["state"] == "done"
        answered = client.post(
            f"/indexes/{index_id}/ask", json={"question": "How long does a new SIM take to activate?"}
        )
    assert answered.status_code == 200, answered.text
    assert server.requests, "the question should have reached the saved service"
    assert server.requests[0].headers["x-api-key"] == KEY and server.requests[0].body["model"] == "cust-m"
    assert KEY not in answered.text
