"""Plan M M177 (DEC-1607): the "New campaign" journey in a real browser, with the decisions counted.

Plan M §0 and §5 M177: from "+ New campaign" to a downloaded contact list, inside one wizard, in at most five
decisions once the file is chosen. This module starts the real app under uvicorn on an empty store (the app start
of `test_acceptance.py`, with its one-minute model search, the only change to the product's defaults), opens
`#/campaigns/new` in Chromium - the address "+ New campaign" opens - and walks it as a marketer would:

    Goal -> choose the file -> [1] Approve (Guided setup) -> [2] Approve and continue (what was learned)
         -> [3] Continue (Audience) -> [4] Approve (Review) -> [5] Download contact list

It is a goal's first campaign, so the wizard learns from the file and then builds the list from the same
customers; a new model waits for an approval by default (`governance.approval_required`), and the person here
holds every role (sign-in is off), so the approval is one of the five. The test plan is checked through the API
to be registered and frozen before the download is clicked, and the file downloaded is the contact list the API
serves for the campaign's list, byte for byte. Nothing in the engine is new: every call is an existing route.

Skips without playwright or a usable Chromium, like the other browser journeys.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

from tests.fixtures.make_data import CLEAN, GenerationSpec, write_csv
from tests.integration.test_acceptance import HOST, TEST_BUDGET, free_port, wait_for_health

pytestmark = [pytest.mark.slow, pytest.mark.integration]

sync_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright is not installed; the journey needs a browser"
)

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
USE_CASE: Final[str] = "targeted-advertisement"
GOAL_NAME: Final[str] = "Find customers likely to buy"
ROWS: Final[int] = 4000
SEED: Final[int] = 20_261_011
MAX_DECISIONS: Final[int] = 5
"""Plan M §0: "Clicks from Home to a downloaded contact list (file already prepared): <= 5, inside one wizard"."""
DECISIONS_PINNED: Final[int] = 5
"""What the journey takes today. A change that adds a decision fails here; one that removes one updates the pin."""
WAIT_MS: Final[int] = 900_000
"""Learning runs a one-minute model search plus preparing, evaluating and explaining; building the list follows."""


@pytest.fixture(scope="module")
def workdir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("wizard-journey")


@pytest.fixture(scope="module")
def config_root(workdir: Path) -> Path:
    root = workdir / "configs"
    shutil.copytree(REPO_ROOT / "configs", root)
    path = root / "use_cases" / f"{USE_CASE.replace('-', '_')}.yaml"
    assert "model_search" not in yaml.safe_load(path.read_text(encoding="utf-8"))
    path.write_text(path.read_text(encoding="utf-8") + TEST_BUDGET, encoding="utf-8")
    return root


@pytest.fixture(scope="module")
def customers(workdir: Path, config_root: Path) -> Path:
    """A prepared file: one row per customer with what each did last time (synthetic, `make_data`'s clean)."""
    spec = GenerationSpec(USE_CASE, rows=ROWS, seed=SEED, variant=CLEAN, config_root=config_root)
    return write_csv(spec, workdir / "customers.csv")


@pytest.fixture(scope="module")
def server(workdir: Path, config_root: Path) -> Iterator[str]:
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
        process = subprocess.Popen(command, cwd=workdir, env=env, stdout=sink, stderr=subprocess.STDOUT)
        try:
            wait_for_health(base_url, process, log)
            yield base_url
        finally:
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:  # pragma: no cover - a wedged trainer
                process.kill()
                process.wait(timeout=30)


def _get(base: str, path: str) -> Any:
    with urllib.request.urlopen(f"{base}{path}", timeout=30) as response:
        body = response.read()
    return json.loads(body) if body[:1] in (b"{", b"[") else body


def test_new_campaign_to_a_downloaded_contact_list_in_five_decisions(
    server: str, customers: Path, workdir: Path
) -> None:
    with sync_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(executable_path="/opt/pw-browsers/chromium")
        except sync_api.Error as error:  # pragma: no cover - a machine without the browser
            pytest.skip(f"no usable chromium for playwright: {error}")
        page = browser.new_page(viewport={"width": 1440, "height": 900}, accept_downloads=True)
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        decisions: list[str] = []

        def decide(selector: str, *, timeout: int = 60_000) -> None:
            """One decision: a click the person makes after the file is chosen."""
            locator = page.locator(selector)
            locator.wait_for(state="visible", timeout=timeout)
            decisions.append(selector)
            locator.click()

        try:
            page.goto(f"{server}/ui/#/campaigns/new")
            # Goal: the card named in business words.
            goal = page.locator(f'[data-cw-goal="{USE_CASE}"]')
            goal.wait_for(timeout=60_000)
            assert goal.locator(".cw-goal-name").inner_text() == GOAL_NAME
            goal.click()
            # Customers: Guided setup, unchanged, inside the wizard; the file is chosen.
            page.locator("[data-cw-customers=first] #ag-file").wait_for(state="attached", timeout=60_000)
            page.locator("#ag-file").set_input_files(str(customers))
            page.locator("#ag-approve:not([disabled])").wait_for(timeout=180_000)

            decide("#ag-approve")  # 1
            decide("[data-cw-approve-model]", timeout=WAIT_MS)  # 2: what was learned waits for an Approver
            page.locator("[data-cw-next]").wait_for(timeout=WAIT_MS)  # Audience, once the list is built
            size = page.locator("[data-cw-test-size]").inner_text()
            assert re.fullmatch(r"Enough to see a \d+\.\d-point change\.", size), size
            decide("[data-cw-next]")  # 3
            decide("[data-cw-approve]")  # 4: Review
            page.locator("[data-cw-download]").wait_for(timeout=60_000)

            campaign_id = page.evaluate("location.hash").rsplit("/", 1)[-1]
            # The test plan is registered and frozen BEFORE the download is offered (and clicked).
            plans = _get(server, f"/campaigns/{campaign_id}/plan")
            view = _get(server, f"/campaigns/{campaign_id}")
            assert plans["plan"] is not None and len(plans["versions"]) == 1
            assert plans["plan"]["plan_hash"] == view["campaign"]["test_plan_hash"]
            assert view["report"] is None, "nothing has been measured: the plan was fixed in advance"
            assert plans["plan"]["n_holdout"] > 0, "a comparison group is held back"

            with page.expect_download(timeout=60_000) as caught:
                decide("[data-cw-download]")  # 5
            download = caught.value
            saved = workdir / "contact_list.csv"
            download.save_as(saved)
        finally:
            page.screenshot(path=str(workdir / "wizard_last.png"), full_page=True)
            browser.close()

    assert len(decisions) <= MAX_DECISIONS, decisions
    assert len(decisions) == DECISIONS_PINNED, decisions
    run_id = view["campaign"]["run_ids"][0]
    served = _get(server, f"/runs/{run_id}/treat_list.csv")
    assert saved.read_bytes() == served, "the file downloaded is the campaign's contact list"
    assert errors == [], errors
