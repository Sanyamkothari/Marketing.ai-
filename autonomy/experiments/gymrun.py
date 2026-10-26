"""Run a live experiment inside the Gym: the pass test of plan-v3 foundation C, as code.

    python -m autonomy.experiments.gymrun --seeds 10 --customers 5000

The agent (`autonomy.runtime.agent.build_agent_policy`) is wrapped in `ExperimentPolicy`, one offer test
is registered on day 0 in the `better_offer_planted` scenario (the reference is the worse offer, 10% off;
the challenger is free shipping), and the experiment is read every seven days by the book's own rules. The
Gym world is told nothing and the policy is told nothing: the harness only watches the events the world
returns to measure each customer's profit, as the Gym's own ledger does (purchase margin minus offer
cost, minus the cost of each send), and tags every customer with the bucket the wrapper logged.

The plan's bar: the better offer is found within 4 weeks on at least 9 of 10 seeds, and the worse one is
never promoted. `GymExperimentResult` carries what is needed to check both. This takes minutes per seed
at the default size, so it belongs to the nightly job; the tests run it only at smoke size.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import numpy as np

from autonomy.contracts import (
    Brief,
    ContentItem,
    CustomerState,
    Decision,
    DecisionContext,
    Event,
    EventType,
)
from autonomy.experiments.book import ExperimentBook
from autonomy.experiments.model import (
    HOLDOUT_BUCKET,
    Arm,
    EndKind,
    ExperimentSpec,
    PickRule,
    StoppingRule,
    tags_of,
)
from autonomy.experiments.policy import ExperimentPolicy
from autonomy.experiments.reading import Outcomes, Reading
from autonomy.gym.api import ScenarioSpec
from autonomy.gym.scenarios import (
    BETTER_OFFER_ID,
    GYM_GOAL_ID,
    WORSE_OFFER_ID,
    default_brief,
    default_content,
    get_scenario,
)
from autonomy.gym.score import SimWorld, gym_world, run_policy
from autonomy.interfaces import Policy
from autonomy.scientist.questions import QuestionKind, describe_question

__all__ = ["GymExperimentResult", "experiment_spec", "run_gym_experiment"]

REVIEW_EVERY_DAYS = 7
EXPERIMENT_ID = "offer_test_1"


def experiment_spec(*, share: float = 0.10, min_per_arm: int = 100, max_days: int = 28) -> ExperimentSpec:
    """The planted offer question: free shipping against 10% off, for every customer."""
    brief = default_brief()
    return ExperimentSpec(
        experiment_id=EXPERIMENT_ID,
        goal_id=GYM_GOAL_ID,
        kind=QuestionKind.OFFER,
        question=describe_question(QuestionKind.OFFER, brief.audience.description),
        arms=(
            Arm(arm_id=WORSE_OFFER_ID, label="10% off"),
            Arm(arm_id=BETTER_OFFER_ID, label="Free shipping"),
        ),
        reference_arm=WORSE_OFFER_ID,
        audience=brief.audience,
        share=share,
        pick_rule=PickRule(min_per_arm=min_per_arm, min_holdout=min_per_arm),
        stopping_rule=StoppingRule(prior_sd=1.0, max_days=max_days),
    )


@dataclass(frozen=True)
class GymExperimentResult:
    seed: int
    end_kind: EndKind | None
    winner: str | None
    ended_day: float | None
    reading: Reading
    promoted: str | None
    rules_unchanged: bool
    stats: Mapping[str, int]
    """`ExperimentPolicy.stats`: decisions made and sends chosen per arm, and `outside_arm` (should be 0)."""

    @property
    def found_better(self) -> bool:
        return self.winner == BETTER_OFFER_ID

    @property
    def promoted_worse(self) -> bool:
        return self.promoted == WORSE_OFFER_ID


class _Profit:
    """Per-customer profit from the world's events, counted as the Gym's own ledger counts it."""

    def __init__(self, start: datetime, spec: ScenarioSpec, brief: Brief) -> None:
        self.start = start
        self.channel_cost = dict(spec.channel_cost)
        self.offer_cost = {o.offer_id: o.cost_per_redemption for o in brief.envelope.offers}
        self.net: dict[str, float] = {}

    def observe(self, events: Sequence[Event]) -> None:
        for e in events:
            if e.ts < self.start:
                continue
            if e.type is EventType.PURCHASE:
                raw = e.attrs.get("offer_cost")
                oid = e.attrs.get("offer_id")
                cost = (
                    float(raw)
                    if isinstance(raw, int | float)
                    else self.offer_cost.get(oid, 0.0) if isinstance(oid, str) else 0.0
                )
                self.net[e.customer_id] = self.net.get(e.customer_id, 0.0) + (e.margin or 0.0) - cost
            elif e.type is EventType.SEND and e.channel is not None:
                self.net[e.customer_id] = self.net.get(e.customer_id, 0.0) - self.channel_cost.get(
                    e.channel, 0.0
                )


class _Watched:
    """The world, with every event it returns also shown to the profit tracker."""

    def __init__(self, world: SimWorld, profit: _Profit) -> None:
        self._world = world
        self._profit = profit

    def reset(self, seed: int) -> Sequence[Event]:
        history = self._world.reset(seed)
        self._profit.start = self._world.now()  # profit is counted from decision day 0, as the ledger does
        return history

    def advance(self, until: datetime) -> Sequence[Event]:
        events = self._world.advance(until)
        self._profit.observe(events)
        return events

    def __getattr__(self, name: str) -> Any:
        return getattr(self._world, name)


class _Reviewing(ExperimentPolicy):
    """`ExperimentPolicy` that reads the experiment every `REVIEW_EVERY_DAYS` days, as the Scientist would."""

    def __init__(self, inner: Policy, book: ExperimentBook, profit: _Profit) -> None:
        super().__init__(inner, book)
        self._profit = profit
        self._bucket: dict[str, str] = {}
        self._last_review: datetime | None = None
        self.last_reading: Reading | None = None

    def decide(self, state: CustomerState, ctx: DecisionContext, *, trigger: str | None = None) -> Decision:
        decision = super().decide(state, ctx, trigger=trigger)
        found = tags_of(decision.policy_version).get(EXPERIMENT_ID)
        if found is not None:
            self._bucket.setdefault(decision.customer_id, found)
        return decision

    def prepare_day(self, states: Sequence[CustomerState], ctx: DecisionContext) -> None:
        inner_prepare = getattr(self._inner, "prepare_day", None)
        if callable(inner_prepare):
            inner_prepare(states, ctx)
        if self._last_review is None:
            self._last_review = ctx.now
        if (
            ctx.now - self._last_review >= timedelta(days=REVIEW_EVERY_DAYS)
            and self.book.get(EXPERIMENT_ID).running
        ):
            self._last_review = ctx.now
            self.last_reading = self.book.review(EXPERIMENT_ID, self._outcomes(), ctx.now).reading

    def _outcomes(self) -> Outcomes:
        spec = self.book.get(EXPERIMENT_ID).spec

        def values(bucket: str) -> np.ndarray[Any, np.dtype[np.float64]]:
            ids = sorted(c for c, b in self._bucket.items() if b == bucket)
            return np.array([self._profit.net.get(c, 0.0) for c in ids], dtype=np.float64)

        return Outcomes(arms={a: values(a) for a in spec.arm_ids}, holdout=values(HOLDOUT_BUCKET))


def run_gym_experiment(
    seed: int,
    *,
    n_customers: int = 5000,
    days: int = 29,
    share: float = 0.10,
    min_per_arm: int = 100,
    learn: bool = True,
) -> GymExperimentResult:
    """One seed of the planted-offer test: run the wrapped agent and report what the experiment decided.

    Free shipping, once it wins, is promoted (the harness does what the Scientist's weekly job does), so
    `promoted_worse` says whether the wrong offer ever became the default.
    """
    from autonomy.runtime.agent import build_agent_policy

    scenario = get_scenario("better_offer_planted", n_customers=n_customers, days=days)
    brief, content = default_brief(), default_content()
    book = ExperimentBook()
    spec = experiment_spec(share=share, min_per_arm=min_per_arm, max_days=days - 1)
    now0 = brief.created_at
    book.start(spec, brief, now0)
    holder: dict[str, _Reviewing] = {}
    tracker: dict[str, _Profit] = {}

    def world_factory(sc: ScenarioSpec, briefs: Sequence[Brief], items: Sequence[ContentItem]) -> SimWorld:
        world = gym_world(sc, briefs, items)
        tracker["profit"] = _Profit(world.now(), sc, briefs[0])
        return _Watched(world, tracker["profit"])

    def policy_factory(world: SimWorld) -> Policy:  # noqa: ARG001
        reviewing = _Reviewing(build_agent_policy([brief], content), book, tracker["profit"])
        holder["policy"] = reviewing
        return reviewing

    run_policy(
        scenario,
        policy_factory,
        seed,
        [brief],
        content,
        policy_name="agent+experiments",
        world_factory=world_factory,
        with_oracle=False,
        learn=learn,
    )
    policy = holder["policy"]
    final = book.get(EXPERIMENT_ID)
    promoted: str | None = None
    if final.end_kind is EndKind.WINNER:
        promoted = book.promote(EXPERIMENT_ID, brief, final.ended_at or now0).value
    reading = policy.last_reading
    assert reading is not None or final.running or final.ended_at is not None
    return GymExperimentResult(
        seed=seed,
        end_kind=final.end_kind,
        winner=final.winner,
        ended_day=(
            None if final.ended_at is None else (final.ended_at - final.registered_at) / timedelta(days=1)
        ),
        reading=reading if reading is not None else Reading(EXPERIMENT_ID, now0, 0.0, 0, 0.0, (), 0.0, False),
        promoted=promoted,
        rules_unchanged=final.digest_matches(),
        stats=dict(policy.stats),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m autonomy.experiments.gymrun", description=__doc__)
    parser.add_argument("--seeds", type=int, default=10)
    parser.add_argument("--first-seed", type=int, default=0)
    parser.add_argument("--customers", type=int, default=5000)
    parser.add_argument("--days", type=int, default=29)
    args = parser.parse_args(argv)
    found = worse = 0
    seeds = range(args.first_seed, args.first_seed + args.seeds)
    for seed in seeds:
        r = run_gym_experiment(seed, n_customers=args.customers, days=args.days)
        found += r.found_better
        worse += r.promoted_worse
        day = "never" if r.ended_day is None else f"day {r.ended_day:.0f}"
        print(
            f"seed {seed}: {r.end_kind.value if r.end_kind else 'still running'} ({day}); winner {r.winner}."
        )
    print(f"Found the better offer on {found} of {len(seeds)} seeds; promoted the worse one on {worse}.")
    return 0 if worse == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
