"""Plan M M184 (DEC-1614 (e)): a model decision made with sign-in off says, in the audit trail, that who made it
is not known.

With sign-in off every request is the local operator, holding every role, so separation of duties cannot hold
(DEC-862 enforces it between signed-in people only). Local use is not broken: the decision is made and recorded as
before, and its audit event carries `identity_verified: false`. A signed-in decision's event is unchanged.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration.production.test_approvals import CHALLENGER, World

pytestmark = pytest.mark.integration


def test_with_sign_in_off_an_approval_is_made_and_audited_as_unverified(tmp_path: Path) -> None:
    world = World(tmp_path, auth_mode="off")
    response = world.client.post(
        f"/models/{CHALLENGER}/approve", json={"approved_by": "Asha Rao", "reason": "looks right"}
    )
    assert response.status_code == 200, response.text
    (event,) = world.events("models.approve")
    assert event.outcome == "success"
    assert event.details["identity_verified"] is False


def test_a_signed_in_approval_is_audited_as_before(tmp_path: Path) -> None:
    world = World(tmp_path)
    response = world.client.post(
        f"/models/{CHALLENGER}/approve",
        json={"approved_by": "x", "reason": "beats it"},
        headers=world.approver,
    )
    assert response.status_code == 200, response.text
    (event,) = world.events("models.approve")
    assert "identity_verified" not in event.details
