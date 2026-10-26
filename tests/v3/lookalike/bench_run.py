"""Run the real-data scoreboard with the look-alike agent in place of the agent.

    python -m tests.v3.lookalike.bench_run --dataset hillstrom --data-dir DIR --json OUT.json

Takes the flags of `python -m scripts.v2_realbench.run`. The bench builds its agent with
`build_agent_policy` as production does; this wrapper adds `lookalike=True` to that one call and
changes nothing else, so the two scoreboards differ only in the response model.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from typing import Any

import autonomy.runtime.agent as agent_module
from scripts.v2_realbench import run

BUILT: list[Any] = []


def report() -> None:
    for policy in BUILT:
        model = policy.bundle.response
        la = model.lookalike
        cal = la.calibration().summary()
        print(
            f"[look-alike] rows={la.rows} ready={la.ready} refused={la.refused}; "
            f"scored before learned: treated n={cal['treated_n']:.0f} predicted={cal['treated_predicted']:.4f} "
            f"realised={cal['treated_realised']:.4f}; control n={cal['control_n']:.0f} "
            f"predicted={cal['control_predicted']:.4f} realised={cal['control_realised']:.4f}"
        )


def main(argv: Sequence[str] | None = None) -> int:
    build = agent_module.build_agent_policy

    def with_lookalike(*args: Any, **kwargs: Any) -> Any:
        policy = build(*args, lookalike=True, **kwargs)
        BUILT.append(policy)
        return policy

    agent_module.build_agent_policy = with_lookalike
    try:
        code = run.main(argv)
    finally:
        agent_module.build_agent_policy = build
    report()
    return code


if __name__ == "__main__":
    sys.exit(main())
