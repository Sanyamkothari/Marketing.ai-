"""The engine's one PII detector: one pattern set, one set of rules, used by every call site.

Why one module (DEC-092, ruling D6)
-----------------------------------
Until M36 the engine had two detectors that disagreed. `engine.stages.ingest.detect_pii` - the one
the validation report reads - examined only STRING, TEXT and INTEGER columns, matched a whole cell
and asked for a per-kind match rate and an open vocabulary. `engine.stages.prepare._detect_pii` -
the one that actually redacted or dropped columns - examined every column, flagged a column on its
name alone and kept its own looser patterns, whose phone-number shape matched an ISO date. On the
library's `online-retail` file that meant `validation.json` said nothing about `snapshot_date` while
`prepare.json` redacted it as a phone number: the engine told the user one thing and did another.

A rule about people's data must have one behaviour, so both call sites now call :func:`detect_pii`
here, and so does the validate stage (through ingest) and the generative redaction module. What
the old detectors each knew is kept, as one table:

* **Value detectors** (:data:`VALUE_DETECTORS`): e-mail, phone, PAN, Aadhaar and personal name,
  each with a value pattern, a column-name pattern and the calibrated rates ingest already used.
  The patterns are the union of the shapes both old tables recognised - prepare's e-mail accepted
  any non-space local part, its phone shape accepted dots and parentheses - minus the one shape
  that was never a phone number: an ISO date. ``tests/unit/test_pii.py`` holds a case for every
  pattern of both old tables, so nothing either of them caught is lost by accident.
* **Name-only detectors** (:data:`NAME_ONLY_DETECTORS`): address, SSN and passport. No value
  pattern can recognise an address, so prepare found these columns by their header alone, and a
  column called `street_address` must not stop being redacted because the detectors were merged.
  They keep that meaning, now visible to the validation report as well.

The type gate is ingest's: FLOAT, BOOLEAN, DATE and DATETIME columns are never examined, and an
INTEGER column only for the digit-shaped kinds. That gate is exactly what prepare lacked when it
redacted a date column.

Whole cells and free text (DEC-095, ruling D5)
----------------------------------------------
:func:`detect_pii` asks "is this column made of personal data?", with `fullmatch` over a sample of
whole cells, and its answer decides whether a column is redacted or dropped before training.
:func:`free_text_pii` asks a different question - "does this free-text column *mention* personal
data?" - with `search`, and its answer never changes what is trained on (DEC-087): it raises the
`PII_IN_FREE_TEXT` warning and makes every surface that *shows* a cell of the column pass it through
:func:`redact_text` first. The two are separate functions because their thresholds cannot be
shared: the whole-cell rates are calibrated for whole cells, and one phone number in a thousand
complaints is still a phone number somebody typed.

Nothing here returns, stores or logs a matched value. Detectors return kinds; :func:`redact_text`
returns the text with each match replaced by a marker naming its kind.

Heavy libraries are imported inside function bodies only: `import engine` stays fast.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from engine.config import ColumnType

if TYPE_CHECKING:
    from collections.abc import Iterable

    import pandas as pd

__all__ = [
    "DETECTORS",
    "FREE_TEXT_MIN_WORDS",
    "NAME_MIN_DISTINCT_RATIO",
    "NAME_ONLY_DETECTORS",
    "PII_SAMPLE_VALUES",
    "REDACTION_MARKER_PATTERN",
    "TEXT_SCANNERS",
    "VALUE_DETECTORS",
    "PiiDetector",
    "detect_pii",
    "find_in_text",
    "free_text_pii",
    "is_free_text",
    "marker_for",
    "redact_cells",
    "redact_text",
]


PII_SAMPLE_VALUES: Final[int] = 1_000
"""Non-null values examined per column; the same budget for both questions."""

NAME_MIN_DISTINCT_RATIO: Final[float] = 0.40
"""How much of a sampled column must be distinct before its values alone may be read as names.

Well above any ordinary categorical column (a handful of levels over hundreds of rows) and well
below a real roster of people (near one distinct value per row, even when a few names repeat).
"""

FREE_TEXT_MIN_WORDS: Final[float] = 3.0
"""Mean whitespace-separated words per value above which a STRING column reads as free text.

TEXT columns (mean length above ingest's `TEXT_MEAN_LENGTH`) are free text by definition; a short
note - "call me on 0400 123 456" is six words and 23 characters - is not long enough to be TEXT, and
this is what still lets it be scanned. Three words is past every category level in the shipped
fixtures ("Month-to-month", "Fiber optic", "Electronic check") and every identifier, which is the
point: an id like `ACC-1234567` contains a phone-shaped run and must not be reported as prose.
"""


@dataclass(frozen=True)
class PiiDetector:
    """One PII shape: what its values look like, what its column is usually called, and how sure.

    `value_pattern` is `None` for a name-only detector: the header is the only evidence there can
    be, and a header that matches is enough on its own.

    `min_distinct_ratio` guards the *values alone* branch: the share of the sampled values that
    must be distinct before the value pattern may fire on its own. It stays 0 for a shape no
    ordinary column wears by accident (an e-mail address, an Aadhaar number) and rises above 0 for
    a shape that is also the shape of a perfectly innocent category level. It never touches the
    name-assisted branch, so a column whose *name* says it holds names is judged exactly as before.
    """

    kind: str
    value_pattern: re.Pattern[str] | None
    name_pattern: re.Pattern[str] | None
    min_value_match_rate: float
    name_assisted_rate: float = 0.20
    min_distinct_ratio: float = 0.0
    digit_shaped: bool = False


_EMAIL: Final[str] = r"[^\s@<>()\[\],;:\"]+@[^\s@<>()\[\],;:\"]+\.[A-Za-z]{2,}"
"""prepare's wider local part and domain (any non-space, non-delimiter character, so `o'brien@`
and an accented domain are caught), with the delimiters a sentence puts around an address
excluded so that searching prose takes the address and not the bracket before it."""

_PHONE: Final[str] = (
    # 1. country code or trunk digit, an optional area code (bracketed, or bare and followed by a
    #    separator), then two or three groups of three to five digits: `+1-555-555-0001`,
    #    `+91 98765 43210`, `0400 123 456`, `+44 20 7946 0958`, `555.555.0001`, `9876543210`.
    r"(?:(?:\+|00)?\d{1,3}[ \-.]?(?:\(\d{2,4}\)[ \-.]?|\d{2,4}[ \-.])?\d{3,5}(?:[ \-.]?\d{3,5}){1,2}"
    # 2. a bracketed area code first: `(02) 9876 5432`.
    r"|\(\d{2,4}\)[ \-.]?\d{3,5}(?:[ \-.]?\d{3,5}){1,2}"
    # 3. pairs of digits: `01 23 45 67 89`, `+33 6 12 34 56 78`.
    r"|(?:\+|00)?\d{1,3}(?:[ .\-]\d{1,2})?(?:[ .\-]\d{2}){4})"
)
"""Every shape either old pattern knew, and no date.

Each branch needs at least seven digits, so a short integer column cannot look like a phone, and
none can read `2011-09-10`: a four-two-two run has no group of three after its first separator and
no fourth pair. prepare's old `\\+?\\d[\\d\\s().-]{7,17}\\d` could, which is how a snapshot date was
redacted as a phone number on the library's online-retail file.
"""

VALUE_DETECTORS: Final[tuple[PiiDetector, ...]] = (
    PiiDetector(
        kind="email",
        value_pattern=re.compile(_EMAIL),
        name_pattern=re.compile(r"(?i)(^|_)(e?mail|email_address)($|_)"),
        min_value_match_rate=0.60,
    ),
    PiiDetector(
        kind="phone",
        value_pattern=re.compile(_PHONE),
        name_pattern=re.compile(r"(?i)(^|_)(phone|mobile|msisdn|contact_number|telephone)($|_)"),
        min_value_match_rate=0.80,
        digit_shaped=True,
    ),
    PiiDetector(
        kind="pan",
        value_pattern=re.compile(r"(?i)[A-Z]{5}\d{4}[A-Z]"),
        name_pattern=re.compile(r"(?i)(^|_)pan(_no|_number)?($|_)"),
        min_value_match_rate=0.60,
        digit_shaped=True,
    ),
    PiiDetector(
        kind="aadhaar",
        # UIDAI never issues a number starting 0 or 1; prepare's `\d{4}\s?\d{4}\s?\d{4}` accepted
        # one, and such a twelve-digit run is still caught - by the phone shape above.
        value_pattern=re.compile(r"[2-9]\d{3}[ \-]?\d{4}[ \-]?\d{4}"),
        name_pattern=re.compile(r"(?i)(^|_)aadhaa?r(_no|_number)?($|_)"),
        min_value_match_rate=0.80,
        digit_shaped=True,
    ),
    PiiDetector(
        kind="name",
        # The value pattern is "one to four capitalised words", which is what a personal name looks
        # like - and also what a great many category levels look like: `Female`/`Male`, `Yes`/`No`,
        # `Basic`/`Premium` all full-match at a 100 % rate. Redacting such a column would silently
        # drop a model input, and a column like `gender` is exactly the one a fairness report wants.
        # What really separates the two is vocabulary size: names are open-ended and near-unique,
        # a category is a small fixed set repeated over and over. So on values alone the detector
        # also demands an open vocabulary (`min_distinct_ratio`); a column whose name says `name`
        # still fires through the name-assisted branch however few distinct values it carries.
        value_pattern=re.compile(r"[A-Z][a-z]+(?:[ '\-][A-Z][a-z]+){0,3}"),
        name_pattern=re.compile(
            r"(?i)(^|_)(name|first_name|last_name|full_name|given_name|surname|"
            r"customer_name|account_name|contact_name|holder_name)($|_)"
        ),
        min_value_match_rate=0.90,
        min_distinct_ratio=NAME_MIN_DISTINCT_RATIO,
    ),
)
"""The detectors that can recognise a value. `engine.stages.ingest.PII_DETECTORS` is this tuple."""

NAME_ONLY_DETECTORS: Final[tuple[PiiDetector, ...]] = (
    PiiDetector(
        kind="address",
        value_pattern=None,
        name_pattern=re.compile(r"(?i)(^|_)(address|street|postcode|zipcode)($|_)"),
        min_value_match_rate=0.0,
    ),
    PiiDetector(
        kind="ssn",
        value_pattern=None,
        name_pattern=re.compile(r"(?i)(^|_)ssn($|_)"),
        min_value_match_rate=0.0,
    ),
    PiiDetector(
        kind="passport",
        value_pattern=None,
        name_pattern=re.compile(r"(?i)(^|_)passport(_no|_number)?($|_)"),
        min_value_match_rate=0.0,
    ),
)
"""The kinds only a header can reveal - prepare's `address|street|postcode|zipcode|ssn|passport`."""

DETECTORS: Final[tuple[PiiDetector, ...]] = VALUE_DETECTORS + NAME_ONLY_DETECTORS
"""Every detector, in the order `detect_pii` reports kinds."""

TEXT_SCANNERS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = tuple(
    (detector.kind, detector.value_pattern)
    for detector in VALUE_DETECTORS
    if detector.value_pattern is not None and detector.kind != "name"
)
"""The detectors worth running over free text, in their own order.

`name` is left out on purpose. Its pattern is "one to four capitalised words", which inside a
sentence matches the first word of every sentence, every product name and every place - it is
usable over a column, where an open vocabulary distinguishes a roster from a category, and it is
not usable over prose (DEC-213).
"""

_EXAMINED_TYPES: Final[frozenset[ColumnType]] = frozenset(
    {ColumnType.STRING, ColumnType.TEXT, ColumnType.INTEGER}
)
"""FLOAT, BOOLEAN, DATE and DATETIME columns are never examined for PII - not by value, not by name."""

_FREE_TEXT_TYPES: Final[frozenset[ColumnType]] = frozenset({ColumnType.STRING, ColumnType.TEXT})

_MARKER_PREFIX: Final[str] = "[REDACTED:"
_MARKER_SUFFIX: Final[str] = "]"

REDACTION_MARKER_PATTERN: Final[re.Pattern[str]] = re.compile(r"\[REDACTED:[a-z]+\]")
"""What a marker looks like, so a guardrail can tell a redaction from an identifier that got through."""


def _sample(series: pd.Series[Any]) -> list[str]:
    return [str(value).strip() for value in series.dropna().head(PII_SAMPLE_VALUES)]


def detect_pii(series: pd.Series[Any], name: str, inferred: ColumnType) -> tuple[str, ...]:
    """Detector kinds that fired, in `DETECTORS` order. Never returns, stores or logs a value.

    A value detector fires on its values alone (`min_value_match_rate` of whole cells, and an open
    enough vocabulary), or on a matching header plus `name_assisted_rate` of matching cells. A
    name-only detector fires on its header. Either way the column's type must be one the gate
    admits, and an INTEGER column is examined only for the digit-shaped kinds.
    """
    if inferred not in _EXAMINED_TYPES:
        return ()
    textual = inferred is not ColumnType.INTEGER
    sample = _sample(series)
    if not sample:
        return ()
    distinct_ratio = len(set(sample)) / len(sample)
    fired: list[str] = []
    for detector in DETECTORS:
        named = detector.name_pattern is not None and detector.name_pattern.search(name) is not None
        pattern = detector.value_pattern
        if pattern is None:
            if named:
                fired.append(detector.kind)
            continue
        if not textual and not detector.digit_shaped:
            continue
        matches = sum(1 for value in sample if pattern.fullmatch(value) is not None)
        rate = matches / len(sample)
        by_values = rate >= detector.min_value_match_rate and distinct_ratio >= detector.min_distinct_ratio
        by_name = named and rate >= detector.name_assisted_rate
        if by_values or by_name:
            fired.append(detector.kind)
    return tuple(fired)


def is_free_text(series: pd.Series[Any], inferred: ColumnType) -> bool:
    """TEXT, or STRING whose values average at least `FREE_TEXT_MIN_WORDS` words: prose, not codes."""
    if inferred is ColumnType.TEXT:
        return True
    if inferred is not ColumnType.STRING:
        return False
    sample = _sample(series)
    if not sample:
        return False
    return sum(len(value.split()) for value in sample) / len(sample) >= FREE_TEXT_MIN_WORDS


def find_in_text(text: str) -> tuple[str, ...]:
    """The detector kinds present anywhere in `text`, in `TEXT_SCANNERS` order, each at most once.

    Returns kinds and never values, so the result is safe to log, to put in a warning and to store
    in an artefact.
    """
    return tuple(kind for kind, pattern in TEXT_SCANNERS if pattern.search(text) is not None)


def free_text_pii(series: pd.Series[Any], inferred: ColumnType) -> tuple[str, ...]:
    """Kinds found *inside* the sampled values of a free-text column, in `TEXT_SCANNERS` order.

    One match anywhere in the sample is enough: a single phone number in a thousand complaints is
    still somebody's phone number, and the answer only ever raises a warning and hides the match
    from the screens - it never changes what is trained on (DEC-087).
    """
    if not is_free_text(series, inferred):
        return ()
    found: set[str] = set()
    for value in _sample(series):
        found.update(find_in_text(value))
        if len(found) == len(TEXT_SCANNERS):
            break
    return tuple(kind for kind, _ in TEXT_SCANNERS if kind in found)


def marker_for(kind: str) -> str:
    """What replaces a match of `kind`: `[REDACTED:email]` says what it hid, never what it was."""
    return f"{_MARKER_PREFIX}{kind}{_MARKER_SUFFIX}"


def redact_text(text: str) -> tuple[str, tuple[str, ...]]:
    """`text` with every match replaced by its marker, and the kinds that were **replaced**.

    Scanners run in `TEXT_SCANNERS` order, and an earlier one consumes the characters a later one
    would also have matched: the digits of a phone number also satisfy the Aadhaar pattern, so a
    phone number is removed as a phone number and the Aadhaar scanner then finds nothing left. The
    returned kinds are therefore what was taken out rather than everything `find_in_text` would
    report. Either way nothing recognisable survives, which is the property that matters.
    """
    redacted = text
    kinds: list[str] = []
    for kind, pattern in TEXT_SCANNERS:
        replaced, count = pattern.subn(marker_for(kind), redacted)
        if count:
            kinds.append(kind)
            redacted = replaced
    return redacted, tuple(kinds)


def redact_cells(values: Iterable[str]) -> tuple[str, ...]:
    """Each cell with its free-text PII replaced by markers; a cell with none comes back unchanged."""
    return tuple(redact_text(value)[0] for value in values)
