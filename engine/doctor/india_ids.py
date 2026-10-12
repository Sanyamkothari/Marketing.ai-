"""Indian identifiers (Plan O M241, the design §5.5 G #63 and §8.1): GSTIN, voter ID, PIN code and mobile numbers.

`engine.pii` already knows PAN, Aadhaar and phone shapes. This module adds what Indian files carry besides:

* **GSTIN** - 15 characters: a two-digit state code, the holder's PAN, an entity digit, `Z`, and a check character.
  The check character is computed here (`gstin_check_char`), our own code: python-stdnum does it too, but it is
  LGPL ([RES] D3). A value of the right shape with the wrong check character is counted apart, never as valid.
* **EPIC** (the voter ID card number) - three letters and seven digits, `ABC1234567`. It has no check character.
* **PIN code** - six digits, the first 1-9, kept as **text**: a postcode is a place, not an amount, and must never
  be averaged or scaled as a number.
* **Mobile numbers** - ten digits starting 6-9, written with or without `+91`, `0091`, `91` or a trunk `0`,
  spaces, dashes, dots or brackets. `to_e164_india` writes each as `+91XXXXXXXXXX`, so two files that spell the
  same number differently join exactly on it (the design: "phone to `+91` E.164 for exact keys"). Nothing is
  matched fuzzily: a value that is not one of these spellings has no E.164 form.

`detect_india_ids` reads a frame and returns **counts only** - which columns hold which identifier, how many cells
match, how many fail the checksum - never a value. The egress gate masks GSTIN and EPIC wherever they appear in
text (`engine.agent.egress`, with the patterns below), so neither reaches a prompt.

Nothing here imports pandas at module import time beyond typing, and nothing imports `engine.agent`: the egress
gate imports the patterns from here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal

if TYPE_CHECKING:
    from collections.abc import Sequence

    import pandas as pd

__all__ = [
    "EPIC_TEXT",
    "GSTIN_STATE_CODES",
    "GSTIN_TEXT",
    "IdKind",
    "IndiaIdFinding",
    "detect_india_ids",
    "gstin_check_char",
    "is_epic",
    "is_pin",
    "is_valid_gstin",
    "looks_like_gstin",
    "to_e164_india",
]

IdKind = Literal["gstin", "epic", "pin", "phone_in"]

_ALPHABET: Final[str] = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

GSTIN_STATE_CODES: Final[frozenset[int]] = frozenset({*range(1, 39), 96, 97, 99})
"""01-38 for the states and union territories, 96 other country, 97 other territory, 99 centre jurisdiction."""

_GSTIN_SHAPE: Final[re.Pattern[str]] = re.compile(
    r"\d{2}(?:[A-Z]{5}\d{4}|[A-Z]{4}\d{5})[A-Z][1-9A-Z][0-9A-Z]{2}"
)
"""Two digits, a PAN (`AAPFU0939F`) or, for a tax deductor, a TAN (`DELA12345B`), the entity character, the 14th
character (`Z` for most; `D` and others for TDS and other registrations) and the check character."""
_EPIC_SHAPE: Final[re.Pattern[str]] = re.compile(r"[A-Z]{3}\d{7}")
_PIN_SHAPE: Final[re.Pattern[str]] = re.compile(r"[1-9]\d{5}")

GSTIN_TEXT: Final[re.Pattern[str]] = re.compile(
    r"(?<![A-Za-z0-9])\d{2}(?:[A-Za-z]{5}\d{4}|[A-Za-z]{4}\d{5})[A-Za-z][1-9A-Za-z][0-9A-Za-z]{2}(?![A-Za-z0-9])"
)
"""A GSTIN anywhere in text, any case, checksum or not: the egress gate masks the shape, valid or not."""
EPIC_TEXT: Final[re.Pattern[str]] = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{3}\d{7}(?![A-Za-z0-9])")
"""A voter ID anywhere in text, any case. Linear: fixed widths between word edges."""

_PHONE_PUNCTUATION: Final[re.Pattern[str]] = re.compile(r"[\s\-.()\[\]/]")
_MOBILE: Final[re.Pattern[str]] = re.compile(r"[6-9]\d{9}")

MIN_MATCH_SHARE: Final[float] = 0.80
"""A column is reported as holding an identifier when at least this share of its non-empty cells has the shape
(the rate `engine.pii` uses for phone numbers and Aadhaar)."""
SURE_SHARE: Final[float] = 0.95
"""At or above this share (and, for PIN codes, with a header that says so) the finding is `sure`; else `check`."""

_PIN_HEADER: Final[re.Pattern[str]] = re.compile(
    r"(?i)(^|[_\s])(pin|pincode|pin_code|postal|postcode|zip|zipcode)($|[_\s])"
)
_GSTIN_HEADER: Final[re.Pattern[str]] = re.compile(r"(?i)(^|[_\s])(gst|gstin|gst_no|gst_number)($|[_\s])")
_EPIC_HEADER: Final[re.Pattern[str]] = re.compile(r"(?i)(^|[_\s])(epic|voter|voter_id|voterid)($|[_\s])")
_PHONE_HEADER: Final[re.Pattern[str]] = re.compile(r"(?i)(^|[_\s])(phone|mobile|mob|msisdn|contact|whatsapp)")


def gstin_check_char(first_fourteen: str) -> str:
    """The check character of a GSTIN's first fourteen characters (base 36, weights 1 and 2 alternating)."""
    if len(first_fourteen) != 14:
        raise ValueError("a GSTIN's check character is computed from its first 14 characters")
    total = 0
    for index, ch in enumerate(first_fourteen.upper()):
        position = _ALPHABET.find(ch)
        if position < 0:
            raise ValueError("a GSTIN holds digits and letters only")
        product = position * (2 if index % 2 else 1)
        total += product // 36 + product % 36
    return _ALPHABET[(36 - total % 36) % 36]


def looks_like_gstin(text: str) -> bool:
    """Whether `text` has a GSTIN's shape and a known state code, whatever its check character."""
    value = text.strip().upper()
    return _GSTIN_SHAPE.fullmatch(value) is not None and int(value[:2]) in GSTIN_STATE_CODES


def is_valid_gstin(text: str) -> bool:
    """A GSTIN's shape, a known state code and the right check character."""
    value = text.strip().upper()
    return looks_like_gstin(value) and gstin_check_char(value[:14]) == value[14]


def is_epic(text: str) -> bool:
    """A voter ID in today's format: three letters and seven digits (no check character exists)."""
    return _EPIC_SHAPE.fullmatch(text.strip().upper()) is not None


def is_pin(text: str) -> bool:
    """A PIN code written as six digits, the first 1-9."""
    return _PIN_SHAPE.fullmatch(text.strip()) is not None


def to_e164_india(value: Any) -> str | None:
    """An Indian mobile number as `+91XXXXXXXXXX`, or None when `value` is not one.

    Accepted: ten digits starting 6-9, with `+91`, `0091`, `91` (twelve digits) or a trunk `0` (eleven digits) in
    front, and spaces, dashes, dots, slashes or brackets anywhere. A number read as a float (`9876543210.0`) is
    read by its value. Stateless: the answer depends on the value only.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float):
        if value != value or not value.is_integer():  # NaN, or not a whole number
            return None
        text = str(int(value))
    else:
        text = str(value).strip()
    digits = _PHONE_PUNCTUATION.sub("", text)
    if digits.startswith("+"):
        if not digits.startswith("+91"):
            return None
        digits = digits[3:]
    elif digits.startswith("0091"):
        digits = digits[4:]
    elif len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return f"+91{digits}" if _MOBILE.fullmatch(digits) else None


@dataclass(frozen=True)
class IndiaIdFinding:
    """One column that holds an Indian identifier. Counts only; never a value."""

    column: str
    kind: IdKind
    non_empty: int
    matching: int
    """Cells with the identifier's shape (GSTIN, EPIC, PIN) or an E.164 form (phone)."""
    invalid_checksum: int
    """GSTIN only: cells of the right shape whose check character is wrong. Counted in `matching`."""
    confidence: Literal["sure", "check"]
    personal: bool
    """Treated as personal data: masked before any prompt, hidden from examples (GSTIN, EPIC, phone)."""
    keep_as_text: bool
    """Read as text, never as a number (PIN codes; IDs with leading letters already are)."""
    join_key: bool
    """Phone only: every matching cell has one `+91` form, so the column can be an exact join key after
    normalising (M237's `normalise_key` writes it)."""

    @property
    def share(self) -> float:
        return self.matching / self.non_empty if self.non_empty else 0.0

    def as_json(self) -> dict[str, Any]:
        return {
            "column": self.column,
            "kind": self.kind,
            "non_empty": self.non_empty,
            "matching": self.matching,
            "share": round(self.share, 4),
            "invalid_checksum": self.invalid_checksum,
            "confidence": self.confidence,
            "personal": self.personal,
            "keep_as_text": self.keep_as_text,
            "join_key": self.join_key,
        }


def _cells(series: pd.Series[Any]) -> list[Any]:
    present = series.dropna()
    return [v for v in present.tolist() if not (isinstance(v, str) and not v.strip())]


def _as_text(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _finding(column: str, cells: Sequence[Any], *, textual: bool = True) -> IndiaIdFinding | None:
    """The identifier a column holds, if its cells say so; the most specific shape first.

    `textual` is False for a column pandas reads as numbers: there six-digit values are far more often amounts
    (rupees between 1 and 10 lakh) than postcodes, so such a column is a PIN only when its header says so.
    """
    total = len(cells)
    if not total:
        return None
    texts = [_as_text(v) for v in cells]

    def confidence(share: float, header: bool) -> Literal["sure", "check"]:
        return "sure" if share >= SURE_SHARE and header else "check"

    gstin = sum(1 for t in texts if looks_like_gstin(t))
    if gstin / total >= MIN_MATCH_SHARE:
        invalid = sum(1 for t in texts if looks_like_gstin(t) and not is_valid_gstin(t))
        sure = gstin / total >= SURE_SHARE and not invalid
        return IndiaIdFinding(
            column, "gstin", total, gstin, invalid, "sure" if sure else "check", True, True, False
        )
    epic = sum(1 for t in texts if is_epic(t))
    if epic / total >= MIN_MATCH_SHARE:
        header = _EPIC_HEADER.search(column) is not None
        # Three letters and seven digits is also how some firms number customers: the header decides `sure`.
        return IndiaIdFinding(
            column, "epic", total, epic, 0, confidence(epic / total, header), True, True, False
        )
    phones = [to_e164_india(v) for v in cells]
    phone = sum(1 for p in phones if p is not None)
    if phone / total >= MIN_MATCH_SHARE:
        header = _PHONE_HEADER.search(column) is not None
        return IndiaIdFinding(
            column,
            "phone_in",
            total,
            phone,
            0,
            confidence(phone / total, header),
            True,
            True,
            phone == total,
        )
    pin = sum(1 for t in texts if is_pin(t))
    pin_header = _PIN_HEADER.search(column) is not None
    if pin / total >= MIN_MATCH_SHARE and (textual or pin_header):
        header = pin_header
        # Six-digit numbers are also amounts; without a header that says postcode it is a question.
        return IndiaIdFinding(
            column, "pin", total, pin, 0, confidence(pin / total, header), False, True, False
        )
    if _GSTIN_HEADER.search(column) is not None and gstin:
        invalid = sum(1 for t in texts if looks_like_gstin(t) and not is_valid_gstin(t))
        return IndiaIdFinding(column, "gstin", total, gstin, invalid, "check", True, True, False)
    return None


def detect_india_ids(
    frame: pd.DataFrame, *, columns: Sequence[str] | None = None
) -> tuple[IndiaIdFinding, ...]:
    """Every column of `frame` holding a GSTIN, a voter ID, a mobile number or a PIN code; counts only.

    Columns pandas reads as dates or booleans are never examined; a float column only when every value is whole
    (a phone or a PIN read as a number because one cell was empty).
    """
    import pandas as pd

    names = [str(c) for c in frame.columns] if columns is None else [c for c in columns if c in frame.columns]
    found: list[IndiaIdFinding] = []
    for name in names:
        series = frame[name]
        if pd.api.types.is_bool_dtype(series) or pd.api.types.is_datetime64_any_dtype(series):
            continue
        cells = _cells(series)
        if pd.api.types.is_float_dtype(series) and not all(float(v).is_integer() for v in cells):
            continue
        textual = bool(pd.api.types.is_object_dtype(series) or pd.api.types.is_string_dtype(series))
        finding = _finding(name, cells, textual=textual)
        if finding is not None:
            found.append(finding)
    return tuple(found)
