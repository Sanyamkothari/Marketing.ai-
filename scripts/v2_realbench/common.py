"""The scoreboard's engine: split, baselines, the agent through the product path, scoring.

Nothing here knows a dataset. A bench adapter (`scripts/v2_realbench/adapters.py`) turns a file into
a `BenchData`: one row per customer of a randomised campaign (arm 0 = no contact, the design
probability of the arm received, pre-treatment features, outcomes) plus what a customer's connector
would hand Autonomy (events, briefs, content, channel costs). This module then:

1. **Splits by customer id hash** (`split_by_hash`, fixed salt `SPLIT_SALT`, 50/50). Everything is
   fitted on TRAIN; every number is computed on TEST.
2. **Builds the agent the way production does** (`run_agent`): `build_agent_policy(briefs,
   content, costs=...)` (the default bundle, as `autonomy.runtime.settings.agent_policy_factory`
   builds it, with the dataset's channel cost), then the runtime's warm start: `policy.nightly([],
   history, now)` with the TRAIN customers' events (pre-period, the past campaign's sends with
   `logged_probability`, its clicks where the data has a click-like signal, and the campaign-window
   purchases), then `policy.prepare_day(train_states, ctx)`, then `policy.decide(state, ctx)` for
   every TEST customer under several seeds. Test customers' states are folded with the production
   `StateFolder` from pre-campaign events only. A fix in `autonomy/` shows up here with no change
   to the bench.
3. **Baselines fitted on TRAIN**: send to none, send to all (each arm), the best segment rule (repo
   `fit_segment_policy`, columns chosen on a validation slice of train) and a T-learner (one numpy
   logistic regression of conversion per arm, on one-hot binned features; bench only).
4. **Scores on TEST** with `autonomy.proof` (`replay.harness.evaluate`: IPS, SNIPS, DR with 95%
   intervals; reward model `per_action_mean_model`, cross-fitted) and paired DR differences
   (`ope.mean_interval`) of the agent against every baseline.

The "best simple policy" (none / send to all / segment rule) is chosen on TRAIN only: by DR on a
30% validation slice of train, with the segment rule fitted on the other 70%. The agent's
calibration compares its own mean expected net value per contacted customer (`Decision.
expected_net` of its ACT decisions) with the DR-measured value of contacting them (its DR gain over
send-to-none divided by its contact rate), both in the same profit units, net of the contact cost.

The agent's policy is estimated by deciding for each test customer under `seeds` seeds (its Thompson
draw is seeded by (seed, customer)); the share of seeds choosing each arm is its probability of that
arm. A `Decision` logs only the probability of the option it chose, so this is the only way to get
the full distribution; what is scored is the agent randomised over those seeds.
"""

from __future__ import annotations

import gc
import hashlib
import math
import resource
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from statistics import NormalDist
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd

from autonomy.contracts import (
    Action,
    Brief,
    Channel,
    ContentItem,
    CustomerState,
    Decision,
    DecisionContext,
    DecisionKind,
    Event,
    Group,
)
from autonomy.data.folder import StateFolder
from autonomy.data.rebuild import rebuild_states
from autonomy.fakes.stores import InMemoryEventStore, InMemoryStateStore
from autonomy.proof import ope
from autonomy.proof.estimate import Z_95
from autonomy.proof.replay import LoggedBandit, fit_segment_policy
from autonomy.proof.replay import evaluate as harness_evaluate
from autonomy.proof.replay.harness import SegmentPolicy, per_action_mean_model

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]
BoolArray = npt.NDArray[np.bool_]

BENCH_VERSION = 2
SPLIT_SALT = "autonomy-realbench-v1"
"""Salt of the train/test split. Never change it to move a number: every result is on this split."""
VALIDATION_SALT = "autonomy-realbench-v1:validation"
VALIDATION_SHARE = 0.3
TEST_SHARE = 0.5
CALIBRATION_BAND = 2.0
"""The calibration target: expected within a factor of 2 of measured."""


# --- measuring -----------------------------------------------------------------------------------


def peak_rss_mb() -> float:
    """Peak resident memory of this process so far (Linux reports ru_maxrss in KiB)."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


@dataclass
class Timings:
    rows: list[dict[str, Any]] = field(default_factory=list)
    quiet: bool = False

    def step(self, name: str) -> _Step:
        return _Step(self, name)


class _Step:
    def __init__(self, owner: Timings, name: str) -> None:
        self.owner, self.name = owner, name
        self.t0 = 0.0

    def __enter__(self) -> _Step:
        self.t0 = time.perf_counter()
        if not self.owner.quiet:
            print(f"[{time.strftime('%H:%M:%S')}] {self.name} ...", flush=True)
        return self

    def __exit__(self, *exc: object) -> None:
        dt = time.perf_counter() - self.t0
        row = {"step": self.name, "seconds": round(dt, 2), "peak_rss_mb": round(peak_rss_mb(), 1)}
        self.owner.rows.append(row)
        if not self.owner.quiet:
            print(f"[{time.strftime('%H:%M:%S')}]   {dt:.1f}s, peak RSS {row['peak_rss_mb']} MB", flush=True)


# --- hashing and the split -----------------------------------------------------------------------


def unit_hash(ids: Sequence[str] | npt.NDArray[Any], salt: str) -> FloatArray:
    """A number in [0, 1) per id, from BLAKE2b of `salt:id`: stable across runs, machines and Pythons."""
    out = np.empty(len(ids), dtype=np.float64)
    prefix = f"{salt}:".encode()
    for i, cid in enumerate(ids):
        digest = hashlib.blake2b(prefix + str(cid).encode(), digest_size=8).digest()
        out[i] = int.from_bytes(digest, "big") / 2.0**64
    return out


def split_by_hash(
    ids: Sequence[str] | npt.NDArray[Any], *, salt: str = SPLIT_SALT, test_share: float = TEST_SHARE
) -> tuple[IntArray, IntArray]:
    """Train and test row positions: a customer is in TEST when its id's hash is below `test_share`."""
    h = unit_hash(ids, salt)
    test = h < test_share
    return np.flatnonzero(~test).astype(np.int64), np.flatnonzero(test).astype(np.int64)


def pick_by_hash(ids: Sequence[str] | npt.NDArray[Any], n: int, *, salt: str) -> IntArray:
    """The positions of the `n` ids with the smallest hashes (a fixed sample), in their file order."""
    if n >= len(ids):
        return np.arange(len(ids), dtype=np.int64)
    h = unit_hash(ids, salt)
    return np.sort(np.argpartition(h, n)[:n]).astype(np.int64)


def splitmix64(x: npt.NDArray[np.uint64]) -> npt.NDArray[np.uint64]:
    """A fast integer hash (SplitMix64 finaliser), for sampling rows of very large files by row number."""
    with np.errstate(over="ignore"):
        z = x + np.uint64(0x9E3779B97F4A7C15)
        z = (z ^ (z >> np.uint64(30))) * np.uint64(0xBF58476D1CE4E5B9)
        z = (z ^ (z >> np.uint64(27))) * np.uint64(0x94D049BB133111EB)
        return z ^ (z >> np.uint64(31))


def salt_key(salt: str) -> np.uint64:
    return np.uint64(int.from_bytes(hashlib.blake2b(salt.encode(), digest_size=8).digest(), "big"))


# --- what an adapter produces --------------------------------------------------------------------


@dataclass
class AgentInputs:
    """What a customer's connector and setup would hand Autonomy for this campaign.

    `pre_events(ids)` gives each customer's events from before the campaign (signup, consent,
    purchases): states are folded from these only. `campaign_events(ids)` gives what the past
    campaign added to those customers' history (its sends with `content_id` and
    `logged_probability`, clicks where the data has a click-like signal, and the purchases in the
    campaign window). The bench calls it with TRAIN ids only.
    """

    briefs: tuple[Brief, ...]
    content: tuple[ContentItem, ...]
    costs: dict[Channel, float]
    channel: Channel
    goal_id: str
    campaign_at: datetime
    now: datetime
    arm_content: dict[int, str]
    """Arm -> the content id of its message (arm 0, no contact, has none)."""
    pre_events: Callable[[Sequence[str]], dict[str, list[Event]]]
    campaign_events: Callable[[Sequence[str]], dict[str, list[Event]]]

    def arm_of(self, d: Decision) -> int:
        """Which arm of the campaign a decision is: 0 for doing nothing, else its message's arm."""
        if d.kind is DecisionKind.NOTHING or d.action is None:
            return 0
        for arm, cid in self.arm_content.items():
            if d.action.content_id == cid:
                return arm
        raise ValueError(f"the agent chose content {d.action.content_id!r}, which is not an arm")


@dataclass
class BenchData:
    """One randomised campaign, one row per customer, as a bench adapter built it."""

    name: str
    customer_ids: npt.NDArray[Any]
    arm: IntArray
    propensity: FloatArray
    arm_names: tuple[str, ...]
    features: pd.DataFrame
    """Pre-treatment features only (what the baselines read; also handed to the agent's states as
    `preferences`, as folding one PREFERENCE event per column would)."""
    numeric: tuple[str, ...]
    categorical: tuple[str, ...]
    conversion: FloatArray
    margin_value: FloatArray
    """The margin a row's outcome made (before any contact cost)."""
    arm_cost: tuple[float, ...]
    """Contact cost of each arm (0 for arm 0). Profit = margin_value - arm_cost[arm received]."""
    conversion_value: FloatArray | None
    """Margin of a conversion when it is known before the campaign (per row), else None: the
    T-learner then uses each arm's mean margin per conversion on train."""
    segment_candidates: tuple[tuple[str, ...], ...]
    economics: str
    agent: AgentInputs
    notes: tuple[str, ...] = ()
    sizes: dict[str, Any] = field(default_factory=dict)

    @property
    def n(self) -> int:
        return len(self.arm)

    @property
    def n_arms(self) -> int:
        return len(self.arm_names)

    def profit(self) -> FloatArray:
        cost = np.asarray(self.arm_cost, dtype=np.float64)[self.arm]
        return self.margin_value - cost

    def logged(self, rows: IntArray, reward: FloatArray, reward_name: str) -> LoggedBandit:
        return LoggedBandit(
            source=self.name,
            contexts=self.features.iloc[rows].reset_index(drop=True),
            actions=self.arm[rows].astype(np.int64),
            propensity=self.propensity[rows].astype(np.float64),
            reward=np.asarray(reward[rows], dtype=np.float64),
            action_names=self.arm_names,
            reward_name=reward_name,
        )


# --- features into the agent's states ------------------------------------------------------------


def _cell(v: object) -> str | None:
    if v is None:
        return None
    if isinstance(v, float | np.floating):
        if not np.isfinite(v):
            return None
        return format(float(v), ".6g")
    if isinstance(v, int | np.integer):
        return str(int(v))
    text = str(v)
    return text if text and text != "nan" else None


def feature_preferences(features: pd.DataFrame) -> list[dict[str, str]]:
    """Each row's features as the `preferences` folding one PREFERENCE event per column gives."""
    cols = list(features.columns)
    out: list[dict[str, str]] = []
    for row in features.itertuples(index=False, name=None):
        prefs: dict[str, str] = {}
        for col, v in zip(cols, row, strict=True):
            text = _cell(v)
            if text is not None:
                prefs[str(col)] = text
        out.append(prefs)
    return out


def fold_states(events: Mapping[str, Sequence[Event]], now: datetime) -> dict[str, CustomerState]:
    """The product path: events into an event store, `rebuild_states` with the production folder."""
    store, states = InMemoryEventStore(), InMemoryStateStore()
    store.append([e for evs in events.values() for e in evs])
    rebuild_states(store, states, StateFolder(), now)
    return {s.customer_id: s for s in states.scan()}


# --- the agent through the product path ----------------------------------------------------------


@dataclass
class AgentResult:
    pi: FloatArray
    expected_net_per_contact: float | None
    kinds: dict[str, int]
    arm_counts: dict[str, int]
    blocked_by: dict[str, int]
    sample_reasons: list[str]
    ms_per_decision: float
    warm_start_sends_used: int
    history_customers: int
    history_events: int
    decision_log: int
    seeds: int
    learned: tuple[str, ...] = ()
    """What the agent's warm start says it learned (calibration and uplift notes), as plain text."""


def _learned(policy: object) -> tuple[str, ...]:
    """The warm start's own account of what it learned, read through public attributes only."""
    out: list[str] = []
    calib = getattr(policy, "calibration", None)
    if isinstance(calib, Mapping):
        out += [f"calibration {k}: {v}" for k, v in calib.items()]
    uplift = getattr(policy, "uplift", None)
    if uplift is not None:
        out.append(f"uplift attached: {bool(getattr(uplift, 'attached', False))}")
        out += [f"uplift: {n}" for n in getattr(uplift, "notes", ())]
    return tuple(out)


def decision_log(data: BenchData, rows: IntArray) -> list[Decision]:
    """The past campaign's assignments as decisions (ACT in the agent group for a sent arm, NOTHING
    in the holdout group for the control), for `--with-decision-log`: how the runtime's `_learn`
    hands a policy the decisions in its store."""
    inp = data.agent
    out: list[Decision] = []
    for i in rows:
        cid = str(data.customer_ids[i])
        a = int(data.arm[i])
        did = f"bench_{data.name}_{cid}"
        p = float(data.propensity[i])
        if a:
            act = Action(
                goal_id=inp.goal_id,
                channel=inp.channel,
                content_id=inp.arm_content[a],
                send_at=inp.campaign_at,
            )
            out.append(
                Decision(
                    decision_id=did,
                    customer_id=cid,
                    made_at=inp.campaign_at,
                    kind=DecisionKind.ACT,
                    action=act,
                    group=Group.AGENT,
                    probability=p,
                    reason="past randomised campaign",
                    policy_version="bench-rct",
                )
            )
        else:
            out.append(
                Decision(
                    decision_id=did,
                    customer_id=cid,
                    made_at=inp.campaign_at,
                    kind=DecisionKind.NOTHING,
                    group=Group.HOLDOUT,
                    probability=p,
                    reason="past randomised campaign: control",
                    policy_version="bench-rct",
                )
            )
    return out


def run_agent(
    data: BenchData,
    train: IntArray,
    test: IntArray,
    *,
    seeds: int,
    timings: Timings,
    with_decision_log: bool = False,
    attach_features: bool = True,
) -> AgentResult:
    """Build the production agent, warm-start it on TRAIN as the runtime does, decide for TEST."""
    from autonomy.runtime.agent import build_agent_policy

    inp = data.agent
    ids = [str(c) for c in data.customer_ids]
    train_ids = [ids[i] for i in train]
    test_ids = [ids[i] for i in test]
    with timings.step("agent inputs: pre-campaign events, states (StateFolder)"):
        pre = inp.pre_events(ids)
        states = fold_states(pre, inp.now)
        prefs = feature_preferences(data.features) if attach_features else None

        def state_of(i: int) -> CustomerState:
            st = states.get(ids[i]) or CustomerState(customer_id=ids[i], as_of=inp.now)
            if prefs is not None and prefs[i]:
                st = st.model_copy(update={"preferences": {**st.preferences, **prefs[i]}})
            return st

        train_states = [state_of(int(i)) for i in train]
        test_states = [state_of(int(i)) for i in test]
        states.clear()
    with timings.step("agent inputs: train history (pre-period + past campaign)"):
        camp = inp.campaign_events(train_ids)
        test_set = set(test_ids)
        if any(c in test_set for c in camp):
            raise AssertionError("campaign events of a TEST customer reached the agent")
        history = {c: [*pre.get(c, []), *camp.get(c, [])] for c in train_ids}
        n_events = sum(len(v) for v in history.values())
        del pre, camp
        gc.collect()
    log = decision_log(data, train) if with_decision_log else []
    with timings.step(f"agent: build (product path) and warm start on {len(train_ids)} train customers"):
        policy = build_agent_policy(list(inp.briefs), list(inp.content), costs=dict(inp.costs))
        policy.nightly(log, history, inp.now)
        del history
        gc.collect()
        ctx0 = DecisionContext(now=inp.now, briefs=inp.briefs, content=inp.content, seed=1)
        policy.prepare_day(train_states, ctx0)
    pi = np.zeros((len(test_states), data.n_arms), dtype=np.float64)
    kinds: Counter[str] = Counter()
    arms: Counter[int] = Counter()
    blocked: Counter[str] = Counter()
    reasons: list[str] = []
    nets: list[float] = []
    n_dec = 0
    t0 = time.perf_counter()
    with timings.step(f"agent: decide for {len(test_states)} test customers x {seeds} seeds"):
        for seed in range(1, seeds + 1):
            ctx = ctx0 if seed == 1 else ctx0.model_copy(update={"seed": seed})
            for j, st in enumerate(test_states):
                d = policy.decide(st, ctx)
                n_dec += 1
                a = inp.arm_of(d)
                pi[j, a] += 1.0
                arms[a] += 1
                kinds[d.kind.value] += 1
                if d.kind is DecisionKind.ACT:
                    nets.append(float(d.expected_net))
                for code in d.blocked_by:
                    blocked[code] += 1
                if seed == 1 and len(reasons) < 4 and (d.kind is DecisionKind.ACT or j % 5000 == 0):
                    reasons.append(d.reason)
    elapsed = time.perf_counter() - t0
    pi /= float(seeds)
    used = int(getattr(policy, "history_used", 0))
    learned = _learned(policy)
    del policy
    gc.collect()
    return AgentResult(
        pi=pi,
        expected_net_per_contact=float(np.mean(nets)) if nets else None,
        kinds=dict(kinds),
        arm_counts={str(k): v for k, v in sorted(arms.items())},
        blocked_by=dict(blocked.most_common(8)),
        sample_reasons=reasons,
        ms_per_decision=round(1000.0 * elapsed / max(n_dec, 1), 3),
        warm_start_sends_used=used,
        history_customers=len(train_ids),
        history_events=n_events,
        decision_log=len(log),
        seeds=seeds,
        learned=learned,
    )


# --- baselines -----------------------------------------------------------------------------------


@dataclass
class Design:
    """One-hot design: numeric columns cut at train quantiles, categoricals as their top levels."""

    numeric: tuple[str, ...]
    categorical: tuple[str, ...]
    bins: int = 6
    edges: dict[str, FloatArray] = field(default_factory=dict)
    levels: dict[str, list[str]] = field(default_factory=dict)

    def fit(self, frame: pd.DataFrame) -> Design:
        for col in self.numeric:
            v = frame[col].to_numpy(dtype=np.float64)
            qs = np.nanquantile(v, np.linspace(0, 1, self.bins + 1)[1:-1])
            self.edges[col] = np.unique(qs).astype(np.float64)
        for col in self.categorical:
            counts = frame[col].astype(str).value_counts()
            self.levels[col] = [str(x) for x in counts.index[:30]]
        return self

    def matrix(self, frame: pd.DataFrame) -> FloatArray:
        n = len(frame)
        blocks: list[FloatArray] = [np.ones((n, 1), dtype=np.float64)]
        for col in self.numeric:
            v = frame[col].to_numpy(dtype=np.float64)
            b = np.searchsorted(self.edges[col], v, side="right")
            oh = np.zeros((n, len(self.edges[col]) + 1), dtype=np.float64)
            oh[np.arange(n), b] = 1.0
            blocks.append(oh[:, 1:])
        for col in self.categorical:
            s = frame[col].astype(str).to_numpy()
            levels = self.levels[col]
            oh = np.zeros((n, max(len(levels) - 1, 0)), dtype=np.float64)
            for j, lv in enumerate(levels[1:]):
                oh[:, j] = s == lv
            blocks.append(oh)
        return np.hstack(blocks)


def fit_logistic(x: FloatArray, y: FloatArray, *, lam: float = 1.0, iters: int = 30) -> FloatArray:
    """L2-penalised logistic regression by Newton's method (IRLS); column 0 is the unpenalised intercept."""
    k = x.shape[1]
    beta = np.zeros(k, dtype=np.float64)
    mean = float(np.clip(y.mean(), 1e-6, 1 - 1e-6)) if len(y) else 0.5
    beta[0] = np.log(mean / (1.0 - mean))
    reg = lam * np.eye(k)
    reg[0, 0] = 0.0
    for _ in range(iters):
        z = np.clip(x @ beta, -30.0, 30.0)
        p = 1.0 / (1.0 + np.exp(-z))
        w = p * (1.0 - p)
        grad = x.T @ (y - p) - reg @ beta
        hess = (x * w[:, None]).T @ x + reg + 1e-9 * np.eye(k)
        step = np.linalg.solve(hess, grad)
        beta = beta + step
        if float(np.max(np.abs(step))) < 1e-7:
            break
    return beta


def predict_logistic(x: FloatArray, beta: FloatArray) -> FloatArray:
    z = np.clip(x @ beta, -30.0, 30.0)
    out: FloatArray = 1.0 / (1.0 + np.exp(-z))
    return out


def t_learner(data: BenchData, train: IntArray, test: IntArray) -> tuple[FloatArray, dict[str, Any]]:
    """T-learner: per arm, a logistic regression of conversion on the train rows of that arm; the
    expected profit of each arm is P(convert | x, arm) x the margin of a conversion - the arm's cost;
    the policy contacts with the arm of the highest expected profit (deterministic)."""
    design = Design(data.numeric, data.categorical).fit(data.features.iloc[train])
    x_tr = design.matrix(data.features.iloc[train])
    x_te = design.matrix(data.features.iloc[test])
    y = data.conversion[train]
    arms = data.arm[train]
    k = data.n_arms
    value_te = np.zeros((len(test), k), dtype=np.float64)
    per_conv: list[float] = []
    for a in range(k):
        rows = arms == a
        beta = fit_logistic(x_tr[rows], y[rows])
        p = predict_logistic(x_te, beta)
        if data.conversion_value is not None:
            v = data.conversion_value[test]
            per_conv.append(
                float(np.mean(data.conversion_value[train][rows & (y > 0)]))
                if np.any(rows & (y > 0))
                else 0.0
            )
        else:
            conv = rows & (y > 0)
            mean_v = float(data.margin_value[train][conv].mean()) if np.any(conv) else 0.0
            per_conv.append(mean_v)
            v = np.full(len(test), mean_v)
        value_te[:, a] = p * v - data.arm_cost[a]
    pi = np.zeros_like(value_te)
    pi[np.arange(len(test)), value_te.argmax(axis=1)] = 1.0
    info = {"columns": design.matrix(data.features.iloc[:2]).shape[1], "margin_per_conversion": per_conv}
    return pi, info


def fixed(n: int, k: int, arm: int) -> FloatArray:
    pi = np.zeros((n, k), dtype=np.float64)
    pi[:, arm] = 1.0
    return pi


def constant_policy(pi: FloatArray) -> Callable[[LoggedBandit], FloatArray]:
    """A precomputed probability matrix as the harness's policy function."""

    def policy(_logged: LoggedBandit) -> FloatArray:
        return pi

    return policy


def _dr_value(logged: LoggedBandit, pi: FloatArray) -> float:
    return float(harness_evaluate(constant_policy(pi), logged).dr.point)


def choose_simple(
    data: BenchData, train: IntArray, reward: FloatArray
) -> tuple[SegmentPolicy, str, list[dict[str, Any]]]:
    """On TRAIN only: the segment rule (columns by DR on a validation slice of train after fitting on
    the rest, then refitted on all of train) and the best simple policy: the highest of the segment
    rule's validation DR and each blanket policy's DR on all of train (a blanket policy is not
    fitted, so its train value is out of sample)."""
    tr_ids = data.customer_ids[train]
    fit_pos, val_pos = split_by_hash(tr_ids, salt=VALIDATION_SALT, test_share=VALIDATION_SHARE)
    fit_rows, val_rows = train[fit_pos], train[val_pos]
    log_fit = data.logged(fit_rows, reward, "r")
    log_val = data.logged(val_rows, reward, "r")
    table: list[dict[str, Any]] = []
    best_cols: tuple[str, ...] | None = None
    best_val = -np.inf
    for cols in data.segment_candidates:
        pol = fit_segment_policy(log_fit, segment_columns=list(cols))
        v = float(harness_evaluate(pol, log_val).dr.point)
        table.append({"policy": "segment rule (" + "+".join(cols) + ")", "validation_dr": v})
        if v > best_val:
            best_val, best_cols = v, tuple(cols)
    assert best_cols is not None
    candidates: dict[str, float] = {"segment rule": best_val}
    log_train = data.logged(train, reward, "r")
    for a in range(data.n_arms):
        # a blanket policy is not fitted, so its value on all of train is out of sample (and steadier)
        name = simple_name(data, a)
        v = _dr_value(log_train, fixed(log_train.n_rows, data.n_arms, a))
        candidates[name] = v
        table.append({"policy": name, "train_dr": v})
    best = max(candidates, key=lambda key: candidates[key])
    rule = fit_segment_policy(data.logged(train, reward, "r"), segment_columns=list(best_cols))
    return rule, best, table


def simple_name(data: BenchData, arm: int) -> str:
    return "send to none" if arm == 0 else f"send to all: {data.arm_names[arm]}"


# --- scoring -------------------------------------------------------------------------------------


def _est(e: Any) -> dict[str, float] | None:
    if e is None:
        return None
    return {"point": float(e.point), "low": float(e.low), "high": float(e.high)}


def dr_terms(logged: LoggedBandit, pi: FloatArray, q: FloatArray) -> FloatArray:
    rows = np.arange(logged.n_rows)
    w = pi[rows, logged.actions] / logged.propensity
    out: FloatArray = (pi * q).sum(axis=1) + w * (logged.reward - q[rows, logged.actions])
    return out


def score(
    data: BenchData, test: IntArray, reward: FloatArray, reward_name: str, policies: Mapping[str, FloatArray]
) -> tuple[dict[str, dict[str, Any]], dict[str, FloatArray]]:
    """Every policy's IPS / SNIPS / DR on TEST (repo harness) and its paired DR gain over send-to-none.
    Returns the table and each policy's per-row DR terms (for paired differences)."""
    logged = data.logged(test, reward, reward_name)
    q = per_action_mean_model(logged)
    terms: dict[str, FloatArray] = {}
    out: dict[str, dict[str, Any]] = {}
    none = dr_terms(logged, fixed(logged.n_rows, data.n_arms, 0), q)
    for name, pi in policies.items():
        est = harness_evaluate(constant_policy(pi), logged, policy_name=name)
        t = dr_terms(logged, pi, q)
        if abs(float(t.mean()) - est.dr.point) > 1e-9:
            raise AssertionError("DR terms disagree with the harness")
        terms[name] = t
        out[name] = {
            "contact_rate": float(1.0 - pi[:, 0].mean()),
            "arm_shares": [float(x) for x in pi.mean(axis=0)],
            "ips": _est(est.ips),
            "snips": _est(est.snips),
            "dr": _est(est.dr),
            "dr_vs_none": _est(ope.mean_interval(t - none)),
            "ess": float(est.effective_sample_size),
            "n": int(est.n_rows),
        }
    return out, terms


def paired(a: FloatArray, b: FloatArray) -> dict[str, float]:
    e = ope.mean_interval(a - b)
    return {"point": float(e.point), "low": float(e.low), "high": float(e.high)}


def calibration(expected: float | None, gain: Mapping[str, float], contact_rate: float) -> dict[str, Any]:
    """The agent's mean expected net per contact vs the DR-measured net value per contacted customer."""
    if contact_rate <= 0 or expected is None:
        return {
            "expected_per_contact": expected,
            "measured_per_contact": None,
            "ratio": None,
            "within_2x": None,
            "note": "the agent contacted nobody: nothing to calibrate",
        }
    measured = {k: float(v) / contact_rate for k, v in gain.items()}
    m = measured["point"]
    ratio = expected / m if m > 0 else None
    within = None if ratio is None else bool(1.0 / CALIBRATION_BAND <= ratio <= CALIBRATION_BAND)
    return {
        "expected_per_contact": expected,
        "measured_per_contact": measured,
        "ratio": ratio,
        "within_2x": within,
        "expected_inside_measured_ci": bool(measured["low"] <= expected <= measured["high"]),
        "note": "measured = DR gain of the agent over send-to-none / its contact rate (profit, net of cost)"
        + ("" if m > 0 else "; measured value is not above 0, so no ratio"),
    }


def verdict(
    vs_best: Mapping[str, float],
    vs_all: Mapping[str, float],
    vs_test_best: Mapping[str, float],
    calib: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "ties_or_beats_best_simple": bool(vs_best["high"] >= 0.0),
        "beats_best_simple": bool(vs_best["low"] > 0.0),
        "not_worse_than_send_to_all": bool(vs_all["high"] >= 0.0),
        "ties_or_beats_best_simple_on_test": bool(vs_test_best["high"] >= 0.0),
        "calibration_within_2x": calib.get("within_2x"),
        "calibration_expected_inside_measured_ci": calib.get("expected_inside_measured_ci"),
        "intervals": "95% normal, paired, NOT corrected for multiple comparisons: suggestive "
        "(Holm-Bonferroni corrected versions: report.multiplicity and datasets.<d>.verdict_corrected)",
    }


# --- many comparisons, one family: Holm-Bonferroni ----------------------------------------------------

ALPHA = 0.05
NON_INFERIORITY_SHARE = 0.02
"""Pre-registered tolerance: the agent may be this share of the best simple policy's value (DR, in
the dataset's own profit units) worse and still count as "as good as" it. Fixed before the sealed
check; see docs/v2/TARGETS.json."""


def normal_two_sided_p(z: float) -> float:
    """P(|Z| >= |z|) for a standard normal."""
    return math.erfc(abs(z) / math.sqrt(2.0))


def holm_adjust(p_values: Sequence[float]) -> list[float]:
    """Holm's step-down adjusted p-values, in the input order.

    Sort ascending, multiply the i-th smallest (0-based) by m - i, take the running maximum so the
    adjusted values never decrease, cap at 1. Rejecting every hypothesis whose adjusted p is below
    alpha controls the chance of any false rejection at alpha, with no assumption on how the
    comparisons depend on each other (they share customers, so they do depend)."""
    m = len(p_values)
    order = sorted(range(m), key=lambda i: p_values[i])
    adjusted = [0.0] * m
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (m - rank) * p_values[i]))
        adjusted[i] = running
    return adjusted


def bonferroni_interval(est: Mapping[str, float], m: int, alpha: float = ALPHA) -> dict[str, float]:
    """The interval of `est` widened to level 1 - alpha/m, from its own standard error.

    `est` has point / low / high at the usual 95%. Simultaneously valid for all m comparisons; it is
    the interval that goes with a Bonferroni test and is conservative for Holm's (which rejects at
    least as often)."""
    se = (est["high"] - est["low"]) / (2.0 * Z_95)
    z = NormalDist().inv_cdf(1.0 - alpha / (2.0 * m))
    return {
        "point": est["point"],
        "low": est["point"] - z * se,
        "high": est["point"] + z * se,
        "level": 1.0 - alpha / m,
    }


def multiplicity(comparisons: Mapping[str, Mapping[str, float]], alpha: float = ALPHA) -> dict[str, Any]:
    """Raw and corrected statistics for one family of comparisons (each a paired difference with a
    95% interval: `point`, `low`, `high`).

    Per comparison: the raw interval and two-sided normal p, the Holm adjusted p, the Bonferroni-
    widened interval (level 1 - alpha/m), and whether it is significant raw and after Holm."""
    keys = list(comparisons)
    m = len(keys)
    out: dict[str, Any] = {
        "method": "Holm-Bonferroni (adjusted p) with Bonferroni-widened intervals",
        "alpha": alpha,
        "m": m,
        "comparisons": {},
    }
    if m == 0:
        return out
    stats: list[tuple[float, float]] = []
    for k in keys:
        e = comparisons[k]
        se = (e["high"] - e["low"]) / (2.0 * Z_95)
        zero_width = 1.0 if e["point"] == 0 else 0.0
        p = normal_two_sided_p(e["point"] / se) if se > 0 else zero_width
        stats.append((se, p))
    adj = holm_adjust([p for _, p in stats])
    for k, (se, p), ap in zip(keys, stats, adj, strict=True):
        e = comparisons[k]
        out["comparisons"][k] = {
            "raw": {"point": e["point"], "low": e["low"], "high": e["high"], "se": se, "p": p},
            "holm_p": ap,
            "corrected": bonferroni_interval(e, m, alpha),
            "significant_raw": bool(p < alpha),
            "significant_holm": bool(ap < alpha),
        }
    return out


def agent_comparisons(block: Mapping[str, Any]) -> dict[str, dict[str, float]]:
    """'Agent minus P' (paired DR difference) for every policy P on one dataset's scoreboard."""
    return {
        f"agent minus {name}": dict(p["agent_minus_this"])
        for name, p in block["policies"].items()
        if name != "AGENT"
    }


def add_multiplicity(report: dict[str, Any], alpha: float = ALPHA) -> None:
    """Add the multiple-comparison tables to a report, in place.

    Two families, both reported: each dataset's own comparisons (`datasets.<d>.multiplicity`) and all
    comparisons of every dataset in the report (`multiplicity`, the family the corrected verdicts
    use). Then `datasets.<d>.verdict_corrected`: the pre-registered targets judged on the
    report-wide corrected intervals (see docs/v2/TARGETS.json)."""
    blocks = {n: b for n, b in report["datasets"].items() if "error" not in b}
    family: dict[str, Mapping[str, float]] = {}
    for name, b in blocks.items():
        own = agent_comparisons(b)
        b["multiplicity"] = multiplicity(own, alpha)
        for key, est in own.items():
            family[f"{name}: {key}"] = est
    report["multiplicity"] = {
        **multiplicity(family, alpha),
        "family": "every 'agent minus policy' comparison of every dataset in this report",
    }
    comps = report["multiplicity"]["comparisons"]
    for name, b in blocks.items():
        b["verdict_corrected"] = corrected_verdict(b, comps, name)


def corrected_verdict(block: Mapping[str, Any], family: Mapping[str, Any], name: str) -> dict[str, Any]:
    """The pre-registered bench targets for one dataset, on the report-wide corrected intervals.

    * `non_inferior_to_best_simple`: the corrected lower bound of (agent - best simple policy) is
      above minus `NON_INFERIORITY_SHARE` of the best simple policy's DR value.
    * `beats_best_simple_where_signal`: where the T-learner beats the best simple policy (features
      carry signal: its raw lower bound is above 0), the corrected lower bound of (agent - best) is
      above 0. None where no signal is shown (not applicable).
    * `calibration_within_2x`: the agent's expected net per contact is within 2x of the DR-measured
      value (None if it contacted nobody or the measured value is not positive)."""
    best = block["best_simple"]["name"]
    comp = family[f"{name}: agent minus {best}"]
    best_value = float(block["policies"][best]["dr"]["point"])
    tolerance = NON_INFERIORITY_SHARE * abs(best_value)
    low = float(comp["corrected"]["low"])
    signal = bool(block["feature_signal"]["t_learner_minus_best_simple"]["low"] > 0.0)
    return {
        "best_simple": best,
        "tolerance": tolerance,
        "tolerance_rule": f"{NON_INFERIORITY_SHARE:.0%} of the best simple policy's DR value ({best_value:.6g})",
        "corrected_low_vs_best": low,
        "holm_p_vs_best": comp["holm_p"],
        "non_inferior_to_best_simple": bool(low > -tolerance),
        "features_carry_signal": signal,
        "beats_best_simple_where_signal": (bool(low > 0.0) if signal else None),
        "calibration_within_2x": block["calibration"].get("within_2x"),
    }


def evaluate_dataset(
    data: BenchData,
    *,
    seeds: int,
    timings: Timings,
    with_decision_log: bool = False,
    attach_features: bool = True,
) -> dict[str, Any]:
    """The whole scoreboard for one dataset: split, agent, baselines, scores, calibration, verdict."""
    train, test = split_by_hash(data.customer_ids)
    profit = data.profit()
    agent = run_agent(
        data,
        train,
        test,
        seeds=seeds,
        timings=timings,
        with_decision_log=with_decision_log,
        attach_features=attach_features,
    )
    with timings.step("baselines on train (segment rule, T-learner, best simple policy)"):
        rule, best_name, val_table = choose_simple(data, train, profit)
        log_te = data.logged(test, profit, "profit")
        rule_pi = rule(log_te)
        t_pi, t_info = t_learner(data, train, test)
    k, nt = data.n_arms, len(test)
    rule_name = "segment rule (" + "+".join(rule.columns) + ")"
    policies: dict[str, FloatArray] = {simple_name(data, a): fixed(nt, k, a) for a in range(k)}
    policies[rule_name] = rule_pi
    policies["T-learner (logistic)"] = t_pi
    policies["AGENT"] = agent.pi
    best_policy = rule_name if best_name == "segment rule" else best_name
    # send to all: the sent arm with the best mean profit on TRAIN (a randomised trial: plain means)
    tr_means = [float(profit[train][data.arm[train] == a].mean()) for a in range(1, k)]
    all_name = simple_name(data, 1 + int(np.argmax(tr_means)))
    none_mean = float(profit[train][data.arm[train] == 0].mean())
    blanket_name = all_name if max(tr_means) > none_mean else simple_name(data, 0)
    with timings.step("score on test (IPS / SNIPS / DR)"):
        table, terms = score(data, test, profit, "profit", policies)
        conv_table, _ = score(data, test, data.conversion, "conversion", policies)
    for name in policies:
        table[name]["agent_minus_this"] = paired(terms["AGENT"], terms[name])
    simple = [simple_name(data, a) for a in range(k)] + [rule_name]
    best_on_test = max(simple, key=lambda n: table[n]["dr"]["point"])
    vs_best = paired(terms["AGENT"], terms[best_policy])
    vs_all = paired(terms["AGENT"], terms[all_name])
    vs_test_best = paired(terms["AGENT"], terms[best_on_test])
    calib = calibration(
        agent.expected_net_per_contact, table["AGENT"]["dr_vs_none"], table["AGENT"]["contact_rate"]
    )
    return {
        "dataset": data.name,
        "rows": data.n,
        "train": len(train),
        "test": len(test),
        "split": {"by": "customer id hash (BLAKE2b)", "salt": SPLIT_SALT, "test_share": TEST_SHARE},
        "arms": list(data.arm_names),
        "treated_share": float((data.arm != 0).mean()),
        "economics": data.economics,
        "reward": "profit",
        "notes": list(data.notes),
        "sizes": data.sizes,
        "agent": {
            "value": {
                "dr": table["AGENT"]["dr"],
                "ips": table["AGENT"]["ips"],
                "snips": table["AGENT"]["snips"],
            },
            "contact_rate": table["AGENT"]["contact_rate"],
            "arm_shares": table["AGENT"]["arm_shares"],
            "expected_net_per_contact": agent.expected_net_per_contact,
            "kinds": agent.kinds,
            "arm_counts": agent.arm_counts,
            "blocked_by": agent.blocked_by,
            "sample_reasons": agent.sample_reasons,
            "ms_per_decision": agent.ms_per_decision,
            "warm_start_sends_used": agent.warm_start_sends_used,
            "history_customers": agent.history_customers,
            "history_events": agent.history_events,
            "decision_log_fed": agent.decision_log,
            "seeds": agent.seeds,
            "features_attached": attach_features,
            "learned": list(agent.learned),
        },
        "policies": table,
        "conversion": conv_table,
        "best_simple": {
            "name": best_policy,
            "chosen_on": (
                f"train only: blanket policies by DR on all of train; the segment rule by DR on a "
                f"{VALIDATION_SHARE:.0%} validation slice of train (salt {VALIDATION_SALT}) after fitting on the rest"
            ),
            "validation": val_table,
            "agent_minus_best_simple": vs_best,
        },
        "send_to_all": {"name": all_name, "agent_minus_send_to_all": vs_all},
        "best_simple_on_test": {
            "name": best_on_test,
            "agent_minus": vs_test_best,
            "note": "diagnostic only: chosen on TEST, so it flatters the baseline",
        },
        "t_learner": t_info,
        "feature_signal": {
            "best_blanket_on_train": blanket_name,
            "segment_rule_minus_best_blanket": paired(terms[rule_name], terms[blanket_name]),
            "t_learner_minus_best_blanket": paired(terms["T-learner (logistic)"], terms[blanket_name]),
            "t_learner_minus_best_simple": paired(terms["T-learner (logistic)"], terms[best_policy]),
            "note": "do customer features carry signal here? A feature policy (segment rule or "
            "T-learner) beating the best blanket policy (none or send to all, chosen on train) says "
            "yes; where it does, the agent should beat the best blanket policy too",
        },
        "calibration": calib,
        "verdict": verdict(vs_best, vs_all, vs_test_best, calib),
    }
