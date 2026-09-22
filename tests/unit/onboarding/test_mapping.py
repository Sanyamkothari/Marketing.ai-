"""Unit tests for `engine.onboarding.mapping` (Phase 2 plan sections 6.1 and 7, M9).

The schemas here are built in the test rather than loaded from a shipped use case. A mapping is
scored entirely out of `StandardColumn` - aliases, type, value vocabulary, range - so a schema
written next to the assertions says exactly which signal each test is pinning, and no test breaks
because a YAML somebody else owns gained an alias. `profile_source` still needs a `UseCaseConfig`,
and the first predictive id is used for it exactly as `tests/unit/onboarding/test_sources.py` does,
because it is only ever read for its use-case-independent `.catalog`.

The messy-name frame is the one from the plan: `CUST_ID`, `TNR_MNTHS`, `Plan Type`, `DND_FLAG`.
"""

from __future__ import annotations

import datetime

import pandas as pd
import pytest

from engine.config import (
    RoleSpec,
    StandardColumn,
    StandardSchemaConfig,
    StandardType,
    UseCaseConfig,
    get_roles,
    load_use_case,
)
from engine.contracts import ColumnProfile, Severity
from engine.onboarding.mapping import (
    HeuristicMappingSuggester,
    MappingCandidate,
    apply_mapping,
    range_plausibility,
    suggested_mapping_spec,
    type_compatibility,
    value_overlap,
)
from engine.onboarding.sources import profile_source
from engine.onboarding.specs import (
    ColumnTransform,
    DecidedBy,
    MappingColumn,
    MappingSpec,
    SourceProfile,
    TransformKind,
)
from tests.fixtures.make_data import predictive_use_case_ids

pytestmark = pytest.mark.filterwarnings("ignore::UserWarning")

ROWS = 60


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def config() -> UseCaseConfig:
    return load_use_case(predictive_use_case_ids()[0])


def entity_schema() -> StandardSchemaConfig:
    """The one-row-per-entity shape: one column per signal the scorer is supposed to read."""
    return StandardSchemaConfig(
        entity_key="customer_id",
        columns=(
            StandardColumn(
                name="tenure_months",
                type=StandardType.NUMERIC,
                aliases=("tenure", "tnr_mnths", "months_active"),
                range=(0.0, 600.0),
            ),
            StandardColumn(
                name="plan_type",
                type=StandardType.CATEGORICAL,
                aliases=("plan", "tariff", "package"),
                value_aliases={"prepaid": ("pre", "ppd"), "postpaid": ("post", "postpay")},
            ),
            StandardColumn(
                name="marketing_opt_in",
                type=StandardType.BOOLEAN,
                aliases=("opt_in", "consent", "dnd_flag", "dnd"),
                value_aliases={"true": ("y", "yes", "1"), "false": ("n", "no", "0")},
            ),
            StandardColumn(
                name="monthly_charges",
                type=StandardType.NUMERIC,
                aliases=("mrc", "arpu"),
                range=(0.0, 100_000.0),
            ),
            StandardColumn(
                name="signup_date",
                type=StandardType.DATE,
                required=True,
                aliases=("signup_dt", "start_date"),
            ),
        ),
    )


def messy_frame() -> pd.DataFrame:
    """A customer master spelled the way a client's extract actually arrives."""
    return pd.DataFrame(
        {
            "CUST_ID": [f"C{index:05d}" for index in range(ROWS)],
            "TNR_MNTHS": [index % 48 for index in range(ROWS)],
            "Plan Type": ["PRE" if index % 2 else "POST" for index in range(ROWS)],
            "DND_FLAG": ["Y" if index % 3 else "N" for index in range(ROWS)],
            "MRC": [100.0 + index for index in range(ROWS)],
            "SIGNUP_DT": [f"2021-{(index % 12) + 1:02d}-05" for index in range(ROWS)],
        }
    )


def profile_of(frame: pd.DataFrame, config: UseCaseConfig, *, file_name: str = "master.csv") -> SourceProfile:
    return profile_source(
        frame,
        config,
        source_id="src_1",
        client_id="cli_1",
        file_name=file_name,
        roles=get_roles(),
    )


def column_profile(frame: pd.DataFrame, config: UseCaseConfig, name: str) -> ColumnProfile:
    columns = profile_of(frame, config).profile.columns
    return next(column for column in columns if column.name == name)


def entity_role() -> RoleSpec:
    return get_roles().require(get_roles().entity_role)


def mapping_of(*columns: MappingColumn) -> MappingSpec:
    return MappingSpec(
        mapping_id="map_1",
        client_id="cli_1",
        source_id="src_1",
        use_case="uc",
        role="entity",
        columns=columns,
        created_at=datetime.datetime.now(datetime.UTC),
    )


def suggest(
    frame: pd.DataFrame, config: UseCaseConfig, schema: StandardSchemaConfig
) -> dict[str, MappingCandidate]:
    candidates = HeuristicMappingSuggester(suggest_confidence=0.5, max_categorical_levels=50).suggest(
        profile_of(frame, config), schema, entity_role()
    )
    return {candidate.standard: candidate for candidate in candidates}


# ---------------------------------------------------------------------------
# Suggestion
# ---------------------------------------------------------------------------
def test_messy_client_names_reach_their_standard_columns(config: UseCaseConfig) -> None:
    """The whole point: four differently-mangled names, four correct standard columns."""
    found = suggest(messy_frame(), config, entity_schema())

    assert {standard: candidate.source for standard, candidate in found.items()} == {
        "entity_key": "CUST_ID",
        "tenure_months": "TNR_MNTHS",
        "plan_type": "Plan Type",
        "marketing_opt_in": "DND_FLAG",
        "monthly_charges": "MRC",
        "signup_date": "SIGNUP_DT",
    }
    assert found["tenure_months"].confidence == 1.0


def test_a_y_n_column_gets_a_value_map_built_from_the_value_aliases(config: UseCaseConfig) -> None:
    frame = messy_frame().rename(columns={"DND_FLAG": "CONSENT"})

    transform = suggest(frame, config, entity_schema())["marketing_opt_in"].transform

    assert transform is not None
    assert transform.kind is TransformKind.VALUE_MAP
    assert transform.value_map == {"Y": True, "N": False}


def test_an_inverted_flag_is_negated_rather_than_mapped_value_for_value(config: UseCaseConfig) -> None:
    """`DND_FLAG` is `marketing_opt_in` stated backwards: Y on the file means False for us."""
    frame = messy_frame()
    candidate = suggest(frame, config, entity_schema())["marketing_opt_in"]
    assert candidate.transform is not None
    assert candidate.transform.kind is TransformKind.NEGATE

    mapped = apply_mapping(
        frame,
        mapping_of(
            MappingColumn(
                source="DND_FLAG",
                standard="marketing_opt_in",
                transform=candidate.transform,
                confidence=1.0,
                decided_by=DecidedBy.AUTO,
            )
        ),
        schema=entity_schema(),
        auto_accept_confidence=0.85,
    )
    assert mapped.frame["marketing_opt_in"].tolist() == [index % 3 == 0 for index in range(ROWS)]


def test_a_source_column_claimed_by_one_standard_column_is_never_claimed_again(
    config: UseCaseConfig,
) -> None:
    """Two columns alias `monthly_charges`; the loser becomes an alternative, not a second claim."""
    frame = messy_frame().assign(ARPU=lambda f: f["MRC"] * 1.1)

    found = suggest(frame, config, entity_schema())
    winner = found["monthly_charges"]
    loser = "ARPU" if winner.source == "MRC" else "MRC"

    assert winner.source in {"MRC", "ARPU"}
    assert loser in winner.alternatives
    assert loser not in {candidate.source for candidate in found.values()}


def test_the_entity_key_is_never_offered_as_an_ordinary_column(config: UseCaseConfig) -> None:
    """A schema column whose alias *is* the key column must not be given the key column."""
    schema = StandardSchemaConfig(
        entity_key="customer_id",
        columns=(StandardColumn(name="account_ref", type=StandardType.TEXT, aliases=("cust_id",)),),
    )

    found = suggest(messy_frame(), config, schema)

    assert found["entity_key"].source == "CUST_ID"
    assert "CUST_ID" not in {
        candidate.source for standard, candidate in found.items() if standard != "entity_key"
    }


def test_a_required_column_with_nothing_to_map_it_to_is_reported(config: UseCaseConfig) -> None:
    """A billing extract with no date column can never be point-in-time correct, and says so."""
    frame = pd.DataFrame(
        {
            "CUST_ID": [f"C{index:05d}" for index in range(ROWS)],
            "AMOUNT": [10.0 + index for index in range(ROWS)],
            "STATUS": ["paid" if index % 2 else "unpaid" for index in range(ROWS)],
        }
    )

    spec = suggested_mapping_spec(
        profile_of(frame, config, file_name="bills.csv"),
        config.model_copy(update={"standard_schema": entity_schema()}),
        role="bills",
        use_case=config.id,
        mapping_id="map_1",
    )

    assert spec.missing_required == ("event_time",)
    assert spec.by_standard("entity_key") is not None


def test_the_suggested_spec_auto_accepts_only_above_the_configured_bar(config: UseCaseConfig) -> None:
    schema = entity_schema()
    spec = suggested_mapping_spec(
        profile_of(messy_frame(), config),
        config.model_copy(update={"standard_schema": schema}),
        role=get_roles().entity_role,
        use_case=config.id,
        mapping_id="map_1",
    )
    bar = config.onboarding.mapping.auto_accept_confidence

    assert spec.unmapped_source == ()
    assert spec.value_maps["plan_type"] == {"POST": "postpaid", "PRE": "prepaid"}
    for column in spec.columns:
        expected = DecidedBy.AUTO if column.confidence >= bar else DecidedBy.USER
        assert column.decided_by is expected


# ---------------------------------------------------------------------------
# The signals
# ---------------------------------------------------------------------------
def test_a_number_is_only_a_category_while_it_has_few_enough_levels(config: UseCaseConfig) -> None:
    frame = pd.DataFrame({"code": list(range(ROWS))})
    column = column_profile(frame, config, "code")

    assert type_compatibility(column, StandardType.CATEGORICAL, max_levels=ROWS) == 1.0
    assert type_compatibility(column, StandardType.CATEGORICAL, max_levels=10) == 0.0


def test_value_overlap_scores_the_vocabulary_and_not_the_spelling(config: UseCaseConfig) -> None:
    """`PRE`/`POST` are the expected values; `gold`/`silver` are somebody else's column."""
    aliases = {"prepaid": ("pre", "ppd"), "postpaid": ("post", "postpay")}
    frame = pd.DataFrame(
        {
            "theirs": ["PRE" if index % 2 else "POST" for index in range(ROWS)],
            "other": ["gold" if index % 2 else "silver" for index in range(ROWS)],
        }
    )

    assert value_overlap(column_profile(frame, config, "theirs"), aliases) == 1.0
    assert value_overlap(column_profile(frame, config, "other"), aliases) == 0.0


def test_a_column_whose_values_leave_the_configured_range_is_not_plausible(config: UseCaseConfig) -> None:
    frame = pd.DataFrame({"months": [index % 48 for index in range(ROWS)]})
    column = column_profile(frame, config, "months")

    assert range_plausibility(column, (0.0, 600.0)) == 1.0
    assert range_plausibility(column, (0.0, 12.0)) == 0.0


# ---------------------------------------------------------------------------
# apply_mapping
# ---------------------------------------------------------------------------
def test_an_unmapped_source_column_never_reaches_the_built_frame() -> None:
    frame = messy_frame()
    mapping = mapping_of(
        MappingColumn(
            source="TNR_MNTHS",
            standard="tenure_months",
            transform=ColumnTransform(kind=TransformKind.CAST, to_type=StandardType.NUMERIC),
            confidence=1.0,
            decided_by=DecidedBy.AUTO,
        )
    )

    result = apply_mapping(frame, mapping, schema=entity_schema(), auto_accept_confidence=0.85)

    assert list(result.frame.columns) == ["tenure_months"]
    assert result.dropped == ("CUST_ID", "DND_FLAG", "MRC", "Plan Type", "SIGNUP_DT")
    assert list(frame.columns) == list(messy_frame().columns)


def test_applying_the_same_mapping_twice_gives_the_same_answer() -> None:
    """Next month's file is built without a human, so a mapping has to be a pure function."""
    frame = messy_frame()
    mapping = mapping_of(
        MappingColumn(source="CUST_ID", standard="entity_key", confidence=1.0, decided_by=DecidedBy.AUTO),
        MappingColumn(
            source="Plan Type",
            standard="plan_type",
            transform=ColumnTransform(
                kind=TransformKind.VALUE_MAP, value_map={"PRE": "prepaid", "POST": "postpaid"}
            ),
            confidence=1.0,
            decided_by=DecidedBy.AUTO,
        ),
    )

    first = apply_mapping(frame, mapping, schema=entity_schema(), auto_accept_confidence=0.85)
    second = apply_mapping(frame, mapping, schema=entity_schema(), auto_accept_confidence=0.85)

    pd.testing.assert_frame_equal(first.frame, second.frame)
    assert first.checks == second.checks == ()


def test_a_cast_most_values_fail_is_an_error_naming_the_share_and_one_of_them() -> None:
    frame = pd.DataFrame({"TNR_MNTHS": [str(index) for index in range(17)] + ["n/a", "n/a", "n/a"]})
    mapping = mapping_of(
        MappingColumn(
            source="TNR_MNTHS",
            standard="tenure_months",
            transform=ColumnTransform(kind=TransformKind.CAST, to_type=StandardType.NUMERIC),
            confidence=1.0,
            decided_by=DecidedBy.AUTO,
        )
    )

    (check,) = apply_mapping(frame, mapping, schema=entity_schema(), auto_accept_confidence=0.85).checks

    assert check.code == "MAPPING_TYPE_CONFLICT"
    assert check.severity is Severity.ERROR
    assert "15.0%" in check.message
    assert "n/a" in check.message
    assert check.details["failed"] == 3


def test_values_the_map_does_not_cover_are_listed_for_the_user() -> None:
    frame = messy_frame()
    mapping = mapping_of(
        MappingColumn(
            source="Plan Type",
            standard="plan_type",
            transform=ColumnTransform(kind=TransformKind.VALUE_MAP, value_map={"PRE": "prepaid"}),
            confidence=1.0,
            decided_by=DecidedBy.AUTO,
        )
    )

    (check,) = apply_mapping(frame, mapping, schema=entity_schema(), auto_accept_confidence=0.85).checks

    assert check.code == "VALUE_UNMAPPED"
    assert check.severity is Severity.WARNING
    assert check.details["values"] == ["POST"]
    assert "POST" in check.message


def test_a_shaky_match_is_flagged_once_for_the_column_it_is_about() -> None:
    frame = messy_frame()
    mapping = mapping_of(
        MappingColumn(source="CUST_ID", standard="entity_key", confidence=0.61, decided_by=DecidedBy.USER),
        MappingColumn(source="MRC", standard="monthly_charges", confidence=0.99, decided_by=DecidedBy.AUTO),
    )

    (check,) = apply_mapping(frame, mapping, schema=entity_schema(), auto_accept_confidence=0.85).checks

    assert check.code == "MAPPING_LOW_CONFIDENCE"
    assert check.column == "CUST_ID"
    assert "61%" in check.message and "85%" in check.message
