"""Plan M M181 part B (DEC-1611): the click counts of the two usability tasks, pinned in a real browser.

`docs/ux/USABILITY_TEST.md` gives five people two tasks. This module walks the shortest path a person has for each,
in Chromium, against the real app in the `campaigns` layout on the validated Hillstrom demo store
(`scripts.seed_validated` on the committed sample, the store the kit tells the facilitator to serve), starting from
`#/overview` and counting every click:

* **Task 1, "Plan a win-back campaign and download who to contact".** The task card says "win them back", and on the
  demo store (default industry) the wizard's card for that is "Win back lapsed customers", a goal with no model and no
  prepared file yet: the participant's path is a FIRST campaign, so that is the pinned walk. Overview, "+ New campaign",
  the goal, the prepared customer file, then the wizard's own decisions (Approve, approve the new model, Continue,
  Approve, Download), to the downloaded contact list. The Plan M §0 measure is "Clicks from Home to a downloaded contact
  list (file already prepared): at most 5, inside one wizard". The reading used here is the strict one: every mouse
  click from the Overview to the download counts, and choosing the file on disk is not a click (it is the "file already
  prepared" the measure allows for) but is recorded beside the clicks as a file choice so nobody mistakes it for free.
  The model search is capped on a copy of the configs, as `test_campaign_wizard_journey.py` does. A secondary walk, a
  LATER campaign (the goal already has an approved model: "Choose the best e-mail for each customer" on the demo
  store), has no learning and no model to approve, and is pinned as such. In both, the test plan is checked through
  the API to be registered and frozen before the download is clicked, and the download is checked to be the contact
  list the API serves for the very run the button points at, byte for byte.

* **Task 2, "Show what last month's campaign earned".** From the Overview, the shortest real path to a measured
  campaign's earned value AND how sure the product is (the evidence label): the sidebar's Reports (one click: the
  Value Proof Packs table has each campaign's headline and what it can claim), and, for a person who looks for the
  campaign itself, Campaigns, the campaign, its Results tab. Both are pinned, and the figures on screen are the API's
  (`GET /pilot/proof`, `GET /pilot/proof/{id}`). The Overview's own "Value proven to date" tile is the programme
  total, not one campaign's figure, so it is compared with `GET /overview` and is not counted as an answer to the task.

Each count is what the screens take TODAY. A change that adds a click fails here; one that removes a click updates the
pin and the kit (`docs/ux/USABILITY_TEST.md` section 12). Task 1 does not meet Plan M §0's target of 5, so that
target is asserted in a test marked `xfail(strict=True)`: it turns into a failure the day the gap is closed, and the
default-layout switch-over (M181) waits for it. Neither task replaces a person: the sessions decide whether people find
the path, these tests prove the path exists and how long it is.

Skips without playwright or a usable Chromium, like the other browser journeys. The seed trains two small models
(about two minutes); `USABILITY_STORE` names a store that is already seeded (the `data` folder `scripts.seed_validated`
printed) to save that while this file is being worked on. The store is copied for each task, as the kit does for each
participant, so no task starts from another's changes.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

from tests.fixtures.make_data import CLEAN, GenerationSpec, write_csv
from tests.integration.test_acceptance import HOST, TEST_BUDGET, free_port, wait_for_health

pytestmark = [pytest.mark.slow, pytest.mark.integration]

sync_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright is not installed; the journeys need a browser"
)

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
CUSTOMER_FILE: Final[Path] = REPO_ROOT / "library" / "hillstrom-email" / "sample.csv"
GOAL: Final[str] = "win-back-campaign"
"""The goal behind "win them back": the wizard's "Win back lapsed customers" card (`wizard_rules.js` GOAL_NAMES)."""
GOAL_NAME: Final[str] = "Win back lapsed customers"
LATER_GOAL: Final[str] = "hillstrom-email"
LATER_GOAL_NAME: Final[str] = "Choose the best e-mail for each customer"
ROWS: Final[int] = 4000
SEED: Final[int] = 20_261_012
VIEWPORT: Final[dict[str, int]] = {"width": 1440, "height": 900}
WAIT_MS: Final[int] = 900_000
"""Learning runs a capped model search plus preparing, evaluating and explaining; building the list follows."""

# --- Plan M §0 and what the screens take today ---------------------------------------------------------

TASK1_TARGET: Final[int] = 5
"""Plan M §0: "Clicks from Home to a downloaded contact list (file already prepared): <= 5, inside one wizard"."""
TASK1_CLICKS: Final[tuple[str, ...]] = (
    "overview: + New campaign",
    "wizard goal: the goal card",
    "customers: Approve (Guided setup)",
    "learned: Approve the new model",
    "audience: Continue",
    "review: Approve",
    "send: Download contact list",
)
"""Every click from the Overview to the downloaded file, in order, for a goal's first campaign (the file is chosen
between the 2nd and the 3rd)."""
TASK1_CLICKS_PINNED: Final[int] = 7
TASK1_FILE_CHOICES_PINNED: Final[int] = 1
TASK1_WIZARD_DECISIONS_PINNED: Final[int] = 5
"""M177's own measure (`test_campaign_wizard_journey.py` DECISIONS_PINNED): the decisions after the goal and the file
are chosen, which for a first campaign includes approving the new model."""
LATER_CAMPAIGN_CLICKS: Final[tuple[str, ...]] = (
    "overview: + New campaign",
    "wizard goal: the goal card",
    "customers: Approve (Guided setup)",
    "audience: Continue",
    "review: Approve",
    "send: Download contact list",
)
LATER_CAMPAIGN_CLICKS_PINNED: Final[int] = 6
"""A goal that already has an approved model: no learning, so no model to approve."""

TASK2_REPORTS_CLICKS: Final[tuple[str, ...]] = ("sidebar: Reports",)
TASK2_CAMPAIGN_CLICKS: Final[tuple[str, ...]] = (
    "sidebar: Campaigns",
    "campaigns list: the campaign",
    "campaign page: Results tab",
)
EVIDENCE_ON_SCREEN: Final[dict[str, tuple[str, str]]] = {
    "proven": ("proven", "Proven"),
    "stated_random": ("estimated", "Estimated"),
    "descriptive": ("not-proven", "Not yet proven"),
}
"""The pack's claim (`GET /pilot/proof/{id}?format=json`) and the evidence label a person reads for it
(`ui/labels.js`: evidenceProven, evidenceEstimated, evidenceNotProven), written out so the screen's own rule is
not what the test copies."""
TASK2_REPORTS_PINNED: Final[int] = 1
TASK2_CAMPAIGN_PAGE_PINNED: Final[int] = 3

PAGE_TIMEOUT_MS: Final[int] = 60_000
FONT_HOSTS: Final[tuple[str, ...]] = ("fonts.googleapis.com", "fonts.gstatic.com")

PRISTINE: Final[str] = "pristine"


# --- the store, the app, the browser ------------------------------------------------------------------


@pytest.fixture(scope="module")
def pristine_store(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The validated-data demo store, seeded from the committed sample (never the full file in `data/`)."""
    from scripts import seed_validated

    existing = os.environ.get("USABILITY_STORE")
    if existing:
        return Path(existing)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(seed_validated, "FULL_FILE", tmp_path_factory.getbasetemp() / "no-full-file.csv")
        result = seed_validated.seed(
            data_dir=tmp_path_factory.mktemp("usability-seed"), echo=lambda _line: None
        )
    assert result.is_sample, "the journeys seed from the committed sample"
    return result.store


@contextmanager
def serve(store: Path, workdir: Path, name: str, config_root: Path | None = None) -> Iterator[str]:
    """The real app in the campaigns layout on a copy of the store, as the kit tells the facilitator to run it."""
    copy = workdir / f"store-{name}"
    shutil.copytree(store, copy)
    port = free_port()
    base_url = f"http://{HOST}:{port}"
    log = workdir / f"server-{name}.log"
    env = {
        **os.environ,
        "MARKETING_AI_UI_LAYOUT": "campaigns",
        "MARKETING_AI_DATA_DIR": str(copy),
        "PYTHONPATH": str(REPO_ROOT),
        "PYTHONUNBUFFERED": "1",
    }
    if config_root is not None:
        env["MARKETING_AI_CONFIG_DIR"] = str(config_root)
    env.pop(
        "MARKETING_AI_DEMO_MODE", None
    )  # the kit: that switch would put a false "Sample data" notice in the bar
    command = [sys.executable, "-m", "uvicorn", "api.main:app", "--host", HOST, "--port", str(port)]
    command += ["--log-level", "warning"]
    with log.open("wb") as sink:
        process = subprocess.Popen(command, cwd=REPO_ROOT, env=env, stdout=sink, stderr=subprocess.STDOUT)
        try:
            wait_for_health(base_url, process, log)
            yield base_url
        finally:
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:  # pragma: no cover - a wedged server
                process.kill()
                process.wait(timeout=30)


@pytest.fixture(scope="module")
def workdir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("usability-journeys")


@pytest.fixture(scope="module")
def playwright_driver() -> Iterator[Any]:
    with sync_api.sync_playwright() as playwright:
        yield playwright


def new_page(playwright: Any) -> tuple[Any, Any, list[str]]:
    try:
        browser = playwright.chromium.launch(executable_path="/opt/pw-browsers/chromium")
    except sync_api.Error as error:  # pragma: no cover - a machine without the browser
        pytest.skip(f"no usable chromium for playwright: {error}")
    page = browser.new_page(viewport=VIEWPORT, accept_downloads=True)
    problems: list[str] = []
    page.on("pageerror", lambda error: problems.append(f"uncaught: {error}"))
    # A resource that fails to load logs a console line without its address, so those are judged by the request
    # events below (which name the host); the Google font the sandbox cannot fetch is the only allowance.
    page.on(
        "console",
        lambda message: (
            problems.append(f"console: {message.text}")
            if message.type == "error" and not message.text.startswith("Failed to load resource")
            else None
        ),
    )
    page.on(
        "requestfailed",
        lambda request: (
            problems.append(f"request failed: {request.url}")
            if not any(host in request.url for host in FONT_HOSTS)
            else None
        ),
    )
    page.on(
        "response",
        lambda reply: problems.append(f"HTTP {reply.status}: {reply.url}") if reply.status >= 400 else None,
    )
    return browser, page, problems


def _get(base: str, path: str) -> Any:
    with urllib.request.urlopen(f"{base}{path}", timeout=60) as response:
        body = response.read()
    return json.loads(body) if body[:1] in (b"{", b"[") else body


# --- counting ---------------------------------------------------------------------------------------------


@dataclass
class Walk:
    """A person's way through the screens: every click and every file choice, in the order they happened."""

    page: Any
    clicks: list[str] = field(default_factory=list)
    file_choices: list[str] = field(default_factory=list)
    opened: int = 0

    def open(self, url: str) -> None:
        """The one thing that is not a click: the person is at the Overview when the task starts."""
        self.opened += 1
        assert self.opened == 1, "a journey starts once; every later move is a click"
        self.page.goto(url)

    def click(self, what: str, selector: str, *, timeout: int = PAGE_TIMEOUT_MS) -> None:
        locator = self.page.locator(selector)
        locator.first.wait_for(state="visible", timeout=timeout)
        assert locator.count() == 1, f"{what}: {selector} matches {locator.count()} controls"
        self.clicks.append(what)
        locator.click()

    def choose_file(self, what: str, selector: str, path: Path) -> None:
        self.page.locator(selector).wait_for(state="attached", timeout=PAGE_TIMEOUT_MS)
        self.file_choices.append(what)
        self.page.set_input_files(selector, str(path))


@dataclass
class Task1:
    walk: Walk
    campaign_id: str
    run_id: str
    saved: bytes
    served: bytes
    wizard_decisions: int
    problems: list[str]


@pytest.fixture(scope="module")
def capped_configs(workdir: Path) -> Path:
    """A copy of `configs/` whose win-back model search is capped (the checkout's own configs stay untouched)."""
    root = workdir / "configs"
    shutil.copytree(REPO_ROOT / "configs", root)
    path = root / "use_cases" / f"{GOAL.replace('-', '_')}.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    search = document["model_search"]
    assert (
        "time_limit_minutes" not in search and "tuning_trials" not in search
    ), "the win-back config sets the budget itself; edit this cap instead of appending to it"
    # TEST_BUDGET is a second top-level `model_search:` block; merging it into the existing one keeps the YAML valid
    budget = yaml.safe_load(TEST_BUDGET)["model_search"]
    path.write_text(
        yaml.safe_dump({**document, "model_search": {**search, **budget}}, sort_keys=False), encoding="utf-8"
    )
    return root


@pytest.fixture(scope="module")
def win_back_customers(workdir: Path, capped_configs: Path) -> Path:
    """The prepared file: one row per lapsed customer with what each did last time (synthetic, `make_data`'s clean)."""
    spec = GenerationSpec(GOAL, rows=ROWS, seed=SEED, variant=CLEAN, config_root=capped_configs)
    return write_csv(spec, workdir / "win_back_customers.csv")


def _download_and_check(
    base: str, page: Any, walk: Walk, label: str, saved_to: Path, *, campaign_id: str
) -> tuple[bytes, bytes, str]:
    """On the Send step: the plan is frozen BEFORE the download is clicked; then download, and fetch the very run's list."""
    plans = _get(base, f"/campaigns/{campaign_id}/plan")
    view = _get(base, f"/campaigns/{campaign_id}")
    assert (
        plans["plan"] is not None and len(plans["versions"]) == 1
    ), "the test plan was registered and frozen"
    assert plans["plan"]["plan_hash"] == view["campaign"]["test_plan_hash"]
    assert view["report"] is None, "nothing has been measured: the plan was fixed in advance"
    run_ids = [str(run) for run in view["campaign"]["run_ids"]]
    assert len(run_ids) == 1, run_ids
    href = page.locator("[data-cw-download]").get_attribute("href") or ""
    assert f"/runs/{run_ids[0]}/treat_list.csv" in href, (href, run_ids)
    with page.expect_download(timeout=PAGE_TIMEOUT_MS) as caught:
        walk.click(label, "[data-cw-download]")
    caught.value.save_as(saved_to)
    return saved_to.read_bytes(), _get(base, f"/runs/{run_ids[0]}/treat_list.csv"), run_ids[0]


@pytest.fixture(scope="module")
def task1(
    pristine_store: Path,
    workdir: Path,
    playwright_driver: Any,
    capped_configs: Path,
    win_back_customers: Path,
) -> Task1:
    """Task 1 once, as the task card leads a participant: a win-back FIRST campaign, Overview to the downloaded list."""
    with serve(pristine_store, workdir, "task1", capped_configs) as base:
        browser, page, problems = new_page(playwright_driver)
        walk = Walk(page)
        try:
            walk.open(f"{base}/ui/#/overview")
            walk.click(TASK1_CLICKS[0], "[data-new-campaign]")
            goal = page.locator(f'[data-cw-goal="{GOAL}"]')
            goal.wait_for(timeout=PAGE_TIMEOUT_MS)
            assert goal.locator(".cw-goal-name").inner_text() == GOAL_NAME
            walk.click(TASK1_CLICKS[1], f'[data-cw-goal="{GOAL}"]')
            page.locator("[data-cw-customers=first]").wait_for(
                timeout=PAGE_TIMEOUT_MS
            )  # no model yet: it learns
            walk.choose_file("the prepared customer file", "#ag-file", win_back_customers)
            page.locator("#ag-approve:not([disabled])").wait_for(timeout=180_000)
            walk.click(TASK1_CLICKS[2], "#ag-approve")
            walk.click(TASK1_CLICKS[3], "[data-cw-approve-model]", timeout=WAIT_MS)
            page.locator("[data-cw-next]").wait_for(timeout=WAIT_MS)  # the list is built; Audience
            walk.click(TASK1_CLICKS[4], "[data-cw-next]")
            walk.click(TASK1_CLICKS[5], "[data-cw-approve]")
            page.locator("[data-cw-download]").wait_for(timeout=PAGE_TIMEOUT_MS)
            campaign_id = str(page.evaluate("location.hash")).rsplit("/", 1)[-1]
            saved_to = workdir / "task1_contact_list.csv"
            saved, served, run_id = _download_and_check(
                base, page, walk, TASK1_CLICKS[6], saved_to, campaign_id=campaign_id
            )
            page.screenshot(path=str(workdir / "task1_last.png"), full_page=True)
        finally:
            browser.close()
    # M177's own measure: the decisions after the goal and the file were chosen
    wizard_decisions = len(walk.clicks) - 2
    return Task1(walk, campaign_id, run_id, saved, served, wizard_decisions, problems)


@pytest.fixture(scope="module")
def task1_later(pristine_store: Path, workdir: Path, playwright_driver: Any) -> Task1:
    """The secondary walk: a LATER campaign (the demo store already has an approved e-mail model), same start."""
    with serve(pristine_store, workdir, "task1-later") as base:
        browser, page, problems = new_page(playwright_driver)
        walk = Walk(page)
        try:
            walk.open(f"{base}/ui/#/overview")
            walk.click(LATER_CAMPAIGN_CLICKS[0], "[data-new-campaign]")
            walk.click(LATER_CAMPAIGN_CLICKS[1], f'[data-cw-goal="{LATER_GOAL}"]')
            page.locator("[data-cw-customers=again]").wait_for(timeout=PAGE_TIMEOUT_MS)  # a later campaign
            walk.choose_file("the prepared customer file", "#ag-file", CUSTOMER_FILE)
            page.locator("#ag-approve:not([disabled])").wait_for(timeout=180_000)
            walk.click(LATER_CAMPAIGN_CLICKS[2], "#ag-approve")
            page.locator("[data-cw-next]").wait_for(timeout=300_000)  # the list is built; Audience
            walk.click(LATER_CAMPAIGN_CLICKS[3], "[data-cw-next]")
            walk.click(LATER_CAMPAIGN_CLICKS[4], "[data-cw-approve]")
            page.locator("[data-cw-download]").wait_for(timeout=PAGE_TIMEOUT_MS)
            campaign_id = str(page.evaluate("location.hash")).rsplit("/", 1)[-1]
            saved, served, run_id = _download_and_check(
                base,
                page,
                walk,
                LATER_CAMPAIGN_CLICKS[5],
                workdir / "task1_later_contact_list.csv",
                campaign_id=campaign_id,
            )
        finally:
            browser.close()
    return Task1(walk, campaign_id, run_id, saved, served, len(walk.clicks) - 2, problems)


# --- Task 1 ------------------------------------------------------------------------------------------------


def test_task_1_overview_to_a_downloaded_contact_list_is_pinned(task1: Task1) -> None:
    """Pins today's count from the Overview for the win-back first campaign: a click added fails here, a click
    removed updates the pin."""
    walk = task1.walk
    assert tuple(walk.clicks) == TASK1_CLICKS, walk.clicks
    assert len(walk.clicks) == TASK1_CLICKS_PINNED, walk.clicks
    assert len(walk.file_choices) == TASK1_FILE_CHOICES_PINNED, walk.file_choices
    assert (
        task1.wizard_decisions == TASK1_WIZARD_DECISIONS_PINNED
    ), "M177's measure: after the goal and the file"
    assert task1.problems == [], task1.problems


def test_task_1_the_download_is_the_campaigns_contact_list(task1: Task1) -> None:
    assert task1.saved, "the file is not empty"
    assert (
        task1.saved == task1.served
    ), "the file downloaded is the contact list the API serves for the campaign's run"
    header, *rows = task1.saved.decode("utf-8").splitlines()
    assert "customer_id" in header.split(","), header
    assert rows, "the contact list holds customers"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Plan M section 0 targets at most 5 clicks from Home to a downloaded contact list (file already prepared); "
        "the screens take 7 from the Overview for a goal's first campaign (+ New campaign, the goal, Approve, "
        "Approve the new model, Continue, Approve, Download) and 6 for a later one. The gap blocks the switch of "
        "the default layout to campaigns (M181)."
    ),
)
def test_task_1_meets_plan_ms_target_of_five_clicks(task1: Task1) -> None:
    assert len(task1.walk.clicks) <= TASK1_TARGET, task1.walk.clicks


def test_task_1_for_a_later_campaign_is_one_click_shorter_and_ends_in_the_same_list(
    task1_later: Task1,
) -> None:
    """Secondary pin: a goal that already has an approved model needs no approval of a new one."""
    walk = task1_later.walk
    assert tuple(walk.clicks) == LATER_CAMPAIGN_CLICKS, walk.clicks
    assert len(walk.clicks) == LATER_CAMPAIGN_CLICKS_PINNED == TASK1_CLICKS_PINNED - 1, walk.clicks
    assert len(walk.file_choices) == TASK1_FILE_CHOICES_PINNED
    assert task1_later.saved and task1_later.saved == task1_later.served
    assert task1_later.problems == [], task1_later.problems


# --- Task 2 ------------------------------------------------------------------------------------------------


@dataclass
class Task2:
    base: str
    proofs: list[dict[str, Any]]
    overview: dict[str, Any]
    reports_walk: Walk
    reports_rows: dict[str, str]
    reports_claims: dict[str, str]
    reports_words: dict[str, str]
    reports_problems: list[str]
    campaign_walk: Walk
    campaign_headline: str
    campaign_evidence: str
    campaign_id: str
    campaign_problems: list[str]
    overview_tile: str


@pytest.fixture(scope="module")
def task2(pristine_store: Path, workdir: Path, playwright_driver: Any) -> Iterator[Task2]:
    """Task 2 twice, each on its own copy: by Reports, and by Campaigns, the campaign and its Results tab."""
    with serve(pristine_store, workdir, "task2") as base:
        proofs = [p for p in _get(base, "/pilot/proof")["proofs"] if p["status"] == "ready"]
        assert proofs, "the demo store has measured campaigns with a Value Proof Pack"
        overview = _get(base, "/overview")

        # a) Overview -> Reports
        browser, page, problems = new_page(playwright_driver)
        reports = Walk(page)
        try:
            reports.open(f"{base}/ui/#/overview")
            page.locator("[data-value]").wait_for(timeout=PAGE_TIMEOUT_MS)
            tile = page.locator("[data-value-amount]").inner_text()  # on screen with no click at all
            reports.click(TASK2_REPORTS_CLICKS[0], "nav.sh-side a[href='#/reports']")
            page.locator("[data-proofs]").wait_for(timeout=PAGE_TIMEOUT_MS)
            rows: dict[str, str] = {}
            claims: dict[str, str] = {}
            words: dict[str, str] = {}
            for proof in proofs:
                row = page.locator("tr", has=page.locator(f'[data-proof-campaign="{proof["campaign_id"]}"]'))
                rows[proof["campaign_id"]] = row.inner_text()
                claims[proof["campaign_id"]] = row.locator("[data-claim]").get_attribute("data-claim") or ""
                words[proof["campaign_id"]] = row.locator("[data-claim]").inner_text()
            page.screenshot(path=str(workdir / "task2_reports.png"), full_page=True)
        finally:
            browser.close()

        # b) Overview -> Campaigns -> the campaign -> Results
        browser, page, campaign_problems = new_page(playwright_driver)
        by_page = Walk(page)
        try:
            by_page.open(f"{base}/ui/#/overview")
            page.locator("[data-new-campaign]").wait_for(timeout=PAGE_TIMEOUT_MS)
            by_page.click(TASK2_CAMPAIGN_CLICKS[0], "nav.sh-side a[href='#/campaigns']")
            page.locator("[data-campaigns-list]").wait_for(timeout=PAGE_TIMEOUT_MS)
            wanted = proofs[0]["campaign_id"]
            by_page.click(
                TASK2_CAMPAIGN_CLICKS[1], f'[data-campaigns-list] a[href="#/campaigns/{wanted}/overview"]'
            )
            by_page.click(TASK2_CAMPAIGN_CLICKS[2], f'a[href="#/campaigns/{wanted}/results"]')
            page.locator("[data-results-measured]").wait_for(timeout=PAGE_TIMEOUT_MS)
            headline = page.locator("[data-headline]").inner_text()
            evidence = page.locator("[data-evidence]").inner_text()
            page.screenshot(path=str(workdir / "task2_campaign.png"), full_page=True)
        finally:
            browser.close()

        yield Task2(
            base=base,
            proofs=proofs,
            overview=overview,
            reports_walk=reports,
            reports_rows=rows,
            reports_claims=claims,
            reports_words=words,
            reports_problems=problems,
            campaign_walk=by_page,
            campaign_headline=headline,
            campaign_evidence=evidence,
            campaign_id=wanted,
            campaign_problems=campaign_problems,
            overview_tile=tile,
        )


def test_task_2_by_reports_is_one_click_and_shows_each_campaigns_value_and_how_sure(task2: Task2) -> None:
    walk = task2.reports_walk
    assert tuple(walk.clicks) == TASK2_REPORTS_CLICKS, walk.clicks
    assert len(walk.clicks) == TASK2_REPORTS_PINNED, walk.clicks
    for proof in task2.proofs:
        text = task2.reports_rows[proof["campaign_id"]]
        assert (
            proof["headline"] in text
        ), f"the row shows the API's headline for {proof['campaign_id']}: {text!r}"
        # the row's evidence label is the one the API's own `claim` for this campaign maps to (all three values)
        claim = str(_get(task2.base, f"/pilot/proof/{proof['campaign_id']}?format=json")["claim"])
        key, word = EVIDENCE_ON_SCREEN[claim]
        assert task2.reports_claims[proof["campaign_id"]] == key, (claim, text)
        assert task2.reports_words[proof["campaign_id"]] == word, (claim, text)
    assert task2.reports_problems == [], task2.reports_problems


def test_task_2_by_the_campaign_page_is_three_clicks_and_shows_the_apis_figure(task2: Task2) -> None:
    walk = task2.campaign_walk
    assert tuple(walk.clicks) == TASK2_CAMPAIGN_CLICKS, walk.clicks
    assert len(walk.clicks) == TASK2_CAMPAIGN_PAGE_PINNED, walk.clicks
    pack = _get(task2.base, f"/pilot/proof/{task2.campaign_id}?format=json")
    assert task2.campaign_headline == pack["headline"], "the Results tab says what the Value Proof Pack says"
    assert task2.campaign_evidence.startswith("Proven") is (
        pack["claim"] == "proven"
    ), task2.campaign_evidence
    assert task2.campaign_problems == [], task2.campaign_problems


def test_the_overviews_value_tile_is_the_programme_total_and_needs_no_click(task2: Task2) -> None:
    """The tile is on screen when the task starts (no click), is the API's own figure, and is the total of the
    measured campaigns, so it answers "what has the programme proven", not "what did last month's campaign earn".
    """
    headline = task2.overview["headline"]
    assert headline is not None and task2.overview["summary_checked"] is True
    assert headline["total"]["text"] in task2.overview_tile, (headline["total"]["text"], task2.overview_tile)
    assert headline["campaigns_counted"] >= 1
    assert len(task2.proofs) >= headline["campaigns_counted"]
