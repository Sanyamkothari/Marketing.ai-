"""How far an AI-proposed step may change a column (Plan O decision O10, M238, DEC-1808 (g)).

Plan L §7 promised it and never built it (`agent.max_ai_step_change_pct`, [INV] §7): an AI's step is never
trusted for its effect. Its preview is measured, and the share of the column it changes decides what happens:

| Share changed | Verdict | What the person sees |
|---|---|---|
| up to 30% | `ok` | the proposal, pending, never ticked |
| above 30%, up to 60% | `warn` (`AI_STEP_CHANGES_MANY`) | the proposal with a warning naming the share |
| above 60% | `refuse` (`AI_STEP_CHANGES_TOO_MANY`) | nothing is added; the person may write the same step as their own edit |

The guard reads the source of the proposal only: a step the rules or the person proposed is not an AI step, and
the person re-proposing the AI's step as their own edit is exactly the way past a refusal. The share is of the
column's non-empty values for a value step, of the column's rows for a step that fills empty cells
(`fill_missing`, so a fill never scores above its real share), and of the rows for a step that leaves rows out;
it is computed by
running the step itself on a copy (`measure_step`), never estimated.

A step that makes a new table (`sql_query`) has no single column: each output column that has the name of an input
column is compared with it customer by customer (`engine.doctor.guards.cell_change`). A step that keeps the
customers but changes what a row is (combine, latest row, unpivot) is measured by the customers it loses. A step
that cannot be measured (no ID to line the rows up by) is `NotMeasurableError`, and :func:`unmeasured_verdict` makes it
"needs a yes" - it is never counted as 0%.

This module has no state and reads no configuration: the limits are passed in (`ChangeShareLimits`), defaulting
to the decision's 30% and 60%, so M233's policy and a use case's settings can carry them later.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

import pandas as pd

from engine.agent.config import AgentLevel
from engine.agent.contracts import ProposalSource, RecipeStep, RecipeStepKind
from engine.agent.recipe import run_recipe
from engine.doctor.guards import NotMeasurableError, cell_change, infer_key, make_receipt

__all__ = [
    "CHANGE_SHARE_CODES",
    "DEFAULT_LIMITS",
    "ChangeShareLimits",
    "ChangeShareVerdict",
    "NotMeasurableError",
    "change_share_verdict",
    "measure_step",
    "unmeasured_verdict",
]

CHANGE_SHARE_CODES: Final[frozenset[str]] = frozenset({"AI_STEP_CHANGES_MANY", "AI_STEP_CHANGES_TOO_MANY"})

_ROW_STEPS: Final[frozenset[RecipeStepKind]] = frozenset({RecipeStepKind.REMOVE_ROWS})
"""Steps whose change is counted in rows: every row they leave out is a changed row."""

_RESHAPE_STEPS: Final[frozenset[RecipeStepKind]] = frozenset(
    {RecipeStepKind.KEEP_LATEST_PER_ID, RecipeStepKind.COMBINE_ROWS, RecipeStepKind.UNPIVOT}
)
"""Steps that change what a row is: measured by the customers they keep, not by how many rows they make."""

_FILLS_EMPTY_STEPS: Final[frozenset[RecipeStepKind]] = frozenset({RecipeStepKind.FILL_MISSING})
"""Value steps that write into empty cells: their share is measured against every row of the column."""


@dataclass(frozen=True)
class ChangeShareLimits:
    """Decision O10's two limits, in percent of the column (or of the rows)."""

    warn_pct: float = 30.0
    refuse_pct: float = 60.0

    def __post_init__(self) -> None:
        if not 0.0 <= self.warn_pct <= self.refuse_pct <= 100.0:
            raise ValueError("limits must satisfy 0 <= warn_pct <= refuse_pct <= 100")


DEFAULT_LIMITS: Final[ChangeShareLimits] = ChangeShareLimits()


@dataclass(frozen=True)
class ChangeShareVerdict:
    """What the guard decided for one proposal."""

    level: Literal["ok", "warn", "refuse"]
    share_pct: float
    changed: int
    out_of: int
    code: str | None
    message: str


def change_share_verdict(
    *,
    changed: int,
    out_of: int,
    source: ProposalSource,
    limits: ChangeShareLimits = DEFAULT_LIMITS,
) -> ChangeShareVerdict:
    """The verdict for a step that changes `changed` of `out_of` values (or rows), proposed by `source`."""
    share = 0.0 if out_of <= 0 else changed / out_of * 100.0
    if source is not ProposalSource.AI or share <= limits.warn_pct:
        return ChangeShareVerdict("ok", share, changed, out_of, None, "")
    if share <= limits.refuse_pct:
        return ChangeShareVerdict(
            "warn",
            share,
            changed,
            out_of,
            "AI_STEP_CHANGES_MANY",
            f"This suggestion from the AI changes {changed:,} of {out_of:,} values ({share:.0f}%), more than "
            f"{limits.warn_pct:g}%. Check the before and after carefully before you tick it.",
        )
    return ChangeShareVerdict(
        "refuse",
        share,
        changed,
        out_of,
        "AI_STEP_CHANGES_TOO_MANY",
        f"This suggestion from the AI would change {changed:,} of {out_of:,} values ({share:.0f}%), more than "
        f"{limits.refuse_pct:g}%, so it is not offered. If you want it, add the same change as your own edit.",
    )


def unmeasured_verdict(reason: str) -> ChangeShareVerdict:
    """The verdict for a step whose effect could not be measured: it needs a person's yes, whoever proposed it."""
    return ChangeShareVerdict(
        "warn",
        0.0,
        0,
        0,
        "STEP_NOT_MEASURED",
        f"{reason} The step needs your yes: check the before and after before you tick it.",
    )


def measure_step(
    frame: pd.DataFrame,
    step: RecipeStep,
    *,
    primary_key: str | None,
    target: str | None,
    snapshot_column: str | None = None,
    tables: Mapping[str, pd.DataFrame] | None = None,
) -> tuple[int, int]:
    """(changed, out of): what `step` does to `frame`, by running it alone on a copy.

    Run with every level and no failure limit: the guard measures, it does not decide whether the step may run
    (`check_recipe` and Approve still do). Raises `RecipeError` when the step cannot run on this file at all, and
    :class:`NotMeasurableError` when the step makes a new table and the customers cannot be lined up.
    """
    alone = step.model_copy(update={"order": 1})
    levels: Sequence[AgentLevel] = tuple(AgentLevel)
    run = run_recipe(
        frame,
        [alone],
        upload_id="change-share",
        primary_key=primary_key,
        target=target,
        levels=levels,
        max_failure_pct=100.0,
        snapshot_column=snapshot_column,
        tables=tables,
    )
    receipt = run.receipt.steps[0]
    if step.kind is RecipeStepKind.SQL_QUERY:
        return cell_change(frame, run.frame, key=primary_key or infer_key(frame, run.frame))
    if step.kind in _RESHAPE_STEPS:
        counted = make_receipt(1, step.kind.value, {"this": frame}, run.frame, key=primary_key or step.column)
        if counted.keys_in is None:
            raise NotMeasurableError(
                "The ID column is not in the result, so the customers it keeps could not be counted."
            )
        return counted.keys_lost, counted.keys_in
    if step.kind in _ROW_STEPS:
        # Rows left out, combined away or added: every row a step removes or creates is a changed row.
        return abs(len(frame) - len(run.frame)), len(frame)
    if step.column not in frame.columns:
        return receipt.changed, 0
    if step.kind in _FILLS_EMPTY_STEPS:
        # It writes into empty cells, so its share is of the column's rows, not of its non-empty values.
        return receipt.changed, len(frame)
    return receipt.changed, int(frame[step.column].notna().sum())
