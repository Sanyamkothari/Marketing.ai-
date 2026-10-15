"""What a goal is called in a sentence a person approves (Plan O M247, DEC-1817).

The sentence Approve records (`engine.doctor.visit.approval_sentence`) is shown and checked by the server, word for
word, so the goal's name in it must be the one the screens show, not its id: `ui/labels.js` `GOAL_NAMES` (Plan M
§4.2, DEC-1602) by use-case id, else the use case's configured `name` (the goal brief's `goal_name`), else the id.

`GOAL_DISPLAY_NAMES` repeats `GOAL_NAMES` for the server, read from `configs/doctor/goal_names.yaml` (the engine
never names a use case itself); `tests/unit/doctor/test_goal_names.py` reads `ui/labels.js` and fails when the two
differ, so the copy cannot drift.
"""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType
from typing import Final

import yaml

__all__ = ["GOAL_DISPLAY_NAMES", "GOAL_NAMES_PATH", "goal_display_name", "load_goal_display_names"]

GOAL_NAMES_PATH: Final[Path] = Path(__file__).resolve().parents[2] / "configs" / "doctor" / "goal_names.yaml"


def load_goal_display_names(path: Path | None = None) -> MappingProxyType[str, str]:
    """The `names:` table of `goal_names.yaml`: goal id to the name the screens show."""
    document = yaml.safe_load((path or GOAL_NAMES_PATH).read_text(encoding="utf-8")) or {}
    names = document.get("names") or {}
    return MappingProxyType({str(goal_id): str(name) for goal_id, name in names.items()})


GOAL_DISPLAY_NAMES: Final[MappingProxyType[str, str]] = load_goal_display_names()
"""`ui/labels.js` `GOAL_NAMES`, kept equal by a test."""


def goal_display_name(goal_id: str, configured: str | None = None) -> str:
    """The goal's name as the screens show it: its display name, else its configured name, else its id."""
    return GOAL_DISPLAY_NAMES.get(goal_id) or (configured or "").strip() or goal_id
