"""Plan M M182 (DEC-1612): the brand in a real browser.

The app runs under uvicorn three times on an empty data directory: with nothing set (today's product), with the four
brand settings and the campaigns layout, and with the four brand settings and the classic layout. The settings reach the
browser the way they do in production, on the real `GET /auth/me`. Every other GET the pages make is answered from the
recorded Hillstrom demo (`tests/fixtures/ui_demo/api.json`) when it holds it, as `test_shell_browser.py` does.

What is checked in Chromium, where the real style sheet applies:

* the accent colour: `--brand-blue` is the brand's colour on the page, the active place, the focus ring and the tab's
  icon are drawn in it, and the built-in Minfy logo keeps Minfy's own blue;
* the name and logo: the wordmark, the logo image (loaded, with its alt text) and the link's name; the tab's title ends
  with the brand's name after a page change;
* the last brand is remembered: with `GET /auth/me` refused (as it is before sign-in), a reload still draws it;
* the default server draws today's product, with nothing set on <html>.

The module skips when playwright or a usable browser is missing.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Generator, Iterator
from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qsl, urlencode, urlparse

import pytest

pytestmark = [pytest.mark.slow, pytest.mark.integration]

sync_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright is not installed; the brand needs a browser"
)

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
RECORDED: Final[Path] = REPO_ROOT / "tests" / "fixtures" / "ui_demo" / "api.json"
HOST: Final[str] = "127.0.0.1"
BRAND: Final[dict[str, str]] = {
    "MARKETING_AI_BRAND_NAME": "Acme Growth",
    "MARKETING_AI_BRAND_LOGO_URL": "https://cdn.example.com/acme-logo.svg",
    "MARKETING_AI_BRAND_LOGO_ALT": "Acme",
    "MARKETING_AI_BRAND_COLOR": "#0b5d3b",
}
GREEN: Final[str] = "rgb(11, 93, 59)"
MINFY_BLUE: Final[str] = "rgb(30, 87, 189)"
LOGO_SVG: Final[str] = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 32"><rect width="120" height="32" fill="#0B5D3B"/></svg>'
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((HOST, 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def shots(tmp_path_factory: pytest.TempPathFactory) -> Path:
    target = Path(os.environ.get("M182_SHOTS") or tmp_path_factory.mktemp("m182-shots"))
    target.mkdir(parents=True, exist_ok=True)
    return target


def _serve(workdir: Path, extra: dict[str, str]) -> Generator[str, None, None]:
    data = workdir / "data"
    data.mkdir(parents=True)
    port = _free_port()
    base = f"http://{HOST}:{port}"
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("MARKETING_AI_")},
        "MARKETING_AI_DATA_DIR": str(data),
        "PYTHONPATH": str(REPO_ROOT),
        "PYTHONUNBUFFERED": "1",
        **extra,
    }
    log = workdir / "server.log"
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "api.main:app",
        "--host",
        HOST,
        "--port",
        str(port),
        "--log-level",
        "warning",
    ]
    with log.open("wb") as sink:
        process = subprocess.Popen(command, cwd=REPO_ROOT, env=env, stdout=sink, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 120
            while True:
                if process.poll() is not None:
                    pytest.fail(
                        f"the server exited with {process.returncode}:\n{log.read_text(errors='replace')[-3000:]}"
                    )
                try:
                    with urllib.request.urlopen(f"{base}/healthz", timeout=5) as response:
                        if response.status == 200:
                            break
                except OSError:
                    pass
                if time.monotonic() > deadline:
                    pytest.fail("/healthz never answered")
                time.sleep(0.3)
            yield base
        finally:
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:  # pragma: no cover
                process.kill()


@pytest.fixture(scope="module")
def default_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    yield from _serve(tmp_path_factory.mktemp("m182-default"), {})


@pytest.fixture(scope="module")
def campaigns_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    yield from _serve(
        tmp_path_factory.mktemp("m182-campaigns"), {**BRAND, "MARKETING_AI_UI_LAYOUT": "campaigns"}
    )


@pytest.fixture(scope="module")
def classic_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    yield from _serve(tmp_path_factory.mktemp("m182-classic"), dict(BRAND))


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    playwright = sync_api.sync_playwright().start()
    try:
        try:
            launched = playwright.chromium.launch(headless=True)
        except sync_api.Error as error:
            pytest.skip(f"no usable chromium for playwright: {error}")
        try:
            yield launched
        finally:
            launched.close()
    finally:
        playwright.stop()


RECORDING: Final[dict[str, Any]] = json.loads(RECORDED.read_text(encoding="utf-8"))


def _key(url: str) -> str:
    parsed = urlparse(url)
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    return f"GET {parsed.path}{'?' + query if query else ''}"


def _replay(route: Any, request: Any) -> None:
    """The recorded demo's answer for a GET it holds (never `/auth/me`: the real app says the brand)."""
    key = _key(request.url)
    hit = RECORDING.get(key) if request.method == "GET" and "/auth/me" not in key else None
    if hit is None:
        route.continue_()
        return
    body = hit["body"]
    route.fulfill(
        status=hit["status"],
        content_type=hit.get("content_type") or "application/json",
        body=body if isinstance(body, str) else json.dumps(body),
    )


def _context(browser: Any, base: str, problems: list[str], *, width: int = 1440, height: int = 900) -> Any:
    context = browser.new_context(viewport={"width": width, "height": height})
    context.route("https://fonts.googleapis.com/**", lambda route: route.abort())
    context.route("https://fonts.gstatic.com/**", lambda route: route.abort())
    context.route(
        "https://cdn.example.com/**",
        lambda route: route.fulfill(status=200, content_type="image/svg+xml", body=LOGO_SVG),
    )
    context.route(f"{base}/**", _replay)
    context.on("page", lambda page: page.on("pageerror", lambda error: problems.append(f"uncaught: {error}")))
    return context


def _open(page: Any, base: str, hash_: str) -> None:
    page.goto(f"{base}/ui/{hash_}")
    page.wait_for_load_state("networkidle")


def test_the_default_server_is_todays_product(browser: Any, default_server: str, shots: Path) -> None:
    problems: list[str] = []
    context = _context(browser, default_server, problems)
    page = context.new_page()
    _open(page, default_server, "#/settings")
    page.wait_for_selector("#pb-bar .wordmark")
    assert page.locator("#pb-bar .wordmark").inner_html() == "Marketing AI"
    assert page.locator("#pb-bar a.tb-logo").get_attribute("aria-label") == "Minfy, home"
    assert page.locator("#pb-bar a.tb-logo svg.logo").count() == 1
    assert page.evaluate("document.documentElement.hasAttribute('style')") is False
    assert (
        page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--brand-blue').trim()")
        == "#1E57BD"
    )
    assert page.title().endswith(" · Marketing AI"), page.title()
    assert page.evaluate("localStorage.getItem('marketing-ai.brand')") is None
    page.screenshot(path=str(shots / "default_1440_settings.png"))
    assert problems == []
    context.close()


def test_the_campaigns_shell_draws_the_brand(browser: Any, campaigns_server: str, shots: Path) -> None:
    problems: list[str] = []
    context = _context(browser, campaigns_server, problems)
    page = context.new_page()
    _open(page, campaigns_server, "#/settings")
    page.wait_for_selector("#pb-bar .sh-side")
    assert page.locator("#pb-bar .wordmark").inner_text().strip() == "Acme Growth"
    link = page.locator("#pb-bar a.tb-logo")
    assert link.get_attribute("aria-label") == "Acme, home"
    logo = link.locator("img.logo-img")
    assert logo.get_attribute("alt") == "Acme"
    page.wait_for_function("document.querySelector('#pb-bar img.logo-img').complete")
    assert page.evaluate("document.querySelector('#pb-bar img.logo-img').naturalWidth") > 0, "the logo loaded"
    assert page.locator("#pb-bar svg.logo").count() == 0
    # the accent colour, in the real style sheet
    assert (
        page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--brand-blue').trim()")
        == "#0B5D3B"
    )
    active = page.locator("#pb-bar a.sh-item[aria-current='page']")
    assert active.evaluate("el => getComputedStyle(el).color") == GREEN
    page.locator("#pb-bar a.wordmark").focus()
    page.keyboard.press("Tab")
    assert (
        page.evaluate("getComputedStyle(document.activeElement).outlineColor") == GREEN
    ), "the focus ring is the brand's"
    assert (
        page.evaluate("document.querySelector('link[rel=icon]').getAttribute('href')").count("%230B5D3B") == 1
    )
    # the tab's title, whoever set it
    assert page.title() == "Settings · Acme Growth", page.title()
    page.locator("#pb-bar a.sh-item[href='#/campaigns']").click()
    page.wait_for_function("document.title.startsWith('Campaigns')")
    assert page.title().endswith(" · Acme Growth"), page.title()
    page.screenshot(path=str(shots / "campaigns_1440_brand.png"))
    # at 390 px the folded bar still fits one row with the brand's name
    page.set_viewport_size({"width": 390, "height": 844})
    bar = page.locator("#pb-bar .sh-row").bounding_box()
    assert bar is not None and bar["height"] <= 60, f"the bar fits one row at 390 px ({bar})"
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    page.screenshot(path=str(shots / "campaigns_390_brand.png"))
    assert problems == []
    context.close()


def test_the_classic_bar_draws_the_brand(browser: Any, classic_server: str, shots: Path) -> None:
    problems: list[str] = []
    context = _context(browser, classic_server, problems)
    page = context.new_page()
    _open(page, classic_server, "#/settings")
    page.wait_for_selector("#pb-bar .topnav")
    assert page.locator("#pb-bar .wordmark").inner_text().strip() == "Acme Growth"
    assert page.locator("#pb-bar a.tb-logo").get_attribute("aria-label") == "Acme, home"
    page.wait_for_function("document.querySelector('#pb-bar img.logo-img').complete")
    assert page.evaluate("document.querySelector('#pb-bar img.logo-img').naturalWidth") > 0
    assert GREEN in page.locator("#pb-bar .tn-item.on").evaluate("el => getComputedStyle(el).boxShadow")
    page.screenshot(path=str(shots / "classic_1440_brand.png"))
    assert problems == []
    context.close()


def test_the_brand_is_remembered_when_the_answer_cannot_be_asked(browser: Any, classic_server: str) -> None:
    """Before sign-in `GET /auth/me` answers 401 and carries no brand: the page still draws the last one it knew."""
    problems: list[str] = []
    context = _context(browser, classic_server, problems)
    page = context.new_page()
    _open(page, classic_server, "#/settings")
    page.wait_for_selector("#pb-bar .topnav")
    assert page.evaluate("localStorage.getItem('marketing-ai.brand')") is not None
    context.route(
        f"{classic_server}/auth/me",
        lambda route: route.fulfill(
            status=401,
            content_type="application/json",
            body=json.dumps(
                {"detail": {"code": "AUTH_REQUIRED", "message": "Sign in to do this.", "path": None}}
            ),
        ),
    )
    page.reload()
    page.wait_for_load_state("networkidle")
    assert page.locator("#pb-bar .wordmark").inner_text().strip() == "Acme Growth"
    assert (
        page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--brand-blue').trim()")
        == "#0B5D3B"
    )
    assert problems == []
    context.close()


def test_the_built_in_logo_keeps_minfys_blue_when_only_the_colour_changes(
    browser: Any, tmp_path: Path
) -> None:
    """A deployment that changes the colour but not the logo: links are its colour, the Minfy mark stays Minfy's."""
    server = _serve(tmp_path, {"MARKETING_AI_BRAND_COLOR": "#0B5D3B"})
    base = next(server)
    try:
        problems: list[str] = []
        context = _context(browser, base, problems)
        page = context.new_page()
        _open(page, base, "#/settings")
        page.wait_for_selector("#pb-bar svg.logo")
        assert (
            page.evaluate(
                "getComputedStyle(document.documentElement).getPropertyValue('--brand-blue').trim()"
            )
            == "#0B5D3B"
        )
        assert (
            page.evaluate("getComputedStyle(document.querySelector('#pb-bar svg.logo .b')).fill")
            == MINFY_BLUE
        )
        assert page.locator("#pb-bar .wordmark").inner_html() == "Marketing AI"
        assert problems == []
        context.close()
    finally:
        server.close()
