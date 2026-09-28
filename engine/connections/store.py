"""Saved connections: one JSON document each, secrets encrypted with Fernet (Plan H M80, DEC-1101).

`connections/<connection_id>.json` in the artefact store holds a connection's kind, name, non-secret
settings, the names of the secrets it has and its last test. The secrets themselves are one Fernet
token (AES-128-CBC with an HMAC-SHA256, from `cryptography`) of a small JSON object - never
plaintext on disk, never in a listing, never in a response, never in a log line.

**The key.** `MARKETING_AI_CONNECTIONS_KEY` (a Fernet key: 32 url-safe base64 bytes), read through
`engine.settings`. Without it:

* on `env=prod`, saving a secret is refused (`SettingsError` naming the variable);
* where the store is the local filesystem (a laptop, a test), a key is generated once and kept at
  `<data dir>/connections_key` with 0600 permissions, beside the connections it protects;
* anywhere else (S3 storage on dev or staging), refused as well: there is no local file to keep a
  generated key in, and a key that changed with every container would lock every saved secret.

**A wrong key is a clear error, not garbage.** Each connection records the key's *fingerprint* (a
domain-separated SHA-256, never the key), so a changed key is recognised before decryption is even
tried and the message says what to do: set the old key back, or enter the passwords again.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets as random
import threading
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, ConfigDict, Field

from engine.connections.base import ConfigValue, ConnectorError, StepStatus, TestReport
from engine.settings import ENV_VARS, Settings, SettingsError
from engine.storage import LocalStorage, Storage, StorageError
from engine.utils.time import utc_now

__all__ = [
    "KEY_FILENAME",
    "ConnectionRecord",
    "ConnectionStatus",
    "ConnectionStore",
    "connections_key",
    "key_fingerprint",
]

_LOGGER = logging.getLogger(__name__)

PREFIX: Final[str] = "connections/"
KEY_FILENAME: Final[str] = "connections_key"
_ID: Final[re.Pattern[str]] = re.compile(r"^c_[0-9a-f]{12}$")
_FINGERPRINT_DOMAIN: Final[bytes] = b"marketing-ai/connections-key/v1\x00"
_KEY_LOCK: Final[threading.Lock] = threading.Lock()
_WRITE_LOCK: Final[threading.Lock] = threading.Lock()

ConnectionStatus = Literal["connected", "failed", "not_tested", "needs_addon"]
"""What the card's badge says."""


class ConnectionRecord(BaseModel):
    """One saved connection, as stored. `secrets_token` is the only place its secrets exist."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    connection_id: str
    kind: str
    name: str
    config: dict[str, ConfigValue] = Field(default_factory=dict)
    secret_names: tuple[str, ...] = ()
    secrets_token: str | None = None
    key_fingerprint: str | None = None
    last_test: TestReport | None = None
    created_at: datetime
    updated_at: datetime

    def status(self, available: bool) -> ConnectionStatus:
        if not available:
            return "needs_addon"
        if self.last_test is None:
            return "not_tested"
        return "connected" if self.last_test.ok else "failed"

    def failure(self) -> str | None:
        """The first failed step's message, for the badge "Failed: <reason>"."""
        if self.last_test is None:
            return None
        failed = [s for s in self.last_test.steps if s.status is StepStatus.FAILED]
        return failed[0].message if failed else None


def key_fingerprint(key: bytes) -> str:
    return hashlib.sha256(_FINGERPRINT_DOMAIN + key).hexdigest()[:16]


def connections_key(settings: Settings, storage: Storage) -> bytes:
    """The Fernet key secrets are encrypted with; see the module docstring for where it comes from."""
    if settings.connections_key is not None:
        key = settings.connections_key.get_secret_value().strip().encode("ascii", errors="replace")
        try:
            Fernet(key)
        except (ValueError, TypeError):
            raise SettingsError(
                "SETTING_INVALID",
                f"{ENV_VARS['connections_key']} is not a valid key: it must be 32 url-safe base64-encoded "
                'bytes, as `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` prints.',
                env_var=ENV_VARS["connections_key"],
            ) from None
        return key
    if settings.env == "prod":
        raise _key_required(
            f"{ENV_VARS['connections_key']} must be set on env=prod to save a connection's secrets"
        )
    if not isinstance(storage, LocalStorage):
        raise _key_required(
            f"{ENV_VARS['connections_key']} must be set: the artefact store is not a local folder, so there "
            "is nowhere safe to keep a generated key"
        )
    return _read_or_create_key(storage.root / KEY_FILENAME)


def _read_or_create_key(path: Path) -> bytes:
    with _KEY_LOCK:
        if path.is_file():
            return path.read_bytes().strip()
        key = Fernet.generate_key()
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(key)
        _LOGGER.warning(
            "connections: no %s set; generated a key for this computer at %s (0600). Back it up with the data "
            "folder: without it saved passwords cannot be read. Set the variable on a deployment.",
            ENV_VARS["connections_key"],
            path,
        )
        return key


def _key_required(message: str) -> SettingsError:
    return SettingsError("SETTING_REQUIRED", message + ".", env_var=ENV_VARS["connections_key"])


class ConnectionStore:
    """Saved connections in an artefact store. `key` is resolved lazily: listing never needs it."""

    def __init__(self, storage: Storage, settings: Settings) -> None:
        self._storage = storage
        self._settings = settings

    # --- reading ------------------------------------------------------------------------------------
    def list(self) -> list[ConnectionRecord]:
        records = []
        for key in self._storage.list_keys(PREFIX):
            if not key.endswith(".json"):
                continue
            try:
                records.append(self._storage.read_model(key, ConnectionRecord))
            except (StorageError, ValueError):
                _LOGGER.warning("connections: skipped an unreadable connection file %s", key)
        return sorted(records, key=lambda r: r.created_at)

    def get(self, connection_id: str) -> ConnectionRecord:
        if not _ID.match(connection_id):
            raise _not_found()
        try:
            return self._storage.read_model(self._key(connection_id), ConnectionRecord)
        except StorageError:
            raise _not_found() from None

    def secrets(self, record: ConnectionRecord) -> dict[str, str]:
        """The connection's secrets, decrypted. Only a connector ever receives them."""
        if record.secrets_token is None:
            return {}
        key = connections_key(self._settings, self._storage)
        if record.key_fingerprint is not None and record.key_fingerprint != key_fingerprint(key):
            raise _wrong_key()
        try:
            data = json.loads(Fernet(key).decrypt(record.secrets_token.encode("ascii")))
        except (InvalidToken, ValueError):
            raise _wrong_key() from None
        return {str(k): str(v) for k, v in data.items()}

    # --- writing ------------------------------------------------------------------------------------
    def create(
        self, *, kind: str, name: str, config: Mapping[str, ConfigValue], secrets: Mapping[str, str]
    ) -> ConnectionRecord:
        now = utc_now()
        record = ConnectionRecord(
            connection_id=f"c_{random.token_hex(6)}",
            kind=kind,
            name=name,
            config=dict(config),
            created_at=now,
            updated_at=now,
            **self._sealed(dict(secrets)),
        )
        self._write(record)
        return record

    def update(
        self,
        record: ConnectionRecord,
        *,
        name: str,
        config: Mapping[str, ConfigValue],
        secrets: Mapping[str, str],
        drop_secrets: frozenset[str] = frozenset(),
    ) -> ConnectionRecord:
        """New settings; a secret not given keeps its saved value. The last test is forgotten.

        Saved secrets that can no longer be read (the key changed) are dropped rather than kept: the
        caller asks `readable()` first and requires them again (the "enter it again" fix).
        """
        sealed: dict[str, Any] = {
            "secret_names": record.secret_names,
            "secrets_token": record.secrets_token,
            "key_fingerprint": record.key_fingerprint,
        }
        if secrets or drop_secrets:
            current = self.secrets(record) if self.readable(record) else {}
            merged = {k: v for k, v in current.items() if k not in drop_secrets}
            merged.update(secrets)
            sealed = self._sealed(merged)
        updated = record.model_copy(
            update={
                "name": name,
                "config": dict(config),
                "last_test": None,
                "updated_at": utc_now(),
                **sealed,
            }
        )
        self._write(updated)
        return updated

    def readable(self, record: ConnectionRecord) -> bool:
        """Whether the saved secrets can be decrypted with the key in force (True when there are none)."""
        if record.secrets_token is None:
            return True
        try:
            self.secrets(record)
        except (ConnectorError, SettingsError):
            return False
        return True

    def record_test(self, record: ConnectionRecord, result: TestReport) -> ConnectionRecord:
        updated = record.model_copy(update={"last_test": result})
        self._write(updated)
        return updated

    def delete(self, record: ConnectionRecord) -> None:
        self._storage.delete(self._key(record.connection_id))

    # --- helpers ------------------------------------------------------------------------------------
    def _sealed(self, secrets: dict[str, str]) -> dict[str, Any]:
        if not secrets:
            return {"secret_names": (), "secrets_token": None, "key_fingerprint": None}
        key = connections_key(self._settings, self._storage)
        token = Fernet(key).encrypt(json.dumps(secrets, sort_keys=True).encode("utf-8")).decode("ascii")
        return {
            "secret_names": tuple(sorted(secrets)),
            "secrets_token": token,
            "key_fingerprint": key_fingerprint(key),
        }

    def _write(self, record: ConnectionRecord) -> None:
        with _WRITE_LOCK:
            self._storage.write_model(self._key(record.connection_id), record)

    @staticmethod
    def _key(connection_id: str) -> str:
        return f"{PREFIX}{connection_id}.json"


def _not_found() -> ConnectorError:
    return ConnectorError(
        "CONNECTION_NOT_FOUND", "There is no such connection.", "Open Connections and pick one.", status=404
    )


def _wrong_key() -> ConnectorError:
    return ConnectorError(
        "CONNECTION_SECRETS_UNREADABLE",
        "The saved password or key of this connection cannot be read: it was saved with a different "
        f"encryption key ({ENV_VARS['connections_key']}).",
        "Set the encryption key back to the one used when the connection was saved, or edit the "
        "connection and enter its password or key again.",
        status=409,
    )
