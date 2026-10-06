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

**Keyword scoring only ever re-orders; it never decides what counts as evidence.** A search given
the question's text and a `bm25_weight` above 0 ranks by a blend of the cosine and an Okapi BM25
score (DEC-1260), but every `Match` still carries the plain cosine as `similarity`, and that is the
number `retrieval` applies its floor to (DEC-1261). The BM25 statistics of an index are computed
once per distinct set of chunk texts and reused (DEC-1262).
"""

from __future__ import annotations

import functools
import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Protocol, runtime_checkable

import numpy as np
import numpy.typing as npt
import pandas as pd

from engine.generative.contracts import CHUNKS_FILENAME, EMBEDDINGS_FILENAME, Chunk
from engine.generative.errors import (
    INDEX_CORRUPT,
    INDEX_EMPTY,
    INDEX_NOT_FOUND,
    generative_error,
)
from engine.storage import Storage, StorageError, index_key
from engine.utils.logging import get_logger

__all__ = [
    "BM25_B",
    "BM25_K1",
    "CHUNK_COLUMNS",
    "EMBEDDING_COLUMNS",
    "Bm25Corpus",
    "LocalVectorStore",
    "Match",
    "VectorStore",
    "bm25_corpus",
    "bm25_scores",
    "bm25_tokens",
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
    """One chunk a search returned, and how close it was to the question.

    `similarity` is always the cosine between the question's vector and the chunk's - the number
    the similarity floor is defined on and the one a citation shows. A hybrid search also fills
    `keyword_score` (the chunk's BM25 score for the question, scaled to 0..1) and `hybrid_score`
    (the blend the search ranked by); a dense-only search leaves both `None` and ranks by
    `similarity` (DEC-1261). A reranked match also carries `rerank_score` (the cross-encoder's
    relevance, 0..1), which then decides its rank; the floor never reads it (DEC-1282).
    """

    chunk: Chunk
    similarity: float
    keyword_score: float | None = None
    hybrid_score: float | None = None
    rerank_score: float | None = None

    @property
    def rank(self) -> float:
        """The score this match is ranked by: the reranker's when there is one, else the hybrid
        blend when there is one, else the cosine."""
        if self.rerank_score is not None:
            return self.rerank_score
        return self.similarity if self.hybrid_score is None else self.hybrid_score


@runtime_checkable
class VectorStore(Protocol):
    """Everything the generative engine needs from a vector index.

    Deliberately small. A store writes an index, hands back its chunks, and answers a query vector
    with scored matches; anything richer - filters expressed in a query language, an
    update-in-place API - would be one provider's shape, and the two implementations this has to
    cover (NumPy over Parquet, and OpenSearch) agree on nothing beyond these.

    `search` may also be given the question's text and a `bm25_weight` (0 to 1). At 0 - the
    default - it is a pure vector search. Above 0 it ranks by `(1 - w) * cosine + w * keyword`,
    where `keyword` is a BM25 score scaled to 0..1, and it still reports the cosine as each
    match's `similarity` (DEC-1260, DEC-1261).
    """

    def write(self, index_id: str, chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]]) -> None: ...

    def chunks(self, index_id: str) -> tuple[Chunk, ...]: ...

    def search(
        self,
        index_id: str,
        query: Sequence[float],
        *,
        top_k: int,
        query_text: str | None = None,
        bm25_weight: float = 0.0,
    ) -> tuple[Match, ...]: ...

    def exists(self, index_id: str) -> bool: ...


class LocalVectorStore:
    """`VectorStore` over two Parquet files and a NumPy dot product.

    Zero infrastructure, which is the point for Phase 3a: an index is a directory beside the runs,
    a developer can open it in pandas, and nothing has to be running for a test to search one.
    Vectors are held as a normalised matrix in memory for the length of a search and not cached
    between them - an index worth caching is an index worth putting in OpenSearch (Phase 4). The
    one thing that is kept is an index's BM25 statistics, memoised by the chunk texts themselves
    (`bm25_corpus`), so a rebuilt index under the same id can never be scored with stale ones.
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

    def search(
        self,
        index_id: str,
        query: Sequence[float],
        *,
        top_k: int,
        query_text: str | None = None,
        bm25_weight: float = 0.0,
    ) -> tuple[Match, ...]:
        """The `top_k` best chunks for `query`, best first.

        Both sides are L2-normalised before the dot product, so `similarity` is a cosine whatever
        the embedding model's scale - and a zero vector, which cannot be normalised, keeps its zeros
        and therefore matches nothing.

        With `query_text` and a `bm25_weight` above 0 the order is by the hybrid score
        `(1 - bm25_weight) * cosine + bm25_weight * keyword` (DEC-1260); otherwise it is by the
        cosine alone, exactly as before hybrid search existed.
        """
        if top_k < 1:
            raise ValueError(f"top_k must be at least 1, got {top_k}")
        if not 0.0 <= bm25_weight <= 1.0:
            raise ValueError(f"bm25_weight must be between 0 and 1, got {bm25_weight}")
        chunks = self.chunks(index_id)
        if not chunks:
            raise generative_error(INDEX_EMPTY, index_id=index_id)
        matrix = self.matrix(index_id)
        if matrix.shape[0] != len(chunks):
            # The two files are written separately, so a crash between the writes - or an
            # overwrite of a live index id - can leave one new and one old. Row `i` of the matrix
            # would then be a different chunk's vector than `chunks[i]`, and every similarity in
            # the answer would be measured against the wrong passage while looking perfectly
            # ordinary. Saying so is the whole value of the check: silence here is a wrong answer
            # with a citation attached.
            raise generative_error(
                INDEX_CORRUPT, index_id=index_id, chunks=len(chunks), vectors=matrix.shape[0]
            )
        question = _normalise(np.asarray(query, dtype=np.float64).reshape(1, -1))
        if question.shape[1] != matrix.shape[1]:
            raise ValueError(
                f"the question has {question.shape[1]} dimensions and the index has {matrix.shape[1]}"
            )
        dense = (matrix @ question.T).ravel()
        if not (query_text and query_text.strip() and bm25_weight > 0.0):
            # `argsort` on the negated scores gives most-similar-first; ties keep index order, which
            # is reading order, so a search over identical chunks is still deterministic.
            order = np.argsort(-dense, kind="stable")[:top_k]
            return tuple(Match(chunk=chunks[int(index)], similarity=float(dense[index])) for index in order)
        keyword = bm25_corpus(tuple(_keyword_text(chunk) for chunk in chunks)).scores(query_text)
        hybrid = (1.0 - bm25_weight) * dense + bm25_weight * keyword
        order = np.argsort(-hybrid, kind="stable")[:top_k]
        return tuple(
            Match(
                chunk=chunks[int(index)],
                similarity=float(dense[index]),
                keyword_score=float(keyword[index]),
                hybrid_score=float(hybrid[index]),
            )
            for index in order
        )

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


BM25_K1: Final[float] = 1.5
"""Okapi BM25's term-frequency saturation: how quickly a word's tenth mention stops adding score."""

BM25_B: Final[float] = 0.75
"""Okapi BM25's length normalisation: 0 ignores a chunk's length, 1 divides fully by it."""

_TOKEN: Final[re.Pattern[str]] = re.compile(r"[^\W_]+")
"""A run of letters or digits, in any script, so a question not in English is not reduced to nothing."""


def bm25_tokens(text: str) -> list[str]:
    """`text` lower-cased and split into words, the one tokenisation both sides of BM25 use."""
    return _TOKEN.findall(text.lower())


def _keyword_text(chunk: Chunk) -> str:
    """What BM25 reads of a chunk: its heading and its text, so a question naming a section matches."""
    return f"{chunk.section}\n{chunk.text}"


@dataclass(frozen=True)
class Bm25Corpus:
    """The statistics Okapi BM25 needs about a set of chunks, computed once.

    `term_counts[i]` is chunk `i`'s word counts, `lengths[i]` its word count, and
    `document_frequency[w]` how many chunks contain `w`. Nothing here depends on the question, so
    one `Bm25Corpus` answers every question asked of an index.
    """

    term_counts: tuple[Mapping[str, int], ...]
    lengths: tuple[int, ...]
    document_frequency: Mapping[str, int]
    average_length: float

    @property
    def size(self) -> int:
        """How many chunks the statistics describe."""
        return len(self.lengths)

    def idf(self, term: str) -> float:
        """Lucene's non-negative inverse document frequency, `ln(1 + (N - n + 0.5) / (n + 0.5))`."""
        n = self.document_frequency.get(term, 0)
        return math.log(1.0 + (self.size - n + 0.5) / (n + 0.5))

    def scores(self, query: str, *, k1: float = BM25_K1, b: float = BM25_B) -> npt.NDArray[np.float64]:
        """Each chunk's BM25 score for `query`, scaled to 0..1, in chunk order.

        A question's words are counted once each: asking "plan plan plan" is not three times as
        much about plans. Words that appear in no chunk are left out of the sum and out of the
        scale - they cannot favour one chunk over another, and counting them would only shrink
        every score by the same factor.

        The scale divides by `sum(idf(w) * (k1 + 1))` over the question's words, the score a chunk
        would approach if it contained every one of them infinitely often. The result is therefore
        in [0, 1) and comparable in size to a cosine, which is what lets the two be blended.
        """
        terms = [term for term in dict.fromkeys(bm25_tokens(query)) if term in self.document_frequency]
        result = np.zeros(self.size, dtype=np.float64)
        if not terms or self.average_length <= 0.0:
            return result
        weights = {term: self.idf(term) for term in terms}
        ceiling = sum(weight * (k1 + 1.0) for weight in weights.values())
        for index, (counts, length) in enumerate(zip(self.term_counts, self.lengths, strict=True)):
            norm = k1 * (1.0 - b + b * length / self.average_length)
            total = 0.0
            for term, weight in weights.items():
                frequency = counts.get(term, 0)
                if frequency:
                    total += weight * frequency * (k1 + 1.0) / (frequency + norm)
            result[index] = total / ceiling
        return result


def bm25_corpus(texts: Sequence[str]) -> Bm25Corpus:
    """The BM25 statistics of `texts`, memoised by the texts themselves (DEC-1262).

    The cache key is the full tuple of texts, not an index id: an index rebuilt under the same id
    has different texts and therefore different statistics, and a stale entry cannot be served.
    Python hashes and compares the tuple in C, a small fraction of the cost of tokenising it.
    """
    return _bm25_corpus(tuple(texts))


@functools.lru_cache(maxsize=16)
def _bm25_corpus(texts: tuple[str, ...]) -> Bm25Corpus:
    term_counts = tuple(Counter(bm25_tokens(text)) for text in texts)
    lengths = tuple(sum(counts.values()) for counts in term_counts)
    document_frequency: Counter[str] = Counter()
    for counts in term_counts:
        document_frequency.update(counts.keys())
    average = sum(lengths) / len(lengths) if lengths else 0.0
    return Bm25Corpus(
        term_counts=term_counts,
        lengths=lengths,
        document_frequency=dict(document_frequency),
        average_length=average,
    )


def bm25_scores(
    corpus_texts: Sequence[str],
    query: str,
    *,
    k1: float = BM25_K1,
    b: float = BM25_B,
) -> npt.NDArray[np.float64]:
    """Okapi BM25 scores of `query` against each of `corpus_texts`, scaled to 0..1 (`Bm25Corpus.scores`)."""
    return bm25_corpus(corpus_texts).scores(query, k1=k1, b=b)


def chunk_ids(chunks: Iterable[Chunk]) -> tuple[str, ...]:
    """The ids of `chunks`, in order - the shape a manifest and a citation both want."""
    return tuple(chunk.chunk_id for chunk in chunks)
