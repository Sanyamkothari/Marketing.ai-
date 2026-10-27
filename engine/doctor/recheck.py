"""Next month's recheck: a client who reached ready uploads the next file, and the approved plan replays on it.

The replay IS the materialisation (:func:`engine.doctor.materialize.materialize_plan`): the same accepted steps, the
same table names, the same guards, run once on the old files (the baseline) and once on the new ones. Nothing here
runs a step of its own, and there is no second replay path. What this module adds is the comparison:

* **drift checks** of the new file against the approved run - columns added, removed or renamed; a type that
  changed; the row count; empty shares that jumped; categories that are new or gone; the date range, which is
  expected to move forward; the outcome rate. Counts, shares and names only - never a cell.
* **only what changed is asked again.** Each finding names its subject the way the case does (`engine.doctor.subjects`:
  `question@CODE:column#upload` for a question, `data_check@CODE:upload:column` for a stop). A same-shape file has no
  finding: every item keeps its approval, and the new case opens with nothing to answer. A renamed column is one
  question; its yes adds one `column_alias` item and the approved steps run unchanged on the file.
* **the training snapshot moves with the data, never into the future.** The case's as-of date and training snapshot
  move forward by the days the data moved; the snapshot is held back so the outcome window still ends inside the data,
  and the scoring date is never later than the day after the data ends. A step that froze one of the old dates
  (a query's `as_of`) is moved with them.

Hard rules stay hard: at least 1,000 rows and 200 positives, the guards' leakage and consent stops, and a step that
cannot run are stops nobody may say yes to. The person approves every change and every meaning: the replay changes
nothing about the approved plan, and a changed subject waits for an answer.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Literal

import pandas as pd
from pydantic import Field

from engine.agent.contracts import AgentConfidence, ProposalSource, ProposalState
from engine.agentic.canonical import canonical_bytes, sha256_hex
from engine.config import StrictBase
from engine.doctor.case import (
    Case,
    CaseDoubt,
    CaseFile,
    CaseItem,
    CaseOption,
    CaseQuestion,
    CaseRecheck,
    EventDraft,
    TimelineKind,
    payload_digest,
)
from engine.doctor.decisions import decisions, sets_of_effect
from engine.doctor.materialize import (
    ALIAS_KIND,
    Full,
    Prepared,
    materialize,
    materialize_plan,
    roles_of,
)
from engine.doctor.plan_check import recipe_steps_of
from engine.doctor.prescription_contract import (
    OUTCOME_RATE_FLOOR,
    OUTCOME_RATE_MOVE,
    DataContract,
    learn_contract,
    type_group,
)
from engine.doctor.prescription_contract import check_contract as check_prepared
from engine.doctor.renames import match_renames, recipe_columns
from engine.doctor.subjects import doubt_subject, question_subject
from engine.doctor.visit import active_goal, active_section, is_approved
from engine.doctor.visit_contracts import CaseVisit, OptionEffect

if TYPE_CHECKING:
    from engine.doctor.case import CaseStore
    from engine.doctor.diagnose import LoadedFile
    from engine.storage import Storage

__all__ = [
    "MIN_POSITIVES",
    "MIN_ROWS",
    "DateMove",
    "RecheckError",
    "RecheckFinding",
    "RecheckReport",
    "Rechecked",
    "open_recheck_case",
    "read_report",
    "recheck",
    "report_key",
]

MIN_ROWS: Final[int] = 1_000
MIN_POSITIVES: Final[int] = 200
"""The hard floor of a training file: below either, no yes lifts the stop."""
ROW_COUNT_BAND: Final[tuple[float, float]] = (0.5, 2.0)
"""A new file with fewer than half or more than twice the rows of the approved one is asked about."""
MISSING_CATEGORY_SHARE: Final[float] = 0.05
"""A value that held this share of an approved closed column and is gone now is asked about."""

Level = Literal["question", "stop", "note"]


class RecheckError(Exception):
    """A recheck that cannot be made (not ready, files not matched, the approved run cannot be reproduced)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class RecheckFinding(StrictBase):
    """One thing that differs from the approved run. Names, counts and shares only."""

    code: str = Field(
        description="`COLUMN_RENAMED`, `COLUMN_MISSING`, `NEW_CATEGORY`, `OUTCOME_RATE_SHIFT`, ..."
    )
    level: Level = Field(
        description="`question`: asked again; `stop`: the new file does not go on; `note`: said, nothing to answer."
    )
    text: str = Field(description="What changed, in plain words with counts.")
    subject: str = Field(
        description="What it is about, as the case names a subject (`engine.doctor.subjects`)."
    )
    upload_id: str | None = Field(default=None, description="The new upload it is about.")
    column: str | None = None
    question_id: str | None = Field(default=None, description="The question that asks it, when it asks one.")
    hard: bool = Field(default=False, description="A rule nobody may say yes to.")


class DateMove(StrictBase):
    """Where the data and the case's two dates stood, and where they stand now."""

    data_end_before: date | None = None
    data_end_after: date | None = None
    as_of_before: date | None = None
    as_of_after: date | None = None
    training_as_of_before: date | None = None
    training_as_of_after: date | None = None
    moved: bool = Field(default=False, description="The data moved forward and the dates moved with it.")


class RecheckReport(StrictBase):
    """The drift report of one recheck."""

    source_case_id: str
    case_id: str | None = Field(default=None, description="The case the recheck opened, once it is made.")
    uploads: dict[str, str] = Field(description="Old upload id -> new upload id.")
    rows_before: int
    rows_after: int
    dates: DateMove = Field(default_factory=DateMove)
    kept_items: int = Field(description="Approved items that keep their approval.")
    findings: tuple[RecheckFinding, ...] = ()

    @property
    def questions(self) -> tuple[RecheckFinding, ...]:
        return tuple(f for f in self.findings if f.level == "question")

    @property
    def stops(self) -> tuple[RecheckFinding, ...]:
        return tuple(f for f in self.findings if f.level == "stop")

    @property
    def passed(self) -> bool:
        """Nothing to ask and nothing stopped: the approved plan stands as it was approved."""
        return not (self.questions or self.stops)


@dataclass(frozen=True)
class Rechecked:
    """The report, and what the follow-up case holds (the old case is never changed)."""

    report: RecheckReport
    items: tuple[CaseItem, ...]
    questions: tuple[CaseQuestion, ...]
    doubts: tuple[CaseDoubt, ...]
    offers: dict[str, tuple[OptionEffect, ...]]


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------
def _as_dates(series: pd.Series[Any]) -> pd.Series[Any]:
    if pd.api.types.is_datetime64_any_dtype(series):
        return series.dropna()
    if pd.api.types.is_numeric_dtype(series):
        return pd.Series(dtype="datetime64[ns]")
    parsed = pd.to_datetime(series, errors="coerce", format="mixed")
    return parsed.dropna() if parsed.notna().mean() >= 0.9 else pd.Series(dtype="datetime64[ns]")


def _data_end(frame: pd.DataFrame, columns: Sequence[str]) -> date | None:
    ends = [
        found.max()
        for name in columns
        if name in frame.columns and not (found := _as_dates(frame[name])).empty
    ]
    return max(ends).date() if ends else None


def _date_columns(prepared: pd.DataFrame, as_of_column: str | None) -> list[str]:
    named = [as_of_column] if as_of_column else []
    return [
        *named,
        *(str(c) for c in prepared.columns if type_group(prepared[c]) == "date" and c != as_of_column),
    ]


def _window_days(items: Sequence[CaseItem], window: int | None) -> int | None:
    if window is not None:
        return window
    found = [
        int(step.params["outcome_window_days"])
        for i in items
        if isinstance(i.payload.get("step"), dict)
        for step in [_step_of(i)]
        if step is not None and isinstance(step.params.get("outcome_window_days"), int)
    ]
    return max(found) if found else None


def _step_of(item: CaseItem) -> Any:
    from engine.agent.contracts import RecipeStep

    try:
        return RecipeStep.model_validate(item.payload["step"])
    except (ValueError, KeyError):
        return None


def moved_dates(
    *,
    end_before: date | None,
    end_after: date | None,
    as_of: date | None,
    training: date | None,
    window: int | None,
) -> DateMove:
    """The case's dates for the new file: forward by the days the data moved, never into the future.

    The scoring date is at most the day after the data ends; the training snapshot is held back so its outcome window
    (when there is one) ends inside the data. Data that did not move forward moves nothing."""
    if end_before is None or end_after is None or end_after <= end_before:
        return DateMove(
            data_end_before=end_before,
            data_end_after=end_after,
            as_of_before=as_of,
            as_of_after=as_of,
            training_as_of_before=training,
            training_as_of_after=training,
        )
    shift = timedelta(days=(end_after - end_before).days)
    new_as_of = None if as_of is None else min(as_of + shift, end_after + timedelta(days=1))
    new_training = None
    if training is not None:
        latest = end_after - timedelta(days=window or 0)
        new_training = min(training + shift, latest)
    return DateMove(
        data_end_before=end_before,
        data_end_after=end_after,
        as_of_before=as_of,
        as_of_after=new_as_of,
        training_as_of_before=training,
        training_as_of_after=new_training,
        moved=True,
    )


def _move_value(value: Any, mapping: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        return mapping.get(value, value)
    if isinstance(value, dict):
        return {k: _move_value(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [_move_value(v, mapping) for v in value]
    return value


def _date_mapping(moved: DateMove) -> dict[str, str]:
    pairs = (
        (moved.as_of_before, moved.as_of_after),
        (moved.training_as_of_before, moved.training_as_of_after),
    )
    return {a.isoformat(): b.isoformat() for a, b in pairs if a is not None and b is not None and a != b}


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------
def _short(value: Any) -> str:
    return sha256_hex(canonical_bytes(value))[:12]


@dataclass
class _Found:
    """The findings so far, with the questions and stops they open."""

    findings: list[RecheckFinding]
    questions: list[CaseQuestion]
    doubts: list[CaseDoubt]
    offers: dict[str, tuple[OptionEffect, ...]]
    now: datetime

    def ask(
        self,
        code: str,
        text: str,
        *,
        upload_id: str | None,
        column: str | None = None,
        yes: tuple[str, OptionEffect] | None = None,
    ) -> None:
        """One question about a subject: `yes` is the answer that changes something (its effect, with its label)."""
        question_id = f"q.recheck_{code.lower()}.base.{_short([upload_id, column])}"
        agree = yes[0] if yes else "Yes, that is expected; go on"
        effect = yes[1] if yes else OptionEffect(option_id="yes")
        options = (
            CaseOption(
                option_id="yes",
                label=agree,
                effect_digest=payload_digest(effect.payload) if effect.kind is not None else None,
                sets=sets_of_effect(effect.kind),
            ),
            CaseOption(
                option_id="no",
                label="No, I need to look at it first",
                sets="note",
                safe=True,
            ),
        )
        question = CaseQuestion(
            question_id=question_id,
            code=f"RECHECK_{code}",
            upload_id=upload_id,
            column=column,
            text=text,
            options=options,
            blocking=True,
        )
        self.questions.append(question)
        self.offers[question_id] = (
            effect.model_copy(update={"option_id": "yes"}),
            OptionEffect(option_id="no"),
        )
        self.findings.append(
            RecheckFinding(
                code=code,
                level="question",
                text=text,
                subject=question_subject(question),
                upload_id=upload_id,
                column=column,
                question_id=question_id,
            )
        )

    def stop(
        self, code: str, text: str, *, upload_id: str | None, column: str | None = None, hard: bool = False
    ) -> None:
        doubt = CaseDoubt(
            doubt_id=f"d.recheck_{code.lower()}.{_short([upload_id, column])}"[:128],
            because=text,
            would_do="Stop here: the new file is not used until this is settled.",
            if_not="Nothing is trained or scored with this file.",
            upload_id=upload_id,
        )
        self.doubts.append(doubt)
        where = upload_id or "case"
        self.findings.append(
            RecheckFinding(
                code=code,
                level="stop",
                text=text,
                subject=f"{doubt_subject(doubt)}:{where}" + (f":{column}" if column else ""),
                upload_id=upload_id,
                column=column,
                hard=hard,
            )
        )

    def note(self, code: str, text: str, *, upload_id: str | None, column: str | None = None) -> None:
        self.findings.append(
            RecheckFinding(
                code=code,
                level="note",
                text=text,
                subject=f"note@{code}:{upload_id or 'case'}" + (f":{column}" if column else ""),
                upload_id=upload_id,
                column=column,
            )
        )


def _profiles(file: LoadedFile) -> dict[str, Any]:
    columns = getattr(file.profile, "columns", ()) if file.profile is not None else ()
    return {str(c.name): c for c in columns}


def _alias_item(upload_id: str, column: str, as_: str, now: datetime, *, decided_by: str | None) -> CaseItem:
    payload: dict[str, Any] = {"column": column, "as": as_, "upload_id": upload_id}
    return CaseItem(
        item_id=f"alias.{_short([upload_id, column, as_])}",
        kind=ALIAS_KIND,
        title=f"'{column}' is the column the plan reads as '{as_}'",
        source=ProposalSource.RULES,
        confidence=AgentConfidence.CHECK,
        state=ProposalState.ACCEPTED,
        upload_id=upload_id,
        payload=payload,
        payload_digest=payload_digest(payload),
        proposed_at=now,
        decided_by=decided_by,
        decided_at=None if decided_by is None else now,
    )


def _columns(
    found: _Found,
    old: LoadedFile,
    new: LoadedFile,
    new_id: str,
    protected: frozenset[str],
    now: datetime,
) -> list[CaseItem]:
    """Columns added, removed or renamed. Returns the aliases to try (provisional: not the person's yes yet)."""
    old_names, new_names = [str(c) for c in old.frame.columns], [str(c) for c in new.frame.columns]
    missing = [c for c in old_names if c not in new_names]
    fresh = [c for c in new_names if c not in old_names]
    have_old, have_new = _profiles(old), _profiles(new)
    suggested, unresolved = match_renames(
        [have_old[c] for c in missing if c in have_old],
        [have_new[c] for c in fresh if c in have_new],
        old_rows=getattr(old.profile, "row_count", len(old.frame)),
        new_rows=getattr(new.profile, "row_count", len(new.frame)),
    )
    trying: list[CaseItem] = []
    renamed = set()
    for pick in suggested:
        renamed.add(pick.candidate)
        effect = OptionEffect(
            option_id="yes",
            kind=ALIAS_KIND,
            title=f"'{pick.candidate}' is the column the plan reads as '{pick.missing}'",
            payload={"column": pick.candidate, "as": pick.missing, "upload_id": new_id},
        )
        found.ask(
            "COLUMN_RENAMED",
            f"'{pick.missing}' is not in this file, but '{pick.candidate}' is new and looks like it ({pick.reason}) "
            f"Is '{pick.candidate}' the column that was called '{pick.missing}'?",
            upload_id=new_id,
            column=pick.missing,
            yes=(f"Yes, '{pick.candidate}' is '{pick.missing}'", effect),
        )
        trying.append(_alias_item(new_id, pick.candidate, pick.missing, now, decided_by=None))
    placed = {s.missing for s in suggested}
    for name in [c for c in missing if c not in placed]:
        close = next((u.candidates for u in unresolved if u.missing == name), ())
        hint = f" It could be {', '.join(repr(c) for c in close)}." if close else ""
        text = f"'{name}' is not in this file.{hint}"
        if name in protected:
            found.stop("COLUMN_MISSING", text + " The approved plan reads it.", upload_id=new_id, column=name)
        else:
            found.ask(
                "COLUMN_MISSING",
                text + " The approved plan does not read it. Is it fine to go on without it?",
                upload_id=new_id,
                column=name,
            )
    for name in [c for c in fresh if c not in renamed]:
        found.note(
            "COLUMN_ADDED",
            f"'{name}' is new in this file; the approved plan does not use it.",
            upload_id=new_id,
            column=name,
        )
    return trying


def _yes_rate(series: pd.Series[Any]) -> float | None:
    numbers = pd.to_numeric(series.dropna(), errors="coerce").dropna()
    if numbers.empty or not set(numbers.unique().tolist()) <= {0.0, 1.0}:
        return None
    return float(numbers.mean())


def _content(
    found: _Found,
    old: pd.DataFrame,
    new: pd.DataFrame,
    *,
    upload_id: str,
    key: str | None,
    target: str | None,
) -> None:
    """What the prepared columns look like now against the approved run's (the contract learnt from it)."""
    contract: DataContract = learn_contract(old, primary_key=key, target=target)
    # the outcome rate is judged below, as a stop; the contract's own check for it is a warning
    contract = DataContract(
        columns=tuple(c.model_copy(update={"outcome_rate": None}) for c in contract.columns)
    )
    for drift in check_prepared(new, contract, mode="train"):
        if drift.detector in ("MISSING_COLUMN", "NEW_COLUMN"):
            continue  # the files' columns are checked above, by name
        if drift.detector == "MEANING_CHANGED":
            code = "TYPE_CHANGED"
        elif drift.detector in ("NEW_VALUE", "NEW_CATEGORY"):
            code = "NEW_CATEGORY"
        else:
            code = "NULL_RATE_JUMP" if " is empty in " in drift.summary else "VALUE_SHIFT"
        found.ask(code, drift.summary, upload_id=upload_id, column=drift.column)
    for column in contract.columns:
        if not column.closed or column.column not in new.columns:
            continue
        gone = {v.value for v in column.top if v.share >= MISSING_CATEGORY_SHARE} - {
            str(v).strip() for v in new[column.column].dropna().unique()
        }
        if gone:
            found.ask(
                "CATEGORY_MISSING",
                f"'{column.column}' no longer holds {len(gone)} value{'s' if len(gone) != 1 else ''} that were "
                f"at least {MISSING_CATEGORY_SHARE:.0%} of its rows when the plan was approved. Is that expected?",
                upload_id=upload_id,
                column=column.column,
            )
    if target is not None and target in old.columns and target in new.columns:
        before, after = _yes_rate(old[target]), _yes_rate(new[target])
        if before is not None and after is not None:
            if abs(after - before) > max(OUTCOME_RATE_FLOOR, OUTCOME_RATE_MOVE * before):
                found.stop(
                    "OUTCOME_RATE_SHIFT",
                    f"The share of yes in '{target}' is {after:.1%}; it was {before:.1%} when the plan was "
                    "approved. A shift this big needs a person to look before anything is trained.",
                    upload_id=upload_id,
                    column=target,
                )
            positives = int(pd.to_numeric(new[target], errors="coerce").fillna(0).sum())
            if positives < MIN_POSITIVES:
                found.stop(
                    "TOO_FEW_POSITIVES",
                    f"'{target}' has {positives:,} yes outcomes; at least {MIN_POSITIVES:,} are needed.",
                    upload_id=upload_id,
                    column=target,
                    hard=True,
                )


# ---------------------------------------------------------------------------
# The recheck
# ---------------------------------------------------------------------------
def _remap_items(
    items: Sequence[CaseItem], uploads: Mapping[str, str], dates: Mapping[str, str]
) -> tuple[CaseItem, ...]:
    out: list[CaseItem] = []
    for item in items:
        payload = _move_value(item.payload, {**dates, **uploads})
        out.append(
            item.model_copy(
                update={
                    "upload_id": uploads.get(item.upload_id or "", item.upload_id),
                    "payload": payload,
                    "payload_digest": (
                        payload_digest(payload) if payload != item.payload else item.payload_digest
                    ),
                }
            )
        )
    return tuple(out)


def _remap_questions(questions: Sequence[CaseQuestion], dates: Mapping[str, str]) -> tuple[CaseQuestion, ...]:
    out: list[CaseQuestion] = []
    for q in questions:
        if q.answer is not None and dates:
            option = next((o for o in q.options if o.option_id == q.answer), None)
            value = q.answer_value
            if value is None and option is not None:
                value = next((d for d in dates if d in option.label), None)
            if value in dates:
                q = q.model_copy(update={"answer_value": dates[value]})
        out.append(q)
    return tuple(out)


def recheck(
    case: Case,
    uploads: Mapping[str, str],
    old_files: Sequence[LoadedFile],
    new_files: Sequence[LoadedFile],
    *,
    now: datetime,
) -> Rechecked:
    """Replay the ready `case`'s approved plan on `new_files` (`uploads`: old upload id -> new upload id) and say what
    differs from the approved run. The case is not changed; :func:`open_recheck_case` makes the follow-up case.
    """
    if not is_approved(case):
        raise RecheckError(
            "RECHECK_NOT_READY", "Only a case the person approved and took to ready can be rechecked."
        )
    old_by = {f.upload_id: f for f in old_files}
    new_by = {f.upload_id: f for f in new_files}
    if set(uploads) != set(case.upload_ids()) or any(
        u not in old_by or n not in new_by for u, n in uploads.items()
    ):
        raise RecheckError(
            "RECHECK_FILES_MISMATCH", "Give one new file for each file of the case, and no others."
        )
    items, _ = active_section(case)
    decided = decisions(case)
    baseline = materialize(case, items, list(old_files), Full())
    if baseline.failed:
        raise RecheckError(
            "RECHECK_BASELINE_FAILED",
            "The approved plan cannot be run on the approved files any more, so there is nothing to compare with.",
        )
    found = _Found([], [], [], {}, now)
    reads: set[str] = set()
    for upload_id in uploads:
        reads |= recipe_columns(s for s, _ in recipe_steps_of(items, upload_id, only_file=len(uploads) == 1))[
            0
        ]
        roles = (decided.column(kind, upload_id) for kind in ("key", "outcome", "as_of_column", "consent"))
        reads |= {c for c in roles if c is not None}
    trying: list[CaseItem] = []
    standing: list[LoadedFile] = []
    for old_id, new_id in uploads.items():
        old, new = old_by[old_id], new_by[new_id]
        trying += _columns(found, old, new, new_id, frozenset(reads), now)
        # the new file stands in for the old one under the old name, so the plan's table names still match
        standing.append(replace(new, file_name=old.file_name))

    # the data's end, then the dates that move with it (from the files as read, before any step)
    window = _window_days(items, decided.window_days(getattr(active_goal(case), "goal_id", None)))
    ends_before, ends_after = [], []
    for old_id, new_id in uploads.items():
        name = baseline.by_upload[old_id]
        named = decided.as_of_columns.get(old_id)
        columns = _date_columns(baseline.tables[name], str(named.value) if named is not None else None)
        ends_before.append(_data_end(old_by[old_id].frame, columns))
        ends_after.append(_data_end(new_by[new_id].frame, columns))
    end_before = max((d for d in ends_before if d), default=None)
    end_after = max((d for d in ends_after if d), default=None)
    dates = moved_dates(
        end_before=end_before,
        end_after=end_after,
        as_of=decided.as_of_date,
        training=decided.training_as_of_date,
        window=window,
    )
    if end_before is not None and end_after is not None and end_after <= end_before:
        found.ask(
            "DATE_RANGE_NOT_FORWARD",
            f"The newest date in this file ({end_after.isoformat()}) is not later than in the approved file "
            f"({end_before.isoformat()}). Is this the newer file?",
            upload_id=next(iter(uploads.values())),
        )
    mapping = _date_mapping(dates)

    new_case_files = [f.model_copy(update={"upload_id": uploads[f.upload_id]}) for f in case.files]
    moved_items = _remap_items(items, uploads, mapping)
    run: Prepared = materialize_plan(
        new_case_files,
        [*moved_items, *trying],
        standing,
        Full(),
        as_of=dates.as_of_after,
        training_as_of=dates.training_as_of_after,
    )
    for finding in run.findings:
        if finding.level == "stop":
            found.stop(
                finding.code,
                finding.text,
                upload_id=None,
                column=finding.column,
                hard=finding.hard,
            )
    rows_before = sum(f.file_rows or len(f.frame) for f in old_files)
    rows_after = sum(f.file_rows or len(f.frame) for f in new_files)
    if not run.failed:
        for old_id, new_id in uploads.items():
            name = baseline.by_upload[old_id]
            columns = {str(c) for c in baseline.tables[name].columns}
            key, target = roles_of(items, old_id, columns)
            _content(found, baseline.tables[name], run.tables[name], upload_id=new_id, key=key, target=target)
    if rows_after < MIN_ROWS:
        found.stop(
            "TOO_FEW_ROWS",
            f"This file has {rows_after:,} rows; at least {MIN_ROWS:,} are needed.",
            upload_id=None,
            hard=True,
        )
    low, high = ROW_COUNT_BAND
    if rows_before and not low <= rows_after / rows_before <= high:
        found.ask(
            "ROW_COUNT_CHANGED",
            f"This file has {rows_after:,} rows; the approved file had {rows_before:,}. Is that expected?",
            upload_id=next(iter(uploads.values())),
        )
    kept = sum(1 for i in items if i.state is ProposalState.ACCEPTED)  # nothing here changes an approved item
    report = RecheckReport(
        source_case_id=case.case_id,
        uploads=dict(uploads),
        rows_before=rows_before,
        rows_after=rows_after,
        dates=dates,
        kept_items=kept,
        findings=tuple(found.findings),
    )
    all_items = _remap_items(case.items, uploads, mapping)
    all_questions = _remap_questions(case.questions, mapping)
    return Rechecked(
        report=report,
        items=all_items,
        questions=(*all_questions, *found.questions),
        doubts=(*case.doubts, *found.doubts),
        offers=found.offers,
    )


# ---------------------------------------------------------------------------
# The follow-up case
# ---------------------------------------------------------------------------
def report_key(client_id: str, case_id: str) -> str:
    from engine.doctor.case import case_dir

    return f"{case_dir(client_id, case_id)}recheck.json"


def read_report(storage: Storage, client_id: str, case_id: str) -> RecheckReport | None:
    key = report_key(client_id, case_id)
    return storage.read_model(key, RecheckReport) if storage.exists(key) else None


def open_recheck_case(
    store: CaseStore,
    case: Case,
    rechecked: Rechecked,
    new_case_files: Sequence[CaseFile],
    *,
    actor: str,
) -> Case:
    """A new case linked to `case`: the approved items, approvals and answers carry over unchanged (their approval
    stands); what the recheck found waits as questions and stops. `new_case_files` are the new uploads, in the
    order of `case.files`. A case with nothing open stays where the old one stood; any question or stop sends it to ASK.
    """
    from engine.doctor.case import VisitState

    report = rechecked.report
    uploads = report.uploads
    files = tuple(
        new.model_copy(update={"role": old.role, "key_columns": old.key_columns})
        for old, new in zip(case.files, new_case_files, strict=True)
    )
    created = store.create(
        client_id=case.client_id, created_by=actor, title=f"{case.title} (next month)".strip(), files=files
    )
    visit = (case.visit or CaseVisit()).model_copy(
        update={
            "offers": {**(case.visit.offers if case.visit else {}), **rechecked.offers},
            "done": {},
            "ops": (),
            "hand_off": None,
            "stopped": None,
            "plan_check": None,
            "acknowledged": (),
        }
    )
    open_now = bool(report.questions or report.stops)

    def change(fresh: Case) -> Any:
        updated = fresh.model_copy(
            update={
                "items": rechecked.items,
                "questions": rechecked.questions,
                "doubts": rechecked.doubts,
                "approvals": case.approvals,
                "goals": case.goals,
                "diagnosis": case.diagnosis,
                "locale_profile": case.locale_profile,
                "visit": visit,
                "state": VisitState.ASK if open_now else case.state,
                "recheck_of": CaseRecheck(
                    source_case_id=case.case_id, source_version=case.version, uploads=dict(uploads)
                ),
            }
        )
        return updated, (
            EventDraft(
                kind=TimelineKind.REPLAY,
                details={
                    "source_case": case.case_id,
                    "questions": len(report.questions),
                    "stops": len(report.stops),
                    "kept_items": report.kept_items,
                },
            ),
        )

    return store.update(
        case.client_id, created.case_id, expected_version=created.version, actor=actor, change=change
    )
