"""The built-in user store (M46): passwords, sessions, roles and the last-Admin rule.

Every store here hashes with a few hundred PBKDF2 iterations instead of `PBKDF2_ITERATIONS`, which
is the constructor's documented test override; one test proves the production default is the one a
store uses when nobody overrides it.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine

from engine.access.roles import Role
from engine.access.users import (
    MIN_PASSWORD_LENGTH,
    PBKDF2_ITERATIONS,
    AccessError,
    SqlUserStore,
    UserStore,
    hash_password,
    hash_token,
    verify_password,
)
from engine.platform_db import PLATFORM_DB_FILENAME, sqlite_engine

PASSWORD = "correct horse battery staple"
FAST = 300


class Clock:
    """A settable clock, so expiry is tested without sleeping."""

    def __init__(self) -> None:
        self.now = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return sqlite_engine(tmp_path / PLATFORM_DB_FILENAME)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(engine: Engine, clock: Clock) -> SqlUserStore:
    return SqlUserStore(engine, session_ttl_seconds=3600, iterations=FAST, clock=clock)


def make(store: SqlUserStore, name: str, *roles: Role) -> str:
    return store.create_user(name, PASSWORD, roles=roles or (Role.VIEWER,), created_by="test").user_id


# ---------------------------------------------------------------------------
# Password hashing (DEC-710)
# ---------------------------------------------------------------------------
def test_a_hash_is_self_describing_salted_and_verifies_in_constant_time() -> None:
    first = hash_password(PASSWORD, iterations=FAST)
    second = hash_password(PASSWORD, iterations=FAST)
    assert first != second, "two hashes of one password must differ: the salt is per hash"
    algorithm, iterations, salt, digest = first.split("$")
    assert (algorithm, iterations) == ("pbkdf2_sha256", str(FAST))
    assert salt and digest and PASSWORD not in first
    assert verify_password(PASSWORD, first)
    assert not verify_password(PASSWORD + "x", first)


@pytest.mark.parametrize(
    "encoded", ["", "nonsense", "md5$1$a$b", "pbkdf2_sha256$x$a$b", "pbkdf2_sha256$0$a$b"]
)
def test_a_malformed_hash_verifies_nothing(encoded: str) -> None:
    assert not verify_password(PASSWORD, encoded)


def test_the_iteration_count_is_read_from_the_hash_so_it_can_be_raised_later() -> None:
    old = hash_password(PASSWORD, iterations=FAST)
    assert verify_password(PASSWORD, old)  # verified with 300, whatever today's default is


def test_the_default_store_uses_the_owasp_iteration_count(engine: Engine) -> None:
    subject = SqlUserStore(engine, session_ttl_seconds=3600)
    user_id = subject.create_user("slowhash", PASSWORD, roles=[Role.VIEWER], created_by="test").user_id
    path = engine.url.database
    assert path is not None
    with sqlite3.connect(path) as connection:
        (encoded,) = connection.execute(
            "SELECT password_hash FROM platform_user WHERE user_id = ?", (user_id,)
        ).fetchone()
    assert encoded.split("$")[1] == str(PBKDF2_ITERATIONS) == "600000"


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
def test_the_sql_store_is_a_user_store(store: SqlUserStore) -> None:
    assert isinstance(store, UserStore)


def test_a_created_user_has_no_password_on_its_record(store: SqlUserStore) -> None:
    user = store.create_user(
        "Asha", PASSWORD, roles=[Role.ANALYST], created_by="u-admin", display_name="Asha K"
    )
    assert user.username == "Asha"
    assert user.display_name == "Asha K"
    assert user.roles == frozenset({Role.ANALYST})
    assert user.created_by == "u-admin"
    assert user.created_at.tzinfo is not None
    assert "password" not in type(user).model_fields
    assert store.get_user(user.user_id) == user


def test_usernames_are_unique_case_insensitively(store: SqlUserStore) -> None:
    make(store, "Asha")
    with pytest.raises(AccessError) as caught:
        make(store, "ASHA")
    assert caught.value.code == "USERNAME_TAKEN"
    found = store.find_by_username("aSHa")
    assert found is not None and found.username == "Asha"


@pytest.mark.parametrize("name", ["ab", "-leading", "has space", "x" * 65, "semi;colon"])
def test_a_malformed_username_is_refused(store: SqlUserStore, name: str) -> None:
    with pytest.raises(AccessError) as caught:
        make(store, name)
    assert caught.value.code == "USERNAME_INVALID"


def test_a_short_password_is_refused_with_the_rule(store: SqlUserStore) -> None:
    with pytest.raises(AccessError) as caught:
        store.create_user("asha", "x" * (MIN_PASSWORD_LENGTH - 1), roles=[Role.VIEWER], created_by="t")
    assert caught.value.code == "PASSWORD_TOO_SHORT"
    assert str(MIN_PASSWORD_LENGTH) in caught.value.message
    assert store.find_by_username("asha") is None


def test_a_user_needs_a_role(store: SqlUserStore) -> None:
    with pytest.raises(AccessError) as caught:
        store.create_user("asha", PASSWORD, roles=[], created_by="t")
    assert caught.value.code == "ROLES_REQUIRED"


def test_check_credentials_tells_the_audit_log_why_and_the_person_nothing(store: SqlUserStore) -> None:
    user_id = make(store, "asha", Role.ANALYST)
    assert store.check_credentials("ASHA", PASSWORD).user_id == user_id
    with pytest.raises(AccessError) as wrong:
        store.check_credentials("asha", "not the password")
    with pytest.raises(AccessError) as unknown:
        store.check_credentials("nobody", PASSWORD)
    make(store, "admin", Role.ADMIN)
    store.update_user(user_id, disabled=True)
    with pytest.raises(AccessError) as disabled:
        store.check_credentials("asha", PASSWORD)
    assert (wrong.value.code, wrong.value.user_id) == ("BAD_CREDENTIALS", user_id)
    assert (unknown.value.code, unknown.value.user_id) == ("BAD_CREDENTIALS", None)
    assert (disabled.value.code, disabled.value.user_id) == ("USER_DISABLED", user_id)
    assert wrong.value.message == unknown.value.message == disabled.value.message


# ---------------------------------------------------------------------------
# Sessions (DEC-711)
# ---------------------------------------------------------------------------
def test_a_token_is_stored_only_as_its_sha256(store: SqlUserStore, engine: Engine) -> None:
    user_id = make(store, "asha")
    issued = store.create_session(user_id)
    path = engine.url.database
    assert path is not None
    raw = Path(path).read_bytes() + b"".join(
        candidate.read_bytes() for candidate in Path(path).parent.glob(f"{PLATFORM_DB_FILENAME}-*")
    )
    assert issued.token.encode() not in raw
    with sqlite3.connect(path) as connection:
        hashes = [row[0] for row in connection.execute("SELECT token_hash FROM auth_session")]
    assert hashes == [hash_token(issued.token)]


def test_a_session_resolves_until_it_expires(store: SqlUserStore, clock: Clock) -> None:
    user_id = make(store, "asha")
    issued = store.create_session(user_id)
    assert issued.expires_at == clock.now + timedelta(seconds=3600)
    found = store.resolve_session(issued.token)
    assert found is not None and found.user_id == user_id
    clock.now += timedelta(seconds=3599)
    assert store.resolve_session(issued.token) is not None
    clock.now += timedelta(seconds=1)
    assert store.resolve_session(issued.token) is None


def test_an_unknown_or_empty_token_resolves_nobody(store: SqlUserStore) -> None:
    assert store.resolve_session("") is None
    assert store.resolve_session("not-a-token") is None


def test_logout_revokes_one_session_only(store: SqlUserStore) -> None:
    user_id = make(store, "asha")
    first, second = store.create_session(user_id), store.create_session(user_id)
    assert store.revoke_session(first.token) is True
    assert store.revoke_session(first.token) is False
    assert store.resolve_session(first.token) is None
    assert store.resolve_session(second.token) is not None


@pytest.mark.parametrize(
    "change",
    [
        {"roles": [Role.VIEWER, Role.APPROVER]},
        {"disabled": True},
    ],
)
def test_a_role_change_or_disabling_signs_the_user_out_everywhere(store: SqlUserStore, change: dict) -> None:
    make(store, "admin", Role.ADMIN)
    user_id = make(store, "asha", Role.VIEWER)
    tokens = [store.create_session(user_id).token for _ in range(2)]
    store.update_user(user_id, **change)
    assert [store.resolve_session(token) for token in tokens] == [None, None]


def test_a_display_name_change_keeps_the_sessions(store: SqlUserStore) -> None:
    user_id = make(store, "asha")
    token = store.create_session(user_id).token
    assert store.update_user(user_id, display_name="Asha K").display_name == "Asha K"
    assert store.resolve_session(token) is not None


def test_a_password_change_signs_the_user_out_and_the_new_password_works(store: SqlUserStore) -> None:
    user_id = make(store, "asha")
    token = store.create_session(user_id).token
    store.set_password(user_id, "a brand new passphrase")
    assert store.resolve_session(token) is None
    assert store.check_credentials("asha", "a brand new passphrase").user_id == user_id
    with pytest.raises(AccessError):
        store.check_credentials("asha", PASSWORD)


def test_a_disabled_users_live_token_stops_working(store: SqlUserStore, engine: Engine) -> None:
    make(store, "admin", Role.ADMIN)
    user_id = make(store, "asha")
    token = store.create_session(user_id).token
    with engine.begin() as connection:  # a disabled flag set behind the store's back is still honoured
        connection.exec_driver_sql("UPDATE platform_user SET disabled = 1 WHERE user_id = ?", (user_id,))
    assert store.resolve_session(token) is None


# ---------------------------------------------------------------------------
# The last Admin (DEC-712)
# ---------------------------------------------------------------------------
def test_the_last_admin_cannot_be_disabled_or_demoted(store: SqlUserStore) -> None:
    admin = make(store, "admin", Role.ADMIN, Role.APPROVER)
    for change in ({"disabled": True}, {"roles": [Role.APPROVER]}):
        with pytest.raises(AccessError) as caught:
            store.update_user(admin, **change)
        assert caught.value.code == "LAST_ADMIN"
    after = store.get_user(admin)
    assert after is not None and Role.ADMIN in after.roles and not after.disabled
    assert store.count_active_admins() == 1


def test_a_disabled_admin_does_not_count_as_the_other_admin(store: SqlUserStore) -> None:
    first = make(store, "admin1", Role.ADMIN)
    second = make(store, "admin2", Role.ADMIN)
    store.update_user(second, disabled=True)
    assert store.count_active_admins() == 1
    with pytest.raises(AccessError) as caught:
        store.update_user(first, roles=[Role.VIEWER])
    assert caught.value.code == "LAST_ADMIN"


def test_with_two_admins_one_may_step_down(store: SqlUserStore) -> None:
    first = make(store, "admin1", Role.ADMIN)
    make(store, "admin2", Role.ADMIN)
    assert store.update_user(first, roles=[Role.VIEWER]).roles == frozenset({Role.VIEWER})
    assert store.count_active_admins() == 1


def test_updating_an_unknown_user_is_not_found(store: SqlUserStore) -> None:
    with pytest.raises(AccessError) as caught:
        store.update_user("u-missing", display_name="x")
    assert caught.value.code == "USER_NOT_FOUND"
    with pytest.raises(AccessError):
        store.set_password("u-missing", PASSWORD)


def test_list_users_is_sorted_by_username(store: SqlUserStore) -> None:
    for name in ("carol", "Bob", "alice"):
        make(store, name)
    assert [user.username for user in store.list_users()] == ["alice", "Bob", "carol"]
