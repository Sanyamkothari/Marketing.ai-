"""Editing by hand (Plan L M154, DEC-1540): the person's own fixes, as steps like every other.

Nothing here writes to a file. An edit in the table view becomes a `RecipeStep`, checked by the same
`check_recipe` and tried on the same sample as the chat's suggestions, then added to the session as an
**accepted** suggestion labelled as the person's (`ProposalSource.PERSON`): the box starts ticked, and
unticking it undoes it. Because it is a step, it appears in the preview, the receipt, the audit log and
next month's replay. Two edits of one kind on one column are kept as one step (`correct_cells`' cells,
`map_values`' spellings), so a person correcting fifty cells adds one step, not fifty.

`table_rows` is what the table view reads: rows in file order, personal-data columns shown as
`[personal data]`, every other cell masked and cut as the preview cuts it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, Final

from engine.agent.contracts import (
    AgentConfidence,
    AgentSession,
    DecidedBy,
    Proposal,
    ProposalKind,
    ProposalSource,
    ProposalState,
    RecipeStep,
    RecipeStepKind,
    ToolResult,
)
from engine.agent.session import SessionError, roles_of, with_summary
from engine.agent.tools import AgentContext, AgentToolError
from engine.agent.untrusted import display_name
from engine.utils.time import utc_now

__all__ = ["EDIT_TOOL", "PERSON_STEP_KINDS", "TABLE_MAX_ROWS", "add_edit", "table_rows"]

EDIT_TOOL: Final[str] = "person_edit"
TABLE_MAX_ROWS: Final[int] = 100
TABLE_CELL_CHARS: Final[int] = 60
SAMPLE_ROWS: Final[int] = 1_000

PERSON_STEP_KINDS: Final[frozenset[RecipeStepKind]] = frozenset(
    {
        RecipeStepKind.CORRECT_CELLS,
        RecipeStepKind.MAP_VALUES,
        RecipeStepKind.SET_MISSING_TEXT,
        RecipeStepKind.CONVERT_UNITS,
        RecipeStepKind.SPLIT_COLUMN,
        RecipeStepKind.VALID_RANGE,
        RecipeStepKind.FILL_MISSING,
        RecipeStepKind.REMOVE_ROWS,
        RecipeStepKind.RENAME_COLUMN,
        RecipeStepKind.DROP_COLUMN,
    }
)
"""What a person may do by hand. Parsing stays the rules' (they measure it); combining stays the rules'."""

_MERGED: Final[frozenset[RecipeStepKind]] = frozenset(
    {RecipeStepKind.CORRECT_CELLS, RecipeStepKind.MAP_VALUES}
)

_TITLES: Final[dict[RecipeStepKind, str]] = {
    RecipeStepKind.CORRECT_CELLS: "Your corrections in '{c}'",
    RecipeStepKind.MAP_VALUES: "Your spellings in '{c}'",
    RecipeStepKind.SET_MISSING_TEXT: "Treat your words in '{c}' as empty",
    RecipeStepKind.CONVERT_UNITS: "Put '{c}' in one unit",
    RecipeStepKind.SPLIT_COLUMN: "Take part of '{c}' into a new column",
    RecipeStepKind.VALID_RANGE: "Treat values outside your range in '{c}' as unknown",
    RecipeStepKind.FILL_MISSING: "Fill the empty cells of '{c}'",
    RecipeStepKind.REMOVE_ROWS: "Leave out rows by '{c}'",
    RecipeStepKind.RENAME_COLUMN: "Rename '{c}'",
    RecipeStepKind.DROP_COLUMN: "Hide '{c}'",
}


def _merged_params(kind: RecipeStepKind, old: Mapping[str, Any], new: Mapping[str, Any]) -> dict[str, Any]:
    if kind is RecipeStepKind.MAP_VALUES:
        return {"mapping": {**dict(old.get("mapping", {})), **dict(new.get("mapping", {}))}}
    by_key = {str(cell["key"]): cell for cell in old.get("cells", [])}
    by_key.update({str(cell["key"]): cell for cell in new.get("cells", [])})
    return {"key_column": new.get("key_column", old.get("key_column")), "cells": list(by_key.values())}


def _policy_gate(ctx: AgentContext, session: AgentSession) -> Callable[..., None] | None:
    """Plan O M233: the data policy's `data.person_edit` rules for this edit, behind `data_doctor.unified`."""
    from engine.doctor.policy import agent_gate

    key, target = roles_of(session)
    return agent_gate(
        ctx,
        "data.person_edit",
        source="person",
        primary_key=key,
        target=target,
        raise_as=SessionError,
    )


def add_edit(
    session: AgentSession,
    ctx: AgentContext,
    *,
    kind: RecipeStepKind,
    column: str,
    params: Mapping[str, Any],
    new_column: str | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> AgentSession:
    """The session with the person's edit added as an accepted step; `SessionError` when it cannot run."""
    from engine.agent.advisor import ordered_steps, recipe_steps
    from engine.agent.recipe import RecipeError, check_recipe, run_recipe
    from engine.doctor import policy as data_policy

    if session.status.value == "applied":
        raise SessionError(
            "AGENT_SESSION_APPLIED", "This setup was already approved. Start again to change it."
        )
    if session.explore or session.mode != "train":
        raise SessionError("AGENT_EDIT_NOT_ALLOWED", "Edits are made while setting up a training file.")
    if kind not in PERSON_STEP_KINDS:
        raise SessionError("AGENT_EDIT_NOT_ALLOWED", f"{kind.value} is not an edit made by hand.")
    # Plan O M233 (DEC-1803): behind `data_doctor.unified`, each check here is followed by the same rule of the
    # data policy (`data.person_edit`), with the same code; None (nothing read) when the switch is off.
    gate = _policy_gate(ctx, session)
    if gate is not None:
        gate(data_policy.STEP_KIND, steps=(data_policy.StepFacts(kind, column),))
    try:
        column = ctx.resolve(column)
    except AgentToolError as exc:
        raise SessionError(exc.code, exc.message) from exc
    personal = {c.name for c in ctx.profile.columns if c.pii_kinds} | set(
        ctx.config.agent.always_hide_columns
    )
    if column in personal and kind is not RecipeStepKind.DROP_COLUMN:
        raise SessionError("AGENT_EDIT_NOT_ALLOWED", "A column holding personal data can only be hidden.")
    if gate is not None:
        gate(data_policy.PERSONAL, steps=(data_policy.StepFacts(kind, column),))
    params = dict(params)
    if kind is RecipeStepKind.CORRECT_CELLS:
        # Always the ID column: a correction found by any other column would be a lookup on it (and
        # would keep what was typed, an email say, in the session).
        key_column = roles_of(session)[0]
        if key_column is None:
            raise SessionError(
                "AGENT_EDIT_INVALID", "Choose the ID column first: a correction is found by it."
            )
        if params.get("key_column", key_column) != key_column:
            raise SessionError("AGENT_EDIT_NOT_ALLOWED", "A correction is found by the ID column only.")
        if key_column in personal:
            raise SessionError(
                "AGENT_EDIT_NOT_ALLOWED",
                "The ID column holds personal data, so a cell cannot be corrected by it; change the value "
                "everywhere instead.",
            )
        params["key_column"] = key_column
    earlier: Proposal | None = None
    if kind in _MERGED:
        earlier = next(
            (
                p
                for p in session.proposals
                if p.source is ProposalSource.PERSON
                and p.state is ProposalState.ACCEPTED
                and p.step is not None
                and p.step.kind is kind
                and p.step.column == column
            ),
            None,
        )
        if earlier is not None and earlier.step is not None:
            params = _merged_params(kind, earlier.step.params, params)
    try:
        step = RecipeStep(
            order=1, kind=kind, column=column, new_column=new_column, params=params, decided_by=DecidedBy.USER
        )
    except ValueError as exc:
        raise SessionError("AGENT_EDIT_INVALID", str(exc).splitlines()[-1][:200]) from exc
    key, target = roles_of(session)
    levels = ctx.config.agent.levels
    others = recipe_steps(
        p
        for p in session.proposals
        if p.state is ProposalState.ACCEPTED and p.kind is ProposalKind.RECIPE_STEP and p is not earlier
    )
    try:
        check_recipe(
            ordered_steps([*others, step]),
            columns=[str(c) for c in ctx.frame.columns],
            primary_key=key,
            target=target,
            levels=levels,
        )
        if gate is not None:
            gate(data_policy.PROTECTED, data_policy.LEVEL, steps=(data_policy.StepFacts.of(step),))
        trial = run_recipe(
            ctx.frame.head(SAMPLE_ROWS),
            [step],
            upload_id=ctx.upload_id,
            primary_key=key,
            target=target,
            levels=levels,
            max_failure_pct=100.0,
        )
    except RecipeError as exc:
        raise SessionError(exc.code, exc.message) from exc
    receipt = trial.receipt.steps[0]
    evidence_id = f"h{len(session.tool_results) + 1}"
    while any(r.evidence_id == evidence_id for r in session.tool_results):
        evidence_id += "x"
    result = ToolResult(
        evidence_id=evidence_id,
        tool=EDIT_TOOL,
        args={"kind": kind.value, "column": column},
        result={"kind": kind.value, "column": column, "rows": receipt.rows, "changed": receipt.changed},
        created_at=clock(),
    )
    reason = (
        f"You made this edit. On the first {receipt.rows} rows it changes {receipt.changed} "
        f"value{'' if receipt.changed == 1 else 's'}; untick it to undo it."
    )
    if kind is RecipeStepKind.REMOVE_ROWS:
        reason = f"You chose to leave these rows out: {receipt.changed} of the first {receipt.rows}. Untick it to keep them."
    proposal_id = earlier.proposal_id if earlier is not None else f"h{len(session.proposals) + 1}"
    while earlier is None and any(p.proposal_id == proposal_id for p in session.proposals):
        proposal_id += "x"
    proposal = Proposal(
        proposal_id=proposal_id,
        kind=ProposalKind.RECIPE_STEP,
        title=_TITLES[kind].format(c=display_name(column)),
        reason=reason,
        step=step.model_copy(update={"reason": reason}),
        evidence_ids=(evidence_id,),
        confidence=AgentConfidence.CHECK,
        state=ProposalState.ACCEPTED,
        decided_by=DecidedBy.USER,
        source=ProposalSource.PERSON,
    )
    proposals = (
        tuple(proposal if p is earlier else p for p in session.proposals)
        if earlier is not None
        else (*session.proposals, proposal)
    )
    kept_results = tuple(
        r for r in session.tool_results if earlier is None or r.evidence_id not in earlier.evidence_ids
    )
    updated = session.model_copy(update={"proposals": proposals, "tool_results": (*kept_results, result)})
    return with_summary(updated, ctx, clock=clock)


def table_rows(
    ctx: AgentContext,
    *,
    offset: int = 0,
    limit: int = 50,
    where_column: str | None = None,
    equals: str | None = None,
) -> dict[str, Any]:
    """Rows of the file for the table view: real values, personal data hidden, every cell masked and cut."""
    from engine.agent.formats import masked_cut

    frame = ctx.frame
    limit = max(1, min(limit, TABLE_MAX_ROWS))
    offset = max(0, offset)
    personal = {c.name for c in ctx.profile.columns if c.pii_kinds} | set(
        ctx.config.agent.always_hide_columns
    )
    if where_column is not None:
        name = ctx.resolve(where_column)
        if name in personal:
            raise AgentToolError("AGENT_TOOL_ARGS_INVALID", "A personal-data column cannot be searched.")
        frame = frame[frame[name].astype(str).str.strip() == (equals or "").strip()]
    window = frame.iloc[offset : offset + limit]
    columns = [str(c) for c in frame.columns]

    def cell(name: str, value: Any) -> str | None:
        if name in personal:
            return "[personal data]"
        if value is None or (isinstance(value, float) and value != value):
            return None
        text = str(value)
        if hasattr(value, "isoformat"):
            text = str(value.isoformat())
            text = text[:10] if text.endswith("T00:00:00") else text
        return masked_cut(text, TABLE_CELL_CHARS)

    rows = [
        [cell(name, value) for name, value in zip(columns, record, strict=True)]
        for record in window.itertuples(index=False)
    ]
    return {
        "columns": [display_name(c) for c in columns],
        "personal": sorted(display_name(c) for c in personal if c in frame.columns),
        "rows": rows,
        "offset": offset,
        "total": len(frame),
        "row_numbers": [int(i) + 2 for i in window.index],
    }
