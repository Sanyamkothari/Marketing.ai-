"""WF6 shell, Goal and Plan screens: the mock data matches the contracts, the files follow the UI rules, and
the JavaScript draws and clicks through (in node, with a tiny DOM shim, so no jsdom is needed).

The mock files are what the UI shows when /v2 is not there, so they must be shaped exactly like the API's
answers (`model_dump(mode="json")` of the contracts); validating them here catches a drifted field before a
screen silently shows an em dash for it.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import TypeAdapter

from autonomy.contracts import Brief, ContentItem, Goal
from autonomy.contracts.outcomes import ProofSummary

ROOT: Final[Path] = Path(__file__).resolve().parents[3]
UI: Final[Path] = ROOT / "ui" / "v2"
MOCK: Final[Path] = UI / "mock"
NODE_TEST: Final[Path] = Path(__file__).resolve().parent / "shell_goal_plan.test.mjs"

MY_FILES: Final[tuple[str, ...]] = (
    "index.html",
    "app.js",
    "api.js",
    "styles.css",
    "components.js",
    "screens/connect.js",
    "screens/goal.js",
    "screens/plan.js",
)
GOALS = TypeAdapter(list[Goal])


def load(name: str) -> Any:
    return json.loads((MOCK / name).read_text(encoding="utf-8"))


def test_goals_mock_is_a_list_of_goals() -> None:
    goals = GOALS.validate_python(load("goals.json"))
    assert goals, "the switcher needs at least one goal"
    assert len({g.goal_id for g in goals}) == len(goals)


def test_opportunities_mock_is_a_list_of_draft_goals() -> None:
    opps = GOALS.validate_python(load("opportunities.json"))
    assert opps
    assert all(o.status == "draft" for o in opps)


def test_goal_detail_mock_matches_the_contracts() -> None:
    raw = load("goal_detail.json")
    assert set(raw) == {"goal", "brief", "content", "proof"}
    goal = Goal.model_validate(raw["goal"])
    brief = Brief.model_validate(raw["brief"])
    content = TypeAdapter(list[ContentItem]).validate_python(raw["content"])
    proof = ProofSummary.model_validate(raw["proof"]) if raw["proof"] is not None else None
    assert brief.goal_id == goal.goal_id
    assert all(c.goal_id == goal.goal_id for c in content)
    assert proof is None or proof.goal_id == goal.goal_id
    # every offer a message uses is one the brief allows
    allowed = {o.offer_id for o in brief.envelope.offers}
    assert {c.offer_id for c in content} <= allowed
    # the Plan screen's approve/reject needs something to review
    assert any(c.status == "draft" for c in content)
    # the detail's goal is the one the switcher lists
    assert goal.goal_id in {g.goal_id for g in GOALS.validate_python(load("goals.json"))}


def test_no_external_resources() -> None:
    """No CDN, no remote font, no remote script: the UI works offline and vendors nothing."""
    external = re.compile(
        r"""(?:\b(?:src|href)\s*=\s*["']?\s*(?:https?:)?//)"""
        r"""|(?:\bfrom\s+["'](?:https?:)?//)"""
        r"""|(?:\bimport\(\s*["'](?:https?:)?//)"""
        r"""|(?:url\(\s*["']?(?:https?:)?//)"""
        r"""|(?:@import)"""
    )
    for name in MY_FILES:
        text = (UI / name).read_text(encoding="utf-8")
        assert not external.search(text), f"{name} loads something from outside"


def test_index_loads_the_shell_as_a_module() -> None:
    html = (UI / "index.html").read_text(encoding="utf-8")
    assert '<script type="module" src="./app.js"></script>' in html
    assert '<link rel="stylesheet" href="./styles.css">' in html
    assert 'name="viewport"' in html and 'lang="en"' in html
    assert ">Autonomy<" in html
    assert 'id="pause-button"' in html and 'id="goal-select"' in html
    assert re.search(r'<label for="goal-select"', html), "the goal switcher has a label"


def test_light_and_dark_mode() -> None:
    css = (UI / "styles.css").read_text(encoding="utf-8")
    assert "@media (prefers-color-scheme: dark)" in css
    assert ":focus-visible" in css
    assert "@media (min-width: 720px)" in css


def test_every_screen_module_has_the_shared_signature() -> None:
    for name in ("connect", "goal", "plan"):
        text = (UI / "screens" / f"{name}.js").read_text(encoding="utf-8")
        assert re.search(r"export async function render\(root, ctx\)", text), name
    app = (UI / "app.js").read_text(encoding="utf-8")
    for name in ("connect", "goal", "plan", "live", "proof", "operator"):
        assert f'import("./screens/{name}.js")' in app, name


def test_api_covers_every_endpoint() -> None:
    api = (UI / "api.js").read_text(encoding="utf-8")
    for path in (
        '"/goals"',
        "`/goals/${enc(goalId)}`",
        "`/goals/${enc(goalId)}/approve`",
        "`/goals/${enc(goalId)}/pause`",
        "`/goals/${enc(goalId)}/resume`",
        "`/goals/${enc(goalId)}/content`",
        "`/content/${enc(contentId)}/status`",
        "`/decisions${",
        "`/customers/${enc(customerId)}`",
        "`/briefing${",
        "`/proof${",
        '"/health"',
        '"/alerts"',
        '"/opportunities"',
    ):
        assert path in api, path


def node_command() -> list[str]:
    """`node`, plus the flag that reads ui/v2's `.js` files as ES modules on a node older than 22.7.

    ui/v2 has no package.json (no build step, nothing to install), so node tells ES modules apart by their
    syntax; that is on by default from 22.7 and behind `--experimental-detect-module` from 20.10.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; the JS half of this test needs it")
    out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10, check=False)
    match = re.match(r"v(\d+)\.(\d+)", out.stdout.strip())
    version = (int(match.group(1)), int(match.group(2))) if match else (0, 0)
    if version < (20, 10):
        pytest.skip(f"node {out.stdout.strip()} is too old to load ES modules by syntax")
    return [node] if version >= (22, 7) else [node, "--experimental-detect-module"]


def test_the_javascript_draws_and_clicks_through() -> None:
    done = subprocess.run(
        [*node_command(), str(NODE_TEST)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stdout[-4000:] + done.stderr[-2000:]
