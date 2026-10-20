"""The Plan screen keeps its promises (docs/v2/PROMISE_MAP.md rows 15-26).

The JavaScript half draws the screen in a tiny DOM shim (no jsdom) against the sample files and a fake /v2,
and checks that every number shown is one the data sent, and that the product test (58% more purchases than
best-sellers) appears only as a labelled fact about Autonomy. This file runs it and checks the screen's own
stylesheet follows the UI rules.
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
NODE_TEST: Final[Path] = Path(__file__).resolve().parent / "plan.test.mjs"
SCREEN_FILES: Final[tuple[str, ...]] = ("screens/plan.js", "screens/plan.css")


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
    css = (UI / "screens" / "plan.css").read_text(encoding="utf-8")
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", css), "plan.css hard-codes a colour; use the tokens"
    assert "overflow-wrap: anywhere" in css, "long WhatsApp template names must wrap on a phone"


def test_product_fact_is_labelled_and_sourced() -> None:
    js = (UI / "screens" / "plan.js").read_text(encoding="utf-8")
    assert "How we tested this" in js
    assert "not a result from your shop" in js
    assert "docs/v2/CATALOG.md" in js
    catalog = (ROOT / "docs" / "v2" / "CATALOG.md").read_text(encoding="utf-8")
    # the numbers the note quotes are the ones the catalogue's evaluation table reports (k = 5)
    assert re.search(r"co-purchase \(cosine\) \| 5 \| 34\.7%", catalog)
    assert re.search(r"popularity \(baseline\) \| 5 \| 22\.0%", catalog)


def test_plan_draws_and_clicks_through() -> None:
    done = subprocess.run(
        [*node_command(), str(NODE_TEST)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stdout[-4000:] + done.stderr[-2000:]
