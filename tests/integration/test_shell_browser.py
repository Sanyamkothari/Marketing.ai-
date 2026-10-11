"""Plan M M174 (DEC-1604): the new shell in a real browser, both layouts.

The app runs under uvicorn twice, on an empty data directory: once with nothing set (`ui_layout` is `classic`) and
once with `MARKETING_AI_UI_LAYOUT=campaigns`. The setting reaches the browser the way it does in production, on
the real `GET /auth/me`. Every other GET the pages make is answered from the recorded Hillstrom demo
(`tests/fixtures/ui_demo/api.json`, M170) when the recording has it, so the screenshots show a filled product
rather than an empty one; anything else goes to the real app.

What is checked:

* classic: today's top bar (Home, Connections, Results, Settings) and no sidebar;
* campaigns at 1440 x 900: the five places in a left sidebar beside the page, the route's place marked, the bell
  and Help top right; the keyboard path from the skip link through the wordmark, the five places, the bell and
  Help, each with a visible focus ring;
* campaigns at 390 px: the sidebar folded behind Menu, no horizontal scroll, Menu opens it, choosing a place
  navigates and folds it again;
* no uncaught error on any of it.

Screenshots of both layouts go to `$M174_SHOTS` when set (Plan M keeps a set for visual review), else to the
test's temporary directory. The module skips when playwright or a usable browser is missing.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qsl, urlencode, urlparse

import pytest

pytestmark = [pytest.mark.slow, pytest.mark.integration]

sync_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright is not installed; the shell needs a browser"
)

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
RECORDED: Final[Path] = REPO_ROOT / "tests" / "fixtures" / "ui_demo" / "api.json"
HOST: Final[str] = "127.0.0.1"
PLACES: Final[list[str]] = ["Overview", "Campaigns", "Data", "Reports", "Settings"]
VALUE_PAGE: Final[str] = "#/pilot/value/r_20261010_11100004"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((HOST, 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def shots(tmp_path_factory: pytest.TempPathFactory) -> Path:
    target = Path(os.environ.get("M174_SHOTS") or tmp_path_factory.mktemp("m174-shots"))
    target.mkdir(parents=True, exist_ok=True)
    return target


def _serve(workdir: Path, layout: str | None) -> Iterator[str]:
    data = workdir / "data"
    data.mkdir(parents=True)
    port = _free_port()
    base = f"http://{HOST}:{port}"
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("MARKETING_AI_")},
        "MARKETING_AI_DATA_DIR": str(data),
        "PYTHONPATH": str(REPO_ROOT),
        "PYTHONUNBUFFERED": "1",
    }
    if layout is not None:
        env["MARKETING_AI_UI_LAYOUT"] = layout
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
def classic_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    yield from _serve(tmp_path_factory.mktemp("m174-classic"), None)


@pytest.fixture(scope="module")
def campaigns_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    yield from _serve(tmp_path_factory.mktemp("m174-campaigns"), "campaigns")


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
    """The recorded demo's answer for a GET it holds (never `/auth/me`: the real app says the layout)."""
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


def _page(browser: Any, base: str, *, width: int, height: int, problems: list[str]) -> Any:
    context = browser.new_context(viewport={"width": width, "height": height})
    context.route("https://fonts.googleapis.com/**", lambda route: route.abort())
    context.route("https://fonts.gstatic.com/**", lambda route: route.abort())
    context.route(f"{base}/**", _replay)
    page = context.new_page()
    page.on("pageerror", lambda error: problems.append(f"uncaught: {error}"))
    return page


def _open(page: Any, base: str, hash_: str) -> None:
    page.goto(f"{base}/ui/{hash_}")
    page.wait_for_load_state("networkidle")


def test_classic_is_todays_top_bar(browser: Any, classic_server: str, shots: Path) -> None:
    problems: list[str] = []
    page = _page(browser, classic_server, width=1440, height=900, problems=problems)
    _open(page, classic_server, "#/settings")
    page.wait_for_selector("#pb-bar .topnav")
    assert page.locator("#pb-bar nav[aria-label='Main'] .tn-item").all_inner_texts() == [
        "Home",
        "Connections",
        "Results",
        "Settings",
    ]
    assert page.locator(".sh-side").count() == 0
    assert page.evaluate("document.documentElement.hasAttribute('data-ui-layout')") is False
    page.screenshot(path=str(shots / "classic_1440_settings.png"))
    _open(page, classic_server, VALUE_PAGE)
    page.wait_for_selector("text=Value of one extra customer", state="attached")
    page.screenshot(path=str(shots / "classic_1440_value.png"))
    assert problems == []
    page.context.close()


def test_campaigns_sidebar_and_keyboard_at_1440(browser: Any, campaigns_server: str, shots: Path) -> None:
    problems: list[str] = []
    page = _page(browser, campaigns_server, width=1440, height=900, problems=problems)
    _open(page, campaigns_server, "#/settings")
    page.wait_for_selector("#pb-bar .sh-side")
    side = page.locator("#pb-bar nav.sh-side[aria-label='Main']")
    assert side.is_visible()
    assert [t.strip() for t in side.locator("a.sh-item").all_inner_texts()] == PLACES
    assert side.locator("a[aria-current='page']").inner_text().strip() == "Settings"
    assert page.locator("#pb-bar .topnav").count() == 0
    assert page.locator("#pb-bar .sh-menu-btn").is_hidden(), "no Menu button on a wide screen"
    # the sidebar sits left of the page, which makes room for it
    box = side.bounding_box()
    screen = page.locator("#app .screen").first.bounding_box()
    assert box is not None and screen is not None
    assert box["x"] == 0 and box["width"] == 216
    assert screen["x"] >= box["width"], "the page is not under the sidebar"
    assert page.locator("#pb-bar a.sh-bell").is_visible()
    assert page.locator("#pb-bar button[data-menu='sh-help']").is_visible()
    page.screenshot(path=str(shots / "campaigns_1440_settings.png"))

    # The keyboard path: the skip link, then the wordmark, the five places, the bell and Help - each with a ring.
    page.evaluate("document.activeElement && document.activeElement.blur()")
    page.locator("a.skip").focus()
    path: list[str] = []
    rings: list[str] = []
    for _ in range(8):
        page.keyboard.press("Tab")
        path.append(
            page.evaluate(
                "(() => { const el = document.activeElement; return el.getAttribute('aria-label') || el.textContent.trim(); })()"
            )
        )
        rings.append(page.evaluate("getComputedStyle(document.activeElement).outlineStyle"))
    assert path == ["Marketing AI", *PLACES, "Waiting for approval", "Help"], path
    assert all(ring == "solid" for ring in rings), rings
    # Enter on a place follows it, and the new place is marked
    page.locator("#pb-bar .sh-side a[href='#/settings']").focus()
    page.keyboard.press("Shift+Tab")
    assert page.evaluate("document.activeElement.textContent.trim()") == "Reports"
    page.locator("#pb-bar a.sh-bell").focus()
    page.keyboard.press("Enter")
    page.wait_for_function("location.hash === '#/approvals'")
    page.wait_for_selector("#pb-bar a.sh-bell[aria-current='page']")
    _open(page, campaigns_server, VALUE_PAGE)
    page.wait_for_selector("text=Value of one extra customer", state="attached")
    assert side.locator("a[aria-current='page']").inner_text().strip() == "Reports"
    page.screenshot(path=str(shots / "campaigns_1440_value.png"))
    page.locator("#pb-bar button[data-menu='sh-help']").click()
    assert page.locator("#sh-help").is_visible()
    page.screenshot(path=str(shots / "campaigns_1440_help_open.png"))
    page.keyboard.press("Escape")
    assert page.locator("#sh-help").is_hidden()
    assert problems == []
    page.context.close()


def test_campaigns_folds_behind_menu_at_390(browser: Any, campaigns_server: str, shots: Path) -> None:
    problems: list[str] = []
    page = _page(browser, campaigns_server, width=390, height=844, problems=problems)
    _open(page, campaigns_server, VALUE_PAGE)
    page.wait_for_selector("text=Value of one extra customer", state="attached")
    menu = page.locator("#pb-bar .sh-menu-btn")
    assert menu.is_visible()
    assert page.locator("#pb-bar .sh-side").is_hidden()
    assert page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    ), "no sideways scroll"
    bar = page.locator("#pb-bar .sh-row").bounding_box()
    assert bar is not None and bar["height"] <= 60, f"the bar fits one row at 390 px ({bar})"
    page.screenshot(path=str(shots / "campaigns_390_closed.png"))
    menu.click()
    side = page.locator("#pb-bar .sh-side")
    assert side.is_visible()
    assert menu.get_attribute("aria-expanded") == "true"
    assert [t.strip() for t in side.locator("a.sh-item").all_inner_texts()] == PLACES
    page.screenshot(path=str(shots / "campaigns_390_menu_open.png"))
    side.locator("a[href='#/settings']").click()
    page.wait_for_function("location.hash === '#/settings'")
    assert side.is_hidden(), "choosing a place folds the sidebar"
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    assert problems == []
    page.context.close()
