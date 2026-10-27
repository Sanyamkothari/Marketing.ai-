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
steps before it. The trial is not a second implementation of that: it is :func:`engine.doctor.materialize
.materialize_plan` with the query as the `candidate` step, on a seeded sample of every file - the same steps, order,
table names, key, as-of date and guards the plan check and the run use - so a query that cannot run later fails
here, with the same code. The trial applies the accepted fixes and the per-column fixes still waiting for a yes,
counted as accepted (the plan as it stands if the person ticks what is proposed); a waiting query, combine or
reshape, and an item the chat is swapping, are left out (:func:`_trial_items`). The rules' own queries (the grain
checks) are tried the same way, on the plan so far.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, Final, Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from api.access_policy import RoutePolicy
from engine.access.roles import Role
from engine.agent.contracts import ProposalState, RecipeStep, RecipeStepKind
from engine.agent.recipe import STEP_PHASE
from engine.agent.sql_step import (
    MAX_INPUTS,
    MAX_SQL_CHARS,
    AsOfRule,
    SqlStepError,
    resolve_as_of_rule,
    sql_params,
)
from engine.config import StrictBase
from engine.doctor.guards import Finding, NotMeasurableError, Receipt, cell_change, infer_key
from engine.doctor.limits import limits
from engine.doctor.materialize import (
    CANDIDATE,
    Full,
    Prepared,
    Sample,
    materialize_plan,
    table_names,
    tried_step,
)
from engine.doctor.registry import (
    DataTool,
    DataToolContext,
    DataToolError,
    DataToolKind,
    DataToolRegistry,
    Exposure,
)

if TYPE_CHECKING:
    from engine.doctor.case import CaseItem
    from engine.doctor.diagnose import LoadedFile

__all__ = ["M_SQL_TOOLS", "ProposeSqlStepArgs", "SqlStepProposal", "register_sql_tools"]

MAX_OUTPUT_CHARS: Final[int] = 12_000
"""The answer's own cap through the registry (as `propose_features` has): the step, the column lists and the findings
of an ordinary customer table were 5,856 characters, more than the default 5,500. Names and counts only, no cells."""
M_SQL_TOOLS: Final[tuple[str, ...]] = ("propose_sql_step",)
# A query is tried on at most `data_doctor.limits.trial.dry_run_rows` rows of each file (a seeded random sample, drawn as the plan check draws its
# own); a bigger file is said to be sampled. It is larger than the plan check's sample on purpose: the checks the writer
# states about the table (`rows_out_equals`, the customers kept) are about the file, so the trial reads as much of it
# as it can. On the same rows the trial's receipt is the plan check's (`tests/unit/doctor/test_trial_is_run.py`).
_SHAPING: Final[frozenset[RecipeStepKind]] = frozenset(
    {
        RecipeStepKind.SQL_QUERY,
        RecipeStepKind.COMBINE_ROWS,
        RecipeStepKind.UNPIVOT,
        RecipeStepKind.KEEP_LATEST_PER_ID,
    }
)
"""The steps that make other rows or another table: a query's trial never applies one that is only proposed."""
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
        "column is let off the future-rows check. Then also set `outcome_window_days`.",
    )
    outcome_window_days: int | None = Field(
        default=None,
        ge=1,
        le=3650,
        description="With `reads_future`: how many days after `$as_of` the outcome looks. The `$as_of` of a "
        "training table must be at least that many days before the data ends.",
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


def _step_of(item: CaseItem) -> RecipeStep | None:
    raw = item.payload.get("step")
    if not isinstance(raw, dict):
        return None
    try:
        return RecipeStep.model_validate(raw)
    except ValueError:
        return None


def _trial_items(items: Sequence[CaseItem], replacing: Collection[str] = ()) -> list[CaseItem]:
    """The plan a new query is tried on: the accepted items, and the waiting per-column fixes (dates parsed, numbers
    read) counted as accepted. Never a waiting query, combine or reshape (a run puts the query last, on the file's
    rows), never an item named in `replacing` (the chat swaps or takes it back at the end of its turn), and never a
    waiting fix that reads what a left-out step makes (it would not run). Items that are no step pass through.
    """
    live = (ProposalState.ACCEPTED, ProposalState.PENDING)
    steps = {i.item_id: s for i in items if i.state in live and (s := _step_of(i)) is not None}
    left_out: dict[str | None, int] = {}
    for item in items:
        step = steps.get(item.item_id)
        if (
            step is not None
            and step.kind in _SHAPING
            and (item.item_id in replacing or item.state is not ProposalState.ACCEPTED)
        ):
            phase = STEP_PHASE[step.kind]
            left_out[item.upload_id] = min(phase, left_out.get(item.upload_id, phase))
    kept: list[CaseItem] = []
    for item in items:
        step = steps.get(item.item_id)
        if step is None:
            kept.append(item)
            continue
        cut = left_out.get(item.upload_id, left_out.get(None))
        if item.item_id in replacing or (
            item.state is not ProposalState.ACCEPTED
            and (step.kind in _SHAPING or (cut is not None and STEP_PHASE[step.kind] > cut))
        ):
            continue
        kept.append(
            item
            if item.state is ProposalState.ACCEPTED
            else item.model_copy(update={"state": ProposalState.ACCEPTED})
        )
    return kept


def _as_of_rule(
    given: ProposeSqlStepArgs, tables: dict[str, pd.DataFrame], names: dict[str, str]
) -> tuple[date, dict[str, Any]]:
    """The rule as the step keeps it, and the date it gives on these files (as the plan leaves them) now."""
    table = names.get(given.as_of_upload_id or "")
    if table is None or given.as_of_column is None or given.as_of_rule is None:
        raise SqlStepError("SQL_NOT_VALID", "An as-of date rule names the file and the date column it reads.")
    frame = tables[table]
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


_DATE_HINT: Final = ("date", "time", "day", "_at")


_AS_OF_SIDE: Final = (
    r"(?:\"?(\w+)\"?\s*(?:>=|<=|>|<|=|BETWEEN)\s*\$as_of\b|\$as_of\b(?:\s*\+[^,)]*?)?\s*(?:>=|<=|>|<|=)\s*\"?(\w+)\"?)"
)
_DAYS_AFTER: Final = re.compile(
    r"\$as_of\s*\+\s*(?:INTERVAL\s*'?\s*(\d+)\s*(?:DAYS?)?\s*'?\s*(?:DAYS?)?|(\d+)\b)", re.IGNORECASE
)


def _compared_columns(sql: str) -> set[str]:
    """Column names the query sets against `$as_of`: the event date it reads, when it says so."""
    found = {m for pair in re.findall(_AS_OF_SIDE, sql, flags=re.IGNORECASE) for m in pair if m}
    return {n.lower() for n in found}


def _window_in_sql(sql: str) -> int:
    """The longest literal day offset added to `$as_of` in the query (0 when it has none)."""
    return max((int(a or b) for a, b in _DAYS_AFTER.findall(sql)), default=0)


def _data_end(frames: dict[str, pd.DataFrame], prefer: Collection[str] = ()) -> date | None:
    """The latest date in the date columns of these frames (parsed ones, and text columns named like a date).

    The columns the case or the query names as its event date count alone when the files have them (a card expiry or
    renewal date lies in the future and says nothing of how far the data reaches); a column that cannot be read is
    skipped."""
    wanted = {str(n).lower() for n in prefer}
    named = any(str(c).lower() in wanted for f in frames.values() for c in f.columns)
    latest: date | None = None
    for frame in frames.values():
        for name in frame.columns:
            if named and str(name).lower() not in wanted:
                continue
            column = frame[name]
            try:
                if not pd.api.types.is_datetime64_any_dtype(column):
                    if column.dtype != object or not any(h in str(name).lower() for h in _DATE_HINT):
                        continue
                    column = pd.to_datetime(column, errors="coerce", format="mixed")
                top = column.max()
                day = top.date() if pd.notna(top) else None
            except (TypeError, ValueError, OverflowError, AttributeError):
                continue
            if day is not None and (latest is None or day > latest):
                latest = day
    return latest


def _window_finding(
    given: ProposeSqlStepArgs, as_of: date | None, frames: dict[str, pd.DataFrame]
) -> Finding | None:
    """A query reading the outcome from rows after `$as_of` needs the data to reach `as_of` + its window; when it
    does not, most customers read as no (the same maturity rule as the readiness check, applied to the step).
    """
    stated = given.outcome_window_days
    if not given.reads_future or stated is None or as_of is None:
        return None
    window = max(
        stated, _window_in_sql(given.sql)
    )  # the query's own literal offset is not for the AI to shrink
    prefer = _compared_columns(given.sql) | ({given.as_of_column} if given.as_of_column else set())
    end = _data_end(frames, prefer)
    if end is None or as_of + timedelta(days=window) <= end:
        return None
    return Finding(
        level="stop",
        code="OUTCOME_WINDOW_PAST_DATA",
        count=window,
        message=(
            f"The outcome looks {window} days past {as_of.isoformat()}, but the data ends on {end.isoformat()}: "
            f"most customers would read as no. Use an as-of date at least {window} days before the end "
            f"(for example {(end - timedelta(days=window)).isoformat()})."
        ),
    )


def _refused(prepared: Prepared, reads: Collection[str], files: Sequence[LoadedFile]) -> DataToolError | None:
    """Why the plan could not be run with the query: the query's own failure (its engine code, 422), or a fix already
    in the plan that cannot be applied to a file the query reads (409, naming the file). A failure in a file the
    query does not read is not the query's: the plan check reports it as the plan's own stop. None when the query
    can be tried. `reads` are the uploads the query reads."""
    own = next((f for f in prepared.failures if f.item_id == CANDIDATE), None)
    if own is not None:
        return DataToolError(own.code, own.message, status=422)
    other = next((f for f in prepared.failures if f.upload_id in reads), None)
    if other is None:
        return None
    name = (
        next((str(f.file_name or "").strip() for f in files if f.upload_id == other.upload_id), "")
        or "the file"
    )
    return DataToolError(
        "TOOL_INPUT_INVALID",
        f"The fixes already in the plan cannot be applied to {name} first: {other.message}",
        status=409,
    )


def _propose(
    given: ProposeSqlStepArgs,
    ctx: DataToolContext,
    *,
    extra: Mapping[str, Any] | None = None,
) -> SqlStepProposal:
    if given.reads_future and given.outcome_window_days is None:
        raise DataToolError(
            "SQL_NOT_VALID",
            "A query that reads the outcome after `$as_of` states how many days it looks: set outcome_window_days.",
            status=422,
        )
    loaded = {f.upload_id: f for f in ctx.files}
    if any(u not in loaded for u in given.upload_ids):
        raise DataToolError("TOOL_INPUT_INVALID", "That file is not in this case.", status=422)
    names = table_names(ctx.files)
    asked = {names[u]: loaded[u] for u in given.upload_ids}
    items = _trial_items(ctx.case_items or (), ctx.replacing)
    params: dict[str, Any] = {
        "sql": given.sql,
        "inputs": list(asked),
        "output_name": given.output_name,
        "description": given.description,
    }
    if extra:
        params.update(extra)  # what our own code knows about a query it compiled (`propose_features`)
    if len(ctx.files) == 1 and len(asked) == 1:
        # an older recipe runner (a replay's file recipe) is given no tables: the query names its one file
        params["this_table"] = next(iter(asked))

    def plan_so_far() -> Prepared:
        """Every row of every file as the plan leaves it, before the query (the as-of rule and the window read it)."""
        whole = materialize_plan(
            (), items, ctx.files, Full(), twice=False, as_of=ctx.as_of, training_as_of=ctx.training_as_of
        )
        if (refusal := _refused(whole, given.upload_ids, ctx.files)) is not None:
            raise refusal
        return whole

    as_of = given.as_of
    whole: Prepared | None = None
    try:
        if given.as_of_rule is not None:
            whole = plan_so_far()  # the rule reads the whole file, after the same fixes, as a run does
            as_of, rule = _as_of_rule(given, whole.tables, names)
            params["as_of_rule"] = rule
        if as_of is not None:
            params["as_of"] = as_of.isoformat()
        if given.reads_future:
            params["reads_future"] = True
            params["outcome_window_days"] = given.outcome_window_days
        sql_params(params)
    except SqlStepError as exc:
        raise DataToolError(exc.code, exc.message, status=422) from exc
    step = RecipeStep(
        order=1,
        kind=RecipeStepKind.SQL_QUERY,
        column=given.output_name,
        params=params,
        reason=given.description,
    )
    # The trial is the plan's own materialisation with the query as one more step. It is given the date the rule
    # gives on the whole file, so a sample cannot move it.
    tried = tried_step(step, ctx.as_of, ctx.training_as_of)
    first_upload = given.upload_ids[0]
    seen: dict[str, pd.DataFrame] = {}

    def watch(
        _upload: str, item_id: str, _step: RecipeStep, was: pd.DataFrame, _became: pd.DataFrame
    ) -> None:
        if item_id == CANDIDATE:
            seen["before"] = was  # the rows the query started from: the first table, after the plan's fixes

    prepared = materialize_plan(
        (),
        items,
        ctx.files,
        Sample(limits().trial.dry_run_rows),
        candidate=(first_upload, tried),
        as_of=ctx.as_of,
        training_as_of=ctx.training_as_of,
        watch=watch,
    )
    if (refusal := _refused(prepared, given.upload_ids, ctx.files)) is not None:
        raise refusal
    table = names[first_upload]
    made = prepared.tables[table]
    done = next(f for f in prepared.files if f.upload_id == first_upload)
    receipt = next(r for item_id, r in done.receipts if item_id == CANDIDATE)
    findings = [
        Finding(level=f.level, code=f.code, message=f.text, count=f.count, detail=f.detail)
        for f in done.findings
        if f.item_id in (CANDIDATE, None)
    ]
    if given.reads_future:
        whole = whole or plan_so_far()
        if late := _window_finding(
            given, as_of or ctx.training_as_of or ctx.as_of, {n: whole.tables[n] for n in asked}
        ):
            findings.append(late)
    first = seen["before"]
    key = ctx.primary_key
    changed_cells: int | None
    compared_cells: int | None
    try:
        changed_cells, compared_cells = cell_change(first, made, key=key or infer_key(first, made))
    except NotMeasurableError as exc:
        changed_cells = compared_cells = None
        findings.append(Finding(level="flag", code="STEP_NOT_MEASURED", message=f"{exc} It needs your yes."))
    inputs = tuple(
        SqlTableSummary(
            name=name,
            file_name=str(file.file_name or "").strip() or file.upload_id,
            rows=len(file.frame),
            columns=len((first if file.upload_id == first_upload else prepared.tables[name]).columns),
        )
        for name, file in asked.items()
    )
    sampled = any(len(file.frame) > limits().trial.dry_run_rows for file in asked.values())
    read = ", ".join(f"{t.file_name} ({t.rows:,} rows)" for t in inputs)
    columns = tuple(str(c) for c in made.columns)
    sentence = (
        f"{given.description.rstrip('.')}. Reads {read}; the new table '{given.output_name}' would have "
        f"{receipt.rows_out:,} rows and {len(columns):,} columns"
        + (f" (tried on {limits().trial.dry_run_rows:,} sampled rows of each file)." if sampled else ".")
    )
    if receipt.keys_in is not None:
        sentence += f" Customers: {receipt.keys_in:,} before, {receipt.keys_out:,} after ({receipt.keys_lost:,} lost, {receipt.keys_repeated:,} repeated)."
    own = step.params.get("as_of")
    decided = ctx.training_as_of or ctx.as_of
    if own and decided is not None and own != decided.isoformat() and "as_of_rule" not in step.params:
        sentence += (
            f" It reads the data as of {own}, not {decided.isoformat()}."  # shown, never silently changed
        )
    return SqlStepProposal(
        source="ai" if ctx.caller == "ai" else "rules",
        title=given.description.rstrip("."),
        sentence=sentence,
        step=step.model_dump(mode="json", exclude={"proposal_id"}),
        inputs=inputs,
        rows_in=len(first),
        rows_out=receipt.rows_out,
        columns_out=len(columns),
        columns=columns[:MAX_COLUMNS_LISTED],
        all_columns=columns[:MAX_COLUMNS_KEPT],
        two_valued=_two_valued(made)[:MAX_COLUMNS_KEPT],
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
            max_output_bytes=MAX_OUTPUT_CHARS,
            policy_kind="data.propose_sql_step",
            wraps="engine/agent/sql_step.py run_sql (DuckDB, sandboxed); the sql_query recipe step",
        )
    )
    return M_SQL_TOOLS
