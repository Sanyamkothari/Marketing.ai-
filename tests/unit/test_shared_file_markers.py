"""The shared files carry their three marker blocks (PARALLEL_WORK_PROTOCOL.md §4).

Three branches edit these files at once. §4 gives each branch a block of its own and forbids
editing above it, which works only while the blocks are there: a hurried merge that drops one
sends the next branch's code somewhere nobody agreed on, and nothing would notice until the
three-way merge this whole protocol exists to keep boring.

So the blocks are checked, not remembered. Each file must carry all three, exactly once, opened
before it is closed, and in the order Phase 2, Phase 3a, Phase 4a - so "append at the end of your
block" means the same place to every reader.

`docs/API.md` is in §4's list and deliberately absent from this one: it is generated and `make
lint` fails on a hand-edit (DEC-014), so a block there would be deleted by the next `make
generate`. Models added by a branch reach it through `gen_api_docs`, which is the point.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

SHARED_FILES: Final[tuple[str, ...]] = (
    "engine/contracts.py",
    "engine/config.py",
    "engine/settings.py",
    "engine/pipeline.py",
    "api/main.py",
    "api/schemas.py",
    "ui/index.html",
    "ui/modules/router.js",
    "Makefile",
    "pyproject.toml",
    "README.md",
    "docs/DECISIONS.md",
)
"""Every file §4's table calls shared.

`engine/llm.py` and `engine/generative/contracts.py` are deliberately absent: §3 gives both to
Phase 3a outright, and a file with one owner needs no blocks. The same goes for
`engine/onboarding/specs.py`, which belongs to Phase 2."""

PHASES: Final[tuple[str, ...]] = ("PHASE-2", "PHASE-3A", "PHASE-4A", "PHASE-4B")

OPEN: Final[str] = "---- {phase} ("
CLOSE: Final[str] = "---- END {phase} ----"


@pytest.mark.parametrize("relative", SHARED_FILES)
def test_a_shared_file_carries_all_three_blocks(repo_root: Path, relative: str) -> None:
    path = repo_root / relative
    assert path.is_file(), f"{relative} is listed as shared and does not exist"
    text = path.read_text(encoding="utf-8")
    positions: list[int] = []
    for phase in PHASES:
        opened = [m.start() for m in re.finditer(re.escape(OPEN.format(phase=phase)), text)]
        closed = [m.start() for m in re.finditer(re.escape(CLOSE.format(phase=phase)), text)]
        assert len(opened) == 1, f"{relative} has {len(opened)} {phase} opening markers, expected 1"
        assert len(closed) == 1, f"{relative} has {len(closed)} {phase} closing markers, expected 1"
        assert opened[0] < closed[0], f"{relative} closes its {phase} block before it opens it"
        positions += [opened[0], closed[0]]
    assert positions == sorted(positions), f"{relative} has its blocks out of order or nested"


@pytest.mark.parametrize("relative", SHARED_FILES)
def test_a_shared_file_says_it_is_shared(repo_root: Path, relative: str) -> None:
    """The rule travels with the file: a reader who opens one must not have to know the protocol."""
    text = (repo_root / relative).read_text(encoding="utf-8")
    assert "PARALLEL_WORK_PROTOCOL.md §4" in text, f"{relative} does not name the rule it lives under"


def test_the_generated_api_docs_carry_no_blocks(repo_root: Path) -> None:
    """`docs/API.md` is generated; a block in it would not survive the next `make generate`."""
    text = (repo_root / "docs" / "API.md").read_text(encoding="utf-8")
    for phase in PHASES:
        assert OPEN.format(phase=phase) not in text
