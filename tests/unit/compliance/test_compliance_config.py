"""The gate's switch (Plan K M134, design §11): off by default, off without the file, refused when not valid."""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.agentic.codes import COMPLIANCE_CODES, PLAN_K_CODES
from engine.compliance.config import gate_enabled, load_compliance_config
from engine.compliance.errors import ComplianceError
from engine.compliance.gate import REASON_ORDER


def test_the_shipped_switch_is_off(config_root: Path) -> None:
    loaded = load_compliance_config(config_root)
    assert loaded.gate.enabled is False
    assert loaded.gate.opt_in_channels == ("whatsapp",)
    assert loaded.gate.dlt_var_max_chars == 30
    assert gate_enabled(config_root) is False


def test_a_root_without_the_file_is_off(tmp_path: Path) -> None:
    assert gate_enabled(tmp_path) is False


@pytest.mark.parametrize(
    "text",
    [
        "compliance:\n  gate:\n    enabled: maybe\n",
        "compliance:\n  gate:\n    enabled: true\n    invented: 1\n",
        "compliance:\n  gate:\n    opt_in_channels: [what's app]\n",
        "compliance: [\n",
    ],
)
def test_a_file_that_is_not_valid_is_refused(tmp_path: Path, text: str) -> None:
    (tmp_path / "compliance").mkdir()
    (tmp_path / "compliance" / "compliance.yaml").write_text(text, encoding="utf-8")
    with pytest.raises(ComplianceError) as caught:
        load_compliance_config(tmp_path)
    assert caught.value.code == "CONFIG_INVALID"


def test_every_reason_is_a_plan_k_code() -> None:
    assert set(REASON_ORDER) == COMPLIANCE_CODES
    assert COMPLIANCE_CODES <= PLAN_K_CODES
    assert {
        "COMPLIANCE_TEMPLATE_MISMATCH",
        "COMPLIANCE_URL_NOT_WHITELISTED",
        "COMPLIANCE_WHATSAPP_NO_OPT_IN",
        "COMPLIANCE_CONSENT",
    } <= COMPLIANCE_CODES
