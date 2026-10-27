"""Plan O M230 (DEC-1800): the `data_doctor:` settings are validated, round-trip, and move nothing.

There is no on/off switch: the one Data Doctor is always on. These tests pin that the settings are real validated
values that survive a dump and a reload, and that at their defaults a run's `run_config.json` and recipe hash are
exactly what they were before Plan O. They also pin that `PLAN_O_CODES` is wired into the catalogue check.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from engine.config import (
    ConfigError,
    ResolvedConfig,
    UseCaseConfig,
    load_all_use_cases,
    load_engine_config,
    load_use_case,
    overridable_paths,
    recipe_from_config,
    resolve_config,
)
from engine.doctor.codes import PLAN_O_CODES
from engine.doctor.config import DataDoctorConfig
from engine.pilot.help import known_codes

USE_CASE = "targeted-advertisement"


def test_the_engine_defaults_carry_no_switch(config_root: Path) -> None:
    assert "unified" not in load_engine_config(config_root).defaults["data_doctor"]
    assert "unified" not in DataDoctorConfig().model_dump()


def test_every_use_case_loads_with_the_default_data_doctor_settings(config_root: Path) -> None:
    for use_case_id, config in load_all_use_cases(config_root).items():
        assert config.data_doctor == DataDoctorConfig(), use_case_id


def test_a_setting_round_trips_through_a_dump(config_root: Path) -> None:
    config = load_use_case(USE_CASE, config_root)
    changed = config.model_copy(update={"data_doctor": DataDoctorConfig(workspace_currency="USD")})
    for dumped in (changed.model_dump(mode="json"), changed.model_dump()):
        assert UseCaseConfig.model_validate(dumped).data_doctor.workspace_currency == "USD"
    assert UseCaseConfig.model_validate_json(changed.model_dump_json()) == changed


def test_a_setting_round_trips_through_yaml(tmp_path: Path, config_root: Path) -> None:
    """A use-case file that sets a Data Doctor value loads with it, and its dump carries it."""
    root = tmp_path / "configs"
    shutil.copytree(config_root, root)
    path = root / "use_cases" / "targeted_advertisement.yaml"
    path.write_text(
        path.read_text(encoding="utf-8") + "\ndata_doctor:\n  workspace_currency: USD\n", encoding="utf-8"
    )
    config = load_use_case(USE_CASE, root)
    assert config.data_doctor.workspace_currency == "USD"
    assert config.model_dump(mode="json")["data_doctor"] == {"workspace_currency": "USD"}
    resolved = resolve_config(USE_CASE, root=root)
    reloaded = ResolvedConfig.model_validate_json(resolved.model_dump_json())
    assert reloaded.config.data_doctor.workspace_currency == "USD"


def test_the_defaults_are_left_out_of_run_config(config_root: Path) -> None:
    """With the defaults, `run_config.json` holds exactly what it held before Plan O (the plan's §3.2 rule 1)."""
    resolved = resolve_config(USE_CASE, root=config_root)
    dumped = resolved.model_dump(mode="json")
    assert "data_doctor" not in dumped["config"]
    assert not any(leaf.startswith("data_doctor") for leaf in dumped["sources"])
    assert ResolvedConfig.model_validate(dumped).config.data_doctor == DataDoctorConfig()


def test_data_doctor_settings_do_not_change_the_training_recipe_hash(config_root: Path) -> None:
    base = load_use_case(USE_CASE, config_root)
    changed = base.model_copy(update={"data_doctor": DataDoctorConfig(workspace_currency="USD")})
    kwargs = {"primary_key": "customer_id", "feature_columns": ("visits_last_7d",), "seed": 7}
    assert recipe_from_config(base, **kwargs).recipe_hash == recipe_from_config(changed, **kwargs).recipe_hash


def test_data_doctor_settings_are_not_overridable_per_run(config_root: Path) -> None:
    config = load_use_case(USE_CASE, config_root)
    assert not any(path.startswith("data_doctor") for path in overridable_paths(config))
    with pytest.raises(ConfigError) as caught:
        resolve_config(USE_CASE, {"data_doctor.workspace_currency": "USD"}, root=config_root)
    assert caught.value.code == "OVERRIDE_UNKNOWN_PATH"


def test_the_removed_switch_is_refused_like_any_unknown_field() -> None:
    with pytest.raises(ValidationError):
        DataDoctorConfig.model_validate({"unified": True})
    with pytest.raises(ValidationError):
        DataDoctorConfig.model_validate({"autonomy": "full"})


def test_a_malformed_value_in_yaml_is_a_config_error(tmp_path: Path, config_root: Path) -> None:
    """A bad value in a use-case file fails loading with the dotted path, like any other leaf."""
    root = tmp_path / "configs"
    shutil.copytree(config_root, root)
    path = root / "use_cases" / "targeted_advertisement.yaml"
    path.write_text(
        path.read_text(encoding="utf-8") + "\ndata_doctor:\n  workspace_currency: dollars\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError) as caught:
        load_use_case(USE_CASE, root)
    assert caught.value.code == "CONFIG_INVALID"
    assert "data_doctor.workspace_currency" in str(caught.value)


def test_plan_o_codes_are_in_the_catalogue_check() -> None:
    """Every code Plan O raises must have a plain-language entry (tests/unit/pilot/test_help.py)."""
    assert known_codes() >= PLAN_O_CODES


def test_the_wave_two_batch_b_codes_are_plan_o_codes() -> None:
    """Plan O wave 2 integration (batch B): M239's, M244's and M243's codes are registered, so each has help."""
    from engine.doctor.outcome_definition import OUTCOME_CODES
    from engine.doctor.prescription import PRESCRIPTION_CODES
    from engine.doctor.time_settings import TIME_CODES

    assert OUTCOME_CODES | TIME_CODES | PRESCRIPTION_CODES <= PLAN_O_CODES
