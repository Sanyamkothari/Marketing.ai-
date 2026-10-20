"""Runs the node tests of the Live, Proof and Operator screens (live_proof_operator.test.mjs).

The screens are plain ES modules with no build step, so node can import them directly; the test file
brings its own tiny fake DOM, so no jsdom or npm install is needed. Without node the test skips with
a reason (or fails under REQUIRE_JSDOM=1, as everywhere else in the suite).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from tests.fixtures.node import skip_without_node

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SCREENS = ROOT / "ui" / "v2" / "screens"


def test_screen_modules_parse() -> None:
    node = skip_without_node()
    for name in ("live.js", "proof.js", "operator.js"):
        done = subprocess.run(
            [node, "--check", str(SCREENS / name)], capture_output=True, text=True, timeout=30
        )
        assert done.returncode == 0, f"{name}:\n{done.stderr}"


def test_live_proof_operator_screens() -> None:
    node = skip_without_node()
    done = subprocess.run(
        [node, "--test", str(HERE / "live_proof_operator.test.mjs")],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=ROOT,
    )
    assert done.returncode == 0, done.stdout[-6000:] + done.stderr[-2000:]
