"""The AI review as a detector: an adapter that runs one :class:`~engine.doctor.agent_turn.DoctorTurn` for the visit.

The visit (`engine.doctor.visit`, `VisitDeps.agent`) still runs the AI as one more check after the rules' TREAT pass and
reads a :class:`~engine.doctor.visit.Detected`. This module is that port over the one turn: it does the checks the
visit expects of a detector (no consent or no spending left: it finds nothing; the state it already finished on: it is
not asked again) and turns the turn's result into `Detected`. It goes when the visit runs the turn itself.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from engine.agent.contracts import AgentConfidence, ProposalState
from engine.agent.tools import AgentContext
from engine.doctor.agent_turn import DoctorTurn, TurnResult
from engine.doctor.subjects import WITHDRAW_KIND

if TYPE_CHECKING:
    from pathlib import Path

    from engine.access.roles import Principal
    from engine.doctor.ai_guard import AiGuard
    from engine.doctor.case import Case, CaseItem
    from engine.doctor.diagnose import LoadedFile
    from engine.doctor.goal_brief import GoalBrief
    from engine.doctor.visit import Detected, DetectorPort
    from engine.generative.guardrails import Guardrails
    from engine.storage import Storage

__all__ = ["REVIEW_OWED", "agent_review", "lazy_review", "pending"]

REVIEW_OWED = (
    "The rules have made their pass. Look through every table, propose what the plan is missing and ask only what "
    "the data cannot say."
)


def lazy_review(build: Callable[[], DetectorPort | None]) -> DetectorPort:
    """The review, connected to the AI service only when TREAT is about to run it; no AI service: it finds nothing."""
    built: list[DetectorPort | None] = []

    def review(case: Case, files: tuple[LoadedFile, ...], brief: GoalBrief | None) -> Detected:
        from engine.doctor.visit import Detected

        if not built:
            built.append(build())
        port = built[0]
        return Detected() if port is None else port(case, files, brief)

    return review


def pending(item: CaseItem) -> CaseItem:
    """An item as the visit takes it from the AI: waiting for a tick and not decided, whatever the author made of it
    (the visit judges the placement itself until it runs the turn). A swap is told to the visit by the id it takes out,
    not by `replaces`, except for a request to take an item out, which carries the item in `replaces`."""
    return item.model_copy(
        update={
            "state": ProposalState.PENDING,
            "confidence": AgentConfidence.CHECK,
            "decided_by": None,
            "decided_at": None,
            **({} if item.kind == WITHDRAW_KIND else {"replaces": None}),
        }
    )


def agent_review(
    context_for: Callable[[Case, tuple[LoadedFile, ...]], AgentContext],
    *,
    meter: AiGuard,
    guardrails: Guardrails,
    principal: Principal,
    config_root: Path | None = None,
    registry: Any = None,
    storage: Storage | None = None,
) -> DetectorPort:
    """The review as a detector. `context_for` gives one file's `AgentContext` (as CONVERSE's port does)."""

    def review(case: Case, files: tuple[LoadedFile, ...], brief: GoalBrief | None) -> Detected:
        from engine.doctor.visit import Detected, visit_registry

        if not files or meter.ask(case) != "ok":
            return Detected()  # no consent or no spending left: the rules alone, said on the screen
        turn = DoctorTurn(
            case,
            files,
            brief,
            context_for,
            meter,
            guardrails,
            principal,
            config_root,
            registry or visit_registry(),
            chat_steps=meter.chat_steps,
            storage=storage,
            owed=(REVIEW_OWED,),
        )
        digest = _digest(turn.state)
        if case.visit is not None and case.visit.done.get("agent_state") == digest:
            # The state the AI reads is the one it already finished on (a tick that changes nothing it sees): asking
            # again would only bring the same answer back.
            return Detected(unchanged=True, state_digest=digest)
        return _detected(turn.run(), digest)

    return review


def _digest(state: dict[str, Any]) -> str:
    """A digest of what the review has always read of the state: the files, the goal, the changes that are about a
    file and column (with their receipts), the questions still open and the blocking room. An answer, a note or a
    change of no file does not make the same review worth asking again; the visit's `owed` replaces this."""
    from engine.agentic.canonical import sha256_hex

    shown = {
        "files": state["files"],
        "goal": state.get("goal"),
        "goals_that_fit": state.get("goals_that_fit"),
        "plan": [
            {k: row[k] for k in ("file", "kind", "column", "by", "state", "receipt") if k in row}
            for row in state["plan"]
            if "file" in row
        ],
        "open_questions": [q["text"] for q in state["questions"] if q["answer"] is None],
        "left": state["left"],
    }
    return sha256_hex(json.dumps(shown, sort_keys=True, ensure_ascii=False, default=str).encode())[:16]


def _detected(done: TurnResult, digest: str) -> Detected:
    from engine.doctor.visit import Detected

    # A turn whose first call the AI service did not answer did not happen: the visit says why and asks again later.
    failed = done.blocked_by == "unavailable" and done.answered == 0
    items = tuple(pending(i) for i in done.items if i.kind != WITHDRAW_KIND)
    questions = len(done.asks) + len(done.requests)
    said = done.reply if done.finished else ""  # a turn that stopped short has no words of the model's
    return Detected(
        asks=done.asks,
        doubts=done.doubts,
        requests=done.requests,
        items=items,
        evidence=done.evidence,
        summary=said
        or (_summary(len(items), questions, len(done.doubts)) if items or questions or done.doubts else ""),
        sent=done.sent,
        blocked_by="unavailable" if failed else None,
        error_codes=done.error_codes if failed else (),
        reply=done.reply if failed else "",
        state_digest=digest if done.finished else "",
        model_calls=done.model_calls,
        tokens=done.tokens,
    )


def _summary(items: int, questions: int, doubts: int = 0) -> str:
    """The review in our own words, when the model's summary is missing."""
    unsure = f", and {doubts} thing{'s' if doubts != 1 else ''} it is not sure about" if doubts else ""
    return (
        f"I looked through your files with the AI: {items} suggested change{'s' if items != 1 else ''} "
        f"and {questions} question{'s' if questions != 1 else ''}{unsure}. Nothing is ticked; you decide."
    )
