"""Plan J M96: the uplift Output page's ranking banner (`ui/modules/uplift/views.js` `rankingBanner`).

Runs `ranking_banner.test.mjs` in node (no npm install needed: views.js is pure). Skipped with the
reason where there is no node, failed instead under `REQUIRE_JSDOM=1` (`tests/fixtures/node.py`).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Final

from tests.fixtures.node import skip_without_node

HERE: Final[Path] = Path(__file__).resolve().parent


def test_the_ranking_banner_node_suite_passes() -> None:
    node = skip_without_node()
    result = subprocess.run(
        [node, "--test", str(HERE / "ranking_banner.test.mjs")],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
    assert "# fail 0" in result.stdout
