"""`engine.onboarding.replay`: last month's recipe, pointed at this month's files (Plan A M35).

Pure and row-free, so every rule is exercised here on hand-built specs: which new file stands in for
which old one, what a copied mapping keeps and loses, when a file's mapping has to be reviewed, and
that only a replay with nothing missing becomes a recipe.
"""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import count

import pytest

from engine.config import get_roles, load_use_case
from engine.contracts import DatasetFingerprint
from engine.onboarding.replay import ReplayPlan, plan_replay, replayed_spec
from engine.onboarding.specs import (
    ColumnTransform,
    DecidedBy,
    FeatureSpec,
    MappingColumn,
    MappingSpec,
    OnboardingSpec,
    SnapshotDefinition,
    SourceSpec,
    StandardType,
    TransformKind,
)

NOW = datetime(2026, 9, 23, tzinfo=UTC)
CLIENT = "c_replay_1"
USE_CASE = "telco-churn"

CUSTOMER_COLUMNS = ("CUST_ID", "Plan Type", "DND_FLAG")
COMPLAINT_COLUMNS = ("CUST_ID", "TICKET_DT", "SEVERITY")


def _source(source_id: str, file_name: str, columns: tuple[str, ...], role: str | None = None) -> SourceSpec:
    return SourceSpec(
        source_id=source_id,
        client_id=CLIENT,
        file_name=file_name,
        storage_key=f"clients/{CLIENT}/sources/{source_id}/raw.csv",
        file_format="csv",
        role=role,
        rows=10,
        columns=columns,
        fingerprint=DatasetFingerprint(
            hash="sha256:v1:deadbeef", algorithm="sha256:v1", n_rows=10, columns=columns
        ),
        created_at=NOW,
    )


def _column(source: str, standard: str, transform: ColumnTransform | None = None) -> MappingColumn:
    return MappingColumn(
        source=source, standard=standard, transform=transform, confidence=0.9, decided_by=DecidedBy.AUTO
    )


PLAN_MAP = ColumnTransform(kind=TransformKind.VALUE_MAP, value_map={"PRE": "prepaid", "POST": "postpaid"})

RECIPE_SOURCES = {
    "src_old_customers": _source("src_old_customers", "customers.csv", CUSTOMER_COLUMNS, role="entity"),
    "src_old_complaints": _source(
        "src_old_complaints", "complaints.csv", COMPLAINT_COLUMNS, role="complaints"
    ),
}
RECIPE_MAPPINGS = (
    MappingSpec(
        mapping_id="map_old_customers",
        client_id=CLIENT,
        source_id="src_old_customers",
        use_case=USE_CASE,
        role="entity",
        columns=(
            _column("CUST_ID", "entity_key"),
            _column("Plan Type", "plan_type", PLAN_MAP),
            _column("DND_FLAG", "marketing_opt_in", ColumnTransform(kind=TransformKind.NEGATE)),
        ),
        value_maps={"plan_type": {"PRE": "prepaid", "POST": "postpaid"}},
        created_at=NOW,
    ),
    MappingSpec(
        mapping_id="map_old_complaints",
        client_id=CLIENT,
        source_id="src_old_complaints",
        use_case=USE_CASE,
        role="complaints",
        columns=(
            _column("CUST_ID", "entity_key"),
            _column(
                "TICKET_DT", "event_time", ColumnTransform(kind=TransformKind.CAST, to_type=StandardType.DATE)
            ),
            _column("SEVERITY", "severity"),
        ),
        created_at=NOW,
    ),
)
SPEC = OnboardingSpec(
    spec_id="spec_old",
    client_id=CLIENT,
    use_case=USE_CASE,
    entity_source_id="src_old_customers",
    event_source_ids=("src_old_complaints",),
    mapping_ids=("map_old_complaints", "map_old_customers"),
    feature_spec=FeatureSpec(features=()),
    label_spec=None,
    snapshot_spec=SnapshotDefinition(),
    created_at=NOW,
)


def _plan(
    new_sources: tuple[SourceSpec, ...],
    *,
    detected: dict[str, str | None] | None = None,
    settled: dict[str, MappingSpec] | None = None,
) -> ReplayPlan:
    ids = count(1)
    return plan_replay(
        SPEC,
        recipe_sources=RECIPE_SOURCES,
        recipe_mappings=RECIPE_MAPPINGS,
        new_sources=new_sources,
        detected_roles=detected or {},
        settled=settled or {},
        schema=load_use_case(USE_CASE).standard_schema,
        roles=get_roles(),
        new_mapping_id=lambda: f"map_new_{next(ids)}",
    )


def test_files_with_the_same_names_and_columns_replay_with_nothing_to_review() -> None:
    plan = _plan(
        (
            _source("src_new_complaints", "complaints.csv", COMPLAINT_COLUMNS),
            _source("src_new_customers", "customers.csv", CUSTOMER_COLUMNS),
        )
    )
    assert plan.ready
    assert [(s.source_id, s.role, s.replaces_source_id) for s in plan.sources] == [
        ("src_new_customers", "entity", "src_old_customers"),
        ("src_new_complaints", "complaints", "src_old_complaints"),
    ]
    assert all(not s.missing_columns and not s.message for s in plan.sources)
    assert not plan.unmatched and not plan.unused


def test_a_copied_mapping_keeps_every_decision_and_points_at_the_new_file() -> None:
    plan = _plan(
        (
            _source("src_new_customers", "customers.csv", CUSTOMER_COLUMNS),
            _source("src_new_complaints", "complaints.csv", COMPLAINT_COLUMNS),
        )
    )
    copied = {mapping.source_id: mapping for mapping in plan.mappings}["src_new_customers"]
    old = RECIPE_MAPPINGS[0]
    assert copied.mapping_id not in {m.mapping_id for m in RECIPE_MAPPINGS}
    assert copied.source_id == "src_new_customers"
    assert copied.columns == old.columns
    assert copied.value_maps == old.value_maps


def test_a_confirmed_role_outranks_the_file_name_and_the_name_outranks_detection() -> None:
    renamed = _source("src_a", "subscribers_2025_03.csv", CUSTOMER_COLUMNS, role="entity")
    decoy = _source("src_b", "customers.csv", CUSTOMER_COLUMNS)
    complaints = _source("src_c", "tickets.csv", COMPLAINT_COLUMNS)
    plan = _plan((decoy, complaints, renamed), detected={"src_c": "complaints", "src_b": "entity"})
    by_old = {s.replaces_source_id: s.source_id for s in plan.sources}
    assert by_old == {"src_old_customers": "src_a", "src_old_complaints": "src_c"}
    assert [source.source_id for source in plan.unused] == ["src_b"]


def test_a_missing_column_is_dropped_from_the_copy_and_reopens_only_that_file() -> None:
    plan = _plan(
        (
            _source("src_new_customers", "customers.csv", CUSTOMER_COLUMNS),
            _source("src_new_complaints", "complaints.csv", ("CUST_ID", "TICKET_DT", "SEV_LEVEL")),
        )
    )
    assert not plan.ready
    reopened = [s for s in plan.sources if s.missing_columns]
    assert [(s.source_id, s.missing_columns) for s in reopened] == [("src_new_complaints", ("SEVERITY",))]
    assert "SEVERITY" in reopened[0].message
    copied = {m.source_id: m for m in plan.mappings}["src_new_complaints"]
    assert "SEVERITY" not in copied.source_names
    assert "SEV_LEVEL" in copied.unmapped_source
    with pytest.raises(ValueError):
        replayed_spec(SPEC, plan, spec_id="spec_new")


def test_a_required_column_that_goes_missing_is_listed_as_missing_again() -> None:
    plan = _plan(
        (
            _source("src_new_customers", "customers.csv", CUSTOMER_COLUMNS),
            _source("src_new_complaints", "complaints.csv", ("CUST_ID", "SEVERITY")),
        )
    )
    copied = {m.source_id: m for m in plan.mappings}["src_new_complaints"]
    assert "event_time" in copied.missing_required


def test_a_mapping_the_user_settled_stands_and_closes_the_review() -> None:
    new = (
        _source("src_new_customers", "customers.csv", CUSTOMER_COLUMNS),
        _source("src_new_complaints", "complaints.csv", ("CUST_ID", "TICKET_DT", "SEV_LEVEL")),
    )
    first = _plan(new)
    fixed = {m.source_id: m for m in first.mappings}["src_new_complaints"]
    fixed = fixed.model_copy(update={"columns": (*fixed.columns, _column("SEV_LEVEL", "severity"))})
    second = _plan(new, settled={"src_new_complaints": fixed})
    assert second.ready
    entry = {s.source_id: s for s in second.sources}["src_new_complaints"]
    assert entry.settled_by_user and entry.mapping_id == fixed.mapping_id
    spec = replayed_spec(SPEC, second, spec_id="spec_new")
    assert spec.entity_source_id == "src_new_customers"
    assert spec.event_source_ids == ("src_new_complaints",)
    assert fixed.mapping_id in spec.mapping_ids
    assert spec.feature_spec == SPEC.feature_spec and spec.snapshot_spec == SPEC.snapshot_spec


def test_a_table_this_month_does_not_have_is_unmatched_and_says_so() -> None:
    plan = _plan((_source("src_new_customers", "customers.csv", CUSTOMER_COLUMNS),))
    assert not plan.ready
    assert [(u.source_id, u.role) for u in plan.unmatched] == [("src_old_complaints", "complaints")]
    assert "complaints.csv" in plan.unmatched[0].message
