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
from engine.doctor.limits import limits
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
    "PlannedSource",
    "PreparedSources",
    "RoleProposer",
    "RoleSuggestion",
    "approval_for",
    "build_record_key",
    "drive_builder",
    "mapping_columns",
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


class LeftOutFile(StrictBase):
    upload_id: str
    file_name: str
    code: str
    summary: str


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
) -> BuildPlan:
    """Propose how the diagnosed files become one built dataset (the `build_dataset` tool's rules)."""
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
    if any((source.dropped_share or 0.0) > limits().gates.approve_drop_share for source in events):
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
        if (event.dropped_share or 0.0) > limits().gates.approve_drop_share and event.rows:
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
