"""Plan M M187 (DEC-1617): the look of the "New campaign" wizard, in jsdom and in a real browser.

* `production/ui/campaigns/wizard/wizard_look.test.mjs` draws every screen with the wizard's own builders and reads
  the markup: the design system's header, stepper, chips, stat tiles, value range, attention items and buttons; a line
  icon per goal and the stage as a quiet tag; the Review summary; Send's success state.
* In Chromium, on the recorded Hillstrom demo (`tests/fixtures/ui_replay.py`, campaigns layout), where the real style
  sheets apply: the stepper's circles and line, the goal cards (icon, lift on hover), the Review summary (three tiles,
  the range bar), Send's success state, at 1440 px and at 390 px (the phone line "Step 1 of 5 · Goal", one column,
  full-width buttons inside their card, no sideways scroll), and a deployment's own brand colour recolouring every
  brand-derived thing the wizard adds. The demo's campaign has its results already, so it is answered as a Draft for
  Review and as just sent for Send, exactly as `wizard_visible_text.test.mjs` does.

The browser part skips when playwright or a usable browser is missing.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import pytest

from tests.fixtures.node import skip_without_jsdom
from tests.fixtures.ui_replay import ReplayServer, has_campaigns_layout

pytestmark = pytest.mark.integration

NODE_DIR: Final[Path] = Path(__file__).resolve().parent / "production" / "ui"
LOOK_TEST: Final[Path] = NODE_DIR / "campaigns" / "wizard" / "wizard_look.test.mjs"
CAMPAIGN: Final[str] = "c_20261011_c9276210"
UC: Final[str] = "hillstrom-email"
GREEN: Final[str] = "rgb(11, 93, 59)"
DESKTOP: Final[dict[str, int]] = {"width": 1440, "height": 900}
PHONE: Final[dict[str, int]] = {"width": 390, "height": 844}


def test_the_wizard_look_in_jsdom() -> None:
    node = skip_without_jsdom(NODE_DIR)
    result = subprocess.run(
        [node, "--test", str(LOOK_TEST)],
        cwd=NODE_DIR,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-8000:] + result.stderr[-3000:]
    assert "# fail 0" in result.stdout


# --- in a real browser ----------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def server() -> Iterator[str]:
    if not has_campaigns_layout():  # pragma: no cover - a checkout before M174
        pytest.skip("the campaigns layout is not in this tree")
    with ReplayServer(ui_layout="campaigns") as replay:
        yield replay.base


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
    playwright = sync_api.sync_playwright().start()
    try:
        launched = None
        for options in ({}, {"executable_path": "/opt/pw-browsers/chromium"}):
            try:
                launched = playwright.chromium.launch(headless=True, **options)
                break
            except sync_api.Error:
                continue
        if launched is None:
            pytest.skip("no usable chromium for playwright")
        try:
            yield launched
        finally:
            launched.close()
    finally:
        playwright.stop()


def _page(browser: Any, base: str, problems: list[str], *, viewport: dict[str, int], mode: str = "") -> Any:
    """A page on the replay; with `mode` the demo's campaign is answered as a Draft or as just sent (live)."""
    context = browser.new_context(viewport=viewport)
    context.route(lambda url: not url.startswith(base), lambda route: route.abort())
    if mode:

        def answer(route: Any) -> None:
            body = route.fetch().json()
            body["report"] = None
            body["campaign"] = {**body["campaign"], "status": "live"}
            if mode == "draft":
                body["plan"] = None
                body["campaign"] = {**body["campaign"], "test_plan_hash": None, "test_plan_version": None}
            route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

        context.route(lambda url: url.endswith(f"/campaigns/{CAMPAIGN}"), answer)
    page = context.new_page()
    page.on("pageerror", lambda error: problems.append(f"uncaught: {error}"))
    return page


def _open(page: Any, base: str, hash_: str, ready: str) -> None:
    page.goto(f"{base}/ui/{hash_}")
    page.locator(ready).first.wait_for(timeout=20_000)
    page.wait_for_timeout(300)


def _close(page: Any) -> None:
    """Close the page's context, letting the answers still on their way (the page polls) go."""
    page.context.unroute_all(behavior="ignoreErrors")
    page.context.close()


def _sideways(page: Any) -> bool:
    return bool(page.evaluate("() => document.documentElement.scrollWidth > window.innerWidth + 1"))


def _style(page: Any, selector: str, prop: str) -> str:
    return str(
        page.evaluate(
            "([s, p]) => getComputedStyle(document.querySelector(s)).getPropertyValue(p)", [selector, prop]
        )
    )


def test_goal_step_desktop_and_phone(browser: Any, server: str) -> None:
    problems: list[str] = []
    page = _page(browser, server, problems, viewport=DESKTOP)
    _open(page, server, "#/campaigns/new", "[data-cw-goals] .cw-goal")
    # the stepper: five circles in one row, the current one a brand ring, labels under the circles
    dots = page.locator(".cw ol.stepper .st-dot")
    assert dots.count() == 5
    boxes = [dots.nth(i).bounding_box() for i in range(5)]
    assert all(abs(b["y"] - boxes[0]["y"]) < 1 for b in boxes), "one row"
    assert all(round(b["width"]) == 32 for b in boxes)
    brand = _style(page, ".cw [data-cw-stepper=goal] .st-dot", "border-top-color")
    assert brand == "rgb(30, 87, 189)", "the current step is a brand ring"
    assert page.locator(".stepper-phone").is_hidden(), "the phone line is for the phone"
    # the goal cards: four across, an icon each, a lift on hover
    cards = page.locator("[data-cw-goals] > .cw-goal")
    tops = {round(cards.nth(i).bounding_box()["y"]) for i in range(min(4, cards.count()))}
    assert len(tops) == 1, "four goal cards across on a wide screen"
    first = cards.first
    assert first.locator(".cw-goal-icon svg").is_visible()
    first.hover()
    page.wait_for_timeout(400)
    assert "matrix(1, 0, 0, 1, 0, -2)" in str(first.evaluate("e => getComputedStyle(e).transform"))
    assert page.locator("#app .btn.primary").count() == 0
    _close(page)

    phone = _page(browser, server, problems, viewport=PHONE)
    _open(phone, server, "#/campaigns/new", "[data-cw-goals] .cw-goal")
    assert phone.locator(".stepper-phone").inner_text() == "Step 1 of 5 · Goal"
    assert phone.locator(".cw .stepper .st-label").first.bounding_box()["width"] <= 1, "labels fold away"
    cards = phone.locator("[data-cw-goals] > .cw-goal")
    lefts = {round(cards.nth(i).bounding_box()["x"]) for i in range(cards.count())}
    assert len(lefts) == 1, "one column of goals on a phone"
    card = cards.first.bounding_box()
    name = cards.first.locator(".cw-goal-name").bounding_box()
    icon = cards.first.locator(".cw-goal-icon").bounding_box()
    assert name["x"] > icon["x"] + icon["width"], "the name sits beside its icon, not squeezed under it"
    assert card["width"] >= PHONE["width"] - 40
    assert not _sideways(phone)
    _close(phone)
    assert problems == [], problems


def test_review_summary_and_phone_buttons(browser: Any, server: str) -> None:
    problems: list[str] = []
    page = _page(browser, server, problems, viewport=DESKTOP, mode="draft")
    _open(page, server, f"#/campaigns/new/review/{CAMPAIGN}", "[data-cw-review] [data-cw-approve]")
    tiles = page.locator(".cw .stat-row[data-cw-summary] > .stat")
    assert tiles.count() == 3
    widths = [tiles.nth(i).bounding_box()["width"] for i in range(3)]
    assert widths[2] > widths[0] * 1.6, "the expected value has room for its range"
    assert page.locator("#app .btn.primary").count() == 1
    assert _style(page, "[data-cw-approve]", "background-color") == "rgb(30, 87, 189)"
    current = page.locator(".cw ol.stepper .st-step.current")
    assert current.count() == 2, "Audience and Review are both on screen"
    _close(page)

    phone = _page(browser, server, problems, viewport=PHONE, mode="draft")
    _open(phone, server, f"#/campaigns/new/review/{CAMPAIGN}", "[data-cw-review] [data-cw-approve]")
    assert phone.locator(".stepper-phone").inner_text() == "Step 4 of 5 · Review"
    assert not _sideways(phone)
    card = phone.locator("[data-cw-review]").bounding_box()
    for selector in ("[data-cw-approve]", "[data-cw-again]"):
        box = phone.locator(selector).bounding_box()
        assert box["x"] >= card["x"] and box["x"] + box["width"] <= card["x"] + card["width"] + 0.5, selector
        assert box["width"] >= card["width"] - 2 * 17 - 1, f"{selector} is full width on a phone"
        assert box["height"] >= 40
    # M187 review: every action of the wizard's own (Guided setup's are its own) is a 40px target on a phone,
    # the technical details' Copy buttons and summaries included.
    phone.evaluate("() => document.querySelectorAll('.cw details.tech').forEach((d) => { d.open = true; })")
    small = phone.evaluate("""() => [...document.querySelectorAll('.cw .btn, .cw details.tech > summary')]
             .filter((e) => !e.closest('[data-cw-guided]') && e.getClientRects().length)
             .filter((e) => e.getBoundingClientRect().height < 40)
             .map((e) => e.outerHTML.slice(0, 80))""")
    assert small == [], small
    _close(phone)
    assert problems == [], problems


def test_a_touch_screen_gets_40px_targets_at_any_width(browser: Any, server: str) -> None:
    """A tablet (coarse pointer, 1024 px): the wizard's tertiary links and Copy buttons are 40px targets too."""
    problems: list[str] = []
    context = browser.new_context(viewport={"width": 1024, "height": 800}, has_touch=True, is_mobile=True)
    context.route(lambda url: not url.startswith(server), lambda route: route.abort())

    def answer(route: Any) -> None:
        body = route.fetch().json()
        body["report"] = None
        body["plan"] = None
        body["campaign"] = {
            **body["campaign"],
            "status": "live",
            "test_plan_hash": None,
            "test_plan_version": None,
        }
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    context.route(lambda url: url.endswith(f"/campaigns/{CAMPAIGN}"), answer)
    page = context.new_page()
    page.on("pageerror", lambda error: problems.append(f"uncaught: {error}"))
    _open(page, server, f"#/campaigns/new/review/{CAMPAIGN}", "[data-cw-review] [data-cw-approve]")
    assert page.evaluate("() => matchMedia('(pointer: coarse)').matches"), "the context is a touch screen"
    page.evaluate("() => document.querySelectorAll('.cw details.tech').forEach((d) => { d.open = true; })")
    small = page.evaluate("""() => [...document.querySelectorAll('.cw .btn, .cw details.tech > summary')]
             .filter((e) => !e.closest('[data-cw-guided]') && e.getClientRects().length)
             .filter((e) => e.getBoundingClientRect().height < 40)
             .map((e) => e.outerHTML.slice(0, 80))""")
    assert small == [], small
    _close(page)
    assert problems == [], problems


@pytest.mark.parametrize("width", [390, 320])
def test_the_stepper_is_not_clipped_on_a_phone(browser: Any, server: str, width: int) -> None:
    """M185 note: the five circles, the line and "Step 4 of 5 · Review" fit a phone, even a narrow one."""
    problems: list[str] = []
    page = _page(browser, server, problems, viewport={"width": width, "height": 800}, mode="draft")
    _open(page, server, f"#/campaigns/new/review/{CAMPAIGN}", "[data-cw-review] [data-cw-approve]")
    stepper = page.locator(".cw ol.stepper")
    box = stepper.bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= width
    assert page.evaluate(
        "() => { const s = document.querySelector('.cw ol.stepper'); return s.scrollWidth <= s.clientWidth; }"
    )
    dots = page.locator(".cw ol.stepper .st-dot")
    assert dots.count() == 5
    for i in range(5):
        dot = dots.nth(i).bounding_box()
        assert dot["x"] >= box["x"] and dot["x"] + dot["width"] <= box["x"] + box["width"] + 0.5, i
        assert dot["width"] >= 24
    line = page.locator(".cw .stepper-phone")
    assert line.inner_text() == "Step 4 of 5 · Review"
    lb = line.bounding_box()
    assert lb["x"] + lb["width"] <= width
    assert not _sideways(page)
    _close(page)
    assert problems == [], problems


def test_send_success_state(browser: Any, server: str) -> None:
    problems: list[str] = []
    for viewport in (DESKTOP, PHONE):
        page = _page(browser, server, problems, viewport=viewport, mode="live")
        _open(page, server, f"#/campaigns/new/send/{CAMPAIGN}", "[data-cw-send]")
        assert page.locator("[data-cw-send] .cw-done-icon svg").is_visible()
        assert page.locator("[data-cw-status]").inner_text() == "Live"
        assert _style(page, "[data-cw-send] .cw-done-icon", "color") == "rgb(21, 128, 61)", "the good tone"
        assert page.locator("#app .btn.primary").count() == 1
        assert not _sideways(page)
        if viewport is PHONE:
            body = page.locator("[data-cw-send] .cw-done-actions").bounding_box()
            for i in range(page.locator(".cw-done-actions > .btn").count()):
                assert (
                    page.locator(".cw-done-actions > .btn").nth(i).bounding_box()["width"]
                    >= body["width"] - 1
                )
        _close(page)
    assert problems == [], problems


def test_a_custom_brand_colour_recolours_what_the_wizard_adds(browser: Any, server: str) -> None:
    problems: list[str] = []
    page = _page(browser, server, problems, viewport=DESKTOP, mode="draft")
    _open(page, server, f"#/campaigns/new/review/{CAMPAIGN}", "[data-cw-review] [data-cw-approve]")
    page.evaluate("() => document.documentElement.style.setProperty('--brand-blue', '#0b5d3b')")
    page.wait_for_timeout(300)
    checks = {
        "the primary button": ("[data-cw-approve]", "background-color"),
        "the current step's ring": ("[data-cw-stepper=review] .st-dot", "border-top-color"),
        "the line joining the two current steps": ("[data-cw-stepper=review]", "--brand-blue"),
        "a share bar": ("[data-cw-priority] .cw-bar > span", "background-color"),
        "the done step": ("[data-cw-stepper=goal] .st-dot", "background-color"),
        "the secondary link": ("[data-cw-again]", "color"),
    }
    seen = {name: _style(page, sel, prop).strip() for name, (sel, prop) in checks.items()}
    seen["the line joining the two current steps"] = str(
        page.evaluate(
            "() => getComputedStyle(document.querySelector('[data-cw-stepper=review]'), '::before').backgroundColor"
        )
    )
    assert all(v == GREEN for v in seen.values()), seen
    _close(page)

    goal = _page(browser, server, problems, viewport=DESKTOP)
    _open(goal, server, "#/campaigns/new", "[data-cw-goals] .cw-goal")
    goal.evaluate("() => document.documentElement.style.setProperty('--brand-blue', '#0b5d3b')")
    goal.wait_for_timeout(300)
    assert _style(goal, "[data-cw-goals] .cw-goal-icon", "color") == GREEN, "the goal's icon"
    goal.locator("[data-cw-goals] .cw-goal").first.evaluate("e => e.classList.add('is-selected')")
    goal.wait_for_timeout(300)
    assert _style(goal, "[data-cw-goals] .cw-goal.is-selected", "border-top-color") == GREEN, "a chosen goal"
    _close(goal)
    assert problems == [], problems
