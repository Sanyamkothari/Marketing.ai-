"""Run the Gym with the look-alike agent added as the policy `agent_lookalike`.

    python -m tests.v3.lookalike.gym_run --scenario baseline_shop --policy agent_lookalike --seeds 2 --customers 1000

Takes the flags of `python -m autonomy.gym.run`. After the run it prints what each look-alike
agent learned (rows, whether the crowd's fit was in use, calibration of its purchase predictions),
for docs/v3/LOOKALIKE.md. It reads nothing from the Gym's truth: the factory is the agent's own.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from autonomy.gym import run
from autonomy.gym.scenarios import default_brief, default_content
from autonomy.interfaces import Policy
from autonomy.runtime.agent import AgentPolicy, LookalikeClickModel, build_agent_policy

BUILT: list[AgentPolicy] = []


def lookalike_factory(world: object) -> Policy:
    policy = build_agent_policy([default_brief()], default_content(), lookalike=True)
    BUILT.append(policy)
    return policy


def report() -> None:
    for i, policy in enumerate(BUILT):
        model = policy.bundle.response
        if not isinstance(model, LookalikeClickModel):
            continue
        la = model.lookalike
        cal = la.calibration().summary()
        book = policy.outcomes
        print(
            f"[look-alike agent {i}] rows={la.rows} ready={la.ready} refused={la.refused} "
            f"dropped={book.dropped if book else {}} pending={len(book) if book else 0}"
        )
        if la.rows:
            crowd = la._crowd_weights(la._rows)
            treated = sum(w for r, w in zip(la._rows, crowd, strict=True) if r.key is not None)
            control = sum(w for r, w in zip(la._rows, crowd, strict=True) if r.key is None)
            people = len({r.customer_id for r in la._rows if r.key is None})
            print(
                f"  crowd weight (all rows, after the per-customer cap): treated={treated:.1f} "
                f"control={control:.1f} from {people} control customers"
            )
        print(
            "  calibration (scored before learned): treated n={treated_n:.0f} predicted={treated_predicted:.4f} "
            "realised={treated_realised:.4f}; control n={control_n:.0f} predicted={control_predicted:.4f} "
            "realised={control_realised:.4f}".format(**cal)
        )


def main(argv: Sequence[str] | None = None) -> int:
    run.register_policy("agent_lookalike", lookalike_factory, replace=True)
    code = run.main(argv)
    report()
    return code


if __name__ == "__main__":
    sys.exit(main())
