"""The local open-source embedding model (DEC-1263): chosen by name, honest when it cannot run.

`sentence-transformers` is an optional extra and is never downloaded in a test: every test here puts a
fake `sentence_transformers` module in `sys.modules` (or takes it away) and asserts what the client does
with it. The point being proved is the absence of a fallback - a missing or broken model is a coded,
plain-language `LLMError`, never a keyword-hash vector under the local model's name.
"""

from __future__ import annotations

import sys
import threading
import time
import types
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import ClassVar

import pytest
from pydantic import SecretStr

from engine import llm
from engine.ai_service import (
    PROVIDERS,
    AiServiceStore,
    KeywordEmbeddingClient,
    ServiceInput,
    build_service_client,
    resolve_client,
    save_service,
    uses_embedding_model,
)
from engine.config import LlmConfig
from engine.llm import (
    LOCAL_EMBEDDING_DIMENSIONS,
    LOCAL_EMBEDDING_MODEL_ID,
    LOCAL_EMBEDDINGS_NOT_INSTALLED,
    LOCAL_EMBEDDINGS_UNAVAILABLE,
    FakeLLMClient,
    LLMClient,
    LLMError,
    LocalEmbeddingClient,
)
from engine.settings import Settings
from engine.storage import LocalStorage

KEY = "zk-test-key-0123456789abcdef"


class FakeSentenceTransformer:
    """Stands in for `sentence_transformers.SentenceTransformer`; counts loads, records encodes."""

    loads = 0
    encoded: ClassVar[list[tuple[list[str], bool]]] = []

    def __init__(self, model_id: str) -> None:
        type(self).loads += 1
        time.sleep(0.05)  # long enough for a second thread to arrive while the first loads
        self.model_id = model_id

    def encode(self, texts: Sequence[str], *, normalize_embeddings: bool) -> list[list[float]]:
        type(self).encoded.append((list(texts), normalize_embeddings))
        return [[1.0 / (i + 1)] * LOCAL_EMBEDDING_DIMENSIONS for i, _ in enumerate(texts)]


@pytest.fixture(autouse=True)
def _no_loaded_model() -> Iterator[None]:
    llm._LOCAL_EMBEDDERS.clear()
    FakeSentenceTransformer.loads = 0
    FakeSentenceTransformer.encoded = []
    yield
    llm._LOCAL_EMBEDDERS.clear()


def _library(monkeypatch: pytest.MonkeyPatch, cls: type | None) -> None:
    """Install a fake `sentence_transformers` exposing `cls`, or make importing it fail (`None`)."""
    if cls is None:
        monkeypatch.setitem(sys.modules, "sentence_transformers", None)  # `import` raises ImportError
        return
    module = types.ModuleType("sentence_transformers")
    module.SentenceTransformer = cls  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)


def _client() -> LocalEmbeddingClient:
    return LocalEmbeddingClient(FakeLLMClient())


def test_the_local_client_is_an_llm_client_like_the_keyword_one() -> None:
    assert isinstance(_client(), LLMClient)
    assert isinstance(KeywordEmbeddingClient(FakeLLMClient()), LLMClient)
    assert _client().model_id == LOCAL_EMBEDDING_MODEL_ID


def test_embeddings_come_from_the_local_model_normalised(monkeypatch: pytest.MonkeyPatch) -> None:
    _library(monkeypatch, FakeSentenceTransformer)
    vectors = _client().embed(["refund policy", "opening hours"], model_id="ignored")
    assert len(vectors) == 2 and all(len(v) == LOCAL_EMBEDDING_DIMENSIONS for v in vectors)
    assert vectors[1][0] == 0.5
    assert FakeSentenceTransformer.encoded == [(["refund policy", "opening hours"], True)]
    assert _client().embed([]) == ()


def test_completions_and_token_counts_go_to_the_ai_service() -> None:
    inner = FakeLLMClient()
    client = LocalEmbeddingClient(inner)
    assert client.complete("hello").text == inner.complete("hello").text
    assert client.count_tokens("one two three") == inner.count_tokens("one two three")


def test_without_the_library_the_error_says_how_to_install_it_and_no_vector_is_invented(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _library(monkeypatch, None)
    with pytest.raises(LLMError) as caught:
        _client().embed(["refund policy"])
    assert caught.value.code == LOCAL_EMBEDDINGS_NOT_INSTALLED
    assert "pip install 'marketing-ai[local-embeddings]'" in caught.value.message
    assert caught.value.model_id == LOCAL_EMBEDDING_MODEL_ID


def test_a_model_that_cannot_load_is_a_plain_coded_error_and_is_retried_next_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Offline:
        def __init__(self, model_id: str) -> None:
            raise OSError("We couldn't connect to 'https://huggingface.co' to load this model")

    _library(monkeypatch, Offline)
    with pytest.raises(LLMError) as caught:
        _client().embed(["x"])
    assert caught.value.code == LOCAL_EMBEDDINGS_UNAVAILABLE
    assert "huggingface.co'" not in caught.value.message, "the library's own text is not shown"
    assert "internet access once" in caught.value.message
    _library(monkeypatch, FakeSentenceTransformer)
    assert len(_client().embed(["x"])[0]) == LOCAL_EMBEDDING_DIMENSIONS, "a failure is not remembered"


def test_a_failed_encode_is_a_coded_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken(FakeSentenceTransformer):
        def encode(self, texts: Sequence[str], *, normalize_embeddings: bool) -> list[list[float]]:
            raise RuntimeError("CUDA out of memory")

    _library(monkeypatch, Broken)
    with pytest.raises(LLMError) as caught:
        _client().embed(["x"])
    assert caught.value.code == LOCAL_EMBEDDINGS_UNAVAILABLE


def test_the_model_is_loaded_once_however_many_requests_arrive_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _library(monkeypatch, FakeSentenceTransformer)
    errors: list[BaseException] = []

    def embed() -> None:
        try:
            _client().embed(["x"])
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=embed) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert FakeSentenceTransformer.loads == 1


# --- choosing it ----------------------------------------------------------------------------------
def test_the_local_model_is_usable_with_every_provider_and_other_names_only_with_embeddings() -> None:
    for info in PROVIDERS.values():
        assert uses_embedding_model(info, LOCAL_EMBEDDING_MODEL_ID)
        assert uses_embedding_model(info, "some-embedder") is info.supports_embeddings
        assert not uses_embedding_model(info, "")
        assert info.local_embedding_model == LOCAL_EMBEDDING_MODEL_ID


def test_naming_the_local_model_builds_the_local_client_even_for_a_provider_without_embeddings() -> None:
    client = build_service_client(
        PROVIDERS["anthropic"],
        address="https://api.anthropic.example",
        api_key=KEY,
        model="m",
        embedding_model=LOCAL_EMBEDDING_MODEL_ID,
        region="",
        timeout_s=5,
        max_retries=0,
    )
    assert isinstance(client, LocalEmbeddingClient)


def test_a_saved_service_with_the_local_model_records_it_as_the_embedding_model(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path / "data")
    settings = Settings(connections_key=SecretStr("C" * 40))
    save_service(
        AiServiceStore(storage, settings),
        "product",
        ServiceInput(provider="anthropic", api_key=KEY, model="m", embedding_model=LOCAL_EMBEDDING_MODEL_ID),
        settings=settings,
        storage=storage,
    )
    resolved = resolve_client(LlmConfig(), slot="product", storage=storage, settings=settings)
    assert isinstance(resolved.client, LocalEmbeddingClient)
    assert resolved.llm.embedding_model == LOCAL_EMBEDDING_MODEL_ID
