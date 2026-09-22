"""`engine.aws.secrets` against moto: what it reads, what it refuses, and what it never says.

Offline: moto stands in for SSM Parameter Store and Secrets Manager, so nothing here needs an AWS
account or credentials.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest

boto3 = pytest.importorskip("boto3", reason="the `aws` extra is not installed (pip install -e '.[dev]')")
moto = pytest.importorskip("moto", reason="moto is not installed (pip install -e '.[dev]')")

from engine.aws.secrets import (  # noqa: E402 - must follow the importorskip guards
    WIRE_LOGGERS,
    AwsParameterSource,
    SecretsError,
    StaticParameterSource,
    quiet_aws_wire_logs,
)
from engine.settings import Settings, secret_name, ssm_prefix  # noqa: E402

REGION = "ap-south-1"
SECRET_VALUE = "postgresql+psycopg://marketing:hunter2-do-not-log@db.internal:5432/marketing"


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A moto-backed AWS, with credentials that are obviously not real."""
    for name, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": REGION,
    }.items():
        monkeypatch.setenv(name, value)
    with moto.mock_aws():
        yield


def put_parameters(values: dict[str, str], *, env: str = "dev", kind: str = "String") -> None:
    client = boto3.client("ssm", region_name=REGION)
    for leaf, value in values.items():
        client.put_parameter(Name=f"{ssm_prefix(env)}{leaf}", Value=value, Type=kind)


def put_secret(document: dict[str, str], *, env: str = "dev") -> None:
    boto3.client("secretsmanager", region_name=REGION).create_secret(
        Name=secret_name(env), SecretString=json.dumps(document)
    )


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
def test_parameters_are_returned_by_leaf_name(aws: None) -> None:
    put_parameters({"s3_bucket": "marketing-ai-dev", "storage_backend": "s3"})
    found = AwsParameterSource(region=REGION).parameters(ssm_prefix("dev"))
    assert found == {"s3_bucket": "marketing-ai-dev", "storage_backend": "s3"}


def test_a_secure_string_is_decrypted(aws: None) -> None:
    put_parameters({"s3_bucket": "encrypted-bucket"}, kind="SecureString")
    assert AwsParameterSource(region=REGION).parameters(ssm_prefix("dev"))["s3_bucket"] == "encrypted-bucket"


def test_every_page_is_read(aws: None) -> None:
    """The real API pages at 10; a deployment can easily describe more settings than that."""
    put_parameters({f"unused_{index}": str(index) for index in range(25)})
    assert len(AwsParameterSource(region=REGION).parameters(ssm_prefix("dev"))) == 25


def test_another_environment_is_not_read(aws: None) -> None:
    put_parameters({"s3_bucket": "dev-bucket"}, env="dev")
    put_parameters({"s3_bucket": "prod-bucket"}, env="prod")
    assert AwsParameterSource(region=REGION).parameters(ssm_prefix("dev")) == {"s3_bucket": "dev-bucket"}


def test_an_absent_prefix_is_empty_rather_than_an_error(aws: None) -> None:
    """A deployment may legitimately describe itself entirely through the environment."""
    assert AwsParameterSource(region=REGION).parameters(ssm_prefix("staging")) == {}


def test_the_secret_is_parsed_as_a_flat_json_object(aws: None) -> None:
    put_secret({"database_url": SECRET_VALUE})
    assert AwsParameterSource(region=REGION).secret(secret_name("dev")) == {"database_url": SECRET_VALUE}


def test_an_absent_secret_is_empty_rather_than_an_error(aws: None) -> None:
    assert AwsParameterSource(region=REGION).secret(secret_name("dev")) == {}


# ---------------------------------------------------------------------------
# Refusing
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("payload", ['"a string"', "[1, 2]", '{"nested": {"a": 1}}', "not json at all"])
def test_a_secret_that_is_not_a_flat_object_of_strings_is_refused(aws: None, payload: str) -> None:
    """Silently ignoring it would leave the deployment quietly misconfigured."""
    boto3.client("secretsmanager", region_name=REGION).create_secret(
        Name=secret_name("dev"), SecretString=payload
    )
    with pytest.raises(SecretsError) as caught:
        AwsParameterSource(region=REGION).secret(secret_name("dev"))
    assert caught.value.code == "SECRETS_MALFORMED"


def test_a_failure_names_the_path_and_never_a_value() -> None:
    class Denied:
        def get_parameters_by_path(self, **kwargs: Any) -> dict[str, Any]:
            raise _client_error("AccessDeniedException")

    with pytest.raises(SecretsError) as caught:
        AwsParameterSource(region=REGION, ssm=Denied()).parameters("/marketing-ai/dev/")
    assert caught.value.code == "SECRETS_ACCESS_DENIED"
    assert "/marketing-ai/dev/" in caught.value.message


def test_an_unexpected_failure_is_unavailable_and_still_says_nothing_about_a_value() -> None:
    class Broken:
        def get_secret_value(self, **kwargs: Any) -> dict[str, Any]:
            raise _client_error("InternalServiceError")

    with pytest.raises(SecretsError) as caught:
        AwsParameterSource(region=REGION, secrets=Broken()).secret("marketing-ai/dev/app")
    assert caught.value.code == "SECRETS_UNAVAILABLE"
    assert SECRET_VALUE not in caught.value.message


# ---------------------------------------------------------------------------
# Not saying
# ---------------------------------------------------------------------------
def test_building_a_source_caps_the_wire_loggers(caplog: pytest.LogCaptureFixture) -> None:
    """botocore at DEBUG prints signed URLs, which are bearer credentials (DEC-309)."""
    for name in WIRE_LOGGERS:
        logging.getLogger(name).setLevel(logging.DEBUG)
    AwsParameterSource(region=REGION)
    for name in WIRE_LOGGERS:
        assert logging.getLogger(name).level == logging.INFO


def test_the_cap_never_raises_a_logger_that_is_already_quieter() -> None:
    logging.getLogger("botocore").setLevel(logging.ERROR)
    quiet_aws_wire_logs()
    assert logging.getLogger("botocore").level == logging.ERROR


def test_a_read_logs_how_many_it_found_and_not_what_they_were(
    aws: None, caplog: pytest.LogCaptureFixture
) -> None:
    put_parameters({"s3_bucket": "marketing-ai-dev"})
    put_secret({"database_url": SECRET_VALUE})
    with caplog.at_level(logging.INFO, logger="engine.aws.secrets"):
        source = AwsParameterSource(region=REGION)
        source.parameters(ssm_prefix("dev"))
        source.secret(secret_name("dev"))
    written = caplog.text
    assert "parameters=1" in written
    assert "keys=1" in written
    assert "marketing-ai-dev" not in written
    assert "hunter2-do-not-log" not in written


# ---------------------------------------------------------------------------
# End to end into Settings
# ---------------------------------------------------------------------------
def test_settings_from_aws_reads_both_stores_and_the_environment_still_wins(aws: None) -> None:
    put_parameters({"storage_backend": "s3", "s3_bucket": "from-ssm", "region": REGION})
    put_secret({"database_url": SECRET_VALUE, "metadata_backend": "postgres"})
    source = AwsParameterSource(region=REGION)

    settings = Settings.from_aws(source=source, env="dev", environ={})
    assert settings.s3_bucket == "from-ssm"
    assert settings.metadata_backend.value == "postgres"
    assert settings.database_url is not None
    assert settings.database_url.get_secret_value() == SECRET_VALUE
    assert SECRET_VALUE not in repr(settings)

    overridden = Settings.from_aws(source=source, env="dev", environ={"MARKETING_AI_S3_BUCKET": "from-env"})
    assert overridden.s3_bucket == "from-env"


def test_a_parameter_may_be_named_either_way(aws: None) -> None:
    """One naming convention in Parameter Store serves both spellings (DEC-305)."""
    put_parameters({"MARKETING_AI_LOG_FORMAT": "json", "log_level": "DEBUG"})
    settings = Settings.from_aws(source=AwsParameterSource(region=REGION), env="dev", environ={})
    assert settings.log_format == "json"
    assert settings.log_level == "DEBUG"


def test_an_unknown_parameter_is_ignored_rather_than_refused(aws: None) -> None:
    """Parameter Store is a shared namespace; a later phase will put things there this one ignores."""
    put_parameters({"s3_bucket": "b", "storage_backend": "s3", "phase_9_setting": "whatever"})
    assert Settings.from_aws(source=AwsParameterSource(region=REGION), env="dev", environ={}).s3_bucket == "b"


def test_the_static_source_behaves_like_the_real_one() -> None:
    source = StaticParameterSource(
        {"/marketing-ai/dev/s3_bucket": "b", "/marketing-ai/dev/storage_backend": "s3"},
        {"marketing-ai/dev/app": {"database_url": SECRET_VALUE, "metadata_backend": "postgres"}},
    )
    settings = Settings.from_aws(source=source, env="dev", environ={})
    assert settings.s3_bucket == "b"
    assert settings.metadata_backend.value == "postgres"


def _client_error(code: str) -> Exception:
    """A botocore `ClientError` with `code`, constructed rather than provoked out of moto."""
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code, "Message": "withheld"}}, "GetParameter")
