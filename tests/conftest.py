"""Fixtures shared by every test module.

Only the two path fixtures live here (design §11): everything else a test needs is defined in the
module that needs it, so owners never contend over this file.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# The deterministic fake language model is a test tool: a real run with no AI service connected is
# refused with 409 AI_NOT_CONNECTED (DEC-1140), and the suite opts in here. Tests of that refusal
# switch it off for themselves. `setdefault`, so a run that exports the variable itself still wins.
os.environ.setdefault("MARKETING_AI_ALLOW_FAKE_AI", "1")


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """The repository checkout root (the directory holding pyproject.toml)."""
    return Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def config_root(repo_root: Path) -> Path:
    """The ``configs/`` directory of the checkout."""
    return repo_root / "configs"
