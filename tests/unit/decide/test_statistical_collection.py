"""`make test` never collects the statistical suite, and `make test-statistical` does (M90, DEC-1300 (e)).

The Makefile's `test` and `test-all` targets cannot be changed (`PARALLEL_WORK_PROTOCOL.md` §4), so
`tests/statistical/conftest.py` does the excluding. These tests run a real `pytest --collect-only` in a
child process, with the variable unset and set, because the conftest is read at collection time and
only a fresh interpreter shows what a default run would do. `addopts` is replaced so `-q` is not
doubled: one `-q` lists each collected test by node id, two only count them per file.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _collect(repo_root: Path, *args: str, statistical: bool) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k not in {"MARKETING_AI_STATISTICAL", "PYTEST_ADDOPTS"}}
    if statistical:
        env["MARKETING_AI_STATISTICAL"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-o", "addopts=--strict-markers", *args],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_default_marker_filter_does_not_collect_the_statistical_suite(repo_root: Path) -> None:
    """The exact filter `make test` uses, over the whole `tests/` tree, with the variable unset."""
    run = _collect(
        repo_root, "tests/statistical", "-m", "not slow and not bedrock and not aws", statistical=False
    )
    assert "test_harness_smoke" not in run.stdout, run.stdout
    assert "skipped" not in run.stdout.lower(), run.stdout
    assert run.returncode in (0, 5), run.stdout + run.stderr  # 5: nothing collected, which is the point


def test_the_variable_turns_collection_on(repo_root: Path) -> None:
    run = _collect(repo_root, "tests/statistical", "-m", "statistical", statistical=True)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "test_a_fixed_seed_gives_the_same_draws_twice" in run.stdout, run.stdout


def test_the_smoke_tests_carry_the_statistical_marker(repo_root: Path) -> None:
    """With the variable set, `-m "not statistical"` leaves the suite empty: every test is marked."""
    run = _collect(repo_root, "tests/statistical", "-m", "not statistical", statistical=True)
    assert "test_harness_smoke" not in run.stdout, run.stdout
