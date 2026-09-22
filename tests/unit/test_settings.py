"""`engine.settings`: the defaults, the precedence, the refusals and the redaction guarantees.

The first test in this module is the one that matters most: a `Settings()` with nothing set must
reproduce Phase 1 exactly. Everything else Phase 4a adds is allowed to be new; that is not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.registry import LocalModelRegistry
from engine.settings import (
    ENV_VAR_FOR_FIELD,
    NON_FIELD_ENV_VARS,
    REDACTED,
    SECRET_FIELDS,
    SUMMARY_FIELDS,
    Deployment,
    JobBackend,
    MetadataBackend,
    Settings,
    SettingsError,
    StorageBackend,
    build_registry,
    build_services,
    build_storage,
    secret_name,
    ssm_prefix,
)
from engine.storage import LocalStorage

SECRET_VALUE = "postgresql+psycopg://marketing:hunter2-do-not-log@db.internal:5432/marketing"


# ---------------------------------------------------------------------------
# The defaults are Phase 1
# ---------------------------------------------------------------------------
def test_the_defaults_reproduce_phase_1_exactly() -> None:
    """DEC-300: local behaviour is unchanged by construction, not by anyone remembering to keep it."""
    settings = Settings()
    assert settings.env is Deployment.LOCAL
    assert settings.storage_backend is StorageBackend.LOCAL
    assert settings.metadata_backend is MetadataBackend.SQLITE
    assert settings.job_backend is JobBackend.THREAD
    assert settings.data_dir == Path("data")  # engine.storage.DEFAULT_DATA_DIR
    assert settings.job_max_workers == 2  # ThreadJobRunner's own default
    assert settings.config_dir is None  # falls through to the checkout's configs/
    assert settings.region is None
    assert settings.cors_origins == ("*",)  # DEC-024


def test_the_default_services_are_the_phase_1_services(tmp_path: Path) -> None:
    storage, registry = build_services(Settings(data_dir=tmp_path))
    assert isinstance(storage, LocalStorage)
    assert isinstance(registry, LocalModelRegistry)
    assert registry.db_path == tmp_path / "registry.db"


def test_the_two_phase_1_environment_variables_keep_their_names() -> None:
    """Nothing that set these before has to change."""
    assert ENV_VAR_FOR_FIELD["data_dir"] == "MARKETING_AI_DATA_DIR"
    assert ENV_VAR_FOR_FIELD["config_dir"] == "MARKETING_AI_CONFIG_DIR"
    settings = Settings.from_env(
        {"MARKETING_AI_DATA_DIR": "/srv/artefacts", "MARKETING_AI_CONFIG_DIR": "/srv/c"}
    )
    assert settings.data_dir == Path("/srv/artefacts")
    assert settings.config_dir == Path("/srv/c")


def test_every_other_field_is_the_screaming_snake_of_its_name() -> None:
    for field, variable in ENV_VAR_FOR_FIELD.items():
        if field in {"data_dir", "config_dir"}:
            continue
        assert variable == f"MARKETING_AI_{field.upper()}"


# ---------------------------------------------------------------------------
# Coercion and precedence
# ---------------------------------------------------------------------------
def test_a_comma_separated_variable_becomes_a_tuple() -> None:
    settings = Settings.from_env(
        {
            "MARKETING_AI_SAGEMAKER_SUBNET_IDS": "subnet-a, subnet-b ,subnet-c",
            "MARKETING_AI_CORS_ORIGINS": "https://a.example",
            "MARKETING_AI_BEDROCK_MODEL_IDS": "",
        }
    )
    assert settings.sagemaker_subnet_ids == ("subnet-a", "subnet-b", "subnet-c")
    assert settings.cors_origins == ("https://a.example",)
    assert settings.bedrock_model_ids == ()  # empty means deny, never allow-all


@pytest.mark.parametrize(
    ("raw", "expected"), [("1", True), ("true", True), ("on", True), ("0", False), ("no", False)]
)
def test_booleans_accept_the_usual_spellings(raw: str, expected: bool) -> None:
    assert Settings.from_env({"MARKETING_AI_BEDROCK_ENABLED": raw}).bedrock_enabled is expected


def test_a_boolean_that_is_not_one_names_the_variable_and_not_the_value() -> None:
    with pytest.raises(SettingsError) as caught:
        Settings.from_env({"MARKETING_AI_BEDROCK_ENABLED": "perhaps"})
    assert caught.value.code == "SETTINGS_INVALID"
    assert "MARKETING_AI_BEDROCK_ENABLED" in caught.value.message
    assert "perhaps" not in caught.value.message


def test_the_region_falls_back_to_the_standard_aws_variables() -> None:
    assert Settings.from_env({"AWS_REGION": "ap-south-1"}).region == "ap-south-1"
    assert Settings.from_env({"AWS_DEFAULT_REGION": "eu-west-1"}).region == "eu-west-1"
    assert (
        Settings.from_env({"MARKETING_AI_REGION": "us-east-1", "AWS_REGION": "eu-west-1"}).region
        == "us-east-1"
    )


def test_an_unrecognised_marketing_ai_variable_is_refused_rather_than_ignored() -> None:
    """DEC-304: a misspelt parameter is otherwise invisible until someone wonders why it did nothing."""
    with pytest.raises(SettingsError) as caught:
        Settings.from_env({"MARKETING_AI_S3_BUKKET": "typo"})
    assert caught.value.code == "SETTINGS_UNKNOWN"
    assert "MARKETING_AI_S3_BUKKET" in caught.value.message


def test_the_non_field_variables_are_exempt_and_are_written_down() -> None:
    """The escape hatch is an explicit list, so adding to it is a visible act."""
    environ = {name: "x" for name in NON_FIELD_ENV_VARS if name != "MARKETING_AI_SETTINGS_SOURCE"}
    assert Settings.from_env(environ) == Settings()


def test_an_explicit_override_beats_the_environment() -> None:
    assert Settings.from_env({"MARKETING_AI_LOG_LEVEL": "DEBUG"}, log_level="WARNING").log_level == "WARNING"


# ---------------------------------------------------------------------------
# A backend that cannot work is refused where it is described
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({"MARKETING_AI_STORAGE_BACKEND": "s3"}, "s3_bucket"),
        ({"MARKETING_AI_METADATA_BACKEND": "postgres"}, "database_url"),
        ({"MARKETING_AI_JOB_BACKEND": "sagemaker"}, "sagemaker_role_arn"),
    ],
)
def test_an_incomplete_backend_is_refused_at_description_time(environ: dict[str, str], expected: str) -> None:
    """DEC-302: not at the first request that happens to need it."""
    with pytest.raises(SettingsError) as caught:
        Settings.from_env(environ)
    assert caught.value.code == "SETTINGS_INCOMPLETE"
    assert expected in caught.value.message


def test_sagemaker_without_s3_is_refused_because_a_remote_job_cannot_read_a_local_disk() -> None:
    with pytest.raises(SettingsError) as caught:
        Settings(
            job_backend=JobBackend.SAGEMAKER,
            region="ap-south-1",
            sagemaker_role_arn="arn:aws:iam::111122223333:role/r",
            sagemaker_image_uri="111122223333.dkr.ecr.ap-south-1.amazonaws.com/i:t",
            sagemaker_train_instance_type="ml.m5.2xlarge",
            sagemaker_processing_instance_type="ml.m5.xlarge",
        )
    assert "storage_backend=s3" in caught.value.message


def test_a_production_deployment_may_not_inherit_the_laptop_s_open_cors() -> None:
    """DEC-307: the wide-open default is a statement about a laptop, not about a load balancer."""
    with pytest.raises(SettingsError) as caught:
        Settings.from_env({"MARKETING_AI_ENV": "prod"})
    assert "cors_origins" in caught.value.message
    narrowed = Settings.from_env(
        {"MARKETING_AI_ENV": "prod", "MARKETING_AI_CORS_ORIGINS": "https://app.example"}
    )
    assert narrowed.cors_origins == ("https://app.example",)


def test_a_prefix_is_normalised_and_a_traversal_is_refused() -> None:
    assert (
        Settings.from_env({"MARKETING_AI_S3_PREFIX": "/marketing-ai/prod/"}).s3_prefix == "marketing-ai/prod"
    )
    with pytest.raises(SettingsError):
        Settings.from_env({"MARKETING_AI_S3_PREFIX": "a/../../etc"})


def test_the_download_ttl_has_a_floor_and_a_ceiling() -> None:
    for value in ("30", "7200"):
        with pytest.raises(SettingsError):
            Settings.from_env({"MARKETING_AI_DOWNLOAD_URL_TTL_SECONDS": value})
    assert (
        Settings.from_env({"MARKETING_AI_DOWNLOAD_URL_TTL_SECONDS": "3600"}).download_url_ttl_seconds == 3600
    )


# ---------------------------------------------------------------------------
# Secrets are invisible unless something deliberately asks
# ---------------------------------------------------------------------------
def test_a_secret_never_reaches_repr_str_or_summary() -> None:
    settings = Settings.from_env(
        {"MARKETING_AI_METADATA_BACKEND": "postgres", "MARKETING_AI_DATABASE_URL": SECRET_VALUE}
    )
    for rendered in (repr(settings), str(settings), settings.summary(), f"{settings}"):
        assert "hunter2-do-not-log" not in rendered
    assert settings.redacted()["database_url"] == REDACTED
    assert settings.database_url is not None
    assert settings.database_url.get_secret_value() == SECRET_VALUE  # still available to whoever asks


def test_summary_is_an_allow_list_so_a_new_field_is_hidden_by_default() -> None:
    """DEC-303: forgetting to add a field here costs a missing word, not a leaked credential."""
    rendered = Settings().summary()
    for field in SUMMARY_FIELDS:
        assert f"{field}=" in rendered
    assert set(SUMMARY_FIELDS).isdisjoint(SECRET_FIELDS)
    unlisted = set(Settings.model_fields) - set(SUMMARY_FIELDS)
    assert unlisted, "this test is vacuous if summary() renders every field"
    for field in unlisted:
        assert f"{field}=" not in rendered


def test_redacted_covers_every_field_and_hides_every_secret() -> None:
    redacted = Settings().redacted()
    assert set(redacted) == set(Settings.model_fields)
    for field in SECRET_FIELDS:
        assert redacted[field] == REDACTED


def test_an_invalid_value_is_never_echoed_back() -> None:
    with pytest.raises(SettingsError) as caught:
        Settings.from_env({"MARKETING_AI_LOG_LEVEL": "LOUD"})
    assert "LOUD" not in caught.value.message
    assert "MARKETING_AI_LOG_LEVEL" in caught.value.message


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
def test_the_parameter_store_and_secret_names_are_derived_from_the_environment() -> None:
    settings = Settings.from_env({"MARKETING_AI_ENV": "staging"})
    assert settings.ssm_prefix == ssm_prefix("staging") == "/marketing-ai/staging/"
    assert settings.secret_name == secret_name("staging") == "marketing-ai/staging/app"


def test_settings_is_frozen_and_forbids_unknown_fields() -> None:
    settings = Settings()
    with pytest.raises(Exception, match=r"frozen"):
        settings.env = Deployment.PROD  # type: ignore[misc]
    with pytest.raises(SettingsError):
        Settings.build(not_a_field=1)


def test_build_storage_and_build_registry_agree_with_build_services(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    storage = build_storage(settings)
    registry = build_registry(settings, storage)
    assert isinstance(storage, LocalStorage)
    assert isinstance(registry, LocalModelRegistry)
    assert storage.root == tmp_path
