"""Plan M M173 (DEC-1603): every route renders with a clean console, and every route is a click away.

Two checks, in a real browser against the real app on the validated-data demo store (`scripts.seed_validated`
on the committed Hillstrom sample: uploads, a risk model and a campaign-effect model, a contact list for each,
two measured campaigns and their Value Proof Packs - the store the manager demo shows).

**The sweep.** The routes are not typed here: the page says which prefixes its modules registered
(`ui/modules/router.js`'s `phaseModules()`, plus `uc`, which `ui/app.js` draws itself), and `SAMPLES` must have an
entry for every one of them, so a module that registers a new route fails this test until it is given a sample
page. Every sample page is opened fresh and must produce no console error, no uncaught exception, no failed
request and no HTTP 4xx/5xx. The one allowance is the Google font the sandbox cannot fetch
(`fonts.googleapis.com`, `fonts.gstatic.com`), which is the sandbox and not the app.

**The crawler.** From the top bar, breadth first, it follows every link (`a[href^="#/"]`) it finds - opening the
menus and `<details>` sections that hide links, never submitting a form and never pressing an action button -
and asserts that every registered route prefix was reached by a click. `#/signin`, `#/account` and `#/admin/...`
exist for a deployment with sign-in on (the bar's user menu links `#/account` and the Users and Audit log, and the
sign-in screen is where a 401 sends you; Settings draws its Admin group only then, because with sign-in off there is
one person who needs no user list), so with sign-in off, which is this app's default, they are the prefixes a click
cannot reach; they are checked for what they do say instead
(`test_the_sign_in_and_admin_screens_exist_and_say_sign_in_is_off`).

**What this does not cover, on purpose.** A scoring run that one of Plan J's campaigns measured has two answers to
"was it measured?": the campaign's report, which the value view reads, and the run's own `campaign-results`
file, which only the Campaign results page writes. The value view cannot say whether the second exists, so that
run's Campaign results tab and its outcomes page still ask for it and may log one 404. The sweep therefore opens
those two pages for the scoring run no campaign measured (the usual case) and says so here rather than hiding the
request behind an allow-list.

The module skips, rather than fails, when playwright or a browser is missing, and is slow (the seed trains two
models): it runs in the slow suite with the other browser journeys.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlparse

import pytest

pytestmark = [pytest.mark.slow, pytest.mark.integration]

sync_api = pytest.importorskip("playwright.sync_api", reason="the sweep needs a browser")

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
HOST: Final[str] = "127.0.0.1"
USE_CASE: Final[str] = "hillstrom-email"
FONT_HOSTS: Final[frozenset[str]] = frozenset({"fonts.googleapis.com", "fonts.gstatic.com"})
SIGN_IN_ONLY: Final[frozenset[str]] = frozenset({"signin", "account", "admin"})
SETTLE_MS: Final[int] = 900
PAGE_TIMEOUT_MS: Final[int] = 60_000
PER_PREFIX_CAP: Final[int] = 4
"""The crawler follows at most this many distinct pages under one route prefix (a list of runs would otherwise
be a page per run); reaching the prefix is what the test asserts."""


# --- the store and the app ------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def store(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The validated-data demo store, seeded from the committed sample (never the full file in `data/`).

    `UI_SWEEP_STORE` names a store that is already seeded (the `data` folder `scripts.seed_validated` printed),
    to save the minutes the seed takes while this file is being worked on."""
    from scripts import seed_validated

    existing = os.environ.get("UI_SWEEP_STORE")
    if existing:
        return Path(existing)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(seed_validated, "FULL_FILE", tmp_path_factory.getbasetemp() / "no-full-file.csv")
        result = seed_validated.seed(data_dir=tmp_path_factory.mktemp("sweep-store"), echo=lambda _line: None)
    assert result.is_sample, "the sweep seeds from the committed sample"
    return result.store


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((HOST, 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def server(store: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    port = _free_port()
    log = tmp_path_factory.mktemp("sweep-server") / "server.log"
    env = {
        **os.environ,
        "MARKETING_AI_DATA_DIR": str(store),
        "PYTHONPATH": str(REPO_ROOT),
        "PYTHONUNBUFFERED": "1",
    }
    env.pop("MARKETING_AI_DEMO_MODE", None)  # the real demo-less app: no sample-data chip, no auto tour
    with log.open("wb") as sink:
        process = subprocess.Popen(
            [
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
            ],
            cwd=REPO_ROOT,
            env=env,
            stdout=sink,
            stderr=subprocess.STDOUT,
        )
        base = f"http://{HOST}:{port}"
        try:
            deadline = time.monotonic() + 120
            while True:
                try:
                    with urllib.request.urlopen(f"{base}/healthz", timeout=5):
                        break
                except OSError:
                    if process.poll() is not None or time.monotonic() > deadline:
                        pytest.fail(log.read_text(errors="replace")[-3000:])
                    time.sleep(0.5)
            yield base
        finally:
            process.terminate()
            process.wait(timeout=30)


@pytest.fixture(scope="module")
def browser(server: str) -> Iterator[Any]:
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


def api(server: str, path: str) -> Any:
    with urllib.request.urlopen(f"{server}{path}", timeout=60) as response:
        return json.loads(response.read())


# --- what the page says it registered -------------------------------------------------------------------


def registered_prefixes(page: Any) -> set[str]:
    """Every first segment a module claimed (the router's `phaseModules()`), plus `uc` and `industry`, which
    `ui/app.js` itself draws (`industry` only to send an old link Home)."""
    modules = page.evaluate("import('/ui/modules/router.js').then((router) => router.phaseModules())")
    return {route for module in modules for route in module["routes"]} | {"uc", "industry"}


@dataclass(frozen=True)
class Entities:
    """The ids the parametrised routes need, read from the app itself."""

    train: str
    train_uplift: str
    score: str
    score_unmeasured: str
    campaigns: tuple[str, ...]


def entities(server: str) -> Entities:
    runs = api(server, "/runs")["runs"]
    done = [run for run in runs if run["state"] == "done" and run["use_case_id"] == USE_CASE]

    def newest(mode: str, *, uplift: bool) -> str:
        found = [r for r in done if r["mode"] == mode and (r["problem_type"] == "uplift") is uplift]
        assert found, f"the demo store has no finished {'uplift ' if uplift else ''}{mode} run"
        return str(found[0]["run_id"])

    score = newest("score", uplift=True)  # the campaign-effect model's list, which the two campaigns measured
    score_unmeasured = ""
    for run in (r for r in done if r["mode"] == "score"):
        view = api(server, f"/pilot/roi/{run['run_id']}")
        if view["status"] == "not_measured" and view["campaign_id"] is None:
            score_unmeasured = str(run["run_id"])
            break
    assert score_unmeasured, "the demo store has a scoring run nobody measured"
    campaigns = tuple(str(c["campaign_id"]) for c in api(server, "/campaigns")["campaigns"])
    assert campaigns, "the demo store has measured campaigns"
    return Entities(
        train=newest("train", uplift=False),
        train_uplift=newest("train", uplift=True),
        score=score,
        score_unmeasured=score_unmeasured,
        campaigns=campaigns,
    )


def samples(e: Entities) -> dict[str, list[str]]:
    """One or more pages for every route prefix. A module that registers a new prefix must be added here."""
    uc = USE_CASE
    runs = (e.train, e.train_uplift, e.score, e.score_unmeasured)
    return {
        "uc": [
            "#/",
            f"#/uc/{uc}",
            *[f"#/uc/{uc}/{page}/{run}" for run in runs for page in ("run", "data", "model", "output")],
            "#/uc/targeted-advertisement",
            "#/uc/win-back-campaign",
        ],
        "industry": ["#/industry/ecommerce"],
        "uplift": [
            "#/uplift",
            f"#/uplift/{uc}",
            f"#/uplift/{uc}/score",
            f"#/uplift/{uc}/model/{e.train_uplift}",
            f"#/uplift/{uc}/output/{e.train_uplift}",
        ],
        "campaign": [f"#/campaign/{uc}/{e.score_unmeasured}"],
        "campaigns": [f"#/campaigns/{c}" for c in e.campaigns]
        # Plan M M176 (DEC-1606): the Campaigns list and every tab of each campaign's page.
        + ["#/campaigns"]
        + [
            f"#/campaigns/{c}/{tab}"
            for c in e.campaigns
            for tab in ("overview", "audience", "results", "technical")
        ]
        # Plan M M177 (DEC-1607): the New campaign wizard.
        + ["#/campaigns/new"],
        "results": ["#/results"],
        # Plan M M192 (DEC-1622): Settings › AI service.
        "settings": ["#/settings", "#/settings/ai"],
        "help": ["#/help/glossary", "#/help/whats-new"],
        "connections": [
            "#/connections",
            "#/connections/new/s3",
            "#/connections/new/postgres",
            "#/connections/ai/product",
            "#/connections/ai/deliverable",
        ],
        "pilot": [
            "#/pilot",
            "#/pilot/kit",
            f"#/pilot/view/results/{uc}",
            f"#/pilot/view/readiness/{uc}",
            f"#/pilot/value/{e.score_unmeasured}",
            *[f"#/pilot/proof/{c}" for c in e.campaigns],
        ],
        "audit": ["#/audit"],
        # Plan M M175 (DEC-1605): the Overview.
        "overview": ["#/overview"],
        # Plan M M179 (DEC-1609): the Data page, one page per tab.
        "data": ["#/data", "#/data/check"],
        # Plan M M178 (DEC-1608): Reports, one page per tab (the Model reports tab draws the #/pilot lists).
        "reports": ["#/reports", "#/reports/programme", "#/reports/check", "#/reports/models"],
        "cost": ["#/cost"],
        "doctor": ["#/doctor"],
        "generative": ["#/generative/assistant/ai-onboarding-assistant", "#/generative/connection"],
        "signin": ["#/signin"],
        "account": ["#/account"],
        "admin": ["#/admin/users", "#/admin/audit"],
        "privacy": ["#/privacy/consent", "#/privacy/erasure", "#/privacy/access", "#/privacy/retention"],
        "monitoring": [
            "#/monitoring/schedules",
            "#/monitoring/alerts",
            "#/monitoring/missed",
            "#/monitoring/runs",
            f"#/monitoring/runs/{e.score_unmeasured}",
        ],
        "approvals": ["#/approvals"],
    }


# --- watching the console -------------------------------------------------------------------------------


@dataclass
class Watch:
    """What a page did wrong while it was open. Fonts the sandbox cannot fetch are the one thing left out."""

    problems: list[str] = field(default_factory=list)

    def attach(self, page: Any) -> None:
        def host(url: str) -> str:
            return urlparse(url).hostname or ""

        def console(message: Any) -> None:
            if message.type != "error":
                return
            if host(message.location.get("url", "")) in FONT_HOSTS:
                return
            self.problems.append(f"console.error: {message.text} ({message.location.get('url', '')})")

        def failed(request: Any) -> None:
            if host(request.url) not in FONT_HOSTS:
                self.problems.append(f"request failed: {request.url} {request.failure}")

        def response(reply: Any) -> None:
            if reply.status >= 400 and host(reply.url) not in FONT_HOSTS:
                self.problems.append(f"HTTP {reply.status}: {reply.url}")

        page.on("console", console)
        page.on("pageerror", lambda error: self.problems.append(f"uncaught: {error}"))
        page.on("requestfailed", failed)
        page.on("response", response)


def settle(page: Any) -> None:
    """The page's own requests finished, and its modules' repaints (one per module that registers) are done."""
    with contextlib.suppress(sync_api.TimeoutError):
        page.wait_for_load_state("networkidle", timeout=PAGE_TIMEOUT_MS)
    page.wait_for_timeout(SETTLE_MS)
    with contextlib.suppress(sync_api.TimeoutError):
        page.wait_for_load_state("networkidle", timeout=PAGE_TIMEOUT_MS)


def new_page(browser: Any) -> Any:
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    context.set_default_timeout(PAGE_TIMEOUT_MS)
    return context.new_page()


# --- the sweep ------------------------------------------------------------------------------------------


def test_every_registered_route_has_a_sample_page(server: str, browser: Any) -> None:
    page = new_page(browser)
    page.goto(f"{server}/ui/")
    settle(page)
    registered = registered_prefixes(page)
    covered = set(samples(entities(server)))
    assert registered == covered, (
        f"routes without a sample page: {sorted(registered - covered)}; "
        f"sample pages for routes nobody registered: {sorted(covered - registered)}"
    )
    page.context.close()


def test_no_route_logs_a_console_error(server: str, browser: Any) -> None:
    found: dict[str, list[str]] = {}
    for hashes in samples(entities(server)).values():
        for hash_ in hashes:
            page = new_page(browser)
            watch = Watch()
            watch.attach(page)
            page.goto(f"{server}/ui/{hash_}")
            settle(page)
            if watch.problems:
                found[hash_] = sorted(set(watch.problems))
            page.context.close()
    assert not found, "console errors on:\n" + "\n".join(
        f"  {h}\n    " + "\n    ".join(p) for h, p in found.items()
    )


def test_a_page_that_logs_an_error_is_caught(server: str, browser: Any) -> None:
    """The sweep's own check: a request for something that is not there is reported, and a blocked font is not."""
    page = new_page(browser)
    watch = Watch()
    watch.attach(page)
    page.goto(f"{server}/ui/#/")
    settle(page)
    page.evaluate("fetch('/runs/r_nothing_here/campaign-results').catch(() => {})")
    page.wait_for_timeout(500)
    assert any("r_nothing_here" in problem for problem in watch.problems), watch.problems
    page.context.close()


# --- the crawler ----------------------------------------------------------------------------------------

LINKS: Final[str] = """
() => [...new Set([...document.querySelectorAll('a[href^="#/"]')]
    .filter((a) => !a.hasAttribute('download'))
    .map((a) => a.getAttribute('href')))]
"""

LINK_DRAWN: Final[str] = """
(href) => [...document.querySelectorAll('a[href]')].some((a) => a.getAttribute('href') === href)
"""

CLICK: Final[str] = """
(href) => {
  const link = [...document.querySelectorAll('a[href]')].find((a) => a.getAttribute('href') === href);
  if (!link) return false;
  // Open what hides it: every <details> around it, and the menu (by its button) it sits in.
  for (let el = link.parentElement; el; el = el.parentElement) {
    if (el.tagName === 'DETAILS') el.open = true;
    if (el.classList && el.classList.contains('menu-pop') && el.hidden) {
      const trigger = el.parentElement && el.parentElement.querySelector('[data-menu]');
      if (trigger) trigger.click();
    }
  }
  // One synchronous step, so a repaint between finding the link and pressing it cannot lose it.
  const live = [...document.querySelectorAll('a[href]')].find((a) => a.getAttribute('href') === href);
  if (!live) return false;
  live.click();
  return true;
}
"""


def prefix_of(hash_: str) -> str:
    parts = [p for p in hash_.removeprefix("#").strip("/").split("/") if p]
    return parts[0] if parts else ""


def test_the_crawler_reaches_every_route_by_clicks(server: str, browser: Any) -> None:
    page = new_page(browser)
    page.goto(f"{server}/ui/")
    settle(page)
    wanted = registered_prefixes(page) - {"industry"} - SIGN_IN_ONLY
    reached: dict[str, str] = {"": "#/"}
    per_prefix: dict[str, int] = {}
    seen: set[str] = {"#/"}
    queue: deque[tuple[str, str]] = deque((href, "#/") for href in page.evaluate(LINKS))
    for href, _parent in queue:
        seen.add(href)
    clicks = 0
    while queue and (wanted - set(reached)):
        href, parent = queue.popleft()
        prefix = prefix_of(href)
        if per_prefix.get(prefix, 0) >= PER_PREFIX_CAP and prefix in reached:
            continue
        if page.evaluate("location.hash || '#/'") != parent:
            page.evaluate("(hash) => { location.hash = hash; }", parent)
            try:  # back on the parent once the link we came for is drawn again
                page.wait_for_function(LINK_DRAWN, arg=href, timeout=15_000)
            except sync_api.TimeoutError:
                settle(page)
        if not page.evaluate(CLICK, href):
            continue  # it was on an earlier version of the page (a repaint): another route to it will be found
        clicks += 1
        settle(page)
        landed = page.evaluate("location.hash || '#/'")
        landed_prefix = prefix_of(landed)
        per_prefix[landed_prefix] = per_prefix.get(landed_prefix, 0) + 1
        reached.setdefault(landed_prefix, landed)
        for found in page.evaluate(LINKS):
            if found not in seen:
                seen.add(found)
                queue.append((found, landed))
    missing = wanted - set(reached)
    assert not missing, (
        f"not reachable by clicks from the top bar: {sorted(missing)} "
        f"(after {clicks} clicks; reached {sorted(reached)})"
    )
    page.context.close()


def press_until_focused(page: Any, selector: str, limit: int = 40) -> None:
    """Tab through the page until `selector` has focus: how a keyboard user reaches a control."""
    for _ in range(limit):
        if page.evaluate("(sel) => document.activeElement && document.activeElement.matches(sel)", selector):
            return
        page.keyboard.press("Tab")
    pytest.fail(f"{selector} was not reached with Tab in {limit} presses")


def test_the_help_menu_works_from_the_keyboard_and_the_tour_starts_without_demo_mode(
    server: str, browser: Any
) -> None:
    """The acceptance of M173's Help menu, in a real browser with demo mode OFF (this app has no sample-data chip)."""
    page = new_page(browser)
    watch = Watch()
    watch.attach(page)
    page.goto(f"{server}/ui/#/")
    settle(page)
    assert page.locator(".pe-demo").count() == 0, "demo mode is off"
    press_until_focused(page, ".tb-help-btn")
    page.keyboard.press("Enter")
    assert page.get_attribute(".tb-help-btn", "aria-expanded") == "true"
    page.keyboard.press("ArrowDown")
    entries = page.eval_on_selector_all(
        "#tn-help a, #tn-help button", "(els) => els.map((e) => e.textContent.trim())"
    )
    assert entries == ["Take the tour", "Glossary", "Send feedback", "What's new"]
    assert page.evaluate("document.activeElement.textContent.trim()") == "Take the tour"

    # Take the tour: it starts on the real pages, and each of its first steps finds its own elements.
    page.keyboard.press("Enter")
    page.locator("#pe-tour").wait_for(timeout=PAGE_TIMEOUT_MS)
    assert "Step 1 of 6" in page.inner_text("#pe-tour")
    for index, (title, hash_) in enumerate(
        [("Home", "#/"), ("Connections", "#/connections"), ("Results", "#/results")]
    ):
        page.wait_for_function(
            f"document.querySelector('#pe-tour h2').textContent === {json.dumps(title)}",
            timeout=PAGE_TIMEOUT_MS,
        )
        page.wait_for_function(f"location.hash === {json.dumps(hash_)}", timeout=PAGE_TIMEOUT_MS)
        assert f"Step {index + 1} of 6" in page.inner_text("#pe-tour")
        page.wait_for_selector(".pe-target", timeout=PAGE_TIMEOUT_MS)
        page.locator("#pe-tour [data-pe-tour='next']").click()
    page.keyboard.press("Escape")
    assert page.locator("#pe-tour").count() == 0, "Escape closes the tour"
    assert page.evaluate(
        "document.activeElement.matches('.tb-help-btn')"
    ), "and focus is back on the Help button"

    # Glossary and What's new: arrow to the entry, Enter, and its page opens with the menu closed again.
    for presses, label in ((2, "Glossary"), (4, "What's new")):
        page.focus(".tb-help-btn")
        page.keyboard.press("Enter")
        for _ in range(presses):
            page.keyboard.press("ArrowDown")
        assert page.evaluate("document.activeElement.textContent.trim()") == label
        page.keyboard.press("Enter")
        page.wait_for_function(
            f"document.querySelector('main h1') && document.querySelector('main h1').textContent === {json.dumps(label)}"
        )
        assert (
            page.get_attribute(".tb-help-btn", "aria-expanded") == "false"
        ), "following an entry closes the menu"

    # Send feedback opens the form, with focus inside it; Escape closes it and puts focus back on Help.
    page.focus(".tb-help-btn")
    page.keyboard.press("Enter")
    for _ in range(3):
        page.keyboard.press("ArrowDown")
    assert page.evaluate("document.activeElement.textContent.trim()") == "Send feedback"
    page.keyboard.press("Enter")
    page.locator("#pe-fb").wait_for(state="visible")
    assert page.evaluate("document.activeElement.closest('#pe-fb') !== null")
    page.keyboard.press("Escape")
    assert not page.locator("#pe-fb").is_visible()
    assert watch.problems == [], watch.problems
    page.context.close()


def test_the_sign_in_and_admin_screens_exist_and_say_sign_in_is_off(server: str, browser: Any) -> None:
    """`#/signin`, `#/account` and `#/admin/users` have no link while sign-in is off; they still answer."""
    page = new_page(browser)
    for hash_ in ("#/signin", "#/account", "#/admin/users"):
        page.goto(f"{server}/ui/{hash_}")
        settle(page)
        assert "Sign-in is turned off" in page.inner_text("#app"), hash_
    page.context.close()
