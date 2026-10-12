"""Plan O M242 (DEC-1812): the workspace currency in `ui/format.js`.

Runs `workspace_currency.test.mjs` in node (no npm install needed: the module is pure). Skipped with the reason where
there is no node, failed instead under `REQUIRE_JSDOM=1` (`tests/fixtures/node.py`).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Final

from tests.fixtures.node import skip_without_node

HERE: Final[Path] = Path(__file__).resolve().parent
UI_FORMAT: Final[Path] = HERE.parents[2] / "ui" / "format.js"


def test_the_workspace_currency_node_suite_passes() -> None:
    node = skip_without_node()
    result = subprocess.run(
        [node, "--test", str(HERE / "workspace_currency.test.mjs")],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
    assert "# fail 0" in result.stdout


def test_no_currency_sign_is_typed_into_the_format_module() -> None:
    """The format test's rule (tests/integration/test_ui.py) still holds after M242: the rupee is a code point."""
    text = UI_FORMAT.read_text(encoding="utf-8")
    assert "₹" not in text
    assert "\\u20B9" in text
