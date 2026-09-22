"""What `engine.generative.parsers` and `engine.generative.chunking` promise the rest of Phase 3a.

Four claims are made downstream of this pair, and none of them can be checked once an index has
been built, so they are checked here against the real fourteen-document corpus that
`tests.fixtures.make_docs` writes in all four formats.

**A document's structure survives its format.** An operator uploads whatever their policy team
exported, and the same refund policy arrives as a PDF from one team and a DOCX from another. If
the two parse into different sentences then two indexes built from the same knowledge base answer
differently, and nobody can tell which one is wrong. So the corpus is written to two formats and
the sentences are compared, document by document.

**Headings are recovered, not invented.** `Citation.section` is the line that tells a reader where
an answer came from. A parser that collapsed an eight-section policy into one section would make
every citation on it useless while still looking like it worked, which is why the section counts
here are compared against the committed Markdown sources rather than against a number written down
in this file.

**A refusal is coded.** Three things can go wrong with an upload - the wrong kind of file, a file
that will not open, and a file with no text in it - and each has to be distinguishable by the API,
so each gets its own code and each is proved to raise it.

**Nothing a document carries reaches a log.** A document's name is customer data the moment it
leaves the caller's own screen (plan section 13.7), so both paths - a document that parses and one
that will not open - are driven with a sentinel in the name and every record they emit is searched
for it.

The chunking claims are the ones a rebuild depends on: a chunk holds whole sentences, it respects
the configured target, its overlap is whole trailing sentences, and running twice over an unchanged
document produces byte-identical rows.

Personal data inside a document is `engine.generative.redaction`'s, and is proved in
`tests/unit/generative/test_guardrails.py` over the same planted corpus; nothing about it is
repeated here.
"""

from __future__ import annotations

import logging
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from engine.config import RagConfig
from engine.generative.chunking import (
    DOCUMENT_FINGERPRINT_PREFIX,
    chunk_document,
    fingerprint,
    sentences,
)
from engine.generative.parsers import (
    DOCUMENT_EMPTY,
    DOCUMENT_NO_HEADINGS,
    DOCUMENT_PARSERS,
    DOCUMENT_TYPE_UNSUPPORTED,
    DOCUMENT_UNREADABLE,
    PARSE_ERRORS,
    ParsedDocument,
    ParseError,
    Section,
    parse,
    parse_error,
)
from tests.fixtures.make_docs import (
    DOCUMENT_STEMS,
    FORMATS,
    SOURCE_DIR,
    build_knowledge_base,
    source_text,
    write_docx,
    write_txt,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

SPLIT_TOKENS: int = 200
"""A chunk target large enough that a section splits into several chunks with room for an overlap."""

SPLIT_OVERLAP: float = 0.3
"""A share of the target wide enough to carry whole sentences; a narrower one carries none."""

LEAK_SENTINEL: str = "QZLEAK"
"""Planted in a document name. No log line may carry it, on the happy path or the failure path."""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def knowledge_base(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The corpus, each document in the one format `FORMATS` assigns it, built once per module."""
    out_dir = tmp_path_factory.mktemp("knowledge_base")
    build_knowledge_base(out_dir)
    return out_dir


@pytest.fixture(scope="module")
def two_formats(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Every document written twice, as plain text and as DOCX, so the two can be compared.

    `build_knowledge_base` gives each document one format, which is what an index test wants and
    the opposite of what this comparison needs, so the writers are called directly.
    """
    out_dir = tmp_path_factory.mktemp("two_formats")
    for stem in DOCUMENT_STEMS:
        markdown = source_text(stem)
        write_txt(out_dir / f"{stem}.txt", markdown)
        write_docx(out_dir / f"{stem}.docx", markdown)
    return out_dir


@pytest.fixture
def captured_logs() -> Iterator[list[logging.LogRecord]]:
    """Every record the engine's loggers emit while the test runs, at any level."""
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Capture(level=logging.DEBUG)
    logger = logging.getLogger("engine")
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


def parsed_sentences(document: ParsedDocument) -> list[str]:
    """Every sentence a parse produced, headings included, in reading order.

    Headings are included deliberately: a heading one format marks and another leaves as a line of
    text is the *same sentence* in both, and a comparison that looked only at section bodies would
    call that a difference.
    """
    found: list[str] = []
    for section in document.sections:
        found.extend(sentences(section.heading))
        found.extend(sentences(section.text))
    return found


def source_headings(stem: str) -> list[str]:
    """The headings of one committed Markdown source, hashes stripped, in order."""
    return [
        line.lstrip("#").strip()
        for line in (SOURCE_DIR / f"{stem}.md").read_text(encoding="utf-8").splitlines()
        if line.startswith("#")
    ]


def folded(text: str) -> str:
    """`text` with what the PDF writer cannot carry folded, and its spacing normalised.

    `make_docs.write_pdf` folds an em dash to a spaced hyphen because WinAnsi has no code point for
    one, and extraction spaces glyphs by position rather than by character. Comparing a PDF's
    headings against the Markdown they were written from without the same fold would report the
    writer's substitution as the parser's mistake.
    """
    return " ".join(text.replace("—", "-").split())


def contiguous_runs(parts: Sequence[str]) -> set[str]:
    """Every text a chunker may legitimately emit from `parts`: one unbroken run of whole sentences.

    This is the precise form of "never splits a sentence", and it is stronger than re-splitting a
    chunk and checking the pieces: re-splitting cannot tell a chunk that starts mid-sentence from
    one that merely joined a table row to the paragraph after it.
    """
    return {
        " ".join(parts[start:end]) for start in range(len(parts)) for end in range(start + 1, len(parts) + 1)
    }


def long_section_document(stem: str) -> ParsedDocument:
    """One real document flattened into a single section, so the splitting rules have work to do.

    The corpus is written in short sections on purpose - that is what a good policy document looks
    like - and every one of them fits in a chunk. Joining them is how a section longer than the
    target is obtained without inventing prose that nobody wrote.
    """
    parsed = parse(SOURCE_DIR / f"{stem}.md")
    body = "\n\n".join(section.text for section in parsed.sections if section.text)
    return ParsedDocument(
        name=parsed.name,
        media_type=parsed.media_type,
        sections=(Section(heading="Everything", text=body, page=None, ordinal=0),),
        pages=None,
        warnings=(),
    )


# ---------------------------------------------------------------------------
# Reading the corpus
# ---------------------------------------------------------------------------
def test_the_corpus_these_sweeps_run_over_holds_every_format() -> None:
    """A sweep that shrank to nothing, or to one format, would look exactly like a passing suite."""
    assert len(DOCUMENT_STEMS) >= 1, "the corpus is empty"
    assert set(FORMATS.values()) == {
        parser.value for parser in DOCUMENT_PARSERS
    }, "a format the corpus is written in has no parser, or the other way round"


@pytest.mark.parametrize("stem", DOCUMENT_STEMS)
def test_every_document_in_the_corpus_parses_in_the_format_it_was_written_in(
    stem: str, knowledge_base: Path
) -> None:
    """All four readers work on the real corpus, and each reports what it actually measured.

    `pages` is the honest example: a PDF knows how many pages it has and the other three formats do
    not, so those report `None` rather than 1 (plan section 13.3).
    """
    extension = FORMATS[stem]
    document = parse(knowledge_base / f"{stem}.{extension}")

    assert document.media_type == extension, f"{stem} was read as {document.media_type}"
    assert document.sections, f"{stem} parsed to no sections"
    assert all(
        section.ordinal == index for index, section in enumerate(document.sections)
    ), f"{stem} produced sections that are not in reading order"
    if extension == "pdf":
        assert document.pages is not None and document.pages >= 1, f"{stem} reported no page count"
    else:
        assert document.pages is None, f"{stem} is unpaged but reported {document.pages} pages"


@pytest.mark.parametrize("stem", DOCUMENT_STEMS)
def test_the_same_document_in_two_formats_yields_the_same_sentences(stem: str, two_formats: Path) -> None:
    """One knowledge base must not answer differently because a team exported a different format.

    Plain text and DOCX are compared because both are written from the same prose by
    `make_docs`, so any difference is the parsers' and not the writers'.
    """
    from_text = parsed_sentences(parse(two_formats / f"{stem}.txt"))
    from_docx = parsed_sentences(parse(two_formats / f"{stem}.docx"))

    assert from_text == from_docx, f"{stem} reads differently as text and as DOCX"
    assert len(from_text) > 5, f"{stem} yielded only {len(from_text)} sentences, so this proves little"


@pytest.mark.parametrize("stem", DOCUMENT_STEMS)
def test_every_heading_the_source_carries_is_found_in_the_format_it_was_written_in(
    stem: str, knowledge_base: Path
) -> None:
    """A policy document is read as its several headed sections and never collapsed into one.

    The expected headings come from the committed Markdown, not from a list written here, so a
    document that gains a section cannot quietly stop being checked.
    """
    document = parse(knowledge_base / f"{stem}.{FORMATS[stem]}")
    expected = [folded(heading) for heading in source_headings(stem)]

    assert [
        folded(section.heading) for section in document.sections
    ] == expected, f"{stem} lost or invented a heading"
    assert DOCUMENT_NO_HEADINGS not in document.warnings


def test_a_policy_document_is_several_sections_and_each_one_carries_its_own_prose(
    knowledge_base: Path,
) -> None:
    """The failure this guards is a parser that "works" while making every citation useless.

    One section named after the file is what a broken reader produces, and it reads as a success
    everywhere downstream: the index builds, the retriever retrieves, and every citation points at
    the whole document.
    """
    document = parse(knowledge_base / "policy_fair_use.docx")

    assert len(document.sections) > 5, f"the fair usage policy parsed into {len(document.sections)}"
    bodies = [section for section in document.sections if section.text.strip()]
    assert len(bodies) >= len(document.sections) - 1, "sections were opened with nothing under them"


def test_a_document_with_no_heading_at_all_becomes_one_section_named_after_the_file(
    tmp_path: Path,
) -> None:
    """A heading has to be recovered or admitted to, never guessed at from a line of prose."""
    path = tmp_path / "unstructured_note.txt"
    path.write_text("A note with no heading in it at all, written as one paragraph.\n", encoding="utf-8")

    document = parse(path)

    assert len(document.sections) == 1
    assert document.sections[0].heading == "unstructured_note.txt"
    assert document.warnings == (DOCUMENT_NO_HEADINGS,)


def test_the_name_a_citation_shows_is_the_one_the_caller_gave_and_not_the_one_on_disk(
    tmp_path: Path,
) -> None:
    """An upload is stored under a generated key; the name a reader recognises is the caller's."""
    path = tmp_path / "u_0a1b2c3d4e5f.txt"
    path.write_text("Refunds are paid back to the instrument they came from.\n", encoding="utf-8")

    document = parse(path, name="Refund policy.txt")

    assert document.name == "Refund policy.txt"
    assert document.sections[0].heading == "Refund policy.txt"


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------
def test_an_unsupported_extension_raises_its_own_code(tmp_path: Path) -> None:
    """The API maps a code onto a status, so "wrong kind of file" cannot share a code with "torn"."""
    path = tmp_path / "tariffs.rtf"
    path.write_bytes(b"{\\rtf1 nothing to see}")

    with pytest.raises(ParseError) as raised:
        parse(path)

    assert raised.value.code == DOCUMENT_TYPE_UNSUPPORTED
    assert "rtf" in raised.value.message
    assert raised.value.suggestion, "a refusal with no suggestion tells a user nothing to do"


def test_an_empty_file_raises_its_own_code_rather_than_indexing_nothing(tmp_path: Path) -> None:
    """An empty index that built successfully is the failure mode that wastes an afternoon."""
    path = tmp_path / "blank.md"
    path.write_text("   \n\n  \n", encoding="utf-8")

    with pytest.raises(ParseError) as raised:
        parse(path)

    assert raised.value.code == DOCUMENT_EMPTY


def test_a_corrupt_pdf_raises_its_own_code(tmp_path: Path) -> None:
    """A file that will not open is a different problem from one that opens and is blank."""
    path = tmp_path / "torn.pdf"
    path.write_bytes(b"%PDF-1.4\nthis is not a page tree\n%%EOF\n")

    with pytest.raises(ParseError) as raised:
        parse(path)

    assert raised.value.code == DOCUMENT_UNREADABLE


def test_a_file_that_is_not_there_is_unreadable_rather_than_empty(tmp_path: Path) -> None:
    """ "Nothing was uploaded" and "the upload had no text" send a user to different places."""
    with pytest.raises(ParseError) as raised:
        parse(tmp_path / "never_written.txt")

    assert raised.value.code == DOCUMENT_UNREADABLE


def test_a_code_that_is_not_in_the_table_cannot_be_raised() -> None:
    """`parse_error` is the only constructor, so an uncoded refusal cannot reach the API."""
    assert set(PARSE_ERRORS) == {DOCUMENT_TYPE_UNSUPPORTED, DOCUMENT_UNREADABLE, DOCUMENT_EMPTY}
    for code in PARSE_ERRORS:
        built = parse_error(code, name="a.pdf", suffix="pdf")
        assert built.code == code
        assert built.message.endswith("."), f"{code} has a message that is not a sentence"
        assert built.suggestion.endswith("."), f"{code} has a suggestion that is not a sentence"
    with pytest.raises(KeyError):
        parse_error("DOCUMENT_SMELLS_WRONG", name="a.pdf")


def test_no_log_line_the_parser_writes_carries_the_document_it_read(
    tmp_path: Path, captured_logs: list[logging.LogRecord]
) -> None:
    """A document name is customer data the moment it leaves the caller's own screen (13.7).

    Both paths are driven, because the failure path is where a value leaks without anybody having
    decided to log it: an exception's message is written verbatim by `logger.exception`, which is
    exactly why `log_failure` exists.
    """
    good = tmp_path / "good.txt"
    good.write_text("A policy line that is long enough to be prose.\n", encoding="utf-8")
    parse(good, name=f"{LEAK_SENTINEL}-policy.txt")

    torn = tmp_path / "torn.pdf"
    torn.write_bytes(b"%PDF-1.4\nbroken\n")
    with pytest.raises(ParseError):
        parse(torn, name=f"{LEAK_SENTINEL}-tariffs.pdf")

    assert captured_logs, "nothing was captured, so this test proves nothing"
    for record in captured_logs:
        rendered = f"{record.getMessage()} {record.args!r}"
        assert LEAK_SENTINEL not in rendered, f"a document name reached the log: {record.name}"


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------
def test_a_section_shorter_than_the_target_is_exactly_one_chunk(knowledge_base: Path) -> None:
    """Splitting a section that already fits would cost an embedding and buy nothing."""
    document = parse(knowledge_base / "policy_refunds.pdf")
    config = RagConfig()

    chunks = chunk_document(document, doc_id="d_short", config=config)

    filled = [section for section in document.sections if section.text.strip()]
    assert len(chunks) == len(filled), "a section that fits the target was split anyway"
    assert [chunk.section for chunk in chunks] == [section.heading for section in filled]


def test_a_section_longer_than_the_target_becomes_several_chunks_within_it() -> None:
    """The target is what an embedding model is being asked to read at once; it has to hold."""
    document = long_section_document("policy_refunds")
    config = RagConfig(chunk_tokens=SPLIT_TOKENS, chunk_overlap=SPLIT_OVERLAP)

    chunks = chunk_document(document, doc_id="d_long", config=config)

    assert len(chunks) > 1, "a section several times the target came back as one chunk"
    for chunk in chunks:
        assert (
            chunk.tokens <= config.chunk_tokens or len(sentences(chunk.text)) == 1
        ), f"chunk {chunk.ordinal} is {chunk.tokens} tokens and holds more than one sentence"


def test_a_chunk_holds_only_whole_sentences_of_the_section_it_came_from() -> None:
    """A half sentence embeds as something nobody wrote and quotes as something nobody said.

    The document chosen carries a retention table, because a table row is the piece that finishes
    without punctuation and is therefore the one a character-counting chunker would cut.
    """
    document = long_section_document("policy_privacy")
    config = RagConfig(chunk_tokens=SPLIT_TOKENS, chunk_overlap=SPLIT_OVERLAP)

    chunks = chunk_document(document, doc_id="d_whole", config=config)

    allowed = contiguous_runs(sentences(document.sections[0].text))
    assert len(chunks) > 1, "this section did not split, so nothing was proved about splitting"
    for chunk in chunks:
        assert chunk.text in allowed, f"chunk {chunk.ordinal} is not an unbroken run of whole sentences"


def test_the_overlap_between_two_chunks_is_whole_trailing_sentences() -> None:
    """The overlap exists so a claim split across a boundary is retrievable from either side.

    Taken as characters it would put half a claim at the head of the next chunk, which is the
    failure the no-split rule exists to prevent, so it is taken as whole trailing sentences.
    """
    document = long_section_document("policy_refunds")
    config = RagConfig(chunk_tokens=SPLIT_TOKENS, chunk_overlap=SPLIT_OVERLAP)

    chunks = chunk_document(document, doc_id="d_overlap", config=config)

    assert len(chunks) > 2, "too few chunks to have an overlap worth checking"
    overlaps = 0
    for earlier, later in pairwise(chunks):
        head = sentences(later.text)
        tail = sentences(earlier.text)
        shared = tuple(piece for piece in head if piece in tail)
        assert shared == head[: len(shared)], "the overlap is not the head of the later chunk"
        assert shared == tail[len(tail) - len(shared) :], "the overlap is not the tail of the earlier one"
        overlaps += 1 if shared else 0
    assert overlaps > 0, "no pair of chunks overlapped at all, so the setting does nothing"


def test_an_overlap_of_nothing_repeats_nothing() -> None:
    """Zero has to mean zero: a chunker that always repeated a sentence would double the index."""
    document = long_section_document("policy_refunds")
    config = RagConfig(chunk_tokens=SPLIT_TOKENS, chunk_overlap=0.0)

    chunks = chunk_document(document, doc_id="d_none", config=config)

    for earlier, later in pairwise(chunks):
        assert not set(sentences(earlier.text)) & set(sentences(later.text))


def test_a_rebuild_of_an_unchanged_document_produces_byte_identical_chunks(
    knowledge_base: Path,
) -> None:
    """The incremental rebuild in `DocIndexManifest` is only safe if this holds.

    A chunk id derived from a clock or a random draw would make every rebuild write new rows for
    documents that had not changed, and every stored citation would point at an id that no longer
    exists.
    """
    document = parse(knowledge_base / "faq_billing.txt")
    config = RagConfig(chunk_tokens=SPLIT_TOKENS, chunk_overlap=SPLIT_OVERLAP)

    first = chunk_document(document, doc_id="d_stable", config=config)
    second = chunk_document(document, doc_id="d_stable", config=config)

    assert [chunk.model_dump_json() for chunk in first] == [chunk.model_dump_json() for chunk in second]


def test_a_chunk_id_is_derived_from_the_document_id_and_the_ordinal_alone(
    knowledge_base: Path,
) -> None:
    """Two ids that differ must differ because the document or the position did, and nothing else."""
    document = parse(knowledge_base / "faq_billing.txt")
    config = RagConfig()

    one = chunk_document(document, doc_id="d_one", config=config)
    two = chunk_document(document, doc_id="d_two", config=config)

    assert [chunk.ordinal for chunk in one] == list(range(len(one)))
    assert len({chunk.chunk_id for chunk in one}) == len(one), "two chunks share an id"
    assert [chunk.text for chunk in one] == [chunk.text for chunk in two], "the text moved with the id"
    assert all(chunk.chunk_id.startswith("d_one") for chunk in one)
    assert not {chunk.chunk_id for chunk in one} & {chunk.chunk_id for chunk in two}


def test_a_chunk_carries_the_heading_and_the_page_of_the_section_it_came_from(
    knowledge_base: Path,
) -> None:
    """A citation is the heading and the page; a chunk that lost them cannot be checked by a reader."""
    document = parse(knowledge_base / "policy_refunds.pdf")

    chunks = chunk_document(document, doc_id="d_cite", config=RagConfig())

    headings = {section.heading: section.page for section in document.sections}
    for chunk in chunks:
        assert chunk.document == document.name
        assert chunk.section in headings, f"chunk {chunk.ordinal} cites a heading the document lacks"
        assert chunk.page == headings[chunk.section]


def test_a_heading_with_nothing_under_it_produces_no_chunk() -> None:
    """An index full of bare titles would spend its `top_k` on passages that answer nothing."""
    document = ParsedDocument(
        name="notices.md",
        media_type="md",
        sections=(
            Section(heading="Notice", text="", page=None, ordinal=0),
            Section(heading="Scope", text="This notice covers prepaid packs only.", page=None, ordinal=1),
        ),
        pages=None,
        warnings=(),
    )

    chunks = chunk_document(document, doc_id="d_bare", config=RagConfig())

    assert [chunk.section for chunk in chunks] == ["Scope"]


# ---------------------------------------------------------------------------
# Fingerprints
# ---------------------------------------------------------------------------
def test_a_fingerprint_is_stable_and_differs_for_differing_bytes() -> None:
    """A rebuild skips a document whose fingerprint has not moved, so both halves have to hold."""
    first = fingerprint(b"Refunds are paid back to the instrument they came from.")
    again = fingerprint(b"Refunds are paid back to the instrument they came from.")
    other = fingerprint(b"Refunds are paid back to the instrument they came from!")

    assert first == again
    assert first != other
    assert first.startswith(DOCUMENT_FINGERPRINT_PREFIX), "the digest scheme is not named in the string"
    assert fingerprint(b"") != ""


def test_a_fingerprint_names_the_bytes_and_not_the_sections(knowledge_base: Path) -> None:
    """The question it answers is "is this the same file?", not "does it read the same?"."""
    markdown = (knowledge_base / "faq_activation.md").read_bytes()

    assert fingerprint(markdown) == fingerprint(markdown)
    assert fingerprint(markdown) != fingerprint(markdown + b"\n")
