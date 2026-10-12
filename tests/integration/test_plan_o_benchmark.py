"""Plan O M248 acceptance (DEC-1818): `make agent-eval` - the script writes `reports/plan_o_benchmark.md`, harm metrics
are all zero, pass^5 is 100% acceptable with no harmful proposal or the kill criterion applies to that tool, and the
readiness invariant holds on every benchmark file.

All `slow`: they drive benchmark files through upload, Data Doctor, Guided setup and Approve. The first takes one
file per group; the second is the whole benchmark (Plan L's 30, the extended cases, the 20 held-out Indian files),
which CI's nightly run and `make agent-eval` take. Training each `ready` file to the end is the gate run, after
M247 (`--train`).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from engine.doctor.evals import gate_failures, kill_verdict
from scripts import plan_o_benchmark as bench


@pytest.mark.slow
def test_the_script_writes_the_report_with_every_part(tmp_path: Path) -> None:
    code = bench.main(
        [
            "--parts",
            ",".join(bench.PARTS),
            "--limit",
            "1",
            "--k",
            "2",
            "--model",
            "recorded",
            "--out",
            str(tmp_path),
        ]
    )
    text = (tmp_path / "plan_o_benchmark.md").read_text(encoding="utf-8")
    summary = json.loads((tmp_path / "plan_o_benchmark.json").read_text(encoding="utf-8"))["summary"]
    assert code == (1 if summary["gate_failures"] else 0), "the exit code is the gate's"
    for heading in (
        "## Harm metrics (each must be zero)",
        "## Tools: pass^5",
        "### Not built yet",
        "## The messy-file benchmark",
        "## The extended benchmark",
        "## The 20 held-out Indian files (reported separately; never used for tuning)",
        "## Several files",
        "## Cost per case",
        "## Golden benchmark through the unified runtime",
        "## Kill criteria (§9.3)",
    ):
        assert heading in text, heading
    assert (tmp_path / "plan_o_benchmark.json").is_file()


@pytest.mark.slow
def test_the_whole_benchmark_has_no_harm_and_holds_the_invariant() -> None:
    report = bench.run(model="recorded", k=5)
    assert gate_failures(report) == []
    assert report.harms.total == 0, report.harm_events
    for tool in report.tools:
        if not tool.status.built:
            continue
        for passk in (tool.pass_k, tool.ai_pass_k):
            if passk is not None and not passk.passed:
                assert kill_verdict(tool).startswith("pass^5 below 100%"), (tool.status.name, passk.problems)
    ran = [f for f in report.files if f.ran]
    assert [f.name for f in ran if f.invariant_problem] == []
    plan_l = [f for f in ran if f.group == "plan_l"]
    assert len(plan_l) == 30 and all(
        f.approved for f in plan_l
    ), "P1: 30 of 30 reach Approve through Data Doctor"
    assert sum(f.questions for f in plan_l) / 30 <= 0.33 + 1e-9
    assert len([f for f in report.files if f.group == "heldout"]) == 20
