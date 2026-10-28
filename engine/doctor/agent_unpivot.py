"""The AI's `propose_unpivot` action (review): a wide sheet's period columns turned into rows, offered by a question.

Guided setup already offers this (`engine.agent.advisor._ask_to_unpivot`); here the AI proposes it for the file it is
reviewing. Code, not the model, reads the column names (`engine.agent.unpivot.find_wide_columns`) and builds the
step, so the model passes no column list and no year.

A change to the rows is offered only through its blocking question, with the trial's counts in the question (decision
O3, `engine.doctor.reshape_guard`): the person's answer is what adds the step, so nothing is added before they say
yes. Months or quarters whose names carry no year (`spend_jan`) cannot be put in date order: the question is then
"which year do they cover", each year an answer that adds the step with that year frozen in it (and shown in its
title). The data policy (`data.unpivot`) checks the step first, like every other AI step.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from engine.agent.contracts import ProposalKind, ProposalState, RecipeStep, RecipeStepKind
from engine.agent.loop import STEP_SAMPLE_ROWS, _policy_gate
from engine.agent.recipe import RecipeError, check_recipe, run_recipe
from engine.agent.tools import AgentToolError
from engine.agent.unpivot import MAX_YEAR, MIN_YEAR, find_wide_columns
from engine.agent.untrusted import display_name
from engine.doctor.decisions import decisions
from engine.doctor.questions import QuestionError, build_ask
from engine.doctor.reshape_guard import doctor_levels
from engine.doctor.visit_contracts import OptionEffect

if TYPE_CHECKING:
    from engine.doctor.agent_review import _Review

__all__ = ["run_unpivot"]

REASON_CHARS = 200
YEARS_OFFERED = 4
"""The years offered for months that name none: this year and the three before it."""


def _and(names: list[str]) -> str:
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def run_unpivot(review: _Review, index: int, args: dict[str, Any]) -> None:
    from engine.agent.advisor import ordered_steps, recipe_steps
    from engine.doctor import policy as data_policy

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
    wide = set(group.columns)
    kept = [c for c in columns if c not in wide and c != outcome and c not in personal]
    measures = [display_name(m) for m in group.measures]
    reason = str(args.get("reason") or "").strip()[:REASON_CHARS] or (
        f"The {group.kind} columns hold the same measures for different {group.kind}s; as rows they can be "
        "combined back into one row per customer."
    )
    # A year the names do not carry is the person's to say: one answer per year offered.
    years: list[int | None] = (
        [None]
        if group.dated
        else [
            y
            for y in range(review.clock().year, review.clock().year - YEARS_OFFERED, -1)
            if MIN_YEAR <= y <= MAX_YEAR
        ]
    )
    agent = ctx.config.agent
    accepted = recipe_steps(
        p
        for p in session.proposals
        if p.state is ProposalState.ACCEPTED and p.kind is ProposalKind.RECIPE_STEP
    )
    levels = doctor_levels(tuple(agent.levels))
    gate = _policy_gate(ctx, session, "data.unpivot")
    effects: list[OptionEffect | None] = []
    counts: tuple[int, int] = (0, 0)
    for year in years:
        params = group.params(key=key, id_columns=kept, outcome=outcome, taken=columns, year=year)
        step = RecipeStep(order=1, kind=RecipeStepKind.UNPIVOT, column=key, params=params, reason=reason)
        facts = (data_policy.StepFacts.of(step),)
        if gate is not None:
            gate(data_policy.STEP_KIND, data_policy.PERSONAL, steps=facts)
            gate(data_policy.STEP_PARAMS, data_policy.SPELLINGS, steps=facts, looked_at=True)
        try:
            check_recipe(
                ordered_steps([*accepted, step]),
                columns=columns,
                primary_key=key,
                target=outcome,
                levels=levels,
            )
            if gate is not None:
                gate(data_policy.LEVEL, steps=facts, looked_at=True)
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
        if gate is not None:
            # Offered only through this question; never ticked (the answer is the person's).
            gate(data_policy.RESHAPE_GUARD, data_policy.NEVER_TICKED, steps=facts, asked=True, looked_at=True)
        counts = (trial.receipt.steps[0].rows, len(trial.frame))
        when = f" of {year}" if year is not None else ""
        title = f"Turn the {group.kind} columns{when} of {_and(measures)} into rows"
        effects.append(
            OptionEffect(
                option_id="",
                kind="recipe_step",
                title=title,
                payload={
                    "upload_id": ctx.upload_id,
                    "path": None,
                    "value": None,
                    "step": step.model_dump(mode="json"),
                    "rows_in": counts[0],
                    "rows_out": counts[1],
                },
            )
        )
    values: dict[str, object] = {
        "file": display_name(ctx.profile.file_name or "the sheet"),
        "kind": group.kind,
        "measures": _and(measures),
        "entity": ctx.config.entity,
        "rows_in": f"{counts[0]:,}",
        "rows_out": f"{counts[1]:,}",
    }
    choices = [f"They cover {year}" for year in years if year is not None]
    try:
        ask = build_ask(
            "Q25" if group.dated else "Q26",
            values,
            rows=len(ctx.frame),
            choices=choices,
            effects=[*effects, None],
            upload_id=review._upload_of(index),
            column=key,
            by_code=True,
        )
    except QuestionError as exc:
        raise AgentToolError("AGENT_QUESTION_INVALID", str(exc)[:200]) from exc
    if ask.blocking and turn.blocking >= review.limits.asks.blocking:
        raise AgentToolError(
            "AGENT_BLOCKING_LIMIT",
            f"You may ask {review.limits.asks.blocking} blocking questions in one review; this one is dropped.",
        )
    turn.blocking += int(ask.blocking)
    turn.asks.append(ask)
    turn.feedback.append({"asked": ask.code})
