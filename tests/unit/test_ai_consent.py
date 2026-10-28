"""The consent gate: a call that carries a client's data is refused, before anything is sent, until that client
has consented; a call that says it carries none is allowed (`engine.ai_consent`)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from pydantic import SecretStr

from engine.ai_consent import (
    AI_CONSENT_REQUIRED,
    AiConsentRequiredError,
    AiConsentStore,
    ClientData,
    ConsentGatedClient,
    NoClientData,
)
from engine.ai_service import resolve_client
from engine.config import LlmConfig
from engine.doctor.ai_guard import AiConsentStore as DoctorAiConsentStore
from engine.llm import FakeLLMClient, LLMCompletion
from engine.settings import Settings
from engine.storage import LocalStorage
from engine.utils.time import utc_now

CLIENT = "acme"


class Spy(FakeLLMClient):
    """Counts what reached the provider."""

    def __init__(self) -> None:
        super().__init__()
        self.reached = 0

    def complete(self, prompt: str, **kwargs: object) -> LLMCompletion:
        self.reached += 1
        return super().complete(prompt, **kwargs)  # type: ignore[arg-type]

    def embed(self, texts, **kwargs):  # type: ignore[no-untyped-def]
        self.reached += 1
        return super().embed(texts, **kwargs)

    def count_tokens(self, text: str, **kwargs: object) -> int:
        self.reached += 1
        return super().count_tokens(text, **kwargs)  # type: ignore[arg-type]


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path / "data")


def test_every_kind_of_call_is_refused_without_consent_and_nothing_reaches_the_provider(
    storage: LocalStorage, caplog: pytest.LogCaptureFixture
) -> None:
    spy = Spy()
    gated = ConsentGatedClient(spy, scope=ClientData(CLIENT), storage=storage)
    with caplog.at_level(logging.DEBUG):
        for call in (
            lambda: gated.complete("a secret prompt"),
            lambda: gated.embed(["a secret text"]),
            lambda: gated.count_tokens("a secret text"),
        ):
            with pytest.raises(AiConsentRequiredError) as refused:
                call()
            assert refused.value.code == AI_CONSENT_REQUIRED
            assert CLIENT not in refused.value.message and "secret" not in refused.value.message
    assert spy.reached == 0
    assert "secret" not in caplog.text and CLIENT not in caplog.text


def test_the_clients_yes_opens_the_gate_for_that_client_only(storage: LocalStorage) -> None:
    spy = Spy()
    AiConsentStore(storage).give(CLIENT, actor="u1", at=utc_now())
    ConsentGatedClient(spy, scope=ClientData(CLIENT), storage=storage).complete("hello")
    assert spy.reached == 1
    with pytest.raises(AiConsentRequiredError):
        ConsentGatedClient(spy, scope=ClientData("other"), storage=storage).complete("hello")
    assert spy.reached == 1


def test_a_call_that_says_it_carries_no_client_data_needs_no_consent() -> None:
    spy = Spy()
    ConsentGatedClient(spy, scope=NoClientData("a fixed greeting")).complete("hello")
    assert spy.reached == 1


def test_no_client_data_has_to_say_why_and_a_client_call_has_to_say_where_consent_is_kept() -> None:
    with pytest.raises(ValueError):
        NoClientData("  ")
    with pytest.raises(ValueError):
        ConsentGatedClient(Spy(), scope=ClientData(CLIENT))


def test_the_refusal_is_an_llm_error_so_every_fallback_to_the_rules_applies() -> None:
    from engine.llm import LLMError

    assert issubclass(AiConsentRequiredError, LLMError)


def test_there_is_one_consent_store_and_the_doctor_uses_it() -> None:
    assert DoctorAiConsentStore is AiConsentStore


def test_resolving_a_client_cannot_skip_the_consent_scope(storage: LocalStorage) -> None:
    settings = Settings(connections_key=SecretStr("C" * 40), allow_fake_ai=True)
    with pytest.raises(TypeError):
        resolve_client(LlmConfig(), slot="product", storage=storage, settings=settings)  # type: ignore[call-arg]
    ai = resolve_client(
        LlmConfig(), slot="product", storage=storage, settings=settings, consent=ClientData(CLIENT)
    )
    assert isinstance(ai.client, ConsentGatedClient)
    with pytest.raises(AiConsentRequiredError):
        ai.client.complete("hello")
