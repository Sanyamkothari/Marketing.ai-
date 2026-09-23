"""The identity providers (M46): who an `Authorization` header proves, and nothing more."""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.access.identity import DisabledIdentity, IdentityProvider, LocalIdentity, bearer_token
from engine.access.roles import LOCAL_OPERATOR, Role
from engine.access.users import SqlUserStore
from engine.platform_db import PLATFORM_DB_FILENAME, sqlite_engine

PASSWORD = "correct horse battery staple"


@pytest.fixture
def store(tmp_path: Path) -> SqlUserStore:
    return SqlUserStore(
        sqlite_engine(tmp_path / PLATFORM_DB_FILENAME), session_ttl_seconds=3600, iterations=300
    )


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, None),
        ("", None),
        ("Bearer abc", "abc"),
        ("bearer abc", "abc"),
        ("BEARER   abc  ", "abc"),
        ("Basic dXNlcjpwYXNz", None),
        ("Bearer", None),
        ("Bearer a b", None),
        ("Bearer " + "x" * 257, None),
    ],
)
def test_bearer_token_parses_only_the_bearer_scheme(header: str | None, expected: str | None) -> None:
    assert bearer_token(header) == expected


def test_disabled_identity_is_always_the_local_operator() -> None:
    subject = DisabledIdentity()
    assert isinstance(subject, IdentityProvider)
    assert subject.authenticate(None) is LOCAL_OPERATOR
    assert subject.authenticate("Bearer stale-token-from-yesterday") is LOCAL_OPERATOR
    assert LOCAL_OPERATOR.roles == frozenset(Role)


def test_local_identity_turns_a_live_session_into_a_principal(store: SqlUserStore) -> None:
    user = store.create_user("asha", PASSWORD, roles=[Role.APPROVER], created_by="t")
    token = store.create_session(user.user_id).token
    subject = LocalIdentity(store)
    assert isinstance(subject, IdentityProvider)
    principal = subject.authenticate(f"Bearer {token}")
    assert principal is not None
    assert (principal.user_id, principal.username, principal.kind) == (user.user_id, "asha", "user")
    assert principal.roles == frozenset({Role.APPROVER, Role.VIEWER})
    assert principal.has(Role.APPROVER) and not principal.has(Role.ANALYST)


def test_local_identity_refuses_everything_else(store: SqlUserStore) -> None:
    user = store.create_user("asha", PASSWORD, roles=[Role.VIEWER], created_by="t")
    token = store.create_session(user.user_id).token
    subject = LocalIdentity(store)
    assert subject.authenticate(None) is None
    assert subject.authenticate(token) is None  # no scheme
    assert subject.authenticate("Bearer not-a-real-token") is None
    store.revoke_session(token)
    assert subject.authenticate(f"Bearer {token}") is None
