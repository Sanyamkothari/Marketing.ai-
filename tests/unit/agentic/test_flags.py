"""Plan K's three switches exist, are documented and are off by default (M130, DEC-1400 (d); design §11).

`agentic.enabled` (M132), `compliance.gate.enabled` (M134) and `mcp.enabled` (M133) live in
`configs/agentic/agentic.yaml`, read by `engine.agentic.config`. With all three off the product is Plan J's;
the later milestones read switches that already exist, so these tests pin where they are and that they are off.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from engine.agentic.config import AGENTIC_CONFIG_FILE, AgenticConfig, load_agentic_config
from engine.agentic.errors import AgenticError


def _switches(config: AgenticConfig) -> dict[str, bool]:
    return {
        "agentic.enabled": config.agentic.enabled,
        "compliance.gate.enabled": config.compliance.gate.enabled,
        "mcp.enabled": config.mcp.enabled,
    }


ALL_OFF = {"agentic.enabled": False, "compliance.gate.enabled": False, "mcp.enabled": False}


def test_the_model_defaults_are_all_off() -> None:
    assert _switches(AgenticConfig()) == ALL_OFF


def test_the_shipped_file_names_all_three_switches_and_each_is_off(config_root: Path) -> None:
    raw = yaml.safe_load((config_root / AGENTIC_CONFIG_FILE).read_text(encoding="utf-8"))
    assert raw == {
        "agentic": {"enabled": False},
        "compliance": {"gate": {"enabled": False}},
        "mcp": {"enabled": False},
    }
    assert _switches(load_agentic_config(config_root)) == ALL_OFF


def test_each_switch_is_documented_in_the_shipped_file(config_root: Path) -> None:
    text = (config_root / AGENTIC_CONFIG_FILE).read_text(encoding="utf-8")
    for name in ALL_OFF:
        assert f"# {name}:" in text, f"{name} has no comment in {AGENTIC_CONFIG_FILE}"


def test_a_configuration_root_without_the_file_is_all_off(tmp_path: Path) -> None:
    assert _switches(load_agentic_config(tmp_path)) == ALL_OFF


def test_one_switch_turned_on_leaves_the_others_off(tmp_path: Path) -> None:
    path = tmp_path / AGENTIC_CONFIG_FILE
    path.parent.mkdir(parents=True)
    path.write_text("mcp:\n  enabled: true\n", encoding="utf-8")
    assert _switches(load_agentic_config(tmp_path)) == {**ALL_OFF, "mcp.enabled": True}


def test_a_misspelt_switch_is_refused_not_ignored(tmp_path: Path) -> None:
    path = tmp_path / AGENTIC_CONFIG_FILE
    path.parent.mkdir(parents=True)
    path.write_text("compliance:\n  gate:\n    enabeld: true\n", encoding="utf-8")
    with pytest.raises(AgenticError) as caught:
        load_agentic_config(tmp_path)
    assert caught.value.code == "CONFIG_INVALID"
