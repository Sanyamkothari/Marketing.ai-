"""One file into a file to learn from and a file to score, by one rule and one recipe (Plan O M238, DEC-1808 (e)).

Plan L §5.5: "Score the customers who joined after March." `split_file` is a PROPOSE tool and an APPROVE-class
item (design §6.6: it chooses which customers are learnt from). Its rule is frozen, stateless and row-wise:

* **By date** - rows dated on or before `cut` go to the training part, rows dated after it to the scoring part.
  A row whose date cannot be read goes to neither and is counted; more than the use case's failure limit stops
  the split (`SPLIT_VALUES_UNREADABLE`). Text dates are read in the frozen day/month order (`dayfirst`), each
  value on its own, exactly as `combine_rows` reads them; an order nothing proved stops instead of guessing.
* **By values** - rows whose value is one of `scoring_values` (compared as trimmed text) go to the scoring part,
  every other row to the training part.

The rule may not name the ID or the outcome (`SPLIT_RULE_INVALID`): choosing rows by the outcome would hand the
model the answer, and an ID's spelling carries no meaning to split on.

**The point-in-time guard** (Plan L §7): on a split by date, every training row is dated on or before the cut,
checked again after the split; and a training row whose *other* date columns already hold a date after the cut
was written after it, so it is reported (`SPLIT_LATER_DATES_IN_TRAINING`, with counts per column) for the person
to decide, never silently kept as if nothing were wrong.

**One recipe, two prepared uploads.** `split_file` runs the approved recipe once on the whole file, then splits
the prepared copy, so both parts carry the same `recipe_hash` and the scoring part is prepared exactly as the
training part. Both parts keep every column; writing them as uploads is the executor's work, never a tool's.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal, Self

import pandas as pd
from pydantic import Field, model_validator

from engine.agent.config import AgentLevel
from engine.agent.contracts import RecipeReceipt, RecipeStep
from engine.agent.recipe import RecipeRun, run_recipe
from engine.agent.reshape import CombineError, LeakCheck, _dates
from engine.config import StrictBase
from engine.utils.time import utc_now

__all__ = [
    "SPLIT_CODES",
    "LaterDates",
    "SplitError",
    "SplitFileRun",
    "SplitParts",
    "SplitReceipt",
    "SplitRule",
    "check_split_rule",
    "split_file",
    "split_rows",
]

SPLIT_CODES: Final[frozenset[str]] = frozenset(
    {"SPLIT_RULE_INVALID", "SPLIT_PART_EMPTY", "SPLIT_VALUES_UNREADABLE", "SPLIT_LATER_DATES_IN_TRAINING"}
)

MAX_SCORING_VALUES: Final[int] = 50
_DAY: Final[pd.Timedelta] = pd.Timedelta(days=1)
_MAX_OTHER_DATE_COLUMNS: Final[int] = 10


class SplitRule(StrictBase):
    """A frozen rule: which rows are learnt from and which are scored."""

    column: str = Field(min_length=1, description="The column the rule reads.")
    by: Literal["date", "values"] = Field(description="Split on a date cut or on listed values.")
    cut: str | None = Field(default=None, description="`date`: the last day of the training part (ISO date).")
    dayfirst: bool | None = Field(
        default=None, description="`date` on text: the frozen day/month order; null when the values prove it."
    )
    scoring_values: tuple[str, ...] = Field(
        default=(),
        description="`values`: rows holding one of these are scored; every other row is learnt from.",
    )

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if self.by == "date":
            if self.cut is None or self.scoring_values:
                raise ValueError("a split by date names a cut date and no values")
            try:
                datetime.strptime(self.cut, "%Y-%m-%d")
            except ValueError as exc:
                raise ValueError("the cut is a date written YYYY-MM-DD") from exc
        else:
            if self.cut is not None or self.dayfirst is not None:
                raise ValueError("a split by values names no cut date")
            if not 0 < len(self.scoring_values) <= MAX_SCORING_VALUES or not all(
                v.strip() for v in self.scoring_values
            ):
                raise ValueError(f"list 1 to {MAX_SCORING_VALUES} values whose rows are scored")
        return self

    @property
    def rule_hash(self) -> str:
        blob = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def sentence(self) -> str:
        """The exact text the person approves (exact-text consent, design §6.6)."""
        if self.by == "date":
            order = (
                ""
                if self.dayfirst is None
                else f" Dates written with numbers are read {'day' if self.dayfirst else 'month'} first."
            )
            return (
                f"Learn from the rows whose '{self.column}' is on or before {self.cut}, and score the rows whose "
                f"'{self.column}' is after it.{order}"
            )
        shown = ", ".join(f"'{v}'" for v in self.scoring_values[:5]) + (
            " …" if len(self.scoring_values) > 5 else ""
        )
        return f"Score the rows whose '{self.column}' is {shown}, and learn from every other row."


class SplitError(Exception):
    """A split that cannot be made on this file; `code` is one of `SPLIT_CODES`."""

    def __init__(self, code: str, message: str, *, column: str | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.column = column


class LaterDates(StrictBase):
    """Training rows whose other date column already holds a date after the cut."""

    column: str = Field(description="The other date column.")
    rows: int = Field(ge=0, description="Training rows with a date in it after the cut.")


class SplitReceipt(StrictBase):
    """What the split did: counted, never silent."""

    rule: SplitRule = Field(description="The rule that was applied.")
    rule_hash: str = Field(description="Hash of the rule.")
    recipe_hash: str | None = Field(default=None, description="The recipe both parts were prepared with.")
    rows_in: int = Field(ge=0, description="Rows of the prepared file.")
    rows_training: int = Field(ge=0, description="Rows learnt from.")
    rows_scoring: int = Field(ge=0, description="Rows scored.")
    rows_unassigned: int = Field(ge=0, description="Rows whose date could not be read: in neither part.")
    point_in_time: str | None = Field(
        default=None, description="The point-in-time check, on a split by date."
    )
    later_dates: tuple[LaterDates, ...] = Field(
        default=(), description="Other dates after the cut in training."
    )
    warnings: tuple[str, ...] = Field(default=(), description="`SPLIT_LATER_DATES_IN_TRAINING` when any.")


@dataclass(frozen=True)
class SplitParts:
    training: pd.DataFrame
    scoring: pd.DataFrame
    receipt: SplitReceipt


@dataclass(frozen=True)
class SplitFileRun:
    """Two prepared uploads with one recipe: each part's frame and receipt, and the split's receipt."""

    training: RecipeRun
    scoring: RecipeRun
    split: SplitReceipt


def check_split_rule(
    rule: SplitRule, *, columns: Sequence[str], primary_key: str | None, target: str | None
) -> None:
    """Refuse a rule on the ID or the outcome, or on a column the file lacks."""
    if rule.column in {primary_key, target} - {None}:
        raise SplitError(
            "SPLIT_RULE_INVALID",
            f"'{rule.column}' is the ID or the outcome column; a file is never split by it.",
            column=rule.column,
        )
    if rule.column not in columns:
        raise SplitError("SPLIT_RULE_INVALID", f"The file has no column '{rule.column}'.", column=rule.column)


def _text(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _other_dates(series: pd.Series[Any]) -> pd.Series[Any] | None:
    from engine.doctor.impossible import _as_dates  # the same rule for "this column holds dates"

    read = _as_dates(series)
    return None if read is None else read[0]


def split_rows(
    frame: pd.DataFrame,
    rule: SplitRule,
    *,
    primary_key: str | None,
    target: str | None,
    max_failure_pct: float,
    recipe_hash: str | None = None,
) -> SplitParts:
    """Split `frame` (a prepared copy) by `rule`. `frame` is not modified."""
    check_split_rule(rule, columns=[str(c) for c in frame.columns], primary_key=primary_key, target=target)
    series = frame[rule.column]
    point: str | None = None
    later: list[LaterDates] = []
    if rule.by == "date":
        try:
            when = _dates(series, rule.dayfirst, rule.column)
        except CombineError as exc:
            raise SplitError("SPLIT_VALUES_UNREADABLE", exc.message, column=rule.column) from exc
        cut_end = pd.Timestamp(str(rule.cut)) + _DAY  # the whole cut day is on or before it
        readable = when.notna().to_numpy()
        training = readable & (when < cut_end).to_numpy()
        scoring = readable & (when >= cut_end).to_numpy()
        unassigned = int((~readable).sum())
        if len(frame) and unassigned / len(frame) * 100.0 > max_failure_pct:
            raise SplitError(
                "SPLIT_VALUES_UNREADABLE",
                f"{unassigned} of {len(frame)} rows have no readable '{rule.column}' "
                f"({unassigned / len(frame):.1%}), more than the {max_failure_pct:g}% limit.",
                column=rule.column,
            )
        if not bool((when[training] < cut_end).all()):  # pragma: no cover - a guard on the rule above
            raise SplitError("SPLIT_RULE_INVALID", "A training row is dated after the cut: an engine fault.")
        point = f"Every one of the {int(training.sum()):,} training rows has '{rule.column}' on or before {rule.cut}."
        protected = {rule.column, primary_key, target}
        for name in [str(c) for c in frame.columns if str(c) not in protected]:
            if len(later) >= _MAX_OTHER_DATE_COLUMNS:
                break
            dates = _other_dates(frame.loc[training, name])
            if dates is None:
                continue
            rows = int((dates >= cut_end).sum())
            if rows:
                later.append(LaterDates(column=name, rows=rows))
    else:
        wanted = {_text(v) for v in rule.scoring_values}
        hit = series.map(
            lambda v: v is not None and not (isinstance(v, float) and math.isnan(v)) and _text(v) in wanted
        ).to_numpy(dtype=bool)
        scoring, training, unassigned = hit, ~hit, 0
    for name, part in (("learn from", training), ("score", scoring)):
        if not part.any():
            raise SplitError(
                "SPLIT_PART_EMPTY",
                f"The rule leaves no rows to {name}; change the cut or the values.",
                column=rule.column,
            )
    receipt = SplitReceipt(
        rule=rule,
        rule_hash=rule.rule_hash,
        recipe_hash=recipe_hash,
        rows_in=len(frame),
        rows_training=int(training.sum()),
        rows_scoring=int(scoring.sum()),
        rows_unassigned=unassigned,
        point_in_time=point,
        later_dates=tuple(later),
        warnings=("SPLIT_LATER_DATES_IN_TRAINING",) if later else (),
    )
    return SplitParts(
        training=frame[training].reset_index(drop=True),
        scoring=frame[scoring].reset_index(drop=True),
        receipt=receipt,
    )


def split_file(
    frame: pd.DataFrame,
    steps: Sequence[RecipeStep],
    rule: SplitRule,
    *,
    upload_id: str,
    primary_key: str | None,
    target: str | None,
    levels: Sequence[AgentLevel],
    max_failure_pct: float,
    snapshot_column: str | None = None,
    leak_check: LeakCheck | None = None,
    training_upload_id: str | None = None,
    scoring_upload_id: str | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> SplitFileRun:
    """Run the recipe once on `frame`, then split the prepared copy by `rule`: two prepared parts, one recipe."""
    run = run_recipe(
        frame,
        steps,
        upload_id=upload_id,
        primary_key=primary_key,
        target=target,
        levels=levels,
        max_failure_pct=max_failure_pct,
        snapshot_column=snapshot_column,
        leak_check=leak_check,
        clock=clock,
    )
    parts = split_rows(
        run.frame,
        rule,
        primary_key=primary_key,
        target=target,
        max_failure_pct=max_failure_pct,
        recipe_hash=run.receipt.recipe_hash,
    )

    def part(rows: pd.DataFrame, derived: str | None) -> RecipeRun:
        receipt: RecipeReceipt = run.receipt.model_copy(
            update={"rows_out": len(rows), "derived_upload_id": derived}
        )
        return RecipeRun(frame=rows, receipt=receipt)

    return SplitFileRun(
        training=part(parts.training, training_upload_id),
        scoring=part(parts.scoring, scoring_upload_id),
        split=parts.receipt,
    )
