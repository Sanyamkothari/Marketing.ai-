"""`engine.settings`: the defaults reproduce Phase 1, and a half-configured backend is refused.

Two things are worth pinning here and nothing else is. The first is that an empty environment
produces the engine that ran before this module existed - the whole justification for introducing
it was "defaults keep behaviour identical", and that is a claim a test can hold to. The second is
that selecting a backend without its connection fields fails at construction with the variable
named, rather than at the first call that needed the missing value.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.config import DEFAULT_CONFIG_ROOT, config_root
from engine.registry import default_registry
from engine.settings import (
    DEFAULT_CONFIG_DIR,
    DEFAULT_DATA_DIR,
    ENV_PREFIX,
    ENV_VARS,
    Settings,
    SettingsError,
    settings,
)
from engine.storage import default_storage


def test_an_empty_environment_is_phase_1() -> None:
    """Every default: local files, a thread pool, SQLite, the fake model."""
    resolved = Settings.from_env({})
    assert resolved.storage_backend == "local"
    assert resolved.job_backend == "thread"
    assert resolved.metadata_backend == "sqlite"
    assert resolved.llm_backend == "fake"
    assert resolved.aws_region is None
    assert resolved.data_dir == Path(DEFAULT_DATA_DIR)
    assert resolved.job_max_workers == 2  # ThreadJobRunner's own default
    assert resolved.config_dir is None
    assert resolved.config_directory == DEFAULT_CONFIG_DIR


def test_every_variable_this_module_reads_is_in_env_vars() -> None:
    """`ENV_VARS` is the documented list; a field missing from it would be read by nothing."""
    assert set(ENV_VARS) == set(Settings.model_fields)
    assert all(name.startswith(ENV_PREFIX) for name in ENV_VARS.values())


def test_an_empty_value_is_read_as_unset() -> None:
    """`MARKETING_AI_DATA_DIR=` must not root the artefact store at the working directory."""
    assert Settings.from_env({ENV_VARS["data_dir"]: "   "}).data_dir == Path(DEFAULT_DATA_DIR)


def test_the_data_directory_is_taken_as_given() -> None:
    """Relative before this module, relative after it: resolving here would change behaviour."""
    assert Settings.from_env({ENV_VARS["data_dir"]: "runs/here"}).data_dir == Path("runs/here")


def test_the_config_directory_is_resolved(tmp_path: Path) -> None:
    """`config_root` resolved it before `Settings` existed, so `Settings` resolves it."""
    relative = tmp_path / "configs"
    relative.mkdir()
    assert Settings.from_env({ENV_VARS["config_dir"]: str(relative)}).config_dir == relative.resolve()


@pytest.mark.parametrize(
    ("switch", "value", "missing"),
    [
        ("storage_backend", "s3", "s3_bucket"),
        ("job_backend", "sagemaker", "sagemaker_role_arn"),
        ("metadata_backend", "postgres", "postgres_dsn"),
        ("llm_backend", "bedrock", "bedrock_model_id"),
    ],
)
def test_a_backend_without_its_connection_fields_is_refused(switch: str, value: str, missing: str) -> None:
    with pytest.raises(SettingsError) as raised:
        Settings.from_env({ENV_VARS[switch]: value})
    assert raised.value.code == "SETTING_REQUIRED"
    assert raised.value.env_var == ENV_VARS[missing]
    assert ENV_VARS[missing] in raised.value.message


def test_a_fully_configured_aws_backend_is_accepted() -> None:
    resolved = Settings.from_env(
        {
            ENV_VARS["storage_backend"]: "s3",
            ENV_VARS["s3_bucket"]: "a-bucket",
            ENV_VARS["aws_region"]: "ap-south-1",
        }
    )
    assert resolved.storage_backend == "s3"
    assert resolved.s3_bucket == "a-bucket"


def test_an_unreadable_value_names_its_variable() -> None:
    with pytest.raises(SettingsError) as raised:
        Settings.from_env({ENV_VARS["job_max_workers"]: "two"})
    assert raised.value.code == "SETTING_INVALID"
    assert raised.value.env_var == ENV_VARS["job_max_workers"]


def test_an_unknown_backend_names_its_variable() -> None:
    with pytest.raises(SettingsError) as raised:
        Settings.from_env({ENV_VARS["storage_backend"]: "gcs"})
    assert raised.value.env_var == ENV_VARS["storage_backend"]


def test_the_dsn_does_not_print_itself() -> None:
    """A connection string carries a password; a stray log line must not carry it too."""
    resolved = Settings.from_env(
        {ENV_VARS["metadata_backend"]: "postgres", ENV_VARS["postgres_dsn"]: "postgres://u:pw@h/db"}
    )
    assert "pw" not in repr(resolved)
    assert resolved.postgres_dsn is not None
    assert resolved.postgres_dsn.get_secret_value() == "postgres://u:pw@h/db"


def test_the_environment_is_read_on_every_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing is cached: the three `os.environ` reads this replaced were not, and tests patch them."""
    monkeypatch.setenv(ENV_VARS["data_dir"], "first")
    assert settings().data_dir == Path("first")
    monkeypatch.setenv(ENV_VARS["data_dir"], "second")
    assert settings().data_dir == Path("second")


def test_the_three_phase_1_factories_read_the_same_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The point of the refactor: one variable still moves all three, and the default is unchanged."""
    monkeypatch.setenv(ENV_VARS["data_dir"], str(tmp_path))
    monkeypatch.delenv(ENV_VARS["config_dir"], raising=False)
    assert default_storage().root == tmp_path
    assert default_registry().db_path == tmp_path / "registry.db"
    assert config_root() == DEFAULT_CONFIG_ROOT
