"""The Data Doctor combines several files, with no AI: a join or a stack, proposed as one change with its counts.

One detector (`engine.doctor.visit.DetectorPort`) reads how the case's files fit together
(`engine.doctor.joins.how_files_fit`, kept in the case's diagnosis) and gives back:

* **A join** when a customer list and the logs about its customers (orders, bills, tickets) meet on a confident
  customer column: one `build_dataset` item, never ticked for the person, that combines them into one row per
  customer. It says in plain words how many customers were matched ("7,950 of 8,214 customers matched"), how many
  log rows belong to nobody on the list, and how the IDs are made to meet (`C-00123` against `123`). It always
  needs its own yes, because it replaces the files with one table.
* **A stack** when files are the same report for different months: one `stack_files` item. A row two exports both
  hold is counted once, and the item says how many.
* **A question, not a proposal**, when the customer column is not clear: a log whose customers barely meet the
  list even after the IDs are tidied (nothing is matched by being similar), or a customer list with a customer on
  more than one row. Monthly customer lists are stacked first; the join is proposed on the stacked list next.

The counts come from the rows the visit read. `engine.doctor.combine_run` builds the table from the whole files
once the item is approved.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING, Final

from pydantic import JsonValue, ValidationError

from engine.agent.contracts import AgentConfidence
from engine.agent.tools import AgentToolError
from engine.doctor.build_driver import BuildDriveError, BuildPlan, prepare_sources
from engine.doctor.checks_time_keys import unit_scale_checks
from engine.doctor.combine_run import (
    JOIN_KIND,
    STACK_KIND,
    _one_row_per_customer,
    _real_keys,
    guard_join,
    guard_stack,
    plan_of,
    rules_digest,
)
from engine.doctor.contracts import Diagnosis, FileStack
from engine.doctor.protocol import Ask, AskOption
from engine.doctor.questions import build_ask
from engine.doctor.stack_files import StackError, StackProposal, plan_stack, run_stack
from engine.doctor.visit_contracts import OptionEffect
from engine.privacy.working_key import WorkingKeyError, new_salt, pii_key_kind, refuse_aadhaar
from engine.utils.logging import get_logger
from engine.utils.time import utc_now

if TYPE_CHECKING:
    from engine.doctor.case import Case, CaseItem
    from engine.doctor.diagnose import LoadedFile
    from engine.doctor.goal_brief import GoalBrief
    from engine.doctor.visit import Detected, DetectorPort

__all__ = ["combining_detector"]

_LOG = get_logger(__name__)

_ENTITY: Final[str] = "entity"
_KEEP_SEPARATE: Final[str] = "Keep the files separate"


def combining_detector(*, clock: Callable[[], datetime] = utc_now) -> DetectorPort:
    """The visit's combining detector (several files in the case, no AI)."""

    def detect(case: Case, files: tuple[LoadedFile, ...], brief: GoalBrief | None) -> Detected:
        from engine.doctor.visit import Detected

        del brief
        if len(files) < 2 or not case.diagnosis:
            return Detected()
        try:
            diagnosis = Diagnosis.model_validate(case.diagnosis)
            items, asks = _propose(diagnosis, {f.upload_id: f for f in files}, clock())
        except (ValidationError, BuildDriveError, StackError) as exc:  # not a reason to stop the visit
            _LOG.info("doctor.combining_failed case=%s error=%s", case.case_id, type(exc).__name__)
            return Detected()
        return Detected(asks=tuple(asks), items=tuple(items))

    return detect


def _propose(
    diagnosis: Diagnosis, loaded: dict[str, LoadedFile], now: datetime
) -> tuple[list[CaseItem], list[Ask]]:
    items: list[CaseItem] = []
    asks: list[Ask] = []
    stacks = [s for s in diagnosis.stacks if all(u in loaded for u in s.upload_ids)]
    covered: set[str] = set()
    if not any(
        s.kind == _ENTITY for s in stacks
    ):  # monthly customer lists are stacked before they are joined
        join, join_asks, covered = _join(diagnosis, loaded, now)
        items.extend([join] if join is not None else [])
        asks.extend(join_asks)
    for stack in stacks:
        if not covered.intersection(stack.upload_ids):
            item, ask = _stack(stack, diagnosis, loaded, now)
            items.extend([item] if item is not None else [])
            asks.extend([ask] if ask is not None else [])
    return items, asks


def _names(ids: tuple[str, ...], loaded: dict[str, LoadedFile]) -> str:
    names = [loaded[u].file_name or u for u in ids]
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def _sampled(diagnosis: Diagnosis, loaded: dict[str, LoadedFile], ids: tuple[str, ...]) -> str:
    rows = {d.upload_id: d.rows for d in diagnosis.files}
    read = [len(loaded[u].frame) for u in ids if len(loaded[u].frame) < rows.get(u, 0)]
    return f" Counted on a random sample of {max(read):,} rows of each long file." if read else ""


# ---------------------------------------------------------------------------
# A join: the customer list and its logs
# ---------------------------------------------------------------------------
def _join(
    diagnosis: Diagnosis, loaded: dict[str, LoadedFile], now: datetime
) -> tuple[CaseItem | None, list[Ask], set[str]]:
    try:
        plan = plan_of(diagnosis, {u: f.frame for u, f in loaded.items()})
    except BuildDriveError:  # no customer list: nothing to join onto
        return None, [], set()
    asks = [
        _ask(
            "FILES_DO_NOT_MEET",
            left.upload_id,
            f"{left.summary} What would you like to do?",
            "Customers are only matched by an ID that is the same, never by one that looks alike.",
            loaded,
        )
        for left in plan.left_out
        if left.code == "JOIN_KEYS_DO_NOT_MEET" and left.upload_id in loaded
    ]
    if not plan.events or plan.blocking or plan.questions:
        return None, asks, set()
    entity = plan.entity
    keys = loaded[entity.upload_ids[0]].frame
    column = next(c for c in keys.columns if str(c) == entity.key_column)
    try:
        refuse_aadhaar(pii_key_kind(keys[column], str(column)))
    except WorkingKeyError as exc:  # an Aadhaar number is never stored: the person picks another ID
        asks.append(
            _ask(
                exc.code,
                entity.upload_ids[0],
                f"{exc.message} What would you like to do?",
                "An Aadhaar number is never stored.",
                loaded,
            )
        )
        return None, asks, set()
    missing = int(keys[column].isna().sum())
    repeated = int(keys[column].dropna().duplicated().sum())
    if repeated or missing:
        problem = (
            f"{repeated:,} rows for customers who are already on another row"
            if repeated
            else f"{missing:,} rows with no customer ID"
        )
        asks.append(
            _ask(
                "CUSTOMER_REPEATS",
                entity.upload_ids[0],
                f"{entity.source_name} has {problem}, so it cannot be combined into one row per customer. "
                "What would you like to do?",
                "A table with one row per customer needs each customer once.",
                loaded,
            )
        )
        return None, asks, set()
    log_ids = tuple(u for source in plan.events for u in source.upload_ids)
    return _join_item(plan, diagnosis, loaded, log_ids, now), asks, set(log_ids)


def _ask(code: str, upload_id: str, text: str, why: str, loaded: dict[str, LoadedFile]) -> Ask:
    return Ask(
        code=code,
        subject={"upload_id": upload_id},
        text=text,
        why=why,
        options=(
            AskOption(label=_KEEP_SEPARATE, effect=OptionEffect(option_id="c0"), safe=True),
            AskOption(
                label="I will check the ID columns and add the files again",
                effect=OptionEffect(option_id="c1"),
            ),
        ),
        rows=len(loaded[upload_id].frame),
        upload_id=upload_id,
        meaning_bearing=True,
    )


def _join_item(
    plan: BuildPlan,
    diagnosis: Diagnosis,
    loaded: dict[str, LoadedFile],
    log_ids: tuple[str, ...],
    now: datetime,
) -> CaseItem | None:
    from engine.doctor.visit import _item

    files = {s.source_name: [loaded[u].frame for u in s.upload_ids] for s in (plan.entity, *plan.events)}
    (entity, base), *logs = prepare_sources(plan, files).sources
    key = next(c for c in base.columns if str(c) == entity.key_column)
    seen: set[str] = set()
    for source, frame in logs:
        seen.update(frame[next(c for c in frame.columns if str(c) == source.key_column)].astype(str))
    total = len(base)
    matched = int(base[key].astype(str).isin(seen).sum())
    rows = sum(s.rows or 0 for s in plan.events)
    left_out = sum(s.rows_dropped or 0 for s in plan.events)
    ids = (entity.upload_ids[0], *log_ids)
    lists = _names(entity.upload_ids, loaded)
    logs_named = _names(log_ids, loaded)
    title = (
        f"Combine {lists} with {logs_named} into one table with one row per customer: "
        f"{matched:,} of {total:,} customers matched."
    )
    if left_out:
        title += (
            f" {left_out:,} of {rows:,} rows in {logs_named} belong to someone who is not in {lists} "
            "and are left out."
        )
    rules = [
        f"{loaded[s.upload_ids[0]].file_name}: {s.key_rule.words()}"
        for s in (plan.entity, *plan.events)
        if not s.key_rule.changes_nothing
    ]
    if rules:
        title += f" To match the IDs: {'; '.join(rules)}. {lists} keeps its own IDs."
    twice = sum(r.duplicates_removed for r in plan.stack_receipts)
    if twice:
        title += f" {twice:,} rows that two exports both hold are counted once."
    title += _sampled(diagnosis, loaded, ids)
    payload: dict[str, JsonValue] = {
        "upload_ids": list(ids),
        "rules": rules_digest(plan),
        "customers": total,
        "customers_matched": matched,
        "log_rows": rows,
        "log_rows_left_out": left_out,
    }
    try:  # what the combined table would look like, counted by code on the rows the visit read
        table, vault = _one_row_per_customer(plan, files, new_salt())
        receipt, findings = guard_join(plan, files, _real_keys(table, vault))
        payload["receipt"] = receipt.model_dump(mode="json")
        payload["findings"] = [f.model_dump(mode="json") for f in findings]
        payload["needs_yes"] = any(f.level == "stop" for f in findings)
    except (
        AgentToolError,
        BuildDriveError,
        WorkingKeyError,
    ):  # no dates in the logs: no receipt, the join is still offered
        pass
    # No `rows_in` / `rows_out`: a join always needs its own yes, however few rows it leaves out.
    return _item(
        JOIN_KIND,
        payload,
        title=title,
        upload_id=ids[0],
        confidence=AgentConfidence.CHECK,
        evidence=(),
        now=now,
    )


# ---------------------------------------------------------------------------
# A stack: the same report for different months
# ---------------------------------------------------------------------------
def _unit_ask(stack: FileStack, loaded: dict[str, LoadedFile]) -> Ask | None:
    """Q6, blocking, when a number column is far larger in one stacked file than in another (rupees and paise): the
    stack would add up amounts in two units. The stack is still offered; the answer comes before the approval.
    """
    named = [(loaded[u].file_name or u, loaded[u].frame) for u in stack.upload_ids]
    gaps = unit_scale_checks(named)
    if not gaps:
        return None
    column, numbers = gaps[0].column or "", gaps[0].numbers
    hint = (
        f"about {numbers['gap']:,.0f} times larger in {numbers['larger']} than in {numbers['smaller']}, "
        "as if the files use different units (for example rupees and paise)"
    )
    ask = build_ask(
        "Q6",
        {"column": column, "hint": hint},
        rows=sum(len(frame) for _, frame in named),
        choices=(
            "The files use different units: I will send them in one unit",
            "All the files use the same unit",
        ),
        recommended_choice=0,
        upload_id=stack.upload_ids[0],
        column=column,
    )
    return replace(ask, blocking=True)


def _stack(
    stack: FileStack, diagnosis: Diagnosis, loaded: dict[str, LoadedFile], now: datetime
) -> tuple[CaseItem | None, Ask | None]:
    from engine.doctor.visit import _item

    first = next(d for d in diagnosis.files if d.upload_id == stack.upload_ids[0])
    frames = {u: loaded[u].frame for u in stack.upload_ids}
    date = first.date_columns[0] if first.date_columns else None

    def propose(rule: str | None) -> StackProposal:
        try:
            return plan_stack(stack, frames, key_column=first.id_column, date_column=date, on_conflict=rule)  # type: ignore[arg-type]
        except StackError:  # a column the conflicts read is missing from a file: stack without that check
            return plan_stack(stack, frames, on_conflict=None)

    def title_and_payload(proposal: StackProposal) -> tuple[str, dict[str, JsonValue]] | None:
        receipt = proposal.receipt
        if receipt is None:
            return None
        names = _names(stack.upload_ids, loaded)
        once = (
            f"{receipt.duplicates_removed:,} rows that two exports both hold are counted once"
            if receipt.duplicates_removed
            else "no row is in two exports"
        )
        if receipt.conflict_rows_removed:
            once += f", and {receipt.conflict_rows_removed:,} rows that two exports give differently keep one version"
        title = (
            f"Stack {names} into one table: {receipt.rows_in:,} rows in the files, {once}, "
            f"{receipt.rows_out:,} rows in the table.{_sampled(diagnosis, loaded, stack.upload_ids)}"
        )
        payload: dict[str, JsonValue] = {
            "upload_ids": list(stack.upload_ids),
            "spec": proposal.spec.model_dump(mode="json"),
            "rows_in": receipt.rows_in,
            "rows_out": receipt.rows_out,
            "duplicates_removed": receipt.duplicates_removed,
        }
        try:
            stacked = [frames[f.upload_id] for f in proposal.spec.files]
            seen, findings = guard_stack(
                stacked, run_stack(stacked, proposal.spec).frame, proposal.spec.key_column
            )
            payload["receipt"] = seen.model_dump(mode="json")
            payload["findings"] = [f.model_dump(mode="json") for f in findings]
            payload["needs_yes"] = any(f.level == "stop" for f in findings)
        except StackError:  # the stack cannot be built on these rows: no receipt
            pass
        return title, payload

    proposal = propose(None)
    made = title_and_payload(proposal)
    if made is not None:
        title, payload = made
        item = _item(
            STACK_KIND,
            payload,
            title=title,
            upload_id=stack.upload_ids[0],
            confidence=AgentConfidence.CHECK,
            evidence=(),
            now=now,
        )
        return item, _unit_ask(stack, loaded)
    # Two exports give the same customer and day different values: one question, with each answer's item.
    assert proposal.question is not None
    options: list[AskOption] = []
    for index, option in enumerate(proposal.question.options):
        answered = title_and_payload(propose(option.option_id))
        if answered is None:
            return None, None
        title, payload = answered
        options.append(
            AskOption(
                label=option.label,
                effect=OptionEffect(option_id=f"c{index}", kind=STACK_KIND, title=title, payload=payload),
                rows_changed=int(payload["rows_in"]) - int(payload["rows_out"]),  # type: ignore[arg-type]
            )
        )
    return None, Ask(
        code="STACK_CONFLICTS",
        subject={"upload_ids": list(stack.upload_ids)},
        text=proposal.question.text,
        why="The same customer and day is written differently in two exports of the same report.",
        options=tuple(options),
        rows=sum(len(f) for f in frames.values()),
        upload_id=stack.upload_ids[0],
        blocking=True,
        meaning_bearing=True,
    )
