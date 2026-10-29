"""Ask your data (Plan I, DEC-1250 ... DEC-1259): an upload's read-only chat with the data helper.

`GET`/`DELETE /uploads/{upload_id}/ask` read and clear the chat; `POST .../ask/messages` asks a question. The
helper looks things up in the file as it was sent and cannot change anything. The Guided-setup session routes
that used to share this module were removed with Guided setup (the Data Doctor replaced it).
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

from fastapi import APIRouter, HTTPException, Request
from pydantic import Field

from api.access import current_principal
from api.access_policy import RoutePolicy, register
from api.deps import ConfigRootDep, StorageDep, get_settings
from api.routes.ai_service import resolve_slot
from api.routes.runs import read_frame
from api.routes.uploads import http_error, load_upload, load_upload_profile, profile_row_cap, use_case_config
from api.schemas import ErrorResponse, UploadRecord
from engine.access.roles import Role
from engine.agent.config import DataAccess
from engine.agent.contracts import AgentSession, ChatMessage, ChatRole, SessionStatus
from engine.agent.egress import cap_sent, is_third_party
from engine.agent.explore import EXPLORE_SESSION_FILENAME, RateChart, charts_of, start_explore
from engine.agent.loop import chat_turn
from engine.agent.scope import agent_available
from engine.agent.tools import AgentContext
from engine.ai_consent import client_scope
from engine.ai_service import effective_service
from engine.config import RunMode, StrictBase, UseCaseConfig
from engine.generative.budget import Meter
from engine.generative.contracts import LlmUsageReport
from engine.generative.guardrails import Guardrails, load_policy
from engine.pii import redact_text
from engine.storage import Storage, upload_key
from engine.utils.time import utc_now

__all__ = ["AskResponse", "router"]

router = APIRouter(tags=["agent"])

LLM_USAGE_FILENAME: Final[str] = "agent/llm_usage.json"

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


class MessageRequest(StrictBase):
    text: str = Field(min_length=1, max_length=1_000)


def _refuse(status: int, code: str, message: str) -> HTTPException:
    return http_error(status, code, message)


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


EXPLORE_KEY: Final[str] = f"agent/{EXPLORE_SESSION_FILENAME}"

register(
    {
        # Reading the chat is a read (DEC-716); asking spends tokens and starting over deletes (DEC-1253).
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


def _chat_availability(config: UseCaseConfig, storage: Storage, request: Request) -> ChatAvailability:
    """What the chat box may do: the Product AI's effective provider decides it (not the use case's
    `generative.llm.backend`); with none connected the chat is `available: false` (DEC-1140)."""
    effective = effective_service(
        config.generative.llm, slot="product", storage=storage, settings=get_settings(request)
    )
    if effective is None:
        return ChatAvailability(
            available=False,
            reason="AI_NOT_CONNECTED",
            backend="none",
            data_access=config.agent.ai_data_access,
            third_party=False,
        )
    return ChatAvailability(
        backend=effective.provider,
        provider_label=effective.label,
        generation_model_id=effective.llm.generation_model_id or None,
        data_access=config.agent.ai_data_access,
        third_party=is_third_party(effective.llm, effective.provider),
    )


def _ask_response(
    session: AgentSession | None, config: UseCaseConfig, storage: Storage, request: Request
) -> AskResponse:
    """The chat, what its box may do, and the charts of every reply."""
    return AskResponse(
        session=session,
        chat=_chat_availability(config, storage, request),
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
