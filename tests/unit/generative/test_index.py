"""`engine.generative.index`: what a build writes down, and what a rebuild is allowed to skip.

An index is the generative counterpart of a trained model, so a build is asked the questions a
training run is asked: what went in, under which settings, what failed on the way, and what the
next build is entitled to reuse. Five properties carry most of the weight here.

**The saving is counted, never inferred.** A rebuild that reuses a document is the easiest thing in
this module to assert wrongly, because chunking is deterministic: the chunks come back identical
whether they were carried over or derived again, so comparing them proves nothing at all. The
reuse is therefore proved on the fake client - a second build over an untouched knowledge base
asks for no embedding whatsoever, and a build after one document was edited asks for that
document's chunks and no others. The cost of proving it this way is that these tests know the
client keeps a log, which is exactly what `GroundedFakeLLMClient.calls` is for.

**A carried vector is proved to belong to the text it sits beside.** `chunk_id` is `(doc_id,
ordinal)`, so an edited document produces chunks with the ids its old ones had; carrying a vector
over by id would leave the index silently stale, matching on wording nobody can read any more. The
stored row for an edited chunk is read back and compared with the passage it now holds and with
the passage it replaced, which is the measurement DEC-220 was written from.

**One bad file costs that file.** A corrupt PDF, a text file with nothing in it and a spreadsheet
nobody asked for are put in beside real documents, and the build is asserted to finish: each
failure is recorded against its own row with the code that explains it, and contributes no chunks.
The build that does raise is the one where every document failed, because an index with no chunks
can answer nothing.

**PII in a document is a warning and never a redaction.** Both halves are asserted on purpose
(DEC-216) - the manifest names the kind that was found, *and* the address is still in the chunk
text character for character. The second half is the one a later reader is most likely to tidy
away, and the first half would go on passing without it.

**What is embedded is not what is stored.** A chunk is embedded as `chunking.embedding_text`
renders it, its document id and heading in front of the passage, and the proof is the texts the
fake recorded rather than a restatement of the call site (DEC-217).

No test here asserts the similarity floor, and only one reads a vector back at all. 0.25 is
calibrated for a real embedding model and means something else under a lexical fake (DEC-218), so
the one comparison made is between a passage and itself - a claim about staleness, which travels,
rather than about a threshold, which does not.

Two tests are `xfail(strict=True)`. A document's identity across a rebuild is its fingerprint and
nothing else, so a renamed file keeps the name it had in the manifest and on every citation, and
two files with the same bytes collapse into one entry and a duplicated chunk id.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import pytest

from engine.config import KnowledgeBaseConfig, UseCaseConfig, load_use_case
from engine.generative import parsers
from engine.generative.budget import Meter
from engine.generative.chunking import embedding_text
from engine.generative.contracts import DOC_INDEX_MANIFEST_FILENAME, DocIndexManifest, IndexedDocument
from engine.generative.errors import GenerativeError
from engine.generative.index import (
    ANSWER_PROMPTS,
    EMBED_BATCH,
    PII_IN_DOCS,
    BuildResult,
    build_index,
    read_manifest,
)
from engine.generative.vectorstore import LocalVectorStore, cosine
from engine.llm import GroundedFakeLLMClient
from engine.storage import LocalStorage, index_key
from engine.utils.ids import new_index_id
from tests.fixtures.make_docs import build_knowledge_base

USE_CASE: Final[UseCaseConfig] = load_use_case("ai-onboarding-assistant")
"""The one shipped generative use case, read once: loading it is a catalog read per test otherwise."""

STEMS: Final[tuple[str, ...]] = ("faq_billing", "plans_prepaid", "faq_roaming")
"""Three documents in three formats - plain text, Markdown and a PDF - and not all fourteen.

A subset is the difference between a module that runs in a second and one nobody waits for, and the
PDF is in it so `IndexedDocument.pages` is measured on a format that has pages.
"""

DIMENSIONS: Final[int] = 32
"""The fake's vector width here, and deliberately not its default: a width nobody chose proves nothing."""

SUPPORT_ADDRESS: Final[str] = "support@northwind.example.invalid"
"""An invented address in a documentation-reserved domain, planted so a detector has something to find."""

WITH_PII: Final[str] = f"# Support contact\n\nWrite to {SUPPORT_ADDRESS} when an activation stalls.\n"
NO_HEADINGS: Final[str] = (
    "Northwind Telecom upgrades the core network overnight on the first Sunday of every month, and "
    "prepaid packs carry on working while the work is done.\n"
)
ROUTER_PLACEMENT: Final[str] = (
    "# Router placement\n\nPut the router in the open, away from a metal cupboard, so the signal "
    "reaches every room.\n"
)
REFUND_WINDOW: Final[str] = (
    "# Refund windows\n\nAn unused prepaid pack is refunded in full within fourteen days of purchase.\n"
)
"""The same file, rewritten end to end: no word of the first survives into the second."""

EXTRA_SECTION: Final[str] = (
    "\n\nExtra section\n\nThe billing cycle now closes on the fifth of each month and invoices are "
    "issued the next day.\n"
)
NOT_A_PDF: Final[bytes] = b"not a pdf at all, just the bytes of one that never finished uploading"
SPREADSHEET: Final[str] = "plan,price\nStarter,149\n"
OVERSIZED: Final[str] = "# Tariffs\n\n" + "A sentence about prepaid tariffs. " * 40_000
"""Comfortably over a one-megabyte ceiling, and never parsed, because the refusal comes first."""

MANY_SECTIONS: Final[str] = "".join(
    f"## Plan {number}\n\nPlan {number} carries its own tariff, its own allowance and its own validity.\n\n"
    for number in range(EMBED_BATCH + 6)
)
"""One headed section per chunk, six past a full batch, so the second batch is a short one."""


def entry(result: BuildResult, name: str) -> IndexedDocument:
    """The manifest row for one document, by the filename it was uploaded under."""
    return next(item for item in result.manifest.documents if item.name == name)


def vector_of(text: str) -> tuple[float, ...]:
    """The fake's embedding of one text, from a client of its own so no build's log is disturbed."""
    return GroundedFakeLLMClient(dimensions=DIMENSIONS).embed([text])[0]


def with_limits(**overrides: object) -> UseCaseConfig:
    """`USE_CASE` with knowledge-base limits this module owns, rather than the shipped ones it does not."""
    knowledge_base = KnowledgeBaseConfig(**overrides)  # type: ignore[arg-type]
    generative = USE_CASE.generative.model_copy(update={"knowledge_base": knowledge_base})
    return USE_CASE.model_copy(update={"generative": generative})


class Rig:
    """One knowledge base and the storage, store and fake client a build over it needs.

    Each build gets a client of its own, so `embedded` is everything *that* build asked for and
    nothing an earlier one did. Sharing one would make the incremental saving unreadable, which is
    the whole thing several of these tests are counting.
    """

    def __init__(self, root: Path) -> None:
        self.docs = root / "documents"
        self.storage = LocalStorage(root / "data")
        self.store = LocalVectorStore(self.storage)
        self.client = GroundedFakeLLMClient(dimensions=DIMENSIONS)

    def documents(self, *stems: str) -> list[Path]:
        """The named documents of the synthetic corpus, written into this rig's knowledge base."""
        return list(build_knowledge_base(self.docs, stems=stems or STEMS))

    def write(self, filename: str, content: str | bytes) -> Path:
        """One document of a test's own, beside the corpus, and the path it was written to."""
        path = self.docs / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return path

    def build(
        self,
        paths: Sequence[Path],
        *,
        previous: DocIndexManifest | None = None,
        use_case: UseCaseConfig | None = None,
        client_id: str | None = None,
        index_id: str | None = None,
        now: datetime | None = None,
    ) -> BuildResult:
        """Build one index over `paths`, against a fake and a meter belonging to this build alone."""
        chosen = use_case if use_case is not None else USE_CASE
        self.client = GroundedFakeLLMClient(dimensions=DIMENSIONS)
        meter = Meter(
            self.client,
            job_id="x_build",
            llm=chosen.generative.llm,
            budget=chosen.generative.budget,
        )
        return build_index(
            list(paths),
            index_id=index_id if index_id is not None else new_index_id(),
            use_case=chosen,
            storage=self.storage,
            store=self.store,
            meter=meter,
            client_id=client_id,
            previous=previous,
            now=now,
        )

    @property
    def embedded(self) -> tuple[str, ...]:
        """Every text the last build asked the model to embed, in the order it asked for them."""
        return tuple(text for call in self.client.calls if call.kind == "embed" for text in call.texts)

    @property
    def batches(self) -> tuple[int, ...]:
        """How many texts each embedding call of the last build carried."""
        return tuple(len(call.texts) for call in self.client.calls if call.kind == "embed")


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    """A knowledge base directory, the storage beside it and a vector store over that storage."""
    return Rig(tmp_path)


@pytest.fixture
def parsed(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Every path the parser was actually asked to read, so a refusal can be proved to precede one."""
    asked: list[Path] = []
    real = parsers.parse

    def spy(path: Path, **keywords: object) -> parsers.ParsedDocument:
        asked.append(path)
        return real(path, **keywords)  # type: ignore[arg-type]

    monkeypatch.setattr(parsers, "parse", spy)
    return asked


# ---------------------------------------------------------------------------
# What a build writes
# ---------------------------------------------------------------------------
def test_a_build_writes_a_manifest_that_says_what_went_into_the_index(rig: Rig) -> None:
    """The manifest is the record of the build, so it has to survive the round trip through storage."""
    result = rig.build(rig.documents())
    stored = read_manifest(rig.storage, result.index_id)
    assert stored == result.manifest
    assert rig.storage.exists(index_key(result.index_id, DOC_INDEX_MANIFEST_FILENAME))
    assert stored.index_id == result.index_id
    assert stored.use_case_id == USE_CASE.id
    assert [item.name for item in stored.documents] == [
        "faq_billing.txt",
        "faq_roaming.pdf",
        "plans_prepaid.md",
    ]
    assert all(item.chunks for item in stored.documents)
    assert all(item.warnings == () for item in stored.documents)
    assert stored.warnings == ()
    assert stored.build_seconds >= 0.0


def test_the_manifest_counts_the_chunks_that_went_into_the_store(rig: Rig) -> None:
    """`total_chunks` is a number a screen shows, so it is the number the store actually holds."""
    result = rig.build(rig.documents())
    assert result.manifest.total_chunks == len(result.chunks)
    assert result.manifest.total_chunks == sum(item.chunks for item in result.manifest.documents)
    assert rig.store.chunks(result.index_id) == result.chunks
    assert rig.store.matrix(result.index_id).shape == (len(result.chunks), DIMENSIONS)


def test_the_chunk_config_records_the_settings_the_model_and_the_width_it_measured(rig: Rig) -> None:
    """What a rebuild is compared against, and what tells a reader which floor suits this index (DEC-218)."""
    result = rig.build(rig.documents("plans_prepaid"))
    config = result.manifest.chunk_config
    assert config.chunk_tokens == USE_CASE.generative.rag.chunk_tokens
    assert config.chunk_overlap == USE_CASE.generative.rag.chunk_overlap
    assert config.embedding_model_id == USE_CASE.generative.llm.embedding_model
    assert {call.model_id for call in rig.client.calls} == {config.embedding_model_id}
    assert config.dimensions == DIMENSIONS
    assert rig.store.matrix(result.index_id).shape[1] == config.dimensions


def test_a_paged_format_records_the_pages_it_had_and_an_unpaged_one_records_none(rig: Rig) -> None:
    """Measured by the reader that has pages; null is the honest answer for a format that has none."""
    pages = {item.name: item.pages for item in rig.build(rig.documents()).manifest.documents}
    assert pages["faq_roaming.pdf"] is not None
    assert pages["faq_roaming.pdf"] >= 1
    assert pages["faq_billing.txt"] is None
    assert pages["plans_prepaid.md"] is None


def test_the_documents_are_sorted_by_name_whatever_order_they_arrived_in(rig: Rig) -> None:
    """The screen renders this list and never computes one, which is the rule every artefact here follows."""
    arrived = list(reversed(rig.documents()))
    names = [item.name for item in rig.build(arrived).manifest.documents]
    assert names == sorted(names)
    assert names != [path.name for path in arrived]


def test_the_manifest_names_the_prompts_an_answer_from_this_index_will_use(rig: Rig) -> None:
    """An answer's wording is a property of the index it came from, so the index records the versions."""
    versions = rig.build(rig.documents("plans_prepaid")).manifest.prompt_versions
    assert set(versions) == set(ANSWER_PROMPTS)
    assert all(version >= 1 for version in versions.values())


def test_the_client_the_documents_belong_to_is_recorded_and_defaults_to_nobody(rig: Rig) -> None:
    """One deployment holds several clients' knowledge bases, and null is not the same as unclaimed."""
    paths = rig.documents("plans_prepaid")
    assert rig.build(paths, client_id="northwind").manifest.client_id == "northwind"
    assert rig.build(paths).manifest.client_id is None


def test_the_time_a_build_finished_is_the_clock_the_caller_handed_it(rig: Rig) -> None:
    """`now` is an argument so a manifest can be pinned, which is what an artefact test needs of it."""
    finished = datetime(2026, 9, 22, 11, 30, tzinfo=UTC)
    result = rig.build(rig.documents("plans_prepaid"), now=finished)
    assert result.manifest.built_at == finished
    assert read_manifest(rig.storage, result.index_id).built_at == finished


# ---------------------------------------------------------------------------
# What is embedded (DEC-217)
# ---------------------------------------------------------------------------
def test_what_is_embedded_is_the_chunk_with_its_document_and_heading_in_front_of_it(rig: Rig) -> None:
    """The prefix is the difference between retrieving the right document 28 times and 40 (DEC-217)."""
    result = rig.build(rig.documents("plans_prepaid"))
    assert rig.embedded == tuple(embedding_text(chunk) for chunk in result.chunks)
    first = result.chunks[0]
    assert rig.embedded[0].startswith(f"{first.doc_id.replace('_', ' ')} {first.section}\n\n")
    assert rig.embedded[0].endswith(first.text)
    assert first.text not in rig.embedded


def test_the_passage_is_stored_as_the_parser_read_it_and_not_as_it_was_embedded(rig: Rig) -> None:
    """A citation quotes the document's own words, so the prefix that was embedded is not kept (DEC-217)."""
    result = rig.build(rig.documents("plans_prepaid"))
    assert all(embedding_text(chunk) != chunk.text for chunk in result.chunks)
    assert all(embedding_text(chunk).endswith(chunk.text) for chunk in result.chunks)
    assert {chunk.document for chunk in result.chunks} == {"plans_prepaid.md"}
    assert [chunk.ordinal for chunk in result.chunks] == list(range(len(result.chunks)))


def test_chunks_are_embedded_in_batches_rather_than_one_call_per_chunk(rig: Rig) -> None:
    """One call per chunk would meter hundreds of requests for one build, which is what `EMBED_BATCH` is for."""
    result = rig.build([rig.write("tariffs.md", MANY_SECTIONS)])
    assert result.manifest.total_chunks == EMBED_BATCH + 6
    assert rig.batches == (EMBED_BATCH, 6)
    assert rig.embedded == tuple(embedding_text(chunk) for chunk in result.chunks)


# ---------------------------------------------------------------------------
# The incremental rebuild
# ---------------------------------------------------------------------------
def test_a_rebuild_reuses_an_unchanged_document_and_embeds_nothing_at_all(rig: Rig) -> None:
    """Counted on the client, because deterministic chunks look identical whether reused or re-derived."""
    paths = rig.documents("faq_billing", "plans_prepaid")
    first = rig.build(paths)
    embedded_first = rig.embedded
    assert len(embedded_first) == first.manifest.total_chunks

    second = rig.build(paths, previous=first.manifest)
    assert len(rig.embedded) < len(embedded_first)
    assert rig.embedded == ()
    assert second.manifest.total_chunks == first.manifest.total_chunks
    assert [chunk.chunk_id for chunk in second.chunks] == [chunk.chunk_id for chunk in first.chunks]
    assert second.manifest.documents == first.manifest.documents


def test_editing_one_document_re_embeds_that_document_and_no_other(rig: Rig) -> None:
    """Re-indexing after one edit costs one document, which is the whole value of the fingerprint."""
    paths = rig.documents("faq_billing", "plans_prepaid")
    first = rig.build(paths)
    billing = next(path for path in paths if path.name == "faq_billing.txt")
    billing.write_text(billing.read_text(encoding="utf-8") + EXTRA_SECTION, encoding="utf-8")

    second = rig.build(paths, previous=first.manifest)
    rewritten = [chunk for chunk in second.chunks if chunk.doc_id == "faq_billing"]
    assert rig.embedded == tuple(embedding_text(chunk) for chunk in rewritten)
    assert len(rig.embedded) < second.manifest.total_chunks

    edited = entry(second, "faq_billing.txt")
    assert edited.fingerprint != entry(first, "faq_billing.txt").fingerprint
    assert entry(second, "plans_prepaid.md") == entry(first, "plans_prepaid.md")


def test_an_edited_passage_is_never_left_holding_the_vector_of_the_passage_it_replaced(rig: Rig) -> None:
    """`chunk_id` is `(doc_id, ordinal)`, so carrying a vector over by id would stale the index (DEC-220)."""
    handbook = rig.write("handbook.md", ROUTER_PLACEMENT)
    paths = [*rig.documents("plans_prepaid"), handbook]
    first = rig.build(paths)
    before = next(chunk for chunk in first.chunks if chunk.doc_id == "handbook")

    rig.write("handbook.md", REFUND_WINDOW)
    second = rig.build(paths, previous=first.manifest)
    after = next(chunk for chunk in second.chunks if chunk.doc_id == "handbook")
    stored = rig.store.matrix(second.index_id)[second.chunks.index(after)]

    assert after.chunk_id == before.chunk_id
    assert after.text != before.text
    assert cosine(stored, vector_of(embedding_text(after))) == pytest.approx(1.0)
    assert cosine(stored, vector_of(embedding_text(before))) < 0.5


def test_adding_a_document_embeds_only_the_document_that_was_added(rig: Rig) -> None:
    """A knowledge base grows one file at a time, and re-embedding the rest would make that expensive."""
    paths = rig.documents("faq_billing", "plans_prepaid")
    first = rig.build(paths)
    added = rig.write("handbook.md", ROUTER_PLACEMENT)

    second = rig.build([*paths, added], previous=first.manifest)
    fresh = [chunk for chunk in second.chunks if chunk.doc_id == "handbook"]
    assert rig.embedded == tuple(embedding_text(chunk) for chunk in fresh)
    assert second.manifest.total_chunks == first.manifest.total_chunks + len(fresh)


# ---------------------------------------------------------------------------
# A document that fails does not fail the build
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("filename", "content", "code"),
    [
        ("broken.pdf", NOT_A_PDF, "DOCUMENT_UNREADABLE"),
        ("empty.txt", "", "DOCUMENT_EMPTY"),
    ],
)
def test_one_document_that_cannot_be_read_costs_that_document_and_not_the_index(
    rig: Rig, filename: str, content: str | bytes, code: str
) -> None:
    """One corrupt file in a knowledge base of two hundred should cost that file, and nothing else."""
    good = rig.documents("faq_billing", "plans_prepaid")
    result = rig.build([*good, rig.write(filename, content)])
    failed = entry(result, filename)
    assert failed.warnings == (code,)
    assert failed.chunks == 0
    assert failed.sections == 0
    assert failed.pages is None
    assert failed.fingerprint.startswith("sha256-document:")
    assert code in result.manifest.warnings
    assert all(item.chunks for item in result.manifest.documents if item.name != filename)
    assert result.manifest.total_chunks == sum(item.chunks for item in result.manifest.documents)


def test_a_build_where_every_document_failed_raises_rather_than_writing_an_empty_index(rig: Rig) -> None:
    """An index with no chunks can answer nothing, so it is a refusal and not an artefact."""
    index_id = new_index_id()
    with pytest.raises(GenerativeError) as error:
        rig.build(
            [rig.write("broken.pdf", NOT_A_PDF), rig.write("tariffs.csv", SPREADSHEET)],
            index_id=index_id,
        )
    assert error.value.code == "INDEX_EMPTY"
    assert index_id in error.value.message
    assert error.value.suggestion
    assert rig.embedded == ()
    with pytest.raises(GenerativeError) as missing:
        read_manifest(rig.storage, index_id)
    assert missing.value.code == "INDEX_NOT_FOUND"


def test_an_unsupported_extension_is_refused_before_a_parser_is_asked_about_it(
    rig: Rig, parsed: list[Path]
) -> None:
    """The accepted set is configuration, so a spreadsheet is turned away on its name and never opened."""
    spreadsheet = rig.write("tariffs.csv", SPREADSHEET)
    result = rig.build([*rig.documents("plans_prepaid"), spreadsheet])
    refused = entry(result, "tariffs.csv")
    assert refused.warnings == ("DOCUMENT_TYPE_UNSUPPORTED",)
    assert refused.media_type == "csv"
    assert refused.chunks == 0
    assert refused.bytes == len(SPREADSHEET)
    assert parsed
    assert spreadsheet not in parsed


# ---------------------------------------------------------------------------
# Warnings: what reaches the index-wide list and what stays on its own row
# ---------------------------------------------------------------------------
def test_personal_data_in_a_document_is_a_warning_and_never_a_redaction(rig: Rig) -> None:
    """Both halves of DEC-216: the manifest names the kind, and the address is indexed as written."""
    result = rig.build([*rig.documents("plans_prepaid"), rig.write("support_contact.md", WITH_PII)])
    warned = entry(result, "support_contact.md")
    assert warned.warnings == (f"{PII_IN_DOCS}:email",)
    assert f"{PII_IN_DOCS}:email" in result.manifest.warnings

    indexed = next(chunk for chunk in result.chunks if chunk.doc_id == "support_contact")
    assert SUPPORT_ADDRESS in indexed.text
    assert "[REDACTED" not in indexed.text
    assert all(SUPPORT_ADDRESS not in warning for warning in result.manifest.warnings)


def test_a_document_with_no_headings_warns_on_its_own_row_and_not_on_the_whole_index(rig: Rig) -> None:
    """Every unheaded document raises it, so on the manifest it would say nothing about this index."""
    result = rig.build(
        [rig.write("notice.md", NO_HEADINGS), rig.write("support_contact.md", WITH_PII)],
    )
    assert entry(result, "notice.md").warnings == ("DOCUMENT_NO_HEADINGS",)
    assert "DOCUMENT_NO_HEADINGS" not in result.manifest.warnings
    assert f"{PII_IN_DOCS}:email" in result.manifest.warnings


# ---------------------------------------------------------------------------
# The limits, and the index that was never built
# ---------------------------------------------------------------------------
def test_a_knowledge_base_past_its_document_limit_is_refused_before_a_byte_is_parsed(
    rig: Rig, parsed: list[Path]
) -> None:
    """A limit that was checked after the work is a limit that saved nothing."""
    paths = rig.documents("faq_billing", "plans_prepaid")
    with pytest.raises(GenerativeError) as error:
        rig.build(paths, use_case=with_limits(max_docs=1))
    assert error.value.code == "KNOWLEDGE_BASE_TOO_LARGE"
    assert "2 documents" in error.value.message
    assert error.value.suggestion
    assert parsed == []


def test_a_knowledge_base_past_its_size_limit_is_refused_the_same_way(rig: Rig, parsed: list[Path]) -> None:
    """Measured off the files as uploaded, so the refusal costs one stat call per document."""
    with pytest.raises(GenerativeError) as error:
        rig.build([rig.write("tariffs.md", OVERSIZED)], use_case=with_limits(max_mb=1))
    assert error.value.code == "KNOWLEDGE_BASE_TOO_LARGE"
    assert "1.3 MB" in error.value.message
    assert parsed == []


def test_reading_the_manifest_of_an_index_that_was_never_built_is_a_coded_error(rig: Rig) -> None:
    """The id is what a caller can act on; a storage key is a deployment detail they cannot."""
    with pytest.raises(GenerativeError) as error:
        read_manifest(rig.storage, "x_20260101_deadbeef")
    assert error.value.code == "INDEX_NOT_FOUND"
    assert "x_20260101_deadbeef" in error.value.message
    assert "/" not in error.value.message
    assert error.value.suggestion


# ---------------------------------------------------------------------------
# A document's identity across a rebuild is its fingerprint, and nothing else
# ---------------------------------------------------------------------------
def test_a_document_renamed_between_builds_is_indexed_under_the_name_it_now_has(rig: Rig) -> None:
    """A file the knowledge base no longer holds is named by the manifest and quoted by every citation."""
    paths = rig.documents("plans_prepaid")
    original = rig.write("handbook.md", ROUTER_PLACEMENT)
    first = rig.build([*paths, original])
    renamed = rig.write("handbook_v2.md", ROUTER_PLACEMENT)
    original.unlink()

    second = rig.build([*paths, renamed], previous=first.manifest)
    assert [item.name for item in second.manifest.documents] == ["handbook_v2.md", "plans_prepaid.md"]
    assert "handbook.md" not in {chunk.document for chunk in second.chunks}


def test_two_files_with_the_same_bytes_are_two_documents_in_the_index(rig: Rig) -> None:
    """A duplicated chunk id makes the store's id-to-vector map lossy on the rebuild after this one."""
    original = rig.write("handbook.md", ROUTER_PLACEMENT)
    paths = [*rig.documents("plans_prepaid"), original]
    first = rig.build(paths)

    second = rig.build([*paths, rig.write("handbook_copy.md", ROUTER_PLACEMENT)], previous=first.manifest)
    assert len({item.name for item in second.manifest.documents}) == 3
    assert len({chunk.chunk_id for chunk in second.chunks}) == len(second.chunks)
