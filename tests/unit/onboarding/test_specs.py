"""The Phase 2 contracts: the spec vocabulary, the artefact models and the stable hash.

These are the documents every other onboarding module reads and writes, so what is pinned here is
what the parallel branches can rely on: the shapes a spec may take, the shapes it may not, and the
promise that two specs saying the same thing hash the same.
"""

from __future__ import annotations

import datetime as dt

import pytest
from pydantic import ValidationError

from engine.config import (
    ConfigError,
    RoleKind,
    StandardType,
    get_roles,
    list_use_case_ids,
    load_use_case,
)
from engine.contracts import VALIDATION_CODES, DatasetFingerprint, Severity, ValidationCheck
from engine.onboarding.roles import validate_onboarding_config
from engine.onboarding.specs import (
    DATASET_ARTEFACT_REGISTRY,
    DATASET_ARTEFACTS,
    ONBOARDING_VALIDATION_CODES,
    AggFunction,
    ColumnTransform,
    DatasetColumn,
    DatasetManifest,
    DecidedBy,
    FeatureDef,
    FeatureSpec,
    LabelDefinition,
    LabelType,
    MappingColumn,
    MappingSpec,
    OnboardingCheck,
    OnboardingSpec,
    SnapshotDefinition,
    SnapshotMode,
    TransformKind,
    WhereClause,
    WhereOp,
    dataset_artefact_model,
    spec_hash,
)

UTC = dt.UTC
NOW = dt.datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# The plan section 7 table
# ---------------------------------------------------------------------------
# Transcribed by hand from the Phase 2 plan section 7 table, never parsed from the source.
PLAN_SECTION_7_CODES = frozenset(
    {
        "NO_ENTITY_SOURCE",
        "MULTIPLE_ENTITY_SOURCES",
        "ENTITY_KEY_UNMAPPED",
        "ENTITY_DUPLICATE_KEYS",
        "JOIN_KEY_UNMAPPED",
        "EVENT_TIME_UNMAPPED",
        "EVENT_TIME_UNPARSEABLE",
        "DATE_FORMAT_AMBIGUOUS",
        "JOIN_KEY_COVERAGE_LOW",
        "KEY_FORMAT_MISMATCH",
        "REQUIRED_STANDARD_COLUMN_UNMAPPED",
        "MAPPING_LOW_CONFIDENCE",
        "MAPPING_TYPE_CONFLICT",
        "VALUE_UNMAPPED",
        "SNAPSHOT_OUTSIDE_DATA_RANGE",
        "TOO_LITTLE_HISTORY",
        "LABEL_HORIZON_CENSORED",
        "LABEL_DEGENERATE_SNAPSHOT",
        "LABEL_ROLE_MISSING",
        "FEATURE_ALL_NULL",
        "FEATURE_NAME_COLLISION",
        "TOO_MANY_FEATURES",
        "ENTITY_ATTRIBUTES_NOT_TIME_VERSIONED",
        "FUTURE_EVENTS_LEAKED",
        "SOURCE_TOO_LARGE",
        "TOO_MANY_SOURCES",
    }
)


def test_the_onboarding_table_is_the_plan_s_table() -> None:
    assert ONBOARDING_VALIDATION_CODES == PLAN_SECTION_7_CODES


def test_the_phase_one_table_is_untouched() -> None:
    """Phase 2 adds a second vocabulary; it does not grow the Phase 1 one (plan section 13, rule 10)."""
    assert len(VALIDATION_CODES) == 19
    assert not (VALIDATION_CODES & ONBOARDING_VALIDATION_CODES)


def test_a_build_report_check_may_carry_either_vocabulary() -> None:
    """A build report appends the Phase 1 findings to the onboarding ones, so one type takes both."""
    assert OnboardingCheck(code="JOIN_KEY_COVERAGE_LOW", severity=Severity.WARNING, message="x")
    assert OnboardingCheck(code="PK_NOT_UNIQUE", severity=Severity.ERROR, message="x")
    with pytest.raises(ValidationError, match="unknown validation code"):
        OnboardingCheck(code="NOT_A_CODE", severity=Severity.WARNING, message="x")


def test_a_phase_one_finding_crosses_unchanged() -> None:
    check = ValidationCheck(
        code="PK_NOT_UNIQUE", severity=Severity.ERROR, message="2.0 rows per customer", column="id"
    )
    carried = OnboardingCheck.from_validation_check(check)
    assert (carried.code, carried.message, carried.column) == (check.code, check.message, check.column)


def test_a_leak_is_never_acknowledgeable() -> None:
    """Plan section 7: FUTURE_EVENTS_LEAKED is a bug, and must never be user-acknowledgeable."""
    with pytest.raises(ValidationError, match="never something a user may wave through"):
        OnboardingCheck(
            code="FUTURE_EVENTS_LEAKED", severity=Severity.ERROR, message="x", acknowledgeable=True
        )


# ---------------------------------------------------------------------------
# Feature definitions: the shapes a function may take
# ---------------------------------------------------------------------------
def test_a_count_needs_no_column() -> None:
    feature = FeatureDef(name="complaints_90d", role="complaints", function=AggFunction.COUNT, window_days=90)
    assert feature.windows == (90,)


def test_a_mean_without_a_column_is_refused() -> None:
    with pytest.raises(ConfigError, match="column it aggregates"):
        FeatureDef(name="avg_bill_6m", role="bills", function=AggFunction.MEAN, window_days=180)


def test_a_ratio_needs_both_halves_and_reports_both_windows() -> None:
    feature = FeatureDef(
        name="usage_drop",
        role="usage",
        function=AggFunction.RATIO,
        of={"function": "mean", "column": "data_mb", "window_days": 30},
        over={"function": "mean", "column": "data_mb", "window_days": 90},
    )
    assert feature.windows == (30, 90)
    with pytest.raises(ConfigError, match="both 'of' and 'over'"):
        FeatureDef(
            name="usage_drop",
            role="usage",
            function=AggFunction.RATIO,
            of={"function": "mean", "column": "data_mb", "window_days": 30},
        )


def test_only_a_ratio_carries_parts() -> None:
    with pytest.raises(ConfigError, match="Only a ratio"):
        FeatureDef(
            name="complaints_90d",
            role="complaints",
            function=AggFunction.COUNT,
            of={"function": "mean", "column": "x", "window_days": 30},
        )


def test_a_ratio_carries_no_window_of_its_own() -> None:
    with pytest.raises(ConfigError, match="carries no window"):
        FeatureDef(
            name="usage_drop",
            role="usage",
            function=AggFunction.RATIO,
            window_days=30,
            of={"function": "mean", "column": "data_mb", "window_days": 30},
            over={"function": "mean", "column": "data_mb", "window_days": 90},
        )


def test_only_a_derived_feature_carries_an_expression() -> None:
    assert FeatureDef(
        name="tenure_months",
        role="entity",
        function=AggFunction.DERIVE,
        expression="months_between(signup_date, snapshot_date)",
    ).expression
    with pytest.raises(ConfigError, match="needs an expression"):
        FeatureDef(name="tenure_months", role="entity", function=AggFunction.DERIVE)
    with pytest.raises(ConfigError, match="Only a derived feature"):
        FeatureDef(name="x_90d", role="usage", function=AggFunction.COUNT, expression="1 + 1")


def test_a_ratio_of_a_ratio_is_refused() -> None:
    """A sub-aggregation is deliberately not recursive; the message says so rather than nesting."""
    with pytest.raises(ConfigError, match="cannot be one half of a ratio"):
        FeatureDef(
            name="nested",
            role="usage",
            function=AggFunction.RATIO,
            of={"function": "ratio"},
            over={"function": "mean", "column": "data_mb", "window_days": 90},
        )


# ---------------------------------------------------------------------------
# Where clauses
# ---------------------------------------------------------------------------
def test_is_null_takes_no_value() -> None:
    assert WhereClause(column="resolved_time", op=WhereOp.IS_NULL).value is None
    with pytest.raises(ConfigError, match="takes no value"):
        WhereClause(column="resolved_time", op=WhereOp.IS_NULL, value=1)


def test_a_comparison_needs_a_value() -> None:
    with pytest.raises(ConfigError, match="needs a value"):
        WhereClause(column="days_late", op=WhereOp.GT)


def test_in_takes_a_list_and_nothing_else_does() -> None:
    assert WhereClause(column="event_type", op=WhereOp.IN, value=["sent", "converted"]).value
    with pytest.raises(ConfigError, match="list of values"):
        WhereClause(column="event_type", op=WhereOp.IN, value="converted")
    with pytest.raises(ConfigError, match="use 'in' for a list"):
        WhereClause(column="event_type", op=WhereOp.EQ, value=["a", "b"])


# ---------------------------------------------------------------------------
# Labels: the four types, and the fields each one may carry
# ---------------------------------------------------------------------------
def test_an_event_absence_label_needs_a_role_and_a_horizon() -> None:
    label = LabelDefinition(
        name="churn_next_60d", type=LabelType.EVENT_ABSENCE, role="activity", horizon_days=60
    )
    assert label.horizon_days == 60
    with pytest.raises(ConfigError, match="must name the role"):
        LabelDefinition(name="churn", type=LabelType.EVENT_ABSENCE, horizon_days=60)
    with pytest.raises(ConfigError, match="needs a horizon"):
        LabelDefinition(name="churn", type=LabelType.EVENT_ABSENCE, role="activity")


def test_a_column_label_names_a_column_and_nothing_else() -> None:
    assert LabelDefinition(name="delayed", type=LabelType.COLUMN, column="delayed").column == "delayed"
    with pytest.raises(ConfigError, match="must name it"):
        LabelDefinition(name="delayed", type=LabelType.COLUMN)
    with pytest.raises(ConfigError, match="has no horizon"):
        LabelDefinition(name="delayed", type=LabelType.COLUMN, column="delayed", horizon_days=30)


def test_a_value_threshold_label_needs_its_expression() -> None:
    label = LabelDefinition(
        name="late_or_missed_payment",
        type=LabelType.VALUE_THRESHOLD,
        role="bills",
        horizon_days=45,
        expression="paid_date IS NULL",
        **{"any": True},
    )
    assert label.any_event is True
    with pytest.raises(ConfigError, match="needs an expression"):
        LabelDefinition(name="late", type=LabelType.VALUE_THRESHOLD, role="bills", horizon_days=45)


def test_the_plan_s_any_key_is_the_wire_name() -> None:
    """Plan section 5.2 writes `any: true` in YAML; the field is `any_event` to keep the builtin free."""
    label = LabelDefinition(
        name="late",
        type=LabelType.VALUE_THRESHOLD,
        role="bills",
        horizon_days=45,
        expression="x",
        **{"any": False},
    )
    assert label.any_event is False
    assert label.model_dump(by_alias=True)["any"] is False


def test_a_label_is_never_agent_editable() -> None:
    """Plan section 14: an agent that can redefine churn can make any score go up."""
    with pytest.raises(ValidationError):
        LabelDefinition(
            name="churn",
            type=LabelType.EVENT_ABSENCE,
            role="activity",
            horizon_days=60,
            agent_editable=True,
        )


def test_a_snapshot_spec_is_never_agent_editable() -> None:
    with pytest.raises(ValidationError):
        SnapshotDefinition(agent_editable=True)


def test_a_feature_spec_is_the_one_an_agent_may_propose_edits_to() -> None:
    spec = FeatureSpec(features=(FeatureDef(name="c_90d", role="complaints", function=AggFunction.COUNT),))
    assert spec.agent_editable is True


def test_an_inverted_snapshot_range_is_refused() -> None:
    with pytest.raises(ConfigError, match="after the last"):
        SnapshotDefinition(start=dt.date(2026, 6, 1), end=dt.date(2026, 1, 1))


# ---------------------------------------------------------------------------
# Feature specs
# ---------------------------------------------------------------------------
def _spec() -> FeatureSpec:
    return FeatureSpec(
        features=(
            FeatureDef(name="complaints_90d", role="complaints", function=AggFunction.COUNT, window_days=90),
            FeatureDef(
                name="avg_bill_6m",
                role="bills",
                function=AggFunction.MEAN,
                column="amount",
                window_days=180,
            ),
            FeatureDef(name="days_since_last_bill", role="bills", function=AggFunction.DAYS_SINCE_LAST),
        )
    )


def test_a_feature_spec_groups_by_role() -> None:
    spec = _spec()
    assert spec.roles == ("bills", "complaints")
    assert [f.name for f in spec.for_role("bills")] == ["avg_bill_6m", "days_since_last_bill"]


def test_two_features_with_one_name_are_refused() -> None:
    with pytest.raises(ValidationError, match="more than once"):
        FeatureSpec(
            features=(
                FeatureDef(name="x", role="bills", function=AggFunction.COUNT),
                FeatureDef(name="x", role="usage", function=AggFunction.COUNT),
            )
        )


# ---------------------------------------------------------------------------
# The stable hash (plan section 13, rule 15)
# ---------------------------------------------------------------------------
def test_two_identical_specs_hash_identically() -> None:
    assert _spec().hash == _spec().hash


def test_a_changed_window_changes_the_hash() -> None:
    other = _spec().model_copy(
        update={
            "features": (
                FeatureDef(
                    name="complaints_90d", role="complaints", function=AggFunction.COUNT, window_days=30
                ),
                *_spec().features[1:],
            )
        }
    )
    assert other.hash != _spec().hash


def test_a_timestamp_never_reaches_a_spec_hash() -> None:
    """Two recipes saved a day apart, saying the same thing, must be the same recipe."""

    def recipe(created: dt.datetime, spec_id: str) -> OnboardingSpec:
        return OnboardingSpec(
            spec_id=spec_id,
            client_id="demo",
            use_case="some-use-case",
            entity_source_id="s1",
            event_source_ids=("s2",),
            mapping_ids=("m1", "m2"),
            feature_spec=_spec(),
            label_spec=LabelDefinition(
                name="churn_next_60d", type=LabelType.EVENT_ABSENCE, role="activity", horizon_days=60
            ),
            snapshot_spec=SnapshotDefinition(),
            created_at=created,
        )

    monday = recipe(NOW, "sp_1")
    tuesday = recipe(NOW + dt.timedelta(days=1), "sp_2")
    assert spec_hash(monday) == spec_hash(tuesday)
    assert monday.with_hash().hash == spec_hash(monday)


def test_a_hash_is_domain_separated_from_a_file_hash() -> None:
    assert _spec().hash.startswith("sha256:v1:")


# ---------------------------------------------------------------------------
# Onboarding specs
# ---------------------------------------------------------------------------
def _recipe(**changes: object) -> OnboardingSpec:
    base = {
        "spec_id": "sp_1",
        "client_id": "demo",
        "use_case": "some-use-case",
        "entity_source_id": "s1",
        "event_source_ids": ("s2", "s3"),
        "mapping_ids": ("m1",),
        "feature_spec": _spec(),
        "snapshot_spec": SnapshotDefinition(),
        "created_at": NOW,
    }
    return OnboardingSpec(**{**base, **changes})  # type: ignore[arg-type]


def test_a_source_cannot_be_both_the_entity_table_and_an_event_table() -> None:
    with pytest.raises(ValidationError, match="both"):
        _recipe(event_source_ids=("s1", "s2"))


def test_an_event_source_listed_twice_is_refused() -> None:
    with pytest.raises(ValidationError, match="more than once"):
        _recipe(event_source_ids=("s2", "s2"))


def test_the_scoring_snapshot_is_derived_not_configured() -> None:
    """Plan section 6.4: the user configures the training snapshots and nothing else."""
    recipe = _recipe(snapshot_spec=SnapshotDefinition(mode=SnapshotMode.PERIODIC, max_snapshots=12))
    scoring = recipe.scoring_snapshot_spec()
    assert scoring.mode is SnapshotMode.SINGLE
    assert scoring.max_snapshots == 1
    assert scoring.start is None and scoring.end is None
    # deriving it never mutates the training rule
    assert recipe.snapshot_spec.mode is SnapshotMode.PERIODIC


# ---------------------------------------------------------------------------
# Mappings
# ---------------------------------------------------------------------------
def _mapping(**changes: object) -> MappingSpec:
    base = {
        "mapping_id": "m1",
        "client_id": "demo",
        "source_id": "s1",
        "use_case": "some-use-case",
        "role": "entity",
        "columns": (
            MappingColumn(source="CUST_ID", standard="entity_key", confidence=1.0, decided_by=DecidedBy.AUTO),
            MappingColumn(
                source="TNR_MNTHS", standard="tenure_months", confidence=0.9, decided_by=DecidedBy.USER
            ),
        ),
        "created_at": NOW,
    }
    return MappingSpec(**{**base, **changes})  # type: ignore[arg-type]


def test_one_source_column_cannot_mean_two_standard_columns() -> None:
    with pytest.raises(ValidationError, match="claimed twice"):
        _mapping(
            columns=(
                MappingColumn(source="X", standard="a", confidence=1.0, decided_by=DecidedBy.AUTO),
                MappingColumn(source="X", standard="b", confidence=1.0, decided_by=DecidedBy.AUTO),
            )
        )


def test_two_source_columns_cannot_mean_one_standard_column() -> None:
    with pytest.raises(ValidationError, match="claimed twice"):
        _mapping(
            columns=(
                MappingColumn(source="A", standard="x", confidence=1.0, decided_by=DecidedBy.AUTO),
                MappingColumn(source="B", standard="x", confidence=1.0, decided_by=DecidedBy.AUTO),
            )
        )


def test_a_column_cannot_be_mapped_and_unmapped_at_once() -> None:
    with pytest.raises(ValidationError, match="CUST_ID"):
        _mapping(unmapped_source=("CUST_ID",))


def test_a_mapping_finds_its_columns_by_either_name() -> None:
    mapping = _mapping()
    assert mapping.by_standard("tenure_months") is not None
    assert mapping.by_standard("nope") is None
    assert mapping.source_names == ("CUST_ID", "TNR_MNTHS")


def test_a_mapping_hash_ignores_its_own_id_and_timestamp() -> None:
    assert spec_hash(_mapping()) == spec_hash(_mapping(mapping_id="m2", created_at=NOW + dt.timedelta(1)))


# ---------------------------------------------------------------------------
# Transforms
# ---------------------------------------------------------------------------
def test_each_transform_kind_demands_what_it_needs() -> None:
    assert ColumnTransform(kind=TransformKind.CAST, to_type=StandardType.NUMERIC)
    assert ColumnTransform(kind=TransformKind.SCALE, factor=0.01)
    assert ColumnTransform(kind=TransformKind.VALUE_MAP, value_map={"Y": True, "N": False})
    assert ColumnTransform(kind=TransformKind.NEGATE)
    for kind, match in (
        (TransformKind.CAST, "to_type"),
        (TransformKind.SCALE, "factor"),
        (TransformKind.DERIVE, "expression"),
        (TransformKind.DEDUPE, "by"),
        (TransformKind.VALUE_MAP, "value_map"),
    ):
        with pytest.raises(ValidationError, match=match):
            ColumnTransform(kind=kind)


def test_only_a_value_map_carries_a_value_map() -> None:
    with pytest.raises(ValidationError, match="only a value_map"):
        ColumnTransform(kind=TransformKind.NEGATE, value_map={"Y": True})


def test_only_a_cast_carries_a_date_format() -> None:
    assert ColumnTransform(kind=TransformKind.CAST, to_type=StandardType.DATE, date_format="%d/%m/%Y")
    with pytest.raises(ValidationError, match="only a cast"):
        ColumnTransform(kind=TransformKind.NEGATE, date_format="%d/%m/%Y")


# ---------------------------------------------------------------------------
# The dataset manifest
# ---------------------------------------------------------------------------
def _fingerprint() -> DatasetFingerprint:
    return DatasetFingerprint(hash="sha256:v1:abc", algorithm="sha256:v1", n_rows=10, columns=("a",))


def _manifest(**changes: object) -> DatasetManifest:
    base = {
        "dataset_id": "ds_1",
        "client_id": "demo",
        "use_case": "some-use-case",
        "spec_id": "sp_1",
        "spec_hash": "sha256:v1:aaa",
        "feature_spec_hash": "sha256:v1:bbb",
        "snapshot_mode": SnapshotMode.PERIODIC,
        "primary_key": ("customer_id", "snapshot_date"),
        "target": "churn_next_60d",
        "columns": (DatasetColumn(name="customer_id", type=StandardType.CATEGORICAL, origin="key"),),
        "n_rows": 100,
        "n_entities": 50,
        "snapshot_dates": (dt.date(2026, 1, 31), dt.date(2026, 2, 28)),
        "fingerprint": _fingerprint(),
        "built_at": NOW,
        "engine_version": "0.1.0",
    }
    return DatasetManifest(**{**base, **changes})  # type: ignore[arg-type]


def test_a_periodic_dataset_is_keyed_by_entity_and_snapshot() -> None:
    assert _manifest().primary_key == ("customer_id", "snapshot_date")


def test_a_periodic_dataset_with_a_one_column_key_is_refused() -> None:
    with pytest.raises(ValidationError, match="2-column primary key"):
        _manifest(primary_key=("customer_id",))


def test_a_single_snapshot_dataset_is_keyed_by_the_entity_alone() -> None:
    assert _manifest(snapshot_mode=SnapshotMode.SINGLE, primary_key=("customer_id",))
    with pytest.raises(ValidationError, match="1-column primary key"):
        _manifest(snapshot_mode=SnapshotMode.SINGLE)


# ---------------------------------------------------------------------------
# The dataset directory registry
# ---------------------------------------------------------------------------
def test_the_dataset_registry_is_separate_from_the_run_registry() -> None:
    """A dataset is not a run; plan section 7's run contract stays a run contract (DEC-102)."""
    from engine.contracts import ARTEFACT_REGISTRY

    assert not (set(DATASET_ARTEFACT_REGISTRY) & set(ARTEFACT_REGISTRY))


def test_every_dataset_artefact_the_plan_lists_is_known() -> None:
    # plan section 3: data/datasets/<dataset_id>/ holds these five files.
    assert {
        "dataset.parquet",
        "dataset_manifest.json",
        "build_report.json",
        "build_status.json",
        "sample.json",
    } | {"features.sql"} == DATASET_ARTEFACTS


def test_dataset_artefact_model_lists_what_it_knows_when_it_fails() -> None:
    with pytest.raises(KeyError) as excinfo:
        dataset_artefact_model("nope.json")
    assert "dataset_manifest.json" in str(excinfo.value)


# ---------------------------------------------------------------------------
# The role catalogue
# ---------------------------------------------------------------------------
def test_exactly_one_role_is_an_entity_role() -> None:
    roles = get_roles()
    assert roles.entity_role == "entity"
    assert roles.roles["entity"].kind is RoleKind.ENTITY
    assert all(roles.roles[name].is_event for name in roles.event_roles)


def test_every_event_role_requires_a_key_and_a_time() -> None:
    """Plan section 4.1: every event role has the same two required standard columns."""
    roles = get_roles()
    for name in roles.event_roles:
        assert set(roles.roles[name].required_columns) == {"entity_key", "event_time"}


def test_an_unknown_role_names_the_ones_that_exist() -> None:
    with pytest.raises(ConfigError, match="complaints"):
        get_roles().require("invoices")


@pytest.mark.parametrize("use_case_id", list_use_case_ids())
def test_every_shipped_use_case_carries_the_onboarding_defaults(use_case_id: str) -> None:
    config = load_use_case(use_case_id)
    assert config.onboarding.mapping.suggest_confidence <= config.onboarding.mapping.auto_accept_confidence
    assert config.onboarding.features.max_features >= 1
    assert config.onboarding.snapshots.agent_editable is False


@pytest.mark.parametrize("use_case_id", list_use_case_ids())
def test_every_shipped_use_case_passes_the_onboarding_checks(use_case_id: str) -> None:
    """The cross-field checks run here, because the config loader cannot run them (roles.py)."""
    validate_onboarding_config(load_use_case(use_case_id))
