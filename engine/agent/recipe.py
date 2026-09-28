"""The recipe engine (Plan G §6): check a recipe, then run it on a copy of the data.

The helper never edits a file. It proposes `RecipeStep`s; once the user approves them, this module
runs them - and only this module does. Three rules hold for every run:

* **Fixed order.** Parsing and tidying steps run first, then derived columns, then drops, so a
  derived column always reads cleaned values and a drop never removes something a later step needs.
  `check_recipe` refuses a recipe written in another order rather than silently re-sorting it.
* **Stateless.** Every step's output for a row depends only on that row and the step's parameters
  (DEC-1004), so the same recipe gives the same result on next month's file and nothing learnt from
  the test rows can leak into training.
* **Counted.** Every step reports how many values it changed and how many it could not convert, with
  masked examples. A step that fails on more than `max_failure_pct` of a column's non-empty values
  stops the run with `RECIPE_VALUES_UNCONVERTED`; a column the recipe needs and the file lacks stops
  it with `RECIPE_COLUMN_MISSING`. Nothing is guessed.

The input frame is never modified; `run_recipe` works on a copy.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

import pandas as pd

from engine.agent.config import AgentLevel
from engine.agent.contracts import RecipeReceipt, RecipeStep, RecipeStepKind, StepReceipt, recipe_hash
from engine.agent.formats import ParseOutcome, map_booleans, normalise_texts, parse_dates, parse_numbers
from engine.onboarding.transforms import TransformError, derive
from engine.utils.time import utc_now

__all__ = [
    "STEP_PHASE",
    "RecipeError",
    "RecipeRun",
    "check_recipe",
    "derive_names",
    "run_recipe",
]

STEP_PHASE: Final[dict[RecipeStepKind, int]] = {
    RecipeStepKind.PARSE_NUMBER: 1,
    RecipeStepKind.PARSE_DATE: 1,
    RecipeStepKind.MAP_BOOLEAN: 1,
    RecipeStepKind.NORMALISE_TEXT: 1,
    RecipeStepKind.DERIVE: 2,
    RecipeStepKind.DROP_COLUMN: 3,
}
"""Parse and tidy, then derive, then drop (Plan G §6.3)."""

_LEVEL_OF: Final[dict[RecipeStepKind, AgentLevel]] = {
    RecipeStepKind.DERIVE: AgentLevel.DERIVE,
}
"""The level a step kind needs; every kind not listed is `clean`."""

SNAPSHOT_NAME: Final[str] = "snapshot_date"
"""The fixed name a derive expression uses for the row's snapshot date (onboarding's convention)."""


class RecipeError(Exception):
    """A recipe that cannot run on this file. `code` is a validation code or a RECIPE_* code."""

    def __init__(
        self, code: str, message: str, *, column: str | None = None, order: int | None = None
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.column = column
        self.order = order


@dataclass(frozen=True)
class RecipeRun:
    """The prepared copy and the receipt of what each step did."""

    frame: pd.DataFrame
    receipt: RecipeReceipt


def derive_names(expression: str) -> frozenset[str]:
    """Every name a derive expression reads (columns, `snapshot_date` and function names)."""
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise RecipeError("RECIPE_STEP_INVALID", f"'{expression}' is not a valid expression.") from exc
    return frozenset(node.id for node in ast.walk(tree) if isinstance(node, ast.Name))


def _params(step: RecipeStep) -> dict[str, Any]:
    return dict(step.params)


def check_recipe(
    steps: Sequence[RecipeStep],
    *,
    columns: Sequence[str],
    primary_key: str | None,
    target: str | None,
    levels: Sequence[AgentLevel],
    snapshot_column: str | None = None,
) -> None:
    """Refuse a recipe that is out of order, above the allowed level, or unsafe for this file.

    `columns` are the file's columns. A step may not touch the primary key (scores are joined back
    on it) or the target (the engine reads the outcome itself), and a derived column may not read
    the target - that would hand the model the answer.
    """
    available = list(columns)
    protected = {name for name in (primary_key, target) if name}
    phase = 0
    created: set[str] = set()
    for expected, step in enumerate(steps, start=1):
        if step.order != expected:
            raise RecipeError(
                "RECIPE_STEP_INVALID", "Steps must be numbered 1, 2, 3 … in order.", order=step.order
            )
        if STEP_PHASE[step.kind] < phase:
            raise RecipeError(
                "RECIPE_STEP_INVALID",
                "Steps run in a fixed order: fix values first, then add new columns, then hide columns.",
                order=step.order,
            )
        phase = STEP_PHASE[step.kind]
        needed = _LEVEL_OF.get(step.kind, AgentLevel.CLEAN)
        if needed not in levels:
            raise RecipeError(
                "RECIPE_STEP_INVALID",
                f"This use case's helper may not use {step.kind.value} steps.",
                order=step.order,
            )
        if step.column in protected and step.kind is not RecipeStepKind.DERIVE:
            raise RecipeError(
                "RECIPE_STEP_INVALID",
                f"'{step.column}' is the ID or the outcome column, which the recipe never changes.",
                column=step.column,
                order=step.order,
            )
        _check_params(step)
        if step.kind is RecipeStepKind.DERIVE:
            names = derive_names(str(_params(step)["expression"]))
            if target and target in names:
                raise RecipeError(
                    "RECIPE_STEP_INVALID",
                    f"A new column may not be computed from the outcome column '{target}'.",
                    column=step.new_column,
                    order=step.order,
                )
            if SNAPSHOT_NAME in names and snapshot_column is None:
                raise RecipeError(
                    "RECIPE_STEP_INVALID",
                    "This file has no snapshot date, so a column measured from it cannot be computed.",
                    column=step.new_column,
                    order=step.order,
                )
            new = str(step.new_column)
            if new in available or new in created or new in protected:
                raise RecipeError(
                    "RECIPE_STEP_INVALID",
                    f"There is already a column called '{new}'.",
                    column=new,
                    order=step.order,
                )
            created.add(new)
            available.append(new)
        elif step.column not in available:
            raise RecipeError(
                "RECIPE_COLUMN_MISSING",
                f"The file has no column '{step.column}'.",
                column=step.column,
                order=step.order,
            )
        if step.kind is RecipeStepKind.DROP_COLUMN:
            available.remove(step.column)
        elif step.new_column is not None and step.kind is not RecipeStepKind.DERIVE:
            if step.new_column in available:
                raise RecipeError(
                    "RECIPE_STEP_INVALID",
                    f"There is already a column called '{step.new_column}'.",
                    column=step.new_column,
                    order=step.order,
                )
            available.append(step.new_column)


def _check_params(step: RecipeStep) -> None:
    params = _params(step)
    kind = step.kind

    def refuse(what: str) -> RecipeError:
        return RecipeError(
            "RECIPE_STEP_INVALID", f"Step {step.order} ({kind.value}): {what}", order=step.order
        )

    allowed: dict[RecipeStepKind, frozenset[str]] = {
        RecipeStepKind.DROP_COLUMN: frozenset(),
        RecipeStepKind.PARSE_NUMBER: frozenset({"decimal", "percent_to_fraction"}),
        RecipeStepKind.PARSE_DATE: frozenset({"dayfirst"}),
        RecipeStepKind.MAP_BOOLEAN: frozenset({"true_values", "false_values"}),
        RecipeStepKind.NORMALISE_TEXT: frozenset({"strip", "merge"}),
        RecipeStepKind.DERIVE: frozenset({"expression"}),
    }
    unknown = set(params) - allowed[kind]
    if unknown:
        raise refuse(f"unknown parameter {sorted(unknown)[0]!r}")
    if kind is RecipeStepKind.PARSE_NUMBER and params.get("decimal", ".") not in {".", ","}:
        raise refuse("decimal must be '.' or ','")
    if kind is RecipeStepKind.PARSE_DATE and not isinstance(params.get("dayfirst"), bool):
        raise refuse("the day/month order must be decided (dayfirst true or false)")
    if kind is RecipeStepKind.MAP_BOOLEAN:
        true_values, false_values = params.get("true_values"), params.get("false_values")
        if not (
            isinstance(true_values, list) and true_values and isinstance(false_values, list) and false_values
        ):
            raise refuse("list the spellings of yes and of no")
        if {str(v).strip().casefold() for v in true_values} & {
            str(v).strip().casefold() for v in false_values
        }:
            raise refuse("a spelling cannot mean both yes and no")
    if kind is RecipeStepKind.NORMALISE_TEXT:
        merge = params.get("merge", {})
        if not isinstance(merge, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in merge.items()
        ):
            raise refuse("merge must map spellings to spellings")
    if kind is RecipeStepKind.DERIVE and not isinstance(params.get("expression"), str):
        raise refuse("a new column needs an expression")


def _apply(frame: pd.DataFrame, step: RecipeStep, snapshot_column: str | None) -> ParseOutcome:
    params = _params(step)
    series = frame[step.column] if step.kind is not RecipeStepKind.DERIVE else None
    runners: dict[RecipeStepKind, Callable[[], ParseOutcome]] = {
        RecipeStepKind.PARSE_NUMBER: lambda: parse_numbers(
            _series(series),
            decimal=str(params.get("decimal", ".")),
            percent_to_fraction=bool(params.get("percent_to_fraction", True)),
        ),
        RecipeStepKind.PARSE_DATE: lambda: parse_dates(_series(series), dayfirst=bool(params["dayfirst"])),
        RecipeStepKind.MAP_BOOLEAN: lambda: map_booleans(
            _series(series),
            true_values=[str(v) for v in params["true_values"]],
            false_values=[str(v) for v in params["false_values"]],
        ),
        RecipeStepKind.NORMALISE_TEXT: lambda: normalise_texts(
            _series(series),
            merge={str(k): str(v) for k, v in dict(params.get("merge", {})).items()},
            strip=bool(params.get("strip", True)),
        ),
        RecipeStepKind.DERIVE: lambda: _derive(frame, str(params["expression"]), snapshot_column, step),
    }
    return runners[step.kind]()


def _series(series: pd.Series[Any] | None) -> pd.Series[Any]:
    if series is None:  # pragma: no cover - only derive passes None, and derive does not call this
        raise RecipeError("RECIPE_STEP_INVALID", "This step reads no column.")
    return series


def _derive(
    frame: pd.DataFrame, expression: str, snapshot_column: str | None, step: RecipeStep
) -> ParseOutcome:
    work = frame
    snapshot = snapshot_column
    if snapshot is None:
        # `derive` binds `snapshot_date` to a column it requires; `check_recipe` has already refused any
        # expression that reads it, so an empty stand-in is never read.
        snapshot = "__recipe_no_snapshot__"
        work = frame.assign(**{snapshot: pd.Series(pd.NaT, index=frame.index, dtype="datetime64[ns]")})
    try:
        values = derive(work, expression, snapshot_column=snapshot)
    except TransformError as exc:
        raise RecipeError(
            "RECIPE_STEP_INVALID", exc.message, column=step.new_column, order=step.order
        ) from exc
    return ParseOutcome(values=values, changed=int(values.notna().sum()), failed=0, failed_examples=())


def run_recipe(
    frame: pd.DataFrame,
    steps: Sequence[RecipeStep],
    *,
    upload_id: str,
    primary_key: str | None,
    target: str | None,
    levels: Sequence[AgentLevel],
    max_failure_pct: float,
    snapshot_column: str | None = None,
    derived_upload_id: str | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> RecipeRun:
    """Check, then run, `steps` on a copy of `frame`. Raises `RecipeError` instead of guessing."""
    check_recipe(
        steps,
        columns=[str(c) for c in frame.columns],
        primary_key=primary_key,
        target=target,
        levels=levels,
        snapshot_column=snapshot_column,
    )
    work = frame.copy()
    receipts: list[StepReceipt] = []
    for step in steps:
        if step.kind is RecipeStepKind.DROP_COLUMN:
            receipts.append(
                StepReceipt(
                    order=step.order, kind=step.kind, column=step.column, rows=len(work), changed=0, failed=0
                )
            )
            work = work.drop(columns=[step.column])
            continue
        outcome = _apply(work, step, snapshot_column)
        if step.kind is not RecipeStepKind.DERIVE:
            non_empty = int(work[step.column].notna().sum())
            if non_empty and outcome.failed / non_empty * 100.0 > max_failure_pct:
                raise RecipeError(
                    "RECIPE_VALUES_UNCONVERTED",
                    f"{outcome.failed} of {non_empty} values in '{step.column}' could not be converted "
                    f"({outcome.failed / non_empty:.1%}), more than the {max_failure_pct:g}% limit.",
                    column=step.column,
                    order=step.order,
                )
        work[step.new_column or step.column] = outcome.values
        receipts.append(
            StepReceipt(
                order=step.order,
                kind=step.kind,
                column=step.column,
                rows=len(work),
                changed=outcome.changed,
                failed=outcome.failed,
                examples_failed=outcome.failed_examples,
            )
        )
    receipt = RecipeReceipt(
        recipe_hash=recipe_hash(tuple(steps)),
        upload_id=upload_id,
        derived_upload_id=derived_upload_id,
        rows_in=len(frame),
        rows_out=len(work),
        columns_in=len(frame.columns),
        columns_out=len(work.columns),
        steps=tuple(receipts),
        created_at=clock(),
    )
    return RecipeRun(frame=work, receipt=receipt)
