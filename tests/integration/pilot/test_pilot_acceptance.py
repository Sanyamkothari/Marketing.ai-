"""Plan E's acceptance, end to end, on the seeded demo (slow: the seed trains two models, ~2.5 min).

Plan E §4: a colleague outside the project, given only the data request kit and the demo, can
understand what data to send, run the pre-flight checker on the demo raw tables, read the data
readiness report and name the one blocking problem planted in a broken fixture, and explain the
results report and the value view. This module checks every piece of that a machine can:

* the demo seeds through the platform's own API, synthetic only, with no Criteo data;
* the pre-flight check on the demo's raw tables (downloaded as a visitor would) passes on the clean
  extract and names exactly the planted problem on the broken one;
* the readiness report of the broken build says "Not ready" and names that one problem with its fix;
* the results report states the headline, compares it to random, shows no customer ID and no jargon,
  and every number on it is one an artefact holds;
* the value view is measured, is a range, and scales with the client's inputs;
* in a browser, with demo mode on: the tour walks overview → setup → results → output → campaign
  results → the Pilot screen, a warning pill gets its "What does this mean?" button, and feedback
  is recorded.
"""

from __future__ import annotations

import io
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine.contracts import DecileLift
from engine.pilot.demo import EXCLUDED_DATA, DemoManifest
from engine.pilot.plain import jargon_in
from engine.pilot.preflight import run_preflight
from engine.storage import LocalStorage, run_key

pytestmark = [pytest.mark.slow, pytest.mark.integration]

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def data_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("pilot-demo") / "data"


@pytest.fixture(scope="module")
def manifest(data_dir: Path) -> DemoManifest:
    from scripts.seed_demo import seed

    data_dir.mkdir(parents=True, exist_ok=True)
    return seed(data_dir, rng_seed=20260923, force=True)


@pytest.fixture(scope="module")
def client(manifest: DemoManifest, data_dir: Path, config_root: Path) -> Iterator[TestClient]:
    # Demo mode through the environment rather than `create_app(settings=...)`, which would also
    # reconfigure the process's logging for every test after this module.
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("MARKETING_AI_DEMO_MODE", "true")
        patch.setenv("MARKETING_AI_DATA_DIR", str(data_dir))
        with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as test_client:
            yield test_client


def visible_text(html: str) -> str:
    body = html.split("<main", 1)[1]
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))


# --- the demo -----------------------------------------------------------------------------------------


def test_the_demo_is_synthetic_and_carries_no_excluded_data(manifest: DemoManifest) -> None:
    sources = " ".join(manifest.data_sources).lower()
    assert all(excluded not in sources for excluded in EXCLUDED_DATA)
    assert all("synthetic" in source or "simulated" in source for source in manifest.data_sources)
    assert manifest.planted_problem == "ENTITY_DUPLICATE_KEYS"


def test_demo_mode_serves_the_manifest(client: TestClient, manifest: DemoManifest) -> None:
    body = client.get("/pilot/demo").json()
    assert body["demo_mode"] and body["seeded"]
    assert DemoManifest.model_validate(body["manifest"]) == manifest


# --- understand what to send, and check it --------------------------------------------------------------


def unzip(client: TestClient, variant: str, target: Path) -> Path:
    response = client.get(f"/pilot/demo/raw/{variant}")
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        archive.extractall(target)
    return target / f"demo_telecom_{variant}"


def test_the_preflight_passes_the_clean_demo_tables(
    client: TestClient, tmp_path: Path, config_root: Path
) -> None:
    folder = unzip(client, "clean", tmp_path)
    result = run_preflight([folder], ("telco-churn",), root=config_root)
    assert [f.code for f in result.findings if f.severity == "error"] == []


def test_the_preflight_names_the_one_planted_problem(
    client: TestClient, tmp_path: Path, config_root: Path
) -> None:
    folder = unzip(client, "broken", tmp_path)
    result = run_preflight([folder], ("telco-churn",), root=config_root)
    assert [f.code for f in result.findings if f.severity == "error"] == ["ENTITY_DUPLICATE_KEYS"]


# --- the data readiness report ---------------------------------------------------------------------------


def test_the_broken_extract_is_not_ready_for_exactly_one_reason(
    client: TestClient, manifest: DemoManifest
) -> None:
    document = client.get(f"/pilot/readiness/{manifest.broken_dataset_id}", params={"format": "json"}).json()
    verdict = document["blocks"][0]
    assert verdict["kind"] == "verdict" and verdict["state"] == "not_ready"
    assert verdict["title"] == "Not ready: The customer table repeats customer IDs"
    blocking = [b for b in document["blocks"] if b["kind"] == "callout" and b["tone"] == "error"]
    assert len(blocking) == 1
    assert "How to fix it:" in blocking[0]["text"] and "one row per customer" in blocking[0]["text"]


def test_the_clean_build_is_ready(client: TestClient, manifest: DemoManifest) -> None:
    document = client.get(f"/pilot/readiness/{manifest.train_dataset_id}", params={"format": "json"}).json()
    assert document["blocks"][0]["state"] in ("ready", "warnings")
    html = client.get(f"/pilot/readiness/{manifest.train_dataset_id}").text
    assert "Examples of the outcome" in html and "How the tables link up" in html


def test_the_readiness_pdf_is_rendered_on_the_server(client: TestClient, manifest: DemoManifest) -> None:
    response = client.get(f"/pilot/readiness/{manifest.broken_dataset_id}", params={"format": "pdf"})
    assert response.status_code == 200 and response.content.startswith(b"%PDF-")
    assert response.headers["content-type"] == "application/pdf"


# --- the results report ----------------------------------------------------------------------------------


def test_the_results_headline_is_the_artefacts_first_decile(
    client: TestClient, manifest: DemoManifest, data_dir: Path
) -> None:
    html = client.get("/pilot/results", params={"use_case": manifest.use_case_id}).text
    deciles = LocalStorage(data_dir).read_model(
        run_key(manifest.train_run_id, "decile_lift.json"), DecileLift
    )
    capture = deciles.bins[0].cumulative_capture_pct
    assert capture is not None
    assert f"The 10% of customers the model flags first contain {capture:.0f}% of all" in html
    assert "A random 10% would contain 10% of them" in html


def test_the_results_report_shows_no_customer_id_and_no_jargon(
    client: TestClient, manifest: DemoManifest, data_dir: Path
) -> None:
    html = client.get("/pilot/results", params={"use_case": manifest.use_case_id}).text
    text = visible_text(html)
    scores = pd.read_parquet(data_dir / "runs" / manifest.score_run_id / "scores.parquet")
    ids = {str(value) for value in scores["entity_key"]}
    assert not set(re.findall(r"\b\d{5,}\b", text)) & ids
    assert jargon_in(text) == ()
    for section in (
        "What the model does",
        "How good it is",
        "Why customers are flagged",
        "What to do",
        "Limits",
    ):
        assert section in text
    assert "Customer A" in text and "Control group" in text


def test_the_uplift_champion_has_a_results_report_too(client: TestClient, manifest: DemoManifest) -> None:
    text = visible_text(client.get("/pilot/results", params={"use_case": manifest.uplift_use_case_id}).text)
    assert "an offer raised the response rate by" in text
    assert jargon_in(text) == ()


# --- the value view ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("index", [0, 1])
def test_each_demo_campaign_is_measured_and_valued_as_a_range(
    client: TestClient, manifest: DemoManifest, index: int
) -> None:
    run_id = manifest.campaigns[index].score_run_id
    view = client.get(f"/pilot/roi/{run_id}").json()
    assert view["status"] == "measured" and view["causal"]
    benefit = view["benefit"]
    assert benefit["low"] < benefit["value"] < benefit["high"]
    assert benefit["low"] > 0, "the planted effect is large enough to show"
    assert view["net_value"]["low"] < view["net_value"]["high"]


def test_new_inputs_change_the_rupees_and_not_the_count(client: TestClient, manifest: DemoManifest) -> None:
    run_id = manifest.campaigns[0].score_run_id
    before = client.get(f"/pilot/roi/{run_id}").json()
    inputs = {**before["inputs"], "value_per_outcome": before["inputs"]["value_per_outcome"] * 2}
    inputs.pop("entered_at")
    after = client.put(f"/pilot/roi/{run_id}", json=inputs).json()
    assert after["benefit"] == before["benefit"]
    assert after["gross_value"]["value"] == pytest.approx(2 * before["gross_value"]["value"])
    html = client.get(f"/pilot/roi/{run_id}", params={"format": "html"}).text
    assert "The value depends on these inputs" in html and "Demo data" in html


# --- in a browser --------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def server(
    manifest: DemoManifest, data_dir: Path, config_root: Path, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[str]:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    log = tmp_path_factory.mktemp("pilot-server") / "server.log"
    env = {
        **os.environ,
        "MARKETING_AI_CONFIG_DIR": str(config_root),
        "MARKETING_AI_DATA_DIR": str(data_dir),
        "MARKETING_AI_DEMO_MODE": "true",
        "PYTHONPATH": str(REPO_ROOT),
    }
    with log.open("wb") as sink:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "api.main:app",
                "--host",
                "127.0.0.1",
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
        base = f"http://127.0.0.1:{port}"
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
def page(server: str) -> Iterator[Any]:
    sync_api = pytest.importorskip("playwright.sync_api", reason="the journey needs a browser")
    playwright = sync_api.sync_playwright().start()
    try:
        try:
            browser = playwright.chromium.launch(headless=True)
        except sync_api.Error as error:
            pytest.skip(f"no usable chromium for playwright: {error}")
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        yield context.new_page()
        browser.close()
    finally:
        playwright.stop()


def test_the_tour_walks_the_six_screens(page: Any, server: str, manifest: DemoManifest) -> None:
    page.goto(f"{server}/ui/#/")
    tour = page.locator("#pe-tour")
    tour.wait_for(timeout=30_000)
    expected = [
        "#/",
        f"#/uc/{manifest.use_case_id}",
        f"#/uc/{manifest.use_case_id}/run/{manifest.train_run_id}",
        f"#/uc/{manifest.use_case_id}/output/{manifest.score_run_id}",
        f"#/campaign/{manifest.use_case_id}/{manifest.score_run_id}",
        "#/pilot",
    ]
    for index, hash_ in enumerate(expected):
        assert f"Step {index + 1} of 6" in (tour.text_content() or "")
        page.wait_for_function(f"window.location.hash === {json.dumps(hash_)}")
        tour.locator("[data-pe-tour='next']").click()
    assert page.locator("#pe-tour").count() == 0
    assert page.evaluate("window.localStorage.getItem('marketing-ai:pilot-tour-seen')") == "1"
    page.locator(".pe-demo").wait_for()
    assert "Demo Telecom" in page.locator(".pe-demo").inner_text()


def test_the_pilot_screen_lists_the_reports_and_shows_one_in_place(
    page: Any, server: str, manifest: DemoManifest
) -> None:
    page.goto(f"{server}/ui/#/pilot/kit")
    page.get_by_text("Data request kit").wait_for(timeout=30_000)
    page.goto(f"{server}/ui/#/pilot/view/readiness/{manifest.broken_dataset_id}")
    frame = page.frame_locator("iframe.pe-frame")
    frame.get_by_text("Not ready: The customer table repeats customer IDs").wait_for(timeout=30_000)


def test_every_setting_gets_its_explanation(page: Any, server: str, manifest: DemoManifest) -> None:
    # A fresh tab: the one above has visited a run, and Setup then reopens on that run's results.
    page = page.context.browser.new_page(viewport={"width": 1280, "height": 900})
    page.add_init_script("window.localStorage.setItem('marketing-ai:pilot-tour-seen', '1')")
    page.goto(f"{server}/ui/#/uc/{manifest.use_case_id}")
    page.locator("#f-adv > summary").click()
    page.evaluate("document.querySelectorAll('#f-adv details').forEach((d) => { d.open = true; })")
    assert page.locator("#f-adv [data-path]").count() > 0
    unexplained = page.evaluate(
        "[...document.querySelectorAll('#f-adv [data-path]')]"
        ".filter((e) => !(e.closest('.field') || e.parentElement).querySelector('.pe-q')).length"
    )
    assert unexplained == 0
    page.locator("#f-adv .pe-q:visible").first.click()
    popover = page.locator("#pe-pop")
    popover.wait_for(state="visible")
    assert len(popover.inner_text()) > 20
    page.close()


def test_a_warning_pill_painted_by_any_screen_gets_its_explanation(page: Any, server: str) -> None:
    """The validation lists belong to other workstreams; whatever paints a pill, the button follows."""
    page.goto(f"{server}/ui/#/pilot/kit")
    page.get_by_text("Data request kit").wait_for(timeout=30_000)
    page.evaluate(
        "document.querySelector('#app .screen').insertAdjacentHTML('beforeend',"
        ' \'<div class="vitem"><span class="pill warn">PII_DETECTED</span></div>'
        '<div class="vcoded" data-code="PII_DETECTED"><b>Personal details</b></div>\')'
    )
    # A screen that shows the plain title keeps the code in `data-code`; it is explained too.
    page.locator(".vcoded .pe-q").wait_for(timeout=10_000)
    button = page.locator(".vitem .pe-q")
    button.wait_for(timeout=10_000)
    button.click()
    popover = page.locator("#pe-pop")
    popover.wait_for(state="visible")
    assert "Personal details were found" in popover.inner_text()
    assert "What to do" in popover.inner_text()


def test_feedback_from_the_screen_is_recorded(page: Any, server: str, data_dir: Path) -> None:
    page.goto(f"{server}/ui/#/pilot")
    # "Send feedback" lives in the top bar's Help menu (v1 WP8): open the menu, then the entry.
    page.locator("#pb-bar [data-menu='tn-help']").click()
    page.locator("#pe-fb-btn").click()
    page.locator("#pe-fb textarea").fill("The value page is clear; call 9876543210")
    page.locator("#pe-fb button[type='submit']").click()
    page.get_by_text("Thank you, it was recorded.").wait_for(timeout=15_000)
    stored = list((data_dir / "pilot" / "feedback").glob("*.json"))
    assert stored
    assert all("9876543210" not in path.read_text() for path in stored)
