"""Mapping suggestion (M9, Phase 2 plan section 6.1): the client's column names become ours.

A client ships `CUST_ID`, `TNR_MNTHS`, `Plan Type` and `DND_FLAG`; the engine builds against
`entity_key`, `tenure_months`, `plan_type` and `marketing_opt_in`. This module proposes which of
theirs means which of ours, how confident it is, and what transform closes the gap. It proposes
only: an auto-accepted column is still shown and still reversible (the package's "suggest, never
decide silently" rule), and `suggested_mapping_spec` returns a `MappingSpec` nobody has saved yet.

**Two kinds of column, recognised two different ways.** Most standard columns are recognised by
*name*: a use case's `StandardColumn` carries every spelling a client might have used, so an alias
match, a fuzzy name score, type compatibility, value overlap and range plausibility together say
how likely `TNR_MNTHS` is to be `tenure_months`. The entity key and the event time are not
recognisable that way - a key is `CUST_ID`, `ACCT_NO`, `MSISDN` or whatever the client's billing
system called it, and no alias list can cover that - so they are settled from the *shape* of the
data instead, out of the `key_candidates` and `time_candidates` `SourceProfile` already measured.
That is also why they can never be suggested as an ordinary column: the source column that becomes
the key is claimed before the name-scoring loop runs, so it is never offered as a feature too.

**Why the scores are summed and clipped rather than averaged.** Each signal contributes its weight
times its strength, and an exact alias match alone is worth 1.00 - it saturates the score, because
a client who spelled a column exactly the way the use case says they might has told us what it is
and nothing else needs to vote. Averaging would drag that certainty back down for a column whose
type we happen not to know, which is how a confident, correct match ends up under the
auto-accept bar and in front of a user who has nothing useful to decide.

**Why the suggester is a protocol.** Phase 3 adds an LLM suggester behind the same `suggest`, and
it will see only what this one sees - column names, types and the handful of sample values the
Phase 1 profiler already redacted for PII - because a suggester that needed the raw table would be
a second copy of the client's data in a second place.

Nothing here branches on a use case or a client: the aliases, the value vocabularies, the ranges
and the two confidence thresholds all arrive as configuration. No data value is ever logged; the
values that appear in a check message are on the user's own screen, about the user's own file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, Protocol

from engine.config import ColumnType, get_roles
from engine.contracts import Severity
from engine.onboarding.specs import (
    ColumnTransform,
    DecidedBy,
    MappingColumn,
    MappingSpec,
    OnboardingCheck,
    StandardColumn,
    StandardSchemaConfig,
    StandardType,
    TransformKind,
)
from engine.onboarding.transforms import apply_transform
from engine.stages.ingest import REDACTED
from engine.utils.time import utc_now

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    import pandas as pd

    from engine.config import RoleSpec, UseCaseConfig
    from engine.contracts import ColumnProfile
    from engine.onboarding.specs import SourceProfile

__all__ = [
    "ENTITY_KEY",
    "EVENT_TIME",
    "MAX_ALTERNATIVES",
    "TYPE_CONFLICT_FRACTION",
    "WEIGHT_ALIAS",
    "WEIGHT_NAME",
    "WEIGHT_RANGE",
    "WEIGHT_TYPE",
    "WEIGHT_VALUES",
    "HeuristicMappingSuggester",
    "MappingCandidate",
    "MappingResult",
    "MappingSuggester",
    "alias_match",
    "apply_mapping",
    "name_similarity",
    "range_plausibility",
    "required_standard_columns",
    "suggested_mapping_spec",
    "type_compatibility",
    "value_overlap",
]


# ---------------------------------------------------------------------------
# The two names the engine owns
# ---------------------------------------------------------------------------
ENTITY_KEY: Final[str] = "entity_key"
EVENT_TIME: Final[str] = "event_time"
"""The standard names every role's `required_columns` is written in, and the names a mapped table
carries into DuckDB (`engine.onboarding.features` and `engine.onboarding.labels` join on exactly
these). They are engine vocabulary, not a use case's: a client never sees them and a use-case YAML
cannot rename them."""

_SHAPE_IDENTIFIED: Final[frozenset[str]] = frozenset({ENTITY_KEY, EVENT_TIME})


# ---------------------------------------------------------------------------
# Signal weights (plan section 6.1)
# ---------------------------------------------------------------------------
WEIGHT_ALIAS: Final[float] = 1.00
WEIGHT_NAME: Final[float] = 0.60
WEIGHT_TYPE: Final[float] = 0.25
WEIGHT_VALUES: Final[float] = 0.40
WEIGHT_RANGE: Final[float] = 0.10

MAX_ALTERNATIVES: Final[int] = 3
"""Runners-up carried on a candidate. A number, rather than every column that scored above nothing,
because the mapping screen offers a short list the user picks from and a long one is the same as
none."""

TYPE_CONFLICT_FRACTION: Final[float] = 0.05
"""Share of a column's non-null values that may fail their transform before MAPPING_TYPE_CONFLICT
fires (plan section 7). Measured over non-null values only - see `transforms.TransformResult`."""

_LISTED_VALUES: Final[int] = 5
"""How many uncovered values VALUE_UNMAPPED spells out; `details` always carries the whole list."""


# ---------------------------------------------------------------------------
# Name normalisation
# ---------------------------------------------------------------------------
_SEPARATORS: Final[re.Pattern[str]] = re.compile(r"[\s_.\-]+")


def _normalise(name: str) -> str:
    """`TNR_MNTHS` -> `tnr mnths`: lower case, one space wherever the client put a separator.

    Two spellings come out of this one function. The spaced form is what the fuzzy score compares,
    because `token_set_ratio` needs tokens to set-compare; `_compact` removes the spaces again for
    the exact-alias test, so `TNR_MNTHS`, `tnr-mnths` and `Tnr Mnths` are all the alias `tnr_mnths`.
    A client's capitalisation and choice of separator are never a fact about what a column means.
    """
    return _SEPARATORS.sub(" ", name.strip().lower()).strip()


def _compact(name: str) -> str:
    return _normalise(name).replace(" ", "")


# ---------------------------------------------------------------------------
# The five signals, one small function each
# ---------------------------------------------------------------------------
def alias_match(source: str, standard: str, aliases: Sequence[str]) -> float:
    """1.0 when the source column *is* one of the names this standard column is known by."""
    known = {_compact(standard), *(_compact(alias) for alias in aliases)}
    return 1.0 if _compact(source) in known else 0.0


def name_similarity(source: str, standard: str, aliases: Sequence[str]) -> float:
    """Best `token_set_ratio` of the source name against the standard name and every alias, 0 to 1.

    Token-set rather than a character ratio because a client's name is usually ours plus or minus a
    word - `customer_plan_type` against `plan_type` - which a character ratio punishes for the
    length difference and a set comparison does not.
    """
    from rapidfuzz.fuzz import token_set_ratio

    normalised = _normalise(source)
    return max(token_set_ratio(normalised, _normalise(name)) for name in (standard, *aliases)) / 100.0


_NUMERIC_COLUMN_TYPES: Final[frozenset[ColumnType]] = frozenset({ColumnType.INTEGER, ColumnType.FLOAT})

_CASTABLE: Final[Mapping[StandardType, frozenset[ColumnType]]] = MappingProxyType(
    {
        StandardType.NUMERIC: frozenset({ColumnType.INTEGER, ColumnType.FLOAT}),
        StandardType.BOOLEAN: frozenset({ColumnType.BOOLEAN, ColumnType.INTEGER, ColumnType.STRING}),
        StandardType.DATE: frozenset({ColumnType.DATE, ColumnType.DATETIME, ColumnType.STRING}),
        StandardType.CATEGORICAL: frozenset({ColumnType.STRING, ColumnType.TEXT, ColumnType.BOOLEAN}),
        StandardType.TEXT: frozenset(ColumnType),
    }
)
"""Which inferred types can honestly become which standard type. Deliberately not "everything casts
to text and text casts to everything": a date column read as a number is a mapping mistake the type
signal exists to make less likely, so a cast that would technically succeed but destroy the meaning
scores nothing."""


def type_compatibility(column: ColumnProfile, to: StandardType, *, max_levels: int) -> float:
    """1.0 when this column's values can become `to` without losing what they mean.

    A number can stand for a category - a plan shipped as 1/2/3 - but only while it has few enough
    distinct values to *be* categories; above `max_levels` it is a measurement, and reading it as a
    category turns a feature into an id. That is the one rule `_CASTABLE` cannot express, because it
    depends on the column's data rather than on its type.
    """
    if to is StandardType.CATEGORICAL and column.inferred_type in _NUMERIC_COLUMN_TYPES:
        return 1.0 if column.distinct_count <= max_levels else 0.0
    return 1.0 if column.inferred_type in _CASTABLE[to] else 0.0


def value_overlap(column: ColumnProfile, value_aliases: Mapping[str, Sequence[str]]) -> float:
    """Jaccard of the standard values this column's own values resolve to against the declared set.

    Compared after resolution rather than raw, because an alias list is long on purpose - a raw
    Jaccard of `{Y, N}` against a fourteen-token vocabulary would report 0.14 for a perfect match
    and the signal would never fire. Resolved, a column holding exactly the expected values scores
    1.0, a column holding half of them plus one the vocabulary has never heard of scores in between,
    and a column from another vocabulary scores 0.
    """
    observed = _observed_values(column)
    if not observed or not value_aliases:
        return 0.0
    lookup = _alias_lookup(value_aliases)
    resolved = {lookup.get(_compact(value), _compact(value)) for value in observed}
    declared = set(value_aliases)
    return len(resolved & declared) / len(resolved | declared)


def range_plausibility(column: ColumnProfile, bounds: tuple[float, float]) -> float:
    """1.0 when the column's extremes both sit inside the range the standard column declares.

    Binary rather than a share, because the profile carries only the extremes: there is no measured
    middle ground to report, and inventing one from the mean would be a number nobody counted.
    """
    if column.minimum is None or column.maximum is None:
        return 0.0
    low, high = bounds
    return 1.0 if low <= column.minimum and column.maximum <= high else 0.0


# ---------------------------------------------------------------------------
# Value vocabularies
# ---------------------------------------------------------------------------
def _observed_values(column: ColumnProfile) -> tuple[str, ...]:
    """The distinct values the profiler saw, or nothing at all for a column it redacted for PII."""
    if column.top_categories:
        return tuple(category.value for category in column.top_categories)
    return tuple(value for value in column.sample_values if value != REDACTED)


def _alias_lookup(value_aliases: Mapping[str, Sequence[str]]) -> dict[str, str]:
    return {
        _compact(spelling): standard
        for standard, aliases in value_aliases.items()
        for spelling in (standard, *aliases)
    }


_NEGATION_MARKERS: Final[frozenset[str]] = frozenset(
    {
        "not",
        "non",
        "never",
        "dnd",
        "dnc",
        "optout",
        "unsubscribe",
        "unsubscribed",
        "suppress",
        "suppressed",
        "blocked",
        "denied",
        "disabled",
        "inactive",
        "excluded",
    }
)
"""English words that turn a flag's name around. Generic vocabulary, not any client's or any use
case's: `DND_FLAG` and `marketing_opt_in` are the same fact stated in opposite directions, and the
only thing that says so is the name, because the values (`Y`/`N`) are identical either way."""


def _is_inverted(source: str, standard: str, definition: StandardColumn) -> bool:
    """True when exactly one of the two names is phrased negatively, so the flag needs `negate`.

    Exactly one, not simply the source: a client's `DNC_FLAG` mapped onto a standard `do_not_call`
    already agrees, and negating it would silently invert every row of a correct column.
    """
    if definition.type is not StandardType.BOOLEAN:
        return False
    return _negated(source) != _negated(standard)


def _negated(name: str) -> bool:
    normalised = _normalise(name)
    return _compact(name) in _NEGATION_MARKERS or bool(set(normalised.split()) & _NEGATION_MARKERS)


def _value_map(definition: StandardColumn, column: ColumnProfile) -> dict[str, Any]:
    """The values this column actually holds, each mapped to the standard value it spells.

    Keyed by the raw value as the file holds it, because that is what `transforms.apply_transform`
    looks up. A value the vocabulary does not cover is deliberately left out rather than guessed at:
    it comes back from the transform as `unmapped`, and VALUE_UNMAPPED puts it in front of the user.
    """
    lookup = _alias_lookup(definition.value_aliases)
    boolean = definition.type is StandardType.BOOLEAN
    mapped: dict[str, Any] = {}
    for value in _observed_values(column):
        standard = lookup.get(_compact(value))
        if standard is not None:
            mapped[value] = standard.lower() == "true" if boolean else standard
    return mapped


def _transform_for(
    standard: str, definition: StandardColumn | None, column: ColumnProfile
) -> ColumnTransform | None:
    if standard == EVENT_TIME:
        return ColumnTransform(kind=TransformKind.CAST, to_type=StandardType.DATE)
    if definition is None:
        return None
    if _is_inverted(column.name, standard, definition):
        return ColumnTransform(kind=TransformKind.NEGATE)
    if definition.value_aliases:
        mapped = _value_map(definition, column)
        if mapped:
            return ColumnTransform(kind=TransformKind.VALUE_MAP, value_map=mapped, unmapped="keep")
    return ColumnTransform(kind=TransformKind.CAST, to_type=definition.type)


# ---------------------------------------------------------------------------
# The candidate and the protocol
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MappingCandidate:
    """One proposal: this source column means that standard column, this well, this way.

    `reasons` are the signals that fired, strongest first, in the words the mapping screen shows;
    `alternatives` are the runners-up still free to be chosen, so a user who disagrees picks from a
    list instead of scrolling the whole file.
    """

    standard: str
    source: str
    confidence: float
    transform: ColumnTransform | None
    reasons: tuple[str, ...] = ()
    alternatives: tuple[str, ...] = ()


class MappingSuggester(Protocol):
    """Everything the mapping screen needs from whatever proposes a mapping.

    Named as a protocol because Phase 3's LLM suggester has to be droppable in behind it without a
    line of the build path changing, and because the arguments here are the whole of what such a
    suggester may see: a profile (names, types, counts and redacted samples), the schema it is
    aiming at, and the role the source was confirmed as - never the client's table.
    """

    def suggest(
        self, profile: SourceProfile, schema: StandardSchemaConfig, role: RoleSpec
    ) -> tuple[MappingCandidate, ...]: ...


def _targets(schema: StandardSchemaConfig, role: RoleSpec) -> dict[str, StandardColumn | None]:
    """The standard columns a source in this role may fill, in the order the screen lists them.

    An entity source carries the use case's own one-row-per-entity shape; an event source carries
    the columns `configs/roles.yaml` says that kind of event log has. Both start with the role's
    required columns. A name with no `StandardColumn` behind it - `event_time`, or a role-typical
    column like `amount` - is still a target; it simply has fewer signals to be recognised by.
    """
    names = [*role.required_columns]
    names += [*role.typical_names] if role.is_event else [*schema.column_names]
    names += [*role.optional_columns]
    # A role-typical column's definition lives on the ROLE, not on the use case: `amount` means the
    # same thing, and is spelled `AMT` or `bill_amt` just as often, whichever client's bills these
    # are. Falling back to the use case's schema keeps an entity source reading its own columns.
    return {
        name: (role.typical(name) if role.is_event else None) or schema.by_name(name)
        for name in dict.fromkeys(names)
    }


@dataclass(frozen=True)
class HeuristicMappingSuggester:
    """The shipped suggester: five weighted name, type and value signals, greedily assigned.

    Both settings are `onboarding.mapping`'s (`suggest_confidence` and
    `max_value_levels_for_value_mapping`) and neither has a default, so every caller states which
    configuration it is suggesting under rather than inheriting a number written here.

    `suggest_confidence` is applied while assigning and not afterwards, which matters: a source
    column that faintly resembles a standard column nobody has a column for would otherwise be
    handed to it and be gone by the time the column that really wanted it came up.
    """

    suggest_confidence: float
    max_categorical_levels: int

    def suggest(
        self, profile: SourceProfile, schema: StandardSchemaConfig, role: RoleSpec
    ) -> tuple[MappingCandidate, ...]:
        """Every standard column this source can fill, best source column each, nothing claimed twice.

        Two passes. The entity key and the event time go first, from the profile's own ranked
        candidates, and take their source columns out of circulation - which is the whole of "never
        suggest the key or the event time as a feature column". Then every remaining (standard,
        source) pair is scored and assigned highest-first: when two standard columns want one source
        column the higher score keeps it and the loser is simply still in the list, re-offered
        against whatever is left, which is the same thing as re-running it.
        """
        columns = {column.name: column for column in profile.profile.columns}
        targets = _targets(schema, role)
        chosen: dict[str, MappingCandidate] = {}
        taken: set[str] = set()

        for standard in targets:
            if standard in _SHAPE_IDENTIFIED:
                candidate = _shape_candidate(profile, standard, taken)
                if candidate is not None:
                    chosen[standard] = candidate
                    taken.add(candidate.source)

        ranked: dict[str, list[tuple[float, str, tuple[str, ...]]]] = {
            standard: self._ranked_sources(profile, standard, definition, taken)
            for standard, definition in targets.items()
            if standard not in _SHAPE_IDENTIFIED
        }
        pairs = [
            (score, source, standard, why) for standard, rows in ranked.items() for score, source, why in rows
        ]
        for score, source, standard, why in sorted(pairs, key=lambda pair: (-pair[0], pair[2], pair[1])):
            if standard in chosen or source in taken:
                continue
            chosen[standard] = MappingCandidate(
                standard=standard,
                source=source,
                confidence=round(score, 4),
                transform=_transform_for(standard, targets[standard], columns[source]),
                reasons=why,
            )
            taken.add(source)

        return tuple(
            (
                chosen[standard]
                if standard in _SHAPE_IDENTIFIED
                else _with_alternatives(chosen[standard], ranked[standard], taken)
            )
            for standard in targets
            if standard in chosen
        )

    def _ranked_sources(
        self,
        profile: SourceProfile,
        standard: str,
        definition: StandardColumn | None,
        taken: set[str],
    ) -> list[tuple[float, str, tuple[str, ...]]]:
        scored: list[tuple[float, str, tuple[str, ...]]] = []
        for column in profile.profile.columns:
            if column.name in taken:
                continue
            score, why = _score(column, standard, definition, max_levels=self.max_categorical_levels)
            if score >= self.suggest_confidence:
                scored.append((score, column.name, why))
        return sorted(scored, key=lambda row: (-row[0], row[1]))


def _with_alternatives(
    candidate: MappingCandidate, ranked: list[tuple[float, str, tuple[str, ...]]], taken: set[str]
) -> MappingCandidate:
    alternatives = tuple(source for _, source, _ in ranked if source not in taken)[:MAX_ALTERNATIVES]
    return MappingCandidate(
        standard=candidate.standard,
        source=candidate.source,
        confidence=candidate.confidence,
        transform=candidate.transform,
        reasons=candidate.reasons,
        alternatives=alternatives,
    )


def _score(
    column: ColumnProfile, standard: str, definition: StandardColumn | None, *, max_levels: int
) -> tuple[float, tuple[str, ...]]:
    aliases = definition.aliases if definition is not None else ()
    total = 0.0
    reasons: list[str] = []

    if alias_match(column.name, standard, aliases):
        total += WEIGHT_ALIAS
        reasons.append(f"'{column.name}' is one of the names we know '{standard}' by")
    similarity = name_similarity(column.name, standard, aliases)
    if similarity:
        total += WEIGHT_NAME * similarity
        reasons.append(f"its name reads {similarity:.0%} like '{standard}' or one of its aliases")

    if definition is not None:
        overlap = value_overlap(column, definition.value_aliases)
        if overlap:
            total += WEIGHT_VALUES * overlap
            reasons.append(f"its values line up {overlap:.0%} with the ones '{standard}' expects")
        if type_compatibility(column, definition.type, max_levels=max_levels):
            total += WEIGHT_TYPE
            reasons.append(f"{column.inferred_type.value} values can be read as {definition.type.value}")
        if definition.range is not None and range_plausibility(column, definition.range):
            total += WEIGHT_RANGE
            low, high = definition.range
            reasons.append(f"its values fall inside the expected {low:g} to {high:g}")

    return min(total, 1.0), tuple(reasons)


def _shape_candidate(profile: SourceProfile, standard: str, taken: set[str]) -> MappingCandidate | None:
    """The entity key or the event time, taken from what the profiler measured about the data.

    Neither is recognisable by name - one client's key is `CUST_ID` and the next one's is `MSISDN` -
    so `SourceProfile` already ranks them by shape: `key_candidates` by null rate, id-like name and
    rows-per-value, `time_candidates` by the share of values that parse as a date. The confidence
    here is that same measurement and not a second opinion about it, which is why an entity key that
    is null on a tenth of the rows arrives at 0.9 and in front of the user rather than auto-accepted.
    """
    if standard == ENTITY_KEY:
        keys = [key for key in profile.key_candidates if key.column not in taken]
        if not keys:
            return None
        best = keys[0]
        return MappingCandidate(
            standard=ENTITY_KEY,
            source=best.column,
            confidence=round(1.0 - best.null_rate, 4),
            transform=None,
            reasons=(
                f"it identifies rows: {best.distinct_count:,} distinct values, "
                f"{best.rows_per_key:.1f} rows each",
                f"it is filled in on {1.0 - best.null_rate:.0%} of rows",
            ),
            alternatives=tuple(key.column for key in keys[1 : 1 + MAX_ALTERNATIVES]),
        )
    times = [time for time in profile.time_candidates if time.column not in taken]
    if not times:
        return None
    best_time = times[0]
    return MappingCandidate(
        standard=EVENT_TIME,
        source=best_time.column,
        confidence=round(best_time.parse_rate, 4),
        transform=ColumnTransform(kind=TransformKind.CAST, to_type=StandardType.DATE),
        reasons=(
            f"{best_time.parse_rate:.0%} of its values read as a date",
            f"they run from {best_time.earliest} to {best_time.latest}",
        ),
        alternatives=tuple(time.column for time in times[1 : 1 + MAX_ALTERNATIVES]),
    )


# ---------------------------------------------------------------------------
# Applying a mapping
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MappingResult:
    """A mapped table: standard names only, what the mapping says about itself, and what it left.

    `dropped` is the source columns that did not survive. It is returned rather than logged because
    "the engine quietly ignored eleven of your columns" is the single most expensive thing a
    mapping screen can fail to say.
    """

    frame: pd.DataFrame
    checks: tuple[OnboardingCheck, ...]
    dropped: tuple[str, ...]


def apply_mapping(
    frame: pd.DataFrame,
    mapping: MappingSpec,
    *,
    schema: StandardSchemaConfig,
    auto_accept_confidence: float,
) -> MappingResult:
    """Rename and transform `frame` into the standard shape `mapping` describes.

    The output is built column by column from the mapping rather than by renaming the input, so a
    source column nobody mapped cannot reach the build: there is no path by which it ends up in the
    frame and then has to be remembered about and removed again. Pure and repeatable - the same
    frame and the same mapping give the same frame and the same checks every time, which is what
    lets next month's file be built without a human in the loop.

    `auto_accept_confidence` is `onboarding.mapping.auto_accept_confidence`, passed in rather than
    read here, because this function is given data and a mapping and never a use case.
    """
    import pandas as pd

    built: dict[str, pd.Series[Any]] = {}
    checks: list[OnboardingCheck] = []
    for column in mapping.columns:
        source = frame[column.source]
        if column.transform is None:
            built[column.standard] = source
        else:
            result = apply_transform(source, column.transform, column=column.source)
            built[column.standard] = result.values
            if result.total and result.failed / result.total > TYPE_CONFLICT_FRACTION:
                checks.append(
                    _type_conflict(column, source, result.values, result.failed, result.total, schema)
                )
            if result.unmapped:
                checks.append(_value_unmapped(column, result.unmapped))
        if column.confidence < auto_accept_confidence:
            checks.append(_low_confidence(column, auto_accept_confidence))

    mapped = set(mapping.source_names)
    return MappingResult(
        frame=pd.DataFrame(built, index=frame.index, columns=list(built)),
        checks=tuple(checks),
        dropped=tuple(sorted(str(name) for name in frame.columns if str(name) not in mapped)),
    )


def _wanted(column: MappingColumn, schema: StandardSchemaConfig) -> str:
    definition = schema.by_name(column.standard)
    if definition is not None:
        return definition.type.value
    return column.transform.to_type.value if column.transform and column.transform.to_type else "usable"


def _type_conflict(
    column: MappingColumn,
    source: pd.Series[Any],
    values: pd.Series[Any],
    failed: int,
    total: int,
    schema: StandardSchemaConfig,
) -> OnboardingCheck:
    share = failed / total
    example = str(source[source.notna() & values.isna()].iloc[0])
    return OnboardingCheck(
        code="MAPPING_TYPE_CONFLICT",
        severity=Severity.ERROR,
        message=(
            f"{share:.1%} of the values in '{column.source}' ({failed:,} of {total:,}) could not be "
            f"read as {_wanted(column, schema)} for '{column.standard}'; one of them is '{example}'."
        ),
        suggestion=(
            f"Correct those values in the source file, or map '{column.standard}' to a different column."
        ),
        column=column.source,
        details={
            "standard": column.standard,
            "failed": failed,
            "total": total,
            "failed_fraction": round(share, 4),
            "example": example,
        },
    )


def _value_unmapped(column: MappingColumn, unmapped: tuple[str, ...]) -> OnboardingCheck:
    listed = ", ".join(f"'{value}'" for value in unmapped[:_LISTED_VALUES])
    more = f" and {len(unmapped) - _LISTED_VALUES:,} more" if len(unmapped) > _LISTED_VALUES else ""
    return OnboardingCheck(
        code="VALUE_UNMAPPED",
        severity=Severity.WARNING,
        message=(
            f"'{column.source}' holds {len(unmapped):,} value(s) the value map for "
            f"'{column.standard}' does not cover: {listed}{more}."
        ),
        suggestion="Add each one to the value map, or say whether to keep it as it is or blank it out.",
        column=column.source,
        details={"standard": column.standard, "values": list(unmapped)},
        acknowledgeable=True,
    )


def _low_confidence(column: MappingColumn, threshold: float) -> OnboardingCheck:
    return OnboardingCheck(
        code="MAPPING_LOW_CONFIDENCE",
        severity=Severity.WARNING,
        message=(
            f"'{column.source}' was read as '{column.standard}' with {column.confidence:.0%} "
            f"confidence, below the {threshold:.0%} needed to accept a match without review."
        ),
        suggestion=(
            f"Confirm '{column.source}' on the mapping screen, or choose a different column for "
            f"'{column.standard}'."
        ),
        column=column.source,
        details={"standard": column.standard, "confidence": column.confidence, "threshold": threshold},
        acknowledgeable=True,
    )


# ---------------------------------------------------------------------------
# The entry point the mapping screen calls
# ---------------------------------------------------------------------------
def suggested_mapping_spec(
    profile: SourceProfile,
    config: UseCaseConfig,
    *,
    role: str,
    use_case: str,
    mapping_id: str,
) -> MappingSpec:
    """A complete, unsaved `MappingSpec` for one source: what we would do, for the user to change.

    A candidate below `suggest_confidence` is not offered at all - a guess nobody can act on costs
    the user a decision and buys nothing - and one at or above `auto_accept_confidence` is marked
    `AUTO`, which means "we filled this in", never "this is settled": the screen shows every column
    either way and the user overrides any of them.
    """
    catalogue = get_roles()
    role_spec = catalogue.require(role)
    schema = config.standard_schema
    defaults = config.onboarding.mapping

    candidates = HeuristicMappingSuggester(
        suggest_confidence=defaults.suggest_confidence,
        max_categorical_levels=defaults.max_value_levels_for_value_mapping,
    ).suggest(profile, schema, role_spec)
    claimed = {candidate.source for candidate in candidates}
    filled = {candidate.standard for candidate in candidates}

    return MappingSpec(
        mapping_id=mapping_id,
        client_id=profile.client_id,
        source_id=profile.source_id,
        use_case=use_case,
        role=role,
        columns=tuple(
            MappingColumn(
                source=candidate.source,
                standard=candidate.standard,
                transform=candidate.transform,
                confidence=candidate.confidence,
                decided_by=(
                    DecidedBy.AUTO
                    if candidate.confidence >= defaults.auto_accept_confidence
                    else DecidedBy.USER
                ),
            )
            for candidate in candidates
        ),
        unmapped_source=tuple(
            sorted(column.name for column in profile.profile.columns if column.name not in claimed)
        ),
        missing_required=tuple(sorted(_required(schema, role_spec) - filled)),
        value_maps={
            candidate.standard: dict(candidate.transform.value_map)
            for candidate in candidates
            if candidate.transform is not None and candidate.transform.kind is TransformKind.VALUE_MAP
        },
        created_at=utc_now(),
    )


def _required(schema: StandardSchemaConfig, role: RoleSpec) -> set[str]:
    """Standard columns this role must produce and cannot compute for itself.

    A column with a `derivable` is not missing when nobody mapped it - the build computes it from
    the columns that were mapped - so listing it would send the user looking for a column their
    file is not expected to have.
    """
    required = set(role.required_columns)
    if not role.is_event:
        required |= {column.name for column in schema.required_columns if column.derivable is None}
    return required


def required_standard_columns(schema: StandardSchemaConfig, role: RoleSpec) -> frozenset[str]:
    """The public face of `_required`: what a mapping for `role` must fill, and nothing derivable.

    `engine.onboarding.replay` copies last month's mapping onto this month's file and has to say
    which standard columns are missing again when a source column disappears; asking this rule rather
    than keeping a second one is what stops a replayed mapping and a suggested one disagreeing about
    what "required" means.
    """
    return frozenset(_required(schema, role))
