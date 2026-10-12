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
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final, TypeAlias

import numpy as np
import numpy.typing as npt
import pandas as pd
from pydantic import Field, model_validator

from engine.agent.formats import ParseOutcome, masked_cut
from engine.config import StrictBase
from engine.doctor.india_ids import to_e164_india

__all__ = [
    "KEY_JOIN_OK",
    "KEY_PREFIX_SHARE",
    "MAX_PREFIX_CHARS",
    "PHONE_SHARE",
    "KeyColumn",
    "KeyRule",
    "KeyRuleProposal",
    "KeysLike",
    "factorize_exact",
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
    """`text` as `+91` and ten digits, or as written when it already is E.164; None when it is not a phone.

    An Indian mobile number is written by M241's `engine.doctor.india_ids.to_e164_india`, the one Indian phone
    rule (Plan O wave 1 integration, DEC-1807 (q)); what it does not read - a number already in another
    country's E.164, a ten-digit landline with its area code - keeps M237's reading below.
    """
    indian = to_e164_india(text)
    if indian is not None and indian.isascii():
        return indian
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


# ---------------------------------------------------------------------------
# One key column, read once (M237 review: linear, and quick at a million rows)
# ---------------------------------------------------------------------------
_FAST_WIDTH: Final[int] = 64
"""IDs up to this many characters are written with numpy's text arrays (C loops); a column holding a longer
one is written value by value (the same rule, the same result, only slower)."""

_WS_CODES: Final[tuple[tuple[int, int], ...]] = ((9, 13), (28, 32))
"""The ASCII characters Python's `\\s` and `str.strip` read as space: tab to carriage return, 28 to 32."""

_SEP_CODES: Final[tuple[int, ...]] = tuple(ord(ch) for ch in "-_ #:/.")

Op = tuple[str, Any]
"""One operation of a rule, in the order a rule applies them: spaces, case, then phone or prefix and zeros."""


def _ops(rule: KeyRule) -> tuple[Op, ...]:
    ops: list[Op] = []
    if rule.spaces:
        ops.append(("spaces", None))
    if rule.case:
        ops.append(("case", None))
    if rule.phone:
        ops.append(("phone", None))
        return tuple(ops)
    if rule.prefix is not None:
        ops.append(("prefix", rule.prefix.casefold() if rule.case else rule.prefix))
    if rule.zeros:
        ops.append(("zeros", None))
    return tuple(ops)


def _nul_in(values: npt.NDArray[np.object_]) -> bool:
    """Whether a text holds a NUL: pandas' text hash table reads only up to it, so `"a\x00"` would be `"a"`."""
    return "\x00" in "".join(value for value in values if type(value) is str)


def factorize_exact(values: npt.NDArray[np.object_]) -> tuple[npt.NDArray[np.int64], npt.NDArray[np.object_]]:
    """`pd.factorize` (codes in order of first appearance, -1 for a missing value) that never merges two texts
    differing after a NUL character."""
    if not _nul_in(values):
        codes, uniques = pd.factorize(values, use_na_sentinel=True)
        return np.asarray(codes, dtype=np.int64), np.asarray(uniques, dtype=object)
    seen: dict[Any, int] = {}
    out = np.empty(len(values), dtype=np.int64)
    for position, value in enumerate(values):
        if value is None or (not isinstance(value, str) and bool(pd.isna(value))):
            out[position] = -1
            continue
        out[position] = seen.setdefault(value, len(seen))
    return out, np.array(list(seen), dtype=object)


def _hashable(value: Any) -> Any:
    try:
        hash(value)
    except TypeError:
        return repr(value)
    return value


def _unique_texts(uniques: Any) -> npt.NDArray[np.object_]:
    """`key_text` of each distinct value, vectorised where the column's type allows it."""
    values = pd.Series(uniques)
    kind = values.dtype.kind
    if kind == "f":
        numbers = values.to_numpy(dtype=float)
        out = values.astype(str).to_numpy(dtype=object)
        finite = np.isfinite(numbers)
        whole = finite & (np.where(finite, numbers, 0.0) % 1 == 0)
        small = whole & (np.abs(numbers) < 2**62)
        out[small] = numbers[small].astype(np.int64).astype(str)
        for position in np.flatnonzero(whole & ~small):  # beyond int64: Python's exact integer
            out[position] = str(int(numbers[position]))
        return out
    if kind in "iub":
        return values.astype(str).to_numpy(dtype=object)
    if kind == "O":
        raw = values.to_numpy(dtype=object)
        if pd.api.types.infer_dtype(raw, skipna=False) == "string":
            fixed = _as_fixed(raw)
            if fixed is not None:  # blank: every character a space (ASCII in C; the rest by Python)
                codes = _codes(fixed)
                wide = (codes >= 128).any(axis=1)
                blank = (_between(codes, _WS_CODES) | (codes == 0)).all(axis=1) & ~wide
                blank[wide] = [not text.strip() for text in raw[wide]]
            else:
                blank = np.array([not text.strip() for text in raw], dtype=bool)
            out = raw.copy()
            out[blank] = None
            return out
        return np.array([key_text(value) for value in raw], dtype=object)
    return np.array([key_text(value) for value in values], dtype=object)


def _as_fixed(texts: npt.NDArray[np.object_]) -> npt.NDArray[np.str_] | None:
    """`texts` as a fixed-width numpy text array, when that holds them exactly: none longer than `_FAST_WIDTH`
    and none holding a NUL (numpy drops a trailing one)."""
    if not len(texts):
        return None
    lengths = np.fromiter((len(text) for text in texts), dtype=np.int64, count=len(texts))
    if int(lengths.max()) > _FAST_WIDTH:
        return None
    fixed = texts.astype(str)
    if not np.array_equal(np.strings.str_len(fixed), lengths):
        return None
    return fixed


def _codes(fixed: npt.NDArray[np.str_]) -> npt.NDArray[np.uint32]:
    """Each text's characters as code points, one row per text (zero past its end)."""
    width = max(fixed.dtype.itemsize // 4, 1)
    return fixed.view(np.uint32).reshape(len(fixed), width)


def _between(codes: npt.NDArray[np.uint32], ranges: tuple[tuple[int, int], ...]) -> npt.NDArray[np.bool_]:
    hit = np.zeros(codes.shape, dtype=bool)
    for low, high in ranges:
        hit |= (codes >= low) & (codes <= high)
    return hit


def _any_char(hit: npt.NDArray[np.bool_]) -> npt.NDArray[np.bool_]:
    """Per text: whether any of its characters is hit."""
    return np.asarray(hit.any(axis=1), dtype=np.bool_)


def _python_op(values: Sequence[str], op: Op) -> list[str]:
    name, arg = op
    if name == "spaces":
        return [_INNER_SPACE.sub("", text.strip()) for text in values]
    if name == "case":
        return [text.casefold() for text in values]
    if name == "prefix":
        return [
            text[len(arg) :] if text.startswith(arg) and len(text) > len(arg) else text for text in values
        ]
    return [text.lstrip("0") or "0" for text in values]


def _assign(
    fixed: npt.NDArray[np.str_], rows: npt.NDArray[np.bool_], values: list[str]
) -> npt.NDArray[np.str_]:
    """`fixed` with `rows` replaced by `values`, widened when one of them is longer."""
    longest = max((len(value) for value in values), default=0)
    if longest > fixed.dtype.itemsize // 4:
        fixed = fixed.astype(f"<U{longest}")
    fixed[rows] = values
    return fixed


def _fixed_op(fixed: npt.NDArray[np.str_], op: Op) -> npt.NDArray[np.str_]:
    """One operation on a fixed-width array: masks in C, and Python only for the texts the mask cannot clear.
    When it changes no text, `fixed` itself is returned, so a rule that changes nothing costs nothing after.
    """
    name, arg = op
    codes = _codes(fixed)
    wide = _any_char(codes >= 128)
    if name == "spaces":
        rows = _any_char(_between(codes, _WS_CODES)) | wide
        return _assign(fixed.copy(), rows, _python_op(list(fixed[rows]), op)) if rows.any() else fixed
    if name == "case":
        ascii_upper = _any_char(_between(codes, ((65, 90),))) & ~wide
        if not ascii_upper.any() and not wide.any():
            return fixed
        out = fixed.copy()
        if ascii_upper.any():
            out[ascii_upper] = np.strings.lower(fixed[ascii_upper])  # ASCII: lower-case is the case fold
        if wide.any():
            out = _assign(out, wide, _python_op(list(fixed[wide]), op))
        return out
    if name == "prefix":
        rows = np.strings.startswith(fixed, arg) & (np.strings.str_len(fixed) > len(arg))
        if not rows.any():
            return fixed
        out = fixed.copy()
        out[rows] = np.strings.slice(fixed[rows], len(arg), None)
        return out
    rows = codes[:, 0] == ord("0")  # zeros
    if not rows.any():
        return fixed
    out = fixed.copy()
    stripped = np.strings.lstrip(fixed[rows], "0")
    stripped[np.strings.str_len(stripped) == 0] = "0"
    out[rows] = stripped
    return out


class KeyColumn:
    """One key column read once: its distinct IDs as text (in order of first appearance), the rows of each, and
    each rule's writing of them, cached step by step (a rule's spaces-then-case is reused by every rule that
    starts the same way). Every measure works on the distinct IDs weighed by their rows, so a column is read once
    however many rules are tried on it."""

    __slots__ = (
        "_applied",
        "_as_text",
        "_fixed",
        "_found",
        "_index",
        "_prefix",
        "_stages",
        "_written",
        "codes",
        "counts",
        "rows",
        "texts",
    )

    def __init__(self, series: pd.Series[Any]) -> None:
        if series.dtype == object:
            values = series.to_numpy(dtype=object)
            try:
                codes, uniques = factorize_exact(values)
            except TypeError:  # unhashable cells: read through their text
                codes, uniques = factorize_exact(
                    series.map(_hashable, na_action="ignore").to_numpy(dtype=object)
                )
            texts = _unique_texts(uniques)
        else:
            typed_codes, typed_uniques = pd.factorize(series, use_na_sentinel=True)
            codes, texts = np.asarray(typed_codes, dtype=np.int64), _unique_texts(typed_uniques)
        keep = np.array([text is not None for text in texts], dtype=bool)
        if not keep.all():  # an empty cell names nobody: read like a missing one
            recode = np.full(len(texts), -1, dtype=np.int64)
            recode[keep] = np.arange(int(keep.sum()))
            codes = np.where(codes >= 0, recode[np.maximum(codes, 0)], -1)
            texts = texts[keep]
        self.codes: npt.NDArray[np.int64] = np.asarray(codes, dtype=np.int64)
        self.texts: npt.NDArray[np.object_] = texts
        self.counts: npt.NDArray[np.int64] = np.bincount(self.codes[self.codes >= 0], minlength=len(texts))
        self.rows = int(self.counts.sum())
        self._fixed: tuple[npt.NDArray[np.str_] | None] | None = None
        self._stages: dict[tuple[Op, ...], npt.NDArray[np.str_]] = {}
        self._applied: dict[tuple[int, Op], tuple[Any, npt.NDArray[np.str_]]] = {}
        self._written: dict[tuple[Op, ...], tuple[npt.NDArray[np.object_], npt.NDArray[np.bool_]]] = {}
        self._index: dict[int, pd.Index[Any]] = {}
        self._as_text: dict[int, npt.NDArray[np.object_]] = {}
        self._found: dict[tuple[int, int], tuple[Any, Any, npt.NDArray[np.bool_]]] = {}
        self._prefix: tuple[str | None] | None = None

    @staticmethod
    def of(column: pd.Series[Any] | KeyColumn) -> KeyColumn:
        return column if isinstance(column, KeyColumn) else KeyColumn(column)

    def fixed(self) -> npt.NDArray[np.str_] | None:
        if self._fixed is None:
            self._fixed = (_as_fixed(self.texts),)
        return self._fixed[0]

    def _stage(self, ops: tuple[Op, ...]) -> npt.NDArray[np.str_]:
        found = self._stages.get(ops)
        if found is None:
            if not ops:
                base = self.fixed()
                assert base is not None
                return base
            before = self._stage(ops[:-1])
            held = self._applied.get((id(before), ops[-1]))
            if held is not None and held[0] is before:  # `spaces` changed nothing: `case` was done already
                found = held[1]
            else:
                found = _fixed_op(before, ops[-1])
                self._applied[(id(before), ops[-1])] = (before, found)
            self._stages[ops] = found
        return found

    def written(self, rule: KeyRule) -> tuple[npt.NDArray[np.object_], npt.NDArray[np.bool_]]:
        """`(each distinct ID written by rule, whether the phone rule could not read it)`; cached per rule."""
        ops = _ops(rule)
        found = self._written.get(ops)
        if found is None:
            found = self._write(ops)
            self._written[ops] = found
        return found

    def _write(self, ops: tuple[Op, ...]) -> tuple[npt.NDArray[np.object_], npt.NDArray[np.bool_]]:
        failed = np.zeros(len(self.texts), dtype=bool)
        if not ops:
            return self.texts, failed
        phone = ops[-1][0] == "phone"
        steps = ops[:-1] if phone else ops
        if self.fixed() is not None:
            values = self._objects(self._stage(steps))
        else:
            current = list(self.texts)
            for op in steps:
                current = _python_op(current, op)
            values = np.array(current, dtype=object)
        if not phone:
            return values, failed
        written = [_phone(text) for text in values]
        failed = np.array([text is None for text in written], dtype=bool)
        out = np.array(written, dtype=object)
        out[failed] = values[failed]
        return out, failed

    def _objects(self, stage: npt.NDArray[np.str_]) -> npt.NDArray[np.object_]:
        """A stage as Python text, converted once per distinct stage (a rule that changed nothing shares it)."""
        if stage is self.fixed():
            return self.texts
        found = self._as_text.get(id(stage))
        if found is None:
            found = stage.astype(object)
            self._as_text[id(stage)] = found
        return found

    def index(self, rule: KeyRule) -> pd.Index[Any]:
        """The distinct IDs written by `rule`, as an index looked up in (its hash table is built once, and once
        for every rule that writes them alike)."""
        written = self.written(rule)[0]
        found = self._index.get(id(written))
        if found is None:
            found = pd.Index(written[np.unique(factorize_exact(written)[0], return_index=True)[1]])
            self._index[id(written)] = found
        return found

    def rows_of(self, rule: KeyRule) -> npt.NDArray[np.object_]:
        """Every row's ID written by `rule`; None for an empty cell."""
        return self.take(self.written(rule)[0])

    def take(self, per_id: npt.NDArray[Any]) -> npt.NDArray[Any]:
        """A value per distinct ID spread over the rows; None for an empty cell."""
        out = np.full(len(self.codes), None, dtype=object)
        present = self.codes >= 0
        out[present] = per_id[self.codes[present]]
        return out

    def flags(self, per_id: npt.NDArray[np.bool_]) -> npt.NDArray[np.bool_]:
        """A flag per distinct ID spread over the rows; False for an empty cell."""
        return (
            np.where(self.codes >= 0, per_id[np.maximum(self.codes, 0)], False)
            if len(per_id)
            else (np.zeros(len(self.codes), dtype=bool))
        )

    def failed_rows(self, rule: KeyRule) -> int:
        return int(self.counts[self.written(rule)[1]].sum())

    def found_in(self, rule: KeyRule, other: KeyColumn, other_rule: KeyRule) -> npt.NDArray[np.bool_]:
        """For each distinct ID written by `rule`, whether `other` holds it written by `other_rule`."""
        mine, theirs = self.written(rule)[0], other.index(other_rule)
        key = (id(mine), id(theirs))
        held = self._found.get(key)
        if held is not None and held[0] is mine and held[1] is theirs:  # the very arrays, not a reused id
            return held[2]
        found = np.asarray(theirs.get_indexer(pd.Index(mine, copy=False)) >= 0, dtype=np.bool_)
        self._found[key] = (mine, theirs, found)
        return found

    def share_in(self, rule: KeyRule, other: KeyColumn, other_rule: KeyRule) -> float:
        """Share of this column's non-empty rows whose ID, written by `rule`, is among `other`'s by its rule."""
        if not self.rows:
            return 1.0
        found = self.found_in(rule, other, other_rule)
        return round(int(self.counts[found].sum()) / self.rows, 4)

    def common_prefix(self) -> str | None:
        """The letters most of the rows' IDs start with (`KEY_PREFIX_SHARE`), when there are such letters."""
        if self._prefix is None:
            self._prefix = (_prefix_of(self),)
        return self._prefix[0]

    def phone_like(self) -> bool:
        """Whether nearly every row's ID reads as a phone number (`PHONE_SHARE`)."""
        if not self.rows:
            return False
        fixed = self.fixed()
        if fixed is not None:
            codes = _codes(fixed)
            digits = _between(codes, ((48, 57),)).sum(axis=1)
            shaped = digits >= 8
        else:
            shaped = np.array([sum(ch.isdigit() for ch in text) >= 8 for text in self.texts], dtype=bool)
        if int(self.counts[shaped].sum()) / self.rows < PHONE_SHARE:
            return False
        failed = self.written(KeyRule(phone=True))[1]
        return int(self.counts[~failed].sum()) / self.rows >= PHONE_SHARE


def _prefix_of(column: KeyColumn) -> str | None:
    """`_PREFIX` on every distinct ID, weighed by its rows: in C for ASCII IDs, by the regex for the rest."""
    if not column.rows:
        return None
    fixed = column.fixed()
    if fixed is None:
        found = (
            pd.Series(column.texts, dtype=object).str.extract(_PREFIX, expand=False).to_numpy(dtype=object)
        )
    else:
        codes = _codes(fixed)
        n, width = codes.shape
        wide = (codes >= 128).any(axis=1)
        letter = _between(codes, ((65, 90), (97, 122)))
        first = np.argmin(letter, axis=1)  # the first character that is not a letter
        rows = np.arange(n)
        at = codes[rows, first]
        after = codes[rows, np.minimum(first + 1, width - 1)]
        digit_at = (at >= 48) & (at <= 57)
        sep_at = np.isin(at, _SEP_CODES) & (first + 1 < width) & (after >= 48) & (after <= 57)
        length = np.where(digit_at, first, np.where(sep_at, first + 1, 0))
        valid = (first >= 1) & ~letter.all(axis=1) & (length > 0) & ~wide
        found = np.full(n, None, dtype=object)
        if valid.any():
            found[valid] = np.strings.slice(fixed[valid], 0, length[valid]).astype(object)
        if wide.any():  # a non-ASCII digit is a digit to the regex
            found[wide] = (
                pd.Series(column.texts[wide], dtype=object)
                .str.extract(_PREFIX, expand=False)
                .to_numpy(dtype=object)
            )
    present = pd.notna(found)
    if not present.any():
        return None
    weights = pd.Series(column.counts[present]).groupby(found[present]).sum()
    prefix = str(weights.idxmax())
    return prefix if int(weights.max()) / column.rows >= KEY_PREFIX_SHARE else None


def normalise_keys(series: pd.Series[Any], rule: KeyRule) -> ParseOutcome:
    """The column with every ID written by `rule`, once per distinct value; `changed` counts cells whose text
    changed, `failed` the non-empty cells the phone rule could not read (kept as written)."""
    column = KeyColumn(series)
    written, failed = column.written(rule)
    changed_ids = (written != column.texts) & ~failed
    examples = tuple(masked_cut(str(value), 60) for value in list(dict.fromkeys(written[failed]))[:_EXAMPLES])
    out = pd.Series(column.rows_of(rule), index=series.index, dtype=object)
    return ParseOutcome(
        values=out,
        changed=int(column.counts[changed_ids].sum()),
        failed=int(column.counts[failed].sum()),
        failed_examples=examples,
    )


# ---------------------------------------------------------------------------
# Measuring two key columns, and proposing the rules under which they meet
# ---------------------------------------------------------------------------
KeysLike: TypeAlias = "pd.Series[Any] | KeyColumn"
"""A key column, or one already read (`KeyColumn`): a caller trying many rules on one column reads it once."""


def key_coverage(
    keys: KeysLike,
    other: KeysLike,
    rule: KeyRule | None = None,
    other_rule: KeyRule | None = None,
) -> float:
    """Share of `keys`' non-empty IDs found among `other`'s, each side written by its rule (as written when
    None). 1.0 when `keys` has none (nothing failed to meet)."""
    return KeyColumn.of(keys).share_in(
        rule or KeyRule.identity(), KeyColumn.of(other), other_rule or KeyRule.identity()
    )


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


def _candidates(keys: KeyColumn, base: KeyColumn) -> list[tuple[KeyRule, KeyRule]]:
    """Rule pairs from the smallest change up; the first that clears the bar is proposed."""
    pairs: list[tuple[KeyRule, KeyRule]] = []
    simple = [
        KeyRule(spaces=True),
        KeyRule(case=True),
        KeyRule(zeros=True),
        KeyRule(spaces=True, case=True, zeros=True),
    ]
    pairs += [(rule, rule) for rule in simple]
    base_prefix, prefix = base.common_prefix(), keys.common_prefix()
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
    if keys.phone_like() and base.phone_like():
        pairs.append((KeyRule(phone=True), KeyRule(phone=True)))
    return pairs


def propose_key_rules(keys: KeysLike, base: KeysLike) -> KeyRuleProposal:
    """How `keys` meet `base`: as written when they already do; otherwise the first rule pair (smallest change
    first) that clears `KEY_JOIN_OK`; otherwise the pair that got furthest, which `meets` says is not enough.
    Only exact equality after the rules counts: nothing is ever matched by being similar."""
    mine, theirs = KeyColumn.of(keys), KeyColumn.of(base)
    identity = KeyRule.identity()
    written = mine.share_in(identity, theirs, identity)
    if written >= KEY_JOIN_OK:
        return KeyRuleProposal(identity, identity, written, written)
    best = KeyRuleProposal(identity, identity, written, written)
    for base_rule, rule in _candidates(mine, theirs):
        fixed = mine.share_in(rule, theirs, base_rule)
        if fixed >= KEY_JOIN_OK:
            return KeyRuleProposal(base_rule, rule, written, fixed)
        if fixed > best.fixed_coverage:
            best = KeyRuleProposal(base_rule, rule, written, fixed)
    return best


def fit_rule(keys: KeysLike, base: KeysLike, base_rule: KeyRule) -> KeyRuleProposal:
    """How `keys` meet `base` once `base` is written by `base_rule` (chosen for another file): the first of
    `keys`' own proposal, the base's rule itself, the base's rule without its prefix, and as written, that
    clears `KEY_JOIN_OK`; else the best of them. Several files joined to one list share the list's rule."""
    mine, theirs = KeyColumn.of(keys), KeyColumn.of(base)
    own = propose_key_rules(mine, theirs)
    written = own.coverage
    candidates: list[KeyRule] = []
    if own.base_rule == base_rule:
        candidates.append(own.rule)
    candidates.append(base_rule)
    if base_rule.prefix is not None:
        candidates.append(base_rule.model_copy(update={"prefix": None}))
    candidates.append(KeyRule.identity())
    best = KeyRuleProposal(base_rule, KeyRule.identity(), written, 0.0)
    seen: set[tuple[tuple[str, Any], ...]] = set()
    for rule in candidates:
        marker = tuple(sorted(rule.params().items()))
        if marker in seen:
            continue
        seen.add(marker)
        fixed = mine.share_in(rule, theirs, base_rule)
        found = KeyRuleProposal(base_rule, rule, written, fixed)
        if found.meets:
            return found
        if fixed > best.fixed_coverage:
            best = found
    return best


def rule_from_params(params: dict[str, Any]) -> KeyRule:
    """The rule a `normalise_key` step's parameters spell; raises `ValueError` when they spell none."""
    return KeyRule.model_validate(params)
