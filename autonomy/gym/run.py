"""Run the Gym from the command line and print a plain-English scoreboard.

    python -m autonomy.gym.run --scenario sleeping_dogs_15 --policy agent --seeds 5 --customers 2000

By default the chosen policy is compared on the same seeds with send-to-all, the rule baseline, the
best fixed rule (WhatsApp if consented else email, no offer, one fixed hour), the segment oracle (the
realistic ceiling) and the oracle, then judged against the staged bar of docs/v2/GYM_BAR.md: Stage 1
Safe, Stage 2 Useful, Stage 3 Smart, with the plan's 80%-of-the-oracle as a stretch metric.
`--no-compare` scores the policy alone. `--report-json PATH` writes the scorecards and the verdicts
as JSON, for CI artefacts.

Policies are found by name in a registry. `agent` is Autonomy's own policy
(`autonomy.runtime.agent.build_agent_policy`). Another stream adds its own with
`register_policy("name", lambda world: MyPolicy(...))`; the factory gets the world so a fresh
policy is built for every run (the oracle uses the world's truth; nothing else may).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from autonomy.contracts import Brief, ContentItem
from autonomy.fakes.policies import NothingPolicy
from autonomy.gym.api import ScenarioSpec
from autonomy.gym.baselines import BEST_FIXED_HOUR, BestFixedRule
from autonomy.gym.scenarios import SCENARIOS, default_brief, default_content, get_scenario
from autonomy.gym.score import (
    BEST_FIXED,
    EVERYONE,
    NOTHING,
    ORACLE,
    RULE,
    SEGMENT_ORACLE,
    PolicyFactory,
    ScoreCard,
    SimWorld,
    WorldFactory,
    best_fixed_factory,
    compare,
    everyone_factory,
    oracle_factory,
    pass_criteria,
    rule_factory,
    run_policy,
    segment_oracle_factory,
    staged_criteria,
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
    BEST_FIXED: best_fixed_factory,
    SEGMENT_ORACLE: segment_oracle_factory,
}

REFERENCES: tuple[str, ...] = (EVERYONE, RULE, BEST_FIXED, SEGMENT_ORACLE, ORACLE)
"""What every policy is compared with, in the order the scoreboard lists them."""


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


def fixed_hour_factory(hour: int) -> PolicyFactory:
    """`BestFixedRule` at another fixed local hour. Like the default, it never sees the truth."""

    def factory(world: SimWorld) -> Policy:  # noqa: ARG001
        return BestFixedRule(hour)

    return factory


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
    if card.share_of_segment_oracle is not None and card.segment_oracle_value is not None:
        parts.append(
            f"Captured {card.share_of_segment_oracle:.0%} of the segment oracle's "
            f"{money(card.segment_oracle_value)}."
        )
    if card.value_vs_best_fixed_rule is not None and card.policy != BEST_FIXED:
        diff = card.value_vs_best_fixed_rule
        parts.append(
            f"{money(abs(diff))} {'more' if diff >= 0 else 'less'} true value than the best fixed rule."
        )
    if card.lost_value:
        parts.append(f"Lost {money(card.lost_value)} of future value to unsubscribes and complaints.")
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
    parser.add_argument(
        "--fixed-hour",
        type=int,
        default=None,
        help=f"Local send hour of the best fixed rule (default {BEST_FIXED_HOUR})",
    )
    parser.add_argument(
        "--report-json", type=Path, default=None, metavar="PATH", help="Write scorecards and verdicts as JSON"
    )
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
    if args.fixed_hour is not None and not 0 <= args.fixed_hour < 24:
        parser.error("--fixed-hour must be 0-23")
    registry = dict(POLICIES)
    if args.fixed_hour is not None:
        registry[BEST_FIXED] = fixed_hour_factory(args.fixed_hour)
    factory = registry[args.policy]
    seeds = list(range(args.first_seed, args.first_seed + args.seeds))
    worlds = world_factory or make_world
    learn = not args.no_learn

    print(f"Scenario {spec.name}: {spec.description}")
    print(
        f"Policy {args.policy} on {len(seeds)} seed(s), {spec.n_customers:,} customers, "
        f"{spec.days} decision days.\n"
    )
    started = time.perf_counter()
    report: dict[str, object] = {
        "scenario": spec.name,
        "policy": args.policy,
        "seeds": seeds,
        "n_customers": spec.n_customers,
        "days": spec.days,
    }
    if args.no_compare:
        cards = [
            run_policy(spec, factory, seed, policy_name=args.policy, world_factory=worlds, learn=learn)
            for seed in seeds
        ]
        for card in cards:
            print(describe(card))
        report["cards"] = [c.model_dump(mode="json") for c in cards]
    else:
        names = [args.policy, *REFERENCES]
        policies = {n: registry[n] for n in dict.fromkeys(names)}
        result = compare(spec, policies, seeds, world_factory=worlds, learn=learn)
        for card in result.cards:
            if card.policy == args.policy:
                print(describe(card))
        print("\nAverages over seeds:")
        print(result.summary().to_string(index=False, float_format=lambda x: f"{x:,.3f}"))
        report["cards"] = [c.model_dump(mode="json") for c in result.cards]
        if args.policy in REFERENCES:
            print(f"\n{args.policy} is a baseline itself; the pass bar is for the policy under test.")
        else:
            staged = staged_criteria(result.cards, policy=args.policy)
            plan = pass_criteria(result.cards, policy=args.policy)
            print("\nAgainst the pass bar (staged, docs/v2/GYM_BAR.md):")
            for line in staged.lines():
                print("  " + line)
            print("\nThe plan's original bar (plan-v2 section 8), for reference:")
            print("  " + plan.lines()[-1])
            report["verdict"] = staged.verdict()
            report["staged"] = staged.model_dump(mode="json")
            report["plan_bar"] = {**plan.model_dump(mode="json"), "passed": plan.passed}
    elapsed = time.perf_counter() - started
    report["seconds"] = round(elapsed, 1)
    if args.report_json is not None:
        write_report(args.report_json, report)
        print(f"\nWrote {args.report_json}.")
    print(f"\nTook {elapsed:.1f}s.")
    return 0


def write_report(path: Path, report: dict[str, object]) -> None:
    """Write the report as indented JSON, creating the folder if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
