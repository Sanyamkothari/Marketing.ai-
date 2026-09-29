"""Saving, encrypting and describing an AI service: the key exists in one place and never leaves it."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from pydantic import SecretStr

from engine.ai_service import (
    AiServiceError,
    AiServiceStore,
    ServiceInput,
    build_state,
    build_states,
    candidate_from,
    key_for,
    key_store_problem,
    save_service,
    store_key,
)
from engine.settings import Settings
from engine.storage import LocalStorage

KEY = "zk-test-key-0123456789abcdef"
OTHER = "zk-test-other-9876543210fedcba"
ENC_A = "A" * 40  # a generated secret (letters and digits): the Connections key derives from it
ENC_B = "B" * 40


def _settings(secret: str = ENC_A, **kw: object) -> Settings:
    return Settings(connections_key=SecretStr(secret), **kw)  # type: ignore[arg-type]


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path / "data")


def _save(storage: LocalStorage, settings: Settings, slot: str = "product", **body: object) -> None:
    save_service(AiServiceStore(storage, settings), slot, ServiceInput(**body), settings=settings, storage=storage)  # type: ignore[arg-type]


def _everything_on_disk(root: Path) -> str:
    return "".join(p.read_bytes().decode("utf-8", "ignore") for p in root.rglob("*") if p.is_file())


def test_the_key_is_encrypted_on_disk_and_absent_from_every_state(
    storage: LocalStorage, tmp_path: Path
) -> None:
    settings = _settings()
    _save(storage, settings, provider="openai", api_key=KEY, model="m-1")
    on_disk = json.loads((tmp_path / "data" / store_key("product")).read_text())
    assert on_disk["secrets_token"] and on_disk["key_fingerprint"] and on_disk["provider"] == "openai"
    assert KEY not in _everything_on_disk(tmp_path)
    states = build_states(storage, settings)
    dumped = states.model_dump_json()
    assert KEY not in dumped and "secrets_token" not in dumped and "api_key" not in dumped
    product = states.slots["product"]
    assert product.has_key and product.key_readable and product.connected and product.source == "saved"
    assert (product.provider, product.provider_label, product.model, product.third_party) == (
        "openai",
        "OpenAI",
        "m-1",
        True,
    )
    assert product.base_url == "https://api.openai.com/v1" and product.last_test is None


def test_the_key_can_be_read_back_only_by_the_store(storage: LocalStorage) -> None:
    settings = _settings()
    _save(storage, settings, provider="openai", api_key=KEY, model="m-1")
    store = AiServiceStore(storage, settings)
    record = store.read("product")
    assert record is not None and store.api_key(record) == KEY
    assert KEY not in repr(record) and KEY not in record.model_dump_json()


def test_a_changed_connections_key_is_a_clear_error_never_garbage(storage: LocalStorage) -> None:
    _save(storage, _settings(ENC_A), provider="openai", api_key=KEY, model="m-1")
    other = AiServiceStore(storage, _settings(ENC_B))
    record = other.read("product")
    assert record is not None
    assert other.key_readable(record) is False
    with pytest.raises(AiServiceError) as caught:
        other.api_key(record)
    assert caught.value.status == 409 and caught.value.code == "AI_KEY_UNREADABLE"
    assert "MARKETING_AI_CONNECTIONS_KEY" in caught.value.message and "enter the key again" in (
        caught.value.fix or ""
    )
    assert KEY not in caught.value.message + (caught.value.fix or "") and KEY not in str(caught.value)
    state = build_state(storage, _settings(ENC_B), "product")
    assert (
        state.has_key and state.key_readable is False and state.connected is False and state.source == "saved"
    )


def test_a_tampered_token_is_the_same_clear_error(storage: LocalStorage, tmp_path: Path) -> None:
    settings = _settings()
    _save(storage, settings, provider="openai", api_key=KEY, model="m-1")
    path = tmp_path / "data" / store_key("product")
    document = json.loads(path.read_text())
    document["secrets_token"] = document["secrets_token"][:-6] + "AAAAAA"
    path.write_text(json.dumps(document))
    store = AiServiceStore(storage, settings)
    record = store.read("product")
    assert record is not None
    with pytest.raises(AiServiceError) as caught:
        store.api_key(record)
    assert caught.value.code == "AI_KEY_UNREADABLE"


def test_nothing_that_is_logged_carries_the_key(
    storage: LocalStorage, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    settings = _settings()
    _save(storage, settings, provider="openai", api_key=KEY, model="m-1")
    build_states(storage, settings)
    with pytest.raises(AiServiceError) as caught:  # the saved key cannot be read: it must be typed again
        _save(storage, _settings(ENC_B), provider="openai", model="m-2")
    assert (
        caught.value.code == "AI_KEY_REQUIRED" and "enter the key again" in (caught.value.fix or "").lower()
    )
    assert KEY not in caplog.text


def test_a_blank_key_keeps_the_saved_one_for_the_same_provider_and_address(storage: LocalStorage) -> None:
    settings = _settings()
    store = AiServiceStore(storage, settings)
    _save(storage, settings, provider="openai", api_key=KEY, model="m-1")
    before = store.read("product")
    _save(storage, settings, provider="openai", api_key="  ", model="m-2", embedding_model="e-1")
    after = store.read("product")
    assert before and after and after.secrets_token == before.secrets_token and after.model == "m-2"
    assert store.api_key(after) == KEY and after.last_test is None


def test_a_saved_key_never_follows_a_changed_provider_or_address(storage: LocalStorage) -> None:
    settings = _settings()
    _save(storage, settings, provider="openai", api_key=KEY, model="m-1")
    for body in (
        {"provider": "openrouter", "model": "m"},
        {"provider": "openai", "model": "m", "base_url": "https://llm.example.com/v1"},
        {"provider": "openai_compatible", "model": "m", "base_url": "https://llm.example.com/v1"},
    ):
        needs = body["provider"] != "openai_compatible"
        if needs:
            with pytest.raises(AiServiceError) as caught:
                _save(storage, settings, **body)
            assert caught.value.code == "AI_KEY_REQUIRED" and caught.value.field == "api_key"
        else:
            _save(storage, settings, **body)  # key optional for a compatible server
            assert AiServiceStore(storage, settings).read("product").secrets_token is None  # type: ignore[union-attr]
        _save(storage, settings, provider="openai", api_key=KEY, model="m-1")
    # and for a test or a model listing, which use the saved key only for the same place
    store = AiServiceStore(storage, settings)
    record = store.read("product")
    same = candidate_from(ServiceInput(model="m-9"), saved=record, settings=settings, partial=True)
    assert key_for(store, record, same) == KEY
    moved = candidate_from(
        ServiceInput(base_url="https://llm.example.com/v1"), saved=record, settings=settings, partial=True
    )
    assert key_for(store, record, moved) is None
    typed = candidate_from(
        ServiceInput(api_key=OTHER, model="m"), saved=record, settings=settings, partial=True
    )
    assert key_for(store, record, typed) == OTHER


def test_required_fields_name_the_field_not_the_value(storage: LocalStorage) -> None:
    settings = _settings()
    cases = [
        ({}, "provider", "AI_FIELD_REQUIRED"),
        ({"provider": "nope", "model": "m"}, "provider", "AI_PROVIDER_UNKNOWN"),
        ({"provider": "openai"}, "model", "AI_FIELD_REQUIRED"),
        ({"provider": "openai", "model": "m"}, "api_key", "AI_KEY_REQUIRED"),
        ({"provider": "openai_compatible", "model": "m"}, "base_url", "AI_FIELD_REQUIRED"),
        (
            {"provider": "openai_compatible", "model": "m", "base_url": "http://example.com"},
            "base_url",
            "AI_BASE_URL_INVALID",
        ),
        ({"provider": "bedrock", "model": "m"}, "region", "AI_FIELD_REQUIRED"),
        ({"provider": "bedrock", "model": "m", "region": "mars"}, "region", "AI_FIELD_INVALID"),
        (
            {"provider": "bedrock", "model": "m", "region": "us-east-1", "api_key": KEY},
            "api_key",
            "AI_KEY_NOT_USED",
        ),
        ({"provider": "openai", "model": "m", "api_key": "two words"}, "api_key", "AI_KEY_INVALID"),
        ({"provider": "openai", "model": "m\nx", "api_key": KEY}, "model", "AI_FIELD_INVALID"),
    ]
    for body, field, code in cases:
        with pytest.raises(AiServiceError) as caught:
            _save(storage, settings, **body)
        assert (caught.value.status, caught.value.field, caught.value.code) == (422, field, code), body
        for value in (KEY, "two words"):
            assert value not in caught.value.message
    assert AiServiceStore(storage, settings).read("product") is None  # nothing half-saved


def test_bedrock_needs_no_key_and_keeps_region_and_models(storage: LocalStorage) -> None:
    settings = _settings()
    _save(storage, settings, provider="bedrock", model="m", embedding_model="e", region="ap-south-1")
    state = build_state(storage, settings, "product")
    assert state.connected and not state.has_key and state.third_party is False
    assert (state.region, state.embedding_model, state.base_url) == ("ap-south-1", "e", None)


def test_a_provider_without_embeddings_drops_the_embedding_model(storage: LocalStorage) -> None:
    settings = _settings()
    _save(storage, settings, provider="anthropic", api_key=KEY, model="m", embedding_model="ignored")
    state = build_state(storage, settings, "product")
    assert state.embedding_model is None and state.base_url == "https://api.anthropic.com"


def test_slots_are_separate_files_and_the_deliverable_inherits_the_product(
    storage: LocalStorage, tmp_path: Path
) -> None:
    settings = _settings()
    empty = build_states(storage, settings)
    assert not empty.slots["product"].connected and empty.slots["deliverable"].source == "none"
    assert empty.slots["deliverable"].inherits_product and not empty.slots["product"].inherits_product
    _save(storage, settings, "product", provider="openai", api_key=KEY, model="m-p")
    states = build_states(storage, settings)
    deliverable = states.slots["deliverable"]
    assert deliverable.source == "inherited" and deliverable.connected and deliverable.model == "m-p"
    assert deliverable.inherits_product and deliverable.has_key
    assert not (tmp_path / "data" / store_key("deliverable")).exists()
    _save(storage, settings, "deliverable", provider="anthropic", api_key=OTHER, model="m-d")
    states = build_states(storage, settings)
    assert (
        states.slots["deliverable"].source == "saved" and states.slots["deliverable"].provider == "anthropic"
    )
    assert not states.slots["deliverable"].inherits_product and states.slots["product"].provider == "openai"
    assert KEY not in states.model_dump_json() and OTHER not in states.model_dump_json()
    AiServiceStore(storage, settings).delete("deliverable")
    assert build_states(storage, settings).slots["deliverable"].source == "inherited"
    with pytest.raises(AiServiceError) as caught:
        _save(
            storage, settings, "deliverable", provider="openai", model="m"
        )  # a key is not borrowed by saving
    assert caught.value.code == "AI_KEY_REQUIRED"


def test_the_product_never_inherits_the_deliverable(storage: LocalStorage) -> None:
    settings = _settings()
    _save(storage, settings, "deliverable", provider="openai", api_key=KEY, model="m")
    states = build_states(storage, settings)
    assert states.slots["product"].source == "none" and not states.slots["product"].connected
    assert states.slots["deliverable"].connected


def test_the_state_says_when_saving_is_locked(storage: LocalStorage, tmp_path: Path) -> None:
    prod = Settings(env="prod", cors_origins=("https://app.example.com",))
    assert "MARKETING_AI_CONNECTIONS_KEY" in (key_store_problem(prod, storage) or "")
    state = build_state(storage, prod, "product")
    assert state.editable is False and "MARKETING_AI_CONNECTIONS_KEY" in (state.locked_reason or "")
    with pytest.raises(AiServiceError) as caught:
        save_service(
            AiServiceStore(storage, prod),
            "product",
            ServiceInput(provider="bedrock", model="m", region="us-east-1"),
            settings=prod,
            storage=storage,
        )
    assert caught.value.status == 409 and caught.value.code == "AI_SERVICE_LOCKED"
    assert (
        key_store_problem(_settings(env="prod", cors_origins=("https://app.example.com",)), storage) is None
    )
    assert key_store_problem(Settings(), storage) is None  # a laptop generates its own key file


def test_an_unreadable_record_file_reads_as_not_saved(storage: LocalStorage) -> None:
    storage.write_text(store_key("product"), "{not json")
    assert AiServiceStore(storage, _settings()).read("product") is None


def test_the_test_model_makes_a_slot_usable_only_when_a_test_allows_it(storage: LocalStorage) -> None:
    off = build_state(storage, _settings(), "product")
    assert (off.connected, off.source, off.provider) == (False, "none", None)
    on = build_state(storage, _settings(allow_fake_ai=True), "deliverable")
    assert (on.connected, on.source, on.provider, on.third_party) == (True, "config", "fake", False)
    _save(
        storage, _settings(), "deliverable", provider="openai", api_key=KEY, model="m"
    )  # a saved service wins
    saved = build_state(storage, _settings(allow_fake_ai=True), "deliverable")
    assert (saved.source, saved.provider) == ("saved", "openai")


def test_a_late_test_result_does_not_overwrite_a_newer_setting_or_bring_back_a_deleted_one(
    tmp_path: Path,
) -> None:
    """A test outlives the record it began with; recording its result must not clobber what came after."""
    from datetime import UTC, datetime

    from engine.ai_service import AiServiceRecord, AiServiceStore, LastTest

    def _record(model: str) -> AiServiceRecord:
        return AiServiceRecord(provider="openai_compatible", model=model, updated_at=datetime.now(UTC))

    store = AiServiceStore(LocalStorage(tmp_path), Settings(connections_key=SecretStr("F" * 40)))
    old = _record("old-m")
    store.write("product", old)
    result = LastTest(ok=True, message="ready", fix=None, latency_ms=5, tested_at=datetime.now(UTC))

    store.write("product", _record("new-m"))  # saved while the test was running
    store.record_test("product", old, result)
    kept = store.read("product")
    assert kept is not None and kept.model == "new-m" and kept.last_test is None

    store.delete("product")  # disconnected while another test was running
    store.record_test("product", old, result)
    assert store.read("product") is None

    store.write("product", old)  # the same settings still there: the result is recorded
    store.record_test("product", old, result)
    saved = store.read("product")
    assert saved is not None and saved.last_test is not None and saved.last_test.ok
