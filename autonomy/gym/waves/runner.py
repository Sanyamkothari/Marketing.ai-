"""Run one policy in one world on one seed, through the Gym's loop, and score it exactly.

`run_policy` (`autonomy.gym.score`) drives the day-by-day loop: events folded into customer state, one decision per
non-holdout customer per day, decisions acted on, time moved on. This module adds the benchmark's own score:

* **True value** (`true_value`): the incremental net margin of every contact the policy made, from the hidden
  truth, against the same customers' no-message world: expected margin of the incremental purchases over the 14-day
  window, less the pull-forward payback in the following 30 days, less message costs, less the expected opt-out cost.
  It is computed from the contact ledger, so it counts the whole tail beyond day 84, and it has no sampling noise.
  It is the same expectation the oracle maximises and every policy is ranked on.
* **Measured value** (`measured_*`): what a business could read off its own books at day 84: the contacted
  customers' mean realised net minus the holdout's, times the contacted group's size, with a 95% interval. It
  sees only the 84 days, so late sends and the payback are missing, and it is noisy. Putting it beside the true
  value shows over-claiming.

Common random numbers: the world draws every purchase, thinning, click and opt-out roll from `(seed, customer,
day)` alone, so policies in the same world and seed face the same luck. The customers and their real receipts
are the same in every world and seed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, cast

import numpy as np
import numpy.typing as npt

from autonomy.contracts import Channel, Group
from autonomy.gym.score import SimWorld, assign_group, difference_interval, run_policy
from autonomy.gym.waves.config import HOLDOUT_SHARE, HORIZON_DAYS, WorldSpec
from autonomy.gym.waves.data import WavesData
from autonomy.gym.waves.policies import make_factory, run_briefs
from autonomy.gym.waves.setup import waves_brief, waves_content, waves_spec, world_factory
from autonomy.gym.waves.truth import CHANNEL_INDEX
from autonomy.gym.waves.world import WavesWorld
from autonomy.interfaces import Policy

FloatArray = npt.NDArray[np.float64]


@dataclass
class RunResult:
    """One policy, one world, one seed."""

    row: dict[str, Any]
    by_customer: FloatArray
    """True value per customer (0 for the holdout), for paired differences between policies."""


def split_salt(world_name: str, seed: int) -> str:
    return f"gym-split:waves_{world_name}:{seed}"


def groups_of(data: WavesData, salt: str) -> list[Group]:
    return [
        assign_group(c, holdout_share=HOLDOUT_SHARE, baseline_share=0.0, salt=salt) for c in data.customer_ids
    ]


def exact_score(world: WavesWorld, groups: list[Group]) -> dict[str, Any]:
    """The true value and its parts, from the ledger of the contacts the run made."""
    led = world.truth.ledger.merged()
    n = world.n
    idx = np.asarray(led["idx"], dtype=np.int64)

    def per_customer(key: str) -> FloatArray:
        return np.asarray(
            np.bincount(idx, weights=np.asarray(led[key], dtype=np.float64), minlength=n), dtype=np.float64
        )

    gross, gross_7d = per_customer("gross"), per_customer("gross_7d")
    cost, unsub = per_customer("cost"), per_customer("unsubscribe_cost")
    true = gross - cost - unsub
    agent = np.array([g is not Group.HOLDOUT for g in groups], dtype=np.bool_)
    holdout_contacts = int(np.count_nonzero(np.bincount(idx, minlength=n)[~agent]))
    sends = np.bincount(idx, minlength=n)
    delivered = np.bincount(idx[np.asarray(led["delivered"], dtype=np.bool_)], minlength=n)
    n_agent = int(agent.sum())
    weeks = world.days / 7.0
    channel = np.asarray(led["channel"], dtype=np.int64)
    return {
        "true_value": float(true.sum()),
        "gross_margin": float(gross.sum()),
        "message_cost": float(cost.sum()),
        "unsubscribe_cost_expected": float(unsub.sum()),
        "true_value_7d_reading": float((gross_7d - cost - unsub).sum()),
        "contacts": int(sends.sum()),
        "delivered": int(delivered.sum()),
        "sms_contacts": int((channel == CHANNEL_INDEX[Channel.SMS]).sum()),
        "email_contacts": int((channel == CHANNEL_INDEX[Channel.EMAIL]).sum()),
        "contacts_per_customer_week": float(sends[agent].sum() / n_agent / weeks) if n_agent else 0.0,
        "share_contacted": float((sends[agent] > 0).mean()) if n_agent else 0.0,
        "holdout_contacted": holdout_contacts,
        "_by_customer": true,
    }


def measured_value(world: WavesWorld, groups: list[Group], seed: int) -> dict[str, float]:
    """The holdout comparison at day 84: contacted group's mean realised net minus the holdout's, scaled to the
    contacted group's size, with a normal 95% interval (the Gym's `difference_interval`)."""
    net = world.measured_net()
    agent = np.array([g is Group.AGENT for g in groups], dtype=np.bool_)
    holdout = np.array([g is Group.HOLDOUT for g in groups], dtype=np.bool_)
    if not agent.any() or not holdout.any():
        return {"measured": 0.0, "measured_low": 0.0, "measured_high": 0.0}
    est = difference_interval(net[agent], net[holdout], scale=int(agent.sum()), method="normal", seed=seed)
    return {"measured": est.point, "measured_low": est.low, "measured_high": est.high}


def run_one(
    data: WavesData,
    world: WorldSpec,
    policy: str,
    seed: int,
    *,
    days: int = HORIZON_DAYS,
) -> RunResult:
    """Run `policy` for `days` decision days in `world` on `seed`, through the Gym's loop, and score it."""
    started = time.perf_counter()
    brief, content = waves_brief(), waves_content()
    briefs = run_briefs(policy, brief)
    spec = waves_spec(world, data, seed=seed, days=days)
    holder: list[SimWorld] = []
    made: list[Policy] = []
    build = make_factory(policy, brief, content)

    def factory(w: SimWorld) -> Policy:
        built_policy = build(w)
        made.append(built_policy)
        return built_policy

    salt = split_salt(world.name, seed)
    card = run_policy(
        spec,
        factory,
        seed,
        briefs,
        content,
        policy_name=policy,
        world_factory=world_factory(data, world, holder),
        with_oracle=False,
        learn=True,
        split_salt=salt,
    )
    built = cast(WavesWorld, holder[-1])
    groups = groups_of(data, salt)
    score = exact_score(built, groups)
    by_customer = score.pop("_by_customer")
    measured = measured_value(built, groups, seed)
    n_agent = card.n_agent
    row: dict[str, Any] = {
        "world": world.name,
        "policy": policy,
        "seed": seed,
        "customers": data.n,
        "n_agent": n_agent,
        "n_holdout": card.n_holdout,
        "days": days,
        **score,
        **measured,
        "measured_over_true": (
            measured["measured"] / score["true_value"] if abs(score["true_value"]) > 1e-9 else None
        ),
        "unsubscribe_rate": card.unsubscribe_rate,
        "violations": card.violations,
        "real_receipts_removed": built.real_receipts_removed,
        "incremental_purchases": built.incremental_purchases,
        "seconds": round(time.perf_counter() - started, 1),
    }
    diagnostics = getattr(made[-1], "diagnostics", None) if made else None
    if callable(diagnostics):
        row["policy_diagnostics"] = diagnostics()
    return RunResult(row=row, by_customer=np.asarray(by_customer, dtype=np.float64))
