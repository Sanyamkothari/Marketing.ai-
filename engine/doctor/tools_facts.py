"""Facts for the AI to read before it proposes a meaning (catalogue #90 and #91), names and counts only.

Two goals used to be decided by header name lists: which columns of an order file are the customer, the date and the
amount of a spend outcome (`engine.doctor.amount_outcome`), and which column of a campaign file says who got the
campaign and which of its groups is the control group (`engine.doctor.uplift_scope`). A name list is a reading that
grows with every new spelling, and it decided what "spend" means (`UnitPrice` summed instead of `Quantity * UnitPrice`)
or hid a goal (`email_sent` holds the test, but no name says so). These two read tools return the facts instead, with
the old name lists kept only as hints; the AI reads them, proposes the meaning with its reason, and the person
approves it.

* `describe_order_file` (#90): per column, the type as read, the share that reads as numbers and as dates, how often a
  value repeats, the count of negative numbers, and a name hint; and, for a text column whose values carry a letter
  prefix, whether that prefix comes with the negative rows (a cancelled invoice: `C` before the number). Shares only.
* `find_test_columns` (#91): the columns with 2 to 10 groups, each group by a level id (`L1`, `L2`, ...) with its
  count and whether its name reads like a control group, and whether the goal's outcome is in the file. The groups'
  own names are cell text, so the model gets their ids; the person sees them in the question.

Neither tool changes anything, and neither returns a cell value.
"""

from __future__ import annotations

import re
from typing import Final

import pandas as pd
from pydantic import Field

from api.access_policy import RoutePolicy
from engine.access.roles import Role
from engine.config import ProblemType, StrictBase, UseCaseConfig
from engine.doctor.registry import (
    DataTool,
    DataToolContext,
    DataToolError,
    DataToolKind,
    DataToolRegistry,
    Exposure,
)
from engine.doctor.tools import FileArgs, _config, _file_or_files, _loaded

__all__ = [
    "M_FACT_TOOLS",
    "CampaignColumn",
    "CampaignColumns",
    "OrderColumn",
    "OrderFileFacts",
    "campaign_columns",
    "level_labels",
    "order_file_facts",
    "register_fact_tools",
]

M_FACT_TOOLS: Final[tuple[str, ...]] = ("describe_order_file", "find_test_columns")

AMOUNT_HINT: Final[re.Pattern[str]] = re.compile(
    r"amount|spend|revenue|sales|total|price|paid|payment|value", re.IGNORECASE
)
QUANTITY_HINT: Final[re.Pattern[str]] = re.compile(r"count|qty|quantity|units|number", re.IGNORECASE)
DATE_HINT: Final[re.Pattern[str]] = re.compile(r"date|time|ordered|purchase|placed|day", re.IGNORECASE)
ID_HINT: Final[re.Pattern[str]] = re.compile(
    r"(^|[^a-z])(id|key|code|no|number)$|^(customer|client|user|account)", re.IGNORECASE
)
_PREFIXED: Final[re.Pattern[str]] = re.compile(r"^\s*[A-Za-z]{1,3}\d")
MAX_COLUMNS: Final[int] = 60
MAX_TEST_LEVELS: Final[int] = 10


class OrderColumn(StrictBase):
    column: str
    dtype: str = Field(description="The type as read.")
    numbers_share: float = Field(description="Share of filled cells that read as numbers.")
    dates_share: float = Field(description="Share of filled cells that read as dates.")
    distinct: int
    rows_per_value: float = Field(
        description="Rows per distinct value: above 1, a value repeats (an ID per order)."
    )
    negatives: int = Field(default=0, description="Cells below zero (numbers only).")
    hint: str | None = Field(
        default=None,
        description="What the name suggests (amount, quantity, date, id): a hint, not a decision.",
    )
    prefix_on_negatives: float | None = Field(
        default=None,
        description="Text only: share of the negative rows whose value starts with letters before a number.",
    )
    prefix_elsewhere: float | None = Field(
        default=None,
        description="The same share on the other rows (a prefix only on negatives: a cancellation).",
    )


class OrderFileFacts(StrictBase):
    upload_id: str
    rows: int
    negative_rows: int = Field(
        description="Rows where any number is below zero (a refund or a cancellation?)."
    )
    columns: tuple[OrderColumn, ...]


class CampaignLevel(StrictBase):
    level: str = Field(description="The group's id in this answer (`L1`, `L2`, ...), in the file's order.")
    rows: int
    control_hint: bool = Field(description="Its name reads like a control group: a hint, not a decision.")


class CampaignColumn(StrictBase):
    column: str
    levels: tuple[CampaignLevel, ...]
    name_hint: bool = Field(description="The column is named like a test assignment: a hint, not a decision.")
    configured: bool = Field(description="The use case names this column as the test's.")


class CampaignColumns(StrictBase):
    upload_id: str
    outcome_in_file: bool = Field(description="The goal's outcome column is in this file.")
    columns: tuple[CampaignColumn, ...] = Field(description="Columns with 2 to 10 groups, hinted ones first.")


def _share(parsed: pd.Series, raw: pd.Series) -> float:
    filled = int(raw.notna().sum())
    return round(float(parsed.notna().sum()) / filled, 3) if filled else 0.0


def _hint(name: str) -> str | None:
    if DATE_HINT.search(name):
        return "date"
    if ID_HINT.search(name):
        return "id"
    if QUANTITY_HINT.search(name) and not AMOUNT_HINT.search(name):
        return "quantity"
    if AMOUNT_HINT.search(name):
        return "amount"
    return None


def order_file_facts(upload_id: str, frame: pd.DataFrame) -> OrderFileFacts:
    """The facts of one file of orders: per column, and the rows with a negative number."""
    numbers = {str(c): pd.to_numeric(frame[c], errors="coerce") for c in frame.columns}
    negative = pd.Series(False, index=frame.index)
    for name, parsed in numbers.items():
        if _share(parsed, frame[name]) >= 0.8:
            negative |= parsed.lt(0).fillna(False)
    rows = len(frame)
    out: list[OrderColumn] = []
    for raw in list(frame.columns)[:MAX_COLUMNS]:
        name = str(raw)
        series = frame[raw]
        parsed = numbers[name]
        is_number = _share(parsed, series) >= 0.8
        dates = (
            pd.Series(dtype="datetime64[ns]")
            if is_number
            else pd.to_datetime(series, errors="coerce", format="mixed")
        )
        distinct = int(series.nunique(dropna=True))
        on_neg = elsewhere = None
        if not pd.api.types.is_numeric_dtype(series) and negative.any():
            prefixed = series.astype("string").str.match(_PREFIXED).fillna(False).astype(bool)
            if prefixed.any():
                on_neg = round(float(prefixed[negative].mean()), 3)
                rest = prefixed[~negative]
                elsewhere = round(float(rest.mean()), 3) if len(rest) else 0.0
        out.append(
            OrderColumn(
                column=name,
                dtype=str(series.dtype),
                numbers_share=_share(parsed, series),
                dates_share=0.0 if is_number else _share(dates, series),
                distinct=distinct,
                rows_per_value=round(rows / distinct, 2) if distinct else 0.0,
                negatives=int(parsed.lt(0).sum()) if is_number else 0,
                hint=_hint(name),
                prefix_on_negatives=on_neg,
                prefix_elsewhere=elsewhere,
            )
        )
    return OrderFileFacts(
        upload_id=upload_id, rows=rows, negative_rows=int(negative.sum()), columns=tuple(out)
    )


def level_labels(series: pd.Series) -> list[str]:
    """The groups of a column as text, in the file's order of first appearance: `L1` is the first of them."""
    from engine.doctor.uplift_scope import _text

    return list(dict.fromkeys(series.dropna().map(_text).tolist()))


def campaign_columns(upload_id: str, frame: pd.DataFrame, config: UseCaseConfig) -> CampaignColumns:
    """The columns that could hold a campaign test's groups, with the facts to choose one (no group name)."""
    from engine.doctor.diagnose import outcome_columns
    from engine.doctor.uplift_scope import _CONTROL_NAME, _TREATMENT_NAME, _text

    outcomes = {c.column for c in outcome_columns(frame, config)}
    found: list[CampaignColumn] = []
    for raw in frame.columns:
        name = str(raw)
        if name in outcomes:
            continue
        labels = level_labels(frame[raw])
        if not 2 <= len(labels) <= MAX_TEST_LEVELS:
            continue
        counts = frame[raw].dropna().map(_text).value_counts()
        found.append(
            CampaignColumn(
                column=name,
                levels=tuple(
                    CampaignLevel(
                        level=f"L{n}",
                        rows=int(counts.get(label, 0)),
                        control_hint=bool(_CONTROL_NAME.search(label)),
                    )
                    for n, label in enumerate(labels, start=1)
                ),
                name_hint=bool(_TREATMENT_NAME.match(name)),
                configured=name == config.uplift.treatment_column,
            )
        )
    found.sort(key=lambda c: (not c.configured, not c.name_hint, not any(lv.control_hint for lv in c.levels)))
    return CampaignColumns(upload_id=upload_id, outcome_in_file=bool(outcomes), columns=tuple(found))


def _uplift_config(ctx: DataToolContext) -> UseCaseConfig:
    for offer in ctx.offers:
        if offer.config.problem_type is ProblemType.UPLIFT or offer.config.uplift.treatment_column:
            return offer.config
    config: UseCaseConfig = _config(ctx)
    return config


def _describe_order_file(given: FileArgs, ctx: DataToolContext) -> OrderFileFacts:
    loaded = _loaded(ctx, given.upload_id)
    if loaded.frame.empty:
        raise DataToolError("TOOL_INPUT_INVALID", "The file has no rows.", status=422)
    return order_file_facts(loaded.upload_id, loaded.frame)


def _find_test_columns(given: FileArgs, ctx: DataToolContext) -> CampaignColumns:
    loaded = _loaded(ctx, given.upload_id)
    return campaign_columns(loaded.upload_id, loaded.frame, _uplift_config(ctx))


def register_fact_tools(registry: DataToolRegistry) -> tuple[str, ...]:
    """Register `describe_order_file` (#90) and `find_test_columns` (#91), for the rules, the AI and the person."""
    registry.register(
        DataTool(
            number=90,
            name="describe_order_file",
            kind=DataToolKind.READ,
            description=(
                "Facts of a file of orders, before you propose a spend outcome: per column the type, the share "
                "read as numbers and as dates, how often a value repeats, negatives, a name hint; and whether a "
                "letter prefix comes with the negative rows (a cancellation). Changes nothing."
            ),
            input_model=FileArgs,
            output_model=OrderFileFacts,
            route_policy=RoutePolicy(
                role=Role.ANALYST, action="doctor.describe_order_file", purpose="read a file of orders"
            ),
            exposure=Exposure.SESSION_ONLY,
            run=_describe_order_file,
            callers=frozenset({"rules", "ai", "person"}),
            available=_file_or_files,
            wraps="engine/doctor/amount_outcome.py (the column hints that chose the spend columns before)",
        )
    )
    registry.register(
        DataTool(
            number=91,
            name="find_test_columns",
            kind=DataToolKind.READ,
            description=(
                "The columns that could say who got a campaign: 2 to 10 groups each, every group by id with its "
                "count and whether its name reads like a control group, and whether the outcome is in the file. "
                "Propose the column and the control group with `propose_test_column`. Changes nothing."
            ),
            input_model=FileArgs,
            output_model=CampaignColumns,
            route_policy=RoutePolicy(
                role=Role.ANALYST, action="doctor.find_test_columns", purpose="find a campaign test's groups"
            ),
            exposure=Exposure.SESSION_ONLY,
            run=_find_test_columns,
            callers=frozenset({"rules", "ai", "person"}),
            available=_file_or_files,
            wraps="engine/doctor/uplift_scope.py treatment_column_of, control_level (their names as hints)",
        )
    )
    return M_FACT_TOOLS
