"""Plan J M98: the hand-off is evidence, so what it names must exist (review finding 6).

The partner's `docs/handoff/M98.md` cited two tests that were never written and listed a file as changed
that was untouched. This test reads the hand-off and the review and fails when a test function, a test
file or a source path they name in backticks is not there.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

ROOT: Final[Path] = Path(__file__).resolve().parents[3]
DOCUMENTS: Final[tuple[str, ...]] = ("docs/handoff/M98.md", "docs/handoff/M98_REVIEW.md")
PATH_PREFIXES: Final[tuple[str, ...]] = ("tests/", "engine/", "api/", "ui/", "configs/", "docs/", "scripts/")
CITED_AS_MISSING: Final[frozenset[str]] = frozenset(
    {
        "test_build_treat_list_on_real_synthetic_uplift_and_propensity_runs",
        "test_treat_list_invariants_and_null_reasons",
    }
)
"""The review quotes the two names the partner cited and says neither exists; that is its finding."""
CODE: Final[re.Pattern[str]] = re.compile(r"`([^`\n]+)`")


def _tokens(document: str) -> list[str]:
    return CODE.findall((ROOT / document).read_text(encoding="utf-8"))


def _defined_in(path: Path, name: str) -> bool:
    return (
        re.search(rf"^\s*(?:async\s+)?def {re.escape(name)}\(", path.read_text(encoding="utf-8"), re.M)
        is not None
    )


def _test_functions() -> dict[str, list[Path]]:
    found: dict[str, list[Path]] = {}
    for path in (ROOT / "tests").rglob("test_*.py"):
        for match in re.finditer(r"^\s*def (test_\w+)\(", path.read_text(encoding="utf-8"), re.M):
            found.setdefault(match.group(1), []).append(path)
    return found


@pytest.mark.parametrize("document", DOCUMENTS)
def test_every_path_the_handoff_names_exists(document: str) -> None:
    missing = []
    for token in _tokens(document):
        head = token.split("::")[0].split()[0] if token.split() else ""
        if not head.startswith(PATH_PREFIXES) or any(mark in head for mark in "*<>{}$"):
            continue
        if head.endswith(","):
            head = head.rstrip(",")
        target = ROOT / head
        if not (target.exists()):
            missing.append(head)
    assert not missing, f"{document} names paths that do not exist: {sorted(set(missing))}"


@pytest.mark.parametrize("document", DOCUMENTS)
def test_every_test_the_handoff_names_exists(document: str) -> None:
    functions = _test_functions()
    missing = []
    for token in _tokens(document):
        if "::" in token:
            path_text, _, name = token.partition("::")
            name = name.split("[")[0].split()[0]
            path = ROOT / path_text
            if path.is_file() and not _defined_in(path, name):
                missing.append(token)
        elif re.fullmatch(r"test_\w+", token) and token not in functions and token not in CITED_AS_MISSING:
            missing.append(token)
    assert not missing, f"{document} names tests that do not exist: {sorted(set(missing))}"


def test_the_two_tests_the_partner_cited_are_no_longer_cited() -> None:
    """They never existed; the hand-off must not say they do."""
    text = (ROOT / "docs/handoff/M98.md").read_text(encoding="utf-8")
    for invented in (
        "test_build_treat_list_on_real_synthetic_uplift_and_propensity_runs",
        "test_treat_list_invariants_and_null_reasons",
        "test_treat_list_download_analyst_and_viewer_access",
    ):
        assert invented not in text, invented


def test_the_untouched_row_level_download_test_is_listed_as_untouched() -> None:
    text = (ROOT / "docs/handoff/M98.md").read_text(encoding="utf-8")
    row = next(
        line
        for line in text.splitlines()
        if line.startswith("| `tests/integration/decide/test_row_level_downloads.py`")
    )
    assert "Not changed" in row
