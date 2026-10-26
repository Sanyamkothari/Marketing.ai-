"""Run the real-data scoreboard with the who-to-contact agent in place of the agent.

    python -m tests.v3.who.bench_run --dataset hillstrom --data-dir DIR --json OUT.json

Takes the flags of `python -m scripts.v2_realbench.run`. The bench builds its agent with
`build_agent_policy` as production does; this wrapper adds `lookalike=True, who=True` to that one call
(the look-alike scoreboard, `tests.v3.lookalike.bench_run`, is the one to compare it with).

One more change, without which decision 1 would not be measured at all: the bench hands `nightly` only
the TRAIN customers' history, and the TEST states carry no nightly profile, so the book would hold no
event of a customer it decides for (no profile, no score, no trait group). The wrapper also hands the
book every customer's pre-campaign events (`pre` in `scripts.v2_realbench.common.run_agent`, from
before the campaign starts and so point-in-time legal; the bench folds the TEST states from the same
events). The campaign events of TEST customers are never handed over. It prints how many customers the
book scored and keyed (`WhoBook.coverage`). Not collected as a test; the datasets are not in the build
environment, so it runs in CI.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from typing import Any

import autonomy.runtime.agent as agent_module
from scripts.v2_realbench import common, run

BUILT: list[Any] = []
PRE: list[tuple[Any, Any]] = []
"""The pre-campaign events the bench last folded its states from, with its `now`."""


def report() -> None:
    for policy in BUILT:
        book = policy.who
        if book is None:
            continue
        print(f"[who] {book.describe()}")
        print(f"[who] customers read at the last decision day: {book.coverage()}")


def main(argv: Sequence[str] | None = None) -> int:
    build = agent_module.build_agent_policy
    fold = common.fold_states

    def keep_pre(events: Any, now: Any) -> Any:
        PRE[:] = [(events, now)]
        return fold(events, now)

    def with_who(*args: Any, **kwargs: Any) -> Any:
        policy = build(*args, lookalike=True, who=True, **kwargs)
        if PRE and policy.who is not None:
            events, now = PRE[0]
            policy.who.ingest(events, now)
            PRE.clear()
        BUILT.append(policy)
        return policy

    agent_module.build_agent_policy = with_who
    common.fold_states = keep_pre
    try:
        code = run.main(argv)
    finally:
        agent_module.build_agent_policy = build
        common.fold_states = fold
    report()
    return code


if __name__ == "__main__":
    sys.exit(main())
