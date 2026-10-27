"""The AI's `propose_unpivot` action (review): a wide sheet's period columns turned into rows, as one typed step.

Guided setup already offers this (`engine.agent.advisor._ask_to_unpivot`); here the AI proposes it for the file it is
reviewing. Code, not the model, reads the column names (`engine.agent.unpivot.find_wide_columns`) and builds the
step, so the model passes no column list. Months or quarters whose names carry no year (`spend_jan`) cannot be put in
date order: the action then refuses until the person has said which year they cover (the model asks first and passes
`year`), and the year is frozen in the step. The proposal is pending and never ticked; the person decides.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from engine.agent.contracts import (
    AgentConfidence,
    Proposal,
    ProposalKind,
    ProposalSource,
    ProposalState,
    RecipeStep,
    RecipeStepKind,
    ToolResult,
)
from engine.agent.loop import STEP_SAMPLE_ROWS, STEP_TEXT_KEYS, STEP_TOOL
from engine.agent.recipe import RecipeError, check_recipe, run_recipe
from engine.agent.tools import AgentToolError
from engine.agent.unpivot import MAX_YEAR, MIN_YEAR, find_wide_columns
from engine.agent.untrusted import display_name
from engine.doctor.decisions import decisions
from engine.doctor.reshape_guard import doctor_levels

if TYPE_CHECKING:
    from engine.doctor.agent_review import _Review

__all__ = ["run_unpivot"]

REASON_CHARS = 200


def _year(args: dict[str, Any]) -> int | None:
    given = args.get("year")
    if given is None or given == "":
        return None
    try:
        year = int(str(given).strip())
    except ValueError:
        year = 0
    if not MIN_YEAR <= year <= MAX_YEAR:
        raise AgentToolError("AGENT_UNPIVOT_YEAR", "`year` is a four-digit year such as 2024.")
    return year


def run_unpivot(review: _Review, index: int, args: dict[str, Any]) -> None:
    from engine.agent.advisor import ordered_steps, recipe_steps

    review._file_only(index)
    turn, ctx, session = review.turn, review.ctxs[index], review.sessions[index]
    decided = decisions(review.case)
    key = decided.key(ctx.upload_id)
    if not key or key not in ctx.frame.columns:
        raise AgentToolError(
            "AGENT_UNPIVOT_NEEDS_ID",
            "Turning columns into rows is per customer: settle which column is the customer ID first.",
        )
    outcome = decided.outcome_of_upload(ctx.upload_id) or ctx.target
    outcome = outcome if outcome in ctx.frame.columns else None
    personal = {c.name for c in ctx.profile.columns if c.pii_kinds} | set(
        ctx.config.agent.always_hide_columns
    )
    columns = [str(c) for c in ctx.frame.columns]
    group = find_wide_columns(columns, skip=personal | {key} | ({outcome} if outcome else set()))
    if group is None:
        raise AgentToolError(
            "AGENT_UNPIVOT_NOT_WIDE",
            "No columns repeat a measure over months, quarters or years (such as spend_jan, spend_feb).",
        )
    year = _year(args)
    if group.dated and year is not None:
        raise AgentToolError(
            "AGENT_UNPIVOT_YEAR", "The column names already carry the year; leave `year` out."
        )
    if not group.dated and year is None:
        raise AgentToolError(
            "AGENT_UNPIVOT_NEEDS_YEAR",
            f"The {group.kind} columns name no year, so their rows cannot be put in date order. Ask the person which "
            "year they cover (`ask`), then propose again with `year`.",
        )
    wide = set(group.columns)
    kept = [c for c in columns if c not in wide and c != outcome and c not in personal]
    params = group.params(key=key, id_columns=kept, outcome=outcome, taken=columns, year=year)
    reason = str(args.get("reason") or "").strip()[:REASON_CHARS] or (
        f"The {group.kind} columns hold the same measures for different {group.kind}s; as rows they can be "
        "combined back into one row per customer."
    )
    step = RecipeStep(order=1, kind=RecipeStepKind.UNPIVOT, column=key, params=params, reason=reason)
    agent = ctx.config.agent
    accepted = recipe_steps(
        p
        for p in session.proposals
        if p.state is ProposalState.ACCEPTED and p.kind is ProposalKind.RECIPE_STEP
    )
    levels = doctor_levels(tuple(agent.levels), unified=True)
    try:
        check_recipe(
            ordered_steps([*accepted, step]),
            columns=columns,
            primary_key=key,
            target=outcome,
            levels=levels,
        )
        trial = run_recipe(
            ctx.frame.head(STEP_SAMPLE_ROWS),
            [step],
            upload_id=ctx.upload_id,
            primary_key=key,
            target=outcome,
            levels=levels,
            max_failure_pct=agent.max_conversion_failure_pct,
        )
    except RecipeError as exc:
        raise AgentToolError(exc.code, exc.message) from exc
    receipt = trial.receipt.steps[0]
    measures: list[Any] = [display_name(m) for m in group.measures]
    number = len(turn.steps) + 1
    result = ToolResult(
        evidence_id=f"s{number}",
        tool=STEP_TOOL,
        args={"kind": step.kind.value, "column": key, "params": dict(params)},
        result={
            "kind": step.kind.value,
            "column": key,
            "periods": len({p for _, _, p in group.value_columns}),
            "measures": measures,
            "rows_in": receipt.rows,
            "rows_out": len(trial.frame),
            "kept_columns": len(kept) + (1 if outcome else 0),
            "year": year,
        },
        created_at=review.clock(),
    )
    proposal = Proposal(
        proposal_id=f"p{number}",
        kind=ProposalKind.RECIPE_STEP,
        title=f"Turn the {group.kind} columns of {', '.join(measures)} into rows",
        reason=reason,
        step=step,
        evidence_ids=(result.evidence_id,),
        confidence=AgentConfidence.CHECK,
        state=ProposalState.PENDING,
        source=ProposalSource.AI,
    )
    turn.steps.append((index, result, proposal))
    turn.looks.append(result)
    turn.files_of[result.evidence_id] = index
    review._record(index, result, STEP_TEXT_KEYS | {"measures"})
