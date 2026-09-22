"""`infra/naming.py` mirrors `engine/settings.py`, and here the two are made to agree.

`infra/` must stay importable from a checkout that installed only the deploy extra, so it copies
the names rather than importing the engine. A copy is a thing that drifts, and this is the test
that stops it drifting: the copy is asserted *equal* to the original, so a field added to
`Settings` without a thought for the deployment fails here rather than at container start.

The engine is imported with `importorskip` rather than a plain import. `make infra-setup` installs
the product into `.venv-infra`, so it is normally there; if somebody trims that venv the
infrastructure tests should say "this check did not run" - and `-rs` in `pyproject.toml` prints
every skip with its reason - rather than fail as though the names had diverged.
"""

from __future__ import annotations

from typing import Any

import pytest
from infra.naming import (
    JOB_NAME_PREFIX,
    METRIC_NAMESPACE,
    NON_FIELD_ENV_VARS,
    SETTINGS_ENV_VARS,
    SETTINGS_FIELDS,
    SSM_ROOT,
    secret_name,
    ssm_path_prefix,
)
from infra.observability import UNSET_CLIENT_ID


@pytest.fixture(scope="module")
def settings_module() -> Any:
    return pytest.importorskip(
        "engine.settings",
        reason="the engine is not installed in this venv; `make infra-setup` installs it",
    )


def test_the_field_list_is_the_field_list(settings_module: Any) -> None:
    assert tuple(settings_module.Settings.model_fields) == SETTINGS_FIELDS


def test_the_environment_variable_table_matches(settings_module: Any) -> None:
    assert dict(settings_module.ENV_VARS) == SETTINGS_ENV_VARS


def test_the_non_field_variables_match(settings_module: Any) -> None:
    assert set(settings_module.NON_FIELD_ENV_VARS) == set(NON_FIELD_ENV_VARS)


def test_the_parameter_path_matches(settings_module: Any) -> None:
    assert settings_module.SSM_ROOT == SSM_ROOT
    assert settings_module.ssm_prefix("dev") == ssm_path_prefix("dev")


def test_the_secret_name_matches(settings_module: Any) -> None:
    assert settings_module.secret_name("dev") == secret_name("dev")


def test_the_job_name_prefix_matches_the_settings_default(settings_module: Any) -> None:
    """It is also the IAM resource boundary, so a changed default silently widens a policy."""
    assert settings_module.Settings().sagemaker_job_name_prefix == JOB_NAME_PREFIX


def test_the_metric_namespace_matches() -> None:
    metrics = pytest.importorskip("engine.aws.metrics", reason="the engine is not installed in this venv")
    assert metrics.NAMESPACE == METRIC_NAMESPACE
    assert metrics.UNSET_CLIENT_ID == UNSET_CLIENT_ID


def test_the_storage_prefixes_match() -> None:
    storage = pytest.importorskip("engine.storage", reason="the engine is not installed in this venv")
    from infra.naming import MODELS_PREFIX, RUNS_PREFIX, UPLOADS_PREFIX

    assert storage.upload_key("u1", "f.csv").startswith(UPLOADS_PREFIX)
    assert storage.run_key("r1", "run.json").startswith(RUNS_PREFIX)
    assert storage.published_model_key("churn", 1, "model").startswith(MODELS_PREFIX)


def test_the_bootstrap_probe_prefix_matches() -> None:
    bootstrap = pytest.importorskip(
        "scripts.aws_bootstrap", reason="the engine is not installed in this venv"
    )
    from infra.naming import BOOTSTRAP_PREFIX

    assert bootstrap.PROBE_KEY.startswith(BOOTSTRAP_PREFIX)


def test_a_deployment_built_from_these_names_actually_validates(settings_module: Any) -> None:
    """The final check: the parameter values this app writes make a `Settings` that constructs.

    Everything else here compares names. This one builds the object, which is what the container
    does at start, and is the only way to notice that a combination the deployment selects is one
    `Settings` refuses.
    """
    from infra.app import build_app
    from infra.context import AppContext

    from tests.infra.conftest import ACCOUNT, PROD_CONTEXT_KEYS

    deployment = build_app(AppContext.from_mapping(dict(PROD_CONTEXT_KEYS)), account=ACCOUNT)
    parameters = deployment.compute.deployment_parameters(
        s3_bucket="marketing-ai-prod-123456789012",
        s3_kms_key_id=f"arn:aws:kms:ap-south-1:{ACCOUNT}:key/abc",
        sagemaker_role_arn=f"arn:aws:iam::{ACCOUNT}:role/marketing-ai-prod-sagemaker-execution",
        sagemaker_image_uri=f"{ACCOUNT}.dkr.ecr.ap-south-1.amazonaws.com/marketing-ai@sha256:" + "0" * 64,
        sagemaker_subnet_ids="subnet-aaaa,subnet-bbbb",
        sagemaker_security_group_ids="sg-aaaa",
    )
    # `postgres_dsn` arrives from the application secret, never as a parameter and never as a
    # keyword: it is the one field in `SECRET_FIELDS`, and the point of the split is that the
    # deployment writes it somewhere only the task's own role can read (DEC-305).
    settings = settings_module.settings_from_aws(
        source=_StaticSource(
            parameters, secret={"postgres_dsn": "postgresql+psycopg://u:p@host:5432/marketing_ai"}
        ),
        env="prod",
        environ={"MARKETING_AI_AWS_REGION": "ap-south-1"},
    )
    assert settings.storage_backend == "s3"
    assert settings.job_backend == "sagemaker"
    assert settings.metadata_backend == "postgres"
    assert settings.cors_origins == ("https://marketing.example.com",)


class _StaticSource:
    """A `ParameterSource` backed by two dictionaries; no AWS, no boto3."""

    def __init__(self, parameters: dict[str, str], secret: dict[str, str] | None = None) -> None:
        self._parameters = parameters
        self._secret = dict(secret or {})

    def parameters(self, _prefix: str) -> dict[str, str]:
        return dict(self._parameters)

    def secret(self, _name: str) -> dict[str, str]:
        return dict(self._secret)
