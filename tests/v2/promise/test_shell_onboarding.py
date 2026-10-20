"""The shell's promise parts and the Getting started screen, drawn and clicked in node with a tiny DOM.

Checks the autonomy badge, the "Paused itself" badge, the inbox count (the same set Live's Inbox lists), the one
big Pause button that pauses every running goal, the Shadow note, and that each Getting started step's state
and numbers come from the API (or the sample files), never made up. The JavaScript half is
`shell_onboarding.test.mjs`; it skips without node 20.10 or newer.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Final

import pytest

ROOT: Final[Path] = Path(__file__).resolve().parents[3]
UI: Final[Path] = ROOT / "ui" / "v2"
NODE_TEST: Final[Path] = Path(__file__).resolve().parent / "shell_onboarding.test.mjs"


def node_command() -> list[str]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; the JS half of this test needs it")
    out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10, check=False)
    match = re.match(r"v(\d+)\.(\d+)", out.stdout.strip())
    version = (int(match.group(1)), int(match.group(2))) if match else (0, 0)
    if version < (20, 10):
        pytest.skip(f"node {out.stdout.strip()} is too old to load ES modules by syntax")
    return [node] if version >= (22, 7) else [node, "--experimental-detect-module"]


def test_index_has_the_shell_promise_elements() -> None:
    html = (UI / "index.html").read_text(encoding="utf-8")
    for element_id in ("autonomy-badge", "selfpause-badge", "goal-note", "start-link", "pause-scope"):
        assert f'id="{element_id}"' in html, element_id
    assert '<link rel="stylesheet" href="./screens/shell.css">' in html
    assert '<link rel="stylesheet" href="./screens/onboarding.css">' in html


def test_onboarding_labels_product_facts_as_product_facts() -> None:
    """The 3-10% and 'not yet run with a real business' lines are about Autonomy, never about the shop."""
    js = (UI / "screens" / "onboarding.js").read_text(encoding="utf-8")
    assert "Honest status" in js
    assert "not yet run with a real business" in js
    assert "general guidance about the field, not a number about your shop" in js


def test_the_javascript_draws_and_clicks_through() -> None:
    done = subprocess.run(
        [*node_command(), str(NODE_TEST)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert done.returncode == 0, done.stdout[-4000:] + done.stderr[-2000:]
