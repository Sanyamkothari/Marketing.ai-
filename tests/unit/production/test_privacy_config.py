"""`configs/privacy.yaml` and its loader (DEC-730)."""

from __future__ import annotations

import shutil
import stat
from pathlib import Path

import pytest
from pydantic import SecretStr

from engine.config import ConfigError, list_use_case_ids
from engine.platform_db import PLATFORM_DB_FILENAME, sqlite_engine
from engine.privacy.config import (
    MIN_SALT_LENGTH,
    SALT_FILENAME,
    ErasureMode,
    check_privacy_salt,
    load_privacy_config,
    privacy_config_or_none,
    privacy_salt,
)
from engine.settings import Settings, SettingsError, redacted


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


# ---------------------------------------------------------------------------
# The salt is a secret with no default in code (ruling R3, DEC-860)
# ---------------------------------------------------------------------------
def test_the_configured_salt_is_the_salt_and_the_client_id_never_is(tmp_path: Path) -> None:
    engine = sqlite_engine(tmp_path / PLATFORM_DB_FILENAME)
    settings = Settings(client_id="acme", privacy_salt="a-long-secret-salt-01")  # type: ignore[arg-type]
    assert privacy_salt(settings, engine=engine) == "a-long-secret-salt-01"
    assert not (tmp_path / SALT_FILENAME).exists(), "a configured salt writes nothing"
    assert "a-long-secret-salt-01" not in repr(settings) and "a-long-secret-salt-01" not in str(
        redacted(settings)
    )


def test_a_local_database_gets_a_generated_salt_once_kept_beside_it(tmp_path: Path) -> None:
    engine = sqlite_engine(tmp_path / PLATFORM_DB_FILENAME)
    first = privacy_salt(Settings(client_id="acme"), engine=engine)
    assert first not in {"acme", "local", ""} and len(first) >= MIN_SALT_LENGTH
    path = tmp_path / SALT_FILENAME
    assert path.read_text(encoding="ascii") == first
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert privacy_salt(Settings(), engine=sqlite_engine(tmp_path / PLATFORM_DB_FILENAME)) == first, "stable"
    other = tmp_path / "other"
    other.mkdir()
    assert (
        privacy_salt(Settings(), engine=sqlite_engine(other / PLATFORM_DB_FILENAME)) != first
    ), "per deployment"


def test_prod_without_a_salt_is_refused(tmp_path: Path) -> None:
    prod = Settings(env="prod", cors_origins=("https://x.example",))  # type: ignore[arg-type]
    with pytest.raises(SettingsError) as excinfo:
        privacy_salt(prod, engine=sqlite_engine(tmp_path / PLATFORM_DB_FILENAME))
    assert (excinfo.value.code, excinfo.value.env_var) == ("SETTING_REQUIRED", "MARKETING_AI_PRIVACY_SALT")
    with pytest.raises(SettingsError):
        check_privacy_salt(prod)
    check_privacy_salt(prod.model_copy(update={"privacy_salt": SecretStr("prod-secret-salt-0001")}))
    check_privacy_salt(Settings())  # a laptop starts without one


def test_without_a_local_file_there_is_nowhere_to_keep_a_generated_salt() -> None:
    with pytest.raises(SettingsError) as excinfo:
        privacy_salt(Settings(env="dev"), engine=None)
    assert excinfo.value.env_var == "MARKETING_AI_PRIVACY_SALT"


def test_a_short_salt_is_refused_at_load() -> None:
    with pytest.raises(SettingsError) as excinfo:
        Settings.from_env({"MARKETING_AI_PRIVACY_SALT": "short"})
    assert excinfo.value.env_var == "MARKETING_AI_PRIVACY_SALT"
    assert Settings.from_env({"MARKETING_AI_PRIVACY_SALT": "x" * 16}).privacy_salt is not None


def test_a_prod_api_does_not_start_without_the_salt(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from api.main import create_app

    app = create_app(data_dir=tmp_path)
    app.state.settings = Settings(env="prod", cors_origins=("https://x.example",), auth_mode="local")  # type: ignore[arg-type]
    with pytest.raises(SettingsError) as excinfo, TestClient(app):
        pass
    assert excinfo.value.env_var == "MARKETING_AI_PRIVACY_SALT"
    app = create_app(data_dir=tmp_path)
    app.state.settings = Settings(
        env="prod", cors_origins=("https://x.example",), auth_mode="local", privacy_salt="prod-secret-salt-0001"  # type: ignore[arg-type]
    )
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
