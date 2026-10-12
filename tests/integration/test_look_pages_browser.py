"""Plan M M188 (DEC-1618): the look of Reports, Data, Settings and Help in a real browser.

The pages are drawn by the real `ui/` against the API answers recorded from the validated Hillstrom demo
(`tests/fixtures/ui_replay.py`), in the campaigns layout. What is checked in Chromium, where the real style sheets apply:

* no page scrolls sideways at 390 px, and none draws nothing;
* the page header: the one primary action is on the right of the title on a desktop and below the text on a phone;
* the tabs are underlines, not pills: the selected one has a 2px brand underline and no filled background;
* a brand colour a deployment sets (M182) reaches every new brand-coloured thing of these pages: the primary button,
  the selected tab, the check form's step numbers and its range tint, the source rows' type icons, the Settings toggle;
* the Settings toggle is drawn as a toggle (44 x 24, a knob that moves) and not as a 16px checkbox;
* a value-range bar is drawn and stays inside its row; a status chip is 24px tall and carries its icon;
* Settings has no breadcrumb and the Glossary keeps its own.

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
    "playwright.sync_api", reason="playwright is not installed; the look needs a browser"
)

CHROMIUM: Final[Path] = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")) / "chromium"
GREEN: Final[str] = "#0b5d3b"
GREEN_RGB: Final[str] = "rgb(11, 93, 59)"
SETTLE_MS: Final[int] = 1500
PAGES: Final[tuple[tuple[str, str], ...]] = (
    ("reports", "#/reports"),
    ("reports-programme", "#/reports/programme"),
    ("reports-check", "#/reports/check"),
    ("reports-models", "#/reports/models"),
    ("data", "#/data"),
    ("data-check", "#/data/check"),
    ("settings", "#/settings"),
    ("glossary", "#/help/glossary"),
    ("whats-new", "#/help/whats-new"),
)


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


def _open(
    browser: Any, base: str, fragment: str, width: int, height: int = 900, *, technical: bool = False
) -> Any:
    context = browser.new_context(viewport={"width": width, "height": height})
    context.route(
        "**/*", lambda route: route.continue_() if route.request.url.startswith(base) else route.abort()
    )
    if technical:
        context.add_init_script("try{localStorage.setItem('marketing-ai.show-technical','on')}catch(e){}")
    page = context.new_page()
    page.goto(f"{base}/ui/{fragment}", wait_until="load")
    page.wait_for_timeout(SETTLE_MS)
    return page


def _style(page: Any, selector: str, prop: str) -> str:
    return str(
        page.evaluate(
            "([s, p]) => getComputedStyle(document.querySelector(s))[p]",
            [selector, prop],
        )
    )


def _box(page: Any, selector: str) -> dict[str, float]:
    box = page.evaluate(
        "(s) => { const r = document.querySelector(s).getBoundingClientRect();"
        " return { x: r.x, y: r.y, w: r.width, h: r.height, r: r.right, b: r.bottom }; }",
        selector,
    )
    assert isinstance(box, dict)
    return {k: float(v) for k, v in box.items()}


@pytest.mark.parametrize(("name", "fragment"), PAGES)
def test_no_page_scrolls_sideways_at_390_px_or_draws_nothing(
    browser: Any, base: str, name: str, fragment: str
) -> None:
    page = _open(browser, base, fragment, 390, 844, technical=name == "reports-models")
    try:
        sideways = page.evaluate("() => document.documentElement.scrollWidth > window.innerWidth + 1")
        assert not sideways, f"{name} scrolls sideways at 390 px"
        text = page.evaluate(
            "() => (document.querySelector('#app') || document.body).innerText.trim().length"
        )
        assert text > 40, f"{name} drew nothing"
    finally:
        page.context.close()


def test_the_primary_action_is_right_of_the_title_on_a_desktop_and_below_the_text_on_a_phone(
    browser: Any, base: str
) -> None:
    desktop = _open(browser, base, "#/reports", 1440)
    try:
        h1, action = _box(desktop, "main h1"), _box(desktop, ".page-header .ph-actions .btn.primary")
        assert action["x"] > h1["r"] + 100, "on the right of the title"
        assert action["y"] < h1["b"] + 40, "level with the title, not under the tabs"
    finally:
        desktop.context.close()
    phone = _open(browser, base, "#/reports", 390, 844)
    try:
        desc, action = _box(phone, ".page-header .desc"), _box(phone, ".page-header .ph-actions .btn.primary")
        assert action["y"] >= desc["b"], "below the text on a phone"
        assert action["x"] < 40, "starting at the page's left edge, never squeezed beside the text"
    finally:
        phone.context.close()


def test_the_tabs_are_underlines_not_pills(browser: Any, base: str) -> None:
    page = _open(browser, base, "#/reports", 1440)
    try:
        selected = '[role="tab"][aria-selected="true"]'
        other = '[role="tab"][aria-selected="false"]'
        assert _style(page, selected, "borderBottomWidth") == "2px"
        assert _style(page, selected, "backgroundColor") == "rgba(0, 0, 0, 0)", "no filled pill"
        assert _style(page, other, "borderBottomWidth") == "2px"
        assert _style(page, other, "borderBottomColor") == "rgba(0, 0, 0, 0)", "the others have no underline"
        assert _style(page, other, "borderRadius") == "0px", "not pills"
    finally:
        page.context.close()


def test_a_status_chip_is_24px_with_its_icon_and_a_value_range_bar_is_drawn_inside_its_row(
    browser: Any, base: str
) -> None:
    page = _open(browser, base, "#/reports", 1440)
    try:
        chip = _box(page, "[data-claim] .badge")
        assert 24 <= chip["h"] <= 26, f"a 24px chip, not {chip['h']}"
        assert _box(page, "[data-claim] .badge svg")["w"] == 12, "a 12px icon"
        track, row = _box(page, "[data-proofs] .vr-track"), _box(page, "[data-proofs] tbody tr")
        bar = _box(page, "[data-proofs] .vr-bar")
        assert (
            bar["w"] > 4 and track["x"] <= bar["x"] and bar["r"] <= track["r"] + 1
        ), "the range sits on its track"
        assert row["x"] <= track["x"] and track["r"] <= row["r"], "and the track inside its row"
        assert row["h"] >= 52, "rows are at least 52px tall"
    finally:
        page.context.close()


def test_settings_has_no_breadcrumb_and_the_glossary_keeps_its_own(browser: Any, base: str) -> None:
    settings = _open(browser, base, "#/settings", 1440)
    try:
        assert settings.locator(".crumbs").count() == 0
        assert settings.locator(".card.st-group").count() >= 4
    finally:
        settings.context.close()
    glossary = _open(browser, base, "#/help/glossary", 1440)
    try:
        assert glossary.locator(".crumbs").count() == 1
        glossary.fill("[data-glossary-filter]", "contact")
        shown = glossary.evaluate(
            "() => [...document.querySelectorAll('[data-glossary] tbody tr')].filter(r => r.offsetParent !== null).length"
        )
        total = glossary.locator("[data-glossary] tbody tr").count()
        assert 0 < shown < total, "typing narrows the list, in the real browser too"
    finally:
        glossary.context.close()


def test_the_settings_toggle_is_a_toggle(browser: Any, base: str) -> None:
    page = _open(browser, base, "#/settings", 1440)
    try:
        switch = "[data-settings-view] input[data-tech-switch]"
        box = _box(page, switch)
        assert (round(box["w"]), round(box["h"])) == (44, 24), "a 44 x 24 track, not a 16px checkbox"
        row = _box(page, "[data-settings-view] .tech-switch")
        assert box["r"] > row["r"] - 30, "at the right end of its row"
        assert _style(page, switch, "backgroundColor") != _style(
            page, "[data-settings-view]", "backgroundColor"
        )
        before = page.evaluate(
            "(s) => getComputedStyle(document.querySelector(s), '::after').transform", switch
        )
        page.click(switch)
        after = page.evaluate(
            "(s) => getComputedStyle(document.querySelector(s), '::after').transform", switch
        )
        page.wait_for_timeout(300)
        after = page.evaluate(
            "(s) => getComputedStyle(document.querySelector(s), '::after').transform", switch
        )
        assert before != after, "the knob moves when it is switched"
    finally:
        page.context.close()


def _recolour(page: Any) -> None:
    """What `ui/brand.js` does for a deployment's colour (M182): set the accent on <html>."""
    page.evaluate(f"() => document.documentElement.style.setProperty('--brand-blue', '{GREEN}')")
    page.wait_for_timeout(400)  # the colours transition for 140 ms


def _is_greenish(rgb: str) -> bool:
    inside = rgb[rgb.index("(") + 1 : rgb.index(")")]
    if rgb.startswith(
        "color(srgb"
    ):  # a tint made with color-mix() computes to `color(srgb r g b)`, each 0 to 1
        red, green, blue = (float(x) * 255 for x in inside.replace("srgb", "").replace("/", " ").split()[:3])
    else:
        red, green, blue = (float(x) for x in inside.split(",")[:3])
    return green >= blue and green >= red - 1  # the default blue's tints have blue well above green


def test_a_brand_colour_reaches_every_new_brand_coloured_thing_of_these_pages(
    browser: Any, base: str
) -> None:
    reports = _open(browser, base, "#/reports", 1440)
    try:
        _recolour(reports)
        assert (
            _style(reports, ".page-header .btn.primary", "backgroundColor") == GREEN_RGB
        ), "the primary button"
        assert (
            _style(reports, '[role="tab"][aria-selected="true"]', "borderBottomColor") == GREEN_RGB
        ), "the selected tab"
        assert _is_greenish(_style(reports, "[data-proofs] .vr-bar", "backgroundColor")), "the range's tint"
    finally:
        reports.context.close()
    check = _open(browser, base, "#/reports/check", 1440)
    try:
        _recolour(check)
        assert _style(check, ".rp-step-n", "backgroundColor") == GREEN_RGB, "the step numbers"
        assert _style(check, ".rp-drop-ico", "color") == GREEN_RGB, "the drop zone's icon"
    finally:
        check.context.close()
    data = _open(browser, base, "#/data", 1440)
    try:
        _recolour(data)
        assert _style(data, ".dt-ico", "color") == GREEN_RGB, "the type icons"
        assert _is_greenish(_style(data, ".dt-ico", "backgroundColor")), "and their tint"
        assert _style(data, "#dt-add", "backgroundColor") == GREEN_RGB, "+ Add a source"
    finally:
        data.context.close()
    settings = _open(browser, base, "#/settings", 1440)
    try:
        _recolour(settings)
        settings.click("[data-settings-view] input[data-tech-switch]")
        settings.wait_for_timeout(300)
        assert (
            _style(settings, "[data-settings-view] input[data-tech-switch]", "backgroundColor") == GREEN_RGB
        ), "the toggle"
        assert _style(settings, "[data-settings-main] .st-label", "color") == GREEN_RGB, "the row links"
    finally:
        settings.context.close()


@pytest.mark.parametrize(("fragment", "technical"), [("#/reports/check", False), ("#/reports/models", True)])
def test_on_a_phone_the_selected_tab_is_scrolled_into_view(
    browser: Any, base: str, fragment: str, technical: bool
) -> None:
    """M188 review 3: the row of tabs scrolls inside itself on a phone; the selected one must not be cut off."""
    page = _open(browser, base, fragment, 390, 844, technical=technical)
    try:
        tab = _box(page, '[role="tab"][aria-selected="true"]')
        row = _box(page, '[role="tablist"]')
        assert (
            tab["x"] >= row["x"] - 1 and tab["r"] <= row["r"] + 1
        ), f"the selected tab shows whole: {tab} in {row}"
        assert page.evaluate("() => window.scrollY") == 0, "the page itself did not scroll"
    finally:
        page.context.close()


def _small_targets(page: Any, selector: str) -> list[str]:
    found = page.evaluate(
        "(s) => [...document.querySelectorAll(s)].filter(e => e.offsetParent !== null)"
        ".map(e => [e.textContent.trim(), e.getBoundingClientRect().height])",
        selector,
    )
    assert found, f"nothing matched {selector}"
    return [f"{text} ({height}px)" for text, height in found if height < 40]


def test_on_a_phone_every_row_action_is_a_40px_target(browser: Any, base: str) -> None:
    """M188 review 2: the small (32px and 24px) buttons of a row grow to 40px on a phone."""
    reports = _open(browser, base, "#/reports", 390, 844)
    try:
        assert _small_targets(reports, "[data-proofs] .rp-acts .btn") == []
    finally:
        reports.context.close()
    models = _open(browser, base, "#/reports/models", 390, 844, technical=True)
    try:
        models.evaluate("() => { document.querySelector('[data-rp-models]').open = true; }")
        assert _small_targets(models, "[data-rp-models] .rp-acts .btn") == []
    finally:
        models.context.close()
    data = _open(browser, base, "#/data", 390, 844)
    try:
        data.click("#dt-add")
        data.wait_for_timeout(300)
        assert _small_targets(data, "#dt-chooser .btn") == []
    finally:
        data.context.close()
