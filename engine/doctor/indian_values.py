"""Indian numbers and Hindi / Hinglish yes-no words (Plan O M241, the design §5.5 G #61-#62 and §8.1).

Two rules-only proposers. Each reads a frame and returns, per column, a **pending** recipe step built from the fixed
step vocabulary - never code - with counts, masked examples and a confidence. Nothing is applied here: the step goes
through the case's proposal, policy and approval like every other (the design §5.1: APPLY is never a tool).

* `propose_indian_numbers` (#61) - a text column whose values read as numbers only with the Indian number words
  (`₹2.5 L`, `1.2 Cr`, `Rs 3,50,000/-`, `12K`) gets `parse_number` with `indian_scale_words: true`. It is `sure`
  only when every value reads; a column where some letters are not a listed unit (`5 kg`, `12 GB`) is `check`, and
  so is one whose only words are a bare `L`, `K` or `M` with nothing saying it is money (`30m` may be minutes).
* `propose_indian_booleans` (#62) - a yes/no column written in Hindi or Hinglish (`haan` / `nahi`, `हाँ` / `नहीं`)
  gets `map_boolean` with the spellings it holds. It is proposed, `sure`, **only when both sides are seen**: a column
  holding only `haan` and blanks says nothing about what a no looks like, so it is a `check` finding with no step.
  `na` is never mapped: it may be Hinglish for no or "not available", so a column holding it is a question.

Neither touches the ID or the outcome column (Plan L §5.2.3 and §5.2.6): both are skipped, whatever they hold.
The `map_boolean` and `parse_number` runners are Plan G's (`engine.agent.formats`), so replay at scoring is the same
code with the same frozen parameters.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal

import pandas as pd

from engine.agent.contracts import RecipeStep, RecipeStepKind
from engine.agent.formats import (
    BOOLEAN_FALSE,
    BOOLEAN_TRUE,
    CURRENCY_TOKENS,
    INDIAN_SCALE_WORDS,
    MIN_CONVERT_SHARE,
    decimal_evidence,
    masked_cut,
    parse_numbers,
)

if TYPE_CHECKING:
    from engine.doctor.locale import LocaleProfile

__all__ = [
    "AMBIGUOUS_NO",
    "DEVANAGARI_FALSE",
    "DEVANAGARI_TRUE",
    "HINGLISH_FALSE",
    "HINGLISH_TRUE",
    "LocaleStepProposal",
    "bare_letters",
    "boolean_word_family",
    "money_evidence",
    "propose_indian_booleans",
    "propose_indian_numbers",
]

HINGLISH_TRUE: Final[frozenset[str]] = frozenset(
    {
        "haan",
        "haa",
        "han",
        "ha",
        "haanji",
        "haan ji",
        "hanji",
        "han ji",
        "ji haan",
        "ji han",
        "jee haan",
        "ji ha",
    }
)
HINGLISH_FALSE: Final[frozenset[str]] = frozenset(
    {"nahi", "nahin", "nahee", "nahi ji", "nahin ji", "ji nahi", "ji nahin", "jee nahi", "nai", "naa"}
)
DEVANAGARI_TRUE: Final[frozenset[str]] = frozenset(
    {"हाँ", "हां", "हा", "जी हाँ", "जी हां", "हाँ जी", "हां जी"}
)
DEVANAGARI_FALSE: Final[frozenset[str]] = frozenset({"नहीं", "नही", "ना", "जी नहीं", "जी नही", "नहीं जी"})
AMBIGUOUS_NO: Final[frozenset[str]] = frozenset({"na"})
"""`na` is Hinglish for no, and English files write `NA` for "not available". It is never mapped by the rules, not
even beside a Hindi yes: a column holding it is a question ("does NA mean no, or not available?"), never `sure`."""

MAX_BOOLEAN_SPELLINGS: Final[int] = 20
"""A yes/no column has a handful of spellings; above this many distinct values it is not one."""
MAX_EXAMPLES: Final[int] = 5

_UNIT_HEADER: Final[re.Pattern[str]] = re.compile(
    r"(?i)(litre|liter|volume|weight|_kg|_gb|_mb|data|usage|minute|mins|metre|meter|distance|height)"
)
"""A header that names a physical unit: there `L` may be litres and `M` metres, so the proposal is `check`."""
_LETTERS: Final[re.Pattern[str]] = re.compile(r"[^\W\d_]+")
_BARE_LETTERS: Final[dict[str, str]] = {
    "l": "litres",
    "m": "minutes, metres or months",
    "k": "kilograms or kilometres",
}
"""The one-letter number words, and what else the letter means after a number: `30m`, `2L`, `5k` are not always money."""
_UNAMBIGUOUS_WORDS: Final[frozenset[str]] = frozenset(
    {"lac", "lacs", "lakh", "lakhs", "cr", "crore", "crores"}
)
_MONEY_HEADER: Final[re.Pattern[str]] = re.compile(
    r"(?i)(amount|amt|price|revenue|sales|spend|cost|value|salary|income|budget|ctc|fee|bill|balance|turnover)"
)
"""A header that names money: there a bare `L`, `K` or `M` after a number is lakh, thousand or million."""
_CURRENCY_TEXT: Final[re.Pattern[str]] = re.compile(
    "|".join(re.escape(token) for token in CURRENCY_TOKENS), flags=re.IGNORECASE
)
_TRAILING_RUPEE_DASH: Final[re.Pattern[str]] = re.compile(r"\d\s*/-\s*$")
_CURRENCY_WORDS: Final[frozenset[str]] = frozenset(
    t.casefold().rstrip(".") for t in CURRENCY_TOKENS if t.isalpha() or t.rstrip(".").isalpha()
)
_LAKH_CRORE: Final[frozenset[str]] = frozenset(
    w for w, scale in INDIAN_SCALE_WORDS.items() if scale in {10**5, 10**7}
)


def _key(text: str) -> str:
    """A spelling as the lexicons compare it: NFC, inner spaces collapsed, stripped, casefolded."""
    return " ".join(unicodedata.normalize("NFC", text).split()).casefold()


def boolean_word_family(spelling: str) -> Literal["english", "hinglish", "devanagari"] | None:
    """Which lexicon a yes/no spelling belongs to; None when it is none of them (or the ambiguous `na`)."""
    key = _key(spelling)
    if key in BOOLEAN_TRUE or key in BOOLEAN_FALSE:
        return "english"
    if key in HINGLISH_TRUE or key in HINGLISH_FALSE:
        return "hinglish"
    if key in DEVANAGARI_TRUE or key in DEVANAGARI_FALSE:
        return "devanagari"
    return None


@dataclass(frozen=True)
class LocaleStepProposal:
    """One column's pending fix (or a finding with no fix), counts and masked examples only."""

    tool: Literal["parse_indian_numbers", "indian_booleans"]
    column: str
    step: RecipeStep | None
    """The step a person may approve; None when nothing can be proposed safely (one side only, unknown words)."""
    confidence: Literal["sure", "check"]
    non_empty: int
    convertible: int
    failed: int
    reason: str
    """Plain words, counts only - never a cell value."""
    examples: tuple[str, ...] = ()
    """Masked cells that need the new reading (`masked_cut`); shown on the screen, never sent unmasked."""
    unknown_words: int = 0
    """Cells holding a word that is not a listed unit (numbers) or not a known yes/no spelling (booleans)."""

    def as_json(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "column": self.column,
            "step": None if self.step is None else self.step.model_dump(mode="json"),
            "confidence": self.confidence,
            "non_empty": self.non_empty,
            "convertible": self.convertible,
            "failed": self.failed,
            "reason": self.reason,
            "examples": list(self.examples),
            "unknown_words": self.unknown_words,
        }


def _text_columns(
    frame: pd.DataFrame, columns: Sequence[str] | None, primary_key: str | None, target: str | None
) -> list[str]:
    names = [str(c) for c in frame.columns] if columns is None else [c for c in columns if c in frame.columns]
    protected = {name for name in (primary_key, target) if name}
    return [
        name
        for name in names
        if name not in protected
        and (pd.api.types.is_object_dtype(frame[name]) or pd.api.types.is_string_dtype(frame[name]))
    ]


def _present_text(series: pd.Series[Any]) -> pd.Series[Any]:
    present = series.dropna()
    present = present[[isinstance(v, str) for v in present.tolist()]]
    return present[present.str.strip() != ""]


def _unlisted_words(text: str) -> bool:
    """Whether `text` holds a word that is neither a listed number word nor a currency word."""
    for word in _LETTERS.findall(text):
        folded = word.casefold()
        if folded not in INDIAN_SCALE_WORDS and folded not in _CURRENCY_WORDS:
            return True
    return False


def money_evidence(header: str, cells: Sequence[str]) -> bool:
    """Whether a column is money, so a bare `L`, `K` or `M` after a number may be read as lakh, thousand, million.

    Evidence is a money header (`amount`, `price`, `income`, `ctc`, ...), a currency sign or code in a value
    (`₹`, `Rs`, `INR`, `$`), or a word only money uses in the same column (`lakh`, `lac`, `crore`, `Cr`, a trailing
    `/-`). Without it `30m` may be minutes and `2L` litres (Plan O M241 review item 3).
    """
    if _MONEY_HEADER.search(header):
        return True
    for text in dict.fromkeys(cells):
        if _CURRENCY_TEXT.search(text) or _TRAILING_RUPEE_DASH.search(text):
            return True
        if any(word.casefold() in _UNAMBIGUOUS_WORDS for word in _LETTERS.findall(text)):
            return True
    return False


def bare_letters(cells: Sequence[str]) -> tuple[str, ...]:
    """The one-letter number words (`l`, `k`, `m`) written in `cells`, casefolded, sorted."""
    return tuple(
        sorted(
            {word.casefold() for text in dict.fromkeys(cells) for word in _LETTERS.findall(text)}
            & set(_BARE_LETTERS)
        )
    )


# ---------------------------------------------------------------------------
# #61 parse_indian_numbers
# ---------------------------------------------------------------------------
def propose_indian_numbers(
    frame: pd.DataFrame,
    *,
    columns: Sequence[str] | None = None,
    primary_key: str | None = None,
    target: str | None = None,
    profile: LocaleProfile | None = None,
) -> tuple[LocaleStepProposal, ...]:
    """A `parse_number` step with `indian_scale_words` for each text column that needs the Indian number words.

    A column is proposed when the words read more of its values than the plain parser does and, with them, at least
    `MIN_CONVERT_SHARE` of its values read. A profile whose number words are `none` proposes nothing; one whose
    words are `k_m` proposes a column that uses lakh or crore as `check` only.
    """
    words = None if profile is None else profile.number_words.value
    if words == "none":
        return ()
    found: list[LocaleStepProposal] = []
    for name in _text_columns(frame, columns, primary_key, target):
        cells = _present_text(frame[name])
        if cells.empty:
            continue
        point, comma = decimal_evidence(cells)
        decimal = "," if comma > point else "."
        plain = parse_numbers(cells, decimal=decimal)
        indian = parse_numbers(cells, decimal=decimal, indian_scale_words=True)
        if indian.changed <= plain.changed or indian.changed / len(cells) < MIN_CONVERT_SHARE:
            continue
        needs = cells[indian.values.notna().to_numpy() & plain.values.isna().to_numpy()]
        failed_cells = cells[indian.values.isna().to_numpy()]
        unknown = int(sum(1 for text in failed_cells if _unlisted_words(text)))
        uses_lakh = any(
            word.casefold() in _LAKH_CRORE for text in dict.fromkeys(needs) for word in _LETTERS.findall(text)
        )
        letters = bare_letters(needs.tolist())
        unit_letters = bool(letters) and not money_evidence(name, cells.tolist())
        sure = (
            indian.failed == 0
            and _UNIT_HEADER.search(name) is None
            and not (words == "k_m" and uses_lakh)
            and not unit_letters
        )
        reason = (
            f"{len(needs)} of {len(cells)} values are written with lakh, crore, K or M, or end in /-; "
            f"read with them, {indian.changed} of {len(cells)} are numbers"
        )
        if unknown:
            reason += f"; {unknown} hold a word that is not a listed number word, so please check"
        if unit_letters:
            meanings = "; ".join(f"{letter.upper()} may be {_BARE_LETTERS[letter]}" for letter in letters)
            reason += f"; nothing says this column is money ({meanings}), so please check"
        examples = tuple(masked_cut(text, 80) for text in list(dict.fromkeys(needs))[:MAX_EXAMPLES])
        step = RecipeStep(
            order=1,
            kind=RecipeStepKind.PARSE_NUMBER,
            column=name,
            params={"decimal": decimal, "percent_to_fraction": True, "indian_scale_words": True},
            reason="Read Indian number words (lakh, crore, K, M) and a trailing /- as numbers.",
        )
        found.append(
            LocaleStepProposal(
                tool="parse_indian_numbers",
                column=name,
                step=step,
                confidence="sure" if sure else "check",
                non_empty=len(cells),
                convertible=indian.changed,
                failed=indian.failed,
                reason=reason,
                examples=examples,
                unknown_words=unknown,
            )
        )
    return tuple(found)


# ---------------------------------------------------------------------------
# #62 indian_booleans
# ---------------------------------------------------------------------------
def _side(key: str) -> Literal["yes", "no"] | None:
    if key in BOOLEAN_TRUE or key in HINGLISH_TRUE or key in DEVANAGARI_TRUE:
        return "yes"
    if key in BOOLEAN_FALSE or key in HINGLISH_FALSE or key in DEVANAGARI_FALSE:
        return "no"
    return None


def propose_indian_booleans(
    frame: pd.DataFrame,
    *,
    columns: Sequence[str] | None = None,
    primary_key: str | None = None,
    target: str | None = None,
    profile: LocaleProfile | None = None,
) -> tuple[LocaleStepProposal, ...]:
    """A `map_boolean` step for each text column whose yes/no is written in Hindi or Hinglish, both sides seen.

    A column written only in English is Plan G's (`find_format_issues`) and is not reported here. A profile whose
    boolean words are English only proposes nothing.
    """
    if profile is not None and not {"hinglish", "devanagari"} & set(profile.boolean_words.value):
        return ()
    found: list[LocaleStepProposal] = []
    for name in _text_columns(frame, columns, primary_key, target):
        cells = _present_text(frame[name])
        if cells.empty:
            continue
        spellings = Counter(text.strip() for text in cells)
        if len(spellings) > MAX_BOOLEAN_SPELLINGS:
            continue
        keys = {spelling: _key(spelling) for spelling in spellings}
        families = {boolean_word_family(s) for s in spellings} - {None}
        if not families & {"hinglish", "devanagari"}:
            continue
        sides = {spelling: _side(key) for spelling, key in keys.items()}
        ambiguous = sum(spellings[s] for s, key in keys.items() if key in AMBIGUOUS_NO)
        unknown = sum(spellings[s] for s, side in sides.items() if side is None) - ambiguous
        if unknown and unknown / len(cells) > 0.5:
            continue  # mostly something else: a text column that happens to hold a "haan"
        true_values = sorted(s for s, side in sides.items() if side == "yes")
        false_values = sorted(s for s, side in sides.items() if side == "no")
        both = bool(true_values) and bool(false_values)
        mapped = len(cells) - unknown - ambiguous
        shown = [masked_cut(s, 40) for s, _ in spellings.most_common(MAX_EXAMPLES)]
        na_note = (
            f"{ambiguous} of {len(cells)} values are written na (any case), which may mean no or 'not available'"
            if ambiguous
            else ""
        )
        if unknown:
            reason = (
                f"{unknown} of {len(cells)} values are not a known yes or no spelling, so the column is not read "
                "as yes/no until you say what they mean"
            )
            if na_note:
                reason += f"; {na_note}"
            step = None
        elif ambiguous:
            # `na` is never the evidence for no, and never mapped beside another no either: always a question.
            reason = f"{na_note}, so the column is not read as yes/no until you say which"
            step = None
        elif not both:
            only = "yes" if true_values else "no"
            reason = (
                f"Only {only} is written ({mapped} values); a yes/no column is read only when both yes and no are "
                "seen"
            )
            step = None
        else:
            reason = (
                f"{len(true_values)} spellings of yes and {len(false_values)} of no, in Hindi or Hinglish; "
                f"all {len(cells)} values read"
            )
            step = RecipeStep(
                order=1,
                kind=RecipeStepKind.MAP_BOOLEAN,
                column=name,
                params={"true_values": list(true_values), "false_values": list(false_values)},
                reason="Read Hindi and Hinglish yes and no (haan, nahi, हाँ, नहीं) as yes and no.",
            )
        found.append(
            LocaleStepProposal(
                tool="indian_booleans",
                column=name,
                step=step,
                confidence="sure" if step is not None else "check",
                non_empty=len(cells),
                convertible=mapped if step is not None else 0,
                failed=unknown + ambiguous,
                reason=reason,
                examples=tuple(shown),
                unknown_words=unknown + ambiguous,
            )
        )
    return tuple(found)
