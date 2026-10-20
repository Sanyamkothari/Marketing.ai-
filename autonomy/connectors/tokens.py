"""Tokens: kept encrypted, refreshed before they expire by exactly one worker, kept alive when idle.

**Where tokens live.** `SqlTokenVault` keeps each connection's `TokenSet` as one Fernet token in
`connector_secret`, encrypted with the key of v1's credential store
(`engine.connections.store.connections_key`: `MARKETING_AI_CONNECTIONS_KEY`, or the generated
local key) and stamped with that key's fingerprint (`key_fingerprint`), so a changed key is a clear
error ("set the old key back or reconnect"), never garbage. The token itself is never in a log, a
response, a repr or an error message. Why SQL rather than v1's JSON files: an hourly refresh that
rotates the refresh token (Shopify expiring offline tokens) needs a transactional write next to
the lease that serialises it, and SaaS tokens must not appear on v1's Connections page.

**Refresh.** `TokenManager.access_token(connection, now)` returns a token valid for at least the
provider's `refresh_margin` (5 minutes by default), refreshing first if needed. Refreshing takes
the lease `token:<connection_id>`; a worker that finds it taken uses the current token if it has
not expired yet, or asks to be retried in a few seconds. The lease holder re-reads the vault
before calling the platform, so when two workers raced, the second sees the first's new token and
does not spend (and, with rotating refresh tokens, invalidate) another refresh.

**Keep-alive.** Klaviyo revokes a refresh token unused for 90 days; Shopify's expiring offline
refresh tokens last 90 days. `keep_alive` refreshes any connection whose last refresh is older
than the provider's `keep_alive_every` (30 days for both), including paused ones, from the daily
maintenance job.

**Revoked.** A refresh refused as revoked (`NeedsReconnect`) calls `on_revoked` once with the
connection, which marks it `needs_reconnect` and raises the single "reconnect" alert. The manager
never loops on a dead refresh token.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from autonomy.connectors.base import Connection, ConnectorFailure, NeedsReconnect, TransientFailure

if TYPE_CHECKING:
    from autonomy.connectors.oauth import OAuthClient
    from autonomy.connectors.store import ConnectorStore

__all__ = [
    "FernetCipher",
    "MemoryTokenVault",
    "SecretCipher",
    "SqlTokenVault",
    "TokenManager",
    "TokenSet",
    "TokenVault",
    "VaultUnreadable",
]

LEASE_TTL = timedelta(minutes=5)
"""Longer than the worst-case token call (the engine's token client: 3 attempts of at most 20 s plus
backoff), so the lease cannot expire mid-refresh and let a second worker spend the same rotating
refresh token."""
SAVE_ATTEMPTS = 3
BUSY_RETRY = timedelta(seconds=5)


class TokenSet(BaseModel):
    """What a platform gave us at connect or refresh. The tokens never appear in a repr."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    access_token: str = Field(repr=False)
    refresh_token: str | None = Field(default=None, repr=False)
    token_type: str = "bearer"
    scope: str | None = None
    expires_at: datetime | None = Field(default=None, description="None: does not expire (an API key)")
    refresh_expires_at: datetime | None = None
    obtained_at: datetime
    refreshed_at: datetime | None = None
    extra: dict[str, str] = Field(default_factory=dict)

    def expired(self, now: datetime) -> bool:
        return self.expires_at is not None and self.expires_at <= now

    def expires_within(self, now: datetime, margin: timedelta) -> bool:
        return self.expires_at is not None and self.expires_at - margin <= now

    def last_renewed(self) -> datetime:
        return self.refreshed_at or self.obtained_at

    @classmethod
    def api_key(cls, key: str, now: datetime) -> TokenSet:
        """A non-expiring private key (Klaviyo private keys, Shopify custom-app tokens)."""
        return cls(access_token=key, token_type="api_key", obtained_at=now)


class VaultUnreadable(NeedsReconnect):
    """The stored tokens were encrypted with another key."""

    code = "connector.vault_unreadable"


class SecretCipher(Protocol):
    fingerprint: str

    def encrypt(self, data: bytes) -> str: ...

    def decrypt(self, token: str) -> bytes: ...


class FernetCipher:
    """Fernet with v1's credential-store key and fingerprint. `cryptography` is imported lazily."""

    def __init__(self, key: bytes) -> None:
        from cryptography.fernet import Fernet

        from engine.connections.store import key_fingerprint

        self._fernet: Any = Fernet(key)
        self.fingerprint = key_fingerprint(key)

    @classmethod
    def from_settings(cls, settings: Any, storage: Any) -> FernetCipher:
        """The key v1 saved connections use (`engine.connections.store.connections_key`)."""
        from engine.connections.store import connections_key

        return cls(connections_key(settings, storage))

    def encrypt(self, data: bytes) -> str:
        return str(self._fernet.encrypt(data).decode("ascii"))

    def decrypt(self, token: str) -> bytes:
        from cryptography.fernet import InvalidToken

        try:
            return bytes(self._fernet.decrypt(token.encode("ascii")))
        except (InvalidToken, ValueError):
            raise VaultUnreadable("the saved tokens cannot be decrypted with the current key") from None


class TokenVault(Protocol):
    def load(self, connection_id: str) -> TokenSet | None: ...

    def save(self, connection_id: str, tokens: TokenSet, now: datetime) -> None: ...

    def delete(self, connection_id: str) -> None: ...


class MemoryTokenVault:
    """For tests and the Gym. Holds `TokenSet`s in a dict; nothing is encrypted."""

    def __init__(self) -> None:
        self.tokens: dict[str, TokenSet] = {}
        self.saves = 0

    def load(self, connection_id: str) -> TokenSet | None:
        return self.tokens.get(connection_id)

    def save(self, connection_id: str, tokens: TokenSet, now: datetime) -> None:  # noqa: ARG002
        self.tokens[connection_id] = tokens
        self.saves += 1

    def delete(self, connection_id: str) -> None:
        self.tokens.pop(connection_id, None)


class SqlTokenVault:
    """Token sets encrypted in `connector_secret`. See the module docstring."""

    def __init__(self, store: ConnectorStore, cipher: SecretCipher) -> None:
        self._store = store
        self._cipher = cipher

    def load(self, connection_id: str) -> TokenSet | None:
        row = self._store.read_secret(connection_id)
        if row is None:
            return None
        token, fingerprint, _version = row
        if fingerprint != self._cipher.fingerprint:
            raise VaultUnreadable(
                "the saved tokens were encrypted with a different key: set the old "
                "MARKETING_AI_CONNECTIONS_KEY back, or connect the account again"
            )
        return TokenSet.model_validate(json.loads(self._cipher.decrypt(token)))

    def save(self, connection_id: str, tokens: TokenSet, now: datetime) -> None:
        sealed = self._cipher.encrypt(tokens.model_dump_json().encode("utf-8"))
        self._store.write_secret(connection_id, sealed, self._cipher.fingerprint, now)

    def delete(self, connection_id: str) -> None:
        self._store.delete_secret(connection_id)


OAuthFor = Callable[[Connection], "OAuthClient | None"]
RevokedHook = Callable[[Connection, NeedsReconnect, datetime], None]


class TokenManager:
    """Hands out valid access tokens; refreshes them under a lease; keeps idle ones alive."""

    def __init__(
        self,
        vault: TokenVault,
        store: ConnectorStore,
        oauth_for: OAuthFor,
        *,
        holder: str,
        on_revoked: RevokedHook | None = None,
        lease_ttl: timedelta = LEASE_TTL,
    ) -> None:
        self.vault = vault
        self.store = store
        self.oauth_for = oauth_for
        self.holder = holder
        self.on_revoked = on_revoked
        self.lease_ttl = lease_ttl
        self.refreshes = 0

    def save(self, connection: Connection, tokens: TokenSet, now: datetime) -> None:
        self.vault.save(connection.connection_id, tokens, now)

    def current(self, connection: Connection) -> TokenSet:
        tokens = self.vault.load(connection.connection_id)
        if tokens is None:
            raise NeedsReconnect(f"{connection.label} has no saved credentials; connect it again")
        return tokens

    def access_token(self, connection: Connection, now: datetime) -> TokenSet:
        """Tokens whose access token is good for at least the provider's refresh margin."""
        try:
            tokens = self.current(connection)
            client = self.oauth_for(connection)
            if client is None or tokens.refresh_token is None:
                if tokens.expired(now):
                    raise NeedsReconnect(f"{connection.label}'s access has expired and cannot be renewed")
                return tokens
            if not tokens.expires_within(now, client.provider.refresh_margin):
                return tokens
            try:
                return self._refresh(connection, client, tokens, now, force=False)
            except NeedsReconnect:
                raise
            except ConnectorFailure:
                if tokens.expired(now):
                    raise
                return tokens  # the token endpoint hiccupped; this token still works for a few minutes
        except NeedsReconnect as exc:
            self._revoked(connection, exc, now)
            raise

    def force_refresh(self, connection: Connection, now: datetime) -> TokenSet:
        """After the API refused a token we thought was valid (a 401): refresh now if we can."""
        try:
            tokens = self.current(connection)
            client = self.oauth_for(connection)
            if client is None or tokens.refresh_token is None:
                return tokens  # nothing to refresh with: the caller's second 401 says "reconnect"
            return self._refresh(connection, client, tokens, now, force=True)
        except NeedsReconnect as exc:
            self._revoked(connection, exc, now)
            raise

    def keep_alive_due(self, connection: Connection, now: datetime) -> bool:
        client = self.oauth_for(connection)
        if client is None or client.provider.keep_alive_every is None:
            return False
        tokens = self.vault.load(connection.connection_id)
        if tokens is None or tokens.refresh_token is None:
            return False
        return now - tokens.last_renewed() >= client.provider.keep_alive_every

    def keep_alive(self, connection: Connection, now: datetime) -> bool:
        """Refresh an idle connection before its refresh token is revoked for disuse; True if refreshed."""
        if not self.keep_alive_due(connection, now):
            return False
        client = self.oauth_for(connection)
        assert client is not None  # keep_alive_due checked it
        try:
            self._refresh(connection, client, self.current(connection), now, force=True)
        except NeedsReconnect as exc:
            self._revoked(connection, exc, now)
            raise
        return True

    def _refresh(
        self, connection: Connection, client: OAuthClient, seen: TokenSet, now: datetime, *, force: bool
    ) -> TokenSet:
        name = f"token:{connection.connection_id}"
        if not self.store.acquire_lease(name, self.holder, now, self.lease_ttl):
            latest = self.current(connection)
            if force and latest.last_renewed() > seen.last_renewed():
                return latest  # the other worker already saved a new token: retry with it
            if not force and not latest.expired(now):
                return latest  # another worker is refreshing; this token still works meanwhile
            # Forced (the API just refused this token): handing the same token back would turn a
            # refresh in flight elsewhere into a second 401, `AuthRejected` and a false "reconnect".
            raise TransientFailure(
                "another worker is refreshing this connection's token", retry_at=now + BUSY_RETRY
            )
        try:
            latest = self.current(connection)
            if latest.last_renewed() > seen.last_renewed():
                return latest  # someone refreshed between our read and our lease
            margin = client.provider.refresh_margin
            if not force and not latest.expires_within(now, margin):
                return latest
            if now - latest.last_renewed() < client.provider.min_refresh_interval and not latest.expired(now):
                return latest  # refreshed moments ago: do not spend the platform's refresh quota
            fresh = client.refresh(latest, now, connection.config)
            self._save_rotated(connection, fresh, now)
            self.refreshes += 1
            return fresh
        finally:
            self.store.release_lease(name, self.holder)

    def _save_rotated(self, connection: Connection, fresh: TokenSet, now: datetime) -> None:
        """Save a just-issued token set, trying again on a database error: with rotating refresh
        tokens the old one is already dead, so losing this write means asking a person to reconnect."""
        for attempt in range(1, SAVE_ATTEMPTS + 1):
            try:
                self.vault.save(connection.connection_id, fresh, now)
                return
            except Exception:
                if attempt == SAVE_ATTEMPTS:
                    raise

    def _revoked(self, connection: Connection, exc: NeedsReconnect, now: datetime) -> None:
        if self.on_revoked is not None:
            self.on_revoked(connection, exc, now)
