"""One fixture world for every Plan K scenario and route test (a real training and scoring run, built once)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.agentic.scenarios.world import World, build_world


@pytest.fixture(scope="session")
def agentic_world(config_root: Path, tmp_path_factory: pytest.TempPathFactory) -> World:
    return build_world(config_root, tmp_path_factory.mktemp("agentic-world"))
