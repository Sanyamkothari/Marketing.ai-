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
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

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

if TYPE_CHECKING:
    from engine.doctor.diagnose import LoadedFile

__all__ = [
    "M_FACT_TOOLS",
    "CampaignColumn",
    "CampaignColumns",
    "ConsentFacts",
    "FactQuestion",
    "FactSuggestion",
    "GoalBuildFacts",
    "KeyFacts",
    "OrderColumn",
    "OrderFileFacts",
    "TimeFacts",
    "campaign_columns",
    "extract_consent_facts",
    "extract_goal_build_facts",
    "extract_key_facts",
    "extract_time_facts",
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


# ---------------------------------------------------------------------------
# Fact Models & Extractors (converting rule writers to read-only fact tools)
# ---------------------------------------------------------------------------


class FactSuggestion(StrictBase):
    kind: str
    payload: dict[str, Any]
    title: str
    reason: str
    confidence: str = "sure"
    ticked: bool = False
    needs_yes: bool = False
    upload_id: str | None = None
    goal_id: str | None = None


class FactQuestion(StrictBase):
    code: str
    text: str
    choices: tuple[tuple[str, dict[str, Any]], ...]
    upload_id: str | None = None
    column: str | None = None
    goal_id: str | None = None
    blocking: bool = True
    recommended: str | None = None
    why: str = ""
    safe: str | None = None


class KeyFacts(StrictBase):
    upload_id: str
    unique_keys: tuple[dict[str, Any], ...] = ()
    near_keys: tuple[dict[str, Any], ...] = ()
    is_event_file: bool = False
    candidate_key: str | None = None
    candidate_customer_column: str | None = None
    suggestions: tuple[FactSuggestion, ...] = ()
    questions: tuple[FactQuestion, ...] = ()


class GoalBuildFacts(StrictBase):
    goal_id: str
    buildable_outcome: Any | None = None
    enough: bool = False
    snapshot: date | None = None
    window_days: int = 0
    audiences: tuple[Any, ...] = ()
    suggested_build: FactSuggestion | None = None
    audience_question: FactQuestion | None = None


class TimeFacts(StrictBase):
    upload_id: str
    time_columns: tuple[str, ...] = ()
    partial_month: bool = False
    partial_rows: int = 0
    last_month_end: str | None = None
    suggested_as_of: str | None = None
    missing: bool = False
    suggestions: tuple[FactSuggestion, ...] = ()
    questions: tuple[FactQuestion, ...] = ()


class ConsentFacts(StrictBase):
    upload_id: str
    consent_columns: tuple[str, ...] = ()
    missing: bool = False
    suggestions: tuple[FactSuggestion, ...] = ()


_ID_NAMED: Final[re.Pattern[str]] = re.compile(
    r"(^|[_\s-])id$|^id[_\s-]|_key$|(customer|cust|client|member|account)[_\s-]?(id|no|num|number|code|ref)$",
    re.I,
)


def id_named(unique: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    named = [key for key in unique if _ID_NAMED.search(str(key.get("column", "")).strip())]
    return named or list(unique)


def dates_column(series: pd.Series) -> bool:
    from engine.onboarding.sources import MIN_TIME_PARSE_RATE

    seen = series.dropna().astype(str).head(500)
    if seen.empty or seen.str.contains(r"\d[-/:.]\d", regex=True).mean() < MIN_TIME_PARSE_RATE:
        return False
    return bool(pd.to_datetime(seen, errors="coerce", format="mixed").notna().mean() >= MIN_TIME_PARSE_RATE)


def near_keys(frame: pd.DataFrame, profile: Any) -> list[dict[str, Any]]:
    from engine.onboarding.sources import key_candidates

    found: list[dict[str, Any]] = []
    for candidate in key_candidates(profile):
        key = candidate.model_dump(mode="json")
        col = str(key["column"])
        if col not in frame.columns:
            continue
        named = bool(_ID_NAMED.search(col.strip()))
        series = frame[col]
        text = not (pd.api.types.is_numeric_dtype(series) or pd.api.types.is_bool_dtype(series))
        repeats = float(key.get("rows_per_key") or 99.0)
        blanks = float(key.get("null_rate") or 0.0)
        if named or (
            text and repeats <= 50.0 and blanks < 0.5 and series.nunique() > 1 and not dates_column(series)
        ):
            found.append(key)
    found.sort(
        key=lambda k: (not _ID_NAMED.search(str(k["column"]).strip()), float(k.get("rows_per_key") or 99.0))
    )
    return found


def event_file(roles: dict[str, Any], event_roles: frozenset[str]) -> bool:
    ranked = [r for r in roles.get("roles", []) if isinstance(r, dict)]
    if not ranked or ranked[0].get("role") not in event_roles:
        return False
    top = ranked[0].get("confidence")
    second = ranked[1].get("confidence") if len(ranked) > 1 else None
    if not isinstance(top, (int, float)):
        return False
    return not isinstance(second, (int, float)) or top > second


def extract_key_facts(
    loaded: LoadedFile,
    event_roles: frozenset[str] = frozenset(),
    roles_output: dict[str, Any] | None = None,
) -> KeyFacts:
    from engine.onboarding.sources import key_candidates

    raw_candidates = [c.model_dump(mode="json") for c in key_candidates(loaded.profile)]
    unique = [
        k
        for k in raw_candidates
        if float(k.get("rows_per_key") or 99.0) <= 1.0 and float(k.get("null_rate") or 0.0) < 0.05
    ]
    unique = id_named(unique)
    near = near_keys(loaded.frame, loaded.profile)
    is_events = event_file(roles_output or {}, event_roles) if event_roles else False

    suggestions: list[FactSuggestion] = []
    questions: list[FactQuestion] = []
    candidate_key: str | None = None

    name = loaded.file_name or "this file"
    if len(unique) == 1:
        col = str(unique[0]["column"])
        candidate_key = col
        suggestions.append(
            FactSuggestion(
                kind="key",
                payload={"upload_id": loaded.upload_id, "column": col},
                title=f"“{col}” names each row of {name}.",
                reason=f"“{col}” has a unique value on every row and no missing values.",
                confidence="sure",
                ticked=True,
                upload_id=loaded.upload_id,
            )
        )
    elif len(unique) > 1:
        choices: tuple[tuple[str, dict[str, Any]], ...] = (
            *(
                (
                    str(k["column"]),
                    {
                        "option_id": f"c{i}",
                        "kind": "key",
                        "payload": {"upload_id": loaded.upload_id, "column": str(k["column"])},
                        "title": f"“{k['column']}” names each row of {name}.",
                    },
                )
                for i, k in enumerate(unique[:5])
            ),
            ("I don't know", {"option_id": "none"}),
        )
        questions.append(
            FactQuestion(
                code="KEY_AMBIGUOUS",
                text=f"Which column names each row of {name}? Several hold a different value on every row.",
                choices=choices,
                upload_id=loaded.upload_id,
            )
        )
    elif len(loaded.frame) > 0:
        if near:
            near_choices: tuple[tuple[str, dict[str, Any]], ...] = (
                *(
                    (
                        f"{k['column']} (repeats ~{float(k.get('rows_per_key') or 1):.1f} times"
                        + (
                            f"; {float(k.get('null_rate') or 0.0):.0%} blank"
                            if float(k.get("null_rate") or 0.0) >= 0.005
                            else ""
                        )
                        + ")",
                        {
                            "option_id": f"c{i}",
                            "kind": "key",
                            "payload": {"upload_id": loaded.upload_id, "column": str(k["column"])},
                            "title": f"“{k['column']}” names the customers of {name}.",
                        },
                    )
                    for i, k in enumerate(near[:5])
                ),
                ("I don't know", {"option_id": "none"}),
            )
            questions.append(
                FactQuestion(
                    code="KEY_NOT_UNIQUE",
                    text=(
                        f"{name} has many rows for each customer, so no column is different on every row. "
                        "Which column names the customer on each row?"
                        if is_events
                        else f"No column of {name} holds a different value on every row with no blanks. "
                        "Which column names the customers in it?"
                    ),
                    choices=near_choices,
                    upload_id=loaded.upload_id,
                )
            )

    return KeyFacts(
        upload_id=loaded.upload_id,
        unique_keys=tuple(unique),
        near_keys=tuple(near),
        is_event_file=is_events,
        candidate_key=candidate_key,
        suggestions=tuple(suggestions),
        questions=tuple(questions),
    )


def extract_goal_build_facts(
    files: Sequence[LoadedFile],
    config: UseCaseConfig,
    config_root: Path | None = None,
) -> GoalBuildFacts:
    from engine.agent.sql_step import table_alias
    from engine.doctor.buildable import buildable_outcome, compile_build
    from engine.doctor.materialize import table_names

    built = buildable_outcome(files, config, config_root)
    if built is None or not built.enough or built.snapshot is None:
        return GoalBuildFacts(goal_id=config.id, enough=False)

    loaded = next((f for f in files if f.upload_id == built.upload_id), None)
    if loaded is None:
        return GoalBuildFacts(goal_id=config.id, enough=False)

    suggested_build: FactSuggestion | None = None
    audience_question: FactQuestion | None = None

    table = table_names(files)[loaded.upload_id]
    try:
        sql, description = compile_build(built, config, table_alias(table), None)
        suggested_build = FactSuggestion(
            kind="recipe_step",
            payload={
                "description": description,
                "sql": sql,
                "upload_ids": [loaded.upload_id],
                "as_of": built.snapshot.isoformat(),
                "reads_future": True,
                "outcome_window_days": built.window_days,
                "buildable": built.model_dump(mode="json"),
                "needs_yes": True,
            },
            title=f"Build training table for {config.name}",
            reason=f"Builds one row per customer with the outcome from the event log around {built.snapshot}.",
            confidence="check",
            ticked=True,
            needs_yes=True,
            upload_id=loaded.upload_id,
            goal_id=config.id,
        )
    except Exception:
        suggested_build = None

    return GoalBuildFacts(
        goal_id=config.id,
        buildable_outcome=built,
        enough=True,
        snapshot=built.snapshot,
        window_days=built.window_days,
        audiences=tuple(built.audiences),
        suggested_build=suggested_build,
        audience_question=audience_question,
    )


def extract_time_facts(
    loaded: LoadedFile,
    brief_time_names: Sequence[str] = (),
    required: bool = False,
    goal_id: str | None = None,
) -> TimeFacts:
    time_cols: list[str] = []
    partial_month = False
    partial_rows = 0
    last_month_end_str: str | None = None

    for col in loaded.frame.columns:
        series = loaded.frame[col]
        if dates_column(series):
            time_cols.append(str(col))
            # Check partial month on the primary date column
            if not partial_month:
                parsed_dates = pd.to_datetime(series.dropna(), errors="coerce", format="mixed")
                if not parsed_dates.empty:
                    max_dt = parsed_dates.max()
                    # Month end for max_dt
                    period = pd.Period(max_dt, freq="M")
                    m_end = period.end_time.date()
                    if max_dt.date() < m_end:
                        # Partial month
                        prev_m_end = (period.start_time - pd.Timedelta(days=1)).date()
                        p_rows = int((parsed_dates > pd.Timestamp(prev_m_end)).sum())
                        if p_rows > 0:
                            partial_month = True
                            partial_rows = p_rows
                            last_month_end_str = prev_m_end.isoformat()

    suggestions: list[FactSuggestion] = []
    if time_cols:
        target_col = next((c for c in time_cols if c in brief_time_names), time_cols[0])
        suggestions.append(
            FactSuggestion(
                kind="as_of_column",
                payload={"upload_id": loaded.upload_id, "column": target_col},
                title=f"“{target_col}” is the date each row describes.",
                reason="Testing splits by date and uses the newest dates.",
                confidence="sure",
                ticked=True,
                upload_id=loaded.upload_id,
                goal_id=goal_id,
            )
        )

    return TimeFacts(
        upload_id=loaded.upload_id,
        time_columns=tuple(time_cols),
        partial_month=partial_month,
        partial_rows=partial_rows,
        last_month_end=last_month_end_str,
        suggested_as_of=last_month_end_str,
        missing=required and not time_cols,
        suggestions=tuple(suggestions),
    )


def extract_consent_facts(
    loaded: LoadedFile,
    brief_consent_names: Sequence[str] = (),
    required: bool = False,
    goal_id: str | None = None,
) -> ConsentFacts:
    consent_cols: list[str] = []
    for col in loaded.frame.columns:
        c_name = str(col).lower()
        if c_name in brief_consent_names or any(
            k in c_name for k in ("consent", "opt_in", "opt_out", "marketing_permission")
        ):
            consent_cols.append(str(col))

    suggestions: list[FactSuggestion] = []
    if consent_cols:
        suggestions.append(
            FactSuggestion(
                kind="consent_column",
                payload={"upload_id": loaded.upload_id, "column": consent_cols[0]},
                title=f"“{consent_cols[0]}” says who agreed to be contacted.",
                reason="Only contacted people who agreed to be contacted are included.",
                confidence="check",
                ticked=False,
                upload_id=loaded.upload_id,
                goal_id=goal_id,
            )
        )

    return ConsentFacts(
        upload_id=loaded.upload_id,
        consent_columns=tuple(consent_cols),
        missing=required and not consent_cols,
        suggestions=tuple(suggestions),
    )
