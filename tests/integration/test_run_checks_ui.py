""" "Things we checked" on a training run's Model page (Plan L follow-up, DEC-1599), rendered under node.

The panel (`ui/modules/checks/panel.js`) draws the server's own answer: the view below is what
`engine.run_checks.things_checked` builds from a training run's artefacts, so the test checks the page
shows the too-good-to-be-true alarm with the column it names, one line per check, and "Not run" for a
check that did not run. `pages.js` draws a module's panels at the end of the Model page, as it already
does on the Data and Output pages. The module registers itself from `ui/index.html`'s PLAN-L block.
Node is not a Python dependency, so the rendering half skips cleanly where it is absent.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Final

import pytest

from api.main import UI_DIR
from engine.run_checks import things_checked
from engine.storage import LocalStorage
from tests.unit.doctor.test_result_alarm import RUN_ID, record, write_training_run

pytestmark = pytest.mark.integration

NODE: Final[str | None] = shutil.which("node")

SCRIPT: Final[str] = """
const panel = await import(%(panel)s);
const pages = await import(%(pages)s);
let text = "";
for await (const chunk of process.stdin) text += chunk;
const { view, uc, run, art } = JSON.parse(text);
process.stdout.write(JSON.stringify({
  panel: panel.checksPanelHtml(view),
  empty: panel.checksPanelHtml({ run_id: run.run_id, checks: [], warnings: 0 }),
  page: pages.renderPage("model", uc, run, art, "", { panelsHtml: "<div data-module-panel></div>" }),
}));
"""


def test_the_module_is_loaded_from_the_plan_l_block() -> None:
    index = (UI_DIR / "index.html").read_text(encoding="utf-8")
    block = index[index.index("PLAN-L (data doctor)") : index.index("END PLAN-L")]
    assert './modules/checks/index.js"' in block
    module = (UI_DIR / "modules" / "checks" / "index.js").read_text(encoding="utf-8")
    assert "registerPagePanel" in module and "/checks`" in module
    assert 'kind === "model"' in module and 'run.mode === "train"' in module


@pytest.fixture(scope="module")
def rendered(tmp_path_factory: pytest.TempPathFactory, config_root: Path) -> dict[str, str]:
    if NODE is None:
        pytest.skip("node is not installed; the static half of this module still runs")
    data = tmp_path_factory.mktemp("checks")
    storage = LocalStorage(data)
    write_training_run(storage, config_root)
    view = things_checked(storage, record(), config_root)
    payload: dict[str, Any] = {
        "view": view.model_dump(mode="json"),
        "uc": {
            "id": "targeted-advertisement",
            "name": "Targeted",
            "marker": "p",
            "stars": "★",
            "pages": {"model": "Model"},
        },
        "run": {"run_id": RUN_ID, "mode": "train", "created_at": None},
        "art": {"best_model.json": {"display_name": "LightGBM"}},
    }
    script = SCRIPT % {
        "panel": json.dumps((UI_DIR / "modules" / "checks" / "panel.js").as_uri()),
        "pages": json.dumps((UI_DIR / "pages.js").as_uri()),
    }
    done = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    result: dict[str, str] = json.loads(done.stdout)
    return result


def test_the_alarm_is_drawn_first_and_names_the_column(rendered: dict[str, str]) -> None:
    html = rendered["panel"]
    assert "Things we checked" in html
    alarm = re.search(r"<p role=\"alert\" data-result-suspicious[^>]*>(.*?)</p>", html)
    assert alarm is not None
    assert "Too good to be true?" in alarm.group(1) and "cancel_date" in alarm.group(1)
    assert html.index("data-result-suspicious") < html.index('data-check="split"')


def test_each_check_is_one_line_and_a_check_that_did_not_run_says_so(rendered: dict[str, str]) -> None:
    html = rendered["panel"]
    statuses = dict(re.findall(r'data-check="([a-z_]+)" data-status="([a-z_]+)"', html))
    assert statuses == {
        "too_good": "warning",
        "before_training": "info",
        "leak_columns": "warning",
        "split": "passed",
        "baseline": "passed",
        "fairness": "not_run",
    }
    fairness = html[html.index('data-check="fairness"') :]
    assert ">Not checked<" in fairness[:200]  # Plan M M172: "run" is not a marketer's word
    assert "Warnings block nothing." in html


def test_nothing_to_show_draws_nothing(rendered: dict[str, str]) -> None:
    assert rendered["empty"] == ""


def test_the_model_page_draws_a_module_panel_at_its_end(rendered: dict[str, str]) -> None:
    assert "<div data-module-panel></div>" in rendered["page"]
