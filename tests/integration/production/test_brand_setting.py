"""Plan M M182 (DEC-1612): the brand as deployment settings, and its delivery to the UI.

Four settings - `brand_name`, `brand_logo_url`, `brand_logo_alt`, `brand_color` - let a deployment draw
its own name, logo and colour. Presentation only, like `ui_layout` (M174): they reach the browser on the
call the UI already makes at start-up, `GET /auth/me`, in a `brand` object, with sign-in on or off. With
nothing set the answer is today's product (name "Marketing AI", the built-in Minfy logo, `#1E57BD`).

A colour that cannot be read is refused at settings load, naming the variable; and so is one that fails
WCAG AA (4.5:1) for white text on it or for it as text on the pages' light backgrounds, naming the ratio.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest
from fastapi.testclient import TestClient

from engine.access.roles import Role
from engine.settings import (
    DEFAULT_BRAND_COLOR,
    DEFAULT_BRAND_LOGO_ALT,
    DEFAULT_BRAND_NAME,
    ENV_VARS,
    Settings,
    SettingsError,
    contrast_ratio,
)
from tests.integration.production.access_support import bearer, local_app, make_user

pytestmark = pytest.mark.integration

ROOT: Final[Path] = Path(__file__).resolve().parents[3]
INDEX_HTML: Final[Path] = ROOT / "ui" / "index.html"
FIELDS: Final[tuple[str, ...]] = ("brand_name", "brand_logo_url", "brand_logo_alt", "brand_color")


def test_the_defaults_are_todays_product() -> None:
    settings = Settings()
    assert (settings.brand_name, settings.brand_logo_url, settings.brand_logo_alt, settings.brand_color) == (
        "Marketing AI",
        "",
        "Minfy",
        "#1E57BD",
    )
    assert Settings.from_env({}).brand_color == DEFAULT_BRAND_COLOR
    assert (DEFAULT_BRAND_NAME, DEFAULT_BRAND_LOGO_ALT) == ("Marketing AI", "Minfy")


def test_the_default_colour_is_the_one_the_page_draws_today() -> None:
    """The default is `--brand-blue` of ui/index.html exactly (so a drift in either is caught here)."""
    token = re.search(r"--brand-blue:(#[0-9A-Fa-f]{6})", INDEX_HTML.read_text(encoding="utf-8"))
    assert token is not None
    assert token.group(1) == DEFAULT_BRAND_COLOR


def test_the_environment_names() -> None:
    assert ENV_VARS["brand_name"] == "MARKETING_AI_BRAND_NAME"
    assert ENV_VARS["brand_logo_url"] == "MARKETING_AI_BRAND_LOGO_URL"
    assert ENV_VARS["brand_logo_alt"] == "MARKETING_AI_BRAND_LOGO_ALT"
    assert ENV_VARS["brand_color"] == "MARKETING_AI_BRAND_COLOR"


def test_the_environment_sets_the_brand() -> None:
    built = Settings.from_env(
        {
            "MARKETING_AI_BRAND_NAME": "Acme Growth",
            "MARKETING_AI_BRAND_LOGO_URL": "https://cdn.example.com/acme.svg",
            "MARKETING_AI_BRAND_LOGO_ALT": "Acme",
            "MARKETING_AI_BRAND_COLOR": "#0b5d3b",
        }
    )
    assert built.brand_name == "Acme Growth"
    assert built.brand_logo_url == "https://cdn.example.com/acme.svg"
    assert built.brand_logo_alt == "Acme"
    assert built.brand_color == "#0B5D3B", "a colour is kept in capitals, as the page's own token is"


def test_an_empty_value_is_the_default() -> None:
    built = Settings.from_env({"MARKETING_AI_BRAND_NAME": "  ", "MARKETING_AI_BRAND_COLOR": ""})
    assert built.brand_name == "Marketing AI"
    assert built.brand_color == "#1E57BD"


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("MARKETING_AI_BRAND_NAME", "x" * 41),
        ("MARKETING_AI_BRAND_NAME", "Acme\nGrowth"),
        ("MARKETING_AI_BRAND_LOGO_URL", "javascript:alert(1)"),
        ("MARKETING_AI_BRAND_LOGO_URL", "data:image/svg+xml;base64,AAAA"),
        ("MARKETING_AI_BRAND_LOGO_URL", "http://cdn.example.com/acme.svg"),
        ("MARKETING_AI_BRAND_LOGO_URL", "//cdn.example.com/acme.svg"),
        ("MARKETING_AI_BRAND_LOGO_URL", "https://cdn.example.com/a b.svg"),
        ("MARKETING_AI_BRAND_LOGO_URL", 'https://cdn.example.com/a".svg'),
        ("MARKETING_AI_BRAND_LOGO_ALT", "x" * 81),
        ("MARKETING_AI_BRAND_COLOR", "blue"),
        ("MARKETING_AI_BRAND_COLOR", "#12F"),
        ("MARKETING_AI_BRAND_COLOR", "1E57BD"),
        ("MARKETING_AI_BRAND_COLOR", "#GG57BD"),
        ("MARKETING_AI_BRAND_COLOR", "#1E57BDFF"),
    ],
)
def test_an_invalid_value_is_refused_naming_the_variable(variable: str, value: str) -> None:
    with pytest.raises(SettingsError) as raised:
        Settings.from_env({variable: value})
    assert raised.value.code == "SETTING_INVALID"
    assert raised.value.env_var == variable
    assert variable in str(raised.value)


def test_a_root_relative_logo_path_is_allowed() -> None:
    assert (
        Settings.from_env({"MARKETING_AI_BRAND_LOGO_URL": "/static/acme.svg"}).brand_logo_url
        == "/static/acme.svg"
    )


# --- the contrast guard ------------------------------------------------------------------------------


def test_the_contrast_ratio_is_wcag() -> None:
    assert contrast_ratio("#000000", "#FFFFFF") == pytest.approx(21.0)
    assert contrast_ratio("#FFFFFF", "#FFFFFF") == pytest.approx(1.0)
    # the page's own brand blue: 5.97:1 on the tint (ui/../a11y/contrast_known.json), higher on white
    assert contrast_ratio("#1E57BD", "#EDF3FD") == pytest.approx(5.97, abs=0.01)


def test_a_passing_colour_is_accepted() -> None:
    assert Settings.from_env({"MARKETING_AI_BRAND_COLOR": "#0B5D3B"}).brand_color == "#0B5D3B"
    assert Settings(brand_color="#1E57BD").brand_color == "#1E57BD"


@pytest.mark.parametrize("colour", ["#FFD500", "#9CA3AF", "#FF0000", "#7FBFFF"])
def test_a_colour_that_fails_aa_is_refused_naming_the_ratio(colour: str) -> None:
    with pytest.raises(SettingsError) as raised:
        Settings.from_env({"MARKETING_AI_BRAND_COLOR": colour})
    assert raised.value.code == "SETTING_INVALID"
    assert raised.value.env_var == "MARKETING_AI_BRAND_COLOR"
    message = str(raised.value)
    assert "4.5" in message and "contrast" in message.lower()
    ratio = re.search(r"(\d+\.\d\d):1", message)
    assert ratio is not None, message
    assert float(ratio.group(1)) < 4.5


def test_the_message_names_the_worst_ground() -> None:
    """A colour that passes on white but fails on the active-item tint is refused, and says so."""
    # Found by search: AA on white, below AA on the tint #EDF3FD.
    colour = "#3B72C8"
    assert contrast_ratio(colour, "#FFFFFF") >= 4.5 > contrast_ratio(colour, "#EDF3FD")
    with pytest.raises(SettingsError) as raised:
        Settings.from_env({"MARKETING_AI_BRAND_COLOR": colour})
    assert "#EDF3FD" in str(raised.value)


def test_the_grounds_are_the_pages_own_tokens() -> None:
    """The backgrounds the guard checks are tokens of ui/index.html; a drift fails here."""
    from engine.settings import BRAND_GROUNDS

    root = re.search(r":root\{([^}]*)\}", INDEX_HTML.read_text(encoding="utf-8"))
    assert root is not None
    tokens = dict(re.findall(r"(--[a-z0-9-]+):(#[0-9A-Fa-f]{6})", root.group(1)))
    assert {name: tokens[name].upper() for name in BRAND_GROUNDS} == dict(BRAND_GROUNDS)


# --- the delivery -------------------------------------------------------------------------------------


def test_auth_me_carries_the_default_brand_with_sign_in_off(tmp_path: Path) -> None:
    app = local_app(tmp_path, auth_mode="off")
    with TestClient(app) as client:
        body = client.get("/auth/me").json()
    assert body["brand"] == {"name": "Marketing AI", "logo_url": "", "logo_alt": "Minfy", "color": "#1E57BD"}


def test_auth_me_carries_a_custom_brand_with_sign_in_off(tmp_path: Path) -> None:
    app = local_app(
        tmp_path,
        auth_mode="off",
        brand_name="Acme Growth",
        brand_logo_url="https://cdn.example.com/acme.svg",
        brand_logo_alt="Acme",
        brand_color="#0B5D3B",
    )
    with TestClient(app) as client:
        response = client.get("/auth/me")
    assert response.status_code == 200, response.text
    assert response.json()["brand"] == {
        "name": "Acme Growth",
        "logo_url": "https://cdn.example.com/acme.svg",
        "logo_alt": "Acme",
        "color": "#0B5D3B",
    }


def test_auth_me_carries_the_brand_to_every_signed_in_role(tmp_path: Path) -> None:
    app = local_app(tmp_path, brand_name="Acme Growth", brand_color="#0B5D3B")
    ids = {role: make_user(app, f"{role.value}-person", (role,)) for role in Role}
    with TestClient(app) as client:
        for role, user_id in ids.items():
            response = client.get("/auth/me", headers=bearer(app, user_id))
            assert response.status_code == 200, response.text
            brand = response.json()["brand"]
            assert (brand["name"], brand["color"]) == ("Acme Growth", "#0B5D3B"), role


def test_the_brand_is_a_deployment_setting_not_a_secret(tmp_path: Path) -> None:
    """Nothing else about the deployment rides along: the object has exactly four fields."""
    app = local_app(tmp_path, auth_mode="off")
    with TestClient(app) as client:
        brand = client.get("/auth/me").json()["brand"]
    assert set(brand) == {"name", "logo_url", "logo_alt", "color"}
