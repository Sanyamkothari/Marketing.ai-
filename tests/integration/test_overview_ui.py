"""Plan M M175 (DEC-1605): the Overview page, drawn in jsdom and in a real browser.

* `tests/integration/production/ui/overview/overview.test.mjs` (jsdom, run here through node) draws `#/overview` against
  the answers recorded from the validated Hillstrom demo and against answers shaped for the cases the demo lacks: one
  primary button, the plan's order, one action per card, rupees rounded, short campaign names, the first-visit checklist
  ticked from real state and hidden once done or dismissed (also with browser storage blocked), a failed call, and no
  glossary "never shown" word on any state with an empty allow-list.
* The Chromium tests serve the real UI from the real app and answer every API call from the same recording
  (`tests/fixtures/ui_demo/api.json`), so the page is judged on the demo's own data: it fits 1440 x 900 without
  scrolling (a screenshot is kept), the busiest case (five cards, five live campaigns, the checklist) also fits, there is
  no horizontal scroll at phone width (390 px), and the console stays clean.

Node and jsdom are a development dependency of `tests/integration/production/ui`: without them the jsdom half skips with
the install command (`REQUIRE_JSDOM=1` turns the skip into a failure, as CI sets it). The browser half skips without
playwright or a Chromium (`PLAYWRIGHT_BROWSERS_PATH`, `/opt/pw-browsers/chromium`).
"""

from __future__ import annotations

import copy
import json
import os
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qsl, urlencode, urlparse

import pytest

from tests.fixtures.node import skip_without_jsdom

pytestmark = pytest.mark.integration

NODE_DIR: Final[Path] = Path(__file__).resolve().parent / "production" / "ui"
OVERVIEW_TEST: Final[Path] = NODE_DIR / "overview" / "overview.test.mjs"
RECORDED: Final[Path] = Path(__file__).resolve().parents[1] / "fixtures" / "ui_demo" / "api.json"
CHROMIUM: Final[str] = "/opt/pw-browsers/chromium"
DESKTOP: Final[dict[str, int]] = {"width": 1440, "height": 900}
PHONE: Final[dict[str, int]] = {"width": 390, "height": 844}


def test_the_overview_page_in_jsdom() -> None:
    node = skip_without_jsdom(NODE_DIR)
    result = subprocess.run(
        [node, "--test", str(OVERVIEW_TEST)],
        cwd=NODE_DIR,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-8000:] + result.stderr[-3000:]
    assert "# fail 0" in result.stdout


# --- the real browser ---------------------------------------------------------------------------------------------------


def _key(method: str, url: str) -> str:
    parsed = urlparse(url)
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    return f"{method.upper()} {parsed.path}" + (f"?{query}" if query else "")


def _busy(demo: dict[str, Any]) -> dict[str, Any]:
    """The most the page can hold: five cards (one with a long name), five live campaigns, an unticked step."""
    card = demo["cards"][0]
    cards = []
    for n, kind in enumerate(
        ["no_control", "contamination", "backfire", "challenger_ready", "uplift_fading"]
    ):
        c = copy.deepcopy(card)
        c.update(
            code=f"CARD_{n}", kind=kind, title=f"{card['title']} ({n + 1})", campaign_id=f"c_20261011_{n:08x}"
        )
        cards.append(c)
    rows = [
        {
            "campaign_id": f"c_20261011_{n:08x}",
            "name": f"Win back lapsed customers - month {n}, a name that is far too long for one line",
            "use_case_id": "win-back-campaign",
            "status": "live" if n % 2 else "waiting_for_results",
            "next_step": "wait" if n % 2 else "add_results",
            "results_date": "2026-11-12",
        }
        for n in range(5)
    ]
    return {
        **demo,
        "cards": cards,
        "cards_total": 8,
        "campaigns": rows,
        "campaigns_total": 7,
        "checklist": {"connected": True, "campaign": True, "results": False},
    }


class _Site:
    """The real app serving `/ui`, in a thread, with the demo's recorded answers in front of its API."""

    def __init__(self, port: int) -> None:
        import uvicorn

        from api.main import create_app

        self.port = port
        self._server = uvicorn.Server(
            uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning")
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def start(self) -> None:
        self._thread.start()
        deadline = time.monotonic() + 60
        while not self._server.started:
            assert time.monotonic() < deadline, "the app did not start"
            time.sleep(0.05)

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=15)


@pytest.fixture(scope="module")
def site() -> Iterator[_Site]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    running = _Site(port)
    running.start()
    yield running
    running.stop()


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
    if not Path(CHROMIUM).exists():
        pytest.skip(f"no Chromium at {CHROMIUM}")
    with sync_api.sync_playwright() as p:
        launched = p.chromium.launch(executable_path=CHROMIUM)
        yield launched
        launched.close()


def _open(
    browser: Any, site: _Site, overview: dict[str, Any], viewport: dict[str, int]
) -> tuple[Any, list[str]]:
    """The page on `#/overview` with every API answer taken from the recording (and `GET /overview` from `overview`)."""
    recorded: dict[str, Any] = json.loads(RECORDED.read_text(encoding="utf-8"))
    errors: list[str] = []
    context = browser.new_context(viewport=viewport)
    page = context.new_page()
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.on(
        "console",
        lambda m: (
            errors.append(f"console.{m.type}: {m.text}")
            if m.type == "error" and "404" not in m.text
            else None
        ),
    )

    def answer(route: Any) -> None:
        request = route.request
        parsed = urlparse(request.url)
        path = parsed.path
        if (
            parsed.hostname != "127.0.0.1"
        ):  # web fonts and the like: not this page's business, and no network is assumed
            route.fulfill(status=200, content_type="text/css", body="")
            return
        if path.startswith("/ui") or request.resource_type not in {"fetch", "xhr"}:
            route.continue_()
            return
        if request.method == "GET" and path == "/overview":
            route.fulfill(status=200, content_type="application/json", body=json.dumps(overview))
            return
        hit = recorded.get(_key(request.method, request.url))
        if hit is None:
            route.fulfill(
                status=404,
                content_type="application/json",
                body='{"detail":{"code":"NOT_RECORDED","message":"Not found."}}',
            )
            return
        body = hit["body"] if isinstance(hit["body"], str) else json.dumps(hit["body"])
        route.fulfill(status=hit["status"], content_type=hit["content_type"] or "application/json", body=body)

    page.route("**/*", answer)
    page.goto(f"http://127.0.0.1:{site.port}/ui/#/overview", wait_until="load")
    page.wait_for_selector("[data-attention]", timeout=15000)
    page.wait_for_timeout(300)
    return page, errors


def _shots(tmp_path: Path) -> Path:
    where = Path(os.environ.get("OVERVIEW_SHOTS", str(tmp_path)))
    where.mkdir(parents=True, exist_ok=True)
    return where


def _height(page: Any) -> int:
    """The bottom edge of everything the page draws, in CSS pixels."""
    return int(
        page.evaluate(
            "Math.ceil(Math.max(document.documentElement.scrollHeight, document.body.scrollHeight))"
        )
    )


def test_the_demo_fits_one_screen_at_1440_by_900(browser: Any, site: _Site, tmp_path: Path) -> None:
    demo = json.loads(RECORDED.read_text(encoding="utf-8"))["GET /overview"]["body"]
    page, errors = _open(browser, site, demo, DESKTOP)
    page.screenshot(path=str(_shots(tmp_path) / "overview-demo-1440x900.png"))
    assert _height(page) <= DESKTOP["height"], f"the page is {_height(page)} px tall"
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    button = page.locator("[data-new-campaign]")
    assert button.is_visible() and button.inner_text().strip() == "+ New campaign"
    assert page.locator('[data-module="overview"] .btn.primary').count() == 1
    assert page.locator("[data-attention] [data-card]").count() == 1
    assert errors == []
    page.context.close()


def test_a_fresh_install_with_its_checklist_fits_one_screen(
    browser: Any, site: _Site, tmp_path: Path
) -> None:
    fresh = {
        "schema_version": 1,
        "cards": [],
        "cards_total": 0,
        "headline": None,
        "summary_checked": True,
        "campaigns": [],
        "campaigns_total": 0,
        "checklist": {"connected": False, "campaign": False, "results": False},
        "built_at": "2026-10-11T12:00:00Z",
    }
    page, errors = _open(browser, site, fresh, DESKTOP)
    page.screenshot(path=str(_shots(tmp_path) / "overview-fresh-1440x900.png"))
    assert page.locator("[data-checklist] [data-check]").count() == 3
    assert _height(page) <= DESKTOP["height"], f"the first visit is {_height(page)} px tall"
    assert errors == []
    page.context.close()


def test_the_busiest_page_has_five_cards_with_one_action_each_and_five_live_campaigns(
    browser: Any, site: _Site, tmp_path: Path
) -> None:
    demo = json.loads(RECORDED.read_text(encoding="utf-8"))["GET /overview"]["body"]
    page, errors = _open(browser, site, _busy(demo), DESKTOP)
    page.screenshot(path=str(_shots(tmp_path) / "overview-busy-1440x900.png"), full_page=True)
    assert page.locator("[data-attention] [data-card]").count() == 5
    assert page.locator("[data-live-row]").count() == 5
    assert page.locator("[data-checklist]").count() == 1
    for card in page.locator("[data-attention] [data-card]").all():
        assert card.locator("a, button").count() == 1
    # Five cards of two sentences each need more than one screen: the plan's "fits one screen" is the demo's, above.
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert errors == []
    page.context.close()


@pytest.mark.parametrize("case", ["demo", "busy"])
def test_phone_width_has_no_horizontal_scroll(browser: Any, site: _Site, tmp_path: Path, case: str) -> None:
    demo = json.loads(RECORDED.read_text(encoding="utf-8"))["GET /overview"]["body"]
    page, errors = _open(browser, site, demo if case == "demo" else _busy(demo), PHONE)
    page.screenshot(path=str(_shots(tmp_path) / f"overview-{case}-390.png"), full_page=True)
    assert page.evaluate(
        "document.documentElement.scrollWidth <= window.innerWidth"
    ), "no horizontal page scroll"
    assert page.locator("[data-new-campaign]").is_visible()
    assert errors == []
    page.context.close()


def test_the_keyboard_reaches_the_new_campaign_button_and_a_card_action(browser: Any, site: _Site) -> None:
    demo = json.loads(RECORDED.read_text(encoding="utf-8"))["GET /overview"]["body"]
    page, _ = _open(browser, site, _busy(demo), DESKTOP)
    reached: set[str] = set()
    for _ in range(60):
        page.keyboard.press("Tab")
        label = page.evaluate(
            "(() => { const e = document.activeElement; return e && e.closest('[data-module=\"overview\"]') ? (e.hasAttribute('data-new-campaign') ? 'new' : e.hasAttribute('data-card-action') ? 'card' : e.hasAttribute('data-how-counted') ? 'how' : '') : ''; })()"
        )
        if label:
            reached.add(label)
            outline = page.evaluate("getComputedStyle(document.activeElement).outlineStyle")
            assert outline != "none", "focus is visible"
    assert {"new", "card", "how"} <= reached
    page.context.close()
