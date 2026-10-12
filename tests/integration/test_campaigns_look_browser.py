"""Plan M M186 (DEC-1616): the look of the Overview, the Campaigns list and the campaign page, in a real browser.

The real `ui/` is served with the API answers recorded from the validated Hillstrom demo (`tests/fixtures/ui_replay.py`)
in the campaigns layout, and Chromium measures what the look promises (docs/ux/VISUAL_DESIGN.md, visual_common.md):

* the Overview: no breadcrumb, the value proven to date as a hero figure of at least 48 px, attention items with the
  action on the right on a wide screen and below the text on a phone;
* the Campaigns list: the status chip in its tone, the contacted count right-aligned in tabular figures, the net value
  with its likely range bar; on a phone each row is a stacked card;
* the campaign page: underline tabs in the brand colour, the status chip beside the name, a KPI row whose net value
  tile draws its range, and the Results tab's drop zone, which takes a dropped file into the form's file input;
* no page scrolls sideways at 390 px, and no page logs a console error.

Skips with the reason when playwright or a Chromium is missing.
"""

from __future__ import annotations

import copy
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import pytest

from tests.fixtures.ui_replay import RECORDED, ReplayServer, has_campaigns_layout

pytestmark = [pytest.mark.integration]

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")

CHROMIUM: Final[Path] = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")) / "chromium"
DESKTOP: Final[dict[str, int]] = {"width": 1440, "height": 900}
PHONE: Final[dict[str, int]] = {"width": 390, "height": 844}
CAMPAIGN: Final[str] = "c_20261011_c9276210"
BRAND_BLUE: Final[str] = "rgb(30, 87, 189)"
GOOD: Final[str] = "rgb(21, 128, 61)"


@pytest.fixture(scope="module")
def server() -> Iterator[ReplayServer]:
    if not has_campaigns_layout():
        pytest.skip("the campaigns layout (ui/layout.js, M174) is not in this tree")
    with ReplayServer(ui_layout="campaigns") as running:
        yield running


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    if not CHROMIUM.exists():
        pytest.skip(f"no Chromium at {CHROMIUM}")
    with sync_api.sync_playwright() as p:
        launched = p.chromium.launch(executable_path=str(CHROMIUM))
        yield launched
        launched.close()


def _live_campaign() -> dict[str, Any]:
    """The conversion campaign as it stood before its results were in: Live, no report, its plan's date to come."""
    recorded = json.loads(RECORDED.read_text(encoding="utf-8"))
    view: dict[str, Any] = copy.deepcopy(recorded[f"GET /campaigns/{CAMPAIGN}"]["body"])
    view["campaign"].update(status="live", outcomes=None, measured_at=None)
    view.update(report=None, verdict=None)
    if view.get("plan"):
        view["plan"]["analysis_date"] = "2099-01-15"
    return view


def _answer(body: Any) -> Any:
    """A route handler that answers with `body` (one argument: playwright passes the request to a second one)."""

    def handler(route: Any) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    return handler


def _open(
    browser: Any,
    server: ReplayServer,
    fragment: str,
    viewport: dict[str, int],
    *,
    overrides: dict[str, Any] | None = None,
) -> tuple[Any, list[str]]:
    context = browser.new_context(viewport=viewport)
    errors: list[str] = []
    context.route(
        "**/*",
        lambda route: route.continue_() if route.request.url.startswith(server.base) else route.abort(),
    )
    for path, body in (overrides or {}).items():
        context.route(f"{server.base}{path}", _answer(body))
    page = context.new_page()
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.on(
        "console",
        lambda m: (
            errors.append(m.text) if m.type == "error" and "Failed to load resource" not in m.text else None
        ),
    )
    page.goto(f"{server.base}/ui/{fragment}", wait_until="load")
    page.wait_for_selector("#app h1")
    page.wait_for_timeout(800)
    return page, errors


def _style(page: Any, selector: str, prop: str, pseudo: str = "") -> str:
    return str(
        page.locator(selector).first.evaluate(
            f"el => getComputedStyle(el, {json.dumps(pseudo) if pseudo else 'null'}).{prop}"
        )
    )


def _box(page: Any, selector: str) -> dict[str, float]:
    box = page.locator(selector).first.bounding_box()
    assert box is not None, f"{selector} is not drawn"
    return dict(box)


def _no_sideways_scroll(page: Any) -> bool:
    return bool(page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth"))


def test_the_overview_hero_and_attention_items(browser: Any, server: ReplayServer) -> None:
    page, errors = _open(browser, server, "#/overview", DESKTOP)
    page.wait_for_selector("[data-value-amount]")
    assert page.locator("#app .crumbs").count() == 0, "a top-level place has no breadcrumb"
    assert float(_style(page, "[data-value-amount] .hero-figure", "fontSize").rstrip("px")) >= 48
    assert _style(page, "[data-value-amount] .hero-figure", "fontWeight") == "600"
    text, action = _box(page, "[data-card] .attn-body"), _box(page, "[data-card] .attn-action")
    assert (
        action["x"] >= text["x"] + text["width"]
    ), "the action sits to the right of the text on a wide screen"
    assert (
        _style(page, "[data-card] .attn-icon", "color") == "rgb(180, 83, 9)"
    ), "a warning's icon in its tone"
    assert errors == []
    page.context.close()
    page, errors = _open(browser, server, "#/overview", PHONE)
    page.wait_for_selector("[data-card]")
    text, action = _box(page, "[data-card] .attn-body"), _box(page, "[data-card] .attn-action")
    assert action["y"] >= text["y"] + text["height"], "on a phone the action goes below the text"
    assert text["width"] > 250, "the text keeps the card's width, never a squeezed column"
    assert _no_sideways_scroll(page)
    assert errors == []
    page.context.close()


def test_the_campaigns_list_table_and_its_phone_cards(browser: Any, server: ReplayServer) -> None:
    page, errors = _open(browser, server, "#/campaigns", DESKTOP)
    page.wait_for_selector("[data-campaigns-list] tbody tr [data-result]")
    assert page.locator("#app .crumbs").count() == 0
    chip = "[data-campaigns-list] tbody tr [data-status='measured']"
    assert _style(page, chip, "color") == GOOD
    assert (
        page.locator(chip).first.evaluate("el => el.querySelector('svg').getBoundingClientRect().width") == 12
    )
    contacted = "[data-campaigns-list] tbody tr td:nth-child(4)"
    assert _style(page, contacted, "textAlign") == "right"
    assert _style(page, contacted, "fontVariantNumeric") == "tabular-nums"
    goal = _box(page, "[data-campaigns-list] tbody tr td:nth-child(2) .tag")
    assert goal["height"] <= 26, "the goal chip stays on one line (an ellipsis, the whole goal in its title)"
    bar = _box(page, "[data-campaigns-list] [data-result] .vr-bar")
    assert bar["width"] > 8, "the likely range is drawn as a bar"
    assert _style(page, "[data-campaigns-list] [data-result] .vr-dot", "backgroundColor") == BRAND_BLUE
    assert errors == []
    page.context.close()
    page, errors = _open(browser, server, "#/campaigns", PHONE)
    page.wait_for_selector("[data-campaigns-list] tbody tr [data-result]")
    assert (
        _style(page, "[data-campaigns-list] tbody tr", "display") == "grid"
    ), "a stacked row card on a phone"
    name, status = _box(page, "[data-campaigns-list] tbody tr td:nth-child(1)"), _box(
        page, "[data-campaigns-list] tbody tr td:nth-child(3)"
    )
    assert abs(name["y"] - status["y"]) < 4, "the name and its status chip share the card's first line"
    assert _no_sideways_scroll(page)
    assert errors == []
    page.context.close()


@pytest.mark.parametrize("viewport", [DESKTOP, PHONE], ids=["desktop", "phone"])
def test_the_campaign_page_header_tabs_and_tiles(
    browser: Any, server: ReplayServer, viewport: dict[str, int]
) -> None:
    for tab in ("overview", "audience", "results"):
        page, errors = _open(browser, server, f"#/campaigns/{CAMPAIGN}/{tab}", viewport)
        page.wait_for_selector("[data-ds-tabs] [aria-selected='true']")
        selected = "[data-ds-tabs] [aria-selected='true']"
        assert page.locator(selected).get_attribute("data-tab") == tab
        assert _style(page, selected, "borderBottomColor") == BRAND_BLUE
        assert _style(page, selected, "borderBottomWidth") == "2px"
        assert _style(page, selected, "backgroundColor") == "rgba(0, 0, 0, 0)", "an underline, not a pill"
        assert page.locator("#app .head .badge[data-status='measured']").is_visible()
        assert page.locator("#app .btn.primary").count() == 1
        assert _no_sideways_scroll(page), f"{tab} scrolls sideways at {viewport['width']} px"
        if tab == "overview":
            assert page.locator("[data-kpis] > .stat").count() == 3
            assert _box(page, "[data-tile-value] .vr-bar")["width"] > 8
            if viewport is PHONE:
                assert _box(page, "[data-refresh-ask]")["height"] >= 40, "a 40 px touch target on a phone"
        assert errors == []
        page.context.close()


def test_the_results_drop_zone_takes_a_dropped_file(browser: Any, server: ReplayServer) -> None:
    page, errors = _open(
        browser,
        server,
        f"#/campaigns/{CAMPAIGN}/results",
        DESKTOP,
        overrides={f"/campaigns/{CAMPAIGN}": _live_campaign()},
    )
    page.wait_for_selector("[data-results-drop]")
    assert _style(page, "[data-results-drop]", "borderTopStyle") == "dashed"
    assert page.locator("[data-results-chosen]").is_hidden()
    page.evaluate("""() => {
          const zone = document.querySelector('[data-results-drop]');
          const data = new DataTransfer();
          data.items.add(new File(['customer_id,conversion\\n1,1\\n'], 'october_results.csv', { type: 'text/csv' }));
          zone.dispatchEvent(new DragEvent('dragover', { bubbles: true, cancelable: true, dataTransfer: data }));
          zone.dispatchEvent(new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer: data }));
        }""")
    assert page.locator("[data-results-chosen]").inner_text() == "Chosen: october_results.csv"
    assert (
        page.evaluate("document.querySelector('[data-results-file-input]').files[0].name")
        == "october_results.csv"
    )
    assert page.locator("[data-results-drop].is-over").count() == 0
    page.locator("[data-results-file-input]").focus()
    assert _style(page, "[data-results-drop]", "outlineStyle") != "none", "the zone shows the keyboard focus"
    assert page.locator("#app .btn.primary").count() == 1
    assert errors == []
    page.context.close()
