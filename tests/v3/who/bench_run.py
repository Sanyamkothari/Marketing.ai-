"""Run the real-data scoreboard with the who-to-contact agent in place of the agent.

    python -m tests.v3.who.bench_run --dataset hillstrom --data-dir DIR --json OUT.json

Takes the flags of `python -m scripts.v2_realbench.run`. The bench builds its agent with
`build_agent_policy` as production does; this wrapper adds `lookalike=True, who=True` to that one call
and changes nothing else, so the scoreboards differ only in the response model and decision 1's switch
(the look-alike scoreboard, `tests.v3.lookalike.bench_run`, is the one to compare it with). Not collected
as a test; the datasets are not in the build environment, so it runs in CI.
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
        book = policy.who
        if book is None:
            continue
        print(f"[who] {book.describe()}")


def main(argv: Sequence[str] | None = None) -> int:
    build = agent_module.build_agent_policy

    def with_who(*args: Any, **kwargs: Any) -> Any:
        policy = build(*args, lookalike=True, who=True, **kwargs)
        BUILT.append(policy)
        return policy

    agent_module.build_agent_policy = with_who
    try:
        code = run.main(argv)
    finally:
        agent_module.build_agent_policy = build
    report()
    return code


if __name__ == "__main__":
    sys.exit(main())
