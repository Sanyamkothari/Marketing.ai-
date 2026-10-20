"""The Proof screen's promises (PITCH.md screen 5), drawn in node with a tiny DOM.

Checks the verdict before any number (Real extra profit / Not sure yet / Losing money so far / Too early to
tell), the money against the people we never contact with its range, the verdict against the simple rule,
"Ready for Autopilot" only when the low end of the range is above zero, the Shadow forecast labelled as a
forecast, the 3-10% guidance only as a labelled product fact, the goal's currency, and the chart's zero line.
Every number comes from the mock files, which have the API's shapes. The JavaScript half is
`proof.test.mjs`; it skips without node 20.10 or newer.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Final

import pytest

ROOT: Final[Path] = Path(__file__).resolve().parents[3]
PROOF_JS: Final[Path] = ROOT / "ui" / "v2" / "screens" / "proof.js"
NODE_TEST: Final[Path] = Path(__file__).resolve().parent / "proof.test.mjs"


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


def test_proof_never_shows_the_catalogue_claim_or_a_price() -> None:
    """58% belongs to the Plan's product fact; there are no pricing terms, so no price wording either."""
    js = PROOF_JS.read_text(encoding="utf-8")
    assert "58%" not in js
    assert "billed" not in js.lower()


def test_the_javascript_draws_every_verdict() -> None:
    done = subprocess.run(
        [*node_command(), "--test", str(NODE_TEST)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert done.returncode == 0, done.stdout[-4000:] + done.stderr[-2000:]
