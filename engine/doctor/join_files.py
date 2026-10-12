"""Joining per-customer files (Plan O M237, DEC-1807): a customer list, its attributes and an outcome list.

Plan L's builder joins one customer list to event logs. It refuses a second per-customer file, so two
CRM exports (one with the plan, one with the region) or a customer list and a list of the customers who left
could not become one table ([INV] gap 2). `join_files` closes that gap, by rules and nothing else:

* **The base keeps every row.** The first file (the customer list) is the table; every other file is looked up
  in it by its customer column. No base row is ever dropped or repeated, so `rows_out == base rows` always.
* **Attributes.** A per-customer file adds its columns to the customers it names. It must hold one row per
  customer: rows that repeat exactly are counted and kept once; a customer on two different rows is
  `JOIN_KEY_REPEATS`, because a join would have to pick one (stack, or keep the latest row, first).
* **An outcome list.** A list of customer IDs (the churned, the converted) becomes a 1/0 column on the base:
  1 when the customer's ID is on the list, 0 when it is not, and empty (`pd.NA`, a nullable `Int64`) for a base
  row with no ID, which cannot be looked up (counted in `base_rows_without_key`, and said in the sentence). It
  defines what the model learns, so it is **always** an APPROVE-class item, with its sentence shown word for word.
* **Exact keys only.** IDs are compared as text after each side's frozen `KeyRule` (the `normalise_key` step's
  rule: prefix, leading zeros, case, spaces, +91 phones). The base's IDs are matched through their rule but
  **written as they were**: the rule makes a match key, it never rewrites the customer list's own IDs. Nothing
  is matched by being similar (Plan O §1.3).
* **Nothing is silent.** A file offered but not joined - its IDs do not meet, it adds no column, or it is past
  the tenth - has a `JoinPartPlan` with `joined: false` and the reason. The receipt counts, per file: rows, rows with no ID, exact repeats removed, IDs, the
  rows that found their customer, the rows that did not (dropped: their customer is not on the base) and the
  share that is; the base rows that got a value; the columns added and the columns left out. A file whose
  dropped share is over `APPROVE_DROP_SHARE` (2%) makes the join an APPROVE-class item (design §6.6).

`plan_join` measures the files and proposes a `JoinSpec` (pending, `check`, never ticked); `run_join` runs a
spec. The spec is frozen: next month's files are joined by the same rule and the same column list (a new
column is left out and counted, a listed one that is missing is `JOIN_COLUMN_MISSING`), so it replays.
Results hold counts, column names and file names only: never an ID or a cell (the design's privacy rule for
multi-file work: join tools return counts, never keys).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

import pandas as pd
from pydantic import Field, model_validator

from engine.agent.key_steps import (
    KEY_JOIN_OK,
    KeyColumn,
    KeyRule,
    KeyRuleProposal,
    fit_rule,
    propose_key_rules,
)
from engine.agentic.canonical import canonical_bytes, sha256_hex
from engine.config import StrictBase

__all__ = [
    "APPROVE_DROP_SHARE",
    "JOIN_CODES",
    "MAX_JOIN_PARTS",
    "ColumnPick",
    "JoinError",
    "JoinPart",
    "JoinPartPlan",
    "JoinPartReceipt",
    "JoinProposal",
    "JoinReceipt",
    "JoinRun",
    "JoinSpec",
    "outcome_sentence",
    "plan_join",
    "run_join",
]

APPROVE_DROP_SHARE: Final[float] = 0.02
"""A join or de-duplication that drops more than this share of a file's rows needs the person's own yes."""

MAX_JOIN_PARTS: Final[int] = 10
"""Files joined onto one base at most; more is a sign the files are not per-customer."""

JOIN_CODES: Final[frozenset[str]] = frozenset(
    {
        "JOIN_KEY_REPEATS",
        "JOIN_COLUMN_MISSING",
        "JOIN_COLUMN_CLASH",
        "JOIN_DROPS_ROWS",
        "JOIN_DEFINES_OUTCOME",
        "JOIN_KEYS_DO_NOT_MEET",
    }
)
"""The codes this module raises or records (M237); help text in the milestone's result."""

PartKind = Literal["attributes", "outcome_list"]

_STEM: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]+")
_LIST_WORDS: Final[tuple[str, ...]] = (
    "churn",
    "churned",
    "left",
    "lost",
    "cancel",
    "cancelled",
    "converted",
    "conversion",
    "won",
    "responders",
    "defaulters",
    "default",
)


class JoinError(Exception):
    """A join that cannot run on these files. `code` is one of `JOIN_CODES`."""

    def __init__(
        self, code: str, message: str, *, column: str | None = None, upload_id: str | None = None
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.column = column
        self.upload_id = upload_id


class ColumnPick(StrictBase):
    """One column a per-customer file adds, and the name it gets on the base."""

    source: str = Field(description="The column as the file has it.")
    output: str = Field(description="Its name in the joined table (the same unless it would clash).")


class JoinPart(StrictBase):
    """One file looked up in the base by its customer column."""

    upload_id: str
    file_name: str
    kind: PartKind = Field(description="`attributes` adds columns; `outcome_list` adds one 1/0 column.")
    key_column: str = Field(description="The file's column naming the customer.")
    key_rule: KeyRule = Field(default_factory=KeyRule.identity, description="How its IDs are written first.")
    columns: tuple[ColumnPick, ...] = Field(default=(), description="`attributes` only: the columns added.")
    outcome_column: str | None = Field(
        default=None, description="`outcome_list` only: the 1/0 column's name on the base."
    )

    @model_validator(mode="after")
    def _shape(self) -> JoinPart:
        if self.kind == "outcome_list" and (self.outcome_column is None or self.columns):
            raise ValueError("an outcome list adds exactly one column, outcome_column")
        if self.kind == "attributes" and (self.outcome_column is not None or not self.columns):
            raise ValueError("an attributes file adds at least one column and no outcome")
        outputs = [pick.output for pick in self.columns]
        if len(set(outputs)) != len(outputs):
            raise ValueError("two added columns cannot have one name")
        if any(pick.source == self.key_column for pick in self.columns):
            raise ValueError("the customer column is the join key, not an added column")
        return self


class JoinSpec(StrictBase):
    """The frozen join: what the `join_files` proposal carries and what replays next month."""

    base_upload_id: str
    base_file_name: str
    base_key_column: str = Field(description="The base's ID column; its values are kept as written.")
    base_key_rule: KeyRule = Field(
        default_factory=KeyRule.identity, description="How the base's IDs are written to make the match key."
    )
    parts: tuple[JoinPart, ...] = Field(min_length=1, max_length=MAX_JOIN_PARTS)

    @model_validator(mode="after")
    def _names(self) -> JoinSpec:
        added = [pick.output for part in self.parts for pick in part.columns] + [
            str(part.outcome_column) for part in self.parts if part.outcome_column is not None
        ]
        if len(set(added)) != len(added):
            raise ValueError("two files add a column of the same name")
        if self.base_key_column in added:
            raise ValueError("no file may add a column named like the base's ID column")
        return self

    @property
    def spec_hash(self) -> str:
        """Hash of what the join does (files by position, columns, rules), not of file names."""
        logic = self.model_dump(mode="json", exclude={"base_upload_id", "base_file_name"})
        for part in logic["parts"]:
            part.pop("upload_id", None)
            part.pop("file_name", None)
        return sha256_hex(canonical_bytes(logic))


class JoinPartReceipt(StrictBase):
    """What one file did in the join, counted."""

    upload_id: str
    file_name: str
    kind: PartKind
    rows: int = Field(description="Rows in the file.")
    rows_without_key: int = Field(description="Rows with an empty customer column; they join nothing.")
    exact_repeats_removed: int = Field(description="Rows identical to an earlier row of the same file.")
    keys: int = Field(description="Different customers the file names, once its IDs are written by its rule.")
    keys_not_converted: int = Field(description="Cells the phone rule could not read; kept as written.")
    rows_matched: int = Field(description="Rows whose customer is on the base.")
    rows_dropped: int = Field(
        description="Rows that are not in the joined table: no ID, or a customer not on the base."
    )
    dropped_share: float = Field(
        description="`rows_dropped` / `rows`, rounded to 4 places; 0 for an empty file."
    )
    base_rows_matched: int = Field(description="Base rows that got a value from this file.")
    columns_added: tuple[str, ...] = Field(description="The columns it added to the base, as named there.")
    columns_left_out: tuple[str, ...] = Field(description="Its other columns, not in the spec; not joined.")


class JoinReceipt(StrictBase):
    """What the join did: every row in and out, counted (Plan O §3.1, "nothing is silent")."""

    spec_hash: str
    base_upload_id: str
    base_rows: int
    base_rows_without_key: int
    base_key_repeats: int = Field(
        description="Base rows whose ID is on another base row too (kept, all of them)."
    )
    parts: tuple[JoinPartReceipt, ...]
    rows_out: int = Field(description="Rows in the joined table: always the base's.")
    columns_out: int
    approval_required: bool = Field(description="True when the join is an APPROVE-class item.")
    approval_reasons: tuple[str, ...] = Field(
        description="`JOIN_DEFINES_OUTCOME` for an outcome list; `JOIN_DROPS_ROWS` when a file loses over 2%."
    )


@dataclass(frozen=True)
class JoinRun:
    frame: pd.DataFrame
    receipt: JoinReceipt


def _by_name(frame: pd.DataFrame) -> dict[str, Any]:
    return {str(column): column for column in frame.columns}


def _require(frame: pd.DataFrame, column: str, *, file_name: str, upload_id: str) -> Any:
    names = _by_name(frame)
    if column not in names:
        raise JoinError(
            "JOIN_COLUMN_MISSING",
            f"{file_name} has no column '{column}', which the join reads.",
            column=column,
            upload_id=upload_id,
        )
    return names[column]


Read = dict[tuple[str, str], KeyColumn]
"""Key columns already read, by (upload id, column): `plan_join`'s dry run reuses what it measured."""


def _key_column(read: Read, upload_id: str, frame: pd.DataFrame, column: Any) -> KeyColumn:
    found = read.get((upload_id, str(column)))
    if found is None:
        found = KeyColumn(frame[column])
        read[(upload_id, str(column))] = found
    return found


def _share(part: int, whole: int) -> float:
    return round(part / whole, 4) if whole else 0.0


def run_join(base: pd.DataFrame, parts: Mapping[str, pd.DataFrame], spec: JoinSpec) -> JoinRun:
    """Run `spec` on `base` and the part frames (by upload id). The inputs are never modified."""
    return _run_join(base, parts, spec, {})


def _run_join(base: pd.DataFrame, parts: Mapping[str, pd.DataFrame], spec: JoinSpec, read: Read) -> JoinRun:
    base_key_col = _require(
        base, spec.base_key_column, file_name=spec.base_file_name, upload_id=spec.base_upload_id
    )
    base_column = _key_column(read, spec.base_upload_id, base, base_key_col)
    base_keys = pd.Series(base_column.rows_of(spec.base_key_rule), index=base.index, dtype=object)
    base_present = base_keys.dropna()
    repeats = int(base_present.duplicated(keep=False).sum())
    existing = set(_by_name(base))
    out = base.copy()
    receipts: list[JoinPartReceipt] = []
    reasons: list[str] = []
    for part in spec.parts:
        if part.upload_id not in parts:
            raise JoinError(
                "JOIN_COLUMN_MISSING",
                f"{part.file_name} is not among the files given to the join.",
                upload_id=part.upload_id,
            )
        frame = parts[part.upload_id]
        key_col = _require(frame, part.key_column, file_name=part.file_name, upload_id=part.upload_id)
        names = _by_name(frame)
        for pick in part.columns:
            _require(frame, pick.source, file_name=part.file_name, upload_id=part.upload_id)
        added = [pick.output for pick in part.columns] or [str(part.outcome_column)]
        clash = sorted(name for name in added if name in existing)
        if clash:
            raise JoinError(
                "JOIN_COLUMN_CLASH",
                f"The joined table already has a column '{clash[0]}'; {part.file_name} cannot add another.",
                column=clash[0],
                upload_id=part.upload_id,
            )
        part_column = _key_column(read, part.upload_id, frame, key_col)
        keys = pd.Series(part_column.rows_of(part.key_rule), index=frame.index, dtype=object)
        not_converted = part_column.failed_rows(part.key_rule)
        has_key = keys.notna()
        rows_without_key = int((~has_key).sum())
        used = [key_col] + [names[pick.source] for pick in part.columns]
        kept = frame.loc[has_key.to_numpy(), used].copy()
        kept.insert(0, "__key", keys[has_key].to_numpy())
        exact = (
            kept.drop(columns=[key_col]).duplicated(keep="first")
            if part.columns
            else kept["__key"].duplicated(keep="first")
        )
        exact_repeats = int(exact.sum()) if part.kind == "attributes" else 0
        unique = kept[~exact.to_numpy()]
        if part.kind == "attributes" and bool(unique["__key"].duplicated().any()):
            repeated = int(unique["__key"].duplicated(keep=False).sum())
            raise JoinError(
                "JOIN_KEY_REPEATS",
                f"In {part.file_name}, {repeated} rows name a customer that is on another row with different "
                f"values. A per-customer file has one row per customer: stack the files or keep the latest "
                "row per customer first.",
                column=part.key_column,
                upload_id=part.upload_id,
            )
        # Each distinct ID is looked up once, then spread over its rows.
        found = part_column.found_in(part.key_rule, base_column, spec.base_key_rule)
        rows_matched = int(part_column.counts[found].sum())
        rows_dropped = len(frame) - rows_matched
        on_list = len(part_column.index(part.key_rule))
        base_found = base_column.found_in(spec.base_key_rule, part_column, part.key_rule)
        base_hit = pd.Series(base_column.flags(base_found), index=base.index)
        if part.kind == "outcome_list":
            # A base row with no ID cannot be looked up: its outcome is unknown, not "no" (counted in
            # `base_rows_without_key`).
            flags = pd.array(base_hit.astype("int64").to_numpy(), dtype="Int64")
            flags[base_keys.isna().to_numpy()] = pd.NA
            out[str(part.outcome_column)] = flags
        else:
            lookup = unique.set_index("__key")
            for pick in part.columns:
                column = lookup[names[pick.source]]
                out[pick.output] = base_keys.map(column).to_numpy()
        existing.update(added)
        left_out = tuple(
            name for name in names if name != part.key_column and name not in {p.source for p in part.columns}
        )
        dropped_share = _share(rows_dropped, len(frame))
        if part.kind == "outcome_list" and "JOIN_DEFINES_OUTCOME" not in reasons:
            reasons.append("JOIN_DEFINES_OUTCOME")
        if dropped_share > APPROVE_DROP_SHARE and "JOIN_DROPS_ROWS" not in reasons:
            reasons.append("JOIN_DROPS_ROWS")
        receipts.append(
            JoinPartReceipt(
                upload_id=part.upload_id,
                file_name=part.file_name,
                kind=part.kind,
                rows=len(frame),
                rows_without_key=rows_without_key,
                exact_repeats_removed=exact_repeats,
                keys=on_list,
                keys_not_converted=not_converted,
                rows_matched=rows_matched,
                rows_dropped=rows_dropped,
                dropped_share=dropped_share,
                base_rows_matched=int(base_hit.sum()),
                columns_added=tuple(added),
                columns_left_out=left_out,
            )
        )
    receipt = JoinReceipt(
        spec_hash=spec.spec_hash,
        base_upload_id=spec.base_upload_id,
        base_rows=len(base),
        base_rows_without_key=int(base_keys.isna().sum()),
        base_key_repeats=repeats,
        parts=tuple(receipts),
        rows_out=len(out),
        columns_out=len(out.columns),
        approval_required=bool(reasons),
        approval_reasons=tuple(reasons),
    )
    return JoinRun(frame=out, receipt=receipt)


# ---------------------------------------------------------------------------
# Proposing a join
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class JoinInput:
    """One file offered to `plan_join`: its upload, name, frame and (optionally) what it is."""

    upload_id: str
    file_name: str
    frame: pd.DataFrame
    kind: PartKind | None = None
    key_column: str | None = None


class JoinPartPlan(StrictBase):
    """How one file meets the base, measured, and whether it is in the proposed join."""

    upload_id: str
    file_name: str
    kind: PartKind
    key_column: str | None
    coverage: float = Field(description="Share of its IDs found on the base as written.")
    fixed_coverage: float = Field(description="The same share under the proposed key rules.")
    key_rule: KeyRule
    base_key_rule: KeyRule
    joined: bool = Field(description="False when its IDs do not meet the base's even after a tidy-up.")
    summary: str


class JoinProposal(StrictBase):
    """`join_files`' answer: a pending, `check`, never-ticked suggestion with its dry-run receipt."""

    state: Literal["pending"] = "pending"
    confidence: Literal["check"] = "check"
    never_ticked: Literal[True] = True
    spec: JoinSpec | None = Field(description="The join proposed; null when no file meets the base.")
    receipt: JoinReceipt | None = Field(description="What it does, run on the files as they are.")
    parts: tuple[JoinPartPlan, ...]
    normalise_key: tuple[str, ...] = Field(
        description="The files whose IDs the join writes by a rule before comparing (a `normalise_key` item)."
    )
    outcome_sentences: tuple[str, ...] = Field(
        description="For each outcome list, the sentence the person agrees to, word for word."
    )
    summary: str


def _stem(file_name: str) -> str:
    base = file_name.rsplit("/", 1)[-1].rsplit(".", 1)[0].casefold()
    return _STEM.sub("_", base).strip("_") or "file"


def outcome_sentence(file_name: str, column: str, *, rows_without_id: int = 0) -> str:
    """The sentence an outcome list is agreed with: exact text, compared word for word at approval. Rows of
    the list with no ID cannot be looked up, so their outcome is left empty, and the sentence says how many.
    """
    sentence = f"'{column}' is 1 for a customer whose ID is in {file_name}, and 0 for every other customer in the list."
    if rows_without_id:
        rows = "1 row of the list has" if rows_without_id == 1 else f"{rows_without_id} rows of the list have"
        sentence += f" {rows} no ID; '{column}' is left empty there."
    return sentence


def _guess_kind(item: JoinInput, key: str) -> PartKind:
    """An outcome list holds IDs and nothing a model could learn from: no other column, or only dates and
    flags that are the same on every row; or its name says it lists who left or converted."""
    if item.kind is not None:
        return item.kind
    others = [column for column in _by_name(item.frame) if column != key]
    words = set(_STEM.split(_stem(item.file_name)))
    if words & set(_LIST_WORDS) and len(others) <= 2:
        return "outcome_list"
    if not others:
        return "outcome_list"
    return "attributes"


def _key_candidates(item: JoinInput, base_key: str) -> list[str]:
    from engine.onboarding.sources import _ID_LIKE_NAME

    names = list(_by_name(item.frame))
    if item.key_column is not None:
        return [item.key_column] if item.key_column in names else []
    ordered = [name for name in names if name.casefold() == base_key.casefold()]
    ordered += [name for name in names if _ID_LIKE_NAME.search(name)]
    if len(names) == 1:
        ordered += names
    return list(dict.fromkeys(ordered))[:4]


def _left_out(
    item: JoinInput,
    kind: PartKind,
    column: str | None,
    found: KeyRuleProposal | None,
    base_rule: KeyRule,
    summary: str,
) -> JoinPartPlan:
    """A file that is not in the proposed join, recorded with the reason (nothing is silent)."""
    return JoinPartPlan(
        upload_id=item.upload_id,
        file_name=item.file_name,
        kind=kind,
        key_column=column,
        coverage=found.coverage if found else 0.0,
        fixed_coverage=found.fixed_coverage if found else 0.0,
        key_rule=KeyRule.identity(),
        base_key_rule=base_rule,
        joined=False,
        summary=summary,
    )


def plan_join(
    base: JoinInput,
    others: Sequence[JoinInput],
    *,
    base_key_column: str,
    outcome_name: str | None = None,
) -> JoinProposal:
    """Measure how each file meets `base` and propose the join, with its receipt on the files as they are.

    A file is joined only when its IDs meet the base's (`KEY_JOIN_OK`), as written or under a key rule; the
    rule is a pending `normalise_key` item, never applied before the person approves the whole join.
    """
    base_column = _require(base.frame, base_key_column, file_name=base.file_name, upload_id=base.upload_id)
    read: Read = {}
    base_keys = _key_column(
        read, base.upload_id, base.frame, base_column
    )  # read once for every file and rule
    taken = set(_by_name(base.frame))
    plans: list[JoinPartPlan] = []
    parts: list[JoinPart] = []
    measured: list[tuple[JoinInput, PartKind, str | None, KeyRuleProposal | None]] = []
    for item in others[MAX_JOIN_PARTS:]:
        plans.append(
            _left_out(
                item,
                item.kind or "attributes",
                None,
                None,
                KeyRule.identity(),
                f"{item.file_name} is left out: at most {MAX_JOIN_PARTS} files are joined onto one list at a "
                "time. Join it in a second step.",
            )
        )
    for item in others[:MAX_JOIN_PARTS]:
        best: tuple[str, KeyRuleProposal] | None = None
        for candidate in _key_candidates(item, base_key_column):
            tried = propose_key_rules(
                _key_column(read, item.upload_id, item.frame, _by_name(item.frame)[candidate]), base_keys
            )
            if best is None or tried.fixed_coverage > best[1].fixed_coverage:
                best = (candidate, tried)
        kind = _guess_kind(item, best[0] if best else "")
        measured.append((item, kind, best[0] if best else None, best[1] if best else None))
    # One rule for the base's IDs, from the first file that needs one; every file then meets the base written
    # that way (several files joined to one list share the list's match key).
    base_rule = next(
        (
            found.base_rule
            for _, _, _, found in measured
            if found is not None and found.meets and not found.base_rule.changes_nothing
        ),
        KeyRule.identity(),
    )
    for item, kind, column, found in measured:
        if column is not None and found is not None and found.meets and found.base_rule != base_rule:
            found = fit_rule(
                _key_column(read, item.upload_id, item.frame, _by_name(item.frame)[column]),
                base_keys,
                base_rule,
            )
        if column is None or found is None or not found.meets:
            coverage = found.coverage if found else 0.0
            plans.append(
                JoinPartPlan(
                    upload_id=item.upload_id,
                    file_name=item.file_name,
                    kind=kind,
                    key_column=column,
                    coverage=coverage,
                    fixed_coverage=found.fixed_coverage if found else 0.0,
                    key_rule=KeyRule.identity(),
                    base_key_rule=base_rule,
                    joined=False,
                    summary=(
                        f"{item.file_name} is left out: at most {coverage:.0%} of its IDs are in "
                        f"{base.file_name}, and no tidy-up of the IDs makes {KEY_JOIN_OK:.0%} meet. "
                        "Nothing is joined on a near match."
                    ),
                )
            )
            continue
        stem = _stem(item.file_name)
        if kind == "outcome_list":
            name = outcome_name or f"in_{stem}"
            while name in taken:
                name = f"{name}_{stem}"
            taken.add(name)
            part = JoinPart(
                upload_id=item.upload_id,
                file_name=item.file_name,
                kind="outcome_list",
                key_column=column,
                key_rule=found.rule,
                outcome_column=name,
            )
        else:
            picks: list[ColumnPick] = []
            for source in _by_name(item.frame):
                if source == column:
                    continue
                output = source
                if output in taken:
                    output = f"{stem}_{source}"
                    serial = 2
                    while output in taken:
                        output = f"{stem}_{source}_{serial}"
                        serial += 1
                taken.add(output)
                picks.append(ColumnPick(source=source, output=output))
            if not picks:
                plans.append(
                    _left_out(
                        item,
                        kind,
                        column,
                        found,
                        base_rule,
                        f"{item.file_name} is left out: it has no column but its customer column, so it adds "
                        "nothing to the list.",
                    )
                )
                continue
            part = JoinPart(
                upload_id=item.upload_id,
                file_name=item.file_name,
                kind="attributes",
                key_column=column,
                key_rule=found.rule,
                columns=tuple(picks),
            )
        parts.append(part)
        tidy = "" if not found.needed else f" once the IDs are tidied ({found.rule.words()})"
        plans.append(
            JoinPartPlan(
                upload_id=item.upload_id,
                file_name=item.file_name,
                kind=kind,
                key_column=column,
                coverage=found.coverage,
                fixed_coverage=found.fixed_coverage,
                key_rule=found.rule,
                base_key_rule=base_rule,
                joined=True,
                summary=f"{found.fixed_coverage:.0%} of the IDs in {item.file_name} are in {base.file_name}{tidy}.",
            )
        )
    if not parts:
        return JoinProposal(
            spec=None,
            receipt=None,
            parts=tuple(plans),
            normalise_key=(),
            outcome_sentences=(),
            summary=f"No file meets {base.file_name} on its IDs, so nothing is proposed.",
        )
    spec = JoinSpec(
        base_upload_id=base.upload_id,
        base_file_name=base.file_name,
        base_key_column=base_key_column,
        base_key_rule=base_rule,
        parts=tuple(parts),
    )
    try:
        run = _run_join(base.frame, {item.upload_id: item.frame for item in others}, spec, read)
    except JoinError as exc:
        return JoinProposal(
            spec=None,
            receipt=None,
            parts=tuple(plans),
            normalise_key=(),
            outcome_sentences=(),
            summary=f"These files cannot be joined as they are: {exc.message}",
        )
    tidied = tuple(
        name
        for name, rule in (
            (base.file_name, spec.base_key_rule),
            *((part.file_name, part.key_rule) for part in spec.parts),
        )
        if not rule.changes_nothing
    )
    sentences = tuple(
        outcome_sentence(
            part.file_name, str(part.outcome_column), rows_without_id=run.receipt.base_rows_without_key
        )
        for part in spec.parts
        if part.kind == "outcome_list"
    )
    joined = ", ".join(part.file_name for part in spec.parts)
    return JoinProposal(
        spec=spec,
        receipt=run.receipt,
        parts=tuple(plans),
        normalise_key=tidied,
        outcome_sentences=sentences,
        summary=(
            f"Join {joined} onto {base.file_name}: {run.receipt.rows_out} rows, one per row of "
            f"{base.file_name}." + (" This needs your own approval." if run.receipt.approval_required else "")
        ),
    )
