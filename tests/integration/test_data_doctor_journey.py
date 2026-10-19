"""Plan O M247 (DEC-1817): one flow from the Data page to the campaign, on ONE Data Doctor case, in a real browser.

The real app runs under uvicorn with `data_doctor.unified` on and the campaigns layout, on an empty store with no AI
service connected. Chromium opens the Data page's "Check my data" (no goal), and a marketer goes:

    choose the file -> [1] answer the one question (which column names each customer?)
                    -> [2] Choose this goal ("Find customers likely to buy")
                    -> the "New campaign" wizard's Customers step, on the SAME case, nothing asked again
                    -> [3] "Approve and start" (the sentence shown is the one recorded, naming the goal by its name)
                    -> the consent change ("who agreed to be contacted") was never agreed: it is a checkbox in the
                       card, unticked, and says it is left out unless agreed - the campaign starts on the same click

The screen is the chat (`ui/modules/datadoctor/chat.js`): the cards of `view.conversation`, one approval card.
The clicks from the file to the started campaign are pinned (`CLICKS_PINNED`): a change that adds one fails here.
CHANGED ON PURPOSE (agent-style Data Doctor plan): the earlier screen made the person approve, and then choose to
continue without the change left out, as a second click (`CHOICES_AFTER_APPROVE`); the one card now carries that choice
- the change is listed in the card as a checkbox, unticked and "Left out unless you agree", right above the button - so
Approve and the start are one click. The server's rules are untouched: the card sends the exact sentence, and the own
yes of a change only when its box is ticked. The test then
checks through the API that the wizard used the same case, that the question was answered once (the timeline holds one
answer and the wizard asked nothing), that the approval recorded the exact sentence shown, and that the run started
on the case's file with its ID and outcome columns; with no AI service the box says so and links to Settings › AI service.
Screenshots of every screen are taken at desktop and phone sizes (into `$LOOK_AFTER_DIR` when it is set), and the
phone page never scrolls sideways.

Skips without playwright or a usable Chromium, like the other browser journeys.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

from tests.fixtures.make_data import CLEAN, GenerationSpec, generate
from tests.integration.test_acceptance import HOST, TEST_BUDGET, free_port, wait_for_health

pytestmark = [pytest.mark.slow, pytest.mark.integration]

sync_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright is not installed; the journey needs a browser"
)

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
GOAL: Final[str] = "targeted-advertisement"
ROWS: Final[int] = 4000
SEED: Final[int] = 20_261_011
CLICKS_PINNED: Final[int] = 3
"""From the chosen file to the started campaign: the one answer, the goal, Approve and start. A change that adds a click
fails here; one that removes one updates the pin."""
GOAL_NAME: Final[str] = "Find customers likely to buy"
WAIT_MS: Final[int] = 180_000
PHONE: Final[dict[str, int]] = {"width": 390, "height": 844}
DESKTOP: Final[dict[str, int]] = {"width": 1440, "height": 900}


@pytest.fixture(scope="module")
def workdir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("data-doctor-journey")


@pytest.fixture(scope="module")
def config_root(workdir: Path) -> Path:
    root = workdir / "configs"
    shutil.copytree(REPO_ROOT / "configs", root)
    engine = root / "engine.yaml"
    text = engine.read_text(encoding="utf-8")
    switch = "    unified: false"
    assert text.count(switch) == 1
    engine.write_text(text.replace(switch, "    unified: true"), encoding="utf-8")
    path = root / "use_cases" / f"{GOAL.replace('-', '_')}.yaml"
    assert "model_search" not in yaml.safe_load(path.read_text(encoding="utf-8"))
    path.write_text(path.read_text(encoding="utf-8") + TEST_BUDGET, encoding="utf-8")
    return root


@pytest.fixture(scope="module")
def customers(workdir: Path, config_root: Path) -> Path:
    """One row per customer, with a second column that holds a different value on every row: which one names the
    customer is the one thing Data Doctor cannot tell from the values, so it asks."""
    frame = generate(GenerationSpec(GOAL, rows=ROWS, seed=SEED, variant=CLEAN, config_root=config_root))
    frame.insert(1, "member_code", [f"M{700000 + 3 * i}" for i in range(len(frame))])
    path = workdir / "customers.csv"
    frame.to_csv(path, index=False)
    return path


@pytest.fixture(scope="module")
def server(workdir: Path, config_root: Path) -> Iterator[str]:
    data_dir = workdir / "data"
    data_dir.mkdir()
    port = free_port()
    base_url = f"http://{HOST}:{port}"
    log = workdir / "server.log"
    # No AI service: the test suite's stand-in AI (`MARKETING_AI_ALLOW_FAKE_AI`, tests/conftest.py) is left out, so
    # the product has none connected, as a new deployment has none.
    env = {
        **{k: v for k, v in os.environ.items() if k != "MARKETING_AI_ALLOW_FAKE_AI"},
        "MARKETING_AI_CONFIG_DIR": str(config_root),
        "MARKETING_AI_DATA_DIR": str(data_dir),
        "MARKETING_AI_UI_LAYOUT": "campaigns",
        "PYTHONPATH": str(REPO_ROOT),
        "PYTHONUNBUFFERED": "1",
    }
    command = [sys.executable, "-m", "uvicorn", "api.main:app", "--host", HOST, "--port", str(port)]
    with log.open("wb") as sink:
        process = subprocess.Popen(
            [*command, "--log-level", "warning"], cwd=workdir, env=env, stdout=sink, stderr=subprocess.STDOUT
        )
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
        return json.loads(response.read())


def _shots(workdir: Path) -> Path:
    wanted = os.environ.get("LOOK_AFTER_DIR")
    out = Path(wanted) if wanted else workdir / "shots"
    out.mkdir(parents=True, exist_ok=True)
    return out


def test_data_page_to_the_campaign_on_one_case_nothing_asked_twice(
    server: str, customers: Path, workdir: Path
) -> None:
    shots = _shots(workdir)
    with sync_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(executable_path="/opt/pw-browsers/chromium")
        except sync_api.Error as error:  # pragma: no cover - a machine without the browser
            pytest.skip(f"no usable chromium for playwright: {error}")
        page = browser.new_page(viewport=DESKTOP)
        page.set_default_timeout(120_000)  # the machine's CPUs are shared: a slow answer is not a failure
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        # The Google font the sandbox cannot fetch is the one allowance (as the UI sweep's): the sandbox, not the app.
        page.on(
            "console",
            lambda m: (
                errors.append(m.text)
                if m.type == "error" and "fonts.g" not in str((m.location or {}).get("url", ""))
                else None
            ),
        )
        clicks: list[str] = []

        def click(selector: str, *, timeout: int = 60_000) -> None:
            """One click a person makes after the file is chosen."""
            locator = page.locator(selector)
            locator.wait_for(state="visible", timeout=timeout)
            clicks.append(selector)
            locator.click()

        def shoot(name: str) -> None:
            page.wait_for_timeout(300)
            page.screenshot(path=str(shots / f"{name}_desktop.png"), full_page=True)

        try:
            page.goto(f"{server}/ui/#/data/check")
            page.locator('[data-dds-place="data"] [data-dds-start]').wait_for(timeout=60_000)
            shoot("01_data_start")
            page.locator("#dds-file").set_input_files(str(customers))
            key = page.locator('[data-dds-code="KEY_AMBIGUOUS"]')
            key.wait_for(timeout=WAIT_MS)
            chat = page.locator("[data-dds-composer]")
            chat.locator("a[data-ai-settings-link]").wait_for(timeout=30_000)
            assert "Chat needs the AI service" in chat.inner_text()
            assert chat.locator("a[data-ai-settings-link]").get_attribute("href") == "#/settings/ai"
            assert chat.locator('a[href^="#/connections"]').count() == 0, "no AI: Settings, never Connections"
            shoot("02_data_question")

            click('[data-dds-code="KEY_AMBIGUOUS"] [data-dds-answer="c0"]')  # 1
            page.locator(f'[data-dds-goal="{GOAL}"]:not([disabled])').wait_for(timeout=WAIT_MS)
            shoot("03_data_goals")
            case_id = page.locator('[data-dds-place="data"]').get_attribute("data-dds-case")
            assert case_id

            click(f'[data-dds-goal="{GOAL}"]')  # 2
            page.wait_for_function(f"location.hash === '#/campaigns/new/customers/{GOAL}'", timeout=60_000)
            wizard = page.locator('[data-cw-customers] [data-cw-guided-main] [data-dds-place="wizard"]')
            wizard.locator("[data-dds-verdict]").wait_for(timeout=WAIT_MS)
            assert wizard.get_attribute("data-dds-case") == case_id, "the wizard continues on the same case"
            page.locator("[data-dds-approve-start]:not([disabled])").wait_for(timeout=WAIT_MS)
            answered = wizard.locator('[data-dds-code="KEY_AMBIGUOUS"] [data-dds-answered]')
            assert answered.count() == 1, "the Data page's answer stands"
            assert wizard.locator(".dds-q.is-open").filter(has_text="Needed to go on").count() == 0
            sentence = page.locator("[data-dds-approve-sentence]").inner_text()
            assert f"for the goal “{GOAL_NAME}”" in sentence and GOAL not in sentence, sentence
            left_out = wizard.locator("[data-dds-confirm-row]")
            assert left_out.count() == 1, "the consent change waiting for its own yes is a box in the card"
            assert "who agreed to be contacted" in left_out.inner_text()
            assert "Left out unless you agree" in left_out.inner_text()
            assert left_out.locator("input").is_checked() is False, "never ticked for the person"
            assert (
                wizard.locator("[data-dds-fit]").count() == 1
            ), "the goal was chosen first: does the data fit it"
            shoot("04_wizard_case")

            click(
                "[data-dds-approve-start]"
            )  # 3: the own yes (none ticked), Approve with the sentence, the start
            assert len(clicks) == CLICKS_PINNED, clicks
            page.wait_for_function("location.hash.startsWith('#/campaigns/run/')", timeout=WAIT_MS)
            campaign_run_id = page.evaluate("location.hash").rsplit("/", 1)[-1]
            run_id = _get(server, f"/campaign-runs/{campaign_run_id}")["record"]["train_run_id"]
            shoot("06_campaign_run_started")

            phone = browser.new_page(viewport=PHONE)
            phone.on("pageerror", lambda error: errors.append(str(error)))
            for name, hash_, ready in (
                ("02_data_case", "#/data/check", '[data-dds-place="data"] [data-dds-verdict]'),
                (
                    "04_wizard_case",
                    f"#/campaigns/new/customers/{GOAL}",
                    '[data-dds-place="wizard"] [data-dds-verdict]',
                ),
            ):
                phone.goto(f"{server}/ui/{hash_}")
                phone.locator(ready).wait_for(timeout=60_000)
                phone.wait_for_timeout(400)
                phone.screenshot(path=str(shots / f"{name}_phone.png"), full_page=True)
                width = phone.evaluate("document.scrollingElement.scrollWidth")
                assert width <= PHONE["width"], f"{name}: the phone page scrolls sideways ({width}px)"
        finally:
            page.screenshot(path=str(workdir / "journey_last.png"), full_page=True)
            browser.close()

    assert len(clicks) == CLICKS_PINNED, clicks
    case = _get(server, f"/cases/{case_id}")
    assert [g["goal_id"] for g in case["goals"] if g["active"]] == [GOAL]
    key_questions = [q for q in case["questions"] if q["code"] == "KEY_AMBIGUOUS"]
    assert len(key_questions) == 1 and key_questions[0]["answer"] == "c0"
    events = _get(server, f"/cases/{case_id}/timeline")["events"]
    answered = [e for e in events if e["kind"] == "question.answered"]
    assert len(answered) == 1, "asked and answered once, on the Data page"
    key_id = key_questions[0]["question_id"]
    asked = [e for e in events if e["kind"] == "question.asked" and e["item_id"] == key_id]
    assert len(asked) == 1, "the question the person answered was asked once, never again in the wizard"
    ids = [e["item_id"] for e in events if e["kind"] == "question.asked"]
    assert len(ids) == len(set(ids)), "no question is asked twice"
    approval = next(a for a in case["approvals"] if not a["approve_class"])
    assert approval["text_shown"] == sentence, "the sentence shown is the one recorded"
    run = _get(server, f"/runs/{run_id}")["run"]
    outcome = next(i for i in case["items"] if i["kind"] == "outcome_column" and i["state"] == "accepted")
    assert run["upload_id"] == outcome["payload"]["upload_id"]
    assert run["primary_key"] == "customer_id" and run["target"] == outcome["payload"]["column"]
    assert errors == [], errors
