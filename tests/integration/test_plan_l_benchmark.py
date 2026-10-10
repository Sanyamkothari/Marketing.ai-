"""Plan L M160: the messy-file benchmark (`scripts/plan_l_benchmark.py`).

The fast tests drive two files, the plainest and the messiest of one use case, through upload, Data Doctor,
the hand-off and Guided setup to Approve with no person; the slow one runs all thirty and holds plan §11's
targets: no file fails at upload, at least 80% reach an approved run without Manual setup, and at most three
questions are asked per file on average.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.plan_l_benchmark import cases, file_bytes, report, run, summary


def test_thirty_files_five_use_cases_six_mixes() -> None:
    every = cases()
    assert len(every) == 30
    assert len({case.name for case in every}) == 30
    assert {case.use_case for case in every} == {
        "targeted-advertisement",
        "payment-propensity",
        "order-fulfillment",
        "fault-prediction",
        "rca",
    }
    assert sum(1 for case in every if case.fmt == "xlsx") == 15


def test_the_messiest_file_really_is_messy(config_root: Path) -> None:
    messiest = cases()[5]
    name, data, _kind = file_bytes(messiest, config_root)
    assert name.endswith(".xlsx") and data[:2] == b"PK"
    plain = cases()[0]
    name, data, _kind = file_bytes(plain, config_root)
    assert name.endswith(".csv") and data.splitlines()[0].count(b",") > 3


def test_the_plainest_and_the_messiest_file_reach_an_approved_run(config_root: Path) -> None:
    outcomes = run((cases()[0], cases()[5]), config_root)
    for outcome in outcomes:
        assert outcome.uploaded, outcome
        assert outcome.doctor_status in {"ready", "check"}, outcome
        assert outcome.applied, outcome
        assert outcome.questions <= 3, outcome
    assert outcomes[1].intake_notes > outcomes[0].intake_notes, "intake said what it did to the messy file"
    text = report(outcomes)
    assert "| Files that reach an approved run without Manual setup | ≥ 80% | 2 of 2 (100%) |" in text


@pytest.mark.slow
def test_all_thirty_files_meet_plan_targets(config_root: Path) -> None:
    measured = summary(run(None, config_root))
    assert measured["failed_at_upload"] == 0
    assert measured["share_reaching_approved_run"] >= 0.8, measured
    assert measured["questions_per_file"] <= 3, measured
