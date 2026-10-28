"""The AI's `propose_unpivot` action (review): a wide sheet's period columns turned into rows, offered by a question.

Guided setup already offered this (`engine.agent.advisor._ask_to_unpivot`); here the AI proposes it for the file it is
reviewing. The facts are code's: `engine.agent.unpivot.wide_column_groups` reads the column names into candidate
groups (measure, kind of period, columns, whether the names carry a year), and every call answers them back to the
model. The proposal is the AI's: which group (`columns`, a subset of one candidate; the largest when it names none)
and, when the names carry no year, which year with its evidence in `reason` (another date column, the file's name, the
person's words). Code checks both (the group against the candidates, the year in `MIN_YEAR..MAX_YEAR`), builds the
step and tries it on the first rows.

A change to the rows is offered only through its blocking question, with the trial's counts in the question (decision
O3, `engine.doctor.reshape_guard`): the person's answer is what adds the step, so nothing is added before they say
yes. Months or quarters whose names carry no year (`spend_jan`) cannot be put in date order: the question is then
"which year do they cover", with the AI's year recommended, this year and the three before it, and a typed year
(`value_kind: number`; the visit writes the typed year into the step). A sheet whose months run across the turn of
the year (a financial year, `apr` .. `mar`) asks for the year it starts in, and the months after the turn are dated
the year after (`first_month`). Personal columns stay on every row (names only); the question says which columns the
step leaves out. The data policy (`data.unpivot`) checks the step first, like every other AI step.
"""

from __future__ import annotations

from dataclasses import replace
from itertools import pairwise
from typing import TYPE_CHECKING, Any

from engine.agent.contracts import ProposalKind, ProposalState, RecipeStep, RecipeStepKind
from engine.agent.loop import STEP_SAMPLE_ROWS, _policy_gate
from engine.agent.recipe import RecipeError, check_recipe, run_recipe
from engine.agent.tools import AgentToolError
from engine.agent.unpivot import MAX_YEAR, MIN_YEAR, WideGroup, parse_period, wide_column_groups
from engine.agent.untrusted import display_name
from engine.doctor.decisions import decisions
from engine.doctor.questions import QuestionError, build_ask
from engine.doctor.reshape_guard import doctor_levels
from engine.doctor.visit_contracts import OptionEffect

if TYPE_CHECKING:
    from engine.doctor.agent_review import _Review

__all__ = ["first_month_of", "run_unpivot", "typed_year_effect"]

REASON_CHARS = 200
YEARS_OFFERED = 4
"""The years offered besides the AI's own for months that name none: this year and the three before it."""
_MONTH_NAMES = ("", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _and(names: list[str]) -> str:
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def first_month_of(group: WideGroup, columns: list[str]) -> int | None:
    """The month an undated month sheet starts with when, in the file's column order, its months run across the
    turn of the year (`apr` .. `dec`, `jan` .. `mar`); None when they run in calendar order."""
    if group.dated or group.kind != "month":
        return None
    where = {name: n for n, name in enumerate(columns)}
    first = group.measures[0]
    months = [
        period.index
        for column, measure, token in sorted(group.value_columns, key=lambda v: where.get(v[0], 0))
        if measure == first and (period := parse_period(token)) is not None
    ]
    drops = sum(1 for a, b in pairwise(months) if b < a)
    return months[0] if drops == 1 and months[-1] < months[0] else None


def _facts(groups: tuple[WideGroup, ...]) -> list[dict[str, object]]:
    """The candidate groups as the model reads them: names only (the egress gate aliases them)."""
    return [
        {
            "measures": list(g.measures),
            "kind": g.kind,
            "dated": g.dated,
            "columns": list(g.columns),
        }
        for g in groups
    ]


def _chosen(groups: tuple[WideGroup, ...], wanted: object) -> WideGroup:
    if not wanted:
        return groups[0]
    if not isinstance(wanted, list) or not all(isinstance(c, str) for c in wanted):
        raise AgentToolError("AGENT_UNPIVOT_GROUP_INVALID", "`columns` is a list of column names.")
    for group in groups:
        cut = group.only(wanted)
        if cut is not None:
            return cut
    raise AgentToolError(
        "AGENT_UNPIVOT_GROUP_INVALID",
        "Those columns are not one group of period columns; pick the group from `groups` in the answer.",
    )


def _year_of(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise AgentToolError("AGENT_UNPIVOT_YEAR_INVALID", "The year is a four-digit year such as 2024.")
    try:
        year = int(value)
    except ValueError:
        raise AgentToolError(
            "AGENT_UNPIVOT_YEAR_INVALID", "The year is a four-digit year such as 2024."
        ) from None
    if not MIN_YEAR <= year <= MAX_YEAR:
        raise AgentToolError("AGENT_UNPIVOT_YEAR_INVALID", f"The year is between {MIN_YEAR} and {MAX_YEAR}.")
    return year


def typed_year_effect(effect: OptionEffect, typed: str) -> OptionEffect:
    """The person typed the year (Q26's "Another year"): the step of `effect` with that year, checked again.
    Raises `ValueError` with a plain sentence when the year cannot be used."""
    from engine.agent.unpivot import UnpivotError, UnpivotSpec

    step = dict(effect.payload.get("step") or {})
    params = dict(step.get("params") or {})
    try:
        year = int(float(typed))
    except ValueError:
        raise ValueError("Type a four-digit year, for example 2019.") from None
    if float(typed) != year or not MIN_YEAR <= year <= MAX_YEAR:
        raise ValueError(f"Type a year between {MIN_YEAR} and {MAX_YEAR}.")
    old = params.get("year")
    params["year"] = year
    try:
        UnpivotSpec.from_params(str(step.get("column")), params)
    except UnpivotError as exc:
        raise ValueError(exc.message) from exc
    step["params"] = params
    title = effect.title.replace(f" of {old}", f" of {year}") if old is not None else effect.title
    return effect.model_copy(update={"payload": {**effect.payload, "step": step}, "title": title})


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
    groups = wide_column_groups(columns, skip=personal | {key} | ({outcome} if outcome else set()))
    if not groups:
        raise AgentToolError(
            "AGENT_UNPIVOT_NOT_WIDE",
            "No columns repeat a measure over months, quarters or years (such as spend_jan, spend_feb).",
        )
    turn.feedback.append(review.gates[index].prepare({"tool": "propose_unpivot", "groups": _facts(groups)}))
    group = _chosen(groups, args.get("columns"))
    proposed = _year_of(args.get("year"))
    if proposed is not None and group.dated:
        raise AgentToolError(
            "AGENT_UNPIVOT_YEAR_INVALID", "These column names carry their year; no year is given for them."
        )
    wide = set(group.columns)
    # Every other column stays on every row, personal ones too (by name; the engine hides their values as anywhere).
    kept = [c for c in columns if c not in wide and c != outcome]
    measures = [display_name(m) for m in group.measures]
    reason = str(args.get("reason") or "").strip()[:REASON_CHARS] or (
        f"The {group.kind} columns hold the same measures for different {group.kind}s; as rows they can be "
        "combined back into one row per customer."
    )
    first_month = first_month_of(group, columns)
    # A year the names do not carry is the person's to say: the AI's year first, then the recent ones, or typed.
    now = review.clock().year
    offered = [] if group.dated else [proposed, *range(now, now - YEARS_OFFERED, -1)]
    years: list[int | None] = (
        [None] if group.dated else list(dict.fromkeys(y for y in offered if y and MIN_YEAR <= y <= MAX_YEAR))
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
        params = group.params(
            key=key, id_columns=kept, outcome=outcome, taken=columns, year=year, first_month=first_month
        )
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
    left = [c for c in columns if c not in wide and c not in kept and c != outcome]
    values: dict[str, object] = {
        "file": display_name(ctx.profile.file_name or "the sheet"),
        "kind": group.kind,
        "measures": _and(measures),
        "entity": ctx.config.entity,
        "rows_in": f"{counts[0]:,}",
        "rows_out": f"{counts[1]:,}",
    }
    said = (
        ""  # what the catalogue's sentence cannot say: a year that turns, and the columns the step leaves out
    )
    if first_month is not None:
        said += (
            f" The months run from {_MONTH_NAMES[first_month]} across the new year: say the year "
            f"{_MONTH_NAMES[first_month]} falls in; the months after December are dated the year after."
        )
    if left:
        said += f" It leaves out {_and([display_name(c) for c in left])}."
    choices = [f"They cover {year}" for year in years if year is not None]
    if first_month is not None:
        choices = [f"It starts in {_MONTH_NAMES[first_month]} {year}" for year in years if year is not None]
    # The typed year starts from the AI's year (or the first offered) and is checked again when the person types it.
    typed = effects[0] if effects and not group.dated else None
    try:
        ask = build_ask(
            "Q25" if group.dated else "Q26",
            values,
            rows=len(ctx.frame),
            choices=choices,
            recommended_choice=0 if proposed is not None and not group.dated else None,
            effects=[*effects, typed, None] if not group.dated else [*effects, None],
            upload_id=review._upload_of(index),
            column=key,
            by_code=True,
        )
    except QuestionError as exc:
        raise AgentToolError("AGENT_QUESTION_INVALID", str(exc)[:200]) from exc
    if said:
        ask = replace(ask, text=ask.text + said)
    if ask.blocking and turn.blocking >= review.limits.asks.blocking:
        raise AgentToolError(
            "AGENT_BLOCKING_LIMIT",
            f"You may ask {review.limits.asks.blocking} blocking questions in one review; this one is dropped.",
        )
    turn.blocking += int(ask.blocking)
    turn.asks.append(ask)
    turn.feedback.append({"asked": ask.code})
