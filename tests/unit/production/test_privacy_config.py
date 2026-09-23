"""`configs/privacy.yaml` and its loader (DEC-730)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from engine.config import ConfigError, list_use_case_ids
from engine.privacy.config import (
    ErasureMode,
    load_privacy_config,
    privacy_config_or_none,
    privacy_salt,
)
from engine.settings import Settings


def copy_root(config_root: Path, tmp_path: Path) -> Path:
    target = tmp_path / "configs"
    shutil.copytree(config_root, target)
    return target


def test_the_shipped_file_loads_and_every_mapped_use_case_exists(config_root: Path) -> None:
    privacy = load_privacy_config(config_root)
    assert privacy.purposes, "privacy.yaml declares no purposes"
    assert set(privacy.use_case_purposes) <= set(list_use_case_ids(config_root))
    assert set(privacy.use_case_purposes.values()) <= set(privacy.purposes)
    assert privacy.erasure.mode is ErasureMode.DELETE
    assert "scores.csv" in privacy.retention.row_level_run_artefacts


def test_a_use_case_with_no_purpose_is_ungated(config_root: Path) -> None:
    privacy = load_privacy_config(config_root)
    unmapped = sorted(set(list_use_case_ids(config_root)) - set(privacy.use_case_purposes))
    assert unmapped, "every use case is mapped; the ungated path would be untested"
    assert privacy.purpose_for(unmapped[0]) is None
    assert privacy.purpose_for("targeted-advertisement") == "marketing_communication"


def test_a_mapping_to_an_unknown_use_case_is_refused(config_root: Path, tmp_path: Path) -> None:
    root = copy_root(config_root, tmp_path)
    path = root / "privacy.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "use_case_purposes:", "use_case_purposes:\n  no-such-use-case: marketing_communication"
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_privacy_config(root)
    assert excinfo.value.code == "PRIVACY_USE_CASE_UNKNOWN"


def test_a_mapping_to_an_undeclared_purpose_is_refused(config_root: Path, tmp_path: Path) -> None:
    root = copy_root(config_root, tmp_path)
    path = root / "privacy.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "targeted-advertisement: marketing_communication", "targeted-advertisement: profiling"
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_privacy_config(root)
    assert excinfo.value.code == "CONFIG_INVALID"


def test_a_root_without_the_file_has_no_privacy_controls(config_root: Path, tmp_path: Path) -> None:
    root = copy_root(config_root, tmp_path)
    (root / "privacy.yaml").unlink()
    assert privacy_config_or_none(root) is None


def test_the_salt_is_the_client_id_or_local() -> None:
    assert privacy_salt(Settings(client_id="acme")) == "acme"
    assert privacy_salt(Settings()) == "local"
