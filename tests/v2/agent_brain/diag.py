"""Diagnostics for the agent brain: the fixed rule it must beat, and a small comparison runner.

The fixed rule is the dumb policy that beat the agent in `docs/v2/GYM_RESULTS.md`: WhatsApp if the
customer consented, else email; no offer; one fixed local hour; everyone, as often as the weekly cap
allows. It sees only what a real policy sees (customer state and the decision context). It lives here,
not in `autonomy/gym`, because it is a yardstick for this stream, not part of the Gym.

    python -m tests.v2.agent_brain.diag --scenarios baseline_shop send_time_matters --seeds 2

prints, per scenario and seed, each policy's true value and share of the oracle's value, and what it
did: contacts per customer-week, the average true value of one contact, and the mix of channels,
offers and "contacts this week" over its sends (from the contact log the world keeps). Policies:
`agent`, `fixed14` and `fixed18` (the fixed rule at 14:00 or 18:00 local). The scoreboard reads the
truth to score; nothing here hands it to a policy.

Keep runs small (<= 1,000 customers, <= 2 seeds): a 1,000-customer, 90-day agent run takes about a
minute.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from autonomy.contracts import (
    Action,
    Brief,
    Channel,
    ContentItem,
    CustomerState,
    Decision,
    DecisionContext,
    DecisionKind,
    Reward,
    stable_id,
)
from autonomy.gym.api import ScenarioSpec
from autonomy.gym.baselines import next_local_time, send_hour, usable_content, weekly_cap
from autonomy.gym.scenarios import default_brief, default_content, get_scenario
from autonomy.gym.score import (
    ORACLE,
    SimWorld,
    assign_group,
    gym_world,
    oracle_factory,
    run_policy,
    split_shares,
)
from autonomy.interfaces import Policy

FIXED_HOUR = 14
"""The fixed rule's local send hour. GYM_RESULTS measured 14:00 (57%) and 18:00 (59%) on
baseline_shop; we use 14:00 so the yardstick is not the best of several tries."""

MONEY_SCENARIOS: tuple[str, ...] = (
    "baseline_shop",
    "sleeping_dogs_15",
    "discount_cannibalisation",
    "send_time_matters",
)


@dataclass
class FixedRule:
    """WhatsApp if consented else email, no offer, one fixed local hour, everyone up to the cap.

    The rule remembers its own scheduled sends so that a send timed for tomorrow counts towards the
    weekly cap before the customer's state shows it (the same trap `AutonomousPolicy` guards against).
    """

    hour: int = FIXED_HOUR
    version: str = "fixed-rule-v1"
    _sends: dict[str, list[datetime]] = field(default_factory=dict)

    def learn(self, rewards: Sequence[Reward], decisions: Mapping[str, Decision]) -> None:
        return None

    def _nothing(self, state: CustomerState, ctx: DecisionContext, reason: str) -> Decision:
        return Decision(
            decision_id=stable_id("dec", state.customer_id, ctx.now.isoformat(), self.version),
            customer_id=state.customer_id,
            made_at=ctx.now,
            kind=DecisionKind.NOTHING,
            group=state.group,
            probability=1.0,
            reason=reason,
            policy_version=self.version,
        )

    def decide(self, state: CustomerState, ctx: DecisionContext) -> Decision:
        reachable = state.contactable_channels()
        if state.open_complaint:
            return self._nothing(state, ctx, "Open complaint.")
        cap = weekly_cap(ctx.briefs)
        for brief in ctx.briefs:
            order = (Channel.WHATSAPP, Channel.EMAIL)
            channel = next((c for c in order if c in reachable and c in brief.envelope.channels), None)
            if channel is None:
                continue
            hour = send_hour(self.hour, brief.envelope)
            if hour is None:
                continue
            item = next(
                (c for c in usable_content(ctx.content, brief, channel) if c.offer_id == "none"), None
            )
            if item is None:
                continue
            send_at = next_local_time(ctx.now, state.timezone, hour)
            mine = self._sends.setdefault(state.customer_id, [])
            folded = [t for t in state.recent_contacts if t > send_at - timedelta(days=7)]
            pending = [t for t in mine if t > send_at - timedelta(days=7)]
            # A send we scheduled shows up in recent_contacts once it went out: count it once.
            seen = set(folded)
            total = len(folded) + sum(1 for t in pending if t not in seen)
            if total >= cap:
                return self._nothing(state, ctx, "Weekly cap reached.")
            if any(abs((t - send_at).total_seconds()) < 6 * 3600 for t in mine):
                return self._nothing(state, ctx, "A message is already scheduled.")
            mine[:] = [t for t in mine if t > ctx.now - timedelta(days=8)] + [send_at]
            action = Action(
                goal_id=brief.goal_id,
                channel=channel,
                offer_id="none",
                content_id=item.content_id,
                format=item.format,
                send_at=send_at,
            )
            return Decision(
                decision_id=stable_id("dec", state.customer_id, ctx.now.isoformat(), self.version),
                customer_id=state.customer_id,
                made_at=ctx.now,
                kind=DecisionKind.ACT,
                action=action,
                group=state.group,
                probability=1.0,
                reason=f"Fixed rule: {channel.value}, no offer, {hour}:00 local.",
                policy_version=self.version,
            )
        return self._nothing(state, ctx, "No allowed channel.")


def fixed_rule_factory(hour: int = FIXED_HOUR) -> Callable[[SimWorld], Policy]:
    def factory(world: SimWorld) -> Policy:
        return FixedRule(hour=hour)

    return factory


def agent_factory(world: SimWorld) -> Policy:
    from autonomy.runtime.agent import build_agent_policy

    return build_agent_policy([default_brief()], default_content())


@dataclass
class RunResult:
    scenario: str
    policy: str
    seed: int
    true_value: float
    oracle_value: float
    seconds: float
    contacts_per_week: float
    mix: dict[str, float]
    mean_value_per_contact: float

    @property
    def share(self) -> float:
        return self.true_value / self.oracle_value if self.oracle_value > 1e-9 else float("nan")


class _Capture:
    """A world factory that keeps the world, so the contact log can be read after the run."""

    def __init__(self) -> None:
        self.world: SimWorld | None = None

    def __call__(
        self, spec: ScenarioSpec, briefs: Sequence[Brief], content: Sequence[ContentItem]
    ) -> SimWorld:
        self.world = gym_world(spec, briefs, content)
        return self.world


def _mix(world: SimWorld | None, agent_ids: set[str]) -> tuple[dict[str, float], float]:
    contacts = [c for c in getattr(world, "contacts", ()) if c.customer_id in agent_ids]
    if not contacts:
        return {}, 0.0
    n = len(contacts)
    counts: Counter[str] = Counter()
    for c in contacts:
        counts[f"ch:{c.channel.value}"] += 1
        counts[f"offer:{c.offer_id}"] += 1
        counts[f"wk:{c.contacts_7d}"] += 1
        if c.sleeping_dog:
            counts["dog"] += 1
    return {k: v / n for k, v in sorted(counts.items())}, sum(c.expected_net for c in contacts) / n


def run_one(
    scenario: str,
    policy_name: str,
    factory: Callable[[SimWorld], Policy],
    seed: int,
    customers: int,
    oracle_value: float,
    days: int | None = None,
) -> RunResult:
    overrides: dict[str, object] = {"n_customers": customers}
    if days is not None:
        overrides["days"] = days
    spec = get_scenario(scenario, **overrides)
    capture = _Capture()
    started = time.perf_counter()
    card = run_policy(
        spec, factory, seed, policy_name=policy_name, world_factory=capture, oracle_value=oracle_value
    )
    took = time.perf_counter() - started
    hs, bs = split_shares([default_brief()])
    salt = f"gym-split:{spec.name}:{seed}"
    world = capture.world
    ids = set(getattr(world, "customer_ids", ()))
    agent_ids = {
        c for c in ids if assign_group(c, holdout_share=hs, baseline_share=bs, salt=salt).value == "agent"
    }
    mix, per_contact = _mix(world, agent_ids)
    return RunResult(
        scenario=scenario,
        policy=policy_name,
        seed=seed,
        true_value=card.true_value,
        oracle_value=oracle_value,
        seconds=took,
        contacts_per_week=card.contacts_per_customer_week,
        mix=mix,
        mean_value_per_contact=per_contact,
    )


def oracle_value(scenario: str, seed: int, customers: int, days: int | None = None) -> float:
    overrides: dict[str, object] = {"n_customers": customers}
    if days is not None:
        overrides["days"] = days
    spec = get_scenario(scenario, **overrides)
    return run_policy(
        spec, oracle_factory, seed, policy_name=ORACLE, with_oracle=False, learn=False
    ).true_value


POLICIES: dict[str, Callable[[SimWorld], Policy]] = {
    "agent": agent_factory,
    "fixed14": fixed_rule_factory(14),
    "fixed18": fixed_rule_factory(18),
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.v2.agent_brain.diag")
    parser.add_argument("--scenarios", nargs="+", default=list(MONEY_SCENARIOS))
    parser.add_argument("--policies", nargs="+", default=["agent", "fixed14"])
    parser.add_argument("--seeds", type=int, default=2)
    parser.add_argument("--first-seed", type=int, default=0)
    parser.add_argument("--customers", type=int, default=1000)
    parser.add_argument("--days", type=int, default=None)
    args = parser.parse_args(argv)
    rows: list[RunResult] = []
    for scenario in args.scenarios:
        for seed in range(args.first_seed, args.first_seed + args.seeds):
            ov = oracle_value(scenario, seed, args.customers, args.days)
            for name in args.policies:
                r = run_one(scenario, name, POLICIES[name], seed, args.customers, ov, args.days)
                rows.append(r)
                top = {k: round(v, 2) for k, v in r.mix.items()}
                print(
                    f"{scenario:26s} seed {seed} {name:8s} true ${r.true_value:9,.0f} "
                    f"oracle ${ov:9,.0f} share {r.share:6.1%} c/wk {r.contacts_per_week:.2f} "
                    f"$/contact {r.mean_value_per_contact:.3f} {r.seconds:5.1f}s {top}",
                    flush=True,
                )
    print("\nPooled over seeds (sum of true values / sum of oracle values):")
    for scenario in args.scenarios:
        line = [f"{scenario:26s}"]
        for name in args.policies:
            mine = [r for r in rows if r.scenario == scenario and r.policy == name]
            tv = sum(r.true_value for r in mine)
            ov = sum(r.oracle_value for r in mine)
            line.append(f"{name} {tv / ov:6.1%}" if ov > 0 else f"{name} n/a")
        print("  ".join(line))
    return 0


if __name__ == "__main__":
    sys.exit(main())
