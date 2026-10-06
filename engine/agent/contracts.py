"""Contracts for the use-case agents (Plan G): the session, its proposals, the recipe and its receipt.

These follow every rule `engine/contracts.py` sets - frozen, `extra="forbid"`, tuples rather than
lists, timezone-aware datetimes, one `Field(description=...)` per field - and live in their own
module because Plan G owns `engine/agent/**` (`PARALLEL_WORK_PROTOCOL.md` §3).

Four shapes are worth knowing before reading:

**A proposal is a suggestion, never a change.** Everything the helper wants to do - hide a column,
turn text into numbers, pick a setting, acknowledge a warning - is a `Proposal` in state `pending`.
Only the user moves it to `accepted` or `rejected`, and only `POST /agent-sessions/{id}/apply`
acts on accepted ones (DEC-1002).

**Every proposal points at evidence.** `evidence_ids` name `ToolResult`s in the same session. A
number the helper shows comes from one of them; a proposal without evidence is refused.

**A recipe is steps, not data.** `DataRecipe` lists stateless, row-wise `RecipeStep`s (DEC-1004).
Our code runs them on a copy; the helper never edits a file. `recipe_hash` covers the logic only -
kinds, columns and parameters - so the same fixes on next month's file hash the same.

**A receipt is what a recipe run did.** `RecipeReceipt` counts, per step, the rows it touched and the
values it could not convert, with masked examples, so a user sees exactly what changed.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Annotated, Final, Self

from pydantic import AwareDatetime, Field, JsonValue, model_validator

from engine.agent.config import AgentLevel, DataAccess
from engine.config import StrictBase
from engine.contracts import Artefact

__all__ = [
    "AGENT_SESSION_FILENAME",
    "DATA_RECIPE_FILENAME",
    "RECIPE_RECEIPT_FILENAME",
    "AgentConfidence",
    "AgentSession",
    "AgentSummary",
    "ChatMessage",
    "ChatRole",
    "DataRecipe",
    "DecidedBy",
    "Proposal",
    "ProposalKind",
    "ProposalState",
    "Question",
    "QuestionOption",
    "RecipeReceipt",
    "RecipeStep",
    "RecipeStepKind",
    "SentItem",
    "SessionStatus",
    "StepReceipt",
    "ToolResult",
    "TurnLog",
    "recipe_hash",
]

AGENT_SESSION_FILENAME: Final[str] = "agent_session.json"
"""Under `uploads/<upload_id>/agent/`; deleted with the upload (DEC-1007)."""
DATA_RECIPE_FILENAME: Final[str] = "data_recipe.json"
"""Beside a derived upload's source, and beside a model version's `schema.json` (DEC-1006)."""
RECIPE_RECEIPT_FILENAME: Final[str] = "recipe_receipt.json"
"""Beside a derived upload's source: what the recipe run did."""

_ID: Final[str] = r"^[a-z0-9][a-z0-9_-]{0,63}$"


class ProposalKind(StrEnum):
    """What a proposal would change."""

    ROLE = "role"  # which column is the key, the outcome, the date
    RECIPE_STEP = "recipe_step"  # a change to the data, run on a copy
    SETTING = "setting"  # an advanced-settings value
    ACKNOWLEDGEMENT = "acknowledgement"  # "this warning is expected"


class ProposalState(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class AgentConfidence(StrEnum):
    """How sure the helper is. `sure` proposals start ticked; `check` ones follow `tick_uncertain`."""

    SURE = "sure"
    CHECK = "check"


class DecidedBy(StrEnum):
    AGENT = "agent"
    USER = "user"


class RecipeStepKind(StrEnum):
    """The only things a recipe can do (Plan G §6.2). Each is stateless (DEC-1004).

    Every kind but one is row-wise. `combine_rows` (level 3, M76) is entity-wise: it turns the rows
    of one entity into one row, and its output for an entity depends only on that entity's own rows
    and the step's frozen parameters - never on another entity or a statistic of the whole file.
    """

    DROP_COLUMN = "drop_column"
    PARSE_NUMBER = "parse_number"
    PARSE_DATE = "parse_date"
    MAP_BOOLEAN = "map_boolean"
    NORMALISE_TEXT = "normalise_text"
    SET_MISSING = "set_missing"  # placeholder codes (99, -1, …) to an empty cell (DEC-1220)
    DERIVE = "derive"
    COMBINE_ROWS = "combine_rows"


class SessionStatus(StrEnum):
    """Where a Guided-setup session is. Only `apply` moves `ready` to `applied`."""

    ANALYSING = "analysing"
    NEEDS_REVIEW = "needs_review"  # proposals pending or blocking questions unanswered
    READY = "ready"  # everything decided; Approve is allowed
    APPLIED = "applied"  # the recipe ran and the run request was pre-filled
    STOPPED = "stopped"  # the data cannot work (too few rows, no outcome); the summary says why
    FAILED = "failed"


class ChatRole(StrEnum):
    USER = "user"
    AGENT = "agent"


class ToolResult(StrictBase):
    """One tool call and what it returned. The only source of a number the helper shows."""

    evidence_id: Annotated[str, Field(pattern=_ID, description="Stable id proposals cite.")]
    tool: str = Field(description="Registered tool name.")
    args: dict[str, JsonValue] = Field(default_factory=dict, description="Validated arguments.")
    supplied: dict[str, JsonValue] | None = Field(
        default=None,
        description="Only the arguments the model wrote, before defaults were filled in; null in older records.",
    )
    result: dict[str, JsonValue] = Field(
        default_factory=dict, description="JSON result; sample values are masked before storage."
    )
    created_at: AwareDatetime = Field(description="When the tool ran.")

    @property
    def typed_args(self) -> dict[str, JsonValue]:
        """What the model typed: a number in it never grounds a reply. Older records: every argument."""
        return self.args if self.supplied is None else self.supplied


class RecipeStep(StrictBase):
    """One change to the data, run by our code on a copy (Plan G §6)."""

    order: Annotated[
        int, Field(ge=1, description="1-based position; runs parse → set missing → combine → derive → drop.")
    ]
    kind: RecipeStepKind = Field(description="What the step does.")
    column: str = Field(description="The column it reads (and rewrites, unless `new_column` is set).")
    new_column: str | None = Field(default=None, description="Name of a new column; required for `derive`.")
    params: dict[str, JsonValue] = Field(default_factory=dict, description="Kind-specific parameters.")
    reason: str = Field(default="", description="Plain-language reason shown to the user.")
    proposal_id: str | None = Field(default=None, description="The proposal this step came from.")
    decided_by: DecidedBy = Field(default=DecidedBy.AGENT, description="Who proposed it.")

    @model_validator(mode="after")
    def _derive_names_a_column(self) -> Self:
        if self.kind is RecipeStepKind.DERIVE and not self.new_column:
            raise ValueError("a derive step must name new_column")
        if self.kind is RecipeStepKind.DROP_COLUMN and self.new_column is not None:
            raise ValueError("a drop_column step creates no column")
        if self.kind is RecipeStepKind.COMBINE_ROWS and self.new_column is not None:
            raise ValueError("a combine_rows step names its new columns in params.features")
        if self.kind is RecipeStepKind.SET_MISSING and self.new_column is not None:
            raise ValueError("a set_missing step empties cells of its own column")
        return self


def recipe_hash(steps: tuple[RecipeStep, ...]) -> str:
    """Hash of the recipe's logic: kinds, columns and parameters in order; reasons and ids excluded."""
    logic = [
        {
            "order": s.order,
            "kind": s.kind.value,
            "column": s.column,
            "new_column": s.new_column,
            "params": s.params,
        }
        for s in sorted(steps, key=lambda step: step.order)
    ]
    blob = json.dumps(logic, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


class DataRecipe(Artefact):
    """The approved steps for one use case's data. Replayed on every later file (DEC-1006)."""

    recipe_id: Annotated[str, Field(pattern=_ID, description="Id of this recipe version.")]
    use_case_id: str = Field(description="The use case it was approved for.")
    source_fingerprint: str = Field(description="Fingerprint hash of the upload it was approved on.")
    steps: tuple[RecipeStep, ...] = Field(description="Ordered steps.")
    recipe_hash: str = Field(description="`recipe_hash(steps)`; checked on load.")
    primary_key: str | None = Field(default=None, description="The ID column; no step may change it.")
    target: str | None = Field(default=None, description="The outcome column; no step may change or read it.")
    snapshot_column: str | None = Field(
        default=None,
        description="The column a derive step's `snapshot_date` means; null when the file has none.",
    )
    approved_by: str | None = Field(
        default=None, description="Principal id of the approver, when sign-in is on."
    )
    derived_from_recipe_id: str | None = Field(
        default=None, description="The recipe this one replaced, when a changed file forced a new version."
    )
    levels: tuple[AgentLevel, ...] | None = Field(
        default=None,
        description=(
            "The use case's `agent.levels` when the recipe was approved; every replay runs under them. "
            "Null on a recipe saved before they were recorded: the use case's current levels apply."
        ),
    )
    max_failure_pct: float | None = Field(
        default=None,
        ge=0.0,
        le=100.0,
        description=(
            "The use case's `agent.max_conversion_failure_pct` when the recipe was approved; every "
            "replay uses it. Null on a recipe saved before it was recorded: the current limit applies."
        ),
    )
    created_at: AwareDatetime = Field(description="When it was approved.")

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        orders = [step.order for step in self.steps]
        if orders != list(range(1, len(orders) + 1)):
            raise ValueError("steps must be numbered 1..n in order")
        if self.recipe_hash != recipe_hash(self.steps):
            raise ValueError("recipe_hash does not match the steps")
        return self

    def prepares_like(self, other: DataRecipe) -> bool:
        """True when the two recipes turn the same file into the same prepared file.

        `recipe_hash` covers the steps only, so that the same fixes hash the same on next month's
        file. What a replay also depends on - the ID and outcome columns the steps are checked
        against, the snapshot column a derive step reads, and the levels and failure limit it runs
        under - is compared here, so an upload prepared under one of them is never reused for
        another (Plan G review).
        """
        return (
            self.recipe_hash,
            self.primary_key,
            self.target,
            self.snapshot_column,
            self.levels,
            self.max_failure_pct,
        ) == (
            other.recipe_hash,
            other.primary_key,
            other.target,
            other.snapshot_column,
            other.levels,
            other.max_failure_pct,
        )


class StepReceipt(StrictBase):
    """What one step did on one file."""

    order: int = Field(description="The step's position.")
    kind: RecipeStepKind = Field(description="The step's kind.")
    column: str = Field(description="The column it read.")
    rows: Annotated[int, Field(ge=0, description="Rows the step looked at.")]
    changed: Annotated[int, Field(ge=0, description="Values it changed.")]
    failed: Annotated[int, Field(ge=0, description="Non-empty values it could not convert.")]
    examples_failed: tuple[str, ...] = Field(
        default=(), description="Up to five masked examples of failures."
    )
    leak_check: str | None = Field(
        default=None,
        description=(
            "`combine_rows` only: which future-data check ran on the combined rows and what it found; "
            "null for every other step and on a preview."
        ),
    )
    skipped: bool = Field(
        default=False,
        description=(
            "`drop_column` only: true when the file did not have the column, so there was nothing to "
            "hide - a column hidden at setup need not be in a later scoring file."
        ),
    )


class RecipeReceipt(Artefact):
    """What a recipe run did: the receipt the user reads after Approve and after each scoring file."""

    recipe_hash: str = Field(description="Hash of the recipe that ran.")
    upload_id: str = Field(description="The upload it read (never changed).")
    derived_upload_id: str | None = Field(default=None, description="The upload it wrote; null on a preview.")
    rows_in: Annotated[int, Field(ge=0, description="Rows read.")]
    rows_out: Annotated[int, Field(ge=0, description="Rows written.")]
    columns_in: Annotated[int, Field(ge=0, description="Columns read.")]
    columns_out: Annotated[int, Field(ge=0, description="Columns written.")]
    steps: tuple[StepReceipt, ...] = Field(description="One entry per step, in order.")
    created_at: AwareDatetime = Field(description="When it ran.")


class Proposal(StrictBase):
    """One suggestion the user can accept or reject (Plan G §5.2)."""

    proposal_id: Annotated[str, Field(pattern=_ID, description="Stable within the session.")]
    kind: ProposalKind = Field(description="What it would change.")
    title: str = Field(description="What, in one plain sentence.")
    reason: str = Field(description="Why, in one plain sentence.")
    path: str | None = Field(
        default=None,
        description="For `setting`, `role` and `acknowledgement`: the run-override path it sets.",
    )
    value: JsonValue = Field(default=None, description="The value that will be used if accepted.")
    suggested_value: JsonValue = Field(
        default=None, description="What the helper suggested; differs from `value` after an edit."
    )
    step: RecipeStep | None = Field(default=None, description="For `recipe_step`: the step it adds.")
    evidence_ids: tuple[str, ...] = Field(description="Tool results it rests on; never empty.")
    examples: tuple[str, ...] = Field(
        default=(),
        description=(
            "The cell values `title` and `reason` quote, as written there (already masked for personal data). "
            "The screen shows them; the chat's prompt gets each one as its shape or a hidden marker, "
            "never as written. Empty for a suggestion that quotes no value."
        ),
    )
    confidence: AgentConfidence = Field(description="`sure` or `check`.")
    state: ProposalState = Field(default=ProposalState.PENDING, description="Only the user changes it.")
    decided_by: DecidedBy | None = Field(default=None, description="Who set the state; null while pending.")

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if not self.evidence_ids:
            raise ValueError("a proposal must cite at least one tool result")
        if self.kind is ProposalKind.RECIPE_STEP and self.step is None:
            raise ValueError("a recipe_step proposal carries its step")
        if self.kind is not ProposalKind.RECIPE_STEP and self.step is not None:
            raise ValueError(f"a {self.kind.value} proposal carries no recipe step")
        if self.kind is not ProposalKind.RECIPE_STEP and not self.path:
            raise ValueError(f"a {self.kind.value} proposal names the path it sets")
        if (self.state is ProposalState.PENDING) != (self.decided_by is None):
            raise ValueError("decided_by is set exactly when the proposal is no longer pending")
        return self


class QuestionOption(StrictBase):
    option_id: Annotated[str, Field(pattern=_ID, description="Stable within the question.")]
    label: str = Field(description="Button text.")
    effect: str = Field(default="", description="What choosing it does, in plain words.")
    proposal: Proposal | None = Field(
        default=None,
        description="The proposal choosing it adds, accepted by the user; null when it adds none.",
    )


class Question(StrictBase):
    """Something the helper cannot decide alone (Plan G §5.3)."""

    question_id: Annotated[str, Field(pattern=_ID, description="Stable within the session.")]
    text: str = Field(description="The question, in plain words.")
    options: tuple[QuestionOption, ...] = Field(description="At least two choices.")
    blocking: bool = Field(
        default=True, description="Approve is refused until a blocking question is answered."
    )
    evidence_ids: tuple[str, ...] = Field(default=(), description="Tool results it rests on.")
    examples: tuple[str, ...] = Field(
        default=(),
        description="The cell values `text` quotes, as written there; the chat's prompt never gets them as written.",
    )
    answer: str | None = Field(default=None, description="The chosen option_id.")

    @model_validator(mode="after")
    def _shape(self) -> Self:
        ids = [option.option_id for option in self.options]
        if len(ids) < 2:
            raise ValueError("a question offers at least two options")
        if len(set(ids)) != len(ids):
            raise ValueError("option ids repeat")
        if self.answer is not None and self.answer not in ids:
            raise ValueError(f"answer {self.answer!r} is not one of the options")
        return self


class TurnLog(StrictBase):
    """What one chat turn did, without its content: for audit and for tuning the helper."""

    llm_calls: Annotated[int, Field(ge=0, description="Model calls the turn made.")]
    tools: tuple[str, ...] = Field(default=(), description="Actions the model took, in order.")
    error_codes: tuple[str, ...] = Field(
        default=(), description="Codes of actions the engine refused, in order."
    )
    blocked_by: str | None = Field(
        default=None,
        description="Why the model's reply was replaced by a plain sentence (a rule, `budget`, …); null when kept.",
    )


class SentItem(StrictBase):
    """One tool result as it was put in a prompt to the AI service: what left, not what was found.

    `preview` is the masked payload (`engine.agent.egress.prepare`), cut to 1,500 characters;
    `chars` is the length of the whole payload before that cut. A turn records at most 12 items and
    a session file keeps at most 20 KB of previews (older ones are replaced by a short note).
    """

    tool: str = Field(description="The tool that produced the result (`propose_setting` for a suggestion).")
    args: dict[str, JsonValue] = Field(
        default_factory=dict, description="The arguments the model gave, masked like the payload."
    )
    preview: str = Field(description="The masked payload sent to the AI service, at most 1,500 characters.")
    chars: Annotated[int, Field(ge=0, description="Characters in the whole payload, before the cut.")]
    mode: DataAccess = Field(description="`masked_data` or `summaries_only`: how the payload was made.")


class ChatMessage(StrictBase):
    role: ChatRole = Field(description="Who wrote it.")
    text: str = Field(
        description="The message; a person's is masked, the helper's is checked by the guardrails before storage."
    )
    evidence_ids: tuple[str, ...] = Field(default=(), description="Tool results the reply used.")
    turn: TurnLog | None = Field(default=None, description="The helper's replies only: what the turn did.")
    sent: tuple[SentItem, ...] = Field(
        default=(),
        description="The helper's replies only: exactly which tool results were put in this turn's prompts.",
    )
    created_at: AwareDatetime = Field(description="When it was written.")


class AgentSummary(StrictBase):
    """The four lists the user reads before Approve (Plan G §5.1, step 4)."""

    decisions: tuple[str, ...] = Field(default=(), description="What will change, one line each.")
    assumptions: tuple[str, ...] = Field(default=(), description="What the helper took as given.")
    intended_actions: tuple[str, ...] = Field(default=(), description="What happens when Run is clicked.")
    hidden_columns: tuple[str, ...] = Field(
        default=(), description="Columns the model will not see, with why."
    )


class AgentSession(Artefact):
    """One Guided-setup conversation about one upload (DEC-1007)."""

    session_id: Annotated[str, Field(pattern=_ID, description="Session id.")]
    upload_id: str = Field(description="The upload it is about; never changed.")
    use_case_id: str = Field(description="The use case whose helper runs it.")
    mode: str = Field(description="`train` or `score`.")
    model_version_id: str | None = Field(
        default=None,
        description="Scoring only: the model version the file is checked against; null = the one in use.",
    )
    agent_name: str = Field(description="The helper's display name.")
    status: SessionStatus = Field(description="Where the session is.")
    proposals: tuple[Proposal, ...] = Field(default=(), description="Every suggestion, in display order.")
    questions: tuple[Question, ...] = Field(default=(), description="Every question, in display order.")
    tool_results: tuple[ToolResult, ...] = Field(
        default=(), description="Evidence, in the order it was gathered."
    )
    transcript: tuple[ChatMessage, ...] = Field(default=(), description="The chat, oldest first.")
    summary: AgentSummary = Field(default_factory=AgentSummary, description="The pre-Approve summary.")
    stop_reason: str | None = Field(
        default=None,
        description="Why the data cannot work as it is, in plain words; set when status is `stopped`.",
    )
    assumptions: tuple[str, ...] = Field(default=(), description="What the advisor took as given.")
    engine_hidden: tuple[str, ...] = Field(
        default=(), description="Columns the engine will leave out by itself, with why (one line each)."
    )
    rounds: int = Field(default=1, ge=1, description="How many times the advisor has looked at the file.")
    llm_calls: int = Field(default=0, ge=0, description="Model calls the chat has made in this session.")
    applied_upload_id: str | None = Field(default=None, description="The derived upload `apply` wrote.")
    explore: bool = Field(
        default=False,
        description="An *Ask your data* session (Plan I, DEC-1250): read-only questions about the file, outside "
        "Guided setup. It has no suggestions and no questions, and the helper cannot suggest a setting.",
    )
    created_at: AwareDatetime = Field(description="When the session started.")
    updated_at: AwareDatetime = Field(description="When it last changed.")

    @model_validator(mode="after")
    def _references(self) -> Self:
        evidence = {result.evidence_id for result in self.tool_results}
        if len(evidence) != len(self.tool_results):
            raise ValueError("evidence ids repeat")
        for proposal in self.proposals:
            missing = [e for e in proposal.evidence_ids if e not in evidence]
            if missing:
                raise ValueError(f"proposal {proposal.proposal_id} cites unknown evidence {missing}")
        for question in self.questions:
            cited = list(question.evidence_ids)
            for option in question.options:
                if option.proposal is not None:
                    cited += option.proposal.evidence_ids
            missing = [e for e in cited if e not in evidence]
            if missing:
                raise ValueError(f"question {question.question_id} cites unknown evidence {missing}")
        ids = [p.proposal_id for p in self.proposals]
        if len(set(ids)) != len(ids):
            raise ValueError("proposal ids repeat")
        qids = [q.question_id for q in self.questions]
        if len(set(qids)) != len(qids):
            raise ValueError("question ids repeat")
        if self.status is SessionStatus.APPLIED and self.applied_upload_id is None:
            raise ValueError("an applied session names the upload it wrote")
        if (self.status is SessionStatus.STOPPED) != (self.stop_reason is not None):
            raise ValueError("stop_reason is set exactly when the session is stopped")
        if self.explore and (self.proposals or self.questions or self.status is not SessionStatus.READY):
            raise ValueError("an Ask-your-data session is ready and holds no suggestion or question")
        return self

    @property
    def undecided(self) -> tuple[str, ...]:
        """Ids of pending proposals and unanswered blocking questions: what stops Approve."""
        pending = tuple(p.proposal_id for p in self.proposals if p.state is ProposalState.PENDING)
        open_questions = tuple(q.question_id for q in self.questions if q.blocking and q.answer is None)
        return pending + open_questions
