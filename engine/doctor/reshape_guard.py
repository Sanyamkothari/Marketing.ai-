"""The reshape level for every goal, with guards (Plan O decision O3, M238, DEC-1808 (f)).

Today the reshape level - `combine_rows`, `unpivot`, and now `keep_latest_per_id` - is on only where a use case
lists it (`retail-win-back`; the default is `[clean, derive]`, `configs/engine.yaml` `defaults.agent.levels`).
Decision O3 turns it on for every goal once Data Doctor is the data agent (`data_doctor.unified`), and only with
four guards, which this module states as code so the planner and Approve can hold every reshape step to them:

1. **A blocking question.** A reshape step changes what one row means. It is offered only through one blocking
   question the person answers (`reshape_question`), never as a silent default.
2. **Never ticked.** Whoever proposed it - the rules too - it starts unticked (`advisor.NEVER_TICKED_STEPS`
   lists `keep_latest_per_id`; `combine_rows` and `unpivot` are already added only through their question).
3. **The point-in-time probe.** A training Approve runs the step with the full future-data check
   (`leak_check="full"`); a scoring file with the narrow one. A preview runs neither and is never approved.
4. **The receipt.** The preview's receipt (rows in, rows out) is shown before the question is answered.

With the switch off nothing here is called and every use case keeps its own levels (`doctor_levels`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from engine.agent.config import AgentLevel
from engine.agent.contracts import Question, QuestionOption, RecipeStep, RecipeStepKind, StepReceipt
from engine.agent.reshape import LeakCheck

__all__ = [
    "RESHAPE_GUARD_CODES",
    "RESHAPE_STEP_KINDS",
    "ReshapeGuardError",
    "ReshapeItem",
    "check_reshape_guards",
    "doctor_levels",
    "reshape_guard_failures",
    "reshape_question",
]

RESHAPE_STEP_KINDS: Final[frozenset[RecipeStepKind]] = frozenset(
    {RecipeStepKind.COMBINE_ROWS, RecipeStepKind.UNPIVOT, RecipeStepKind.KEEP_LATEST_PER_ID}
)
RESHAPE_GUARD_CODES: Final[frozenset[str]] = frozenset({"RESHAPE_GUARD_MISSING"})

_ORDER: Final[tuple[AgentLevel, ...]] = (AgentLevel.CLEAN, AgentLevel.DERIVE, AgentLevel.RESHAPE)


class ReshapeGuardError(Exception):
    """A reshape step that reached Approve without one of decision O3's guards."""

    def __init__(self, reasons: Sequence[str]) -> None:
        self.code = "RESHAPE_GUARD_MISSING"
        self.reasons = tuple(reasons)
        self.message = "This change to the rows cannot be approved yet: " + "; ".join(self.reasons) + "."
        super().__init__(f"{self.code}: {self.message}")


def doctor_levels(levels: Sequence[AgentLevel], *, unified: bool) -> tuple[AgentLevel, ...]:
    """The levels a recipe may use: the use case's own, plus `reshape` for every goal when Data Doctor is on."""
    wanted = set(levels) | ({AgentLevel.RESHAPE} if unified else set())
    if not unified:
        return tuple(levels)
    return tuple(level for level in _ORDER if level in wanted)


@dataclass(frozen=True)
class ReshapeItem:
    """What Approve knows about one reshape step: how it was offered and what will check it."""

    step: RecipeStep
    ticked_when_offered: bool
    """Whether the step was offered already ticked (it never may be)."""
    question_answered: bool
    """Whether the person answered its blocking question with the option that adds it."""
    leak_check: LeakCheck | None
    """The future-data check the run will do: `full` at a training Approve, `narrow` on a scoring file."""
    receipt: StepReceipt | None
    """The preview receipt shown with the question."""
    training: bool = True


def reshape_guard_failures(item: ReshapeItem) -> tuple[str, ...]:
    """Each of decision O3's guards `item` misses, in plain words; empty when it may be approved."""
    if item.step.kind not in RESHAPE_STEP_KINDS:
        return ()
    missing: list[str] = []
    if not item.question_answered:
        missing.append("its question has not been answered")
    if item.ticked_when_offered:
        missing.append("it was offered already ticked, and a change to the rows never is")
    wanted: LeakCheck = "full" if item.training else "narrow"
    if item.leak_check != wanted:
        missing.append(f"the {wanted} future-data check is not set to run")
    if item.receipt is None or item.receipt.kind is not item.step.kind:
        missing.append("its preview (rows in and rows out) has not been shown")
    return tuple(missing)


def check_reshape_guards(item: ReshapeItem) -> None:
    """Raise `ReshapeGuardError` (`RESHAPE_GUARD_MISSING`) when `item` misses a guard."""
    missing = reshape_guard_failures(item)
    if missing:
        raise ReshapeGuardError(missing)


_WHAT: Final[dict[RecipeStepKind, str]] = {
    RecipeStepKind.KEEP_LATEST_PER_ID: "keep only the latest row of each '{column}'",
    RecipeStepKind.COMBINE_ROWS: "combine the rows of each '{column}' into one",
    RecipeStepKind.UNPIVOT: "turn the month columns into rows, one per '{column}' per month",
}


def reshape_question(step: RecipeStep, *, receipt: StepReceipt, rows_out: int) -> Question:
    """The one blocking question a reshape step is offered through (guard 1), with its preview (guard 4)."""
    if step.kind not in RESHAPE_STEP_KINDS:
        raise ValueError(f"{step.kind.value} is not a change to the rows")
    what = _WHAT[step.kind].format(column=step.column)
    return Question(
        question_id=f"reshape-{step.kind.value.replace('_', '-')}",
        text=f"Should Data Doctor {what}? The file has {receipt.rows:,} rows; this leaves {rows_out:,}.",
        options=(
            QuestionOption(
                option_id="yes",
                label="Yes, do this",
                effect=f"Adds the step to {what}, unticked, for you to tick at Approve.",
            ),
            QuestionOption(option_id="no", label="No, leave the rows as they are", effect="Adds nothing."),
            QuestionOption(
                option_id="dont-know",
                label="I don't know",
                effect="Adds nothing; the rows stay as they are.",
            ),
        ),
        blocking=True,
    )
