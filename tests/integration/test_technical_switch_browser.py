"""Plan M M180 (DEC-1610): "Show technical details" in a real browser, with sign-in off (the app's default).

The real app runs under uvicorn on an empty data directory; Chromium opens Settings, where the switch is a labelled
checkbox. It starts on (the classic layout's default), is turned off with the keyboard (Tab to it, Space), and the
choice survives a reload because this browser keeps it (localStorage): no request saves it, since with sign-in off
there is no person on the server to keep it for. With it off, <html> carries the class whose one CSS rule hides
every technical mark, which is checked on a planted section of each kind through the computed style the browser
really applies. Turned back on, the page is as it was.

Skips, rather than fails, when playwright or a usable Chromium is missing.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import pytest

pytestmark = pytest.mark.integration

sync_api = pytest.importorskip("playwright.sync_api", reason="the switch check needs a browser")

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1].parent
HOST: Final[str] = "127.0.0.1"
STORAGE_KEY: Final[str] = "marketing-ai.show-technical"
SWITCH: Final[str] = "[data-settings-view] input[data-tech-switch]"
PLANTED: Final[str] = """
const holder = document.createElement('div');
holder.id = 'planted';
holder.innerHTML = '<details class="tech" open><summary>Technical details</summary><p>a</p></details>'
  + '<a href="#/uc/x/model/r" data-technical>Model</a>'
  + '<details open><summary>Technical details</summary><p>b</p></details>'
  + '<p id="plain">Customers to contact</p>';
document.body.appendChild(holder); // outside #app, which Settings repaints once the version is in
"""
SHOWN: Final[str] = (
    "() => [...document.querySelectorAll('#planted > *')].map((el) => getComputedStyle(el).display !== 'none')"
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((HOST, 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    data = tmp_path_factory.mktemp("tech-switch-data")
    log = tmp_path_factory.mktemp("tech-switch-server") / "server.log"
    env = {
        **os.environ,
        "MARKETING_AI_DATA_DIR": str(data),
        "PYTHONPATH": str(REPO_ROOT),
        "PYTHONUNBUFFERED": "1",
    }
    for name in ("MARKETING_AI_DEMO_MODE", "MARKETING_AI_AUTH_MODE", "MARKETING_AI_UI_LAYOUT"):
        env.pop(name, None)
    port = _free_port()
    base = f"http://{HOST}:{port}"
    command = [sys.executable, "-m", "uvicorn", "api.main:app", "--host", HOST, "--port", str(port)]
    with log.open("wb") as sink:
        process = subprocess.Popen(
            [*command, "--log-level", "warning"],
            cwd=REPO_ROOT,
            env=env,
            stdout=sink,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 120
            while True:
                if process.poll() is not None:
                    pytest.fail(f"the server exited: {log.read_text(errors='replace')[-2000:]}")
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
            except subprocess.TimeoutExpired:  # pragma: no cover - a wedged server
                process.kill()


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    playwright = sync_api.sync_playwright().start()
    try:
        try:
            chromium = playwright.chromium.launch(headless=True)
        except sync_api.Error as error:
            pytest.skip(f"no usable chromium for playwright: {error}")
        yield chromium
        chromium.close()
    finally:
        playwright.stop()


def test_the_switch_on_settings_is_remembered_by_this_browser_across_a_reload(
    server: str, browser: Any
) -> None:
    context = browser.new_context()
    page = context.new_page()
    saves: list[str] = []
    page.on("request", lambda r: saves.append(r.url) if "/auth/me/preferences" in r.url else None)
    page.goto(f"{server}/ui/#/settings")
    switch = page.locator(SWITCH)
    switch.wait_for(state="visible", timeout=30_000)
    # Settings draws twice (again once the engine version is in): act on the second drawing.
    page.locator("[data-settings-about] .mono").wait_for(timeout=30_000)
    page.wait_for_load_state("networkidle")  # every module loaded: no later redraw takes the focus away
    assert page.get_by_label("Show technical details").count() == 1, "the checkbox is named by its label"
    assert switch.is_checked(), "the classic layout's default is on"
    assert page.evaluate("document.documentElement.classList.contains('hide-tech')") is False

    page.evaluate(PLANTED)
    assert page.evaluate(SHOWN) == [True, True, True, True]

    # The keyboard: focus moves to the checkbox, Space turns it off.
    switch.focus()
    assert page.evaluate("document.activeElement.matches('input[data-tech-switch]')")
    page.keyboard.press("Space")
    page.wait_for_function("document.documentElement.classList.contains('hide-tech')")
    assert not switch.is_checked()
    assert page.evaluate(SHOWN) == [
        False,
        False,
        False,
        True,
    ], "every technical mark hidden; plain text stays"
    assert page.evaluate(f"localStorage.getItem('{STORAGE_KEY}')") == "off"

    page.reload()
    page.locator("[data-settings-about] .mono").wait_for(timeout=30_000)
    assert not page.locator(SWITCH).is_checked(), "the choice survived the reload"
    assert page.evaluate("document.documentElement.classList.contains('hide-tech')")

    page.locator(SWITCH).click()
    page.wait_for_function("!document.documentElement.classList.contains('hide-tech')")
    page.reload()
    page.locator("[data-settings-about] .mono").wait_for(timeout=30_000)
    assert page.locator(SWITCH).is_checked()
    assert saves == [], "with sign-in off nothing is sent to the server"
    context.close()
