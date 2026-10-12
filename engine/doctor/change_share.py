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
column's non-empty values for a value step and of the rows for a step that leaves rows out; it is computed by
running the step itself on a copy (`measure_step`), never estimated.

This module has no state and reads no configuration: the limits are passed in (`ChangeShareLimits`), defaulting
to the decision's 30% and 60%, so M233's policy and a use case's settings can carry them later.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

import pandas as pd

from engine.agent.config import AgentLevel
from engine.agent.contracts import ProposalSource, RecipeStep, RecipeStepKind
from engine.agent.recipe import run_recipe

__all__ = [
    "CHANGE_SHARE_CODES",
    "DEFAULT_LIMITS",
    "ChangeShareLimits",
    "ChangeShareVerdict",
    "change_share_verdict",
    "measure_step",
]

CHANGE_SHARE_CODES: Final[frozenset[str]] = frozenset({"AI_STEP_CHANGES_MANY", "AI_STEP_CHANGES_TOO_MANY"})

_ROW_STEPS: Final[frozenset[RecipeStepKind]] = frozenset(
    {
        RecipeStepKind.REMOVE_ROWS,
        RecipeStepKind.KEEP_LATEST_PER_ID,
        RecipeStepKind.COMBINE_ROWS,
        RecipeStepKind.UNPIVOT,
    }
)
"""Steps whose change is counted in rows, not in one column's values."""


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


def measure_step(
    frame: pd.DataFrame,
    step: RecipeStep,
    *,
    primary_key: str | None,
    target: str | None,
    snapshot_column: str | None = None,
) -> tuple[int, int]:
    """(changed, out of): what `step` does to `frame`, by running it alone on a copy.

    Run with every level and no failure limit: the guard measures, it does not decide whether the step may run
    (`check_recipe` and Approve still do). Raises `RecipeError` when the step cannot run on this file at all.
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
    )
    receipt = run.receipt.steps[0]
    if step.kind in _ROW_STEPS:
        # Rows left out, combined away or added: every row a step removes or creates is a changed row.
        return abs(len(frame) - len(run.frame)), len(frame)
    if step.column not in frame.columns:
        return receipt.changed, 0
    return receipt.changed, int(frame[step.column].notna().sum())
