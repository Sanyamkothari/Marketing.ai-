"""Plan G routes: the dry-run data checks (M71) and the Guided-setup session (M74, DEC-1018).

`POST /uploads/{upload_id}/checks` answers "what would the Run button say about this file, with
this ID column, outcome and these settings?" without creating a run or writing anything. It
follows `POST /runs` step for step - the same config resolution, the same role rule on overrides,
the same mode check, the same model-version resolver for a scoring file, and the same validators
through `engine.agent.checks.check_plan` - so the two cannot disagree (Plan G §9, M71).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import Field, JsonValue

from api.access import set_audit_context
from api.access_policy import RoutePolicy, register
from api.deps import ConfigRootDep, StorageDep, get_registry
from api.routes.agent_recipes import model_recipe, recipe_failure_report, write_derived_upload
from api.routes.runs import read_frame, requested_by, score_version, validation_conflict
from api.routes.uploads import http_error, load_upload, load_upload_profile, profile_row_cap, use_case_config
from api.schemas import ErrorResponse, UploadRecord, ValidationErrorResponse
from engine.access.roles import Role
from engine.agent.advisor import recipe_steps, run_overrides
from engine.agent.checks import check_plan
from engine.agent.contracts import (
    AgentSession,
    AgentSummary,
    ChatMessage,
    ChatRole,
    DataRecipe,
    ProposalState,
    RecipeReceipt,
    SessionStatus,
    recipe_hash,
)
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
    with_summary,
)
from engine.agent.tools import AgentContext
from engine.aws_connection import profile_in_force
from engine.config import PrimaryKey, RunMode, StrictBase, UseCaseConfig, resolve_config, sole_key
from engine.contracts import FeatureSchema, ValidationReport
from engine.generative.budget import Meter
from engine.generative.guardrails import Guardrails, load_policy
from engine.llm import build_client
from engine.pii import redact_cells
from engine.storage import Storage, upload_key
from engine.utils.ids import new_upload_id
from engine.utils.time import utc_now

__all__ = ["AgentSessionResponse", "ApplyResponse", "ChecksRequest", "PreviewResponse", "router"]

router = APIRouter(tags=["agent"])

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
    """The report `POST /runs` would give, always with 200; `passed` says whether Run would start.

    Nothing is written: not a run directory, and not the upload's `validation.json`, which stays the
    record of the last real Run. The registry is opened only for a scoring file, because opening it
    creates the local registry database.
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
    if upload.mode is RunMode.SCORE:
        version = score_version(get_registry(request), config=config, version_id=body.model_version_id)
        schema = storage.read_model(version.schema_key, FeatureSchema)
    elif body.model_version_id is not None:
        raise http_error(
            409, "UPLOAD_MODE_MISMATCH", "A model version is only used to check a file uploaded for scoring."
        )
    return check_plan(
        read_frame(storage, upload.source_key, upload.file_format, profile_row_cap(config)),
        config,
        mode=upload.mode,
        primary_key=primary_key,
        target=body.target,
        upload_id=upload_id,
        row_count=profile.row_count,
        schema=schema,
    )


# ---------------------------------------------------------------------------
# M74: the Guided-setup session (Plan G §9, DEC-1018)
# ---------------------------------------------------------------------------
SESSION_FILENAME: Final[str] = "agent/agent_session.json"
LLM_USAGE_FILENAME: Final[str] = "agent/llm_usage.json"
PREVIEW_ROWS: Final[int] = 5

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
    }
)


class ChatAvailability(StrictBase):
    backend: str = Field(description="`fake` (practice answers) or `bedrock`.")
    generation_model_id: str | None = Field(
        default=None, description="The model that writes replies, when known."
    )


class AgentSessionResponse(StrictBase):
    session: AgentSession
    chat: ChatAvailability


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
    profile = load_upload_profile(storage, upload_id)
    frame = read_frame(storage, upload.source_key, upload.file_format, profile_row_cap(config))
    schema: FeatureSchema | None = None
    recipe_error: RecipeError | None = None
    if upload.mode is RunMode.SCORE:
        version = score_version(get_registry(request), config=config, version_id=model_version_id)
        schema = storage.read_model(version.schema_key, FeatureSchema)
        recipe = model_recipe(storage, version)
        if recipe is not None:
            try:
                frame = run_recipe(
                    frame,
                    recipe.steps,
                    upload_id=upload_id,
                    primary_key=recipe.primary_key,
                    target=recipe.target,
                    levels=config.agent.levels,
                    max_failure_pct=config.agent.max_conversion_failure_pct,
                    snapshot_column=recipe.snapshot_column,
                ).frame
            except RecipeError as exc:
                recipe_error = exc
    ctx = AgentContext(
        use_case_id=config.id,
        config=config,
        config_root=root,
        upload_id=upload_id,
        mode=upload.mode,
        profile=profile,
        frame=frame,
        schema=schema,
    )
    return _Loaded(ctx=ctx, upload=upload, recipe_error=recipe_error)


def _load_session(storage: Storage, upload_id: str) -> AgentSession:
    key = _session_key(upload_id)
    if not storage.exists(key):
        raise _refuse(404, "AGENT_SESSION_NOT_FOUND", "Guided setup has not been started for this file.")
    return storage.read_model(key, AgentSession)


def _save(storage: Storage, session: AgentSession) -> AgentSession:
    storage.write_model(_session_key(session.upload_id), session)
    return session


def _response(session: AgentSession, config: UseCaseConfig) -> AgentSessionResponse:
    llm = config.generative.llm
    return AgentSessionResponse(
        session=session,
        chat=ChatAvailability(backend=llm.backend.value, generation_model_id=llm.generation_model_id or None),
    )


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
            stop_reason=f"{loaded.recipe_error.message} The model in use prepares its data in saved steps, "
            "and this file does not fit them.",
            created_at=now,
            updated_at=now,
        )
    else:
        session = start_session(ctx, session_id=session_id)
    # Every later call checks against the same model this one did (the one Run will score with).
    session = session.model_copy(update={"model_version_id": body.model_version_id})
    return _response(_save(storage, session), ctx.config)


@router.get(
    "/uploads/{upload_id}/agent-session",
    response_model=AgentSessionResponse,
    responses={404: {"model": ErrorResponse}},
    summary="The Guided-setup session of an upload",
)
def read_agent_session(upload_id: str, root: ConfigRootDep, storage: StorageDep) -> AgentSessionResponse:
    session = _load_session(storage, upload_id)
    return _response(session, use_case_config(session.use_case_id, root))


@router.post(
    "/uploads/{upload_id}/agent-session/decisions",
    response_model=AgentSessionResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
    summary="Accept or reject the helper's suggestions",
)
def decide_agent_session(
    upload_id: str, body: DecisionsRequest, root: ConfigRootDep, storage: StorageDep, request: Request
) -> AgentSessionResponse:
    session = _load_session(storage, upload_id)
    ctx = _load_context(storage, root, request, upload_id, session.use_case_id, session.model_version_id).ctx
    try:
        if body.decisions:
            session = decide(session, ctx, [(d.proposal_id, d.state, d.value) for d in body.decisions])
        if body.accept_recommended:
            session = accept_recommended(session, ctx)
    except SessionError as exc:
        raise _session_error(exc) from exc
    return _response(_save(storage, session), ctx.config)


@router.post(
    "/uploads/{upload_id}/agent-session/answers",
    response_model=AgentSessionResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    summary="Answer one of the helper's questions",
)
def answer_agent_session(
    upload_id: str, body: AnswerRequest, root: ConfigRootDep, storage: StorageDep, request: Request
) -> AgentSessionResponse:
    session = _load_session(storage, upload_id)
    ctx = _load_context(storage, root, request, upload_id, session.use_case_id, session.model_version_id).ctx
    try:
        session = answer(session, ctx, body.question_id, body.option_id)
    except SessionError as exc:
        raise _session_error(exc) from exc
    return _response(_save(storage, session), ctx.config)


@router.post(
    "/uploads/{upload_id}/agent-session/messages",
    response_model=AgentSessionResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    summary="Ask the helper something; its reply is checked before it is kept",
)
def message_agent_session(
    upload_id: str, body: MessageRequest, root: ConfigRootDep, storage: StorageDep, request: Request
) -> AgentSessionResponse:
    session = _load_session(storage, upload_id)
    if session.status is SessionStatus.APPLIED:
        raise _refuse(
            409, "AGENT_SESSION_APPLIED", "This setup was already approved. Start again to change it."
        )
    ctx = _load_context(storage, root, request, upload_id, session.use_case_id, session.model_version_id).ctx
    generative = ctx.config.generative
    meter = Meter(
        build_client(generative.llm, profile=profile_in_force()),
        job_id=session.session_id,
        llm=generative.llm,
        budget=generative.budget,
    )
    guardrails = Guardrails(load_policy(root), meter=meter, prompts_root=root)
    turn = chat_turn(ctx, session, body.text, meter=meter, guardrails=guardrails, config_root=root)
    asked = ChatMessage(role=ChatRole.USER, text=body.text, created_at=utc_now())
    session = session.model_copy(
        update={
            "transcript": (*session.transcript, asked, turn.reply),
            "tool_results": (*session.tool_results, *turn.tool_results),
            "proposals": (*session.proposals, *turn.proposals),
            "llm_calls": session.llm_calls + turn.llm_calls,
        }
    )
    session = with_summary(session, ctx)
    storage.write_model(upload_key(upload_id, LLM_USAGE_FILENAME), meter.usage())
    return _response(_save(storage, session), ctx.config)


def _accepted_recipe(session: AgentSession, upload: UploadRecord, principal: str | None) -> DataRecipe | None:
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
        created_at=utc_now(),
    )


def _cell(value: Any) -> str:
    """One preview cell as text. Numbers and dates are formatted, never run through the text masker:
    a float's digits (0.020000000000000002) would otherwise read as a phone number."""
    if value is None or (isinstance(value, float) and value != value):
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)) or hasattr(value, "dtype"):
        try:
            return f"{float(value):.6g}"
        except (TypeError, ValueError):
            pass
    if hasattr(value, "isoformat"):
        text = str(value.isoformat())
        return text[:10] if text.endswith("T00:00:00") else text
    return redact_cells([str(value)[:60]])[0]


def _rows(frame: Any, columns: tuple[str, ...], personal: set[str]) -> tuple[tuple[str, ...], ...]:
    head = frame.head(PREVIEW_ROWS)
    rows: list[tuple[str, ...]] = []
    for _, row in head.iterrows():
        rows.append(tuple("[personal data]" if name in personal else _cell(row[name]) for name in columns))
    return tuple(rows)


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
    before = ctx.frame
    recipe = _accepted_recipe(session, loaded.upload, None)
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
    columns_before = tuple(str(c) for c in before.columns)
    columns_after = tuple(str(c) for c in after.columns)
    return PreviewResponse(
        columns_before=columns_before,
        rows_before=_rows(before, columns_before, personal),
        columns_after=columns_after,
        rows_after=_rows(after, columns_after, personal),
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
    Run button's checks with the accepted settings; a 409 carries them when they would fail."""
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
    key, target = roles_of(session)
    recipe = _accepted_recipe(session, upload, requested_by(request))
    prepared, receipt = upload, None
    if recipe is not None:
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
    _save(storage, applied.model_copy(update={"updated_at": utc_now()}))
    set_audit_context(
        request,
        object_id=prepared.upload_id,
        object_type="upload",
        after_hash=recipe.recipe_hash if recipe is not None else None,
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
