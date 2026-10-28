"""Plan G routes: the dry-run data checks (M71) and the Guided-setup session (M74, DEC-1018).

`POST /uploads/{upload_id}/checks` answers "what would the Run button say about this file, with
this ID column, outcome and these settings?" without creating a run or writing anything. It
follows `POST /runs` step for step - the same config resolution, the same role rule on overrides,
the same mode check, the same model-version resolver for a scoring file, and the same validators
through `engine.agent.checks.check_plan` - so the two cannot disagree (Plan G §9, M71).
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import Field, JsonValue

from api.access import current_principal, set_audit_context
from api.access_policy import RoutePolicy, register
from api.deps import ConfigRootDep, StorageDep, get_registry, get_settings
from api.routes.agent_recipes import (
    load_receipt,
    load_recipe,
    model_recipe,
    prepare_in_memory,
    recipe_failure_report,
    refuse_other_roles,
    replay_for_scoring,
    run_saved_recipe,
    scoring_source,
    write_derived_upload,
)
from api.routes.agent_renames import load_changes, memory_changes, model_changes, save_changes
from api.routes.ai_service import resolve_slot
from api.routes.runs import read_frame, requested_by, score_version, validation_conflict
from api.routes.uploads import http_error, load_upload, load_upload_profile, profile_row_cap, use_case_config
from api.schemas import ErrorResponse, UploadRecord, ValidationErrorResponse
from engine.access.roles import Role
from engine.agent.advisor import ROLE_PRIMARY_KEY, recipe_steps, run_overrides
from engine.agent.assist import assist_session
from engine.agent.checks import check_plan
from engine.agent.config import DataAccess
from engine.agent.contracts import (
    ROW_RESHAPE_STEP_KINDS,
    ROWS_PER_ID_STEP_KINDS,
    AgentSession,
    AgentSummary,
    ChatMessage,
    ChatRole,
    DataRecipe,
    ProposalSource,
    ProposalState,
    RecipeReceipt,
    RecipeStepKind,
    SessionStatus,
    recipe_hash,
)
from engine.agent.edits import TABLE_MAX_ROWS, add_edit, table_rows
from engine.agent.egress import cap_sent, is_third_party
from engine.agent.explore import EXPLORE_SESSION_FILENAME, RateChart, charts_of, start_explore
from engine.agent.loop import chat_turn
from engine.agent.recipe import RecipeError, run_recipe
from engine.agent.scope import agent_available
from engine.agent.session import (
    SessionError,
    accept_recommended,
    answer,
    decide,
    roles_of,
    start_session,
    unsettled_roles,
    with_summary,
)
from engine.agent.tools import AgentContext, AgentToolError
from engine.ai_consent import client_scope
from engine.ai_service import effective_service
from engine.config import PrimaryKey, RunMode, StrictBase, UseCaseConfig, resolve_config, sole_key
from engine.contracts import FeatureSchema, ModelVersion, ValidationReport
from engine.doctor.memory import (
    TreatmentOffer,
    offer_for,
    recall,
    remember,
    remembered_recipe,
    use_same_treatment,
    used_in,
)
from engine.doctor.preview import masked_rows
from engine.doctor.renames import FileChanges
from engine.generative.budget import Meter
from engine.generative.contracts import LlmUsageReport
from engine.generative.guardrails import Guardrails, load_policy
from engine.intake.contracts import IntakeReport
from engine.pii import redact_text
from engine.stages import ingest
from engine.storage import Storage, StorageError, upload_key
from engine.utils.ids import new_upload_id
from engine.utils.logging import get_logger
from engine.utils.time import utc_now

__all__ = [
    "AgentSessionResponse",
    "ApplyResponse",
    "AskResponse",
    "ChecksRequest",
    "PreviewResponse",
    "router",
]

router = APIRouter(tags=["agent"])
_LOGGER = get_logger(__name__)

register(
    {
        # Analyst: a dry run resolves overrides exactly as starting a run does (DEC-1011).
        ("POST", "/uploads/{upload_id}/checks"): RoutePolicy(
            role=Role.ANALYST,
            action="uploads.checks",
            purpose="check an upload",
            object_type="upload",
            object_param="upload_id",
        ),
    }
)


class ChecksRequest(StrictBase):
    """The roles and settings to check an upload against; the same fields `POST /runs` takes."""

    use_case: str = Field(description="Use-case id.")
    primary_key: PrimaryKey | None = Field(default=None, description="The ID column.")
    target: str | None = Field(default=None, description="The outcome column; training files only.")
    model_version_id: str | None = Field(
        default=None,
        description="Scoring files only: the model version to check against; default the one in use.",
    )
    overrides: dict[str, object] = Field(
        default_factory=dict, description="Run overrides, as for `POST /runs`."
    )


@router.post(
    "/uploads/{upload_id}/checks",
    response_model=ValidationReport,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
    summary="Run the data checks for an upload without starting a run",
)
def check_upload(
    upload_id: str,
    body: ChecksRequest,
    root: ConfigRootDep,
    storage: StorageDep,
    request: Request,
) -> ValidationReport:
    """The report `POST /runs` would give, with 200; `passed` says whether Run would start.

    Nothing is written: not a run directory, and not the upload's `validation.json`, which stays the
    record of the last real Run. The registry is opened only for a scoring file, because opening it
    creates the local registry database. What `POST /runs` refuses before its checks is refused here
    with the same status and code: a setting the caller's role may not loosen, and an ID column or
    outcome other than the ones a prepared file's recipe was checked against (409
    `RECIPE_ROLES_MISMATCH`, DEC-1011).
    """
    use_case_config(body.use_case, root)  # 404 for a planned or unknown id, before anything is read
    resolved = resolve_config(body.use_case, body.overrides, root=root)
    config = resolved.config
    from api.access import require_roles_for_run_overrides  # as in POST /runs: avoids an import cycle

    require_roles_for_run_overrides(request, use_case=use_case_config(body.use_case, root), resolved=config)
    upload = load_upload(storage, upload_id)
    profile = load_upload_profile(storage, upload_id)
    primary_key = sole_key(body.primary_key, what="A check") if body.primary_key is not None else None
    schema: FeatureSchema | None = None
    row_count = profile.row_count
    if upload.mode is RunMode.SCORE:
        version = score_version(get_registry(request), config=config, version_id=body.model_version_id)
        schema = storage.read_model(version.schema_key, FeatureSchema)
        recipe = model_recipe(storage, version)
        if primary_key is not None:  # `POST /runs` needs an ID column before it gets this far
            refuse_other_roles(recipe, primary_key=primary_key, target=None, training=False)
        # As `POST /runs` does (DEC-1006): the model's recipe prepares the file before it is checked
        # against the model's columns - here in memory, since a dry run writes nothing.
        prepared = prepare_in_memory(storage, config, upload, recipe, version)
        if isinstance(prepared, ValidationReport):
            return prepared
        frame, row_count = prepared
    elif body.model_version_id is not None:
        raise http_error(
            409, "UPLOAD_MODE_MISMATCH", "A model version is only used to check a file uploaded for scoring."
        )
    else:
        if primary_key is not None:  # the roles a prepared upload was checked against (DEC-1004)
            refuse_other_roles(
                load_recipe(storage, upload_id), primary_key=primary_key, target=body.target, training=True
            )
        frame = read_frame(storage, upload.source_key, upload.file_format, profile_row_cap(config))
    return check_plan(
        frame,
        config,
        mode=upload.mode,
        primary_key=primary_key,
        target=body.target,
        upload_id=upload_id,
        row_count=row_count,
        schema=schema,
    )


# ---------------------------------------------------------------------------
# M74: the Guided-setup session (Plan G §9, DEC-1018)
# ---------------------------------------------------------------------------
SESSION_FILENAME: Final[str] = "agent/agent_session.json"
LLM_USAGE_FILENAME: Final[str] = "agent/llm_usage.json"
PREVIEW_ROWS: Final[int] = 5
PREVIEW_SAMPLE_ROWS: Final[int] = 1_000
"""The preview runs the accepted steps on the first 1,000 rows, or the rows of the first 1,000
entities when rows are combined (Plan G §6.3), not on every row."""
CHAT_GRACE_TURNS: Final[int] = 10
"""Turns a session may still take once its model-call budget is spent (each answers "out of budget").

A turn that reaches the model costs at least one call, so `2 * (max_llm_calls_per_session +
CHAT_GRACE_TURNS)` messages is a hard ceiling on a transcript that would otherwise grow with every
request (M77 hardening)."""

_SESSION_LOCKS_GUARD: Final[threading.Lock] = threading.Lock()
_SESSION_LOCKS: dict[str, threading.Lock] = {}


@contextmanager
def _session_lock(upload_id: str) -> Iterator[None]:
    """Serialise every read-modify-write of one upload's session file within this process.

    Without it two requests read the same session, each change it, and the second write silently
    drops the first's change - two chat turns at once could each spend the whole model-call budget
    and record only one of them (M77 hardening). Sync routes run on a thread pool, so this is a
    thread lock; one lock per upload keeps different uploads independent. A deployment with several
    API processes needs the session store's own locking, which the local store does not have.
    """
    with _SESSION_LOCKS_GUARD:
        lock = _SESSION_LOCKS.setdefault(upload_id, threading.Lock())
    with lock:
        yield


register(
    {
        ("POST", "/uploads/{upload_id}/agent-session"): RoutePolicy(
            role=Role.ANALYST,
            action="agent.start",
            purpose="start Guided setup",
            object_type="upload",
            object_param="upload_id",
        ),
        ("GET", "/uploads/{upload_id}/agent-session"): RoutePolicy(
            role=Role.VIEWER,
            action="agent.read",
            purpose="see Guided setup",
            object_type="upload",
            object_param="upload_id",
        ),
        ("POST", "/uploads/{upload_id}/agent-session/decisions"): RoutePolicy(
            role=Role.ANALYST,
            action="agent.decide",
            purpose="accept or reject a suggestion",
            object_type="upload",
            object_param="upload_id",
        ),
        ("POST", "/uploads/{upload_id}/agent-session/answers"): RoutePolicy(
            role=Role.ANALYST,
            action="agent.answer",
            purpose="answer the helper",
            object_type="upload",
            object_param="upload_id",
        ),
        ("POST", "/uploads/{upload_id}/agent-session/messages"): RoutePolicy(
            role=Role.ANALYST,
            action="agent.chat",
            purpose="ask the helper",
            object_type="upload",
            object_param="upload_id",
        ),
        ("POST", "/uploads/{upload_id}/agent-session/assist"): RoutePolicy(
            role=Role.ANALYST,
            action="agent.assist",
            purpose="ask the AI service for a second opinion on the columns",
            object_type="upload",
            object_param="upload_id",
        ),
        ("POST", "/uploads/{upload_id}/agent-session/preview"): RoutePolicy(
            role=Role.ANALYST,
            action="agent.preview",
            purpose="preview the prepared data",
            object_type="upload",
            object_param="upload_id",
        ),
        ("POST", "/uploads/{upload_id}/agent-session/apply"): RoutePolicy(
            role=Role.ANALYST,
            action="agent.apply",
            purpose="approve Guided setup",
            object_type="upload",
            object_param="upload_id",
        ),
        # Plan L M154 (DEC-1541): editing by hand, and the table view it is made in.
        ("POST", "/uploads/{upload_id}/agent-session/edits"): RoutePolicy(
            role=Role.ANALYST,
            action="agent.edit",
            purpose="edit the data by hand",
            object_type="upload",
            object_param="upload_id",
        ),
        ("POST", "/uploads/{upload_id}/agent-session/rows"): RoutePolicy(
            role=Role.ANALYST,
            action="agent.rows",
            purpose="see the rows of the file being set up",
            object_type="upload",
            object_param="upload_id",
        ),
        # Plan L M159 (DEC-1594): tick the steps that treated a file of the same shape last time.
        ("POST", "/uploads/{upload_id}/agent-session/same-treatment"): RoutePolicy(
            role=Role.ANALYST,
            action="agent.same_treatment",
            purpose="use the same treatment as last time",
            object_type="upload",
            object_param="upload_id",
        ),
    }
)


class EditRequest(StrictBase):
    """Body of `POST /uploads/{id}/agent-session/edits`: one edit made by hand (Plan L M154)."""

    kind: RecipeStepKind = Field(description="What the edit does: one of the step kinds a person may use.")
    column: str = Field(min_length=1, max_length=512, description="The column it changes.")
    params: dict[str, Any] = Field(default_factory=dict, description="The step's parameters.")
    new_column: str | None = Field(default=None, max_length=128, description="For a split or a rename.")


class RowsRequest(StrictBase):
    """Body of `POST /uploads/{id}/agent-session/rows`."""

    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=100)
    where_column: str | None = Field(default=None, max_length=512, description="Only rows where this column…")
    equals: str | None = Field(default=None, max_length=200, description="…equals this value.")


class RowsResponse(StrictBase):
    """Body of `GET /uploads/{id}/agent-session/rows`: a page of the file for the table view."""

    columns: tuple[str, ...]
    personal: tuple[str, ...] = Field(description="Columns whose values are hidden.")
    rows: tuple[tuple[str | None, ...], ...]
    row_numbers: tuple[int, ...] = Field(description="Each row's line in the file, the header being line 1.")
    key_column: str | None = Field(default=None, description="The ID column a correction finds its row by.")
    offset: int
    total: int = Field(description="Rows that match (all rows when nothing is searched).")


class ChatAvailability(StrictBase):
    available: bool = Field(
        default=True,
        description="False when no AI service is connected for Product AI: the suggestions and questions "
        "still work, only the chat box does not.",
    )
    reason: str | None = Field(
        default=None, description="Why it is not available: `AI_NOT_CONNECTED`. Null when it is."
    )
    backend: str = Field(
        description="Which AI service answers: the provider's id (`bedrock`, `openai`, `anthropic`, "
        "`openrouter`, `huggingface`, `openai_compatible`), `fake` in tests, or `none` when not connected."
    )
    provider_label: str | None = Field(default=None, description="The provider's name, as a person reads it.")
    generation_model_id: str | None = Field(
        default=None, description="The model that writes replies, when known."
    )
    data_access: DataAccess = Field(
        default=DataAccess.MASKED_DATA,
        description="What the chat model sees of the file's cells: `masked_data` (masked values) or `summaries_only` (their shape only).",
    )
    third_party: bool = Field(
        default=False,
        description="True when the chat model runs outside the platform's own account (not `bedrock`, not the test model).",
    )


class AgentSessionResponse(StrictBase):
    session: AgentSession
    chat: ChatAvailability
    # Plan L M159 (DEC-1594): a file of a shape treated before is offered that treatment (never applied).
    treatment: TreatmentOffer | None = Field(
        default=None,
        description="The treatment of a file of the same shape, offered until it is used; null when there is none.",
    )
    # Plan L #16 (DEC-1597): a column renamed since the saved steps were approved, and new spellings (shown only).
    changes: FileChanges | None = Field(
        default=None,
        description="What changed in this file against the file the saved steps were approved on: renames to "
        "tick, columns no new one clearly matches, new spellings; null when nothing did.",
    )


class SameTreatmentRequest(StrictBase):
    """Body of `POST /uploads/{id}/agent-session/same-treatment`: the offer the person accepted."""

    recipe_id: str = Field(
        min_length=1, max_length=64, description="`treatment.recipe_id` of the offer shown."
    )


class SessionStartRequest(StrictBase):
    use_case: str = Field(description="Use-case id whose helper runs the session.")
    model_version_id: str | None = Field(
        default=None, description="Scoring files only: the model Run will use; default the one in use."
    )


class Decision(StrictBase):
    proposal_id: str
    state: ProposalState
    value: JsonValue = Field(default=None, description="A setting's new value, when the person changed it.")


class DecisionsRequest(StrictBase):
    decisions: tuple[Decision, ...] = Field(default=(), description="Per-suggestion decisions.")
    accept_recommended: bool = Field(
        default=False, description="Also accept every pending suggestion the helper is sure of."
    )


class AnswerRequest(StrictBase):
    question_id: str
    option_id: str


class MessageRequest(StrictBase):
    text: str = Field(min_length=1, max_length=1_000)


class PreviewResponse(StrictBase):
    columns_before: tuple[str, ...]
    rows_before: tuple[tuple[str, ...], ...]
    columns_after: tuple[str, ...]
    rows_after: tuple[tuple[str, ...], ...]
    receipt: RecipeReceipt | None = Field(
        description="What each step did on the preview rows; null with no steps."
    )


class ApplyResponse(StrictBase):
    """What the Setup screen's Run needs: the upload to run on, the roles and the overrides."""

    upload_id: str = Field(description="The prepared upload, or the original when nothing needed preparing.")
    mode: RunMode
    primary_key: str | None
    target: str | None
    overrides: dict[str, JsonValue]
    summary: AgentSummary
    receipt: RecipeReceipt | None = Field(description="What the recipe did to every row; null with no steps.")


@dataclass(frozen=True)
class _Loaded:
    ctx: AgentContext
    upload: UploadRecord
    recipe_error: RecipeError | None
    version: ModelVersion | None = None
    """Scoring files only: the model version a scoring run would use."""
    model_has_recipe: bool = False
    """Scoring files only: whether that model prepares its data in saved steps."""


def _session_key(upload_id: str) -> str:
    return upload_key(upload_id, SESSION_FILENAME)


def _refuse(status: int, code: str, message: str) -> HTTPException:
    return http_error(status, code, message)


def _load_context(
    storage: Storage,
    root: Path,
    request: Request,
    upload_id: str,
    use_case_id: str,
    model_version_id: str | None = None,
) -> _Loaded:
    config = use_case_config(use_case_id, root)
    if not agent_available(config):
        raise _refuse(409, "AGENT_NOT_AVAILABLE", f"{config.name} has no Guided setup; use Manual setup.")
    upload = load_upload(storage, upload_id)
    if upload.use_case_id != config.id:
        raise _refuse(409, "AGENT_USE_CASE_MISMATCH", "This file was uploaded for another use case.")
    if upload.mode is RunMode.TRAIN and load_recipe(storage, upload_id) is not None:
        # A session saves only the steps it accepts; on a prepared file the model's recipe would
        # leave out every step that prepared it, and scoring would replay only part (DEC-1006).
        raise _refuse(
            409,
            "AGENT_UPLOAD_PREPARED",
            "This file was already prepared by Guided setup. Start Guided setup on the file you sent.",
        )
    source = upload
    schema: FeatureSchema | None = None
    recipe_error: RecipeError | None = None
    version: ModelVersion | None = None
    recipe: DataRecipe | None = None
    model_has_recipe = False
    if upload.mode is RunMode.SCORE:
        version = score_version(get_registry(request), config=config, version_id=model_version_id)
        schema = storage.read_model(version.schema_key, FeatureSchema)
        recipe = model_recipe(storage, version)
        model_has_recipe = recipe is not None
        # The file Run would prepare (or read as it is): never the model's recipe on top of its own
        # output, nor on another recipe's output (`scoring_source`, as `POST /runs`).
        chosen = scoring_source(storage, upload, recipe)
        if isinstance(chosen, ValidationReport):
            check = chosen.checks[0]
            recipe_error = RecipeError(check.code, check.message, column=check.column)
            recipe = None
        else:
            source = chosen.upload
            if chosen.ready:
                recipe = None
    profile = load_upload_profile(storage, source.upload_id)
    frame = read_frame(storage, source.source_key, source.file_format, profile_row_cap(config))
    if recipe is not None:
        try:
            frame = run_saved_recipe(frame, recipe, config, upload_id=upload_id).frame
        except RecipeError as exc:
            recipe_error = exc
        else:
            if any(step.kind in ROW_RESHAPE_STEP_KINDS for step in recipe.steps):
                # Reshaped rows are other rows: the profile the helper reads must describe them.
                profile = ingest.profile_dataset(
                    frame,
                    config,
                    upload_id=upload_id,
                    file_name=profile.file_name,
                    file_format=profile.file_format,
                    file_size_bytes=profile.file_size_bytes,
                    delimiter=profile.delimiter,
                    encoding=profile.encoding,
                    row_count=len(frame),
                )
    ctx = AgentContext(
        use_case_id=config.id,
        config=config,
        config_root=root,
        upload_id=upload_id,
        mode=upload.mode,
        profile=profile,
        frame=frame,
        schema=schema,
        # Plan O M244 (DEC-1814): the training budget is worked out on this deployment's past runs, and the
        # choices offered are kept beside the file for the run's promise. Read only while the switch is on.
        run_history=storage,
    )
    return _Loaded(
        ctx=ctx, upload=upload, recipe_error=recipe_error, version=version, model_has_recipe=model_has_recipe
    )


def _load_session(storage: Storage, upload_id: str) -> AgentSession:
    """The upload's session, or 404 - also for an id the store refuses as a key (`..`, a backslash),
    which used to escape as a 500 (M77)."""
    missing = _refuse(404, "AGENT_SESSION_NOT_FOUND", "Guided setup has not been started for this file.")
    try:
        key = _session_key(upload_id)
        if not storage.exists(key):
            raise missing
        return storage.read_model(key, AgentSession)
    except StorageError as exc:
        raise missing from exc


def _save(storage: Storage, session: AgentSession) -> AgentSession:
    storage.write_model(_session_key(session.upload_id), session)
    return session


def _response(
    session: AgentSession, config: UseCaseConfig, storage: Storage, request: Request
) -> AgentSessionResponse:
    """The session and what the chat box may do: the Product AI's effective provider decides
    `chat` (not the use case's `generative.llm.backend`), and with none connected the rules-based
    suggestions still work while the chat is `available: false` (DEC-1140)."""
    effective = effective_service(
        config.generative.llm, slot="product", storage=storage, settings=get_settings(request)
    )
    if effective is None:
        chat = ChatAvailability(
            available=False,
            reason="AI_NOT_CONNECTED",
            backend="none",
            data_access=config.agent.ai_data_access,
            third_party=False,
        )
    else:
        chat = ChatAvailability(
            backend=effective.provider,
            provider_label=effective.label,
            generation_model_id=effective.llm.generation_model_id or None,
            data_access=config.agent.ai_data_access,
            third_party=is_third_party(effective.llm, effective.provider),
        )
    return AgentSessionResponse(
        session=session,
        chat=chat,
        treatment=_treatment(storage, session),
        changes=(
            None
            if session.status is SessionStatus.APPLIED or session.explore
            else load_changes(storage, session.upload_id)
        ),
    )


def _find_changes(storage: Storage, loaded: _Loaded) -> None:
    """Plan L #16 (DEC-1597): what changed in this file against the one the saved steps were approved on,
    kept beside the session. A courtesy: it never fails a start."""
    ctx, upload = loaded.ctx, loaded.upload
    try:
        changes = None
        if ctx.mode is RunMode.SCORE and loaded.version is not None:
            recipe = model_recipe(storage, loaded.version)
            chosen = scoring_source(storage, upload, recipe)
            if not isinstance(chosen, ValidationReport) and not (chosen.ready and recipe is not None):
                changes = model_changes(storage, ctx.config, loaded.version, chosen.upload, recipe)
        elif ctx.mode is RunMode.TRAIN:
            intake = (
                storage.read_model(upload.intake_key, IntakeReport)
                if upload.intake_key is not None and storage.exists(upload.intake_key)
                else None
            )
            changes = memory_changes(storage, ctx.config, upload, ctx.profile, intake, ctx.frame)
        save_changes(storage, upload.upload_id, changes)
    except Exception as exc:
        _LOGGER.warning("doctor.renames.find_failed upload=%s error=%s", upload.upload_id, type(exc).__name__)


def _treatment(storage: Storage, session: AgentSession, *, unused: bool = True) -> TreatmentOffer | None:
    """The remembered treatment of a file of this session's shape, or None (Plan L M159)."""
    if session.status is SessionStatus.APPLIED or session.explore:
        return None
    try:
        upload = load_upload(storage, session.upload_id)
        profile = load_upload_profile(storage, session.upload_id)
        intake = (
            storage.read_model(upload.intake_key, IntakeReport)
            if upload.intake_key is not None and storage.exists(upload.intake_key)
            else None
        )
    except (HTTPException, StorageError):
        return None
    offer = offer_for(
        storage,
        use_case_id=session.use_case_id,
        upload_id=session.upload_id,
        columns=profile.fingerprint.columns,
        intake=intake,
        mode=session.mode,
    )
    if offer is not None and unused and used_in(session, offer.recipe_id):
        return None
    return offer


def _session_error(exc: SessionError) -> HTTPException:
    status = 404 if exc.code.endswith("_UNKNOWN") else 409 if exc.code == "AGENT_SESSION_APPLIED" else 422
    return http_error(status, exc.code, exc.message)


@router.post(
    "/uploads/{upload_id}/agent-session",
    response_model=AgentSessionResponse,
    status_code=201,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    summary="Start (or restart) Guided setup for an upload: the helper's suggestions and questions",
)
def start_agent_session(
    upload_id: str, body: SessionStartRequest, root: ConfigRootDep, storage: StorageDep, request: Request
) -> AgentSessionResponse:
    """Runs the advisor - rules only, no AI service - and stores the session. Nothing is changed."""
    with _session_lock(upload_id):
        return _start_agent_session(upload_id, body, root, storage, request)


def _start_agent_session(
    upload_id: str, body: SessionStartRequest, root: Path, storage: Storage, request: Request
) -> AgentSessionResponse:
    loaded = _load_context(storage, root, request, upload_id, body.use_case, body.model_version_id)
    ctx = loaded.ctx
    session_id = f"s-{upload_id}".lower()
    if loaded.recipe_error is not None:
        now = utc_now()
        session = AgentSession(
            session_id=session_id,
            upload_id=upload_id,
            use_case_id=ctx.use_case_id,
            mode=ctx.mode.value,
            agent_name=ctx.config.agent.name_for(ctx.config.name),
            status=SessionStatus.STOPPED,
            stop_reason=loaded.recipe_error.message
            + (
                " The model in use prepares its data in saved steps, and this file does not fit them."
                if loaded.model_has_recipe
                else ""
            ),
            created_at=now,
            updated_at=now,
        )
    else:
        session = start_session(ctx, session_id=session_id)
    # Every later call checks against the same model this one did (the one Run will score with).
    session = session.model_copy(update={"model_version_id": body.model_version_id})
    _find_changes(storage, loaded)
    return _response(_save(storage, session), ctx.config, storage, request)


@router.get(
    "/uploads/{upload_id}/agent-session",
    response_model=AgentSessionResponse,
    responses={404: {"model": ErrorResponse}},
    summary="The Guided-setup session of an upload",
)
def read_agent_session(
    upload_id: str, root: ConfigRootDep, storage: StorageDep, request: Request
) -> AgentSessionResponse:
    session = _load_session(storage, upload_id)
    return _response(session, use_case_config(session.use_case_id, root), storage, request)


@router.post(
    "/uploads/{upload_id}/agent-session/decisions",
    response_model=AgentSessionResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
    summary="Accept or reject the helper's suggestions",
)
def decide_agent_session(
    upload_id: str, body: DecisionsRequest, root: ConfigRootDep, storage: StorageDep, request: Request
) -> AgentSessionResponse:
    with _session_lock(upload_id):
        session = _load_session(storage, upload_id)
        ctx = _load_context(
            storage, root, request, upload_id, session.use_case_id, session.model_version_id
        ).ctx
        before = session
        try:
            if body.decisions:
                session = decide(session, ctx, [(d.proposal_id, d.state, d.value) for d in body.decisions])
            if body.accept_recommended:
                session = accept_recommended(session, ctx)
        except SessionError as exc:
            raise _session_error(exc) from exc
        saved = _save(storage, session)
        _note_proposers(storage, request, before, saved)
        return _response(saved, ctx.config, storage, request)


@router.post(
    "/uploads/{upload_id}/agent-session/edits",
    response_model=AgentSessionResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
    summary="Add an edit made by hand: a step like every other, ticked, undone by unticking",
)
def edit_agent_session(
    upload_id: str, body: EditRequest, root: ConfigRootDep, storage: StorageDep, request: Request
) -> AgentSessionResponse:
    with _session_lock(upload_id):
        session = _load_session(storage, upload_id)
        ctx = _load_context(
            storage, root, request, upload_id, session.use_case_id, session.model_version_id
        ).ctx
        before = session
        try:
            session = add_edit(
                session,
                ctx,
                kind=body.kind,
                column=body.column,
                params=body.params,
                new_column=body.new_column,
            )
        except SessionError as exc:
            raise _session_error(exc) from exc
        saved = _save(storage, session)
        _note_proposers(storage, request, before, saved)
        return _response(saved, ctx.config, storage, request)


@router.post(
    "/uploads/{upload_id}/agent-session/rows",
    response_model=RowsResponse,
    responses={404: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
    summary="A page of the file being set up, for editing by hand (personal data hidden)",
)
def rows_agent_session(
    upload_id: str, body: RowsRequest, root: ConfigRootDep, storage: StorageDep, request: Request
) -> RowsResponse:
    """A POST, like the preview: it reads rows of the file, which an Analyst may and a Viewer may not."""
    offset, limit, where_column, equals = body.offset, body.limit, body.where_column, body.equals
    session = _load_session(storage, upload_id)
    ctx = _load_context(storage, root, request, upload_id, session.use_case_id, session.model_version_id).ctx
    try:
        page = table_rows(
            ctx, offset=offset, limit=min(limit, TABLE_MAX_ROWS), where_column=where_column, equals=equals
        )
    except AgentToolError as exc:
        raise _refuse(422, exc.code, exc.message) from exc
    return RowsResponse(
        columns=tuple(page["columns"]),
        personal=tuple(page["personal"]),
        rows=tuple(tuple(row) for row in page["rows"]),
        row_numbers=tuple(page["row_numbers"]),
        key_column=roles_of(session)[0],
        offset=page["offset"],
        total=page["total"],
    )


@router.post(
    "/uploads/{upload_id}/agent-session/answers",
    response_model=AgentSessionResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    summary="Answer one of the helper's questions",
)
def answer_agent_session(
    upload_id: str, body: AnswerRequest, root: ConfigRootDep, storage: StorageDep, request: Request
) -> AgentSessionResponse:
    with _session_lock(upload_id):
        session = _load_session(storage, upload_id)
        ctx = _load_context(
            storage, root, request, upload_id, session.use_case_id, session.model_version_id
        ).ctx
        before = session
        try:
            session = answer(session, ctx, body.question_id, body.option_id)
        except SessionError as exc:
            raise _session_error(exc) from exc
        saved = _save(storage, session)
        _note_proposers(storage, request, before, saved)
        return _response(saved, ctx.config, storage, request)


@router.post(
    "/uploads/{upload_id}/agent-session/messages",
    response_model=AgentSessionResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    summary="Ask the helper something; its reply is checked before it is kept",
)
def message_agent_session(
    upload_id: str, body: MessageRequest, root: ConfigRootDep, storage: StorageDep, request: Request
) -> AgentSessionResponse:
    """What the person typed is masked (`engine.pii.redact_text`) before it is stored, shown to a
    Viewer or put in a prompt; the transcript is bounded (`CHAT_GRACE_TURNS`); and the session's
    `llm_usage.json` adds each turn to the turns before it."""
    with _session_lock(upload_id):
        session = _load_session(storage, upload_id)
        if session.status is SessionStatus.APPLIED:
            raise _refuse(
                409, "AGENT_SESSION_APPLIED", "This setup was already approved. Start again to change it."
            )
        ctx = _load_context(
            storage, root, request, upload_id, session.use_case_id, session.model_version_id
        ).ctx
        ceiling = 2 * (ctx.config.agent.max_llm_calls_per_session + CHAT_GRACE_TURNS)
        if len(session.transcript) + 2 > ceiling:
            raise _refuse(
                409,
                "AGENT_CHAT_FULL",
                "This chat has used all its questions. The suggestions on the screen still work; "
                "start Guided setup again for a new chat.",
            )
        generative = ctx.config.generative
        # The Product AI, before anything is stored: no AI service means 409 AI_NOT_CONNECTED and a
        # session exactly as it was (the suggestions and questions do not need one).
        ai = resolve_slot(
            generative.llm,
            slot="product",
            storage=storage,
            settings=get_settings(request),
            consent=client_scope(storage, upload_ids=(upload_id,)),
        )
        meter = Meter(ai.client, job_id=session.session_id, llm=ai.llm, budget=generative.budget)
        guardrails = Guardrails(load_policy(root), meter=meter, prompts_root=root)
        text = redact_text(body.text)[0]
        turn = chat_turn(
            ctx,
            session,
            text,
            meter=meter,
            guardrails=guardrails,
            config_root=root,
            principal=current_principal(request),  # Plan O M232: role per tool call
        )
        asked = ChatMessage(role=ChatRole.USER, text=text, created_at=utc_now())
        session = session.model_copy(
            update={
                "transcript": cap_sent((*session.transcript, asked, turn.reply)),
                "tool_results": (*session.tool_results, *turn.tool_results),
                "proposals": (*session.proposals, *turn.proposals),
                "llm_calls": session.llm_calls + turn.llm_calls,
            }
        )
        session = with_summary(session, ctx)
        usage_key = upload_key(upload_id, LLM_USAGE_FILENAME)
        earlier = storage.read_model(usage_key, LlmUsageReport) if storage.exists(usage_key) else None
        storage.write_model(usage_key, add_usage(earlier, meter.usage()))
        return _response(_save(storage, session), ctx.config, storage, request)


@router.post(
    "/uploads/{upload_id}/agent-session/assist",
    response_model=AgentSessionResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    summary="Ask the AI service for a second opinion on the columns (optional)",
)
def assist_agent_session(
    upload_id: str, root: ConfigRootDep, storage: StorageDep, request: Request
) -> AgentSessionResponse:
    """Two optional second opinions (M142, DEC-1409): the AI service may put the rules' candidate columns in
    a better order, and may add questions about columns that may be recorded after the outcome, beside the
    rule-based leak check. It adds no suggestion and ticks nothing; what left is recorded under "What the AI
    looked at" like a chat turn. No AI service connected is `409 AI_NOT_CONNECTED` and a session exactly as
    it was: the rules' suggestions and questions never need one."""
    with _session_lock(upload_id):
        session = _load_session(storage, upload_id)
        if session.status is SessionStatus.APPLIED:
            raise _refuse(
                409, "AGENT_SESSION_APPLIED", "This setup was already approved. Start again to change it."
            )
        ctx = _load_context(
            storage, root, request, upload_id, session.use_case_id, session.model_version_id
        ).ctx
        generative = ctx.config.generative
        ai = resolve_slot(
            generative.llm,
            slot="product",
            storage=storage,
            settings=get_settings(request),
            consent=client_scope(storage, upload_ids=(upload_id,)),
        )
        meter = Meter(ai.client, job_id=session.session_id, llm=ai.llm, budget=generative.budget)
        guardrails = Guardrails(load_policy(root), meter=meter, prompts_root=root)
        result = assist_session(ctx, session, meter=meter, guardrails=guardrails, config_root=root)
        session = result.session.model_copy(update={"transcript": cap_sent(result.session.transcript)})
        usage_key = upload_key(upload_id, LLM_USAGE_FILENAME)
        earlier = storage.read_model(usage_key, LlmUsageReport) if storage.exists(usage_key) else None
        storage.write_model(usage_key, add_usage(earlier, meter.usage()))
        return _response(_save(storage, session), ctx.config, storage, request)


def _sum_cost(a: float | None, b: float | None) -> float | None:
    """Two priced costs added; null when either could not be priced (DEC-208)."""
    return None if a is None or b is None else round(a + b, 6)


def add_usage(earlier: LlmUsageReport | None, turn: LlmUsageReport) -> LlmUsageReport:
    """One session's `llm_usage.json`: this turn's usage added to every earlier turn's.

    A `Meter` belongs to one request, so writing `meter.usage()` on its own kept only the last turn.
    A turn that made no call adds nothing and leaves an earlier null cost null.
    """
    if earlier is None:
        return turn
    models = {m.model_id: m for m in earlier.by_model}
    for m in turn.by_model:
        old = models.get(m.model_id)
        models[m.model_id] = (
            m
            if old is None
            else m.model_copy(
                update={
                    "calls": old.calls + m.calls,
                    "input_tokens": old.input_tokens + m.input_tokens,
                    "output_tokens": old.output_tokens + m.output_tokens,
                    "cost_estimate_usd": _sum_cost(old.cost_estimate_usd, m.cost_estimate_usd),
                }
            )
        )
    purposes = {p.purpose.value: p for p in earlier.by_purpose}
    for p in turn.by_purpose:
        prev = purposes.get(p.purpose.value)
        purposes[p.purpose.value] = (
            p
            if prev is None
            else p.model_copy(
                update={
                    "calls": prev.calls + p.calls,
                    "input_tokens": prev.input_tokens + p.input_tokens,
                    "output_tokens": prev.output_tokens + p.output_tokens,
                    "cost_estimate_usd": _sum_cost(prev.cost_estimate_usd, p.cost_estimate_usd),
                }
            )
        )
    before, now = earlier.totals, turn.totals
    if now.calls == 0:
        cost = before.cost_estimate_usd
    elif before.calls == 0:
        cost = now.cost_estimate_usd
    else:
        cost = _sum_cost(before.cost_estimate_usd, now.cost_estimate_usd)
    totals = now.model_copy(
        update={
            "calls": before.calls + now.calls,
            "input_tokens": before.input_tokens + now.input_tokens,
            "output_tokens": before.output_tokens + now.output_tokens,
            "cost_estimate_usd": cost,
            "model_ids": tuple(sorted({*before.model_ids, *now.model_ids})),
        }
    )
    return turn.model_copy(
        update={
            "totals": totals,
            "cache_hits": earlier.cache_hits + turn.cache_hits,
            "by_model": tuple(models[k] for k in sorted(models)),
            "by_purpose": tuple(purposes[k] for k in sorted(purposes)),
            "warnings": tuple(dict.fromkeys((*earlier.warnings, *turn.warnings))),
        }
    )


def _accepted_recipe(
    session: AgentSession, upload: UploadRecord, principal: str | None, config: UseCaseConfig
) -> DataRecipe | None:
    """The accepted steps as a recipe, with the levels and failure limit they are approved under:
    every later replay runs under those, whatever the use case says by then."""
    steps = recipe_steps(p for p in session.proposals if p.state is ProposalState.ACCEPTED)
    if not steps:
        return None
    key, target = roles_of(session)
    return DataRecipe(
        recipe_id=f"rc-{new_upload_id()}".lower().replace("_", "-"),
        use_case_id=session.use_case_id,
        source_fingerprint=upload.fingerprint_hash,
        steps=steps,
        recipe_hash=recipe_hash(steps),
        primary_key=key,
        target=target,
        approved_by=principal,
        levels=tuple(config.agent.levels),
        max_failure_pct=config.agent.max_conversion_failure_pct,
        created_at=utc_now(),
    )


def _preview_sample(frame: Any, recipe: DataRecipe | None) -> Any:
    """The rows a preview runs on: the first `PREVIEW_SAMPLE_ROWS` rows, or - when the recipe makes the
    rows one per entity (combines them, or keeps each one's latest) - every row of the first
    `PREVIEW_SAMPLE_ROWS` entities, so no entity is combined from, or its latest row chosen among, part
    of its rows."""
    combine = next((s for s in (recipe.steps if recipe else ()) if s.kind in ROWS_PER_ID_STEP_KINDS), None)
    if combine is None or combine.column not in frame.columns:
        return frame.head(PREVIEW_SAMPLE_ROWS)
    keys = frame[combine.column].dropna().drop_duplicates().head(PREVIEW_SAMPLE_ROWS)
    return frame[frame[combine.column].isin(keys)]


@router.post(
    "/uploads/{upload_id}/agent-session/preview",
    response_model=PreviewResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    summary="The first rows before and after the accepted steps, and what each step did",
)
def preview_agent_session(
    upload_id: str, root: ConfigRootDep, storage: StorageDep, request: Request
) -> PreviewResponse:
    session = _load_session(storage, upload_id)
    loaded = _load_context(storage, root, request, upload_id, session.use_case_id, session.model_version_id)
    ctx = loaded.ctx
    personal = {column.name for column in ctx.profile.columns if column.pii_kinds}
    recipe = _accepted_recipe(session, loaded.upload, None, ctx.config)
    before = _preview_sample(ctx.frame, recipe)
    after, receipt = before, None
    if recipe is not None:
        try:
            run = run_recipe(
                before,
                recipe.steps,
                upload_id=upload_id,
                primary_key=recipe.primary_key,
                target=recipe.target,
                levels=ctx.config.agent.levels,
                max_failure_pct=ctx.config.agent.max_conversion_failure_pct,
            )
        except RecipeError as exc:
            raise _refuse(409, exc.code, exc.message) from exc
        after, receipt = run.frame, run.receipt
    # Plan O M246 (DEC-1816): Approve checks that this preview was shown.
    from engine.doctor.plan_g_approval import record_preview

    record_preview(
        storage,
        upload_id,
        recipe_hash=recipe.recipe_hash if recipe is not None else None,
        receipt=receipt,
        actor=requested_by(request),
        at=utc_now(),
    )
    columns_before = tuple(str(c) for c in before.columns)
    columns_after = tuple(str(c) for c in after.columns)
    return PreviewResponse(
        columns_before=columns_before,
        rows_before=masked_rows(before, columns_before, personal, PREVIEW_ROWS),
        columns_after=columns_after,
        rows_after=masked_rows(after, columns_after, personal, PREVIEW_ROWS),
        receipt=receipt,
    )


@router.post(
    "/uploads/{upload_id}/agent-session/apply",
    response_model=ApplyResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ValidationErrorResponse}},
    summary="Approve: prepare the data with the accepted steps and return what Run needs",
)
def apply_agent_session(
    upload_id: str, root: ConfigRootDep, storage: StorageDep, request: Request
) -> ApplyResponse | JSONResponse:
    """Refused while anything is undecided. Runs the recipe on every row into a new upload, then the
    Run button's checks with the accepted settings; a 409 carries them when they would fail.

    A scoring file is prepared by the model's own saved recipe, exactly as `POST /runs` would
    (`replay_for_scoring`), and checked as prepared; Run then reuses that prepared upload."""
    with _session_lock(upload_id):
        return _apply_agent_session(upload_id, root, storage, request)


def _apply_agent_session(
    upload_id: str,
    root: Path,
    storage: Storage,
    request: Request,
    approved_sentence: str | None = None,
) -> ApplyResponse | JSONResponse:
    session = _load_session(storage, upload_id)
    loaded = _load_context(storage, root, request, upload_id, session.use_case_id, session.model_version_id)
    ctx, upload = loaded.ctx, loaded.upload
    if session.status is SessionStatus.STOPPED:
        raise _refuse(
            409, "AGENT_SESSION_STOPPED", session.stop_reason or "This data cannot be used as it is."
        )
    if session.status is SessionStatus.APPLIED:
        raise _refuse(409, "AGENT_SESSION_APPLIED", "This setup was already approved.")
    if session.undecided:
        raise _refuse(
            409,
            "AGENT_UNDECIDED",
            f"{len(session.undecided)} suggestion(s) or question(s) still need a decision before approving.",
        )
    missing = unsettled_roles(session)
    if missing:
        # DEC-1821: a session saved before the rule (ready with its ID or outcome rejected) is refused plainly
        # here, rather than by the run checks' VALIDATION_FAILED.
        what = " and ".join(
            "the column that identifies each row" if path == ROLE_PRIMARY_KEY else "the outcome column"
            for path in missing
        )
        raise _refuse(409, "AGENT_UNDECIDED", f"Choose {what} before approving.")
    key, target = roles_of(session)
    recipe = _accepted_recipe(session, upload, requested_by(request), ctx.config)
    approval = None
    if ctx.mode is RunMode.TRAIN:
        # Plan O M246 (DEC-1816): M233's approval-time facts - the preview shown, the reshape guards, the exact
        # sentence and the role - before anything is written.
        approval = _approval_gate(storage, session, upload, ctx.config, request, recipe, approved_sentence)
    prepared, receipt = upload, None
    if ctx.mode is RunMode.SCORE and loaded.version is not None:
        # The model's recipe prepares a scoring file; the session proposes no steps of its own. Run
        # would refuse another ID column than the recipe's, so Approve does not prepare for one.
        if key is not None:
            refuse_other_roles(
                model_recipe(storage, loaded.version), primary_key=key, target=None, training=False
            )
        replayed = replay_for_scoring(storage, ctx.config, upload, loaded.version)
        if isinstance(replayed, ValidationReport):
            return validation_conflict(replayed)
        prepared = replayed[0]
        receipt = load_receipt(storage, prepared.upload_id) if prepared.upload_id != upload_id else None
        recipe = None
    elif recipe is not None:
        try:
            derived = write_derived_upload(storage, ctx.config, upload, recipe)
        except RecipeError as exc:
            return validation_conflict(recipe_failure_report(exc, upload_id=upload_id, mode=ctx.mode))
        prepared, receipt = derived.record, derived.receipt
    overrides = run_overrides(session.proposals)
    resolved = resolve_config(ctx.use_case_id, overrides, root=root)
    report = check_plan(
        read_frame(storage, prepared.source_key, prepared.file_format, profile_row_cap(resolved.config)),
        resolved.config,
        mode=ctx.mode,
        primary_key=key,
        target=target,
        upload_id=prepared.upload_id,
        row_count=prepared.row_count,
        schema=ctx.schema,
    )
    if not report.passed:
        return validation_conflict(report)
    applied = session.model_copy(
        update={"status": SessionStatus.APPLIED, "applied_upload_id": prepared.upload_id}
    )
    if approval is not None:
        # Plan O M246: the approval's record first - before the client's memory of the recipe, the session or
        # anything else that keeps the approval's effects - so nothing is kept for an approval not recorded.
        _record_approval(root, request, session, upload, recipe, approval)
    if ctx.mode is RunMode.TRAIN and recipe is not None and prepared.upload_id != upload_id:
        _remember(storage, ctx, upload, recipe, prepared.upload_id)
    _save(storage, applied.model_copy(update={"updated_at": utc_now()}))
    if approval is not None:
        _count_patterns(storage, session, upload)
    set_audit_context(
        request,
        object_id=prepared.upload_id,
        object_type="upload",
        after_hash=receipt.recipe_hash if receipt is not None else None,
        details={
            "use_case_id": ctx.use_case_id,
            "count": sum(1 for p in session.proposals if p.state is ProposalState.ACCEPTED),
        },
    )
    return ApplyResponse(
        upload_id=prepared.upload_id,
        mode=ctx.mode,
        primary_key=key,
        target=target,
        overrides=overrides,
        summary=applied.summary,
        receipt=receipt,
    )


def _approver(request: Request) -> Any:
    from engine.access.roles import LOCAL_OPERATOR, Principal

    principal = getattr(request.state, "principal", None)
    return principal if isinstance(principal, Principal) else LOCAL_OPERATOR


def _approval_gate(
    storage: Storage,
    session: AgentSession,
    upload: UploadRecord,
    config: UseCaseConfig,
    request: Request,
    recipe: DataRecipe | None,
    sentence: str | None,
) -> Any:
    """Plan O M246 (DEC-1816): Guided setup's Approve with Data Doctor on holds M233's approval-time rules
    (`engine.doctor.plan_g_approval`); a refusal records nothing. Returns what was approved."""
    from engine.doctor.plan_g_approval import approval_of, approval_refusal, read_preview

    required = bool(config.governance.approval_required)
    approval = approval_of(
        session,
        file_name=upload.file_name,
        preview=read_preview(storage, upload.upload_id),
        recipe_hash=recipe.recipe_hash if recipe is not None else None,
        training=True,
        approval_required=required,
    )
    found = approval_refusal(
        approval, approver=_approver(request), sentence_read=sentence, approval_required=required
    )
    if found is not None:
        code = found.code or "POLICY_NO_RULES"
        reason = str(found.detail.get("reason", "Not allowed."))
        if code == "APPROVAL_SENTENCE_CHANGED":
            reason = f"{reason} It now reads: “{approval.sentence}”"
        raise _refuse(403 if code in {"TOOL_ROLE_REQUIRED", "POLICY_ROLE_SEPARATION"} else 409, code, reason)
    return approval


def _note_proposers(storage: Storage, request: Request, before: AgentSession, after: AgentSession) -> None:
    """Plan O M246 (O17): who ticked, answered or added each suggestion, for separation of
    duties at Approve (`engine.doctor.plan_g_approval.record_proposers`)."""
    from engine.doctor.plan_g_approval import record_proposers

    record_proposers(storage, after.upload_id, before, after, requested_by(request))


def _record_approval(
    root: Path,
    request: Request,
    session: AgentSession,
    upload: UploadRecord,
    recipe: DataRecipe | None,
    approval: Any,
) -> None:
    """Plan O M246 (DEC-1816): the approval's record - Plan K's signed ledger (`data.prescription.approved`) when
    `agentic.enabled` is on. Called before anything keeps the approval's effects; a ledger that cannot be written
    refuses the approval (no approval without its record). Idempotent: a retried Approve whose first attempt
    was recorded finds that entry (`engine.doctor.protocol.ledger_port`)."""
    from engine.agentic.canonical import digest
    from engine.agentic.errors import AgenticError
    from engine.doctor.protocol import DATA_PRESCRIPTION_APPROVED, decision_body

    approver = _approver(request)
    accepted = [p for p in session.proposals if p.state is ProposalState.ACCEPTED]
    body = decision_body(
        case_id=None,
        upload_id=upload.upload_id,
        item_digests=[digest(p.model_dump(mode="json", exclude={"decided_by"})) for p in accepted],
        text=approval.sentence,
        decided_by=approver.user_id,
        kind="guided_setup",
        recipe_hash=recipe.recipe_hash if recipe is not None else None,
        # Plan O M243 (DEC-1813): Guided setup's recipe is a prescription with one part; its hash is the record's.
        prescription_hash=_one_part_hash(recipe),
        identity_verified=False if approver.kind != "user" else None,
    )
    try:
        from engine.agentic.config import agentic_enabled

        if agentic_enabled(root):
            from api.access import get_platform_engine, platform_data_dir
            from engine.agentic.config import policy_version
            from engine.agentic.ledger import signer_for_data_dir
            from engine.doctor.protocol import ledger_port

            data_dir = platform_data_dir(request) or get_settings(request).data_dir
            ledger_port(get_platform_engine(request), signer_for_data_dir(data_dir), policy_version(root))(
                DATA_PRESCRIPTION_APPROVED, approver.user_id, body
            )
    except AgenticError as exc:
        raise _refuse(exc.status, exc.code, exc.message) from exc


def _one_part_hash(recipe: DataRecipe | None) -> str | None:
    """Plan O M243: the hash of `recipe` read as a one-part prescription (`engine.doctor.prescription.one_part`)."""
    if recipe is None:
        return None
    from engine.doctor.prescription import one_part

    return one_part(recipe).prescription_hash


def _count_patterns(storage: Storage, session: AgentSession, upload: UploadRecord) -> None:
    """[REQ] 11: the approved AI patterns counted for the product team, once the approval is kept. Counting
    never fails an approval."""
    from engine.doctor.patterns import record_approved

    ai_steps = [
        (p.step.kind.value, dict(p.step.params))
        for p in session.proposals
        if p.state is ProposalState.ACCEPTED and p.source is ProposalSource.AI and p.step is not None
    ]
    if not ai_steps:
        return
    try:
        record_approved(storage, ai_steps, place=upload.upload_id, client_id="default", at=utc_now())
    except Exception as exc:  # a count never undoes an approval
        _LOGGER.warning("agent.patterns_not_counted upload=%s error=%s", upload.upload_id, type(exc).__name__)


def _remember(
    storage: Storage, ctx: AgentContext, upload: UploadRecord, recipe: DataRecipe, prepared_id: str
) -> None:
    """Plan L M159 (DEC-1592): remember this shape's treatment. Never fails an approval."""
    try:
        intake = (
            storage.read_model(upload.intake_key, IntakeReport)
            if upload.intake_key is not None and storage.exists(upload.intake_key)
            else None
        )
        remember(
            storage,
            use_case_id=ctx.use_case_id,
            columns=ctx.profile.fingerprint.columns,
            sheet=intake.sheet if intake is not None else None,
            intake_enabled=not (intake is not None and intake.turned_off),
            recipe=recipe,
            recipe_upload_id=prepared_id,
            source_upload_id=upload.upload_id,
        )
    except Exception as exc:  # the memory is a convenience; the approval it follows has succeeded
        _LOGGER.warning(
            "doctor.memory.remember_failed upload=%s error=%s", upload.upload_id, type(exc).__name__
        )


@router.post(
    "/uploads/{upload_id}/agent-session/same-treatment",
    response_model=AgentSessionResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
    summary="Use the same treatment as last time: tick the remembered steps, still to be checked and approved",
)
def same_treatment_agent_session(
    upload_id: str, body: SameTreatmentRequest, root: ConfigRootDep, storage: StorageDep, request: Request
) -> AgentSessionResponse:
    """Plan L M159 (DEC-1594): the offer `treatment` names, taken. Nothing is prepared until Approve."""
    with _session_lock(upload_id):
        session = _load_session(storage, upload_id)
        ctx = _load_context(
            storage, root, request, upload_id, session.use_case_id, session.model_version_id
        ).ctx
        offer = _treatment(storage, session, unused=False)
        if offer is None or not offer.can_apply:
            raise _refuse(
                409, "DOCTOR_MEMORY_NONE", "There is no earlier treatment for a file like this one."
            )
        if offer.recipe_id != body.recipe_id:
            raise _refuse(
                409,
                "DOCTOR_MEMORY_CHANGED",
                "The treatment for files like this one has changed since it was shown. Look at it again.",
            )
        memory = recall(storage, session.use_case_id, offer.shape_hash)
        recipe = remembered_recipe(storage, memory) if memory is not None else None
        if memory is None or recipe is None:
            raise _refuse(409, "DOCTOR_MEMORY_NONE", "The earlier treatment is no longer kept.")
        try:
            session = use_same_treatment(session, ctx, recipe, memory)
        except SessionError as exc:
            raise _session_error(exc) from exc
        set_audit_context(
            request,
            object_id=upload_id,
            object_type="upload",
            after_hash=recipe.recipe_hash,
            details={"use_case_id": ctx.use_case_id, "count": len(recipe.steps)},
        )
        return _response(_save(storage, session), ctx.config, storage, request)


# ---------------------------------------------------------------------------
# Plan I: Ask your data (DEC-1250 … DEC-1259)
# ---------------------------------------------------------------------------
EXPLORE_KEY: Final[str] = f"agent/{EXPLORE_SESSION_FILENAME}"

register(
    {
        # Reading the chat is a read (DEC-716); asking spends tokens and starting over deletes, as in Guided
        # setup's chat (DEC-1253).
        ("GET", "/uploads/{upload_id}/ask"): RoutePolicy(
            role=Role.VIEWER,
            action="agent.ask.read",
            purpose="see Ask your data",
            object_type="upload",
            object_param="upload_id",
        ),
        ("POST", "/uploads/{upload_id}/ask/messages"): RoutePolicy(
            role=Role.ANALYST,
            action="agent.ask",
            purpose="ask about your data",
            object_type="upload",
            object_param="upload_id",
        ),
        ("DELETE", "/uploads/{upload_id}/ask"): RoutePolicy(
            role=Role.ANALYST,
            action="agent.ask.clear",
            purpose="start a new Ask your data chat",
            object_type="upload",
            object_param="upload_id",
        ),
    }
)


class AskResponse(StrictBase):
    """An upload's Ask-your-data chat, what the chat box may do, and the charts under its replies."""

    session: AgentSession | None = Field(
        description="The read-only explore session (`explore: true`); null until the first question."
    )
    chat: ChatAvailability
    charts: tuple[RateChart, ...] = Field(
        default=(),
        description="One bar chart per `rate_by` result of a reply's own turn (at most three per reply), "
        "read from the stored tool result, never from the reply's text.",
    )


def _explore_key(upload_id: str) -> str:
    return upload_key(upload_id, EXPLORE_KEY)


def _ask_config(storage: Storage, root: Path, upload_id: str) -> tuple[UploadRecord, UseCaseConfig]:
    """The upload and its use case, or 404 / `409 AGENT_NOT_AVAILABLE` for a use case with no helper."""
    upload = load_upload(storage, upload_id)
    config = use_case_config(upload.use_case_id, root)
    if not agent_available(config):
        raise _refuse(409, "AGENT_NOT_AVAILABLE", f"{config.name} has no data helper to ask.")
    return upload, config


def _explore_context(storage: Storage, root: Path, upload_id: str) -> AgentContext:
    """The upload exactly as it was sent: no recipe replayed and no model resolved, because nothing is
    prepared or run from here. A prepared (derived) upload may be asked about like any other."""
    upload, config = _ask_config(storage, root, upload_id)
    profile = load_upload_profile(storage, upload_id)
    frame = read_frame(storage, upload.source_key, upload.file_format, profile_row_cap(config))
    return AgentContext(
        use_case_id=config.id,
        config=config,
        config_root=root,
        upload_id=upload_id,
        mode=upload.mode,
        profile=profile,
        frame=frame,
    )


def _load_explore(storage: Storage, upload_id: str) -> AgentSession | None:
    key = _explore_key(upload_id)
    return storage.read_model(key, AgentSession) if storage.exists(key) else None


def _ask_response(
    session: AgentSession | None, config: UseCaseConfig, storage: Storage, request: Request
) -> AskResponse:
    """`_response`'s chat availability (it reads only the config), with the charts of every reply."""
    shown = session if session is not None else _unsaved(config)
    return AskResponse(
        session=session,
        chat=_response(shown, config, storage, request).chat,
        charts=charts_of(session) if session is not None else (),
    )


def _unsaved(config: UseCaseConfig) -> AgentSession:
    """An empty explore session that is never stored: what stands in before the first question."""
    now = utc_now()
    return AgentSession(
        session_id="ask",
        upload_id="ask",
        use_case_id=config.id,
        mode=RunMode.TRAIN.value,
        agent_name=config.agent.name_for(config.name),
        status=SessionStatus.READY,
        explore=True,
        created_at=now,
        updated_at=now,
    )


@router.get(
    "/uploads/{upload_id}/ask",
    response_model=AskResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    summary="Ask your data: an upload's read-only chat with the data helper, and its charts",
)
def read_ask(upload_id: str, root: ConfigRootDep, storage: StorageDep, request: Request) -> AskResponse:
    """`session` is null until the first question; `chat.available` says whether a question can be asked."""
    _, config = _ask_config(storage, root, upload_id)
    return _ask_response(_load_explore(storage, upload_id), config, storage, request)


@router.post(
    "/uploads/{upload_id}/ask/messages",
    response_model=AskResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    summary="Ask the data helper a question about an upload; it looks things up and cannot change anything",
)
def ask_message(
    upload_id: str, body: MessageRequest, root: ConfigRootDep, storage: StorageDep, request: Request
) -> AskResponse:
    """Guided setup's chat turn (`chat_turn`) in a read-only explore session: the same tools, egress gate,
    grounding and guardrails, with `propose_setting` refused (DEC-1251). The first question opens the
    session. With no AI service connected it is `409 AI_NOT_CONNECTED` and nothing is stored."""
    with _session_lock(f"{upload_id}:ask"):
        ctx = _explore_context(storage, root, upload_id)
        session = _load_explore(storage, upload_id) or start_explore(ctx, session_id=f"x-{upload_id}".lower())
        ceiling = 2 * (ctx.config.agent.max_llm_calls_per_session + CHAT_GRACE_TURNS)
        if len(session.transcript) + 2 > ceiling:
            raise _refuse(
                409, "AGENT_CHAT_FULL", "This chat has used all its questions. Start a new chat to ask more."
            )
        generative = ctx.config.generative
        ai = resolve_slot(
            generative.llm,
            slot="product",
            storage=storage,
            settings=get_settings(request),
            consent=client_scope(storage, upload_ids=(upload_id,)),
        )
        meter = Meter(ai.client, job_id=session.session_id, llm=ai.llm, budget=generative.budget)
        guardrails = Guardrails(load_policy(root), meter=meter, prompts_root=root)
        text = redact_text(body.text)[0]
        turn = chat_turn(
            ctx,
            session,
            text,
            meter=meter,
            guardrails=guardrails,
            config_root=root,
            principal=current_principal(request),  # Plan O M232: role per tool call
        )
        asked = ChatMessage(role=ChatRole.USER, text=text, created_at=utc_now())
        session = session.model_copy(
            update={
                "transcript": cap_sent((*session.transcript, asked, turn.reply)),
                "tool_results": (*session.tool_results, *turn.tool_results),
                "llm_calls": session.llm_calls + turn.llm_calls,
                "updated_at": utc_now(),
            }
        )
        storage.write_model(_explore_key(upload_id), session)
        usage_key = upload_key(upload_id, LLM_USAGE_FILENAME)
        earlier = storage.read_model(usage_key, LlmUsageReport) if storage.exists(usage_key) else None
        storage.write_model(usage_key, add_usage(earlier, meter.usage()))
        return _ask_response(session, ctx.config, storage, request)


@router.delete(
    "/uploads/{upload_id}/ask",
    response_model=AskResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    summary="Start a new Ask-your-data chat on an upload; the old one is deleted",
)
def clear_ask(upload_id: str, root: ConfigRootDep, storage: StorageDep, request: Request) -> AskResponse:
    with _session_lock(f"{upload_id}:ask"):
        _, config = _ask_config(storage, root, upload_id)
        key = _explore_key(upload_id)
        if storage.exists(key):
            storage.delete(key)
        return _ask_response(None, config, storage, request)
