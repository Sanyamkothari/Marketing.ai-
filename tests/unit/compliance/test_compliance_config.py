"""The gate's switch and settings (Plan K M134, design §11; DEC-1405 (a)).

The switch is `compliance.gate.enabled` in `configs/agentic/agentic.yaml`, the only one: off by default and off
without the file. `configs/compliance/compliance.yaml` holds the gate's settings, is refused when not valid, and
refuses a switch of its own with `COMPLIANCE_SWITCH_MISPLACED`.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from engine.agentic.codes import COMPLIANCE_CODES, COMPLIANCE_CONFIG_CODES, PLAN_K_CODES
from engine.compliance.config import (
    SWITCH_MISPLACED_CODE,
    gate_enabled,
    load_compliance_config,
)
from engine.compliance.errors import ComplianceError
from engine.compliance.gate import REASON_ORDER
from tests.unit.compliance.switch import set_gate_switch


def _settings(root: Path, text: str) -> None:
    (root / "compliance").mkdir(parents=True, exist_ok=True)
    (root / "compliance" / "compliance.yaml").write_text(text, encoding="utf-8")


def test_the_shipped_switch_is_off(config_root: Path) -> None:
    loaded = load_compliance_config(config_root)
    assert "enabled" not in type(loaded.gate).model_fields
    assert loaded.gate.opt_in_channels == ("whatsapp",)
    assert loaded.gate.dlt_var_max_chars == 30
    assert gate_enabled(config_root) is False


def test_a_root_without_the_files_is_off(tmp_path: Path) -> None:
    assert gate_enabled(tmp_path) is False


def test_the_switch_is_read_from_agentic_yaml(tmp_path: Path, config_root: Path) -> None:
    root = tmp_path / "configs"
    shutil.copytree(config_root, root)
    assert gate_enabled(root) is False
    set_gate_switch(root, on=True)
    assert gate_enabled(root) is True
    set_gate_switch(root, on=False)
    assert gate_enabled(root) is False


def test_the_switch_alone_turns_the_gate_on_with_the_default_settings(tmp_path: Path) -> None:
    set_gate_switch(tmp_path, on=True)
    assert gate_enabled(tmp_path) is True
    assert load_compliance_config(tmp_path).gate.opt_in_channels == ("whatsapp",)


@pytest.mark.parametrize("value", ["true", "false"])
@pytest.mark.parametrize("switch_on", [True, False])
def test_a_switch_written_in_compliance_yaml_is_refused_by_its_code(
    tmp_path: Path, value: str, switch_on: bool
) -> None:
    set_gate_switch(tmp_path, on=switch_on)
    _settings(tmp_path, f"compliance:\n  gate:\n    enabled: {value}\n    opt_in_channels: [whatsapp]\n")
    for read in (load_compliance_config, gate_enabled):
        with pytest.raises(ComplianceError) as caught:
            read(tmp_path)
        assert caught.value.code == SWITCH_MISPLACED_CODE == "COMPLIANCE_SWITCH_MISPLACED"
        assert "agentic/agentic.yaml" in caught.value.message


@pytest.mark.parametrize(
    "text",
    [
        "compliance:\n  gate:\n    invented: 1\n",
        "compliance:\n  gate:\n    opt_in_channels: [what's app]\n",
        "compliance:\n  gate:\n    dlt_var_max_chars: 0\n",
        "compliance: [\n",
    ],
)
def test_a_settings_file_that_is_not_valid_is_refused(tmp_path: Path, text: str) -> None:
    _settings(tmp_path, text)
    with pytest.raises(ComplianceError) as caught:
        load_compliance_config(tmp_path)
    assert caught.value.code == "CONFIG_INVALID"
    with pytest.raises(ComplianceError) as caught:
        gate_enabled(tmp_path)
    assert caught.value.code == "CONFIG_INVALID"


def test_an_agentic_yaml_that_is_not_valid_is_refused(tmp_path: Path) -> None:
    (tmp_path / "agentic").mkdir()
    (tmp_path / "agentic" / "agentic.yaml").write_text(
        "compliance:\n  gate:\n    enabled: maybe\n", encoding="utf-8"
    )
    with pytest.raises(ComplianceError) as caught:
        gate_enabled(tmp_path)
    assert caught.value.code == "CONFIG_INVALID"


def test_every_reason_is_a_plan_k_code() -> None:
    assert set(REASON_ORDER) == COMPLIANCE_CODES
    assert COMPLIANCE_CODES <= PLAN_K_CODES
    assert {SWITCH_MISPLACED_CODE} == COMPLIANCE_CONFIG_CODES <= PLAN_K_CODES
    assert {
        "COMPLIANCE_TEMPLATE_MISMATCH",
        "COMPLIANCE_URL_NOT_WHITELISTED",
        "COMPLIANCE_WHATSAPP_NO_OPT_IN",
        "COMPLIANCE_CONSENT",
    } <= COMPLIANCE_CODES
