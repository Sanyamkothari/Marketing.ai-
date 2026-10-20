"""The Live screen's promises (PITCH.md screen 4), drawn and clicked in node with a tiny DOM.

Checks the pitch's daily sentence ("Yesterday I contacted N customers, left M alone, and here's why") and the
left-alone reasons, the money with its range or "not sure yet", the spend against the budget, the delivery
labels (Would send / Waiting for your approval / Scheduled / Delivered), the person's first name, the batch
in words, the self-pause banner, and the customer panel in words. Every number comes from the API client's
mock layer, which serves the sample files in the /v2 shapes. The JavaScript half is `live.test.mjs`; it skips
without node 20.10 or newer.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Final

import pytest

ROOT: Final[Path] = Path(__file__).resolve().parents[3]
LIVE_JS: Final[Path] = ROOT / "ui" / "v2" / "screens" / "live.js"
NODE_TEST: Final[Path] = Path(__file__).resolve().parent / "live.test.mjs"


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


def test_live_never_words_the_product_claims_as_the_shops_numbers() -> None:
    """The pitch's product numbers (58%, 3-10%) are not Live's to show; Live only shows the API's numbers."""
    js = LIVE_JS.read_text(encoding="utf-8")
    assert "58%" not in js
    assert "3-10%" not in js and "3–10%" not in js


def test_the_javascript_draws_and_clicks_through() -> None:
    done = subprocess.run(
        [*node_command(), "--test", str(NODE_TEST)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert done.returncode == 0, done.stdout[-4000:] + done.stderr[-2000:]
