"""The AI's `propose_combine_rows` action (review and chat): the registry's tool, its answer kept as a pending step.

The model names only the dates and the columns that hold one value per ID (`time_column`, `snapshot_column`,
`carry`); the tool (`engine.doctor.tools_reshape`) plans, tries and freezes the combine itself, so the model never
writes its parameters. The answer becomes a pending, never-ticked case item at the end of the turn; the guards of a
reshape step still run at Approve (`engine.doctor.reshape_guard`). What the model is shown back is counts and column
names only: no cell value goes back (the egress gate prepares it).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from engine.agent.grounding import grounded_numbers
from engine.doctor.actions import ProposeCombineRowsArgs
from engine.doctor.decisions import decisions

if TYPE_CHECKING:
    from engine.doctor.agent_review import _Review

__all__ = ["COMBINE_ARGS", "run_combine_rows"]

COMBINE_ARGS: Final[frozenset[str]] = frozenset(ProposeCombineRowsArgs.model_fields) - {"replaces"}
"""What the model may pass to the tool: the action's arguments (`engine.doctor.actions`), less the chat's swap."""

_SHOWN: Final[tuple[str, ...]] = (
    "rows_in",
    "rows_out",
    "ids",
    "rows_per_id",
    "most_rows",
    "failed",
    "features",
)


def run_combine_rows(review: _Review, index: int, args: dict[str, Any]) -> None:
    ctx = review.ctxs[index]
    given = {k: v for k, v in args.items() if k in COMBINE_ARGS and v is not None}
    key = decisions(review.case).key(ctx.upload_id)
    result = review.registry.call(
        "propose_combine_rows", given, review._context(index, primary_key=key, replacing=review.replacing)
    )
    review.turn.combines[index] = (index, result)
    output = result.output
    shown = {
        "evidence_id": result.evidence_id,
        "file": f"F{index + 1}",
        "tool": "propose_combine_rows",
        "result": review.gates[index].prepare(
            {name: output[name] for name in _SHOWN},
        ),
    }
    review.checker.grounded = review.checker.grounded | grounded_numbers([shown["result"]])
    review.turn.feedback.append(shown)
