"""Shared pieces for the family-A runners (randomised campaigns): split, baselines, agent, OPE.

Nothing here downloads anything or changes product code. The estimators are the repo's own:
`autonomy.proof.replay.harness.evaluate` (which calls `autonomy.proof.ope.ips / snips /
doubly_robust`) gives every policy's IPS, SNIPS and DR value with 95% intervals; the paired
"incremental over send-to-none" difference is built from the same DR per-row terms
(`q_pi + w (r - q_logged)`, reward model `harness.per_action_mean_model`) and summarised with
`autonomy.proof.ope.mean_interval`.

Conventions used by every runner:

* Arm 0 is always "no contact". `propensity[i]` is the design's chance of the arm row i got.
* The split is by customer (one row is one customer in every family-A file), seeded, 50/50.
  Every model, rule and the agent's learning see TRAIN only; every number is computed on TEST.
* A policy is an (n_test, n_arms) matrix of probabilities. The agent's matrix is estimated by
  deciding for each test customer under several seeds (its Thompson draw is seeded by
  (seed, customer)), so it is the agent's real stochastic policy at that moment, exploration included.

Run with the repo root on the path (the runners do it): python3 -I scripts/v2_realdata/a_<name>.py
"""

from __future__ import annotations

import gc
import json
import math
import resource
import sys
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from autonomy.contracts import (  # noqa: E402
    Brief,
    ContentItem,
    CustomerState,
    Decision,
    DecisionContext,
    DecisionKind,
    Event,
)
from autonomy.data.folder import StateFolder  # noqa: E402
from autonomy.data.rebuild import rebuild_states  # noqa: E402
from autonomy.fakes.stores import InMemoryEventStore, InMemoryStateStore  # noqa: E402
from autonomy.proof import ope  # noqa: E402
from autonomy.proof.replay import LoggedBandit, fit_segment_policy  # noqa: E402
from autonomy.proof.replay import evaluate as harness_evaluate  # noqa: E402
from autonomy.proof.replay.harness import per_action_mean_model  # noqa: E402

DATASETS = Path("/home/user/datasets")
RESULTS = DATASETS / "_results"


# --- measuring ---------------------------------------------------------------------------------


def peak_rss_mb() -> float:
    """Peak resident memory of this process so far (Linux reports ru_maxrss in KiB)."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


@dataclass
class Timings:
    rows: list[dict[str, Any]] = field(default_factory=list)

    def step(self, name: str) -> _Step:
        return _Step(self, name)


class _Step:
    def __init__(self, owner: Timings, name: str) -> None:
        self.owner, self.name = owner, name

    def __enter__(self) -> _Step:
        self.t0 = time.perf_counter()
        print(f"[{time.strftime('%H:%M:%S')}] {self.name} ...", flush=True)
        return self

    def __exit__(self, *exc: object) -> None:
        dt = time.perf_counter() - self.t0
        row = {"step": self.name, "seconds": round(dt, 2), "peak_rss_mb": round(peak_rss_mb(), 1)}
        self.owner.rows.append(row)
        print(
            f"[{time.strftime('%H:%M:%S')}]   done in {dt:.1f}s, peak RSS {row['peak_rss_mb']} MB", flush=True
        )


# --- the randomised table ----------------------------------------------------------------------


@dataclass
class Rct:
    """One row per customer of a randomised campaign, outcomes kept apart from features."""

    name: str
    customer_ids: np.ndarray
    arm: np.ndarray  # int, 0 = no contact
    propensity: np.ndarray  # design probability of the arm received
    arm_names: tuple[str, ...]
    features: pd.DataFrame  # pre-treatment only
    outcomes: dict[str, np.ndarray]  # e.g. conversion, revenue

    def take(self, idx: np.ndarray) -> Rct:
        return Rct(
            self.name,
            self.customer_ids[idx],
            self.arm[idx],
            self.propensity[idx],
            self.arm_names,
            self.features.iloc[idx].reset_index(drop=True),
            {k: v[idx] for k, v in self.outcomes.items()},
        )

    @property
    def n(self) -> int:
        return len(self.arm)

    def logged(self, reward: np.ndarray, reward_name: str) -> LoggedBandit:
        return LoggedBandit(
            source=self.name,
            contexts=self.features,
            actions=self.arm.astype(np.int64),
            propensity=self.propensity.astype(np.float64),
            reward=np.asarray(reward, dtype=np.float64),
            action_names=self.arm_names,
            reward_name=reward_name,
        )


def split_customers(n: int, *, test_share: float = 0.5, seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """Train and test row positions, by customer (one row is one customer), seeded."""
    order = np.random.default_rng(seed).permutation(n)
    n_test = round(n * test_share)
    return np.sort(order[n_test:]), np.sort(order[:n_test])


# --- economics -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Economics:
    """How a row's outcome becomes profit: margin_rate x revenue - cost_per_send if contacted."""

    margin_rate: float
    cost_per_send: float
    currency: str
    note: str

    def profit(self, revenue: np.ndarray, arm: np.ndarray) -> np.ndarray:
        return np.asarray(
            self.margin_rate * np.asarray(revenue, dtype=np.float64) - self.cost_per_send * (arm != 0)
        )


# --- simple features for the baselines -----------------------------------------------------------


@dataclass
class Design:
    """One-hot design: numeric columns cut at train quantiles (bins), categoricals as levels."""

    numeric: tuple[str, ...]
    categorical: tuple[str, ...]
    bins: int = 8
    edges: dict[str, np.ndarray] = field(default_factory=dict)
    levels: dict[str, list[str]] = field(default_factory=dict)

    def fit(self, frame: pd.DataFrame) -> Design:
        for col in self.numeric:
            v = frame[col].to_numpy(dtype=np.float64)
            qs = np.unique(np.nanquantile(v, np.linspace(0, 1, self.bins + 1)[1:-1]))
            self.edges[col] = qs
        for col in self.categorical:
            counts = frame[col].astype(str).value_counts()
            self.levels[col] = [str(x) for x in counts.index[:30]]
        return self

    def matrix(self, frame: pd.DataFrame) -> np.ndarray:
        blocks = [np.ones((len(frame), 1))]
        for col in self.numeric:
            v = frame[col].to_numpy(dtype=np.float64)
            b = np.searchsorted(self.edges[col], v, side="right")
            oh = np.zeros((len(frame), len(self.edges[col]) + 1))
            oh[np.arange(len(frame)), b] = 1.0
            blocks.append(oh[:, 1:])
        for col in self.categorical:
            s = frame[col].astype(str).to_numpy()
            levels = self.levels[col]
            oh = np.zeros((len(frame), len(levels)))
            for j, lv in enumerate(levels[1:], start=1):
                oh[:, j] = s == lv
            blocks.append(oh[:, 1:])
        return np.hstack(blocks)


def fit_ridge(x: np.ndarray, y: np.ndarray, lam: float = 1.0) -> np.ndarray:
    xtx = x.T @ x
    reg = lam * np.eye(x.shape[1])
    reg[0, 0] = 0.0
    return np.linalg.solve(xtx + reg, x.T @ y)


def uplift_t_learner(
    train: Rct, test: Rct, reward_train: np.ndarray, design: Design, *, lam: float = 10.0
) -> tuple[np.ndarray, np.ndarray]:
    """T-learner: one ridge regression of the reward per arm (train rows of that arm only).

    Returns the deterministic policy (argmax predicted reward per test row) and the predictions.
    The reward already carries the contact cost, so "contact" must pay for itself to win.
    """
    design.fit(train.features)
    k = len(train.arm_names)
    chunk = 500_000
    xtx: dict[int, np.ndarray] = {}
    xty: dict[int, np.ndarray] = {}
    for start in range(0, train.n, chunk):
        x = design.matrix(train.features.iloc[start : start + chunk])
        arms, y = train.arm[start : start + chunk], reward_train[start : start + chunk]
        for a in range(k):
            rows = arms == a
            xtx[a] = xtx.get(a, 0) + x[rows].T @ x[rows]
            xty[a] = xty.get(a, 0) + x[rows].T @ y[rows]
    betas = {}
    for a in range(k):
        reg = lam * np.eye(xtx[a].shape[0])
        reg[0, 0] = 0.0
        betas[a] = np.linalg.solve(xtx[a] + reg, xty[a])
    pred = np.zeros((test.n, k))
    for start in range(0, test.n, chunk):
        x = design.matrix(test.features.iloc[start : start + chunk])
        for a in range(k):
            pred[start : start + chunk, a] = x @ betas[a]
    pi = np.zeros_like(pred)
    pi[np.arange(test.n), pred.argmax(axis=1)] = 1.0
    return pi, pred


def best_segment_rule(
    train: Rct,
    reward_train: np.ndarray,
    candidates: Sequence[Sequence[str]],
    *,
    seed: int = 1,
    shrinkage: float = 50.0,
) -> tuple[Any, list[dict[str, Any]]]:
    """Pick the segment columns by DR value on a 30% validation slice of TRAIN, then refit on all of
    train with the repo's `fit_segment_policy`. Returns the fitted policy and the validation table."""
    fit_idx, val_idx = split_customers(train.n, test_share=0.3, seed=seed)
    tr, va = train.take(fit_idx), train.take(val_idx)
    log_tr, log_va = tr.logged(reward_train[fit_idx], "r"), va.logged(reward_train[val_idx], "r")
    table: list[dict[str, Any]] = []
    for cols in candidates:
        pol = fit_segment_policy(log_tr, segment_columns=list(cols), shrinkage=shrinkage)
        est = harness_evaluate(pol, log_va, policy_name="+".join(cols))
        table.append({"columns": list(cols), "val_dr": est.dr.point})
    best = max(table, key=lambda r: r["val_dr"])
    policy = fit_segment_policy(
        train.logged(reward_train, "r"), segment_columns=best["columns"], shrinkage=shrinkage
    )
    return policy, table


# --- the agent -----------------------------------------------------------------------------------


def fold_states(events: Sequence[Event], now: datetime) -> dict[str, CustomerState]:
    """The product path: events into an event store, `rebuild_states` with the production folder."""
    store, states = InMemoryEventStore(), InMemoryStateStore()
    store.append(events)
    rebuild_states(store, states, StateFolder(), now)
    return {s.customer_id: s for s in states.scan()}


@dataclass
class AgentRun:
    pi: np.ndarray
    kinds: Counter[str]
    blocked: Counter[str]
    reasons: list[str]
    arm_counts: Counter[int]
    seconds_per_decision: float
    mean_logged_probability: float
    history_used: int
    mean_expected_net_act: float = float("nan")
    """The agent's own expected net value of the messages it chose (its bid, before the attention price)."""


def agent_policy_matrix(
    policy: Any,
    test_states: Sequence[CustomerState],
    *,
    now: datetime,
    briefs: Sequence[Brief],
    content: Sequence[ContentItem],
    arm_of: Callable[[Decision], int],
    n_arms: int,
    seeds: Sequence[int],
    all_states: Sequence[CustomerState],
) -> AgentRun:
    """Let the agent decide for every test customer under each seed; the share of seeds that chose
    each arm is the agent's probability of that arm for that customer."""
    ctx0 = DecisionContext(now=now, briefs=tuple(briefs), content=tuple(content), seed=seeds[0])
    policy.prepare_day(list(all_states), ctx0)
    pi = np.zeros((len(test_states), n_arms))
    kinds: Counter[str] = Counter()
    blocked: Counter[str] = Counter()
    arms: Counter[int] = Counter()
    reasons: list[str] = []
    probs: list[float] = []
    nets: list[float] = []
    t0 = time.perf_counter()
    n_dec = 0
    for s_i, seed in enumerate(seeds):
        ctx = ctx0 if s_i == 0 else ctx0.model_copy(update={"seed": seed})
        for i, st in enumerate(test_states):
            d = policy.decide(st, ctx)
            n_dec += 1
            a = arm_of(d)
            pi[i, a] += 1.0
            arms[a] += 1
            kinds[d.kind.value] += 1
            probs.append(d.probability)
            if d.kind is DecisionKind.ACT:
                nets.append(d.expected_net)
            for code in d.blocked_by:
                blocked[code] += 1
            if s_i == 0 and len(reasons) < 6 and (d.kind is DecisionKind.ACT or i % 5000 == 0):
                reasons.append(d.reason)
        print(f"    seed {seed}: {n_dec} decisions so far, {time.perf_counter() - t0:.1f}s", flush=True)
    pi /= len(seeds)
    return AgentRun(
        pi=pi,
        kinds=kinds,
        blocked=blocked,
        reasons=reasons,
        arm_counts=arms,
        seconds_per_decision=(time.perf_counter() - t0) / max(n_dec, 1),
        mean_logged_probability=float(np.mean(probs)) if probs else float("nan"),
        history_used=int(getattr(policy, "history_used", 0)),
        mean_expected_net_act=float(np.mean(nets)) if nets else float("nan"),
    )


@dataclass
class TrainLog:
    """The train split as the agent's learning API takes it."""

    history: dict[str, list[Event]]  # customer -> pre-period + campaign sends/clicks + outcomes
    decisions: dict[str, Decision]  # the RCT's own assignments, as Decision records
    rewards: list[Any]  # autonomy.contracts.Reward, one per decision


def run_agent_variants(
    *,
    variants: Sequence[str],
    train: TrainLog,
    train_states: Sequence[CustomerState],
    test_states: Sequence[CustomerState],
    briefs: Sequence[Brief],
    content: Sequence[ContentItem],
    now: datetime,
    arm_of: Callable[[Decision], int],
    n_arms: int,
    seeds: Sequence[int],
    costs: Mapping[Any, float] | None,
    horizon_days: int,
    timings: Timings,
) -> dict[str, AgentRun]:
    """Build the agent each way, let it learn from TRAIN only, and let it decide for TEST.

    * ``click``: `build_agent_policy` as the runtime builds it (ClickResponseModel). The train
      history (sends with `content_id` and `logged_probability`, their clicks, purchases) is handed
      over through `nightly` before the first decision, which is the product's warm start.
    * ``purchase``: the same agent on `autonomy.learning.build_bundle()` (BetaResponseModel: uplift =
      treated - control conversion rate per segment and action). After the warm start, the RCT's own
      assignments and window outcomes are fed to `learn_from_rewards` (holdout = the RCT control),
      with each train customer's segment as the agent labels it.
    """
    from autonomy.learning import build_bundle, learn_from_rewards
    from autonomy.runtime.agent import build_agent_policy

    out: dict[str, AgentRun] = {}
    all_states = [*train_states, *test_states]
    content_by_id = {c.content_id: c for c in content}
    for variant in variants:
        with timings.step(f"agent[{variant}]: build, warm start and learn on train"):
            bundle = build_bundle() if variant == "purchase" else None
            policy = build_agent_policy(list(briefs), list(content), bundle=bundle, costs=costs)
            policy.nightly([], train.history, now)
            ctx = DecisionContext(now=now, briefs=tuple(briefs), content=tuple(content), seed=seeds[0])
            policy.prepare_day(all_states, ctx)
            if variant == "purchase":
                seg = {s.customer_id: policy.segment(s, now) for s in train_states}
                report = learn_from_rewards(
                    policy.bundle,
                    train.rewards,
                    train.decisions,
                    content_by_id,
                    horizon_days=horizon_days,
                    segment_of=seg.get,
                )
                print(f"    learn_from_rewards: {report.summary()}")
            print(f"    warm start used {policy.history_used} logged sends")
        with timings.step(
            f"agent[{variant}]: decide for {len(test_states)} test customers x {len(seeds)} seeds"
        ):
            out[variant] = agent_policy_matrix(
                policy,
                test_states,
                now=now,
                briefs=briefs,
                content=content,
                arm_of=arm_of,
                n_arms=n_arms,
                seeds=seeds,
                all_states=all_states,
            )
        run = out[variant]
        print(
            f"    kinds {dict(run.kinds)}, arms {dict(run.arm_counts)}, top blocks {run.blocked.most_common(4)}, "
            f"{run.seconds_per_decision * 1000:.2f} ms/decision"
        )
        for r in run.reasons[:3]:
            print(f"    reason: {r}")
        # free this agent before the next is built: two agents over 4M events do not fit in 14 GB
        del policy
        gc.collect()
    return out


def agent_summary(run: AgentRun) -> dict[str, Any]:
    return {
        "kinds": dict(run.kinds),
        "arm_counts": {str(k): v for k, v in run.arm_counts.items()},
        "blocked_by": dict(run.blocked.most_common(10)),
        "sample_reasons": run.reasons,
        "ms_per_decision": round(run.seconds_per_decision * 1000, 3),
        "mean_logged_probability": run.mean_logged_probability,
        "warm_start_sends_used": run.history_used,
        "mean_expected_net_of_chosen_sends": finite(run.mean_expected_net_act),
    }


# --- off-policy evaluation -----------------------------------------------------------------------


def _est(e: Any) -> dict[str, float] | None:
    if e is None:
        return None
    return {"point": e.point, "low": e.low, "high": e.high}


def dr_terms(logged: LoggedBandit, pi: np.ndarray, q: np.ndarray) -> np.ndarray:
    rows = np.arange(logged.n_rows)
    w = pi[rows, logged.actions] / logged.propensity
    return np.asarray((pi * q).sum(axis=1) + w * (logged.reward - q[rows, logged.actions]))


def best_blanket_arm(train: Rct, reward_train: np.ndarray) -> int:
    """The single arm with the highest mean reward on TRAIN (a randomised trial: plain means)."""
    means = [float(reward_train[train.arm == a].mean()) for a in range(len(train.arm_names))]
    return int(np.argmax(means))


def evaluate_policies(
    test: Rct,
    reward: np.ndarray,
    reward_name: str,
    policies: Mapping[str, np.ndarray],
    *,
    reference: np.ndarray | None = None,
) -> list[dict[str, Any]]:
    """Every policy's IPS / SNIPS / DR (repo harness), its DR gain over send-to-none, and (when a
    reference policy is given, e.g. the best blanket arm chosen on train) its DR gain over that
    reference; both differences are paired row by row."""
    logged = test.logged(reward, reward_name)
    q = per_action_mean_model(logged)
    none = np.zeros((test.n, len(test.arm_names)))
    none[:, 0] = 1.0
    base = dr_terms(logged, none, q)
    ref = dr_terms(logged, reference, q) if reference is not None else None
    out: list[dict[str, Any]] = []
    for name, pi in policies.items():

        def fixed_pi(_logged: LoggedBandit, m: np.ndarray = pi) -> np.ndarray:
            return m

        est = harness_evaluate(fixed_pi, logged, policy_name=name)
        terms = dr_terms(logged, pi, q)
        assert abs(float(terms.mean()) - est.dr.point) < 1e-9, "DR terms disagree with the harness"
        gain = ope.mean_interval(terms - base)
        out.append(
            {
                "policy": name,
                "contact_share": float(1.0 - pi[:, 0].mean()),
                "arm_shares": [float(x) for x in pi.mean(axis=0)],
                "ips": _est(est.ips),
                "snips": _est(est.snips),
                "dr": _est(est.dr),
                "dr_vs_none": {"point": gain.point, "low": gain.low, "high": gain.high},
                "dr_vs_logging": _est(est.dr_vs_logging),
                "dr_vs_reference": None if ref is None else _est(ope.mean_interval(terms - ref)),
                "ess": est.effective_sample_size,
                "n": est.n_rows,
            }
        )
    return out


def print_table(title: str, rows: Sequence[Mapping[str, Any]], scale: float = 1.0, unit: str = "") -> None:
    print(f"\n{title}")
    print(
        f"{'policy':34s} {'contact':>7s} {'IPS':>10s} {'SNIPS':>10s} {'DR':>10s} {'DR vs none [95% CI]':>34s} {'ESS':>8s}  DR vs best blanket [95% CI]"
    )
    for r in rows:
        sn = r["snips"]["point"] * scale if r["snips"] else float("nan")
        g = r["dr_vs_none"]
        print(
            f"{r['policy'][:34]:34s} {r['contact_share']:7.1%} {r['ips']['point'] * scale:10.4f} {sn:10.4f} "
            f"{r['dr']['point'] * scale:10.4f} {g['point'] * scale:+10.4f} [{g['low'] * scale:+.4f}, {g['high'] * scale:+.4f}]"
            f" {r['ess']:8.0f}"
            + (
                f"  {r['dr_vs_reference']['point'] * scale:+.4f} [{r['dr_vs_reference']['low'] * scale:+.4f}, {r['dr_vs_reference']['high'] * scale:+.4f}]"
                if r.get("dr_vs_reference")
                else ""
            )
        )
    if unit:
        print(f"(values per customer{unit})")


def save(name: str, payload: Mapping[str, Any]) -> Path:
    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / f"{name}.json"

    def default(o: object) -> object:
        if isinstance(o, np.integer | np.floating):
            return o.item()
        if isinstance(o, np.ndarray):
            return o.tolist()
        if isinstance(o, Counter):
            return dict(o)
        return str(o)

    path.write_text(json.dumps(payload, indent=1, default=default))
    print(f"\nresults written to {path}")
    return path


def save_matrices(name: str, mats: Mapping[str, np.ndarray]) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    arrays: dict[str, Any] = {k.replace(" ", "_"): v for k, v in mats.items()}
    np.savez_compressed(RESULTS / f"{name}_pi.npz", **arrays)


def fixed(n: int, k: int, arm: int) -> np.ndarray:
    pi = np.zeros((n, k))
    pi[:, arm] = 1.0
    return pi


def uniform(n: int, k: int) -> np.ndarray:
    return np.full((n, k), 1.0 / k)


def finite(x: float) -> float | None:
    return x if math.isfinite(x) else None
