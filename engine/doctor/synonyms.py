"""Spellings that mean the same value, grouped by rules (Plan O M238, DEC-1808 (d)): `synonym_clusters`.

A READ tool. It groups the spellings of one category column by five frozen rules, and proposes one `map_values`
step that would write every spelling of a group as the group's canonical spelling. The proposal is pending,
`check` and never ticked: `Pre-paid`, `PP` and `Prepaid` are merged only when the person ticks it.

The rules, each deterministic and each reading only the column's distinct spellings and their counts:

1. **Same letters** - equal once case and the separators between letters (space, hyphen, underscore, dot,
   other punctuation) are ignored (`Pre-paid`, `pre paid`, `PREPAID`). A sign is never ignored: `+`, `%`, `&`
   and a leading or trailing `-` change the value (`O+` and `O-` are blood groups, `A+` is not `A`).
2. **Lexicon** - listed as one value in `configs/doctor/value_synonyms.yaml` (`Bombay` and `Mumbai`; `haan`
   and `Yes`). A group needs two of its spellings in the column.
3. **Initials** - a short all-letter spelling equal to the initials of exactly one other group's words
   (`PP` and `Pre-paid`). When the initials fit two groups (`Pre-paid` and `Post-paid`), nothing is merged and
   the spelling is listed as ambiguous: a question for the person, never a guess.
4. **One-letter slip** - two spellings with no digits, one edit apart (a letter added, dropped, changed or two
   swapped) inside one word of five or more letters, every other word identical, where the rarer is at most a
   fifth as common as the other (`Mumbai` and `Mumabi`). Digits never take part, and a short word that differs
   is a different value: `Plan 1` and `Plan 2`, `Plan A` and `Plan B` stay apart.
5. **AI** (`check_ai_merge`) - the AI may propose a group too; the rules then verify that every spelling is in
   the column, measure the change, and apply decision O10's change-share guard.

The canonical spelling of a group is the lexicon's first spelling when the column has it; else the most common
spelling that is not an abbreviation; ties go to the spelling the file uses first. Columns with more than 500
distinct spellings are free text or IDs and are left alone; the ID and the outcome are never touched.
"""

from __future__ import annotations

import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any, Final, Literal

import pandas as pd
import yaml
from pydantic import Field

from engine.agent.contracts import ProposalSource, RecipeStepKind
from engine.agent.formats import MAX_CATEGORY_DISTINCT, masked_cut
from engine.agent.value_steps import MAX_MAPPINGS, map_values
from engine.config import StrictBase
from engine.doctor.change_share import DEFAULT_LIMITS, ChangeShareLimits, change_share_verdict
from engine.doctor.row_contracts import StepProposal

__all__ = [
    "SYNONYM_CODES",
    "VALUE_SYNONYMS_PATH",
    "AiMergeRefusedError",
    "ClusterMember",
    "SynonymCluster",
    "SynonymReport",
    "check_ai_merge",
    "find_synonym_clusters",
    "load_value_synonyms",
    "spelling_key",
]

SYNONYM_CODES: Final[frozenset[str]] = frozenset({"SYNONYM_MERGE_INVALID"})

VALUE_SYNONYMS_PATH: Final[Path] = (
    Path(__file__).resolve().parents[2] / "configs" / "doctor" / "value_synonyms.yaml"
)

EDIT_MIN_LENGTH: Final[int] = 5
EDIT_MAX_MINOR_SHARE: Final[float] = 0.2
INITIALS_MAX_LENGTH: Final[int] = 5

Rule = Literal["same_letters", "lexicon", "initials", "one_letter_slip", "ai"]
_RULE_ORDER: Final[tuple[Rule, ...]] = ("same_letters", "lexicon", "initials", "one_letter_slip", "ai")


_SIGNS: Final[frozenset[str]] = frozenset("+%&")
"""Characters that change what a value means (`O+`, `10%`, `R&D`): kept in the key, never ignored."""


def _kept(text: str, i: int) -> bool:
    ch = text[i]
    if unicodedata.category(ch)[0] in "LNM" or ch in _SIGNS:
        return True
    # A hyphen joins two parts (`Pre-paid`) unless it ends or starts the value, where it is a sign (`O-`).
    return ch == "-" and (i == 0 or i == len(text) - 1)


def spelling_key(text: str) -> str:
    """Case and separators ignored; letters, digits, Indian-script vowel signs and signs (`+`, `%`, `&`, a
    leading or trailing `-`) kept, so `O+` and `O-`, or `A+` and `A`, never share a key."""
    folded = unicodedata.normalize("NFC", str(text)).casefold().strip()
    return "".join(ch for i, ch in enumerate(folded) if _kept(folded, i))


def _slip_words(text: str) -> list[str]:
    """The words of a spelling for the one-letter-slip rule: runs of kept characters, signs included."""
    folded = unicodedata.normalize("NFC", str(text)).casefold().strip()
    spaced = "".join(ch if _kept(folded, i) else " " for i, ch in enumerate(folded))
    return spaced.split()


def _slip_in_one_word(a: str, b: str) -> bool:
    """Whether `a` and `b` differ by one edit inside one word of five or more letters, every other word equal."""
    wa, wb = _slip_words(a), _slip_words(b)
    if len(wa) != len(wb):
        return False
    differ = [(x, y) for x, y in zip(wa, wb, strict=True) if x != y]
    if len(differ) != 1:
        return False
    x, y = differ[0]
    letters = all(unicodedata.category(ch)[0] in "LM" for ch in x + y)
    return letters and min(len(x), len(y)) >= EDIT_MIN_LENGTH and _one_edit(x, y)


@lru_cache(maxsize=8)
def _load(path: str) -> tuple[tuple[str, ...], ...]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    groups = data.get("groups", [])
    if not isinstance(groups, list) or not all(
        isinstance(g, list) and len(g) >= 2 and all(isinstance(s, str) and s.strip() for s in g)
        for g in groups
    ):
        raise ValueError(f"{path}: groups must be lists of two or more spellings")
    return tuple(tuple(str(s).strip() for s in g) for g in groups)


def load_value_synonyms(path: Path | None = None) -> tuple[tuple[str, ...], ...]:
    """The lexicon's groups of spellings, first spelling canonical."""
    return _load(str(path or VALUE_SYNONYMS_PATH))


class ClusterMember(StrictBase):
    spelling: str = Field(description="The spelling, masked.")
    count: int = Field(ge=0, description="Rows that hold it.")


class SynonymCluster(StrictBase):
    """One group of spellings the rules think are one value."""

    rules: tuple[Rule, ...] = Field(
        description="Every rule that joined spellings of the group, in rule order."
    )
    canonical: str = Field(description="The spelling the merge would write, masked.")
    members: tuple[ClusterMember, ...] = Field(
        description="Every spelling of the group, the canonical first."
    )
    rows_changed: int = Field(ge=0, description="Rows whose spelling the merge would change.")


class SynonymReport(StrictBase):
    """What `synonym_clusters` found in one column."""

    column: str = Field(description="The column.")
    non_empty: int = Field(ge=0, description="Non-empty values.")
    distinct: int = Field(ge=0, description="Distinct spellings.")
    clusters: tuple[SynonymCluster, ...] = Field(description="Groups of two or more spellings.")
    ambiguous: tuple[str, ...] = Field(
        default=(),
        description="Masked abbreviations whose initials fit more than one group: asked, never merged.",
    )
    step: StepProposal | None = Field(
        default=None, description="One pending `map_values` step for every group."
    )
    note: str | None = Field(default=None, description="Why nothing was looked at, when nothing was.")


class AiMergeRefusedError(Exception):
    """An AI merge the rules refuse: `code` is `SYNONYM_MERGE_INVALID` or `AI_STEP_CHANGES_TOO_MANY`."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------
class _Groups:
    """Union-find over spellings, remembering every rule that joined each group."""

    def __init__(self, spellings: Iterable[str]) -> None:
        self.parent = {s: s for s in spellings}
        self.rules: dict[str, set[Rule]] = {}

    def find(self, s: str) -> str:
        while self.parent[s] != s:
            self.parent[s] = self.parent[self.parent[s]]
            s = self.parent[s]
        return s

    def join(self, a: str, b: str, rule: Rule) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        self.parent[rb] = ra
        self.rules[ra] = self.rules.pop(ra, set()) | self.rules.pop(rb, set()) | {rule}

    def groups(self) -> list[list[str]]:
        out: dict[str, list[str]] = {}
        for s in self.parent:
            out.setdefault(self.find(s), []).append(s)
        return [g for g in out.values() if len(g) > 1]


def _words(text: str) -> list[str]:
    spaced = "".join(" " if not (ch.isalnum()) else ch for ch in text)
    out: list[str] = []
    for word in spaced.split():
        # camelCase: "PrePaid" is two words
        start = 0
        for i in range(1, len(word)):
            if word[i].isupper() and word[i - 1].islower():
                out.append(word[start:i])
                start = i
        out.append(word[start:])
    return out


def _one_edit(a: str, b: str) -> bool:
    """Whether `a` and `b` are one insertion, deletion, substitution or swap of neighbours apart."""
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        diff = [i for i in range(len(a)) if a[i] != b[i]]
        if len(diff) == 1:
            return True
        return (
            len(diff) == 2
            and diff[1] == diff[0] + 1
            and a[diff[0]] == b[diff[1]]
            and a[diff[1]] == b[diff[0]]
        )
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    i = 0
    while i < len(short) and short[i] == long_[i]:
        i += 1
    return short[i:] == long_[i + 1 :]


def _cluster(
    counts: Mapping[str, int], lexicon: Sequence[Sequence[str]]
) -> tuple[_Groups, set[str], list[str]]:
    """(the groups, spellings that are abbreviations of their group, ambiguous abbreviations)."""
    spellings = list(counts)
    groups = _Groups(spellings)
    by_key: dict[str, list[str]] = {}
    for s in spellings:
        by_key.setdefault(spelling_key(s), []).append(s)
    for members in by_key.values():
        for other in members[1:]:
            groups.join(members[0], other, "same_letters")
    for entry in lexicon:
        keys = {spelling_key(s) for s in entry}
        present = [s for s in spellings if spelling_key(s) in keys]
        for other in present[1:]:
            groups.join(present[0], other, "lexicon")
    abbreviations: set[str] = set()
    ambiguous: list[str] = []
    for s in spellings:
        compact = s.strip()
        if not (2 <= len(compact) <= INITIALS_MAX_LENGTH and compact.isalpha()):
            continue
        fits = {
            groups.find(o)
            for o in spellings
            if o != s
            and len(_words(o)) >= 2
            and "".join(w[0] for w in _words(o)).casefold() == compact.casefold()
        }
        fits.discard(groups.find(s))
        if len(fits) == 1:
            groups.join(next(iter(fits)), s, "initials")
            abbreviations.add(s)
        elif len(fits) > 1:
            ambiguous.append(s)
    ranked = sorted(by_key, key=lambda k: (-sum(counts[s] for s in by_key[k]), k))
    total = {k: sum(counts[s] for s in by_key[k]) for k in ranked}
    for i, a in enumerate(ranked):
        if len(a) < EDIT_MIN_LENGTH or any(ch.isdigit() for ch in a):
            continue
        for b in ranked[i + 1 :]:
            if len(b) < EDIT_MIN_LENGTH or any(ch.isdigit() for ch in b) or abs(len(a) - len(b)) > 1:
                continue
            if total[b] > EDIT_MAX_MINOR_SHARE * total[a] or not _one_edit(a, b):
                continue
            if not any(_slip_in_one_word(x, y) for x in by_key[a] for y in by_key[b]):
                continue  # the edit is not inside one long word (`Plan A`/`Plan B`), or a sign differs
            groups.join(by_key[a][0], by_key[b][0], "one_letter_slip")
    return groups, abbreviations, ambiguous


def _canonical(
    members: Sequence[str], counts: Mapping[str, int], order: Mapping[str, int], lexicon: Sequence[Sequence[str]],
    abbreviations: set[str],
) -> str:  # fmt: skip
    present = {spelling_key(m) for m in members}

    def most_common(spellings: Sequence[str]) -> str:
        return max(spellings, key=lambda m: (counts[m], -order[m]))

    for entry in lexicon:
        first = spelling_key(entry[0])
        if first in present and len({spelling_key(s) for s in entry} & present) >= 2:
            # The lexicon's own spelling: as listed when the column writes it so, else as the column mostly does.
            written = [m for m in members if spelling_key(m) == first]
            return entry[0] if entry[0] in written else most_common(written)
    return most_common([m for m in members if m not in abbreviations] or list(members))


def _distinct(series: pd.Series[Any]) -> tuple[Counter[str], dict[str, int]]:
    present = series.dropna()
    text = present.map(
        lambda v: str(int(v)) if isinstance(v, float) and v.is_integer() else str(v)
    ).str.strip()
    text = text[text != ""]
    counts: Counter[str] = Counter(text.tolist())
    order = {s: i for i, s in enumerate(dict.fromkeys(text.tolist()))}
    return counts, order


def _proposal(
    column: str,
    series: pd.Series[Any],
    mapping: dict[str, str],
    *,
    source: ProposalSource,
    limits: ChangeShareLimits,
    groups: int,
) -> StepProposal:
    outcome = map_values(series, mapping=mapping)
    non_empty = int(series.notna().sum())
    verdict = change_share_verdict(changed=outcome.changed, out_of=non_empty, source=source, limits=limits)
    if verdict.level == "refuse":
        raise AiMergeRefusedError(str(verdict.code), verdict.message)
    who = "The AI suggests" if source is ProposalSource.AI else "The rules found"
    return StepProposal(
        kind=RecipeStepKind.MAP_VALUES,
        column=column,
        params={"mapping": dict(sorted(mapping.items()))},
        source=source,
        title=f"Write {len(mapping)} spelling(s) in '{column}' the same way as {groups} value(s)",
        reason=(
            f"{who} {groups} group(s) of spellings that look like one value each; merging changes "
            f"{outcome.changed:,} of {non_empty:,} values."
        ),
        changed=outcome.changed,
        out_of=non_empty,
        share_pct=verdict.share_pct,
        guard="warn" if verdict.level == "warn" else "ok",
        guard_code=verdict.code,
    )


def find_synonym_clusters(
    series: pd.Series[Any],
    *,
    column: str,
    lexicon: Sequence[Sequence[str]] | None = None,
    limits: ChangeShareLimits = DEFAULT_LIMITS,
) -> SynonymReport:
    """The groups of spellings in `series` and one pending `map_values` proposal that merges them."""
    words = load_value_synonyms() if lexicon is None else lexicon
    counts, order = _distinct(series)
    non_empty = int(series.notna().sum())
    if pd.api.types.is_numeric_dtype(series) or pd.api.types.is_datetime64_any_dtype(series):
        return SynonymReport(
            column=column, non_empty=non_empty, distinct=len(counts), clusters=(), note="not text"
        )
    if len(counts) > MAX_CATEGORY_DISTINCT:
        return SynonymReport(
            column=column,
            non_empty=non_empty,
            distinct=len(counts),
            clusters=(),
            note=f"more than {MAX_CATEGORY_DISTINCT} different values: free text or an ID, not a category",
        )
    groups, abbreviations, ambiguous = _cluster(counts, words)
    clusters: list[SynonymCluster] = []
    mapping: dict[str, str] = {}
    for members in sorted(groups.groups(), key=lambda g: min(order[m] for m in g)):
        canonical = _canonical(members, counts, order, words, abbreviations)
        ranked = [
            canonical,
            *sorted((m for m in members if m != canonical), key=lambda m: (-counts[m], order[m])),
        ]
        for m in ranked[1:]:
            mapping[m] = canonical
        clusters.append(
            SynonymCluster(
                rules=tuple(r for r in _RULE_ORDER if r in groups.rules[groups.find(members[0])]),
                canonical=masked_cut(canonical, 80),
                members=tuple(ClusterMember(spelling=masked_cut(m, 80), count=counts[m]) for m in ranked),
                rows_changed=sum(counts[m] for m in ranked[1:]),
            )
        )
    step = None
    if mapping and len(mapping) <= MAX_MAPPINGS:
        step = _proposal(
            column, series, mapping, source=ProposalSource.RULES, limits=limits, groups=len(clusters)
        )
    return SynonymReport(
        column=column,
        non_empty=non_empty,
        distinct=len(counts),
        clusters=tuple(clusters),
        ambiguous=tuple(masked_cut(s, 80) for s in ambiguous),
        step=step,
    )


def check_ai_merge(
    series: pd.Series[Any],
    *,
    column: str,
    groups: Sequence[Sequence[str]],
    canonicals: Sequence[str],
    limits: ChangeShareLimits = DEFAULT_LIMITS,
) -> StepProposal:
    """The rules' check of a merge the AI proposed: every spelling is in the column, each group's canonical is
    one of its spellings, no spelling is in two groups; then measured and held to decision O10.

    Raises `AiMergeRefusedError` with `SYNONYM_MERGE_INVALID`, or `AI_STEP_CHANGES_TOO_MANY` above 60% of the
    column. The proposal is pending, `check`, never ticked, and labelled as the AI's.
    """
    counts, _ = _distinct(series)
    if len(groups) != len(canonicals) or not groups:
        raise AiMergeRefusedError(
            "SYNONYM_MERGE_INVALID", "Each group of spellings needs exactly one spelling to keep."
        )
    mapping: dict[str, str] = {}
    seen: set[str] = set()
    for members, canonical in zip(groups, canonicals, strict=True):
        spelled = [str(m).strip() for m in members]
        if len(spelled) < 2 or canonical not in spelled:
            raise AiMergeRefusedError(
                "SYNONYM_MERGE_INVALID", "A group lists two or more spellings, one of them kept."
            )
        for m in spelled:
            if m not in counts:
                raise AiMergeRefusedError(
                    "SYNONYM_MERGE_INVALID", f"'{column}' has no value spelled as suggested."
                )
            if m in seen:
                raise AiMergeRefusedError("SYNONYM_MERGE_INVALID", "A spelling may be in one group only.")
            seen.add(m)
            if m != canonical:
                mapping[m] = canonical
    if len(mapping) > MAX_MAPPINGS:
        raise AiMergeRefusedError("SYNONYM_MERGE_INVALID", f"A merge lists at most {MAX_MAPPINGS} spellings.")
    return _proposal(column, series, mapping, source=ProposalSource.AI, limits=limits, groups=len(groups))
