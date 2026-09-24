"""`scripts.demo_users` creates one sign-in user per role for a local demo (DEC-961)."""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.access.roles import Role
from engine.access.users import SqlUserStore
from engine.platform_db import platform_engine
from engine.settings import load_settings
from scripts import demo_users


def _store(data_dir: Path) -> SqlUserStore:
    return SqlUserStore(platform_engine(load_settings(), data_dir=data_dir), session_ttl_seconds=3600)


def test_every_role_gets_a_user_that_can_sign_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MARKETING_AI_AUTH_MODE", "local")
    assert demo_users.main(["--data-dir", str(tmp_path)]) == 0
    store = _store(tmp_path)
    for username, _, roles in demo_users.DEMO_USERS:
        principal = store.check_credentials(username, demo_users.demo_password(username))
        assert {role.value for role in principal.roles} == set(roles), username
    covered = {role for _, _, roles in demo_users.DEMO_USERS for role in roles}
    assert covered == {role.value for role in Role}


def test_running_it_twice_is_harmless(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MARKETING_AI_AUTH_MODE", "local")
    assert demo_users.main(["--data-dir", str(tmp_path)]) == 0
    assert demo_users.main(["--data-dir", str(tmp_path)]) == 0


def test_it_refuses_a_production_deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MARKETING_AI_ENV", "prod")
    assert demo_users.main(["--data-dir", str(tmp_path)]) != 0
