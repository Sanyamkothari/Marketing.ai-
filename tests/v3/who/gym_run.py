"""Run the Gym with the who-to-contact agent as the policy `agent_who`, and say what it learned.

    python -m tests.v3.who.gym_run --scenario sleeping_dogs_15 --policy agent_who --seeds 2 --customers 1000

Takes the flags of `python -m autonomy.gym.run`. After the run it prints, for each agent built, the
held-out checks of the two slow models (used or not, and why), the trait groups and how many customers
the look-alikes' harm reading holds back, and the look-alike model's rows. It reads nothing from the
Gym's truth: the factory is the agent's own. For docs/v3/WHO.md.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from autonomy.decide.bidders import contact_harm_of
from autonomy.gym import run
from autonomy.gym.scenarios import default_brief, default_content
from autonomy.interfaces import Policy
from autonomy.runtime.agent import AgentPolicy, LookalikeClickModel, build_agent_policy

BUILT: list[AgentPolicy] = []


def who_factory(world: object) -> Policy:
    policy = build_agent_policy([default_brief()], default_content(), lookalike=True, who=True)
    BUILT.append(policy)
    return policy


def report() -> None:
    for i, policy in enumerate(BUILT):
        book = policy.who
        if book is None:
            continue
        print(f"[who agent {i}] events held={sum(len(v) for v in book.events.values())}")
        print(f"  {book.models.risk_check.describe()}")
        print(f"  {book.models.value_check.describe()}")
        harm = contact_harm_of(policy.bundle.response)
        if harm is not None:
            print(f"  own-record harm model: {harm.describe()}")
        traits = book.traits
        print(
            f"  trait groups: {traits.describe()} (tau {traits.tau:.2f}, spread between groups "
            f"{traits.spread:.2f}, customers read {traits.customers})"
        )
        for key, (n, ratio, p) in traits.groups().items():
            print(f"    {key}: {n} customers, ratio {ratio:.2f}, chance below 1 {p:.2f}")
        model = policy.bundle.response
        if isinstance(model, LookalikeClickModel):
            la = model.lookalike
            print(f"  look-alike: rows={la.rows} ready={la.ready} refused={la.refused}")


def main(argv: Sequence[str] | None = None) -> int:
    run.register_policy("agent_who", who_factory, replace=True)
    code = run.main(argv)
    report()
    return code


if __name__ == "__main__":
    sys.exit(main())
