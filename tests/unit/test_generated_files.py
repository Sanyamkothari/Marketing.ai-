"""The committed generated files never drift from their generators (DEC-014)."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import gen_api_docs, gen_templates


def test_templates_are_up_to_date(repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(repo_root)
    assert gen_templates.main(["--check"]) == 0


def test_api_docs_are_up_to_date(repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(repo_root)
    assert gen_api_docs.main(["--check"]) == 0
