"""Import hygiene: the engine's import graph is a DAG and stays free of heavy libraries.

Each check runs in a fresh interpreter, because `sys.modules` in the test process is polluted by
every other test module.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

HEAVY_MODULES: tuple[str, ...] = ("autogluon", "shap", "sklearn", "imblearn")

pytestmark = pytest.mark.integration


def run_probe(repo_root: Path, source: str) -> str:
    """Run `source` in a fresh interpreter rooted at the checkout and return its stdout."""
    completed = subprocess.run(
        [sys.executable, "-c", source],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
        env={"PYTHONPATH": str(repo_root), "PATH": "/usr/bin:/bin"},
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def test_config_does_not_import_contracts(repo_root: Path) -> None:
    source = "import sys; import engine.config; print('engine.contracts' in sys.modules)"
    assert run_probe(repo_root, source) == "False"


def test_importing_the_engine_does_not_load_a_heavy_library(repo_root: Path) -> None:
    source = (
        "import importlib, sys\n"
        "import engine, engine.pipeline, engine.storage, engine.jobs, engine.registry\n"
        "from engine.config import STAGE_MODULE_MAP\n"
        "for name in sorted(set(STAGE_MODULE_MAP.values())):\n"
        "    importlib.import_module(name)\n"
        f"heavy = {HEAVY_MODULES!r}\n"
        "print(sorted(m for m in sys.modules if m.split('.')[0] in heavy))"
    )
    assert run_probe(repo_root, source) == "[]"


def test_the_stage_modules_do_not_import_pandas_at_module_level(repo_root: Path) -> None:
    source = (
        "import importlib, sys\n"
        "from engine.config import STAGE_MODULE_MAP\n"
        "for name in sorted(set(STAGE_MODULE_MAP.values())):\n"
        "    importlib.import_module(name)\n"
        "print('pandas' in sys.modules)"
    )
    assert run_probe(repo_root, source) == "False"
