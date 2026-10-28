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

**The per-column plan is a draft the AI edits.** `plan_combine` drafts how each column is combined by its type
(numbers summed, averaged, the largest and the latest; categories the latest and the count of values). The output's
`plan` lists it per column (dtype, whether it holds one value per ID, the functions) - names only, no cell. The AI
changes a row with a reason (`columns`: "UnitPrice: mean, not sum"; an empty list leaves a column out) and may read the
outcome as the largest value of the rows at the snapshot (`outcome_function: max`, "ever bought"); the tool checks the
edited table (`CombineSpec.from_params`) and the person approves that table, shown in the question. What never moves:
the consent, opt-out and recently-contacted columns are carried and never combined, a personal column is never a
signal (nor a date), the step is never ticked, and the future-data check runs at Approve.

Like `keep_latest_per_id` it is a reshape step (decision O3): offered through one blocking question, never ticked,
guarded at Approve (`engine.doctor.reshape_guard`). Only column names and counts are returned - no cell.
"""

from __future__ import annotations

from typing import Any, Final, Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from api.access_policy import RoutePolicy
from engine.access.roles import Role
from engine.agent.contracts import ProposalSource, Question, RecipeStepKind, StepReceipt
from engine.agent.reshape import (
    COMBINE_FUNCTIONS,
    CombineError,
    CombineSpec,
    choose_dates,
    combine_rows,
    plan_combine,
)
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


_COLUMN_FUNCTIONS: Final[frozenset[str]] = COMBINE_FUNCTIONS - {"count", "days_since_first"}
_NUMBER_ONLY: Final[frozenset[str]] = frozenset({"sum", "mean", "max", "min"})


class CombineColumnChange(_Model):
    """The AI's change to one row of the drafted per-column plan, with its reason."""

    column: str = Field(min_length=1, max_length=128, description="The column of the file.")
    functions: tuple[str, ...] = Field(
        max_length=6,
        description="How it is combined: sum, mean, max, min, latest, nunique, days_since_last; empty: left out.",
    )
    reason: str = Field(
        min_length=1, max_length=120, description="Why, in a few words ('a price: mean, not sum')."
    )


class CombineColumnRow(_Model):
    """One column of the plan, as the person approves it: names and facts only, never a cell."""

    column: str
    dtype: str
    one_value_per_id: bool = Field(description="True when no ID has two different values in it.")
    functions: tuple[str, ...] = Field(description="How it is combined; empty: left out.")
    carried: bool = Field(default=False, description="Copied from each ID's latest row (one value per ID).")
    reason: str | None = Field(default=None, description="The AI's reason, when it changed the draft.")


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
    columns: tuple[CombineColumnChange, ...] = Field(
        default=(),
        max_length=60,
        description="Changes to the drafted per-column plan, each with a reason; the rest stays as drafted.",
    )
    outcome_function: Literal["latest", "max"] | None = Field(
        default=None,
        description="How the outcome is read from each ID's rows at its snapshot: 'latest' (the default) or "
        "'max' (a yes on any of them: 'ever bought').",
    )
    outcome_reason: str | None = Field(default=None, max_length=120, description="Why, when 'max'.")


class ProposeCombineRowsOutput(_Model):
    step: StepProposal = Field(description="The pending step, never ticked.")
    rows_in: int = Field(ge=0, description="Rows of the file.")
    rows_out: int = Field(ge=0, description="Rows the step would leave: one per ID.")
    ids: int = Field(ge=0, description="IDs in the file.")
    rows_per_id: float = Field(ge=0.0, description="Rows an ID has on average.")
    most_rows: int = Field(ge=0, description="Rows of the ID that has the most.")
    failed: int = Field(ge=0, description="Rows with no ID or no readable date, left out of the combine.")
    features: tuple[str, ...] = Field(description="The columns the combined file would hold besides the ID.")
    plan: tuple[CombineColumnRow, ...] = Field(
        default=(),
        description="How each column is combined (the draft with the AI's changes): what is approved.",
    )
    outcome_function: Literal["latest", "max"] = "latest"
    left_out: tuple[str, ...] = Field(
        default=(), description="Personal columns the combine leaves out (personal data is never a signal)."
    )
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
    for name in (given.time_column, given.snapshot_column):
        if name is not None and (name in held or name in personal):
            raise DataToolError(
                "DOCTOR_COLUMN_PROTECTED",
                f"'{name}' is a consent, opt-out, contact or personal column, which is never used as the date.",
            )
    refused = sorted(name for name in given.carry if name in personal)
    if refused:
        raise DataToolError(
            "DOCTOR_COLUMN_PROTECTED",
            f"'{refused[0]}' holds personal data, which the combine never carries: it is left out.",
        )
    carried = (held | set(given.carry)) - {key, outcome or ""}

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
    params, given = _edited(params, given, frame, key, outcome, held=held, personal=personal, carried=carried)
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
    max_outcome = params.get("outcome_function") == "max"
    read_as = "the largest value of the rows at its snapshot" if max_outcome else f"the {entity}'s latest row"
    reason = (
        f"Each {entity} appears on {rows_per_id:g} rows on average (up to {most}), and the model needs one row "
        f"per {entity}. Per '{display_name(key)}', the rows are counted and their values added up, averaged or "
        f"taken from the latest row, using only rows dated on or before {as_of}"
        + (f"; '{display_name(outcome)}' is read from {read_as}." if outcome else ".")
    )
    if max_outcome and given.outcome_reason:
        reason += f" (Outcome: {given.outcome_reason.strip()})"
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
    plan = _plan_rows(params, frame, key, carried, given.columns)
    receipt = StepReceipt(
        order=1,
        kind=RecipeStepKind.COMBINE_ROWS,
        column=key,
        rows=tried.rows_in,
        changed=tried.entities,
        failed=tried.failed,
        note=_plan_note(plan),
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
        plan=plan,
        outcome_function="max" if max_outcome else "latest",
        left_out=tuple(sorted(c for c in personal if c in frame.columns and c not in {key, outcome})),
        question=reshape_question(step.as_step(1), receipt=receipt, rows_out=tried.entities),
        sentence=f"{title}: {tried.rows_in:,} rows become {tried.entities:,}, built only from each {entity}'s "
        "rows on or before its snapshot.",
        role=Role.ANALYST,
    )


def _stem(name: str) -> str:
    from engine.agent.reshape import _safe

    return _safe(name)


def _edited(
    params: dict[str, Any],
    given: ProposeCombineRowsInput,
    frame: pd.DataFrame,
    key: str,
    outcome: str | None,
    *,
    held: set[str],
    personal: set[str],
    carried: set[str],
) -> tuple[dict[str, Any], ProposeCombineRowsInput]:
    """The drafted parameters with the AI's per-column changes and outcome reading, checked by the engine's own
    parameter check. Refuses a change to the ID, the dates, the outcome, a carried consent column or personal data.
    """
    if outcome is not None:
        # a change on the outcome column says how the outcome is read ("max": a yes on any row), with its reason
        on_outcome = [c for c in given.columns if c.column == outcome]
        if on_outcome:
            how = on_outcome[0].functions
            if how not in (("max",), ("latest",)):
                raise DataToolError(
                    "TOOL_INPUT_INVALID", "The outcome is read from the latest row ('latest') or as 'max'."
                )
            given = given.model_copy(
                update={
                    "columns": tuple(c for c in given.columns if c.column != outcome),
                    "outcome_function": how[0],
                    "outcome_reason": given.outcome_reason or on_outcome[0].reason,
                }
            )
    if not given.columns and given.outcome_function in (None, "latest"):
        return params, given
    fixed = {key, params["time_column"], params.get("snapshot_column"), outcome}
    features = list(params["features"])
    taken = {f["name"] for f in features} | {key, params.get("snapshot_column") or params["time_column"]}
    if outcome:
        taken.add(outcome)
    seen: set[str] = set()
    for change in given.columns:
        name = change.column
        if name not in frame.columns:
            raise DataToolError("DOCTOR_COLUMN_UNKNOWN", f"The file has no column '{name}'.")
        if name in seen:
            raise DataToolError("TOOL_INPUT_INVALID", f"'{name}' is changed twice; give one row per column.")
        seen.add(name)
        if name in fixed:
            raise DataToolError(
                "DOCTOR_COLUMN_PROTECTED",
                f"'{name}' is the ID, a date or the outcome of the combine, which no change touches.",
            )
        if name in held or name in personal:
            raise DataToolError(
                "DOCTOR_COLUMN_PROTECTED",
                f"'{name}' is a consent, opt-out, contact or personal column: carried as it is, or left out, "
                "never combined.",
            )
        unknown = [f for f in change.functions if f not in _COLUMN_FUNCTIONS]
        if unknown:
            raise DataToolError(
                "TOOL_INPUT_INVALID",
                f"'{unknown[0]}' is not a way of combining a column; use {', '.join(sorted(_COLUMN_FUNCTIONS))}.",
            )
        series = frame[name]
        numeric = pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_bool_dtype(series)
        if not numeric and set(change.functions) & _NUMBER_ONLY:
            raise DataToolError(
                "TOOL_INPUT_INVALID", f"'{name}' does not hold numbers, so it cannot be added up."
            )
        features = [f for f in features if f["column"] != name]
        taken -= {f["name"] for f in params["features"] if f["column"] == name}
        for function in dict.fromkeys(change.functions):
            if name in carried and function == "latest":
                feature = name
            else:
                feature, n = f"{_stem(name)}_{function}", 2
                while feature in taken:
                    feature, n = f"{_stem(name)}_{function}_{n}", n + 1
            taken.add(feature)
            features.append({"name": feature, "function": function, "column": name})
    edited = {**params, "features": features}
    if given.outcome_function == "max":
        if outcome is None:
            raise DataToolError("TOOL_INPUT_INVALID", "There is no outcome to read as the largest value.")
        edited["outcome_function"] = "max"
    dayfirst = edited.get("dayfirst")
    if isinstance(dayfirst, dict):
        reads = {params["time_column"], params.get("snapshot_column"), *(f["column"] for f in features)}
        edited["dayfirst"] = {c: o for c, o in dayfirst.items() if c in reads}
    try:
        CombineSpec.from_params(key, edited)
    except CombineError as exc:
        raise DataToolError("TOOL_INPUT_INVALID", exc.message) from exc
    return edited, given


def _plan_rows(
    params: dict[str, Any],
    frame: pd.DataFrame,
    key: str,
    carried: set[str],
    changes: tuple[CombineColumnChange, ...],
) -> tuple[CombineColumnRow, ...]:
    """The per-column table: every column the step reads besides the ID and dates, in file order."""
    by_column: dict[str, list[str]] = {}
    for feature in params["features"]:
        if feature["column"] is not None:
            by_column.setdefault(feature["column"], []).append(feature["function"])
    reasons = {c.column: c.reason.strip() for c in changes}
    skip = {key, params["time_column"], params.get("snapshot_column"), params.get("outcome")}
    rows: list[CombineColumnRow] = []
    grouped = frame.groupby(key, dropna=True, sort=False)
    for raw in frame.columns:
        name = str(raw)
        if name in skip:
            continue
        functions = tuple(by_column.get(name, ()))
        if not functions and name not in reasons:
            continue
        rows.append(
            CombineColumnRow(
                column=name,
                dtype=str(frame[name].dtype),
                one_value_per_id=bool(grouped[name].nunique(dropna=True).max() <= 1) if len(frame) else True,
                functions=functions,
                carried=name in carried,
                reason=reasons.get(name),
            )
        )
    return tuple(rows)


def _plan_note(plan: tuple[CombineColumnRow, ...]) -> str:
    """The per-column plan in the question's words (column names and functions only)."""
    parts = [
        f"'{display_name(r.column)}': {', '.join(r.functions) or 'left out'}"
        + (f" ({r.reason})" if r.reason else "")
        for r in plan
    ]
    return ("Per column: " + "; ".join(parts) + ".") if parts else ""


def register_reshape_tools(registry: DataToolRegistry) -> tuple[str, ...]:
    """Register `propose_combine_rows` (catalogue #87), for the rules, the AI and the screen; its names."""
    registry.register(
        DataTool(
            number=87,
            name="propose_combine_rows",
            kind=DataToolKind.PROPOSE,
            description=(
                "Propose combining the several rows of each ID into one row per ID (reshape level): counts, totals, "
                "averages and the latest value, from the rows dated on or before the ID's snapshot. Its answer's "
                "`plan` is a draft per column: change rows with `columns` (column, functions, reason; on the "
                "outcome column, `max` reads a yes on any row). Offered through a blocking question and never "
                "ticked; nothing changes until the person approves the table."
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
