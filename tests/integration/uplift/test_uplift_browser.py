"""Plan B §8 and §11, clicked through the uplift screens in a real browser.

`tests/unit/uplift/uplift_ui*.test.mjs` render every uplift screen from JSON in node, and
`tests/integration/uplift/test_uplift_api.py` drives the API in process; neither ever opened a page.
This module does, the way `tests/integration/test_acceptance.py` does for Phase 1: it starts the real
app under uvicorn on an isolated data directory, opens `/ui` in Chromium and walks the journey
through the markup only - no request is made to the API by the test itself.

The journey, in order:

1. Overview -> the top bar's Models › "Uplift models" -> Win-back Campaign -> uplift Setup.
2. Upload a randomised campaign (`make_uplift_data`), see the treatment column detected and the
   Uplift problem type explained, click "Train uplift model", watch Running, reach Results.
3. Model page: the Qini chart with its random line, AUUC with its interval, the decile bars, an
   off-policy estimate. Output page: four segments, "Recommended to contact", expected incremental
   conversions with an interval.
4. Score a new file through Phase 1's own use-case screen ("Score new data", the uplift model in the
   dropdown), open its Output page, then follow the "Campaign results" block the module offers on
   the run's Results (a run action, `runActionsHtml`).
5. Campaign results: download the treat list, upload outcomes (`make_winback_campaign` +
   `outcomes_for`), judge them as of a date before the 90-day window has elapsed ("Results available
   on <date>"), then after it (the lift with its interval).
6. A targeted file: the TREATMENT_NOT_RANDOM refusal, the acknowledgement, and the "not causal"
   banner on the run's Model and Output pages.
7. Every uplift screen again at 390px wide: no horizontal page scroll.

On every screen the browser console must stay clean: no uncaught exception, no `console.error`,
and no failed request other than the ones the contract defines as "not produced yet" (a `404` for
an artefact a run writes only on request, a campaign never measured) or the one refusal the journey
provokes on purpose (`409` from `POST /uplift/runs`). The numbers the screens show are checked
against the artefacts the run wrote, read from the data directory, and a missing value must be "—".

**The compute budget is the one thing that differs from a user's run.** The shipped uplift default
is an X-learner on AutoGluon with 200 bootstrap resamples; the app is started against a *copy* of
`configs/` whose `win_back_campaign.yaml` sets `uplift.base_model: lightgbm` and 30 resamples, so a
training run takes seconds. The screens never expose that edit.

The browser runs in India's time zone on purpose: a date picked on the Campaign results page must
read back as that date there, not as the UTC instant (it once read "02 Jun, 5:29 am" for 1 June).

The module **skips** rather than fails when playwright or a usable browser is missing. Screenshots
go to the test's temporary directory, or to `$UPLIFT_BROWSER_SHOTS` when that is set.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Final

import pandas as pd
import pytest
import yaml

from tests.fixtures.make_uplift_data import make_uplift_data, make_winback_campaign, outcomes_for

pytestmark = [pytest.mark.slow, pytest.mark.integration]

sync_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright is not installed; the uplift journey needs a browser"
)

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
USE_CASE: Final[str] = "win-back-campaign"
USE_CASE_NAME: Final[str] = "Win-back Campaign"
PRIMARY_KEY: Final[str] = "customer_id"
TARGET: Final[str] = "reactivated_90d"
TREATMENT: Final[str] = "treatment"
EM_DASH: Final[str] = "—"
NOT_RANDOM: Final[str] = "TREATMENT_NOT_RANDOM"

TRAIN_ROWS: Final[int] = 6000
CAMPAIGN_ROWS: Final[int] = 3000
TARGETED_ROWS: Final[int] = 4000

HOST: Final[str] = "127.0.0.1"
HEALTH_TIMEOUT_S: Final[float] = 120.0
RUN_TIMEOUT_S: Final[float] = 600.0
POLL_MS: Final[int] = 500
ACTION_TIMEOUT_MS: Final[int] = 30_000
UPLOAD_TIMEOUT_MS: Final[int] = 120_000
TIMEZONE: Final[str] = "Asia/Kolkata"
MOBILE_WIDTH: Final[int] = 390

IMMATURE_AS_OF: Final[str] = "2026-06-01"
"""Before 2026-07-30, when the 90-day window of a campaign sent on 2026-05-01 has elapsed."""
MATURE_AS_OF: Final[str] = "2026-08-15"

TEST_BUDGET: Final[str] = """
# --- appended by tests/integration/uplift/test_uplift_browser.py, never committed to configs/ ---
# The shipped default is an X-learner on AutoGluon with 200 bootstrap resamples: minutes per run.
uplift:
  base_model: lightgbm
  bootstrap_samples: 30
"""

EXPECTED_404: Final[tuple[str, ...]] = ("/uplift/ope_report.json", "/campaign-results")
"""GETs the contract answers `404` for "not produced yet": OPE runs on request, and a campaign has no
report until outcomes are uploaded. Every other 4xx/5xx on a screen is a failure."""


# --- the app, in a browser-reachable process ------------------------------------------------------


@pytest.fixture(scope="module")
def workdir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("uplift-browser")


@pytest.fixture(scope="module")
def shots(workdir: Path) -> Path:
    target = Path(os.environ.get("UPLIFT_BROWSER_SHOTS") or workdir / "shots")
    target.mkdir(parents=True, exist_ok=True)
    return target


@pytest.fixture(scope="module")
def config_root(workdir: Path) -> Path:
    """A copy of `configs/` carrying the test budget, so the checkout's own configs stay untouched."""
    root = workdir / "configs"
    shutil.copytree(REPO_ROOT / "configs", root)
    path = root / "use_cases" / "win_back_campaign.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "uplift" not in document, (
        "win_back_campaign.yaml now sets `uplift:` itself; appending a second block would silently "
        "shadow it. Merge the test budget into that block instead."
    )
    path.write_text(path.read_text(encoding="utf-8") + TEST_BUDGET, encoding="utf-8")
    return root


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((HOST, 0))
        return int(sock.getsockname()[1])


def log_tail(log: Path, lines: int = 40) -> str:
    try:
        return "\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError as error:  # pragma: no cover - only when the log itself is unreadable
        return f"(no server log: {error})"


@dataclass(frozen=True)
class Server:
    base_url: str
    data_dir: Path
    log: Path

    def artefact(self, run_id: str, name: str) -> Any:
        """A run's artefact as the engine wrote it - what the screen's numbers must come from."""
        return json.loads((self.data_dir / "runs" / run_id / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def server(workdir: Path, config_root: Path) -> Iterator[Server]:
    data_dir = workdir / "data"
    data_dir.mkdir()
    port = free_port()
    base_url = f"http://{HOST}:{port}"
    log = workdir / "server.log"
    env = {
        **os.environ,
        "MARKETING_AI_CONFIG_DIR": str(config_root),
        "MARKETING_AI_DATA_DIR": str(data_dir),
        "PYTHONPATH": str(REPO_ROOT),
        "PYTHONUNBUFFERED": "1",
    }
    command = [sys.executable, "-m", "uvicorn", "api.main:app", "--host", HOST, "--port", str(port)]
    with log.open("wb") as sink:
        process = subprocess.Popen(
            [*command, "--log-level", "warning"], cwd=workdir, env=env, stdout=sink, stderr=subprocess.STDOUT
        )
        try:
            deadline = time.monotonic() + HEALTH_TIMEOUT_S
            while True:
                if process.poll() is not None:
                    pytest.fail(f"the server exited with {process.returncode}:\n{log_tail(log)}")
                try:
                    with urllib.request.urlopen(f"{base_url}/healthz", timeout=5) as response:
                        if response.status == 200:
                            break
                except OSError:
                    pass
                if time.monotonic() > deadline:
                    pytest.fail(f"/healthz never answered:\n{log_tail(log)}")
                time.sleep(0.5)
            yield Server(base_url=base_url, data_dir=data_dir, log=log)
        finally:
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:  # pragma: no cover - a wedged trainer
                process.kill()
                process.wait(timeout=30)


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


# --- watching the console -------------------------------------------------------------------------


class Console:
    """Everything a page complained about since the last `take()`.

    `phase1` is set while the journey is on a Phase 1 screen (`ui/usecase.js`, `ui/pages.js`). Those
    pages ask `GET /runs/{id}/artefacts/{name}` for every artefact they can draw and render a `404` as
    "—"; an uplift run writes no `prepare.json` (DEC-647), `drift.json` (DEC-648) or `decile_lift.json`,
    so those 404s are Phase 1's own contract, not this module's. Uncaught errors and `console.error`
    still fail a Phase 1 screen.
    """

    def __init__(self) -> None:
        self.problems: list[str] = []
        self.phase1 = False

    def attach(self, page: Any) -> None:
        page.on("pageerror", lambda error: self.problems.append(f"uncaught: {error}"))
        page.on("console", self._console)
        page.on("response", self._response)
        page.on("requestfailed", self._failed)

    def _console(self, message: Any) -> None:
        # The browser echoes every failed request as "Failed to load resource"; the request itself is
        # judged in `_response` / `_failed`, which know its URL.
        if message.type == "error" and not message.text.startswith("Failed to load resource"):
            self.problems.append(f"console.error: {message.text}")

    def _response(self, response: Any) -> None:
        if response.status < 400:
            return
        url, method = response.url, response.request.method
        expected = (response.status == 404 and method == "GET" and url.endswith(EXPECTED_404)) or (
            response.status == 409 and method == "POST" and url.endswith("/uplift/runs")
        )
        if self.phase1 and response.status == 404 and method == "GET" and "/artefacts/" in url:
            expected = True
        if not expected:
            self.problems.append(f"HTTP {response.status} {method} {url}")

    def _failed(self, request: Any) -> None:
        url, failure = request.url, str(request.failure)
        if "fonts.googleapis.com" in url or "fonts.gstatic.com" in url:
            return  # refused on purpose by `new_context`
        if url.endswith("/scores.csv") and "ERR_ABORTED" in failure:
            return  # the treat-list link became a download, which aborts the navigation by design
        self.problems.append(f"request failed: {url} ({failure})")

    def take(self) -> list[str]:
        taken, self.problems = self.problems, []
        return taken


def new_context(browser: Any, **options: Any) -> Any:
    context = browser.new_context(accept_downloads=True, timezone_id=TIMEZONE, **options)
    # The page asks Google Fonts for Inter; the journey must not depend on the internet.
    context.route("https://fonts.googleapis.com/**", lambda route: route.abort())
    context.route("https://fonts.gstatic.com/**", lambda route: route.abort())
    return context


# --- reading the screens --------------------------------------------------------------------------


def tiles(page: Any) -> dict[str, str]:
    found: dict[str, str] = {}
    for tile in page.locator(".kpis .kpi").all():
        found[tile.locator(".l").inner_text().strip()] = tile.locator(".v").inner_text().strip()
    return found


def key_values(page: Any) -> dict[str, str]:
    """`.kv` rows as key -> value; the uplift pages never repeat a key within one screen's cards."""
    rows: dict[str, str] = {}
    for row in page.locator(".kv").all():
        rows.setdefault(row.locator(".k").inner_text().strip(), row.locator(".v").inner_text().strip())
    return rows


def numbers(text: str) -> list[float]:
    """Every number in a rendered string, with the real minus sign and thousands separators read."""
    cleaned = text.replace("−", "-").replace(",", "")
    return [float(match) for match in re.findall(r"[-+]?\d+(?:\.\d+)?", cleaned)]


def close(shown: float, value: float, places: int) -> bool:
    """`shown` is `value` rounded to `places` decimals, as `fmtNum` rounds it."""
    return abs(shown - value) <= 0.5 * 10.0**-places + 1e-9


@dataclass
class Screen:
    """One screen as it was seen: its text, KPI tiles, key-value rows and what the console said."""

    url: str = ""
    text: str = ""
    tiles: dict[str, str] = field(default_factory=dict)
    rows: dict[str, str] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


def capture(page: Any, console: Console, shots: Path, name: str, **extra: Any) -> Screen:
    page.wait_for_timeout(300)  # a late re-render or entry link lands inside this
    page.screenshot(path=str(shots / f"{name}.png"), full_page=True)
    return Screen(
        url=page.url,
        text=page.locator("#app").inner_text(),
        tiles=tiles(page),
        rows=key_values(page),
        problems=console.take(),
        extra=extra,
    )


def wait_for(page: Any, predicate: Callable[[], bool], what: str, log: Path) -> None:
    deadline = time.monotonic() + RUN_TIMEOUT_S
    while not predicate():
        if page.locator(".summary .bad").count() or page.locator(".apierr").count():
            pytest.fail(
                f"{what} failed in the UI: {page.locator('#app').inner_text()[:2000]}\n{log_tail(log)}"
            )
        if time.monotonic() > deadline:
            pytest.fail(f"{what} did not finish within {RUN_TIMEOUT_S:.0f}s\n{log_tail(log)}")
        page.wait_for_timeout(POLL_MS)


def run_id_of(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


CLIPPED_JS: Final[str] = """() => {
  const edge = document.documentElement.clientWidth + 1;
  return [...document.querySelectorAll('main .card, main .kpi, main .tbl-wrap, main .uchart')]
    .filter((el) => el.getBoundingClientRect().right > edge)
    .map((el) => `${el.className}: ${(el.querySelector('h3') || el).textContent.trim().slice(0, 40)}`);
}"""
"""Cards, tiles, table wrappers and charts whose right edge is past the screen's."""


# --- the journey ----------------------------------------------------------------------------------


@dataclass
class Journey:
    screens: dict[str, Screen] = field(default_factory=dict)
    train_run: str = ""
    score_run: str = ""
    targeted_run: str = ""
    treat_list: pd.DataFrame | None = None
    mobile: dict[str, tuple[int, int]] = field(default_factory=dict)
    clipped: dict[str, list[str]] = field(default_factory=dict)
    seconds: dict[str, float] = field(default_factory=dict)


@pytest.fixture(scope="module")
def journey(browser: Any, server: Server, workdir: Path, shots: Path) -> Journey:
    expect = sync_api.expect
    seen = Journey()
    log = server.log
    console = Console()
    context = new_context(browser, viewport={"width": 1400, "height": 1000})
    page = context.new_page()
    page.set_default_timeout(ACTION_TIMEOUT_MS)
    console.attach(page)
    started = time.monotonic()

    history = workdir / "history.csv"
    make_uplift_data(TRAIN_ROWS, seed=7).frame.to_csv(history, index=False)
    campaign = make_winback_campaign(CAMPAIGN_ROWS, seed=11)
    scoring = campaign.frame.drop(columns=[TARGET, TREATMENT, "treatment_date"]).copy()
    scoring["marketing_opt_in"] = [index % 10 != 3 for index in range(len(scoring.index))]
    scoring_csv = workdir / "campaign_customers.csv"
    scoring.to_csv(scoring_csv, index=False)
    targeted = workdir / "targeted.csv"
    make_uplift_data(TARGETED_ROWS, seed=21, targeted=True).frame.to_csv(targeted, index=False)

    # 1. Overview -> the top bar's Models menu -> "Uplift models" -> the use case's uplift Setup.
    page.goto(f"{server.base_url}/ui/", wait_until="domcontentloaded")
    models = page.locator("#pb-bar nav[aria-label=Main] button", has_text="Models")
    expect(models).to_be_visible()
    seen.screens["overview"] = capture(page, console, shots, "01-overview")
    models.click()
    page.locator("#pb-bar a", has_text="Uplift models").click()
    expect(page.get_by_role("heading", name="Measure what a campaign changes (uplift)")).to_be_visible()
    seen.screens["index"] = capture(page, console, shots, "02-uplift-index")
    page.get_by_role("link", name=USE_CASE_NAME).click()
    expect(page.locator("#u-file")).to_be_attached()
    seen.screens["setup_empty"] = capture(page, console, shots, "03-setup-empty")

    # 2. Upload, detect the treatment column, train, watch Running, reach Results.
    page.set_input_files("#u-file", str(history))
    expect(page.locator("#u-treatment")).to_have_value(TREATMENT, timeout=UPLOAD_TIMEOUT_MS)
    expect(page.locator("#u-pk")).to_have_value(PRIMARY_KEY)
    expect(page.locator("#u-target")).to_have_value(TARGET)
    seen.screens["setup_filled"] = capture(
        page,
        console,
        shots,
        "04-setup-filled",
        treatment_options=page.locator("#u-treatment option").all_inner_texts(),
        ptype=page.locator(".ptype").inner_text(),
    )
    run = page.get_by_role("button", name="Train uplift model").last
    expect(run).to_be_enabled()
    run.click()
    expect(page.locator(".progress")).to_be_visible()
    seen.screens["running"] = capture(page, console, shots, "05-running")
    wait_for(page, lambda: page.locator(".summary .ok").count() > 0, "the uplift training run", log)
    seen.train_run = run_id_of(page.url)
    seen.screens["results"] = capture(page, console, shots, "06-results")
    seen.seconds["train"] = round(time.monotonic() - started, 1)

    # 3. Model page, reached through the Results block; then an off-policy estimate on it.
    page.locator(".flow .block").nth(1).click()
    expect(page.locator(".tab.on")).to_contain_text("Model")
    expect(page.locator(".uchart svg").first).to_be_visible()
    seen.screens["model"] = capture(
        page,
        console,
        shots,
        "07-model",
        qini_model=page.locator(".uchart polyline.model").count(),
        qini_random=page.locator(".uchart polyline.rand").count(),
        qini_points=len((page.locator(".uchart polyline.model").get_attribute("points") or "").split()),
        decile_bars=page.locator(".uchart rect.pos, .uchart rect.neg").count(),
    )
    # "What if…" is folded and pre-filled with 10.
    page.locator("summary", has_text="What if we contacted only the top customers?").click()
    expect(page.locator("#u-ope-share")).to_have_value("10")
    page.fill("#u-ope-share", "30")
    with page.expect_response(lambda r: r.url.endswith("/uplift/ope") and r.request.method == "POST"):
        page.locator("#u-ope button[type=submit]").click()
    expect(page.get_by_text("Treat the top 30% of customers by predicted uplift").first).to_be_visible()
    seen.screens["model_ope"] = capture(page, console, shots, "08-model-ope")

    page.locator(".tabs .tab", has_text="Contact list").click()
    expect(page.locator(".tab.on")).to_contain_text("Contact list")
    expect(page.locator(".usegs")).to_be_visible()
    seen.screens["output"] = capture(page, console, shots, "09-output", segments=segment_rows(page))

    # The uplift Setup's own score mode offers the model just trained (DEC-609: candidates too).
    page.goto(f"{server.base_url}/ui/#/uplift/{USE_CASE}", wait_until="domcontentloaded")
    page.get_by_role("button", name="Score new data").click()
    expect(page.locator("#u-model")).to_be_attached()
    seen.screens["uplift_score_setup"] = capture(
        page,
        console,
        shots,
        "10-uplift-score-setup",
        models=page.locator("#u-model option").all_inner_texts(),
    )

    # 4. Score through Phase 1's own use-case screen, with the uplift model from its dropdown.
    at = time.monotonic()
    console.phase1 = True
    page.goto(f"{server.base_url}/ui/#/uc/{USE_CASE}", wait_until="domcontentloaded")
    expect(page.locator("#f-file")).to_be_attached()
    page.get_by_role("button", name="Score new data").click()
    expect(page.locator("#f-scorerun")).to_be_visible()
    phase1_models = page.locator("#f-scorerun option").all_inner_texts()
    page.set_input_files("#f-file", str(scoring_csv))
    expect(page.locator("#f-pk")).to_have_value(PRIMARY_KEY, timeout=UPLOAD_TIMEOUT_MS)
    score = page.get_by_role("button", name="Score data")
    expect(score).to_be_enabled()
    score.click()
    wait_for(page, lambda: page.locator(".summary .ok").count() > 0, "the Phase 1 scoring run", log)
    seen.screens["phase1_score_results"] = capture(
        page, console, shots, "11-phase1-score-results", models=phase1_models
    )
    seen.seconds["score"] = round(time.monotonic() - at, 1)
    seen.score_run = run_id_of(page.url)
    page.locator(".flow .block").nth(2).click()
    # v1 (WP4): Phase 1's Output link of an uplift run redirects to the uplift module's own Output.
    expect(page.locator("main[data-module=uplift]")).to_be_attached()
    expect(page.locator(".tab.on")).to_contain_text("Contact list")
    seen.score_run = run_id_of(page.url)
    seen.screens["phase1_score_output"] = capture(page, console, shots, "12-phase1-score-output")
    page.locator(".tabs .tab", has_text="Campaign results").click()
    expect(page.locator("main[data-module=uplift]")).to_be_attached()
    console.phase1 = False

    # 5. Campaign results: nothing yet; the uplift Output tab with the treat list; outcomes.
    expect(page.locator("#u-camp-file")).to_be_attached()
    seen.screens["campaign_empty"] = capture(page, console, shots, "13-campaign-empty")
    page.locator(".tabs .tab", has_text="Contact list").click()
    expect(page.locator(".usegs")).to_be_visible()
    seen.screens["score_output"] = capture(
        page, console, shots, "14-score-output", segments=segment_rows(page)
    )
    with page.expect_download() as download_info:
        page.get_by_role("link", name="Download contact list (CSV)").click()
    treat_list = workdir / "treat_list.csv"
    download_info.value.save_as(treat_list)
    seen.treat_list = pd.read_csv(treat_list, dtype={PRIMARY_KEY: str})
    treated = set(seen.treat_list.loc[seen.treat_list["action"] == "Treat", PRIMARY_KEY])
    outcomes = outcomes_for(campaign, treated_keys=treated, seed=5)
    outcomes["treatment_date"] = campaign.frame["treatment_date"].to_numpy()
    outcomes_csv = workdir / "outcomes.csv"
    outcomes.to_csv(outcomes_csv, index=False)

    page.locator(".tabs .tab", has_text="Campaign results").click()
    expect(page.locator("#u-camp-file")).to_be_attached()
    page.set_input_files("#u-camp-file", str(outcomes_csv))
    expect(page.locator("#u-camp-outcome")).to_be_visible(timeout=UPLOAD_TIMEOUT_MS)
    page.select_option("#u-camp-outcome", TARGET)
    page.select_option("#u-camp-date", "treatment_date")
    page.fill("#u-camp-window", "90")
    page.locator("#u-camp details.adv > summary", has_text="Advanced").click()

    def measure(as_of: str) -> None:
        page.fill("#u-camp-asof", as_of)
        with page.expect_response(
            lambda r: r.url.endswith("/campaign-results") and r.request.method == "POST"
        ) as answer:
            page.locator("#u-camp-run").click()
        assert answer.value.status == 200, answer.value.text()
        expect(page.locator("#u-camp-run")).to_have_text("Measure the campaign")

    measure(IMMATURE_AS_OF)
    expect(page.locator(".uwait")).to_be_visible()
    seen.screens["campaign_immature"] = capture(
        page,
        console,
        shots,
        "15-campaign-immature",
        report=server.artefact(seen.score_run, "incrementality_report.json"),
    )
    measure(MATURE_AS_OF)
    expect(page.locator(".uwait")).to_have_count(0)
    expect(page.locator(".kpis")).to_be_visible()
    seen.screens["campaign_mature"] = capture(
        page,
        console,
        shots,
        "16-campaign-mature",
        report=server.artefact(seen.score_run, "incrementality_report.json"),
    )

    # 6. A targeted campaign: refused, acknowledged, trained, labelled not causal.
    page.goto(f"{server.base_url}/ui/#/uplift/{USE_CASE}", wait_until="domcontentloaded")
    page.get_by_role("button", name="Train uplift model").first.click()
    page.set_input_files("#u-file", str(targeted))
    expect(page.locator("#u-treatment")).to_have_value(TREATMENT, timeout=UPLOAD_TIMEOUT_MS)
    with page.expect_response(lambda r: r.url.endswith("/uplift/runs")) as refused:
        page.locator("#u-run").click()
    assert refused.value.status == 409, refused.value.text()
    expect(page.locator(f"[data-uack={NOT_RANDOM}]")).to_be_visible()
    seen.screens["refusal"] = capture(
        page,
        console,
        shots,
        "17-not-random-refusal",
        refused_codes=page.locator(f".vitem[data-code={NOT_RANDOM}]").count(),
        randomness_pill=page.locator(".vlist [data-code=RANDOMNESS]").get_attribute("class"),
    )
    page.locator(f"[data-uack={NOT_RANDOM}]").check()
    expect(page.locator(".ptype .uwarn")).to_be_visible()
    seen.screens["acknowledged"] = capture(page, console, shots, "18-not-random-acknowledged")
    page.locator("#u-run").click()
    wait_for(page, lambda: page.locator(".summary .ok").count() > 0, "the acknowledged uplift run", log)
    seen.targeted_run = run_id_of(page.url)
    page.locator(".flow .block").nth(1).click()
    expect(page.locator(".unotcausal")).to_be_visible()
    seen.screens["targeted_model"] = capture(page, console, shots, "19-not-causal-model")
    page.locator(".tabs .tab", has_text="Contact list").click()
    expect(page.locator(".usegs")).to_be_visible()
    seen.screens["targeted_output"] = capture(page, console, shots, "20-not-causal-output")
    context.close()

    # 7. Every uplift screen at phone width.
    mobile = new_context(
        browser, viewport={"width": MOBILE_WIDTH, "height": 844}, is_mobile=True, has_touch=True
    )
    phone = mobile.new_page()
    phone.set_default_timeout(ACTION_TIMEOUT_MS)
    console.attach(phone)
    base = f"{server.base_url}/ui/#"
    for name, route, ready in (
        ("index", "/uplift", ".uindex"),
        ("setup", f"/uplift/{USE_CASE}", "#u-setup"),
        ("results", f"/uplift/{USE_CASE}/run/{seen.train_run}", ".summary"),
        ("model", f"/uplift/{USE_CASE}/model/{seen.train_run}", ".uchart svg"),
        ("output", f"/uplift/{USE_CASE}/output/{seen.train_run}", ".usegs"),
        ("score_output", f"/uplift/{USE_CASE}/output/{seen.score_run}", ".usegs"),
        ("campaign", f"/campaign/{USE_CASE}/{seen.score_run}", ".kpis"),
        ("targeted_model", f"/uplift/{USE_CASE}/model/{seen.targeted_run}", ".unotcausal"),
    ):
        phone.goto(base + route, wait_until="domcontentloaded")
        phone.locator(ready).first.wait_for()
        phone.wait_for_timeout(300)
        seen.mobile[name] = phone.evaluate(
            "() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]"
        )
        seen.clipped[name] = phone.evaluate(CLIPPED_JS)
        phone.screenshot(path=str(shots / f"m-{name}.png"), full_page=True)
        seen.screens[f"mobile_{name}"] = Screen(url=phone.url, problems=console.take())
    mobile.close()
    seen.seconds["total"] = round(time.monotonic() - started, 1)
    print(f"\nuplift browser journey seconds: {seen.seconds}")
    return seen


def segment_rows(page: Any) -> dict[str, str]:
    """The four-segment chart: segment label -> the count cell as rendered."""
    found: dict[str, str] = {}
    for row in page.locator(".usegs .useg").all():
        found[row.locator(".ul").inner_text().strip()] = row.locator(".un").inner_text().strip()
    return found


# --- every screen: a clean console and no invented number ----------------------------------------


def test_no_screen_logged_an_error_or_an_unexpected_failed_request(journey: Journey) -> None:
    dirty = {name: screen.problems for name, screen in journey.screens.items() if screen.problems}
    assert not dirty, dirty


def test_no_screen_rendered_a_placeholder_for_a_value(journey: Journey) -> None:
    for name, screen in journey.screens.items():
        leaked = re.findall(r"\b(?:undefined|NaN|null|Infinity)\b|\[object Object\]", screen.text)
        assert not leaked, (name, leaked)


def test_no_uplift_screen_scrolls_sideways_on_a_phone(journey: Journey) -> None:
    assert len(journey.mobile) == 8, journey.mobile
    wide = {name: size for name, size in journey.mobile.items() if size[0] > size[1]}
    assert not wide, wide
    assert all(size[1] == MOBILE_WIDTH for size in journey.mobile.values()), journey.mobile


def test_no_card_is_cut_off_at_the_edge_of_a_phone(journey: Journey) -> None:
    """No page scroll is not enough: `.card` hides its overflow, so a card wider than the screen
    would be cut off rather than scrollable. Wide tables must scroll inside their `.tbl-wrap`."""
    clipped = {name: cards for name, cards in journey.clipped.items() if cards}
    assert not clipped, clipped


# --- the journey, step by step --------------------------------------------------------------------


def test_the_top_bar_leads_to_uplift_and_the_index_lists_the_use_case(journey: Journey) -> None:
    index = journey.screens["index"].text
    assert "Uplift modelling" in index  # the breadcrumb
    assert USE_CASE_NAME in index
    assert "Uplift model" in index  # each row says whether a model exists


def test_setup_detects_the_treatment_column_and_explains_the_problem_type(journey: Journey) -> None:
    filled = journey.screens["setup_filled"]
    options = filled.extra["treatment_options"]
    assert any(option.startswith(f"{TREATMENT} · ") and "treated" in option for option in options), options
    assert not any(option.startswith(f"{TARGET} ·") for option in options), "an outcome is never a treatment"
    assert "Uplift" in filled.extra["ptype"]
    assert "predicts who changes behaviour because of your action" in filled.extra["ptype"]
    assert f"{TRAIN_ROWS:,} rows" in filled.text


def test_running_showed_progress_and_results_name_the_model(journey: Journey, server: Server) -> None:
    assert "Running" in journey.screens["running"].text
    run = server.artefact(journey.train_run, "run.json")
    results = journey.screens["results"].text
    assert "Uplift model trained" in results
    assert run["best_model"] in results
    # The headline is the finding in words; AUUC is on the Model page, under Technical metrics.
    assert "Contacting the top 10% the model picks" in results, results
    assert "Score customers with this model" in results


def test_the_model_page_draws_the_qini_curve_against_random(journey: Journey, server: Server) -> None:
    model = journey.screens["model"]
    curve = server.artefact(journey.train_run, "qini_curve.json")
    assert model.extra["qini_model"] == 1 and model.extra["qini_random"] == 1
    assert model.extra["qini_points"] == len(curve["points"])
    evaluation = server.artefact(journey.train_run, "uplift_evaluation.json")
    with_bar = [d for d in evaluation["deciles"] if d["observed_uplift"] is not None]
    assert model.extra["decile_bars"] == len(with_bar) > 0


def test_the_model_page_shows_auuc_with_its_interval_from_the_artefact(
    journey: Journey, server: Server
) -> None:
    model = journey.screens["model"]
    auuc = server.artefact(journey.train_run, "uplift_evaluation.json")["auuc"]
    # AUUC and its interval sit under "Technical metrics", at three decimals.
    value, level, low, high = numbers(model.rows["AUUC"])
    assert close(value, auuc["value"], 3), model.rows
    assert level == 95 and close(low, auuc["ci_low"], 3) and close(high, auuc["ci_high"], 3), model.rows
    assert "AUUC" not in model.text, "the metric code stays behind Details"
    evaluation = server.artefact(journey.train_run, "uplift_evaluation.json")
    assert model.rows["Hold-out rows"] == f"{evaluation['rows_evaluated']:,}"
    assert model.rows["Bootstrap resamples"] == str(evaluation["bootstrap_samples"])
    assert "Measurable uplift" in model.text


def test_the_off_policy_estimate_is_the_stored_report(journey: Journey, server: Server) -> None:
    ope = server.artefact(journey.train_run, "ope_report.json")
    screen = journey.screens["model_ope"]
    assert screen.rows["Policy"] == ope["policy_description"]
    assert screen.rows["Rows"] == f"{ope['rows']:,}"


def test_the_output_page_shows_segments_and_the_recommendation(journey: Journey, server: Server) -> None:
    check_output(journey.screens["output"], server, journey.train_run)
    assert "Measured on the hold-out split of the training run." in journey.screens["output"].text


def check_output(screen: Screen, server: Server, run_id: str) -> None:
    policy = server.artefact(run_id, "policy_recommendation.json")
    segments = server.artefact(run_id, "segments.json")
    assert screen.tiles["Customers to contact"] == f"{policy['contacts_recommended']:,}"
    recommended = numbers(screen.rows["Recommended to contact"])
    assert recommended == [policy["contacts_recommended"], policy["eligible_persuadables"]], screen.rows
    expected = policy["expected_incremental_conversions"]
    # People are whole: "about 247".
    assert close(numbers(screen.tiles["Extra customers expected to respond"])[0], expected["value"], 0)
    row = numbers(screen.rows["Expected incremental conversions"])
    assert close(row[0], expected["value"], 0) and row[1] == 95, screen.rows
    assert close(row[2], expected["ci_low"], 0) and close(row[3], expected["ci_high"], 0), screen.rows
    # No cost or value is configured, so none may be printed.
    assert policy["expected_net_value"] is None
    assert "Expected net value" not in screen.tiles and "Expected net value" not in screen.rows
    assert screen.rows["Cost per contact"] == EM_DASH
    shown = screen.extra["segments"]
    assert len(shown) == 4, shown
    for item in segments["segments"]:
        assert numbers(shown[item["label"]])[0] == item["rows"], (item, shown)


def test_both_score_screens_offer_the_uplift_model(journey: Journey) -> None:
    for models in (
        journey.screens["uplift_score_setup"].extra["models"],
        journey.screens["phase1_score_results"].extra["models"],
    ):
        assert len(models) == 1 and "X-learner (LightGBM)" in models[0], models
    assert journey.screens["uplift_score_setup"].extra["models"][0].startswith("Uplift model trained ")


def test_phase1_scoring_of_the_uplift_model_came_back(journey: Journey, server: Server) -> None:
    summary = journey.screens["phase1_score_results"].text
    assert "Scoring complete" in summary and f"{CAMPAIGN_ROWS:,} rows scored" in summary, summary
    run = server.artefact(journey.score_run, "run.json")
    assert run["mode"] == "score" and run["problem_type"] == "uplift"


def test_the_score_output_is_the_treat_list_the_browser_downloaded(journey: Journey, server: Server) -> None:
    screen = journey.screens["score_output"]
    check_output(screen, server, journey.score_run)
    assert "Computed over every customer this run scored." in screen.text
    treat_list = journey.treat_list
    assert treat_list is not None and len(treat_list.index) == CAMPAIGN_ROWS
    policy = server.artefact(journey.score_run, "policy_recommendation.json")
    treat = treat_list[treat_list["action"] == "Treat"]
    assert len(treat.index) == policy["contacts_recommended"]
    assert set(treat["segment"]) == {"persuadable"}, "sleeping dogs are never Treat"


def test_campaign_results_start_empty(journey: Journey) -> None:
    assert "No outcomes have been uploaded for this run yet." in journey.screens["campaign_empty"].text


def test_an_immature_campaign_says_only_when_results_will_be_available(journey: Journey) -> None:
    screen = journey.screens["campaign_immature"]
    report = screen.extra["report"]
    assert report["status"] == "immature" and report["absolute_lift"] is None
    assert date.fromisoformat(report["results_available_on"]) == date(2026, 7, 30)
    assert "Results available on 30 Jul 2026" in screen.text
    assert not screen.tiles, "no lift, rate or count is shown before the window has elapsed"
    # The date picked on screen reads back as that date where the user is, not as a UTC instant.
    assert "as of 01 Jun 2026, 11:59 pm" in screen.text, screen.text


def test_a_mature_campaign_shows_the_lift_with_its_interval(journey: Journey) -> None:
    screen = journey.screens["campaign_mature"]
    report = screen.extra["report"]
    assert report["status"] == "mature" and report["causal"] is True
    lift = report["absolute_lift"]
    # A verdict first, in the value view's words: a win-back outcome is one to have more of.
    assert "The campaign" in screen.text or "We cannot yet tell" in screen.text, screen.text
    inc = report["incremental_conversions"]
    tile = screen.tiles["Extra customers because of the campaign"]
    assert close(numbers(tile)[0], inc["value"], 0), screen.tiles
    rates = numbers(screen.tiles["Response rate: contacted vs not contacted"])
    assert close(rates[0], report["treated_rate"] * 100, 1) and close(
        rates[1], report["control_rate"] * 100, 1
    )
    # The statistics are under "Statistical details", at one decimal.
    row = numbers(screen.rows["Absolute lift"])
    assert close(row[0], lift["value"] * 100, 1) and row[1] == 95, screen.rows
    assert close(row[2], lift["ci_low"] * 100, 1) and close(row[3], lift["ci_high"] * 100, 1), screen.rows
    assert "p-value" not in screen.text and "Incremental conversions" not in screen.text
    assert f"{report['treated_rows']:,}" in screen.text and f"{report['control_rows']:,}" in screen.text
    assert screen.rows["Outcome window"] == "90 days"
    assert screen.rows["Judged as of"] == "15 Aug 2026, 11:59 pm"
    assert "Not causal" not in screen.text


def test_a_targeted_file_is_refused_until_acknowledged(journey: Journey) -> None:
    refused = journey.screens["refusal"]
    refusal = refused.text
    assert refused.extra["refused_codes"] == 1, "the code is kept in data-code"
    assert "Who was contacted does not look random" in refusal or "can be predicted" in refusal, refusal
    assert "1 thing to fix before training" in refusal, refusal
    assert "bad" in (refused.extra["randomness_pill"] or ""), "a failed randomness check is red"
    acknowledged = journey.screens["acknowledged"].text
    assert "Nothing left to fix before training" in acknowledged, acknowledged
    assert "Not causal: the treatment was not randomly assigned" in acknowledged


def test_the_acknowledged_run_is_labelled_not_causal(journey: Journey, server: Server) -> None:
    evaluation = server.artefact(journey.targeted_run, "uplift_evaluation.json")
    assert evaluation["causal"] is False
    for name in ("targeted_model", "targeted_output"):
        text = journey.screens[name].text
        assert "Not causal" in text and f"{NOT_RANDOM} was acknowledged for this run" in text, name
    for name in ("model", "output", "score_output", "campaign_mature"):
        assert "TREATMENT_NOT_RANDOM was acknowledged" not in journey.screens[name].text, name
