"""The Approvals screen (Plan D M54) in jsdom, against REAL API responses.

As in `test_production_ops_ui_js.py`, what the node test replays is what the app answered: the world
of `test_approvals.World` (a champion, a challenger waiting, its training run started by `trainer`),
read by the Approver and by the trainer, and the Approver's real approval. The node test then drives
the screen: the Approver sees the head-to-head with the differences marked and approves with a
reason; the trainer sees the same challenger with the controls replaced by the server's sentence.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Final

import pytest

from tests.integration.production.test_approvals import CHALLENGER, World

pytestmark = pytest.mark.integration

NODE_DIR: Final[Path] = Path(__file__).resolve().parent / "ui"
TESTS_DIR: Final[Path] = NODE_DIR / "approvals"


def _ok(response: Any) -> Any:
    assert response.status_code == 200, response.text
    return response.json()


def write_approval_fixtures(root: Path) -> Path:
    out = root / "fixtures"
    out.mkdir()
    world = World(root / "world")
    bodies = {
        "me_approver": _ok(world.client.get("/auth/me", headers=world.approver)),
        "me_trainer": _ok(world.client.get("/auth/me", headers=world.trainer)),
        "approvals_approver": world.approvals(world.approver),
        "approvals_trainer": world.approvals(world.trainer),
    }
    approved = world.client.post(
        f"/models/{CHALLENGER}/approve",
        json={"approved_by": "approver", "reason": "beats the champion on ROC-AUC"},
        headers=world.approver,
    )
    bodies["approve_ok"] = _ok(approved)
    bodies["approvals_after"] = world.approvals(world.approver)
    refused = world.client.post(
        f"/models/{CHALLENGER}/approve",
        json={"approved_by": "trainer", "reason": "mine"},
        headers=world.trainer,
    )
    assert refused.status_code in (403, 409), refused.text
    for name, body in bodies.items():
        (out / f"{name}.json").write_text(json.dumps(body, indent=1), encoding="utf-8")
    return out


def test_the_fixtures_are_what_the_screen_needs(tmp_path: Path) -> None:
    out = write_approval_fixtures(tmp_path)
    read = lambda name: json.loads((out / f"{name}.json").read_text(encoding="utf-8"))  # noqa: E731
    assert read("approvals_approver")["items"][0]["can_decide"] is True
    assert read("approvals_trainer")["items"][0]["can_decide"] is False
    assert read("approvals_after")["items"] == []
    assert read("approve_ok")["is_champion"] is True


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_approvals_screen_in_jsdom(tmp_path: Path) -> None:
    if not (NODE_DIR / "node_modules" / "jsdom").is_dir():
        pytest.skip(f"jsdom is not installed: cd {NODE_DIR} && npm install --no-audit --no-fund")
    out = write_approval_fixtures(tmp_path)
    tests = sorted(str(p) for p in TESTS_DIR.glob("*.test.mjs"))
    assert tests, "no jsdom test was found"
    result = subprocess.run(
        ["node", "--test", *tests],
        cwd=NODE_DIR,
        env={**os.environ, "PB_FIXTURES": str(out)},
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-8000:] + result.stderr[-3000:]
