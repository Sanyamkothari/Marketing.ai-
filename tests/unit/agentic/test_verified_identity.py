"""Plan M M184 (DEC-1614 (e)): `agentic.require_sign_in` and `require_verified_identity`.

With sign-in off every request is the local operator, holding every role at once, so separation of duties cannot
hold. The switch is off by default - a laptop with sign-in off approves and carries out campaign plans today, and
keeps doing so, its approvals marked `identity_verified: false` - and, on, an approval by anyone but a signed-in
person is refused with `AUTH_NOT_CONFIGURED` before anything is carried out.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.access.roles import LOCAL_OPERATOR, SYSTEM_SCHEDULER, Principal, Role
from engine.agentic.config import AGENTIC_CONFIG_FILE, load_agentic_config, require_verified_identity
from engine.agentic.errors import AgenticError
from engine.approvals import identity_part

PERSON = Principal(user_id="u_ravi", username="ravi", roles=frozenset({Role.APPROVER}), kind="user")


def _switch(root: Path, text: str) -> Path:
    path = root / AGENTIC_CONFIG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return root


def test_the_switch_is_off_by_default_and_in_the_shipped_file(config_root: Path, tmp_path: Path) -> None:
    assert load_agentic_config(config_root).agentic.require_sign_in is False
    assert load_agentic_config(tmp_path).agentic.require_sign_in is False
    assert "agentic.require_sign_in" in (config_root / AGENTIC_CONFIG_FILE).read_text(encoding="utf-8")


def test_off_nobody_is_refused(tmp_path: Path) -> None:
    root = _switch(tmp_path, "agentic:\n  enabled: true\n")
    for principal in (PERSON, LOCAL_OPERATOR, SYSTEM_SCHEDULER):
        require_verified_identity(principal, root)


def test_on_only_a_signed_in_person_is_let_through(tmp_path: Path) -> None:
    root = _switch(tmp_path, "agentic:\n  enabled: true\n  require_sign_in: true\n")
    require_verified_identity(PERSON, root)
    for principal in (LOCAL_OPERATOR, SYSTEM_SCHEDULER):
        with pytest.raises(AgenticError) as refused:
            require_verified_identity(principal, root)
        assert refused.value.code == "AUTH_NOT_CONFIGURED" and refused.value.status == 503
        assert "sign-in is off" in refused.value.message


def test_a_decision_without_a_signed_in_person_is_marked_unverified() -> None:
    assert identity_part(PERSON) == {}
    assert identity_part(LOCAL_OPERATOR) == {"identity_verified": False}
    assert identity_part(SYSTEM_SCHEDULER) == {"identity_verified": False}
