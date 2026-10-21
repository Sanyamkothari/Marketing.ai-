"""docs/v2/TARGETS.json: well formed, covers what it promises, and judged mechanically."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from autonomy.gym.run import main as gym_main
from autonomy.gym.scenarios import PLANTED_HARM, SCENARIOS
from scripts.v2_realbench.adapters import DATASETS
from scripts.v2_realbench.common import add_multiplicity
from scripts.v2_realbench.sealed import SEALED_DATASETS
from scripts.v2_realbench.targets import DEFAULT_TARGETS, KEYS, judge, load_targets, main, validate
from tests.v2.scientist.test_testing_holm import synthetic_report

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def targets() -> list[dict[str, Any]]:
    return load_targets(DEFAULT_TARGETS)


def by_id(targets: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {t["id"]: t for t in targets}


def test_the_file_is_well_formed(targets: list[dict[str, Any]]) -> None:
    assert DEFAULT_TARGETS == REPO / "docs" / "v2" / "TARGETS.json"
    assert validate(targets) == []
    assert isinstance(json.loads(DEFAULT_TARGETS.read_text()), list)
    for t in targets:
        assert tuple(t) == KEYS == ("id", "claim", "command", "threshold", "passes")
        assert t["passes"] in (None, True, False)


PREREGISTERED = "f54d2454bf6b761a8b8f50c7d66923663222c718849c4ffc51390479eec62f6f"
"""SHA-256 of every claim's (id, claim, command, threshold), fixed when the file was written, before any
command was run for it. `passes` is the only field a run may change; editing a claim, a command or a
threshold after seeing a result changes this and fails the test."""


def test_only_the_results_changed_since_the_claims_were_written(targets: list[dict[str, Any]]) -> None:
    payload = json.dumps(
        [[t["id"], t["claim"], t["command"], t["threshold"]] for t in targets],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    assert hashlib.sha256(payload.encode()).hexdigest() == PREREGISTERED


def test_ids_are_unique(targets: list[dict[str, Any]]) -> None:
    ids = [t["id"] for t in targets]
    assert len(ids) == len(set(ids))


def test_the_bench_is_covered_per_dev_dataset(targets: list[dict[str, Any]]) -> None:
    ids = by_id(targets)
    assert set(DATASETS) == {"hillstrom", "x5", "criteo", "megafon"}
    for ds in DATASETS:
        non_inferior = ids[f"bench.{ds}.non_inferior"]
        assert (
            f"datasets.{ds}.verdict_corrected.non_inferior_to_best_simple == true"
            in non_inferior["threshold"]
        )
        assert "2%" in non_inferior["claim"] and "Holm" in non_inferior["claim"]
        cal = ids[f"bench.{ds}.calibration"]
        assert f"datasets.{ds}.calibration.within_2x == true" in cal["threshold"]
    assert "beats_best_simple_where_signal == true" in ids["bench.megafon.beats_best_simple"]["threshold"]
    for ds in set(DATASETS) - {"megafon"}:
        assert "beats_best_simple_where_signal != false" in ids[f"bench.{ds}.beats_where_signal"]["threshold"]
    for t in targets:
        if t["id"].startswith("bench."):
            assert "--dataset all" in t["command"] and "--sealed-final" not in t["command"]


def test_the_sealed_check_is_one_run_per_sealed_dataset(targets: list[dict[str, Any]]) -> None:
    ids = by_id(targets)
    for ds in SEALED_DATASETS:
        for kind in ("non_inferior", "calibration"):
            t = ids[f"sealed.{ds}.{kind}"]
            assert f"--dataset {ds} " in t["command"] and "--sealed-final" in t["command"]
            assert f"out/sealed_{ds}.json" in t["threshold"]
    sealed_ids = [t for t in targets if "--sealed-final" in t["command"]]
    assert len(sealed_ids) == 2 * len(SEALED_DATASETS)
    # a dev claim never touches sealed data
    assert not [
        t
        for t in targets
        if not t["id"].startswith("sealed.") and any(s in t["command"] for s in SEALED_DATASETS)
    ]


def test_the_gym_is_covered_per_scenario_and_stage(targets: list[dict[str, Any]]) -> None:
    ids = by_id(targets)
    for name in SCENARIOS:
        if name == "smoke":
            assert not [t for t in targets if t["id"].startswith("gym.smoke.")]
            continue
        top = 2 if (name in PLANTED_HARM or name == "null_effect") else 3
        for k in range(1, 4):
            key = f"gym.{name}.stage{k}"
            if k <= top:
                t = ids[key]
                assert t["threshold"] == f"out/gym/{name}.json :: verdict.stage_reached >= {k}"
                assert f"--scenario {name} " in t["command"] and "--customers 3000" in t["command"]
                assert "--seeds 3" in t["command"]
            else:
                assert key not in ids
    for name in ("real_email_like", "real_sms_like"):
        assert f"gym.{name}.stage1" in ids and f"gym.{name}.stage3" in ids


def test_the_commands_are_runnable_forms(targets: list[dict[str, Any]]) -> None:
    for t in targets:
        assert t["command"].startswith(("python -m ", "timeout "))


# --- the mechanical judge ------------------------------------------------------------------------------


def test_the_bench_paths_exist_in_a_real_report_shape(targets: list[dict[str, Any]], tmp_path: Path) -> None:
    rep = synthetic_report()
    add_multiplicity(rep)
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "real_bench.json").write_text(json.dumps(rep))
    # d1/d2 stand in for datasets: take the bench claim shapes and point them at d1
    t = {
        "threshold": "out/real_bench.json :: datasets.d1.verdict_corrected.non_inferior_to_best_simple == true"
    }
    assert judge(t["threshold"], tmp_path) is True
    assert judge("out/real_bench.json :: datasets.d1.calibration.within_2x == true", tmp_path) is True
    assert judge("out/real_bench.json :: datasets.d2.calibration.within_2x == true", tmp_path) is False
    assert (
        judge(
            "out/real_bench.json :: datasets.d1.verdict_corrected.beats_best_simple_where_signal != false",
            tmp_path,
        )
        is True
    )
    assert (
        judge(
            "out/real_bench.json :: datasets.d2.verdict_corrected.beats_best_simple_where_signal == true",
            tmp_path,
        )
        is True
    )
    # a path the report does not hold is a failure, not a pass
    assert judge("out/real_bench.json :: datasets.d1.verdict_corrected.nope == true", tmp_path) is False
    # a missing report is "not run"; a human rule is not judged
    assert judge("out/missing.json :: a.b == true", tmp_path) is None
    assert judge("exit status 0", tmp_path) is None
    # every bench claim in the file points at a field the report builder writes
    for tt in targets:
        if tt["id"].startswith(("bench.", "sealed.")):
            field = tt["threshold"].split("::")[1].split()[0].split(".")
            assert field[0] == "datasets" and field[2] in ("verdict_corrected", "calibration")
            assert field[3] in rep["datasets"]["d1"][field[2]]


def test_numeric_thresholds_and_gym_reports(tmp_path: Path) -> None:
    (tmp_path / "g.json").write_text(json.dumps({"verdict": {"stage_reached": 2, "stage_name": "Useful"}}))
    assert judge("g.json :: verdict.stage_reached >= 2", tmp_path) is True
    assert judge("g.json :: verdict.stage_reached >= 3", tmp_path) is False
    assert judge("g.json :: verdict.stage_reached < 3", tmp_path) is True
    assert judge("g.json :: verdict.stage_reached == 2", tmp_path) is True


def test_validate_catches_bad_claims() -> None:
    good = {"id": "a", "claim": "c", "command": "python -m x", "threshold": "t", "passes": None}
    assert validate([good]) == []
    assert validate([good, dict(good)])  # duplicate id
    assert validate([{**good, "passes": "yes"}])
    assert validate([{"id": "a", "claim": "c", "command": "x", "threshold": "t"}])  # missing key
    assert validate([{**good, "threshold": "file :: path ~~ 3"}])  # unparseable rule
    assert validate([{**good, "claim": ""}])


def test_the_cli_validates_and_writes_results(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    claims = [
        {"id": "x.one", "claim": "one", "command": "c", "threshold": "r.json :: a.b >= 1", "passes": None},
        {"id": "x.two", "claim": "two", "command": "c", "threshold": "r.json :: a.b >= 5", "passes": None},
        {
            "id": "x.three",
            "claim": "three",
            "command": "c",
            "threshold": "gone.json :: a.b >= 5",
            "passes": None,
        },
        {"id": "x.four", "claim": "four", "command": "c", "threshold": "exit status 0", "passes": None},
    ]
    path = tmp_path / "T.json"
    path.write_text(json.dumps(claims))
    (tmp_path / "r.json").write_text(json.dumps({"a": {"b": 2}}))
    assert main(["--targets", str(path)]) == 0
    assert "[not run] x.one" in capsys.readouterr().out
    assert main(["--targets", str(path), "--base-dir", str(tmp_path), "--write"]) == 0
    out = capsys.readouterr().out
    assert "[PASS] x.one" in out and "[FAIL] x.two" in out and "[not run] x.three" in out
    written = {t["id"]: t["passes"] for t in json.loads(path.read_text())}
    assert written == {"x.one": True, "x.two": False, "x.three": None, "x.four": None}
    path.write_text(json.dumps([{"id": "bad"}]))
    assert main(["--targets", str(path)]) == 1


def test_a_gym_report_has_the_verdict_field_the_claims_read(tmp_path: Path) -> None:
    """Run the Gym CLI on the tiny `smoke` scenario and read `verdict.stage_reached` as the claims do."""
    out = tmp_path / "gym" / "smoke.json"
    code = gym_main(
        [
            "--scenario",
            "smoke",
            "--policy",
            "nothing",
            "--seeds",
            "1",
            "--customers",
            "150",
            "--days",
            "6",
            "--report-json",
            str(out),
        ]
    )
    assert code == 0
    assert judge("gym/smoke.json :: verdict.stage_reached >= 0", tmp_path) is True
    report = json.loads(out.read_text())
    assert isinstance(report["verdict"]["stage_reached"], int)
