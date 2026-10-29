"""The Data Doctor's chat as a port: an adapter that runs one :class:`~engine.doctor.agent_turn.DoctorTurn` for the visit.

The visit (`engine.doctor.visit`, `VisitDeps.converse`) still answers a person's message through a
:class:`~engine.doctor.visit.ConversePort` and reads a :class:`~engine.doctor.visit.ConverseResult`. This module is that
port over the one turn: the client's yes and the case's spending are checked first (they answer in words and ask
nothing), then the turn runs and its result is handed over as the visit takes it. It goes when the visit runs the turn
itself.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from engine.agent.tools import AgentContext
from engine.doctor.agent_review import pending
from engine.doctor.agent_turn import DoctorTurn
from engine.generative.errors import BUDGET_EXCEEDED

if TYPE_CHECKING:
    from pathlib import Path

    from engine.access.roles import Principal
    from engine.doctor.ai_guard import AiGuard
    from engine.doctor.case import Case
    from engine.doctor.diagnose import LoadedFile
    from engine.doctor.visit import ConversePort, ConverseResult
    from engine.generative.guardrails import Guardrails
    from engine.storage import Storage

__all__ = ["doctor_converse"]


def doctor_converse(
    context_for: Callable[[Case, tuple[LoadedFile, ...]], AgentContext],
    *,
    meter: AiGuard,
    guardrails: Guardrails,
    principal: Principal,
    config_root: Path | None = None,
    registry: Any = None,
    storage: Storage | None = None,
) -> ConversePort:
    """CONVERSE as the Data Doctor's chat: one turn with the person's message in front of it.

    The AI is asked only when the client has allowed it and the case has spending left (`AiGuard.ask`); otherwise the
    reply says so and the visit goes on with its rules. A turn is `data_doctor.ai_limits.max_chat_steps` actions at most.

    The result says why the AI did not answer, when it did not (`ConverseResult.blocked_by`, `error_codes`): `no_consent`
    and `budget` for the two replies above, which ask nothing; and for a turn the AI service failed, `unavailable` with
    the codes of the failure (`LLM_UNAVAILABLE`, `LLM_KEY_EXPIRED` ...), or `budget` with `BUDGET_EXCEEDED` when the
    case's spending ran out between two calls. The reply then also tells the person the cause when the HTTP client has
    a fixed sentence for it.
    """

    def converse(case: Case, message: str, files: tuple[LoadedFile, ...]) -> ConverseResult:
        from engine.doctor.ai_guard import AI_LIMIT_REPLY, AI_NO_CONSENT_REPLY
        from engine.doctor.visit import ConverseResult, stored_brief, visit_registry

        allowed = meter.ask(case)
        if allowed != "ok":
            return ConverseResult(
                reply=AI_NO_CONSENT_REPLY if allowed == "no_consent" else AI_LIMIT_REPLY,
                ai=False,
                blocked_by="no_consent" if allowed == "no_consent" else "budget",
            )
        if not files:
            return ConverseResult(reply="Add a file first, and I will look at it with you.", ai=False)
        turn = DoctorTurn(
            case,
            files,
            stored_brief(storage, case) if storage is not None else None,
            context_for,
            meter,
            guardrails,
            principal,
            config_root,
            registry or visit_registry(),
            chat_steps=meter.chat_steps,
            storage=storage,
        )
        done = turn.run(message)
        # The case's limit speaks only when it is what stopped the turn (`BUDGET_EXCEEDED`: a call refused before it
        # was made). A call that failed and was billed anyway (the model ran out of room) can take the case to its
        # limit too, but then the reason is the failure's, and the limit is told on the next turn (`ask`).
        spent = done.blocked_by == "unavailable" and BUDGET_EXCEEDED in done.error_codes and meter.spent()
        return ConverseResult(
            reply=AI_LIMIT_REPLY if spent else done.reply,
            blocked_by="budget" if spent else done.blocked_by,
            error_codes=done.error_codes,
            actions=done.actions,
            items=_items(done.items),
            evidence=done.evidence,
            sent=done.sent,
            asks=done.asks,
            doubts=done.doubts,
            requests=done.requests,
            withdrawn=_withdrawn(done.items, done.withdrawn),
            suggestion=done.suggestion,
            suggestions=done.suggestions,
            goal=done.goal,
            tools=done.tools,
            model_calls=done.model_calls,
            tokens=done.tokens,
        )

    return converse


def _items(items: tuple[Any, ...]) -> tuple[Any, ...]:
    """The changes the turn put forward, for a reader of the older fields; the visit applies `actions`."""
    return tuple(pending(i) for i in items)


def _withdrawn(items: tuple[Any, ...], withdrawn: tuple[str, ...]) -> tuple[str, ...]:
    """Ids of the changes the turn took out or swapped, other than those it made itself."""
    made = {i.item_id for i in items}
    return tuple(i for i in withdrawn if i not in made)
