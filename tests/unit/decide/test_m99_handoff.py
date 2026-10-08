"""Plan J M99: the hand-off is evidence, so what it names must exist (as `test_m98_handoff.py` for M98).

The partner's `docs/handoff/M99.md` described acceptance tests that wrote `scores.parquet` by hand as proof
that a real run honours channel consent. This test reads the rewritten hand-off and the review and fails
when a test function or a path they name in backticks is not there.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

ROOT: Final[Path] = Path(__file__).resolve().parents[3]
DOCUMENTS: Final[tuple[str, ...]] = ("docs/handoff/M99.md", "docs/handoff/M99_REVIEW.md")
PATH_PREFIXES: Final[tuple[str, ...]] = (
    "tests/",
    "engine/",
    "api/",
    "ui/",
    "configs/",
    "docs/",
    "scripts/",
    "alembic/",
)
ABSENT_BY_DESIGN: Final[frozenset[str]] = frozenset({"configs/decide/catalogue.yaml"})
"""The catalogue the partner shipped and the review removed: named because it must not exist."""
CODE: Final[re.Pattern[str]] = re.compile(r"`([^`\n]+)`")


def _tokens(document: str) -> list[str]:
    return CODE.findall((ROOT / document).read_text(encoding="utf-8"))


def _test_functions() -> set[str]:
    found: set[str] = set()
    for path in (ROOT / "tests").rglob("test_*.py"):
        found.update(re.findall(r"^\s*def (test_\w+)\(", path.read_text(encoding="utf-8"), re.M))
    return found


@pytest.mark.parametrize("document", DOCUMENTS)
def test_every_path_the_handoff_names_exists(document: str) -> None:
    missing = []
    for token in _tokens(document):
        head = token.split("::")[0].split()[0].rstrip(",") if token.split() else ""
        if not head.startswith(PATH_PREFIXES) or any(mark in head for mark in "*<>{}$"):
            continue
        if head in ABSENT_BY_DESIGN:
            continue
        if not (ROOT / head).exists():
            missing.append(head)
    assert not missing, f"{document} names paths that do not exist: {sorted(set(missing))}"


@pytest.mark.parametrize("document", DOCUMENTS)
def test_every_test_the_handoff_names_exists(document: str) -> None:
    functions = _test_functions()
    named = [token for token in _tokens(document) if re.fullmatch(r"test_\w+", token)]
    assert named, "the hand-off names its tests"
    assert sorted(set(named) - functions) == []


def test_what_must_be_absent_is_absent() -> None:
    for path in ABSENT_BY_DESIGN:
        assert not (ROOT / path).exists(), path


def test_the_reverted_benchmark_is_listed_as_not_changed() -> None:
    text = (ROOT / "docs/handoff/M99.md").read_text(encoding="utf-8")
    row = next(line for line in text.splitlines() if line.startswith("| `scripts/bench_1m.py`"))
    assert "Not changed" in row
