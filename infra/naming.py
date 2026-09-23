"""The names two sides of a boundary have to agree on, written down once.

A deployment is two artefacts that never import each other: the CDK app in this package, and the
application in `engine/`. They still have to agree about a dozen strings - the SSM path a parameter
lives at, the environment variable that fills a setting, the S3 prefix a policy is written against,
the log group a task writes to, the prefix every SageMaker job name starts with. Every one of those
is a place where a rename in one tree is a silent outage in the other.

So they are constants here, and `tests/infra/test_settings_contract.py` compares them against
`engine.settings` itself. That test is the point of this module: the names are not "kept in sync by
remembering", they are asserted equal to the frozen contract, and a field added to `Settings`
without a thought for the deployment fails the infrastructure suite (DEC-368).

Nothing in this module imports `engine`. The infra venv installs the product only so that the tests
*can* import it; `infra/` itself must stay runnable from a checkout that has nothing but the deploy
extra.
"""

from __future__ import annotations

from typing import Final

from aws_cdk import aws_logs as logs

__all__ = [
    "BOOTSTRAP_PREFIX",
    "JOB_NAME_PREFIX",
    "METRIC_NAMESPACE",
    "MODELS_PREFIX",
    "NON_FIELD_ENV_VARS",
    "OBJECT_PREFIXES",
    "PRODUCT",
    "RUNS_PREFIX",
    "SETTINGS_ENV_VARS",
    "SETTINGS_FIELDS",
    "SSM_ROOT",
    "UPLOADS_PREFIX",
    "api_log_group_name",
    "db_instance_identifier",
    "jobs_log_group_name",
    "log_retention",
    "rds_log_group_name",
    "secret_name",
    "ssm_parameter_name",
    "ssm_path_prefix",
    "stack_name",
]

PRODUCT: Final[str] = "marketing-ai"
"""The one word every name is built from, and the value of the `product` tag."""

SSM_ROOT: Final[str] = "/marketing-ai"
"""Mirrors `engine.settings.SSM_ROOT`."""

SECRET_NAME_TEMPLATE: Final[str] = "marketing-ai/{env}/app"
"""Mirrors `engine.settings.SECRET_NAME_TEMPLATE`."""

JOB_NAME_PREFIX: Final[str] = "marketing-ai"
"""Mirrors the default of `Settings.sagemaker_job_name_prefix`, and it is the IAM resource boundary.

Every SageMaker job this product creates is named `<prefix>-...`, and every SageMaker statement in
`infra/policies.py` is scoped to `.../<prefix>-*`. The runner never calls a `List*` API, so the
prefix is the whole of what the role can see.
"""

METRIC_NAMESPACE: Final[str] = "MarketingAI"
"""The EMF namespace `metrics_backend=emf` writes into, and the only namespace the task may write."""

UPLOADS_PREFIX: Final[str] = "uploads/"
RUNS_PREFIX: Final[str] = "runs/"
MODELS_PREFIX: Final[str] = "models/"
BOOTSTRAP_PREFIX: Final[str] = "_bootstrap/"

OBJECT_PREFIXES: Final[tuple[str, ...]] = (UPLOADS_PREFIX, RUNS_PREFIX, MODELS_PREFIX, BOOTSTRAP_PREFIX)
"""The four prefixes the product writes under, and the whole of what an S3 statement may name.

`uploads/` is `engine.storage.upload_key`, `runs/` is `run_key`, `models/` is
`published_model_key`, and `_bootstrap/` is the single probe object
`scripts/aws_bootstrap.py` writes and deletes to prove the role works. Granting the bucket's whole
contents instead would be one character shorter and would also grant every prefix a later phase
invents.
"""

SETTINGS_FIELDS: Final[tuple[str, ...]] = (
    "storage_backend",
    "job_backend",
    "metadata_backend",
    "llm_backend",
    "aws_region",
    "data_dir",
    "config_dir",
    "s3_bucket",
    "s3_prefix",
    "job_max_workers",
    "sagemaker_role_arn",
    "sagemaker_instance_type",
    "postgres_dsn",
    "bedrock_model_id",
    "env",
    "s3_kms_key_id",
    "download_url_ttl_seconds",
    "local_cache_dir",
    "postgres_schema",
    "sagemaker_image_uri",
    "sagemaker_processing_instance_type",
    "sagemaker_instance_count",
    "sagemaker_volume_size_gb",
    "sagemaker_max_runtime_seconds",
    "sagemaker_max_concurrent_jobs",
    "sagemaker_subnet_ids",
    "sagemaker_security_group_ids",
    "sagemaker_job_name_prefix",
    "log_level",
    "log_format",
    "metrics_backend",
    "client_id",
    "cors_origins",
    # Phase 4b (docs/PHASE4B_PLAN.md): access control, the audit export, the scheduler and alerts.
    "auth_mode",
    "auth_session_ttl_seconds",
    "audit_export_bucket",
    "audit_export_prefix",
    "audit_retention_days",
    "scheduler_backend",
    "scheduler_tick_seconds",
    "scheduler_group_name",
    "scheduler_target_arn",
    "scheduler_role_arn",
    "alert_backend",
    "alert_sns_topic_arn",
)
"""Every field of `engine.settings.Settings`, in `engine.settings.ENV_VARS` order.

A field name is simultaneously three things: the field, the SSM parameter leaf under
`/marketing-ai/<env>/`, and (upper-cased, with the `MARKETING_AI_` prefix) the environment
variable. `SETTINGS_ENV_VARS` below is derived from this tuple rather than typed out again, so the
three can only disagree in one place.

This is a second copy of a list that `engine.settings` also holds, and that is deliberate: `infra`
is a separate virtualenv with aws-cdk-lib in it and no pydantic, so it cannot import the engine.
`tests/infra/test_settings_contract.py` imports both and fails when they disagree, which is what
makes the copy safe to keep (DEC-361).
"""

SETTINGS_ENV_VARS: Final[dict[str, str]] = {name: f"MARKETING_AI_{name.upper()}" for name in SETTINGS_FIELDS}
"""Field name -> environment variable. The frozen table; a task definition may set nothing else.

Every name derives the same way, with no exceptions: `engine.settings.ENV_VARS` spells all of its
values `f"{ENV_PREFIX}{name.upper()}"`, and the contract test above compares the two mappings key
by key rather than trusting that sentence.
"""

NON_FIELD_ENV_VARS: Final[frozenset[str]] = frozenset(
    {
        "MARKETING_AI_SETTINGS_SOURCE",
        "MARKETING_AI_JOB_SPEC_KEY",
        "MARKETING_AI_FAILURE_PATH",
        "MARKETING_AI_REQUIRE_POSTGRES",
        "MARKETING_AI_TEST_DATABASE_URL",
    }
)
"""`MARKETING_AI_*` variables that are deliberately not settings. Mirrors `engine.settings`.

A `MARKETING_AI_*` variable that is in neither this set nor `SETTINGS_ENV_VARS` makes the
application refuse to start, so a task definition that invents one is a deployment that does not
boot (DEC-304). Of these five, a task definition sets exactly one: `MARKETING_AI_SETTINGS_SOURCE`.
"""


def ssm_path_prefix(env_name: str) -> str:
    """The Parameter Store path this deployment's parameters live under, with its trailing slash."""
    return f"{SSM_ROOT}/{env_name}/"


def ssm_parameter_name(env_name: str, field_name: str) -> str:
    """`/marketing-ai/<env>/<field>`; the leaf is a field name, flat, with no hyphens or children.

    `AwsParameterSource.parameters` calls `GetParametersByPath` with `Recursive=False`, so a
    parameter one level deeper is invisible to the application. There is nothing to stop CDK
    writing one, which is why this function exists and is the only way a parameter is named here.
    """
    if field_name not in SETTINGS_ENV_VARS:
        raise ValueError(f"{field_name!r} is not a field of Settings; it would be ignored on read.")
    return f"{SSM_ROOT}/{env_name}/{field_name}"


def secret_name(env_name: str) -> str:
    """The Secrets Manager secret holding this deployment's one credential, the database URL."""
    return SECRET_NAME_TEMPLATE.format(env=env_name)


def stack_name(env_name: str, component: str) -> str:
    """`marketing-ai-<env>-<component>`; what `cdk deploy --all` prints and an operator greps for."""
    return f"{PRODUCT}-{env_name}-{component}"


def api_log_group_name(env_name: str) -> str:
    """Where the Fargate tasks write. Created by the observability stack, used by the compute stack."""
    return f"/{PRODUCT}/{env_name}/api"


def jobs_log_group_name(env_name: str) -> str:
    """Where SageMaker job containers write, when the runner points them at a group of our own."""
    return f"/{PRODUCT}/{env_name}/jobs"


def db_instance_identifier(env_name: str) -> str:
    """The RDS instance identifier.

    Explicit rather than generated, because the identifier is what the CloudWatch log group for the
    exported Postgres logs is named after, and the observability stack has to be able to create that
    group - with a retention - before RDS creates it without one.
    """
    return f"{PRODUCT}-{env_name}"


def rds_log_group_name(env_name: str) -> str:
    """`/aws/rds/instance/<identifier>/postgresql`, the name RDS exports `postgresql` logs to."""
    return f"/aws/rds/instance/{db_instance_identifier(env_name)}/postgresql"


_RETENTION_DAYS: Final[dict[int, logs.RetentionDays]] = {
    1: logs.RetentionDays.ONE_DAY,
    3: logs.RetentionDays.THREE_DAYS,
    5: logs.RetentionDays.FIVE_DAYS,
    7: logs.RetentionDays.ONE_WEEK,
    14: logs.RetentionDays.TWO_WEEKS,
    30: logs.RetentionDays.ONE_MONTH,
    60: logs.RetentionDays.TWO_MONTHS,
    90: logs.RetentionDays.THREE_MONTHS,
    180: logs.RetentionDays.SIX_MONTHS,
    365: logs.RetentionDays.ONE_YEAR,
    731: logs.RetentionDays.TWO_YEARS,
}
"""CloudWatch Logs accepts a fixed set of retentions, not any number of days; this is that set."""


def log_retention(days: int) -> logs.RetentionDays:
    """The `RetentionDays` member for `days`, refusing a number CloudWatch Logs does not offer.

    The one translation from this deployment's `log_retention_days` to CDK's enum. It lives beside
    the log group names because both are things the observability stack and the stacks that write
    into those groups have to agree about.
    """
    if days not in _RETENTION_DAYS:
        raise ValueError(
            f"log_retention_days={days} is not a retention CloudWatch Logs offers; "
            f"choose one of {', '.join(str(value) for value in sorted(_RETENTION_DAYS))}."
        )
    return _RETENTION_DAYS[days]
