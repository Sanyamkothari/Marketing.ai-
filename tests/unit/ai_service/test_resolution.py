"""Which client answers a language call, and with which model names (DEC-1140)."""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest
from pydantic import SecretStr

from engine.ai_service import (
    AiNotConnectedError,
    AiServiceStore,
    KeywordEmbeddingClient,
    ServiceInput,
    effective_service,
    resolve_client,
    save_service,
    store_key,
)
from engine.config import LlmBackend, LlmConfig
from engine.llm import KEYWORD_HASH_MODEL_ID, BedrockLLMClient, FakeLLMClient
from engine.llm_http import OpenAICompatibleClient
from engine.settings import Settings
from engine.storage import LocalStorage
from tests.unit.ai_service.server import chat_reply, message_reply, serve

KEY = "zk-test-key-0123456789abcdef"
ENC = "C" * 40
YAML_FAKE = LlmConfig()
YAML_BEDROCK = LlmConfig(
    backend=LlmBackend.BEDROCK,
    region="ap-south-1",
    generation_model_id="yaml-gen",
    judge_model_id="yaml-judge",
    embedding_model_id="yaml-emb",
)


def _settings(**kw: object) -> Settings:
    return Settings(connections_key=SecretStr(ENC), **kw)  # type: ignore[arg-type]


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path / "data")


def _save(storage: LocalStorage, slot: str = "product", **body: object) -> None:
    settings = _settings()
    save_service(AiServiceStore(storage, settings), slot, ServiceInput(**body), settings=settings, storage=storage)  # type: ignore[arg-type]


def test_nothing_connected_is_a_refusal_naming_the_slot(storage: LocalStorage) -> None:
    for slot, label in (("product", "Product AI"), ("deliverable", "Deliverable AI")):
        with pytest.raises(AiNotConnectedError) as caught:
            resolve_client(YAML_FAKE, slot=slot, storage=storage, settings=_settings())
        assert caught.value.code == "AI_NOT_CONNECTED" and caught.value.slot == slot
        assert label in caught.value.message and "No AI service is connected" in caught.value.message
        assert "Connections" in caught.value.fix and label in caught.value.fix
        assert effective_service(YAML_FAKE, slot=slot, storage=storage, settings=_settings()) is None


def test_the_fake_answers_only_when_a_test_allows_it(storage: LocalStorage) -> None:
    resolved = resolve_client(
        YAML_FAKE, slot="product", storage=storage, settings=_settings(allow_fake_ai=True)
    )
    assert isinstance(resolved.client, FakeLLMClient)
    assert (resolved.source, resolved.provider, resolved.third_party) == ("fake", "fake", False)
    assert resolved.llm == YAML_FAKE


def test_the_use_case_files_bedrock_still_works_as_before(storage: LocalStorage) -> None:
    resolved = resolve_client(YAML_BEDROCK, slot="deliverable", storage=storage, settings=_settings())
    assert isinstance(resolved.client, BedrockLLMClient) and resolved.client.model_id == "yaml-gen"
    assert (resolved.source, resolved.provider, resolved.third_party) == ("config", "bedrock", False)
    assert resolved.llm is YAML_BEDROCK or resolved.llm == YAML_BEDROCK


def test_a_saved_service_beats_the_use_case_file(storage: LocalStorage) -> None:
    _save(storage, provider="openai", api_key=KEY, model="saved-m")
    resolved = resolve_client(YAML_BEDROCK, slot="product", storage=storage, settings=_settings())
    assert isinstance(resolved.client, KeywordEmbeddingClient)
    assert (resolved.source, resolved.provider, resolved.third_party) == ("saved", "openai", True)


def test_the_saved_models_become_the_llm_config_every_call_is_metered_with(storage: LocalStorage) -> None:
    _save(storage, provider="openai", api_key=KEY, model="saved-m")
    llm = resolve_client(YAML_FAKE, slot="product", storage=storage, settings=_settings()).llm
    assert llm.backend is LlmBackend.EXTERNAL
    assert (llm.generation_model, llm.judge_model, llm.embedding_model) == (
        "saved-m",
        "saved-m",
        KEYWORD_HASH_MODEL_ID,
    )
    assert (llm.timeout_s, llm.max_retries, llm.temperature) == (60, 2, 0.20)  # the use case's own policy


def test_an_embedding_model_makes_a_real_client_and_none_makes_a_keyword_hash(storage: LocalStorage) -> None:
    _save(storage, provider="openai", api_key=KEY, model="m", embedding_model="emb-1")
    with_model = resolve_client(YAML_FAKE, slot="product", storage=storage, settings=_settings())
    assert isinstance(with_model.client, OpenAICompatibleClient) and with_model.llm.embedding_model == "emb-1"
    _save(storage, provider="openai", api_key=KEY, model="m")
    without = resolve_client(YAML_FAKE, slot="product", storage=storage, settings=_settings())
    assert isinstance(without.client, KeywordEmbeddingClient)


def test_keyword_embeddings_are_deterministic_and_match_by_shared_words(storage: LocalStorage) -> None:
    _save(storage, provider="anthropic", api_key=KEY, model="m")
    client = resolve_client(YAML_FAKE, slot="product", storage=storage, settings=_settings()).client
    assert isinstance(client, KeywordEmbeddingClient)
    a, b, c = client.embed(
        ["refund policy for damaged parcels", "damaged parcels refund", "office opening hours"]
    )
    assert client.embed(["refund policy for damaged parcels"])[0] == a and len(a) == 1024

    def cosine(x: tuple[float, ...], y: tuple[float, ...]) -> float:
        return sum(p * q for p, q in zip(x, y, strict=True))

    assert cosine(a, b) > 0.5 > cosine(a, c)


def test_anthropic_never_asks_the_service_for_embeddings(storage: LocalStorage) -> None:
    _save(storage, provider="anthropic", api_key=KEY, model="m", embedding_model="ignored")
    resolved = resolve_client(YAML_FAKE, slot="product", storage=storage, settings=_settings())
    assert resolved.llm.embedding_model == KEYWORD_HASH_MODEL_ID
    assert isinstance(resolved.client, KeywordEmbeddingClient)


def test_a_saved_bedrock_uses_its_region_and_models(storage: LocalStorage) -> None:
    _save(storage, provider="bedrock", model="b-gen", embedding_model="b-emb", region="eu-west-1")
    resolved = resolve_client(
        YAML_FAKE, slot="product", storage=storage, settings=_settings(), profile="prof"
    )
    assert isinstance(resolved.client, BedrockLLMClient) and resolved.client.model_id == "b-gen"
    assert resolved.llm.backend is LlmBackend.BEDROCK and resolved.llm.region == "eu-west-1"
    assert (resolved.llm.generation_model, resolved.llm.embedding_model, resolved.third_party) == (
        "b-gen",
        "b-emb",
        False,
    )


def test_the_deliverable_follows_the_product_until_it_has_its_own(storage: LocalStorage) -> None:
    _save(storage, "product", provider="openai", api_key=KEY, model="prod-m")
    inherited = resolve_client(YAML_FAKE, slot="deliverable", storage=storage, settings=_settings())
    assert (inherited.source, inherited.provider, inherited.llm.generation_model) == (
        "inherited",
        "openai",
        "prod-m",
    )
    _save(storage, "deliverable", provider="anthropic", api_key=KEY, model="cust-m")
    own = resolve_client(YAML_FAKE, slot="deliverable", storage=storage, settings=_settings())
    assert (own.source, own.provider, own.llm.generation_model) == ("saved", "anthropic", "cust-m")
    assert isinstance(own.client, KeywordEmbeddingClient)
    assert (
        resolve_client(YAML_FAKE, slot="product", storage=storage, settings=_settings()).provider == "openai"
    )


def test_the_product_never_uses_the_deliverables_service(storage: LocalStorage) -> None:
    _save(storage, "deliverable", provider="openai", api_key=KEY, model="m")
    with pytest.raises(AiNotConnectedError) as caught:
        resolve_client(YAML_FAKE, slot="product", storage=storage, settings=_settings())
    assert caught.value.slot == "product"


def test_an_unreadable_key_does_not_fall_through_to_another_provider(storage: LocalStorage) -> None:
    _save(storage, provider="openai", api_key=KEY, model="m")
    other = Settings(connections_key=SecretStr("D" * 40), allow_fake_ai=True)
    for llm in (YAML_FAKE, YAML_BEDROCK):
        with pytest.raises(AiNotConnectedError) as caught:
            resolve_client(llm, slot="product", storage=storage, settings=other)
        assert "can no longer be read" in caught.value.message and "enter the key again" in caught.value.fix
        assert KEY not in caught.value.message + caught.value.fix
    with pytest.raises(AiNotConnectedError):  # the deliverable inherits the same unreadable key
        resolve_client(YAML_FAKE, slot="deliverable", storage=storage, settings=other)


def test_a_saved_address_that_a_deployment_may_not_call_is_refused(
    storage: LocalStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    _save(storage, provider="openai_compatible", model="m", base_url="https://llm.corp.example/v1")
    prod = _settings(env="prod", cors_origins=("https://app.example.com",))

    def private(host: str, port: int, **_: object) -> list[tuple[object, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", port))]

    monkeypatch.setattr(socket, "getaddrinfo", private)
    with pytest.raises(AiNotConnectedError) as caught:
        resolve_client(YAML_FAKE, slot="product", storage=storage, settings=prod)
    assert "not allowed" in caught.value.message
    resolve_client(YAML_FAKE, slot="product", storage=storage, settings=_settings())  # a laptop may


def test_a_resolved_client_really_talks_to_its_service(storage: LocalStorage) -> None:
    with serve(lambda _: chat_reply("hi there", prompt_tokens=4, completion_tokens=2)) as openai:
        _save(storage, provider="openai_compatible", model="local-m", base_url=openai.url + "/v1")
        resolved = resolve_client(YAML_FAKE, slot="product", storage=storage, settings=_settings())
        completion = resolved.client.complete("hello", model_id=resolved.llm.generation_model, max_tokens=8)
    assert completion.text == "hi there" and completion.model_id == "local-m"
    assert openai.requests[0].body["model"] == "local-m" and "authorization" not in openai.requests[0].headers

    with serve(lambda _: message_reply("hello")) as anthropic:
        settings = _settings()
        save_service(
            AiServiceStore(storage, settings),
            "deliverable",
            ServiceInput(provider="anthropic", api_key=KEY, model="c-m", base_url=anthropic.url),
            settings=settings,
            storage=storage,
        )
        resolved = resolve_client(YAML_FAKE, slot="deliverable", storage=storage, settings=settings)
        assert isinstance(resolved.client, KeywordEmbeddingClient)
        assert resolved.client.complete("hi").text == "hello"
    assert anthropic.requests[0].headers["x-api-key"] == KEY
    stored = (storage.root / store_key("deliverable")).read_text()  # type: ignore[attr-defined]
    assert KEY not in stored and json.loads(stored)["provider"] == "anthropic"
