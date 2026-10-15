"""The `frozen` list in tests/conftest.py names real tests, so a renamed folder cannot quietly un-freeze one."""

from __future__ import annotations

from pathlib import Path

from tests.conftest import FROZEN_PATHS, STILL_FAST

ROOT = Path(__file__).resolve().parents[2]


def _matches(prefix: str) -> list[Path]:
    return [p for p in ROOT.glob("tests/**/*.py") if p.relative_to(ROOT).as_posix().startswith(prefix)]


def test_every_frozen_prefix_matches_a_test_file() -> None:
    assert [prefix for prefix in FROZEN_PATHS if not _matches(prefix)] == []


def test_every_still_fast_prefix_matches_a_test_file_under_a_frozen_one() -> None:
    for prefix in STILL_FAST:
        assert _matches(prefix), prefix
        assert prefix.startswith(FROZEN_PATHS), prefix
