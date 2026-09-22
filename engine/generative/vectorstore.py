"""Where an index's chunks and their vectors live, and the protocol that lets that change.

`VectorStore` is three operations - write an index, read its chunks, search it - and
`LocalVectorStore` is the Phase 3a implementation: Parquet for the chunks, Parquet for the vectors,
and cosine similarity in NumPy. Phase 4 swaps in OpenSearch Serverless behind the same protocol,
which is the reason the protocol exists at all; nothing above this module knows how a search is
done.

Two files rather than one, and it is worth saying why. A citation reads a chunk's text and its
heading; a question reads every vector. Keeping them apart means a citation does not pay to load a
float array it will not look at, and a search does not pay to load the prose. At index sizes worth
having that is the difference between a fast answer and a slow one.

Three properties the local implementation guarantees, because the rest of the engine leans on them:

**A search returns similarities, not ranks.** `retrieval` needs the number to apply a floor and to
trade relevance against diversity, and a store that returned only an order would make both
impossible.

**A zero vector matches nothing.** A chunk with no content word in it embeds to zero, and its
cosine similarity to everything is 0 rather than undefined. That is the honest answer for "nothing
to match on", and it keeps such a chunk below any sensible floor instead of at the top of an
arbitrary ordering.

**An index is written whole or not at all.** The write goes through `Storage`, whose writes are
atomic, so a crashed build leaves the previous index readable rather than half of a new one.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final, Protocol, runtime_checkable

import numpy as np
import numpy.typing as npt
import pandas as pd

from engine.generative.contracts import CHUNKS_FILENAME, EMBEDDINGS_FILENAME, Chunk
from engine.generative.errors import INDEX_EMPTY, INDEX_NOT_FOUND, generative_error
from engine.storage import Storage, StorageError, index_key
from engine.utils.logging import get_logger

__all__ = [
    "CHUNK_COLUMNS",
    "EMBEDDING_COLUMNS",
    "LocalVectorStore",
    "Match",
    "VectorStore",
    "chunk_ids",
    "cosine",
]

_LOGGER = get_logger(__name__)

CHUNK_COLUMNS: Final[tuple[str, ...]] = tuple(Chunk.model_fields)
"""The columns of `chunks.parquet`, taken from the contract so the two cannot drift."""

EMBEDDING_COLUMNS: Final[tuple[str, ...]] = ("chunk_id", "embedding")
"""The columns of `embeddings.parquet`. `embedding` is a list of floats, one row per chunk."""


@dataclass(frozen=True)
class Match:
    """One chunk a search returned, and how close it was to the question."""

    chunk: Chunk
    similarity: float


@runtime_checkable
class VectorStore(Protocol):
    """Everything the generative engine needs from a vector index.

    Deliberately small. A store writes an index, hands back its chunks, and answers a query vector
    with scored matches; anything richer - filters expressed in a query language, hybrid scoring,
    an update-in-place API - would be one provider's shape, and the two implementations this has to
    cover (NumPy over Parquet, and OpenSearch) agree on nothing beyond these.
    """

    def write(self, index_id: str, chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]]) -> None: ...

    def chunks(self, index_id: str) -> tuple[Chunk, ...]: ...

    def search(self, index_id: str, query: Sequence[float], *, top_k: int) -> tuple[Match, ...]: ...

    def exists(self, index_id: str) -> bool: ...


class LocalVectorStore:
    """`VectorStore` over two Parquet files and a NumPy dot product.

    Zero infrastructure, which is the point for Phase 3a: an index is a directory beside the runs,
    a developer can open it in pandas, and nothing has to be running for a test to search one.
    Vectors are held as a normalised matrix in memory for the length of a search and not cached
    between them - an index worth caching is an index worth putting in OpenSearch (Phase 4).
    """

    def __init__(self, storage: Storage) -> None:
        self._storage = storage

    def exists(self, index_id: str) -> bool:
        """True when both files of `index_id` are present."""
        return self._storage.exists(index_key(index_id, CHUNKS_FILENAME)) and self._storage.exists(
            index_key(index_id, EMBEDDINGS_FILENAME)
        )

    def write(self, index_id: str, chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]]) -> None:
        """Write both files. `chunks` and `vectors` are parallel and must be the same length."""
        if len(chunks) != len(vectors):
            raise ValueError(f"{len(chunks)} chunks and {len(vectors)} vectors are not parallel")
        chunk_frame = pd.DataFrame([chunk.model_dump() for chunk in chunks], columns=list(CHUNK_COLUMNS))
        vector_frame = pd.DataFrame(
            {
                "chunk_id": [chunk.chunk_id for chunk in chunks],
                "embedding": [list(vector) for vector in vectors],
            },
            columns=list(EMBEDDING_COLUMNS),
        )
        self._write_parquet(index_key(index_id, CHUNKS_FILENAME), chunk_frame)
        self._write_parquet(index_key(index_id, EMBEDDINGS_FILENAME), vector_frame)
        _LOGGER.info("vectorstore.written chunks=%d", len(chunks))

    def chunks(self, index_id: str) -> tuple[Chunk, ...]:
        """Every chunk of `index_id`, in the order it was written.

        A missing `page` makes the round trip as `NaN`, because Parquet has no nullable integer
        that pandas reads back as `None`. The contract says `int | None` and means it, so the
        absence is restored here, at the one boundary that knows the file is a file.
        """
        frame = self._read_parquet(index_id, CHUNKS_FILENAME)
        rows = frame.astype(object).where(pd.notna(frame), None).to_dict(orient="records")
        return tuple(Chunk.model_validate(row) for row in rows)

    def search(self, index_id: str, query: Sequence[float], *, top_k: int) -> tuple[Match, ...]:
        """The `top_k` chunks closest to `query`, most similar first.

        Both sides are L2-normalised before the dot product, so the result is a cosine whatever the
        embedding model's scale - and a zero vector, which cannot be normalised, keeps its zeros
        and therefore matches nothing.
        """
        if top_k < 1:
            raise ValueError(f"top_k must be at least 1, got {top_k}")
        chunks = self.chunks(index_id)
        if not chunks:
            raise generative_error(INDEX_EMPTY, index_id=index_id)
        matrix = self.matrix(index_id)
        question = _normalise(np.asarray(query, dtype=np.float64).reshape(1, -1))
        if question.shape[1] != matrix.shape[1]:
            raise ValueError(
                f"the question has {question.shape[1]} dimensions and the index has {matrix.shape[1]}"
            )
        scores = (matrix @ question.T).ravel()
        # `argsort` on the negated scores gives most-similar-first; ties keep index order, which is
        # reading order, so a search over identical chunks is still deterministic.
        order = np.argsort(-scores, kind="stable")[:top_k]
        return tuple(Match(chunk=chunks[int(index)], similarity=float(scores[index])) for index in order)

    def matrix(self, index_id: str) -> npt.NDArray[np.float64]:
        """The index's vectors as a normalised matrix, one row per chunk, in chunk order."""
        frame = self._read_parquet(index_id, EMBEDDINGS_FILENAME)
        if frame.empty:
            raise generative_error(INDEX_EMPTY, index_id=index_id)
        return _normalise(np.asarray([list(row) for row in frame["embedding"]], dtype=np.float64))

    # -- storage ------------------------------------------------------------
    def _write_parquet(self, key: str, frame: pd.DataFrame) -> None:
        with self._storage.open_write(key) as handle:
            frame.to_parquet(handle, index=False)

    def _read_parquet(self, index_id: str, filename: str) -> pd.DataFrame:
        key = index_key(index_id, filename)
        try:
            with self._storage.open_read(key) as handle:
                return pd.read_parquet(handle)
        except StorageError as exc:
            raise generative_error(INDEX_NOT_FOUND, index_id=index_id) from exc


def _normalise(matrix: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
    """Each row scaled to unit length; a zero row is left as zeros rather than divided by zero.

    Leaving it zero is the meaningful choice: its similarity to everything becomes 0, which is what
    "this chunk has nothing to match on" should score, and it sits below any floor worth setting.
    """
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    scaled: npt.NDArray[np.float64] = np.divide(matrix, norms, out=np.zeros_like(matrix), where=norms > 0)
    return scaled


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """Cosine similarity of two vectors; 0 when either has no length."""
    a = np.asarray(left, dtype=np.float64)
    b = np.asarray(right, dtype=np.float64)
    denominator = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(a @ b / denominator) if denominator else 0.0


def chunk_ids(chunks: Iterable[Chunk]) -> tuple[str, ...]:
    """The ids of `chunks`, in order - the shape a manifest and a citation both want."""
    return tuple(chunk.chunk_id for chunk in chunks)
