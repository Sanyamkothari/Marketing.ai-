"""`engine.generative.rerank`: an optional cross-encoder that re-orders, never admits, never falls back.

No model is downloaded: a fake `sentence_transformers` module is put in `sys.modules` (or taken
away), exactly as the local embedding tests do (DEC-1263), and what is asserted is what the engine
does with the scores it is handed.
"""

from __future__ import annotations

import sys
import threading
import types
from collections.abc import Sequence
from typing import ClassVar

import pytest

from engine.config import RagConfig
from engine.generative import rerank as rerank_module
from engine.generative.contracts import Chunk
from engine.generative.rerank import (
    RERANKER_NOT_INSTALLED,
    RERANKER_UNAVAILABLE,
    LocalReranker,
    rerank,
    reranker_for,
)
from engine.generative.retrieval import retrieve
from engine.generative.vectorstore import Match
from engine.llm import LLMError


def chunk(text: str, ordinal: int) -> Chunk:
    return Chunk(
        chunk_id=f"d-{ordinal:05d}",
        doc_id="d",
        document="d.md",
        section="s",
        ordinal=ordinal,
        tokens=len(text.split()),
        text=text,
    )


class Store:
    """A store that returns fixed matches, so the floor and the order are the test's own."""

    def __init__(self, matches: Sequence[Match]) -> None:
        self._matches = tuple(matches)

    def search(self, index_id: str, vector: Sequence[float], **_: object) -> tuple[Match, ...]:
        del index_id, vector
        return self._matches


class ByWord:
    """A reranker that scores a passage 0.9 when it contains `word`, else 0.1."""

    def __init__(self, word: str) -> None:
        self.word = word
        self.seen: list[list[str]] = []

    def scores(self, question: str, passages: Sequence[str]) -> list[float]:
        del question
        self.seen.append(list(passages))
        return [0.9 if self.word in passage else 0.1 for passage in passages]


MATCHES = (
    Match(chunk=chunk("alpha opening hours", 0), similarity=0.80),
    Match(chunk=chunk("beta refund policy", 1), similarity=0.60),
    Match(chunk=chunk("gamma refund is twenty one days", 2), similarity=0.10),  # below the floor
)


def config(**overrides: object) -> RagConfig:
    values: dict[str, object] = {"top_k": 2, "min_similarity": 0.25, "mmr_lambda": 1.0, "bm25_weight": 0.0}
    values.update(overrides)
    return RagConfig(**values)


def test_rerank_is_off_by_default() -> None:
    assert RagConfig().rerank == "none"
    assert reranker_for("none") is None
    assert isinstance(reranker_for("local"), LocalReranker)


def test_the_reranker_reorders_what_passed_the_floor_and_admits_nothing_below_it() -> None:
    reranker = ByWord("refund")
    found = retrieve(
        Store(MATCHES), "i", [1.0], config=config(), question_text="refund?", reranker=reranker  # type: ignore[arg-type]
    )
    assert [m.chunk.ordinal for m in found.matches] == [1, 0], "the refund passage now comes first"
    assert reranker.seen == [["alpha opening hours", "beta refund policy"]], "the floor ran first"
    assert all(m.chunk.ordinal != 2 for m in found.matches)
    assert found.matches[0].similarity == 0.60, "a citation still shows the cosine"
    assert found.matches[0].rerank_score == 0.9


def test_without_the_question_text_nothing_is_reranked() -> None:
    reranker = ByWord("refund")
    found = retrieve(Store(MATCHES), "i", [1.0], config=config(), reranker=reranker)  # type: ignore[arg-type]
    assert reranker.seen == []
    assert [m.chunk.ordinal for m in found.matches] == [0, 1]


def test_a_reranker_that_returns_the_wrong_number_of_scores_is_an_error() -> None:
    class Short:
        def scores(self, question: str, passages: Sequence[str]) -> list[float]:
            return [0.5]

    with pytest.raises(LLMError) as caught:
        rerank("q", MATCHES[:2], Short())
    assert caught.value.code == RERANKER_UNAVAILABLE


# ---------------------------------------------------------------------------
# The local cross-encoder, with a fake library
# ---------------------------------------------------------------------------
class FakeCrossEncoder:
    loads = 0
    output: ClassVar[list[float]] = []

    def __init__(self, model_id: str) -> None:
        type(self).loads += 1
        self.model_id = model_id

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        return type(self).output[: len(pairs)]


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rerank_module, "_MODELS", {})
    FakeCrossEncoder.loads = 0
    FakeCrossEncoder.output = [0.2, 0.7]


def _library(monkeypatch: pytest.MonkeyPatch, cls: type | None) -> None:
    if cls is None:
        monkeypatch.setitem(sys.modules, "sentence_transformers", None)  # `import` raises ImportError
        return
    module = types.ModuleType("sentence_transformers")
    module.CrossEncoder = cls  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)


def test_not_installed_is_a_coded_error_with_the_fix_and_never_a_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _library(monkeypatch, None)
    with pytest.raises(LLMError) as caught:
        retrieve(Store(MATCHES), "i", [1.0], config=config(rerank="local"), question_text="refund?")  # type: ignore[arg-type]
    assert caught.value.code == RERANKER_NOT_INSTALLED
    assert "pip install 'marketing-ai[local-embeddings]'" in caught.value.message


def test_a_model_that_will_not_load_is_unavailable_and_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken(FakeCrossEncoder):
        def __init__(self, model_id: str) -> None:
            raise OSError("no network")

    _library(monkeypatch, Broken)
    with pytest.raises(LLMError) as caught:
        LocalReranker().scores("q", ["a"])
    assert caught.value.code == RERANKER_UNAVAILABLE
    assert "no network" not in caught.value.message
    _library(monkeypatch, FakeCrossEncoder)
    assert LocalReranker().scores("q", ["a", "b"]) == [0.2, 0.7]


def test_the_local_reranker_orders_by_the_models_scores(monkeypatch: pytest.MonkeyPatch) -> None:
    _library(monkeypatch, FakeCrossEncoder)
    found = retrieve(Store(MATCHES), "i", [1.0], config=config(rerank="local"), question_text="refund?")  # type: ignore[arg-type]
    assert [m.chunk.ordinal for m in found.matches] == [1, 0]


def test_raw_logits_are_put_through_a_sigmoid_as_a_whole(monkeypatch: pytest.MonkeyPatch) -> None:
    _library(monkeypatch, FakeCrossEncoder)
    FakeCrossEncoder.output = [-2.0, 3.0]
    low, high = LocalReranker().scores("q", ["a", "b"])
    assert 0.0 < low < 0.5 < high < 1.0


def test_concurrent_first_uses_load_the_model_once(monkeypatch: pytest.MonkeyPatch) -> None:
    _library(monkeypatch, FakeCrossEncoder)
    threads = [threading.Thread(target=lambda: LocalReranker().scores("q", ["a"])) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert FakeCrossEncoder.loads == 1
