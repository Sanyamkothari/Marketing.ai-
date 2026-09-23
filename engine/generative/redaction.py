"""Finding and removing personal data *inside* free text.

Phase 1 already detects PII, and this module does not detect anything Phase 1 does not: since M36
the patterns, the scanners and the substring redaction all live in `engine.pii` (DEC-092), which the
profiler also uses to hide contacts inside a free-text column (DEC-095), and this module is the
generative engine's name for them. What differs from `detect_pii` is where it looks.

`ingest.detect_pii` asks "is this column made of e-mail addresses?" and answers with `fullmatch`
over a sample of whole cells, which is right for a column and useless for a complaint. A complaint
is a sentence with a phone number in the middle of it, and the generative engine handles two kinds
of text a column never contains: a customer's own words on their way into an evidence pack, and a
model's completion on its way to a screen. Both need `finditer`, and both need the text back with
the match taken out rather than the column dropped.

Two rules hold throughout, and they are why this is a module rather than three lines at two call
sites:

**A matched value is never returned, logged or stored.** `find` gives back detector *kinds*;
`redact` gives back the text with each match replaced by a marker naming its kind, and the kinds it
replaced. Nothing in this module has a code path that carries a matched substring out of it, which
is the same promise `ingest.detect_pii`'s docstring makes.

**A marker says what it hid.** `[REDACTED:email]` rather than `[REDACTED]`, because an evidence
pack that says an identifier was removed is more useful to a reader than one that says something
was, and because a test can then assert that the right detector fired without quoting the value.
"""

from __future__ import annotations

import re
from typing import Final

from engine import pii

__all__ = ["REDACTION_PATTERN", "contains_pii", "find", "marker_for", "redact"]

REDACTION_PATTERN: Final[re.Pattern[str]] = pii.REDACTION_MARKER_PATTERN
"""What a marker looks like, so a guardrail can tell a redaction from an identifier that got through."""

# `name` is not among the scanners, on purpose: its pattern is "one to four capitalised words",
# which inside a sentence matches the first word of every sentence, every product name and every
# place. It is usable over a column, where an open vocabulary distinguishes a roster from a
# category, and it is not usable over prose. Leaving it in would redact the text into uselessness;
# leaving it out is recorded in `engine.pii.TEXT_SCANNERS` so nobody adds it back without reading
# why (DEC-213).


def marker_for(kind: str) -> str:
    """What replaces a match of `kind`."""
    return pii.marker_for(kind)


def find(text: str) -> tuple[str, ...]:
    """The detector kinds present in `text`, in `PII_DETECTORS` order, each at most once.

    Returns kinds and never values, so the result is safe to log, to put in a warning and to store
    in an artefact.
    """
    return pii.find_in_text(text)


def contains_pii(text: str) -> bool:
    """True when anything a Phase 1 detector recognises is anywhere in `text`."""
    return bool(find(text))


def redact(text: str) -> tuple[str, tuple[str, ...]]:
    """`text` with every match replaced by its marker, and the kinds that were **replaced**.

    Detectors run in `PII_DETECTORS` order, and an earlier one consumes the characters a later one
    would also have matched: the digits of a phone number also satisfy the Aadhaar pattern, so a
    phone number is removed as a phone number and the Aadhaar detector then finds nothing left. The
    returned kinds are therefore what was taken out rather than everything `find` would report, and
    that is the more useful of the two - it is the statement an evidence pack carries. Either way
    nothing recognisable survives, which is the property that matters and the one the tests pin.
    """
    return pii.redact_text(text)
