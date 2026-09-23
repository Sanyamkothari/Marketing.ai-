"""`configs/privacy.yaml`: the purposes, the use-case-to-purpose table, retention and erasure policy.

One file, engine data like `configs/roles.yaml`, never merged into a use case. It exists because
three decisions in M48 are *policy* rather than code, and policy belongs where a reviewer (and
Minfy's legal team) can read and change it without reading Python (DEC-730):

* **Which purpose a use case processes data for.** The consent ledger is keyed by purpose
  ("marketing communication"), a scoring run by use case. Something has to join the two, and the
  engine may not name a use case (plan section 2.1, principle 1, enforced by
  `tests/unit/test_no_use_case_branching.py`), so the join is a YAML table. A use case missing from
  it has no consent purpose and scores exactly as Phase 1 did.
* **What counts as a row-level artefact.** Retention deletes row-level run artefacts and keeps
  aggregate reports; which files are which is a list here, so a later artefact is classified by
  adding a line rather than by editing the job.
* **Delete or tombstone.** Whether an erased customer's rows disappear or are replaced by a marker
  is a choice a client's counsel may want to make differently.

`load_privacy_config` validates the file with a frozen pydantic model and checks, against the same
config root, that every use case it names really exists - a typo would otherwise silently leave a
use case ungated. The result is cached per root, like `load_engine_config`.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from engine.config import ConfigError, config_root, list_use_case_ids, load_yaml
from engine.settings import Settings

__all__ = [
    "DEFAULT_SALT",
    "PRIVACY_FILENAME",
    "ErasureMode",
    "ErasurePolicy",
    "LifecyclePolicy",
    "PrivacyConfig",
    "Purpose",
    "RetentionPolicy",
    "load_privacy_config",
    "privacy_config_or_none",
    "privacy_salt",
]

PRIVACY_FILENAME: Final[str] = "privacy.yaml"
SCHEMA_VERSION: Final[int] = 1

DEFAULT_SALT: Final[str] = "local"
"""The principal-hash salt of a deployment that names no client (a laptop)."""

_CACHE: dict[Path, PrivacyConfig] = {}


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class Purpose(_Model):
    """One purpose a data principal may consent to."""

    label: Annotated[str, Field(min_length=1)] = Field(description="How the purpose is named to a person.")
    description: str = Field(default="", description="One sentence on what processing it covers.")


class LifecyclePolicy(_Model):
    """How the S3 lifecycle backstop is built (`engine.privacy.lifecycle`)."""

    rule_id_prefix: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")] = Field(
        default="marketing-ai-retention-",
        description="Every lifecycle rule this code owns has an ID starting with this; others are preserved.",
    )
    grace_days: Annotated[int, Field(ge=1, le=365)] = Field(
        default=7,
        description="Days after the retention job's own deadline that the backstop expires an object.",
    )


class RetentionPolicy(_Model):
    """What the retention job treats as row-level, and which samples it empties in kept reports."""

    row_level_run_artefacts: tuple[str, ...] = Field(
        description="Run-directory filenames holding one row per customer; deleted when a run expires."
    )
    sample_fields: dict[str, tuple[str, ...]] = Field(
        default_factory=dict,
        description="Run-directory filename -> dotted field paths (`*` = every list element) to empty.",
    )
    lifecycle: LifecyclePolicy = Field(default_factory=LifecyclePolicy)


class ErasureMode(StrEnum):
    """What erasure does to a row that belongs to the erased principal (DEC-742)."""

    DELETE = "delete"
    TOMBSTONE = "tombstone"


class ErasurePolicy(_Model):
    """How an erasure request rewrites the stores."""

    mode: ErasureMode = Field(default=ErasureMode.DELETE, description="`delete` or `tombstone`.")
    tombstone: Annotated[str, Field(min_length=1, max_length=64)] = Field(
        default="[erased]", description="What a tombstoned identifier or a masked cell becomes."
    )
    consent_history: Literal["keep", "delete"] = Field(
        default="keep", description="Whether the principal's hashed consent-ledger rows survive erasure."
    )


class PrivacyConfig(_Model):
    """The whole of `configs/privacy.yaml`."""

    schema_version: Literal[1] = Field(description="File format version; this engine reads 1.")
    purposes: dict[str, Purpose] = Field(description="Purpose id -> purpose.")
    use_case_purposes: dict[str, str] = Field(
        default_factory=dict, description="Use-case id -> purpose id; unlisted use cases are ungated."
    )
    retention: RetentionPolicy
    erasure: ErasurePolicy = Field(default_factory=ErasurePolicy)

    @model_validator(mode="after")
    def _purposes_are_declared(self) -> PrivacyConfig:
        unknown = sorted(
            {purpose for purpose in self.use_case_purposes.values() if purpose not in self.purposes}
        )
        if unknown:
            raise ValueError(f"use_case_purposes names undeclared purposes: {', '.join(unknown)}")
        return self

    def purpose_for(self, use_case_id: str) -> str | None:
        """The purpose `use_case_id` processes personal data for, or None when it has none."""
        return self.use_case_purposes.get(use_case_id)

    def is_purpose(self, purpose: str) -> bool:
        """Whether `purpose` is one this file declares."""
        return purpose in self.purposes


def load_privacy_config(root: Path | None = None) -> PrivacyConfig:
    """`configs/privacy.yaml` of `root`, validated and cached; `ConfigError` when missing or wrong.

    Codes: `CONFIG_NOT_FOUND` / `CONFIG_YAML_ERROR` / `CONFIG_NOT_A_MAPPING` (from `load_yaml`),
    `PRIVACY_SCHEMA_VERSION`, `CONFIG_INVALID`, and `PRIVACY_USE_CASE_UNKNOWN` when the purpose table
    names a use case the same root does not define.
    """
    base = config_root(root)
    cached = _CACHE.get(base)
    if cached is not None:
        return cached
    document = load_yaml(base / PRIVACY_FILENAME)
    if document.get("schema_version") != SCHEMA_VERSION:
        raise ConfigError(
            "PRIVACY_SCHEMA_VERSION",
            f"privacy.yaml declares schema_version {document.get('schema_version')!r}; "
            f"this engine reads {SCHEMA_VERSION}.",
            path="schema_version",
        )
    try:
        loaded = PrivacyConfig.model_validate(document)
    except ValidationError as exc:
        first = exc.errors()[0]
        dotted = ".".join(str(part) for part in first["loc"])
        raise ConfigError("CONFIG_INVALID", f"{dotted}: {first['msg']}.", path=dotted) from exc
    known = set(list_use_case_ids(base))
    unknown = sorted(set(loaded.use_case_purposes) - known)
    if unknown:
        raise ConfigError(
            "PRIVACY_USE_CASE_UNKNOWN",
            f"privacy.yaml maps use cases this configuration does not define: {', '.join(unknown)}.",
            path="use_case_purposes",
        )
    _CACHE[base] = loaded
    return loaded


def privacy_config_or_none(root: Path | None = None) -> PrivacyConfig | None:
    """The privacy configuration, or None when the config root has no `privacy.yaml` at all.

    Only a *missing* file means "no privacy controls here" (a test's minimal config root, a
    deployment that predates M48); a file that exists and is wrong still raises, because silently
    ungating every use case over a typo is the failure this whole module exists to prevent.
    """
    if not (config_root(root) / PRIVACY_FILENAME).is_file():
        return None
    return load_privacy_config(root)


def privacy_salt(settings: Settings) -> str:
    """The per-deployment salt every principal hash in M48 uses: the client id, else `local`.

    The same salt for the consent ledger, the erasure register and the audit log, so an operator
    holding a customer id can find all three with one hash, while the same id at two clients (two
    deployments) never produces a joinable value (DEC-705, DEC-733).
    """
    return settings.client_id or DEFAULT_SALT
