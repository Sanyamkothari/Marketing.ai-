"""The Data Doctor asks what one row is, which rows are real money, whether anyone was held out and who can
respond, with no AI.

One detector (`engine.doctor.visit.DetectorPort`) counts, from the rows the visit read, five things a total or a
rate would silently get wrong, and asks one question for each, with the recommended answer and, where a fixed
step does it, the step ready on the answer:

* a money column that is the same on every row of a non-key ID (an order total on each line item): Q21, and a
  SQL step that takes it once per order (`propose_sql_step`, so it carries receipts);
* a non-key ID whose rows are one thing's status history (a ticket log): Q21, `keep_latest_per_id`;
* a status column with failed, declined or refunded rows beside an amount: Q22, `remove_rows`;
* a yes/no "contacted" column and no random-holdout column: Q13, and a note on the plan that no lift is claimed;
* yes outcomes among customers who cannot have it (never active, or already hold the product): Q23, `remove_rows`.

Nothing is changed here: an answer ticks the step, the plan shows it, and the approval applies it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import pandas as pd
from pydantic import JsonValue

from engine.agent.contracts import AgentConfidence, RecipeStep, RecipeStepKind
from engine.agent.sql_step import THIS_TABLE, table_alias
from engine.doctor.decisions import decisions
from engine.doctor.detect_time_keys import CHECK_KIND, _outcome_meanings
from engine.doctor.prescription import slot_name
from engine.doctor.protocol import Ask
from engine.doctor.questions import build_ask
from engine.doctor.visit_contracts import OptionEffect
from engine.utils.logging import get_logger
from engine.utils.time import utc_now

if TYPE_CHECKING:
    from engine.doctor.case import Case, CaseItem
    from engine.doctor.diagnose import LoadedFile
    from engine.doctor.goal_brief import GoalBrief
    from engine.doctor.visit import Detected, DetectorPort

__all__ = ["grain_detector"]

_LOG = get_logger(__name__)

_ID: Final[re.Pattern[str]] = re.compile(r"(^id$|_id$|^id_|_key$|_no$|_ref$)", re.I)
_MONEY: Final[re.Pattern[str]] = re.compile(r"total|amount|revenue|bill|charge|spend|premium|fee|price", re.I)
_STATUS: Final[re.Pattern[str]] = re.compile(r"status|state|stage", re.I)
_NOT_MONEY: Final[re.Pattern[str]] = re.compile(r"fail|declin|refund|revers|chargeback|bounce", re.I)
_TREATED: Final[re.Pattern[str]] = re.compile(
    r"(^|_)(contacted|treated|exposed|sent|mailed|targeted|messaged)($|_)", re.I
)
_HELD_OUT: Final[re.Pattern[str]] = re.compile(
    r"hold.?out|control|random|(^|_)(arm|variant)($|_)|^((ab|test|treatment)_)?group$", re.I
)
"""A holdout column's name ("age_group" or "cell_phone" is not one)."""
_ACTIVITY: Final[re.Pattern[str]] = re.compile(r"order|purchase|login|visit|transaction|txn|usage", re.I)
"""Activity history (a count or a date); a flag such as `is_active` or an amount is not."""
_NOT_DONE: Final[str] = (
    " This change could not be prepared here, so an answer is recorded and nothing is changed."
)
_HOLDS: Final[re.Pattern[str]] = re.compile(
    r"^(has|have|holds?|owns?|already)_|^existing_|_(held|owned)$", re.I
)
_ISO: Final[re.Pattern[str]] = re.compile(r"^\d{4}-\d{2}-\d{2}")
_REPEATED: Final[float] = 1.2
"""Rows per value for an ID to count as repeating."""
_ALL: Final[float] = 0.98
"""Share of IDs a rule must hold for ("the same on every row" allows for a stray typo)."""
_MIN_SHARE: Final[float] = 0.01
_MIN_RATE_MOVE: Final[float] = 0.5
"""Points the yes share must move for leaving customers out to be worth a question (the protocol's own floor)."""
_NOTE: Final[str] = (
    "No random holdout is recorded in {file}: results say who responds among those contacted, never extra sales "
    "caused by the campaign (no lift claims) until a random group left out is confirmed."
)


def grain_detector(*, config_root: Path | None = None) -> DetectorPort:
    """The visit's detector for the grain of the rows, real money, the holdout and who can respond (rules only)."""

    def detect(case: Case, files: tuple[LoadedFile, ...], brief: GoalBrief | None) -> Detected:
        from engine.doctor.visit import Detected

        asks: list[Ask] = []
        items: list[CaseItem] = []
        for loaded in files:
            try:
                found = _file(case, loaded, brief, files, config_root)
            except Exception as exc:  # a file a check cannot read is not a reason to stop the visit
                _LOG.info("doctor.grain_failed upload=%s error=%s", loaded.upload_id, type(exc).__name__)
                continue
            asks.extend(ask for ask in found if isinstance(ask, Ask))
            items.extend(item for item in found if not isinstance(item, Ask))
        return Detected(asks=tuple(asks), items=tuple(items))

    return detect


@dataclass(frozen=True)
class _File:
    """One file as the checks read it."""

    loaded: LoadedFile
    brief: GoalBrief | None
    files: tuple[LoadedFile, ...]
    root: Path | None
    key: str | None
    outcome: str | None
    items: tuple[CaseItem, ...] = ()
    """The plan so far: a query is tried on the file after these fixes, as a run reads it."""

    @property
    def frame(self) -> pd.DataFrame:
        return self.loaded.frame

    @property
    def upload_id(self) -> str:
        return self.loaded.upload_id

    @property
    def name(self) -> str:
        return self.loaded.file_name or self.loaded.upload_id

    @property
    def rows(self) -> int:
        return self.loaded.file_rows or len(self.loaded.frame)


def _file(
    case: Case, loaded: LoadedFile, brief: GoalBrief | None, files: tuple[LoadedFile, ...], root: Path | None
) -> list[Ask | CaseItem]:
    from engine.doctor.visit import active_section

    decided = decisions(case)
    f = _File(
        loaded,
        brief,
        files,
        root,
        decided.key(loaded.upload_id),
        decided.outcome_of_upload(loaded.upload_id),
        tuple(active_section(case)[0]),
    )
    found: list[Ask | CaseItem] = []
    for check in (_one_row, _real_money, _holdout, _eligible):
        found.extend(check(f))
    return found


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _words(name: str) -> str:
    return re.sub(r"[_\s]+", " ", name).strip().lower()


def _step(kind: RecipeStepKind, column: str, params: dict[str, Any], reason: str) -> dict[str, Any]:
    step = RecipeStep(order=1, kind=kind, column=column, params=params, reason=reason)
    return step.model_dump(mode="json")


def _effect(upload_id: str, step: dict[str, Any], title: str) -> OptionEffect:
    payload: dict[str, JsonValue] = {"upload_id": upload_id, "path": None, "value": None, "step": step}
    return OptionEffect(option_id="x", kind="recipe_step", title=title, payload=payload)


def _numbers(series: pd.Series[Any]) -> pd.Series[Any]:
    return pd.to_numeric(series, errors="coerce")


def _meaning(series: pd.Series[Any]) -> pd.Series[Any] | None:
    """True for each yes, when every filled value of the column is a yes or a no word and both occur."""
    from engine.doctor.outcome_definition import rules_meaning, value_text

    said = series.map(lambda v: rules_meaning(text) if (text := value_text(v)) else None)
    filled = said.dropna()
    if filled.empty or len(filled) < 0.95 * series.notna().sum() or filled.nunique() < 2:
        return None
    return said.eq("yes")


def _repeating(frame: pd.DataFrame, key: str | None) -> list[str]:
    """ID-named columns, other than the key, that repeat, the one with the most different values first."""
    found = {str(c): int(frame[c].nunique()) for c in frame.columns if str(c) != key and _ID.search(str(c))}
    return sorted((c for c, n in found.items() if 1 < n <= len(frame) / _REPEATED), key=lambda c: -found[c])


def _same_within(frame: pd.DataFrame, group: str, column: str) -> bool:
    """Whether `column` is the same on every row of a `group` (nearly): per ID, one value."""
    per = frame.groupby(group, sort=False)[column].nunique(dropna=False)
    return bool((per <= 1).mean() >= _ALL)


# ---------------------------------------------------------------------------
# What is one row (Q21): a total repeated on each line, or a status history
# ---------------------------------------------------------------------------
def _one_row(f: _File) -> list[Ask | CaseItem]:
    frame = f.frame
    others = [str(c) for c in frame.columns if str(c) not in {f.key, f.outcome}]
    for group in _repeating(frame, f.key):
        left = len(frame) - int(frame[group].nunique())
        total = next(
            (
                c
                for c in others
                if c != group
                and _MONEY.search(c)
                and not _ID.search(c)
                and _numbers(frame[c]).nunique() > 2
                and _same_within(frame, group, c)
            ),
            None,
        )
        if total is not None:
            return [_total_ask(f, group, total, left)]
        status = next((c for c in others if _STATUS.search(c) and not _same_within(frame, group, c)), None)
        when = next((c for c in others if c != status and _is_date(frame[c])), None)
        rest = [c for c in others if c not in {group, status, when}]
        if status and when and all(_same_within(frame, group, c) for c in rest):
            return [_log_ask(f, group, status, when, left)]
    return []


def _is_date(series: pd.Series[Any]) -> bool:
    text = series.dropna().astype(str)
    return len(text) > 0 and bool(text.str.match(_ISO).mean() >= 0.95)


def _total_ask(f: _File, group: str, column: str, left: int) -> Ask:
    once = f"{_words(column)} once per {_words(group)}"
    table = slot_name(Path(f.name).stem)
    sql = (
        f'SELECT * REPLACE (CASE WHEN row_number() OVER (PARTITION BY "{group}" ORDER BY "{f.key or group}") = 1 '
        f'THEN "{column}" END AS "{column}") FROM {table_alias(table)}'
    )
    effect = _sql_effect(f, sql, table, f"Count the {once}")
    detail = (
        f"The {_words(column)} is the same on every row of the same {_words(group)}, so it looks like the "
        f"{_words(column)} repeated on each line item. Count it once per {_words(group)}?"
    ) + ("" if effect else _NOT_DONE)
    return build_ask(
        "Q21",
        {"id": group, "file": f.name, "detail": detail},
        rows=f.rows,
        choices=[
            f"Take the {once}: the later rows of each {_words(group)} are left blank",
            "Count every row as it is",
        ],
        recommended_choice=0,
        rows_changed=[left, 0],
        effects=[effect, None] if effect else None,
        upload_id=f.upload_id,
        column=column,
    )


def _sql_effect(f: _File, sql: str, table: str, title: str) -> OptionEffect | None:
    """A `sql_query` step tried on the rows with its receipt (`propose_sql_step`), ready on an answer; None when
    the tool refuses it (the question is then asked without a step)."""
    from engine.doctor.registry import DataToolContext, DataToolError, DataToolRegistry
    from engine.doctor.tools_sql import SqlStepProposal, register_sql_tools

    registry = DataToolRegistry()
    register_sql_tools(registry)
    ctx = DataToolContext(
        caller="rules", files=f.files, config_root=f.root, primary_key=f.key, case_items=f.items
    )
    given = {"description": title, "sql": sql, "output_name": table, "upload_ids": [f.upload_id]}
    try:
        result = registry.call("propose_sql_step", given, ctx)
    except DataToolError as exc:
        _LOG.info("doctor.grain_sql_refused code=%s", exc.code)
        return None
    proposal = SqlStepProposal.model_validate(result.output)
    # Tried on the file by its name (for the receipt); kept as a step of the file's own recipe that reads `this`,
    # the file as the steps above left it, so an earlier approved step is never undone on replay.
    this = sql.replace(table_alias(table), table_alias(THIS_TABLE))
    step = {**proposal.step, "params": {**proposal.step["params"], "sql": this, "inputs": [THIS_TABLE]}}
    payload: dict[str, JsonValue] = {
        **{k: v for k, v in proposal.payload().items() if k != "stage"},
        "upload_id": f.upload_id,
        "path": None,
        "value": None,
        "step": step,
        "detail": this,
    }
    return OptionEffect(option_id="x", kind="recipe_step", title=proposal.title, payload=payload)


def _log_ask(f: _File, group: str, status: str, when: str, left: int) -> Ask:
    table = slot_name(Path(f.name).stem)
    sql = (
        f'SELECT * FROM {table_alias(table)} QUALIFY row_number() OVER (PARTITION BY "{group}" '
        f'ORDER BY "{when}" DESC, "{f.key or group}" DESC) = 1'
    )
    effect = _sql_effect(f, sql, table, f"Keep only the latest row of each {_words(group)}")
    detail = (
        f"The {_words(status)} changes from row to row, so it looks like a status history. Count the latest "
        f"status per {_words(group)}?"
    ) + ("" if effect else _NOT_DONE)
    return build_ask(
        "Q21",
        {"id": group, "file": f.name, "detail": detail},
        rows=f.rows,
        choices=[f"Keep the latest status per {_words(group)}", "Count every row as it is"],
        recommended_choice=0,
        rows_changed=[left, 0],
        effects=[effect, None] if effect else None,
        upload_id=f.upload_id,
        column=group,
    )


# ---------------------------------------------------------------------------
# Which rows are real money (Q22)
# ---------------------------------------------------------------------------
def _real_money(f: _File) -> list[Ask | CaseItem]:
    frame = f.frame
    amount = next(
        (str(c) for c in frame.columns if _MONEY.search(str(c)) and _numbers(frame[c]).notna().mean() > 0.9),
        None,
    )
    for column in (str(c) for c in frame.columns):
        if amount is None or column in {f.key, f.outcome} or not _STATUS.search(column):
            continue
        text = frame[column].dropna().astype(str).str.strip()
        bad = sorted({v for v in text.unique() if _NOT_MONEY.search(v)})
        count = int(text.isin(bad).sum())
        if not bad or text.nunique() > 12:
            continue
        shown = ", ".join(f"“{v}”" for v in bad)
        step = _step(
            RecipeStepKind.REMOVE_ROWS,
            column,
            {"operator": "in", "values": bad},
            f"Leave out the {shown} rows",
        )
        detail = (
            f"{count:,} of {f.rows:,} rows say {shown}: that money did not arrive, but the {_words(amount)} "
            "adds it up."
        )
        return [
            build_ask(
                "Q22",
                {"column": column, "file": f.name, "detail": detail},
                rows=f.rows,
                choices=[
                    f"Only the other rows are real money: leave out the {shown} rows",
                    "Count every row",
                ],
                recommended_choice=0,
                rows_changed=[count, 0],
                effects=[_effect(f.upload_id, step, f"Leave out the rows where {column} is {shown}."), None],
                upload_id=f.upload_id,
                column=column,
            )
        ]
    return []


# ---------------------------------------------------------------------------
# Was anyone held out at random (Q13)
# ---------------------------------------------------------------------------
def _holdout(f: _File) -> list[Ask | CaseItem]:
    from engine.doctor.visit import _item

    columns = [str(c) for c in f.frame.columns]
    if any(_HELD_OUT.search(c) for c in columns):
        return []
    for column in columns:
        yes = (
            _meaning(f.frame[column])
            if _TREATED.search(column) and column not in {f.key, f.outcome}
            else None
        )
        if yes is None or not _MIN_SHARE < yes.mean() < 1 - _MIN_SHARE:
            continue
        ask = build_ask(
            "Q13", {"campaign": f"recorded in “{column}”"}, rows=f.rows, upload_id=f.upload_id, column=column
        )
        payload: dict[str, JsonValue] = {
            "upload_id": f.upload_id,
            "code": "NO_HOLDOUT",
            "level": "note",
            "column": column,
            "numbers": {"treated": int(yes.sum()), "rows": f.rows},
        }
        note = _item(
            CHECK_KIND,
            payload,
            title=_NOTE.format(file=f.name),
            upload_id=f.upload_id,
            confidence=AgentConfidence.CHECK,
            evidence=(),
            now=utc_now(),
        )
        return [ask, note]
    return []


# ---------------------------------------------------------------------------
# Who can have the outcome (Q23): never active, or already holds the product
# ---------------------------------------------------------------------------
_Found = tuple[str, "dict[str, Any] | None", "pd.Series[Any]", str]
"""The column, the `remove_rows` params that leave out exactly those rows (None when no one column says it), the
rows to leave out, and why."""


def _eligible(f: _File) -> list[Ask | CaseItem]:
    meaning = _outcome_meanings(f.loaded, f.outcome) if f.outcome else None
    if f.outcome is None or meaning is None:
        return []
    yes = meaning[2]
    columns = [str(c) for c in f.frame.columns if str(c) not in {f.key, f.outcome}]
    found = _never_active(f.frame, columns)
    if found is None and f.brief is not None and "cross" in f.brief.goal_id:
        found = _holders(f.frame, columns)
    if found is None:
        return []
    column, params, mask, reason = found
    count, hits = int(mask.sum()), int((mask & yes).sum())
    rates = [float((yes & ~mask).sum() / max(1, (~mask).sum()) * 100), float(yes.mean() * 100)]
    if count < _MIN_SHARE * len(f.frame) or abs(rates[0] - rates[1]) < _MIN_RATE_MOVE:
        return []
    effect = None
    if params is not None:
        step = _step(RecipeStepKind.REMOVE_ROWS, column, params, f"Leave out: {reason}")
        effect = _effect(f.upload_id, step, f"Leave out the {count:,} customers who {reason}.")
    said = {
        "count": f"{count:,}",
        "file": f.name,
        "reason": reason + ("" if effect else "." + _NOT_DONE.rstrip(".")),
        "positives": f"{hits:,}",
        "outcome": f.outcome,
    }
    return [
        build_ask(
            "Q23",
            said,
            rows=f.rows,
            rows_changed=[count, 0],
            label_rates=rates,
            effects=[effect, None] if effect else None,
            upload_id=f.upload_id,
            column=column,
        )
    ]


def _blank_or_zero(series: pd.Series[Any]) -> pd.Series[Any]:
    return series.isna() | series.astype(str).str.strip().eq("") | _numbers(series).eq(0)


def _never_active(frame: pd.DataFrame, columns: list[str]) -> _Found | None:
    """Rows with a blank or zero in every activity column (last order date, orders count, visits...)."""
    active = [c for c in columns if _ACTIVITY.search(c) and not _ID.search(c) and not _MONEY.search(c)]
    if not active:
        return None
    mask = pd.concat([_blank_or_zero(frame[c]) for c in active], axis=1).all(axis=1)
    for column in active:  # a step on one column, only when it leaves out exactly the rows counted
        if _numbers(frame[column]).eq(0).equals(mask):
            return column, {"operator": "equals", "values": ["0"]}, mask, "were never active"
        if (frame[column].isna() | frame[column].astype(str).str.strip().eq("")).equals(mask):
            return column, {"operator": "is_empty", "values": []}, mask, "were never active"
    return active[0], None, mask, "were never active"


def _holders(frame: pd.DataFrame, columns: list[str]) -> _Found | None:
    """Rows whose "has the product" column says yes: they cannot buy it again."""
    for column in columns:
        yes = _meaning(frame[column]) if _HOLDS.search(column) else None
        if yes is not None:
            values = sorted(
                {str(v).strip() for v in frame.loc[yes, column].dropna()} | {"1"}
            )  # "1": once mapped
            return column, {"operator": "in", "values": values}, yes, "already hold the product"
    return None
