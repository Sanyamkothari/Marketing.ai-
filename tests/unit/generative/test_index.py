"""`engine.generative.index`: the three decisions its own docstring names, proved against real builds.

Every test here runs the real parsers, the real chunker and `GroundedFakeLLMClient`'s real lexical
embedding over a two- or three-document slice of the synthetic Northwind corpus - never a hand-built
`Chunk` or a stubbed vector - because the property this module exists for is what happens *between*
those steps, and stubbing any one of them would leave it unreachable. The corpus is sliced to two or
three of its fourteen documents throughout, which is the whole of what "small" buys here: the same
code runs, the same bugs are reachable, and a build finishes in milliseconds rather than seconds.

**The stale-vector invariant is the one test in this file worth more than the others.** `chunk_id` is
`(doc_id, ordinal)`, so a document rewritten end to end produces chunks with exactly the same ids as
the ones they replace - which means a rebuild that carried a previous vector over by matching on
chunk id alone would hand a completely rewritten passage the vector of the passage it used to be,
silently. `chunking.embedding_text` and `retrieval` would then agree on nothing: the text stored says
one thing and the vector a question is matched against says another, and nothing about the citation
that results looks wrong. DEC-220 records that this was a real bug and measured it - a rewritten
document's stored vector was still cosine 1.0000 with its *old* text - so the test below rewrites a
document's bytes completely, rather than only adding one, and checks the vector itself rather than
trusting that a rebuild merely "ran".

**A build tolerates one bad document and refuses none.** A corrupt file with an accepted extension
and a file with an extension the configuration does not accept are two different failures with two
different codes, and both are proved to cost only themselves - the manifest still gets built, the
other documents still get chunked - while a knowledge base of nothing but such failures is proved to
raise `INDEX_EMPTY` instead, because an index with no chunks cannot answer a question.

**PII in a document is named, never removed.** A document containing an e-mail address is indexed
with that address still in the chunk text a citation would quote, and the manifest carries
`PII_IN_DOCS:email` naming what was seen - both halves are asserted together, because a warning with
no text-unchanged assertion beside it would not catch a change that quietly started redacting.

One cost is accepted throughout: `GroundedFakeLLMClient`'s embedding is a lexical hash, not a real
model's, so a cosine near zero here is two texts with almost no shared vocabulary and not a claim
about semantic distance - which is exactly the comparison the stale-vector test needs and no more.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from engine.config import KnowledgeBaseConfig, UseCaseConfig, load_use_case
from engine.generative import parsers
from engine.generative.budget import Meter
from engine.generative.chunking import embedding_text
from engine.generative.contracts import ChunkConfig, DocIndexManifest
from engine.generative.errors import (
    DOCUMENT_TYPE_UNSUPPORTED,
    INDEX_EMPTY,
    INDEX_NOT_FOUND,
    KNOWLEDGE_BASE_TOO_LARGE,
    GenerativeError,
)
from engine.generative.index import PII_IN_DOCS, BuildResult, build_index, read_manifest
from engine.generative.vectorstore import LocalVectorStore, cosine
from engine.llm import GroundedFakeLLMClient, LLMCall
from engine.storage import LocalStorage
from engine.utils.ids import new_index_id
from tests.fixtures.make_docs import build_knowledge_base

USE_CASE = "ai-onboarding-assistant"
BASE: UseCaseConfig = load_use_case(USE_CASE)
"""The shipped RAG use case. Its `generative` block is what every build in this module runs under."""

REWRITE = (
    "# A Completely Different Document\n\n"
    "This paragraph is about volcanic islands and has nothing to do with telecom plans at all.\n"
)
"""What `plans_prepaid.md` becomes when a test rewrites it: no word in common with the original."""


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path / "data")


@pytest.fixture
def store(storage: LocalStorage) -> LocalVectorStore:
    return LocalVectorStore(storage)


@pytest.fixture
def mixed_knowledge_base(tmp_path: Path) -> tuple[Path, ...]:
    """Two real documents, one corrupt PDF and one file of a type the shipped config does not accept."""
    docs_dir = tmp_path / "docs"
    valid = build_knowledge_base(docs_dir, stems=("faq_activation", "plans_prepaid"))
    broken = docs_dir / "broken.pdf"
    broken.write_bytes(b"this is not a pdf file and pypdf will refuse to open it")
    unsupported = docs_dir / "notes.rtf"
    unsupported.write_text("Some notes in a format nobody indexes.", encoding="utf-8")
    return (*valid, broken, unsupported)


def use_case_with(**overrides: object) -> UseCaseConfig:
    """The shipped assistant use case, with one field of its `generative` block overridden."""
    return BASE.model_copy(update={"generative": BASE.generative.model_copy(update=overrides)})


def meter_for(client: GroundedFakeLLMClient, *, use_case: UseCaseConfig = BASE) -> Meter:
    """A meter carrying `use_case`'s own LLM and budget settings, so `ChunkConfig` records what ran."""
    generative = use_case.generative
    return Meter(client, job_id=new_index_id(), llm=generative.llm, budget=generative.budget)


def build(
    paths: Sequence[Path],
    *,
    store: LocalVectorStore,
    storage: LocalStorage,
    use_case: UseCaseConfig = BASE,
    previous: DocIndexManifest | None = None,
) -> tuple[GroundedFakeLLMClient, BuildResult]:
    """Build once against `store`, handing back the client so its call log can be read afterwards.

    A fresh client every time, never a shared one, because what each test reads off `.calls` is what
    *this* build asked of a model - a client carried over from an earlier build would answer that
    question about the wrong build.
    """
    client = GroundedFakeLLMClient()
    result = build_index(
        list(paths),
        index_id=new_index_id(),
        use_case=use_case,
        storage=storage,
        store=store,
        meter=meter_for(client, use_case=use_case),
        previous=previous,
    )
    return client, result


def embed_calls(client: GroundedFakeLLMClient) -> tuple[LLMCall, ...]:
    """Every call this build made to embed something, in call order."""
    return tuple(call for call in client.calls if call.kind == "embed")


def embedded_texts(client: GroundedFakeLLMClient) -> tuple[str, ...]:
    """Every text this build actually sent to be embedded, across every call it made."""
    return tuple(text for call in embed_calls(client) for text in call.texts)


def vector_for(store: LocalVectorStore, index_id: str, chunk_id: str) -> tuple[float, ...]:
    """The vector `index_id` stores for `chunk_id`, read back the way a search would read it."""
    for chunk, vector in zip(store.chunks(index_id), store.matrix(index_id), strict=True):
        if chunk.chunk_id == chunk_id:
            return tuple(float(value) for value in vector)
    raise AssertionError(f"{chunk_id!r} is not in {index_id!r}")


def _unreachable_parse(*args: object, **kwargs: object) -> None:
    """Stands in for `parsers.parse`; its call is the proof that a size refusal came before parsing."""
    raise AssertionError("parsers.parse must not run once the knowledge base is oversized")


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------
def test_the_happy_path_over_a_small_subset_writes_a_manifest_that_reads_back_whole(
    storage: LocalStorage, store: LocalVectorStore, tmp_path: Path
) -> None:
    paths = build_knowledge_base(tmp_path / "docs", stems=("faq_activation", "plans_prepaid"))
    _, result = build(paths, store=store, storage=storage)
    manifest = result.manifest

    assert manifest.total_chunks == len(result.chunks) > 0
    assert sum(doc.chunks for doc in manifest.documents) == manifest.total_chunks
    assert {doc.name for doc in manifest.documents} == {"faq_activation.md", "plans_prepaid.md"}

    rag = BASE.generative.rag
    dimensions = store.matrix(manifest.index_id).shape[1]
    assert manifest.chunk_config == ChunkConfig(
        chunk_tokens=rag.chunk_tokens,
        chunk_overlap=rag.chunk_overlap,
        embedding_model_id=BASE.generative.llm.embedding_model,
        dimensions=int(dimensions),
    )
    assert manifest.chunk_config.dimensions > 0

    assert read_manifest(storage, manifest.index_id) == manifest


# ---------------------------------------------------------------------------
# Incremental rebuild saves work
# ---------------------------------------------------------------------------
def test_rebuilding_with_nothing_changed_embeds_no_texts_at_all(
    storage: LocalStorage, store: LocalVectorStore, tmp_path: Path
) -> None:
    paths = build_knowledge_base(tmp_path / "docs", stems=("faq_activation", "plans_prepaid"))
    _, first = build(paths, store=store, storage=storage)
    assert first.manifest.total_chunks > 0

    second_client, second = build(paths, store=store, storage=storage, previous=first.manifest)

    assert embed_calls(second_client) == ()
    assert second.manifest.total_chunks == first.manifest.total_chunks
    assert {chunk.chunk_id for chunk in second.chunks} == {chunk.chunk_id for chunk in first.chunks}


def test_editing_one_documents_bytes_re_embeds_only_that_documents_chunks(
    storage: LocalStorage, store: LocalVectorStore, tmp_path: Path
) -> None:
    docs_dir = tmp_path / "docs"
    paths = build_knowledge_base(docs_dir, stems=("faq_activation", "faq_devices", "plans_prepaid"))
    _, first = build(paths, store=store, storage=storage)

    (docs_dir / "plans_prepaid.md").write_text(REWRITE, encoding="utf-8")
    second_client, second = build(paths, store=store, storage=storage, previous=first.manifest)

    edited = [chunk for chunk in second.chunks if chunk.doc_id == "plans_prepaid"]
    untouched = [chunk for chunk in second.chunks if chunk.doc_id != "plans_prepaid"]
    assert edited
    assert untouched

    assert set(embedded_texts(second_client)) == {embedding_text(chunk) for chunk in edited}

    fingerprints_before = {doc.doc_id: doc.fingerprint for doc in first.manifest.documents}
    for doc in second.manifest.documents:
        if doc.doc_id == "plans_prepaid":
            assert doc.fingerprint != fingerprints_before[doc.doc_id]
        else:
            assert doc.fingerprint == fingerprints_before[doc.doc_id]


# ---------------------------------------------------------------------------
# The stale-vector invariant (DEC-220)
# ---------------------------------------------------------------------------
def test_a_rewritten_documents_stored_vector_is_the_vector_of_its_new_text_and_not_its_old(
    storage: LocalStorage, store: LocalVectorStore, tmp_path: Path
) -> None:
    """`chunk_id` is `(doc_id, ordinal)`, so the rewritten document's first chunk keeps its old id.

    A rebuild that carried the previous vector over by matching on that id alone would leave this
    chunk answering to a question about volcanic islands with the vector of a paragraph about
    prepaid plans, and nothing about the stored text or the chunk id would say so.
    """
    docs_dir = tmp_path / "docs"
    paths = build_knowledge_base(docs_dir, stems=("faq_activation", "plans_prepaid"))
    _, first = build(paths, store=store, storage=storage)

    target = "plans_prepaid-00000"
    old_chunk = next(chunk for chunk in first.chunks if chunk.chunk_id == target)
    old_vector = vector_for(store, first.manifest.index_id, target)

    (docs_dir / "plans_prepaid.md").write_text(REWRITE, encoding="utf-8")
    _, second = build(paths, store=store, storage=storage, previous=first.manifest)

    new_chunk = next(chunk for chunk in second.chunks if chunk.chunk_id == target)
    new_vector = vector_for(store, second.manifest.index_id, target)
    assert new_chunk.text != old_chunk.text

    (fresh_vector,) = GroundedFakeLLMClient().embed([embedding_text(new_chunk)])
    assert cosine(new_vector, fresh_vector) == pytest.approx(1.0)
    assert cosine(old_vector, new_vector) < 0.5


# ---------------------------------------------------------------------------
# A document that fails to parse costs only itself
# ---------------------------------------------------------------------------
def test_a_document_that_fails_to_parse_costs_only_itself_and_the_build_still_succeeds(
    storage: LocalStorage, store: LocalVectorStore, mixed_knowledge_base: tuple[Path, ...]
) -> None:
    _, result = build(mixed_knowledge_base, store=store, storage=storage)
    manifest = result.manifest

    by_name = {doc.name: doc for doc in manifest.documents}
    broken = by_name["broken.pdf"]
    assert broken.chunks == 0
    assert parsers.DOCUMENT_UNREADABLE in broken.warnings

    valid_chunks = by_name["faq_activation.md"].chunks + by_name["plans_prepaid.md"].chunks
    assert manifest.total_chunks == valid_chunks > 0


def test_an_extension_the_knowledge_base_does_not_accept_is_document_type_unsupported(
    storage: LocalStorage, store: LocalVectorStore, mixed_knowledge_base: tuple[Path, ...]
) -> None:
    _, result = build(mixed_knowledge_base, store=store, storage=storage)
    unsupported = next(doc for doc in result.manifest.documents if doc.name == "notes.rtf")
    assert unsupported.chunks == 0
    assert unsupported.warnings == (DOCUMENT_TYPE_UNSUPPORTED,)


def test_a_build_where_every_document_fails_to_parse_raises_index_empty(
    storage: LocalStorage, store: LocalVectorStore, tmp_path: Path
) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    broken = docs_dir / "broken.pdf"
    broken.write_bytes(b"not a pdf at all")
    unsupported = docs_dir / "notes.rtf"
    unsupported.write_text("nothing indexable here", encoding="utf-8")

    with pytest.raises(GenerativeError) as error:
        build((broken, unsupported), store=store, storage=storage)
    assert error.value.code == INDEX_EMPTY


# ---------------------------------------------------------------------------
# PII in a document is a warning, never a redaction (DEC-216)
# ---------------------------------------------------------------------------
def test_pii_in_a_knowledge_document_is_warned_about_and_never_redacted(
    storage: LocalStorage, store: LocalVectorStore, tmp_path: Path
) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    doc = docs_dir / "support_contact.md"
    doc.write_text(
        "# Support Contact\n\n"
        "For help with your account, write to support@example.invalid and we reply within a day.\n",
        encoding="utf-8",
    )
    _, result = build((doc,), store=store, storage=storage)
    manifest = result.manifest

    entry = manifest.documents[0]
    assert entry.name == "support_contact.md"
    doc_warning = next(warning for warning in entry.warnings if warning.startswith(f"{PII_IN_DOCS}:"))
    assert "email" in doc_warning
    assert doc_warning in manifest.warnings

    assert any("support@example.invalid" in chunk.text for chunk in result.chunks)


# ---------------------------------------------------------------------------
# The size check runs before anything is parsed
# ---------------------------------------------------------------------------
def test_check_size_refuses_a_knowledge_base_past_max_docs_before_a_byte_is_parsed(
    storage: LocalStorage, store: LocalVectorStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = build_knowledge_base(tmp_path / "docs", stems=("faq_activation", "plans_prepaid"))
    monkeypatch.setattr(parsers, "parse", _unreachable_parse)
    tight = use_case_with(knowledge_base=KnowledgeBaseConfig(max_docs=1))

    with pytest.raises(GenerativeError) as error:
        build(paths, store=store, storage=storage, use_case=tight)
    assert error.value.code == KNOWLEDGE_BASE_TOO_LARGE


def test_check_size_refuses_a_knowledge_base_past_max_mb_before_a_byte_is_parsed(
    storage: LocalStorage, store: LocalVectorStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docs_dir = tmp_path / "docs"
    paths = build_knowledge_base(docs_dir, stems=("faq_activation",))
    filler = docs_dir / "filler.bin"
    filler.write_bytes(b"0" * (2 * 1024 * 1024))
    monkeypatch.setattr(parsers, "parse", _unreachable_parse)
    tight = use_case_with(knowledge_base=KnowledgeBaseConfig(max_mb=1))

    with pytest.raises(GenerativeError) as error:
        build((*paths, filler), store=store, storage=storage, use_case=tight)
    assert error.value.code == KNOWLEDGE_BASE_TOO_LARGE


# ---------------------------------------------------------------------------
# Reading a manifest
# ---------------------------------------------------------------------------
def test_reading_the_manifest_of_a_missing_index_raises_index_not_found(storage: LocalStorage) -> None:
    with pytest.raises(GenerativeError) as error:
        read_manifest(storage, "x_20200101_deadbeef")
    assert error.value.code == INDEX_NOT_FOUND


# ---------------------------------------------------------------------------
# What is embedded (DEC-217)
# ---------------------------------------------------------------------------
def test_what_is_embedded_is_the_chunks_embedding_text_and_never_its_bare_text(
    storage: LocalStorage, store: LocalVectorStore, tmp_path: Path
) -> None:
    paths = build_knowledge_base(tmp_path / "docs", stems=("faq_activation", "plans_prepaid"))
    client, result = build(paths, store=store, storage=storage)

    embedded = set(embedded_texts(client))
    assert embedded == {embedding_text(chunk) for chunk in result.chunks}
    assert not any(chunk.text in embedded for chunk in result.chunks)


# ---------------------------------------------------------------------------
# The manifest's own bookkeeping
# ---------------------------------------------------------------------------
def test_manifest_documents_are_sorted_by_name_and_no_headings_is_not_an_index_wide_warning(
    storage: LocalStorage, store: LocalVectorStore, tmp_path: Path
) -> None:
    """Fed in reverse of alphabetical order, so a manifest that merely kept input order would fail."""
    docs_dir = tmp_path / "docs"
    (plans_prepaid,) = build_knowledge_base(docs_dir, stems=("plans_prepaid",))
    no_heading = docs_dir / "aardvark_notes.txt"
    no_heading.write_text(
        "This short document has no heading at all, only a single plain sentence that ends the way "
        "every other sentence in it does, with a full stop.",
        encoding="utf-8",
    )
    _, result = build((plans_prepaid, no_heading), store=store, storage=storage)
    manifest = result.manifest

    assert [doc.name for doc in manifest.documents] == ["aardvark_notes.txt", "plans_prepaid.md"]

    entry = manifest.documents[0]
    assert entry.name == "aardvark_notes.txt"
    assert parsers.DOCUMENT_NO_HEADINGS in entry.warnings
    assert parsers.DOCUMENT_NO_HEADINGS not in manifest.warnings
