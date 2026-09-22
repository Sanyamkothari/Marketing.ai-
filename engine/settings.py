"""Where this deployment puts things: one frozen `Settings` object and the loaders that fill it.

Phase 1 answered "where do the artefacts live" with two environment variables read at the point of
use (`MARKETING_AI_DATA_DIR` in `engine/storage.py`, `MARKETING_AI_CONFIG_DIR` in `engine/config.py`).
That is enough for one machine and one backend. Phase 4a has three backends per concern, so the
answer has to be written down once, in one shape, that a test can build by hand and an operator can
supply from SSM Parameter Store. This module is that shape.

Three rules hold it together:

**The defaults reproduce Phase 1 exactly.** `Settings()` with nothing set selects the local
filesystem, SQLite and the thread pool, with the same directory and the same worker count. Local
behaviour is unchanged *by construction* - not by anyone remembering to keep it that way - and the
two existing environment variables keep their names, so nothing that set them before has to change.

**Secrets are invisible by default.** Anything in `SECRET_FIELDS` is a `SecretStr`, `__repr__` is
built from an allow-list of non-secret fields rather than from every field, and `SettingsError`
never quotes a value. A field added in a later phase is hidden until someone deliberately adds it to
`SUMMARY_FIELDS`, which is the safe direction for the mistake to fall (DEC-303).

**This module never imports boto3.** `from_aws` takes a `ParameterSource`, and the only
implementation that talks to AWS lives in `engine/aws/secrets.py`. A checkout without the `aws`
extra imports `engine.settings` for free (DEC-306).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Final, Protocol, Self, runtime_checkable

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator

from engine.config import StrictBase

if TYPE_CHECKING:  # these two would be a cycle at runtime; the factories import them lazily
    from engine.registry import ModelRegistry
    from engine.storage import Storage

__all__ = [
    "ENV_VAR_FOR_FIELD",
    "NON_FIELD_ENV_VARS",
    "REDACTED",
    "SECRET_FIELDS",
    "SUMMARY_FIELDS",
    "Deployment",
    "JobBackend",
    "MetadataBackend",
    "ParameterSource",
    "Settings",
    "SettingsError",
    "SettingsSource",
    "StorageBackend",
    "build_registry",
    "build_services",
    "build_storage",
    "resolve_values",
]

ENV_PREFIX: Final[str] = "MARKETING_AI_"
SETTINGS_SOURCE_ENV_VAR: Final[str] = "MARKETING_AI_SETTINGS_SOURCE"
REGION_ENV_VARS: Final[tuple[str, ...]] = ("MARKETING_AI_REGION", "AWS_REGION", "AWS_DEFAULT_REGION")
REDACTED: Final[str] = "<redacted>"

SSM_ROOT: Final[str] = "/marketing-ai"
SECRET_NAME_TEMPLATE: Final[str] = "marketing-ai/{env}/app"

_TRUE: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})
_FALSE: Final[frozenset[str]] = frozenset({"0", "false", "no", "off"})


class SettingsError(Exception):
    """A deployment is described wrongly or incompletely.

    `code` is one of SETTINGS_INVALID (a value does not parse or a field is out of range),
    SETTINGS_INCOMPLETE (a backend was selected without what it needs) or SETTINGS_UNKNOWN
    (a `MARKETING_AI_*` variable names no field). The message names the *field*, never the value:
    a value is the most likely place a credential leaks (DEC-303).
    """

    def __init__(self, code: str, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field


class Deployment(StrEnum):
    """Which deployment this process is; `local` is the default and means "a laptop"."""

    LOCAL = "local"
    DEV = "dev"
    STAGING = "staging"
    PROD = "prod"


class StorageBackend(StrEnum):
    """Which `Storage` implementation `api/deps.py` builds."""

    LOCAL = "local"
    S3 = "s3"


class MetadataBackend(StrEnum):
    """Where run and model metadata lives."""

    SQLITE = "sqlite"
    POSTGRES = "postgres"


class JobBackend(StrEnum):
    """Which `JobRunner` implementation carries a run off the request thread."""

    THREAD = "thread"
    SAGEMAKER = "sagemaker"


class SettingsSource(StrEnum):
    """Where `Settings.load()` reads from: the process environment, or AWS plus the environment."""

    ENV = "env"
    AWS = "aws"


@runtime_checkable
class ParameterSource(Protocol):
    """Somewhere `from_aws` can read parameters and secrets; implemented in `engine/aws/secrets.py`."""

    def parameters(self, prefix: str) -> Mapping[str, str]: ...

    def secret(self, name: str) -> Mapping[str, str]: ...


class Settings(StrictBase):
    """Every "where does this go" answer this deployment has, in one frozen object.

    Built with `from_env()` on a laptop and `from_aws()` in an account; `load()` picks between them
    from `MARKETING_AI_SETTINGS_SOURCE`. A test builds one directly with keyword arguments, which is
    why no loader is called in `__init__` and nothing here reads the environment on its own.
    """

    # identity -----------------------------------------------------------------
    env: Deployment = Field(default=Deployment.LOCAL, description="Which deployment this process is.")
    region: str | None = Field(default=None, description="AWS region; null on a local deployment.")

    # configuration ------------------------------------------------------------
    config_dir: Path | None = Field(
        default=None, description="Configuration root; null falls back to the checkout's configs/."
    )

    # artefact storage ---------------------------------------------------------
    storage_backend: StorageBackend = Field(
        default=StorageBackend.LOCAL, description="Which Storage implementation to build."
    )
    data_dir: Path = Field(default=Path("data"), description="Artefact root for the local backend.")
    s3_bucket: str | None = Field(default=None, description="Bucket for the s3 backend.")
    s3_prefix: str = Field(default="", description="Key prefix inside the bucket; no leading slash.")
    s3_kms_key_id: str | None = Field(
        default=None,
        description="Customer-managed KMS key id or ARN for SSE-KMS; an identifier, not a secret.",
    )
    download_url_ttl_seconds: Annotated[int, Field(ge=60, le=3600)] = Field(
        default=900,
        description="Lifetime of a pre-signed artefact download URL. A policy choice, not a measurement.",
    )
    local_cache_dir: Path | None = Field(
        default=None,
        description="Where the s3 backend materialises a predictor directory; null means a temp directory.",
    )

    # run and model metadata ---------------------------------------------------
    metadata_backend: MetadataBackend = Field(
        default=MetadataBackend.SQLITE, description="Where run and model metadata lives."
    )
    database_url: SecretStr | None = Field(
        default=None, description="SQLAlchemy URL for the postgres backend. Secret: it carries a password."
    )
    postgres_schema: str | None = Field(
        default=None,
        description="Schema the metadata tables live in; null uses the connection's search path.",
    )

    # jobs ---------------------------------------------------------------------
    job_backend: JobBackend = Field(
        default=JobBackend.THREAD, description="Which JobRunner carries a run off the request thread."
    )
    job_max_workers: Annotated[int, Field(ge=1, le=32)] = Field(
        default=2, description="Thread-pool size for the thread backend; matches Phase 1's default."
    )
    sagemaker_role_arn: str | None = Field(
        default=None, description="Execution role a SageMaker job assumes."
    )
    sagemaker_image_uri: str | None = Field(default=None, description="ECR image a SageMaker job runs.")
    sagemaker_train_instance_type: str | None = Field(
        default=None,
        description="Instance type for a training job. No default: an instance type is a cost decision.",
    )
    sagemaker_processing_instance_type: str | None = Field(
        default=None, description="Instance type for a processing job. No default, for the same reason."
    )
    sagemaker_instance_count: Annotated[int, Field(ge=1)] = Field(default=1, description="Instances per job.")
    sagemaker_volume_size_gb: int | None = Field(
        default=None, description="Attached volume size; null leaves the service default in place."
    )
    sagemaker_max_runtime_seconds: int | None = Field(
        default=None, description="Job time limit; null leaves the service default in place."
    )
    sagemaker_max_concurrent_jobs: Annotated[int, Field(ge=1)] = Field(
        default=2, description="How many jobs may run at once before a run waits for compute."
    )
    sagemaker_subnet_ids: tuple[str, ...] = Field(
        default=(), description="Private subnets a job attaches to; empty means no VPC configuration."
    )
    sagemaker_security_group_ids: tuple[str, ...] = Field(
        default=(), description="Security groups a job attaches to."
    )
    sagemaker_job_name_prefix: str = Field(
        default="marketing-ai",
        description="Prefix every job name carries; it is also the IAM resource boundary.",
    )

    # generative ----------------------------------------------------------------
    bedrock_enabled: bool = Field(
        default=False,
        description="Reserved for the generative phase; nothing in this repository reads it yet.",
    )
    bedrock_model_ids: tuple[str, ...] = Field(
        default=(), description="Reserved for the generative phase; empty means deny, never allow-all."
    )

    # observability --------------------------------------------------------------
    log_level: str = Field(default="INFO", description="Root log level.")
    log_format: str = Field(default="text", description="`text` or `json`; json is the CloudWatch shape.")
    metrics_backend: str = Field(
        default="none", description="`none` or `emf`; emf writes CloudWatch EMF log lines."
    )
    client_id: str | None = Field(
        default=None, description="Which customer this deployment serves; tags artefacts and metrics."
    )

    # the browser -----------------------------------------------------------------
    cors_origins: tuple[str, ...] = Field(
        default=("*",),
        description="Origins the API answers. The default reproduces DEC-024 and is refused on a prod deployment.",
    )

    @field_validator("s3_prefix")
    @classmethod
    def _clean_prefix(cls, value: str) -> str:
        """A prefix is a relative key fragment: no leading slash, no dot segment, no trailing slash."""
        cleaned = value.strip("/")
        if any(segment in {".", ".."} for segment in cleaned.split("/")):
            raise ValueError("must not contain a '.' or '..' segment")
        return cleaned

    @field_validator("log_level")
    @classmethod
    def _known_level(cls, value: str) -> str:
        level = value.upper()
        if level not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
            raise ValueError("must be one of CRITICAL, ERROR, WARNING, INFO, DEBUG")
        return level

    @field_validator("log_format")
    @classmethod
    def _known_format(cls, value: str) -> str:
        chosen = value.lower()
        if chosen not in {"text", "json"}:
            raise ValueError("must be 'text' or 'json'")
        return chosen

    @field_validator("metrics_backend")
    @classmethod
    def _known_metrics(cls, value: str) -> str:
        chosen = value.lower()
        if chosen not in {"none", "emf"}:
            raise ValueError("must be 'none' or 'emf'")
        return chosen

    @model_validator(mode="after")
    def _backends_have_what_they_need(self) -> Self:
        """A backend that cannot work is refused here rather than at the first request (DEC-302)."""
        if self.storage_backend is StorageBackend.S3 and not self.s3_bucket:
            raise ValueError("storage_backend=s3 needs s3_bucket")
        if self.metadata_backend is MetadataBackend.POSTGRES and self.database_url is None:
            raise ValueError("metadata_backend=postgres needs database_url")
        if self.job_backend is JobBackend.SAGEMAKER:
            missing = [
                name
                for name in (
                    "sagemaker_role_arn",
                    "sagemaker_image_uri",
                    "sagemaker_train_instance_type",
                    "sagemaker_processing_instance_type",
                    "region",
                )
                if getattr(self, name) is None
            ]
            if missing:
                raise ValueError(f"job_backend=sagemaker needs {', '.join(missing)}")
            if self.storage_backend is not StorageBackend.S3:
                raise ValueError(
                    "job_backend=sagemaker needs storage_backend=s3: a remote job cannot read a local disk"
                )
        if self.env is Deployment.PROD and "*" in self.cors_origins:
            # DEC-024 left CORS wide open because the Phase 1 UI is opened as a local file. That is a
            # statement about a laptop, not about an internet-facing load balancer, and the difference
            # has to be enforced rather than remembered: a production deployment that never mentions
            # cors_origins would otherwise inherit the laptop's answer by omission (DEC-307).
            raise ValueError(
                "env=prod must set cors_origins; '*' is the local default and is not a deployment"
            )
        return self

    def __init__(self, **values: Any) -> None:
        """Build a `Settings`, turning any `ValidationError` into a `SettingsError`.

        The constructor is wrapped, not just the loaders, because of what pydantic puts in the
        message. A failing model validator renders the whole input mapping as `input_value=...`,
        truncated in the middle - so a long database URL happens to come out mangled and a short one
        comes out whole. "Happens to" is not a guarantee, and DEC-303 says a value never reaches a
        message. Wrapping here means there is no construction path left that can leak one:
        `from_env`, `from_aws`, a test fixture and a direct `Settings(...)` all raise the same
        exception carrying the same field-only message.
        """
        try:
            super().__init__(**values)
        except ValidationError as exc:
            raise _settings_error(exc) from None

    # loaders --------------------------------------------------------------------
    @classmethod
    def build(cls, **values: Any) -> Settings:
        """`Settings(**values)`; a named entry point the three loaders below share."""
        return cls(**values)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None, **overrides: Any) -> Settings:
        """From the process environment, with `overrides` winning."""
        env = os.environ if environ is None else environ
        return cls.build(**resolve_values(parameters={}, secrets={}, environ=env, overrides=overrides))

    @classmethod
    def from_aws(
        cls,
        *,
        source: ParameterSource,
        env: str | None = None,
        environ: Mapping[str, str] | None = None,
        **overrides: Any,
    ) -> Settings:
        """From SSM Parameter Store and Secrets Manager, with the environment and `overrides` winning.

        Precedence, lowest first: SSM parameters, the Secrets Manager document, the process
        environment, then explicit overrides (DEC-301). The environment beating AWS is deliberate:
        it is what lets an operator override one value on a task without editing a parameter, and
        what lets a test point a deployed shape at a local directory.
        """
        process_env = os.environ if environ is None else environ
        name = env or process_env.get(f"{ENV_PREFIX}ENV") or Deployment.LOCAL.value
        parameters = _fields_from(source.parameters(ssm_prefix(name)))
        secrets = _fields_from(source.secret(secret_name(name)))
        overrides.setdefault("env", name)
        return cls.build(
            **resolve_values(parameters=parameters, secrets=secrets, environ=process_env, overrides=overrides)
        )

    @classmethod
    def load(
        cls, environ: Mapping[str, str] | None = None, *, source: ParameterSource | None = None
    ) -> Settings:
        """`from_env()`, or `from_aws()` when `MARKETING_AI_SETTINGS_SOURCE=aws`.

        The AWS branch imports `engine.aws.secrets` lazily, so a checkout without boto3 never pays
        for the import and never fails on it.
        """
        env = os.environ if environ is None else environ
        raw = env.get(SETTINGS_SOURCE_ENV_VAR, SettingsSource.ENV.value).strip().lower()
        if raw not in set(SettingsSource):
            raise SettingsError(
                "SETTINGS_INVALID",
                f"{SETTINGS_SOURCE_ENV_VAR} must be 'env' or 'aws'.",
                field=SETTINGS_SOURCE_ENV_VAR,
            )
        if SettingsSource(raw) is SettingsSource.ENV:
            return cls.from_env(env)
        if source is None:
            # A deliberate local import: boto3 is an optional dependency (DEC-306).
            from engine.aws.secrets import AwsParameterSource

            source = AwsParameterSource(region=_region_from(env))
        return cls.from_aws(source=source, environ=env)

    # derived values ----------------------------------------------------------------
    @property
    def ssm_prefix(self) -> str:
        """The Parameter Store path this deployment's parameters live under."""
        return ssm_prefix(self.env.value)

    @property
    def secret_name(self) -> str:
        """The Secrets Manager secret this deployment's secrets live in."""
        return secret_name(self.env.value)

    @property
    def registry_path(self) -> Path:
        """Where `LocalModelRegistry` keeps `registry.db` for this deployment."""
        # A deliberate local import: `engine.registry` imports nothing from here, and this keeps it that way.
        from engine.registry import REGISTRY_FILENAME

        return self.data_dir / REGISTRY_FILENAME

    # rendering ------------------------------------------------------------------
    def redacted(self) -> dict[str, Any]:
        """Every field, with every secret replaced by `REDACTED`. Safe to log or to print."""
        out: dict[str, Any] = {}
        for name in type(self).model_fields:
            out[name] = REDACTED if name in SECRET_FIELDS else _plain(getattr(self, name))
        return out

    def summary(self) -> str:
        """One line naming the backends, for the startup log.

        An allow-list, not a filter: a field added later is absent until someone adds it here, so
        the failure mode of forgetting is a missing word rather than a leaked credential.
        """
        parts = [f"{name}={_plain(getattr(self, name))}" for name in SUMMARY_FIELDS]
        return " ".join(parts)

    def __repr__(self) -> str:
        """`Settings(...)` built from `summary()`; pydantic's own repr would print every field."""
        return f"{type(self).__name__}({self.summary()})"

    __str__ = __repr__


SECRET_FIELDS: Final[frozenset[str]] = frozenset({"database_url"})
"""Fields `redacted()` hides and `summary()` may never name."""

SUMMARY_FIELDS: Final[tuple[str, ...]] = (
    "env",
    "region",
    "storage_backend",
    "metadata_backend",
    "job_backend",
    "log_format",
    "metrics_backend",
)
"""The allow-list `summary()` renders. Adding a field here is a deliberate act."""

_LEGACY_ENV_VARS: Final[Mapping[str, str]] = {
    "data_dir": "MARKETING_AI_DATA_DIR",
    "config_dir": "MARKETING_AI_CONFIG_DIR",
}
"""The two names Phase 1 already reads. They keep their spelling so nothing that set them breaks."""

ENV_VAR_FOR_FIELD: Final[Mapping[str, str]] = {
    name: _LEGACY_ENV_VARS.get(name, f"{ENV_PREFIX}{name.upper()}") for name in Settings.model_fields
}
"""Field name -> the environment variable and SSM parameter leaf that fills it."""

_FIELD_FOR_ENV_VAR: Final[Mapping[str, str]] = {var: name for name, var in ENV_VAR_FOR_FIELD.items()}

_TUPLE_FIELDS: Final[frozenset[str]] = frozenset(
    {"sagemaker_subnet_ids", "sagemaker_security_group_ids", "bedrock_model_ids", "cors_origins"}
)
"""Fields a single environment variable fills with a comma-separated list."""

NON_FIELD_ENV_VARS: Final[frozenset[str]] = frozenset(
    {
        SETTINGS_SOURCE_ENV_VAR,
        "MARKETING_AI_JOB_SPEC_KEY",
        "MARKETING_AI_FAILURE_PATH",
        "MARKETING_AI_REQUIRE_POSTGRES",
        "MARKETING_AI_TEST_DATABASE_URL",
    }
)
"""`MARKETING_AI_*` variables that are deliberately not settings.

`resolve_values` refuses a `MARKETING_AI_*` variable it does not recognise, because a typo in a
parameter name is otherwise invisible until someone wonders why a setting had no effect (DEC-304).
That check needs an explicit escape hatch, and an explicit list is the honest form of one: a name
here is a name somebody decided does not describe a deployment - the job spec key the container is
handed, the failure file SageMaker reads, and the two variables the Postgres test fixtures use.
"""
_BOOL_FIELDS: Final[frozenset[str]] = frozenset({"bedrock_enabled"})


def ssm_prefix(env: str) -> str:
    """The Parameter Store path for `env`, with the trailing slash `GetParametersByPath` expects."""
    return f"{SSM_ROOT}/{env}/"


def secret_name(env: str) -> str:
    """The Secrets Manager secret name for `env`."""
    return SECRET_NAME_TEMPLATE.format(env=env)


def resolve_values(
    *,
    parameters: Mapping[str, Any],
    secrets: Mapping[str, Any],
    environ: Mapping[str, str],
    overrides: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge the four sources into the keyword arguments `Settings` is built from.

    Precedence, lowest first: `parameters`, `secrets`, `environ`, `overrides` (DEC-301). Only keys
    that name a field are taken from the first three; an unrecognised `MARKETING_AI_*` variable is a
    `SettingsError` rather than a silent no-op, because a typo in a parameter name is otherwise
    invisible until someone wonders why a setting had no effect (DEC-304).
    """
    values: dict[str, Any] = {}
    values.update({name: value for name, value in parameters.items() if name in ENV_VAR_FOR_FIELD})
    values.update({name: value for name, value in secrets.items() if name in ENV_VAR_FOR_FIELD})
    for variable, raw in environ.items():
        if not variable.startswith(ENV_PREFIX) or variable in NON_FIELD_ENV_VARS:
            continue
        field = _FIELD_FOR_ENV_VAR.get(variable)
        if field is None:
            raise SettingsError(
                "SETTINGS_UNKNOWN",
                f"{variable} does not name a setting. Remove it or correct the spelling.",
                field=variable,
            )
        values[field] = raw
    if "region" not in overrides and values.get("region") is None:
        region = _region_from(environ)
        if region is not None:
            values["region"] = region
    values.update(overrides)
    return {name: _coerce(name, value) for name, value in values.items() if value is not None}


def _region_from(environ: Mapping[str, str]) -> str | None:
    """The first region any of the three accepted variables gives, or `None`."""
    for variable in REGION_ENV_VARS:
        value = environ.get(variable, "").strip()
        if value:
            return value
    return None


def _fields_from(raw: Mapping[str, str]) -> dict[str, str]:
    """Parameter or secret keys mapped onto field names.

    A key is accepted as the bare field name (`s3_bucket`) or as its environment-variable spelling
    (`MARKETING_AI_S3_BUCKET`), so one naming convention in Parameter Store serves both. A key that
    matches neither is ignored: Parameter Store is a shared namespace and a later phase will put
    things under it that this version knows nothing about (DEC-305).
    """
    out: dict[str, str] = {}
    for key, value in raw.items():
        leaf = key.rsplit("/", 1)[-1]
        field = leaf if leaf in ENV_VAR_FOR_FIELD else _FIELD_FOR_ENV_VAR.get(leaf.upper())
        if field is not None:
            out[field] = value
    return out


def _coerce(name: str, value: Any) -> Any:
    """Turn one string from the environment, SSM or a secret into the field's type.

    Values that arrive already typed (a test passing `Path("x")` or `StorageBackend.S3`) pass
    through untouched; pydantic does the rest.
    """
    if not isinstance(value, str):
        return value
    if name in _TUPLE_FIELDS:
        return tuple(part.strip() for part in value.split(",") if part.strip())
    if name in _BOOL_FIELDS:
        return _as_bool(name, value)
    return value


def _as_bool(name: str, value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise SettingsError(
        "SETTINGS_INVALID",
        f"{ENV_VAR_FOR_FIELD[name]} must be one of {', '.join(sorted(_TRUE | _FALSE))}.",
        field=name,
    )


def _plain(value: Any) -> Any:
    """A field rendered for a log line: enums as their value, paths and tuples as text."""
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return ",".join(str(item) for item in value)
    return value


def build_storage(settings: Settings) -> Storage:
    """The `Storage` this deployment describes.

    `api/deps.py` is the only place the *API* builds one, and it calls this; the SageMaker container
    has no `api/deps.py` at all and calls this too. One function, so the API and a job can never
    disagree about where the artefacts are (DEC-308).
    """
    # A deliberate local import: it keeps `engine.settings` cheap to import and free of any cycle.
    from engine.storage import LocalStorage

    if settings.storage_backend is StorageBackend.LOCAL:
        return LocalStorage(settings.data_dir)
    # A deliberate local import: boto3 is an optional dependency and a laptop must not pay for it (DEC-306).
    from engine.aws.s3_storage import S3Storage

    bucket = settings.s3_bucket
    if bucket is None:  # pragma: no cover - the model validator already refused this
        raise SettingsError("SETTINGS_INCOMPLETE", "storage_backend=s3 needs s3_bucket.", field="s3_bucket")
    return S3Storage(
        bucket,
        prefix=settings.s3_prefix,
        region_name=settings.region,
        kms_key_id=settings.s3_kms_key_id,
        client_id=settings.client_id,
        workspace=settings.local_cache_dir,
    )


def build_registry(settings: Settings, storage: Storage) -> ModelRegistry:
    """The `ModelRegistry` this deployment describes, over `storage` when it keeps files."""
    if settings.metadata_backend is MetadataBackend.SQLITE:
        # A deliberate local import: it keeps `engine.settings` cheap to import and free of any cycle.
        from engine.registry import LocalModelRegistry

        return LocalModelRegistry(settings.registry_path)
    # Deliberate local imports: psycopg and boto3 are optional dependencies (DEC-306).
    from engine.aws.postgres import postgres_store
    from engine.aws.s3_registry import S3ModelRegistry

    return S3ModelRegistry(postgres_store(settings), storage)


def build_services(settings: Settings) -> tuple[Storage, ModelRegistry]:
    """`(storage, registry)` for this deployment, built in the one order that works."""
    storage = build_storage(settings)
    return storage, build_registry(settings, storage)


def _settings_error(exc: ValidationError) -> SettingsError:
    """One pydantic `ValidationError`, as a `SettingsError` that names the field and not the value."""
    first = exc.errors()[0]
    field = ".".join(str(part) for part in first.get("loc", ())) or None
    code = (
        "SETTINGS_INCOMPLETE" if first.get("type") == "value_error" and field is None else "SETTINGS_INVALID"
    )
    return SettingsError(code, _message_for(field, first.get("msg", "is not valid")), field=field)


def _message_for(field: str | None, detail: str) -> str:
    """A sentence that names the field and the rule, and never the value."""
    detail = detail.removeprefix("Value error, ")
    if field is None:
        return f"This deployment is described incompletely: {detail}."
    variable = ENV_VAR_FOR_FIELD.get(field, field)
    return f"{variable} ({field}) {detail}."
