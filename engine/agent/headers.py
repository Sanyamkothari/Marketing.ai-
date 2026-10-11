"""Reading abbreviated and romanised-Hindi headers (Plan K M142, DEC-1409).

Indian exports often call a column `cust_mob_no`, `last_rchg_dt`, `pehla_order`, `grahak_id` or
`avg_mthly_spnd`. The use case's own hints (`primary_key_hints`, `agent.column_hints`) know none of
those, so Guided setup had nothing to suggest. This module reads a header the way a person who
knows the shorthand does, by rules:

1. **Words.** The header is cut into words (`_`, `-`, spaces, camelCase, letter/digit boundaries),
   and a run-on name such as `custmobno` is cut into known words.
2. **Meanings.** Each word, and each run of words the lexicon lists (`opt in`), becomes a canonical
   meaning (`mob` -> mobile, `rchg` -> recharge, `pehla` -> first). The dictionary is
   `configs/agent/header_lexicon.yaml`; an operator adds a spelling there, never here.
3. **A score per role.** For one of the six roles (ID, outcome, date, consent, opt-out, last
   contact): the name score starts at the strongest anchor meaning found, adds the boosters found
   (at most `MAX_BOOST`), subtracts the blockers found, and is cut to 0..1; no anchor, no score. The
   column's values then give a check, 1 when they look like the role (an ID is unique and never
   empty, an outcome or a flag has two values, a date reads as dates) and less otherwise. The final
   score is ``name x (VALUE_FLOOR + (1 - VALUE_FLOOR) x values)``: the values lower a suggestion by up
   to 60% but never create one.
4. **A short list.** Scores under the lexicon's `min_listed` are dropped; the best three per role
   are kept, ties in file order. 0.75 and over is `high`, 0.5 and over `medium`, else `low`.

Nothing here reads a cell: only the header and the profile's counts, so the result carries no
customer data. A suggestion made from it is always "please check" (`AgentConfidence.CHECK`) and is
never ticked for the person. The optional rerank (`engine.agent.assist`) may reorder these
candidates; it can never add a column this module did not list (`reorder_within`).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Final

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from engine.config import ColumnType, config_root
from engine.contracts import ColumnProfile

__all__ = [
    "LEXICON_FILENAME",
    "MAX_BOOST",
    "ROLES",
    "TOP_N",
    "VALUE_FLOOR",
    "HeaderCandidate",
    "HeaderLexicon",
    "HeaderLexiconError",
    "LabelDraftRules",
    "activity_dates",
    "load_lexicon",
    "meanings_of",
    "rank_roles",
    "reorder_within",
    "tokenize",
]

LEXICON_FILENAME: Final[str] = "header_lexicon.yaml"
LEXICON_DIRNAME: Final[str] = "agent"
ROLES: Final[tuple[str, ...]] = ("id", "outcome", "date", "consent", "opt_out", "last_contact")
"""The roles a header can be read as. The same names are the keys of `find_roles`'s `header_candidates`."""

TOP_N: Final[int] = 3
"""Candidates kept per role: the short list a person can check by eye."""
MAX_BOOST: Final[float] = 0.4
"""The most all boosters together add to a name score."""
VALUE_FLOOR: Final[float] = 0.4
"""A column whose values do not look like the role keeps this share of its name score."""
_MAX_PHRASE: Final[int] = 4
_NOT_LAST_ACTIVITY: Final[frozenset[str]] = frozenset({"first", "lifecycle", "deadline"})
"""A date with one of these in its name (`pehla_order`, `signup_dt`, `expiry_dt`) is not when a customer was
last active, so it cannot say who has gone quiet."""
_MIN_EXTRA_WORD: Final[int] = 3
_MIN_COMPOUND: Final[int] = 5
_MAX_COMPOUND_PIECES: Final[int] = 4

_SEPARATORS: Final[re.Pattern[str]] = re.compile(r"[\s_\-./\\:;,()\[\]{}|#@$%&*+=<>?!~^'\"`]+")
_CAMEL: Final[re.Pattern[str]] = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")
_ASCII_WORD: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9]+")


class HeaderLexiconError(ValueError):
    """The lexicon file is malformed; the message names the entry."""


_CONFIG: Final[ConfigDict] = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class RoleRule(BaseModel):
    """One role's name rule: anchors it needs, boosters that help, blockers that count against it."""

    model_config = _CONFIG

    anchors: Mapping[str, Annotated[float, Field(gt=0.0, le=1.0)]]
    boosters: Mapping[str, Annotated[float, Field(gt=0.0, le=1.0)]] = Field(default_factory=dict)
    blockers: Mapping[str, Annotated[float, Field(gt=0.0, le=1.0)]] = Field(default_factory=dict)

    @field_validator("anchors")
    @classmethod
    def _some_anchor(cls, value: Mapping[str, float]) -> Mapping[str, float]:
        if not value:
            raise ValueError("a role needs at least one anchor meaning")
        return value


class LabelDraftRules(BaseModel):
    """How the outcome-label drafts are built (M93's lapse label: a window plus a grace period)."""

    model_config = _CONFIG

    activity: tuple[str, ...] = ("recharge", "order", "purchase", "transaction", "visit", "bill", "payment")
    """Meanings of a column that records something the customer did (a lapse is the lack of it)."""
    windows_days: tuple[Annotated[int, Field(ge=1, le=365)], ...] = (30, 60, 90)
    grace_days: Annotated[int, Field(ge=0, le=365)] = 7
    examples: Annotated[int, Field(ge=1, le=5)] = 2

    @field_validator("windows_days")
    @classmethod
    def _windows(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if not value or len(set(value)) != len(value):
            raise ValueError("windows_days needs one or more different numbers of days")
        return tuple(sorted(value))


class HeaderLexicon(BaseModel):
    """The parsed `header_lexicon.yaml`."""

    model_config = _CONFIG

    schema_version: int
    min_listed: Annotated[float, Field(ge=0.0, le=1.0)]
    high: Annotated[float, Field(ge=0.0, le=1.0)]
    medium: Annotated[float, Field(ge=0.0, le=1.0)]
    meanings: Mapping[str, tuple[str, ...]]
    roles: Mapping[str, RoleRule]
    value_checks: Mapping[str, str]
    label_drafts: LabelDraftRules = LabelDraftRules()

    @model_validator(mode="after")
    def _consistent(self) -> HeaderLexicon:
        if not 0.0 <= self.medium <= self.high <= 1.0:
            raise ValueError("medium must not be above high")
        if set(self.roles) != set(ROLES):
            raise ValueError(f"roles must be exactly {', '.join(ROLES)}")
        if set(self.value_checks) != set(ROLES):
            raise ValueError("value_checks needs one entry for each role")
        for role, check in self.value_checks.items():
            if check not in _VALUE_CHECKS:
                raise ValueError(f"value_checks.{role} must be one of {', '.join(sorted(_VALUE_CHECKS))}")
        seen: dict[str, str] = {}
        for meaning, words in self.meanings.items():
            for word in words:
                key = _fold(word)
                if not key or key != word.strip():
                    raise ValueError(f"meanings.{meaning}: {word!r} must be written lower case")
                if key in seen and seen[key] != meaning:
                    raise ValueError(f"{word!r} is listed under both {seen[key]} and {meaning}")
                seen[key] = meaning
        missing_activity = sorted(set(self.label_drafts.activity) - set(self.meanings))
        if missing_activity:
            raise ValueError(
                f"label_drafts.activity names a meaning that has no words: {', '.join(missing_activity)}"
            )
        for role, rule in self.roles.items():
            for group in (rule.anchors, rule.boosters, rule.blockers):
                missing = sorted(set(group) - set(self.meanings))
                if missing:
                    raise ValueError(f"roles.{role} names a meaning that has no words: {', '.join(missing)}")
        return self


_VALUE_CHECKS: Final[frozenset[str]] = frozenset({"unique", "two_values", "dates"})


def _fold(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold().strip()


@dataclass(frozen=True)
class _Index:
    words: Mapping[str, str]
    """word -> meaning, for single words."""
    phrases: Mapping[tuple[str, ...], str]
    """a run of words -> meaning."""
    longest: int


def _index(lexicon: HeaderLexicon) -> _Index:
    words: dict[str, str] = {}
    phrases: dict[tuple[str, ...], str] = {}
    for meaning, spellings in lexicon.meanings.items():
        for spelling in spellings:
            parts = tuple(p for p in _fold(spelling).split(" ") if p)
            if len(parts) > 1:
                phrases[parts] = meaning
            elif parts:
                words[parts[0]] = meaning
    longest = max((len(p) for p in phrases), default=1)
    return _Index(words, phrases, min(longest, _MAX_PHRASE))


@lru_cache(maxsize=8)
def _load(path: str, mtime_ns: int) -> tuple[HeaderLexicon, _Index]:
    del mtime_ns  # part of the cache key: an edited file is read again
    try:
        loaded = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise HeaderLexiconError(f"The header lexicon {path} could not be read: {exc}") from exc
    if not isinstance(loaded, dict):
        raise HeaderLexiconError(f"The header lexicon {path} must be a mapping.")
    try:
        lexicon = HeaderLexicon.model_validate(loaded)
    except ValueError as exc:
        raise HeaderLexiconError(f"The header lexicon {path} is not valid: {exc}") from exc
    return lexicon, _index(lexicon)


def _lexicon_path(root: Path | None) -> Path:
    path = config_root(root) / LEXICON_DIRNAME / LEXICON_FILENAME
    if not path.exists() and root is not None:
        path = config_root() / LEXICON_DIRNAME / LEXICON_FILENAME  # a test root need not carry its own
    return path


def load_lexicon(root: Path | None = None) -> HeaderLexicon:
    """The lexicon under `root` (else the configuration root), validated; raises `HeaderLexiconError`."""
    path = _lexicon_path(root)
    try:
        mtime = path.stat().st_mtime_ns
    except OSError as exc:
        raise HeaderLexiconError(f"The header lexicon {path} could not be read: {exc}") from exc
    return _load(str(path), mtime)[0]


def _loaded(root: Path | None) -> tuple[HeaderLexicon, _Index]:
    path = _lexicon_path(root)
    try:
        mtime = path.stat().st_mtime_ns
    except OSError as exc:
        raise HeaderLexiconError(f"The header lexicon {path} could not be read: {exc}") from exc
    return _load(str(path), mtime)


# ---------------------------------------------------------------------------
# Words and meanings
# ---------------------------------------------------------------------------
def tokenize(header: str) -> tuple[str, ...]:
    """The words of a header: split on separators, camelCase and letter/digit edges; digits dropped.

    `GrahakID` -> grahak, id; `kharida_30d` -> kharida, d; `Last-Contact Date` -> last, contact, date.
    Words in other scripts (Devanagari) are kept whole.
    """
    text = unicodedata.normalize("NFC", header)
    words: list[str] = []
    for chunk in _SEPARATORS.split(text):
        if not chunk:
            continue
        if _ASCII_WORD.fullmatch(chunk):
            words.extend(w for w in (m.group(0) for m in _CAMEL.finditer(chunk)) if not w.isdigit())
        else:
            # Mixed scripts: keep ASCII runs split as above, other runs whole.
            for part in re.split(r"([A-Za-z0-9]+)", chunk):
                if not part:
                    continue
                if _ASCII_WORD.fullmatch(part):
                    words.extend(w for w in (m.group(0) for m in _CAMEL.finditer(part)) if not w.isdigit())
                else:
                    words.append(part)
    return tuple(w.casefold() for w in words if w)


def _compound(word: str, known: Mapping[str, str]) -> tuple[str, ...] | None:
    """`custmobno` -> (cust, mob, no): the fewest known words that spell the whole run-on word, or None."""
    if len(word) < _MIN_COMPOUND or not word.isascii() or not word.isalpha():
        return None
    best: dict[int, tuple[str, ...]] = {0: ()}
    for end in range(1, len(word) + 1):
        for start in range(max(0, end - 12), end):
            if start not in best or end - start < 2:
                continue
            piece = word[start:end]
            if piece in known and len(best[start]) < _MAX_COMPOUND_PIECES:
                candidate = (*best[start], piece)
                if end not in best or len(candidate) < len(best[end]):
                    best[end] = candidate
    found = best.get(len(word))
    return found if found is not None and len(found) >= 2 else None


def _meanings(words: Sequence[str], index: _Index) -> tuple[str, ...]:
    """Canonical meanings of `words`, in order: longest listed phrase first, then single words."""
    expanded: list[str] = []
    for word in words:
        if word in index.words:
            expanded.append(word)
            continue
        cut = _compound(word, index.words)
        expanded.extend(cut if cut is not None else (word,))
    found: list[str] = []
    at = 0
    while at < len(expanded):
        for size in range(min(index.longest, len(expanded) - at), 1, -1):
            phrase = tuple(expanded[at : at + size])
            if phrase in index.phrases:
                found.append(index.phrases[phrase])
                at += size
                break
        else:
            word = expanded[at]
            found.append(index.words.get(word, f"word:{word}"))
            at += 1
    return tuple(found)


def meanings_of(header: str, root: Path | None = None) -> tuple[str, ...]:
    """The lexicon's meanings for `header`'s words, in order; an unknown word is `word:<word>`."""
    _, index = _loaded(root)
    return _meanings(tokenize(header), index)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class HeaderCandidate:
    """One column the rules read as a role, with the score and the words it was read from."""

    column: str
    role: str
    score: float
    """0..1, two decimals; see the module docstring for the rule."""
    confidence: str
    """`high`, `medium` or `low` from the score; the suggestion is "please check" whatever it is."""
    reads_as: tuple[str, ...]
    """The lexicon words that decided it, in header order (`customer`, `mobile`, `number`)."""


def _unique(column: ColumnProfile, rows: int | None) -> float:
    """1 for a column whose every value is different and none empty; half for one that is nearly so."""
    if column.null_count == 0 and column.is_unique:
        return 1.0
    if rows and column.null_rate <= 0.01 and column.distinct_count / rows >= 0.98:
        return 0.5
    return 0.0


def _two_values(column: ColumnProfile, rows: int | None) -> float:
    del rows  # every check takes the row count; this one needs only the column's own counts
    if column.distinct_count == 2:
        return 1.0
    return 0.2 if column.distinct_count == 1 else 0.0


def _dates(column: ColumnProfile, rows: int | None) -> float:
    del rows
    if column.inferred_type in {ColumnType.DATE, ColumnType.DATETIME}:
        return 1.0
    return 0.8 if column.looks_like_time and column.inferred_type in {ColumnType.STRING} else 0.1


_CHECKS: Final[Mapping[str, Callable[[ColumnProfile, int | None], float]]] = {
    "unique": _unique,
    "two_values": _two_values,
    "dates": _dates,
}


def _value_score(kind: str, column: ColumnProfile, rows: int | None) -> float:
    return _CHECKS[kind](column, rows)


def _extra_key(phrase: tuple[str, ...]) -> str:
    """The anchor name of one of the use case's own outcome names: a single word is plain, a name of several
    words is one phrase (`won back`), matched only when every word is in the header."""
    return f"word:{phrase[0]}" if len(phrase) == 1 else "phrase:" + " ".join(phrase)


def _in_order(phrase: Sequence[str], words: Sequence[str]) -> bool:
    """Whether every word of `phrase` is in `words`, in that order (other words may sit between them)."""
    remaining = iter(words)
    return all(any(w == word for word in remaining) for w in phrase)


def _name_score(rule: RoleRule, found: Iterable[str]) -> tuple[float, tuple[str, ...]]:
    present = list(dict.fromkeys(found))
    anchors = {m: rule.anchors[m] for m in present if m in rule.anchors}
    if not anchors:
        return 0.0, ()
    chosen = max(anchors, key=lambda m: anchors[m])  # the strongest anchor is not also a booster
    boost = min(MAX_BOOST, sum(rule.boosters[m] for m in present if m in rule.boosters and m != chosen))
    block = sum(rule.blockers[m] for m in present if m in rule.blockers and m != chosen)
    used = tuple(m for m in present if m in rule.anchors or m in rule.boosters or m in rule.blockers)
    return max(0.0, min(1.0, anchors[chosen] + boost - block)), used


def _band(lexicon: HeaderLexicon, score: float) -> str:
    return "high" if score >= lexicon.high else "medium" if score >= lexicon.medium else "low"


def rank_roles(
    columns: Sequence[ColumnProfile],
    *,
    rows: int | None = None,
    extra_anchors: Mapping[str, Iterable[str]] | None = None,
    root: Path | None = None,
    top: int = TOP_N,
) -> dict[str, tuple[HeaderCandidate, ...]]:
    """The best `top` columns for each role, by the rule in the module docstring.

    `rows` is the file's row count, which lets a nearly unique column count for half an ID; without it only
    a column that is exactly unique does.

    `extra_anchors` adds words as anchors for one role (the use case's own outcome words: its target
    column and `agent.column_hints.target_synonyms`), each at the role's strongest anchor weight. A name of
    one word (`converted_30d` -> converted) matches that word whatever the lexicon says; a name of several
    words (`won_back`, `churn_next_60d`) matches only a header that has all of them in that order, so
    `cash_back_flag` is not `won_back` and `next_bill_flag` is not `churn_next_60d`.
    A role no column fits is an empty tuple, never a guess.
    """
    lexicon, index = _loaded(root)
    ranked: dict[str, tuple[HeaderCandidate, ...]] = {}
    extra: dict[str, set[tuple[str, ...]]] = {}
    for role, names in (extra_anchors or {}).items():
        wanted: set[tuple[str, ...]] = set()
        for name in names:
            # `converted_30d` -> (converted,); `won_back` -> (won, back). A one- or two-letter piece (`d`)
            # and a word the role only counts as a booster (`flag`, `days`) say nothing by themselves.
            kept = tuple(
                w
                for w in tokenize(name)
                if len(w) >= _MIN_EXTRA_WORD and index.words.get(w) not in lexicon.roles[role].boosters
            )
            if kept:
                wanted.add(kept)
        extra[role] = wanted
    read: list[tuple[str, ...]] = []  # each header read once, for all six roles
    header_words: list[tuple[str, ...]] = []
    for column in columns:
        words = tokenize(column.name)
        header_words.append(words)
        read.append((*_meanings(words, index), *(f"word:{w}" for w in words)))
    for role in ROLES:
        rule = lexicon.roles[role]
        phrases = sorted(extra.get(role, ()))
        if phrases:
            top_weight = max(rule.anchors.values())
            rule = rule.model_copy(
                update={"anchors": {**rule.anchors, **{_extra_key(p): top_weight for p in phrases}}}
            )
        kind = lexicon.value_checks[role]
        scored: list[tuple[float, int, HeaderCandidate]] = []
        for position, column in enumerate(columns):
            found = read[position]
            if phrases:
                found = (
                    *found,
                    *(_extra_key(p) for p in phrases if len(p) > 1 and _in_order(p, header_words[position])),
                )
            by_name, used = _name_score(rule, found)
            if by_name <= 0.0:
                continue
            values = _value_score(kind, column, rows)
            score = round(by_name * (VALUE_FLOOR + (1.0 - VALUE_FLOOR) * values), 2)
            if score < lexicon.min_listed:
                continue
            scored.append(
                (
                    score,
                    position,
                    HeaderCandidate(
                        column.name,
                        role,
                        score,
                        _band(lexicon, score),
                        tuple(m.removeprefix("word:").removeprefix("phrase:") for m in used),
                    ),
                )
            )
        scored.sort(key=lambda item: (-item[0], item[1]))
        ranked[role] = tuple(item[2] for item in scored[:top])
    return ranked


def activity_dates(
    columns: Sequence[ColumnProfile], *, root: Path | None = None
) -> tuple[tuple[str, str], ...]:
    """(column, activity word) for each date column that records something the customer did.

    `last_rchg_dt` -> ("last_rchg_dt", "recharge"). A column with `last` in its name comes first, then
    file order. The activity words are `label_drafts.activity` in the lexicon. A first order, a sign-up or an
    expiry date is not when the customer was last active, so it is left out.
    """
    lexicon, index = _loaded(root)
    wanted = set(lexicon.label_drafts.activity)
    found: list[tuple[int, int, str, str]] = []
    for position, column in enumerate(columns):
        if column.inferred_type not in {ColumnType.DATE, ColumnType.DATETIME} and not (
            column.looks_like_time and column.inferred_type is ColumnType.STRING
        ):
            continue
        meanings = _meanings(tokenize(column.name), index)
        activity = next((m for m in meanings if m in wanted), None)
        if activity is not None and not set(meanings) & _NOT_LAST_ACTIVITY:
            found.append((0 if "last" in meanings else 1, position, column.name, activity))
    return tuple((name, activity) for _, _, name, activity in sorted(found))


def reorder_within(
    candidates: Sequence[HeaderCandidate], proposed: Iterable[str]
) -> tuple[HeaderCandidate, ...]:
    """`candidates` reordered to follow `proposed`; a name that is not a candidate is ignored.

    This is the only way a rerank can change the list, so it can never add a column the rules did
    not list: names outside `candidates` are dropped, repeats count once, and any candidate the
    proposal left out keeps its place after the ones it named.
    """
    by_name = {c.column: c for c in candidates}
    order = [by_name[name] for name in dict.fromkeys(proposed) if name in by_name]
    rest = [c for c in candidates if c.column not in {o.column for o in order}]
    return (*order, *rest)
