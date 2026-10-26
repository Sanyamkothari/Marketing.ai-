"""Data Doctor drives the onboarding builder (Plan O M237, DEC-1807; closes DEC-1550 and DEC-1578).

Until now Data Doctor said how several files fit (DEC-1550) and, for an agreed outcome, printed the onboarding
calls a person or a screen then had to make by hand (`OutcomeAgreement.next_steps`, DEC-1578). This module
turns those words into a plan and runs it, in two separate halves:

* `plan_build` is the **`build_dataset` proposal** (PROPOSE, rules): from the diagnosis (each file's role -
  `propose_role` - its ID and date columns, the stacks it found), the frames, and an outcome (`LabelSpec`, for
  example an agreement's), it proposes a `BuildPlan`: the customer list and each event log as a builder source,
  monthly exports stacked (`stack_files`), the rule each side's IDs are compared by where they do not meet as
  written (`normalise_key`), which column is `entity_key` and `event_time` in each (`propose_mapping`'s required
  columns; the builder's own suggestion adds the rest), the features and the snapshot rule. It is pending,
  `check`, never ticked, and lists every APPROVE-class reason it carries (rows a stack removes, log IDs written
  as the list writes them, a log losing more than 2% of its rows to the join, the outcome). A file it cannot
  place is left out with a code, never guessed into the build.
* `drive_builder` is an **executor, never a tool** (design §5.1: APPLY is never a tool). It runs only a plan
  whose hash the person approved (`BuildApproval`), with the exact sentence for an APPROVE-class plan. It
  stacks, applies each log's `normalise_key` step through the recipe engine (so the step's receipt is the
  recipe's own), and writes each log's matched IDs **as the customer list writes them**: the list's own IDs are
  never rewritten, so the built dataset, and every score joined back on `entity_key`, names the client's
  customers as the client does (`CUST-00123`, not `123`). It then calls the builder through a `BuilderPort`:
  add each source, suggest and save each mapping, save the onboarding recipe with the label unchanged, build,
  and keep the approved plan beside the recipe (`BuildRecord`) so that `replay_build` runs next month's (or a
  scoring) file through the same frozen rules. `engine.doctor.builder_api.ApiBuilderPort` is the adapter over
  the HTTP routes the Manual setup screens use, so the dataset is built by exactly the builder a person would
  have driven.

The plan holds column names, roles, rules and counts; no ID and no cell.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal, Protocol

import numpy as np
import numpy.typing as npt
import pandas as pd
from pydantic import AwareDatetime, Field

from engine.agent.config import AgentLevel
from engine.agent.contracts import Question, RecipeReceipt, RecipeStep, RecipeStepKind
from engine.agent.key_steps import (
    KeyColumn,
    KeyRule,
    KeyRuleProposal,
    factorize_exact,
    fit_rule,
    propose_key_rules,
)
from engine.agent.recipe import RecipeError, run_recipe
from engine.agentic.canonical import canonical_bytes, sha256_hex
from engine.config import AggFunction, FeatureDef, LabelDefinition, SnapshotDefinition, StrictBase
from engine.doctor.contracts import Diagnosis, FileDiagnosis, FileStack
from engine.doctor.join_files import APPROVE_DROP_SHARE
from engine.doctor.order_roll_up import (
    EVENT_TIME,
    ORDER_LINES_ROLE,
    ORDERS_ROLE,
    RETURNS_ROLE,
    LogColumns,
    OrderLogError,
    RolledUp,
    list_matches_log,
    roll_up,
)
from engine.doctor.stack_files import (
    ConflictRule,
    StackError,
    StackFile,
    StackReceipt,
    StackSpec,
    plan_stack,
    run_stack,
)

__all__ = [
    "BUILD_CODES",
    "BuildApproval",
    "BuildDriveError",
    "BuildDriveResult",
    "BuildPlan",
    "BuildRecord",
    "BuilderPort",
    "DiagnosisRoles",
    "KeyMatchReceipt",
    "LeftOutFile",
    "ListKeys",
    "MappedColumn",
    "OrderLogInput",
    "OrderLogPlan",
    "PlannedSource",
    "PreparedSources",
    "RoleProposer",
    "RoleSuggestion",
    "approval_for",
    "build_record_key",
    "drive_builder",
    "mapping_columns",
    "order_log_files",
    "plan_build",
    "prepare_sources",
    "replay_build",
]

BUILD_CODES: Final[frozenset[str]] = frozenset(
    {
        "BUILD_PLAN_NOT_APPROVED",
        "BUILD_PLAN_INCOMPLETE",
        "BUILD_REWRITES_IDS",
        "BUILD_DEFINES_OUTCOME",
        "BUILD_FILE_LEFT_OUT",
        "BUILD_STEP_FAILED",
        "BUILD_REPLAY_FILES",
    }
)
"""The codes this module raises or records (M237). `APPROVAL_SENTENCE_CHANGED` is M246's (PLAN_O_CODES)."""

KEY_STEP_FAILURE_PCT: Final[float] = 5.0
"""The `normalise_key` step's limit of IDs it may fail to write (Plan G's default conversion limit)."""

MappingDecision = Literal["auto", "user"]


# ---------------------------------------------------------------------------
# Roles: the smallest interface to `propose_role` (M236 wraps `detect_roles` as that tool)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RoleSuggestion:
    role: str
    reason: str


class RoleProposer(Protocol):
    """What the build plan needs from `propose_role`: one file's role, or None when nothing fits."""

    def propose_role(self, upload_id: str) -> RoleSuggestion | None: ...


@dataclass(frozen=True)
class DiagnosisRoles:
    """`propose_role` answered by the diagnosis (`FileDiagnosis.kind`, onboarding's `detect_roles`)."""

    diagnosis: Diagnosis

    def propose_role(self, upload_id: str) -> RoleSuggestion | None:
        for item in self.diagnosis.files:
            if item.upload_id == upload_id and item.kind is not None:
                return RoleSuggestion(role=item.kind, reason=item.kind_reason)
        return None


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------
class MappedColumn(StrictBase):
    """A column of a rolled-up source that is mapped under its own name (the order-log path's `order_value`,
    `quantity`, `signup_date`, ...), beside `entity_key` and `event_time`."""

    column: str
    cast: str | None = Field(
        default=None, description="A `ColumnTransform` type to cast to, or null to keep it."
    )


class PlannedSource(StrictBase):
    """One builder source: one file, or several files stacked into one."""

    source_name: str = Field(description="The file name the source is added under.")
    role: str = Field(description="Its onboarding role (`entity`, `bills`, ...), from `propose_role`.")
    role_reason: str
    upload_ids: tuple[str, ...] = Field(min_length=1)
    stack: StackSpec | None = Field(default=None, description="Set when several exports are stacked first.")
    key_column: str = Field(description="Mapped to `entity_key`.")
    key_rule: KeyRule = Field(default_factory=KeyRule.identity, description="The `normalise_key` step on it.")
    date_column: str | None = Field(default=None, description="Mapped to `event_time` (event logs only).")
    coverage: float | None = Field(
        default=None, description="Share of its IDs on the customer list as written."
    )
    fixed_coverage: float | None = Field(default=None, description="The same share under the key rules.")
    rows: int | None = Field(default=None, description="Event logs: rows in its files, before stacking.")
    rows_dropped: int | None = Field(
        default=None,
        description=(
            "Event logs: rows the join to the customer list leaves out - no ID, a customer not on the list, or "
            "an ID that two of the list's customers share under the rules."
        ),
    )
    dropped_share: float | None = Field(
        default=None,
        description="`rows_dropped` / `rows`, rounded to 4 places; over 2% needs the person's yes.",
    )
    mapped: tuple[MappedColumn, ...] = Field(
        default=(),
        exclude_if=lambda value: not value,
        description="Order-log path: the other columns the mapping carries, under their own names.",
    )


class LeftOutFile(StrictBase):
    upload_id: str
    file_name: str
    code: str
    summary: str


_COUNT_FIELDS: Final[tuple[str, ...]] = (
    "lines",
    "lines_without_shopper",
    "lines_without_order",
    "lines_unreadable_date",
    "lines_unreadable_quantity",
    "order_lines",
    "return_lines",
    "zero_lines",
    "orders",
    "return_orders",
    "cancelled_return_orders",
    "return_lines_without_c",
    "two_timestamp_orders",
    "shoppers",
    "shoppers_without_orders",
    "unpriced_lines",
)
"""The roll-up's counts (`RollUpCounts`) a plan keeps, so its hash covers what was approved."""


class OrderLogPlan(StrictBase):
    """What a plan built from one raw order log carries beyond a plan from several files (v3 phase 0, B4).

    The columns a person and the reading decided, and the counts the roll-up measured on the approved log, so the
    plan hash covers the roll-up: a log that differs in any counted way is not the one that was approved. No ID and
    no cell."""

    upload_id: str
    file_name: str
    customer_column: str
    order_column: str
    date_column: str
    quantity_column: str | None = None
    price_column: str | None = None
    product_column: str | None = None
    country_column: str | None = None
    lines: int
    lines_without_shopper: int
    lines_without_order: int
    lines_unreadable_date: int
    lines_unreadable_quantity: int
    order_lines: int
    return_lines: int
    zero_lines: int
    orders: int
    return_orders: int
    cancelled_return_orders: int
    return_lines_without_c: int
    two_timestamp_orders: int
    shoppers: int
    shoppers_without_orders: int
    unpriced_lines: int
    skipped_features: tuple[str, ...] = Field(
        default=(), description="Suggested features left out because the log has no column they read."
    )

    def columns(self) -> LogColumns:
        return LogColumns(
            customer=self.customer_column,
            order=self.order_column,
            date=self.date_column,
            quantity=self.quantity_column,
            price=self.price_column,
            product=self.product_column,
            country=self.country_column,
        )

    def counts(self) -> dict[str, int]:
        """The roll-up's counts, as `RollUpCounts` names them."""
        return {name: int(getattr(self, name)) for name in _COUNT_FIELDS}


class BuildPlan(StrictBase):
    """`build_dataset`'s proposal: pending, `check`, never ticked; runs only once its hash is approved."""

    state: Literal["pending"] = "pending"
    confidence: Literal["check"] = "check"
    never_ticked: Literal[True] = True
    use_case: str
    entity: PlannedSource
    events: tuple[PlannedSource, ...]
    features: tuple[FeatureDef, ...]
    label_spec: LabelDefinition | None
    snapshot_spec: SnapshotDefinition
    left_out: tuple[LeftOutFile, ...] = ()
    questions: tuple[Question, ...] = Field(
        default=(), description="Open questions; the plan waits for them."
    )
    stack_receipts: tuple[StackReceipt, ...] = Field(default=(), description="Each stack's dry run.")
    approval_reasons: tuple[str, ...] = Field(description="Why the person's own yes is needed, as codes.")
    approval_sentence: str = Field(description="What the person agrees to, word for word.")
    blocking: tuple[str, ...] = Field(default=(), description="Codes that keep the plan from running.")
    summary: str
    order_log: OrderLogPlan | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description=(
            "Set only for a plan built from one raw order log (`plan_build(order_log=...)`): the customer list is "
            "derived from the log and the sources are its roll-up. Left out of a dump (and the hash) when unset."
        ),
    )

    @property
    def plan_hash(self) -> str:
        """Hash of what the plan does: never of its wording, its dry-run receipts or its state fields."""
        logic = self.model_dump(
            mode="json",
            exclude={"state", "confidence", "never_ticked", "summary", "stack_receipts", "questions"},
        )
        return sha256_hex(canonical_bytes(logic))


def _frame_column(frame: pd.DataFrame, name: str) -> pd.Series[Any]:
    names = {str(column): column for column in frame.columns}
    return frame[names[name]]


def _features(roles: Sequence[str]) -> tuple[FeatureDef, ...]:
    """Two counts every event log supports with nothing but its ID and date: how many in 90 days, and how
    long since the last one. A use case's richer features stay its own choice (Settings -> Advanced)."""
    out: list[FeatureDef] = []
    for role in dict.fromkeys(roles):
        out.append(FeatureDef(name=f"{role}_90d", role=role, function=AggFunction.COUNT, window_days=90))
        out.append(
            FeatureDef(name=f"days_since_last_{role}", role=role, function=AggFunction.DAYS_SINCE_LAST)
        )
    return tuple(out)


class KeyMatchReceipt(StrictBase):
    """How one log's IDs met the customer list: every row counted (Plan O §3.1, "nothing is silent")."""

    source_name: str
    rows: int
    rows_without_key: int = Field(description="Rows with an empty customer column.")
    rows_matched: int = Field(description="Rows whose customer is on the list, under the rules.")
    rows_rewritten: int = Field(
        description="Matched rows whose ID was written differently; they now carry the list's ID as written."
    )
    rows_ambiguous: int = Field(
        description="Rows whose ID two different customers of the list share under the rules; left as written."
    )
    rows_not_on_list: int = Field(description="Rows whose customer is not on the list; left as written.")
    dropped_share: float = Field(
        description="Rows the builder cannot join (all but `rows_matched`) / `rows`."
    )


@dataclass(frozen=True)
class _Met:
    """A log's distinct IDs looked up in the list: the list's ID as written (None when not found) and whether
    the ID is one two of the list's customers share."""

    written_as: npt.NDArray[np.object_]
    ambiguous: npt.NDArray[np.bool_]


class ListKeys:
    """The customer list's IDs, by the match key its rule writes them to, and back to the ID as written.

    A log's ID meets the list when its match key (by the log's rule) is the match key of exactly one of the
    list's IDs; it is then written as the list writes it, so the list keeps its own IDs (`CUST-00123`) and the
    log reads `CUST-00123` too. A match key two of the list's IDs share (`CUST-007` and `CUST-0007` with zeros
    dropped) names no one customer: those rows are left as written and counted, never assigned to one of them.
    """

    def __init__(self, column: KeyColumn, rule: KeyRule) -> None:
        written = column.written(rule)[0]
        keys, key_values = factorize_exact(written)
        ids, _ = factorize_exact(column.texts)
        # How many different IDs (as written) each match key stands for, and the first of them.
        pairs = np.unique(keys * (int(ids.max(initial=0)) + 1) + ids)
        per_key = np.bincount(pairs // (int(ids.max(initial=0)) + 1), minlength=len(key_values))
        first_at = np.full(len(key_values), -1, dtype=np.int64)
        first_at[keys[::-1]] = np.arange(len(keys))[::-1]
        single = per_key == 1
        self.rule = rule
        self._keys: pd.Index[Any] = pd.Index(key_values[single])
        self._ids: npt.NDArray[np.object_] = column.texts[first_at[single]]
        self._shared: pd.Index[Any] = pd.Index(key_values[per_key > 1])

    def meet(self, column: KeyColumn, rule: KeyRule) -> _Met:
        written = column.written(rule)[0]
        at = np.asarray(self._keys.get_indexer(pd.Index(written, copy=False)))
        found = at >= 0
        written_as = np.full(len(written), None, dtype=object)
        written_as[found] = self._ids[at[found]]
        ambiguous = np.asarray(self._shared.get_indexer(pd.Index(written, copy=False)) >= 0, dtype=np.bool_)
        return _Met(written_as, ambiguous)

    def receipt(self, name: str, series: pd.Series[Any], column: KeyColumn, met: _Met) -> KeyMatchReceipt:
        found = np.array([value is not None for value in met.written_as], dtype=bool)
        rewritten = found & (met.written_as != column.texts)
        rows = len(series)
        matched = int(column.counts[found].sum())
        ambiguous = int(column.counts[met.ambiguous].sum())
        return KeyMatchReceipt(
            source_name=name,
            rows=rows,
            rows_without_key=rows - column.rows,
            rows_matched=matched,
            rows_rewritten=int(column.counts[rewritten].sum()),
            rows_ambiguous=ambiguous,
            rows_not_on_list=column.rows - matched - ambiguous,
            dropped_share=round((rows - matched) / rows, 4) if rows else 0.0,
        )


@dataclass(frozen=True)
class _Pending:
    """An event source measured against the customer list, before the list's one rule is chosen."""

    name: str
    suggestion: RoleSuggestion
    ids: tuple[str, ...]
    stack: StackSpec | None
    key_column: str
    date_column: str
    keys: pd.Series[Any]
    column: KeyColumn
    measured: KeyRuleProposal


def _first_date(item: FileDiagnosis) -> str | None:
    return item.date_columns[0] if item.date_columns else None


def plan_build(
    diagnosis: Diagnosis,
    frames: Mapping[str, pd.DataFrame],
    *,
    use_case: str,
    label_spec: LabelDefinition | None,
    snapshot_spec: SnapshotDefinition | None = None,
    roles: RoleProposer | None = None,
    stack_answers: Mapping[str, ConflictRule] | None = None,
    order_log: OrderLogInput | None = None,
    suggested: Sequence[FeatureDef] = (),
) -> BuildPlan:
    """Propose how the diagnosed files become one built dataset (the `build_dataset` tool's rules).

    With `order_log` (v3 phase 0, B4) the dataset is built from one raw order log instead: no file need be a customer
    list, because the list is derived from the log's shopper column (the one a person chose) and the sources are the
    log's roll-up. It is an explicit argument, never inferred, so without it nothing here changes. `suggested` are the
    use case's own features; those that read a column the log does not have are left out and named."""
    if order_log is not None:
        return _plan_order_log(
            diagnosis,
            frames,
            use_case=use_case,
            label_spec=label_spec,
            snapshot_spec=snapshot_spec,
            order_log=order_log,
            suggested=suggested,
        )
    proposer: RoleProposer = roles or DiagnosisRoles(diagnosis)
    answers = dict(stack_answers or {})
    files = {item.upload_id: _named(item) for item in diagnosis.files if item.upload_id in frames}
    left: list[LeftOutFile] = []
    role_of: dict[str, RoleSuggestion] = {}
    for upload_id, item in files.items():
        suggestion = proposer.propose_role(upload_id)
        if suggestion is None:
            left.append(
                LeftOutFile(
                    upload_id=upload_id,
                    file_name=item.file_name,
                    code="BUILD_FILE_LEFT_OUT",
                    summary=f"{item.file_name} is left out: Data Doctor cannot tell what kind of file it is.",
                )
            )
            continue
        role_of[upload_id] = suggestion
    lists = [files[u] for u, s in role_of.items() if s.role == "entity" and files[u].id_column is not None]
    if not lists:
        raise BuildDriveError(
            "BUILD_PLAN_INCOMPLETE",
            "No file is a customer list with an ID column, so there is nothing to build the table around.",
        )
    customer = max(lists, key=lambda item: item.rows)
    list_keys = KeyColumn(_frame_column(frames[customer.upload_id], str(customer.id_column)))
    for item in lists:
        if item.upload_id != customer.upload_id:
            left.append(
                LeftOutFile(
                    upload_id=item.upload_id,
                    file_name=item.file_name,
                    code="BUILD_FILE_LEFT_OUT",
                    summary=(
                        f"{item.file_name} is a second customer list; join it onto {customer.file_name} first "
                        "(join_files)."
                    ),
                )
            )
    stacked: dict[str, FileStack] = {}
    for stack in diagnosis.stacks:
        placed = all(upload_id in role_of and upload_id in frames for upload_id in stack.upload_ids)
        if placed and role_of[stack.upload_ids[0]].role != "entity":
            for upload_id in stack.upload_ids:
                stacked[upload_id] = stack
    links = {link.upload_id: link for link in diagnosis.links}
    events: list[PlannedSource] = []
    receipts: list[StackReceipt] = []
    questions: list[Question] = []
    blocking: list[str] = []
    pending: list[_Pending] = []
    seen: set[str] = set()
    for upload_id, suggestion in role_of.items():
        if suggestion.role == "entity" or upload_id in seen:
            continue
        item = files[upload_id]
        group = stacked.get(upload_id)
        ids = tuple(group.upload_ids) if group else (upload_id,)
        seen.update(ids)
        first = files[ids[0]]
        link = links.get(first.upload_id)
        key_column = link.column if link is not None else first.id_column
        date_column = _first_date(first)
        if key_column is None or date_column is None:
            for one in ids:
                left.append(
                    LeftOutFile(
                        upload_id=one,
                        file_name=files[one].file_name,
                        code="BUILD_FILE_LEFT_OUT",
                        summary=(
                            f"{files[one].file_name} is left out: it has no "
                            + ("column naming the customer." if key_column is None else "date column.")
                        ),
                    )
                )
            continue
        stack_spec: StackSpec | None = None
        if group is not None:
            proposal = plan_stack(
                group,
                frames,
                key_column=key_column,
                date_column=date_column,
                on_conflict=answers.get(ids[0]),
            )
            stack_spec = proposal.spec
            if proposal.question is not None:
                questions.append(proposal.question.model_copy(update={"question_id": f"stack-{ids[0]}"[:64]}))
                blocking.append("STACK_CONFLICTS_UNDECIDED")
            if proposal.receipt is not None:
                receipts.append(proposal.receipt)
        keys = pd.concat(
            [_frame_column(frames[one], key_column) for one in ids if key_column in frames[one]],
            ignore_index=True,
        )
        column = KeyColumn(keys)
        name = item.file_name if group is None else f"{suggestion.role}_stacked.csv"
        pending.append(
            _Pending(
                name,
                suggestion,
                ids,
                stack_spec,
                key_column,
                date_column,
                keys,
                column,
                propose_key_rules(column, list_keys),
            )
        )
    # One rule for the customer list's IDs, from the first log that needs one; every log then meets the list
    # written that way (the builder joins on one `entity_key`).
    base_rule = next(
        (
            p.measured.base_rule
            for p in pending
            if p.measured.meets and not p.measured.base_rule.changes_nothing
        ),
        KeyRule.identity(),
    )
    on_list = ListKeys(list_keys, base_rule)
    for p in pending:
        measured = p.measured
        if measured.meets and measured.base_rule != base_rule:
            measured = fit_rule(p.column, list_keys, base_rule)
        if not measured.meets:
            for one in p.ids:
                left.append(
                    LeftOutFile(
                        upload_id=one,
                        file_name=files[one].file_name,
                        code="JOIN_KEYS_DO_NOT_MEET",
                        summary=(
                            f"{files[one].file_name} is left out: only {measured.fixed_coverage:.0%} of its customers "
                            f"are in {customer.file_name}, even with the IDs tidied. Nothing is joined on a near match."
                        ),
                    )
                )
            continue
        # Rows the builder will not join, counted on every row of the log's files: no ID, a customer not on
        # the list, or an ID two of the list's customers share (the acceptance's "a join that drops >2%").
        met = on_list.meet(p.column, measured.rule)
        counted = on_list.receipt(p.name, p.keys, p.column, met)
        events.append(
            PlannedSource(
                source_name=p.name,
                role=p.suggestion.role,
                role_reason=p.suggestion.reason,
                upload_ids=p.ids,
                stack=p.stack,
                key_column=p.key_column,
                key_rule=measured.rule,
                date_column=p.date_column,
                coverage=measured.coverage,
                fixed_coverage=measured.fixed_coverage,
                rows=counted.rows,
                rows_dropped=counted.rows - counted.rows_matched,
                dropped_share=counted.dropped_share,
            )
        )
    entity = PlannedSource(
        source_name=customer.file_name,
        role="entity",
        role_reason=role_of[customer.upload_id].reason,
        upload_ids=(customer.upload_id,),
        key_column=str(customer.id_column),
        key_rule=base_rule,
    )
    event_roles = [source.role for source in events]
    label_role = None if label_spec is None else label_spec.role
    if label_spec is not None and label_spec.type.value.startswith("event") and label_role not in event_roles:
        blocking.append("BUILD_PLAN_INCOMPLETE")
    reasons: list[str] = []
    if _rewrites(base_rule, events):
        reasons.append("BUILD_REWRITES_IDS")
    if any((source.dropped_share or 0.0) > APPROVE_DROP_SHARE for source in events):
        reasons.append("JOIN_DROPS_ROWS")
    for receipt in receipts:
        reasons += [reason for reason in receipt.approval_reasons if reason not in reasons]
    if label_spec is not None:
        reasons.append("BUILD_DEFINES_OUTCOME")
    sentence = _approval_sentence(customer.file_name, events, base_rule, label_spec, receipts)
    return BuildPlan(
        use_case=use_case,
        entity=entity,
        events=tuple(events),
        features=_features(event_roles),
        label_spec=label_spec,
        snapshot_spec=snapshot_spec or SnapshotDefinition(),
        left_out=tuple(left),
        questions=tuple(questions),
        stack_receipts=tuple(receipts),
        approval_reasons=tuple(reasons),
        approval_sentence=sentence,
        blocking=tuple(dict.fromkeys(blocking)),
        summary=(
            f"Build one table for {use_case} from {customer.file_name} and "
            f"{len(events)} event source{'' if len(events) == 1 else 's'}"
            + (f"; {len(left)} file{'' if len(left) == 1 else 's'} left out." if left else ".")
        ),
    )


@dataclass(frozen=True)
class OrderLogInput:
    """The one raw order log a plan is built from, and the columns that play a part in it (v3 phase 0)."""

    upload_id: str
    columns: LogColumns


def _feature_columns(feature: FeatureDef) -> set[str]:
    found: set[str] = set()
    for part in (feature, feature.of, feature.over):
        if part is None:
            continue
        if part.column:
            found.add(part.column)
        if part.where is not None:
            found.add(part.where.column)
    return found


def _sources_of(
    rolled: RolledUp, upload_id: str, stem: str, name: str, customer: str
) -> tuple[PlannedSource, ...]:
    """The event sources of a roll-up (orders always; order lines and returns when there are some), each with the
    columns it carries under their own names."""
    out: list[PlannedSource] = []
    for role, frame, reason in (
        (ORDERS_ROLE, rolled.orders, "one row per order with a quantity above zero"),
        (ORDER_LINES_ROLE, rolled.order_lines, "one row per line with a quantity above zero"),
        (RETURNS_ROLE, rolled.returns, "one row per order number with a quantity below zero"),
    ):
        if role != ORDERS_ROLE and frame.empty:
            continue
        out.append(
            PlannedSource(
                source_name=f"{stem}.{role}.csv",
                role=role,
                role_reason=f"{reason}, rolled up from the lines of {name}",
                upload_ids=(upload_id,),
                key_column=customer,
                date_column=EVENT_TIME,
                mapped=tuple(
                    MappedColumn(column=str(c)) for c in frame.columns if c not in {customer, EVENT_TIME}
                ),
            )
        )
    return tuple(out)


def _order_log_sentence(
    name: str, plan: OrderLogPlan, label_spec: LabelDefinition | None, skipped: Sequence[str]
) -> str:
    customer = plan.customer_column
    parts = [
        f"Build the table from {name}: one shopper for each different '{customer}' ({plan.shoppers:,}), with their "
        f"orders, order lines and returns rolled up from {plan.lines:,} lines, using only what happened on or "
        "before each month-end."
    ]
    if plan.lines_without_shopper:
        parts.append(f"{plan.lines_without_shopper:,} lines with no '{customer}' are left out.")
    left = plan.lines_without_order + plan.lines_unreadable_date + plan.lines_unreadable_quantity
    if left:
        parts.append(
            f"{left:,} more lines are left out: {plan.lines_without_order:,} with no '{plan.order_column}', "
            f"{plan.lines_unreadable_date:,} whose date cannot be read and {plan.lines_unreadable_quantity:,} whose "
            "quantity is not a number."
        )
    if plan.return_lines:
        parts.append(
            f"{plan.return_lines:,} lines have a quantity below zero ({plan.return_orders:,} order numbers, "
            f"{plan.cancelled_return_orders:,} of them starting with 'C'); they are counted as returns, not "
            f"orders, and {plan.return_lines_without_c:,} of those lines are on numbers that do not start with 'C'."
        )
    if plan.zero_lines:
        parts.append(f"{plan.zero_lines:,} lines with a quantity of zero count for nothing.")
    if plan.unpriced_lines and plan.price_column:
        parts.append(
            f"{plan.unpriced_lines:,} order lines have a '{plan.price_column}' of zero or less and add nothing to spend."
        )
    if plan.shoppers_without_orders:
        parts.append(f"{plan.shoppers_without_orders:,} shoppers have only returns and are in no month-end.")
    parts.append("Description is not used.")
    if skipped:
        parts.append(f"Not built, the log has no column they read: {', '.join(skipped)}.")
    if label_spec is not None:
        parts.append(f"The outcome is '{label_spec.name}'.")
    return " ".join(parts)


def _plan_order_log(
    diagnosis: Diagnosis,
    frames: Mapping[str, pd.DataFrame],
    *,
    use_case: str,
    label_spec: LabelDefinition | None,
    snapshot_spec: SnapshotDefinition | None,
    order_log: OrderLogInput,
    suggested: Sequence[FeatureDef],
) -> BuildPlan:
    log = frames.get(order_log.upload_id)
    item = next((f for f in diagnosis.files if f.upload_id == order_log.upload_id), None)
    if log is None or item is None:
        raise BuildDriveError(
            "BUILD_PLAN_INCOMPLETE", "The order log is not one of the files, so nothing is built from it."
        )
    item = _named(item)
    columns = order_log.columns
    try:
        rolled = roll_up(log, columns)
    except OrderLogError as exc:
        raise BuildDriveError(exc.code, exc.message) from exc
    if not list_matches_log(rolled.customers, log, columns.customer):
        raise BuildDriveError(
            "BUILD_PLAN_INCOMPLETE",
            f"The customer list does not hold exactly the different '{columns.customer}' values of {item.file_name}.",
        )
    stem = item.file_name.rsplit(".", 1)[0] if "." in item.file_name else item.file_name
    entity = PlannedSource(
        source_name=f"{stem}.customers.csv",
        role="entity",
        role_reason=(
            f"the different '{columns.customer}' values of {item.file_name}, the column a person named as the shopper"
        ),
        upload_ids=(order_log.upload_id,),
        key_column=columns.customer,
        mapped=(MappedColumn(column="signup_date", cast="date"),),
    )
    events = _sources_of(rolled, order_log.upload_id, stem, item.file_name, columns.customer)
    sources = {source.role: set(rolled.tables()[source.role].columns) for source in events}
    kept: list[FeatureDef] = []
    skipped: list[str] = []
    for feature in suggested:
        available = sources.get(feature.role)
        if available is None or _feature_columns(feature) - available:
            skipped.append(feature.name)
        else:
            kept.append(feature)
    features = tuple(kept) if suggested else _features([source.role for source in events])
    counts = rolled.counts
    spec = OrderLogPlan(
        upload_id=order_log.upload_id,
        file_name=item.file_name,
        customer_column=columns.customer,
        order_column=str(columns.order),
        date_column=columns.date,
        quantity_column=columns.quantity,
        price_column=columns.price,
        product_column=columns.product,
        country_column=columns.country,
        skipped_features=tuple(skipped),
        **{name: getattr(counts, name) for name in _COUNT_FIELDS},
    )
    blocking: list[str] = []
    if (
        label_spec is not None
        and label_spec.type.value.startswith("event")
        and label_spec.role not in sources
    ):
        blocking.append("BUILD_PLAN_INCOMPLETE")
    reasons: list[str] = []
    if label_spec is not None:
        reasons.append("BUILD_DEFINES_OUTCOME")
    if counts.lines and counts.lines_without_shopper / counts.lines > APPROVE_DROP_SHARE:
        reasons.append("JOIN_DROPS_ROWS")
    left_out = tuple(
        LeftOutFile(
            upload_id=other.upload_id,
            file_name=_named(other).file_name,
            code="BUILD_FILE_LEFT_OUT",
            summary=(
                f"{_named(other).file_name} is left out: the dataset is built from the order log "
                f"{item.file_name} alone."
            ),
        )
        for other in diagnosis.files
        if other.upload_id != order_log.upload_id and other.upload_id in frames
    )
    return BuildPlan(
        use_case=use_case,
        entity=entity,
        events=events,
        features=features,
        label_spec=label_spec,
        snapshot_spec=snapshot_spec or SnapshotDefinition(),
        left_out=left_out,
        approval_reasons=tuple(reasons),
        approval_sentence=_order_log_sentence(item.file_name, spec, label_spec, skipped),
        blocking=tuple(blocking),
        summary=(
            f"Build one table for {use_case} from {item.file_name}: {counts.shoppers:,} shoppers rolled up from "
            f"{counts.lines:,} lines."
        ),
        order_log=spec,
    )


def order_log_files(
    plan: BuildPlan, log: pd.DataFrame, *, strict: bool = True
) -> dict[str, list[pd.DataFrame]]:
    """The sources of an order-log plan, rolled up from the log by the plan's own columns.

    `strict` (an approved run) refuses a log whose counts differ from the approved plan's: the plan hash covers the
    roll-up, so a different log is a different plan. A replay (`strict=False`) is next month's whole log through the
    frozen rules: the counts may change, the meaning may not."""
    spec = plan.order_log
    if spec is None:
        raise BuildDriveError("BUILD_PLAN_INCOMPLETE", "This plan is not built from an order log.")
    try:
        rolled = roll_up(log, spec.columns())
    except OrderLogError as exc:
        raise BuildDriveError(exc.code, exc.message) from exc
    if strict:
        now = {name: int(getattr(rolled.counts, name)) for name in _COUNT_FIELDS}
        approved = spec.counts()
        if now != approved:
            changed = sorted(name for name in now if now[name] != approved[name])
            raise BuildDriveError(
                "BUILD_PLAN_NOT_APPROVED",
                f"{spec.file_name} is not the log that was approved ({', '.join(changed)} differ); plan again "
                "and approve again.",
            )
    if not list_matches_log(rolled.customers, log, spec.customer_column):
        raise BuildDriveError(
            "BUILD_PLAN_INCOMPLETE",
            f"The customer list does not hold exactly the different '{spec.customer_column}' values of {spec.file_name}.",
        )
    tables = rolled.tables()
    files: dict[str, list[pd.DataFrame]] = {plan.entity.source_name: [rolled.customers]}
    for source in plan.events:
        files[source.source_name] = [tables[source.role]]
    return files


def _named(item: FileDiagnosis) -> FileDiagnosis:
    """The file with a name to print: its stored name, else its upload id, never an empty string (the plan names
    each source in the approval sentence and in the files it leaves out)."""
    name = item.file_name.strip()
    return item if name else item.model_copy(update={"file_name": item.upload_id})


def _rewrites(base_rule: KeyRule, events: Sequence[PlannedSource]) -> bool:
    """Whether some log's IDs are written as the list writes them (a rule on either side)."""
    return not base_rule.changes_nothing or any(not source.key_rule.changes_nothing for source in events)


def _approval_sentence(
    list_name: str,
    events: Sequence[PlannedSource],
    base_rule: KeyRule,
    label_spec: LabelDefinition | None,
    receipts: Sequence[StackReceipt],
) -> str:
    parts = [f"Build the table from {list_name} and " + ", ".join(e.source_name for e in events) + "."]
    if _rewrites(base_rule, events):
        ruled = [e for e in events if not (base_rule.changes_nothing and e.key_rule.changes_nothing)]
        parts.append(
            f"The customer IDs in {', '.join(e.source_name for e in ruled)} are matched to {list_name} by a rule "
            f"({list_name}: {base_rule.words()}; "
            + "; ".join(f"{e.source_name}: {e.key_rule.words()}" for e in ruled)
            + f") and written as {list_name} writes them; {list_name} keeps its IDs as they are."
        )
    for event in events:
        if (event.dropped_share or 0.0) > APPROVE_DROP_SHARE and event.rows:
            parts.append(
                f"Joining {event.source_name} to {list_name} leaves out {event.rows_dropped} of {event.rows} rows "
                f"({event.dropped_share:.1%}): they have no customer ID, or their customer is not in {list_name}."
            )
    for receipt in receipts:
        parts.append(
            f"Stacking removes {receipt.duplicates_removed + receipt.conflict_rows_removed} of "
            f"{receipt.rows_in} rows that the exports repeat."
        )
    if label_spec is not None:
        parts.append(f"The outcome is '{label_spec.name}'.")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Driving the builder
# ---------------------------------------------------------------------------
class BuildDriveError(Exception):
    """A plan that cannot be proposed or run. `code` is one of `BUILD_CODES` or another module's code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class BuildApproval(StrictBase):
    """The person's yes to one plan: its hash, who, when, and the sentence they read."""

    plan_hash: str
    approved_by: str = Field(min_length=1, description="The user id; never an agent.")
    approved_at: AwareDatetime
    sentence: str = Field(description="The plan's `approval_sentence`, word for word.")


class BuilderPort(Protocol):
    """The builder calls Data Doctor makes - exactly the Manual setup screens' (sources, mappings, recipes,
    datasets) - and where the approved plan is kept beside the recipe it saved. `engine.doctor.builder_api.
    ApiBuilderPort` answers them over the HTTP routes."""

    def add_source(self, *, file_name: str, data: bytes, role: str) -> str: ...

    def suggest_mapping(self, *, source_id: str, use_case: str) -> Mapping[str, Any]: ...

    def save_mapping(
        self,
        *,
        mapping_id: str,
        source_id: str,
        use_case: str,
        role: str,
        columns: Sequence[Mapping[str, Any]],
    ) -> str: ...

    def save_spec(self, body: Mapping[str, Any]) -> str: ...

    def build(self, *, spec_id: str, mode: str) -> str: ...

    def save_record(self, record: BuildRecord) -> str | None:
        """Keep `record` beside the recipe it built (`build_record_key`); the key, or None when the port keeps
        no records."""
        ...


class BuildRecord(StrictBase):
    """The approved plan, kept beside the onboarding recipe it saved: what `replay_build` runs next month's
    files (or a scoring file) through, so the stacks and the ID rules replay exactly as approved. Rules, column
    names and counts only: no ID, no cell."""

    plan: BuildPlan
    approval: BuildApproval
    spec_id: str
    source_ids: dict[str, str] = Field(description="Planned source name to builder source id.")
    mapping_ids: tuple[str, ...]

    def check(self) -> None:
        if (
            self.approval.plan_hash != self.plan.plan_hash
            or self.approval.sentence != self.plan.approval_sentence
        ):
            raise BuildDriveError(
                "BUILD_PLAN_NOT_APPROVED",
                "This record's plan is not the one that was approved; nothing replays.",
            )


def build_record_key(client_id: str, spec_id: str) -> str:
    """Where a `BuildRecord` is kept: beside the client's sources, so erasing the client erases it too."""
    return f"clients/{client_id}/doctor_builds/{spec_id}.json"


class BuildDriveResult(StrictBase):
    plan_hash: str
    approved_by: str
    source_ids: dict[str, str] = Field(description="Planned source name to builder source id.")
    mapping_ids: tuple[str, ...]
    spec_id: str
    dataset_id: str | None
    stack_receipts: tuple[StackReceipt, ...]
    key_receipts: tuple[RecipeReceipt, ...] = Field(description="Each log's `normalise_key` step, as run.")
    match_receipts: tuple[KeyMatchReceipt, ...] = Field(
        default=(), description="How each log's IDs met the customer list and were written as it writes them."
    )
    record_key: str | None = Field(default=None, description="Where the `BuildRecord` was kept, if anywhere.")


def mapping_columns(
    suggested: Sequence[Mapping[str, Any]], required: Mapping[str, tuple[str, str | None]]
) -> list[dict[str, Any]]:
    """The mapping to save: the plan's required columns (`entity_key`, `event_time`), decided by the person
    who approved the plan, then the builder's own suggestions that name neither the same standard column nor
    the same file column."""
    taken_sources = {source for source, _ in required.values()}
    columns: list[dict[str, Any]] = [
        {
            "source": source,
            "standard": standard,
            "transform": None if cast is None else {"kind": "cast", "to_type": cast},
            "confidence": 1.0,
            "decided_by": "user",
        }
        for standard, (source, cast) in required.items()
    ]
    for column in suggested:
        if column.get("standard") in required or column.get("source") in taken_sources:
            continue
        columns.append(dict(column))
    return columns


def _stacked(
    source: PlannedSource, frames: Sequence[pd.DataFrame]
) -> tuple[pd.DataFrame, StackReceipt | None]:
    """The source's files as one table: stacked by the plan's frozen spec, or the one file as it is."""
    if source.stack is None or len(frames) == 1:
        return frames[0], None
    spec = source.stack
    if len(frames) != len(spec.files):  # next month's exports: the same rules, other files
        spec = spec.model_copy(
            update={
                "files": tuple(
                    StackFile(
                        upload_id=f"{source.source_name}#{index + 1}",
                        file_name=f"{source.source_name} #{index + 1}",
                    )
                    for index in range(len(frames))
                )
            }
        )
    try:
        run = run_stack(list(frames), spec)
    except StackError as exc:
        raise BuildDriveError(exc.code, exc.message) from exc
    return run.frame, run.receipt


def _key_step(
    frame: pd.DataFrame, source: PlannedSource, clock: Any
) -> tuple[pd.DataFrame, RecipeReceipt | None]:
    """The log with its `normalise_key` step applied by the recipe engine, or as it is."""
    if source.key_rule.changes_nothing:
        return frame, None
    step = RecipeStep(
        order=1,
        kind=RecipeStepKind.NORMALISE_KEY,
        column=source.key_column,
        params=source.key_rule.params(),
        reason=f"Write the IDs one way so the files meet: {source.key_rule.words()}.",
    )
    try:
        run = run_recipe(
            frame,
            [step],
            upload_id=source.upload_ids[0],
            primary_key=None,
            target=None,
            levels=[AgentLevel.CLEAN],
            max_failure_pct=KEY_STEP_FAILURE_PCT,
            **({"clock": clock} if clock is not None else {}),
        )
    except RecipeError as exc:
        raise BuildDriveError("BUILD_STEP_FAILED", f"{source.source_name}: {exc.message}") from exc
    return run.frame, run.receipt


def _matched(
    frame: pd.DataFrame, source: PlannedSource, on_list: ListKeys | None, clock: Any
) -> tuple[pd.DataFrame, RecipeReceipt | None, KeyMatchReceipt | None]:
    """A log whose IDs are written as the customer list writes them, where the rules make them meet; every
    other ID kept as written (the builder leaves those rows out, and the receipt has counted them)."""
    if on_list is None:
        return frame, None, None
    if source.key_column not in {str(column) for column in frame.columns}:
        raise BuildDriveError(
            "BUILD_STEP_FAILED",
            f"{source.source_name} has no column '{source.key_column}', which the plan reads.",
        )
    written, step_receipt = _key_step(frame, source, clock)
    keys = _frame_column(written, source.key_column)
    column = KeyColumn(keys)
    met = on_list.meet(column, KeyRule.identity())  # the step has written them by the log's rule
    as_list = pd.Series(column.take(met.written_as), index=frame.index, dtype=object)
    original = _frame_column(frame, source.key_column)
    out = frame.copy()
    out[source.key_column] = as_list.where(as_list.notna(), original.astype(object))
    return out, step_receipt, on_list.receipt(source.source_name, keys, column, met)


@dataclass(frozen=True)
class PreparedSources:
    """The plan's sources read from their files, before any builder call: the customer list as written and each
    log stacked and written with the list's IDs, with the receipts of those steps."""

    sources: tuple[tuple[PlannedSource, pd.DataFrame], ...]
    stack_receipts: tuple[StackReceipt, ...]
    key_receipts: tuple[RecipeReceipt, ...]
    match_receipts: tuple[KeyMatchReceipt, ...]


def prepare_sources(
    plan: BuildPlan, files: Mapping[str, Sequence[pd.DataFrame]], *, clock: Any = None
) -> PreparedSources:
    """Prepare each planned source from `files` (by source name): stack, then match each log's IDs to the list."""
    stack_receipts: list[StackReceipt] = []
    key_receipts: list[RecipeReceipt] = []
    match_receipts: list[KeyMatchReceipt] = []
    entity, _ = _stacked(plan.entity, files[plan.entity.source_name])
    on_list: ListKeys | None = None
    if _rewrites(plan.entity.key_rule, plan.events):
        if plan.entity.key_column not in {str(column) for column in entity.columns}:
            raise BuildDriveError(
                "BUILD_STEP_FAILED",
                f"{plan.entity.source_name} has no column '{plan.entity.key_column}', which the plan reads.",
            )
        on_list = ListKeys(KeyColumn(_frame_column(entity, plan.entity.key_column)), plan.entity.key_rule)
    prepared: list[tuple[PlannedSource, pd.DataFrame]] = [(plan.entity, entity)]  # the list: as written
    for source in plan.events:
        frame, stack_receipt = _stacked(source, files[source.source_name])
        if stack_receipt is not None:
            stack_receipts.append(stack_receipt)
        frame, key_receipt, match_receipt = _matched(frame, source, on_list, clock)
        if key_receipt is not None:
            key_receipts.append(key_receipt)
        if match_receipt is not None:
            match_receipts.append(match_receipt)
        prepared.append((source, frame))
    return PreparedSources(tuple(prepared), tuple(stack_receipts), tuple(key_receipts), tuple(match_receipts))


def _run_plan(
    plan: BuildPlan,
    approval: BuildApproval,
    files: Mapping[str, Sequence[pd.DataFrame]],
    port: BuilderPort,
    *,
    mode: str,
    build: bool,
    clock: Any,
) -> BuildDriveResult:
    """Prepare each planned source from `files` (by source name), then make the Manual setup calls."""
    source_ids: dict[str, str] = {}
    mapping_ids: list[str] = []
    ready = prepare_sources(plan, files, clock=clock)
    for source, frame in ready.sources:
        data = frame.to_csv(index=False).encode("utf-8")
        source_id = port.add_source(file_name=source.source_name, data=data, role=source.role)
        source_ids[source.source_name] = source_id
        suggestion = port.suggest_mapping(source_id=source_id, use_case=plan.use_case)
        required: dict[str, tuple[str, str | None]] = {"entity_key": (source.key_column, None)}
        if source.date_column is not None:
            required["event_time"] = (source.date_column, "date")
        for extra in source.mapped:
            required[extra.column] = (extra.column, extra.cast)
        columns = mapping_columns(list(suggestion.get("columns", ())), required)
        mapping_ids.append(
            port.save_mapping(
                mapping_id=str(suggestion["mapping_id"]),
                source_id=source_id,
                use_case=plan.use_case,
                role=source.role,
                columns=columns,
            )
        )
    body: dict[str, Any] = {
        "use_case": plan.use_case,
        "entity_source_id": source_ids[plan.entity.source_name],
        "event_source_ids": [source_ids[source.source_name] for source in plan.events],
        "mapping_ids": mapping_ids,
        "feature_spec": {
            "features": [feature.model_dump(mode="json", exclude_defaults=True) for feature in plan.features]
        },
        "label_spec": (
            None
            if plan.label_spec is None
            else plan.label_spec.model_dump(mode="json", by_alias=True, exclude_defaults=True)
        ),
        "snapshot_spec": plan.snapshot_spec.model_dump(mode="json", exclude_defaults=True),
    }
    spec_id = port.save_spec(body)
    record_key = port.save_record(
        BuildRecord(
            plan=plan,
            approval=approval,
            spec_id=spec_id,
            source_ids=source_ids,
            mapping_ids=tuple(mapping_ids),
        )
    )
    dataset_id = port.build(spec_id=spec_id, mode=mode) if build else None
    return BuildDriveResult(
        plan_hash=plan.plan_hash,
        approved_by=approval.approved_by,
        source_ids=source_ids,
        mapping_ids=tuple(mapping_ids),
        spec_id=spec_id,
        dataset_id=dataset_id,
        stack_receipts=ready.stack_receipts,
        key_receipts=ready.key_receipts,
        match_receipts=ready.match_receipts,
        record_key=record_key,
    )


def drive_builder(
    plan: BuildPlan,
    frames: Mapping[str, pd.DataFrame],
    port: BuilderPort,
    approval: BuildApproval,
    *,
    mode: str = "train",
    build: bool = True,
    clock: Any = None,
) -> BuildDriveResult:
    """Run an approved plan through the builder: stack, match each log's IDs to the list (written as the list
    writes them; the list itself as written), add sources, save mappings and the recipe (the label unchanged),
    keep the plan beside it, and build. Refuses a plan that is not the one approved."""
    if approval.plan_hash != plan.plan_hash:
        raise BuildDriveError(
            "BUILD_PLAN_NOT_APPROVED",
            "This plan is not the one that was approved; review it and approve again.",
        )
    if approval.sentence != plan.approval_sentence:
        raise BuildDriveError(
            "APPROVAL_SENTENCE_CHANGED",
            "The plan now reads differently from what was approved; read it again before approving.",
        )
    if plan.blocking or plan.questions:
        raise BuildDriveError(
            "BUILD_PLAN_INCOMPLETE",
            "The plan still has an open question or a missing part: "
            + ", ".join(plan.blocking or ("question",)),
        )
    if plan.order_log is not None:
        log = frames.get(plan.order_log.upload_id)
        if log is None:
            raise BuildDriveError("BUILD_REPLAY_FILES", f"Give the order log {plan.order_log.file_name}.")
        return _run_plan(
            plan, approval, order_log_files(plan, log, strict=True), port, mode=mode, build=build, clock=clock
        )
    files = {
        source.source_name: [frames[upload_id] for upload_id in source.upload_ids]
        for source in (plan.entity, *plan.events)
    }
    return _run_plan(plan, approval, files, port, mode=mode, build=build, clock=clock)


def replay_build(
    record: BuildRecord,
    files: Mapping[str, Sequence[pd.DataFrame]],
    port: BuilderPort,
    *,
    mode: str = "score",
    build: bool = True,
    clock: Any = None,
) -> BuildDriveResult:
    """Next month's files (or a scoring file) through the approved plan's frozen rules: the same stacks, the
    same ID rules (the list's IDs as written, each log's written as the list writes them), the same mappings,
    features and outcome. `files` gives each planned source's files by its `source_name`, oldest first; a
    stacked source may now have more or fewer exports. The person's approval is the record's: nothing new is
    decided here, so nothing new is asked."""
    record.check()
    plan = record.plan
    if (
        plan.order_log is not None
    ):  # the whole log again (B-8): re-uploading is idempotent and cannot double-count
        logs = files.get(plan.order_log.file_name)
        if not logs:
            raise BuildDriveError(
                "BUILD_REPLAY_FILES",
                f"Give a file for each source the plan built: {plan.order_log.file_name} is missing.",
            )
        return _run_plan(
            plan,
            record.approval,
            order_log_files(plan, logs[0], strict=False),
            port,
            mode=mode,
            build=build,
            clock=clock,
        )
    missing = [
        source.source_name for source in (plan.entity, *plan.events) if not files.get(source.source_name)
    ]
    if missing:
        raise BuildDriveError(
            "BUILD_REPLAY_FILES",
            f"Give a file for each source the plan built: {', '.join(missing)} is missing.",
        )
    return _run_plan(plan, record.approval, files, port, mode=mode, build=build, clock=clock)


def approval_for(plan: BuildPlan, *, user_id: str, at: datetime) -> BuildApproval:
    """The approval a person gives by reading `plan.approval_sentence` and saying yes (for screens and tests)."""
    return BuildApproval(
        plan_hash=plan.plan_hash, approved_by=user_id, approved_at=at, sentence=plan.approval_sentence
    )
