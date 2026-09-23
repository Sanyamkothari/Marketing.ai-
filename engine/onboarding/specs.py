"""Onboarding contracts: one pydantic model per Phase 2 document (Phase 2 plan section 8).

Two kinds of model meet here.

*Vocabulary* - `StandardSchemaConfig`, `FeatureDef`, `LabelDefinition`, `SnapshotDefinition` and the
enums around them - is defined in `engine.config`, because those blocks appear in a use-case YAML and
in `engine.yaml:defaults`, and `engine.config` may not import anything of ours. They are re-exported
here so a reader has one place to import the whole Phase 2 vocabulary from (DEC-100).

*Artefacts* - the saved, hashed, per-client documents - are defined here: a source profile, a
mapping, an onboarding spec, a dataset manifest, a build report and a build status.

Every artefact model is frozen, forbids unknown keys, uses tuples for collections and timezone-aware
datetimes, and carries a one-line `Field(description=...)`, exactly as `engine.contracts` does; the
generated `docs/API.md` prints that text. Floats destined for a screen are rounded by the producing
stage and lists arrive pre-sorted: the UI renders, it never computes.

Hashes are what make lineage worth having, so `spec_hash` is the only way anything here is hashed:
sorted keys, no timestamps, no ids that a re-save would change. Two specs that say the same thing
hash the same, or the Phase 5 archive is worthless (plan section 13, rule 15).
"""

from __future__ import annotations

import datetime
import hashlib
import json
from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import Any, Final, Literal

from pydantic import AwareDatetime, BaseModel, Field, model_validator

from engine.config import (
    AGGREGATES_OVER_COLUMN,
    WINDOWLESS_FUNCTIONS,
    AggFunction,
    DecidedBy,
    Derivable,
    FeatureDef,
    LabelDefinition,
    LabelType,
    RoleCatalogue,
    RoleKind,
    RoleSpec,
    SnapshotDefinition,
    SnapshotFrequency,
    SnapshotMode,
    StandardColumn,
    StandardSchemaConfig,
    StandardType,
    SubAggregation,
    TransformKind,
    WhereClause,
    WhereOp,
)
from engine.contracts import (
    ONBOARDING_VALIDATION_CODES,
    Artefact,
    DatasetFingerprint,
    DatasetProfile,
    RunState,
    ValidationCheck,
)

__all__ = [
    "AGGREGATES_OVER_COLUMN",
    "DATASET_ARTEFACTS",
    "DATASET_ARTEFACT_REGISTRY",
    "ONBOARDING_VALIDATION_CODES",
    "SPEC_VERSION",
    "WINDOWLESS_FUNCTIONS",
    "AggFunction",
    "BuildReport",
    "BuildStage",
    "BuildStatus",
    "ClientRecord",
    "ColumnOrigin",
    "ColumnTransform",
    "DatasetColumn",
    "DatasetManifest",
    "DecidedBy",
    "Derivable",
    "FeatureDef",
    "FeatureSpec",
    "FeatureStat",
    "KeyCandidate",
    "LabelDefinition",
    "LabelSpec",
    "LabelType",
    "LeakCheckReason",
    "LeakCheckRecord",
    "LeakCheckScope",
    "MappingColumn",
    "MappingSpec",
    "OnboardingCheck",
    "OnboardingSpec",
    "RoleCandidate",
    "RoleCatalogue",
    "RoleKind",
    "RoleSpec",
    "SnapshotDefinition",
    "SnapshotFrequency",
    "SnapshotMode",
    "SnapshotSpec",
    "SnapshotStat",
    "SourceProfile",
    "SourceSpec",
    "SourceStat",
    "StandardColumn",
    "StandardSchemaConfig",
    "StandardType",
    "SubAggregation",
    "TimeCandidate",
    "TransformKind",
    "WhereClause",
    "WhereOp",
    "dataset_artefact_model",
    "recipe_hash",
    "spec_hash",
]

SPEC_VERSION: Final[int] = 1
"""Version of the spec documents this module reads and writes."""


# ---------------------------------------------------------------------------
# Stable hashing (plan section 13, rule 15)
# ---------------------------------------------------------------------------
HASH_PREFIX: Final[str] = "sha256:v1:"
_HASH_HEADER: Final[bytes] = b"marketing-ai/spec/v1\n"
"""Domain separator, so a spec hash can never collide with a file or dataset hash."""

VOLATILE_FIELDS: Final[frozenset[str]] = frozenset(
    {"created_at", "built_at", "hash", "spec_id", "mapping_id", "dataset_id", "fingerprint"}
)
"""Fields excluded from every spec hash: a timestamp or an id would make two identical specs differ."""


def spec_hash(model: BaseModel, *, exclude: frozenset[str] | None = None) -> str:
    """The stable hash of a spec: sorted keys, no timestamps, no ids.

    Two specs that say the same thing hash the same however they were built, which is what lets a
    dataset manifest name the spec that produced it and a Phase 5 archive key on it.
    """
    payload = model.model_dump(
        mode="json", by_alias=True, exclude=set(VOLATILE_FIELDS if exclude is None else exclude)
    )
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(_HASH_HEADER + body.encode("utf-8")).hexdigest()
    return f"{HASH_PREFIX}{digest}"


_RECIPE_HASH_HEADER: Final[bytes] = b"marketing-ai/recipe/v1\n"
"""Domain separator for `recipe_hash`, so it can never equal a `spec_hash` of the same recipe."""


def recipe_hash(spec: OnboardingSpec, mappings: Iterable[MappingSpec]) -> str:
    """What "the same recipe" means for ruling R1's first-build rule (DEC-871).

    Two builds are of the same recipe when they would run the same logic: the same use case, the
    same feature spec, label spec and snapshot rule, and, for every mapped role, the same column
    decisions (which client column feeds which standard column, through which transform). That is
    everything that decides which events a feature query reads and how it reads them.

    Left out, because a monthly replay (`engine.onboarding.replay`) or a scheduled rebuild changes
    them without changing any of that logic: every id (spec, mapping, source, client, dataset),
    every timestamp, a mapping's suggester confidence and who decided a column, the file's unmapped
    and missing columns, and `value_maps` (a copy of what the transforms already say). A replay that
    had to drop a column changes the recipe, and its next build runs the full check again.

    `spec_hash` cannot serve: it hashes the spec's source and mapping ids, which change every month.
    """
    decisions = sorted(
        (
            {
                "role": mapping.role,
                "columns": [
                    {
                        "source": column.source,
                        "standard": column.standard,
                        "transform": (
                            None if column.transform is None else column.transform.model_dump(mode="json")
                        ),
                    }
                    for column in mapping.columns
                ],
            }
            for mapping in mappings
        ),
        key=lambda decision: json.dumps(decision, sort_keys=True),
    )
    payload = {
        "use_case": spec.use_case,
        "feature_spec": spec.feature_spec.model_dump(mode="json"),
        "label_spec": None if spec.label_spec is None else spec.label_spec.model_dump(mode="json"),
        "snapshot_spec": spec.snapshot_spec.model_dump(mode="json"),
        "mappings": decisions,
    }
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(_RECIPE_HASH_HEADER + body.encode("utf-8")).hexdigest()
    return f"{HASH_PREFIX}{digest}"


# ---------------------------------------------------------------------------
# The onboarding validation table (plan section 7)
# ---------------------------------------------------------------------------
OnboardingCheck = ValidationCheck
"""One row of the build report: the Phase 1 `ValidationCheck`, under the name onboarding code uses.

This was a twin model with the same fields plus `source_id`, because `ValidationCheck` sat above this
branch's block in `engine/contracts.py` and could not accept the onboarding codes (DEC-101). Plan A
ruling D7 merged the two: `ValidationCheck` now carries `source_id`, accepts both code tables
(`CHECK_CODES`) and refuses an acknowledgeable `FUTURE_EVENTS_LEAKED`, so a Phase 1 finding re-run on
an assembled dataset goes into the build report as it is, with nothing to convert. The alias keeps
every onboarding producer and route reading as it did; new code may use either name, and they are the
same class. `ONBOARDING_VALIDATION_CODES` is re-exported from `engine.contracts` for the same reason.
"""


# ---------------------------------------------------------------------------
# 4.1 Clients and sources
# ---------------------------------------------------------------------------
class ClientRecord(Artefact):
    """`client.json` - a company whose data we onboard.

    A `client_id` groups sources, mappings, specs, datasets and runs. It is a folder and a row, not
    a security boundary; tenancy arrives with Phase 4.
    """

    client_id: str = Field(description="Unique id of the client; also the directory name.")
    name: str = Field(description="Display name of the client.")
    industry: str = Field(description="Industry id the client belongs to, for the lifecycle template.")
    created_at: AwareDatetime = Field(description="UTC time the client was created.")
    notes: str = Field(default="", description="Free text the user typed about this client.")


class RoleCandidate(Artefact):
    """One role the detector proposes for a source, and why (plan section 6.1)."""

    role: str = Field(description="Role id from configs/roles.yaml.")
    confidence: float = Field(description="Detector confidence, 0 to 1, rounded.")
    reasons: tuple[str, ...] = Field(
        default=(), description="Plain-language signals behind the score, strongest first."
    )


class KeyCandidate(Artefact):
    """A column that could be the entity key of this source."""

    column: str = Field(description="Column name as it appears in the file.")
    distinct_count: int = Field(description="Distinct non-null values.")
    null_rate: float = Field(description="Share of rows that are null, 0 to 1.")
    rows_per_key: float = Field(
        description="Rows divided by distinct keys: about 1 for an entity table, far above for events."
    )
    coverage: float | None = Field(
        default=None,
        description=(
            "Share of this source's rows whose key is found in the entity table, 0 to 1; null until "
            "an entity source exists to compare against."
        ),
    )


class TimeCandidate(Artefact):
    """A column that could be the event time of this source."""

    column: str = Field(description="Column name as it appears in the file.")
    parse_rate: float = Field(description="Share of non-null values that parse as a date, 0 to 1.")
    earliest: datetime.date | None = Field(default=None, description="Earliest parsed value.")
    latest: datetime.date | None = Field(default=None, description="Latest parsed value.")
    day_first_ambiguous: bool = Field(
        default=False,
        description="Day-first and month-first both parse, so the user must say which one it is.",
    )


class SourceProfile(Artefact):
    """`sources/<source_id>/profile.json` - the Phase 1 profile plus what onboarding needs.

    The Phase 1 profiler is reused unchanged and its `DatasetProfile` is carried whole rather than
    copied field by field, so a source and an upload are described by exactly the same numbers, and
    the PII redaction that profile already applies covers the sample values the mapping UI shows.
    """

    source_id: str = Field(description="Id of the source this profile describes.")
    client_id: str = Field(description="Client the source belongs to.")
    file_name: str = Field(description="The user's original file name.")
    role: str | None = Field(
        default=None, description="Confirmed role; null until the user or the detector settles one."
    )
    role_decided_by: DecidedBy | None = Field(
        default=None, description="Whether the role was auto-detected or chosen by the user."
    )
    role_candidates: tuple[RoleCandidate, ...] = Field(
        default=(), description="Ranked role proposals, most likely first."
    )
    key_candidates: tuple[KeyCandidate, ...] = Field(
        default=(), description="Columns that could identify the entity, most likely first."
    )
    time_candidates: tuple[TimeCandidate, ...] = Field(
        default=(), description="Columns that could be the event time, most likely first."
    )
    profile: DatasetProfile = Field(description="The Phase 1 dataset profile of this file, unchanged.")

    @property
    def fingerprint(self) -> DatasetFingerprint:
        """Identity of the exact table this source holds; the manifest records it per source."""
        return self.profile.fingerprint

    @property
    def rows(self) -> int:
        return self.profile.row_count


class SourceSpec(Artefact):
    """The registry row of one uploaded source: what it is, where it is and what it holds."""

    source_id: str = Field(description="Unique id of the source.")
    client_id: str = Field(description="Client the source belongs to.")
    file_name: str = Field(description="The user's original file name.")
    storage_key: str = Field(description="Storage key of the raw file.")
    file_format: Literal["csv", "parquet"] = Field(description="Format the file is read as.")
    role: str | None = Field(default=None, description="Confirmed role; null until one is settled.")
    rows: int = Field(description="Rows in the file.")
    columns: tuple[str, ...] = Field(description="Column names, in file order.")
    fingerprint: DatasetFingerprint = Field(description="Identity of the exact table.")
    created_at: AwareDatetime = Field(description="UTC time the source was uploaded.")


# ---------------------------------------------------------------------------
# 4.2 Mapping
# ---------------------------------------------------------------------------
class ColumnTransform(Artefact):
    """One pure, replayable transform attached to a mapped column (plan section 6.1).

    Every parameter a kind needs is a named field rather than a free `dict`, so an unreplayable
    transform cannot be saved and a reader can see what a mapping will do without running it.
    """

    kind: TransformKind = Field(description="Which transform this is.")
    to_type: StandardType | None = Field(default=None, description="Target type; `cast` only.")
    date_format: str | None = Field(
        default=None,
        description=(
            "Explicit strptime format; `cast` to a date only. Required when day-first and "
            "month-first both parse, so the engine never guesses which one the client meant."
        ),
    )
    value_map: dict[str, Any] = Field(
        default_factory=dict, description="Source value to standard value; `value_map` only."
    )
    unmapped: Literal["keep", "null"] = Field(
        default="keep", description="What to do with a value the map does not list; `value_map` only."
    )
    factor: float | None = Field(
        default=None, description="Multiplier, for example 0.01 for paise to rupees; `scale` only."
    )
    expression: str | None = Field(default=None, description="Whitelisted derive expression; `derive` only.")
    by: str | None = Field(
        default=None, description="Column whose latest row is kept per entity; `dedupe` only."
    )

    @model_validator(mode="after")
    def _shape(self) -> ColumnTransform:
        required: dict[TransformKind, tuple[str, object]] = {
            TransformKind.CAST: ("to_type", self.to_type),
            TransformKind.SCALE: ("factor", self.factor),
            TransformKind.DERIVE: ("expression", self.expression),
            TransformKind.DEDUPE: ("by", self.by),
        }
        if self.kind in required:
            field, value = required[self.kind]
            if value is None:
                raise ValueError(f"a {self.kind.value} transform needs {field!r}")
        if self.kind is TransformKind.VALUE_MAP and not self.value_map:
            raise ValueError("a value_map transform needs a value_map")
        if self.kind is not TransformKind.VALUE_MAP and self.value_map:
            raise ValueError(f"only a value_map transform carries a value_map, not {self.kind.value}")
        if self.date_format is not None and self.kind is not TransformKind.CAST:
            raise ValueError(f"only a cast carries a date_format, not {self.kind.value}")
        return self


class MappingColumn(Artefact):
    """One decided column of a mapping: their name, our name, and how one becomes the other."""

    source: str = Field(description="Column name as it appears in the client's file.")
    standard: str = Field(description="Standard column name it means, or the entity key / event time.")
    transform: ColumnTransform | None = Field(
        default=None, description="Transform applied on the way; null when the value is taken as-is."
    )
    confidence: float = Field(description="Suggester confidence, 0 to 1, rounded.")
    decided_by: DecidedBy = Field(description="Whether the engine auto-accepted this or the user chose it.")


class MappingSpec(Artefact):
    """`mappings/<mapping_id>.json` - how one source's columns become standard columns."""

    mapping_id: str = Field(description="Unique id of this mapping.")
    client_id: str = Field(description="Client the mapping belongs to.")
    source_id: str = Field(description="Source the mapping is for.")
    use_case: str = Field(description="Use case whose standard schema the mapping targets.")
    role: str = Field(description="Role the source was mapped as.")
    columns: tuple[MappingColumn, ...] = Field(description="Decided columns, in standard-schema order.")
    unmapped_source: tuple[str, ...] = Field(
        default=(), description="Source columns deliberately left out, sorted."
    )
    missing_required: tuple[str, ...] = Field(
        default=(), description="Required standard columns neither mapped nor derivable, sorted."
    )
    value_maps: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="Standard column to its value map, repeated here so the UI renders without transforms.",
    )
    created_at: AwareDatetime = Field(description="UTC time the mapping was saved.")
    hash: str = Field(default="", description="Stable hash of the decisions; set by the writer.")

    @property
    def standard_names(self) -> tuple[str, ...]:
        return tuple(column.standard for column in self.columns)

    @property
    def source_names(self) -> tuple[str, ...]:
        return tuple(column.source for column in self.columns)

    def by_standard(self, standard: str) -> MappingColumn | None:
        for column in self.columns:
            if column.standard == standard:
                return column
        return None

    def with_hash(self) -> MappingSpec:
        """A copy carrying its own stable hash."""
        return self.model_copy(update={"hash": spec_hash(self)})

    @model_validator(mode="after")
    def _one_claim_each(self) -> MappingSpec:
        for label, names in (("source", self.source_names), ("standard", self.standard_names)):
            duplicates = sorted({name for name in names if names.count(name) > 1})
            if duplicates:
                raise ValueError(
                    f"{', '.join(duplicates)} is claimed twice; one {label} column means one thing"
                )
        overlap = sorted(set(self.unmapped_source) & set(self.source_names))
        if overlap:
            raise ValueError(f"{', '.join(overlap)} is both mapped and listed as unmapped")
        return self


# ---------------------------------------------------------------------------
# 4.3 The three specs a build executes
# ---------------------------------------------------------------------------
class FeatureSpec(Artefact):
    """`feature_spec.json` - the features to compute per entity per snapshot (plan section 6.2).

    This is the one spec the Phase 5 Evolve layer may propose changes to; the label and snapshot
    specs are marked `agent_editable: false` for the reason recorded on them.
    """

    spec_version: int = Field(default=SPEC_VERSION, description="Version of the feature-spec format.")
    features: tuple[FeatureDef, ...] = Field(description="Features to build, in build order.")
    agent_editable: Literal[True] = Field(
        default=True,
        description="Phase 5 may propose edits to this spec; the label and snapshot specs it may not.",
    )

    @model_validator(mode="after")
    def _unique_names(self) -> FeatureSpec:
        names = [feature.name for feature in self.features]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"feature(s) {', '.join(duplicates)} defined more than once")
        return self

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(feature.name for feature in self.features)

    @property
    def roles(self) -> tuple[str, ...]:
        """Every role this spec reads, sorted; the build needs one query per role."""
        return tuple(sorted({feature.role for feature in self.features}))

    def for_role(self, role: str) -> tuple[FeatureDef, ...]:
        return tuple(feature for feature in self.features if feature.role == role)

    @property
    def hash(self) -> str:
        return spec_hash(self)


LabelSpec = LabelDefinition
"""`label_spec.json`. The saved document and the use-case YAML block are the same shape, so a client
who keeps the default churn definition and one who edits it are describing the same kind of thing."""

SnapshotSpec = SnapshotDefinition
"""`snapshot_spec.json`. As with the label, the configured default and the saved spec are one model."""


class OnboardingSpec(Artefact):
    """`onboarding_spec.json` - the whole recipe, saved per client per use case.

    This is what makes next month one click: the sources' roles, the mappings, the features, the
    label and the snapshot rule, replayed against new files.
    """

    spec_id: str = Field(description="Unique id of this recipe.")
    client_id: str = Field(description="Client the recipe belongs to.")
    use_case: str = Field(description="Use case the recipe builds a dataset for.")
    entity_source_id: str = Field(description="Source holding one row per entity.")
    event_source_ids: tuple[str, ...] = Field(
        default=(), description="Event sources the features and label read, sorted."
    )
    mapping_ids: tuple[str, ...] = Field(description="Mappings to apply, one per source, sorted.")
    feature_spec: FeatureSpec = Field(description="Features to compute.")
    label_spec: LabelSpec | None = Field(
        default=None, description="How the target is derived; null for a scoring-only recipe."
    )
    snapshot_spec: SnapshotSpec = Field(
        description="Training snapshot rule. The scoring one is derived from it, never configured separately."
    )
    created_at: AwareDatetime = Field(description="UTC time the recipe was saved.")
    hash: str = Field(default="", description="Stable hash of the recipe; set by the writer.")

    def with_hash(self) -> OnboardingSpec:
        """A copy carrying its own stable hash."""
        return self.model_copy(update={"hash": spec_hash(self)})

    def scoring_snapshot_spec(self) -> SnapshotSpec:
        """The scoring recipe's snapshot rule: one snapshot, no history requirement (plan section 6.4).

        Scoring always builds a single snapshot at the end of the new data and skips labels, so the
        user never configures a second snapshot rule and the two can never drift apart.
        """
        return self.snapshot_spec.model_copy(
            update={"mode": SnapshotMode.SINGLE, "start": None, "end": None, "max_snapshots": 1}
        )

    @model_validator(mode="after")
    def _sources_are_distinct(self) -> OnboardingSpec:
        if self.entity_source_id in self.event_source_ids:
            raise ValueError(
                f"{self.entity_source_id!r} is named as both the entity source and an event source"
            )
        duplicates = sorted({sid for sid in self.event_source_ids if self.event_source_ids.count(sid) > 1})
        if duplicates:
            raise ValueError(f"event source(s) {', '.join(duplicates)} listed more than once")
        return self


# ---------------------------------------------------------------------------
# 4.4 The built dataset
# ---------------------------------------------------------------------------
class ColumnOrigin(Artefact):
    """Not a model: see `DatasetColumn.origin`. Kept as a docstring anchor for the generated docs."""


class DatasetColumn(Artefact):
    """One column of a built dataset and where it came from."""

    name: str = Field(description="Column name in the built dataset.")
    type: StandardType = Field(description="Standard type of the column.")
    origin: Literal["key", "mapped", "derived", "feature", "label"] = Field(
        description="Whether the column identifies the row, was mapped, derived, computed or labelled."
    )


class DatasetManifest(Artefact):
    """`dataset_manifest.json` - the lineage of one built dataset.

    A run names a `dataset_id`; from that id this document recovers exactly how the data was
    produced: which source files, which mappings, which specs, at which fingerprints.
    """

    dataset_id: str = Field(description="Unique id of the dataset; also its directory name.")
    client_id: str = Field(description="Client the dataset belongs to.")
    use_case: str = Field(description="Use case the dataset was built for.")
    spec_id: str = Field(description="Onboarding spec that produced it.")
    spec_hash: str = Field(description="Stable hash of that spec.")
    mapping_hashes: dict[str, str] = Field(default_factory=dict, description="Mapping id to its stable hash.")
    source_fingerprints: dict[str, DatasetFingerprint] = Field(
        default_factory=dict, description="Source id to the fingerprint of the exact table read."
    )
    feature_spec_hash: str = Field(description="Stable hash of the feature spec alone.")
    snapshot_mode: SnapshotMode = Field(description="Whether the dataset has one row per entity or many.")
    primary_key: tuple[str, ...] = Field(
        description="Columns that identify a row: the entity key, plus the snapshot date when periodic."
    )
    target: str | None = Field(description="Label column; null for a dataset built for scoring.")
    columns: tuple[DatasetColumn, ...] = Field(description="Every column, in dataset order.")
    n_rows: int = Field(description="Rows in the built dataset.")
    n_entities: int = Field(description="Distinct entities in the built dataset.")
    snapshot_dates: tuple[datetime.date, ...] = Field(
        description="Snapshot dates that survived, earliest first."
    )
    fingerprint: DatasetFingerprint = Field(description="Identity of the built table itself.")
    built_at: AwareDatetime = Field(description="UTC time the build finished.")
    engine_version: str = Field(description="Version of the engine package that built it.")
    recipe_hash: str | None = Field(
        default=None,
        description="`recipe_hash` of the recipe and mappings built; null for a dataset built before M55.",
    )

    @model_validator(mode="after")
    def _key_matches_mode(self) -> DatasetManifest:
        expected = 2 if self.snapshot_mode is SnapshotMode.PERIODIC else 1
        if len(self.primary_key) != expected:
            raise ValueError(
                f"a {self.snapshot_mode.value} dataset has a {expected}-column primary key, "
                f"got {list(self.primary_key)}"
            )
        return self


class SourceStat(Artefact):
    """What one source contributed to a build."""

    source_id: str = Field(description="Source this row is about.")
    role: str = Field(description="Role it was read as.")
    rows: int = Field(description="Rows read from it.")
    fingerprint: str = Field(description="Hash of the exact table read.")
    join_coverage: float | None = Field(
        default=None,
        description="Share of its rows whose key matched an entity, 0 to 1; null for the entity source.",
    )


class SnapshotStat(Artefact):
    """One snapshot date and what it produced."""

    date: datetime.date = Field(description="The snapshot date.")
    entities: int = Field(description="Entities with a row at this date.")
    positives: int | None = Field(default=None, description="Positive labels; null when unlabelled.")
    positive_rate: float | None = Field(
        default=None, description="Positive rate 0 to 1; null when unlabelled."
    )
    censored: bool = Field(
        default=False, description="The outcome window runs past the end of the data, so the row was dropped."
    )
    dropped_reason: str | None = Field(
        default=None, description="Why this snapshot was dropped, when it was."
    )


class FeatureStat(Artefact):
    """One built feature and how it turned out."""

    name: str = Field(description="Feature name.")
    null_fraction: float = Field(description="Share of rows where the feature is null, 0 to 1.")
    dropped: bool = Field(default=False, description="Whether the feature was dropped from the dataset.")
    reason: str | None = Field(default=None, description="Why it was dropped, when it was.")


LeakCheckScope = Literal["full", "narrow"]
"""Which snapshot rows the future-data leak probe rebuilt (DEC-096, ruling R1, DEC-870):

* `full` - every snapshot row the build produced features for;
* `narrow` - the rows of every entity given a future-dated event, plus up to 500 entities given none.
"""

LeakCheckReason = Literal["option", "first_build_of_recipe", "default"]
"""Why that scope was used (DEC-871):

* `first_build_of_recipe` - no earlier build of the same recipe (`recipe_hash`) is registered for
  this client, so the full check is forced whatever was asked;
* `option` - the build was asked for the full check (`full_leak_check: true`);
* `default` - neither, so the narrowed check ran.
"""


class LeakCheckRecord(Artefact):
    """Which future-data leak check a build ran and why: the full one or the narrowed one (DEC-870)."""

    scope: LeakCheckScope = Field(
        description="`full`: every snapshot row was rebuilt; `narrow`: entities given future events plus controls."
    )
    reason: LeakCheckReason = Field(
        description="`first_build_of_recipe`, `option` (full_leak_check was asked for) or `default`."
    )
    recipe_hash: str = Field(
        description="Hash of the recipe's logic without ids or times; what 'the same recipe' means."
    )
    rows_total: int = Field(description="Snapshot rows the build computed features for.")
    rows_probed: int = Field(description="Snapshot rows the probe rebuilt against future-dated events.")
    summary: str = Field(description="One sentence for the build review screen, already worded.")


class BuildReport(Artefact):
    """`build_report.json` - everything the build review screen shows, already computed."""

    dataset_id: str = Field(description="Dataset this report belongs to.")
    checks: tuple[ValidationCheck, ...] = Field(
        description=(
            "Onboarding checks, then the full Phase 1 validation re-run on the assembled dataset; "
            "errors first."
        )
    )
    sources: tuple[SourceStat, ...] = Field(description="One row per source, entity source first.")
    snapshots: tuple[SnapshotStat, ...] = Field(description="One row per snapshot date, earliest first.")
    features: tuple[FeatureStat, ...] = Field(description="One row per built feature, in spec order.")
    rows_out: int = Field(description="Rows in the built dataset.")
    entities_out: int = Field(description="Distinct entities in the built dataset.")
    duration_s: float = Field(description="Wall-clock seconds the build took.")
    features_sql_path: str | None = Field(
        default=None, description="Storage key of the compiled SQL, for debugging and Phase 4 porting."
    )
    error_count: int = Field(description="Blocking errors, excluding acknowledged ones.")
    warning_count: int = Field(description="Warnings.")
    passed: bool = Field(description="True when no blocking error remains, so the dataset is usable.")
    built_at: AwareDatetime = Field(description="UTC time the report was written.")
    leak_check: LeakCheckRecord | None = Field(
        default=None,
        description="Which future-data leak check ran and why; null when the build stopped before it.",
    )


class BuildStage(Artefact):
    """One stage of a build on the progress screen.

    `key` is a plain string rather than an enum because the feature stages are one per mapped role
    (`features:complaints`), which is a fact about the client's data, not about the engine.
    """

    key: str = Field(description="Stage id, for example apply_mappings or features:complaints.")
    title: str = Field(description="Engine label for logs and docs.")
    group_label: str = Field(description="Progress-screen line this stage belongs to.")
    state: RunState = Field(description="Stage state: pending, running, done, failed, cancelled or skipped.")
    detail: str = Field(default="", description="Second line under the row, pre-formatted by the stage.")
    started_at: AwareDatetime | None = Field(default=None, description="UTC time this stage started.")
    ended_at: AwareDatetime | None = Field(default=None, description="UTC time this stage ended.")
    duration_seconds: float | None = Field(default=None, description="Wall-clock duration of the stage.")


class BuildStatus(Artefact):
    """`build_status.json` - what the build progress screen polls, exactly as a run's status.json is."""

    dataset_id: str = Field(description="Dataset being built.")
    client_id: str = Field(description="Client the build belongs to.")
    spec_id: str = Field(description="Onboarding spec being executed.")
    state: RunState = Field(description="Overall build state.")
    updated_at: AwareDatetime = Field(description="UTC time this status file was last written.")
    stages: tuple[BuildStage, ...] = Field(description="The full stage list in execution order.")
    current_stage: str | None = Field(default=None, description="Stage currently running, when any.")
    progress_pct: int = Field(description="Pre-computed completion percentage, done stages over total.")
    detail: str = Field(default="", description="One line about the build as a whole.")
    error: str | None = Field(default=None, description="Failure message; set when the state is failed.")


# ---------------------------------------------------------------------------
# The dataset directory
# ---------------------------------------------------------------------------
DATASET_ARTEFACT_REGISTRY: Final[Mapping[str, type[BaseModel]]] = MappingProxyType(
    {
        "dataset_manifest.json": DatasetManifest,
        "build_report.json": BuildReport,
        "build_status.json": BuildStatus,
    }
)
"""Dataset-directory artefact filename -> the model that validates it.

Deliberately separate from `engine.contracts.ARTEFACT_REGISTRY`, which is the *run* directory's
registry and is pinned name-for-name against plan section 7 by a Phase 1 test. A dataset is not a
run, its files do not live in a run directory, and merging the two registries would make that test
a list of everything the engine ever writes rather than the run contract it is (DEC-102).
"""

DATASET_ARTEFACTS: Final[frozenset[str]] = frozenset(
    {*DATASET_ARTEFACT_REGISTRY, "dataset.parquet", "sample.json", "features.sql"}
)
"""Everything a completed build writes into `data/datasets/<dataset_id>/`."""


def _known_dataset_artefacts() -> str:
    return ", ".join(sorted(DATASET_ARTEFACTS))


def dataset_artefact_model(filename: str) -> type[BaseModel]:
    """The model for a dataset artefact filename; `KeyError` listing the known names for anything else."""
    model = DATASET_ARTEFACT_REGISTRY.get(filename)
    if model is None:
        raise KeyError(
            f"unknown dataset artefact {filename!r}; known artefacts: {_known_dataset_artefacts()}"
        )
    return model
