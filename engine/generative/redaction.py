"""Finding and removing personal data *inside* free text.

Phase 1 already detects PII, and this module does not detect anything Phase 1 does not: it imports
`engine.stages.ingest.PII_DETECTORS` and uses the same patterns. What differs is where it looks.

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

from engine.stages.ingest import PII_DETECTORS

__all__ = ["REDACTION_PATTERN", "contains_pii", "find", "marker_for", "redact"]

_MARKER_PREFIX: Final[str] = "[REDACTED:"
_MARKER_SUFFIX: Final[str] = "]"

REDACTION_PATTERN: Final[re.Pattern[str]] = re.compile(r"\[REDACTED:[a-z]+\]")
"""What a marker looks like, so a guardrail can tell a redaction from an identifier that got through."""

_SCANNERS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = tuple(
    (detector.kind, detector.value_pattern)
    for detector in PII_DETECTORS
    if detector.value_pattern is not None and detector.kind != "name"
)
"""The Phase 1 detectors worth running over free text, in their own order.

`name` is left out on purpose. Its pattern is "one to four capitalised words", which inside a
sentence matches the first word of every sentence, every product name and every place - it is
usable over a column, where an open vocabulary distinguishes a roster from a category, and it is
not usable over prose. Leaving it in would redact the text into uselessness; leaving it out is
recorded here so nobody adds it back without reading this paragraph (DEC-213).
"""


def marker_for(kind: str) -> str:
    """What replaces a match of `kind`."""
    return f"{_MARKER_PREFIX}{kind}{_MARKER_SUFFIX}"


def find(text: str) -> tuple[str, ...]:
    """The detector kinds present in `text`, in `PII_DETECTORS` order, each at most once.

    Returns kinds and never values, so the result is safe to log, to put in a warning and to store
    in an artefact.
    """
    return tuple(kind for kind, pattern in _SCANNERS if pattern.search(text) is not None)


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
    redacted = text
    kinds: list[str] = []
    for kind, pattern in _SCANNERS:
        replaced, count = pattern.subn(marker_for(kind), redacted)
        if count:
            kinds.append(kind)
            redacted = replaced
    return redacted, tuple(kinds)
