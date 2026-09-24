"""Plan A section 6's acceptance test, clicked through the real screens in a real browser (M35).

    "In the browser, on the synthetic raw telecom fixtures: create client -> upload three raw tables
    -> accept suggested roles, mappings and features -> keep the default churn definition -> build
    -> train -> next month's tables -> score -> download scores.csv with the client's own customer
    IDs, a band, an action and a reason for every row."

`tests/integration/test_acceptance.py` walks Phase 1's sentence - a prepared file, uploaded - and
this module walks Phase 2's, the same way: the real app under uvicorn, `/ui` in Chromium, and no
request made by the test itself. Every step is a click, a file handed to a file input, or a value
read off the page.

The journey, screen by screen:

1. The header's client picker stands on the default client ("Demo"); a new client is created from
   it, the way a consultant starting on a new account would.
2. Telco Customer Churn -> Setup Step 1 -> "Build from raw tables", and the four-step panel opens
   inline. The client's raw tables (`tests/fixtures/raw/make_raw.py`: the customer master with the
   client's own column names, one row per invoice, one per ticket, one per login) go into its file
   input. Every proposed role is confirmed, every suggested mapping accepted, the suggested features
   and the use case's own churn definition kept, and the dataset built.
3. "Use this dataset" fills Step 2 - the key is both columns, the target is the label, and the
   split is time-based on the snapshot date - and "Run training" trains on it.
4. The Data page shows where the dataset came from: sources -> mapping -> recipe -> dataset -> run.
5. "Score new data" -> "Upload this month's tables": the same client's extract one month later
   (`make_next_month`) goes into the panel, the saved recipe is replayed onto it with no mapping to
   review, and "Score data" scores it with the model just trained.
6. `scores.csv` is downloaded from the Output page and read: one row per customer, keyed by the
   client's own customer ids, with a band, an action and a reason on every row.

Four tables rather than the sentence's three: the churn definition the use case ships is "no
activity of any kind in the 60 days after the snapshot", so the activity log is the one table the
label cannot do without, and keeping that default is part of the sentence.

**The one thing that differs from a real user's run is the compute budget**, exactly as in
`test_acceptance.py`: the app runs against a copy of `configs/` whose `telco_churn.yaml` sets a
one-minute search budget, in a file the screens never expose. The build's own time is measured
separately at full size (`scripts/bench_onboarding.py`, M37); here the tables are small enough for
a test.

The module **skips** rather than fails when playwright or a usable browser is missing.
"""

from __future__ import annotations

import csv
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import pandas as pd
import pytest
import yaml

from engine.config import load_use_case
from engine.stages.actions import CONTROL_ACTION, SUPPRESSED_ACTION
from tests.fixtures.raw.make_raw import RawTables, make_next_month, make_raw

pytestmark = [pytest.mark.slow, pytest.mark.integration]

sync_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright is not installed; the acceptance journey needs a browser"
)

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
USE_CASE: Final[str] = "telco-churn"
USE_CASE_NAME: Final[str] = "Telco Customer Churn"
CLIENT_NAME: Final[str] = "Acceptance Telecom"
DEFAULT_CLIENT: Final[str] = "Demo"
KEY_LABEL: Final[str] = "entity_key + snapshot_date"
SAVED: Final[re.Pattern[str]] = re.compile(r"^Saved$")
"""A mapping card's saved state, matched whole: "Not saved yet" contains the word too."""
LABEL: Final[str] = "churn_next_60d"
ROLE_LABELS: Final[list[str]] = ["Activity", "Bills", "Complaints", "Subscriber table"]
"""The four tables' roles as the Sources step words them, sorted: the entity role reads as the use
case's own noun (`uc.entity`, "subscriber" for telco churn) plus "table" (v1 UI, WP3)."""

CUSTOMERS: Final[int] = 600
"""Enough customers that twelve monthly snapshots clear validation with room to spare (`min_rows`
1000, `min_positive` 200), few enough that the build and a one-minute search stay test-sized."""

USAGE_ROWS: Final[int] = 6_000
TABLES: Final[tuple[str, ...]] = ("customers", "bills", "complaints", "activity")

HOST: Final[str] = "127.0.0.1"
HEALTH_TIMEOUT_S: Final[float] = 120.0
BUILD_TIMEOUT_MS: Final[int] = 600_000
TRAIN_TIMEOUT_S: Final[float] = 1800.0
SCORE_TIMEOUT_S: Final[float] = 900.0
POLL_MS: Final[int] = 2000
UPLOAD_TIMEOUT_MS: Final[int] = 180_000
ACTION_TIMEOUT_MS: Final[int] = 30_000

TEST_BUDGET: Final[str] = """
# --- appended by tests/integration/test_onboarding_acceptance.py, never committed to configs/ ---
# The ONLY thing this run changes about the product's defaults: the search budget, as in
# test_acceptance.py, in a place the screens cannot reach.
model_search:
  time_limit_minutes: 1
  tuning_trials: 5
"""


# --- the app, in a browser-reachable process ------------------------------------------------------


@pytest.fixture(scope="module")
def workdir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("onboarding-acceptance")


@pytest.fixture(scope="module")
def config_root(workdir: Path) -> Path:
    """A copy of `configs/` carrying the test budget, so the checkout's own configs stay untouched."""
    root = workdir / "configs"
    shutil.copytree(REPO_ROOT / "configs", root)
    path = root / "use_cases" / f"{USE_CASE.replace('-', '_')}.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "model_search" not in document, (
        "telco_churn.yaml now sets model_search itself; appending a second block would silently "
        "shadow it. Merge the test budget into that block instead."
    )
    path.write_text(path.read_text(encoding="utf-8") + TEST_BUDGET, encoding="utf-8")
    return root


@pytest.fixture(scope="module")
def tables(workdir: Path) -> RawTables:
    """This month's raw tables, exactly as `make_raw` writes a client's extract."""
    return make_raw(workdir / "raw", customers=CUSTOMERS, usage_rows=USAGE_ROWS)


@pytest.fixture(scope="module")
def next_month(tables: RawTables, workdir: Path) -> RawTables:
    """The same client's extract a month later: same files, same columns, thirty more days."""
    return make_next_month(tables, workdir / "next_month")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((HOST, 0))
        return int(sock.getsockname()[1])


def log_tail(log: Path, lines: int = 40) -> str:
    try:
        return "\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError as error:  # pragma: no cover - only when the log itself is unreadable
        return f"(no server log: {error})"


def wait_for_health(base_url: str, process: subprocess.Popen[bytes], log: Path) -> None:
    deadline = time.monotonic() + HEALTH_TIMEOUT_S
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail(f"the server exited with {process.returncode} before serving:\n{log_tail(log)}")
        try:
            with urllib.request.urlopen(f"{base_url}/healthz", timeout=5) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.5)
    pytest.fail(f"/healthz never answered within {HEALTH_TIMEOUT_S:.0f}s:\n{log_tail(log)}")


@pytest.fixture(scope="module")
def server(workdir: Path, config_root: Path) -> Iterator[str]:
    """The real app under uvicorn, its working directory in the temp tree (see test_acceptance.py)."""
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
    command += ["--log-level", "warning"]
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


@pytest.fixture(scope="module")
def page() -> Iterator[sync_api.Page]:
    """A Chromium page, or a skip: an environment without a browser is not a product failure."""
    playwright = sync_api.sync_playwright().start()
    try:
        try:
            browser = playwright.chromium.launch(headless=True)
        except sync_api.Error as error:
            pytest.skip(f"no usable chromium for playwright: {error}")
        context = browser.new_context(accept_downloads=True, viewport={"width": 1600, "height": 1400})
        context.route("https://fonts.googleapis.com/**", lambda route: route.abort())
        context.route("https://fonts.gstatic.com/**", lambda route: route.abort())
        opened = context.new_page()
        opened.set_default_timeout(ACTION_TIMEOUT_MS)
        try:
            yield opened
        finally:
            context.close()
            browser.close()
    finally:
        playwright.stop()


# --- reading and driving the screens --------------------------------------------------------------


def screen_report(screen: sync_api.Page, log: Path) -> str:
    """Everything the screen and the server can say about a step that did not finish."""
    parts = []
    for selector, title in (
        (".summary", "summary"),
        (".apierr", "error box"),
        (".vlist", "checks"),
        (".progress", "progress"),
        (".reason", "reasons"),
    ):
        texts = screen.locator(selector).all_inner_texts()
        if texts:
            parts.append(f"{title}: " + " | ".join(text.strip().replace("\n", " · ") for text in texts))
    shot = log.with_name("failure_screen.png")
    try:
        screen.screenshot(path=str(shot), full_page=True)
        parts.append(f"screenshot: {shot}")
    except sync_api.Error as error:  # pragma: no cover - only when the page itself is gone
        parts.append(f"(no screenshot: {error})")
    parts.append(f"server log:\n{log_tail(log)}")
    return "\n".join(parts)


def wait_for_results(screen: sync_api.Page, *, timeout_s: float, log: Path, label: str) -> None:
    """Poll the Running screen until Results, failing with the page's own words if it fails."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if screen.locator(".summary .ok").count():
            return
        if screen.locator(".summary .bad").count() or screen.locator("#f-setup .apierr").count():
            pytest.fail(f"the {label} run failed in the UI:\n{screen_report(screen, log)}")
        screen.wait_for_timeout(POLL_MS)
    pytest.fail(
        f"the {label} run never reached Results within {timeout_s:.0f}s:\n{screen_report(screen, log)}"
    )


def open_step(screen: sync_api.Page, step: str) -> sync_api.Locator:
    """Expand one of the panel's four steps (a `<details>`), and return it."""
    details = screen.locator(f'#f-onboarding details[data-step="{step}"]')
    if not details.evaluate("node => node.open"):
        details.locator(":scope > summary").click()
    return details


def build_and_use(screen: sync_api.Page, log: Path) -> str:
    """Build step: start the build, wait for its report, and take the dataset into Step 2.

    Returns the build review's future-data check line (ruling R1, DEC-870), read before the dataset
    is taken into Step 2."""
    build = open_step(screen, "build")
    button = build.locator('[data-act="build"]')
    sync_api.expect(button).to_be_enabled(timeout=UPLOAD_TIMEOUT_MS)
    button.click()
    use = screen.locator('#f-onboarding [data-act="use-dataset"]')
    try:
        sync_api.expect(use).to_be_enabled(timeout=BUILD_TIMEOUT_MS)
    except AssertionError:
        pytest.fail(f"the dataset build did not pass:\n{screen_report(screen, log)}")
    leak_check = (screen.locator("#f-onboarding [data-leak-check]").first.text_content() or "").strip()
    use.click()
    sync_api.expect(screen.locator("#f-pk")).to_have_value(KEY_LABEL, timeout=ACTION_TIMEOUT_MS)
    return leak_check


def selected_text(screen: sync_api.Page, selector: str) -> str:
    return str(screen.locator(selector).evaluate("node => node.options[node.selectedIndex].text")).strip()


# --- the journey ----------------------------------------------------------------------------------


@dataclass
class Journey:
    """What the browser saw, collected once so each clause of the sentence is asserted on its own."""

    default_client: str = ""
    chosen_client: str = ""
    roles: list[str] = field(default_factory=list)
    pk_after_use: str = ""
    build_leak_check: str = ""
    target_after_use: str = ""
    problem_type: str = ""
    split_after_use: str = ""
    train_summary: str = ""
    data_block: tuple[str, str] = ("", "")
    lineage: list[str] = field(default_factory=list)
    score_roles: list[str] = field(default_factory=list)
    score_mapping_note: str = ""
    score_pk_after_use: str = ""
    score_summary: str = ""
    scores_header: list[str] = field(default_factory=list)
    scores_rows: list[dict[str, str]] = field(default_factory=list)
    seconds: dict[str, float] = field(default_factory=dict)


@pytest.fixture(scope="module")
def journey(
    page: sync_api.Page, server: str, workdir: Path, tables: RawTables, next_month: RawTables
) -> Journey:
    """Walk Plan A section 6's sentence once, through the screens, and bring back what was on them."""
    expect = sync_api.expect
    log = workdir / "server.log"
    seen = Journey()
    started = time.monotonic()

    def mark(step: str, since: float) -> float:
        seen.seconds[step] = round(time.monotonic() - since, 1)
        return time.monotonic()

    # 1. The product opens on the default client; a new one is created from the header's picker.
    page.goto(f"{server}/ui/", wait_until="domcontentloaded")
    expect(page.get_by_role("heading", name="Marketing AI")).to_be_visible()
    expect(page.locator("#f-client")).to_be_visible(timeout=ACTION_TIMEOUT_MS)
    seen.default_client = selected_text(page, "#f-client")
    page.locator("#f-client").select_option(label="+ New client")
    page.locator("#f-client-name").fill(CLIENT_NAME)
    page.locator("#f-client-add").click()
    expect(page.locator("#f-client")).to_be_visible()
    expect(page.locator("#f-client option:checked")).to_have_text(CLIENT_NAME)
    seen.chosen_client = selected_text(page, "#f-client")
    at = mark("create_client", started)

    # 2. The use case, Step 1's second card, and the client's raw tables into the panel.
    page.get_by_role("link", name=USE_CASE_NAME).click()
    try:
        expect(page.get_by_role("heading", name=USE_CASE_NAME)).to_be_visible(timeout=ACTION_TIMEOUT_MS)
    except AssertionError:
        pytest.fail(f"the use case did not open:\n{screen_report(page, log)}")
    page.locator('.pickcard[data-source="raw"]').click()
    panel = page.locator("#f-onboarding")
    files = page.locator('#f-onboarding input[data-act="pick-files"]')
    expect(files).to_be_attached()
    files.set_input_files([str(getattr(tables, name)) for name in TABLES])
    rows = panel.locator('details[data-step="sources"] tbody tr')
    expect(rows).to_have_count(len(TABLES), timeout=UPLOAD_TIMEOUT_MS)

    # Accept every proposed role: the select already shows the detector's first choice.
    confirm = panel.locator('[data-act="confirm-role"]')
    for remaining in range(len(TABLES), 0, -1):
        expect(confirm).to_have_count(remaining, timeout=UPLOAD_TIMEOUT_MS)
        confirm.first.click()
    expect(confirm).to_have_count(0, timeout=UPLOAD_TIMEOUT_MS)
    seen.roles = sorted(
        selected_text(page, f'#f-onboarding select[data-act="set-role"] >> nth={index}')
        for index in range(len(TABLES))
    )
    at = mark("sources", at)

    # Accept every suggested mapping.
    mapping = open_step(page, "mapping")
    accept = mapping.locator('[data-act="accept-all"]')
    expect(accept).to_have_count(len(TABLES), timeout=UPLOAD_TIMEOUT_MS)
    for index in range(len(TABLES)):
        mapping.locator('[data-act="accept-all"]').nth(index).click()
        expect(mapping.locator(".kv .k", has_text=SAVED)).to_have_count(index + 1, timeout=UPLOAD_TIMEOUT_MS)
    at = mark("mapping", at)

    # Suggested features and the default churn definition are kept: step 3 is left as it opened.
    seen.build_leak_check = build_and_use(page, log)
    seen.pk_after_use = selected_text(page, "#f-pk")
    seen.target_after_use = selected_text(page, "#f-target")
    seen.problem_type = page.locator(".ptype .pill").inner_text().strip()
    # The advanced settings are folded away, so the split line is read, not seen.
    split_line = page.locator('.stage-d[data-stage="data_split"] .ss').text_content()
    seen.split_after_use = (split_line or "").strip()
    at = mark("build", at)

    # 3. Train on it.
    run = page.get_by_role("button", name="Run training")
    expect(run).to_be_enabled()
    run.click()
    wait_for_results(page, timeout_s=TRAIN_TIMEOUT_S, log=log, label="training")
    seen.train_summary = page.locator(".summary").inner_text().strip()
    first_block = page.locator(".flow .block").nth(0)
    seen.data_block = (
        first_block.locator(".val").inner_text().strip(),
        first_block.locator(".meta").inner_text().strip(),
    )
    at = mark("train", at)

    # 4. The Data page's lineage block.
    first_block.click()
    expect(page.locator(".tab.on")).to_contain_text("Data")
    page.get_by_text("Details: data lineage").click()  # behind a disclosure since v1 (WP4)
    expect(page.locator(".lineage")).to_be_visible()
    seen.lineage = [text.strip() for text in page.locator(".lineage .lin .lt").all_inner_texts()]
    at = mark("lineage", at)

    # 5. A month later: this month's tables through the saved recipe, scored with the new model.
    page.locator(".crumbs a").nth(2).click()  # Home › journey › the use case (v1 breadcrumb)
    expect(page.locator(".summary")).to_be_visible()
    page.locator("#f-again").click()
    page.get_by_role("button", name="Score new data").click()
    expect(page.locator("#f-scorerun")).to_be_visible()
    page.locator('.pickcard[data-source="raw"]').click()
    files = page.locator('#f-onboarding input[data-act="pick-files"]')
    expect(files).to_be_attached(timeout=ACTION_TIMEOUT_MS)
    files.set_input_files([str(getattr(next_month, name)) for name in TABLES])
    build = page.locator('#f-onboarding details[data-step="build"]')
    expect(build).to_have_attribute("open", "", timeout=UPLOAD_TIMEOUT_MS)
    seen.score_roles = sorted(
        text.strip()
        for text in page.locator(
            '#f-onboarding details[data-step="sources"] tbody tr td:nth-child(3)'
        ).all_text_contents()
    )
    # Both steps are folded away by now (the panel opened Build), so their text is read, not seen.
    note = page.locator('#f-onboarding details[data-step="mapping"] .empty').text_content()
    seen.score_mapping_note = (note or "").strip()
    build_and_use(page, log)
    seen.score_pk_after_use = selected_text(page, "#f-pk")
    score = page.get_by_role("button", name="Score data")
    expect(score).to_be_enabled()
    score.click()
    wait_for_results(page, timeout_s=SCORE_TIMEOUT_S, log=log, label="scoring")
    seen.score_summary = page.locator(".summary").inner_text().strip()
    at = mark("score", at)

    # 6. The scored file, downloaded from the Output page.
    page.locator(".flow .block").nth(2).click()
    expect(page.locator(".tab.on")).to_contain_text("Output")
    with page.expect_download() as scores_info:
        page.get_by_role("link", name="Download contact list (CSV)").click()
    scores = workdir / "scores.csv"
    scores_info.value.save_as(scores)
    with scores.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        seen.scores_header = list(reader.fieldnames or [])
        seen.scores_rows = list(reader)
    mark("download_scores", at)
    seen.seconds["total"] = round(time.monotonic() - started, 1)
    print(f"\nonboarding acceptance journey seconds: {seen.seconds}")
    return seen


# --- the sentence, clause by clause ------------------------------------------------------------------


def test_the_header_starts_on_the_default_client_and_a_new_one_can_be_created(journey: Journey) -> None:
    """ "create client": the picker stands on "Demo" before anyone has made a client, and the one
    created from it is the one every later step builds for."""
    assert journey.default_client == DEFAULT_CLIENT
    assert journey.chosen_client == CLIENT_NAME


def test_the_first_build_of_the_new_recipe_ran_the_full_leak_check(journey: Journey) -> None:
    """Ruling R1 (DEC-871): a new client's first build of its recipe runs the full future-data check,
    and the build review says so in the engine's words."""
    assert journey.build_leak_check.startswith("Future-data check"), journey.build_leak_check
    assert "Full future-data check" in journey.build_leak_check
    assert "first build of this recipe" in journey.build_leak_check


def test_every_raw_table_got_its_proposed_role(journey: Journey) -> None:
    """ "upload raw tables -> accept suggested roles": each table's first-ranked role, confirmed."""
    assert journey.roles == ROLE_LABELS


def test_use_this_dataset_filled_step_two_from_the_manifest(journey: Journey) -> None:
    """Both key columns, the label as the target, and the problem type the label implies."""
    assert journey.pk_after_use == KEY_LABEL
    assert journey.target_after_use == LABEL
    assert journey.problem_type.lower().startswith("classification"), journey.problem_type


def test_use_this_dataset_split_the_snapshots_by_time(journey: Journey, workdir: Path) -> None:
    """A periodic dataset is split on its snapshot date, as the prototype's `useDataset()` sets it.

    Step 2's split line says so, the training run's `run_config.json` carries both leaves as the
    request's own overrides, and `split.json` reports the split that was actually made: the test
    part starts after the last training snapshot.
    """
    assert (
        "Time-based" in journey.split_after_use and "by snapshot_date" in journey.split_after_use
    ), journey.split_after_use
    trained = [
        run_dir
        for run_dir in (workdir / "data" / "runs").iterdir()
        if json.loads((run_dir / "run.json").read_text(encoding="utf-8"))["mode"] == "train"
    ]
    assert len(trained) == 1, trained
    resolved = json.loads((trained[0] / "run_config.json").read_text(encoding="utf-8"))
    assert resolved["overrides_applied"]["split"] == {"type": "time_based", "time_column": "snapshot_date"}
    assert resolved["sources"]["split.type"] == resolved["sources"]["split.time_column"] == "override"
    split = json.loads((trained[0] / "split.json").read_text(encoding="utf-8"))
    assert split["type"] == "time_based" and split["time_column"] == "snapshot_date", split
    assert split["train_cutoff"] < split["test_cutoff"], split


def test_the_training_run_read_the_built_dataset(journey: Journey) -> None:
    """ "train": Results, on the dataset rather than a file, keyed on both columns."""
    assert "Training complete" in journey.train_summary, journey.train_summary
    value, meta = journey.data_block
    assert value.endswith("(built)"), value
    assert f"key {KEY_LABEL}" in meta and f"target {LABEL}" in meta, meta


def test_the_data_page_shows_where_the_dataset_came_from(journey: Journey) -> None:
    """Sources -> mapping -> recipe -> dataset -> run, left to right."""
    assert journey.lineage == ["Sources", "Mapping", "Recipe", "Dataset", "Run"]


def test_next_months_tables_replayed_the_recipe_without_a_mapping_to_review(journey: Journey) -> None:
    """ "next month's tables": roles from the saved recipe, and the mapping step stays closed."""
    assert journey.score_roles == ROLE_LABELS
    assert "replayed exactly as it was" in journey.score_mapping_note, journey.score_mapping_note
    assert journey.score_pk_after_use == KEY_LABEL


def test_the_scoring_run_scored_every_customer(journey: Journey) -> None:
    assert "Scoring complete" in journey.score_summary, journey.score_summary
    assert f"{CUSTOMERS:,} rows scored" in journey.score_summary, journey.score_summary


def test_scores_csv_is_keyed_by_the_clients_own_customer_ids(journey: Journey, next_month: RawTables) -> None:
    """One row per customer, under the id the client's own customer master gives them, as text."""
    customers = pd.read_csv(next_month.customers, dtype=str, keep_default_na=False)
    assert journey.scores_header[:2] == ["entity_key", "snapshot_date"], journey.scores_header
    ids = [row["entity_key"] for row in journey.scores_rows]
    assert len(ids) == len(set(ids)) == CUSTOMERS
    assert set(ids) == set(customers["CUST_ID"])
    assert len({row["snapshot_date"] for row in journey.scores_rows}) == 1


def test_every_scored_row_carries_a_band_an_action_and_a_reason(journey: Journey, config_root: Path) -> None:
    """ "...a band, an action and a reason for every row", read off the downloaded bytes."""
    config = load_use_case(USE_CASE, config_root)
    bands = {band.name for band in config.actions.bands}
    allowed = {band.action for band in config.actions.bands} | {SUPPRESSED_ACTION, CONTROL_ACTION}
    rows = journey.scores_rows
    assert rows
    assert not [row for row in rows if row["band"] not in bands][:3]
    assert not [row for row in rows if row["action"] not in allowed][:3]
    assert not [row for row in rows if not row["reason_1"].strip()][:3]
