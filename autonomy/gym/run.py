"""Run the Gym from the command line and print a plain-English scoreboard.

    python -m autonomy.gym.run --scenario sleeping_dogs_15 --policy agent --seeds 5 --customers 2000

By default the chosen policy is compared with send-to-all, the rule baseline and the oracle on the
same seeds, then judged against the plan's pass bar. `--no-compare` scores the policy alone.

Policies are found by name in a registry. `agent` is Autonomy's own policy
(`autonomy.runtime.agent.build_agent_policy`). Another stream adds its own with
`register_policy("name", lambda world: MyPolicy(...))`; the factory gets the world so a fresh
policy is built for every run (the oracle uses the world's truth; nothing else may).
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Sequence

from autonomy.contracts import Brief, ContentItem
from autonomy.fakes.policies import NothingPolicy
from autonomy.gym.api import ScenarioSpec
from autonomy.gym.scenarios import SCENARIOS, default_brief, default_content, get_scenario
from autonomy.gym.score import (
    EVERYONE,
    NOTHING,
    ORACLE,
    RULE,
    PolicyFactory,
    ScoreCard,
    SimWorld,
    WorldFactory,
    compare,
    everyone_factory,
    oracle_factory,
    pass_criteria,
    rule_factory,
    run_policy,
)
from autonomy.interfaces import Policy

AGENT = "agent"


def _nothing_factory(world: SimWorld) -> Policy:  # noqa: ARG001
    return NothingPolicy()


def agent_factory(world: SimWorld) -> Policy:  # noqa: ARG001
    """A fresh Autonomy agent for one run. It never sees the world's truth: the world is passed only
    because every factory gets it. It starts from the Gym's default brief and library and picks up
    whatever briefs and content the run's decision context carries."""
    from autonomy.runtime.agent import build_agent_policy

    return build_agent_policy([default_brief()], default_content())


POLICIES: dict[str, PolicyFactory] = {
    EVERYONE: everyone_factory,
    RULE: rule_factory,
    ORACLE: oracle_factory,
    NOTHING: _nothing_factory,
    AGENT: agent_factory,
}


def register_policy(name: str, factory: PolicyFactory, *, replace: bool = False) -> None:
    """Make a policy runnable by name. Refuses to overwrite a name unless `replace` is set."""
    if name in POLICIES and not replace:
        raise ValueError(f"a policy called {name!r} is already registered")
    POLICIES[name] = factory


def get_policy(name: str) -> PolicyFactory:
    try:
        return POLICIES[name]
    except KeyError as exc:
        raise KeyError(f"unknown policy {name!r}; known: {', '.join(sorted(POLICIES))}") from exc


def make_world(spec: ScenarioSpec, briefs: Sequence[Brief], content: Sequence[ContentItem]) -> SimWorld:
    """The Gym world for a scenario, imported lazily so this module loads without it."""
    from autonomy.gym.score import gym_world

    return gym_world(spec, briefs, content)


# --- plain English -------------------------------------------------------------------------------


def money(x: float) -> str:
    return f"-${abs(x):,.0f}" if x < 0 else f"${x:,.0f}"


def describe(card: ScoreCard) -> str:
    """One card as a sentence or three a marketer can read."""
    inc = card.incremental_profit
    covered = "inside" if card.interval_covers_truth else "OUTSIDE"
    parts = [
        f"Seed {card.seed}: {money(inc.point)} more profit than the holdout "
        f"(95% range {money(inc.low)} to {money(inc.high)}); the true value was {money(card.true_value)}, "
        f"{covered} the range."
    ]
    if card.oracle_share is not None and card.oracle_value is not None:
        parts.append(f"Captured {card.oracle_share:.0%} of the oracle's {money(card.oracle_value)}.")
    elif card.oracle_value is not None:
        parts.append(f"The oracle found {money(card.oracle_value)} to gain.")
    parts.append(
        f"{card.contacts_per_customer_week:.2f} contacts per customer per week; "
        f"{card.unsubscribe_rate:.1%} unsubscribed; {card.violations} rule breaks."
    )
    if card.sleeping_dog_contact_share is not None:
        parts.append(
            f"Contacted {card.sleeping_dogs_contacted} of {card.sleeping_dogs} sleeping dogs "
            f"({card.sleeping_dog_contact_share:.0%})."
        )
    if card.planted_harm:
        parts.append(
            "Never stopped sending."
            if card.breaker_batches is None
            else f"Stopped sending after {card.breaker_batches} batches."
        )
    return " ".join(parts)


def main(argv: Sequence[str] | None = None, *, world_factory: WorldFactory | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m autonomy.gym.run", description="Run the Gym and print a plain-English scoreboard."
    )
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), help="Scenario to run")
    parser.add_argument("--policy", default=RULE, help="Registered policy name (default: rule_baseline)")
    parser.add_argument("--seeds", type=int, default=3, help="How many seeds, counting from --first-seed")
    parser.add_argument("--first-seed", type=int, default=0)
    parser.add_argument("--customers", type=int, default=None, help="Override the scenario's customers")
    parser.add_argument("--days", type=int, default=None, help="Override the scenario's decision days")
    parser.add_argument("--no-compare", action="store_true", help="Score the policy alone, no pass bar")
    parser.add_argument("--no-learn", action="store_true", help="Do not call policy.learn each night")
    parser.add_argument("--list", action="store_true", help="List scenarios and policies, then exit")
    args = parser.parse_args(argv)

    if args.list or args.scenario is None:
        print("Scenarios:")
        for name, s in sorted(SCENARIOS.items()):
            print(f"  {name}: {s.description}")
        print("Policies: " + ", ".join(sorted(POLICIES)))
        return 0 if args.list else 2

    if args.policy not in POLICIES:
        parser.error(f"unknown policy {args.policy!r}; known: {', '.join(sorted(POLICIES))}")
    overrides: dict[str, object] = {}
    if args.customers is not None:
        overrides["n_customers"] = args.customers
    if args.days is not None:
        overrides["days"] = args.days
    spec = get_scenario(args.scenario, **overrides)
    factory = get_policy(args.policy)
    seeds = list(range(args.first_seed, args.first_seed + args.seeds))
    worlds = world_factory or make_world
    learn = not args.no_learn

    print(f"Scenario {spec.name}: {spec.description}")
    print(
        f"Policy {args.policy} on {len(seeds)} seed(s), {spec.n_customers:,} customers, "
        f"{spec.days} decision days.\n"
    )
    started = time.perf_counter()
    if args.no_compare:
        cards = [
            run_policy(spec, factory, seed, policy_name=args.policy, world_factory=worlds, learn=learn)
            for seed in seeds
        ]
        for card in cards:
            print(describe(card))
    else:
        names = [args.policy, EVERYONE, RULE, ORACLE]
        policies = {n: get_policy(n) for n in dict.fromkeys(names)}
        result = compare(spec, policies, seeds, world_factory=worlds, learn=learn)
        for card in result.cards:
            if card.policy == args.policy:
                print(describe(card))
        print("\nAverages over seeds:")
        print(result.summary().to_string(index=False, float_format=lambda x: f"{x:,.3f}"))
        if args.policy in (EVERYONE, RULE, ORACLE):
            print(f"\n{args.policy} is a baseline itself; the pass bar is for the policy under test.")
        else:
            print("\nAgainst the pass bar (plan-v2 section 8):")
            for line in pass_criteria(result.cards, policy=args.policy).lines():
                print("  " + line)
    print(f"\nTook {time.perf_counter() - started:.1f}s.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
