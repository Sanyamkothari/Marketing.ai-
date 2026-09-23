"""`scripts/create_user.py`: the first Admin, from a shell, with the password never on the command line."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from engine.access.roles import Role
from engine.access.users import SqlUserStore
from engine.audit.events import AuditQuery
from engine.audit.store import SqlAuditLog
from engine.platform_db import PLATFORM_DB_FILENAME, sqlite_engine
from scripts import create_user

PASSWORD = "correct horse battery staple"


@pytest.fixture(autouse=True)
def _local_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ("MARKETING_AI_METADATA_BACKEND", "MARKETING_AI_STORAGE_BACKEND", "MARKETING_AI_ENV"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MARKETING_AI_DATA_DIR", str(tmp_path))
    # `main` configures the root logger as a real invocation should; in-process that would outlive the
    # test and change what tests/unit/test_logging_audit.py sees, so it is a no-op here.
    monkeypatch.setattr(create_user, "configure_logging", lambda *args, **kwargs: None)


def run(tmp_path: Path, *argv: str, stdin: str = f"{PASSWORD}\n") -> int:
    return create_user.main(
        ["--data-dir", str(tmp_path), "--password-stdin", *argv], stdin=io.StringIO(stdin), iterations=300
    )


def test_there_is_no_password_argument() -> None:
    options = {option for action in create_user.build_parser()._actions for option in action.option_strings}
    assert not {option for option in options if "password" in option} - {"--password-stdin"}


def test_the_first_admin_is_created_and_audited_as_the_bootstrap(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(tmp_path, "--username", "asha", "--display-name", "Asha K") == create_user.EXIT_OK
    engine = sqlite_engine(tmp_path / PLATFORM_DB_FILENAME)
    store = SqlUserStore(engine, session_ttl_seconds=3600, iterations=300)
    user = store.find_by_username("asha")
    assert user is not None and user.roles == frozenset({Role.ADMIN}) and user.display_name == "Asha K"
    assert user.created_by == "system:bootstrap"
    assert store.check_credentials("asha", PASSWORD).user_id == user.user_id
    (event,) = SqlAuditLog(engine).query(AuditQuery())
    assert (event.actor_id, event.actor_kind, event.action) == ("system:bootstrap", "system", "users.create")
    assert (event.object_type, event.object_id) == ("user", user.user_id)
    assert event.details["roles"] == "admin"
    output = capsys.readouterr()
    assert PASSWORD not in output.out + output.err
    assert PASSWORD.encode() not in b"".join(
        p.read_bytes() for p in tmp_path.glob(f"{PLATFORM_DB_FILENAME}*")
    )


def test_roles_can_be_chosen(tmp_path: Path) -> None:
    assert run(tmp_path, "--username", "bob", "--role", "analyst", "--role", "approver") == 0
    store = SqlUserStore(sqlite_engine(tmp_path / PLATFORM_DB_FILENAME), session_ttl_seconds=3600)
    user = store.find_by_username("bob")
    assert user is not None and user.roles == frozenset({Role.ANALYST, Role.APPROVER})


def test_a_refusal_writes_nothing_and_says_why(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(tmp_path, "--username", "asha", stdin="short\n") == create_user.EXIT_REFUSED
    assert "PASSWORD_TOO_SHORT" in capsys.readouterr().err
    assert run(tmp_path, "--username", "asha") == 0
    assert run(tmp_path, "--username", "ASHA") == create_user.EXIT_REFUSED
    assert SqlAuditLog(sqlite_engine(tmp_path / PLATFORM_DB_FILENAME)).count(AuditQuery()) == 1


def test_two_prompts_that_differ_create_nobody(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    answers = iter([PASSWORD, PASSWORD + "!"])
    monkeypatch.setattr(create_user.getpass, "getpass", lambda prompt: next(answers))
    code = create_user.main(["--data-dir", str(tmp_path), "--username", "asha"], iterations=300)
    assert code == create_user.EXIT_REFUSED
    assert not (tmp_path / PLATFORM_DB_FILENAME).exists() or (
        SqlUserStore(sqlite_engine(tmp_path / PLATFORM_DB_FILENAME), session_ttl_seconds=3600).list_users()
        == ()
    )
