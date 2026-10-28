"""An amount as the outcome: "spend in the N days after the snapshot" (roadmap 2.2, regression goals).

A regression goal predicts a number (what a customer will spend), not a yes or no. The engine trains regression
already (`problem_type: regression`); what a file rarely has is the number itself, so it is worked out from the
order history exactly as the yes/no outcomes are: **point in time**.

* The customers are the ones known at the snapshot (`$as_of`): those with an order dated on or before it.
* The outcome is their total amount over the `window_days` days **after** it (`> $as_of` and `<= $as_of + N`),
  0 for a customer who ordered nothing in the window. The amount of a row is `amount_column`, or
  `amount_column * quantity_column` for a line with a unit price and a quantity. `returns` says what a negative
  row is: `net` (a refund, netted in) or `leave_out` (left out of the sum). Neither is a default: it is a meaning
  (guide R11: a negative may be a refund, ask first).

**The AI proposes the definition; code does not choose it.** `describe_order_file` (`engine.doctor.tools_facts`,
#90) returns the facts of an order file (types, shares read as numbers and dates, negatives, whether a letter prefix
comes with the negative rows), with the old header name lists kept only as hints. The AI reads them and calls
`propose_amount_outcome` (callers: the rules and the AI) with the columns, the window and what returns mean, and a
reason; the person approves the sentence, which says all of it. `offer_amount_outcome` only says that an order file
can define the outcome (and how many negative rows it holds); it names no amount column and no window as decided.
The point-in-time SQL below, `OUTCOME_WINDOW_PAST_DATA` and `amount_minimums` are unchanged.
* It is the ordinary `sql_query` recipe step, proposed through :func:`engine.doctor.tools_sql._propose` with
  `reads_future` and `outcome_window_days`, the same way `propose_features` proposes the features: the trial and
  every run go through `materialize_plan` with the same as-of / `training_as_of` date, the future-rows check
  lets only this outcome column read past the date, and `OUTCOME_WINDOW_PAST_DATA` stops a snapshot whose
  window runs past the end of the data (every customer would read as spending nothing).
* The features of the same customers come from `propose_features` at the same date, so none of them reads the
  window, and a feature may not read the outcome column (`FEATURE_READS_OUTCOME`).

`amount_minimums` is the data floor of a regression goal (see there). Nothing here writes a cell into a message.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import TYPE_CHECKING, Any, Final, Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from api.access_policy import RoutePolicy
from engine.access.roles import Role
from engine.agent.sql_step import table_alias
from engine.config import StrictBase
from engine.doctor.contracts import AmountOutcomeOffer
from engine.doctor.materialize import table_names
from engine.doctor.registry import (
    DataTool,
    DataToolContext,
    DataToolError,
    DataToolKind,
    DataToolRegistry,
    Exposure,
)
from engine.doctor.tools_features import _col, _refuse
from engine.doctor.tools_sql import MAX_OUTPUT_CHARS, ProposeSqlStepArgs, SqlStepProposal, _propose

if TYPE_CHECKING:
    from engine.config import UseCaseConfig
    from engine.doctor.diagnose import LoadedFile

__all__ = [
    "DEFAULT_WINDOW_DAYS",
    "M_AMOUNT_TOOLS",
    "AmountMinimums",
    "ProposeAmountOutcomeArgs",
    "amount_minimums",
    "compile_amount_outcome",
    "offer_amount_outcome",
    "propose_amount_outcome",
    "register_amount_tools",
]

M_AMOUNT_TOOLS: Final[tuple[str, ...]] = ("propose_amount_outcome",)
DEFAULT_WINDOW_DAYS: Final[int] = 90
"""The days a spend goal looks ahead when nothing else is said (the Customer Spend sentence says 90)."""

_ENTITY: Final[str] = "entity_key"
_EVENT_TIME: Final[str] = "event_time"
_AMOUNT: Final[str] = "amount"


class ProposeAmountOutcomeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    upload_id: str = Field(min_length=1, description="The file of orders (one row per order or line).")
    entity_key: str = Field(min_length=1, max_length=128, description="The customer ID column of that file.")
    time_column: str = Field(min_length=1, max_length=128, description="The date of each row.")
    amount_column: str = Field(
        min_length=1,
        max_length=128,
        description="What the row is worth, or its unit price with `quantity_column`.",
    )
    quantity_column: str | None = Field(
        default=None, max_length=128, description="Units on the row: the amount is amount_column times this."
    )
    window_days: int = Field(ge=1, le=3650, description="Days after the snapshot the spend is added up over.")
    returns: Literal["net", "leave_out"] = Field(
        description="A negative row: `net` (a refund, netted in) or `leave_out` (left out of the sum)."
    )
    reason: str | None = Field(
        default=None, max_length=300, description="Why this definition, in a sentence."
    )
    output_name: str = Field(default="outcome_spend", min_length=1, max_length=64)
    outcome_name: str | None = Field(
        default=None, max_length=64, description="The outcome column; default `spend_next_<N>d`."
    )
    as_of: date | None = Field(
        default=None, description="The snapshot date; default: the case's training date."
    )
    as_of_rule: Literal["latest_date_in_file", "last_complete_month_end"] | None = Field(default=None)


def outcome_column_name(given: ProposeAmountOutcomeArgs) -> str:
    return given.outcome_name or f"spend_next_{given.window_days}d"


def compile_amount_outcome(given: ProposeAmountOutcomeArgs, table: str) -> str:
    """The query over the table whose SQL name is `table`: one row per customer known at `$as_of`, with the
    amount spent in the `window_days` days after it. Raises `DataToolError` for a name that cannot be quoted.
    """
    key, when, amount = _col(given.entity_key), _col(given.time_column), _col(given.amount_column)
    outcome = _col(outcome_column_name(given))
    days = given.window_days
    worth = f"TRY_CAST({amount} AS DOUBLE)"
    if given.quantity_column:
        worth = f"{worth} * TRY_CAST({_col(given.quantity_column)} AS DOUBLE)"
    kept = f" AND ({worth}) >= 0" if given.returns == "leave_out" else ""
    return (
        f"WITH ev AS (SELECT {key} AS {_ENTITY}, TRY_CAST({when} AS DATE) AS {_EVENT_TIME}, "
        f"{worth} AS {_AMOUNT} FROM {table} "
        f"WHERE NULLIF(TRIM(CAST({key} AS VARCHAR)), '') IS NOT NULL AND TRY_CAST({when} AS DATE) IS NOT NULL{kept}), "
        f"known AS (SELECT DISTINCT {_ENTITY} FROM ev WHERE {_EVENT_TIME} <= CAST($as_of AS DATE)) "
        f"SELECT k.{_ENTITY} AS {key}, COALESCE(SUM(e.{_AMOUNT}), 0.0) AS {outcome} "
        f'FROM "known" k LEFT JOIN "ev" e ON e.{_ENTITY} = k.{_ENTITY} '
        f"AND e.{_EVENT_TIME} > CAST($as_of AS DATE) AND e.{_EVENT_TIME} <= CAST($as_of + {days} AS DATE) "
        f"GROUP BY k.{_ENTITY} ORDER BY k.{_ENTITY}"
    )


def propose_amount_outcome(given: ProposeAmountOutcomeArgs, ctx: DataToolContext) -> SqlStepProposal:
    """Propose the spend outcome as one pending `sql_query` step (never ticked: the person approves the plan)."""
    loaded = {f.upload_id: f for f in ctx.files}
    if given.upload_id not in loaded:
        raise DataToolError("TOOL_INPUT_INVALID", "That file is not in this case.", status=422)
    if given.as_of is None and given.as_of_rule is None and ctx.as_of is None and ctx.training_as_of is None:
        raise _refuse(
            "OUTCOME_NEEDS_AS_OF",
            "The spend is added up after a snapshot date: ask the person for it, or name `as_of_rule`.",
        )
    table = table_names(ctx.files)[given.upload_id]
    sql_args = ProposeSqlStepArgs(
        description=amount_sentence(given)[:300],
        sql=compile_amount_outcome(given, table_alias(table)),
        output_name=given.output_name,
        upload_ids=(given.upload_id,),
        as_of=given.as_of,
        as_of_rule=given.as_of_rule,
        as_of_upload_id=given.upload_id if given.as_of_rule else None,
        as_of_column=given.time_column if given.as_of_rule else None,
        reads_future=True,
        outcome_window_days=given.window_days,
    )
    return _propose(sql_args, ctx)


def amount_sentence(given: ProposeAmountOutcomeArgs) -> str:
    """What the person approves, in plain words: the amount, the window, what a negative row is, who counts."""
    worth = (
        f"{given.amount_column} times {given.quantity_column}"
        if given.quantity_column
        else given.amount_column
    )
    returns = "refunds netted in" if given.returns == "net" else "negative rows left out"
    return (
        f"Spend in the {given.window_days} days after the snapshot, per {given.entity_key}: {worth} added up "
        f"({returns}), 0 when none; customers with an order on or before the snapshot."
    )


class AmountMinimums(StrictBase):
    """Whether an amount outcome has enough to learn from, with the numbers."""

    enough: bool
    rows: int
    non_zero: int
    min_rows: int
    min_non_zero: int
    message: str


def amount_minimums(outcome: pd.Series[Any], config: UseCaseConfig) -> AmountMinimums:
    """The floor of a regression goal: `validation.min_rows` customers and `validation.min_positive` of them with
    a non-zero outcome (1,000 and 200 today, the same two numbers a yes/no goal needs).

    Why 200 non-zero amounts: spend is mostly zeros (most customers buy nothing in a window). A model of the amount
    learns from the customers who spent, exactly as a yes/no model learns from its yeses, so a customer who spent
    plays the part of a positive and the same floor applies: below it the amounts the model sees are too few to tell
    a big spender from noise, and the error measured on the held-out rows would swing with a handful of customers.
    Using the configured number (not a second constant) keeps one rule for the person to understand and change.
    """
    values = pd.to_numeric(outcome, errors="coerce").dropna()
    rows, non_zero = len(values), int((values != 0).sum())
    need_rows, need_non_zero = config.validation.min_rows, config.validation.min_positive
    problems: list[str] = []
    if rows < need_rows:
        problems.append(f"{rows:,} customers, and the model needs at least {need_rows:,}")
    if non_zero < need_non_zero:
        problems.append(f"{non_zero:,} customers who spent, and the model needs at least {need_non_zero:,}")
    message = (
        f"{rows:,} customers, {non_zero:,} of them with spend: enough to train on."
        if not problems
        else "Only " + " and ".join(problems) + "."
    )
    return AmountMinimums(
        enough=not problems,
        rows=rows,
        non_zero=non_zero,
        min_rows=need_rows,
        min_non_zero=need_non_zero,
        message=message,
    )


def offer_amount_outcome(files: Sequence[LoadedFile], config: UseCaseConfig) -> AmountOutcomeOffer | None:
    """For an amount goal that no file carries the outcome of: a file of orders that can define it, or None.

    A file qualifies by its facts (`engine.doctor.tools_facts.order_file_facts`): a column whose values repeat (a
    customer per order), a column read as dates and a column read as numbers. Nothing is chosen here: the sentence
    says the outcome can be built and what the AI and the person settle (which amount, how many days, what a negative
    row is); the column hints ride along as hints. The AI proposes the definition with `propose_amount_outcome`.
    """
    from engine.doctor.tools_facts import order_file_facts

    entity = config.entity or "customer"
    for file in files:
        if file.frame.empty:
            continue
        facts = order_file_facts(file.upload_id, file.frame)
        keys = sorted(
            (c for c in facts.columns if c.rows_per_value > 1 and c.dates_share < 0.8),
            key=lambda c: (c.hint != "id", c.numbers_share >= 0.8),
        )
        dates = [c for c in facts.columns if c.dates_share >= 0.8]
        key = keys[0] if keys else None
        numbers = [
            c for c in facts.columns if c.numbers_share >= 0.8 and key is not None and c.column != key.column
        ]
        if key is None or not (dates and numbers):
            continue
        hinted = sorted(numbers, key=lambda c: c.hint != "amount")
        negatives = (
            f" It has {facts.negative_rows:,} rows with a negative number: say whether they are refunds."
            if facts.negative_rows
            else ""
        )
        return AmountOutcomeOffer(
            upload_id=file.upload_id,
            entity_key=key.column,
            time_column=next((c.column for c in dates if c.hint == "date"), dates[0].column),
            amount_column=hinted[0].column,
            window_days=DEFAULT_WINDOW_DAYS,
            outcome_name=config.target.column or f"spend_next_{DEFAULT_WINDOW_DAYS}d",
            amount_candidates=tuple(c.column for c in hinted[:8]),
            sentence=(
                f"{file.file_name} has an order per row, so the spend of each {entity} after the snapshot date can "
                "be built from it. Ask Data Doctor to propose it: which amount (or quantity times price), over how "
                f"many days, and who counts; you approve the sentence.{negatives}"
            ),
        )
    return None


def register_amount_tools(registry: DataToolRegistry) -> tuple[str, ...]:
    """Register `propose_amount_outcome` (catalogue #88), for the rules and the AI; its names."""
    registry.register(
        DataTool(
            number=88,
            name="propose_amount_outcome",
            kind=DataToolKind.PROPOSE,
            description=(
                "Propose the spend in the N days after the snapshot date, per customer, from a file of orders: "
                "an amount, not a yes or no, 0 for a customer who bought nothing. You choose the amount (or "
                "quantity times price), the window and whether a negative row is a refund, from "
                "`describe_order_file`, with a reason. Only the outcome column reads past the date; nothing is "
                "changed until the person approves the plan."
            ),
            input_model=ProposeAmountOutcomeArgs,
            output_model=SqlStepProposal,
            route_policy=RoutePolicy(
                role=Role.ANALYST,
                action="doctor.propose_amount_outcome",
                purpose="ask Data Doctor for a suggestion",
            ),
            exposure=Exposure.SESSION_ONLY,
            run=propose_amount_outcome,
            callers=frozenset({"rules", "ai"}),
            needs=("files",),
            max_output_bytes=MAX_OUTPUT_CHARS,
            policy_kind="data.propose_amount_outcome",
            wraps="engine/agent/sql_step.py run_sql (DuckDB, sandboxed); the sql_query recipe step",
        )
    )
    return M_AMOUNT_TOOLS
