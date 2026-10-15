"""The Data Doctor checks the keys, the dates and the newest rows of every file, with no AI.

One detector (`engine.doctor.visit.DetectorPort`) runs the guards of `engine.doctor.checks_time_keys` on the rows
the visit read (the whole file, or a random sample of it) and gives back, by how serious each finding is:

* **stop** - a data request (IDs Excel rounded off: the digits are gone, send the file again) or a blocking
  question (dates written in two orders; an outcome that looks the wrong way round), or, for a step that cannot
  run, an item that says so;
* **flag** - an item on the approval page, never ticked: a placeholder date, a repeated ID, rows two files share;
* **note** - an item in the log: the ID report, a date that means "still open".

At most `MAX_QUESTIONS` findings interrupt the person; the rest go on the approval page. A thin or unfinished
newest period is one question with a proposed change (leave the later rows out), not a warning. Every number is
counted here from the rows; nothing is written by the AI.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

import pandas as pd
from pydantic import JsonValue

from engine.agent.contracts import AgentConfidence, ProposalState, RecipeStepKind
from engine.doctor.checks_time_keys import (
    Check,
    date_checks,
    date_order_checks,
    file_checks,
    key_quality,
    key_rule_checks,
    outcome_direction,
    read_dates,
    tail_checks,
)
from engine.doctor.protocol import Ask, AskOption, DataRequest
from engine.doctor.sample_rows import KEY_NAME
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
"""Findings that may interrupt the person with a question; the rest wait on the approval page."""
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
        if len(files) > 1:
            by_name = [(f.file_name or f.upload_id, f.frame) for f in files]
            found.extend((None, check) for check in file_checks(by_name))
        questions = [ask for _, ask in sorted(asks, key=lambda pair: pair[0])]
        kept, spilled = questions[:MAX_QUESTIONS], questions[MAX_QUESTIONS:]
        items = [
            _check_item(upload_id, check, now) for upload_id, check in found if not _is_asked(check, kept)
        ]
        items.extend(_ask_item(ask, now) for ask in spilled)
        return Detected(asks=tuple(kept), items=tuple(items), requests=tuple(requests))

    return detect


def _is_asked(check: Check, asks: list[Ask]) -> bool:
    return any(ask.code == check.code and ask.column == check.column for ask in asks)


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
    key_columns = list(dict.fromkeys([*([key] if key else []), *(n for n in names if KEY_NAME.search(n))]))
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

    # Dates.
    found.extend((upload_id, check) for check in date_checks(frame, now.date()))
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
            tails = tail_checks(dates, yes)
            found.extend((upload_id, check) for check in tails)
            keep = min(
                (c for c in tails if c.as_of is not None), key=lambda c: c.as_of or now.date(), default=None
            )
            if keep is not None:
                asks.append((2, _tail_ask(keep, date_column, upload_id, rows)))


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


def _date_column(case: Case, frame: pd.DataFrame, upload_id: str) -> str | None:
    """The date that says when each row happened: the accepted as-of column, else the first date column with a
    date on nearly every row."""
    from engine.agent.formats import looks_like_date_name

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
        if looks_like_date_name(str(name)):
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
        payload={"upload_id": upload_id, "path": None, "value": None, "step": step},
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
