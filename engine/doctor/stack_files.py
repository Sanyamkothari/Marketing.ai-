"""Stacking monthly exports without counting a row twice (Plan O M237, DEC-1807).

Plan L's diagnosis says when several files are the same report for different periods (`FileStack`,
DEC-1552), and the builder stacks same-role files with `pd.concat` - so three monthly bill exports that each
repeat the last week of the month before count that week twice ([INV] gap 3). `stack_files` stacks them and
removes what the exports repeat, by rules and nothing else:

* **Columns.** The first file's columns (file-name order, as the diagnosis orders them), a renamed column
  read under its first name (the diagnosis's drift), and a column only a later file adds kept, empty for the
  others. A column the spec does not list is left out and counted.
* **Exact duplicates across files.** A row identical in every column to a row of an *earlier* file is the
  same row exported twice: it is removed and counted against the pair of files. Rows are counted as a
  multiset: a row that a file holds twice (two real, identical bills) is kept twice, and only the copies a
  later file repeats beyond that are removed. A row is never compared with rows of its own file.
* **Conflicting duplicates.** With a customer and a date column, rows of two different files for the same
  customer and date that still differ (the amount was corrected) are a conflict. Which file wins is one
  question for the person - the later export or the earlier - and until it is answered the stack does not
  run (`STACK_CONFLICTS_UNDECIDED`). Answered, the losing rows are removed and counted.
* **Nothing is silent.** The receipt counts rows in per file, duplicates removed per file and per pair of
  files (with the dates they span when there is a date column), conflicts, rows out; a stack that removes
  more than 2% of the rows is an APPROVE-class item (design §6.6).

The spec names columns, renames and rules, never upload ids for its logic, so next month's exports stack
the same way.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, cast

import pandas as pd
from pydantic import Field

from engine.agent.contracts import Question, QuestionOption
from engine.agentic.canonical import canonical_bytes, sha256_hex
from engine.config import StrictBase
from engine.doctor.contracts import FileStack
from engine.doctor.limits import limits

__all__ = [
    "STACK_CODES",
    "StackError",
    "StackFile",
    "StackFileReceipt",
    "StackOverlap",
    "StackProposal",
    "StackReceipt",
    "StackRename",
    "StackRun",
    "StackSpec",
    "conflict_question",
    "plan_stack",
    "run_stack",
]

STACK_CODES: Final[frozenset[str]] = frozenset(
    {"STACK_CONFLICTS_UNDECIDED", "STACK_COLUMN_MISSING", "STACK_DROPS_ROWS", "STACK_TOO_FEW_FILES"}
)
"""The codes this module raises or records (M237)."""

ConflictRule = Literal["later_file", "earlier_file"]

_FILE: Final[str] = "__stack_file"
_ROW: Final[str] = "__stack_row"
_OCC: Final[str] = "__stack_occ"


class StackError(Exception):
    """A stack that cannot run. `code` is one of `STACK_CODES`."""

    def __init__(
        self, code: str, message: str, *, count: int | None = None, column: str | None = None
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.count = count
        self.column = column


class StackFile(StrictBase):
    upload_id: str
    file_name: str


class StackRename(StrictBase):
    """A later file's name for a column (`after`), read under the first file's name (`before`)."""

    before: str
    after: str


class StackSpec(StrictBase):
    """The frozen stack: what `stack_files` proposes and what replays next month."""

    files: tuple[StackFile, ...] = Field(min_length=2, description="Oldest first; the first wins exact ties.")
    columns: tuple[str, ...] = Field(min_length=1, description="The stacked table's columns, in order.")
    renames: tuple[StackRename, ...] = Field(default=())
    key_column: str | None = Field(default=None, description="The customer column, for conflicts.")
    date_column: str | None = Field(default=None, description="The date column, for conflicts and overlaps.")
    on_conflict: ConflictRule | None = Field(
        default=None, description="Which export wins a conflict; null until the person answers."
    )

    @property
    def spec_hash(self) -> str:
        logic = self.model_dump(mode="json", exclude={"files"})
        logic["files"] = len(self.files)
        return sha256_hex(canonical_bytes(logic))


class StackOverlap(StrictBase):
    """Rows a later file repeats from an earlier one."""

    upload_id: str
    file_name: str
    with_upload_id: str
    with_file_name: str
    rows: int
    first_date: str | None = Field(
        default=None, description="The earliest date among them (ISO), when known."
    )
    last_date: str | None = Field(default=None, description="The latest date among them (ISO), when known.")


class StackFileReceipt(StrictBase):
    upload_id: str
    file_name: str
    rows: int
    duplicates_removed: int = Field(description="Its rows that repeat an earlier file's, removed.")
    conflicts_removed: int = Field(description="Its rows that lost a conflict, removed.")
    columns_missing: tuple[str, ...] = Field(description="Spec columns it lacks; empty in its rows.")
    columns_left_out: tuple[str, ...] = Field(description="Its columns the spec does not list; not stacked.")


class StackReceipt(StrictBase):
    spec_hash: str
    files: tuple[StackFileReceipt, ...]
    overlaps: tuple[StackOverlap, ...]
    rows_in: int
    duplicates_removed: int
    conflicts: int = Field(description="Customer-and-date pairs that two files give differently.")
    conflict_rows_removed: int
    rows_out: int
    removed_share: float
    approval_required: bool
    approval_reasons: tuple[str, ...]


@dataclass(frozen=True)
class StackRun:
    frame: pd.DataFrame
    receipt: StackReceipt


def _aligned(frame: pd.DataFrame, spec: StackSpec) -> tuple[pd.DataFrame, tuple[str, ...], tuple[str, ...]]:
    names = {str(column): column for column in frame.columns}
    renamed = {r.after: r.before for r in spec.renames if r.after in names and r.before not in names}
    view = frame.rename(columns={names[after]: before for after, before in renamed.items()})
    have = {str(column): column for column in view.columns}
    missing = tuple(column for column in spec.columns if column not in have)
    left_out = tuple(
        str(column) for column in frame.columns if renamed.get(str(column), str(column)) not in spec.columns
    )
    out = pd.DataFrame(
        {
            column: view[have[column]] if column in have else pd.Series(None, index=view.index, dtype=object)
            for column in spec.columns
        },
        index=view.index,
    )
    return out, missing, left_out


def _iso(value: Any) -> str | None:
    try:
        return str(pd.Timestamp(value).date())
    except (TypeError, ValueError):
        return None


def _date_span(values: pd.Series[Any]) -> tuple[str | None, str | None]:
    dates = pd.to_datetime(values, errors="coerce", format="mixed").dropna()
    if dates.empty:
        return None, None
    return _iso(dates.min()), _iso(dates.max())


def run_stack(frames: Sequence[pd.DataFrame], spec: StackSpec) -> StackRun:
    """Stack `frames` (one per `spec.files`, in order) by `spec`; the inputs are never modified."""
    if len(frames) != len(spec.files):
        raise StackError("STACK_TOO_FEW_FILES", "Give one table for each file the stack names.")
    for column in (spec.key_column, spec.date_column):
        if column is not None and column not in spec.columns:
            raise StackError(
                "STACK_COLUMN_MISSING", f"'{column}' is not one of the stacked columns.", column=column
            )
    parts: list[pd.DataFrame] = []
    missing_by: list[tuple[str, ...]] = []
    left_by: list[tuple[str, ...]] = []
    for index, frame in enumerate(frames):
        aligned, missing, left_out = _aligned(frame, spec)
        for needed in (spec.key_column, spec.date_column):
            if needed is not None and needed in missing:
                raise StackError(
                    "STACK_COLUMN_MISSING",
                    f"{spec.files[index].file_name} has no column '{needed}', which the stack reads.",
                    column=needed,
                )
        aligned[_FILE] = index
        aligned[_ROW] = range(len(aligned))
        parts.append(aligned.reset_index(drop=True))
        missing_by.append(missing)
        left_by.append(left_out)
    combined = pd.concat(parts, ignore_index=True)
    columns = list(spec.columns)
    hashes = pd.util.hash_pandas_object(combined[columns], index=False)
    combined[_OCC] = combined.groupby([combined[_FILE], hashes], sort=False).cumcount()
    duplicate = combined.duplicated(subset=[*columns, _OCC], keep="first")
    first_file = combined.groupby([hashes, combined[_OCC]], sort=False)[_FILE].transform("first")
    overlaps: list[StackOverlap] = []
    removed_dup = [0] * len(frames)
    dup_rows = combined[duplicate]
    for (later_key, earlier_key), group in dup_rows.groupby(
        [dup_rows[_FILE], first_file[duplicate]], sort=True
    ):
        later, earlier = cast(int, later_key), cast(int, earlier_key)
        removed_dup[later] += len(group)
        first, last = _date_span(group[spec.date_column]) if spec.date_column else (None, None)
        overlaps.append(
            StackOverlap(
                upload_id=spec.files[later].upload_id,
                file_name=spec.files[later].file_name,
                with_upload_id=spec.files[earlier].upload_id,
                with_file_name=spec.files[earlier].file_name,
                rows=len(group),
                first_date=first,
                last_date=last,
            )
        )
    kept = combined[~duplicate]
    conflicts = 0
    removed_conflict = [0] * len(frames)
    if spec.key_column is not None and spec.date_column is not None:
        keys = [spec.key_column, spec.date_column]
        files_per_pair = kept.groupby(keys, dropna=True, sort=False)[_FILE].transform("nunique")
        clash = files_per_pair > 1
        conflicts = int(kept[clash].drop_duplicates(subset=keys).shape[0])
        if conflicts:
            if spec.on_conflict is None:
                raise StackError(
                    "STACK_CONFLICTS_UNDECIDED",
                    f"{conflicts} customer-and-date pairs are written differently in two exports. Say which "
                    "export to keep before stacking.",
                    count=conflicts,
                )
            pick: Literal["max", "min"] = "max" if spec.on_conflict == "later_file" else "min"
            winner = kept.groupby(keys, dropna=True, sort=False)[_FILE].transform(pick)
            loses = clash & (kept[_FILE] != winner)
            for file_index, count in kept[loses][_FILE].value_counts().items():
                removed_conflict[cast(int, file_index)] = int(count)
            kept = kept[~loses]
    out = kept.sort_values([_FILE, _ROW], kind="stable")[columns].reset_index(drop=True)
    rows_in = len(combined)
    removed = sum(removed_dup) + sum(removed_conflict)
    share = round(removed / rows_in, 4) if rows_in else 0.0
    reasons = ("STACK_DROPS_ROWS",) if share > limits().gates.approve_drop_share else ()
    receipt = StackReceipt(
        spec_hash=spec.spec_hash,
        files=tuple(
            StackFileReceipt(
                upload_id=file.upload_id,
                file_name=file.file_name,
                rows=len(frames[index]),
                duplicates_removed=removed_dup[index],
                conflicts_removed=removed_conflict[index],
                columns_missing=missing_by[index],
                columns_left_out=left_by[index],
            )
            for index, file in enumerate(spec.files)
        ),
        overlaps=tuple(overlaps),
        rows_in=rows_in,
        duplicates_removed=sum(removed_dup),
        conflicts=conflicts,
        conflict_rows_removed=sum(removed_conflict),
        rows_out=len(out),
        removed_share=share,
        approval_required=bool(reasons),
        approval_reasons=reasons,
    )
    return StackRun(frame=out, receipt=receipt)


# ---------------------------------------------------------------------------
# Proposing a stack
# ---------------------------------------------------------------------------
class StackProposal(StrictBase):
    """`stack_files`' answer: a pending, `check`, never-ticked suggestion, its dry-run receipt, and the one
    question conflicting duplicates need (null when there are none)."""

    state: Literal["pending"] = "pending"
    confidence: Literal["check"] = "check"
    never_ticked: Literal[True] = True
    spec: StackSpec
    receipt: StackReceipt | None = Field(description="Null while the conflict question is open.")
    question: Question | None
    conflicts: int
    summary: str


def conflict_question(conflicts: int, first: str, last: str) -> Question:
    """One question for every conflict (design §6.5: one atomic question settles it)."""
    return Question(
        question_id="stack-conflicts",
        text=(
            f"{conflicts} customer-and-date pairs appear in two exports with different values. Which export "
            "should be kept for them?"
        ),
        options=(
            QuestionOption(
                option_id="later_file",
                label="The later export",
                effect=f"Keep the rows of the newer file (up to {last}); the older copies are removed and counted.",
            ),
            QuestionOption(
                option_id="earlier_file",
                label="The earlier export",
                effect=f"Keep the rows of the older file ({first} first); the newer copies are removed and counted.",
            ),
        ),
        blocking=True,
    )


def plan_stack(
    stack: FileStack,
    frames: Mapping[str, pd.DataFrame],
    *,
    key_column: str | None = None,
    date_column: str | None = None,
    on_conflict: ConflictRule | None = None,
) -> StackProposal:
    """Propose stacking the files the diagnosis found to be one report (`FileStack`), measured on `frames`."""
    first = frames[stack.upload_ids[0]]
    columns = [str(column) for column in first.columns]
    renames = tuple(
        StackRename(before=rename.before, after=rename.after)
        for drift in stack.drift
        for rename in drift.renamed
    )
    renamed_after = {rename.after for rename in renames}
    for drift in stack.drift:
        for added in drift.added:
            if added not in columns and added not in renamed_after:
                columns.append(added)
    files = tuple(
        StackFile(upload_id=upload_id, file_name=name)
        for upload_id, name in zip(stack.upload_ids, stack.file_names, strict=True)
    )
    spec = StackSpec(
        files=files,
        columns=tuple(columns),
        renames=renames,
        key_column=key_column,
        date_column=date_column,
        on_conflict=on_conflict,
    )
    ordered = [frames[upload_id] for upload_id in stack.upload_ids]
    try:
        run = run_stack(ordered, spec)
    except StackError as exc:
        if exc.code != "STACK_CONFLICTS_UNDECIDED":
            raise
        count = int(exc.count or 0)
        return StackProposal(
            spec=spec,
            receipt=None,
            question=conflict_question(count, files[0].file_name, files[-1].file_name),
            conflicts=count,
            summary=(
                f"Stack {len(files)} files into one table. {count} customer-and-date pairs are written differently "
                "in two exports, so one question comes first."
            ),
        )
    receipt = run.receipt
    removed = receipt.duplicates_removed
    said = (
        f"{removed} rows that a later export repeats from an earlier one are counted once"
        if removed
        else "no export repeats another's rows"
    )
    return StackProposal(
        spec=spec,
        receipt=receipt,
        question=None,
        conflicts=receipt.conflicts,
        summary=(
            f"Stack {len(files)} files into one table of {receipt.rows_out} rows: {said}."
            + (" This needs your own approval." if receipt.approval_required else "")
        ),
    )
