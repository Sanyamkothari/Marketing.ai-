"""The API starts in the image it ships in, which has no `tests/` package.

This file exists because it did not. `api/routes/generative.py` imported the sample corpus from
`tests.fixtures.make_docs` at module level, the production `api` image copies `engine`, `api`,
`configs` and the rest but never `tests/`, and `api.main` imports that route module - so the
deployed API raised `ModuleNotFoundError` at start-up and served nothing at all, predictive routes
included. Every existing test passed throughout, for a reason worth writing down: the `test` image
is built *on top of* the `api` image with `tests/` copied back in, and a developer's checkout has it
too, so there was nowhere in the whole pipeline the real image's import graph was ever exercised.

**The rule is checked statically.** No module under `engine/` or `api/` may import `tests`. That
catches the mistake at the line that makes it, including in a module no test happens to import.

**The consequence is checked by running it.** A subprocess imports `api.main` with `tests` made
unimportable - exactly the state of the image - builds the app and asks `/healthz`. A rule can have
a loophole the AST walk misses (an `importlib` call, a string import); the start-up cannot.

**The sample paths are pinned to their source.** The route now finds the sample corpus by path, so
the three names it uses are asserted equal to `make_docs`' own; moving the corpus without moving
both is a failing test rather than a sample button that quietly stops working.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from api.routes import generative
from tests.fixtures import make_docs

SHIPPED_PACKAGES: tuple[str, ...] = ("engine", "api")
"""What the server imports. `scripts/` ships too, but only its entry point runs in the image."""


def _imports_tests(tree: ast.AST) -> list[int]:
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        if any(name == "tests" or name.startswith("tests.") for name in names):
            lines.append(node.lineno)
    return lines


@pytest.mark.parametrize("package", SHIPPED_PACKAGES)
def test_no_shipped_module_imports_the_tests_package(repo_root: Path, package: str) -> None:
    """A module the server imports never imports `tests`, which the production image does not contain."""
    offenders = {
        str(path.relative_to(repo_root)): lines
        for path in sorted((repo_root / package).rglob("*.py"))
        if (lines := _imports_tests(ast.parse(path.read_text(encoding="utf-8"))))
    }
    assert offenders == {}, f"these would fail to import in the production image: {offenders}"


def test_the_api_starts_and_answers_with_no_tests_package_present(repo_root: Path) -> None:
    """With `tests` unimportable, exactly as in the image, the app builds and `/healthz` answers 200."""
    program = (
        "import sys\n"
        "sys.modules['tests'] = None\n"
        "import api.main\n"
        "from fastapi.testclient import TestClient\n"
        "print(TestClient(api.main.create_app()).get('/healthz').status_code)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", program], cwd=repo_root, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().splitlines()[-1] == "200"


def test_the_sample_corpus_paths_are_the_ones_make_docs_writes() -> None:
    """The route finds the samples by path, so its three names must stay equal to `make_docs`' own."""
    assert generative.SAMPLE_DOCS_DIR == make_docs.DOCS_DIR
    assert generative.SAMPLE_SOURCE_DIR == make_docs.SOURCE_DIR
    assert generative.SAMPLE_REFERENCE_QA_FILENAME == make_docs.REFERENCE_QA_FILENAME
