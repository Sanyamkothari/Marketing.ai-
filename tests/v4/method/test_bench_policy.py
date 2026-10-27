"""The `uplift_threshold` bench policy on tiny frames shaped like each dataset adapter's output.

Hillstrom (three arms, text and numeric columns), X5 (a per-customer margin and a text column), Criteo (12
numeric features, 85/15) and MegaFon (50 numeric features, 50/50). Inline fixtures only; the adapters
themselves are the repo's (`scripts.v2_realbench.adapters`). The scoring is the bench's own
(`common.evaluate_dataset`), so what is checked is that the policy goes through it like the agent does.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from autonomy.decide import uplift_threshold as ut
from scripts.v2_realbench import adapters as A  # noqa: N812
from scripts.v2_realbench import common as C  # noqa: N812
from scripts.v2_realbench import uplift_policy as U  # noqa: N812
from scripts.v2_realbench.run import render
from tests.v2.realfix.test_bench import _hillstrom_rows

ROOT = Path(__file__).resolve().parents[3]


def hillstrom_shaped(n: int = 3000) -> C.BenchData:
    """Three arms; the men's e-mail helps customers who bought men's items and nobody else."""
    frame = _hillstrom_rows(n)
    rng = np.random.default_rng(3)
    arm = frame["segment"].map({a: i for i, a in enumerate(A.HILLSTROM_ARMS)}).to_numpy()
    p = 0.05 + 0.25 * ((arm == 1) & (frame["mens"].to_numpy() == 1))
    conv = (rng.random(n) < p).astype(int)
    frame["conversion"] = conv
    frame["visit"] = np.maximum(conv, frame["visit"].to_numpy())
    frame["spend"] = conv * 80.0
    return A.hillstrom(Path("/nonexistent"), frame=frame)


def x5_shaped(n: int = 3000) -> C.BenchData:
    """Two arms, a per-customer margin of a conversion, a text feature: how the X5 adapter builds its data."""
    rng = np.random.default_rng(4)
    ids = np.array([f"x{i}" for i in range(n)], dtype=object)
    arm = rng.integers(0, 2, n).astype(np.int64)
    recency = rng.integers(1, 200, n).astype(float)
    mean_receipt = rng.uniform(300, 900, n)
    conv = (rng.random(n) < 0.15 + 0.15 * arm * (recency > 100)).astype(float)
    feats = pd.DataFrame(
        {
            "n_receipts": rng.integers(1, 80, n).astype(float),
            "recency_days": recency,
            "mean_receipt": mean_receipt,
            "gender": rng.choice(["F", "M", "U"], n),
        }
    )
    built = A._feature_rct(
        "x5shaped",
        ids,
        arm,
        0.5,
        feats,
        conv,
        None,
        (("recency_days",), ("n_receipts",), ("gender",), ("recency_days", "gender")),
        ("inline fixture shaped like the X5 adapter",),
        {"rows": n},
        categorical=("gender",),
    )
    margin = 0.25 * mean_receipt
    return dataclasses.replace(
        built,
        margin_value=margin * conv,
        conversion_value=margin,
        arm_cost=(0.0, 2.0),
        categorical=("gender",),
        numeric=("n_receipts", "recency_days", "mean_receipt"),
    )


def criteo_shaped(n: int = 4000) -> C.BenchData:
    rng = np.random.default_rng(5)
    ids = np.array([f"cr{i}" for i in range(n)], dtype=object)
    arm = (rng.random(n) < 0.85).astype(np.int64)
    feats = pd.DataFrame({f"f{i}": rng.normal(size=n) for i in range(12)})
    conv = (rng.random(n) < 0.02 + 0.03 * arm * (feats["f8"].to_numpy() > 0)).astype(float)
    visit = np.maximum(conv, rng.random(n) < 0.05)
    return A._feature_rct(
        "criteoshaped",
        ids,
        arm,
        0.85,
        feats,
        conv,
        visit > 0,
        (("f0",), ("f2",), ("f8",), ("f2", "f8")),
        ("inline fixture shaped like the Criteo adapter",),
        {"rows": n},
    )


def megafon_shaped(n: int = 2500, *, base: float = 0.1, lift: float = 0.2) -> C.BenchData:
    rng = np.random.default_rng(6)
    ids = np.array([f"mf{i}" for i in range(n)], dtype=object)
    arm = rng.integers(0, 2, n).astype(np.int64)
    feats = pd.DataFrame({f"X_{i}": rng.normal(size=n) for i in range(1, 51)})
    conv = (rng.random(n) < base + lift * arm * (feats["X_1"].to_numpy() > 0)).astype(float)
    return A._feature_rct(
        "megafonshaped",
        ids,
        arm,
        0.5,
        feats,
        conv,
        None,
        (("X_1",), ("X_2",), ("X_1", "X_2")),
        ("inline fixture shaped like the MegaFon adapter",),
        {"rows": n},
    )


SHAPES = {
    "hillstrom": hillstrom_shaped,
    "x5": x5_shaped,
    "criteo": criteo_shaped,
    "megafon": megafon_shaped,
}


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_the_policy_runs_through_the_bench_like_the_agent(shape: str) -> None:
    data = SHAPES[shape]()
    out = C.evaluate_dataset(data, seeds=1, timings=C.Timings(quiet=True), variant=U.POLICY)
    agent = out["agent"]
    assert agent["variant"] == "uplift_threshold" and agent["seeds"] == 1
    # the scoreboard is the agent's: the same policies, the policy scored as AGENT against all of them
    pol = out["policies"]
    assert "AGENT" in pol and "T-learner (logistic)" in pol and "send to none" in pol
    assert any(k.startswith("segment rule (") for k in pol)
    assert ut.UNGATED in pol and "agent_minus_this" in pol[ut.UNGATED]  # the method itself, ungated
    assert pol["AGENT"]["dr"]["low"] <= pol["AGENT"]["dr"]["point"] <= pol["AGENT"]["dr"]["high"]
    assert abs(sum(agent["arm_shares"]) - 1.0) < 1e-9
    assert agent["diag"]["total"] == out["test"]  # one deterministic decision per test customer
    assert agent["diag"]["act"] + agent["diag"]["nothing"] == out["test"]
    # the DIAG block is the agent's, and one UPLIFT_GATE line follows it
    lines = agent["diag_text"].splitlines()
    assert lines[0].startswith("DIAG run bench dataset=") and "policy=uplift_threshold" in lines[0]
    assert all(line.startswith("DIAG ") for line in lines[:-1])
    gate = ut.parse_gate_line(lines[-1])
    assert gate["chose"] == "model" or gate["chose"].startswith("rule:")
    assert agent["uplift_gate"]["chose"] == gate["chose"]
    assert agent["uplift_gate"]["best_rule"] == gate["best_rule"]
    assert agent["uplift_gate"]["reason"] == gate["reason"]
    assert {"lb", "diff", "best_rule_value", "model_value", "gate_rows", "fit_rows"} <= set(gate)
    md = render({"datasets": {data.name: out}})
    assert f"| {data.name} |" in md and f"### {data.name}" in md


def test_the_policy_is_fitted_on_train_only_and_is_repeatable(monkeypatch: pytest.MonkeyPatch) -> None:
    data = criteo_shaped()
    train, test = C.split_by_hash(data.customer_ids)
    seen: list[set[Any]] = []
    real = ut.fit_uplift_threshold

    def spy(fit_on: ut.UpliftData, **kw: Any) -> ut.UpliftThresholdPolicy:
        seen.append(set(fit_on.customer_ids.tolist()))
        return real(fit_on, **kw)

    monkeypatch.setattr(U, "fit_uplift_threshold", spy)
    first = U.run_uplift_threshold(data, train, test, timings=C.Timings(quiet=True))
    second = U.run_uplift_threshold(data, train, test, timings=C.Timings(quiet=True))
    assert seen[0] == set(data.customer_ids[train].tolist())
    assert not seen[0] & set(data.customer_ids[test].tolist())
    assert np.array_equal(first.pi, second.pi) and first.gate_line == second.gate_line
    assert first.pi.shape == (len(test), data.n_arms) and set(np.unique(first.pi)) <= {0.0, 1.0}


@pytest.mark.slow  # about 5 s single-threaded: an 8,000-row MegaFon-shaped frame, 50 features
def test_planted_strong_signal_is_found_through_the_bench_path() -> None:
    """MegaFon-shaped data where the message helps X_1 > 0 a lot and nobody else, at the break-even cost of
    a blanket send. No segment candidate reads X_1, so no simple rule can find who to message: the gate
    deploys the model, and it messages the customers it helps."""
    data = dataclasses.replace(
        megafon_shaped(8000, base=0.05, lift=0.4), segment_candidates=(("X_2",), ("X_3",))
    )
    train, test = C.split_by_hash(data.customer_ids)
    result = U.run_uplift_threshold(data, train, test, timings=C.Timings(quiet=True))
    assert result.gate is not None and result.gate["chose"] == "model", result.gate_line
    x1 = data.features["X_1"].to_numpy()[test]
    contacted = result.pi[:, 1] > 0
    assert contacted[x1 > 0].mean() > 0.8 and contacted[x1 <= 0].mean() < 0.2
    assert np.array_equal(result.extra_policies[ut.UNGATED], result.pi)
    assert result.expected_net_per_contact is not None and result.expected_net_per_contact > 0


def test_on_x5_only_customers_worth_enough_clear_the_sms_cost() -> None:
    """X5-shaped data where the SMS adds 0.1 to everyone's chance of buying and the margin of a purchase is
    known per customer (`conversion_value`): only customers whose 0.1 x margin is above the SMS cost (2.0)
    are worth a message. The policy reads that value; pricing everyone with the mean buyer would not."""
    rng = np.random.default_rng(8)
    base = x5_shaped(8000)
    n = base.n
    margin = np.where(rng.random(n) < 0.5, 40.0, 8.0)  # 0.1 x 40 = 4 > 2 > 0.8 = 0.1 x 8
    conv = (rng.random(n) < 0.1 + 0.1 * base.arm).astype(float)
    data = dataclasses.replace(
        base, conversion=conv, margin_value=margin * conv, conversion_value=margin, arm_cost=(0.0, 2.0)
    )
    train, test = C.split_by_hash(data.customer_ids)
    result = U.run_uplift_threshold(data, train, test, timings=C.Timings(quiet=True))
    ungated = result.extra_policies[ut.UNGATED][:, 1] > 0
    high = margin[test] == 40.0
    assert ungated[high].mean() == 1.0 and ungated[~high].mean() == 0.0
    assert result.gate is not None and result.gate["chose"] == "model", result.gate_line
    assert np.array_equal(result.pi[:, 1] > 0, ungated)


def test_the_bench_cli_and_ci_know_the_policy() -> None:
    run_py = (ROOT / "scripts" / "v2_realbench" / "run.py").read_text()
    assert 'UPLIFT_POLICY = "uplift_threshold"' in run_py and "UPLIFT_POLICY, " in run_py
    workflow = (ROOT / ".github" / "workflows" / "realbench.yml").read_text()
    [axis] = re.findall(r"policy: \[([^\]]*)\]", workflow)
    policies = [p.strip() for p in axis.split(",")]
    assert policies[:2] == ["agent", "agent_v3"]  # the scoreboard of record and v3 stay
    assert {"uplift_threshold", "agent_greedy", "agent_uplift_only"} <= set(policies)
    assert "dataset: [hillstrom, x5, criteo, megafon]" in workflow
