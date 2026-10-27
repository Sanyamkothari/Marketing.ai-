"""The bench adapter of the `uplift_threshold` policy (autonomy/decide/uplift_threshold.py, docs/v4/METHOD.md).

    python -m scripts.v2_realbench.run --dataset hillstrom --policy uplift_threshold --data-dir DIR

`evaluate_dataset` (common.py) calls `run_uplift_threshold` in the agent's place and scores what it returns
exactly as it scores the agent: the same TEST half, the same baselines, the same tables. The policy is fitted
on TRAIN only (a cross-fitted uplift model, a cost threshold, and the gate against the best simple rule);
there is no agent, no warm start and no seed, so it decides once per test customer and reports `seeds=1`.

It prints the agent's DIAG block (counts of decisions by kind and reason, the figures line) plus one line
(string values JSON-quoted):

    UPLIFT_GATE chose="<model|rule:name>" lb=... diff=... best_rule="<name>" ...

and adds one row to the scoreboard, `uplift model (ungated)`: the cost threshold on the model refitted on all
of TRAIN whatever the gate chose, so the report shows what the method itself is worth next to the gated
output. A customer's purchase is priced with `BenchData.conversion_value` when the dataset knows it before
the campaign (X5), else with each arm's mean margin of a buyer on TRAIN.
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Mapping
from typing import TYPE_CHECKING

import numpy as np

from autonomy.contracts import DecisionKind
from autonomy.decide.uplift_threshold import (
    UNGATED,
    UpliftData,
    UpliftThresholdPolicy,
    fit_uplift_threshold,
    one_hot,
)
from autonomy.runtime.diagnostics import DecisionTally

if TYPE_CHECKING:
    from scripts.v2_realbench.common import AgentResult, BenchData, FloatArray, IntArray, Timings

__all__ = ["POLICY", "run_uplift_threshold", "uplift_data"]

POLICY = "uplift_threshold"
SEED = 0
"""The seed of the fold and gate hashes: fixed, so a run is repeatable."""


def uplift_data(data: BenchData, rows: IntArray, *, attach_features: bool = True) -> UpliftData:
    """The given rows of a bench dataset as the policy's input (arm 0 = no contact)."""
    from scripts.v2_realbench.common import feature_preferences

    frame = data.features.iloc[rows].reset_index(drop=True)
    attributes: tuple[Mapping[str, str], ...] = (
        tuple(feature_preferences(frame)) if attach_features else tuple({} for _ in rows)
    )
    return UpliftData(
        customer_ids=data.customer_ids[rows],
        arm=data.arm[rows].astype(np.int64),
        propensity=data.propensity[rows].astype(np.float64),
        converted=(data.conversion[rows] > 0).astype(np.float64),
        margin=data.margin_value[rows].astype(np.float64),
        arm_cost=np.asarray(data.arm_cost, dtype=np.float64),
        arm_names=tuple(data.arm_names),
        features=frame,
        attributes=attributes,
        conversion_value=(
            None if data.conversion_value is None else data.conversion_value[rows].astype(np.float64)
        ),
    )


def run_uplift_threshold(
    data: BenchData,
    train: IntArray,
    test: IntArray,
    *,
    timings: Timings,
    attach_features: bool = True,
) -> AgentResult:
    """Fit the policy on TRAIN, decide for every TEST customer, and return it in the agent's result shape."""
    from scripts.v2_realbench.common import AgentResult

    with timings.step(f"uplift_threshold: inputs ({len(train)} train, {len(test)} test customers)"):
        train_data = uplift_data(data, train, attach_features=attach_features)
        test_data = uplift_data(data, test, attach_features=attach_features)
    with timings.step(
        "uplift_threshold: cross-fit the model, judge it on held-out train, choose model or rule"
    ):
        policy = fit_uplift_threshold(train_data, segment_candidates=data.segment_candidates, seed=SEED)
    t0 = time.perf_counter()
    with timings.step(f"uplift_threshold: decide for {len(test)} test customers"):
        value = test_data.conversion_value
        arms = policy.arms(test_data.attributes, test_data.features, value)
        nets = policy.net(test_data.attributes, value)
    elapsed = time.perf_counter() - t0
    pi = one_hot(arms, data.n_arms)
    ungated = policy.ungated_arms(test_data.attributes, value)
    extra = {} if ungated is None else {UNGATED: one_hot(ungated, data.n_arms)}
    contacted = arms > 0
    expected: float | None = None
    if nets is not None and contacted.any():
        expected = float(nets[np.flatnonzero(contacted), arms[contacted] - 1].mean())
    tally = _tally(policy, data, arms, nets)
    chosen_shares = Counter(int(a) for a in arms)
    return AgentResult(
        pi=pi,
        expected_net_per_contact=expected,
        kinds=dict(tally.kinds),
        arm_counts={str(k): v for k, v in sorted(chosen_shares.items())},
        blocked_by={},
        sample_reasons=[policy.gate.line()],
        ms_per_decision=round(1000.0 * elapsed / max(len(test), 1), 4),
        warm_start_sends_used=0,
        history_customers=len(train),
        history_events=0,
        decision_log=0,
        seeds=1,
        learned=(policy.gate.line(), *(f"uplift: {n}" for n in policy.notes)),
        tally=tally,
        variant=POLICY,
        changed={
            "decision": "cost threshold on a cross-fitted uplift model, no agent",
            "branch": policy.gate.chose,
        },
        snapshot={"uplift_gate": policy.gate.chose},
        gate_line=policy.gate.line(),
        extra_policies=extra,
        gate={
            "chose": policy.gate.chose,
            "lb": policy.gate.lb,
            "diff": None if policy.gate.diff is None else list(policy.gate.diff),
            "model_contact": policy.gate.model_contact,
            "chosen_on": dict(policy.gate.chosen_on),
            "model_value": policy.gate.model_value,
            "best_rule": policy.gate.best_rule,
            "best_rule_value": policy.gate.best_rule_value,
            "values": {k: list(v) for k, v in policy.gate.values.items()},
            "gate_rows": policy.gate.gate_rows,
            "fit_rows": policy.gate.fit_rows,
            "reason": policy.gate.reason,
        },
    )


def _tally(
    policy: UpliftThresholdPolicy, data: BenchData, arms: IntArray, nets: FloatArray | None
) -> DecisionTally:
    """The decisions counted the way the agent's are, so the DIAG block reads the same."""
    tally = DecisionTally()
    sent = arms > 0
    n_act, n_nothing = int(sent.sum()), int((~sent).sum())
    tally.kinds[DecisionKind.ACT.value] = n_act
    tally.kinds[DecisionKind.NOTHING.value] = n_nothing
    if n_act:
        tally.reasons["act"] = n_act
        tally.channels[data.agent.channel.value] = n_act
        if nets is not None:
            tally.expected_net_sum = float(nets[np.flatnonzero(sent), arms[sent] - 1].sum())
    if n_nothing:
        tally.reasons["net_not_positive" if policy.model is not None else "rule_no_message"] = n_nothing
    return tally
