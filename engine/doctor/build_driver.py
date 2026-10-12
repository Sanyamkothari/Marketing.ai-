"""Data Doctor drives the onboarding builder (Plan O M237, DEC-1807; closes DEC-1550 and DEC-1578).

Until now Data Doctor said how several files fit (DEC-1550) and, for an agreed outcome, printed the onboarding
calls a person or a screen then had to make by hand (`OutcomeAgreement.next_steps`, DEC-1578). This module
turns those words into a plan and runs it, in two separate halves:

* `plan_build` is the **`build_dataset` proposal** (PROPOSE, rules): from the diagnosis (each file's role -
  `propose_role` - its ID and date columns, the stacks it found), the frames, and an outcome (`LabelSpec`, for
  example an agreement's), it proposes a `BuildPlan`: the customer list and each event log as a builder source,
  monthly exports stacked (`stack_files`), the IDs written one way where they do not meet (`normalise_key`),
  which column is `entity_key` and `event_time` in each (`propose_mapping`'s required columns; the builder's own
  suggestion adds the rest), the features and the snapshot rule. It is pending, `check`, never ticked, and
  lists every APPROVE-class reason it carries (rows a stack removes, IDs written another way, the outcome).
  A file it cannot place is left out with a code, never guessed into the build.
* `drive_builder` is an **executor, never a tool** (design §5.1: APPLY is never a tool). It runs only a plan
  whose hash the person approved (`BuildApproval`), with the exact sentence for an APPROVE-class plan. It
  stacks, applies the `normalise_key` step through the recipe engine (so the step's receipt is the recipe's
  own), and calls the builder through a `BuilderPort`: add each source, suggest and save each mapping, save the
  onboarding recipe with the label unchanged, build. `engine.doctor.builder_api.ApiBuilderPort` is the adapter
  over the HTTP routes the Manual setup screens use, so the dataset is built by exactly the builder a person
  would have driven.

The plan holds column names, roles, rules and counts; no ID and no cell.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal, Protocol

import pandas as pd
from pydantic import AwareDatetime, Field

from engine.agent.config import AgentLevel
from engine.agent.contracts import Question, RecipeReceipt, RecipeStep, RecipeStepKind
from engine.agent.key_steps import KeyRule, KeyRuleProposal, fit_rule, propose_key_rules
from engine.agent.recipe import RecipeError, run_recipe
from engine.agentic.canonical import canonical_bytes, sha256_hex
from engine.config import AggFunction, FeatureDef, LabelDefinition, SnapshotDefinition, StrictBase
from engine.doctor.contracts import Diagnosis, FileDiagnosis, FileStack
from engine.doctor.stack_files import ConflictRule, StackError, StackReceipt, StackSpec, plan_stack, run_stack

__all__ = [
    "BUILD_CODES",
    "BuildApproval",
    "BuildDriveError",
    "BuildDriveResult",
    "BuildPlan",
    "BuilderPort",
    "DiagnosisRoles",
    "LeftOutFile",
    "PlannedSource",
    "RoleProposer",
    "RoleSuggestion",
    "drive_builder",
    "mapping_columns",
    "plan_build",
]

BUILD_CODES: Final[frozenset[str]] = frozenset(
    {
        "BUILD_PLAN_NOT_APPROVED",
        "BUILD_PLAN_INCOMPLETE",
        "BUILD_REWRITES_IDS",
        "BUILD_DEFINES_OUTCOME",
        "BUILD_FILE_LEFT_OUT",
        "BUILD_STEP_FAILED",
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
    files = {item.upload_id: item for item in diagnosis.files if item.upload_id in frames}
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
    list_keys = _frame_column(frames[customer.upload_id], str(customer.id_column))
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
        keys = pd.concat([_frame_column(frames[one], key_column) for one in ids if key_column in frames[one]])
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
                propose_key_rules(keys, list_keys),
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
    for p in pending:
        measured = p.measured
        if measured.meets and measured.base_rule != base_rule:
            measured = fit_rule(p.keys, list_keys, base_rule)
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
    if not base_rule.changes_nothing:
        reasons.append("BUILD_REWRITES_IDS")
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


def _approval_sentence(
    list_name: str,
    events: Sequence[PlannedSource],
    base_rule: KeyRule,
    label_spec: LabelDefinition | None,
    receipts: Sequence[StackReceipt],
) -> str:
    parts = [f"Build the table from {list_name} and " + ", ".join(e.source_name for e in events) + "."]
    if not base_rule.changes_nothing:
        parts.append(f"The customer IDs of {list_name} are written another way: {base_rule.words()}.")
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
    datasets). `engine.doctor.builder_api.ApiBuilderPort` answers them over the HTTP routes."""

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


class BuildDriveResult(StrictBase):
    plan_hash: str
    approved_by: str
    source_ids: dict[str, str] = Field(description="Planned source name to builder source id.")
    mapping_ids: tuple[str, ...]
    spec_id: str
    dataset_id: str | None
    stack_receipts: tuple[StackReceipt, ...]
    key_receipts: tuple[RecipeReceipt, ...]


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


def _source_frame(
    source: PlannedSource, frames: Mapping[str, pd.DataFrame]
) -> tuple[pd.DataFrame, StackReceipt | None]:
    if source.stack is None:
        return frames[source.upload_ids[0]], None
    try:
        run = run_stack([frames[upload_id] for upload_id in source.upload_ids], source.stack)
    except StackError as exc:
        raise BuildDriveError(exc.code, exc.message) from exc
    return run.frame, run.receipt


def _normalised(
    frame: pd.DataFrame, source: PlannedSource, clock: Any
) -> tuple[pd.DataFrame, RecipeReceipt | None]:
    """The source with its `normalise_key` step applied by the recipe engine, or as it is."""
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
    """Run an approved plan through the builder: stack, write the IDs one way, add sources, save mappings and
    the recipe (the label unchanged), and build. Refuses a plan that is not the one approved."""
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
    source_ids: dict[str, str] = {}
    mapping_ids: list[str] = []
    stack_receipts: list[StackReceipt] = []
    key_receipts: list[RecipeReceipt] = []
    for source in (plan.entity, *plan.events):
        frame, stack_receipt = _source_frame(source, frames)
        if stack_receipt is not None:
            stack_receipts.append(stack_receipt)
        frame, key_receipt = _normalised(frame, source, clock)
        if key_receipt is not None:
            key_receipts.append(key_receipt)
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
    dataset_id = port.build(spec_id=spec_id, mode=mode) if build else None
    return BuildDriveResult(
        plan_hash=plan.plan_hash,
        approved_by=approval.approved_by,
        source_ids=source_ids,
        mapping_ids=tuple(mapping_ids),
        spec_id=spec_id,
        dataset_id=dataset_id,
        stack_receipts=tuple(stack_receipts),
        key_receipts=tuple(key_receipts),
    )


def approval_for(plan: BuildPlan, *, user_id: str, at: datetime) -> BuildApproval:
    """The approval a person gives by reading `plan.approval_sentence` and saying yes (for screens and tests)."""
    return BuildApproval(
        plan_hash=plan.plan_hash, approved_by=user_id, approved_at=at, sentence=plan.approval_sentence
    )
