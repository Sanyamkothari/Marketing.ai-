"""Plan G routes: the dry-run data checks (M71) and the Guided-setup session (M74, DEC-1018).

`POST /uploads/{upload_id}/checks` answers "what would the Run button say about this file, with
this ID column, outcome and these settings?" without creating a run or writing anything. It
follows `POST /runs` step for step - the same config resolution, the same role rule on overrides,
the same mode check, the same model-version resolver for a scoring file, and the same validators
through `engine.agent.checks.check_plan` - so the two cannot disagree (Plan G §9, M71).
"""

from __future__ import annotations

import numbers
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import numpy as np
import pandas as pd
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import Field, JsonValue

from api.access import set_audit_context
from api.access_policy import RoutePolicy, register
from api.deps import ConfigRootDep, StorageDep, get_registry
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
    RecipeStepKind,
    SessionStatus,
    recipe_hash,
)
from engine.agent.formats import masked_cut
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
from engine.contracts import FeatureSchema, ModelVersion, ValidationReport
from engine.generative.budget import Meter
from engine.generative.contracts import LlmUsageReport
from engine.generative.guardrails import Guardrails, load_policy
from engine.llm import build_client
from engine.pii import redact_text
from engine.stages import ingest
from engine.storage import Storage, StorageError, upload_key
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
        prepared = prepare_in_memory(storage, config, upload, recipe)
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
            if any(step.kind is RecipeStepKind.COMBINE_ROWS for step in recipe.steps):
                # Combined rows are other rows: the profile the helper reads must describe them.
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
    with _session_lock(upload_id):
        session = _load_session(storage, upload_id)
        ctx = _load_context(
            storage, root, request, upload_id, session.use_case_id, session.model_version_id
        ).ctx
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
    with _session_lock(upload_id):
        session = _load_session(storage, upload_id)
        ctx = _load_context(
            storage, root, request, upload_id, session.use_case_id, session.model_version_id
        ).ctx
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
        meter = Meter(
            build_client(generative.llm, profile=profile_in_force()),
            job_id=session.session_id,
            llm=generative.llm,
            budget=generative.budget,
        )
        guardrails = Guardrails(load_policy(root), meter=meter, prompts_root=root)
        text = redact_text(body.text)[0]
        turn = chat_turn(ctx, session, text, meter=meter, guardrails=guardrails, config_root=root)
        asked = ChatMessage(role=ChatRole.USER, text=text, created_at=utc_now())
        session = session.model_copy(
            update={
                "transcript": (*session.transcript, asked, turn.reply),
                "tool_results": (*session.tool_results, *turn.tool_results),
                "proposals": (*session.proposals, *turn.proposals),
                "llm_calls": session.llm_calls + turn.llm_calls,
            }
        )
        session = with_summary(session, ctx)
        usage_key = upload_key(upload_id, LLM_USAGE_FILENAME)
        earlier = storage.read_model(usage_key, LlmUsageReport) if storage.exists(usage_key) else None
        storage.write_model(usage_key, add_usage(earlier, meter.usage()))
        return _response(_save(storage, session), ctx.config)


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


PREVIEW_CELL_CHARS: Final[int] = 60


def _cell(value: Any) -> str:
    """One preview cell as text. Numbers and dates are formatted, never run through the text masker:
    a float's digits (0.020000000000000002) would otherwise read as a phone number.

    Every digit a person needs to tell two values apart is kept: an integer in full, a float to 15
    significant digits (so 20240001 and 20240002 never both read `2.024e+07`). A narrower float (a
    Parquet `float32`) is first written at its own precision, so 0.1 stays `0.1` rather than the
    `0.100000001490116` it is as a double. Text is masked before it is cut, so an e-mail address split
    by the cut is still one the masker knows; a marker the cut would split is left out whole.
    """
    if value is None or value is pd.NA or value is pd.NaT or (isinstance(value, float) and value != value):
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, numbers.Integral):
        return str(int(value))
    if isinstance(value, (int, float)) or hasattr(value, "dtype"):
        try:
            dtype = getattr(value, "dtype", None)
            if getattr(dtype, "kind", "") == "f" and getattr(dtype, "itemsize", 8) < 8:
                value = float(str(value))  # numpy's shortest text at the value's own precision
            number = float(value)
            return "" if number != number else format(number, ".15g")
        except (TypeError, ValueError):
            pass
    if hasattr(value, "isoformat"):
        text = str(value.isoformat())
        return text[:10] if text.endswith("T00:00:00") else text
    return masked_cut(str(value), PREVIEW_CELL_CHARS)


def _rows(frame: Any, columns: tuple[str, ...], personal: set[str]) -> tuple[tuple[str, ...], ...]:
    head = frame.head(PREVIEW_ROWS)
    # Column by column, not `iterrows`: a row Series would turn a float32 into a double (0.1 into
    # 0.100000001490116) before `_cell` could write it at its own precision.
    cells = {
        name: (
            ("[personal data]",) * len(head)
            if name in personal
            else tuple(_cell(value) for value in _values(head[name]))
        )
        for name in columns
    }
    return tuple(tuple(cells[name][index] for name in columns) for index in range(len(head)))


def _values(series: Any) -> list[Any]:
    """The column's values with their own scalar types: numpy scalars for a numpy column."""
    if isinstance(series.dtype, np.dtype):
        return list(series.to_numpy())
    return list(series.array)


def _preview_sample(frame: Any, recipe: DataRecipe | None) -> Any:
    """The rows a preview runs on: the first `PREVIEW_SAMPLE_ROWS` rows, or - when the recipe combines
    rows per entity - every row of the first `PREVIEW_SAMPLE_ROWS` entities, so no entity is combined
    from part of its rows."""
    combine = next(
        (s for s in (recipe.steps if recipe else ()) if s.kind is RecipeStepKind.COMBINE_ROWS), None
    )
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
    Run button's checks with the accepted settings; a 409 carries them when they would fail.

    A scoring file is prepared by the model's own saved recipe, exactly as `POST /runs` would
    (`replay_for_scoring`), and checked as prepared; Run then reuses that prepared upload."""
    with _session_lock(upload_id):
        return _apply_agent_session(upload_id, root, storage, request)


def _apply_agent_session(
    upload_id: str, root: Path, storage: Storage, request: Request
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
    key, target = roles_of(session)
    recipe = _accepted_recipe(session, upload, requested_by(request), ctx.config)
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
    _save(storage, applied.model_copy(update={"updated_at": utc_now()}))
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
