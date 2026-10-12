"""Plan O M230 (DEC-1800): the switch `data_doctor.unified` is off, validated, round-trips, and moves nothing.

M230 changes nothing a run does. These tests pin the three things it adds: the switch's default in
`configs/engine.yaml` (off, for every use case), that the switch is a real validated setting that survives a dump and
a reload whichever way it is set, and that while it is off a run's `run_config.json` and recipe hash are exactly what
they were before Plan O. They also pin that `PLAN_O_CODES` is wired into the catalogue check.
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


def test_the_switch_is_off_in_the_engine_defaults(config_root: Path) -> None:
    assert load_engine_config(config_root).defaults["data_doctor"] == {"unified": False}
    assert DataDoctorConfig().unified is False


def test_every_use_case_loads_with_the_switch_off(config_root: Path) -> None:
    for use_case_id, config in load_all_use_cases(config_root).items():
        assert isinstance(config.data_doctor, DataDoctorConfig), use_case_id
        assert config.data_doctor.unified is False, use_case_id


@pytest.mark.parametrize("unified", [False, True])
def test_the_switch_round_trips_through_a_dump(config_root: Path, unified: bool) -> None:
    config = load_use_case(USE_CASE, config_root)
    switched = config.model_copy(update={"data_doctor": DataDoctorConfig(unified=unified)})
    for dumped in (switched.model_dump(mode="json"), switched.model_dump()):
        assert UseCaseConfig.model_validate(dumped).data_doctor.unified is unified
    assert UseCaseConfig.model_validate_json(switched.model_dump_json()) == switched
    assert DataDoctorConfig.model_validate(DataDoctorConfig(unified=unified).model_dump()).unified is unified


def test_the_switch_round_trips_through_yaml(tmp_path: Path, config_root: Path) -> None:
    """A use-case file that turns the switch on loads with it on, and its dump carries it."""
    root = tmp_path / "configs"
    shutil.copytree(config_root, root)
    path = root / "use_cases" / "targeted_advertisement.yaml"
    path.write_text(path.read_text(encoding="utf-8") + "\ndata_doctor:\n  unified: true\n", encoding="utf-8")
    config = load_use_case(USE_CASE, root)
    assert config.data_doctor.unified is True
    assert config.model_dump(mode="json")["data_doctor"] == {"unified": True}
    resolved = resolve_config(USE_CASE, root=root)
    assert ResolvedConfig.model_validate_json(resolved.model_dump_json()).config.data_doctor.unified is True


def test_while_off_the_switch_is_left_out_of_run_config(config_root: Path) -> None:
    """With the switch off, `run_config.json` holds exactly what it held before Plan O (the plan's §3.2 rule 1)."""
    resolved = resolve_config(USE_CASE, root=config_root)
    dumped = resolved.model_dump(mode="json")
    assert "data_doctor" not in dumped["config"]
    assert not any(leaf.startswith("data_doctor") for leaf in dumped["sources"])
    assert ResolvedConfig.model_validate(dumped).config.data_doctor.unified is False


def test_the_switch_does_not_change_the_training_recipe_hash(config_root: Path) -> None:
    base = load_use_case(USE_CASE, config_root)
    changed = base.model_copy(update={"data_doctor": DataDoctorConfig(unified=True)})
    kwargs = {"primary_key": "customer_id", "feature_columns": ("visits_last_7d",), "seed": 7}
    assert recipe_from_config(base, **kwargs).recipe_hash == recipe_from_config(changed, **kwargs).recipe_hash


def test_the_switch_is_not_overridable_per_run(config_root: Path) -> None:
    config = load_use_case(USE_CASE, config_root)
    assert not any(path.startswith("data_doctor") for path in overridable_paths(config))
    with pytest.raises(ConfigError) as caught:
        resolve_config(USE_CASE, {"data_doctor.unified": True}, root=config_root)
    assert caught.value.code == "OVERRIDE_UNKNOWN_PATH"


@pytest.mark.parametrize("bad", ["yes", "true", 1, None])
def test_the_switch_must_be_a_real_boolean(bad: object) -> None:
    with pytest.raises(ValidationError):
        DataDoctorConfig.model_validate({"unified": bad})


def test_unknown_data_doctor_fields_are_refused() -> None:
    with pytest.raises(ValidationError):
        DataDoctorConfig.model_validate({"unified": False, "autonomy": "full"})


def test_a_malformed_switch_in_yaml_is_a_config_error(tmp_path: Path, config_root: Path) -> None:
    """A bad value in a use-case file fails loading with the dotted path, like any other leaf."""
    root = tmp_path / "configs"
    shutil.copytree(config_root, root)
    path = root / "use_cases" / "targeted_advertisement.yaml"
    path.write_text(path.read_text(encoding="utf-8") + "\ndata_doctor:\n  unified: maybe\n", encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        load_use_case(USE_CASE, root)
    assert caught.value.code == "CONFIG_INVALID"
    assert "data_doctor.unified" in str(caught.value)


def test_plan_o_codes_are_in_the_catalogue_check() -> None:
    """Every code Plan O raises must have a plain-language entry (tests/unit/pilot/test_help.py)."""
    assert known_codes() >= PLAN_O_CODES


def test_the_wave_two_batch_b_codes_are_plan_o_codes() -> None:
    """Plan O wave 2 integration (batch B): M239's, M244's and M243's codes are registered, so each has help."""
    from engine.doctor.outcome_definition import OUTCOME_CODES
    from engine.doctor.prescription import PRESCRIPTION_CODES
    from engine.doctor.time_settings import TIME_CODES

    assert OUTCOME_CODES | TIME_CODES | PRESCRIPTION_CODES <= PLAN_O_CODES
