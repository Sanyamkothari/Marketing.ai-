"""Principle 1 (plan section 2.1): the engine never branches on a use-case id.

Nothing under `engine/` may mention a use-case id at all - not in code, not in a docstring and not
in a comment - because a mention is how branching starts. Use-case behaviour belongs in YAML.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from engine.config import list_use_case_ids


def forbidden_spellings() -> tuple[str, ...]:
    """Every shipped use-case id and its underscore spelling (the YAML file stem)."""
    ids = list_use_case_ids()
    return tuple(sorted({*ids, *(use_case_id.replace("-", "_") for use_case_id in ids)}))


def test_every_shipped_use_case_is_one_we_scan_for(repo_root: Path) -> None:
    """The scan covers the whole configuration directory, however many files it grows to.

    A count here would have to be edited every time a use case is added by YAML alone, which is
    exactly the thing principle 1 promises never happens - so the count comes from the directory.
    """
    ids = list_use_case_ids()
    stems = sorted(path.stem for path in (repo_root / "configs" / "use_cases").glob("*.yaml"))
    assert stems, "configs/use_cases/ is empty; this file would scan for nothing"
    assert stems == sorted(use_case_id.replace("-", "_") for use_case_id in ids)
    assert len(forbidden_spellings()) >= len(ids)


def test_the_engine_package_has_modules_to_scan(repo_root: Path) -> None:
    files = sorted((repo_root / "engine").rglob("*.py"))
    assert len(files) >= 10


@pytest.mark.parametrize("spelling", forbidden_spellings())
def test_no_engine_file_mentions_a_use_case_id(repo_root: Path, spelling: str) -> None:
    pattern = re.compile(rf"(?<![A-Za-z0-9_-]){re.escape(spelling)}(?![A-Za-z0-9_-])")
    offenders = [
        str(path.relative_to(repo_root))
        for path in sorted((repo_root / "engine").rglob("*.py"))
        if pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], f"{spelling!r} appears in {offenders}"
