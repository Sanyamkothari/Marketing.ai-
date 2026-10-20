"""The Connect and Goal screens keep their promises (docs/v2/PROMISE_MAP.md rows 6-14).

The JavaScript half draws both screens in a tiny DOM shim (no jsdom) against the sample files and a fake
/v2, and checks that every number shown is one the data sent. This file runs it and checks the screens'
own stylesheets follow the UI rules.
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
NODE_TEST: Final[Path] = Path(__file__).resolve().parent / "connect_goal.test.mjs"
SCREEN_FILES: Final[tuple[str, ...]] = (
    "screens/connect.js",
    "screens/connect.css",
    "screens/goal.js",
    "screens/goal.css",
)


def node_command() -> list[str]:
    """`node`, plus the flag that reads ui/v2's `.js` files as ES modules on a node older than 22.7."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; the JS half of this test needs it")
    out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10, check=False)
    match = re.match(r"v(\d+)\.(\d+)", out.stdout.strip())
    version = (int(match.group(1)), int(match.group(2))) if match else (0, 0)
    if version < (20, 10):
        pytest.skip(f"node {out.stdout.strip()} is too old to load ES modules by syntax")
    return [node] if version >= (22, 7) else [node, "--experimental-detect-module"]


def test_screen_files_load_nothing_from_outside() -> None:
    external = re.compile(r"""(?:https?:)?//[a-z0-9.-]+\.[a-z]{2,}/|@import""", re.IGNORECASE)
    for name in SCREEN_FILES:
        text = (UI / name).read_text(encoding="utf-8")
        assert not external.search(text), f"{name} loads something from outside"


def test_screen_styles_use_the_shared_tokens() -> None:
    for name in ("screens/connect.css", "screens/goal.css"):
        css = (UI / name).read_text(encoding="utf-8")
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b", css), f"{name} hard-codes a colour; use the tokens"


def test_connect_and_goal_draw_and_click_through() -> None:
    done = subprocess.run(
        [*node_command(), str(NODE_TEST)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stdout[-4000:] + done.stderr[-2000:]
