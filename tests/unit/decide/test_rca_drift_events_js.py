"""Plan J M109 (review fix): the root-cause screen shows the events behind the change, read-only (`ui/modules/generative/rca.js`).

Runs `rca_drift_events.test.mjs` in node (no npm install needed: the card is pure). Skipped with the
reason where there is no node, failed instead under `REQUIRE_JSDOM=1` (`tests/fixtures/node.py`).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Final

from tests.fixtures.node import skip_without_node

HERE: Final[Path] = Path(__file__).resolve().parent


def test_the_rca_drift_events_node_suite_passes() -> None:
    node = skip_without_node()
    result = subprocess.run(
        [node, "--test", str(HERE / "rca_drift_events.test.mjs")],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
    assert "# fail 0" in result.stdout
