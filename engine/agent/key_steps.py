"""The `normalise_key` step (Plan O M237, DEC-1807): one customer ID written one way, so two files meet.

Several files name the same customer differently: `CUST-00123` in the customer list and `123` in the bills,
` c-77 ` against `C-77`, `98765 43210` against `+91 98765 43210`. A join on the IDs as written finds none of
them. `normalise_key` writes every ID one way, by a frozen rule:

* `spaces` - the spaces around the ID and inside it are removed (`" C 77 "` -> `"C77"`);
* `case` - the ID is read lower-case (`"C-77"` and `"c-77"` meet);
* `prefix` - the listed letters are taken off the start, when the ID starts with them (`"CUST-00123"` ->
  `"00123"`); an ID that does not start with them is kept as it is;
* `zeros` - the zeros at the start are dropped (`"00123"` -> `"123"`; `"000"` -> `"0"`);
* `phone` - an Indian phone number is written in E.164 with `+91` (`"098765 43210"`, `"9876543210"`,
  `"91-98765-43210"` -> `"+919876543210"`); a number already written with a `+` and 8 to 15 digits is kept as
  it is; anything else is **not converted**: kept as written and counted, never guessed.

The rule is the step's parameters and nothing else: no statistic of the file is read (DEC-1004), so the same
rule gives the same IDs next month and on the scoring file. It runs once per distinct value. There is no fuzzy
matching anywhere (Plan O §1.3): two IDs meet only when the rule writes them identically.

`KeyRule.identity()` changes nothing. `propose_key_rules` measures two key columns and proposes, for each
side, the smallest rule under which they meet; a proposal is only ever a pending suggestion (M237).
"""

from __future__ import annotations

import math
import re
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Final

import pandas as pd
from pydantic import Field, model_validator

from engine.agent.formats import ParseOutcome, masked_cut
from engine.config import StrictBase

__all__ = [
    "KEY_JOIN_OK",
    "KEY_PREFIX_SHARE",
    "MAX_PREFIX_CHARS",
    "PHONE_SHARE",
    "KeyRule",
    "KeyRuleProposal",
    "fit_rule",
    "key_coverage",
    "key_text",
    "normalise_key_value",
    "normalise_keys",
    "propose_key_rules",
    "rule_from_params",
]

MAX_PREFIX_CHARS: Final[int] = 20
"""A prefix is a few letters (`CUST-`, `ACC_`); a longer one is refused."""

KEY_JOIN_OK: Final[float] = 0.8
"""The share of one side's IDs that must meet the other's for a rule to be proposed: onboarding's own
`JOIN_COVERAGE_OK`, so Data Doctor and `KEY_FORMAT_MISMATCH` use one bar."""

KEY_PREFIX_SHARE: Final[float] = 0.9
"""Share of one side's IDs that must start with the same letters for them to be read as a prefix (DEC-1551)."""

PHONE_SHARE: Final[float] = 0.9
"""Share of both sides' IDs that must read as phone numbers before `phone` is proposed."""

_EXAMPLES: Final[int] = 5
_PREFIX: Final[re.Pattern[str]] = re.compile(r"^([A-Za-z]+[-_ #:/.]?)(?=\d)")
_SEPARATORS: Final[re.Pattern[str]] = re.compile(r"[\s\-().]")
_INNER_SPACE: Final[re.Pattern[str]] = re.compile(r"\s+")


class KeyRule(StrictBase):
    """How one side's IDs are written before they are compared: the `normalise_key` step's parameters."""

    prefix: str | None = Field(
        default=None,
        max_length=MAX_PREFIX_CHARS,
        description="Letters taken off the start of an ID that starts with them; null for none.",
    )
    zeros: bool = Field(default=False, description="Drop the zeros at the start of an ID.")
    case: bool = Field(default=False, description="Read the ID lower-case.")
    spaces: bool = Field(default=False, description="Remove the spaces around and inside the ID.")
    phone: bool = Field(
        default=False, description="Write an Indian phone number as +91 and ten digits (E.164)."
    )

    @model_validator(mode="after")
    def _phone_alone(self) -> KeyRule:
        if self.phone and (self.prefix is not None or self.zeros):
            raise ValueError("a phone number keeps its digits: phone cannot be combined with prefix or zeros")
        if self.prefix is not None and not self.prefix:
            raise ValueError("a prefix is at least one character")
        return self

    @classmethod
    def identity(cls) -> KeyRule:
        return cls()

    @property
    def changes_nothing(self) -> bool:
        return self == KeyRule.identity()

    def params(self) -> dict[str, Any]:
        """The step's parameters, only those that are set (so an unchanged rule hashes as before)."""
        out: dict[str, Any] = {}
        if self.prefix is not None:
            out["prefix"] = self.prefix
        for name in ("zeros", "case", "spaces", "phone"):
            if getattr(self, name):
                out[name] = True
        return out

    def words(self) -> str:
        """The rule in plain words, for a suggestion's title."""
        parts: list[str] = []
        if self.spaces:
            parts.append("remove the spaces")
        if self.case:
            parts.append("ignore upper and lower case")
        if self.prefix is not None:
            parts.append(f"remove {self.prefix!r} from the start")
        if self.zeros:
            parts.append("drop the zeros at the start")
        if self.phone:
            parts.append("write phone numbers as +91 and ten digits")
        return ", then ".join(parts) if parts else "leave the IDs as they are"


def key_text(value: Any) -> str | None:
    """An ID as text, the way a person reads it: `123.0` (a number column with a blank) is `"123"`; empty is
    None. Two IDs are compared as text, so `7` and `"7"` are one customer."""
    if value is None:
        return None
    if isinstance(value, float):
        if math.isnan(value):
            return None
        if value.is_integer():
            return str(int(value))
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, bool):
        return str(value)
    text = str(value)
    return text if text.strip() else None


def _phone(text: str) -> str | None:
    """`text` as `+91` and ten digits, or as written when it already is E.164; None when it is not a phone."""
    compact = _SEPARATORS.sub("", text)
    plus = compact.startswith("+")
    digits = compact[1:] if plus else compact
    if not digits.isdigit() or not digits.isascii():
        return None
    if plus:
        if digits.startswith("91") and len(digits) == 12:
            return "+" + digits
        return "+" + digits if 8 <= len(digits) <= 15 else None
    if digits.startswith("0091") and len(digits) == 14:
        return "+91" + digits[4:]
    if digits.startswith("91") and len(digits) == 12:
        return "+" + digits
    if digits.startswith("0") and len(digits) == 11:
        return "+91" + digits[1:]
    if len(digits) == 10:
        return "+91" + digits
    return None


def normalise_key_value(value: Any, rule: KeyRule) -> tuple[str | None, bool]:
    """`(the ID written by the rule, whether it converted)`. An empty cell is `(None, True)`; a value the
    phone rule cannot read is `(its text as written, False)` - kept, counted, never guessed."""
    text = key_text(value)
    if text is None:
        return None, True
    if rule.spaces:
        text = _INNER_SPACE.sub("", text.strip())
    if rule.case:
        text = text.casefold()
    if rule.phone:
        written = _phone(text)
        return (text, False) if written is None else (written, True)
    if rule.prefix is not None:
        prefix = rule.prefix.casefold() if rule.case else rule.prefix
        if text.startswith(prefix) and len(text) > len(prefix):
            text = text[len(prefix) :]
    if rule.zeros:
        text = text.lstrip("0") or ("0" if text else text)
    return text, True


def normalise_keys(series: pd.Series[Any], rule: KeyRule) -> ParseOutcome:
    """The column with every ID written by `rule`, once per distinct value; `changed` counts cells whose text
    changed, `failed` the non-empty cells the phone rule could not read (kept as written)."""
    cache: dict[Any, tuple[str | None, bool]] = {}
    values: list[Any] = []
    changed = 0
    failed: list[str] = []
    for value in series:
        try:
            hit = cache.get(value)
        except TypeError:  # an unhashable cell: computed every time
            hit = None
        if hit is None:
            hit = normalise_key_value(value, rule)
            with suppress(TypeError):
                cache[value] = hit
        written, ok = hit
        if written is None:
            values.append(None)
            continue
        values.append(written)
        if not ok:
            failed.append(written)
        elif written != key_text(value):
            changed += 1
    examples = tuple(masked_cut(value, 60) for value in list(dict.fromkeys(failed))[:_EXAMPLES])
    out = pd.Series(values, index=series.index, dtype=object)
    return ParseOutcome(values=out, changed=changed, failed=len(failed), failed_examples=examples)


# ---------------------------------------------------------------------------
# Measuring two key columns, and proposing the rules under which they meet
# ---------------------------------------------------------------------------
def _texts(keys: pd.Series[Any], rule: KeyRule) -> list[str]:
    """Every non-empty ID written by `rule`, one per row; computed once per distinct value."""
    present = keys.dropna()
    try:
        distinct = present.unique()
    except TypeError:  # unhashable cells: one at a time
        return [w for w, _ in (normalise_key_value(v, rule) for v in present) if w is not None]
    written = {value: normalise_key_value(value, rule)[0] for value in distinct}
    return [text for text in (written[value] for value in present) if text is not None]


def key_coverage(
    keys: pd.Series[Any],
    other: pd.Series[Any],
    rule: KeyRule | None = None,
    other_rule: KeyRule | None = None,
) -> float:
    """Share of `keys`' non-empty IDs found among `other`'s, each side written by its rule (as written when
    None). 1.0 when `keys` has none (nothing failed to meet)."""
    mine = _texts(keys, rule or KeyRule.identity())
    if not mine:
        return 1.0
    theirs = set(_texts(other, other_rule or KeyRule.identity()))
    return round(sum(1 for key in mine if key in theirs) / len(mine), 4)


def _common_prefix(keys: pd.Series[Any]) -> str | None:
    texts = pd.Series(_texts(keys, KeyRule.identity()), dtype=object)
    if texts.empty:
        return None
    found = texts.str.extract(_PREFIX, expand=False).dropna()
    if found.empty:
        return None
    counts = found.value_counts()
    prefix, count = str(counts.index[0]), int(counts.iloc[0])
    return prefix if count / len(texts) >= KEY_PREFIX_SHARE else None


def _phone_like(keys: pd.Series[Any]) -> bool:
    texts = _texts(keys, KeyRule.identity())
    if not texts:
        return False
    return sum(1 for text in texts if _phone(text) is not None) / len(texts) >= PHONE_SHARE


@dataclass(frozen=True)
class KeyRuleProposal:
    """The rules under which `keys` (the side measured) meet `base` (the side they are looked up in)."""

    base_rule: KeyRule
    rule: KeyRule
    coverage: float
    """Share of `keys` found in `base` as written."""
    fixed_coverage: float
    """The same share with both rules applied."""

    @property
    def needed(self) -> bool:
        return not (self.base_rule.changes_nothing and self.rule.changes_nothing)

    @property
    def meets(self) -> bool:
        return self.fixed_coverage >= KEY_JOIN_OK


def _candidates(keys: pd.Series[Any], base: pd.Series[Any]) -> list[tuple[KeyRule, KeyRule]]:
    """Rule pairs from the smallest change up; the first that clears the bar is proposed."""
    pairs: list[tuple[KeyRule, KeyRule]] = []
    simple = [
        KeyRule(spaces=True),
        KeyRule(case=True),
        KeyRule(zeros=True),
        KeyRule(spaces=True, case=True, zeros=True),
    ]
    pairs += [(rule, rule) for rule in simple]
    base_prefix, prefix = _common_prefix(base), _common_prefix(keys)
    if base_prefix is not None and base_prefix != prefix:
        pairs.append((KeyRule(prefix=base_prefix), KeyRule.identity()))
        pairs.append((KeyRule(prefix=base_prefix, zeros=True), KeyRule(zeros=True)))
        pairs.append(
            (
                KeyRule(prefix=base_prefix, zeros=True, case=True, spaces=True),
                KeyRule(zeros=True, case=True, spaces=True),
            )
        )
    if prefix is not None and prefix != base_prefix:
        pairs.append((KeyRule.identity(), KeyRule(prefix=prefix)))
        pairs.append((KeyRule(zeros=True), KeyRule(prefix=prefix, zeros=True)))
        pairs.append(
            (
                KeyRule(zeros=True, case=True, spaces=True),
                KeyRule(prefix=prefix, zeros=True, case=True, spaces=True),
            )
        )
    if _phone_like(keys) and _phone_like(base):
        pairs.append((KeyRule(phone=True), KeyRule(phone=True)))
    return pairs


def propose_key_rules(keys: pd.Series[Any], base: pd.Series[Any]) -> KeyRuleProposal:
    """How `keys` meet `base`: as written when they already do; otherwise the first rule pair (smallest change
    first) that clears `KEY_JOIN_OK`; otherwise the pair that got furthest, which `meets` says is not enough.
    Only exact equality after the rules counts: nothing is ever matched by being similar."""
    identity = KeyRule.identity()
    written = key_coverage(keys, base)
    if written >= KEY_JOIN_OK:
        return KeyRuleProposal(identity, identity, written, written)
    best = KeyRuleProposal(identity, identity, written, written)
    for base_rule, rule in _candidates(keys, base):
        fixed = key_coverage(keys, base, rule, base_rule)
        if fixed >= KEY_JOIN_OK:
            return KeyRuleProposal(base_rule, rule, written, fixed)
        if fixed > best.fixed_coverage:
            best = KeyRuleProposal(base_rule, rule, written, fixed)
    return best


def fit_rule(keys: pd.Series[Any], base: pd.Series[Any], base_rule: KeyRule) -> KeyRuleProposal:
    """How `keys` meet `base` once `base` is written by `base_rule` (chosen for another file): the first of
    `keys`' own proposal, the base's rule itself, the base's rule without its prefix, and as written, that
    clears `KEY_JOIN_OK`; else the best of them. Several files joined to one list share the list's rule."""
    own = propose_key_rules(keys, base)
    written = own.coverage
    candidates: list[KeyRule] = []
    if own.base_rule == base_rule:
        candidates.append(own.rule)
    candidates.append(base_rule)
    if base_rule.prefix is not None:
        candidates.append(base_rule.model_copy(update={"prefix": None}))
    candidates.append(KeyRule.identity())
    best = KeyRuleProposal(base_rule, KeyRule.identity(), written, 0.0)
    for rule in dict.fromkeys(candidates):
        fixed = key_coverage(keys, base, rule, base_rule)
        found = KeyRuleProposal(base_rule, rule, written, fixed)
        if found.meets:
            return found
        if fixed > best.fixed_coverage:
            best = found
    return best


def rule_from_params(params: dict[str, Any]) -> KeyRule:
    """The rule a `normalise_key` step's parameters spell; raises `ValueError` when they spell none."""
    return KeyRule.model_validate(params)
