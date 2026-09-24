"""Node and jsdom for the JS halves of the Python suite, and the rule that goes with them (M56).

The same rule as `tests/fixtures/postgres.py`: a JS check that does not run says so out loud.
Node is not a Python dependency, so a laptop without it gets a **skip with a reason** - which
`-rs` in `pyproject.toml`'s `addopts` prints next to the test's name - and not a failure.

`REQUIRE_JSDOM=1` turns that skip into a failure. CI sets it, having installed node and run
`npm ci` in every directory that holds jsdom suites, so a runner where either went missing goes red
instead of green with a note (DEC-877). It has no `MARKETING_AI_` prefix on purpose: it describes a
test run, never a deployment, and so it is not something `engine.settings` should know about.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

__all__ = ["REQUIRE_ENV_VAR", "require_jsdom", "skip_without_jsdom", "skip_without_node"]

REQUIRE_ENV_VAR: str = "REQUIRE_JSDOM"
"""Set to `1` to turn "no node" or "no jsdom" from a skip into a failure. CI sets it."""


def require_jsdom() -> bool:
    """Whether a missing node or jsdom must fail the run rather than skip it (`$REQUIRE_JSDOM`)."""
    return os.environ.get(REQUIRE_ENV_VAR, "").strip() in {"1", "true", "yes", "on"}


def _skip(reason: str) -> None:
    if require_jsdom():
        pytest.fail(f"{REQUIRE_ENV_VAR} is set, so this JS check had to run: {reason}")
    pytest.skip(reason)


def skip_without_node() -> str:
    """The path of `node` - or a skip, or under `$REQUIRE_JSDOM` a failure, naming why."""
    node = shutil.which("node")
    if node is None:
        _skip("node is not installed")
    assert node is not None
    return node


def skip_without_jsdom(package_dir: Path) -> str:
    """`skip_without_node()`, and also jsdom installed in `package_dir` by `npm ci`."""
    node = skip_without_node()
    if not (package_dir / "node_modules" / "jsdom").is_dir():
        _skip(f"jsdom is not installed: cd {package_dir} && npm ci --no-audit --no-fund")
    return node
