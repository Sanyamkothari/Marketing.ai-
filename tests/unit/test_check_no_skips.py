"""`scripts/check_no_skips.py`: a skipped or missing must-run test fails CI (M56, DEC-878)."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.check_no_skips import main, read_cases, violations

REPORT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest">
  <testcase classname="tests.integration.production.test_production_ui_js" name="test_in_jsdom" />
  <testcase classname="tests.unit.test_registry" name="test_store[postgres]">%s</testcase>
  <testcase classname="tests.unit.test_registry" name="test_store[sqlite]" />
</testsuite></testsuites>
"""
SKIP = '<skipped type="pytest.skip" message="no PostgreSQL server at postgresql://x">skip</skipped>'


def write(tmp_path: Path, body: str = "") -> Path:
    path = tmp_path / "junit.xml"
    path.write_text(REPORT % body, encoding="utf-8")
    return path


def test_a_report_where_every_pattern_ran_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--junit", str(write(tmp_path)), "--id", "_ui_js::", "--id", r"\[postgres\]"]) == 0
    assert "2 matching tests ran, none skipped" in capsys.readouterr().out


def test_a_skipped_match_fails_and_names_the_test_and_its_reason(tmp_path: Path) -> None:
    cases = read_cases(write(tmp_path, SKIP))
    problems = violations(cases, [r"\[postgres\]"])
    assert (
        problems[0]
        == "skipped: tests.unit.test_registry::test_store[postgres] (no PostgreSQL server at postgresql://x)"
    )
    assert main(["--junit", str(write(tmp_path, SKIP)), "--id", r"\[postgres\]"]) == 1


def test_a_pattern_that_matched_nothing_that_ran_fails(tmp_path: Path) -> None:
    cases = read_cases(write(tmp_path))
    assert violations(cases, ["test_production_ops_ui_js"]) == [
        "never ran: no test matching 'test_production_ops_ui_js' ran in this report"
    ]


def test_skips_outside_the_patterns_are_not_its_business(tmp_path: Path) -> None:
    assert violations(read_cases(write(tmp_path, SKIP)), [r"\[sqlite\]"]) == []


def test_an_unreadable_report_is_exit_2(tmp_path: Path) -> None:
    (tmp_path / "junit.xml").write_text("<not xml", encoding="utf-8")
    assert main(["--junit", str(tmp_path / "junit.xml"), "--id", "x"]) == 2
    assert main(["--junit", str(tmp_path / "missing.xml"), "--id", "x"]) == 2
