"""What TREAT and the AI review depend on (F5 of `docs/plans/DATA_DOCTOR_ROOT_CAUSES.md`).

The visit remembers that it has run the rules (and the review) for a set of inputs, and does not run them again
for the same set. A set that leaves an input out goes stale: a goal chosen first and a table built by the AI later
never reached the goal rules, because the table was in none of the inputs. The inputs are now the files, the goal
and its brief, the answers, **the plan** (:func:`plan_part`: the digest of the accepted changes, and the queries the
AI proposed that are still waiting, which the goal rules read for a table that has the outcome) and **the decisions**
(:func:`decisions_digest`: `engine.doctor.decisions`' digest, and any registered provider's).

Nothing here reads a cell.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from engine.agent.contracts import ProposalSource, ProposalState
from engine.doctor.case import Case, CaseItem
from engine.doctor.plan_check import plan_digest

__all__ = ["decisions_digest", "plan_part", "register_decisions_digest"]

_PROVIDERS: Final[list[Callable[[Case], str]]] = []


def register_decisions_digest(provider: Callable[[Case], str]) -> None:
    """Add a digest of the case's decisions (`engine.doctor.decisions`) to every memo key. Idempotent."""
    if provider not in _PROVIDERS:
        _PROVIDERS.append(provider)


def decisions_digest(case: Case) -> str:
    """The digest of what the person decided: `decisions(case).digest`, then every registered provider's."""
    from engine.doctor.decisions import decisions

    return "|".join([decisions(case).digest, *(provider(case) for provider in _PROVIDERS)])


def _step_kind(item: CaseItem) -> object:
    step = item.payload.get("step")
    return step.get("kind") if isinstance(step, dict) else None


def plan_part(case: Case) -> tuple[str, tuple[str, ...], str]:
    """The plan and the decisions as a memo key sees them: `(digest of the accepted changes, ids of the AI's
    queries still waiting, digest of the decisions)`. A tick, an untick or a new query is another key."""
    waiting = tuple(
        sorted(
            i.item_id
            for i in case.items
            if i.state is ProposalState.PENDING
            and i.source is ProposalSource.AI
            and _step_kind(i) == "sql_query"
        )
    )
    return plan_digest(case.files, case.items), waiting, decisions_digest(case)
