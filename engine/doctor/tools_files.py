"""The several-files tools (Plan O M237, DEC-1807): their specs, and `register(registry)` for M232's registry.

Four PROPOSE tools, all rules, all `session-only` (Plan O decision O7: no Data Doctor tool is listed on MCP),
all for an Analyst (a Viewer may call READ tools only, design §5.2). Each wraps an engine function of this
milestone and returns a pending, `check`, never-ticked proposal whose output holds file names, column names,
rules and counts - never an ID or another cell (the multi-file privacy rule, Plan O §8):

| Tool | Wraps | Proposes |
|---|---|---|
| `join_files` | `engine.doctor.join_files.plan_join` | a `JoinSpec`: per-customer files and outcome lists onto a customer list |
| `stack_files` | `engine.doctor.stack_files.plan_stack` | a `StackSpec`: exports stacked, repeated rows counted once; one question for conflicts |
| `normalise_key` | `engine.agent.key_steps.propose_key_rules` | a `normalise_key` recipe step on one file's ID column |
| `build_dataset` | `engine.doctor.build_driver.plan_build` | a `BuildPlan` for the goal's table, built once approved (`combine_run`, never a tool) |

M232's registry (`engine/doctor/registry.py`) is not on this branch, so the specs are plain declarations
(`DataToolSpec`) shaped like the design's §5.2 entry, and `register(registry)` hands each one to any object
with a `register` method. The integrator maps `DataToolSpec` onto M232's `DataTool`: `name`, `kind`,
`input_model`, `output_model`, `exposure`, `role` (its `RoutePolicy`), `run(input, ctx)`; `policy_kind` names
the M233 rule set each needs (listed in the milestone's cross-branch notes).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol

import pandas as pd
from pydantic import BaseModel, Field

from engine.access.roles import Role
from engine.agent.key_steps import KeyRule, propose_key_rules
from engine.config import LabelDefinition, SnapshotDefinition, StrictBase
from engine.doctor.build_driver import BuildPlan, plan_build
from engine.doctor.contracts import ColumnDrift, Diagnosis, FileStack
from engine.doctor.join_files import MAX_JOIN_PARTS, JoinError, JoinInput, JoinProposal, PartKind, plan_join
from engine.doctor.stack_files import ConflictRule, StackProposal, plan_stack

__all__ = [
    "TOOLS",
    "BuildDatasetInput",
    "DataToolSpec",
    "FilesContext",
    "JoinFilesInput",
    "NormaliseKeyInput",
    "NormaliseKeyProposal",
    "StackFilesInput",
    "register",
]

ToolKindName = Literal["read", "propose", "ask"]
Exposure = Literal["session-only", "aggregate"]


class FilesContext(Protocol):
    """What the tools read: the case's files (frames and names) and its diagnosis. M232's `DataToolContext`
    provides it (the frames are the rows the diagnosis read; never written)."""

    def frame(self, upload_id: str) -> pd.DataFrame: ...

    def file_name(self, upload_id: str) -> str: ...

    def diagnosis(self) -> Diagnosis | None: ...


@dataclass(frozen=True)
class DataToolSpec:
    """One tool, as the design's §5.2 registry entry describes it."""

    name: str
    kind: ToolKindName
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    exposure: Exposure
    role: Role
    mode: Literal["rules", "ai", "rules+ai"]
    wraps: str
    policy_kind: str
    """The M233 rule set the policy needs for this PROPOSE kind (default deny without it)."""
    run: Callable[[Any, FilesContext], BaseModel]


# ---------------------------------------------------------------------------
# Inputs (extra=forbid via StrictBase) and the one output model that is new here
# ---------------------------------------------------------------------------
_UPLOAD = Field(min_length=1, max_length=64)


class JoinFilesInput(StrictBase):
    base_upload_id: str = _UPLOAD
    base_key_column: str = Field(min_length=1, max_length=256)
    upload_ids: tuple[str, ...] = Field(min_length=1, max_length=MAX_JOIN_PARTS)
    kinds: dict[str, PartKind] = Field(
        default_factory=dict, description="Optional: what a file is, by upload id; otherwise read from it."
    )
    outcome_name: str | None = Field(default=None, max_length=64, description="The outcome column's name.")


class StackFilesInput(StrictBase):
    upload_ids: tuple[str, ...] = Field(min_length=2, max_length=24)
    key_column: str | None = Field(default=None, max_length=256)
    date_column: str | None = Field(default=None, max_length=256)
    on_conflict: ConflictRule | None = None


class NormaliseKeyInput(StrictBase):
    upload_id: str = _UPLOAD
    column: str = Field(min_length=1, max_length=256)
    other_upload_id: str = _UPLOAD
    other_column: str = Field(min_length=1, max_length=256)


class BuildDatasetInput(StrictBase):
    use_case: str = Field(min_length=1, max_length=64)
    upload_ids: tuple[str, ...] = Field(min_length=2, max_length=24)
    label_spec: LabelDefinition | None = None
    snapshot_spec: SnapshotDefinition | None = None
    stack_answers: dict[str, ConflictRule] = Field(default_factory=dict)


class NormaliseKeyProposal(StrictBase):
    """`normalise_key`'s answer: the step for each side that needs one, pending, `check`, never ticked."""

    state: Literal["pending"] = "pending"
    confidence: Literal["check"] = "check"
    never_ticked: Literal[True] = True
    upload_id: str
    column: str
    other_upload_id: str
    other_column: str
    rule: KeyRule = Field(description="The step on `column` (identity: no step).")
    other_rule: KeyRule = Field(description="The step on `other_column` (identity: no step).")
    steps: tuple[dict[str, Any], ...] = Field(
        description="The recipe steps proposed: `{upload_id, kind, column, params}`; empty when none is needed."
    )
    coverage: float = Field(description="Share of `column`'s IDs found in `other_column` as written.")
    fixed_coverage: float = Field(description="The same share with the steps applied.")
    meets: bool = Field(description="True when the steps make the files meet (80%); never a near match.")
    summary: str


# ---------------------------------------------------------------------------
# The runs
# ---------------------------------------------------------------------------
def _join(given: JoinFilesInput, ctx: FilesContext) -> JoinProposal:
    base = JoinInput(
        given.base_upload_id, ctx.file_name(given.base_upload_id), ctx.frame(given.base_upload_id)
    )
    others = [
        JoinInput(upload_id, ctx.file_name(upload_id), ctx.frame(upload_id), kind=given.kinds.get(upload_id))
        for upload_id in given.upload_ids
        if upload_id != given.base_upload_id
    ]
    return plan_join(base, others, base_key_column=given.base_key_column, outcome_name=given.outcome_name)


def _stack_of(upload_ids: Sequence[str], ctx: FilesContext) -> FileStack:
    """The diagnosis's stack for exactly these files (with its renames), else one read by name only."""
    diagnosis = ctx.diagnosis()
    wanted = set(upload_ids)
    if diagnosis is not None:
        for stack in diagnosis.stacks:
            if set(stack.upload_ids) == wanted:
                return stack
    frames = {upload_id: ctx.frame(upload_id) for upload_id in upload_ids}
    first = [str(column) for column in frames[upload_ids[0]].columns]
    drift: list[ColumnDrift] = []
    for upload_id in upload_ids[1:]:
        names = [str(column) for column in frames[upload_id].columns]
        added = tuple(name for name in names if name not in first)
        missing = tuple(name for name in first if name not in names)
        if added or missing:
            drift.append(
                ColumnDrift(
                    upload_id=upload_id, file_name=ctx.file_name(upload_id), added=added, missing=missing
                )
            )
    return FileStack(
        upload_ids=tuple(upload_ids),
        file_names=tuple(ctx.file_name(upload_id) for upload_id in upload_ids),
        kind=None,
        columns=tuple(name for name in first if all(name in frames[u].columns for u in upload_ids)),
        rows=sum(len(frame) for frame in frames.values()),
        drift=tuple(drift),
        summary="",
    )


def _stack(given: StackFilesInput, ctx: FilesContext) -> StackProposal:
    stack = _stack_of(given.upload_ids, ctx)
    frames = {upload_id: ctx.frame(upload_id) for upload_id in stack.upload_ids}
    return plan_stack(
        stack,
        frames,
        key_column=given.key_column,
        date_column=given.date_column,
        on_conflict=given.on_conflict,
    )


def _column(frame: pd.DataFrame, name: str, *, file_name: str, upload_id: str) -> pd.Series[Any]:
    names = {str(column): column for column in frame.columns}
    if name not in names:
        raise JoinError(
            "JOIN_COLUMN_MISSING",
            f"{file_name} has no column '{name}', which the tool reads.",
            column=name,
            upload_id=upload_id,
        )
    return frame[names[name]]


def _normalise_key(given: NormaliseKeyInput, ctx: FilesContext) -> NormaliseKeyProposal:
    name, other = ctx.file_name(given.upload_id), ctx.file_name(given.other_upload_id)
    keys = _column(ctx.frame(given.upload_id), given.column, file_name=name, upload_id=given.upload_id)
    base = _column(
        ctx.frame(given.other_upload_id), given.other_column, file_name=other, upload_id=given.other_upload_id
    )
    measured = propose_key_rules(keys, base)
    steps: list[dict[str, Any]] = []
    for upload_id, column, rule in (
        (given.upload_id, given.column, measured.rule),
        (given.other_upload_id, given.other_column, measured.base_rule),
    ):
        if not rule.changes_nothing:
            steps.append(
                {"upload_id": upload_id, "kind": "normalise_key", "column": column, "params": rule.params()}
            )
    if not measured.needed:
        summary = (
            f"{measured.coverage:.0%} of the IDs in {name} are in {other} as written; nothing to change."
        )
    elif measured.meets:
        summary = (
            f"As written, {measured.coverage:.0%} of the IDs in {name} are in {other}. Written one way "
            f"({measured.rule.words()}; in {other}: {measured.base_rule.words()}), {measured.fixed_coverage:.0%} are."
        )
    else:
        summary = (
            f"Only {measured.fixed_coverage:.0%} of the IDs in {name} are in {other}, even tidied. Nothing is joined "
            "on a near match."
        )
    return NormaliseKeyProposal(
        upload_id=given.upload_id,
        column=given.column,
        other_upload_id=given.other_upload_id,
        other_column=given.other_column,
        rule=measured.rule,
        other_rule=measured.base_rule,
        steps=tuple(steps) if measured.meets else (),
        coverage=measured.coverage,
        fixed_coverage=measured.fixed_coverage,
        meets=measured.meets,
        summary=summary,
    )


def _build(given: BuildDatasetInput, ctx: FilesContext) -> BuildPlan:
    diagnosis = ctx.diagnosis()
    if diagnosis is None:
        from engine.doctor.build_driver import BuildDriveError

        raise BuildDriveError(
            "BUILD_PLAN_INCOMPLETE", "Diagnose the files first: the plan reads what each file is."
        )
    frames: Mapping[str, pd.DataFrame] = {upload_id: ctx.frame(upload_id) for upload_id in given.upload_ids}
    # Plan O M236 (DEC-1806): each file's role is `propose_role`'s when the context offers it (the registry's
    # tool, decided by its policy); otherwise the diagnosis's, as M237 built it.
    role_proposer = getattr(ctx, "role_proposer", None)
    return plan_build(
        diagnosis,
        frames,
        use_case=given.use_case,
        label_spec=given.label_spec,
        snapshot_spec=given.snapshot_spec,
        roles=role_proposer() if callable(role_proposer) else None,
        stack_answers=given.stack_answers,
    )


TOOLS: Final[tuple[DataToolSpec, ...]] = (
    DataToolSpec(
        name="join_files",
        kind="propose",
        description="Join per-customer files and outcome lists onto a customer list, by exact IDs, counted.",
        input_model=JoinFilesInput,
        output_model=JoinProposal,
        exposure="session-only",
        role=Role.ANALYST,
        mode="rules",
        wraps="engine.doctor.join_files.plan_join (onboarding join_coverage's bar; run_join replays)",
        policy_kind="data.join_files",
        run=_join,
    ),
    DataToolSpec(
        name="stack_files",
        kind="propose",
        description="Stack exports of one report, counting each repeated row once; conflicts become one question.",
        input_model=StackFilesInput,
        output_model=StackProposal,
        exposure="session-only",
        role=Role.ANALYST,
        mode="rules",
        wraps=(
            "engine.doctor.stack_files.plan_stack (joins.file_stacks' drift; `prepare_sources` stacks before the "
            "builder sees one source; build.py's concat is unchanged)"
        ),
        policy_kind="data.stack_files",
        run=_stack,
    ),
    DataToolSpec(
        name="normalise_key",
        kind="propose",
        description="Propose writing one file's IDs one way (prefix, zeros, case, spaces, +91 phone) so files meet.",
        input_model=NormaliseKeyInput,
        output_model=NormaliseKeyProposal,
        exposure="session-only",
        role=Role.ANALYST,
        mode="rules",
        wraps="engine.agent.key_steps.propose_key_rules (the normalise_key recipe step)",
        policy_kind="data.normalise_key",
        run=_normalise_key,
    ),
    DataToolSpec(
        name="build_dataset",
        kind="propose",
        description="Propose how the files become one built dataset: sources, stacks, IDs, mappings, outcome.",
        input_model=BuildDatasetInput,
        output_model=BuildPlan,
        exposure="session-only",
        role=Role.ANALYST,
        mode="rules",
        wraps="engine.doctor.build_driver.plan_build (the table is built by `combine_run`, never a tool)",
        policy_kind="data.build_dataset",
        run=_build,
    ),
)


class SupportsRegister(Protocol):
    def register(self, tool: Any) -> None: ...


def register(registry: SupportsRegister) -> tuple[str, ...]:
    """Hand every M237 tool to `registry` (M232's, once integrated); return their names, in order."""
    for spec in TOOLS:
        registry.register(spec)
    return tuple(spec.name for spec in TOOLS)
