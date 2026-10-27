"""The AI's `propose_features` action (review and chat): the registry's tool, its answer kept as a query item.

The tool (`engine.doctor.tools_features`) compiles the request into one point-in-time query and proposes it as the
ordinary `sql_query` step, so the answer is kept with the model's other queries (`_Turn.queries`) and becomes a
pending, never-ticked case item at the end of the turn. What the model is shown back is numbers and names only: each
feature's fill rate and state, the counts, the findings. No cell value goes back (the egress gate prepares it).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from engine.agent.grounding import grounded_numbers
from engine.agent.tools import AgentToolError
from engine.doctor.actions import ProposeFeaturesArgs
from engine.doctor.decisions import decisions

if TYPE_CHECKING:
    from engine.doctor.agent_review import _Review

__all__ = ["FEATURE_ARGS", "run_features"]

FEATURE_ARGS: Final[frozenset[str]] = frozenset(ProposeFeaturesArgs.model_fields) - {"replaces"}
"""What the model may pass to the tool: the action's arguments (`engine.doctor.actions`), less the chat's swap."""


def run_features(review: _Review, index: int, args: dict[str, Any]) -> None:
    from engine.doctor.agent_review import _Sql
    from engine.doctor.tools_features import parse_query_output

    turn = review.turn
    ctx = review.ctxs[index]
    given: dict[str, Any] = {k: v for k, v in args.items() if k in FEATURE_ARGS}
    given["upload_id"] = ctx.upload_id
    decided = decisions(review.case)
    outcome = decided.outcome_of_upload(ctx.upload_id) or ctx.target
    if outcome:
        given["outcome_column"] = outcome  # no feature may read it: held in code by the tool
    # No as-of is filled in here: the tool's context carries the case's (`decisions(case).as_of_date`) and the step
    # keeps `$as_of` unbound, so the trial and every run measure at the date the case holds then.
    name = str(given.get("output_name") or "customer_features")
    if name in turn.closed:
        raise AgentToolError(
            "AGENT_RETRIES_USED", f"'{name[:60]}' has failed too often; it is left for the person."
        )
    key = decided.key(ctx.upload_id)
    result = review.registry.call(
        "propose_features", given, review._context(index, primary_key=key, replacing=review.replacing)
    )
    proposal = parse_query_output(result.output)
    turn.queries[name] = _Sql(result, tuple(proposal.findings))
    lines = [
        {"name": f.name, "fill_rate": f.fill_rate, "state": f.state}
        for f in getattr(proposal, "features", ())
    ]
    shown = {
        "evidence_id": result.evidence_id,
        "file": f"F{index + 1}",
        "tool": "propose_features",
        "result": review.gates[index].prepare(
            {
                "rows_out": proposal.rows_out,
                "columns_out": proposal.columns_out,
                "features": lines,
                "findings": [f.model_dump(mode="json") for f in proposal.findings],
            },
            extra_text_keys=frozenset({"message", "code", "level", "key", "column", "kind", "name", "state"}),
        ),
    }
    review.checker.grounded = review.checker.grounded | grounded_numbers([shown["result"]])
    turn.feedback.append(shown)
