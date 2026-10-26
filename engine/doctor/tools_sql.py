"""`propose_sql_step`: a query Data Doctor wrote, tried before it is shown (the owner's "own SQL step").

When no fixed step does the job (join two files, add up bills per customer), the rules or the AI may write one
read-only DuckDB query over the case's files. This tool dry-runs it in the sandbox of `engine.agent.sql_step` -
one SELECT, only the listed tables, no files, no clock, no randomness, a time limit, the files never changed - on
the first rows of each file, and returns a pending, never-ticked proposal: a `sql_query` recipe step, a plain
description first, the rows read from each file and the rows and columns the new table would have. The query
itself is carried as detail only. It is approved with the rest of the plan; nothing is applied here, and next
month's files go through the same step (the prescription replays it).

What the query reads (one rule, for the trial and for every run): each file AFTER the fixes in the plan for it
(a date already parsed, a currency already converted), because a query is a `post_build` step and runs after the
steps before it. The trial applies those steps to the sample first: the accepted ones, and the ones still waiting for
a yes, counted as accepted (the plan as it stands if the person ticks what is proposed). A run applies the same
steps, and in a one-file case the step keeps the file's table name (`this_table`) so the run feeds the query that
file as it stands at the step, never the raw upload and never a table it does not have. In a case with several
files or a build, the name is dropped and the query reads the prepared table of that name the run gives it.

A fix accepted, unticked or proposed after the query changes what the run feeds it. The step keeps a digest of the
fixes it was tried after (`tried_after`); when the plan check finds the query cannot run after other fixes, it says
so (`PLAN_QUERY_TRIED_BEFORE_CHANGE`) and asks for the query again. The rules' own queries (the grain checks) are
tried the same way, on the plan so far.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Final, Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from api.access_policy import RoutePolicy
from engine.access.roles import Role
from engine.agent.config import AgentConfig
from engine.agent.contracts import RecipeStep, RecipeStepKind
from engine.agent.dataset_recipe import consistent_types
from engine.agent.recipe import RecipeError, run_recipe
from engine.agent.sql_step import (
    MAX_INPUTS,
    MAX_SQL_CHARS,
    AsOfRule,
    SqlStepError,
    resolve_as_of_rule,
    run_sql,
    sql_params,
)
from engine.config import StrictBase
from engine.doctor.guards import (
    Finding,
    NotMeasurableError,
    Receipt,
    build_ledger,
    cell_change,
    infer_key,
    make_receipt,
    plan_findings,
    receipt_findings,
)
from engine.doctor.plan_check import guard_levels, recipe_steps_of, steps_digest
from engine.doctor.prescription import slot_name
from engine.doctor.registry import (
    BY_EXPOSURE,
    DataTool,
    DataToolContext,
    DataToolError,
    DataToolKind,
    DataToolRegistry,
    Exposure,
)

__all__ = ["DRY_RUN_ROWS", "M_SQL_TOOLS", "ProposeSqlStepArgs", "SqlStepProposal", "register_sql_tools"]

M_SQL_TOOLS: Final[tuple[str, ...]] = ("propose_sql_step",)
DRY_RUN_ROWS: Final[int] = 100_000
"""A query is tried on at most this many rows of each file; a bigger file is said to be sampled."""
MAX_COLUMNS_LISTED: Final[int] = 40
MAX_COLUMNS_KEPT: Final[int] = 500
"""Names kept for the goal check (the display above shows fewer): a wide table's outcome may come last."""


class ProposeSqlStepArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    description: str = Field(min_length=1, max_length=300, description="What the query does, in plain words.")
    sql: str = Field(
        min_length=1,
        max_length=MAX_SQL_CHARS,
        description="One SELECT over `t_<file name>` tables (`customers.csv` is `t_customers`); `$as_of` is the date.",
    )
    output_name: str = Field(min_length=1, max_length=64, description="What the new table is called.")
    upload_ids: tuple[str, ...] = Field(
        min_length=1, max_length=MAX_INPUTS, description="The files it reads."
    )
    as_of: date | None = Field(
        default=None, description="The date `$as_of` stands for, when the query uses it."
    )
    as_of_rule: Literal["latest_date_in_file", "last_complete_month_end"] | None = Field(
        default=None,
        description="Keep the as-of date as a rule worked out again on every file, read from `as_of_upload_id`'s "
        "`as_of_column`; `as_of` is then the date it gives now.",
    )
    as_of_upload_id: str | None = Field(default=None, description="The file whose dates the rule reads.")
    as_of_column: str | None = Field(
        default=None, max_length=128, description="The date column the rule reads."
    )
    reads_future: bool = Field(
        default=False,
        description="True only for a query that builds the outcome from rows after `$as_of`; only the outcome "
        "column is let off the future-rows check.",
    )


class SqlTableSummary(StrictBase):
    name: str = Field(description="The table's name; the query reads it as `t_` + this.")
    file_name: str
    rows: int
    columns: int


class SqlStepProposal(StrictBase):
    """The proposal: pending, `check`, never ticked; the step is added only when the person approves the plan."""

    state: Literal["pending"] = "pending"
    confidence: Literal["check"] = "check"
    never_ticked: Literal[True] = True
    ticked: Literal[False] = False
    source: Literal["rules", "ai"]
    title: str = Field(description="The item's headline: what it does, in plain words.")
    sentence: str = Field(
        description="What the person approves, word for word: the description and the counts."
    )
    stage: Literal["post_build"] = "post_build"
    step: dict[str, Any] = Field(description="The `sql_query` recipe step.")
    inputs: tuple[SqlTableSummary, ...]
    rows_in: int = Field(description="Rows of the first file the query read.")
    rows_out: int = Field(description="Rows the new table has.")
    columns_out: int
    columns: tuple[str, ...] = Field(description="Names of the new table's columns (the first ones).")
    all_columns: tuple[str, ...] = Field(
        default=(),
        description="Names of the new table's columns, up to MAX_COLUMNS_KEPT (for the goal check).",
    )
    two_valued: tuple[str, ...] = Field(
        default=(), description="Names of the columns with at most two values on the tried rows (names only)."
    )
    sampled: bool = Field(description="True when it was tried on the first rows of a bigger file.")
    receipt: Receipt = Field(
        description="What the query did, counted by our code: customers kept, lost, repeated."
    )
    changed_cells: int | None = Field(
        default=None, description="Cells of a column that kept its name whose value is now different."
    )
    compared_cells: int | None = Field(
        default=None, description="Cells that were compared; none if not measurable."
    )
    findings: tuple[Finding, ...] = Field(
        default=(), description="What the guards found: stop, flag or note."
    )

    needs_yes: bool = Field(
        description="A stop, or a query that could not be measured: the person says yes knowing the counts."
    )

    def payload(self) -> dict[str, Any]:
        """The case item's payload: the step, where it runs, the counts the 2% rule reads, and the sentence."""
        return {
            "step": self.step,
            "stage": self.stage,
            "rows_in": self.rows_in,
            "rows_out": self.rows_out,
            "sentence": self.sentence,
            "receipt": self.receipt.model_dump(mode="json"),
            "findings": [f.model_dump(mode="json") for f in self.findings],
            "columns": list(
                self.all_columns or self.columns
            ),  # names only: what the new table holds (the goal check reads them)
            "two_valued": list(self.two_valued),  # names only: which of them could be a yes/no outcome
            "changed_cells": self.changed_cells,
            "compared_cells": self.compared_cells,
            "needs_yes": self.needs_yes,
            "detail": str(self.step["params"]["sql"]),  # the query, shown only as detail
        }


def _names(ctx: DataToolContext, upload_ids: tuple[str, ...]) -> dict[str, tuple[str, pd.DataFrame, str]]:
    """Table name -> (upload id, frame, file name) for the files asked for, named by file as a prescription does."""
    loaded = {f.upload_id: f for f in ctx.files}
    out: dict[str, tuple[str, pd.DataFrame, str]] = {}
    for upload_id in upload_ids:
        file = loaded.get(upload_id)
        if file is None:
            raise DataToolError("TOOL_INPUT_INVALID", "That file is not in this case.", status=422)
        file_name = str(file.file_name or "").strip() or upload_id
        base = slot_name(Path(file_name).stem)
        name, n = base, 2
        while name in out:
            name, n = f"{base}_{n}", n + 1
        out[name] = (upload_id, file.frame, file_name)
    return out


def _prepared(
    ctx: DataToolContext, named: dict[str, tuple[str, pd.DataFrame, str]], *, whole: bool = False
) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
    """Each file's first rows (all of them when `whole`) after the case's accepted steps on it, with the digest
    of the steps applied: what the query will read in a run.

    The steps that are still waiting for a yes count as accepted (the plan as it stands if the person ticks what is
    proposed); a fix accepted or unticked later changes the input, and the digest is how the step notices."""
    only = len(ctx.files) == 1
    raw = {n: f for n, (_, f, _) in named.items()}
    out: dict[str, pd.DataFrame] = {}
    digests: dict[str, str] = {}
    for name, (upload_id, frame, file_name) in named.items():
        base = consistent_types(frame if whole else frame.head(DRY_RUN_ROWS)).copy()
        steps = [
            step for step, _ in recipe_steps_of(ctx.case_items or (), upload_id, only_file=only, pending=True)
        ]
        digests[name] = steps_digest(steps)
        if not steps:
            out[name] = base
            continue
        try:
            out[name] = run_recipe(
                base,
                steps,
                upload_id=upload_id,
                primary_key=None,
                target=None,
                levels=guard_levels(steps),
                max_failure_pct=AgentConfig().max_conversion_failure_pct,
                tables=raw,
            ).frame
        except RecipeError as exc:
            raise DataToolError(
                "TOOL_INPUT_INVALID",
                f"The fixes already in the plan for {file_name} cannot be applied to it first: {exc.message}",
                status=409,
            ) from exc
    return out, digests


def _as_of_rule(
    given: ProposeSqlStepArgs, named: dict[str, tuple[str, pd.DataFrame, str]]
) -> tuple[date, dict[str, Any]]:
    """The rule as the step keeps it, and the date it gives on these files now."""
    table = next((n for n, (upload_id, _, _) in named.items() if upload_id == given.as_of_upload_id), None)
    if table is None or given.as_of_column is None or given.as_of_rule is None:
        raise SqlStepError("SQL_NOT_VALID", "An as-of date rule names the file and the date column it reads.")
    frame = named[table][1]
    if given.as_of_column not in frame.columns:
        raise SqlStepError("SQL_NOT_VALID", f"There is no column '{given.as_of_column}' in that file.")
    rule = AsOfRule(rule=given.as_of_rule, table=table, column=given.as_of_column)
    return resolve_as_of_rule(rule, frame[given.as_of_column]), rule.model_dump(mode="json")


def _two_valued(frame: pd.DataFrame) -> tuple[str, ...]:
    """Names of the columns holding at most two different values (blanks aside): the shape of a yes/no outcome."""
    found: list[str] = []
    for name in frame.columns:
        try:
            if frame[name].nunique(dropna=True) <= 2:
                found.append(str(name))
        except (TypeError, ValueError):  # unhashable cells or a repeated name: not a yes/no
            continue
    return tuple(found)


def _propose(given: ProposeSqlStepArgs, ctx: DataToolContext) -> SqlStepProposal:
    raw = _names(ctx, given.upload_ids)
    prepared, digests = _prepared(ctx, raw)
    named = {name: (upload_id, prepared[name], file_name) for name, (upload_id, _, file_name) in raw.items()}
    params: dict[str, Any] = {
        "sql": given.sql,
        "inputs": list(named),
        "output_name": given.output_name,
        "description": given.description,
    }
    if len(ctx.files) == 1 and len(named) == 1:
        only = next(iter(named))
        params["this_table"] = only  # a run reads its one file under this name, as tried
        params["tried_after"] = digests[only]  # ... and after the fixes in the plan now
    as_of = given.as_of
    try:
        tried = {name: frame for name, (_, frame, _) in named.items()}
        if given.as_of_rule is not None:
            # The rule reads the whole file, after the same fixes, as a run does (not the tried rows only).
            whole = _prepared(ctx, raw, whole=True)[0]
            as_of, rule = _as_of_rule(given, {n: (u, whole[n], f) for n, (u, _, f) in raw.items()})
            params["as_of_rule"] = rule
        if as_of is not None:
            params["as_of"] = as_of.isoformat()
        if given.reads_future:
            params["reads_future"] = True
        sql_params(params)
        run = run_sql(given.sql, tried, output_name=given.output_name, as_of=as_of)
    except SqlStepError as exc:
        raise DataToolError(exc.code, exc.message, status=422) from exc
    first = next(iter(tried.values()))
    key = ctx.primary_key
    receipt = make_receipt(1, RecipeStepKind.SQL_QUERY.value, tried, run.frame, key=key, sql=given.sql)
    findings = receipt_findings(receipt) + plan_findings(build_ledger([receipt]))
    changed_cells: int | None
    compared_cells: int | None
    try:
        changed_cells, compared_cells = cell_change(first, run.frame, key=key or infer_key(first, run.frame))
    except NotMeasurableError as exc:
        changed_cells = compared_cells = None
        findings.append(Finding(level="flag", code="STEP_NOT_MEASURED", message=f"{exc} It needs your yes."))
    inputs = tuple(
        SqlTableSummary(name=name, file_name=file_name, rows=len(raw[name][1]), columns=len(frame.columns))
        for name, (_, frame, file_name) in named.items()
    )
    sampled = any(len(frame) > DRY_RUN_ROWS for _, frame, _ in raw.values())
    read = ", ".join(f"{t.file_name} ({t.rows:,} rows)" for t in inputs)
    sentence = (
        f"{given.description.rstrip('.')}. Reads {read}; the new table '{given.output_name}' would have "
        f"{run.receipt.rows_out:,} rows and {run.receipt.columns_out:,} columns"
        + (f" (tried on the first {DRY_RUN_ROWS:,} rows of each file)." if sampled else ".")
    )
    if receipt.keys_in is not None:
        sentence += f" Customers: {receipt.keys_in:,} before, {receipt.keys_out:,} after ({receipt.keys_lost:,} lost, {receipt.keys_repeated:,} repeated)."
    step = RecipeStep(
        order=1,
        kind=RecipeStepKind.SQL_QUERY,
        column=given.output_name,
        params=params,
        reason=given.description,
    )
    return SqlStepProposal(
        source="ai" if ctx.caller == "ai" else "rules",
        title=given.description.rstrip("."),
        sentence=sentence,
        step=step.model_dump(mode="json", exclude={"proposal_id"}),
        inputs=inputs,
        rows_in=len(first),
        rows_out=run.receipt.rows_out,
        columns_out=run.receipt.columns_out,
        columns=run.receipt.columns[:MAX_COLUMNS_LISTED],
        all_columns=run.receipt.columns[:MAX_COLUMNS_KEPT],
        two_valued=_two_valued(run.frame)[:MAX_COLUMNS_KEPT],
        sampled=sampled,
        receipt=receipt,
        changed_cells=changed_cells,
        compared_cells=compared_cells,
        findings=tuple(findings),
        needs_yes=compared_cells is None or any(f.level == "stop" for f in findings),
    )


def register_sql_tools(registry: DataToolRegistry) -> tuple[str, ...]:
    """Register `propose_sql_step` (catalogue #84), for the rules and the AI; its names."""
    registry.register(
        DataTool(
            number=84,
            name="propose_sql_step",
            kind=DataToolKind.PROPOSE,
            description=(
                "Propose one read-only SQL query over the case's files that makes a new table (a join, a total "
                "per customer, the latest row). The query reads each file after the plan's fixes, proposed ones "
                "included (dates already parsed). It is tried on the first rows and shown as a plain description "
                "with the rows read and made; nothing is changed until the person approves the plan."
            ),
            input_model=ProposeSqlStepArgs,
            output_model=SqlStepProposal,
            route_policy=RoutePolicy(
                role=Role.ANALYST,
                action="doctor.propose_sql_step",
                purpose="ask Data Doctor for a suggestion",
            ),
            exposure=Exposure.SESSION_ONLY,
            run=_propose,
            callers=frozenset({"rules", "ai"}),
            needs=("files",),
            max_output_bytes=BY_EXPOSURE,
            policy_kind="data.propose_sql_step",
            wraps="engine/agent/sql_step.py run_sql (DuckDB, sandboxed); the sql_query recipe step",
        )
    )
    return M_SQL_TOOLS
