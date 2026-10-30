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

The rules of the visit are facts too (Stage 2, the design §1.3). `key_facts`, `detector_facts` and
`role_step_facts` here, and the goal's own in `engine.doctor.facts_goal`, each return `Facts`: what the data says
(names and counts only), the `Suggestion`s the rules would make (an item, a question, a doubt, a request, a
withdrawal) with their reasons, and the tool results the suggestions cite. Nothing in them writes the case: the
author, the AI or the default one, applies the suggestions it takes.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol

import pandas as pd
from pydantic import Field, JsonValue

from api.access_policy import RoutePolicy
from engine.access.roles import Role
from engine.agent.contracts import AgentConfidence, ProposalState, RecipeStepKind
from engine.agent.formats import to_datetime_mixed
from engine.config import ColumnType, ProblemType, StrictBase, UseCaseConfig
from engine.content_cache import ContentCache, frame_digest
from engine.doctor.author import (
    AskPerson,
    AuthorAction,
    Create,
    Doubt,
    ItemDraft,
    Request,
    Withdraw,
    item_id_of,
)
from engine.doctor.case import Case, CaseDoubt, CaseItem, payload_digest
from engine.doctor.protocol import Ask, AskOption, DataRequest
from engine.doctor.registry import (
    DataTool,
    DataToolContext,
    DataToolError,
    DataToolKind,
    DataToolRegistry,
    DataToolResult,
    Exposure,
    Need,
)
from engine.doctor.tools import FileArgs, _config, _file_or_files, _loaded
from engine.doctor.visit_contracts import OptionEffect

if TYPE_CHECKING:
    from engine.doctor.current_tables import CurrentTables
    from engine.doctor.diagnose import LoadedFile
    from engine.doctor.goal_brief import GoalBrief

__all__ = [
    "M_CASE_FACT_TOOLS",
    "M_FACT_TOOLS",
    "AskPerson",
    "CampaignColumn",
    "CampaignColumns",
    "Create",
    "Doubt",
    "FactAction",
    "FactDeps",
    "FactRun",
    "Facts",
    "FactsPage",
    "Found",
    "ItemDraft",
    "OrderColumn",
    "OrderFileFacts",
    "Request",
    "Suggestion",
    "Withdraw",
    "campaign_columns",
    "detector_facts",
    "draft_item_id",
    "key_facts",
    "level_labels",
    "order_file_facts",
    "register_case_fact_tools",
    "register_fact_tools",
    "role_step_facts",
    "suggestion",
]

M_FACT_TOOLS: Final[tuple[str, ...]] = ("describe_order_file", "find_test_columns")
M_CASE_FACT_TOOLS: Final[tuple[str, ...]] = (
    "key_facts",
    "goal_fit_facts",
    "goal_facts",
    "suggest_build",
    "detector_facts",
    "role_step_facts",
)
"""The visit's rules as tools (#93 to #98): they read the stored case, so they exist only where a context names one."""

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


_ORDER_FACTS: Final[ContentCache[OrderFileFacts]] = ContentCache(8)
"""The facts of a table of orders by what it holds (every step of a visit asks again, and reads every cell)."""


def order_file_facts(upload_id: str, frame: pd.DataFrame) -> OrderFileFacts:
    """The facts of one file of orders: per column, and the rows with a negative number."""
    found = _ORDER_FACTS.get_or_compute(frame_digest(frame), lambda: _order_file_facts(upload_id, frame))
    return found.model_copy(update={"upload_id": upload_id})


def _order_file_facts(upload_id: str, frame: pd.DataFrame) -> OrderFileFacts:
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
        dates = pd.Series(dtype="datetime64[ns]") if is_number else to_datetime_mixed(series)
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
    register_case_fact_tools(registry)
    return M_FACT_TOOLS


# ---------------------------------------------------------------------------
# Rules as facts: what one fact function read and what it would do about it. Nothing here writes the case.
# ---------------------------------------------------------------------------
FactAction = AuthorAction
"""What a fact function suggests: the author's actions (`engine.doctor.author`)."""


class Suggestion(StrictBase):
    id: str = Field(description="A stable hash of the action.")
    action: FactAction
    reason: str


class Facts(StrictBase):
    """What a fact function read (names and counts only), what it would do about it, and how much it left out."""

    facts: dict[str, Any] = Field(default_factory=dict)
    suggested: tuple[Suggestion, ...] = ()
    left_out: int = 0
    evidence: tuple[DataToolResult, ...] = Field(
        default=(), description="The tool results the suggestions cite, for the case's evidence."
    )


def suggestion(action: Create | Withdraw | AskPerson | Doubt | Request, reason: str) -> Suggestion:
    """`action` as a suggestion; its id is a hash of it, and a `Create` cites that id as its basis."""
    key = "s" + payload_digest(action.model_dump(mode="json"))[:16]
    if isinstance(action, Create):
        action = action.model_copy(update={"basis": f"s:{key}"})
    return Suggestion(id=key, action=action, reason=reason)


def draft_item_id(draft: ItemDraft) -> str:
    """The id of the item a draft becomes (`engine.doctor.author.item_id_of`)."""
    return item_id_of(draft.kind, draft.payload, draft.goal_id)


class FactDeps(Protocol):
    """The part of the visit's deps the fact functions read."""

    @property
    def config_root(self) -> Path | None: ...

    @property
    def offers(self) -> Sequence[Any]: ...

    @property
    def detectors(self) -> Sequence[Any]: ...

    def clock(self) -> datetime: ...


class FactRun(Protocol):
    """What the fact functions need of a visit's run (`engine.doctor.visit._Run` is one): the files, the current
    tables, a tool context and a way to call a tool (which keeps the count of calls)."""

    @property
    def deps(self) -> FactDeps: ...

    def files(self, case: Case) -> tuple[LoadedFile, ...]: ...

    def current(self, case: Case) -> CurrentTables: ...

    def context(self, case: Case, **extra: Any) -> DataToolContext: ...

    def call(self, name: str, given: Mapping[str, Any], ctx: DataToolContext) -> DataToolResult: ...


class Found:
    """The suggestions one fact function makes, in the order it makes them, and the results they cite."""

    def __init__(self) -> None:
        self.suggested: list[Suggestion] = []
        self.evidence: list[DataToolResult] = []
        self.drafts: list[ItemDraft] = []

    def add(self, draft: ItemDraft, reason: str) -> ItemDraft:
        self.drafts.append(draft)
        self.suggested.append(suggestion(Create(draft=draft, reason=reason, basis=""), reason))
        return draft

    def create(
        self,
        kind: str,
        payload: dict[str, JsonValue],
        *,
        title: str,
        upload_id: str | None,
        confidence: AgentConfidence,
        evidence: tuple[str, ...],
        reason: str,
        goal_id: str | None = None,
    ) -> ItemDraft:
        draft = ItemDraft(
            kind=kind,
            payload=payload,
            title=title,
            upload_id=upload_id,
            goal_id=goal_id,
            confidence=confidence,
            evidence_ids=evidence,
        )
        return self.add(draft, reason)

    def ask(
        self,
        code: str,
        subject: Mapping[str, JsonValue],
        *,
        text: str,
        choices: Sequence[tuple[str, OptionEffect]],
        upload_id: str | None,
        reason: str,
        column: str | None = None,
        goal_id: str | None = None,
        blocking: bool = True,
        recommended: str | None = None,
        why: str = "",
        safe: str | None = None,
    ) -> None:
        """A question whose id is its code, its goal (none for the base) and its subject - never the mode it is
        asked in - so a question answered once is found, not asked again, whenever the rules need the same answer.

        The rules recommend no column on a guess of their own (which column is which is the person's to say). A
        `recommended` option is only ever one that a step the person already approved, or the build the goal
        offers, uses as a fact; `why` says so in plain words. The person still answers. `safe` is the option "I
        don't know" stands for: the one that is safe to take for them (often the one that decides nothing, but it
        can also be a real option, such as the widest audience). It is still a real answer the agent may point at.
        """
        ask = Ask(
            code=code,
            subject=subject,
            text=text,
            why=why,
            options=tuple(
                # "I don't know" decides nothing, so it is the safe choice unless the question names another: the
                # protocol adds its own only to a question that has none (`protocol.settle`)
                AskOption(
                    label=label,
                    effect=effect,
                    safe=effect.option_id == safe or (safe is None and label == DONT_KNOW),
                )
                for label, effect in choices
            ),
            rows=0,
            upload_id=upload_id,
            column=column,
            goal_id=goal_id,
            blocking=blocking,
            recommended=recommended if recommended in {effect.option_id for _l, effect in choices} else None,
        )
        self.suggested.append(suggestion(AskPerson(ask=ask), reason))

    def put(self, ask: Ask) -> None:
        """A question that is already made (a goal's setting, `engine.doctor.goal_settings`)."""
        self.suggested.append(suggestion(AskPerson(ask=ask), ask.why or ask.text))

    def doubt(self, doubt: CaseDoubt, reason: str) -> None:
        self.suggested.append(suggestion(Doubt(doubt=doubt), reason))

    def request(self, request: DataRequest, reason: str) -> None:
        self.suggested.append(suggestion(Request(request=request), reason))

    def facts(self, facts: dict[str, Any], *, left_out: int = 0) -> Facts:
        return Facts(
            facts=facts, suggested=tuple(self.suggested), left_out=left_out, evidence=tuple(self.evidence)
        )


DONT_KNOW: Final[str] = "I don't know"
UNIQUE: Final[float] = 1.05
"""A column with at most this many rows for each value names each row."""
NEAR_ROWS_PER_KEY: Final[float] = 50.0
"""A text column whose values repeat at most this often may name customers who appear on several rows."""
OUTPUT_NAME_CHARS: Final[int] = 64
MAX_OFFERED: Final[int] = 5
"""Columns offered in one question."""
ROW_ID_WARNING: Final[str] = (
    "This file has no column that names each row, so this adds a row number. A row number only counts the rows "
    "of this file: predictions cannot be joined back to your CRM or customer list without a real ID. Say no and "
    "send a file with an ID column to get that."
)
"""What the person is told with the proposed row number (never silently invented: it waits for their yes)."""

ID_NAMED: Final[re.Pattern[str]] = re.compile(
    r"(^|[_\s-])id$|^id[_\s-]|_key$|(customer|cust|client|member|account)[_\s-]?(id|no|num|number|code|ref)$",
    re.I,
)
"""A column named like the ID of a person or an account: `customer_id`, `Customer ID`, `account_no`, `member_code`."""


def id_named(unique: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Of the columns with a different value on every row, the ID-named ones when there are any: a spend or a
    score that happens to be unique does not compete with `customer_id`. Two ID-named columns still compete.
    """
    named = [key for key in unique if ID_NAMED.search(str(key["column"]).strip())]
    return named or unique


def dates_column(series: pd.Series) -> bool:
    """A text column that reads as dates on nearly every value (digits joined by a date or time separator)."""
    from engine.onboarding.sources import MIN_TIME_PARSE_RATE

    seen = series.dropna().astype(str).head(500)
    if seen.empty or seen.str.contains(r"\d[-/:.]\d", regex=True).mean() < MIN_TIME_PARSE_RATE:
        return False
    return bool(pd.to_datetime(seen, errors="coerce", format="mixed").notna().mean() >= MIN_TIME_PARSE_RATE)


def near_keys(loaded: LoadedFile) -> list[dict[str, Any]]:
    """The columns that could name the customers though none is unique and complete: a text column whose values
    repeat a few times (an events file) or that has some blanks, and any column named like an ID, over every column
    of the file (not the five the role tool lists first, which rank the fewest blanks first and so leave out a
    customer column with blanks). A column of dates never names a customer. Ranked: named like an ID first (a hint,
    not a decision), then the fewest repeats."""
    from engine.onboarding.sources import key_candidates

    found: list[dict[str, Any]] = []
    for candidate in key_candidates(loaded.profile):
        key = candidate.model_dump(mode="json")
        column = str(key["column"])
        if column not in loaded.frame.columns:
            continue
        named = bool(ID_NAMED.search(column.strip()))
        series = loaded.frame[column]
        text = not (pd.api.types.is_numeric_dtype(series) or pd.api.types.is_bool_dtype(series))
        repeats = float(key.get("rows_per_key") or 99.0)
        blanks = float(key.get("null_rate") or 0.0)
        if named or (
            text
            and repeats <= NEAR_ROWS_PER_KEY
            and blanks < 0.5
            and series.nunique() > 1
            and not dates_column(series)
        ):
            found.append(key)
    found.sort(
        key=lambda k: (not ID_NAMED.search(str(k["column"]).strip()), float(k.get("rows_per_key") or 99.0))
    )
    return found


def event_file(roles: dict[str, Any], event_roles: frozenset[str]) -> bool:
    """The file reads as events or transactions: an event role of the catalogue outscores every other role, so a
    row number would not be its ID. A tie is no evidence (a file with no date and nothing that names a role ties
    its event roles at the same low score), so it does not count as events."""
    ranked = [r for r in roles.get("roles", []) if isinstance(r, dict)]
    if not ranked or ranked[0].get("role") not in event_roles:
        return False
    top = ranked[0].get("confidence")
    second = ranked[1].get("confidence") if len(ranked) > 1 else None
    if not isinstance(top, (int, float)):
        return False
    return not isinstance(second, (int, float)) or top > second


def near_key_label(key: dict[str, Any], *, events: bool) -> str:
    """One offered customer column with its facts: how often a value repeats and the share of blank rows (rows
    with a blank there cannot be tied to a person)."""
    blank = float(key.get("null_rate") or 0.0)
    label = f"{key['column']} (a value repeats about {float(key.get('rows_per_key') or 1):.1f} times"
    if blank >= 0.005:
        label += f"; {blank:.0%} blank" + (", those rows cannot be tied to a person" if events else "")
    return label + ")"


def approved_build_key(case: Case, upload_id: str) -> str | None:
    """The customer column of an approved build that reads this file: the build counts customers by it, so the
    question which column names the customer is answered by the step the person approved."""
    from engine.doctor.visit import active_section

    for item in active_section(case)[0]:
        built = item.payload.get("buildable")
        if (
            item.kind == "recipe_step"
            and item.state is ProposalState.ACCEPTED
            and isinstance(built, dict)
            and built.get("upload_id") == upload_id
            and isinstance(built.get("entity_key"), str)
        ):
            return str(built["entity_key"])
    return None


def offered_build_key(diagnosis: Mapping[str, Any] | None, upload_id: str) -> str | None:
    """The one customer column the goals that can be built from this file count customers by (the diagnosis'
    `buildable` facts), or None when there is none or they disagree."""
    found = {
        str(built["entity_key"])
        for use_case in (diagnosis or {}).get("use_cases", [])
        if isinstance(use_case, dict)
        and isinstance(built := use_case.get("buildable"), dict)
        and built.get("upload_id") == upload_id
        and isinstance(built.get("entity_key"), str)
    }
    return next(iter(found)) if len(found) == 1 else None


def key_facts(run: FactRun, case: Case, diagnosis: Mapping[str, Any] | None = None) -> Facts:
    """What does not depend on a goal: which column names each row of each file whose key nobody decided.

    One column that is different on every row is a `Create key`; several are the question which one; none are the
    columns that could name the customers (with how often a value repeats and how many are blank) or, for a file
    that is not events, a row number. For a file of events the customer question recommends the column the goals'
    build counts customers by: `diagnosis` holds those builds as the goal check left them (the case's, when not
    given). The person still answers."""
    from engine.config import get_roles
    from engine.doctor.decisions import decisions

    found = Found()
    diagnosis = case.diagnosis if diagnosis is None else diagnosis
    event_roles = frozenset(get_roles(run.deps.config_root).event_roles)
    current = run.current(case)
    ctx = run.context(case, files=current.files)
    reading: list[dict[str, Any]] = []
    for loaded in current.files:
        upload_id = loaded.upload_id
        if (
            decisions(case).by_person("key", upload_id=upload_id)
            or approved_build_key(case, upload_id) is not None
        ):
            continue
        result = run.call("find_file_role", {"upload_id": upload_id}, ctx)
        found.evidence.append(result)
        fractions = {c.name for c in loaded.profile.columns if c.inferred_type is ColumnType.FLOAT}
        unique = id_named(
            [
                key
                for key in result.output.get("keys", [])
                if isinstance(key, dict)
                and float(key.get("rows_per_key") or 99.0) <= UNIQUE
                and float(key.get("null_rate") or 0.0) < 0.05
                and key.get("column")
                not in fractions  # a measurement with fractions names no row, however unique
            ]
        )
        name = loaded.file_name or "this file"
        events = False
        near: list[dict[str, Any]] = []
        if len(unique) == 1:
            column = str(unique[0]["column"])
            found.create(
                "key",
                {"upload_id": upload_id, "column": column},
                title=f"“{column}” names each row of {name}.",
                upload_id=upload_id,
                confidence=AgentConfidence.SURE,
                evidence=(result.evidence_id,),
                reason=f"“{column}” holds a different value on every row and none is blank.",
            )
        elif len(unique) > 1:
            choices = [
                (
                    str(key["column"]),
                    OptionEffect(
                        option_id=f"c{index}",
                        kind="key",
                        title=f"“{key['column']}” names each row of {name}.",
                        payload={"upload_id": upload_id, "column": str(key["column"])},
                    ),
                )
                for index, key in enumerate(unique[:MAX_OFFERED])
            ]
            choices.append((DONT_KNOW, OptionEffect(option_id="none")))
            found.ask(
                "KEY_AMBIGUOUS",
                {"upload_id": upload_id},
                text=f"Which column names each row of {name}? Several hold a different value on every row.",
                choices=choices,
                upload_id=upload_id,
                reason=f"{len(unique)} columns of {name} hold a different value on every row.",
            )
        elif len(loaded.frame) > 0:
            # No column is unique and complete. The near-candidates are facts (how often a value repeats, how many
            # blanks); a name like `customer_id` only ranks them. With any, the person says which one names the
            # customers; for a file of events (many rows per customer) that is the question, with the blank share
            # of each column shown and the column the goal's build counts customers by recommended, and a row
            # number is never offered (it would name a row, not a customer). A file that is not events can take a
            # row number; with no near-candidate (and not events) the row number is proposed.
            events = event_file(result.output, event_roles)
            near = near_keys(loaded)
            made = row_id_draft(run, found, loaded, ctx, evidence=result.evidence_id) if not events else None
            if near:
                choices = [
                    (
                        near_key_label(key, events=events),
                        OptionEffect(
                            option_id=f"c{index}",
                            kind="key",
                            title=f"“{key['column']}” names the customers of {name}.",
                            payload={"upload_id": upload_id, "column": str(key["column"])},
                        ),
                    )
                    for index, key in enumerate(near[:MAX_OFFERED])
                ]
                if made is not None:
                    choices.append(
                        (
                            "None of these: add a row number",
                            OptionEffect(
                                option_id="rownum", kind="key", title=made.title, payload=made.payload
                            ),
                        )
                    )
                choices.append((DONT_KNOW, OptionEffect(option_id="none")))
                offered = offered_build_key(diagnosis, upload_id)
                pick = next(
                    (
                        effect.option_id
                        for _label, effect in choices
                        if offered is not None and effect.payload.get("column") == offered
                    ),
                    None,
                )
                found.ask(
                    "KEY_NOT_UNIQUE",
                    {"upload_id": upload_id},
                    text=(
                        f"{name} has many rows for each customer, so no column is different on every row. "
                        "Which column names the customer on each row?"
                        if events
                        else f"No column of {name} holds a different value on every row with no blanks. "
                        "Which column names the customers in it?"
                    ),
                    choices=choices,
                    upload_id=upload_id,
                    recommended=pick,
                    why=f"The build for your goal counts each customer by “{offered}”." if pick else "",
                    reason=f"No column of {name} is different on every row; {len(near)} could name the customers.",
                )
            elif made is not None:
                found.add(made, "No column could name each row, so a row number would.")
        reading.append(
            {
                "upload_id": upload_id,
                "unique_keys": [_key_view(key) for key in unique[:MAX_OFFERED]],
                "near_keys": [_key_view(key) for key in near[:MAX_OFFERED]],
                "events": events,
            }
        )
    return found.facts({"files": reading})


def _key_view(key: dict[str, Any]) -> dict[str, Any]:
    return {
        "column": str(key["column"]),
        "rows_per_key": float(key.get("rows_per_key") or 0.0),
        "null_rate": float(key.get("null_rate") or 0.0),
    }


def row_id_draft(
    run: FactRun, found: Found, loaded: LoadedFile, ctx: DataToolContext, *, evidence: str
) -> ItemDraft | None:
    """A file with no ID column (one row per call, say): a pending ID decision whose yes adds a row number, with
    the plain warning. The row number is made by the ordinary `sql_query` step (the trial and the checks are the
    usual ones) and is recorded as the file's key with it. A query the trial refuses is skipped: the visit goes on.
    """
    from engine.agent.sql_step import table_alias
    from engine.doctor.guards import Finding
    from engine.doctor.materialize import table_names
    from engine.doctor.tools_sql import SqlStepProposal

    taken = {str(c).casefold() for c in loaded.frame.columns}
    column = next(name for name in ("row_id", *(f"row_id_{n}" for n in range(2, 100))) if name not in taken)
    table = table_names(ctx.files)[loaded.upload_id]
    safe = re.sub(r"[^A-Za-z0-9_]", "_", table)
    try:
        result = run.call(
            "propose_sql_step",
            {
                "description": f"Add a row number “{column}” to each row, in the order of the file.",
                "sql": f"SELECT row_number() OVER () AS {column}, * FROM {table_alias(table)}",
                "output_name": f"with_row_id_{safe}"[:OUTPUT_NAME_CHARS],
                "upload_ids": [loaded.upload_id],
            },
            ctx,
        )
    except DataToolError:
        return None
    found.evidence.append(result)
    made = SqlStepProposal.model_validate(result.output)
    findings = [*made.findings, Finding(level="flag", code="ROW_ID_INVENTED", message=ROW_ID_WARNING)]
    payload = made.payload()
    payload["findings"] = [f.model_dump(mode="json") for f in findings]
    payload["needs_yes"] = True
    payload["column"] = column  # the yes also records it as the file's ID (the key decision)
    payload["upload_id"] = loaded.upload_id
    return ItemDraft(
        kind="key",
        payload=payload,
        title=f"Add a row number “{column}” to {loaded.file_name or 'this file'}: it has no ID column.",
        upload_id=loaded.upload_id,
        confidence=AgentConfidence.CHECK,
        evidence_ids=(evidence, result.evidence_id),
    )


def _draft_of(item: CaseItem) -> ItemDraft:
    """A detector's proposal as a draft. A detector ticks only what the values prove (or what an earlier approval of
    the same shape proves), so a ticked one is a sure one; where it goes is then the author's rule."""
    return ItemDraft(
        kind=item.kind,
        payload=dict(item.payload),
        title=item.title,
        upload_id=item.upload_id,
        goal_id=item.goal_id,
        confidence=AgentConfidence.SURE if item.state is ProposalState.ACCEPTED else item.confidence,
        evidence_ids=item.evidence_ids,
    )


def detector_facts(run: FactRun, case: Case, brief: GoalBrief | None) -> Facts:
    """The detectors' findings (`VisitDeps.detectors`, the design §7.1): their fixes as `Create`, their doubts, their
    requests for more data and the ambiguities they would ask about as `AskPerson`. The detectors that propose fixes
    (`reads_current`) read the files as the accepted changes leave them, so a fix already in the plan is not found
    again (a date already parsed, a placeholder already blanked); the others (what the file is: its identity, its
    personal data, how files relate) read the upload as it came in."""
    found = Found()
    if not run.deps.detectors:
        return found.facts({})
    raw = run.files(case)
    current = run.current(case).files
    asks: list[Ask] = []
    for detector in run.deps.detectors:
        detected = detector(case, current if getattr(detector, "reads_current", False) else raw, brief)
        found.evidence.extend(detected.evidence)
        for item in detected.items:
            found.add(_draft_of(item), item.title)
        for doubt in detected.doubts:
            found.doubt(doubt, doubt.because)
        for request in detected.requests:
            found.request(request, request.why)
        asks.extend(detected.asks)
    for ask in asks:
        found.put(ask)
    return found.facts({"detectors": len(run.deps.detectors)})


_ROLE_STEP_KINDS: Final[frozenset[str]] = frozenset(
    kind.value
    for kind in (
        RecipeStepKind.DERIVE,
        RecipeStepKind.SQL_QUERY,
        RecipeStepKind.KEEP_LATEST_PER_ID,
        RecipeStepKind.COMBINE_ROWS,
        RecipeStepKind.UNPIVOT,
    )
)
"""Steps that name the ID column without changing it (the recipe's own check lets them through): a derived column,
a query, and the steps that reshape the rows per ID."""


def role_step_facts(case: Case) -> Facts:
    """A recipe step on the column settled as the file's ID or outcome can never run (the recipe never changes them:
    `PLAN_STEP_CANNOT_RUN`), so each is a suggested `Withdraw` with the reason, whoever ticked it: a yes/no outcome
    is read through the goal's positive value, never recoded by a step. Rows left out by their ID (test accounts)
    and a derived column that only reads them are not changes to them, so they stay."""
    from engine.doctor.decisions import decisions

    found = Found()
    decided = decisions(case)
    one = case.files[0].upload_id if len(case.files) == 1 else None
    for item in case.items:
        raw = item.payload.get("step")
        if item.state not in (ProposalState.PENDING, ProposalState.ACCEPTED) or not isinstance(raw, dict):
            continue
        named = item.payload.get("upload_id")
        upload_id = item.upload_id or (named if isinstance(named, str) else None) or one
        column, kind = raw.get("column"), raw.get("kind")
        if upload_id is None or not isinstance(column, str):
            continue
        key, outcome = decided.key(upload_id), decided.outcome_of_upload(upload_id)
        if kind in _ROLE_STEP_KINDS or column not in {key, outcome} - {None}:
            continue
        if kind == RecipeStepKind.REMOVE_ROWS.value and column == key != outcome:
            continue
        role = "ID" if column == key else "outcome"
        reason = (
            f"'{column}' is the {role} column, which no step changes"
            + ("; its yes and no are read as they are" if role == "outcome" else "")
            + "."
        )
        found.suggested.append(
            suggestion(
                Withdraw(
                    item_id=item.item_id,
                    reason=reason,
                    consequence=f"The step is taken out of the plan and '{column}' stays as it is.",
                ),
                reason,
            )
        )
    return found.facts({})


# ---------------------------------------------------------------------------
# The fact functions as registry tools (the design §1.3): the AI reaches them here, paged
# ---------------------------------------------------------------------------
class FactsArgs(StrictBase):
    offset: int = Field(
        default=0,
        ge=0,
        description="Suggestions to skip, to read the rest when `left_out` says some did not fit.",
    )


class FactsPage(StrictBase):
    """`Facts` as a tool returns it: what the function read, the suggestions that fit from `offset` on, and how many
    more there are (`left_out`). The tool results the suggestions cite are the registry's own evidence."""

    facts: dict[str, Any]
    suggested: tuple[Suggestion, ...]
    left_out: int = Field(
        description="Suggestions (or verdicts) not shown: ask again with a larger `offset`."
    )


class _ToolRun:
    """A `FactRun` over one tool call's context: the same files, tables and inner tool calls a visit gives the rules,
    so the AI and the visit read the same facts. The inner calls are the engine's own rules (`caller="rules"`, as in
    the visit), inside a call whose role the registry has already checked."""

    def __init__(self, ctx: DataToolContext) -> None:
        self._ctx = ctx
        self.config_root = ctx.config_root
        self.offers = ctx.offers
        self.detectors = ctx.detectors
        self.clock = ctx.clock

    @property
    def deps(self) -> FactDeps:
        return self

    def files(self, case: Case) -> tuple[LoadedFile, ...]:  # noqa: ARG002 - `FactRun`'s shape
        return self._ctx.files

    def current(self, case: Case) -> CurrentTables:
        from engine.doctor.current_tables import current_tables
        from engine.doctor.visit import active_section

        return current_tables(
            case,
            active_section(case)[0],
            self._ctx.files,
            config=self.offers[0].config if self.offers else None,
        )

    def context(self, case: Case, **extra: Any) -> DataToolContext:  # noqa: ARG002 - `FactRun`'s shape
        files = extra.pop("files") if "files" in extra else self._ctx.files
        return DataToolContext(
            caller="rules",
            storage=self._ctx.storage,
            ledger=self._ctx.ledger,
            files=files,
            offers=self.offers,
            config_root=self.config_root,
            clock=self.clock,
            actor=self._ctx.actor,
            **extra,
        )

    def call(self, name: str, given: Mapping[str, Any], ctx: DataToolContext) -> DataToolResult:
        from engine.doctor.registry import data_registry

        return data_registry().call(name, given, ctx)


def _stored_case(ctx: DataToolContext) -> Case:
    """The case as stored (what the visit reads), never a copy the caller holds."""
    from engine.doctor.case import CaseError, CaseStore

    if (
        ctx.case is None or ctx.storage is None
    ):  # unreachable through the registry: `needs` makes it unavailable
        raise DataToolError("TOOL_UNKNOWN", "This tool needs a case.", status=404)
    try:
        return CaseStore(ctx.storage, clock=ctx.clock).get(ctx.case.client_id, ctx.case.case_id)
    except CaseError as exc:
        raise DataToolError("TOOL_INPUT_INVALID", "The case cannot be read.", status=422) from exc


def _goal_brief(ctx: DataToolContext, case: Case) -> GoalBrief | None:
    """The active goal's brief as the visit froze it, else as the configuration has it; None with no active goal."""
    from engine.config import ConfigError
    from engine.doctor.goal_brief import load_goal_brief
    from engine.doctor.visit import active_goal, stored_brief

    goal = active_goal(case)
    if goal is None or ctx.storage is None:
        return None
    frozen = stored_brief(ctx.storage, case)
    if frozen is not None:
        return frozen
    try:
        return load_goal_brief(goal.goal_id, root=ctx.root())
    except ConfigError as exc:
        raise DataToolError(exc.code, "There is no goal with that name here.", status=404) from exc


def _needed_brief(ctx: DataToolContext, case: Case) -> GoalBrief:
    brief = _goal_brief(ctx, case)
    if brief is None:
        raise DataToolError("TOOL_INPUT_INVALID", "The case has no goal yet; set one first.", status=422)
    return brief


def _paged(parts: Sequence[tuple[str | None, Facts]], offset: int) -> FactsPage:
    """The parts' facts (under the part's name when there are several) and their suggestions from `offset` on, as
    many as fit the tool's budget: the rest are counted in `left_out`."""
    from engine.doctor.tools import _fitted

    facts: dict[str, Any] = {}
    for name, part in parts:
        if name is None:
            facts = dict(part.facts)
        else:
            facts[name] = part.facts
    every = [s for _, part in parts for s in part.suggested]
    page = _fitted(FactsPage(facts=facts, suggested=tuple(every[offset:]), left_out=0), ("suggested",))
    shown = len(page.suggested)
    return page.model_copy(
        update={"left_out": max(0, len(every) - offset - shown) + sum(part.left_out for _, part in parts)}
    )


def _key_facts(given: FactsArgs, ctx: DataToolContext) -> FactsPage:
    case = _stored_case(ctx)
    return _paged([(None, key_facts(_ToolRun(ctx), case))], given.offset)


def _goal_fit_facts(given: FactsArgs, ctx: DataToolContext) -> FactsPage:
    from engine.doctor.facts_goal import goal_fit_facts

    case = _stored_case(ctx)
    return _paged([(None, goal_fit_facts(_ToolRun(ctx), case))], given.offset)


def _goal_facts(given: FactsArgs, ctx: DataToolContext) -> FactsPage:
    from engine.doctor.facts_goal import goal_facts

    case = _stored_case(ctx)
    parts = goal_facts(_ToolRun(ctx), case, _needed_brief(ctx, case))
    return _paged(list(zip(("outcome", "time", "consent"), parts, strict=False)), given.offset)


def _suggest_build(given: FactsArgs, ctx: DataToolContext) -> FactsPage:
    from engine.doctor.facts_goal import suggest_build

    case = _stored_case(ctx)
    return _paged([(None, suggest_build(_ToolRun(ctx), case, _needed_brief(ctx, case)))], given.offset)


def _detector_facts(given: FactsArgs, ctx: DataToolContext) -> FactsPage:
    case = _stored_case(ctx)
    return _paged([(None, detector_facts(_ToolRun(ctx), case, _goal_brief(ctx, case)))], given.offset)


def _role_step_facts(given: FactsArgs, ctx: DataToolContext) -> FactsPage:
    return _paged([(None, role_step_facts(_stored_case(ctx)))], given.offset)


_CaseFactRow = tuple[int, str, str, Callable[[FactsArgs, DataToolContext], FactsPage], tuple[Need, ...], str]
_CASE_FACT_ROWS: Final[tuple[_CaseFactRow, ...]] = (
    (
        93,
        "key_facts",
        "Which column names each row of each file whose ID nobody decided: the ID to confirm, the columns that "
        "could name the customers, or a row number. Suggestions only; changes nothing.",
        _key_facts,
        ("case", "storage", "files"),
        "engine/doctor/tools_facts.py key_facts",
    ),
    (
        94,
        "goal_fit_facts",
        "With no goal yet: what the data can power and what is missing for the other goals (verdicts paged by "
        "`left_out`; `goal_details` gives one in full). Suggestions only; changes nothing.",
        _goal_fit_facts,
        ("case", "storage", "files"),
        "engine/doctor/facts_goal.py goal_fit_facts",
    ),
    (
        95,
        "goal_facts",
        "For the active goal: the outcome column (or the build that makes it), the date each row describes and "
        "consent, as facts under `outcome`, `time` and `consent`, with the changes and questions the rules would "
        "make. Suggestions only; changes nothing.",
        _goal_facts,
        ("case", "storage", "files"),
        "engine/doctor/facts_goal.py goal_facts",
    ),
    (
        96,
        "suggest_build",
        "For the active goal when no file has its outcome: the query that builds it from an event table, with the "
        "receipt of its trial on the first rows. Suggestions only; changes nothing.",
        _suggest_build,
        ("case", "storage", "files"),
        "engine/doctor/facts_goal.py suggest_build",
    ),
    (
        97,
        "detector_facts",
        "What the detectors found in the files as the plan leaves them: fixes, doubts, requests for data and "
        "questions. Suggestions only; changes nothing.",
        _detector_facts,
        ("case", "storage", "files"),
        "engine/doctor/tools_facts.py detector_facts",
    ),
    (
        98,
        "role_step_facts",
        "Steps in the plan that change the column settled as the ID or the outcome, which can never run: each as "
        "a withdrawal to suggest, with its reason. Changes nothing.",
        _role_step_facts,
        ("case", "storage"),
        "engine/doctor/tools_facts.py role_step_facts",
    ),
)


def register_case_fact_tools(registry: DataToolRegistry) -> tuple[str, ...]:
    """Register the visit's fact functions (#93 to #98) as READ tools for the rules and the AI. Each reads the stored
    case and the files in the context, changes nothing, and returns its suggestions paged."""
    for number, name, description, run, needs, wraps in _CASE_FACT_ROWS:
        registry.register(
            DataTool(
                number=number,
                name=name,
                kind=DataToolKind.READ,
                description=description,
                input_model=FactsArgs,
                output_model=FactsPage,
                route_policy=RoutePolicy(
                    role=Role.ANALYST, action=f"doctor.{name}", purpose="read what the rules find in a case"
                ),
                exposure=Exposure.SESSION_ONLY,
                run=run,
                callers=frozenset({"rules", "ai"}),
                needs=needs,
                wraps=wraps,
            )
        )
    return M_CASE_FACT_TOOLS
