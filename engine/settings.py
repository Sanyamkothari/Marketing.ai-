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
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator

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
}
"""Field name to environment variable. One mapping, so docs, tests and readers agree."""

_REQUIRED_FOR: Final[Mapping[tuple[str, str], tuple[str, ...]]] = {
    ("storage_backend", "s3"): ("s3_bucket", "aws_region"),
    ("job_backend", "sagemaker"): ("sagemaker_role_arn", "sagemaker_instance_type", "aws_region"),
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
# ---- END PHASE-4A ----
