"""The summary tables and the command line (fixture mode: no download)."""

from __future__ import annotations

import json
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import numpy as np

from autonomy.gym.waves.report import paired_difference, render_markdown, summarize
from scripts.v2_realbench import waves as cli


def row(world: str, policy: str, seed: int, value: float, **extra: Any) -> dict[str, Any]:
    base = {
        "world": world,
        "policy": policy,
        "seed": seed,
        "customers": 100,
        "true_value": value,
        "contacts_per_customer_week": 1.0,
        "share_contacted": 0.5,
        "unsubscribe_rate": 0.01,
        "measured": value * 2,
        "seconds": 1.0,
    }
    return {**base, **extra}


def test_paired_difference_uses_the_per_customer_gaps() -> None:
    rng = np.random.default_rng(0)
    base = rng.normal(0, 1, 500)
    a = {0: base + 0.5, 1: base + 0.5}
    b = {0: base, 1: base}
    diff, se, seeds = paired_difference(a, b)
    assert seeds == 2 and abs(diff - 250.0) < 1e-9
    assert se is not None and se < 1e-9  # a constant gap has no noise once the luck is shared
    noisy = {0: base + rng.normal(0.5, 1, 500)}
    diff, se, _ = paired_difference(noisy, {0: base})
    assert se is not None and se > 5
    # seeds share their customers: repeating the same per-customer gaps in three seeds is no narrower than one
    _, se3, _ = paired_difference(dict.fromkeys(range(3), noisy[0]), dict.fromkeys(range(3), base))
    assert se3 is not None and abs(se3 - se) < 1e-9
    # and the spread between worlds adds to it
    worlds = {s: base + 0.5 + 0.2 * s for s in range(3)}
    _, se_w, _ = paired_difference(worlds, dict.fromkeys(range(3), base))
    assert se_w is not None and se_w > 50


def test_summary_ranks_against_the_oracle_the_agent_and_the_best_simple_policy() -> None:
    rows = []
    values = {
        "send_none": 0.0,
        "send_all_sms": 40.0,
        "weekly_sms": 55.0,
        "agent": 60.0,
        "uplift_waves": 70.0,
        "oracle": 100.0,
    }
    for seed in (0, 1):
        for policy, v in values.items():
            rows.append(row("heterogeneous", policy, seed, v + seed))
    out = summarize(rows)
    block = out["heterogeneous"]
    assert block["best_simple"] == "weekly_sms"
    by = {e["policy"]: e for e in block["policies"]}
    assert abs(by["agent"]["oracle_share"] - 0.6) < 0.01
    assert abs(by["uplift_waves"]["vs_agent"]["diff"] - 10.0) < 1e-9
    assert abs(by["uplift_waves"]["vs_best_simple"]["diff"] - 15.0) < 1e-9
    assert by["agent"]["vs_agent"] is None
    text = render_markdown(out)
    assert (
        "### heterogeneous" in text
        and "| uplift_waves |" in text
        and text.index("| oracle |") > text.index("| agent |")
    )
    assert "84 decision days" in text and "(sd " in text


def test_the_table_says_how_many_days_ran_and_lists_the_refits() -> None:
    fits = [{"day": 21, "chose": "rule:send to none", "lb": -12.5, "model_contact": 1.0}]
    rows = [
        row("heterogeneous", "send_none", 0, 0.0, days=28),
        row("heterogeneous", "uplift_waves", 0, 5.0, days=28, policy_diagnostics={"fits": fits}),
    ]
    text = render_markdown(summarize(rows))
    assert "28 decision days" in text and "84 decision days" not in text
    assert "`uplift_waves` seed 0 refits: day 21: rule:send to none (lb -12.5, model contacts 1)" in text


def test_the_summary_counts_a_run_found_twice_once(tmp_path: Path, capsys: Any) -> None:
    report = {"benchmark": "waves", "runs": [row("null", "send_none", 0, 0.0), row("null", "agent", 0, -3.0)]}
    for folder in ("a", "b"):  # a re-run's artifacts hold copies of the same runs
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "part.json").write_text(json.dumps(report))
    assert cli.main(["--summarize", str(tmp_path)]) == 0
    text = capsys.readouterr().out
    assert "seeds [0]" in text and text.count("| agent |") == 1


def test_the_ci_matrix_names_only_known_worlds_and_policies() -> None:
    import yaml

    from autonomy.gym.waves.config import WORLDS
    from autonomy.gym.waves.policies import policy_names

    path = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "waves.yml"
    workflow = yaml.safe_load(path.read_text())
    matrix = workflow["jobs"]["waves"]["strategy"]["matrix"]
    assert matrix["world"] and all(isinstance(w, str) and w in WORLDS for w in matrix["world"])
    assert set(matrix["world"]) == set(WORLDS)
    assert all(isinstance(p, str) and p in policy_names() for p in matrix["policy"])
    assert all(isinstance(s, int) for s in matrix["seed"])
    summary = workflow["jobs"]["summary"]["steps"]
    pattern = next(s["with"]["pattern"] for s in summary if "download-artifact" in str(s.get("uses")))
    names = [s["with"]["name"] for s in summary if "upload-artifact" in str(s.get("uses"))]
    assert all(not fnmatch(n, pattern) for n in names)


def test_the_fixture_command_runs_end_to_end_and_the_summary_reads_its_files(
    tmp_path: Path, capsys: Any
) -> None:
    out = tmp_path / "parts" / "null-send_all_sms.json"
    code = cli.main(
        [
            "--fixture",
            "--world",
            "null",
            "--policy",
            "send_none,send_all_sms",
            "--seeds",
            "2",
            "--customers",
            "200",
            "--days",
            "14",
            "--json",
            str(out),
        ]
    )
    assert code == 0
    report = json.loads(out.read_text())
    assert report["benchmark"] == "waves" and report["data"]["source"] == "fixture"
    assert len(report["runs"]) == 4 and out.with_suffix(".npz").exists()
    shown = capsys.readouterr().out
    assert "### null" in shown
    assert cli.main(["--summarize", str(tmp_path / "parts")]) == 0
    assert "### null" in capsys.readouterr().out


def test_the_command_refuses_unknown_names(capsys: Any) -> None:
    import pytest

    with pytest.raises(SystemExit):
        cli.main(["--fixture", "--policy", "no_such_policy"])
    assert cli.main(["--list"]) == 0
    assert "heterogeneous" in capsys.readouterr().out


def test_the_x5_loader_reads_the_files_the_bench_downloads(tmp_path: Path, tables: Any) -> None:
    """The same loader CI uses, on files of X5's layout written from the fixture (no download)."""
    from scripts.v2_realbench.common import Timings

    receipts, clients, _cutoff = tables
    root = tmp_path / "x5_retailhero"
    root.mkdir()
    lines = receipts.assign(regular_points_spent=0.0, express_points_spent=0.0)
    lines.to_csv(root / "purchases.csv.gz", index=False)
    clients.assign(first_redeem_date=None).to_csv(root / "clients.csv.gz", index=False)
    late = clients["client_id"].iloc[:7].tolist()
    clients = clients.assign(
        first_issue_date=[
            "2019-01-10 10:00:00" if c in late else d
            for c, d in zip(clients["client_id"], clients["first_issue_date"], strict=True)
        ]
    )
    clients.assign(first_redeem_date=None).to_csv(root / "clients.csv.gz", index=False)
    data = cli.load_x5(tmp_path, 120, Timings(quiet=True))
    assert data.n == 120 and len(data.post_idx) > 0
    assert not set(late) & set(data.customer_ids)  # issued after the cutoff: not customers yet
    assert data.eligibility["dropped_issued_after_cutoff"] == 7
    assert cli.describe(data, "x5")["eligibility"]["sampled"] == 120
    assert data.start.date().isoformat() == "2018-12-25"
    assert float(data.features["n_receipts"].sum()) == len(data.hist_idx)
    assert cli.load_x5(tmp_path, 120, Timings(quiet=True)).customer_ids == data.customer_ids
