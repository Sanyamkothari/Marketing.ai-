"""`propose_sql_step`: a query Data Doctor wrote, tried before it is shown (the owner's "own SQL step").

When no fixed step does the job (join two files, add up bills per customer), the rules or the AI may write one
read-only DuckDB query over the case's files. This tool dry-runs it in the sandbox of `engine.agent.sql_step` -
one SELECT, only the listed tables, no files, no clock, no randomness, a time limit, the files never changed - on
the first rows of each file, and returns a pending, never-ticked proposal: a `sql_query` recipe step, a plain
description first, the rows read from each file and the rows and columns the new table would have. The query
itself is carried as detail only. It is approved with the rest of the plan; nothing is applied here, and next
month's files go through the same step (the prescription replays it).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Final, Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from api.access_policy import RoutePolicy
from engine.access.roles import Role
from engine.agent.contracts import RecipeStep, RecipeStepKind
from engine.agent.sql_step import MAX_INPUTS, MAX_SQL_CHARS, SqlStepError, run_sql, sql_params
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


def _propose(given: ProposeSqlStepArgs, ctx: DataToolContext) -> SqlStepProposal:
    named = _names(ctx, given.upload_ids)
    params: dict[str, Any] = {
        "sql": given.sql,
        "inputs": list(named),
        "output_name": given.output_name,
        "description": given.description,
    }
    if given.as_of is not None:
        params["as_of"] = given.as_of.isoformat()
    try:
        sql_params(params)
        tried = {name: frame.head(DRY_RUN_ROWS) for name, (_, frame, _) in named.items()}
        run = run_sql(given.sql, tried, output_name=given.output_name, as_of=given.as_of)
    except SqlStepError as exc:
        raise DataToolError(exc.code, exc.message, status=422) from exc
    first = next(iter(tried.values()))
    key = ctx.primary_key
    receipt = make_receipt(1, RecipeStepKind.SQL_QUERY.value, tried, run.frame, key=key)
    findings = receipt_findings(receipt) + plan_findings(build_ledger([receipt]))
    changed_cells: int | None
    compared_cells: int | None
    try:
        changed_cells, compared_cells = cell_change(first, run.frame, key=key or infer_key(first, run.frame))
    except NotMeasurableError as exc:
        changed_cells = compared_cells = None
        findings.append(Finding(level="flag", code="STEP_NOT_MEASURED", message=f"{exc} It needs your yes."))
    inputs = tuple(
        SqlTableSummary(name=name, file_name=file_name, rows=len(frame), columns=len(frame.columns))
        for name, (_, frame, file_name) in named.items()
    )
    sampled = any(len(frame) > DRY_RUN_ROWS for _, frame, _ in named.values())
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
                "per customer, the latest row). It is tried on the first rows and shown as a plain description "
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
