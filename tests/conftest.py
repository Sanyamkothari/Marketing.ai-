"""Fixtures shared by every test module.

Only the two path fixtures live here (design §11): everything else a test needs is defined in the
module that needs it, so owners never contend over this file.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """The repository checkout root (the directory holding pyproject.toml)."""
    return Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def config_root(repo_root: Path) -> Path:
    """The ``configs/`` directory of the checkout."""
    return repo_root / "configs"
