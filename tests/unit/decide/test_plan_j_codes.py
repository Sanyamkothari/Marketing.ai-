"""Plan J's error codes are well formed and explained (M90, DEC-1300 (d)).

`PLAN_J_CODES` is empty at M90 and each milestone adds its own. These tests are written as loops, not
as parametrised cases, so they run (and mean something) while the set is empty and keep working as it
grows: a code that is not upper-snake-case, or has no entry in `configs/pilot/help.yaml`, fails here.
"""

from __future__ import annotations

import re
from pathlib import Path

from engine.decide.codes import PLAN_J_CODES
from engine.pilot.help import known_codes, load_help

UPPER_SNAKE = re.compile(r"^[A-Z][A-Z0-9]*(_[A-Z0-9]+)*$")


def test_the_set_is_an_immutable_set_of_strings() -> None:
    assert isinstance(PLAN_J_CODES, frozenset)
    assert all(isinstance(code, str) for code in PLAN_J_CODES)


def test_every_plan_j_code_is_upper_snake_case() -> None:
    bad = sorted(code for code in PLAN_J_CODES if not UPPER_SNAKE.match(code))
    assert bad == [], f"not UPPER_SNAKE_CASE: {bad}"


def test_every_plan_j_code_has_a_help_entry(config_root: Path) -> None:
    catalogue = load_help(config_root)
    missing = sorted(code for code in PLAN_J_CODES if code not in catalogue.codes)
    assert missing == [], f"add these to configs/pilot/help.yaml in the same change: {missing}"


def test_the_help_catalogue_asks_for_every_plan_j_code() -> None:
    """The hook in `engine/pilot/help.py`: without it a Plan J code would never need an entry."""
    assert known_codes() >= PLAN_J_CODES


def test_the_pattern_itself_accepts_and_rejects_what_it_should() -> None:
    for good in ("CONSENT_PURPOSE_MISSING", "A", "HOLDOUT_SALT_MISSING", "P95_NOT_MET"):
        assert UPPER_SNAKE.match(good), good
    for bad in (
        "consent_missing",
        "Consent_Missing",
        "_LEADING",
        "TRAILING_",
        "DOUBLE__UNDERSCORE",
        "WITH-DASH",
        "",
    ):
        assert not UPPER_SNAKE.match(bad), bad
