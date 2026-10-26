"""The order-log path's configuration (v3 phase 0, B1): the switch, the template, the use case's declarations and the
two hidden roles. While the switch is off none of it moves what a person sees or what a run records."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from engine.config import (
    ConfigError,
    SplitType,
    UseCaseConfig,
    get_roles,
    load_all_use_cases,
    load_engine_config,
    load_use_case,
    overridable_paths,
    recipe_from_config,
    resolve_config,
)
from engine.doctor.config import AudienceConfig, DataDoctorConfig
from engine.doctor.outcomes import load_outcome_catalogue
from engine.onboarding.roles import validate_onboarding_config
from engine.onboarding.sources import detect_roles, profile_source
from tests.fixtures.order_log import order_log_frame
from tests.unit.doctor.order_log_support import USE_CASE, loaded


def test_the_switch_is_off_everywhere_and_left_out_of_a_dump(config_root: Path) -> None:
    assert load_engine_config(config_root).defaults["data_doctor"]["order_log"] is False
    assert DataDoctorConfig().order_log is False
    for use_case_id, config in load_all_use_cases(config_root).items():
        assert config.data_doctor.order_log is False, use_case_id
    unified = DataDoctorConfig(unified=True)
    assert unified.model_dump(mode="json")["unified"] is True and "order_log" not in unified.model_dump(
        mode="json"
    )
    assert DataDoctorConfig(order_log=True).model_dump(mode="json")["order_log"] is True


def test_while_off_a_run_config_holds_no_trace_of_the_path(config_root: Path) -> None:
    dumped = resolve_config("retail-win-back", root=config_root).model_dump(mode="json")
    assert "data_doctor" not in dumped["config"]
    unified = load_use_case(USE_CASE, config_root).model_copy(
        update={"data_doctor": DataDoctorConfig(unified=True)}
    )
    assert unified.model_dump(mode="json")["data_doctor"] == {"unified": True}


@pytest.mark.parametrize("on", [False, True])
def test_the_switch_round_trips_and_stays_out_of_the_recipe_hash(config_root: Path, on: bool) -> None:
    base = load_use_case(USE_CASE, config_root)
    switched = base.model_copy(update={"data_doctor": DataDoctorConfig(unified=True, order_log=on)})
    assert UseCaseConfig.model_validate_json(switched.model_dump_json()) == switched
    assert UseCaseConfig.model_validate(switched.model_dump(mode="json")).data_doctor.order_log is on
    kwargs = {"primary_key": "customer_id", "feature_columns": ("visits_last_7d",), "seed": 7}
    assert (
        recipe_from_config(switched, **kwargs).recipe_hash == recipe_from_config(base, **kwargs).recipe_hash
    )


@pytest.mark.parametrize("bad", ["yes", "true", 1, None])
def test_the_switch_is_a_real_boolean(bad: object) -> None:
    with pytest.raises(ValidationError):
        DataDoctorConfig.model_validate({"order_log": bad})


def test_a_run_cannot_override_the_switch(config_root: Path) -> None:
    assert "data_doctor.order_log" not in overridable_paths(load_use_case(USE_CASE, config_root))
    with pytest.raises(ConfigError) as caught:
        resolve_config(USE_CASE, {"data_doctor.order_log": True}, root=config_root)
    assert caught.value.code == "OVERRIDE_UNKNOWN_PATH"


def test_the_templates_marked_for_the_path_are_the_two_the_owner_chose(config_root: Path) -> None:
    catalogue = load_outcome_catalogue(config_root)
    assert {name for name, t in catalogue.templates.items() if t.order_log} == {
        "retail-win-back",
        "win-back-campaign",
    }
    retail = catalogue.templates["retail-win-back"]
    assert (retail.question, retail.label_type, retail.event_kinds, retail.days, retail.default_days) == (
        "When has a lapsed shopper come back?",
        "event_presence",
        ("orders", "bills", "other_event"),
        (30, 60, 90),
        90,
    )
    assert retail.sentence == catalogue.templates["win-back-campaign"].sentence
    assert retail.order_log_only and not catalogue.templates["win-back-campaign"].order_log_only
    assert not catalogue.templates["targeted-advertisement"].order_log


def test_retail_win_back_declares_its_label_audience_features_and_built_split(config_root: Path) -> None:
    config = load_use_case("retail-win-back", config_root)
    validate_onboarding_config(config, get_roles(config_root))
    # `label` and `suggested_features` stay unset: every onboarding, pilot and measurement path reads them.
    assert config.label is None and config.suggested_features == ()
    label = config.order_log_label
    assert label is not None
    assert (label.name, label.type.value, label.role, label.horizon_days) == (
        "reactivated_90d",
        "event_presence",
        "orders",
        90,
    )
    assert label.where is not None and (label.where.column, label.where.op.value, label.where.value) == (
        "quantity",
        "gt",
        0,
    )
    assert config.audience == AudienceConfig(role="orders", no_event_days=90)
    assert config.built_split is not None
    assert (config.built_split.type, config.built_split.time_column) == (
        SplitType.TIME_BASED,
        "snapshot_date",
    )
    # The shipped one-snapshot file keeps its split: a time split of one date cannot be made.
    assert config.split.type is SplitType.RANDOM_STRATIFIED and config.target.column == "reactivated_90d"
    by_role: dict[str, list[str]] = {}
    for feature in config.order_log_features:
        by_role.setdefault(feature.role, []).append(feature.name)
    assert by_role == {
        "orders": [
            "recency_days",
            "tenure_days",
            "orders_total",
            "spend_total",
            "order_value_mean",
            "order_value_max",
            "active_months",
            "days_between_orders_mean",
            "country",
        ],
        "order_lines": ["items_total", "distinct_products"],
        "returns": ["returned_orders", "returned_items"],
    }
    template = {column.name for column in config.template.columns}
    assert {name for names in by_role.values() for name in names if name != "country"} <= template | {
        "country"
    }


def test_only_retail_win_back_gains_the_declarations(config_root: Path) -> None:
    names = ("audience", "built_split", "order_log_label", "order_log_features")
    for use_case_id, config in load_all_use_cases(config_root).items():
        dump = config.model_dump(mode="json")
        if use_case_id == "retail-win-back":
            assert all(dump[name] for name in names)
        else:
            assert not set(names) & set(dump), use_case_id


def test_an_order_log_feature_or_label_is_checked_like_a_suggested_one(config_root: Path) -> None:
    config = load_use_case("retail-win-back", config_root)
    twice = config.model_copy(
        update={"order_log_features": (*config.order_log_features, config.order_log_features[0])}
    )
    with pytest.raises(ConfigError) as caught:
        validate_onboarding_config(twice, get_roles(config_root))
    assert caught.value.code == "SUGGESTED_FEATURE_DUPLICATE"
    assert config.order_log_label is not None
    entity = config.model_copy(
        update={"order_log_label": config.order_log_label.model_copy(update={"role": "entity"})}
    )
    with pytest.raises(ConfigError) as caught:
        validate_onboarding_config(entity, get_roles(config_root))
    assert caught.value.code == "LABEL_ROLE_NOT_EVENT"


def test_an_audience_never_goes_agent_editable_or_empty() -> None:
    with pytest.raises(ValidationError):
        AudienceConfig.model_validate({"role": "orders", "no_event_days": 0})
    with pytest.raises(ValidationError):
        AudienceConfig.model_validate({"role": "orders", "no_event_days": 90, "agent_editable": True})
    with pytest.raises(ValidationError):
        AudienceConfig.model_validate({"role": "", "no_event_days": 90})


def test_the_two_roles_the_path_builds_are_hidden_from_every_list(config_root: Path) -> None:
    roles = get_roles(config_root)
    for name in ("order_lines", "returns"):
        spec = roles.get(name)
        assert spec is not None and spec.internal and spec.is_event
        assert name not in roles.names and name not in roles.event_roles
        assert name not in roles.model_dump(mode="json")["roles"]
        assert roles.require(name) is spec, "a plan, a mapping and a feature may still name it"
    assert roles.names == (
        "activity",
        "bills",
        "campaign_events",
        "complaints",
        "entity",
        "orders",
        "other_event",
        "payments",
        "plan_changes",
        "usage",
    ), "the ten roles there were before the path, and no others"
    assert "orders" in roles.names and "other_event" in roles.event_roles
    assert "internal" not in roles.model_dump(mode="json")["roles"]["orders"]


def test_role_detection_never_proposes_a_hidden_role(config_root: Path) -> None:
    config = load_use_case(USE_CASE, config_root)
    file = loaded(order_log_frame(), "returns_order_lines.csv", config_root)
    source = profile_source(
        file.frame,
        config,
        source_id="s",
        client_id="c",
        file_name="returns_order_lines.csv",
        row_count=len(file.frame),
        roles=get_roles(config_root),
    )
    proposed = {candidate.role for candidate in detect_roles(source, get_roles(config_root))}
    assert proposed and not proposed & {"order_lines", "returns"}
