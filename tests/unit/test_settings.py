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
    REDACTED,
    SECRET_FIELDS,
    SUMMARY_FIELDS,
    Settings,
    SettingsError,
    build_services,
    load_settings,
    redacted,
    secret_name,
    settings,
    settings_from_aws,
    ssm_prefix,
    summary,
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


# ---------------------------------------------------------------------------
# Phase 4a: the fields, the loaders and the redaction guarantees
# ---------------------------------------------------------------------------
def test_the_phase_4a_defaults_change_nothing_about_a_laptop() -> None:
    """Every field this phase added has a default that reproduces Phase 1 behaviour."""
    config = Settings()
    assert config.env == "local"
    assert config.log_format == "text"  # the JSON renderer is opt-in
    assert config.metrics_backend == "none"  # nothing is published
    assert config.cors_origins == ("*",)  # DEC-024's answer, unchanged
    assert config.download_url_ttl_seconds == 900
    assert config.sagemaker_job_name_prefix == "marketing-ai"
    assert config.sagemaker_subnet_ids == ()
    assert config.local_cache_dir is None


@pytest.mark.parametrize(
    ("field", "variable", "raw", "expected"),
    [
        ("sagemaker_subnet_ids", "MARKETING_AI_SAGEMAKER_SUBNET_IDS", "a, b ,c", ("a", "b", "c")),
        ("cors_origins", "MARKETING_AI_CORS_ORIGINS", "https://x.example", ("https://x.example",)),
        ("sagemaker_security_group_ids", "MARKETING_AI_SAGEMAKER_SECURITY_GROUP_IDS", "sg-1", ("sg-1",)),
    ],
)
def test_one_variable_fills_a_tuple_field(
    field: str, variable: str, raw: str, expected: tuple[str, ...]
) -> None:
    """`from_env` hands every value through as a string; the splitting is a validator, not a loader change."""
    assert getattr(Settings.from_env({variable: raw}), field) == expected


def test_a_prefix_is_normalised_and_a_traversal_is_refused() -> None:
    assert (
        Settings.from_env({"MARKETING_AI_S3_PREFIX": "/marketing-ai/prod/"}).s3_prefix == "marketing-ai/prod"
    )
    with pytest.raises(SettingsError):
        Settings.from_env({"MARKETING_AI_S3_PREFIX": "a/../../etc"})


def test_sagemaker_without_s3_is_refused_because_a_remote_job_cannot_read_a_local_disk() -> None:
    with pytest.raises(SettingsError) as caught:
        Settings.from_env(
            {
                "MARKETING_AI_JOB_BACKEND": "sagemaker",
                "MARKETING_AI_AWS_REGION": "ap-south-1",
                "MARKETING_AI_SAGEMAKER_ROLE_ARN": "arn:aws:iam::111122223333:role/r",
                "MARKETING_AI_SAGEMAKER_INSTANCE_TYPE": "ml.m5.2xlarge",
                "MARKETING_AI_SAGEMAKER_IMAGE_URI": "111122223333.dkr.ecr.ap-south-1.amazonaws.com/i:t",
            }
        )
    assert "storage_backend" in caught.value.message or "STORAGE_BACKEND" in caught.value.message


def test_a_production_deployment_may_not_inherit_the_laptops_open_cors() -> None:
    """DEC-307: `*` is a statement about a laptop, not about an internet-facing load balancer."""
    with pytest.raises(SettingsError) as caught:
        Settings.from_env({"MARKETING_AI_ENV": "prod"})
    assert "CORS_ORIGINS" in caught.value.message
    narrowed = Settings.from_env(
        {"MARKETING_AI_ENV": "prod", "MARKETING_AI_CORS_ORIGINS": "https://app.example"}
    )
    assert narrowed.cors_origins == ("https://app.example",)


def test_a_secret_never_reaches_the_startup_line() -> None:
    """DEC-303: `summary()` is an allow-list, so a field added later is hidden until somebody adds it."""
    config = Settings.from_env(
        {
            "MARKETING_AI_METADATA_BACKEND": "postgres",
            "MARKETING_AI_POSTGRES_DSN": "postgresql+psycopg://u:hunter2-do-not-log@h/db",
        }
    )
    line = summary(config)
    assert "hunter2-do-not-log" not in line
    assert "hunter2-do-not-log" not in repr(config)
    assert redacted(config)["postgres_dsn"] == REDACTED
    assert set(SUMMARY_FIELDS).isdisjoint(SECRET_FIELDS)
    unlisted = set(Settings.model_fields) - set(SUMMARY_FIELDS)
    assert unlisted, "this test is vacuous if summary() renders every field"
    for field in unlisted:
        assert f"{field}=" not in line
    assert set(redacted(config)) == set(Settings.model_fields)


def test_the_parameter_store_and_secret_paths_are_derived_from_the_environment_name() -> None:
    assert ssm_prefix("staging") == "/marketing-ai/staging/"
    assert secret_name("staging") == "marketing-ai/staging/app"


class _StubSource:
    """A `ParameterSource` made of two dictionaries, so the precedence is tested without AWS."""

    def __init__(self, parameters: dict[str, str], secrets: dict[str, str]) -> None:
        self._parameters = parameters
        self._secrets = secrets

    def parameters(self, prefix: str) -> dict[str, str]:
        del prefix
        return dict(self._parameters)

    def secret(self, name: str) -> dict[str, str]:
        del name
        return dict(self._secrets)


def test_the_aws_loader_reads_both_stores_and_the_environment_still_wins() -> None:
    """DEC-301: SSM, then the secret, then the environment. An operator overrides one value on a task."""
    source = _StubSource(
        {"storage_backend": "s3", "s3_bucket": "from-ssm", "MARKETING_AI_AWS_REGION": "ap-south-1"},
        {"postgres_dsn": "postgresql+psycopg://u:p@h/db", "metadata_backend": "postgres"},
    )
    config = settings_from_aws(source=source, env="dev", environ={})
    assert config.env == "dev"
    assert config.s3_bucket == "from-ssm"
    assert config.metadata_backend == "postgres"
    assert config.postgres_dsn is not None

    overridden = settings_from_aws(source=source, env="dev", environ={"MARKETING_AI_S3_BUCKET": "from-env"})
    assert overridden.s3_bucket == "from-env"


def test_a_parameter_leaf_may_be_named_either_way_and_an_unknown_one_is_ignored() -> None:
    """Parameter Store is shared; a later phase will put things there this version knows nothing of."""
    source = _StubSource({"MARKETING_AI_LOG_FORMAT": "json", "phase_9_setting": "whatever"}, {})
    assert settings_from_aws(source=source, env="dev", environ={}).log_format == "json"


def test_the_source_selector_refuses_a_value_that_is_neither_env_nor_aws() -> None:
    with pytest.raises(SettingsError):
        load_settings({"MARKETING_AI_SETTINGS_SOURCE": "magic"})
    assert load_settings({}) == Settings()


def test_the_local_factories_build_exactly_what_phase_1_built(tmp_path: Path) -> None:
    from engine.registry import LocalModelRegistry
    from engine.storage import LocalStorage

    store, registry = build_services(Settings(data_dir=tmp_path))
    assert isinstance(store, LocalStorage)
    assert isinstance(registry, LocalModelRegistry)
    assert registry.db_path == tmp_path / "registry.db"
