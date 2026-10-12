"""Next month's file changed: propose the rename (Plan L follow-up #16, DEC-1597).

A saved treatment - the steps a model was trained with, or the steps Guided setup remembers for a file of
a shape (M159) - names the columns it reads. When next month's export renames one (`amount` becomes
`bill_amount`), replaying it stops with `RECIPE_COLUMN_MISSING`, and the file no longer has the remembered
shape. This module finds, deterministically and without any AI, which new column is most likely the one
that was renamed, by comparing what the training file's profile says about the missing column with what
the new file's profile says about each column that is new in it:

* **Type.** Both must hold the same kind of values (numbers, dates, yes/no, text). An integer column may
  become a decimal one; text never becomes a number.
* **Values.** How many cells are empty, how many distinct values, whether every value is distinct, and -
  for numbers - the average and the range, or - for a category - the most common values. Every number
  is read from the stored `profile.json` (`ColumnProfile`); nothing new is kept.
* **Name.** How close the two names are (`bill_amount` holds every word of `amount`).

A candidate is proposed only when it clearly wins (`match_renames`): the values agree (at least
`STATS_MIN`), and either the names are close (at least `NAME_MIN`) or the values agree so closely on
evidence that says something (numbers or categories, at least `STATS_ALONE`) that the name need not; and
its score beats every other new column of the same kind by at least `MARGIN`. Two close candidates, a
type change, or a name alone are never guessed between: the missing column is listed as unresolved, with
the close candidates named so a person can choose. A new column that clearly wins two missing columns is
proposed for neither.

Nothing here applies anything. The proposal is shown; a person ticks it; the file is then copied with the
column renamed (`POST /uploads/{id}/renamed-copy`) and the saved steps replay on the copy unchanged.

`unseen_values` is information only: for a `map_values` step, the spellings next month's column holds
that neither the training file nor the step's mapping knew (a map leaves them as they are written).
"""

from __future__ import annotations

import difflib
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from pydantic import AwareDatetime, Field

from engine.config import ColumnType, StrictBase
from engine.contracts import ColumnProfile

__all__ = [
    "MARGIN",
    "MAX_RENAMES",
    "NAME_MIN",
    "RENAMED_COPY_FILENAME",
    "STATS_ALONE",
    "STATS_MIN",
    "ColumnRename",
    "FileChanges",
    "RenamedCopy",
    "RenamedCopyRequest",
    "SuggestedRename",
    "UnresolvedColumn",
    "UnseenValues",
    "match_renames",
    "name_similarity",
    "recipe_columns",
    "unseen_values",
    "value_similarity",
]

STATS_MIN: Final[float] = 0.75
"""The values must agree at least this much (0 to 1) for a new column to be proposed at all."""
STATS_ALONE: Final[float] = 0.9
"""Values that agree this much on numbers or categories propose a rename even when the names differ."""
NAME_MIN: Final[float] = 0.5
"""Otherwise the names must be at least this close (0 to 1)."""
MARGIN: Final[float] = 0.1
"""The winner's score must beat every other new column of the same kind by at least this much."""
NAME_WEIGHT: Final[float] = 0.3
"""The score is `(1 - NAME_WEIGHT) * values + NAME_WEIGHT * name`: values decide, the name breaks ties."""
CONTAINED_NAME: Final[float] = 0.8
"""The name score when every word of one name is in the other (`amount`, `bill_amount`)."""
NULL_SPAN: Final[float] = 0.2
"""A difference of this share of empty cells (20 points) or more counts as no agreement on emptiness."""
FEW_DISTINCT: Final[int] = 50
"""Columns with at most this many distinct values compare their counts; others, their share of rows."""
MAX_RENAMES: Final[int] = 20
"""At most this many columns are renamed in one copy."""
MAX_UNSEEN_SHOWN: Final[int] = 10
"""At most this many new spellings are shown per column."""
_VALUE_CHARS: Final[int] = 60
RENAMED_COPY_FILENAME: Final[str] = "renamed_copy.json"
"""Beside a renamed copy's `upload.json`: which upload it copies and which columns it renamed."""

_FAMILY: Final[dict[ColumnType, str]] = {
    ColumnType.INTEGER: "numbers",
    ColumnType.FLOAT: "numbers",
    ColumnType.BOOLEAN: "yes/no values",
    ColumnType.DATE: "dates",
    ColumnType.DATETIME: "dates",
    ColumnType.STRING: "text",
    ColumnType.TEXT: "text",
}


# ---------------------------------------------------------------------------
# What is shown
# ---------------------------------------------------------------------------
class SuggestedRename(StrictBase):
    """One new column that clearly is a column the saved steps read, under a new name."""

    missing: str = Field(description="The column the saved steps read, which this file does not have.")
    candidate: str = Field(description="The column of this file that is new and looks like it.")
    reason: str = Field(description="Why, in one plain sentence, with the numbers compared.")
    score: float = Field(ge=0, le=1, description="The candidate's score, 0 to 1.")
    values: float = Field(ge=0, le=1, description="How much the values agree, 0 to 1.")
    name: float = Field(ge=0, le=1, description="How close the names are, 0 to 1.")


class UnresolvedColumn(StrictBase):
    """A missing column no new column clearly matches; never guessed."""

    missing: str
    candidates: tuple[str, ...] = Field(
        default=(), description="New columns close enough to name, best first; none is chosen for you."
    )
    reason: str


class UnseenValues(StrictBase):
    """New spellings in a column a `map_values` step merges: information only."""

    column: str = Field(description="The column in this file.")
    step: int = Field(description="The `map_values` step's order.")
    values: tuple[str, ...] = Field(
        default=(), description="The most common new spellings, masked; empty for a personal-data column."
    )
    distinct: int = Field(description="How many distinct spellings are new.")
    rows: int = Field(description="How many rows hold one of them.")


class FileChanges(StrictBase):
    """What changed in this file against the file the saved steps were approved on (DEC-1597)."""

    basis: Literal["model", "last_treatment"] = Field(
        description="`model`: the steps saved with the model a file to score is scored with; `last_treatment`: "
        "the steps Guided setup remembers for a file of this shape or of the shape it was before a rename."
    )
    source_upload_id: str | None = Field(default=None, description="The file the steps were approved on.")
    source_file_name: str | None = Field(default=None, description="Its name, while it is kept.")
    suggested_renames: tuple[SuggestedRename, ...] = ()
    unresolved: tuple[UnresolvedColumn, ...] = ()
    unseen_values: tuple[UnseenValues, ...] = ()
    then_same_treatment: bool = Field(
        default=False,
        description="True when a copy with the suggested names has the remembered shape, so Guided setup will "
        "offer the same treatment on it.",
    )
    sentence: str = Field(description="What changed, in plain words.")

    @property
    def empty(self) -> bool:
        return not (self.suggested_renames or self.unresolved or self.unseen_values)


class ColumnRename(StrictBase):
    """One column of a file, and the name the copy gives it."""

    column: str = Field(min_length=1, max_length=300, description="The column as this file names it.")
    new_name: str = Field(min_length=1, max_length=300, description="The name it has in the copy.")


class RenamedCopyRequest(StrictBase):
    """Body of `POST /uploads/{id}/renamed-copy`: the renames a person ticked."""

    renames: tuple[ColumnRename, ...] = Field(min_length=1, max_length=MAX_RENAMES)


class RenamedCopy(StrictBase):
    """`renamed_copy.json`: the copy's lineage. Names and ids only."""

    source_upload_id: str = Field(description="The upload copied; it is left as it is.")
    renames: tuple[ColumnRename, ...]
    requested_by: str | None = Field(default=None, description="Who asked for the copy, when signed in.")
    created_at: AwareDatetime


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------
_CAMEL: Final[re.Pattern[str]] = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SEPARATORS: Final[re.Pattern[str]] = re.compile(r"[^0-9a-z]+")


def _words(name: str) -> tuple[str, ...]:
    return tuple(w for w in _SEPARATORS.split(_CAMEL.sub(" ", str(name)).casefold()) if w)


def name_similarity(a: str, b: str) -> float:
    """How close two column names are, 0 to 1: the better of their spelling's likeness and, when every word
    of one is in the other, `CONTAINED_NAME`. Case, spaces, `_`, `-` and camelCase do not count."""
    words_a, words_b = _words(a), _words(b)
    if not words_a or not words_b:
        return 0.0
    flat_a, flat_b = "".join(words_a), "".join(words_b)
    if flat_a == flat_b:
        return 1.0
    spelling = difflib.SequenceMatcher(None, flat_a, flat_b).ratio()
    contained = set(words_a) <= set(words_b) or set(words_b) <= set(words_a)
    return round(max(spelling, CONTAINED_NAME if contained else 0.0), 4)


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------
def _ratio(a: float, b: float) -> float:
    """`min / max` of two counts or shares (never negative); 1 when both are 0."""
    high = max(a, b)
    return 1.0 if high <= 0 else max(0.0, min(a, b)) / high


def _closeness(a: float, b: float) -> float:
    high = max(abs(a), abs(b))
    return 1.0 if high == 0 else max(0.0, 1.0 - abs(a - b) / high)


def _overlap(low_a: float, high_a: float, low_b: float, high_b: float) -> float:
    union = max(high_a, high_b) - min(low_a, low_b)
    if union <= 0:
        return 1.0 if (low_a, high_a) == (low_b, high_b) else 0.0
    return max(0.0, min(high_a, high_b) - max(low_a, low_b)) / union


@dataclass(frozen=True)
class _Values:
    score: float
    informative: bool
    """True when numbers (average and range) or categories were compared, not only counts."""
    detail: str


def _finite(*values: float | None) -> bool:
    return all(v is not None and math.isfinite(v) for v in values)


def value_similarity(old: ColumnProfile, old_rows: int, new: ColumnProfile, new_rows: int) -> float:
    """How much two columns' profiles agree, 0 to 1 (0 for two kinds of value)."""
    return _value_similarity(old, old_rows, new, new_rows).score


def _value_similarity(old: ColumnProfile, old_rows: int, new: ColumnProfile, new_rows: int) -> _Values:
    if _FAMILY[old.inferred_type] != _FAMILY[new.inferred_type]:
        return _Values(0.0, False, "")
    parts = [1.0 - min(1.0, abs(old.null_rate - new.null_rate) / NULL_SPAN)]
    if max(old.distinct_count, new.distinct_count) <= FEW_DISTINCT:
        parts.append(_ratio(old.distinct_count, new.distinct_count))
    else:
        share_old = old.distinct_count / max(1.0, old_rows * (1.0 - old.null_rate))
        share_new = new.distinct_count / max(1.0, new_rows * (1.0 - new.null_rate))
        parts.append(_ratio(min(share_old, 1.0), min(share_new, 1.0)))
    # Whether the column looks like an ID, not `is_unique`: one repeated amount among 600 flips that.
    parts.append(1.0 if old.looks_like_id == new.looks_like_id else 0.0)
    informative = False
    detail = f"{old.null_rate:.0%} and {new.null_rate:.0%} of cells empty"
    if _finite(old.mean, new.mean, old.minimum, old.maximum, new.minimum, new.maximum):
        assert old.mean is not None and new.mean is not None  # for mypy: `_finite` checked them
        assert old.minimum is not None and old.maximum is not None
        assert new.minimum is not None and new.maximum is not None
        parts.append(_closeness(old.mean, new.mean))
        parts.append(_overlap(old.minimum, old.maximum, new.minimum, new.maximum))
        informative = True
        detail = f"averages of {old.mean:,.4g} and {new.mean:,.4g}, " + detail
    elif (
        old.top_categories
        and new.top_categories
        and max(old.distinct_count, new.distinct_count) <= FEW_DISTINCT
    ):
        # A category's most common values say what it holds; those of a column of mostly different values
        # (amounts written as text) are a handful of chance repeats, and are not compared.
        seen_old = {c.value.strip().casefold() for c in old.top_categories}
        seen_new = {c.value.strip().casefold() for c in new.top_categories}
        shared = seen_old & seen_new
        parts.append(len(shared) / len(seen_old | seen_new))
        informative = True
        detail = f"{len(shared)} of their most common values in common, " + detail
    return _Values(round(sum(parts) / len(parts), 4), informative, detail)


# ---------------------------------------------------------------------------
# The match
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _Scored:
    candidate: ColumnProfile
    values: _Values
    name: float
    score: float

    @property
    def qualifies(self) -> bool:
        if self.values.score < STATS_MIN:
            return False
        return self.name >= NAME_MIN or (self.values.informative and self.values.score >= STATS_ALONE)


def _shown(name: str) -> str:
    from engine.agent.untrusted import display_name

    return display_name(name)


def _unresolved(
    old: ColumnProfile, family: str, best: _Scored | None, scored: Sequence[_Scored]
) -> UnresolvedColumn:
    """Why no new column is proposed for `old`, naming any that comes close by name or values."""
    shown = _shown(old.name)
    if best is None:
        return UnresolvedColumn(
            missing=old.name, reason=f"No column new in this file holds {family}, as '{shown}' did."
        )
    if best.values.score < STATS_MIN:
        named = tuple(s.candidate.name for s in scored if s.name >= NAME_MIN)[:3]
        reason = (
            f"'{_shown(named[0])}' has a name like '{shown}', but its values are not like the ones '{shown}' held."
            if named
            else f"No column new in this file holds {family} like the ones '{shown}' held."
        )
        return UnresolvedColumn(missing=old.name, candidates=named, reason=reason)
    return UnresolvedColumn(
        missing=old.name,
        candidates=(best.candidate.name,),
        reason=f"'{_shown(best.candidate.name)}' holds values somewhat like '{shown}' did, but not enough, with "
        "a name that different, to be sure it is the same column.",
    )


def match_renames(
    missing: Sequence[ColumnProfile],
    candidates: Sequence[ColumnProfile],
    *,
    old_rows: int,
    new_rows: int,
) -> tuple[tuple[SuggestedRename, ...], tuple[UnresolvedColumn, ...]]:
    """For each missing column (from the training file's profile), the new column (from the new file's
    profile) that clearly is it, or why there is none. Pure: the same profiles give the same answer."""
    winners: dict[str, _Scored] = {}
    unresolved: list[UnresolvedColumn] = []
    for old in missing:
        family = _FAMILY[old.inferred_type]
        scored = sorted(
            (
                _Scored(
                    candidate=new,
                    values=(values := _value_similarity(old, old_rows, new, new_rows)),
                    name=(name := name_similarity(old.name, new.name)),
                    score=round((1.0 - NAME_WEIGHT) * values.score + NAME_WEIGHT * name, 4),
                )
                for new in candidates
                if _FAMILY[new.inferred_type] == family
            ),
            key=lambda s: (-s.score, s.candidate.name),
        )
        close = tuple(s.candidate.name for s in scored if s.qualifies)[:3]
        best = scored[0] if scored else None
        runner_up = scored[1].score if len(scored) > 1 else 0.0
        if best is None or not best.qualifies:
            unresolved.append(_unresolved(old, family, best, scored))
            continue
        if best.score - runner_up < MARGIN:
            unresolved.append(
                UnresolvedColumn(
                    missing=old.name,
                    candidates=close or (best.candidate.name, scored[1].candidate.name),
                    reason=f"More than one new column looks like '{_shown(old.name)}'; choose the one it is.",
                )
            )
            continue
        winners[old.name] = best
    taken: dict[str, list[str]] = {}
    for missing_name, best in winners.items():
        taken.setdefault(best.candidate.name, []).append(missing_name)
    suggested: list[SuggestedRename] = []
    for old in missing:
        best = winners.get(old.name)
        if best is None:
            continue
        if len(taken[best.candidate.name]) > 1:
            unresolved.append(
                UnresolvedColumn(
                    missing=old.name,
                    candidates=(best.candidate.name,),
                    reason=f"'{_shown(best.candidate.name)}' looks like more than one missing column; choose "
                    "which one it is.",
                )
            )
            continue
        suggested.append(
            SuggestedRename(
                missing=old.name,
                candidate=best.candidate.name,
                score=best.score,
                values=best.values.score,
                name=best.name,
                reason=(
                    f"Your file has no '{_shown(old.name)}' but has '{_shown(best.candidate.name)}', which is "
                    f"new. Both hold {_FAMILY[old.inferred_type]}, with {best.values.detail}"
                    + (", and the names are close." if best.name >= NAME_MIN else ".")
                ),
            )
        )
    order = {old.name: i for i, old in enumerate(missing)}
    unresolved.sort(key=lambda u: order.get(u.missing, len(order)))
    return tuple(suggested), tuple(unresolved)


# ---------------------------------------------------------------------------
# Which columns the saved steps read
# ---------------------------------------------------------------------------
def recipe_columns(steps: Iterable[Any]) -> tuple[frozenset[str], frozenset[str]]:
    """The file columns `steps` read, and among them the columns an `unpivot` step turns into rows.

    A month's column (`2024_09_spend`) is expected to change from file to file (M156): it is never
    proposed as a rename of another month's.
    """
    from engine.agent.contracts import RecipeStepKind
    from engine.agent.latest import KeepLatestError, KeepLatestSpec
    from engine.agent.recipe import RecipeError, derive_names
    from engine.agent.reshape import CombineError, CombineSpec
    from engine.agent.unpivot import UnpivotError, UnpivotSpec

    reads: set[str] = set()
    months: set[str] = set()
    for step in steps:
        params = dict(step.params)
        reads.add(str(step.column))
        if isinstance(params.get("key_column"), str):
            reads.add(str(params["key_column"]))
        try:
            if step.kind is RecipeStepKind.DERIVE:
                reads |= set(derive_names(str(params.get("expression", ""))))
            elif step.kind is RecipeStepKind.COMBINE_ROWS:
                reads |= set(CombineSpec.from_params(step.column, params).reads)
            elif step.kind is RecipeStepKind.KEEP_LATEST_PER_ID:  # Plan O M238 (DEC-1808)
                reads |= set(KeepLatestSpec.from_params(step.column, params).reads)
            elif step.kind is RecipeStepKind.UNPIVOT:
                spec = UnpivotSpec.from_params(step.column, params)
                months |= {v.column for v in spec.value_columns}
                reads |= set(spec.reads)
        except (RecipeError, CombineError, UnpivotError, KeepLatestError):
            continue
    return frozenset(reads), frozenset(months)


# ---------------------------------------------------------------------------
# New spellings
# ---------------------------------------------------------------------------
def _spelling(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _present(value: Any) -> bool:
    if value is None:
        return False
    try:
        return not bool(value != value)
    except (TypeError, ValueError):
        return True


def unseen_values(
    steps: Iterable[Any],
    new: Any,
    old: Any,
    *,
    renames: Mapping[str, str] | None = None,
    personal: frozenset[str] = frozenset(),
) -> tuple[UnseenValues, ...]:
    """For each `map_values` step, the spellings `new` (next month's frame) holds in its column that neither
    `old` (the training frame) nor the step's mapping holds - compared trimmed, as `map_values` compares.

    `renames` maps a column the step reads to its name in `new` (a rename proposed or made). A column of
    personal data is counted, never quoted.
    """
    from engine.agent.contracts import RecipeStepKind
    from engine.agent.formats import masked_cut

    found: list[UnseenValues] = []
    for step in steps:
        if step.kind is not RecipeStepKind.MAP_VALUES:
            continue
        column = (renames or {}).get(step.column, step.column)
        if column not in new.columns or step.column not in old.columns:
            continue
        mapping = dict(step.params.get("mapping") or {})
        known = {str(k).strip() for k in mapping} | {str(v).strip() for v in mapping.values()}
        known |= {_spelling(v) for v in old[step.column] if _present(v)}
        counts: dict[str, int] = {}
        for value in new[column]:
            if _present(value) and (text := _spelling(value)) not in known:
                counts[text] = counts.get(text, 0) + 1
        if not counts:
            continue
        ranked = sorted(counts, key=lambda v: (-counts[v], v))[:MAX_UNSEEN_SHOWN]
        found.append(
            UnseenValues(
                column=column,
                step=step.order,
                values=() if column in personal else tuple(masked_cut(v, _VALUE_CHARS) for v in ranked),
                distinct=len(counts),
                rows=sum(counts.values()),
            )
        )
    return tuple(found)
