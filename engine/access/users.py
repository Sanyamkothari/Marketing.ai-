"""The built-in user store of `auth_mode=local`: people, their roles, their passwords and sign-ins.

This is the development and small-deployment half of plan M46 ("Local mode: a simple built-in user
store"). A production deployment is expected to delegate *who you are* to the identity provider of
M50 (Cognito or the client's SSO, decision P5); even then this store's shape - a user id, a set of
roles, a disabled flag - is what the rest of the product reads, so nothing downstream changes when
the provider does.

Four rules, each a decision rather than a default:

**Passwords are PBKDF2-HMAC-SHA256 from the standard library** (DEC-710). No new dependency, a
per-user random salt, a constant-time comparison, and a self-describing hash string
(`pbkdf2_sha256$<iterations>$<salt>$<hash>`) so the iteration count can be raised later without
invalidating anyone: each hash is verified with the count it was made with. `PBKDF2_ITERATIONS`
follows the current OWASP figure for this function; the constructor takes an override only so the
test suite does not spend minutes hashing.

**A session token is shown once and stored only as its SHA-256** (DEC-711). The token is 32 random
bytes from `secrets`; the database holds its digest, so a copy of `platform.db` or a database
snapshot signs nobody in. A plain (unsalted, fast) hash is right here and wrong for passwords: the
token has 256 bits of entropy, so there is nothing to brute-force. Expiry is
`settings.auth_session_ttl_seconds`; sign-out revokes; and **disabling a user, changing their roles
or changing their password revokes every session they hold**, so a role taken away is taken away
now and not at the end of somebody's working day.

**The last active Admin cannot be disabled or lose the Admin role** (`LAST_ADMIN`, DEC-712).
A deployment with no Admin can only be recovered from a shell with `scripts/create_user.py`; the
store refuses to walk into that state through the API.

**Usernames are unique case-insensitively** (`Asha` and `asha` are one person) and are stored as
typed for display. The comparison key is `str.casefold()`.

Nothing here logs a username, a password or a token. The audit trail records user ids.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import threading
import uuid
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Final, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Column, DateTime
from sqlalchemy.engine import Engine
from sqlmodel import Field as SQLField
from sqlmodel import Session, SQLModel, col, select

from engine.access.roles import Role
from engine.platform_db import create_tables
from engine.utils.time import utc_now

__all__ = [
    "AUTH_SESSION_TABLE",
    "MIN_PASSWORD_LENGTH",
    "PBKDF2_ITERATIONS",
    "USER_TABLE",
    "AccessError",
    "AuthSessionRow",
    "IssuedSession",
    "PlatformUserRow",
    "SqlUserStore",
    "UserRecord",
    "UserStore",
    "hash_password",
    "hash_token",
    "verify_password",
]

USER_TABLE: Final[str] = "platform_user"
AUTH_SESSION_TABLE: Final[str] = "auth_session"

PBKDF2_ITERATIONS: Final[int] = 600_000
"""OWASP's 2023 recommendation for PBKDF2-HMAC-SHA256. Stored in each hash, so raising it later is safe."""

MIN_PASSWORD_LENGTH: Final[int] = 12
"""Length beats composition rules (NIST SP 800-63B); twelve is the floor, not the advice."""

MAX_PASSWORD_LENGTH: Final[int] = 1024
"""Anything longer is not a password somebody typed, and hashing it is work an attacker chose."""

_SALT_BYTES: Final[int] = 16
_ALGORITHM: Final[str] = "pbkdf2_sha256"
_USERNAME_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{2,63}$")
_DISPLAY_NAME_MAX: Final[int] = 120


class AccessError(Exception):
    """A refusal from the user store: a stable `code`, a business-language `message`.

    `user_id` is set when the refusal is about a known user, so a caller can record *whose*
    sign-in failed in the audit log without ever recording the username that was typed.
    """

    def __init__(self, code: str, message: str, *, user_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.user_id = user_id


class UserRecord(BaseModel):
    """A user as the rest of the product sees one. There is no password field, by construction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    user_id: str = Field(description="Stable id; what the audit log records.")
    username: str = Field(description="What the person signs in with, as they typed it.")
    display_name: str = Field(description="How the person is shown.")
    roles: frozenset[Role] = Field(description="Roles granted, as granted (Viewer is implied at use).")
    disabled: bool = Field(default=False, description="A disabled user cannot sign in.")
    created_at: datetime = Field(description="When the user was created, UTC.")
    created_by: str = Field(
        description="`user_id` of whoever created them (`system:bootstrap` for the first)."
    )
    updated_at: datetime = Field(description="When the record last changed, UTC.")


class IssuedSession(BaseModel):
    """A new sign-in. `token` exists only in this object and in the response that carries it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    token: str = Field(description="Bearer token; never stored, only its SHA-256.")
    user_id: str
    expires_at: datetime


class PlatformUserRow(SQLModel, table=True):
    """One row per user. `username_key` is the case-folded username and carries the uniqueness."""

    __tablename__ = USER_TABLE

    user_id: str = SQLField(primary_key=True)
    username: str
    username_key: str = SQLField(unique=True, index=True)
    display_name: str
    roles: str = SQLField(description="Comma-separated role values, sorted.")
    disabled: bool = False
    password_hash: str
    created_at: datetime = SQLField(sa_column=Column("created_at", DateTime(timezone=True), nullable=False))
    created_by: str
    updated_at: datetime = SQLField(sa_column=Column("updated_at", DateTime(timezone=True), nullable=False))


class AuthSessionRow(SQLModel, table=True):
    """One row per sign-in, keyed by the SHA-256 of its token. Revoked rows are kept, marked."""

    __tablename__ = AUTH_SESSION_TABLE

    token_hash: str = SQLField(primary_key=True)
    user_id: str = SQLField(index=True)
    created_at: datetime = SQLField(sa_column=Column("created_at", DateTime(timezone=True), nullable=False))
    expires_at: datetime = SQLField(sa_column=Column("expires_at", DateTime(timezone=True), nullable=False))
    revoked_at: datetime | None = SQLField(
        default=None, sa_column=Column("revoked_at", DateTime(timezone=True), nullable=True)
    )


@runtime_checkable
class UserStore(Protocol):
    """Users, passwords and sign-ins. `SqlUserStore` is the one implementation; M50 adds a provider."""

    def create_user(
        self,
        username: str,
        password: str,
        *,
        roles: Iterable[Role],
        created_by: str,
        display_name: str | None = None,
    ) -> UserRecord: ...

    def get_user(self, user_id: str) -> UserRecord | None: ...

    def find_by_username(self, username: str) -> UserRecord | None: ...

    def list_users(self) -> tuple[UserRecord, ...]: ...

    def update_user(
        self,
        user_id: str,
        *,
        display_name: str | None = None,
        roles: Iterable[Role] | None = None,
        disabled: bool | None = None,
    ) -> UserRecord: ...

    def set_password(self, user_id: str, password: str) -> None: ...

    def check_credentials(self, username: str, password: str) -> UserRecord: ...

    def create_session(self, user_id: str) -> IssuedSession: ...

    def resolve_session(self, token: str) -> UserRecord | None: ...

    def revoke_session(self, token: str) -> bool: ...

    def revoke_user_sessions(self, user_id: str) -> int: ...

    def count_active_admins(self) -> int: ...


# ---------------------------------------------------------------------------
# Password and token hashing
# ---------------------------------------------------------------------------
def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    """`pbkdf2_sha256$<iterations>$<salt>$<digest>` for `password`, with a fresh random salt."""
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{_ALGORITHM}${iterations}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    """Whether `password` matches `encoded`, compared in constant time; False for any malformed hash."""
    try:
        algorithm, iterations_text, salt_text, digest_text = encoded.split("$")
        iterations = int(iterations_text)
        salt, expected = _unb64(salt_text), _unb64(digest_text)
    except ValueError:
        return False
    if algorithm != _ALGORITHM or iterations < 1:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def hash_token(token: str) -> str:
    """The SHA-256 a session token is stored as (DEC-711)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _roles_text(roles: Iterable[Role]) -> str:
    return ",".join(sorted({Role(role).value for role in roles}))


def _roles_set(text: str) -> frozenset[Role]:
    return frozenset(Role(part) for part in text.split(",") if part)


def _check_password_rule(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AccessError(
            "PASSWORD_TOO_SHORT", f"A password must be at least {MIN_PASSWORD_LENGTH} characters long."
        )
    if len(password) > MAX_PASSWORD_LENGTH:
        raise AccessError("PASSWORD_TOO_LONG", f"A password may be at most {MAX_PASSWORD_LENGTH} characters.")


def _check_roles(roles: frozenset[Role]) -> None:
    if not roles:
        raise AccessError("ROLES_REQUIRED", "A user needs at least one role; Viewer is the smallest.")


# ---------------------------------------------------------------------------
# The SQL store
# ---------------------------------------------------------------------------
class SqlUserStore:
    """`UserStore` on the platform database (`platform.db` locally, the registry's Postgres otherwise).

    Writes that must see a consistent picture - the last-Admin check against the change that would
    break it - run under one process lock and one transaction. Two API processes against one
    Postgres could still race the last-Admin rule; the consequence is an Admin-less deployment that
    `scripts/create_user.py` repairs, which is judged acceptable for a rule that guards against a
    mistake rather than an attack.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        session_ttl_seconds: int,
        iterations: int = PBKDF2_ITERATIONS,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._engine = engine
        self._ttl = timedelta(seconds=session_ttl_seconds)
        self._iterations = iterations
        self._clock = clock
        self._lock = threading.Lock()
        # A hash to verify against when the username is unknown, so "no such user" costs the same
        # time as "wrong password" and the response time does not say which usernames exist.
        self._decoy = hash_password(secrets.token_urlsafe(16), iterations=iterations)
        create_tables(engine, (USER_TABLE, AUTH_SESSION_TABLE))

    # -- users ---------------------------------------------------------------------------
    def create_user(
        self,
        username: str,
        password: str,
        *,
        roles: Iterable[Role],
        created_by: str,
        display_name: str | None = None,
    ) -> UserRecord:
        """Create a user. `USERNAME_INVALID`, `USERNAME_TAKEN`, `PASSWORD_TOO_SHORT`, `ROLES_REQUIRED`."""
        name = username.strip()
        if not _USERNAME_RE.fullmatch(name):
            raise AccessError(
                "USERNAME_INVALID",
                "A username is 3-64 letters, digits, dots, dashes, underscores or @, starting with a letter or digit.",
            )
        role_set = frozenset(Role(role) for role in roles)
        _check_roles(role_set)
        _check_password_rule(password)
        shown = self._display_name(display_name, fallback=name)
        now = self._clock()
        row = PlatformUserRow(
            user_id=f"u-{uuid.uuid4().hex[:16]}",
            username=name,
            username_key=name.casefold(),
            display_name=shown,
            roles=_roles_text(role_set),
            disabled=False,
            password_hash=hash_password(password, iterations=self._iterations),
            created_at=now,
            created_by=created_by,
            updated_at=now,
        )
        with self._lock, Session(self._engine) as session:
            taken = session.exec(
                select(PlatformUserRow).where(col(PlatformUserRow.username_key) == row.username_key)
            ).first()
            if taken is not None:
                raise AccessError("USERNAME_TAKEN", "Another user already has that username.")
            session.add(row)
            session.commit()
            session.refresh(row)
            return _record(row)

    def get_user(self, user_id: str) -> UserRecord | None:
        """The user with this id, or None."""
        with Session(self._engine) as session:
            row = session.get(PlatformUserRow, user_id)
            return None if row is None else _record(row)

    def find_by_username(self, username: str) -> UserRecord | None:
        """The user with this username, compared case-insensitively, or None."""
        with Session(self._engine) as session:
            row = session.exec(
                select(PlatformUserRow).where(
                    col(PlatformUserRow.username_key) == username.strip().casefold()
                )
            ).first()
            return None if row is None else _record(row)

    def list_users(self) -> tuple[UserRecord, ...]:
        """Every user, by username."""
        with Session(self._engine) as session:
            rows = session.exec(select(PlatformUserRow).order_by(col(PlatformUserRow.username_key))).all()
            return tuple(_record(row) for row in rows)

    def update_user(
        self,
        user_id: str,
        *,
        display_name: str | None = None,
        roles: Iterable[Role] | None = None,
        disabled: bool | None = None,
    ) -> UserRecord:
        """Change a user. A change of roles, or disabling, revokes the user's sessions (DEC-711).

        Refused with `LAST_ADMIN` when it would leave no enabled user holding Admin (DEC-712).
        """
        with self._lock, Session(self._engine) as session:
            row = session.get(PlatformUserRow, user_id)
            if row is None:
                raise AccessError("USER_NOT_FOUND", "There is no user with that id.")
            old_roles = _roles_set(row.roles)
            new_roles = old_roles if roles is None else frozenset(Role(role) for role in roles)
            _check_roles(new_roles)
            new_disabled = row.disabled if disabled is None else disabled
            was_admin = Role.ADMIN in old_roles and not row.disabled
            stays_admin = Role.ADMIN in new_roles and not new_disabled
            if was_admin and not stays_admin and self._active_admins(session) <= 1:
                raise AccessError(
                    "LAST_ADMIN",
                    "This is the last active Admin. Make another user an Admin first.",
                    user_id=user_id,
                )
            revoke = new_roles != old_roles or (new_disabled and not row.disabled)
            if display_name is not None:
                row.display_name = self._display_name(display_name, fallback=row.username)
            row.roles = _roles_text(new_roles)
            row.disabled = new_disabled
            row.updated_at = self._clock()
            session.add(row)
            if revoke:
                self._revoke_all(session, user_id)
            session.commit()
            session.refresh(row)
            return _record(row)

    def set_password(self, user_id: str, password: str) -> None:
        """Replace a password and revoke every session the user holds."""
        _check_password_rule(password)
        encoded = hash_password(password, iterations=self._iterations)
        with self._lock, Session(self._engine) as session:
            row = session.get(PlatformUserRow, user_id)
            if row is None:
                raise AccessError("USER_NOT_FOUND", "There is no user with that id.")
            row.password_hash = encoded
            row.updated_at = self._clock()
            session.add(row)
            self._revoke_all(session, user_id)
            session.commit()

    def check_credentials(self, username: str, password: str) -> UserRecord:
        """The user these credentials belong to, or `AccessError` (`BAD_CREDENTIALS`, `USER_DISABLED`).

        The two codes are for the audit log; the API gives the person one message for both, so a
        sign-in form does not confirm which accounts exist or are disabled.
        """
        with Session(self._engine) as session:
            row = session.exec(
                select(PlatformUserRow).where(
                    col(PlatformUserRow.username_key) == username.strip().casefold()
                )
            ).first()
        if row is None:
            verify_password(password, self._decoy)
            raise AccessError("BAD_CREDENTIALS", "The username or password is not right.")
        if not verify_password(password, row.password_hash):
            raise AccessError(
                "BAD_CREDENTIALS", "The username or password is not right.", user_id=row.user_id
            )
        if row.disabled:
            raise AccessError("USER_DISABLED", "The username or password is not right.", user_id=row.user_id)
        return _record(row)

    def count_active_admins(self) -> int:
        """How many enabled users hold Admin."""
        with Session(self._engine) as session:
            return self._active_admins(session)

    # -- sessions ------------------------------------------------------------------------
    def create_session(self, user_id: str) -> IssuedSession:
        """Issue a bearer token for `user_id`, valid for the configured time to live."""
        token = secrets.token_urlsafe(32)
        now = self._clock()
        expires = now + self._ttl
        with Session(self._engine) as session:
            session.add(
                AuthSessionRow(
                    token_hash=hash_token(token), user_id=user_id, created_at=now, expires_at=expires
                )
            )
            session.commit()
        return IssuedSession(token=token, user_id=user_id, expires_at=expires)

    def resolve_session(self, token: str) -> UserRecord | None:
        """The enabled user a live, unrevoked token belongs to, or None."""
        if not token:
            return None
        with Session(self._engine) as session:
            row = session.get(AuthSessionRow, hash_token(token))
            if row is None or row.revoked_at is not None or to_utc(row.expires_at) <= self._clock():
                return None
            user = session.get(PlatformUserRow, row.user_id)
            if user is None or user.disabled:
                return None
            return _record(user)

    def revoke_session(self, token: str) -> bool:
        """Sign one token out. True when it was live."""
        with Session(self._engine) as session:
            row = session.get(AuthSessionRow, hash_token(token))
            if row is None or row.revoked_at is not None:
                return False
            row.revoked_at = self._clock()
            session.add(row)
            session.commit()
            return True

    def revoke_user_sessions(self, user_id: str) -> int:
        """Sign every session of `user_id` out; how many were live."""
        with Session(self._engine) as session:
            count = self._revoke_all(session, user_id)
            session.commit()
            return count

    # -- helpers -------------------------------------------------------------------------
    def _revoke_all(self, session: Session, user_id: str) -> int:
        now = self._clock()
        rows = session.exec(
            select(AuthSessionRow).where(
                col(AuthSessionRow.user_id) == user_id, col(AuthSessionRow.revoked_at).is_(None)
            )
        ).all()
        for row in rows:
            row.revoked_at = now
            session.add(row)
        return len(rows)

    @staticmethod
    def _active_admins(session: Session) -> int:
        rows = session.exec(select(PlatformUserRow).where(col(PlatformUserRow.disabled).is_(False))).all()
        return sum(1 for row in rows if Role.ADMIN in _roles_set(row.roles))

    @staticmethod
    def _display_name(value: str | None, *, fallback: str) -> str:
        text = (value or "").strip() or fallback
        if len(text) > _DISPLAY_NAME_MAX:
            raise AccessError(
                "DISPLAY_NAME_TOO_LONG", f"A display name may be at most {_DISPLAY_NAME_MAX} characters."
            )
        return text


def to_utc(moment: datetime) -> datetime:
    """An aware UTC datetime. SQLite hands a `DateTime(timezone=True)` column back naive, in UTC."""
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def _record(row: PlatformUserRow) -> UserRecord:
    return UserRecord(
        user_id=row.user_id,
        username=row.username,
        display_name=row.display_name,
        roles=_roles_set(row.roles),
        disabled=row.disabled,
        created_at=to_utc(row.created_at),
        created_by=row.created_by,
        updated_at=to_utc(row.updated_at),
    )
