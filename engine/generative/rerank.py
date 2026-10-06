"""Re-ordering what passed the similarity floor with a cross-encoder (Plan I, DEC-1282).

Retrieval scores a passage against a question from two vectors computed apart: the question's and
the passage's, each embedded without seeing the other. A cross-encoder reads the question and the
passage *together* and scores how well one answers the other, which separates "mentions the same
plan" from "says what the plan costs" far better - at the price of one model pass per candidate,
which is why it only ever sees the few dozen candidates the search already returned.

Three rules, each the same as the local embedding model's (DEC-1263):

* **Optional, and off by default.** `generative.rag.rerank: none` changes nothing. `local` loads
  `BAAI/bge-reranker-base` through `sentence-transformers`' `CrossEncoder`, which is the
  `local-embeddings` extra - no second extra, no new pin.
* **Never a silent fallback.** A reranker that was asked for and cannot run is an `LLMError` with a
  plain message (`RERANKER_NOT_INSTALLED`, `RERANKER_UNAVAILABLE`), not an answer ranked some other
  way under a configuration that says it was reranked.
* **Ranking only.** The reranker re-orders what passed the floor; it never adds a passage the floor
  dropped and never lets one through it, so what counts as evidence keeps the meaning DEC-218
  calibrated. MMR then trades on the reranker's score (`Match.rank`), and a citation still shows the
  cosine.

It runs on this server and costs no money, so it is not a metered call: the meter counts what an AI
service bills. Its time is logged with the number of passages it read, and never their text.
"""

from __future__ import annotations

import dataclasses
import importlib
import math
import threading
import time
from collections.abc import Sequence
from typing import Any, Final, Protocol

from engine.generative.vectorstore import Match
from engine.llm import LLMError
from engine.utils.logging import get_logger, log_failure
from engine.utils.openmp import import_lightgbm_before_torch

__all__ = [
    "RERANKER_EXTRA",
    "RERANKER_NOT_INSTALLED",
    "RERANKER_UNAVAILABLE",
    "RERANK_MODEL_ID",
    "LocalReranker",
    "Reranker",
    "rerank",
    "reranker_for",
]

_LOGGER = get_logger(__name__)

RERANK_MODEL_ID: Final[str] = "BAAI/bge-reranker-base"
"""The open-source cross-encoder `rerank: local` uses. Its first use downloads it from Hugging Face."""

RERANKER_EXTRA: Final[str] = "local-embeddings"
"""The `pyproject.toml` extra that installs `sentence-transformers`, which the reranker shares."""

RERANKER_NOT_INSTALLED: Final[str] = "RERANKER_NOT_INSTALLED"
"""`LLMError.code` when `rerank: local` is set but `sentence-transformers` is not installed."""

RERANKER_UNAVAILABLE: Final[str] = "RERANKER_UNAVAILABLE"
"""`LLMError.code` when the library is there but the model could not be loaded or could not score."""

_MODELS: dict[str, Any] = {}
"""Loaded cross-encoders by id: a few hundred MB and seconds to load, so one per process."""

_MODELS_LOCK: Final[threading.Lock] = threading.Lock()
"""Held while a model loads, so two questions arriving together load it once rather than twice."""


class Reranker(Protocol):
    """Anything that scores (question, passage) pairs, higher meaning a better answer."""

    def scores(self, question: str, passages: Sequence[str]) -> list[float]:
        """One relevance score per passage, in the order given."""
        ...


def _cross_encoder(model_id: str) -> Any:
    """The loaded `CrossEncoder` for `model_id`, loaded on first use under a lock.

    A failure is an `LLMError` with a plain message and is not cached: installing the extra and
    restarting, or restoring network access for the first download, is enough to recover.
    """
    with _MODELS_LOCK:
        loaded = _MODELS.get(model_id)
        if loaded is not None:
            return loaded
        import_lightgbm_before_torch()  # sentence-transformers imports torch (DEC-1268)
        try:
            library = importlib.import_module("sentence_transformers")
        except ImportError as exc:
            raise LLMError(
                RERANKER_NOT_INSTALLED,
                "The passage reranker is not installed on this server. Install it with "
                f"pip install 'marketing-ai[{RERANKER_EXTRA}]' and restart, or set "
                "generative.rag.rerank to none.",
                model_id=model_id,
            ) from exc
        try:
            loaded = library.CrossEncoder(model_id)
        except Exception as exc:  # the library raises OSError, ValueError and HTTP errors alike
            log_failure(_LOGGER, "rerank.load", exc)
            raise LLMError(
                RERANKER_UNAVAILABLE,
                f"The passage reranker {model_id} could not be loaded. Its first use downloads it "
                "from Hugging Face, so this server needs internet access once (or a copy in its "
                "Hugging Face cache).",
                model_id=model_id,
            ) from exc
        _MODELS[model_id] = loaded
        return loaded


class LocalReranker:
    """`RERANK_MODEL_ID` (or another cross-encoder) running on this server."""

    def __init__(self, model_id: str = RERANK_MODEL_ID) -> None:
        self._model_id = model_id

    @property
    def model_id(self) -> str:
        """The cross-encoder this reranker scores with."""
        return self._model_id

    def scores(self, question: str, passages: Sequence[str]) -> list[float]:
        """One score per passage, 0 to 1; an `LLMError` when the model cannot run."""
        if not passages:
            return []
        model = _cross_encoder(self._model_id)
        try:
            raw = model.predict([(question, passage) for passage in passages])
            values = [float(value) for value in raw]
        except Exception as exc:
            log_failure(_LOGGER, "rerank.predict", exc)
            raise LLMError(
                RERANKER_UNAVAILABLE,
                f"The passage reranker {self._model_id} could not score the passages.",
                model_id=self._model_id,
            ) from exc
        return _unit_interval(values)


def _unit_interval(values: list[float]) -> list[float]:
    """`values` as 0..1 scores, so MMR can weigh them against its 0..1 repetition penalty.

    `sentence-transformers` applies a sigmoid itself for a single-label cross-encoder, and older
    releases returned raw logits; scores already inside 0..1 are kept as they are, and a set with any
    score outside it is passed through the sigmoid as a whole, so one question's passages are never
    put on two different scales.
    """
    if all(0.0 <= value <= 1.0 for value in values):
        return values
    return [1.0 / (1.0 + math.exp(-value)) for value in values]


def reranker_for(mode: str) -> Reranker | None:
    """The reranker `generative.rag.rerank` asks for: none, or the local cross-encoder."""
    return LocalReranker() if mode == "local" else None


def rerank(question: str, matches: Sequence[Match], reranker: Reranker) -> tuple[Match, ...]:
    """`matches` with their `rerank_score` set, best first. Nothing is added and nothing dropped."""
    if not matches:
        return ()
    started = time.monotonic()
    scored = reranker.scores(question, [match.chunk.text for match in matches])
    if len(scored) != len(matches):
        raise LLMError(
            RERANKER_UNAVAILABLE,
            "The passage reranker returned a different number of scores than it was given passages.",
        )
    reranked = [
        dataclasses.replace(match, rerank_score=round(score, 6))
        for match, score in zip(matches, scored, strict=True)
    ]
    reranked.sort(key=lambda match: -match.rank)
    _LOGGER.info(
        "retrieval.reranked passages=%d ms=%d", len(reranked), int((time.monotonic() - started) * 1000)
    )
    return tuple(reranked)
