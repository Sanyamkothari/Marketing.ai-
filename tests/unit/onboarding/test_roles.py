"""The role catalogue, and the cross-field checks the config loader cannot run itself.

These checks live in `engine/onboarding/roles.py` rather than in `UseCaseConfig._cross_field`
because `PARALLEL_WORK_PROTOCOL.md` §4 confines this branch to one marked block in
`engine/config.py`. That moves the moment of failure from config load to onboarding, so the last
test here is the one that keeps CI honest: every shipped use case passes, every time.
"""

from __future__ import annotations

import pytest

from engine.config import (
    ConfigError,
    FeatureDef,
    LabelDefinition,
    LabelType,
    RoleKind,
    StandardColumn,
    StandardSchemaConfig,
    StandardType,
    get_roles,
    list_use_case_ids,
    load_use_case,
)
from engine.onboarding.roles import check_label, check_suggested_features, validate_onboarding_config


@pytest.fixture(scope="module")
def roles():
    return get_roles()


# ---------------------------------------------------------------------------
# The catalogue itself
# ---------------------------------------------------------------------------
def test_exactly_one_role_is_the_entity_role(roles) -> None:
    assert roles.entity_role == "entity"
    assert roles.roles["entity"].kind is RoleKind.ENTITY
    assert "entity" not in roles.event_roles


def test_every_event_role_requires_a_key_and_a_time(roles) -> None:
    """Plan section 4.1: every event role has the same two required standard columns."""
    assert roles.event_roles, "roles.yaml defines no event roles"
    for name in roles.event_roles:
        assert set(roles.roles[name].required_columns) == {"entity_key", "event_time"}
        assert roles.roles[name].is_event


def test_the_plan_s_roles_are_all_present(roles) -> None:
    # Transcribed from Phase 2 plan section 4.1, never parsed from the YAML.
    for name in (
        "entity",
        "bills",
        "payments",
        "complaints",
        "usage",
        "campaign_events",
        "orders",
        "activity",
        "other_event",
    ):
        assert roles.get(name) is not None, f"roles.yaml is missing {name!r}"


def test_campaign_events_keeps_treatment_for_phase_three(roles) -> None:
    """Plan section 14: uplift modelling needs a treatment flag; keep it a standard column."""
    assert "treatment" in roles.roles["campaign_events"].typical_columns


def test_an_unknown_role_names_the_ones_that_exist(roles) -> None:
    with pytest.raises(ConfigError, match="complaints") as error:
        roles.require("invoices")
    assert error.value.code == "ROLE_UNKNOWN"


# ---------------------------------------------------------------------------
# Suggested features
# ---------------------------------------------------------------------------
def _config(**changes):
    return load_use_case("telco-churn").model_copy(update=changes)


def test_two_suggested_features_with_one_name_are_refused(roles) -> None:
    config = _config(
        suggested_features=(
            FeatureDef(name="complaints_90d", role="complaints", function="count", window_days=90),
            FeatureDef(name="complaints_90d", role="complaints", function="count", window_days=30),
        )
    )
    with pytest.raises(ConfigError, match="more than once") as error:
        check_suggested_features(config, roles)
    assert error.value.code == "SUGGESTED_FEATURE_DUPLICATE"


def test_a_feature_may_not_be_named_after_a_standard_column(roles) -> None:
    config = _config(
        standard_schema=StandardSchemaConfig(
            columns=(StandardColumn(name="tenure_months", type=StandardType.NUMERIC),)
        ),
        suggested_features=(
            FeatureDef(name="tenure_months", role="complaints", function="count", window_days=90),
        ),
    )
    with pytest.raises(ConfigError, match="can only mean one thing") as error:
        check_suggested_features(config, roles)
    assert error.value.code == "FEATURE_NAME_COLLISION"


def test_a_feature_may_not_be_named_after_the_key_or_the_snapshot(roles) -> None:
    """The two names the engine owns. They are per use case, so they are read off the config."""
    base = load_use_case("telco-churn")
    assert base.standard_schema.reserved_names == ("customer_id", "snapshot_date")
    for reserved in base.standard_schema.reserved_names:
        config = _config(suggested_features=(FeatureDef(name=reserved, role="complaints", function="count"),))
        with pytest.raises(ConfigError, match="can only mean one thing"):
            check_suggested_features(config, roles)


def test_a_feature_naming_an_unknown_role_is_refused_with_its_position(roles) -> None:
    config = _config(
        suggested_features=(
            FeatureDef(name="a_90d", role="complaints", function="count", window_days=90),
            FeatureDef(name="b_90d", role="invoices", function="count", window_days=90),
        )
    )
    with pytest.raises(ConfigError) as error:
        check_suggested_features(config, roles)
    assert error.value.path == "suggested_features[1].role"


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------
def test_a_column_label_must_agree_with_the_target(roles) -> None:
    config = _config(label=LabelDefinition(name="x", type=LabelType.COLUMN, column="not_churn"))
    with pytest.raises(ConfigError, match="different columns") as error:
        check_label(config, roles)
    assert error.value.code == "LABEL_TARGET_MISMATCH"


def test_a_column_label_that_agrees_with_the_target_passes(roles) -> None:
    config = _config(label=LabelDefinition(name="Churn", type=LabelType.COLUMN, column="Churn"))
    check_label(config, roles)


def test_a_forward_looking_label_needs_an_event_role(roles) -> None:
    config = _config(
        label=LabelDefinition(
            name="churn_next_60d", type=LabelType.EVENT_ABSENCE, role="entity", horizon_days=60
        )
    )
    with pytest.raises(ConfigError, match="must be an event") as error:
        check_label(config, roles)
    assert error.value.code == "LABEL_ROLE_NOT_EVENT"


def test_a_forward_looking_label_on_an_event_role_passes(roles) -> None:
    config = _config(
        label=LabelDefinition(
            name="churn_next_60d", type=LabelType.EVENT_ABSENCE, role="activity", horizon_days=60
        )
    )
    check_label(config, roles)


def test_a_label_naming_an_unknown_role_is_refused(roles) -> None:
    config = _config(
        label=LabelDefinition(name="x", type=LabelType.EVENT_PRESENCE, role="nope", horizon_days=30)
    )
    with pytest.raises(ConfigError) as error:
        check_label(config, roles)
    assert error.value.code == "ROLE_UNKNOWN"


def test_no_label_is_always_fine(roles) -> None:
    check_label(_config(label=None), roles)


# ---------------------------------------------------------------------------
# The CI gate that pays for moving these checks out of the config loader
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("use_case_id", list_use_case_ids())
def test_every_shipped_use_case_passes(use_case_id: str) -> None:
    validate_onboarding_config(load_use_case(use_case_id))


def test_the_default_catalogue_is_used_when_none_is_given() -> None:
    validate_onboarding_config(load_use_case("telco-churn"))
