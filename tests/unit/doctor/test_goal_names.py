"""The server's copy of the goals' display names is the UI's (Plan O M247, DEC-1817).

`engine.doctor.goal_names.GOAL_DISPLAY_NAMES` names the goal in the sentence a person approves word for word; the
screens name goals by `ui/labels.js` `GOAL_NAMES`. This reads the UI's table and fails when the two differ.
"""

from __future__ import annotations

import re
from pathlib import Path

from engine.doctor.goal_names import GOAL_DISPLAY_NAMES

LABELS = Path(__file__).resolve().parents[3] / "ui" / "labels.js"


def _ui_goal_names() -> dict[str, str]:
    text = LABELS.read_text(encoding="utf-8")
    block = re.search(r"export const GOAL_NAMES = Object\.freeze\(\{(.*?)\}\);", text, re.S)
    assert block is not None, "GOAL_NAMES is no longer where this test reads it"
    pairs = re.findall(r'^\s*"?([a-z0-9-]+)"?\s*:\s*"([^"]+)",?\s*$', block.group(1), re.M)
    return dict(pairs)


def test_the_server_names_every_goal_as_the_screens_do() -> None:
    ui = _ui_goal_names()
    assert ui, "no goal name was read"
    assert dict(GOAL_DISPLAY_NAMES) == ui
