"""`scripts/check_readme.py`: a milestone the tests say is built cannot be called pending.

Every case here feeds the checker a synthetic README snippet and a synthetic JUnit report, so the
tests are about the checker's reading of Markdown and of pytest's report format - not about the
state of the real README, which changes with every milestone and is gated by CI running the script
itself. The one test that touches the repository is the mapping's: every path it names must exist,
because a renamed test file would otherwise turn "passing tests" into "no evidence" without a word.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import check_readme
from scripts.check_readme import CheckError, Evidence, milestone_rows, verdict

TABLE = """\
## Milestone status

| # | Milestone | Definition of done | Status |
|---|---|---|---|
| M1 | Skeleton | configs load | **done** |
| M2 | Ingest | checks with tests | pending |
| M3 | Train | a model | **partial** — see below |
"""

MAPPING = {
    "M1": ("tests/unit/test_one.py",),
    "M2": ("tests/unit/test_two.py", "tests/integration/"),
    "M3": ("tests/unit/test_three.py",),
}


def junit(*cases: tuple[str, str]) -> str:
    """A pytest-shaped report: `(classname, outcome)` with outcome passed / failed / error / skipped."""
    body = []
    for classname, outcome in cases:
        child = {
            "passed": "",
            "failed": '<failure message="boom">trace</failure>',
            "error": '<error message="boom">trace</error>',
            "skipped": '<skipped type="pytest.skip" message="no server">reason</skipped>',
        }[outcome]
        body.append(f'<testcase classname="{classname}" name="test_x" time="0.01">{child}</testcase>')
    return (
        '<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest" tests="1">'
        + "".join(body)
        + "</testsuite></testsuites>"
    )


def cases_of(tmp_path: Path, *cases: tuple[str, str]) -> list[tuple[str, str]]:
    report = tmp_path / "junit.xml"
    report.write_text(junit(*cases), encoding="utf-8")
    return check_readme.read_junit(report)


# --- reading the README ---------------------------------------------------------------------


def test_rows_are_read_from_a_status_table_with_their_line_numbers() -> None:
    rows = milestone_rows(TABLE)
    assert [(r.milestone, r.status, r.line) for r in rows] == [
        ("M1", "done", 5),
        ("M2", "pending", 6),
        ("M3", "partial — see below", 7),
    ]
    assert [r.pending for r in rows] == [False, True, False]


@pytest.mark.parametrize(
    "status",
    ["pending", "**Pending**", "`pending`", "not started", "not yet built", "todo", "To do", "planned"],
)
def test_every_spelling_of_not_built_counts_as_pending(status: str) -> None:
    text = f"| # | Status |\n|---|---|\n| M9 | {status} |\n"
    assert milestone_rows(text)[0].pending


@pytest.mark.parametrize(
    "status", ["done", "**done** — pending a laptop run", "partial", "in progress", "superseded"]
)
def test_a_status_that_only_mentions_pending_later_is_not_pending(status: str) -> None:
    text = f"| # | Status |\n|---|---|\n| M9 | {status} |\n"
    assert not milestone_rows(text)[0].pending


def test_the_status_column_is_found_by_its_header_not_its_position() -> None:
    text = "| Status | # | Milestone |\n|:---|:---:|---|\n| pending | M4 | Score |\n"
    # The first cell must be the milestone id; a table laid out the other way round is not a
    # milestone table, and is skipped rather than misread.
    assert milestone_rows(text) == []
    text = "| # | Status | Milestone |\n|:---|:---:|---|\n| **M4** | pending | Score |\n"
    assert [(r.milestone, r.status) for r in milestone_rows(text)] == [("M4", "pending")]


def test_tables_without_a_status_column_and_prose_are_ignored() -> None:
    text = (
        "M5 is pending, says this sentence, which is not a table.\n\n"
        "| # | Milestone | AWS account needed? |\n|---|---|---|\n| M46 | Roles | No |\n\n"
        "| stage | seconds |\n| --- | --- |\n| ingest | 66.1 |\n"
    )
    assert milestone_rows(text) == []


def test_a_table_inside_a_fenced_block_is_an_example_not_a_claim() -> None:
    text = "```markdown\n| # | Status |\n|---|---|\n| M2 | pending |\n```\n"
    assert milestone_rows(text) == []


def test_rows_that_are_not_milestones_are_skipped() -> None:
    text = (
        "| # | Milestone | Status |\n|---|---|---|\n"
        "| — | Onboarding contracts | **done** |\n| M8 | Clients | pending |\n"
    )
    assert [r.milestone for r in milestone_rows(text)] == ["M8"]


def test_an_escaped_pipe_does_not_shift_the_status_column() -> None:
    text = "| # | Milestone | Status |\n|---|---|---|\n| M2 | `a \\| b` | pending |\n"
    assert milestone_rows(text)[0].status == "pending"


# --- reading the report ---------------------------------------------------------------------


def test_junit_outcomes_are_read_from_their_child_elements(tmp_path: Path) -> None:
    got = cases_of(
        tmp_path,
        ("tests.unit.test_two", "passed"),
        ("tests.unit.test_two.TestClass", "failed"),
        ("tests.unit.test_two", "error"),
        ("tests.unit.test_two", "skipped"),
    )
    assert [outcome for _, outcome in got] == ["passed", "failed", "failed", "skipped"]


def test_tests_are_matched_to_files_and_directories_on_module_boundaries(tmp_path: Path) -> None:
    got = cases_of(
        tmp_path,
        ("tests.unit.test_two", "passed"),
        ("tests.unit.test_two.TestClass", "passed"),
        ("tests.unit.test_two_more", "failed"),  # a different file that shares a prefix
        ("tests.integration.test_api", "passed"),
        ("tests.integration_extra.test_api", "failed"),
    )
    assert check_readme.evidence_for(MAPPING["M2"], got) == Evidence(passed=3, failed=0, skipped=0)


def test_an_unreadable_report_is_an_error_not_a_pass(tmp_path: Path) -> None:
    broken = tmp_path / "junit.xml"
    broken.write_text("<testsuites><testcase", encoding="utf-8")
    with pytest.raises(CheckError, match="Could not read the test report"):
        check_readme.read_junit(broken)
    with pytest.raises(CheckError, match="Could not read the test report"):
        check_readme.read_junit(tmp_path / "absent.xml")


# --- the verdict ----------------------------------------------------------------------------


def test_a_pending_milestone_with_passing_tests_fails_and_names_the_line(tmp_path: Path) -> None:
    cases = cases_of(tmp_path, ("tests.unit.test_two", "passed"), ("tests.unit.test_one", "passed"))
    lines, code = verdict(milestone_rows(TABLE), cases, MAPPING, readme="README.md")
    assert code == 1
    assert any(line.startswith("README.md:6: M2 is marked 'pending'") for line in lines)
    assert lines[-1] == "3 milestone rows, 1 pending, 1 wrongly pending."


@pytest.mark.parametrize(
    ("outcomes", "why"),
    [
        ((), "no test ran"),
        ((("tests.unit.test_two", "skipped"),), "a skip is not evidence"),
        ((("tests.unit.test_two", "passed"), ("tests.integration.test_api", "failed")), "one failed"),
    ],
)
def test_pending_stays_allowed_without_evidence_that_it_is_built(
    tmp_path: Path, outcomes: tuple[tuple[str, str], ...], why: str
) -> None:
    cases = cases_of(tmp_path, *outcomes)
    lines, code = verdict(milestone_rows(TABLE), cases, MAPPING, readme="README.md")
    assert code == 0, why
    assert lines[-1] == "README honest: 3 milestone rows, 1 pending, none with passing tests."


def test_a_pending_milestone_with_no_mapping_is_reported_never_flagged(tmp_path: Path) -> None:
    cases = cases_of(tmp_path, ("tests.unit.test_two", "passed"))
    lines, code = verdict(milestone_rows(TABLE), cases, {"M1": MAPPING["M1"]}, readme="README.md")
    assert code == 0
    assert "README.md:6: M2 is pending; no tests are mapped to it (ok)." in lines


def test_done_and_partial_milestones_are_not_judged_even_with_failing_tests(tmp_path: Path) -> None:
    # One-sided on purpose: the check can say a "pending" is false, never that a "done" is true.
    cases = cases_of(tmp_path, ("tests.unit.test_one", "failed"), ("tests.unit.test_three", "failed"))
    _, code = verdict(milestone_rows(TABLE), cases, MAPPING, readme="README.md")
    assert code == 0


# --- the command ----------------------------------------------------------------------------


def test_main_exits_one_on_a_dishonest_readme_given_a_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(check_readme, "MILESTONE_TESTS", MAPPING)
    monkeypatch.setattr(check_readme, "missing_paths", lambda mapping, root: [])
    readme = tmp_path / "README.md"
    readme.write_text(TABLE, encoding="utf-8")
    report = tmp_path / "junit.xml"
    report.write_text(junit(("tests.unit.test_two", "passed")), encoding="utf-8")
    assert check_readme.main(["--readme", str(readme), "--junit", str(report)]) == 1
    assert "M2 is marked 'pending'" in capsys.readouterr().err


def test_main_exits_two_when_the_readme_has_no_milestone_table(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    readme = tmp_path / "README.md"
    readme.write_text("# A project\n\nEverything is pending.\n", encoding="utf-8")
    assert check_readme.main(["--readme", str(readme), "--junit", str(tmp_path / "unused.xml")]) == 2
    assert "has no milestone table" in capsys.readouterr().err


def test_main_exits_two_when_the_mapping_names_a_missing_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(check_readme, "MILESTONE_TESTS", {"M2": ("tests/unit/test_renamed_away.py",)})
    readme = tmp_path / "README.md"
    readme.write_text(TABLE, encoding="utf-8")
    assert check_readme.main(["--readme", str(readme), "--junit", str(tmp_path / "unused.xml")]) == 2
    assert "M2: tests/unit/test_renamed_away.py" in capsys.readouterr().err


def test_without_a_report_only_the_pending_milestones_tests_are_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[list[str]] = []

    def fake_run(paths: list[str], repo_root: Path, report: Path) -> None:
        ran.append(list(paths))
        report.write_text(junit(("tests.unit.test_two", "passed")), encoding="utf-8")

    monkeypatch.setattr(check_readme, "MILESTONE_TESTS", MAPPING)
    monkeypatch.setattr(check_readme, "missing_paths", lambda mapping, root: [])
    monkeypatch.setattr(check_readme, "run_pytest", fake_run)
    readme = tmp_path / "README.md"
    readme.write_text(TABLE, encoding="utf-8")
    _, code = check_readme.check(readme, None, tmp_path)
    assert ran == [["tests/integration/", "tests/unit/test_two.py"]]
    assert code == 1

    # An honest README with nothing pending runs nothing at all.
    readme.write_text(TABLE.replace("| pending |", "| **done** |"), encoding="utf-8")
    ran.clear()
    _, code = check_readme.check(readme, None, tmp_path)
    assert (ran, code) == ([], 0)


# --- the mapping itself ---------------------------------------------------------------------


def test_every_mapped_path_exists(repo_root: Path) -> None:
    assert check_readme.missing_paths(check_readme.MILESTONE_TESTS, repo_root) == []


def test_every_mapped_key_is_a_milestone_id() -> None:
    assert all(check_readme.MILESTONE_ID.match(key) for key in check_readme.MILESTONE_TESTS)


def test_m13_is_never_proved_by_the_standalone_panel_tests_alone() -> None:
    """M13's definition of done is the onboarding panel mounted inside Setup (Plan A M35).

    The standalone panel's tests pass whether or not Setup mounts it, so a mapping made only of them
    would turn M13's truthful "pending" into a demand to mark it done. M13 stays unmapped until M35
    merges and adds tests that fail while the panel is not mounted.
    """
    standalone = {"tests/unit/test_onboarding_ui.py", "tests/integration/test_onboarding_flow.py"}
    mapped = check_readme.MILESTONE_TESTS.get("M13")
    assert mapped is None or not set(mapped) <= standalone
