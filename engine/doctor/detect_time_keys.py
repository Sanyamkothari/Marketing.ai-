"""The Data Doctor checks the keys, the dates and the newest rows of every file, with no AI.

One detector (`engine.doctor.visit.DetectorPort`) runs the guards of `engine.doctor.checks_time_keys` on the rows
the visit read (the whole file, or a random sample of it) and gives back, by how serious each finding is:

* **stop** - a data request (IDs Excel rounded off: the digits are gone, send the file again) or a blocking
  question (dates written in two orders; an outcome that looks the wrong way round), or, for a step that cannot
  run, an item that says so;
* **flag** - an item on the approval page, never ticked: a repeated ID, rows two files share (a placeholder date, a
  value written once per block of rows and a unit that differs between stacked files are questions, with a ready answer);
* **note** - an item in the log: the ID report, a date that means "still open".

At most `MAX_QUESTIONS` findings interrupt the person; the rest go on the approval page. A thin or unfinished
newest period is one question with a proposed change (leave the later rows out), not a warning. Every number is
counted here from the rows; nothing is written by the AI.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

import pandas as pd
from pydantic import JsonValue

from engine.agent.contracts import AgentConfidence, ProposalState, RecipeStepKind
from engine.doctor.checks_time_keys import (
    ID_NAME,
    Check,
    block_blank_checks,
    date_checks,
    date_order_checks,
    file_checks,
    key_quality,
    key_rule_checks,
    outcome_direction,
    read_dates,
    summary_check,
    tail_checks,
)
from engine.doctor.protocol import Ask, AskOption, DataRequest
from engine.doctor.visit_contracts import OptionEffect
from engine.utils.logging import get_logger
from engine.utils.time import utc_now

if TYPE_CHECKING:
    from engine.doctor.case import Case, CaseItem
    from engine.doctor.diagnose import LoadedFile
    from engine.doctor.goal_brief import GoalBrief
    from engine.doctor.visit import Detected, DetectorPort

__all__ = ["CHECK_KIND", "MAX_QUESTIONS", "time_keys_detector"]

_LOG = get_logger(__name__)

CHECK_KIND: Final[str] = "data_check"
"""The item kind of a flag or a note: something to look at, with no change attached."""
MAX_QUESTIONS: Final[int] = 3
"""Findings that may interrupt the person with a question; the rest wait on the approval page. A stop (a blocking
question) is always asked, whatever the count."""
MAX_KEY_COLUMNS: Final[int] = 4


def time_keys_detector(*, clock: Callable[[], datetime] = utc_now) -> DetectorPort:
    """The visit's detector for keys, dates and the newest rows (rules only)."""

    def detect(case: Case, files: tuple[LoadedFile, ...], brief: GoalBrief | None) -> Detected:
        from engine.doctor.visit import Detected

        now = clock()
        found: list[tuple[str | None, Check]] = []
        asks: list[tuple[int, Ask]] = []
        requests: list[DataRequest] = []
        for loaded in files:
            try:
                _file(case, loaded, brief, now, found, asks, requests)
            except Exception as exc:  # a file a check cannot read is not a reason to stop the visit
                _LOG.info("doctor.time_keys_failed upload=%s error=%s", loaded.upload_id, type(exc).__name__)
        try:  # one file's repeated headers and copied rows, and rows two files share
            by_name = [(f.file_name or f.upload_id, f.frame) for f in files]
            found.extend((None, check) for check in file_checks(by_name))
        except Exception as exc:
            _LOG.info("doctor.time_keys_files_failed error=%s", type(exc).__name__)
        questions = [ask for _, ask in sorted(asks, key=lambda pair: pair[0])]
        # A stop always stops; the budget only holds back the questions that do not block.
        stops = [ask for ask in questions if ask.blocking]
        others = [ask for ask in questions if not ask.blocking]
        room = max(0, MAX_QUESTIONS - len(stops))
        kept, spilled = [*stops, *others[:room]], others[room:]
        # One fact, one line: a finding a question covers (kept or held back) is not listed again, and nor is a
        # sentence already on the page (the combined table repeats what its files said). An item this visit
        # made before (same id) is only refreshed, never dropped.
        on_page = {i.title for i in case.items if i.kind == CHECK_KIND}
        had = {i.item_id for i in case.items}
        items: list[CaseItem] = []
        for upload_id, check in found:
            if _is_asked(check, upload_id, questions):
                continue
            item = _check_item(upload_id, check, now)
            if item.title in on_page and item.item_id not in had:
                continue
            items.append(item)
            on_page.add(item.title)
        for ask in spilled:
            item = _ask_item(ask, now)
            if item.title in on_page and item.item_id not in had:
                continue
            items.append(item)
            on_page.add(item.title)
        return Detected(asks=tuple(kept), items=tuple(items), requests=tuple(requests))

    detect.reads_current = True  # type: ignore[attr-defined]  # a fix already in the plan is not found again
    return detect


def _is_asked(check: Check, upload_id: str | None, asks: list[Ask]) -> bool:
    return any(
        ask.code == check.code and ask.column == check.column and ask.upload_id == upload_id for ask in asks
    )


def _file(
    case: Case,
    loaded: LoadedFile,
    brief: GoalBrief | None,
    now: datetime,
    found: list[tuple[str | None, Check]],
    asks: list[tuple[int, Ask]],
    requests: list[DataRequest],
) -> None:
    upload_id, frame = loaded.upload_id, loaded.frame
    key = _decided(case, "key", upload_id)
    outcome = _decided(case, "outcome_column", upload_id)
    rows = loaded.file_rows or len(frame)

    # Keys: the report, rounded digits, stand-ins, lost zeros.
    names = [str(c) for c in frame.columns]
    key_columns = list(dict.fromkeys([*([key] if key else []), *(n for n in names if ID_NAME.search(n))]))
    for column in key_columns[:MAX_KEY_COLUMNS]:
        report = key_quality(
            frame[column], column, expect_unique=column == key, exact=loaded.exact.get(column)
        )
        # The ID report goes in for the file's own key, or with a finding about the same column; an ID column that
        # is fine says nothing.
        if column != key and all(check.code == "KEY_REPORT" for check in report):
            continue
        for check in report:
            found.append((upload_id, check))
            if check.code == "KEY_DIGITS_ROUNDED":
                requests.append(_digits_request(check, upload_id))
    # Key tidy-up rules already proposed for this file: IDs they would merge.
    for column, rule in _key_rules(case, upload_id):
        if column in frame.columns:
            found.extend((upload_id, check) for check in key_rule_checks(frame[column], rule, column))

    # The outcome, the right way round.
    meaning = _outcome_meanings(loaded, outcome) if outcome else None
    if outcome and meaning is not None:
        direction = outcome_direction(outcome, *meaning[:2])
        if direction is not None:
            found.append((upload_id, direction))
            asks.append((0, _wrong_way_ask(direction, upload_id, rows)))

    # A summary, not customers; a value written once per block of rows.
    summary = summary_check(frame)
    if summary is not None:
        found.append((upload_id, summary))
        requests.append(_summary_request(upload_id))
    if rows == len(frame):  # the blocks are only seen in the file's own order, not in a random sample
        for check in block_blank_checks(frame):
            found.append((upload_id, check))
            asks.append((2, _block_ask(check, upload_id, rows)))

    # Dates.
    for check in date_checks(frame, now.date()):
        found.append((upload_id, check))
        ask = _placeholder_ask(check, frame, upload_id, rows) if check.code == "DATE_PLACEHOLDER" else None
        if ask is not None:
            asks.append((2, ask))
    if brief is None:  # with a goal, Guided setup's date question already asks for the day/month order
        for check in date_order_checks(frame):
            found.append((upload_id, check))
            asks.append((1, _order_ask(check, upload_id, rows)))

    # The newest rows.
    date_column = _date_column(case, frame, upload_id)
    if date_column is not None:
        dates = read_dates(frame[date_column])
        if dates is not None:
            yes = meaning[2] if meaning is not None else None
            tails = tail_checks(dates, yes, _days_covered(frame))
            keep = min(
                (c for c in tails if c.as_of is not None), key=lambda c: c.as_of or now.date(), default=None
            )
            if keep is not None:  # the question says it all (its earliest date leaves out every later row)
                asks.append((2, _tail_ask(keep, date_column, upload_id, rows)))
            else:
                found.extend((upload_id, check) for check in tails)


# ---------------------------------------------------------------------------
# What the case already knows
# ---------------------------------------------------------------------------
def _decided(case: Case, kind: str, upload_id: str) -> str | None:
    for item in case.items:
        if item.kind == kind and item.upload_id == upload_id and item.state is ProposalState.ACCEPTED:
            column = item.payload.get("column")
            return column if isinstance(column, str) else None
    return None


def _key_rules(case: Case, upload_id: str) -> list[tuple[str, Any]]:
    """`(column, KeyRule)` of the key tidy-up steps proposed or accepted for this file."""
    from engine.agent.key_steps import KeyRule

    out: list[tuple[str, Any]] = []
    for item in case.items:
        step = item.payload.get("step")
        if item.upload_id != upload_id or not isinstance(step, dict):
            continue
        if step.get("kind") == RecipeStepKind.NORMALISE_KEY.value and isinstance(step.get("column"), str):
            try:
                out.append((str(step["column"]), KeyRule.model_validate(step.get("params") or {})))
            except ValueError:
                continue
    return out


def _outcome_meanings(loaded: LoadedFile, outcome: str) -> tuple[int, int, pd.Series[Any]] | None:
    """`(yes rows, no rows, yes per row)` when every value of the outcome has a stated meaning (a known word);
    None when some value has none (the outcome questions settle that first)."""
    from engine.doctor.outcome_definition import rules_meaning, value_text

    if outcome not in loaded.frame.columns:
        return None
    exact = loaded.exact.get(outcome)
    if exact is not None and exact.value_counts:
        counts = dict(exact.value_counts)
    else:
        counts = {
            str(k): int(v) for k, v in loaded.frame[outcome].map(value_text).dropna().value_counts().items()
        }
    said = {value: rules_meaning(value) for value in counts}
    if not counts or any(meaning is None for meaning in said.values()):
        return None
    yes = sum(n for value, n in counts.items() if said[value] == "yes")
    no = sum(n for value, n in counts.items() if said[value] == "no")
    per_row = loaded.frame[outcome].map(value_text).map(lambda v: bool(v) and rules_meaning(str(v)) == "yes")
    return yes, no, per_row.astype(bool)


_NOT_AS_OF: Final[frozenset[str]] = frozenset(
    {"birth", "dob", "born", "signup", "joined", "registered", "registration", "created", "activation"}
    | {"first", "last", "expiry", "expires", "expiration", "due", "renewal", "valid"}
)
"""Dates that say when the customer began, last did something, or when something is due: one per customer, not
how far the data reaches (leaving out the newest `last_order_date` rows would drop the most active customers)."""
_PERIOD_WORDS: Final[frozenset[str]] = frozenset({"month", "period", "snapshot"})
"""A column named for the month or period a row belongs to (`2026-09`) says when it happened, too."""
_DAYS_COVERED: Final[re.Pattern[str]] = re.compile(
    r"^(days?[_ ]?(covered|observed|of[_ ]data)|covered[_ ]days)$", re.I
)


def _days_covered(frame: pd.DataFrame) -> pd.Series[Any] | None:
    """The column that says how many days each row covers (`days_covered`), if the file has one."""
    return next((frame[c] for c in frame.columns if _DAYS_COVERED.match(str(c).strip())), None)


def _date_column(case: Case, frame: pd.DataFrame, upload_id: str) -> str | None:
    """The date that says when each row happened: the accepted as-of column, else the first date column with a
    date on nearly every row that is not a birth, sign-up, expiry or due date."""
    from engine.agent.formats import looks_like_date_name
    from engine.doctor.impossible import column_words

    for item in case.items:
        if (
            item.kind == "as_of_column"
            and item.upload_id == upload_id
            and item.state is ProposalState.ACCEPTED
        ):
            column = item.payload.get("column")
            if isinstance(column, str) and column in frame.columns:
                return column
    for name in frame.columns:
        words = column_words(str(name))
        if (looks_like_date_name(str(name)) or words & _PERIOD_WORDS) and not words & _NOT_AS_OF:
            values = read_dates(frame[name])
            if values is not None and values.notna().mean() >= 0.95:
                return str(name)
    return None


# ---------------------------------------------------------------------------
# The findings, as items, questions and requests
# ---------------------------------------------------------------------------
def _json(numbers: Any) -> JsonValue:
    value: JsonValue = json.loads(json.dumps(numbers, default=str))
    return value


def _check_item(upload_id: str | None, check: Check, now: datetime) -> CaseItem:
    from engine.doctor.visit import _item

    payload: dict[str, JsonValue] = {
        "upload_id": upload_id,
        "code": check.code,
        "level": check.level,
        "column": check.column,
        "numbers": _json(dict(check.numbers)),
    }
    return _item(
        CHECK_KIND,
        payload,
        title=check.text,
        upload_id=upload_id,
        confidence=AgentConfidence.CHECK,
        evidence=(),
        now=now,
    )


def _ask_item(ask: Ask, now: datetime) -> CaseItem:
    """A question past the budget: the same finding, on the approval page."""
    from engine.doctor.visit import _item

    payload: dict[str, JsonValue] = {
        "upload_id": ask.upload_id,
        "code": ask.code,
        "level": "flag",
        "column": ask.column,
        "numbers": {},
    }
    return _item(
        CHECK_KIND,
        payload,
        title=ask.text,
        upload_id=ask.upload_id,
        confidence=AgentConfidence.CHECK,
        evidence=(),
        now=now,
    )


def _digits_request(check: Check, upload_id: str) -> DataRequest:
    column = check.column or "the ID column"
    return DataRequest(
        kind="different_file",
        what=f"the '{column}' column saved as Text, or the .xlsx file itself",
        why="Excel rounded the digits of this column off; they cannot be recovered from the file you sent.",
        columns=(column,),
        per="one row per customer",
        ask_whom="whoever exported the file",
        upload_id=upload_id,
    )


def _wrong_way_ask(check: Check, upload_id: str, rows: int) -> Ask:
    column = check.column or ""
    return Ask(
        code=check.code,
        subject={"upload_id": upload_id, "column": column},
        text=check.text,
        why="Churn, default and fraud are the rarer outcome. If the file marks the good outcome as the "
        "positive one, the model would learn the opposite.",
        options=(
            AskOption(label="Yes, it is the right way round", effect=OptionEffect(option_id="right")),
            AskOption(
                label="No, I will send the file with the values the other way round",
                effect=OptionEffect(option_id="other_way"),
                safe=True,
            ),
        ),
        rows=rows,
        upload_id=upload_id,
        column=column,
        blocking=True,
        meaning_bearing=True,
    )


def _order_ask(check: Check, upload_id: str, rows: int) -> Ask:
    column = check.column or ""
    return Ask(
        code=check.code,
        subject={"upload_id": upload_id, "column": column},
        text=check.text + " What would you like to do?",
        why="A date written 03/04/2024 is March or April depending on the order; nothing is guessed.",
        options=(
            AskOption(label="Leave this column out", effect=OptionEffect(option_id="leave_out"), safe=True),
            AskOption(
                label="I will send the dates written one way (2024-03-25)",
                effect=OptionEffect(option_id="new_file"),
            ),
        ),
        rows=rows,
        upload_id=upload_id,
        column=column,
        blocking=True,
        meaning_bearing=True,
    )


def _tail_ask(check: Check, column: str, upload_id: str, rows: int) -> Ask:
    from engine.doctor.time_settings import maturity_step

    keep = check.as_of.isoformat() if check.as_of else ""
    after = int(check.numbers.get("rows_after", 0))
    step = maturity_step(column, keep).model_dump(mode="json")
    leave = OptionEffect(
        option_id="leave_out",
        kind="recipe_step",
        title=f"Use {keep} as the as-of date and leave out the {after:,} rows dated after it.",
        payload={"upload_id": upload_id, "path": None, "value": None, "step": step, "as_of": keep},
    )
    return Ask(
        code="THIN_LAST_PERIOD",
        subject={"upload_id": upload_id, "column": column},
        text=f"The newest rows of '{column}' look unfinished. {check.text} Leave them out and use {keep} as the "
        "as-of date?",
        why=check.text,
        options=(
            AskOption(label="Leave them out", effect=leave, rows_changed=after, safe=True),
            AskOption(label="Keep them", effect=OptionEffect(option_id="keep"), rows_changed=0),
        ),
        rows=rows,
        upload_id=upload_id,
        column=column,
        recommended="leave_out",
    )


def _summary_request(upload_id: str) -> DataRequest:
    from engine.pilot.data_request import render_request_message

    return DataRequest(
        kind="different_file",
        what="the row-level export (one row per customer, order or event), not the summary",
        why=render_request_message("SUMMARY_NOT_ROWS"),
        per="one row per customer",
        ask_whom="whoever exported the file",
        upload_id=upload_id,
    )


def _placeholder_ask(check: Check, frame: pd.DataFrame, upload_id: str, rows: int) -> Ask | None:
    """A date that stands in for a blank (1970-01-01): treat it as blank, or keep it. None when the column is not
    text or has too many spellings for one step; the check then stays on the approval page as a flag."""
    from engine.agent.contracts import RecipeStep
    from engine.agent.value_steps import MAX_MISSING_WORDS

    column = check.column or ""
    day = str(check.numbers.get("date", ""))
    dates = read_dates(frame[column])
    if dates is None or pd.api.types.is_numeric_dtype(frame[column]):
        return None
    cells = frame[column].dropna().astype(str).str.strip()
    spellings = list(dict.fromkeys(cells[dates.reindex(cells.index).dt.strftime("%Y-%m-%d") == day]))
    if not spellings or len(spellings) > MAX_MISSING_WORDS:
        return None
    count = int(check.numbers.get("rows", 0))
    step = RecipeStep(
        order=1, kind=RecipeStepKind.SET_MISSING_TEXT, column=column, params={"values": spellings}
    ).model_dump(mode="json")
    blank = OptionEffect(
        option_id="blank",
        kind="recipe_step",
        title=f"Treat {day} in '{column}' as blank ({count:,} rows).",
        payload={"upload_id": upload_id, "path": None, "value": None, "step": step},
    )
    return Ask(
        code=check.code,
        subject={"upload_id": upload_id, "column": column},
        text=f"{check.text} Treat {day} as blank?",
        why="A system writes this date when nothing was entered. Kept as a real date, it looks like a real "
        "event in the past.",
        options=(
            AskOption(label="Treat it as blank", effect=blank, rows_changed=count),
            AskOption(label="Keep it as a real date", effect=OptionEffect(option_id="keep"), rows_changed=0),
        ),
        rows=rows,
        upload_id=upload_id,
        column=column,
        recommended="blank",
    )


def _block_ask(check: Check, upload_id: str, rows: int) -> Ask:
    column = check.column or ""
    return Ask(
        code=check.code,
        subject={"upload_id": upload_id, "column": column},
        text=check.text,
        why="If a blank means 'same as above', every blank row has a value that is not written; if it means "
        "'not known', filling it would invent one.",
        options=(
            AskOption(
                label="Yes: I will send the file with it filled in on every row",
                effect=OptionEffect(option_id="send_filled"),
            ),
            AskOption(label="No: leave the blanks as blank", effect=OptionEffect(option_id="leave")),
        ),
        rows=rows,
        upload_id=upload_id,
        column=column,
        recommended="send_filled",
    )
