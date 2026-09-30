"""`/ai-service` end to end: two slots, encrypted keys, tests against a real local server (DEC-1140).

Throughout, the key must appear in no response body, no error, no header and no file on disk in the
clear - `_Recorder` keeps every response text so the last test of each scenario can say so once.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from api.access_policy import policy_for
from api.main import create_app
from engine.access.roles import Role
from engine.settings import Settings
from tests.unit.ai_service.server import FakeService, Reply, chat_reply, serve

pytestmark = pytest.mark.integration

KEY = "zk-test-key-0123456789abcdef"
ENC = "E" * 40
TYPED = "zk-test-" + "typed-key-abcdef"


class _Recorder:
    """A `TestClient` wrapper that remembers every response text and header set."""

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.seen: list[str] = []

    def call(self, method: str, url: str, **kw: Any) -> Any:
        response = self.client.request(method, url, **kw)
        self.seen.append(response.text + json.dumps(dict(response.headers)))
        return response

    def get(self, url: str) -> Any:
        return self.call("GET", url)

    def put(self, url: str, body: Any = None, **kw: Any) -> Any:
        return self.call("PUT", url, **({"json": body} if body is not None else kw))

    def post(self, url: str, body: Any = None, **kw: Any) -> Any:
        return self.call("POST", url, **({"json": body} if body is not None else kw))

    def delete(self, url: str) -> Any:
        return self.call("DELETE", url)

    def assert_no_key(self, *keys: str) -> None:
        for key in keys or (KEY,):
            assert not any(key in text for text in self.seen), key


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def api(config_root: Path, data_dir: Path) -> Iterator[_Recorder]:
    settings = Settings(connections_key=SecretStr(ENC), allow_fake_ai=False)
    with TestClient(create_app(config_root=config_root, data_dir=data_dir, settings=settings)) as client:
        yield _Recorder(client)


def _openai_body(server: FakeService, **extra: Any) -> dict[str, Any]:
    return {
        "provider": "openai_compatible",
        "model": "local-m",
        "base_url": server.url + "/v1",
        "api_key": KEY,
        **extra,
    }


# --- reading ---------------------------------------------------------------------------------
def test_the_initial_state_is_two_unconnected_slots_and_six_providers(api: _Recorder) -> None:
    response = api.get("/ai-service")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    body = response.json()
    assert set(body["slots"]) == {"product", "deliverable"}
    product, deliverable = body["slots"]["product"], body["slots"]["deliverable"]
    assert (product["connected"], product["source"], product["provider"], product["has_key"]) == (
        False,
        "none",
        None,
        False,
    )
    assert product["editable"] is True and product["locked_reason"] is None and product["last_test"] is None
    assert product["inherits_product"] is False and deliverable["inherits_product"] is False
    providers = {p["id"]: p for p in body["providers"]}
    assert list(providers) == [
        "bedrock",
        "openai",
        "anthropic",
        "openrouter",
        "huggingface",
        "openai_compatible",
    ]
    assert set(providers["openai"]) == {
        "id",
        "label",
        "protocol",
        "default_base_url",
        "needs_key",
        "needs_base_url",
        "needs_region",
        "key_hint",
        "key_help",
        "key_prefixes",
        "model_suggestions",
        "embedding_suggestions",
        "supports_embeddings",
        "third_party",
    }
    assert providers["openai"]["model_suggestions"] and providers["anthropic"]["supports_embeddings"] is False


def test_an_unknown_slot_is_404_on_every_route(api: _Recorder) -> None:
    for method, path in (
        ("PUT", "/ai-service/nope"),
        ("POST", "/ai-service/nope/test"),
        ("POST", "/ai-service/nope/models"),
        ("DELETE", "/ai-service/nope"),
    ):
        response = api.call(method, path, json={})
        assert response.status_code == 404 and response.json()["detail"]["code"] == "AI_SLOT_UNKNOWN"


def test_access_policy_reads_are_viewer_and_everything_else_is_analyst() -> None:
    assert policy_for("GET", "/ai-service").role is Role.VIEWER  # type: ignore[union-attr]
    for method, path in (
        ("PUT", "/ai-service/{slot}"),
        ("POST", "/ai-service/{slot}/test"),
        ("POST", "/ai-service/{slot}/models"),
        ("DELETE", "/ai-service/{slot}"),
    ):
        assert policy_for(method, path).role is Role.ANALYST  # type: ignore[union-attr]


# --- saving ------------------------------------------------------------------------------------
def test_save_read_and_disconnect_never_return_the_key(api: _Recorder, data_dir: Path) -> None:
    with serve(lambda _: chat_reply()) as server:
        saved = api.put("/ai-service/product", _openai_body(server))
        assert saved.status_code == 200 and saved.headers["cache-control"] == "no-store"
        state = saved.json()
        assert (state["slot"], state["connected"], state["source"], state["provider"]) == (
            "product",
            True,
            "saved",
            "openai_compatible",
        )
        assert state["has_key"] is True and state["key_readable"] is True and state["third_party"] is True
        assert (
            state["model"] == "local-m" and state["base_url"] == server.url + "/v1" and "api_key" not in state
        )
        assert server.requests == []  # saving makes no network call
        after = api.get("/ai-service").json()["slots"]
        assert after["product"]["connected"] and after["deliverable"]["source"] == "none"
        assert not after["deliverable"]["connected"] and after["deliverable"]["model"] is None
        gone = api.delete("/ai-service/product")
        assert (
            gone.status_code == 200 and gone.json()["connected"] is False and gone.json()["source"] == "none"
        )
        assert api.delete("/ai-service/product").status_code == 200  # idempotent
    api.assert_no_key()
    assert not any(KEY in p.read_text(errors="ignore") for p in data_dir.rglob("*") if p.is_file())


def test_blank_key_keeps_the_saved_one_and_a_save_forgets_the_last_test(api: _Recorder) -> None:
    with serve(lambda _: chat_reply()) as server:
        api.put("/ai-service/product", _openai_body(server))
        assert api.post("/ai-service/product/test").json()["ok"] is True
        assert api.get("/ai-service").json()["slots"]["product"]["last_test"]["ok"] is True
        again = api.put("/ai-service/product", _openai_body(server, api_key="", model="local-m2"))
        assert (
            again.status_code == 200 and again.json()["has_key"] is True and again.json()["last_test"] is None
        )
        api.post("/ai-service/product/test")
    assert server.requests[-1].headers["authorization"] == f"Bearer {KEY}"
    api.assert_no_key()


@pytest.mark.parametrize(
    ("body", "code", "field"),
    [
        ({}, "AI_FIELD_REQUIRED", "provider"),
        ({"provider": "openai", "model": "m"}, "AI_KEY_REQUIRED", "api_key"),
        ({"provider": "bedrock", "model": "m"}, "AI_FIELD_REQUIRED", "region"),
        (
            {"provider": "openai_compatible", "model": "m", "base_url": "http://169.254.169.254"},
            "AI_BASE_URL_INVALID",
            "base_url",
        ),
        (
            {"provider": "openai_compatible", "model": "m", "base_url": "https://u:p@example.com"},
            "AI_BASE_URL_INVALID",
            "base_url",
        ),
    ],
)
def test_field_errors_are_422_and_name_fields_never_values(
    api: _Recorder, body: dict[str, Any], code: str, field: str
) -> None:
    response = api.put("/ai-service/product", body)
    detail = response.json()["detail"]
    assert response.status_code == 422 and (detail["code"], detail["field"]) == (code, field)
    assert "p@example" not in response.text and "u:p" not in response.text


def test_the_body_is_read_raw_so_a_422_never_reflects_the_key(api: _Recorder) -> None:
    bad_bodies = [
        json.dumps({"provider": "openai", "model": "m", "api_key": [KEY]}),  # wrong type
        json.dumps({"provider": "openai", "model": ["x"], "api_key": KEY}),
        json.dumps(
            {"provider": "openai", "model": "m", "api_key": KEY, KEY: 1}
        ),  # unknown field named like a key
        json.dumps({"provider": "openai", "model": "m", "api_key": KEY, "bogus": KEY}),
        json.dumps([KEY]),
        json.dumps({"provider": 3, "model": "m", "api_key": KEY}),
        '{"api_key": "' + KEY,  # not JSON at all
    ]
    for raw in bad_bodies:
        for verb, path in (
            ("put", "/ai-service/product"),
            ("post", "/ai-service/product/test"),
            ("post", "/ai-service/product/models"),
        ):
            response = getattr(api, verb)(path, content=raw, headers={"content-type": "application/json"})
            assert response.status_code == 422, (raw, response.text)
            assert response.json()["detail"]["code"] == "BODY_INVALID"
    api.assert_no_key()


def test_the_body_is_capped_at_16_kib(api: _Recorder) -> None:
    big = json.dumps({"provider": "openai", "model": "m", "api_key": "k" * 20000})
    response = api.put("/ai-service/product", content=big, headers={"content-type": "application/json"})
    assert response.status_code == 413 and response.json()["detail"]["code"] == "BODY_TOO_LARGE"
    chunked = api.put("/ai-service/product", content=iter([big.encode()[:9000], big.encode()[9000:]]))
    assert chunked.status_code == 413


def test_saving_is_refused_when_the_operator_has_not_set_the_encryption_key(
    api: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A prod app also needs a privacy salt and sign-in to start; the rule itself is unit-tested on Settings.
    reason = "Saving an AI service needs MARKETING_AI_CONNECTIONS_KEY to be set."
    monkeypatch.setattr("engine.ai_service.key_store_problem", lambda *_a, **_k: reason)
    state = api.get("/ai-service").json()["slots"]["product"]
    assert state["editable"] is False and "MARKETING_AI_CONNECTIONS_KEY" in state["locked_reason"]
    response = api.put("/ai-service/product", {"provider": "bedrock", "model": "m", "region": "us-east-1"})
    assert response.status_code == 409 and response.json()["detail"]["code"] == "AI_SERVICE_LOCKED"
    assert api.delete("/ai-service/product").status_code == 200  # disconnecting is always allowed


# --- testing -----------------------------------------------------------------------------------
def test_test_makes_one_tiny_completion_and_stores_the_result(api: _Recorder) -> None:
    with serve(lambda _: chat_reply("ready")) as server:
        api.put("/ai-service/product", _openai_body(server))
        result = api.post("/ai-service/product/test")
    body = result.json()
    assert result.status_code == 200 and body["ok"] is True and body["model"] == "local-m"
    assert isinstance(body["latency_ms"], int) and body["fix"] is None
    assert "Connected" in body["message"] and "ready" not in body["message"].lower().replace("ready.", "")
    assert body["embeddings_note"] == "Document assistant will match by keywords."
    sent = server.requests[0].body
    assert sent["messages"][-1] == {"role": "user", "content": "Reply with the single word: ready"}
    assert sent["max_tokens"] == 16 and sent["temperature"] == 0.0 and len(server.requests) == 1
    saved = api.get("/ai-service").json()["slots"]["product"]["last_test"]
    assert saved["ok"] is True and saved["message"] == body["message"] and saved["tested_at"]
    api.assert_no_key()


def test_the_test_also_tries_the_embedding_model_when_one_is_named(api: _Recorder) -> None:
    def working(sent: Any) -> Reply:
        if sent.path.endswith("/embeddings"):
            return Reply(200, {"data": [{"index": 0, "embedding": [0.1, 0.2]}]})
        return chat_reply()

    with serve(working) as server:
        api.put("/ai-service/product", _openai_body(server, embedding_model="e-1"))
        assert api.post("/ai-service/product/test").json()["embeddings_note"] is None
    assert [r.path for r in server.requests] == ["/v1/chat/completions", "/v1/embeddings"]

    def broken(sent: Any) -> Reply:
        return Reply(500, {"error": "x"}) if sent.path.endswith("/embeddings") else chat_reply()

    with serve(broken) as server:
        api.put("/ai-service/product", _openai_body(server, embedding_model="e-1"))
        body = api.post("/ai-service/product/test").json()
    assert body["ok"] is True and "embedding model did not answer" in body["embeddings_note"]


def test_a_rejected_key_is_ok_false_with_a_plain_fix_not_an_http_error(api: _Recorder) -> None:
    quoting = {"error": {"message": f"bad key {KEY}; prompt was: Reply with the single word: ready"}}
    with serve(lambda _: Reply(401, quoting)) as server:
        api.put("/ai-service/product", _openai_body(server))
        result = api.post("/ai-service/product/test")
    body = result.json()
    assert result.status_code == 200 and body["ok"] is False
    assert "rejected the key" in body["message"] and "enter it again" in body["fix"]
    assert "bad key" not in result.text
    state = api.get("/ai-service").json()["slots"]["product"]
    assert state["last_test"]["ok"] is False and state["connected"] is True
    api.assert_no_key()


@pytest.mark.parametrize(
    ("status", "words"),
    [(429, "too many requests"), (500, "problem on its side"), (404, "did not find the model")],
)
def test_other_provider_failures_are_answers_too(api: _Recorder, status: int, words: str) -> None:
    with serve(lambda _: Reply(status, {"error": {"message": "secret detail"}})) as server:
        api.put("/ai-service/product", _openai_body(server))
        body = api.post("/ai-service/product/test").json()
    assert (
        body["ok"] is False
        and words in body["message"]
        and body["fix"]
        and "secret detail" not in json.dumps(body)
    )


def test_an_unreachable_server_is_an_answer(api: _Recorder) -> None:
    with serve(lambda _: chat_reply()) as server:
        body = _openai_body(server)
    api.put("/ai-service/product", body)  # the server is now stopped
    result = api.post("/ai-service/product/test").json()
    assert result["ok"] is False and "could not reach" in result["message"]


def test_a_test_with_a_body_tests_those_settings_and_saves_nothing(api: _Recorder) -> None:
    with serve(lambda _: chat_reply()) as server:
        result = api.post("/ai-service/deliverable/test", _openai_body(server))
        assert result.json()["ok"] is True
        assert server.requests[0].headers["authorization"] == f"Bearer {KEY}"
    state = api.get("/ai-service").json()["slots"]["deliverable"]
    assert state["connected"] is False and state["last_test"] is None


def test_test_with_nothing_saved_is_409_not_connected_naming_the_slot(api: _Recorder) -> None:
    response = api.post("/ai-service/deliverable/test")
    detail = response.json()["detail"]
    assert (
        response.status_code == 409
        and detail["code"] == "AI_NOT_CONNECTED"
        and detail["slot"] == "deliverable"
    )
    assert "Deliverable AI" in detail["message"] and detail["fix"]


def test_testing_deliverable_without_service_returns_not_connected(api: _Recorder) -> None:
    with serve(lambda _: chat_reply()) as server:
        api.put("/ai-service/product", _openai_body(server))
        res = api.post("/ai-service/deliverable/test")
        assert res.status_code == 409
        assert res.json()["detail"]["code"] == "AI_NOT_CONNECTED"
    slots = api.get("/ai-service").json()["slots"]
    assert slots["product"]["connected"] is True and slots["deliverable"]["source"] == "none"


def test_a_key_saved_for_one_address_is_not_sent_to_another(api: _Recorder) -> None:
    with serve(lambda _: chat_reply()) as saved_server, serve(lambda _: chat_reply()) as other:
        api.put("/ai-service/product", _openai_body(saved_server))
        api.post("/ai-service/product/test", {"base_url": other.url + "/v1"})
        api.post("/ai-service/product/models", {"base_url": other.url + "/v1"})
    assert other.requests and all("authorization" not in r.headers for r in other.requests)


def test_a_name_resolving_to_the_metadata_address_is_refused_before_any_call(
    api: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    def metadata(host: str, port: int, **_: object) -> list[tuple[object, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port))]

    monkeypatch.setattr(socket, "getaddrinfo", metadata)
    body = {
        "provider": "openai_compatible",
        "model": "m",
        "base_url": "https://sneaky.example.com/v1",
        "api_key": KEY,
    }
    for path in ("/ai-service/product/test", "/ai-service/product/models"):
        response = api.post(path, body)
        assert response.status_code == 422 and response.json()["detail"]["field"] == "base_url"


# --- models --------------------------------------------------------------------------------------
def test_models_lists_what_the_service_lists_using_the_saved_key(api: _Recorder) -> None:
    with serve(lambda _: Reply(200, {"data": [{"id": "b-model"}, {"id": "a-model"}]})) as server:
        api.put("/ai-service/product", _openai_body(server))
        listed = api.post("/ai-service/product/models", {})
        typed = api.post(
            "/ai-service/product/models",
            {"provider": "openai_compatible", "base_url": server.url + "/v1", "api_key": TYPED},
        )
    assert listed.json() == {"models": ["a-model", "b-model"], "note": None}
    assert server.requests[0].headers["authorization"] == f"Bearer {KEY}"
    assert server.requests[1].headers["authorization"] == f"Bearer {TYPED}" and typed.status_code == 200
    api.assert_no_key(KEY, TYPED)


def test_models_failures_are_an_empty_list_and_a_note_never_an_error_page(api: _Recorder) -> None:
    for status, words in (
        (404, "does not list its models"),
        (401, "rejected the key"),
        (500, "Could not load"),
    ):
        with serve(lambda _, s=status: Reply(s, {"error": {"message": KEY}})) as server:
            response = api.post("/ai-service/product/models", _openai_body(server))
        body = response.json()
        assert response.status_code == 200 and body["models"] == [] and words in body["note"], (status, body)
    api.assert_no_key()


def test_models_for_a_keyed_provider_without_a_key_says_to_enter_one(api: _Recorder) -> None:
    body = api.post("/ai-service/product/models", {"provider": "openai"}).json()
    assert body["models"] == [] and "key" in body["note"]


def test_bedrock_models_are_best_effort(api: _Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    import boto3

    class _Client:
        def list_foundation_models(self, **_: object) -> dict[str, Any]:
            return {"modelSummaries": [{"modelId": "z.model"}, {"modelId": "a.model"}]}

    class _Session:
        def __init__(self, **_: object) -> None: ...

        def client(self, *_: object, **__: object) -> _Client:
            return _Client()

    monkeypatch.setattr(boto3, "Session", _Session)
    body = api.post("/ai-service/product/models", {"provider": "bedrock", "region": "us-east-1"}).json()
    assert body == {"models": ["a.model", "z.model"], "note": None}

    class _Broken(_Session):
        def client(self, *_: object, **__: object) -> _Client:
            raise RuntimeError("no credentials")

    monkeypatch.setattr(boto3, "Session", _Broken)
    body = api.post("/ai-service/product/models", {"provider": "bedrock", "region": "us-east-1"}).json()
    assert body["models"] == [] and "AWS" in body["note"]


def test_a_test_of_a_saved_bedrock_calls_converse_in_its_region(
    api: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    import boto3

    calls: list[dict[str, Any]] = []

    class _Runtime:
        def converse(self, **kw: Any) -> dict[str, Any]:
            calls.append(kw)
            return {
                "output": {"message": {"content": [{"text": "ready"}]}},
                "usage": {"inputTokens": 3, "outputTokens": 1},
                "stopReason": "end_turn",
            }

    class _Session:
        def __init__(self, **kw: Any) -> None:
            calls.append({"session": kw})

        def client(self, *_: object, **__: object) -> _Runtime:
            return _Runtime()

    monkeypatch.setattr(boto3, "Session", _Session)
    api.put("/ai-service/product", {"provider": "bedrock", "model": "b-model", "region": "us-east-1"})
    body = api.post("/ai-service/product/test").json()
    assert body["ok"] is True and body["embeddings_note"] == "Document assistant will match by keywords."
    assert calls[0]["session"]["region_name"] == "us-east-1" and calls[1]["modelId"] == "b-model"
    assert calls[1]["inferenceConfig"]["maxTokens"] == 16


def test_a_failing_bedrock_test_has_a_plain_aws_fix(api: _Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    import boto3

    class _Runtime:
        def converse(self, **_: Any) -> dict[str, Any]:
            raise RuntimeError("AccessDenied: arn:aws:iam::123456789012:role/x")

    class _Session:
        def __init__(self, **_: Any) -> None: ...

        def client(self, *_: object, **__: object) -> _Runtime:
            return _Runtime()

    monkeypatch.setattr(boto3, "Session", _Session)
    api.put("/ai-service/product", {"provider": "bedrock", "model": "b-model", "region": "us-east-1"})
    result = api.post("/ai-service/product/test")
    body = result.json()
    assert body["ok"] is False and "Model access" in body["fix"] and "123456789012" not in result.text


# --- concurrency ---------------------------------------------------------------------------------
def test_only_four_network_calls_run_at_once_the_rest_are_429(api: _Recorder) -> None:
    from api.routes import ai_service as routes

    taken = [routes._CALLS.acquire(blocking=False) for _ in range(4)]
    try:
        with serve(lambda _: chat_reply()) as server:
            api.put("/ai-service/product", _openai_body(server))
            response = api.post("/ai-service/product/test")
            models = api.post("/ai-service/product/models", {})
        assert response.status_code == 429 and response.json()["detail"]["code"] == "AI_BUSY"
        assert response.headers["retry-after"] == "5" and models.status_code == 429
        assert server.requests == []
    finally:
        for _ in taken:
            routes._CALLS.release()
