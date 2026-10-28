"""`propose_combine_rows` (catalogue #87): combine the several rows of each ID into one, as a proposal.

Guided setup's advisor asked this question already (`engine.agent.advisor._ask_to_combine`): when an ID repeats, the
model needs one row per ID, and `engine.agent.reshape` can build it - a count, totals, averages and the latest value of
each column, using only the rows dated on or before the ID's snapshot, with the outcome read from the latest row.
This tool puts that plan in the Data Doctor's registry. The rules that were the advisor's are now the tool's:

* the dates come from the file (`choose_dates`) unless the caller names them; a file with no date column is refused,
  because rows cannot be combined without saying when each one happened;
* the columns that hold one value per ID (consent, opt-out, recently contacted, and the ones the caller names) are
  carried as they are, never added up and never used as the date; personal data is never a signal;
* the plan is tried on the file (no future-data check yet: that runs at Approve), so the question shows the rows in
  and the rows out.

Like `keep_latest_per_id` it is a reshape step (decision O3): offered through one blocking question, never ticked,
guarded at Approve (`engine.doctor.reshape_guard`). Only column names and counts are returned - no cell.
"""

from __future__ import annotations

from typing import Any, Final

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from api.access_policy import RoutePolicy
from engine.access.roles import Role
from engine.agent.contracts import ProposalSource, Question, RecipeStepKind, StepReceipt
from engine.agent.reshape import CombineError, choose_dates, combine_rows, plan_combine
from engine.agent.untrusted import display_name
from engine.doctor.registry import (
    DataTool,
    DataToolContext,
    DataToolError,
    DataToolKind,
    DataToolRegistry,
    Exposure,
)
from engine.doctor.reshape_guard import reshape_question
from engine.doctor.row_contracts import StepProposal

__all__ = [
    "M_RESHAPE_TOOLS",
    "ProposeCombineRowsInput",
    "ProposeCombineRowsOutput",
    "propose_combine_rows",
    "register_reshape_tools",
]

M_RESHAPE_TOOLS: Final[tuple[str, ...]] = ("propose_combine_rows",)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProposeCombineRowsInput(_Model):
    time_column: str | None = Field(
        default=None, description="The date that orders each ID's rows; null: the file's own date column."
    )
    snapshot_column: str | None = Field(
        default=None,
        description="The date each row is described as of; null: each ID's latest date. Given only with "
        "`time_column`.",
    )
    carry: tuple[str, ...] = Field(
        default=(),
        max_length=200,
        description="Columns that hold one value per ID, copied from its latest row, never added up.",
    )


class ProposeCombineRowsOutput(_Model):
    step: StepProposal = Field(description="The pending step, never ticked.")
    rows_in: int = Field(ge=0, description="Rows of the file.")
    rows_out: int = Field(ge=0, description="Rows the step would leave: one per ID.")
    ids: int = Field(ge=0, description="IDs in the file.")
    rows_per_id: float = Field(ge=0.0, description="Rows an ID has on average.")
    most_rows: int = Field(ge=0, description="Rows of the ID that has the most.")
    failed: int = Field(ge=0, description="Rows with no ID or no readable date, left out of the combine.")
    features: tuple[str, ...] = Field(description="The columns the combined file would hold besides the ID.")
    question: Question = Field(description="The blocking question the step is offered through (decision O3).")
    sentence: str = Field(description="What the person approves, in plain words.")
    role: Role = Field(description="The least role that may approve it.")


def _protected(ctx: DataToolContext, frame: pd.DataFrame, key: str, outcome: str | None) -> set[str]:
    """The columns that hold one value per ID by the use case's own settings, as the advisor read them."""
    agent = ctx.require_agent()
    config = agent.config
    named = (
        config.actions.suppression.opt_out_column,
        config.actions.suppression.recently_contacted_column,
        config.governance.consent_column,
    )
    return {name for name in named if name and name in frame.columns} - {key, outcome or ""}


def propose_combine_rows(given: ProposeCombineRowsInput, ctx: DataToolContext) -> ProposeCombineRowsOutput:
    """Plan, try and describe the combine of this file's rows into one row per ID."""
    agent = ctx.require_agent()
    frame, profile, config = agent.frame, agent.profile, agent.config
    key, outcome = ctx.primary_key, agent.target
    if key is None or key not in frame.columns:
        raise DataToolError("TOOL_INPUT_INVALID", "The file has no ID column to combine the rows of.")
    if given.snapshot_column is not None and given.time_column is None:
        raise DataToolError("TOOL_INPUT_INVALID", "Name the `time_column` that orders the rows too.")
    entity = config.entity
    for name in (given.time_column, given.snapshot_column, *given.carry):
        if name is None:
            continue
        if name not in frame.columns:
            raise DataToolError("DOCTOR_COLUMN_UNKNOWN", f"The file has no column '{name}'.")
        if name in {key, outcome}:
            raise DataToolError(
                "DOCTOR_COLUMN_PROTECTED",
                f"'{name}' is the ID or the outcome column, which no step ever changes.",
            )
    personal = {c.name for c in profile.columns if c.pii_kinds}
    held = _protected(ctx, frame, key, outcome)
    carried = (held | {name for name in given.carry if name not in personal}) - {key, outcome or ""}

    counts = frame[key].dropna().value_counts()
    ids = len(counts)
    rows_per_id = round(float(counts.sum()) / ids, 1) if ids else 0.0
    most = int(counts.max()) if ids else 0
    if not int((counts > 1).sum()):
        raise DataToolError(
            "TOOL_INPUT_INVALID", f"Each {entity} is on one row already; there is nothing to combine."
        )

    if given.time_column is not None:
        time_column, snapshot_column = given.time_column, given.snapshot_column
    else:
        dates = choose_dates(
            profile,
            exclude=[key, *([outcome] if outcome else []), *sorted(carried)],
            snapshot_hint=config.split.time_column,
        )
        if dates is None:
            raise DataToolError(
                "TOOL_INPUT_INVALID",
                f"The rows could be combined into one per {entity}, but that needs a date column saying when "
                "each row happened, and this file has none.",
            )
        time_column, snapshot_column = dates
    params = plan_combine(
        frame,
        profile,
        key=key,
        time_column=time_column,
        snapshot_column=snapshot_column,
        outcome=outcome,
        carry=sorted(carried - {time_column, snapshot_column}),
    )
    try:
        # The future-data check runs when the plan is approved (training: full), not on this trial.
        tried = combine_rows(
            frame,
            key=key,
            params=params,
            max_failure_pct=config.agent.max_conversion_failure_pct,
            leak_check=None,
        )
    except CombineError as exc:
        raise DataToolError(exc.code, exc.message) from exc

    as_of = (
        f"the latest '{display_name(snapshot_column)}' of each {entity}"
        if snapshot_column is not None
        else f"each {entity}'s latest '{display_name(time_column)}'"
    )
    reason = (
        f"Each {entity} appears on {rows_per_id:g} rows on average (up to {most}), and the model needs one row "
        f"per {entity}. Per '{display_name(key)}', the rows are counted and their values added up, averaged or "
        f"taken from the latest row, using only rows dated on or before {as_of}"
        + (f"; '{display_name(outcome)}' is read from the {entity}'s latest row." if outcome else ".")
    )
    if snapshot_column is None:
        reason += (
            " Every row counts, so the file should hold only rows known before the outcome was measured."
        )
    title = f"Combine the rows into one row per {entity}"
    step = StepProposal(
        kind=RecipeStepKind.COMBINE_ROWS,
        column=key,
        params=params,
        source=ProposalSource.RULES,
        title=title,
        reason=reason,
        changed=tried.entities,
        out_of=tried.rows_in,
        share_pct=0.0 if not tried.rows_in else round(tried.entities / tried.rows_in * 100.0, 4),
    )
    receipt = StepReceipt(
        order=1,
        kind=RecipeStepKind.COMBINE_ROWS,
        column=key,
        rows=tried.rows_in,
        changed=tried.entities,
        failed=tried.failed,
    )
    named: list[Any] = [f["name"] for f in params["features"]]
    return ProposeCombineRowsOutput(
        step=step,
        rows_in=tried.rows_in,
        rows_out=tried.entities,
        ids=ids,
        rows_per_id=rows_per_id,
        most_rows=most,
        failed=tried.failed,
        features=tuple(str(n) for n in named),
        question=reshape_question(step.as_step(1), receipt=receipt, rows_out=tried.entities),
        sentence=f"{title}: {tried.rows_in:,} rows become {tried.entities:,}, built only from each {entity}'s "
        "rows on or before its snapshot.",
        role=Role.ANALYST,
    )


def register_reshape_tools(registry: DataToolRegistry) -> tuple[str, ...]:
    """Register `propose_combine_rows` (catalogue #87), for the rules, the AI and the screen; its names."""
    registry.register(
        DataTool(
            number=87,
            name="propose_combine_rows",
            kind=DataToolKind.PROPOSE,
            description=(
                "Propose combining the several rows of each ID into one row per ID (reshape level): counts, totals, "
                "averages and the latest value, from the rows dated on or before the ID's snapshot. Offered through "
                "a blocking question and never ticked; nothing changes until the person approves."
            ),
            input_model=ProposeCombineRowsInput,
            output_model=ProposeCombineRowsOutput,
            route_policy=RoutePolicy(
                role=Role.ANALYST,
                action="doctor.propose_combine_rows",
                purpose="ask Data Doctor for a suggestion",
            ),
            exposure=Exposure.SESSION_ONLY,
            run=propose_combine_rows,
            callers=frozenset({"rules", "ai", "person"}),
            needs=("agent",),
            policy_kind="data.propose_combine_rows",
            step_kinds=(RecipeStepKind.COMBINE_ROWS,),
            wraps="engine/agent/reshape.py plan_combine, combine_rows (the advisor's _ask_to_combine)",
        )
    )
    return M_RESHAPE_TOOLS
