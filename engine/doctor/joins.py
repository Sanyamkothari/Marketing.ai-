"""How several files fit together (Plan L M155, DEC-1551, DEC-1552).

Two questions a person with several files cannot answer alone, each answered by measuring the files:

* **Do the logs meet the customer list?** The customer list is the file the diagnosis named with the entity
  role; every file it named with an event role (bills, tickets, ...) is measured against it with onboarding's
  own `join_coverage` and `key_format_mismatch` - the functions `JOIN_KEY_COVERAGE_LOW` and
  `KEY_FORMAT_MISMATCH` already use when the raw-tables builder maps the same files - so Data Doctor and the
  builder can never disagree about whether two files join. One case those two do not try is a prefix on one
  side only (`CUST-00123` against `123`): when the IDs on one side nearly all start with the same letters, the
  letters are taken off that side and the same two functions are asked again, so the leading zeros left
  behind are found by `key_format_mismatch` itself. Every share reported is measured; the fix is said in plain
  words, and named as the mapping transform when one onboarding transform is the whole fix.
* **Are these the same report for different months?** Files of the same kind whose columns agree - the same
  names, or one adds a column, or a column was renamed (same place or a close name, and the same type) - can
  be stacked into one table. The differences from the first file (in file-name order) are listed as drift.

Nothing here reads more than the diagnosis already read: the rows `api/routes/doctor.py` loaded (each file up
to the use case's profiling cap) and each file's profile. A file longer than the rows read is said to be
`sampled`, so a share is never presented as the whole file's when it is not.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Any, Final

from engine.doctor.contracts import ColumnDrift, ColumnRename, FileDiagnosis, FileLink, FileStack
from engine.onboarding.sources import (
    _ID_LIKE_NAME,
    _KEY_TRANSFORMS,
    JOIN_COVERAGE_OK,
    join_coverage,
    key_candidates,
    key_format_mismatch,
)

if TYPE_CHECKING:
    import pandas as pd

    from engine.config import RoleCatalogue
    from engine.contracts import ColumnProfile
    from engine.doctor.contracts import Diagnosis
    from engine.doctor.diagnose import LoadedFile

__all__ = [
    "JOIN_CANDIDATES",
    "PREFIX_SHARE",
    "RENAME_MIN_SIMILARITY",
    "STACK_MIN_COMMON",
    "STACK_MIN_SHARED",
    "file_links",
    "file_stacks",
    "how_files_fit",
]

JOIN_CANDIDATES: Final[int] = 4
"""Columns of an event file tried as its customer column, in order (the list's own name first)."""

PREFIX_SHARE: Final[float] = 0.9
"""Share of one side's IDs that must start with the same letters for them to be read as a prefix."""

STACK_MIN_COMMON: Final[int] = 2
"""Columns two files must share under the same name before they can be the same report."""

STACK_MIN_SHARED: Final[float] = 0.75
"""Share of all their columns two files must agree on (same name, or renamed) to be the same report."""

RENAME_MIN_SIMILARITY: Final[float] = 0.5
"""How alike two names must be for a column in another place to be read as renamed."""

_PREFIX: Final[re.Pattern[str]] = re.compile(r"^([A-Za-z]+[-_ #:/.]?)(?=\d)")
"""Letters, optionally one separator, followed by a digit: `CUST-` of `CUST-00123`, `C` of `C123`."""

_TRANSFORM_WORDS: Final[dict[str, str]] = {
    "strip": "remove the spaces around the IDs",
    "lower": "read the IDs without minding upper and lower case",
    "lstrip_zeros": "drop the zeros at the start of the IDs",
}


def how_files_fit(diagnosis: Diagnosis, files: Sequence[LoadedFile], roles: RoleCatalogue) -> Diagnosis:
    """`diagnosis` with its `links` and `stacks` measured on `files` (the frames it was made from)."""
    return diagnosis.model_copy(
        update={
            "links": file_links(files, diagnosis.files, roles),
            "stacks": file_stacks(files, diagnosis.files),
        }
    )


# ---------------------------------------------------------------------------
# Links: each event file against the customer list
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _Measured:
    column: str
    coverage: float
    fix: str | None = None
    transform: str | None = None
    fixed_coverage: float | None = None

    @property
    def best(self) -> float:
        return self.fixed_coverage if self.fixed_coverage is not None else self.coverage


def file_links(
    files: Sequence[LoadedFile], diagnosed: Sequence[FileDiagnosis], roles: RoleCatalogue
) -> tuple[FileLink, ...]:
    """How every event file's customer column meets the customer list's ID column; empty without a list."""
    loaded = {file.upload_id: file for file in files}
    entity = roles.entity_role
    lists = [d for d in diagnosed if d.kind == entity and d.id_column is not None and d.upload_id in loaded]
    if not lists:
        return ()
    # The longest list is the one the logs are measured against; ties keep the order the files came in.
    customer = max(lists, key=lambda d: d.rows)
    list_file = loaded[customer.upload_id]
    list_column = str(customer.id_column)
    list_keys = _column(list_file.frame, list_column)
    links: list[FileLink] = []
    for item in diagnosed:
        if item.upload_id == customer.upload_id or item.kind is None or item.upload_id not in loaded:
            continue
        spec = roles.roles.get(item.kind)
        if spec is None or not spec.is_event:
            continue
        event = loaded[item.upload_id]
        measured = [
            _measure(
                column, _column(event.frame, column), list_keys, f"{list_column} in {customer.file_name}"
            )
            for column in _candidates(event, item, list_column)
        ]
        if not measured:
            continue
        chosen = max(measured, key=lambda m: m.best)  # the first of the best, in candidate order
        sampled = len(event.frame) < item.rows or len(list_file.frame) < customer.rows
        links.append(
            FileLink(
                upload_id=item.upload_id,
                file_name=item.file_name,
                column=chosen.column,
                list_upload_id=customer.upload_id,
                list_file_name=customer.file_name,
                list_column=list_column,
                coverage=chosen.coverage,
                fix=chosen.fix,
                transform=chosen.transform,
                fixed_coverage=chosen.fixed_coverage,
                sampled=sampled,
                summary=_link_summary(chosen, item.file_name, customer.file_name, sampled),
            )
        )
    return tuple(links)


def _column(frame: pd.DataFrame, name: str) -> pd.Series[Any]:
    by_name = {str(column): column for column in frame.columns}
    return frame[by_name[name]]


def _candidates(event: LoadedFile, item: FileDiagnosis, list_column: str) -> list[str]:
    """The event file's columns that could name the customer: the list's own name, the diagnosis's ID
    column, then every column whose name onboarding reads as an ID, in its key-candidate order."""
    names = [str(column) for column in event.frame.columns]
    ordered = [name for name in names if name.casefold() == list_column.casefold()]
    if item.id_column is not None:
        ordered.append(item.id_column)
    ordered += [key.column for key in key_candidates(event.profile) if _ID_LIKE_NAME.search(key.column)]
    present = set(names)
    return [name for name in dict.fromkeys(ordered) if name in present][:JOIN_CANDIDATES]


def _measure(column: str, event: pd.Series[Any], entity: pd.Series[Any], list_where: str) -> _Measured:
    coverage = join_coverage(event, entity)
    if coverage > JOIN_COVERAGE_OK:
        return _Measured(column=column, coverage=coverage)
    name = key_format_mismatch(event, entity)
    if name is not None:
        transform = _KEY_TRANSFORMS[name]
        fixed = join_coverage(_text(event).map(transform), _text(entity).map(transform))
        return _Measured(
            column=column,
            coverage=coverage,
            fix=_TRANSFORM_WORDS[name][:1].upper() + _TRANSFORM_WORDS[name][1:],
            transform=name,
            fixed_coverage=fixed,
        )
    for side, keys in (("list", entity), ("event", event)):
        prefix = _common_prefix(keys)
        if prefix is None:
            continue
        stripped = _strip(keys, prefix)
        pair = (event, stripped) if side == "list" else (stripped, entity)
        where = list_where if side == "list" else column
        words = f"Remove {prefix!r} from the start of {where}"
        fixed = join_coverage(*pair)
        if fixed > JOIN_COVERAGE_OK:
            return _Measured(column=column, coverage=coverage, fix=words, fixed_coverage=fixed)
        then = key_format_mismatch(*pair)
        if then is not None:
            transform = _KEY_TRANSFORMS[then]
            fixed = join_coverage(_text(pair[0]).map(transform), _text(pair[1]).map(transform))
            return _Measured(
                column=column,
                coverage=coverage,
                fix=f"{words}, then {_TRANSFORM_WORDS[then]}",
                fixed_coverage=fixed,
            )
    return _Measured(column=column, coverage=coverage)


def _text(keys: pd.Series[Any]) -> pd.Series[Any]:
    """Non-null keys as text: what `key_format_mismatch` applies its transforms to."""
    return keys.dropna().astype(str)


def _common_prefix(keys: pd.Series[Any]) -> str | None:
    """The letters nearly every key on this side starts with (`CUST-`), or None."""
    texts = _text(keys)
    if texts.empty:
        return None
    found = texts.str.extract(_PREFIX, expand=False).dropna()
    if found.empty:
        return None
    counts = found.value_counts()
    prefix, count = str(counts.index[0]), int(counts.iloc[0])
    return prefix if count / len(texts) >= PREFIX_SHARE else None


def _strip(keys: pd.Series[Any], prefix: str) -> pd.Series[Any]:
    size = len(prefix)
    return _text(keys).map(lambda value: value[size:] if value.startswith(prefix) else value)


def _share(value: float) -> str:
    """A share as a percent that never rounds a gap away: 99.6% is "over 99%", not "100%"."""
    percent = value * 100
    if 0 < percent < 1:
        return "under 1%"
    if 99 < percent < 100:
        return "over 99%"
    return f"{percent:.0f}%"


def _link_summary(measured: _Measured, file_name: str, list_name: str, sampled: bool) -> str:
    rows = f"of the rows of {file_name}" + (" read" if sampled else "")
    if measured.fixed_coverage is None and measured.coverage > JOIN_COVERAGE_OK:
        return (
            f"{_share(measured.coverage)} {rows} find their customer in {list_name} "
            f"({measured.column} matches its IDs)."
        )
    if measured.fix is not None and measured.fixed_coverage is not None:
        return (
            f"As written, {_share(measured.coverage)} {rows} find their customer in {list_name}. "
            f"{measured.fix}, and {_share(measured.fixed_coverage)} do."
        )
    return (
        f"Only {_share(measured.coverage)} {rows} find their customer in {list_name}, and no tidy-up of the "
        "IDs Data Doctor knows makes more of them meet."
    )


# ---------------------------------------------------------------------------
# Stacks: the same report for different periods
# ---------------------------------------------------------------------------
def file_stacks(files: Sequence[LoadedFile], diagnosed: Sequence[FileDiagnosis]) -> tuple[FileStack, ...]:
    """Groups of two or more files of one kind whose columns agree closely enough to be stacked."""
    loaded = {file.upload_id: file for file in files}
    ordered = sorted(
        (item for item in diagnosed if item.upload_id in loaded),
        key=lambda item: (item.file_name.casefold(), item.upload_id),
    )
    groups: list[list[FileDiagnosis]] = []
    for item in ordered:
        for group in groups:
            first = group[0]
            if (
                first.kind == item.kind
                and _drift(loaded[first.upload_id], loaded[item.upload_id]) is not None
            ):
                group.append(item)
                break
        else:
            groups.append([item])
    return tuple(_stack(group, loaded) for group in groups if len(group) >= 2)


def _drift(first: LoadedFile, other: LoadedFile) -> ColumnDrift | None:
    """How `other`'s columns differ from `first`'s, or None when they differ too much to be one report."""
    before = {column.name: column for column in first.profile.columns}
    after = {column.name: column for column in other.profile.columns}
    common = [name for name in before if name in after]
    missing = [name for name in before if name not in after]
    added = [name for name in after if name not in before]
    renamed = _renames(missing, added, before, after)
    matched = len(common) + len(renamed)
    unmatched = len(missing) + len(added) - 2 * len(renamed)
    if len(common) < STACK_MIN_COMMON or matched / (matched + unmatched) < STACK_MIN_SHARED:
        return None
    paired_before = {rename.before for rename in renamed}
    paired_after = {rename.after for rename in renamed}
    return ColumnDrift(
        upload_id=other.upload_id,
        file_name=other.file_name,
        added=tuple(name for name in added if name not in paired_after),
        missing=tuple(name for name in missing if name not in paired_before),
        renamed=tuple(renamed),
    )


def _similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, left.casefold(), right.casefold()).ratio()


def _renames(
    missing: Sequence[str],
    added: Sequence[str],
    before: dict[str, ColumnProfile],
    after: dict[str, ColumnProfile],
) -> list[ColumnRename]:
    """Missing columns paired with added ones of the same type: in the same place, else by a close name."""
    free = list(added)
    pairs: list[ColumnRename] = []
    for name in missing:
        column = before[name]
        options = [other for other in free if after[other].inferred_type == column.inferred_type]
        if not options:
            continue
        same_place = [other for other in options if after[other].position == column.position]
        if same_place:
            pick = same_place[0]
        else:
            pick = max(options, key=lambda other: _similarity(name, other))
            if _similarity(name, pick) < RENAME_MIN_SIMILARITY:
                continue
        pairs.append(ColumnRename(before=name, after=pick))
        free.remove(pick)
    return pairs


def _stack(group: list[FileDiagnosis], loaded: dict[str, LoadedFile]) -> FileStack:
    first = loaded[group[0].upload_id]
    drifts = [_drift(first, loaded[item.upload_id]) for item in group[1:]]
    changed = tuple(
        drift for drift in drifts if drift is not None and (drift.added or drift.missing or drift.renamed)
    )
    shared = [
        column.name
        for column in first.profile.columns
        if all(column.name in {c.name for c in loaded[item.upload_id].profile.columns} for item in group)
    ]
    names = [item.file_name for item in group]
    count = len(group)
    if not changed:
        summary = f"These {count} files have the same columns: they can be stacked into one table."
    else:
        said = "; ".join(_drift_words(drift, group[0].file_name) for drift in changed)
        summary = f"These {count} files look like the same report and can be stacked into one table: {said}."
    return FileStack(
        upload_ids=tuple(item.upload_id for item in group),
        file_names=tuple(names),
        kind=group[0].kind,
        columns=tuple(shared),
        rows=sum(item.rows for item in group),
        drift=changed,
        summary=summary,
    )


def _drift_words(drift: ColumnDrift, first_name: str) -> str:
    parts = [f"{rename.before} is called {rename.after}" for rename in drift.renamed]
    if drift.added:
        parts.append("it adds " + ", ".join(drift.added))
    if drift.missing:
        parts.append("it has no " + ", ".join(drift.missing))
    return f"in {drift.file_name} (against {first_name}) " + " and ".join(parts)
