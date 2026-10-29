"""Require consent for every AI call: the routes that send a client's data to the AI refuse without the client's
yes (`409 AI_CONSENT_REQUIRED`, nothing stored), work after it is given the way the Data Doctor gives it, and the
settings "test connection" call, which carries no client data, works without it."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from api.main import create_app
from engine.settings import Settings
from tests.ai_consent_support import consented
from tests.fixtures.make_data import GenerationSpec, generate
from tests.unit.ai_service.server import chat_reply, serve

pytestmark = pytest.mark.integration

USE_CASE = "targeted-advertisement"
RAG = "ai-onboarding-assistant"
CODE = "AI_CONSENT_REQUIRED"


@pytest.fixture(autouse=True)
def _restore_root_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    state = [(h, list(h.filters), h.formatter) for h in handlers]
    try:
        yield
    finally:
        root.handlers = handlers
        root.setLevel(level)
        for handler, filters, formatter in state:
            handler.filters = filters
            handler.setFormatter(formatter)


@pytest.fixture
def client(config_root: Path, tmp_path: Path) -> Iterator[TestClient]:
    """No client has consented. The test model answers (the suite allows it), so only consent can stop a call."""
    with TestClient(create_app(config_root=config_root, data_dir=tmp_path / "data")) as test_client:
        yield test_client


def _upload(client: TestClient) -> str:
    frame = generate(GenerationSpec(use_case_id=USE_CASE, rows=600))
    response = client.post(
        "/uploads",
        files={"file": ("history.csv", frame.to_csv(index=False).encode(), "text/csv")},
        data={"use_case": USE_CASE, "mode": "train"},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["upload_id"])


def _refused(response: Any) -> None:
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == CODE
    assert "default" not in detail["message"], "the refusal names no client and no data"


def _index(client: TestClient) -> Any:
    return client.post(
        f"/use-cases/{RAG}/indexes",
        data={"use_sample_documents": "true", "model_choice": "__automl__", "overrides": "{}"},
    )


def _give_consent_as_the_doctor_does(client: TestClient, upload_id: str) -> None:
    case = client.post("/cases", json={"upload_ids": [upload_id]})
    assert case.status_code in (200, 201), case.text
    given = client.post(f"/cases/{case.json()['case_id']}/ai-consent", json={"client_id": "default"})
    assert given.status_code == 200 and given.json()["consent"] is True, given.text


def test_every_route_that_sends_the_clients_data_to_the_ai_refuses_without_consent(
    client: TestClient,
) -> None:
    upload_id = _upload(client)
    _refused(
        client.post(f"/uploads/{upload_id}/ask/messages", json={"text": "how many rows?"})
    )  # Ask your data
    _refused(_index(client))  # the document assistant (Deliverable AI)

    assert client.get(f"/uploads/{upload_id}/ask").json()["session"] is None


def test_the_same_routes_work_once_the_client_has_said_yes(client: TestClient) -> None:
    upload_id = _upload(client)
    _refused(client.post(f"/uploads/{upload_id}/ask/messages", json={"text": "hello"}))

    _give_consent_as_the_doctor_does(client, upload_id)

    assert (
        client.post(f"/uploads/{upload_id}/ask/messages", json={"text": "how many rows?"}).status_code == 200
    )
    assert _index(client).status_code == 202


def test_an_upload_in_another_clients_case_needs_that_clients_consent(client: TestClient) -> None:
    upload_id = _upload(client)
    case = client.post("/cases", json={"upload_ids": [upload_id], "client_id": "acme"})
    assert case.status_code in (200, 201), case.text
    text = {"text": "hello"}

    data_dir = Path(client.app.state.data_dir)  # type: ignore[attr-defined]
    consented(data_dir, "default")
    _refused(client.post(f"/uploads/{upload_id}/ask/messages", json=text))

    consented(data_dir, "acme")
    assert client.post(f"/uploads/{upload_id}/ask/messages", json=text).status_code == 200


def test_the_connection_test_carries_no_client_data_and_works_without_consent(client: TestClient) -> None:
    with serve(lambda _: chat_reply("ok")) as server:
        saved = client.put(
            "/ai-service/product",
            json={"provider": "openai_compatible", "model": "local-m", "base_url": server.url + "/v1"},
        )
        assert saved.status_code == 200, saved.text
        tested = client.post("/ai-service/product/test")
    assert tested.status_code == 200 and tested.json()["ok"] is True, tested.text
    assert server.requests, "the greeting did go to the service"


def test_a_settings_object_without_the_fake_still_asks_for_consent_first(
    config_root: Path, tmp_path: Path
) -> None:
    settings = Settings(connections_key=SecretStr("F" * 40), allow_fake_ai=False)
    with TestClient(create_app(config_root=config_root, data_dir=tmp_path / "d", settings=settings)) as app:
        with serve(lambda _: chat_reply("hello")) as server:
            app.put(
                "/ai-service/product",
                json={"provider": "openai_compatible", "model": "m", "base_url": server.url + "/v1"},
            )
            upload_id = _upload(app)
            _refused(app.post(f"/uploads/{upload_id}/ask/messages", json={"text": "hello"}))
        assert server.requests == [], "a real provider was never reached"
