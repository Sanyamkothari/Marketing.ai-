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

import hashlib
import os
import secrets
import threading
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from engine.config import ConfigError, config_root, list_use_case_ids, load_yaml
from engine.platform_db import PLATFORM_SETTING_TABLE, PlatformSettingRow, create_tables
from engine.settings import ENV_VARS, Settings, SettingsError
from engine.utils.logging import get_logger
from engine.utils.time import utc_now

__all__ = [
    "MIN_SALT_LENGTH",
    "PRIVACY_FILENAME",
    "SALT_FILENAME",
    "ErasureMode",
    "ErasurePolicy",
    "LifecyclePolicy",
    "PrivacyConfig",
    "Purpose",
    "RetentionPolicy",
    "check_privacy_salt",
    "load_privacy_config",
    "privacy_config_or_none",
    "privacy_salt",
    "salt_fingerprint",
]

PRIVACY_FILENAME: Final[str] = "privacy.yaml"
SCHEMA_VERSION: Final[int] = 1

SALT_FILENAME: Final[str] = "privacy_salt"
"""The generated salt of a deployment whose platform database is a local file (DEC-860)."""

MIN_SALT_LENGTH: Final[int] = 16
"""A salt shorter than this is refused: it would be guessable, which is what R3 exists to prevent."""

SALT_FINGERPRINT_KEY: Final[str] = "privacy_salt_fingerprint"
"""The `platform_setting` key the salt's fingerprint is kept under (DEC-883)."""

_FINGERPRINT_DOMAIN: Final[bytes] = b"marketing-ai/privacy-salt-fingerprint\x00"

_HASHED_TABLES: Final[tuple[str, ...]] = ("consent_record", "erasure_request")
"""Tables holding principal hashes: rows in them before a fingerprint exists were made with the old salt."""

_LOGGER = get_logger(__name__)

_CACHE: dict[Path, PrivacyConfig] = {}
_VERIFIED: dict[Engine, str] = {}
"""Engine -> the fingerprint already checked against it in this process, so the check is one query."""
_VERIFY_LOCK: Final[threading.Lock] = threading.Lock()


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


def privacy_salt(settings: Settings, *, engine: Engine | None = None) -> str:
    """The per-deployment secret salt every principal hash in M48 uses (ruling R3, DEC-860).

    The same salt for the consent ledger, the erasure register and the audit log, so an operator
    holding a customer id can find all three with one hash, while the same id at two clients (two
    deployments, two salts) never produces a joinable value (DEC-705, DEC-733).

    It is a **secret**, read through `Settings.privacy_salt` (`MARKETING_AI_PRIVACY_SALT`, from the
    application secret on a deployment), and there is **no default in code**: the client id it used
    to be is written on every artefact, so anyone holding a hash and a customer list could test ids
    against it. Without the setting:

    * on `env=prod`, `SettingsError` - the API refuses to start (`check_privacy_salt`);
    * where the platform database is a local SQLite file (a laptop, a test), a random salt is
      generated once and kept beside that file as `privacy_salt` (0600), so hashes stay stable across
      restarts and the salt travels with the ledger it protects - `engine` names that database;
    * anywhere else (Postgres on `dev`/`staging`), `SettingsError`: there is no local file to keep a
      generated salt in, and a salt that changed with every container would orphan every hash.

    **A changed salt is refused** (DEC-883). With an `engine`, the salt's fingerprint - a domain-
    separated SHA-256 of it (`salt_fingerprint`), never the salt - is kept in that database's
    `platform_setting` table the first time, and compared every time after: a different salt raises
    `SettingsError` `SETTING_INVALID` naming `MARKETING_AI_PRIVACY_SALT`, because every consent record
    and erasure request already stored was hashed with the other one and would silently stop matching.
    When no fingerprint is kept yet but those tables already hold rows (a Phase 4b database, whose
    hashes were salted with the client id), a WARNING says they must be re-imported, and the new
    fingerprint is recorded.
    """
    salt = _configured_salt(settings, engine)
    if engine is not None:
        _check_fingerprint(engine, salt)
    return salt


def salt_fingerprint(salt: str) -> str:
    """What `platform_setting` keeps to recognise the salt: a domain-separated SHA-256, never the salt."""
    return hashlib.sha256(_FINGERPRINT_DOMAIN + salt.encode("utf-8")).hexdigest()


def _configured_salt(settings: Settings, engine: Engine | None) -> str:
    if settings.privacy_salt is not None:
        return settings.privacy_salt.get_secret_value().strip()
    if settings.env == "prod":
        raise _salt_required(f"{ENV_VARS['privacy_salt']} must be set on env=prod")
    location = _local_salt_path(engine)
    if location is None:
        raise _salt_required(
            f"{ENV_VARS['privacy_salt']} must be set: the platform database is not a local file, "
            "so there is nowhere to keep a generated salt"
        )
    return _read_or_create_salt(location)


def _check_fingerprint(engine: Engine, salt: str) -> None:
    """Record the salt's fingerprint in `engine`'s database, or refuse a salt that is not the recorded one.

    On SQLite the table is created when missing (it is this code's file). On Postgres Alembic owns the
    schema (`0005_plan_d`); a database not migrated that far has nowhere to keep it and is not checked.
    """
    fingerprint = salt_fingerprint(salt)
    with _VERIFY_LOCK:
        if _VERIFIED.get(engine) == fingerprint:
            return
    create_tables(engine, (PLATFORM_SETTING_TABLE,))
    inspector = inspect(engine)
    if not inspector.has_table(PLATFORM_SETTING_TABLE):
        return
    with Session(engine) as session:
        row = session.get(PlatformSettingRow, SALT_FINGERPRINT_KEY)
        if row is None:
            if _holds_hashes(engine, [name for name in _HASHED_TABLES if inspector.has_table(name)]):
                _LOGGER.warning(
                    "privacy: the platform database holds consent or erasure records hashed before a "
                    "salt fingerprint was kept; they were made with the old salt (the client id) and "
                    "must be re-imported under %s",
                    ENV_VARS["privacy_salt"],
                )
            session.add(PlatformSettingRow(key=SALT_FINGERPRINT_KEY, value=fingerprint, updated_at=utc_now()))
            try:
                session.commit()
                stored = fingerprint
            except IntegrityError:  # another thread recorded one first: compare against it
                session.rollback()
                recorded = session.get(PlatformSettingRow, SALT_FINGERPRINT_KEY)
                stored = fingerprint if recorded is None else recorded.value
        else:
            stored = row.value
    if stored != fingerprint:
        raise SettingsError(
            "SETTING_INVALID",
            f"{ENV_VARS['privacy_salt']} is not the salt this platform database's hashes were made with: "
            "the stored consent records and erasure requests were hashed with a different salt and "
            "would no longer match anyone. Set the original salt again.",
            env_var=ENV_VARS["privacy_salt"],
        )
    with _VERIFY_LOCK:
        _VERIFIED[engine] = fingerprint


def _holds_hashes(engine: Engine, tables: list[str]) -> bool:
    with engine.connect() as connection:
        return any(
            connection.execute(text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None for table in tables
        )


def check_privacy_salt(settings: Settings) -> None:
    """Refuse a production deployment that has no privacy salt (R3). Called when the API starts."""
    if settings.env == "prod" and settings.privacy_salt is None:
        raise _salt_required(
            f"{ENV_VARS['privacy_salt']} must be set on env=prod; the API does not start without it"
        )


def _salt_required(message: str) -> SettingsError:
    return SettingsError("SETTING_REQUIRED", f"{message}.", env_var=ENV_VARS["privacy_salt"])


def _local_salt_path(engine: Engine | None) -> Path | None:
    """`privacy_salt` beside the SQLite file `engine` opens, or None for anything else."""
    if engine is None or engine.dialect.name != "sqlite":
        return None
    database = engine.url.database
    if not database or database == ":memory:":
        return None
    return Path(database).resolve().parent / SALT_FILENAME


def _read_or_create_salt(path: Path) -> str:
    """The salt in `path`, creating it with 32 random bytes (0600) the first time; a race keeps the first."""
    try:
        return _read_salt(path)
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_urlsafe(32)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _read_salt(path)
    with os.fdopen(descriptor, "w", encoding="ascii") as handle:
        handle.write(value)
    _LOGGER.warning(
        "privacy: no %s set; generated a local salt beside the platform database", ENV_VARS["privacy_salt"]
    )
    return value


def _read_salt(path: Path) -> str:
    value = path.read_text(encoding="ascii").strip()
    if len(value) < MIN_SALT_LENGTH:
        raise SettingsError(
            "SETTING_INVALID",
            f"The generated privacy salt beside the platform database is damaged; set {ENV_VARS['privacy_salt']}.",
            env_var=ENV_VARS["privacy_salt"],
        )
    return value
