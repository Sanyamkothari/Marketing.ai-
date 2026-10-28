"""An amount as the outcome: "spend in the N days after the snapshot" (roadmap 2.2, regression goals).

A regression goal predicts a number (what a customer will spend), not a yes or no. The engine trains regression
already (`problem_type: regression`); what a file rarely has is the number itself, so it is worked out from the
order history exactly as the yes/no outcomes are: **point in time**.

* The customers are the ones known at the snapshot (`$as_of`): those with an order dated on or before it.
* The outcome is their total amount over the `window_days` days **after** it (`> $as_of` and `<= $as_of + N`),
  0 for a customer who ordered nothing in the window. Refunds (negative amounts) are netted in.
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

import re
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
    amount_column: str = Field(min_length=1, max_length=128, description="What the row is worth.")
    window_days: int = Field(ge=1, le=3650, description="Days after the snapshot the spend is added up over.")
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
    return (
        f"WITH ev AS (SELECT {key} AS {_ENTITY}, TRY_CAST({when} AS DATE) AS {_EVENT_TIME}, "
        f"TRY_CAST({amount} AS DOUBLE) AS {_AMOUNT} FROM {table} "
        f"WHERE NULLIF(TRIM(CAST({key} AS VARCHAR)), '') IS NOT NULL AND TRY_CAST({when} AS DATE) IS NOT NULL), "
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
        description=(
            f"Spend in the {given.window_days} days after the snapshot, per {given.entity_key} "
            f"(customers with an order on or before it; {given.amount_column} added up, 0 when none)."
        )[:300],
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


_AMOUNT_NAME: Final[re.Pattern[str]] = re.compile(
    r"amount|spend|revenue|sales|total|price|paid|payment|value", re.IGNORECASE
)
_NOT_AMOUNT_NAME: Final[re.Pattern[str]] = re.compile(
    r"count|qty|quantity|id$|number|rate|pct|percent", re.IGNORECASE
)
_DATE_NAME: Final[re.Pattern[str]] = re.compile(r"date|time|ordered|purchase|placed|day", re.IGNORECASE)
_ID_NAME: Final[re.Pattern[str]] = re.compile(
    r"(^|[^a-z])(id|key|code)$|^(customer|client|user|account)$", re.IGNORECASE
)
_READABLE: Final[float] = 0.8
"""A column counts as numbers or dates when this share of its filled cells reads as one."""


def _readable_share(parsed: pd.Series[Any], raw: pd.Series[Any]) -> float:
    filled = int(raw.notna().sum())
    return float(parsed.notna().sum()) / filled if filled else 0.0


def _order_columns(file: LoadedFile, entity: str) -> tuple[str, str, str] | None:
    """`(customer ID, date, amount)` columns of a file with a row per order, or None when it is not one."""
    frame = file.frame
    if frame.empty:
        return None
    amounts = [
        c
        for c in frame.columns
        if _AMOUNT_NAME.search(str(c))
        and not _NOT_AMOUNT_NAME.search(str(c))
        and _readable_share(pd.to_numeric(frame[c], errors="coerce"), frame[c]) >= _READABLE
    ]
    dates = [
        c
        for c in frame.columns
        if _DATE_NAME.search(str(c))
        and c not in amounts
        and _readable_share(pd.to_datetime(frame[c], errors="coerce", format="mixed"), frame[c]) >= _READABLE
    ]
    keys = [
        c
        for c in frame.columns
        if c not in amounts
        and c not in dates
        and (_ID_NAME.search(str(c)) or entity in str(c).lower())
        and frame[c].notna().mean() >= 0.5
        and frame[c].nunique()
        < frame[c].notna().sum()  # customers repeat: one row per order, not per customer
    ]
    if not (amounts and dates and keys):
        return None
    keys.sort(key=lambda c: (entity not in str(c).lower(), list(frame.columns).index(c)))
    return str(keys[0]), str(dates[0]), str(amounts[0])


def offer_amount_outcome(files: Sequence[LoadedFile], config: UseCaseConfig) -> AmountOutcomeOffer | None:
    """For an amount goal that no file carries the outcome of: the order file that can define it, or None.

    A file qualifies with a customer ID that repeats, a date column and an amount column. The outcome is named as
    the goal's own target column, so Guided setup finds it. Nothing is proposed here; the person is shown the
    sentence and `propose_amount_outcome` builds the pending step once they agree.
    """
    entity = config.entity or "customer"
    for file in files:
        found = _order_columns(file, entity)
        if found is None:
            continue
        key, when, amount = found
        outcome = config.target.column or f"spend_next_{DEFAULT_WINDOW_DAYS}d"
        return AmountOutcomeOffer(
            upload_id=file.upload_id,
            entity_key=key,
            time_column=when,
            amount_column=amount,
            window_days=DEFAULT_WINDOW_DAYS,
            outcome_name=outcome,
            sentence=(
                f"{file.file_name} has an order per row. I can add up {amount} in the {DEFAULT_WINDOW_DAYS} days "
                f"after the snapshot date for each {entity} ({key}), 0 for one who bought nothing, as {outcome}."
            ),
        )
    return None


def register_amount_tools(registry: DataToolRegistry) -> tuple[str, ...]:
    """Register `propose_amount_outcome` (catalogue #87), for the rules; its names."""
    registry.register(
        DataTool(
            number=87,
            name="propose_amount_outcome",
            kind=DataToolKind.PROPOSE,
            description=(
                "Propose the spend in the N days after the snapshot date, per customer, from a file of orders: "
                "an amount, not a yes or no, 0 for a customer who bought nothing. Only the outcome column reads "
                "past the date. Shown as a plain description with the rows made; nothing is changed until the "
                "person approves the plan."
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
            callers=frozenset({"rules"}),
            needs=("files",),
            max_output_bytes=MAX_OUTPUT_CHARS,
            policy_kind="data.propose_amount_outcome",
            wraps="engine/agent/sql_step.py run_sql (DuckDB, sandboxed); the sql_query recipe step",
        )
    )
    return M_AMOUNT_TOOLS
