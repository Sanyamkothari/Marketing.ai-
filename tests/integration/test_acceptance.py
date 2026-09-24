"""Plan §11's Phase 1 acceptance test, clicked through the real screens in a real browser.

Plan §11 ends with the criterion this module exists to make real:

    "Overall Phase 1 acceptance test: a non-technical user downloads the template, fills it with
    real historical data, uploads it, keeps every default, clicks Run, and receives a scored file
    with reasons and actions for a second upload, without anyone from the ML team touching it."

Every other test in `tests/integration/` drives the Pipeline or the API in process, and
`tests/integration/test_ui.py` checks the screens' wiring without ever opening them, so until now
nothing performed that sentence. This module does: it starts the real app under uvicorn, opens
`/ui` in Chromium and walks the journey through the markup - Overview, Targeted Advertisement, the
Setup screen's own template link, the file input, Run, the Running screen, Results, the Data /
Model / Output pages, then "Score new data" and the `scores.csv` download link. No request is made
to the API by the test itself; every step is a click or a file the browser was handed.

**The one thing that differs from a real user's run is the compute budget.** The shipped default is
`model_search.time_limit_minutes: 30` (configs/engine.yaml), which no test can wait for, so the app
is started against a *copy* of `configs/` whose `targeted_advertisement.yaml` sets a one-minute
budget and `tuning_trials: 5` - the same pair, for the same reason, as
`tests/integration/test_train_flow.py`'s OVERRIDES. The edit is made before the server starts, in a
file the screens never expose: it is a test budget, not a default the user changed. On the screens
every default is kept - the primary key, the target and the problem type are whatever detection
proposed, Advanced settings is never opened, and the model that scores the second upload is the one
the dropdown offered.

"Real historical data" is not available offline, so the first upload is the committed synthetic
generator's `clean` variant (`tests/fixtures/make_data.py`) and the second is its `scoring` variant.

The module **skips** rather than fails when playwright or a usable browser is missing, so a CI
machine without a browser stays green instead of going red over an environment it never had.
"""

from __future__ import annotations

import csv
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

import pytest
import yaml

from engine.config import load_use_case
from engine.contracts import scores_csv_columns
from engine.stages.actions import CONTROL_ACTION, SUPPRESSED_ACTION
from tests.fixtures.make_data import CLEAN, SCORING, GenerationSpec, write_csv

pytestmark = [pytest.mark.slow, pytest.mark.integration]

sync_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright is not installed; the acceptance journey needs a browser"
)

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
USE_CASE: Final[str] = "targeted-advertisement"
USE_CASE_NAME: Final[str] = "Targeted Advertisement"
PRIMARY_KEY: Final[str] = "customer_id"
TARGET: Final[str] = "converted_30d"
EM_DASH: Final[str] = "—"

TRAIN_ROWS: Final[int] = 4000
"""Enough to clear validation with room to spare: `min_rows` is 1000 and ~12% positives is 480
against a `min_positive` of 200, while still training inside a one-minute budget."""

SCORE_ROWS: Final[int] = 1500
TRAIN_SEED: Final[int] = 20_260_922
SCORE_SEED: Final[int] = 20_260_923

HOST: Final[str] = "127.0.0.1"
HEALTH_TIMEOUT_S: Final[float] = 120.0
TRAIN_TIMEOUT_S: Final[float] = 1800.0
"""The train flow does real AutoGluon work behind a one-minute search budget; the rest of the run
(prepare, split, evaluate, SHAP, register) is not bounded by it, so the wait is generous."""

SCORE_TIMEOUT_S: Final[float] = 900.0
POLL_MS: Final[int] = 2000
UPLOAD_TIMEOUT_MS: Final[int] = 180_000
ACTION_TIMEOUT_MS: Final[int] = 30_000

TEST_BUDGET: Final[str] = """
# --- appended by tests/integration/test_acceptance.py, never committed to configs/ ---
# The ONLY thing this acceptance run changes about the product's defaults. The shipped budget is
# `model_search.time_limit_minutes: 30` (configs/engine.yaml), which no test can wait for. This is
# the budget of plan §10's integration run (test_train_flow.py's OVERRIDES), applied here in a
# place the screens cannot reach, so the journey itself still keeps every default.
model_search:
  time_limit_minutes: 1
  tuning_trials: 5
"""


# --- the app, in a browser-reachable process ------------------------------------------------------


@pytest.fixture(scope="module")
def workdir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One directory for the copied configs, the data dir, the uploads and the screenshot."""
    return tmp_path_factory.mktemp("acceptance")


@pytest.fixture(scope="module")
def config_root(workdir: Path) -> Path:
    """A copy of `configs/` carrying the test budget, so the checkout's own configs stay untouched."""
    root = workdir / "configs"
    shutil.copytree(REPO_ROOT / "configs", root)
    path = root / "use_cases" / f"{USE_CASE.replace('-', '_')}.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "model_search" not in document, (
        "targeted_advertisement.yaml now sets model_search itself; appending a second block would "
        "silently shadow it. Merge the test budget into that block instead."
    )
    path.write_text(path.read_text(encoding="utf-8") + TEST_BUDGET, encoding="utf-8")
    return root


def free_port() -> int:
    """A port the OS has just confirmed is free; uvicorn takes it a moment later."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((HOST, 0))
        return int(sock.getsockname()[1])


def wait_for_health(base_url: str, process: subprocess.Popen[bytes], log: Path) -> None:
    """Block until `/healthz` answers, or fail with whatever the server managed to say first."""
    deadline = time.monotonic() + HEALTH_TIMEOUT_S
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail(f"the server exited with {process.returncode} before serving:\n{log_tail(log)}")
        try:
            with urllib.request.urlopen(f"{base_url}/healthz", timeout=5) as response:
                if response.status == 200:
                    return
        except OSError:  # URLError and every connection refusal are OSErrors
            time.sleep(0.5)
    pytest.fail(f"/healthz never answered within {HEALTH_TIMEOUT_S:.0f}s:\n{log_tail(log)}")


def log_tail(log: Path, lines: int = 40) -> str:
    """The end of the server log, for a failure message that can be acted on."""
    try:
        return "\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError as error:  # pragma: no cover - only when the log itself is unreadable
        return f"(no server log: {error})"


@pytest.fixture(scope="module")
def server(workdir: Path, config_root: Path) -> Iterator[str]:
    """The real app under uvicorn: a browser cannot drive `TestClient`, so a process is needed.

    The child runs with its working directory in the temp tree, so nothing a run writes relative to
    the cwd (AutoGluon's own scratch, for one) can land in the checkout other agents share.
    """
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
        # The page asks Google Fonts for Inter. The journey must not depend on the internet, and a
        # stalled font request would stall `load`, so those two hosts are refused outright.
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


# --- reading the screens --------------------------------------------------------------------------


def tiles(screen: sync_api.Page) -> dict[str, str]:
    """The KPI tiles of a Data / Model / Output page, label -> value, as rendered."""
    found: dict[str, str] = {}
    for tile in screen.locator(".kpis .kpi").all():
        found[tile.locator(".l").inner_text().strip()] = tile.locator(".v").inner_text().strip()
    return found


def key_values(screen: sync_api.Page) -> list[tuple[str, str]]:
    """Every `.kv` row on the page as (key, value); a list because keys repeat across cards."""
    rows = []
    for row in screen.locator(".kv").all():
        rows.append((row.locator(".k").inner_text().strip(), row.locator(".v").inner_text().strip()))
    return rows


def flow_blocks(screen: sync_api.Page) -> list[tuple[str, str, str]]:
    """The Results screen's three pipeline blocks as (label, value, meta)."""
    blocks = []
    for block in screen.locator(".flow .block").all():
        blocks.append(
            (
                block.locator(".lab").inner_text().strip(),
                block.locator(".val").inner_text().strip(),
                block.locator(".meta").inner_text().strip(),
            )
        )
    return blocks


def screen_report(screen: sync_api.Page, log: Path) -> str:
    """Everything the screen and the server can say about a run that did not finish.

    A screenshot is taken here rather than only on success, because the screen a failed journey
    stopped on is the one worth looking at, and it is gone the moment the browser closes.
    """
    parts = []
    for selector, title in ((".summary", "summary"), (".apierr", "error box"), (".progress", "progress")):
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
    """Poll the Running screen until Results, failing with the page's own words if it fails.

    The screens re-render every two seconds while a run is live, so each pass re-queries rather than
    holding a handle. `.summary .ok` is the Results screen's "complete"; `.summary .bad` is its
    "failed"; `.apierr` is the Setup screen refusing the submission.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if screen.locator(".summary .ok").count():
            return
        if screen.locator(".summary .bad").count() or screen.locator(".apierr").count():
            pytest.fail(f"the {label} run failed in the UI:\n{screen_report(screen, log)}")
        screen.wait_for_timeout(POLL_MS)
    pytest.fail(
        f"the {label} run never reached Results within {timeout_s:.0f}s:\n{screen_report(screen, log)}"
    )


# --- the journey ----------------------------------------------------------------------------------


@dataclass
class Journey:
    """What the browser saw, collected once so each clause of plan §11 can be asserted on its own."""

    template_name: str = ""
    template_header: str = ""
    preview: str = ""
    train_summary: str = ""
    train_blocks: list[tuple[str, str, str]] = field(default_factory=list)
    data_tiles: dict[str, str] = field(default_factory=dict)
    data_rows: list[tuple[str, str]] = field(default_factory=list)
    model_tiles: dict[str, str] = field(default_factory=dict)
    model_rows: list[tuple[str, str]] = field(default_factory=list)
    train_output_tiles: dict[str, str] = field(default_factory=dict)
    model_choices: list[str] = field(default_factory=list)
    score_summary: str = ""
    score_output_tiles: dict[str, str] = field(default_factory=dict)
    scores_header: list[str] = field(default_factory=list)
    scores_rows: list[dict[str, str]] = field(default_factory=list)
    screenshot: Path = Path()
    seconds: dict[str, float] = field(default_factory=dict)


@pytest.fixture(scope="module")
def journey(page: sync_api.Page, server: str, workdir: Path, config_root: Path) -> Journey:
    """Walk plan §11's sentence once, through the screens, and bring back what was on them."""
    expect = sync_api.expect
    log = workdir / "server.log"
    seen = Journey()
    started = time.monotonic()

    def mark(step: str, since: float) -> float:
        seen.seconds[step] = round(time.monotonic() - since, 1)
        return time.monotonic()

    # 1. Open the product, land on the Overview, walk into the use case.
    page.goto(f"{server}/ui/", wait_until="domcontentloaded")
    expect(page.get_by_role("heading", name="Customer Lifecycle")).to_be_visible()
    page.get_by_role("link", name=USE_CASE_NAME).click()
    expect(page.get_by_role("heading", name=USE_CASE_NAME)).to_be_visible()
    expect(page.locator("#f-file")).to_be_attached()
    at = mark("open_ui", started)

    # 2. Download the template from the Setup screen's own link.
    with page.expect_download() as download_info:
        page.get_by_role("link", name="Download template").click()
    download = download_info.value
    template = workdir / "downloaded_template.csv"
    download.save_as(template)
    seen.template_name = download.suggested_filename
    seen.template_header = template.read_text(encoding="utf-8").splitlines()[0]
    at = mark("download_template", at)

    # 3. Fill it with data and upload it through the file input on the page.
    train_csv = write_csv(
        GenerationSpec(USE_CASE, rows=TRAIN_ROWS, seed=TRAIN_SEED, variant=CLEAN, config_root=config_root),
        workdir / "history.csv",
    )
    page.set_input_files("#f-file", str(train_csv))
    # Detection fills steps 2 and 3. Nothing below touches them: that is the point of the test.
    expect(page.locator("#f-pk")).to_have_value(PRIMARY_KEY, timeout=UPLOAD_TIMEOUT_MS)
    expect(page.locator("#f-target")).to_have_value(TARGET)
    seen.preview = page.locator(".preview .pv-head").inner_text().strip()
    at = mark("upload_history", at)

    # 4. Click Run. Advanced settings is never opened.
    assert (
        page.locator("#f-adv").evaluate("node => node.open") is False
    ), "Advanced settings was open before Run; this journey must keep every default."
    run = page.get_by_role("button", name="Run training")
    expect(run).to_be_enabled()
    run.click()
    expect(page.locator("#prog")).to_be_visible(timeout=ACTION_TIMEOUT_MS)
    wait_for_results(page, timeout_s=TRAIN_TIMEOUT_S, log=log, label="training")
    at = mark("train_run", at)

    seen.train_summary = page.locator(".summary").inner_text().strip()
    seen.train_blocks = flow_blocks(page)
    seen.screenshot = workdir / "results_screen.png"
    page.screenshot(path=str(seen.screenshot), full_page=True)

    # 5. The three pages behind the Results blocks, reached the way the screen offers them.
    page.locator(".flow .block").nth(0).click()
    # `.tab.on` names the page the router actually painted, so waiting on it cannot read the
    # previous page's tiles while the hash route is still changing.
    expect(page.locator(".tab.on")).to_contain_text("Data")
    seen.data_tiles, seen.data_rows = tiles(page), key_values(page)
    page.locator(".tabs .tab").nth(1).click()
    expect(page.locator(".tab.on")).to_contain_text("Model")
    seen.model_tiles, seen.model_rows = tiles(page), key_values(page)
    page.locator(".tabs .tab").nth(2).click()
    expect(page.locator(".tab.on")).to_contain_text("Output")
    seen.train_output_tiles = tiles(page)
    at = mark("pipeline_pages", at)

    # 6. The second half of the sentence: score a new file with the model just trained.
    page.locator(".crumbs a").nth(2).click()  # Home › journey › the use case (v1 breadcrumb)
    expect(page.locator(".summary")).to_be_visible()
    page.locator("#f-again").click()
    page.get_by_role("button", name="Score new data").click()
    expect(page.locator("#f-scorerun")).to_be_visible()
    seen.model_choices = [
        option.strip() for option in page.locator("#f-scorerun option").all_inner_texts() if option.strip()
    ]
    score_csv = write_csv(
        GenerationSpec(USE_CASE, rows=SCORE_ROWS, seed=SCORE_SEED, variant=SCORING, config_root=config_root),
        workdir / "new_customers.csv",
    )
    page.set_input_files("#f-file", str(score_csv))
    expect(page.locator("#f-pk")).to_have_value(PRIMARY_KEY, timeout=UPLOAD_TIMEOUT_MS)
    score = page.get_by_role("button", name="Score data")
    expect(score).to_be_enabled()
    score.click()
    wait_for_results(page, timeout_s=SCORE_TIMEOUT_S, log=log, label="scoring")
    seen.score_summary = page.locator(".summary").inner_text().strip()
    at = mark("score_run", at)

    # 7. Download the scored file from the Output page.
    page.locator(".flow .block").nth(2).click()
    expect(page.locator(".tab.on")).to_contain_text("Output")
    seen.score_output_tiles = tiles(page)
    with page.expect_download() as scores_info:
        page.get_by_role("link", name="Download all scored rows (CSV)").click()
    scores = workdir / "scores.csv"
    scores_info.value.save_as(scores)
    with scores.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        seen.scores_header = list(reader.fieldnames or [])
        seen.scores_rows = list(reader)
    mark("download_scores", at)
    seen.seconds["total"] = round(time.monotonic() - started, 1)
    print(f"\nacceptance journey seconds: {seen.seconds}")
    return seen


# --- plan §11, clause by clause ---------------------------------------------------------------------


def test_the_template_the_browser_downloaded_is_the_use_case_csv(journey: Journey) -> None:
    """ "...downloads the template...": a CSV, named for the use case, with the template's header."""
    committed = (REPO_ROOT / "templates" / "targeted_advertisement_template.csv").read_text(encoding="utf-8")
    assert journey.template_name.endswith(".csv"), journey.template_name
    assert USE_CASE.replace("-", "_") in journey.template_name
    assert journey.template_header == committed.splitlines()[0]
    assert PRIMARY_KEY in journey.template_header
    assert TARGET in journey.template_header


def test_the_upload_was_read_and_profiled_on_the_screen(journey: Journey) -> None:
    """ "...uploads it...": the Setup screen reports the file it read, not a placeholder."""
    assert f"{TRAIN_ROWS:,} rows" in journey.preview, journey.preview
    assert "10 columns" in journey.preview, journey.preview


def test_the_run_reached_results_and_the_summary_carries_real_values(journey: Journey) -> None:
    """ "...clicks Run...": Results, with the model and its score spelled out, no em dash."""
    assert "Training complete" in journey.train_summary
    assert EM_DASH not in journey.train_summary, journey.train_summary
    assert "ROC-AUC" in journey.train_summary
    score = re.search(r"ROC-AUC\s+([0-9.]+)", journey.train_summary)
    assert score is not None, journey.train_summary
    assert 0.0 < float(score.group(1)) <= 1.0


def test_the_results_blocks_name_the_data_and_the_model(journey: Journey) -> None:
    """The Data and Model blocks render values from the run; neither may fall back to an em dash.

    The Output block is not checked here: a *training* run writes no `scoring_summary.json`, so its
    KPI line is an em dash by design (plan §9.4). It is checked on the scoring run below, where the
    value must exist.
    """
    assert len(journey.train_blocks) == 3, journey.train_blocks
    (_, data_value, data_meta), (_, model_value, model_meta), _ = journey.train_blocks
    assert data_value == "history.csv"
    assert f"ID {PRIMARY_KEY}" in data_meta and f"predicting {TARGET}" in data_meta
    assert EM_DASH not in data_meta, data_meta
    assert model_value and EM_DASH not in model_value, model_value
    assert EM_DASH not in model_meta, model_meta
    assert re.search(r"[0-9]\.[0-9]", model_meta), model_meta


def test_the_data_page_renders_the_profile_and_the_split(journey: Journey) -> None:
    """Data page: `profile.json`, `prepare.json` and `split.json`, as numbers."""
    assert journey.data_tiles.get("Rows") not in (None, EM_DASH), journey.data_tiles
    assert journey.data_tiles.get("Missing values") not in (None, EM_DASH), journey.data_tiles
    features = journey.data_tiles.get("Features", EM_DASH)
    assert features.isdigit() and int(features) > 0, journey.data_tiles
    assert ("Rows", f"{TRAIN_ROWS:,}") in journey.data_rows, journey.data_rows
    assert ("Columns", "10") in journey.data_rows, journey.data_rows
    assert ("Format", "CSV") in journey.data_rows, journey.data_rows


def test_the_model_page_renders_the_trained_model(journey: Journey) -> None:
    """Model page: `best_model.json` and `evaluation.json`, as the algorithm and its metric."""
    algorithm = journey.model_tiles.get("Algorithm", EM_DASH)
    assert algorithm not in ("", EM_DASH), journey.model_tiles
    assert journey.model_tiles.get("Last trained") not in (None, EM_DASH), journey.model_tiles
    headline = journey.model_tiles.get("ROC-AUC", EM_DASH)
    assert re.fullmatch(r"[0-9.]+", headline), journey.model_tiles
    assert 0.0 < float(headline) <= 1.0
    assert ("Target", TARGET) in journey.model_rows, journey.model_rows
    assert ("Algorithm", algorithm) in journey.model_rows, journey.model_rows


def test_the_output_page_of_the_training_run_renders_the_lift(journey: Journey) -> None:
    """Output page after training: `decile_lift.json` is what that run produced, and it is drawn."""
    lift = journey.train_output_tiles.get("Lift (top decile)", EM_DASH)
    assert lift not in ("", EM_DASH), journey.train_output_tiles
    assert re.search(r"[0-9]", lift), lift


def test_the_trained_model_is_offered_for_scoring_without_an_ml_team(journey: Journey) -> None:
    """ "...without anyone from the ML team touching it": the model is selectable straight away.

    Nothing was approved or promoted between the two runs; the run just finished is in the dropdown
    the score mode offers, so the second half of the sentence is reachable by the same user.
    """
    assert journey.model_choices, "the score screen offered no trained model"
    assert "No trained model yet" not in journey.model_choices[0]


def test_the_second_upload_came_back_scored(journey: Journey) -> None:
    """ "...receives a scored file ... for a second upload": Results says how many rows, with which model."""
    assert "Scoring complete" in journey.score_summary
    assert f"{SCORE_ROWS:,} rows scored" in journey.score_summary, journey.score_summary
    assert EM_DASH not in journey.score_summary, journey.score_summary


def test_the_output_page_of_the_scoring_run_renders_the_kpi(journey: Journey) -> None:
    """Output page after scoring: `scoring_summary.json`'s KPI, row count and control group."""
    kpi = journey.score_output_tiles.get("Target audience", EM_DASH)
    assert kpi not in ("", EM_DASH), journey.score_output_tiles
    assert journey.score_output_tiles.get("Rows scored") == f"{SCORE_ROWS:,}", journey.score_output_tiles
    control = journey.score_output_tiles.get("Control group", EM_DASH)
    assert control not in ("", EM_DASH), journey.score_output_tiles


def test_the_downloaded_scores_file_has_the_contract_header(journey: Journey, config_root: Path) -> None:
    """The file the browser saved is `scores.csv` as plan §6.3 defines it, column for column."""
    config = load_use_case(USE_CASE, config_root)
    assert tuple(journey.scores_header) == scores_csv_columns(config, PRIMARY_KEY)


def test_every_scored_row_carries_a_band_an_action_and_a_reason(journey: Journey, config_root: Path) -> None:
    """ "...with reasons and actions...": plan §6.3 and DEC-056, read off the downloaded file.

    One row per uploaded row, every row banded, every row actioned, every row explained - checked on
    the bytes the browser received rather than in process.
    """
    config = load_use_case(USE_CASE, config_root)
    bands = {band.name for band in config.actions.bands}
    allowed = {band.action for band in config.actions.bands} | {SUPPRESSED_ACTION, CONTROL_ACTION}

    assert len(journey.scores_rows) == SCORE_ROWS, len(journey.scores_rows)
    missing_band = [row for row in journey.scores_rows if row["band"] not in bands]
    missing_action = [row for row in journey.scores_rows if not row["action"].strip()]
    unknown_action = [row for row in journey.scores_rows if row["action"] not in allowed]
    missing_reason = [row for row in journey.scores_rows if not row["reason_1"].strip()]
    assert not missing_band, missing_band[:3]
    assert not missing_action, missing_action[:3]
    assert not unknown_action, unknown_action[:3]
    assert not missing_reason, missing_reason[:3]
    assert len({row[PRIMARY_KEY] for row in journey.scores_rows}) == SCORE_ROWS


def test_a_screenshot_of_the_results_screen_was_saved(journey: Journey) -> None:
    """A failure elsewhere in this module should be diagnosable without re-running the journey."""
    assert journey.screenshot.is_file()
    assert journey.screenshot.stat().st_size > 0
