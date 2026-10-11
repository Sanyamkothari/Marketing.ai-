"""What the planner's screen draws, from the real `POST /agentic/plans` answer (Plan K M139; Plan H's rule: no
new top-bar item - the button sits under Results' warning cards).

`ui/modules/agentic/view.js` is pure functions of the server's answers, so plain node runs it here against what
the API actually answered for the world's real `PLAN_UNDERPOWERED` card: the drafted plan shows every sentence
the claim verifier rendered, the policy's checks, and no number the server did not send; a card no campaign
answers has no "Draft a plan" button.
"""

from __future__ import annotations

import html
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.agentic.scenarios.planner_world import PlannerWorld
from tests.fixtures.node import skip_without_node

pytestmark = pytest.mark.integration

VIEW = Path(__file__).resolve().parents[2] / "ui" / "modules" / "agentic" / "view.js"
SCRIPT = """
import fs from "node:fs";
const view = await import(process.argv[1]);
const answer = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const out = {
  plan: view.planHtml(answer, { canApprove: true }),
  waiting: view.planHtml(answer, { canApprove: false }),
  underpowered: view.draftButtonHtml({ code: "PLAN_UNDERPOWERED", campaign_id: "c_1" }),
  drift: view.draftButtonHtml({ code: "DRIFT_DRIFTED", use_case_id: "win-back-campaign" }),
  request: view.cardRequest("PLAN_UNDERPOWERED", "c_1", null),
};
process.stdout.write(JSON.stringify(out));
"""


def _render(answer: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
    node = skip_without_node()
    data = tmp_path / "answer.json"
    data.write_text(json.dumps(answer), encoding="utf-8")
    done = subprocess.run(
        [node, "--input-type=module", "-e", SCRIPT, VIEW.as_uri(), str(data)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return dict(json.loads(done.stdout))


def test_the_drafted_plan_is_drawn_from_the_servers_answer_only(
    planner_world: PlannerWorld, tmp_path: Path
) -> None:
    pw = planner_world
    with pw.world.client() as client:
        response = client.post(
            "/agentic/plans",
            json={"card": {"code": "PLAN_UNDERPOWERED", "campaign_id": pw.world.planned_campaign}},
        )
        assert response.status_code == 201, response.text
    answer = response.json()
    shown = _render(answer, tmp_path)
    page = html.unescape(shown["plan"])
    proposal = answer["proposal"]
    for sentence in proposal["narrative"]["sentences"]:
        assert sentence["text"] in page
    assert f'data-plan-approve="{proposal["proposal_id"]}"' in shown["plan"]
    assert "Waiting for an Approver." in shown["waiting"] and "data-plan-approve" not in shown["waiting"]
    assert page.count("data-rule=") == len(proposal["policy"])
    served = json.dumps(answer)
    for number in re.findall(r"\d+(?:\.\d+)?", re.sub(r"<[^>]+>", " ", page)):
        assert number in served, f"{number} is not in the server's answer"
    assert "Draft a plan" in shown["underpowered"] and shown["drift"] == ""
    assert shown["request"] == {"card": {"code": "PLAN_UNDERPOWERED", "campaign_id": "c_1"}}
