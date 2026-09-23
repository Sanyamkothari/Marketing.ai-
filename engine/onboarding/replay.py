"""Next month's tables through last month's recipe (Plan A M35, Phase 2 plan section 6.6).

A trained model scores rows shaped exactly like the rows it was fitted on, and the only way to get
those from a new extract is to run it through the same decisions: the same role for each table, the
same column mapping, the same transforms and value maps, the same features and snapshot rule. That
recipe is already saved (`OnboardingSpec` and its `MappingSpec`s); what changes from one month to
the next is only *which files* it reads. Replaying is therefore a pure re-pointing, done here:

* each of the saved recipe's sources is matched to one of this month's files - by the role the user
  confirmed on the new file, then by the same file name, then by the role the detector ranked first
  for it - so "customers.csv came back as customers.csv" needs no decision at all;
* each matched file gets a copy of last time's mapping under a fresh id, pointed at the new file;
* every mapped column the new file no longer has is taken out of that copy and reported as
  **missing**, and the source is the one - and the only one - the mapping step has to reopen for.
  The rest of the recipe is never shown again (plan section 8, "No mapping screen, no confidence
  pills, no decisions").

A column the new file *adds* is left unmapped, exactly as a column nobody mapped was left out the
first time (`docs/ONBOARDING.md` section 3): a replay that quietly started reading a new column would
be a different recipe wearing the old one's name.

Nothing here reads a row. The column list a match is judged on is the one `SourceSpec.columns`
recorded at upload, so a replay costs nothing to plan, and the build that follows reads the files
exactly as a first build does. Nothing branches on a use case, a client or a role name: the matching
rule reads roles and names, and the required-column rule is `engine.onboarding.mapping`'s own.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import Field

from engine.contracts import Artefact
from engine.onboarding.mapping import required_standard_columns
from engine.onboarding.specs import MappingColumn, MappingSpec, OnboardingSpec, SourceSpec
from engine.utils.time import utc_now

if TYPE_CHECKING:
    from engine.config import RoleCatalogue, StandardSchemaConfig

__all__ = ["ReplayPlan", "ReplayedSource", "UnmatchedSource", "plan_replay", "replayed_spec"]


class ReplayedSource(Artefact):
    """One of this month's files, and what last month's recipe says to do with it."""

    source_id: str = Field(description="This month's file.")
    file_name: str = Field(description="Its file name, as uploaded.")
    role: str = Field(description="The role the saved recipe read the file it replaces as.")
    replaces_source_id: str = Field(description="Last time's file this one stands in for.")
    mapping_id: str = Field(description="The mapping the build will apply to this file.")
    missing_columns: tuple[str, ...] = Field(
        default=(),
        description=(
            "Columns the saved mapping read that this file no longer has, in mapping order. Empty "
            "when the file can be built exactly as last time; otherwise this file's mapping has to "
            "be reviewed before anything is built."
        ),
    )
    settled_by_user: bool = Field(
        default=False,
        description="The mapping is one the user saved for this file, not a copy of last time's.",
    )
    message: str = Field(
        default="",
        description="What has to be done about the missing columns, worded for the screen; empty when nothing.",
    )


class UnmatchedSource(Artefact):
    """A file the saved recipe read that none of this month's files could stand in for."""

    source_id: str = Field(description="Last time's file.")
    file_name: str = Field(description="Its file name.")
    role: str = Field(description="The role it was read as.")
    message: str = Field(description="What has to be done about it, worded for the screen.")


@dataclass(frozen=True, slots=True)
class ReplayPlan:
    """What replaying a recipe onto this month's files would do; nothing is saved by planning it.

    `mappings` holds one `MappingSpec` per matched file - the copies of last time's mappings, and
    any the user already settled for this month - ready to save. `ready` is the one question the
    caller acts on: every source of the recipe has a file, and no file is missing a column.
    """

    sources: tuple[ReplayedSource, ...]
    mappings: tuple[MappingSpec, ...]
    unmatched: tuple[UnmatchedSource, ...]
    unused: tuple[SourceSpec, ...]

    @property
    def ready(self) -> bool:
        return not self.unmatched and not any(source.missing_columns for source in self.sources)


def plan_replay(
    spec: OnboardingSpec,
    *,
    recipe_sources: Mapping[str, SourceSpec],
    recipe_mappings: Sequence[MappingSpec],
    new_sources: Sequence[SourceSpec],
    detected_roles: Mapping[str, str | None],
    settled: Mapping[str, MappingSpec],
    schema: StandardSchemaConfig,
    roles: RoleCatalogue,
    new_mapping_id: Callable[[], str],
) -> ReplayPlan:
    """Match this month's files to the recipe's, and copy each mapping across.

    `recipe_sources` and `recipe_mappings` are the saved recipe's own rows; `detected_roles` is the
    detector's top-ranked role per new file, or `None` when it had nothing to go on; `settled` is a
    mapping the user has already saved for one of the new files, keyed by that file's id, which is
    used as it stands instead of a fresh copy - that is how a reopened mapping step's answer comes
    back into the replay without being overwritten by last month's.
    """
    by_source = {mapping.source_id: mapping for mapping in recipe_mappings}
    remaining = list(new_sources)
    replayed: list[ReplayedSource] = []
    mappings: list[MappingSpec] = []
    unmatched: list[UnmatchedSource] = []
    for old_id in (spec.entity_source_id, *spec.event_source_ids):
        old = recipe_sources[old_id]
        old_mapping = by_source.get(old_id)
        role = old.role or (old_mapping.role if old_mapping is not None else "")
        match = _match(old, role, remaining, detected_roles)
        if match is None or old_mapping is None:
            unmatched.append(
                UnmatchedSource(
                    source_id=old.source_id,
                    file_name=old.file_name,
                    role=role,
                    message=(
                        f"None of this month's files stands in for {old.file_name}, which the saved "
                        f"recipe read as {role or 'a table'}. Upload this month's copy of it too."
                    ),
                )
            )
            continue
        remaining.remove(match)
        own = settled.get(match.source_id)
        mapping = own if own is not None else _copied(old_mapping, match, new_mapping_id(), schema, roles)
        missing = _missing(old_mapping if own is None else own, match)
        mappings.append(mapping)
        replayed.append(
            ReplayedSource(
                source_id=match.source_id,
                file_name=match.file_name,
                role=role,
                replaces_source_id=old.source_id,
                mapping_id=mapping.mapping_id,
                missing_columns=missing,
                settled_by_user=own is not None,
                message=_missing_message(match.file_name, missing),
            )
        )
    return ReplayPlan(
        sources=tuple(replayed),
        mappings=tuple(mappings),
        unmatched=tuple(unmatched),
        unused=tuple(remaining),
    )


def replayed_spec(spec: OnboardingSpec, plan: ReplayPlan, *, spec_id: str) -> OnboardingSpec:
    """The saved recipe, re-pointed at this month's files: same features, label and snapshot rule.

    Only a `ready` plan can become a recipe - a file with a missing column would build with a hole
    where a feature's input used to be, and a source with no file would build without it - so asking
    for one earlier is a caller's bug, raised rather than papered over.
    """
    if not plan.ready:
        raise ValueError("a replay with unmatched sources or missing columns cannot become a recipe")
    entity = next(source for source in plan.sources if source.replaces_source_id == spec.entity_source_id)
    events = tuple(source.source_id for source in plan.sources if source is not entity)
    return OnboardingSpec(
        spec_id=spec_id,
        client_id=spec.client_id,
        use_case=spec.use_case,
        entity_source_id=entity.source_id,
        event_source_ids=events,
        mapping_ids=tuple(sorted(mapping.mapping_id for mapping in plan.mappings)),
        feature_spec=spec.feature_spec,
        label_spec=spec.label_spec,
        snapshot_spec=spec.snapshot_spec,
        created_at=utc_now(),
    )


def _match(
    old: SourceSpec,
    role: str,
    candidates: Sequence[SourceSpec],
    detected_roles: Mapping[str, str | None],
) -> SourceSpec | None:
    """The new file standing in for `old`, by the strongest evidence available, or `None`.

    A role the user confirmed on the new file outranks everything, because it is a decision; the
    same file name comes next, because it is how an export job names this month's copy of last
    month's table; the detector's first choice is last, because it is only ever a proposal.
    """
    for rule in (
        lambda source: source.role == role,
        lambda source: source.role is None and source.file_name == old.file_name,
        lambda source: source.role is None and detected_roles.get(source.source_id) == role,
    ):
        for source in candidates:
            if rule(source):
                return source
    return None


def _copied(
    old: MappingSpec,
    source: SourceSpec,
    mapping_id: str,
    schema: StandardSchemaConfig,
    roles: RoleCatalogue,
) -> MappingSpec:
    """Last time's mapping, re-pointed at `source`, minus the columns `source` no longer has.

    What is kept is kept exactly - the transform, the value map, the confidence and who decided it -
    because a replay is the same decision applied again, not a new suggestion. A standard column
    that loses its source column becomes "missing required" again when the role needs it, by the
    same rule the suggester used (`required_standard_columns`), so the reopened mapping step says
    precisely what has to be found in this month's file.
    """
    present = set(source.columns)
    columns: tuple[MappingColumn, ...] = tuple(column for column in old.columns if column.source in present)
    claimed = {column.source for column in columns}
    filled = {column.standard for column in columns}
    required = required_standard_columns(schema, roles.require(old.role))
    return MappingSpec(
        mapping_id=mapping_id,
        client_id=source.client_id,
        source_id=source.source_id,
        use_case=old.use_case,
        role=old.role,
        columns=columns,
        unmapped_source=tuple(sorted(name for name in source.columns if name not in claimed)),
        missing_required=tuple(sorted(required - filled)),
        value_maps={
            standard: dict(values) for standard, values in old.value_maps.items() if standard in filled
        },
        created_at=utc_now(),
    )


def _missing(mapping: MappingSpec, source: SourceSpec) -> tuple[str, ...]:
    """The columns `mapping` reads that `source` does not have, in mapping order."""
    present = set(source.columns)
    return tuple(column.source for column in mapping.columns if column.source not in present)


def _missing_message(file_name: str, missing: Sequence[str]) -> str:
    """The sentence the reopened mapping step shows above one file; empty when nothing is missing.

    It names the columns, which are the client's own column *names* on the client's own screen -
    never a data value (house rule 4).
    """
    if not missing:
        return ""
    names = ", ".join(missing)
    noun = "column" if len(missing) == 1 else "columns"
    return (
        f"This month's {file_name} has no {names} {noun}, which last month's recipe read. Point one "
        f"of this file's columns at what {names} was mapped to, or save the mapping without it."
    )
