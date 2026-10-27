"""The summary tables and the command line (fixture mode: no download)."""

from __future__ import annotations

import json
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
    data = cli.load_x5(tmp_path, 120, Timings(quiet=True))
    assert data.n == 120 and len(data.post_idx) > 0
    assert data.start.date().isoformat() == "2018-12-25"
    assert float(data.features["n_receipts"].sum()) == len(data.hist_idx)
    assert cli.load_x5(tmp_path, 120, Timings(quiet=True)).customer_ids == data.customer_ids
