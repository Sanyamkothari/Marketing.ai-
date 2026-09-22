"""Synthetic knowledge base, reference Q&A set and complaint text for the generative tests.

Everything this module produces is **synthetic**. "Northwind Telecom" is a fictional operator
invented for these tests; no tariff, policy, complaint or person described here is real, and
nothing written here is ever a source of numbers shown to a user (plan §13.3).

It is the generative counterpart of :mod:`tests.fixtures.make_data`, and it is held to the same bar
for the same reason: every generative milestone builds on it, so it is type-checked with the rest
of the source tree (DEC-206).

Design notes
------------
*The corpus is prose, the formats are built.* The fourteen documents live as Markdown under
``tests/fixtures/docs/source/``, where a human can read and edit them in a diff. The PDF, DOCX and
plain-text copies a parser test needs are **built from that same prose** by
:func:`build_knowledge_base`, so there is exactly one place a fact is written down and the four
parsers are all fed the same sentences. Which document is written in which format is fixed by
:data:`FORMATS`, not by a hash, so a test can name the PDF it means.

*The PDF writer is ours.* A text-bearing PDF needs sixty lines of uncompressed page description and
one of the base-14 fonts every reader carries; a library that writes one would be a production-
adjacent dependency bought for a test fixture alone (DEC-207). :func:`write_pdf` is that sixty
lines, and ``tests/fixtures/test_make_docs.py`` proves ``pypdf`` reads back exactly what went in.

*Deterministic.* The complaint generator draws from a :class:`numpy.random.Generator` seeded by a
BLAKE2b digest of ``(seed, purpose)``, exactly as :mod:`tests.fixtures.make_data` does, so the same
arguments always produce a byte-identical frame.

*PII is planted on purpose.* :func:`generate_complaints` writes invented e-mail addresses, phone
numbers, PAN-shaped and Aadhaar-shaped strings **inside** free text, because that is the shape the
generative redaction has to find: the Phase 1 detectors match a whole cell, and a complaint hides
its PII in the middle of a sentence. Every planted value is drawn from a documentation-reserved or
deliberately invalid range (``example.invalid`` domains, ``+91 90000 0xxxx`` numbers) so none of
them can collide with a real identifier.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final

import numpy as np
import pandas as pd

__all__ = [
    "COMPLAINT_PII_KINDS",
    "DEFAULT_COMPLAINT_ROWS",
    "DEFAULT_SEED",
    "DOCUMENT_STEMS",
    "FORMATS",
    "REFERENCE_QA_FILENAME",
    "SOURCE_DIR",
    "build_knowledge_base",
    "generate_complaints",
    "main",
    "plain_text",
    "read_reference_qa",
    "source_text",
    "write_docx",
    "write_pdf",
    "write_txt",
]

DOCS_DIR: Final[Path] = Path(__file__).resolve().parent / "docs"
SOURCE_DIR: Final[Path] = DOCS_DIR / "source"
"""Where the corpus is written and edited: one Markdown file per document."""

REFERENCE_QA_FILENAME: Final[str] = "reference_qa.csv"
"""The reference Q&A set, beside the corpus: ``question, reference_answer, expect_refusal, source_doc``."""

DEFAULT_SEED: Final[int] = 20_260_922
DEFAULT_COMPLAINT_ROWS: Final[int] = 200

ENTITY_KEY_COLUMN: Final[str] = "entity_key"
COMPLAINT_TEXT_COLUMN: Final[str] = "text"


MD: Final[str] = "md"
PDF: Final[str] = "pdf"
DOCX: Final[str] = "docx"
TXT: Final[str] = "txt"

FORMATS: Final[MappingProxyType[str, str]] = MappingProxyType(
    {
        # Every parser gets several documents, and the four formats are spread across the three
        # kinds of document (plans, FAQs, policies) so no parser is tested only on short prose.
        "plans_prepaid": MD,
        "plans_postpaid": PDF,
        "plans_broadband": DOCX,
        "faq_activation": MD,
        "faq_billing": TXT,
        "faq_roaming": PDF,
        "faq_devices": MD,
        "policy_refunds": PDF,
        "policy_fair_use": DOCX,
        "policy_privacy": TXT,
        "guide_router_setup": MD,
        "guide_esim": DOCX,
        "support_outages": PDF,
        "support_porting": MD,
    }
)
"""Document stem -> the format :func:`build_knowledge_base` writes it in. Fixed, so tests can name one."""

DOCUMENT_STEMS: Final[tuple[str, ...]] = tuple(FORMATS)
"""The corpus, in a stable order."""


# ---------------------------------------------------------------------------
# The corpus
# ---------------------------------------------------------------------------
def source_text(stem: str) -> str:
    """The Markdown source of one document, as committed.

    Raises `FileNotFoundError` naming the stem, because a typo here is otherwise an empty corpus
    that quietly retrieves nothing.
    """
    path = SOURCE_DIR / f"{stem}.md"
    if not path.is_file():
        raise FileNotFoundError(f"No source document {stem!r} in {SOURCE_DIR}")
    return path.read_text(encoding="utf-8")


def _title_and_body(markdown: str) -> tuple[str, list[str]]:
    """Split a source document into its H1 title and the remaining lines, italic note dropped.

    The italic "synthetic document" note every source carries is a statement about the fixture, not
    a fact about the fictional operator, so it is left out of the built formats: a retrieval test
    that matched it would be matching boilerplate present in all fourteen documents.
    """
    lines = markdown.splitlines()
    title = ""
    body: list[str] = []
    in_note = False
    for line in lines:
        stripped = line.strip()
        if not title and stripped.startswith("# "):
            title = stripped[2:].strip()
            continue
        if stripped.startswith("*Synthetic document."):
            in_note = True
        if in_note:
            if stripped.endswith("*"):
                in_note = False
            continue
        body.append(line.rstrip())
    while body and not body[0]:
        body.pop(0)
    while body and not body[-1]:
        body.pop()
    return title, body


@dataclass(frozen=True)
class PlainLine:
    """One logical line of a document once its Markdown has been taken off.

    `heading` carries the depth (1 for the title, 2 for `##`) or 0 for ordinary prose, because the
    DOCX writer needs to know and the PDF and text writers do not.
    """

    text: str
    heading: int


def _plain_lines(markdown: str) -> tuple[str, tuple[PlainLine, ...]]:
    """The title, and the body as logical lines: no hashes, no table pipes, no hard wrapping.

    The sources are hard-wrapped at about 100 columns so they read well in a diff, but a sentence
    split across two lines is a sentence a chunker would split and a retriever would half-match.
    Consecutive prose lines are therefore rejoined into one paragraph, and only a blank line, a
    heading or a table row ends it - which is what every one of the three writers below wants.
    """
    title, body = _title_and_body(markdown)
    lines: list[PlainLine] = []
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            lines.append(PlainLine(" ".join(paragraph), 0))
            paragraph.clear()

    for raw in body:
        stripped = raw.strip()
        if not stripped:
            flush()
        elif stripped.startswith("#"):
            flush()
            depth = len(stripped) - len(stripped.lstrip("#"))
            lines.append(PlainLine(stripped.lstrip("#").strip(), depth))
        elif "|" in stripped and set(stripped) <= {"|", "-", ":", " "}:
            continue  # a Markdown table's rule row carries nothing to read
        elif stripped.startswith("|"):
            flush()
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            lines.append(PlainLine(" - ".join(cells), 0))
        else:
            paragraph.append(stripped)
    flush()
    return title, tuple(lines)


def plain_text(markdown: str) -> str:
    """One source document with its Markdown taken off: the prose all three writers start from.

    This is what `write_txt` writes, and it is also what a test compares a built PDF or DOCX
    against: the question a format test asks is whether the *writer and its parser* kept the
    sentences, not whether Markdown syntax survived, and comparing against the raw source would
    report every table's pipes as a lost sentence.
    """
    title, lines = _plain_lines(markdown)
    out = [title, ""]
    for line in lines:
        out.append(line.text)
        if line.heading:
            out.append("")
    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------
def write_txt(path: Path, markdown: str) -> Path:
    """Write one document as plain text: headings lose their hashes, tables lose their pipes."""
    path.write_text(plain_text(markdown), encoding="utf-8")
    return path


_PDF_LINE_HEIGHT: Final[int] = 14
_PDF_TOP: Final[int] = 780
_PDF_LEFT: Final[int] = 56
_PDF_PAGE: Final[str] = "[0 0 595 842]"
_PDF_LINES_PER_PAGE: Final[int] = 52
_PDF_WRAP: Final[int] = 92


_WINANSI_FOLD: Final[MappingProxyType[str, str]] = MappingProxyType(
    {
        # WinAnsi has no code point for these, and `errors="replace"` would turn each into a "?",
        # which is a character the reference answers do not contain and the retriever would have to
        # match around. Folding them to their ASCII equivalent keeps the sentence readable.
        "—": " - ",  # em dash
        "–": "-",  # en dash
        "‘": "'",
        "’": "'",
        "“": '"',
        "”": '"',
        "₹": "Rs ",  # rupee sign
        " ": " ",
    }
)


def _pdf_escape(text: str) -> str:
    """Escape a literal string for a PDF content stream, folding what WinAnsi cannot carry."""
    folded = text
    for character, replacement in _WINANSI_FOLD.items():
        folded = folded.replace(character, replacement)
    escaped = folded.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
    return escaped.encode("latin-1", errors="replace").decode("latin-1")


def _wrap(lines: Sequence[str], width: int) -> list[str]:
    """Hard-wrap at `width` characters on spaces; an over-long word is left alone rather than cut."""
    wrapped: list[str] = []
    for line in lines:
        if len(line) <= width:
            wrapped.append(line)
            continue
        current = ""
        for word in line.split(" "):
            candidate = f"{current} {word}".strip()
            if current and len(candidate) > width:
                wrapped.append(current)
                current = word
            else:
                current = candidate
        wrapped.append(current)
    return wrapped


def write_pdf(path: Path, markdown: str) -> Path:
    """Write one document as an uncompressed, single-font PDF that any reader can extract text from.

    The file is a catalog, a page tree, one page object per page, one content stream each and one
    base-14 Helvetica font, laid out by hand with a correct cross-reference table. Nothing is
    compressed and nothing is subset, so the bytes are diffable and the text comes back out of
    `pypdf` exactly as it went in (DEC-207).
    """
    title, lines = _plain_lines(markdown)
    text_lines = _wrap([title, "", *(line.text for line in lines)], _PDF_WRAP)
    pages = [
        text_lines[start : start + _PDF_LINES_PER_PAGE]
        for start in range(0, max(len(text_lines), 1), _PDF_LINES_PER_PAGE)
    ] or [[""]]

    font_id = 3 + 2 * len(pages)
    objects: list[str] = ["", ""]  # 1 = catalog, 2 = page tree; filled once the ids are known
    kids = " ".join(f"{3 + 2 * index} 0 R" for index in range(len(pages)))
    objects[0] = "<< /Type /Catalog /Pages 2 0 R >>"
    objects[1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>"
    for index, page_lines in enumerate(pages):
        content_id = 4 + 2 * index
        stream = (
            f"BT\n/F1 11 Tf\n{_PDF_LINE_HEIGHT} TL\n1 0 0 1 {_PDF_LEFT} {_PDF_TOP} Tm\n"
            + "".join(f"({_pdf_escape(line)}) Tj T*\n" for line in page_lines)
            + "ET\n"
        )
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox {_PDF_PAGE} "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {content_id} 0 R >>"
        )
        objects.append(f"<< /Length {len(stream.encode('latin-1'))} >>\nstream\n{stream}endstream")
    objects.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body_text in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body_text}\nendobj\n".encode("latin-1")
    start_xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("latin-1")
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("latin-1")
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{start_xref}\n%%EOF\n").encode(
        "latin-1"
    )
    path.write_bytes(bytes(out))
    return path


def write_docx(path: Path, markdown: str) -> Path:
    """Write one document as a DOCX, headings as heading paragraphs and table rows as paragraphs."""
    from docx import Document  # imported here so the module loads without python-docx installed

    title, lines = _plain_lines(markdown)
    document = Document()
    document.add_heading(title, level=1)
    for line in lines:
        if line.heading:
            document.add_heading(line.text, level=min(line.heading, 4))
        else:
            document.add_paragraph(line.text)
    document.save(str(path))
    return path


def build_knowledge_base(out_dir: Path, *, stems: Sequence[str] | None = None) -> tuple[Path, ...]:
    """Write the corpus into `out_dir`, each document in the format :data:`FORMATS` assigns it.

    Returns the paths written, in :data:`DOCUMENT_STEMS` order. `stems` narrows the corpus for a
    test that wants a two-document index; an unknown stem raises `KeyError` rather than writing
    nothing, because a silently empty index is the failure mode that wastes an afternoon.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    chosen = tuple(stems) if stems is not None else DOCUMENT_STEMS
    written: list[Path] = []
    for stem in chosen:
        extension = FORMATS[stem]
        markdown = source_text(stem)
        path = out_dir / f"{stem}.{extension}"
        if extension == MD:
            path.write_text(markdown, encoding="utf-8")
        elif extension == TXT:
            write_txt(path, markdown)
        elif extension == PDF:
            write_pdf(path, markdown)
        else:
            write_docx(path, markdown)
        written.append(path)
    return tuple(written)


def read_reference_qa() -> pd.DataFrame:
    """The committed reference Q&A set as a frame, `expect_refusal` parsed to `bool`."""
    frame = pd.read_csv(DOCS_DIR / REFERENCE_QA_FILENAME, dtype=str, keep_default_na=False)
    frame["expect_refusal"] = frame["expect_refusal"].str.strip().str.lower() == "true"
    return frame


# ---------------------------------------------------------------------------
# Complaint text
# ---------------------------------------------------------------------------
def _seed_for(*parts: object) -> int:
    """A stable 64-bit seed from the parts, so a draw never depends on dict order or the clock."""
    digest = hashlib.blake2b("|".join(str(part) for part in parts).encode("utf-8"), digest_size=8)
    return int.from_bytes(digest.digest(), "big")


def _rng(*parts: object) -> np.random.Generator:
    return np.random.default_rng(_seed_for(*parts))


COMPLAINT_TOPICS: Final[tuple[tuple[str, str], ...]] = (
    # (topic, sentence). The topic is what an evidence pack groups on; the sentence is what the
    # reader sees. Each one echoes a feature name the synthetic churn data carries, so a root-cause
    # summary built from SHAP reasons has complaint text that genuinely supports it.
    ("outage", "The line has been down again since Monday and this is the third outage this quarter."),
    ("outage", "Signal drops every evening around eight and does not come back until morning."),
    ("billing", "I was charged twice for the same pack and nobody has reversed it."),
    ("billing", "My bill went up by four hundred rupees with no explanation on the statement."),
    ("price_change", "The renewal price is higher than what I was quoted when I joined."),
    ("price_change", "A competitor is offering the same speed for less and my rent keeps rising."),
    ("speed", "Speeds are nowhere near what the plan promises, especially at peak hours."),
    ("speed", "Downloads crawl after the daily allowance even though the pack says it is unlimited."),
    ("support", "I have called support four times and each time I have to explain it all again."),
    ("support", "The engineer visit was booked twice and nobody turned up either time."),
    ("usage_drop", "We have stopped using the connection because it is unreliable."),
    ("usage_drop", "Most of the household has moved to mobile data, the broadband barely gets used."),
)
"""Twelve complaint sentences over six topics; the generator picks one and dresses it."""

_CONTACT_PREFIXES: Final[tuple[str, ...]] = (
    "Reach me on",
    "My number is",
    "Please call",
    "Contact me at",
)

COMPLAINT_PII_KINDS: Final[tuple[str, ...]] = ("email", "phone", "pan", "aadhaar")
"""The shapes planted inside the complaint text, in the order :func:`generate_complaints` cycles them."""

_PII_SHARE: Final[float] = 0.35
"""Roughly a third of complaints carry an identifier, which is about what a real queue looks like."""


def _planted_pii(kind: str, index: int) -> str:
    """One invented identifier of `kind`, from a range that cannot collide with a real one.

    ``example.invalid`` is reserved by RFC 2606 and resolves nowhere; the phone numbers sit in a
    block that is not allocated; the PAN and Aadhaar shapes match the Phase 1 detectors' patterns
    while carrying digits no issuing authority hands out.
    """
    if kind == "email":
        return f"user{index:04d}@example.invalid"
    if kind == "phone":
        return f"+91 90000 {index % 10_000:04d}"
    if kind == "pan":
        letters = "".join(chr(ord("A") + (index + offset) % 26) for offset in range(5))
        return f"{letters}{index % 10_000:04d}{chr(ord('A') + index % 26)}"
    return f"2{index % 1000:03d} {(index * 7) % 10_000:04d} {(index * 13) % 10_000:04d}"


def generate_complaints(
    rows: int = DEFAULT_COMPLAINT_ROWS,
    *,
    seed: int = DEFAULT_SEED,
    key_prefix: str = "C-7",
) -> pd.DataFrame:
    """A deterministic `entity_key, text, topic` frame of synthetic complaints with planted PII.

    `topic` is carried so a test can assert which complaints a segment *should* have selected
    without re-implementing the selector. It is not part of the contract the engine reads, which is
    `entity_key` and `text` alone.
    """
    if rows < 1:
        raise ValueError(f"rows must be at least 1, got {rows}")
    topic_rng = _rng(seed, "complaint_topic")
    pii_rng = _rng(seed, "complaint_pii")
    choices = topic_rng.integers(0, len(COMPLAINT_TOPICS), size=rows)
    carries_pii = pii_rng.random(size=rows) < _PII_SHARE

    keys: list[str] = []
    texts: list[str] = []
    topics: list[str] = []
    for index in range(rows):
        topic, sentence = COMPLAINT_TOPICS[int(choices[index])]
        text = sentence
        if bool(carries_pii[index]):
            kind = COMPLAINT_PII_KINDS[index % len(COMPLAINT_PII_KINDS)]
            value = _planted_pii(kind, index)
            prefix = _CONTACT_PREFIXES[index % len(_CONTACT_PREFIXES)]
            text = f"{sentence} {prefix} {value}."
        keys.append(f"{key_prefix}{index:05d}")
        texts.append(text)
        topics.append(topic)
    return pd.DataFrame({ENTITY_KEY_COLUMN: keys, COMPLAINT_TEXT_COLUMN: texts, "topic": topics})


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------
def main(argv: Sequence[str] | None = None) -> int:
    """Write the knowledge base and a complaints CSV into a directory; returns the exit code."""
    parser = argparse.ArgumentParser(
        prog="python -m tests.fixtures.make_docs",
        description="Build the synthetic knowledge base and complaint text used by the generative tests.",
    )
    parser.add_argument("--out-dir", type=Path, required=True, help="directory to write into")
    parser.add_argument("--rows", type=int, default=DEFAULT_COMPLAINT_ROWS, help="complaint rows")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="seed for the complaint draw")
    args = parser.parse_args(argv)
    out_dir: Path = args.out_dir
    written = build_knowledge_base(out_dir / "knowledge_base")
    complaints = out_dir / "complaints_synthetic.csv"
    generate_complaints(args.rows, seed=args.seed).to_csv(complaints, index=False)
    print(f"wrote {len(written)} documents to {out_dir / 'knowledge_base'}")
    print(f"wrote {args.rows} complaints to {complaints}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
