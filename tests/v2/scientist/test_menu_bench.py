"""`--policy scientist` on an inline synthetic trial: selection on TRAIN only, scored on TEST."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from autonomy.contracts import Event
from scripts.v2_realbench import adapters, common
from scripts.v2_realbench.run import main
from scripts.v2_realbench.scientist import POLICY_NAME, campaign_of, evaluate_scientist, render_scientist


def synthetic(n: int = 2400, seed: int = 4) -> tuple[common.BenchData, list[list[str]]]:
    """A 50/50 trial where the message adds conversions only where x == 1 (targeting pays)."""
    rng = np.random.default_rng(seed)
    ids = np.array([f"s{i}" for i in range(n)], dtype=object)
    x = rng.integers(0, 2, n)
    arm = rng.integers(0, 2, n).astype(np.int64)
    conv = (rng.random(n) < 0.05 + 0.3 * arm * x).astype(float)
    feats = pd.DataFrame({"x": x.astype(float), "z": rng.normal(size=n)})
    built = adapters._feature_rct(
        "synthetic", ids, arm, 0.5, feats, conv, None, (("x",), ("z",)), ("inline fixture",), {"rows": n}
    )
    calls: list[list[str]] = []
    inner = built.agent.campaign_events

    def recording(sel: Sequence[str]) -> dict[str, list[Event]]:
        calls.append(list(sel))
        return inner(sel)

    built.agent.campaign_events = recording
    return built, calls


def test_campaign_of_carries_the_bench_rows() -> None:
    data, _ = synthetic(600)
    c = campaign_of(data)
    assert c.n == 600 and c.identified and c.arm_names == tuple(data.arm_names)
    assert np.allclose(c.reward(), data.profit())
    assert list(c.features.columns) == list(data.features.columns)


def test_end_to_end_with_the_real_agent_selects_on_train_and_scores_on_test() -> None:
    data, calls = synthetic()
    out = evaluate_scientist(data, seeds=1, timings=common.Timings(quiet=True))
    train, test = common.split_by_hash(data.customer_ids)
    train_ids = set(data.customer_ids[train].tolist())
    test_ids = set(data.customer_ids[test].tolist())
    # the agent was warm-started twice (inside the selection, then for TEST) and never on a TEST customer
    assert len(calls) == 2
    assert all(set(call) <= train_ids for call in calls) and not any(set(call) & test_ids for call in calls)
    assert len(calls[0]) < len(calls[1]) == len(train)  # the inner fit part, then all of TRAIN
    sel = out["selection"]
    assert sel["split"]["fit"] + sel["split"]["held_out"] == len(train)
    assert [v["kind"] for v in sel["verdicts"]] == ["segment", "uplift", "bandit"]
    assert out["test"] == len(test) and out["picked"]["outcome"] in (
        "candidate",
        "default",
        "not_enough_data",
    )
    pol = out["policies"]
    assert POLICY_NAME in pol and "AGENT" in pol and "send to none" in pol
    assert pol[POLICY_NAME]["dr"]["low"] <= pol[POLICY_NAME]["dr"]["point"] <= pol[POLICY_NAME]["dr"]["high"]
    assert out["best_simple"]["name"] in pol
    assert (
        "scientist_minus_best_simple" in out["best_simple"]
        and "non_inferior_to_best_simple" in out["verdict"]
    )
    assert out["multiplicity"]["m"] == len(pol) - 1
    json.dumps(out, default=str)  # a report is JSON
    md = render_scientist({"datasets": {"synthetic": out}})
    assert "| synthetic |" in md and "the Scientist's selection" in md


def test_the_selection_cannot_see_the_test_outcomes() -> None:
    data, _ = synthetic(3000, seed=9)
    _, test = common.split_by_hash(data.customer_ids)
    conversion = data.conversion.copy()
    margin = data.margin_value.copy()
    conversion[test] = 1.0 - conversion[test]  # every TEST outcome flipped
    margin[test] = 20.0 * conversion[test]
    poisoned = dataclasses.replace(data, conversion=conversion, margin_value=margin)
    a = evaluate_scientist(data, seeds=1, timings=common.Timings(quiet=True), with_agent=False)
    b = evaluate_scientist(poisoned, seeds=1, timings=common.Timings(quiet=True), with_agent=False)
    assert a["selection"] == b["selection"]
    assert a["picked"] == b["picked"]
    assert a["policies"][POLICY_NAME]["dr"] != b["policies"][POLICY_NAME]["dr"]  # but the scoring did change


def test_the_cli_keeps_the_seal_for_the_scientist_and_renders_a_saved_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # the seal is the same flag for every policy: no flag, no read; a dev set cannot be opened as sealed
    assert main(["--dataset", "lenta", "--policy", "scientist", "--data-dir", "/nonexistent"]) == 2
    assert main(["--dataset", "starbucks", "--policy", "scientist", "--data-dir", "/nonexistent"]) == 2
    assert (
        main(
            [
                "--dataset",
                "hillstrom",
                "--policy",
                "scientist",
                "--sealed-final",
                "--data-dir",
                "/nonexistent",
            ]
        )
        == 2
    )
    data, _ = synthetic(2400)
    out = evaluate_scientist(data, seeds=1, timings=common.Timings(quiet=True), with_agent=False)
    saved = tmp_path / "r.json"
    saved.write_text(
        json.dumps({"settings": {"policy": "scientist"}, "datasets": {"synthetic": out}}, default=str)
    )
    capsys.readouterr()
    assert main(["--render", str(saved)]) == 0
    assert "the Scientist's selection" in capsys.readouterr().out
