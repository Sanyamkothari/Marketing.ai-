"""The trap bench (`scripts/doctor_trap_bench.py`): the cases are sound, the grader has teeth, and a run reports.

The fast tests do not run the Doctor except for two cases (the smoke test). What they pin:

* the generator writes the same files every time, and the files on disk are what it writes;
* the generator imports nothing from the product (the answer key is not tuned to it);
* every case names its scenario, theme and expected result, and the set covers the twelve themes;
* the ideal result of every case passes its own checks (a check no correct answer could pass is a bad check), both
  with the question the ideal Doctor asks and, where the check has a data alternative, with the data alone;
* a Doctor that does nothing fails most traps (the checks are not vacuous).

The bench itself measures: many traps fail today and that is not a failure of these tests. The whole bench (every
case, k = 2) is `slow`.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from scripts import doctor_trap_bench as bench
from tests.fixtures.doctor_traps import generate

CASES = bench.load_cases()
IDS = [c.case_id for c in CASES]
REQUIRED = {"K1", "V19", "S05", "T06", "F01", "C06", "S01", "T07", "F21", "crit-03"}
SCENARIO = re.compile(r"^(?:[A-Z]{1,2}\d{1,2}|crit-\d\d)$")


def test_the_generator_is_deterministic_and_the_files_are_what_it_writes() -> None:
    first, second = generate.all_files(), generate.all_files()
    assert first == second
    assert generate.main(["--check"]) == 0


def test_the_generator_imports_nothing_from_the_product() -> None:
    tree = ast.parse(Path(generate.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= {
        "__future__", "argparse", "csv", "io", "json", "random", "sys", "zlib",
        "collections", "dataclasses", "datetime", "pathlib", "typing",
    }, sorted(imported)  # fmt: skip


def test_there_are_twenty_to_thirty_cases_covering_the_planted_scenarios() -> None:
    assert 20 <= len(CASES) <= 30
    assert {c.scenario for c in CASES} >= REQUIRED
    assert len(set(IDS)) == len(IDS)
    assert len({c.theme for c in CASES}) == len(generate.THEMES)


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_every_case_is_described_and_has_an_expected_result(case: bench.TrapCase) -> None:
    assert SCENARIO.match(case.scenario), case.scenario
    assert "\n" not in case.description and 20 < len(case.description) < 260
    assert case.expected and "\n" not in case.expected
    assert case.checks, "a case without checks grades nothing"
    for spec in case.checks:
        assert spec["category"] in bench.CATEGORIES and spec["kind"] in bench.CHECKS and spec["label"]
    for table in case.files:
        assert (case.path / case.files[table]).stat().st_size < 20_000, "cases stay small"


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_the_ideal_result_passes_every_check(case: bench.TrapCase) -> None:
    graded = bench.grade(case, bench.ideal_shipped(case))
    failed = [f"{c['label']}: {c['detail']}" for c in graded if not c["passed"]]
    assert not failed, failed


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_the_data_alone_can_meet_a_check_that_offers_it(case: bench.TrapCase) -> None:
    """Where a check says 'asked, or the data is right', the data alternative is reachable without asking."""
    graded = bench.grade(case, bench.ideal_shipped(case, asking=False))
    bare = {c["label"] for c in case.checks if c["kind"] == "asked"}
    failed = [f"{c['label']}: {c['detail']}" for c in graded if not c["passed"] and c["label"] not in bare]
    assert not failed, failed


def test_a_doctor_that_does_nothing_fails_most_traps() -> None:
    failing = [
        c.case_id for c in CASES if not all(r["passed"] for r in bench.grade(c, bench.untouched_shipped(c)))
    ]
    assert len(failing) >= len(CASES) * 0.6, f"only {len(failing)} of {len(CASES)} traps catch doing nothing"


def test_the_checks_have_teeth_one_by_one() -> None:
    """A table with a row gone, a customer repeated and a leak column kept is caught by the matching check."""
    case = next(c for c in CASES if c.case_id == "t06_status_exported_today")
    grader = bench.Grader(case, bench.untouched_shipped(case))
    assert not bench.check_columns_absent(grader, {"columns": ["is_active"]}).passed
    frame = grader.final()
    assert frame is not None
    grader.shipped.final = frame.iloc[1:]
    assert not bench.check_keep_all_rows(grader, {}).passed
    grader.shipped.final = pd.concat([frame, frame.iloc[:1]], ignore_index=True)
    assert not bench.check_one_row_per_key(grader, {}).passed
    assert bench.check_row_count(grader, {"rows": len(frame) + 1}).passed


def test_a_dropped_column_or_a_recoded_rule_column_does_not_hide_a_trap() -> None:
    """Removing a column nobody approved is caught; recoding Y/N to 1/0 does not hide who cannot have the outcome."""
    case = next(c for c in CASES if c.case_id == "c09_already_hold_product")
    grader = bench.Grader(case, bench.untouched_shipped(case))
    frame = grader.final()
    assert frame is not None
    grader.shipped.final = frame.drop(columns=["age"])
    assert not bench.check_untouched_columns(grader, {}).passed
    grader.shipped.final = frame.assign(
        has_vehicle_policy=frame["has_vehicle_policy"].map({"Y": "1", "N": "0"})
    )
    grader.shipped.outcome_column = "Response"
    rule = {"exclude_where": [{"column": "has_vehicle_policy", "values": ["Y"]}]}
    verdict = bench.check_outcome_population(grader, rule)
    assert not verdict.passed and "cannot have the outcome" in verdict.detail


def test_a_case_file_that_cannot_be_read_is_refused(tmp_path: Path) -> None:
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "case.json").write_text(json.dumps({"id": "x"}), encoding="utf-8")
    with pytest.raises(bench.BenchError):
        bench.load_cases(tmp_path)
    with pytest.raises(bench.BenchError, match="reserved"):
        bench.run_bench([], 1, model="product")


def test_smoke_two_cases_through_the_visit_report_and_agree_with_themselves(tmp_path: Path) -> None:
    """The real thing, small: a consent trap and a stop-and-ask trap, twice each, through the unified visit."""
    cases = [c for c in CASES if c.case_id in {"c06_dnd_direction", "f05_pivot_summary"}]
    assert len(cases) == 2
    report = bench.run_bench(cases, 2)
    assert report["cases"] == 2 and report["k"] == 2 and report["passed"] + report["failed"] == 2
    for result in report["results"]:
        assert len(result["runs"]) == 2
        assert result["stable"], "the rules are deterministic: two runs hand on the same thing"
        assert result["runs"][0]["checks"] and result["runs"][0]["questions"]
    md, js = bench.write_report(report, tmp_path / "bench")
    assert "c06_dnd_direction" in md.read_text(encoding="utf-8")
    assert json.loads(js.read_text(encoding="utf-8"))["bench"] == "doctor_trap_bench"


def _question(**kwargs: object) -> SimpleNamespace:
    options = [
        SimpleNamespace(option_id="a", label="First thing"),
        SimpleNamespace(option_id="b", label="Second thing"),
        SimpleNamespace(option_id="c", label="I don't know"),
    ]
    return SimpleNamespace(code="Q", options=options, recommended=None, blocking=False, **kwargs)


def test_a_careful_person_follows_the_key_then_the_recommendation_and_never_guesses_on_an_optional_question() -> (
    None
):
    case = CASES[0]
    keyed = dataclasses.replace(case, answers={"Q": "Second"})
    unknown = dataclasses.replace(case, answers={"Q": "I don't know"})
    blank = dataclasses.replace(case, answers={})
    option, why = bench._person_choice(keyed, _question()) or (None, "")
    assert option is not None and option.option_id == "b" and why == "answer key"
    option, why = bench._person_choice(unknown, _question()) or (None, "")
    assert option is not None and option.option_id == "c" and why == "dont_know"
    recommended = _question()
    recommended.recommended = "b"
    option, why = bench._person_choice(blank, recommended) or (None, "")
    assert option is not None and option.option_id == "b" and why == "recommended"
    assert (
        bench._person_choice(blank, _question()) is None
    )  # an optional question nobody can answer stays open
    blocking = _question()
    blocking.blocking = True
    option, why = bench._person_choice(blank, blocking) or (None, "")
    assert (
        option is not None and option.option_id == "a" and why.startswith("first offered")
    )  # counted as a guess


def test_a_person_ticks_only_what_the_answer_key_names() -> None:
    case = dataclasses.replace(
        CASES[0], person={"tick": [{"kind": "consent_column", "title": "says who agreed"}]}
    )
    consent = SimpleNamespace(
        kind="consent_column", title="“marketing_opt_in” says who agreed to be contacted."
    )
    leak = SimpleNamespace(kind="recipe_step", title="Remove 'x': it looks like it gives away the outcome")
    assert bench._ticks(case, consent) and not bench._ticks(case, leak)
    assert not bench._ticks(dataclasses.replace(case, person={}), consent)


def test_a_proven_leak_is_ticked_by_the_product_so_the_person_who_ticks_nothing_loses_it() -> None:
    """The leak check proves `cancellation_reason` tells yes from no: the plan pre-ticks its removal, so a person
    who ticks nothing and the tick-all driver end with the same table."""
    case = next(c for c in CASES if c.case_id == "s05_cancellation_reason")
    person = bench.run_bench([case], 1)
    ticked = bench.run_bench([case], 1, driver="tick-all")
    assert person["driver"] == "person" and person["passed"] == 1 and person["failed"] == 0
    assert ticked["driver"] == "tick-all" and ticked["passed"] == 1
    with pytest.raises(bench.BenchError, match="no driver"):
        bench.run_bench([case], 1, driver="nobody")


def test_the_report_carries_both_numbers(tmp_path: Path) -> None:
    case = next(c for c in CASES if c.case_id == "s05_cancellation_reason")
    both = bench.run_both([case], 1)
    text = bench.write_report(both, tmp_path / "both")[0].read_text(encoding="utf-8")
    assert "Person-like (the number to track): passed 1 of 1" in text
    assert "Tick-all (the old driver, flatters the product): passed 1 of 1" in text
    alone = bench.render_markdown(bench.run_bench([case], 1, driver="tick-all"))
    assert "Driver tick-all: passed 1 of 1" in alone and "Person-like (the number to track)" not in alone


@pytest.mark.slow
def test_the_whole_bench_runs_every_case_twice_and_reports(tmp_path: Path) -> None:
    """Every trap, k = 2. Passing is not asserted - the bench measures - but every run must finish and agree."""
    report = bench.run_bench(CASES, 2)
    assert report["cases"] == len(CASES)
    assert report["unstable"] == 0, [r["id"] for r in report["results"] if not r["stable"]]
    assert all(
        set(bench.CATEGORIES) >= {c["category"] for run in r["runs"] for c in run["checks"]}
        for r in report["results"]
    )
    md, _ = bench.write_report(report, tmp_path / "bench")
    text = md.read_text(encoding="utf-8")
    assert all(case_id in text for case_id in IDS)
