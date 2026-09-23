"""Every environment-driven choice the engine makes, in one object.

Phase 1 read three environment variables from three modules. That was fine while there were
three; it does not survive Phase 4, where the storage, the job runner, the metadata store and
the LLM each have a local implementation and an AWS one, and the choice between them has to be
visible in one place rather than deduced from which variables happen to be exported.

So: one `Settings`, built from `os.environ` on demand, holding the four backend switches and the
connection fields each backend needs. Every default reproduces Phase 1 behaviour exactly - local
storage under `data/`, a two-worker thread pool, SQLite beside the artefacts, the fake LLM - so
an unset environment runs the engine it ran before this module existed.

Three rules hold here and are worth stating because they are what the module is for.

1. **Secrets arrive as environment variables and are never written down anywhere else.** No key,
   account id, bucket name or ARN belongs in a config file, a document or a commit. `postgres_dsn`
   is a :class:`~pydantic.SecretStr`, so a stray `repr()` or log line prints `**********` rather
   than a password.
2. **A backend that cannot work says so at construction.** Selecting `s3` without a bucket is a
   `SettingsError` naming the variable that is missing, not an `AttributeError` eight frames into
   a stage.
3. **Nothing is cached.** :func:`settings` reads the environment each call, so a test that patches
   `MARKETING_AI_DATA_DIR` changes the next call's answer, exactly as the three direct
   `os.environ` reads it replaces did.

This module imports nothing from `engine`: `engine.config` imports it, and `engine.config` is the
root of the engine's import graph (`tests/integration/test_engine_imports.py`).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final, Literal, Protocol, Self, runtime_checkable

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

__all__ = [
    "DEFAULT_CONFIG_DIR",
    "DEFAULT_DATA_DIR",
    "ENV_PREFIX",
    "ENV_VARS",
    "REGISTRY_FILENAME",
    "JobBackend",
    "LLMBackend",
    "MetadataBackend",
    "Settings",
    "SettingsError",
    "StorageBackend",
    "settings",
]

StorageBackend = Literal["local", "s3"]
"""Where artefacts live: the local filesystem (Phase 1) or S3 (Phase 4a)."""

JobBackend = Literal["thread", "sagemaker"]
"""What carries a run off the request thread: a thread pool (Phase 1) or SageMaker (Phase 4a)."""

MetadataBackend = Literal["sqlite", "postgres"]
"""Where the model registry keeps its rows: SQLite (Phase 1) or Postgres (Phase 4a)."""

LLMBackend = Literal["fake", "bedrock"]
"""Which `engine.llm.LLMClient` is built: the deterministic fake (Phase 1) or Bedrock (Phase 3a)."""

DeploymentName = Literal["local", "dev", "staging", "prod"]
"""Which deployment a process is. Names the SSM path and the Secrets Manager secret (Phase 4a)."""

LogFormat = Literal["text", "json"]
"""How log lines are rendered. `json` is the shape CloudWatch parses (Phase 4a)."""

MetricsBackend = Literal["none", "emf"]
"""Where measurements go: nowhere, or CloudWatch embedded-metric log lines (Phase 4a)."""

ENV_PREFIX: Final[str] = "MARKETING_AI_"
"""Every variable this module reads starts with it; nothing outside the prefix is consulted."""

DEFAULT_DATA_DIR: Final[str] = "data"
"""Phase 1's artefact root, relative to the working directory and gitignored."""

DEFAULT_CONFIG_DIR: Final[Path] = Path(__file__).resolve().parent.parent / "configs"
"""The checkout's `configs/`, used when no directory is given and none is exported."""

REGISTRY_FILENAME: Final[str] = "registry.db"
"""The SQLite registry's file name, inside :attr:`Settings.data_dir`."""

ENV_VARS: Final[Mapping[str, str]] = {
    "storage_backend": f"{ENV_PREFIX}STORAGE_BACKEND",
    "job_backend": f"{ENV_PREFIX}JOB_BACKEND",
    "metadata_backend": f"{ENV_PREFIX}METADATA_BACKEND",
    "llm_backend": f"{ENV_PREFIX}LLM_BACKEND",
    "aws_region": f"{ENV_PREFIX}AWS_REGION",
    "data_dir": f"{ENV_PREFIX}DATA_DIR",
    "config_dir": f"{ENV_PREFIX}CONFIG_DIR",
    "s3_bucket": f"{ENV_PREFIX}S3_BUCKET",
    "s3_prefix": f"{ENV_PREFIX}S3_PREFIX",
    "job_max_workers": f"{ENV_PREFIX}JOB_MAX_WORKERS",
    "sagemaker_role_arn": f"{ENV_PREFIX}SAGEMAKER_ROLE_ARN",
    "sagemaker_instance_type": f"{ENV_PREFIX}SAGEMAKER_INSTANCE_TYPE",
    "postgres_dsn": f"{ENV_PREFIX}POSTGRES_DSN",
    "bedrock_model_id": f"{ENV_PREFIX}BEDROCK_MODEL_ID",
    # --- Phase 4a. Extensions, not replacements: every name above keeps its meaning
    # (PARALLEL_WORK_PROTOCOL.md section 2).
    "env": f"{ENV_PREFIX}ENV",
    "s3_kms_key_id": f"{ENV_PREFIX}S3_KMS_KEY_ID",
    "download_url_ttl_seconds": f"{ENV_PREFIX}DOWNLOAD_URL_TTL_SECONDS",
    "local_cache_dir": f"{ENV_PREFIX}LOCAL_CACHE_DIR",
    "postgres_schema": f"{ENV_PREFIX}POSTGRES_SCHEMA",
    "sagemaker_image_uri": f"{ENV_PREFIX}SAGEMAKER_IMAGE_URI",
    "sagemaker_processing_instance_type": f"{ENV_PREFIX}SAGEMAKER_PROCESSING_INSTANCE_TYPE",
    "sagemaker_instance_count": f"{ENV_PREFIX}SAGEMAKER_INSTANCE_COUNT",
    "sagemaker_volume_size_gb": f"{ENV_PREFIX}SAGEMAKER_VOLUME_SIZE_GB",
    "sagemaker_max_runtime_seconds": f"{ENV_PREFIX}SAGEMAKER_MAX_RUNTIME_SECONDS",
    "sagemaker_max_concurrent_jobs": f"{ENV_PREFIX}SAGEMAKER_MAX_CONCURRENT_JOBS",
    "sagemaker_subnet_ids": f"{ENV_PREFIX}SAGEMAKER_SUBNET_IDS",
    "sagemaker_security_group_ids": f"{ENV_PREFIX}SAGEMAKER_SECURITY_GROUP_IDS",
    "sagemaker_job_name_prefix": f"{ENV_PREFIX}SAGEMAKER_JOB_NAME_PREFIX",
    "log_level": f"{ENV_PREFIX}LOG_LEVEL",
    "log_format": f"{ENV_PREFIX}LOG_FORMAT",
    "metrics_backend": f"{ENV_PREFIX}METRICS_BACKEND",
    "client_id": f"{ENV_PREFIX}CLIENT_ID",
    "cors_origins": f"{ENV_PREFIX}CORS_ORIGINS",
}
"""Field name to environment variable. One mapping, so docs, tests and readers agree."""

_REQUIRED_FOR: Final[Mapping[tuple[str, str], tuple[str, ...]]] = {
    ("storage_backend", "s3"): ("s3_bucket", "aws_region"),
    ("job_backend", "sagemaker"): (
        "sagemaker_role_arn",
        "sagemaker_instance_type",
        "sagemaker_image_uri",
        "aws_region",
    ),
    ("metadata_backend", "postgres"): ("postgres_dsn",),
    ("llm_backend", "bedrock"): ("bedrock_model_id", "aws_region"),
}
"""Which fields a non-default backend cannot work without. Checked once, at construction."""


class SettingsError(Exception):
    """A setting is missing or unreadable.

    `code` is one of SETTING_INVALID | SETTING_REQUIRED. `env_var` names the variable to fix, so
    the message can be acted on without reading this module.
    """

    def __init__(self, code: str, message: str, *, env_var: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.env_var = env_var


class Settings(BaseModel):
    """The four backend switches and the connection fields each of them needs.

    Frozen and `extra="forbid"`, like every other model in the codebase: a typo in a field name is
    a loud failure rather than an attribute that silently does nothing.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    storage_backend: StorageBackend = "local"
    job_backend: JobBackend = "thread"
    metadata_backend: MetadataBackend = "sqlite"
    llm_backend: LLMBackend = "fake"
    aws_region: str | None = Field(default=None, description="AWS region every AWS-backed client uses.")

    # --- local storage and configuration (Phase 1 reads these two) -------------------
    data_dir: Path = Field(default=Path(DEFAULT_DATA_DIR), description="Artefact root of the local store.")
    config_dir: Path | None = Field(
        default=None,
        description="Directory the YAML catalog is read from; null when nothing is exported.",
    )

    # --- storage_backend: s3 ---------------------------------------------------------
    s3_bucket: str | None = Field(default=None, description="Bucket artefacts are written to.")
    s3_prefix: str = Field(default="", description="Key prefix inside the bucket; empty means the root.")

    # --- job_backend -----------------------------------------------------------------
    job_max_workers: int = Field(default=2, ge=1, description="Concurrent runs the thread pool allows.")
    sagemaker_role_arn: str | None = Field(default=None, description="Execution role SageMaker jobs assume.")
    sagemaker_instance_type: str | None = Field(default=None, description="Instance type training jobs use.")

    # --- metadata_backend: postgres ---------------------------------------------------
    postgres_dsn: SecretStr | None = Field(default=None, description="Registry connection string.")

    # --- llm_backend: bedrock ----------------------------------------------------------
    bedrock_model_id: str | None = Field(
        default=None, description="Bedrock model id completions are sent to."
    )

    # --- Phase 4a ------------------------------------------------------------------------
    # Added fields only. Nothing above is renamed or given a new meaning, which is what
    # PARALLEL_WORK_PROTOCOL.md section 2 allows a branch to do to a shared model.
    env: DeploymentName = Field(
        default="local",
        description="Which deployment this process is; names the SSM path and the secret.",
    )
    s3_kms_key_id: str | None = Field(
        default=None, description="Customer-managed KMS key for SSE-KMS; an identifier, not a secret."
    )
    download_url_ttl_seconds: int = Field(
        default=900,
        ge=60,
        le=3600,
        description="Lifetime of a pre-signed artefact URL. A policy choice, not a measurement.",
    )
    local_cache_dir: Path | None = Field(
        default=None, description="Where the s3 store mirrors a predictor directory; null means a temp dir."
    )
    postgres_schema: str | None = Field(
        default=None, description="Schema the metadata tables live in; null uses the search path."
    )
    sagemaker_image_uri: str | None = Field(default=None, description="ECR image a SageMaker job runs.")
    sagemaker_processing_instance_type: str | None = Field(
        default=None,
        description="Instance a processing (score) job uses; falls back to sagemaker_instance_type.",
    )
    sagemaker_instance_count: int = Field(default=1, ge=1, description="Instances per job.")
    sagemaker_volume_size_gb: int | None = Field(
        default=None, description="Attached volume; null leaves the service default. A billed quantity."
    )
    sagemaker_max_runtime_seconds: int | None = Field(
        default=None, description="Job time limit; null leaves the service default in place."
    )
    sagemaker_max_concurrent_jobs: int = Field(
        default=2, ge=1, description="Jobs that may run at once before a run waits for compute."
    )
    sagemaker_subnet_ids: tuple[str, ...] = Field(
        default=(), description="Private subnets a job attaches to; empty means no VPC configuration."
    )
    sagemaker_security_group_ids: tuple[str, ...] = Field(
        default=(), description="Security groups a job attaches to."
    )
    sagemaker_job_name_prefix: str = Field(
        default="marketing-ai", description="Prefix every job name carries; also the IAM resource boundary."
    )
    log_level: str = Field(default="INFO", description="Root log level.")
    log_format: LogFormat = Field(default="text", description="`text`, or `json` for CloudWatch.")
    metrics_backend: MetricsBackend = Field(
        default="none", description="`none`, or `emf` to write CloudWatch embedded-metric log lines."
    )
    client_id: str | None = Field(
        default=None, description="Which customer this deployment serves; tags artefacts and metrics."
    )
    cors_origins: tuple[str, ...] = Field(
        default=("*",),
        description="Origins the API answers. The default is DEC-024's; refused on a prod deployment.",
    )

    @field_validator("sagemaker_subnet_ids", "sagemaker_security_group_ids", "cors_origins", mode="before")
    @classmethod
    def _split_list(cls, value: object) -> object:
        """One environment variable fills a tuple field, comma-separated (Phase 4a).

        `from_env` hands every value through as a string, and pydantic will not turn `"a,b"` into a
        two-element tuple on its own. Splitting here rather than in the loader keeps `from_env`
        exactly as Phase 1 left it.
        """
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return value

    @field_validator("s3_prefix")
    @classmethod
    def _clean_prefix(cls, value: str) -> str:
        """A prefix is a relative key fragment: no leading slash, no dot segment, no trailing slash."""
        cleaned = value.strip("/")
        if any(segment in {".", ".."} for segment in cleaned.split("/")):
            raise ValueError("must not contain a '.' or '..' segment")
        return cleaned

    @model_validator(mode="after")
    def _check_backend_requirements(self) -> Self:
        """Refuse a backend whose connection fields are not set, naming the variable to export."""
        for (switch, value), required in _REQUIRED_FOR.items():
            if getattr(self, switch) != value:
                continue
            for name in required:
                if getattr(self, name) is None:
                    raise SettingsError(
                        "SETTING_REQUIRED",
                        f"{ENV_VARS[switch]}={value} needs {ENV_VARS[name]} to be set.",
                        env_var=ENV_VARS[name],
                    )
        return self

    @model_validator(mode="after")
    def _check_phase_4a_coherence(self) -> Self:
        """The two Phase 4a refusals that are not simply "a field is missing".

        A remote job cannot read a local disk, so `sagemaker` without `s3` is a deployment that
        would fail at its first run. And DEC-024 left CORS wide open because the Phase 1 UI is
        opened as a local file - a statement about a laptop, not about an internet-facing load
        balancer - so a production deployment that never mentions `cors_origins` must not inherit
        the laptop's answer by omission (DEC-307).
        """
        if self.job_backend == "sagemaker" and self.storage_backend != "s3":
            raise SettingsError(
                "SETTING_REQUIRED",
                f"{ENV_VARS['job_backend']}=sagemaker needs {ENV_VARS['storage_backend']}=s3: "
                "a remote job cannot read a local disk.",
                env_var=ENV_VARS["storage_backend"],
            )
        if self.env == "prod" and "*" in self.cors_origins:
            raise SettingsError(
                "SETTING_REQUIRED",
                f"{ENV_VARS['env']}=prod must set {ENV_VARS['cors_origins']}; "
                "'*' is the local default and is not a deployment.",
                env_var=ENV_VARS["cors_origins"],
            )
        return self

    @property
    def config_directory(self) -> Path:
        """The configuration directory, falling back to the checkout's `configs/`.

        `config_dir` stays `None` when nothing is exported, so a caller can tell "the environment
        chose this" from "nobody chose anything" - `engine.config.config_root` needs that
        distinction, because its own `DEFAULT_CONFIG_ROOT` is what a test points at an unusable
        directory to prove an explicit root never falls back to the default one.
        """
        return DEFAULT_CONFIG_DIR if self.config_dir is None else self.config_dir

    @property
    def registry_path(self) -> Path:
        """Where the SQLite registry lives; meaningless when `metadata_backend` is not sqlite."""
        return self.data_dir / REGISTRY_FILENAME

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        """Build from `environ` (default `os.environ`); an unset variable keeps the field's default.

        An empty value is treated as unset, so `MARKETING_AI_DATA_DIR=` behaves like exporting
        nothing rather than rooting the store at the current directory.
        """
        source = os.environ if environ is None else environ
        values: dict[str, str] = {}
        for name, variable in ENV_VARS.items():
            raw = source.get(variable, "").strip()
            if raw:
                values[name] = raw
        try:
            built = cls.model_validate(values)
        except ValidationError as exc:
            raise _invalid(exc) from exc
        # An exported configuration directory is resolved, which is what `config_root` did before
        # this module existed. `data_dir` is deliberately left as given: it was relative to the
        # working directory then, and resolving it here would be a behaviour change dressed as a
        # refactor.
        if "config_dir" in values:
            return built.model_copy(update={"config_dir": Path(values["config_dir"]).resolve()})
        return built


def _invalid(exc: ValidationError) -> SettingsError:
    """Turn pydantic's report into one `SettingsError` naming the first variable at fault."""
    first = exc.errors()[0]
    field = str(first["loc"][0]) if first["loc"] else ""
    variable = ENV_VARS.get(field, ENV_PREFIX + field.upper())
    return SettingsError(
        "SETTING_INVALID", f"{variable} is not a valid value: {first['msg']}.", env_var=variable
    )


def settings(environ: Mapping[str, str] | None = None) -> Settings:
    """The settings the process is running under, read from the environment on every call."""
    return Settings.from_env(environ)


# ===========================================================================
# Shared file (PARALLEL_WORK_PROTOCOL.md §4): three branches edit it at once.
# Add code only inside your own block, at its end. Never edit above your
# block, never reorder, never reformat the rest of the file - run `black` on
# what you paste, not on the file, if the formatter would reflow other lines.
# `tests/unit/test_shared_file_markers.py` fails if a block goes missing.
# ===========================================================================

# ---- PHASE-2 (onboarding) — append only below this line ----
# ---- END PHASE-2 ----

# ---- PHASE-3A (generative) — append only below this line ----
# ---- END PHASE-3A ----

# ---- PHASE-4A (aws) — append only below this line ----
# Everything an AWS deployment needs that a laptop does not. Nothing here imports boto3 at module
# scope: a checkout without the `aws` extra must keep importing this module for free, because
# `engine.config` imports it and `engine.config` is the root of the engine's import graph (DEC-306).

SSM_ROOT: Final[str] = "/marketing-ai"
"""Parameter Store path every deployment's parameters hang under."""

SECRET_NAME_TEMPLATE: Final[str] = "marketing-ai/{env}/app"
"""The one Secrets Manager secret a deployment reads; a flat JSON object of strings."""

SETTINGS_SOURCE_ENV_VAR: Final[str] = f"{ENV_PREFIX}SETTINGS_SOURCE"
"""`env` (the default) or `aws`. Deliberately not a `Settings` field: it selects the loader."""

NON_FIELD_ENV_VARS: Final[frozenset[str]] = frozenset(
    {
        SETTINGS_SOURCE_ENV_VAR,
        f"{ENV_PREFIX}JOB_SPEC_KEY",
        f"{ENV_PREFIX}FAILURE_PATH",
        f"{ENV_PREFIX}REQUIRE_POSTGRES",
        f"{ENV_PREFIX}TEST_DATABASE_URL",
    }
)
"""`MARKETING_AI_*` variables that are deliberately not settings.

`Settings.from_env` ignores a variable it does not recognise, so nothing refuses these at load
time. The list still has to exist, because the deployment does refuse them: `infra/compute.py`
may only put a `MARKETING_AI_*` variable on the container if it is in `ENV_VARS` or here, and
`tests/infra/test_task_definition.py` fails the build otherwise. That makes a typo in a CDK file a
red test rather than a setting that silently had no effect (DEC-304).

A name here is a name somebody decided does not describe a deployment - the job spec key the
container is handed, the failure file SageMaker reads, and the two variables the Postgres test
fixtures use.
"""

REDACTED: Final[str] = "<redacted>"

SECRET_FIELDS: Final[frozenset[str]] = frozenset({"postgres_dsn"})
"""Fields `redacted()` hides and `summary()` may never name."""

SUMMARY_FIELDS: Final[tuple[str, ...]] = (
    "env",
    "aws_region",
    "storage_backend",
    "metadata_backend",
    "job_backend",
    "llm_backend",
    "log_format",
    "metrics_backend",
)
"""The allow-list `summary()` renders.

An allow-list and not a filter: a field added later is absent from the startup line until somebody
deliberately adds it here, so forgetting costs a missing word rather than a leaked credential
(DEC-303).
"""


@runtime_checkable
class ParameterSource(Protocol):
    """Somewhere `settings_from_aws` reads from; `engine.aws.secrets` is the only implementation."""

    def parameters(self, prefix: str) -> Mapping[str, str]: ...

    def secret(self, name: str) -> Mapping[str, str]: ...


def ssm_prefix(env: str) -> str:
    """The Parameter Store path for `env`, with the trailing slash `GetParametersByPath` expects."""
    return f"{SSM_ROOT}/{env}/"


def secret_name(env: str) -> str:
    """The Secrets Manager secret name for `env`."""
    return SECRET_NAME_TEMPLATE.format(env=env)


def _fields_from(raw: Mapping[str, str]) -> dict[str, str]:
    """Parameter or secret keys mapped onto field names.

    A key is accepted as the bare field name (`s3_bucket`) or as its environment spelling
    (`MARKETING_AI_S3_BUCKET`), so one naming convention in Parameter Store serves both. A key
    matching neither is ignored: Parameter Store is a shared namespace, and a later phase will put
    things under it that this version knows nothing about.
    """
    by_variable = {variable: field for field, variable in ENV_VARS.items()}
    out: dict[str, str] = {}
    for key, value in raw.items():
        leaf = key.rsplit("/", 1)[-1]
        field = leaf if leaf in ENV_VARS else by_variable.get(leaf.upper())
        if field is not None and value.strip():
            out[field] = value.strip()
    return out


def settings_from_aws(
    *,
    source: ParameterSource,
    env: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    """Read a deployment from SSM Parameter Store and Secrets Manager, with the environment winning.

    Precedence, lowest first: SSM parameters, the Secrets Manager document, then the process
    environment (DEC-301). The environment beating AWS is the deliberate part: it is what lets an
    operator override one value on a task without editing a parameter, and what lets a test point a
    deployed shape at a local directory.
    """
    process_env = os.environ if environ is None else environ
    name = env or process_env.get(ENV_VARS["env"], "").strip() or "local"
    values: dict[str, str] = {"env": name}
    values.update(_fields_from(source.parameters(ssm_prefix(name))))
    values.update(_fields_from(source.secret(secret_name(name))))
    for field, variable in ENV_VARS.items():
        raw = process_env.get(variable, "").strip()
        if raw:
            values[field] = raw
    try:
        return Settings.model_validate(values)
    except ValidationError as exc:
        raise _invalid(exc) from exc


def load_settings(
    environ: Mapping[str, str] | None = None, *, source: ParameterSource | None = None
) -> Settings:
    """`settings()`, or the AWS loader when `MARKETING_AI_SETTINGS_SOURCE=aws`.

    The AWS branch imports `engine.aws.secrets` lazily, so a checkout without boto3 never pays for
    the import and never fails on it.
    """
    process_env = os.environ if environ is None else environ
    chosen = process_env.get(SETTINGS_SOURCE_ENV_VAR, "env").strip().lower()
    if chosen not in {"env", "aws"}:
        raise SettingsError(
            "SETTING_INVALID",
            f"{SETTINGS_SOURCE_ENV_VAR} must be 'env' or 'aws'.",
            env_var=SETTINGS_SOURCE_ENV_VAR,
        )
    if chosen == "env":
        return Settings.from_env(process_env)
    if source is None:
        # A deliberate local import: boto3 is an optional dependency (DEC-306).
        from engine.aws.secrets import AwsParameterSource

        region = process_env.get(ENV_VARS["aws_region"], "").strip() or None
        source = AwsParameterSource(region=region)
    return settings_from_aws(source=source, environ=process_env)


def redacted(config: Settings) -> dict[str, Any]:
    """Every field, with every secret replaced by `REDACTED`. Safe to log or print."""
    out: dict[str, Any] = {}
    for name in type(config).model_fields:
        value = getattr(config, name)
        out[name] = REDACTED if name in SECRET_FIELDS else _plain(value)
    return out


def summary(config: Settings) -> str:
    """One line naming the backends, for the startup log. Never renders a secret."""
    return " ".join(f"{name}={_plain(getattr(config, name))}" for name in SUMMARY_FIELDS)


def _plain(value: Any) -> Any:
    """A field rendered for a log line: paths and tuples as text, a secret never reached."""
    if isinstance(value, SecretStr):  # pragma: no cover - SECRET_FIELDS catches these first
        return REDACTED
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return ",".join(str(item) for item in value)
    return value


def build_storage(config: Settings) -> Any:
    """The `Storage` this deployment describes.

    `api/deps.py` is the only place the *API* builds one, and it calls this; a SageMaker container
    has no `api/deps.py` at all and calls this too. One function, so the API and a job can never
    disagree about where the artefacts are (DEC-308).
    """
    # Deliberate local imports: `engine.storage` imports this module, so importing it at module
    # scope would be a cycle, and `engine.aws.s3_storage` needs boto3.
    from engine.storage import LocalStorage

    if config.storage_backend == "local":
        return LocalStorage(config.data_dir)
    from engine.aws.s3_storage import S3Storage

    return S3Storage(
        str(config.s3_bucket),
        prefix=config.s3_prefix,
        region_name=config.aws_region,
        kms_key_id=config.s3_kms_key_id,
        client_id=config.client_id,
        workspace=config.local_cache_dir,
    )


def build_registry(config: Settings, storage: Any) -> Any:
    """The `ModelRegistry` this deployment describes, over `storage` when it keeps files."""
    if config.metadata_backend == "sqlite":
        from engine.registry import LocalModelRegistry

        return LocalModelRegistry(config.registry_path)
    from engine.aws.postgres import postgres_store
    from engine.aws.s3_registry import S3ModelRegistry

    return S3ModelRegistry(postgres_store(config), storage)


def build_services(config: Settings) -> tuple[Any, Any]:
    """`(storage, registry)` for this deployment, built in the one order that works."""
    store = build_storage(config)
    return store, build_registry(config, store)


# ---- END PHASE-4A ----

# ---- PHASE-3B (uplift) — append only below this line ----
# ---- END PHASE-3B ----
