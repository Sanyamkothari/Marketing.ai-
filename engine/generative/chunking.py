"""Cutting a parsed document into the passages an index stores and an answer cites.

A chunk is the unit of everything downstream: it is what is embedded, what is retrieved, what a
guardrail measures an answer against and what a citation quotes. Three properties are therefore
worth more here than cleverness, and each one costs something that was given up on purpose.

**A chunk never crosses a heading.** Splitting is done inside one `Section` and never over two, so
`Chunk.section` is a fact rather than a guess and a citation can say *which* part of the refund
policy an answer came from. The cost is that a short section becomes a short chunk instead of being
packed with the next one; a packed chunk would have been cheaper to embed and would have made every
citation on it ambiguous.

**A chunk never splits a sentence.** The unit of packing is a whole sentence, the overlap is whole
trailing sentences, and a sentence longer than the target becomes an oversized chunk of its own
rather than being cut. A half sentence embeds as something nobody wrote and quotes as something
nobody said. The cost is that `config.chunk_tokens` is a target rather than a ceiling, and
:func:`chunk_document` is honest about that instead of trimming to fit.

**A rebuild of an unchanged document produces byte-identical chunks.** Nothing here reads the
clock, samples, or depends on dict order: `chunk_id` is derived from `(doc_id, ordinal)` alone.
That is what makes the incremental rebuild in `DocIndexManifest` safe - a document whose
:func:`fingerprint` has not moved can be skipped, and the chunks already in the index are exactly
the chunks a rebuild would have written.

Token counts come from `engine.llm.estimate_tokens` and from nowhere else. The number is the
chars-over-four estimate and is marked as an estimate at its source; what matters here is that the
chunker, the budget and the usage meter all mean the same thing by "a token", because two
definitions would make a budget that was measured against one and enforced against the other.
"""

from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING, Final

from engine.generative.contracts import Chunk
from engine.llm import estimate_tokens
from engine.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

    from engine.config import RagConfig
    from engine.generative.parsers import ParsedDocument, Section

__all__ = [
    "CHUNK_ID_DIGITS",
    "DOCUMENT_FINGERPRINT_ALGORITHM",
    "DOCUMENT_FINGERPRINT_PREFIX",
    "chunk_document",
    "fingerprint",
    "sentences",
]

_LOGGER = get_logger(__name__)


CHUNK_ID_DIGITS: Final[int] = 5
"""Width the ordinal is padded to in a chunk id, so a listing of an index sorts in reading order.

A document with more chunks than the padding allows still gets unique ids - the ordinal simply
grows a digit - so this is a presentation choice and never a limit.
"""

DOCUMENT_FINGERPRINT_ALGORITHM: Final[str] = "sha256-document"
"""The digest scheme :func:`fingerprint` uses, named for what it covers.

Deliberately not `engine.stages.ingest`'s `sha256:v1`, which digests a *parsed table* and would
imply columns and rows, and deliberately not `engine.pipeline`'s `sha256-file`, which marks an
upload that a run never managed to parse. This one is the bytes of a document that was parsed, and
a reader who sees the three side by side can tell which question each of them answers (DEC-042 set
the precedent for naming a digest after its subject).
"""

DOCUMENT_FINGERPRINT_PREFIX: Final[str] = f"{DOCUMENT_FINGERPRINT_ALGORITHM}:"
"""The scheme travels inside the string, because `IndexedDocument.fingerprint` is one field.

`DatasetFingerprint` can afford a separate `algorithm` field; a single string cannot, so it carries
the prefix the way `ingest.FINGERPRINT_PREFIX` does and a v2 scheme is never mistaken for a v1 one.
"""

_PARAGRAPH_BREAK: Final[re.Pattern[str]] = re.compile(r"\n\s*\n")
"""A blank line ends a paragraph, whatever came before it.

A table row and a heading finish without a full stop, so splitting on sentence punctuation alone
would have glued a row to the paragraph after it and made one unsplittable run of the pair.
"""

_SENTENCE_BREAK: Final[re.Pattern[str]] = re.compile(r"(?<=[.!?])\s+(?=[\"'(\[]?[A-Z0-9])")
"""Whitespace after a full stop, a question mark or an exclamation mark that starts something new.

Requiring the next character to open a sentence is what keeps `e.g. the fibre plans` and
`Rs 1,399.50 a month` in one piece: an abbreviation and a decimal point are both followed by a
lower-case letter or by nothing at all.
"""


def sentences(text: str) -> tuple[str, ...]:
    """Split `text` into the whole sentences a chunk is packed from, in order.

    Paragraphs are split first and sentences within them second, so a table row, a heading line and
    a paragraph that ends without punctuation each stay a unit of their own. Blank pieces are
    dropped; a piece is otherwise returned exactly as it was written, because a chunk is quoted
    back to a reader and normalising it here would make the quotation wrong.
    """
    found: list[str] = []
    for paragraph in _PARAGRAPH_BREAK.split(text):
        for piece in _SENTENCE_BREAK.split(paragraph):
            stripped = piece.strip()
            if stripped:
                found.append(stripped)
    return tuple(found)


def _tokens(text: str) -> int:
    """How long `text` is, in the one definition of a token this engine has."""
    return estimate_tokens(text).tokens


def _join(parts: Sequence[str]) -> str:
    """The chunk text for `parts`: whole sentences, single-spaced.

    The same passage therefore reads identically whether it arrived as a hard-wrapped PDF, a DOCX
    paragraph or a Markdown table, which is what lets a rebuild from a different export of the same
    document produce the same chunk.
    """
    return " ".join(parts)


def _overlap_tail(parts: Sequence[str], budget: int) -> list[str]:
    """The longest run of whole trailing sentences of `parts` that fits in `budget` tokens.

    Whole sentences, not characters: an overlap cut mid-sentence would put half a claim at the head
    of the next chunk, which is the failure the no-split rule exists to prevent. An empty list when
    the budget is zero or when even the last sentence does not fit.
    """
    if budget <= 0:
        return []
    tail: list[str] = []
    for part in reversed(parts):
        candidate = [part, *tail]
        if _tokens(_join(candidate)) > budget:
            break
        tail = candidate
    return tail


def _section_chunks(section: Section, target: int, overlap: int) -> list[str]:
    """Pack one section's sentences into chunk texts of at most `target` tokens.

    A single sentence over the target is emitted whole and alone, which is the only way a chunk
    exceeds the target. Dropping overlap sentences until the next sentence fits is what guarantees
    the loop makes progress in that case rather than packing the same tail for ever.
    """
    parts = sentences(section.text)
    if not parts:
        return []
    if _tokens(section.text) <= target:
        return [_join(parts)]

    texts: list[str] = []
    current: list[str] = []
    for part in parts:
        if current and _tokens(_join([*current, part])) > target:
            texts.append(_join(current))
            current = _overlap_tail(current, overlap)
            while current and _tokens(_join([*current, part])) > target:
                current.pop(0)
        current.append(part)
    if current:
        texts.append(_join(current))
    return texts


def chunk_document(parsed: ParsedDocument, *, doc_id: str, config: RagConfig) -> tuple[Chunk, ...]:
    """Cut `parsed` into `Chunk` rows, heading by heading, in reading order.

    `config.chunk_overlap` is a share of the target rather than a count of tokens, which is what
    the setting means everywhere else it is shown; the share is turned into a token budget once,
    here, so the two never disagree.

    A section with no prose under it produces no chunk. A heading on its own retrieves as a title
    with no answer beneath it, and an index full of them would spend its `top_k` on nothing.
    """
    target = config.chunk_tokens
    overlap = round(target * config.chunk_overlap)
    chunks: list[Chunk] = []
    for section in parsed.sections:
        for text in _section_chunks(section, target, overlap):
            ordinal = len(chunks)
            chunks.append(
                Chunk(
                    chunk_id=f"{doc_id}-{ordinal:0{CHUNK_ID_DIGITS}d}",
                    doc_id=doc_id,
                    document=parsed.name,
                    section=section.heading,
                    ordinal=ordinal,
                    page=section.page,
                    tokens=_tokens(text),
                    text=text,
                )
            )
    _LOGGER.info(
        "chunk.document sections=%d chunks=%d target_tokens=%d overlap_tokens=%d",
        len(parsed.sections),
        len(chunks),
        target,
        overlap,
    )
    return tuple(chunks)


def fingerprint(data: bytes) -> str:
    """The content hash of one document's bytes, scheme included.

    Bytes rather than parsed text on purpose: the question this answers is "is this the same file
    the index was built from?", and a document re-exported from the same source is a different file
    whose sections may have moved.
    """
    return DOCUMENT_FINGERPRINT_PREFIX + hashlib.sha256(data).hexdigest()
