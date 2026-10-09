"""Plan J M101: the Results page's conflicts card (`ui/modules/decide/views.js` `conflictsCardHtml`).

Runs `conflicts_card.test.mjs` in node (no npm install needed: views.js is pure). Skipped with the
reason where there is no node, failed instead under `REQUIRE_JSDOM=1` (`tests/fixtures/node.py`).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Final

from tests.fixtures.node import skip_without_node

HERE: Final[Path] = Path(__file__).resolve().parent


def test_the_conflicts_card_node_suite_passes() -> None:
    node = skip_without_node()
    result = subprocess.run(
        [node, "--test", str(HERE / "conflicts_card.test.mjs")],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
    assert "# fail 0" in result.stdout
