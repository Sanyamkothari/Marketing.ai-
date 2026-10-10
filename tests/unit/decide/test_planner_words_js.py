"""Plan J M109: the Output page and the plan card use the planner's words (`planner_words.test.mjs`).

Runs the node suite (no npm install needed: the builders are pure). Skipped with the reason where there is
no node, failed instead under `REQUIRE_JSDOM=1` (`tests/fixtures/node.py`).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Final

from tests.fixtures.node import skip_without_node

HERE: Final[Path] = Path(__file__).resolve().parent


def test_the_planner_words_node_suite_passes() -> None:
    node = skip_without_node()
    result = subprocess.run(
        [node, "--test", str(HERE / "planner_words.test.mjs")],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
    assert "# fail 0" in result.stdout
