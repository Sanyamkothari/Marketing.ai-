"""The browser's copy of the power maths and the Output page's card, checked without a browser.

`power.test.mjs` runs `ui/power.js` under node against the cases in
`tests/fixtures/power_cases.json` (the same ones `test_power.py` checks the engine on) and renders the
card. Skipped, with the reason printed, where there is no node; `REQUIRE_JSDOM=1` (CI) makes that a failure.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Final

from tests.fixtures.node import skip_without_node

HERE: Final[Path] = Path(__file__).resolve().parent


def test_node_suite_passes() -> None:
    node = skip_without_node()
    result = subprocess.run(
        [node, "--test", str(HERE / "power.test.mjs")],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
    assert "# fail 0" in result.stdout
