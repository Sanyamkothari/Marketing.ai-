"""Plan M M192 (DEC-1622): Settings › AI service (`#/settings/ai`) in a real browser.

The page is drawn by the real `ui/` against the API answers recorded from the validated Hillstrom demo
(`tests/fixtures/ui_replay.py`; there `GET /ai-service` says neither AI service is connected), in the campaigns layout.
What is checked in Chromium, where the real style sheets apply:

* the page draws with no console error, under the breadcrumb Settings › AI service, and nothing scrolls sideways at 390 px;
* the two cards sit side by side on a desktop and are stacked on a phone;
* each card's Connect button is at least 40px tall, the first is the page's one primary (brand-coloured) button, and on a
  phone it spans the card;
* Settings' row, then the Connect button, reach the key form in two clicks, and the key form's breadcrumb goes back to
  Settings › AI service.

The module skips when playwright or a usable Chromium is missing.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import pytest

from tests.fixtures.ui_replay import ReplayServer

pytestmark = [pytest.mark.slow, pytest.mark.integration]

sync_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright is not installed; the page needs a browser"
)

CHROMIUM: Final[Path] = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")) / "chromium"
SETTLE_MS: Final[int] = 1500
PAGE: Final[str] = "#/settings/ai"


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    if not CHROMIUM.exists():
        pytest.skip(f"no Chromium at {CHROMIUM}; set PLAYWRIGHT_BROWSERS_PATH")
    with sync_api.sync_playwright() as p:
        b = p.chromium.launch(executable_path=str(CHROMIUM))
        yield b
        b.close()


@pytest.fixture(scope="module")
def base() -> Iterator[str]:
    with ReplayServer(ui_layout="campaigns") as server:
        yield server.base


def _open(browser: Any, base: str, fragment: str, width: int, height: int = 900) -> tuple[Any, list[str]]:
    context = browser.new_context(viewport={"width": width, "height": height})
    context.route(
        "**/*", lambda route: route.continue_() if route.request.url.startswith(base) else route.abort()
    )
    page = context.new_page()
    errors: list[str] = []

    def console(msg: Any) -> None:
        # A request this test itself aborts (anything outside the replay server: web fonts) is not the page's error.
        where = str(msg.location.get("url", ""))
        if msg.type == "error" and (not where or where.startswith(base)):
            errors.append(f"{msg.text} ({where})")

    page.on("console", console)
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto(f"{base}/ui/{fragment}", wait_until="load")
    page.wait_for_timeout(SETTLE_MS)
    return page, errors


def _box(page: Any, selector: str) -> dict[str, float]:
    box = page.evaluate(
        "(s) => { const r = document.querySelector(s).getBoundingClientRect();"
        " return { x: r.x, y: r.y, w: r.width, h: r.height, r: r.right, b: r.bottom }; }",
        selector,
    )
    assert isinstance(box, dict)
    return {k: float(v) for k, v in box.items()}


def test_on_a_desktop_the_two_services_sit_side_by_side_under_settings_ai_service(
    browser: Any, base: str
) -> None:
    page, errors = _open(browser, base, PAGE, 1440)
    try:
        assert errors == [], f"console errors: {errors}"
        crumbs = page.evaluate(
            "() => [...document.querySelectorAll('main .crumbs a, main .crumbs .cur')].map(e => e.textContent.trim())"
        )
        assert crumbs == ["Settings", "AI service"]
        product = _box(page, "[data-ai-svc-card='product']")
        deliverable = _box(page, "[data-ai-svc-card='deliverable']")
        assert abs(product["y"] - deliverable["y"]) < 2, "side by side"
        assert deliverable["x"] >= product["r"], "the second to the right of the first"
        assert abs(product["h"] - deliverable["h"]) < 2, "the same height"
        primary = page.locator("main .btn.primary")
        assert primary.count() == 1, "the page's one primary button"
        assert primary.get_attribute("href") == "#/connections/ai/product"
        assert _box(page, "[data-ai-svc-go='product']")["h"] >= 40
        background = page.evaluate(
            "() => getComputedStyle(document.querySelector('main .btn.primary')).backgroundColor"
        )
        assert background == "rgb(30, 87, 189)", f"brand blue, not {background}"
        assert page.locator("[data-ai-svc-same]").inner_text().strip() == "You can use the same key for both."
    finally:
        page.context.close()


def test_on_a_phone_the_cards_stack_and_nothing_scrolls_sideways(browser: Any, base: str) -> None:
    page, errors = _open(browser, base, PAGE, 390, 844)
    try:
        assert errors == [], f"console errors: {errors}"
        sideways = page.evaluate("() => document.documentElement.scrollWidth > window.innerWidth + 1")
        assert not sideways, "the page scrolls sideways at 390 px"
        product = _box(page, "[data-ai-svc-card='product']")
        deliverable = _box(page, "[data-ai-svc-card='deliverable']")
        assert deliverable["y"] >= product["b"], "stacked"
        assert abs(product["w"] - deliverable["w"]) < 2
        go = _box(page, "[data-ai-svc-go='product']")
        assert go["h"] >= 40, "a 40px touch target"
        assert go["w"] >= product["w"] - 2 * 16 - 2, "the button spans the card on a phone"
    finally:
        page.context.close()


def test_two_clicks_from_settings_to_the_key_form_and_its_breadcrumb_goes_back(
    browser: Any, base: str
) -> None:
    page, errors = _open(browser, base, "#/settings", 1440)
    try:
        page.click("[data-settings-main] a[data-settings-ai]")
        page.wait_for_selector("[data-ai-svc-card='product']")
        page.click("[data-ai-svc-go='product']")
        page.wait_for_selector("[data-ai-slot='product'] .crumbs")
        assert page.evaluate("() => location.hash") == "#/connections/ai/product"
        crumbs = page.evaluate(
            "() => [...document.querySelectorAll('main .crumbs a')].map(a => [a.textContent.trim(), a.getAttribute('href')])"
        )
        assert crumbs == [["Settings", "#/settings"], ["AI service", "#/settings/ai"]]
        page.click("main .crumbs a[href='#/settings/ai']")
        page.wait_for_selector("[data-ai-svc-card='product']")
        assert errors == [], f"console errors: {errors}"
    finally:
        page.context.close()
