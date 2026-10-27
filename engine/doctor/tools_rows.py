"""M238's tools as registry entries (Plan O, DEC-1808 (h)): `synonym_clusters`, `impossible_values`,
`detect_units`, `keep_latest_per_id` and `split_file`.

Standalone mode: M232's registry (`engine/doctor/registry.py`) is built on another branch, so each tool is a
module-level `DataToolSpec` shaped like the design's §5.2 entry - name, kind (READ / PROPOSE / ASK), typed input
and output models that refuse unknown fields, exposure, the role a caller needs, the engine functions it wraps,
whether it is deterministic - and `register(registry)` hands them to any registrar with a `register(spec)`
method. The integrator adapts a `DataToolSpec` to M232's entry; nothing here imports M232.

Every tool here is `session-only` (decision O7: it returns masked cells or spellings and is never listed on MCP).
None changes data: a READ tool returns evidence and pending proposals; a PROPOSE tool returns one pending item
with the measured preview. APPLY is never a tool. All five run with no AI service; `synonym_clusters` also takes
an AI's groups, which the rules verify and hold to decision O10 before they become a pending proposal.

The run functions read a `RowToolContext`: the frame, the ID and outcome columns, the failure limit and today's
date - the smallest interface this milestone needs from M232's `DataToolContext`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Final, Generic, Literal, Protocol, TypeVar

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from engine.access.roles import Role
from engine.agent.contracts import ProposalSource, Question, QuestionOption, RecipeStepKind, StepReceipt
from engine.agent.latest import (
    KeepLatestError,
    LatestConflicts,
    find_conflicts,
    keep_latest_per_id,
    latest_note,
    plan_keep_latest,
)
from engine.agent.reshape import _order
from engine.doctor.change_share import CHANGE_SHARE_CODES
from engine.doctor.impossible import IMPOSSIBLE_CODES, ImpossibleReport, find_impossible_values
from engine.doctor.limits import limits
from engine.doctor.reshape_guard import RESHAPE_GUARD_CODES, reshape_question
from engine.doctor.row_contracts import StepProposal
from engine.doctor.split import SPLIT_CODES, SplitError, SplitReceipt, SplitRule, split_rows
from engine.doctor.synonyms import (
    SYNONYM_CODES,
    AiMergeRefusedError,
    SynonymReport,
    check_ai_merge,
    find_synonym_clusters,
)
from engine.doctor.units import UNIT_CODES, UnitReport, detect_units

__all__ = [
    "ROW_TOOLS",
    "ROW_TOOL_CODES",
    "DataToolRegistrar",
    "DataToolSpec",
    "RowToolContext",
    "RowToolError",
    "RowToolFrame",
    "register",
]

ToolKind = Literal["read", "propose", "ask"]
Exposure = Literal["session-only", "aggregate"]

TOOL_CODES: Final[frozenset[str]] = frozenset({"DOCTOR_COLUMN_PROTECTED", "DOCTOR_COLUMN_UNKNOWN"})
ROW_TOOL_CODES: Final[frozenset[str]] = (
    TOOL_CODES
    | CHANGE_SHARE_CODES
    | IMPOSSIBLE_CODES
    | RESHAPE_GUARD_CODES
    | SPLIT_CODES
    | SYNONYM_CODES
    | UNIT_CODES
)
"""Every code M238 adds (for `PLAN_O_CODES` and `configs/pilot/help.yaml` at integration)."""


class RowToolError(Exception):
    """A call a tool refuses; `code` is one of `ROW_TOOL_CODES` or a recipe code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class RowToolContext(Protocol):
    """What M238's tools read from M232's `DataToolContext`."""

    @property
    def frame(self) -> pd.DataFrame: ...
    @property
    def primary_key(self) -> str | None: ...
    @property
    def target(self) -> str | None: ...
    @property
    def max_failure_pct(self) -> float: ...
    def today(self) -> date: ...


@dataclass(frozen=True)
class RowToolFrame:
    """A plain `RowToolContext`: for tests, the eval harness and the integrator's adapter."""

    frame: pd.DataFrame
    primary_key: str | None
    target: str | None
    max_failure_pct: float = 5.0
    as_of: date = field(default_factory=date.today)

    def today(self) -> date:
        return self.as_of


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _Output(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


In = TypeVar("In", bound=BaseModel)
Out = TypeVar("Out", bound=BaseModel)


@dataclass(frozen=True)
class DataToolSpec(Generic[In, Out]):
    """One Data Doctor tool, as the design's §5.2 registry entry needs it."""

    name: str
    kind: ToolKind
    mode: Literal["rules", "rules+ai"]
    description: str
    input_model: type[In]
    output_model: type[Out]
    exposure: Exposure
    role: Role
    """The least role a caller needs (design §5.2: a Viewer calls READ tools only)."""
    wraps: tuple[str, ...]
    """The engine functions it calls, as `module:function`."""
    deterministic: bool
    """True when the same input on the same file always gives the same output (no AI inside)."""
    step_kinds: tuple[RecipeStepKind, ...]
    """The step kinds its proposals carry: what M233's policy rule set for it must allow."""
    approve_class: bool | Literal["by_output"]
    """Whether the item it proposes needs the person's explicit yes with the exact text (design §6.6);
    `by_output` when that depends on the file, and each output's own `approve_class` field says."""
    run: Callable[[In, RowToolContext], Out]
    milestone: str = "M238"


class DataToolRegistrar(Protocol):
    """Anything that takes tool specs: M232's registry, through the integrator's adapter."""

    def register(self, spec: DataToolSpec[Any, Any]) -> None: ...


def _column(ctx: RowToolContext, name: str) -> pd.Series[Any]:
    if name in {ctx.primary_key, ctx.target} - {None}:
        raise RowToolError(
            "DOCTOR_COLUMN_PROTECTED",
            f"'{name}' is the ID or the outcome column, which no step ever changes.",
        )
    if name not in ctx.frame.columns:
        raise RowToolError("DOCTOR_COLUMN_UNKNOWN", f"The file has no column '{name}'.")
    return ctx.frame[name]


# ---------------------------------------------------------------------------
# synonym_clusters (#51, READ, rules + AI)
# ---------------------------------------------------------------------------
class SynonymClustersInput(_Input):
    column: str = Field(description="The category column to look at.")
    ai_groups: tuple[tuple[str, ...], ...] = Field(
        default=(), description="AI only: groups of spellings it thinks are one value; the rules verify them."
    )
    ai_keep: tuple[str, ...] = Field(default=(), description="AI only: the spelling to keep, one per group.")


class SynonymClustersOutput(_Output):
    report: SynonymReport
    ai_step: StepProposal | None = Field(default=None, description="The AI's merge after the rules' check.")
    ai_refused: str | None = Field(
        default=None, description="Why the rules refused the AI's merge, when they did."
    )


def _synonym_clusters(given: SynonymClustersInput, ctx: RowToolContext) -> SynonymClustersOutput:
    series = _column(ctx, given.column)
    report = find_synonym_clusters(series, column=given.column)
    if not given.ai_groups:
        return SynonymClustersOutput(report=report)
    try:
        step = check_ai_merge(series, column=given.column, groups=given.ai_groups, canonicals=given.ai_keep)
    except AiMergeRefusedError as exc:
        return SynonymClustersOutput(report=report, ai_refused=f"{exc.code}: {exc.message}")
    return SynonymClustersOutput(report=report, ai_step=step)


# ---------------------------------------------------------------------------
# impossible_values (#52, READ, rules)
# ---------------------------------------------------------------------------
class ImpossibleValuesInput(_Input):
    columns: tuple[str, ...] = Field(default=(), description="Columns to check; empty checks every column.")
    as_of: date | None = Field(
        default=None, description="The reference date for 'in the future'; null is today."
    )


def _impossible_values(given: ImpossibleValuesInput, ctx: RowToolContext) -> ImpossibleReport:
    for name in given.columns:
        _column(ctx, name)
    return find_impossible_values(
        ctx.frame,
        as_of=given.as_of or ctx.today(),
        primary_key=ctx.primary_key,
        target=ctx.target,
        columns=given.columns or None,
    )


# ---------------------------------------------------------------------------
# detect_units (#53, READ, rules)
# ---------------------------------------------------------------------------
class DetectUnitsInput(_Input):
    column: str = Field(description="The column to look at.")


def _detect_units(given: DetectUnitsInput, ctx: RowToolContext) -> UnitReport:
    return detect_units(_column(ctx, given.column), column=given.column, max_failure_pct=ctx.max_failure_pct)


# ---------------------------------------------------------------------------
# keep_latest_per_id (#54, PROPOSE, rules; reshape level, blocking question, never ticked)
# ---------------------------------------------------------------------------
class KeepLatestInput(_Input):
    time_column: str = Field(description="The date that orders each ID's rows.")
    snapshot_column: str | None = Field(
        default=None, description="The date each row is described as of; null: each ID's latest date."
    )


class ConflictCounts(_Output):
    ids: int
    rows: int
    ids_with_identical_copies: int
    after_snapshot: int
    failed: int
    ids_lost: int = Field(
        default=0, ge=0, description="IDs whose every row is dated after their snapshot: left out entirely."
    )


class KeepLatestOutput(_Output):
    step: StepProposal = Field(
        description="The pending step, never ticked; `ties` is `refuse` until answered."
    )
    rows_in: int = Field(ge=0, description="Rows of the file.")
    rows_out: int = Field(ge=0, description="Rows the step would leave: one per ID.")
    counts: ConflictCounts = Field(
        description="Conflicts, identical copies, rows after the snapshot, failures."
    )
    question: Question = Field(description="The blocking question the step is offered through (decision O3).")
    conflict_question: Question | None = Field(
        default=None, description="When IDs have different rows on their latest date: which one to keep."
    )
    sentence: str = Field(description="The exact text the person approves when the item is APPROVE-class.")
    approve_class: bool = Field(
        description="True when the step drops more than 2% of the rows (design §6.6): the Analyst approves it "
        "with the exact text of `sentence`."
    )
    role: Role = Field(description="The least role that may approve it.")


def _conflict_question(conflicts: LatestConflicts, key: str) -> Question:
    return Question(
        question_id="keep-latest-conflicts",
        text=(
            f"{conflicts.ids:,} IDs in '{key}' have two or more different rows on their latest date "
            f"({conflicts.rows:,} rows). Which one should be kept?"
        ),
        options=(
            QuestionOption(
                option_id="last-in-file", label="The later one in the file", effect="Sets ties: last_in_file."
            ),
            QuestionOption(
                option_id="first-in-file",
                label="The earlier one in the file",
                effect="Sets ties: first_in_file.",
            ),
            QuestionOption(
                option_id="dont-know",
                label="I don't know",
                effect="Keeps ties: refuse; the step stops on these IDs until they are fixed at source.",
            ),
        ),
        blocking=True,
    )


def _keep_latest(given: KeepLatestInput, ctx: RowToolContext) -> KeepLatestOutput:
    key = ctx.primary_key
    if key is None or key not in ctx.frame.columns:
        raise RowToolError("DOCTOR_COLUMN_UNKNOWN", "The file has no ID column to keep one row per.")
    for name in (given.time_column, given.snapshot_column):
        if name is not None:
            _column(ctx, name)
    params = plan_keep_latest(ctx.frame, time_column=given.time_column, snapshot_column=given.snapshot_column)
    try:
        conflicts = find_conflicts(ctx.frame, key=key, params=params)
        preview = keep_latest_per_id(
            ctx.frame, key=key, params={**params, "ties": "first_in_file"}, max_failure_pct=100.0
        )
    except KeepLatestError as exc:
        raise RowToolError(exc.code, exc.message) from exc
    removed = preview.rows_in - preview.rows_out
    note = latest_note(
        rows_in=preview.rows_in,
        rows_out=preview.rows_out,
        failed=preview.failed,
        ids_lost=preview.ids_lost,
        identical_copies=preview.identical_copies,
        after_snapshot=preview.after_snapshot,
        conflicts=conflicts.ids,
    )
    step = StepProposal(
        kind=RecipeStepKind.KEEP_LATEST_PER_ID,
        column=key,
        params=params,
        source=ProposalSource.RULES,
        title=f"Keep only the latest row of each '{key}'",
        reason=(
            f"'{key}' repeats: {preview.rows_in:,} rows hold {preview.rows_out:,} IDs. Keeping each ID's latest row "
            f"on or before its snapshot leaves out {removed:,} rows."
        ),
        changed=removed,
        out_of=preview.rows_in,
        share_pct=0.0 if not preview.rows_in else round(removed / preview.rows_in * 100.0, 4),
    )
    receipt = StepReceipt(
        order=1,
        kind=RecipeStepKind.KEEP_LATEST_PER_ID,
        column=key,
        rows=preview.rows_in,
        changed=removed,
        failed=preview.failed,
        note=note,
    )
    approve = bool(preview.rows_in) and removed / preview.rows_in > limits().gates.approve_drop_share
    return KeepLatestOutput(
        step=step,
        rows_in=preview.rows_in,
        rows_out=preview.rows_out,
        counts=ConflictCounts(
            ids=conflicts.ids,
            rows=conflicts.rows,
            ids_with_identical_copies=conflicts.ids_with_identical_copies,
            after_snapshot=conflicts.after_snapshot,
            failed=conflicts.failed,
            ids_lost=conflicts.ids_lost,
        ),
        question=reshape_question(step.as_step(1), receipt=receipt, rows_out=preview.rows_out),
        conflict_question=_conflict_question(conflicts, key) if conflicts.ids else None,
        sentence=(
            f"Keep only the latest row of each '{key}' on or before its snapshot, leaving out {removed:,} of "
            f"{preview.rows_in:,} rows."
        ),
        approve_class=approve,
        role=Role.ANALYST,
    )


# ---------------------------------------------------------------------------
# split_file (#59, PROPOSE (APPROVE), rules)
# ---------------------------------------------------------------------------
class SplitFileInput(_Input):
    rule: SplitRule = Field(description="The frozen rule.")


class SplitFileOutput(_Output):
    rule: SplitRule = Field(
        description="The rule as frozen: a text date column's day/month order is set when its values prove it."
    )
    receipt: SplitReceipt = Field(
        description="The split measured on this file (the recipe runs first at Approve)."
    )
    sentence: str = Field(description="The exact text the person approves.")
    approve_class: Literal[True] = Field(default=True, description="Needs the person's explicit yes.")
    role: Literal["analyst"] = Field(default="analyst", description="The role that approves it.")


def _split_file(given: SplitFileInput, ctx: RowToolContext) -> SplitFileOutput:
    series = _column(ctx, given.rule.column)
    rule = given.rule
    if rule.by == "date" and rule.dayfirst is None:
        # Frozen as `plan_keep_latest` freezes it: a value like 25/03 proves the order, and the rule's hash and
        # the approved sentence then carry it, so Approve reads every date as the preview did.
        proven = _order(series)[0]
        if proven is not None:
            rule = SplitRule.model_validate({**rule.model_dump(), "dayfirst": proven})
    try:
        parts = split_rows(
            ctx.frame,
            rule,
            primary_key=ctx.primary_key,
            target=ctx.target,
            max_failure_pct=ctx.max_failure_pct,
        )
    except SplitError as exc:
        raise RowToolError(exc.code, exc.message) from exc
    return SplitFileOutput(rule=rule, receipt=parts.receipt, sentence=rule.sentence())


ROW_TOOLS: Final[tuple[DataToolSpec[Any, Any], ...]] = (
    DataToolSpec(
        name="synonym_clusters",
        kind="read",
        mode="rules+ai",
        description="Groups spellings of one value (case, punctuation, a lexicon, initials, one-letter slips) "
        "and proposes one pending map_values step; checks an AI's groups the same way.",
        input_model=SynonymClustersInput,
        output_model=SynonymClustersOutput,
        exposure="session-only",
        role=Role.VIEWER,
        wraps=("engine.doctor.synonyms:find_synonym_clusters", "engine.doctor.synonyms:check_ai_merge"),
        deterministic=True,
        step_kinds=(RecipeStepKind.MAP_VALUES,),
        approve_class=False,
        run=_synonym_clusters,
    ),
    DataToolSpec(
        name="impossible_values",
        kind="read",
        mode="rules",
        description="Finds values a column's meaning and type rule out (future dates, 2099, negative tenure or "
        "amount, age over 120) and proposes pending valid_range or set_missing_text steps.",
        input_model=ImpossibleValuesInput,
        output_model=ImpossibleReport,
        exposure="session-only",
        role=Role.VIEWER,
        wraps=("engine.doctor.impossible:find_impossible_values", "engine.agent.value_steps:valid_range"),
        deterministic=True,
        step_kinds=(RecipeStepKind.VALID_RANGE, RecipeStepKind.SET_MISSING_TEXT),
        approve_class=False,
        run=_impossible_values,
    ),
    DataToolSpec(
        name="detect_units",
        kind="read",
        mode="rules",
        description="Finds unit words in values (GB, MB, L, Cr, ...) and magnitude jumps; proposes a verified "
        "convert_units step, or asks which unit plain numbers are in.",
        input_model=DetectUnitsInput,
        output_model=UnitReport,
        exposure="session-only",
        role=Role.VIEWER,
        wraps=("engine.doctor.units:detect_units", "engine.agent.value_steps:convert_units"),
        deterministic=True,
        step_kinds=(RecipeStepKind.CONVERT_UNITS,),
        approve_class=False,
        run=_detect_units,
    ),
    DataToolSpec(
        name="keep_latest_per_id",
        kind="propose",
        mode="rules",
        description="Proposes keeping each ID's latest row on or before its snapshot (reshape level), through a "
        "blocking question; conflicting duplicates become one more question.",
        input_model=KeepLatestInput,
        output_model=KeepLatestOutput,
        exposure="session-only",
        role=Role.ANALYST,
        wraps=("engine.agent.latest:keep_latest_per_id", "engine.agent.latest:find_conflicts"),
        deterministic=True,
        step_kinds=(RecipeStepKind.KEEP_LATEST_PER_ID,),
        approve_class="by_output",
        run=_keep_latest,
    ),
    DataToolSpec(
        name="split_file",
        kind="propose",
        mode="rules",
        description="Proposes cutting one file into a file to learn from and a file to score by a date or by "
        "values; one recipe prepares both; an item the person approves with the exact text.",
        input_model=SplitFileInput,
        output_model=SplitFileOutput,
        exposure="session-only",
        role=Role.ANALYST,
        wraps=("engine.doctor.split:split_rows", "engine.doctor.split:split_file"),
        deterministic=True,
        step_kinds=(),
        approve_class=True,
        run=_split_file,
    ),
)


def register(registry: DataToolRegistrar) -> tuple[str, ...]:
    """Hand every M238 tool to `registry`; returns their names in order."""
    for spec in ROW_TOOLS:
        registry.register(spec)
    return tuple(spec.name for spec in ROW_TOOLS)
