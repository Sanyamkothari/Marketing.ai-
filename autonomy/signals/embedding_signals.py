"""Text -> signals by nearest centroid on embeddings (plan-v2 section 2, "our own embeddings").

The marketer, or we, give a few example texts per label. Each label's centroid is the mean of its
examples' unit vectors; a text gets the label whose centroid is most similar by cosine, and a
probability from a softmax over the similarities divided by `temperature`. Cosine similarities sit
in a narrow band (often 0.6 to 0.9), so a small temperature (0.05 by default) is what lets a clear
winner read as confident; a larger one flattens every answer towards 1 / number of labels.

It is cheap (one embedding call per batch, no generation), deterministic for a deterministic
embedder, and cannot answer outside its labels. The `question` is not read: the examples define
what is being asked. A text the embedder maps to the zero vector (empty, or nothing it can read)
gets the last label with probability 1 / number of labels, the same "don't know" the LLM version
gives.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TypeAlias

import numpy as np
import numpy.typing as npt

from engine.llm import LLMClient

DEFAULT_TEMPERATURE = 0.05

_FloatArray: TypeAlias = npt.NDArray[np.float64]


def _unit_rows(vectors: Sequence[Sequence[float]]) -> _FloatArray:
    """Each row scaled to length 1; zero rows stay zero."""
    arr = np.asarray(vectors, dtype=np.float64)
    if arr.ndim != 2:
        raise ValueError("the embedder returned vectors of different lengths")
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    return np.divide(arr, norms, out=np.zeros_like(arr), where=norms > 0)


def softmax(scores: _FloatArray, temperature: float) -> _FloatArray:
    """Softmax of `scores / temperature`, shifted by the maximum so it never overflows."""
    z = scores / temperature
    z = z - z.max()
    e = np.exp(z)
    out: _FloatArray = e / e.sum()
    return out


class EmbeddingTextSignals:
    """`TextSignals` by nearest centroid over `llm.embed` vectors. See the module docstring."""

    def __init__(
        self,
        llm_for_embed: LLMClient,
        examples: Mapping[str, Sequence[str]],
        *,
        temperature: float = DEFAULT_TEMPERATURE,
        model_id: str | None = None,
        batch_size: int = 64,
    ) -> None:
        if temperature <= 0:
            raise ValueError("temperature must be above 0")
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        if not examples:
            raise ValueError("give at least one label with examples")
        empty = [label for label, texts in examples.items() if not texts]
        if empty:
            raise ValueError(f"labels without examples: {empty}")
        self._llm = llm_for_embed
        self._examples = {label: tuple(texts) for label, texts in examples.items()}
        self._temperature = temperature
        self._model_id = model_id
        self._batch_size = batch_size
        self._centroids: dict[str, _FloatArray] | None = None

    @property
    def labels(self) -> tuple[str, ...]:
        """The labels this classifier has examples for, in the order given."""
        return tuple(self._examples)

    def centroids(self) -> dict[str, _FloatArray]:
        """Each label's unit-length centroid. Embedded once, on first use, then kept."""
        if self._centroids is None:
            flat = [(label, text) for label, texts in self._examples.items() for text in texts]
            vectors = self._embed([text for _, text in flat])
            out: dict[str, _FloatArray] = {}
            for label in self._examples:
                rows = vectors[[i for i, (lab, _) in enumerate(flat) if lab == label]]
                out[label] = _unit_rows([rows.mean(axis=0).tolist()])[0]
            self._centroids = out
        return self._centroids

    def classify(
        self, texts: Sequence[str], *, question: str, labels: Sequence[str]  # noqa: ARG002
    ) -> list[tuple[str, float]]:
        wanted = tuple(labels)
        if not wanted:
            raise ValueError("labels must not be empty")
        missing = [label for label in wanted if label not in self._examples]
        if missing:
            raise ValueError(f"no examples for labels: {missing}")
        if not texts:
            return []
        centroids = self.centroids()
        matrix = np.stack([centroids[label] for label in wanted])
        vectors = self._embed(texts)
        out: list[tuple[str, float]] = []
        for vec in vectors:
            if not np.any(vec):
                out.append((wanted[-1], 1.0 / len(wanted)))
                continue
            probs = softmax(matrix @ vec, self._temperature)
            best = int(np.argmax(probs))  # ties go to the earlier label
            out.append((wanted[best], float(probs[best])))
        return out

    def _embed(self, texts: Sequence[str]) -> _FloatArray:
        rows: list[Sequence[float]] = []
        for start in range(0, len(texts), self._batch_size):
            chunk = texts[start : start + self._batch_size]
            got = self._llm.embed(chunk, model_id=self._model_id)
            if len(got) != len(chunk):
                raise ValueError(f"the embedder returned {len(got)} vectors for {len(chunk)} texts")
            rows.extend(got)
        return _unit_rows(rows)
