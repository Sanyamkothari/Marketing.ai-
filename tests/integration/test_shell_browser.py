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


# --- Plan M M185 (DEC-1615): the look, in the real style sheet --------------------------------------------------------

BRAND_BLUE: Final[str] = "rgb(30, 87, 189)"
CAMPAIGN_PAGE: Final[str] = "#/campaigns/c_20261011_c9276210"
INTER_LOADED: Final[str] = (
    "document.fonts.ready.then(() => [...document.fonts].some("
    "(f) => f.family.replace(/\"/g, '') === 'Inter Variable' && f.status === 'loaded'))"
)


def _box(page: Any, selector: str) -> dict[str, float]:
    box = page.locator(selector).first.bounding_box()
    assert box is not None, f"{selector} is not drawn"
    return dict(box)


def _style(page: Any, selector: str, prop: str) -> str:
    return str(page.locator(selector).first.evaluate(f"el => getComputedStyle(el).{prop}"))


def test_the_look_on_a_wide_screen(
    browser: Any, campaigns_server: str, classic_server: str, shots: Path
) -> None:
    """Inter is shipped and used; no page card; the product mark tops the sidebar, Minfy sits at its foot; brand blue
    primary buttons, active place and selected tab; the page's one primary action on the right of its title.
    """
    problems: list[str] = []
    page = _page(browser, campaigns_server, width=1440, height=900, problems=problems)
    _open(page, campaigns_server, "#/campaigns")
    page.wait_for_selector("#app .head-actions .btn.primary")
    assert page.evaluate(INTER_LOADED) is True, "Inter Variable loaded from ui/fonts/inter"
    assert _style(page, "body", "fontFamily").startswith('"Inter Variable"')
    # no page card: the page sits on the canvas, at most 1200 px wide
    assert _style(page, "#app .screen", "backgroundColor") == "rgba(0, 0, 0, 0)"
    assert _style(page, "#app .screen", "borderTopWidth") == "0px"
    assert _box(page, "#app .screen")["width"] <= 1200
    assert _style(page, "body", "backgroundColor") == "rgb(245, 246, 248)"
    # the shell: the name and mark at the top of the sidebar, "by Minfy" at its foot, the top bar 56 px
    word = _box(page, "#pb-bar a.wordmark")
    assert word["x"] == 0 and word["y"] == 0 and word["width"] == 216 and word["height"] == 56
    logo = _box(page, "#pb-bar a.tb-logo")
    assert logo["x"] == 0 and logo["y"] + logo["height"] == 900, "Minfy at the sidebar's foot"
    assert (
        page.locator("#pb-bar a.tb-logo").evaluate("el => getComputedStyle(el, '::before').content") == '"by"'
    )
    side = _box(page, "#pb-bar .sh-side")
    assert side["y"] == 0 and side["height"] == 900, "the sidebar runs the full height"
    assert _box(page, "#pb-bar .sh-row")["height"] == 56
    # brand colour: the active place (with its bar), the primary button
    assert _style(page, "#pb-bar a.sh-item[aria-current='page']", "color") == BRAND_BLUE
    bar = page.locator("#pb-bar a.sh-item[aria-current='page']").evaluate(
        "el => getComputedStyle(el, '::before').backgroundColor"
    )
    assert bar == BRAND_BLUE, "a 3 px brand bar marks the place"
    primary = "#app .head-actions .btn.primary"
    assert _style(page, primary, "backgroundColor") == BRAND_BLUE
    assert _style(page, primary, "color") == "rgb(255, 255, 255)"
    assert _box(page, primary)["height"] == 40
    # the header: the one primary action on the right of the title, on the same band
    title, action = _box(page, "#app .h1"), _box(page, primary)
    assert action["x"] > title["x"] + title["width"], "the primary action is to the right of the title"
    assert action["y"] < title["y"] + title["height"] + 40
    page.screenshot(path=str(shots / "m185_campaigns_1440.png"))
    # underline tabs: the selected tab is brand text over a 2 px brand line, not a filled pill
    _open(page, campaigns_server, CAMPAIGN_PAGE)
    # M186 (DEC-1616): the campaign page draws components.js `tabs()` (role=tab, aria-selected); it drew `.tabs .tab.on`
    on = "#app [data-ds-tabs] [role='tab'][aria-selected='true']"
    page.wait_for_selector(on)
    assert _style(page, on, "color") == BRAND_BLUE
    assert _style(page, on, "borderBottomColor") == BRAND_BLUE
    assert _style(page, on, "borderBottomWidth") == "2px"
    assert _style(page, on, "backgroundColor") == "rgba(0, 0, 0, 0)"
    # a status chip carries an icon and its word, in the status colours (Measured = good)
    # M186: the chip is components.js `statusChip` (an inline 12 px icon); it was the `.chip.cp-status` bridge
    chip = "#app .head .badge[data-status='measured']"
    assert _style(page, chip, "color") == "rgb(21, 128, 61)"
    assert page.locator(chip).evaluate("el => el.querySelector('svg').getBoundingClientRect().width") == 12
    assert page.locator(chip).inner_text().strip() == "Measured"
    page.screenshot(path=str(shots / "m185_campaign_1440.png"))
    page.context.close()
    # the classic layout shares the font, the tokens and the components; its markup is not reworked
    classic = _page(browser, classic_server, width=1440, height=900, problems=problems)
    _open(classic, classic_server, "#/settings")
    classic.wait_for_selector("#pb-bar .topnav")
    assert classic.evaluate(INTER_LOADED) is True
    assert _style(classic, "#app .screen", "backgroundColor") == "rgba(0, 0, 0, 0)"
    assert _style(classic, "#pb-bar .tn-item.on", "color") == BRAND_BLUE
    assert classic.locator("#pb-bar .sh-side").count() == 0
    classic.screenshot(path=str(shots / "m185_classic_1440.png"))
    classic.context.close()
    assert problems == []


def test_the_look_on_a_phone(browser: Any, campaigns_server: str, shots: Path) -> None:
    """At 390 px: no sideways scroll on any main page, the header's action below its text, Minfy only in the drawer."""
    problems: list[str] = []
    page = _page(browser, campaigns_server, width=390, height=844, problems=problems)
    for hash_ in (
        "#/overview",
        "#/campaigns",
        CAMPAIGN_PAGE,
        "#/campaigns/new",
        "#/data",
        "#/reports",
        "#/settings",
    ):
        _open(page, campaigns_server, hash_)
        page.wait_for_selector("#app h1")
        assert page.evaluate(
            "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
        ), f"{hash_} scrolls sideways at 390 px"
        assert _box(page, "#pb-bar .sh-row")["height"] <= 60, hash_
    _open(page, campaigns_server, "#/campaigns")
    page.wait_for_selector("#app .head-actions .btn.primary")
    desc, action = _box(page, "#app .head .desc"), _box(page, "#app .head-actions .btn.primary")
    assert action["y"] >= desc["y"] + desc["height"], "the primary action sits below the text on a phone"
    assert action["x"] <= desc["x"] + 1, "and starts at the text's left edge"
    assert page.locator(
        "#pb-bar a.tb-logo"
    ).is_hidden(), "the folded bar is the name, Menu, the bell and Help"
    # the bell and Help stay 40 x 40 touch targets on a phone (visual_common section 7)
    for target in ("#pb-bar a.sh-icon.sh-bell", "#pb-bar .sh-help > .sh-icon"):
        box = _box(page, target)
        assert box["width"] >= 40 and box["height"] >= 40, f"{target} is {box['width']} x {box['height']}"
    page.locator("#pb-bar .sh-menu-btn").click()
    assert page.locator("#pb-bar .sh-side").is_visible()
    logo = _box(page, "#pb-bar a.tb-logo")
    assert logo["y"] + logo["height"] == 844, "the open drawer ends with Minfy"
    drawer = _box(page, "#pb-bar .sh-side")
    assert drawer["x"] == 0 and drawer["width"] <= 390
    page.screenshot(path=str(shots / "m185_campaigns_390_drawer.png"))
    page.keyboard.press("Escape")
    assert page.locator("#pb-bar .sh-side").is_hidden()
    assert problems == []
    page.context.close()


# The helpers M186-M188 build every page from, drawn by ui/components.js into the running app (index.html's styles
# and theme.css): a KPI row with a unit and a value range, status chips in a card, and a stepper with links back.
PROBE: Final[str] = """async () => {
  const c = await import(new URL('./components.js', document.baseURI).href);
  const screen = document.createElement('div');
  screen.className = 'screen';
  screen.id = 'm185-probe';
  screen.innerHTML =
    c.kpiRow([
      c.statTile({ label: 'Customers', value: '12,862', unit: 'customers' }),
      c.statTile({
        label: 'Extra revenue',
        value: '₹2.6 lakh',
        range: { low: 88187, high: 418000, estimate: 260000, text: 'Likely 88,187 to 4.18 lakh' },
      }),
      c.statTile({ label: 'Lift', value: '3.1%', range: { low: -0.4, high: 6.2, estimate: 3.1, text: 'Likely -0.4% to 6.2%' } }),
    ]) +
    '<div class="card" id="m185-chips" style="padding:16px">' +
    ['draft', 'live', 'waiting', 'measured'].map((v) => c.statusChip('campaign', v)).join('') +
    c.statusChip('health', 'failed') + c.tagChip('Win-back') + '</div>' +
    '<button type="button" id="m185-before">Before</button>' +
    c.stepper({
      steps: [
        { label: 'Goal', href: '#/campaigns/new?step=goal' },
        { label: 'Customers', href: '#/campaigns/new?step=customers' },
        'Offer',
        'Check',
        'Export',
      ],
      current: 2,
    });
  const app = document.querySelector('#app');
  app.replaceChildren(screen);
  return true;
}"""


def test_the_components_hold_on_a_phone(browser: Any, campaigns_server: str, shots: Path) -> None:
    """M185 review: at 390 px a tile's unit wraps whole (never "cust/omers"), a value range's track stays inside its
    tile, a chip in a card keeps its own width, and a stepper's links back are reachable by Tab with a focus ring.
    """
    problems: list[str] = []
    page = _page(browser, campaigns_server, width=390, height=844, problems=problems)
    _open(page, campaigns_server, "#/campaigns")
    page.wait_for_selector("#app h1")
    assert page.evaluate(PROBE) is True
    page.wait_for_selector("#m185-probe .stepper")
    page.evaluate("document.fonts.ready")
    # the unit is one line box: it moves to its own line as a whole word, and the value never breaks mid-number
    unit = page.locator("#m185-probe .stat .stat-unit").first
    assert unit.evaluate("el => el.getClientRects().length") == 1, "the unit breaks mid-word"
    assert unit.evaluate("el => el.textContent") == "customers"
    for value in page.locator("#m185-probe .stat .stat-value").all():
        lines = value.evaluate(
            "el => { const r = document.createRange(); r.selectNodeContents(el.firstChild);"
            " return new Set([...r.getClientRects()].map((b) => Math.round(b.top))).size; }"
        )
        assert lines == 1, f"the figure {value.inner_text()!r} breaks mid-number"
    # every value range's track ends at or inside its tile
    outside = page.evaluate(
        "() => [...document.querySelectorAll('#m185-probe .vr-track')].map((t) => {"
        " const tile = t.closest('.stat').getBoundingClientRect(); const box = t.getBoundingClientRect();"
        " return [box.left - tile.left, tile.right - box.right, box.width]; })"
    )
    assert len(outside) == 2
    for left, right, width in outside:
        assert left >= 0 and right >= 0, f"a value range's track leaves its tile ({left}, {right})"
        assert 64 <= width <= 160
    # a chip in a card (a flex column) keeps its own width
    card = _box(page, "#m185-chips")
    for chip in page.locator("#m185-chips .badge, #m185-chips .tag").all():
        box = chip.bounding_box()
        assert (
            box is not None and box["width"] < card["width"] / 2
        ), f"{chip.inner_text()!r} stretches to a band"
    # the stepper's done steps are links with a box, reached by Tab, drawn with the 2 px brand ring
    page.focus("#m185-before")
    for name in ("Goal", "Customers"):
        page.keyboard.press("Tab")
        focused = page.evaluate(
            "() => { const a = document.activeElement; const r = a.getBoundingClientRect(); const s = getComputedStyle(a);"
            " return { tag: a.tagName, href: a.getAttribute('href'), in: !!a.closest('.stepper .st-step.done'),"
            " w: r.width, h: r.height, outline: s.outlineStyle, ow: s.outlineWidth, oc: s.outlineColor }; }"
        )
        assert focused["tag"] == "A" and focused["in"], f"Tab skips the {name} link back: {focused}"
        assert focused["href"].endswith(name.lower())
        assert focused["w"] > 0 and focused["h"] > 0, f"the {name} link has no box"
        assert (focused["outline"], focused["ow"], focused["oc"]) == ("solid", "2px", BRAND_BLUE)
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    page.screenshot(path=str(shots / "m185_components_390.png"), full_page=True)
    assert problems == []
    page.context.close()


def test_the_older_status_chips_carry_an_icon(browser: Any, campaigns_server: str) -> None:
    """M185 review: Reports' evidence label and Data's health chip show the status mapping's icon with their word.
    Since M188 both are components.js `statusChip` (an inline 12 px icon), so the theme's bridges for them were
    removed at integration; Connections' `.cn-badge[data-cn-status]` chips keep the bridge's 12 px `::before` icon.
    """
    problems: list[str] = []
    page = _page(browser, campaigns_server, width=1440, height=900, problems=problems)
    icon_width = "el => el.querySelector('svg').getBoundingClientRect().width"
    _open(page, campaigns_server, "#/reports/value-proof")
    page.wait_for_selector("#app [data-claim] .badge")
    proven = page.locator("#app [data-claim='proven'] .badge").first
    assert proven.evaluate(icon_width) == 12
    assert proven.evaluate("el => getComputedStyle(el).color") == "rgb(21, 128, 61)"
    assert proven.inner_text().strip() == "Proven"
    for chip in page.locator("#app [data-claim] .badge").all():
        assert chip.evaluate(icon_width) == 12, chip.inner_text()
    _open(page, campaigns_server, "#/data")
    page.wait_for_selector("#app .badge[data-dt-status='read']")
    read = page.locator("#app .badge[data-dt-status='read']").first
    assert read.evaluate(icon_width) == 12
    assert read.evaluate("el => getComputedStyle(el).color") == "rgb(21, 128, 61)"
    assert read.inner_text().strip() == "Read correctly"
    _open(page, campaigns_server, "#/connections/ai/product")
    page.wait_for_selector("#app .cn-badge[data-cn-status]")
    for chip in page.locator("#app .cn-badge[data-cn-status]").all():
        assert chip.evaluate("el => getComputedStyle(el, '::before').width") == "12px", chip.inner_text()
    assert problems == []
    page.context.close()
