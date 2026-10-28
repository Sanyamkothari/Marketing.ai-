"""Plan M M174 (DEC-1604): the `ui_layout` deployment setting and its delivery to the UI.

The setting is presentation only: it decides which shell the browser draws (today's top bar, or the
five-place sidebar of Plan M §3) and nothing the API does. It reaches the browser on the call the UI
already makes at start-up, `GET /auth/me`, so drawing the shell costs no extra request. The default is
`classic`, so a deployment that never mentions it is byte-for-byte what it was before M174.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from engine.access.roles import Role
from engine.settings import ENV_VARS, Settings, SettingsError
from tests.integration.production.access_support import bearer, local_app, make_user

pytestmark = pytest.mark.integration


def test_the_setting_defaults_to_the_classic_layout() -> None:
    assert Settings().ui_layout == "classic"
    assert Settings.from_env({}).ui_layout == "classic"
    assert ENV_VARS["ui_layout"] == "MARKETING_AI_UI_LAYOUT"


def test_the_environment_chooses_the_campaigns_layout() -> None:
    assert Settings.from_env({"MARKETING_AI_UI_LAYOUT": "campaigns"}).ui_layout == "campaigns"
    assert Settings.from_env({"MARKETING_AI_UI_LAYOUT": "classic"}).ui_layout == "classic"


def test_an_unknown_layout_is_refused_naming_the_variable() -> None:
    with pytest.raises(SettingsError) as raised:
        Settings.from_env({"MARKETING_AI_UI_LAYOUT": "sidebar"})
    assert raised.value.code == "SETTING_INVALID"
    assert raised.value.env_var == "MARKETING_AI_UI_LAYOUT"


@pytest.mark.parametrize("layout", ["classic", "campaigns"])
def test_auth_me_hands_the_layout_to_the_ui_with_sign_in_off(tmp_path: Path, layout: str) -> None:
    app = local_app(tmp_path, auth_mode="off", ui_layout=layout)
    with TestClient(app) as client:
        response = client.get("/auth/me")
    assert response.status_code == 200, response.text
    assert response.json()["ui_layout"] == layout


def test_auth_me_hands_the_layout_to_every_signed_in_role(tmp_path: Path) -> None:
    app = local_app(tmp_path, ui_layout="campaigns")
    ids = {role: make_user(app, f"{role.value}-person", (role,)) for role in Role}
    with TestClient(app) as client:
        for role, user_id in ids.items():
            response = client.get("/auth/me", headers=bearer(app, user_id))
            assert response.status_code == 200, response.text
            assert response.json()["ui_layout"] == "campaigns", role


def test_the_default_answer_says_classic(tmp_path: Path) -> None:
    """With nothing set, the UI is told `classic` and draws exactly today's shell."""
    app = local_app(tmp_path, auth_mode="off")
    with TestClient(app) as client:
        body = client.get("/auth/me").json()
    assert body["ui_layout"] == "classic"
    # M180 (DEC-1610) added `preferences` (null with sign-in off, but sent); M182 added `brand`; M247 added
    # `data_doctor` (always true since the `data_doctor.unified` switch was removed): all additive.
    keys = {"principal", "auth_mode", "permissions", "ui_layout", "preferences", "brand", "data_doctor"}
    assert set(body) == keys
    assert body["data_doctor"] is True
    assert body["preferences"] is None
