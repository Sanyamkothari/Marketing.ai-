"""Tests for the synthetic knowledge-base generator itself (plan §10).

`make_docs.py` is test infrastructure every generative milestone leans on, so it gets the same
treatment as engine code. Three properties matter and each is proved rather than assumed:

**The four formats carry the same prose.** A document written as a PDF and the same document
written as Markdown must come back out of their parsers saying the same things. The check is a
sentence-level one rather than a byte-level one, because a DOCX keeps a heading as a style and a
PDF keeps it as a line of text, and neither is wrong — what would be wrong is a sentence that
survives in one format and not in another.

**Nothing Markdown survives the build.** A retrieved chunk that still carries `##` or a table's
pipes is a chunk whose embedding spends its similarity on punctuation. The built formats are
checked for both, and the PDF additionally for the `?` that a WinAnsi encoder leaves where an em
dash used to be.

**The complaint draw is deterministic and really carries PII.** The whole point of the frame is to
give the generative redaction something to find, so a test that did not assert the planted values
are present would let an empty `_planted_pii` pass. The Phase 1 detectors' own patterns are used to
look for them, so the fixture cannot drift away from the thing it exists to exercise.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from docx import Document
from pypdf import PdfReader

from engine.stages.ingest import PII_DETECTORS
from tests.fixtures.make_docs import (
    COMPLAINT_PII_KINDS,
    DOCUMENT_STEMS,
    DOCX,
    FORMATS,
    MD,
    PDF,
    SOURCE_DIR,
    TXT,
    build_knowledge_base,
    generate_complaints,
    main,
    plain_text,
    read_reference_qa,
    source_text,
)

SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
MIN_SENTENCE_WORDS = 8  # short lines are table cells and headings, which the formats render apart


@pytest.fixture(scope="module")
def knowledge_base(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One built corpus for the whole module; building it fourteen times proves nothing extra."""
    out_dir = tmp_path_factory.mktemp("knowledge_base")
    build_knowledge_base(out_dir)
    return out_dir


def extracted_text(path: Path) -> str:
    """The text a parser gets out of one built document, whatever format it is in."""
    if path.suffix == ".pdf":
        return "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)
    if path.suffix == ".docx":
        return "\n".join(paragraph.text for paragraph in Document(str(path)).paragraphs)
    return path.read_text(encoding="utf-8")


def normalised(text: str) -> str:
    """`text` as one line, with the characters WinAnsi cannot carry folded to their ASCII form.

    An em dash folds to a hyphen and a rupee sign to `Rs` because that is what the PDF encoder
    writes; comparing without the fold would report every price as a lost fact when what changed
    was an encoding. Collapsing the whitespace is what lets a PDF, whose writer hard-wraps at 92
    columns, be compared with a DOCX, whose writer does not.
    """
    folded = text.replace("—", " - ").replace("–", "-").replace("₹", "Rs ")
    folded = folded.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return " ".join(folded.split())


def long_sentences(text: str) -> tuple[str, ...]:
    """The sentences of `text` long enough to be worth looking for in another rendering of it.

    Short ones are headings and table cells, which each writer lays out its own way; a sentence of
    eight words or more says something, and saying it is what must survive the round trip.
    """
    sentences = []
    for raw in SENTENCE_SPLIT.split(normalised(text)):
        cleaned = raw.strip()
        if len(cleaned.split()) >= MIN_SENTENCE_WORDS:
            sentences.append(cleaned.rstrip("."))
    return tuple(sentences)


# ---------------------------------------------------------------------------
# The corpus
# ---------------------------------------------------------------------------
def test_every_stem_in_the_format_map_has_a_source_document() -> None:
    """The map is the corpus: a stem with no file behind it is an index that silently loses a document."""
    missing = [stem for stem in DOCUMENT_STEMS if not (SOURCE_DIR / f"{stem}.md").is_file()]
    assert missing == []


def test_every_source_document_is_in_the_format_map() -> None:
    """And the other way round, so a document added to the directory cannot be left unbuilt."""
    stems = sorted(path.stem for path in SOURCE_DIR.glob("*.md"))
    assert stems == sorted(DOCUMENT_STEMS)


def test_the_corpus_is_big_enough_to_retrieve_against() -> None:
    """A retrieval test over three documents proves nothing about ranking; this keeps the corpus real."""
    assert len(DOCUMENT_STEMS) >= 12
    assert sum(len(source_text(stem).split()) for stem in DOCUMENT_STEMS) >= 4_000


def test_all_four_formats_are_exercised() -> None:
    """Each parser needs at least two documents, or one odd document could carry a whole format."""
    counts = {
        extension: sum(1 for value in FORMATS.values() if value == extension)
        for extension in (MD, PDF, DOCX, TXT)
    }
    assert all(count >= 2 for count in counts.values()), counts


def test_every_document_declares_itself_synthetic() -> None:
    """Plan §13.3: nothing here may be mistaken for a real tariff, and the source says so in the file."""
    for stem in DOCUMENT_STEMS:
        assert "*Synthetic document." in source_text(stem), stem


# ---------------------------------------------------------------------------
# The build
# ---------------------------------------------------------------------------
def test_the_build_writes_one_file_per_stem_in_its_declared_format(knowledge_base: Path) -> None:
    written = sorted(path.name for path in knowledge_base.iterdir())
    assert written == sorted(f"{stem}.{FORMATS[stem]}" for stem in DOCUMENT_STEMS)


@pytest.mark.parametrize("stem", [stem for stem, fmt in FORMATS.items() if fmt == MD])
def test_a_markdown_document_is_written_through_unchanged(stem: str, knowledge_base: Path) -> None:
    """Markdown is the source format, so its writer has nothing to do and must do nothing."""
    assert (knowledge_base / f"{stem}.md").read_text(encoding="utf-8") == source_text(stem)


@pytest.mark.parametrize("stem", [stem for stem, fmt in FORMATS.items() if fmt != MD])
def test_the_built_document_says_what_the_source_says(stem: str, knowledge_base: Path) -> None:
    """Every long sentence of the source survives the trip through its format's writer and parser."""
    built = knowledge_base / f"{stem}.{FORMATS[stem]}"
    expected = long_sentences(plain_text(source_text(stem)))
    assert expected, f"{stem} has no sentence long enough to compare"
    extracted = normalised(extracted_text(built))
    lost = [sentence for sentence in expected if sentence not in extracted]
    assert lost == [], f"{stem}: {lost[:2]}"


@pytest.mark.parametrize("stem", [stem for stem, fmt in FORMATS.items() if fmt != MD])
def test_no_markdown_survives_into_a_built_format(stem: str, knowledge_base: Path) -> None:
    """A heading's hashes and a table's pipes are punctuation an embedding should never have to carry."""
    text = extracted_text(knowledge_base / f"{stem}.{FORMATS[stem]}")
    assert "##" not in text
    assert "|" not in text


@pytest.mark.parametrize("stem", [stem for stem, fmt in FORMATS.items() if fmt == PDF])
def test_the_pdf_encoder_never_leaves_a_replacement_character(stem: str, knowledge_base: Path) -> None:
    """An em dash folded to `-` is readable; one replaced by `?` is a hole in a sentence."""
    text = extracted_text(knowledge_base / f"{stem}.pdf")
    # A question mark legitimately ends an FAQ heading, so the check is for the encoder's own
    # marker: a `?` that does not follow a word character is one the encoder put there.
    assert re.search(r"(?<![\w)])\?", text) is None, text[:400]


@pytest.mark.parametrize("stem", [stem for stem, fmt in FORMATS.items() if fmt == DOCX])
def test_the_docx_keeps_headings_as_headings(stem: str, knowledge_base: Path) -> None:
    """A DOCX that made every line a paragraph would give the chunker no structure to split on."""
    paragraphs = Document(str(knowledge_base / f"{stem}.docx")).paragraphs
    headings = [p for p in paragraphs if p.style is not None and p.style.name.startswith("Heading")]
    assert len(headings) >= 3


def test_a_narrowed_corpus_still_builds(tmp_path: Path) -> None:
    """A test wanting a two-document index must be able to ask for one without building fourteen."""
    written = build_knowledge_base(tmp_path, stems=["policy_refunds", "plans_prepaid"])
    assert [path.name for path in written] == ["policy_refunds.pdf", "plans_prepaid.md"]


def test_an_unknown_stem_is_refused_rather_than_skipped(tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        build_knowledge_base(tmp_path, stems=["no_such_document"])


def test_the_build_is_byte_identical_when_repeated(tmp_path: Path) -> None:
    """Determinism: a fixture that changed between runs would make an index fingerprint meaningless."""
    first = build_knowledge_base(tmp_path / "a")
    second = build_knowledge_base(tmp_path / "b")
    for left, right in zip(first, second, strict=True):
        if left.suffix == ".docx":
            continue  # a DOCX carries a zip timestamp; its text is compared above instead
        assert left.read_bytes() == right.read_bytes(), left.name


# ---------------------------------------------------------------------------
# The reference Q&A set
# ---------------------------------------------------------------------------
def test_the_reference_set_has_the_columns_the_evaluation_reads() -> None:
    """In the template's own order: primary key first, target last, as every other template is."""
    frame = read_reference_qa()
    assert list(frame.columns) == ["question", "expect_refusal", "source_doc", "reference_answer"]


def test_the_reference_set_is_big_enough_and_carries_refusals() -> None:
    """A pass rate over ten questions is noise, and a set with no refusals never tests the refusal."""
    frame = read_reference_qa()
    assert len(frame) >= 40
    assert int(frame["expect_refusal"].sum()) >= 5


def test_an_answerable_question_names_a_document_and_a_refusal_does_not() -> None:
    """`source_doc` is what the retrieval-hit check is scored against, so it must be present and real."""
    frame = read_reference_qa()
    answerable = frame[~frame["expect_refusal"]]
    refusals = frame[frame["expect_refusal"]]
    assert (answerable["reference_answer"].str.len() > 0).all()
    assert answerable["source_doc"].isin([f"{stem}.md" for stem in DOCUMENT_STEMS]).all()
    assert (refusals["reference_answer"] == "").all()
    assert (refusals["source_doc"] == "").all()


def test_every_answerable_question_is_actually_answered_by_its_document() -> None:
    """Question and answer together must share real vocabulary with the document they name.

    Stop words are dropped and a four-word overlap is demanded, which is low enough to allow a
    paraphrase - and a terse answer such as "7 days, and it can be used once" leans on its
    question's words for the rest - and high enough to catch a row attributed to the wrong file.
    """
    stop = {
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "of",
        "to",
        "in",
        "on",
        "and",
        "or",
        "for",
        "it",
        "that",
        "this",
        "with",
        "as",
        "at",
        "by",
        "be",
        "from",
        "not",
        "no",
        "yes",
        "can",
        "if",
    }
    frame = read_reference_qa()
    for row in frame[~frame["expect_refusal"]].itertuples():
        document = set(re.findall(r"[a-z]+", source_text(str(row.source_doc)[:-3]).lower()))
        asked = f"{row.question} {row.reference_answer}".lower()
        vocabulary = set(re.findall(r"[a-z]+", asked)) - stop
        assert len(vocabulary & document) >= 4, f"{row.question!r} does not read like {row.source_doc}"


# ---------------------------------------------------------------------------
# Complaint text
# ---------------------------------------------------------------------------
def test_the_complaint_frame_has_the_two_columns_the_engine_reads() -> None:
    frame = generate_complaints(50)
    assert list(frame.columns) == ["entity_key", "text", "topic"]
    assert frame["entity_key"].is_unique
    assert (frame["text"].str.len() > 0).all()


def test_the_complaint_draw_is_deterministic() -> None:
    left = generate_complaints(120, seed=7)
    right = generate_complaints(120, seed=7)
    assert left.equals(right)
    assert not left.equals(generate_complaints(120, seed=8))


def test_zero_rows_is_refused_rather_than_returning_an_empty_frame() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        generate_complaints(0)


@pytest.mark.parametrize("kind", COMPLAINT_PII_KINDS)
def test_each_planted_pii_shape_is_found_by_the_phase_1_detector_that_owns_it(kind: str) -> None:
    """The fixture exists to be redacted, so the patterns that will hunt it must match it today.

    `detect_pii` full-matches a whole cell, which free text never is; the detector's *pattern* is
    reused with `search` here for the same reason the generative redaction will have to.
    """
    detector = next(d for d in PII_DETECTORS if d.kind == kind)
    assert detector.value_pattern is not None
    text = " ".join(generate_complaints(200)["text"])
    assert detector.value_pattern.search(text) is not None, kind


def test_most_complaints_carry_no_pii_at_all() -> None:
    """A queue where every line carries an identifier would let a redactor that blanks everything pass."""
    frame = generate_complaints(200)
    with_pii = frame["text"].str.contains("example.invalid|\\+91 90000", regex=True).sum()
    assert 0 < with_pii < len(frame) // 2


def test_every_complaint_topic_is_drawn_at_least_once() -> None:
    """A segment builder is tested on the spread of topics, so the default draw must produce one."""
    frame = generate_complaints(200)
    assert frame["topic"].nunique() >= 5


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------
def test_the_command_line_writes_both_a_corpus_and_a_complaints_file(tmp_path: Path) -> None:
    assert main(["--out-dir", str(tmp_path), "--rows", "25"]) == 0
    assert len(list((tmp_path / "knowledge_base").iterdir())) == len(DOCUMENT_STEMS)
    assert (tmp_path / "complaints_synthetic.csv").is_file()
