"""Plan M M177 (DEC-1607): the "New campaign" wizard in jsdom, against REAL API answers.

The node suites in `production/ui/campaigns/` (sharing that directory's parent jsdom install) draw the wizard as
the browser does - `app.js` and every module of `ui/index.html` - and walk its five steps:

* `wizard.test.mjs` answers with the bodies the app below gave over a seeded store: a finished win-back scoring
  run (Phase 1's own shape, `seed_scored_run`), the campaign record `POST /campaigns` made from it, the
  planner's preview on that campaign at the list's own average likelihood, the list's summaries, the cost
  estimate and the goal catalogue. Guided setup is replaced by a stand-in registered through the same seam
  (`registerSetupMode`), so the test holds the door Guided setup approves through;
* `wizard_visible_text.test.mjs` draws each step against the answers recorded from the validated Hillstrom demo
  (`tests/fixtures/ui_demo/api.json`, M170) and fails on any glossary "never shown" word: the allow-list of
  every step is empty.

The test plan the wizard registers is checked against the real API here, before the node suites run: the
body is built by the wizard's own `planBody` from the real preview, registered with `POST /campaigns/{id}/plan`
(it must be accepted and frozen), and the jsdom suite then asserts the wizard sends exactly that body.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Final

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine.config import RunMode, resolve_config
from engine.contracts import RunState, RunStatus, StageStatus
from engine.pipeline import GROUP_LABELS, STAGE_TITLES, STATUS_FILENAME, stages_for
from engine.storage import LocalStorage, run_key
from tests.fixtures.node import skip_without_jsdom, skip_without_node
from tests.integration.uplift.test_measure_campaign import USE_CASE, seed_scored_run

pytestmark = pytest.mark.integration

NODE_DIR: Final[Path] = Path(__file__).resolve().parent / "production" / "ui"
TESTS_DIR: Final[Path] = NODE_DIR / "campaigns"
RULES: Final[Path] = Path(__file__).resolve().parents[2] / "ui" / "modules" / "campaigns" / "wizard_rules.js"
RUN_ID: Final[str] = "r_20261011_7a000001"
OPS_USE_CASE: Final[str] = "order-fulfillment"

PLAN_BODY_SCRIPT: Final[str] = """
import { planBody, baseRateOf } from %(rules)s;
let text = "";
for await (const chunk of process.stdin) text += chunk;
const f = JSON.parse(text);
process.stdout.write(JSON.stringify({
  base: baseRateOf(f.run.run, f.scoring),
  body: planBody({ campaign: f.view.campaign, preview: f.preview, uc: f.uc, outcomeColumn: f.uc.target.column }),
}));
"""


def _ok(response: Any, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json()


def _finish_run(storage: LocalStorage, run_id: str, config_root: Path) -> None:
    """What `POST /runs` and the job write beside the record that `seed_scored_run` leaves: the settings the list
    was made with (`run_config.json`, which the contact list is built from) and `status.json` with every stage
    done (`GET /runs/{id}`, which the wizard polls, answers the two together)."""
    storage.write_model(run_key(run_id, "run_config.json"), resolve_config(USE_CASE, {}, root=config_root))
    record = json.loads(storage.read_bytes(run_key(run_id, "run.json")))
    stages = tuple(
        StageStatus(
            key=key,
            title=STAGE_TITLES[key],
            group_label=GROUP_LABELS[(RunMode.SCORE, key)],
            state=RunState.DONE,
        )
        for key in stages_for(RunMode.SCORE)
    )
    status = RunStatus(
        run_id=run_id,
        mode=RunMode.SCORE,
        state=RunState.DONE,
        updated_at=record["finished_at"],
        stages=stages,
        current_stage=None,
        progress_pct=100,
    )
    storage.write_model(run_key(run_id, STATUS_FILENAME), status)


def _node_plan(node: str, inputs: dict[str, Any]) -> dict[str, Any]:
    done = subprocess.run(
        [node, "--input-type=module", "-e", PLAN_BODY_SCRIPT % {"rules": json.dumps(RULES.as_uri())}],
        input=json.dumps(inputs),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    result: dict[str, Any] = json.loads(done.stdout)
    return result


def write_fixtures(root: Path, config_root: Path, node: str) -> Path:
    out = root / "fixtures"
    out.mkdir(parents=True)
    data_dir = root / "data"
    data_dir.mkdir()
    storage = LocalStorage(data_dir)
    bodies: dict[str, Any] = {}
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as client:
        seed_scored_run(client, storage, run_id=RUN_ID)
        _finish_run(storage, RUN_ID, config_root)
        bodies["industries"] = _ok(client.get("/industries"))
        bodies["use_case"] = _ok(client.get(f"/use-cases/{USE_CASE}"))
        bodies["use_case_ops"] = _ok(client.get(f"/use-cases/{OPS_USE_CASE}"))
        bodies["models_empty"] = _ok(client.get(f"/models?use_case={USE_CASE}"))
        bodies["me"] = _ok(client.get("/auth/me"))
        bodies["run"] = _ok(client.get(f"/runs/{RUN_ID}"))
        bodies["scoring_summary"] = _ok(client.get(f"/runs/{RUN_ID}/artefacts/scoring_summary.json"))
        bodies["treat_list_summary"] = _ok(client.get(f"/runs/{RUN_ID}/artefacts/treat_list_summary.json"))
        bodies["campaigns_of_run_empty"] = _ok(client.get(f"/campaigns?run_id={RUN_ID}"))
        bodies["campaign_created"] = _ok(client.post("/campaigns", json={"run_id": RUN_ID}), 201)
        campaign_id = bodies["campaign_created"]["campaign"]["campaign_id"]
        bodies["campaigns_of_run"] = _ok(client.get(f"/campaigns?run_id={RUN_ID}"))
        bodies["campaign_draft"] = _ok(client.get(f"/campaigns/{campaign_id}"))
        bodies["cost_score"] = _ok(client.get(f"/use-cases/{USE_CASE}/cost-estimate?mode=score"))
        planned = _node_plan(
            node,
            {
                "run": bodies["run"],
                "scoring": bodies["scoring_summary"],
                "view": bodies["campaign_draft"],
                "uc": bodies["use_case"],
                "preview": None,
            },
        )
        base = planned["base"]
        assert base is not None, "the seeded list is a yes/no one, so it has an average likelihood"
        bodies["preview"] = _ok(
            client.get(f"/campaigns/{campaign_id}/plan-preview", params={"base_rate": base})
        )
        planned = _node_plan(
            node,
            {
                "run": bodies["run"],
                "scoring": bodies["scoring_summary"],
                "view": bodies["campaign_draft"],
                "uc": bodies["use_case"],
                "preview": bodies["preview"],
            },
        )
        bodies["plan_body"] = planned["body"]
        # The plan the wizard would register is accepted and frozen by the real API before any download.
        bodies["plan_registered"] = _ok(
            client.post(f"/campaigns/{campaign_id}/plan", json=planned["body"]), 201
        )
        bodies["campaign_planned"] = _ok(client.get(f"/campaigns/{campaign_id}"))
    for name, body in bodies.items():
        (out / f"{name}.json").write_text(json.dumps(body, indent=1), encoding="utf-8")
    return out


@pytest.fixture(scope="module")
def fixtures(config_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    node = skip_without_node()
    return write_fixtures(tmp_path_factory.mktemp("wizard-ui"), config_root, node)


def _read(out: Path, name: str) -> Any:
    return json.loads((out / f"{name}.json").read_text(encoding="utf-8"))


def test_the_plan_the_wizard_registers_is_the_planner_s_and_is_frozen(fixtures: Path) -> None:
    """The wizard's numbers are the planner's: the smallest change at the campaign's own split and the rate it
    was worked out on; the server accepts the body and freezes it (a hash), with the power the plan asked for.
    """
    preview = _read(fixtures, "preview")
    body = _read(fixtures, "plan_body")
    plan = _read(fixtures, "plan_registered")
    point = preview["points"][preview["current_index"]]
    assert point["mde_pp"] is not None and body["mde_pp"] == point["mde_pp"]
    assert body["base_rate"] == preview["base_rate"] == _read(fixtures, "scoring_summary")["score_mean"]
    assert preview["base_rate_source"] == "request"
    assert plan["plan_hash"] and plan["version"] == 1
    assert plan["mde_pp"] == point["mde_pp"] and plan["base_rate"] == preview["base_rate"]
    assert plan["n_holdout"] == point["n_control"] == preview["n_holdout"]
    assert plan["achieved_power"] is not None and plan["achieved_power"] >= 0.79
    assert plan["outcome_column"] == _read(fixtures, "use_case")["target"]["column"]
    assert _read(fixtures, "campaign_planned")["plan"]["plan_hash"] == plan["plan_hash"]
    assert _read(fixtures, "campaign_draft")["plan"] is None


def test_the_fixtures_are_the_world_the_wizard_is_tested_in(fixtures: Path) -> None:
    assert _read(fixtures, "use_case")["config"]["actions"]["contacts_customers"] is True
    assert _read(fixtures, "use_case_ops")["config"]["actions"]["contacts_customers"] is False
    assert _read(fixtures, "models_empty") == {"versions": []}
    assert _read(fixtures, "campaigns_of_run_empty") == {"campaigns": []}
    assert _read(fixtures, "treat_list_summary")["treat_rows"] > 0


def _run_node(fixtures: Path, *names: str) -> None:
    node = skip_without_jsdom(NODE_DIR)  # REQUIRE_JSDOM=1 (CI) turns a skip into a failure
    tests = [str(TESTS_DIR / name) for name in names]
    result = subprocess.run(
        [node, "--test", *tests],
        cwd=NODE_DIR,
        env={**os.environ, "WZ_FIXTURES": str(fixtures)},
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-8000:] + result.stderr[-3000:]
    assert "# fail 0" in result.stdout


def test_the_wizard_steps_in_jsdom(fixtures: Path) -> None:
    _run_node(fixtures, "wizard.test.mjs")


def test_the_wizard_shows_no_engine_word_on_the_demo() -> None:
    node = skip_without_jsdom(NODE_DIR)
    result = subprocess.run(
        [node, "--test", str(TESTS_DIR / "wizard_visible_text.test.mjs")],
        cwd=NODE_DIR,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-8000:] + result.stderr[-3000:]
    assert "# fail 0" in result.stdout
