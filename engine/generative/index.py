"""Building a knowledge index: parse, chunk, embed, write, and say what happened.

An index is the generative counterpart of a trained model - the thing an answer is produced from -
so it is built like one: as a job with visible stages, writing a manifest that a reader can ask
"which documents, which settings, when?" of, and leaving the previous index readable until the new
one is whole.

Three decisions shape this module.

**A rebuild is incremental by content.** Each document carries a `fingerprint` of its bytes. On a
rebuild, a document whose fingerprint has not moved keeps the chunks already in the index rather
than being re-chunked and re-embedded, because chunking is deterministic and embedding is the
expensive part. What that saves is the whole point of the fingerprint: re-indexing a two-hundred
document knowledge base after one edit costs one document's embeddings.

**A document that fails to parse does not fail the build.** One corrupt PDF in a knowledge base of
two hundred should cost that PDF, not the index. Each failure is recorded against its own document
in the manifest with the code that explains it, and the build carries on; a build where *every*
document failed is the one that raises, because an index with no chunks can answer nothing.

**PII in the documents is a warning, never a redaction.** These are the client's own published
documents, and a plan's page that legitimately prints a support address would be damaged by
redaction and useless without it. So the manifest carries `PII_IN_DOCS` naming the kinds found, and
the text is indexed as written. That is the opposite of the rule for *complaint* text, which is a
customer's words and is always redacted - the difference is whose data it is (DEC-216).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Final

from engine.config import GenerativeConfig, UseCaseConfig
from engine.generative import parsers, redaction
from engine.generative.budget import Meter
from engine.generative.chunking import chunk_document, embedding_text, fingerprint
from engine.generative.contracts import (
    DOC_INDEX_MANIFEST_FILENAME,
    Chunk,
    ChunkConfig,
    DocIndexManifest,
    IndexedDocument,
)
from engine.generative.errors import (
    DOCUMENT_TYPE_UNSUPPORTED,
    INDEX_EMPTY,
    KNOWLEDGE_BASE_TOO_LARGE,
    generative_error,
)
from engine.generative.prompts import prompt_versions
from engine.generative.vectorstore import VectorStore
from engine.storage import Storage, index_key
from engine.utils.logging import get_logger, log_stage
from engine.utils.time import utc_now

__all__ = [
    "ANSWER_PROMPTS",
    "BYTES_PER_MB",
    "EMBED_BATCH",
    "PII_IN_DOCS",
    "BuildResult",
    "build_index",
    "read_manifest",
]

_LOGGER = get_logger(__name__)

PII_IN_DOCS: Final[str] = "PII_IN_DOCS"
"""The index-wide warning raised when a document carries something a detector recognises.

Names the kinds and never a value, and changes nothing about what is indexed: these are the
client's own documents, and redacting a published support address would damage the answer that
address is the point of (DEC-216).
"""

BYTES_PER_MB: Final[int] = 1024 * 1024
EMBED_BATCH: Final[int] = 64
"""Chunks per embedding call. One call per chunk would meter hundreds of requests for one build."""

ANSWER_PROMPTS: Final[tuple[str, ...]] = ("assistant_answer", "judge_faithfulness")
"""The prompts an answer from this index will use, recorded so a manifest names them."""


@dataclass(frozen=True)
class BuildResult:
    """What a build produced: the manifest, and the chunks that went into the store."""

    manifest: DocIndexManifest
    chunks: tuple[Chunk, ...] = field(default=())

    @property
    def index_id(self) -> str:
        """The index that was built."""
        return self.manifest.index_id


def build_index(
    paths: Sequence[Path],
    *,
    index_id: str,
    use_case: UseCaseConfig,
    storage: Storage,
    store: VectorStore,
    meter: Meter,
    client_id: str | None = None,
    previous: DocIndexManifest | None = None,
    config_root: Path | None = None,
    now: datetime | None = None,
) -> BuildResult:
    """Parse, chunk, embed and write every document in `paths`; return the manifest.

    `previous` is the manifest of the index being replaced. When a document's fingerprint appears
    in it unchanged, that document's chunks are taken from the existing store instead of being
    re-embedded - which is the whole value of the fingerprint, and why a rebuild after one edit
    costs one document.
    """
    started = utc_now()
    generative = use_case.generative
    _check_size(paths, generative)
    reusable = _reusable_chunks(store, previous) if previous is not None else {}

    documents: list[IndexedDocument] = []
    chunks: list[Chunk] = []
    fresh: list[Chunk] = []
    warnings: set[str] = set()

    for path in sorted(paths):
        entry, produced, reused = _index_one(path, generative, reusable, previous)
        documents.append(entry)
        chunks.extend(produced)
        if not reused:
            fresh.extend(produced)
        warnings.update(entry.warnings)

    if not chunks:
        raise generative_error(INDEX_EMPTY, index_id=index_id)

    vectors = _embed(chunks, fresh, meter, store, previous)
    store.write(index_id, chunks, vectors)

    finished = now if now is not None else utc_now()
    manifest = DocIndexManifest(
        index_id=index_id,
        use_case_id=use_case.id,
        client_id=client_id,
        documents=tuple(sorted(documents, key=lambda item: item.name)),
        total_chunks=len(chunks),
        chunk_config=ChunkConfig(
            chunk_tokens=generative.rag.chunk_tokens,
            chunk_overlap=generative.rag.chunk_overlap,
            embedding_model_id=generative.llm.embedding_model,
            dimensions=len(vectors[0]) if vectors else 0,
        ),
        prompt_versions=prompt_versions(ANSWER_PROMPTS, config_root),
        built_at=finished,
        build_seconds=round((finished - started).total_seconds(), 3),
        warnings=tuple(sorted(warnings - {parsers.DOCUMENT_NO_HEADINGS})),
    )
    storage.write_model(index_key(index_id, DOC_INDEX_MANIFEST_FILENAME), manifest)
    log_stage(_LOGGER, "index.build", rows=len(chunks), seconds=manifest.build_seconds)
    return BuildResult(manifest=manifest, chunks=tuple(chunks))


def read_manifest(storage: Storage, index_id: str) -> DocIndexManifest:
    """The manifest of `index_id`, or `INDEX_NOT_FOUND` when there is none."""
    from engine.generative.errors import INDEX_NOT_FOUND
    from engine.storage import StorageError

    try:
        return storage.read_model(index_key(index_id, DOC_INDEX_MANIFEST_FILENAME), DocIndexManifest)
    except StorageError as exc:
        raise generative_error(INDEX_NOT_FOUND, index_id=index_id) from exc


# ---------------------------------------------------------------------------
# One document
# ---------------------------------------------------------------------------
def _index_one(
    path: Path,
    generative: GenerativeConfig,
    reusable: dict[str, tuple[Chunk, ...]],
    previous: DocIndexManifest | None,
) -> tuple[IndexedDocument, tuple[Chunk, ...], bool]:
    """Parse and chunk one document, or reuse its chunks; never raises for one bad file."""
    data = path.read_bytes()
    digest = fingerprint(data)
    doc_id = path.stem
    extension = path.suffix.lstrip(".").lower()
    accepted = {member.value for member in generative.knowledge_base.accepted_types}

    if extension not in accepted:
        failure = generative_error(DOCUMENT_TYPE_UNSUPPORTED, name=path.name, extension=extension)
        return _failed(doc_id, path, data, failure.code), (), False

    if previous is not None and digest in reusable:
        existing = reusable[digest]
        before = next(item for item in previous.documents if item.fingerprint == digest)
        _LOGGER.info("index.reused doc_chunks=%d", len(existing))
        return before, existing, True

    try:
        parsed = parsers.parse(path)
    except parsers.ParseError as failure:
        _LOGGER.warning("index.document_failed code=%s", failure.code)
        return _failed(doc_id, path, data, failure.code), (), False

    produced = chunk_document(parsed, doc_id=doc_id, config=generative.rag)
    kinds = redaction.find("\n".join(section.text for section in parsed.sections))
    warnings = list(parsed.warnings)
    if kinds:
        warnings.append(f"{PII_IN_DOCS}:{','.join(kinds)}")
    entry = IndexedDocument(
        doc_id=doc_id,
        name=path.name,
        media_type=extension,
        fingerprint=digest,
        bytes=len(data),
        pages=parsed.pages,
        sections=len(parsed.sections),
        chunks=len(produced),
        warnings=tuple(warnings),
    )
    return entry, produced, False


def _failed(doc_id: str, path: Path, data: bytes, code: str) -> IndexedDocument:
    """A document that contributed nothing, recorded with the code that says why."""
    return IndexedDocument(
        doc_id=doc_id,
        name=path.name,
        media_type=path.suffix.lstrip(".").lower(),
        fingerprint=fingerprint(data),
        bytes=len(data),
        pages=None,
        sections=0,
        chunks=0,
        warnings=(code,),
    )


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------
def _embed(
    chunks: Sequence[Chunk],
    fresh: Sequence[Chunk],
    meter: Meter,
    store: VectorStore,
    previous: DocIndexManifest | None,
) -> tuple[tuple[float, ...], ...]:
    """A vector per chunk, embedding only what is new and carrying the rest over.

    What may be carried over is decided by `fresh` - the chunks of documents whose fingerprint
    moved - and never by the chunk id alone. That distinction is the whole correctness of an
    incremental rebuild, and it is not obvious: `chunk_id` is `(doc_id, ordinal)`, so an edited
    document produces chunks with exactly the same ids as the ones it had before. Matching the
    previous index on id would hand a rewritten passage the vector of the passage it replaced, and
    the index would be silently stale - the text updated, the vectors not - which in a retrieval
    system means a question matches on wording that is no longer there and is answered from text
    nobody searched for. A fingerprint moved is the only evidence that a re-embed is needed, and
    `_index_one` has already worked it out.
    """
    stale = {chunk.chunk_id for chunk in fresh}
    carried = {
        chunk_id: vector
        for chunk_id, vector in (_existing_vectors(store, previous) if previous is not None else {}).items()
        if chunk_id not in stale
    }
    to_embed = [chunk for chunk in chunks if chunk.chunk_id not in carried]
    computed: dict[str, tuple[float, ...]] = {}
    for start in range(0, len(to_embed), EMBED_BATCH):
        batch = to_embed[start : start + EMBED_BATCH]
        vectors = meter.embed([embedding_text(chunk) for chunk in batch])
        computed.update({chunk.chunk_id: tuple(vector) for chunk, vector in zip(batch, vectors, strict=True)})
    return tuple(
        carried[chunk.chunk_id] if chunk.chunk_id in carried else computed[chunk.chunk_id] for chunk in chunks
    )


def _reusable_chunks(store: VectorStore, previous: DocIndexManifest) -> dict[str, tuple[Chunk, ...]]:
    """Fingerprint -> the chunks that document already contributed, for an incremental rebuild."""
    by_doc: dict[str, list[Chunk]] = {}
    for chunk in store.chunks(previous.index_id):
        by_doc.setdefault(chunk.doc_id, []).append(chunk)
    return {
        document.fingerprint: tuple(by_doc.get(document.doc_id, ()))
        for document in previous.documents
        if document.chunks
    }


def _existing_vectors(store: VectorStore, previous: DocIndexManifest) -> dict[str, tuple[float, ...]]:
    """Chunk id -> its vector in the index being replaced, so an unchanged chunk is not re-embedded."""
    from engine.generative.vectorstore import LocalVectorStore

    if not isinstance(store, LocalVectorStore) or not store.exists(previous.index_id):
        return {}
    chunks = store.chunks(previous.index_id)
    matrix = store.matrix(previous.index_id)
    return {
        chunk.chunk_id: tuple(float(value) for value in row)
        for chunk, row in zip(chunks, matrix, strict=True)
    }


def _check_size(paths: Sequence[Path], generative: GenerativeConfig) -> None:
    """Refuse a knowledge base past its configured limits before a byte is parsed."""
    limits = generative.knowledge_base
    megabytes = sum(path.stat().st_size for path in paths) / BYTES_PER_MB
    if len(paths) > limits.max_docs or megabytes > limits.max_mb:
        raise generative_error(KNOWLEDGE_BASE_TOO_LARGE, documents=len(paths), megabytes=f"{megabytes:.1f}")
