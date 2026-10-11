"""Optional second opinions from the AI service inside Guided setup (Plan K M142, DEC-1409).

Two, both off unless an AI service is connected, both going through what every chat turn goes through:
the default-deny egress gate (`engine.agent.egress`), the rendered-prompt backstop (`assert_clean`),
the call meter (`GenerativePurpose.DATA_AGENT_ASSIST`) and the session's own call limit.

* **Rerank.** The rules read the headers (`engine.agent.headers`) and list up to three candidates per
  role. The model may put each list in a better order. `headers.reorder_within` is the only way its
  answer is applied, so a column the rules did not list is dropped, a repeat counts once and a role it
  leaves out keeps the rules' order. The new order reaches the person as the order of an open question's
  buttons and is recorded as evidence (`rerank_columns`); it never turns a suggestion into an answer.
* **Leak reviewer.** The deterministic `LEAKAGE_SUSPECTED` check hides a column that predicts the outcome
  almost perfectly and asks the person about it. The reviewer may name up to three more columns whose
  *names* suggest they are recorded after the outcome, and each becomes one more question beside the
  rule's: "Hide it?". It never replaces the rule, never names a column the rule already asked about, the
  ID, the outcome or a column hidden for personal data, and its sentence reaches the person only after
  `numbers_grounded` and the guardrails (otherwise a fixed sentence is shown).

Nothing here ticks, accepts, hides or changes anything: the output is a reordering and questions whose
"Hide it" button adds a pending suggestion only when the person presses it (`session.answer`).
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from engine.agent.contracts import (
    AgentConfidence,
    AgentSession,
    ChatMessage,
    ChatRole,
    Proposal,
    ProposalKind,
    Question,
    QuestionOption,
    RecipeStep,
    RecipeStepKind,
    SentItem,
    ToolResult,
    TurnLog,
)
from engine.agent.egress import EGRESS_LATE_MASK, MAX_SENT_ITEMS, Egress, assert_clean, scrub, sent_item
from engine.agent.grounding import evidence_numbers, grounded_numbers
from engine.agent.headers import HeaderCandidate, reorder_within
from engine.agent.loop import FALLBACK, OUT_OF_BUDGET, UNAVAILABLE, _Checker
from engine.agent.session import roles_of, with_summary
from engine.agent.tools import AgentContext
from engine.agent.untrusted import display_name, for_prompt, resolve_column
from engine.generative.contracts import GenerativePurpose
from engine.generative.errors import GenerativeError
from engine.generative.guardrails import Guardrails
from engine.generative.prompts import load_prompt, render
from engine.llm import LLMError
from engine.utils.logging import get_logger
from engine.utils.time import utc_now

__all__ = [
    "ASSIST_PROMPT",
    "LEAK_REVIEW_TOOL",
    "MAX_LEAK_QUESTIONS",
    "RERANK_TOOL",
    "AssistResult",
    "assist_session",
]

_LOGGER = get_logger(__name__)

ASSIST_PROMPT: Final[str] = "data_agent_assist"
RERANK_TOOL: Final[str] = "rerank_columns"
LEAK_REVIEW_TOOL: Final[str] = "review_leaks"
MAX_LEAK_QUESTIONS: Final[int] = 3
MAX_WHY_CHARS: Final[int] = 200
MAX_ASSIST_PROMPT_CHARS: Final[int] = 60_000
GENERIC_WHY: Final[str] = "Its name suggests it may be recorded after the outcome."
"""What the person reads when the model's own sentence fails a check."""
_RESULT_TEXT: Final[frozenset[str]] = frozenset({"label"})


@dataclass(frozen=True)
class AssistResult:
    """The session after the second opinions, and what happened (for the response and for tests)."""

    session: AgentSession
    llm_calls: int
    reordered: tuple[str, ...]
    """Roles whose order the model changed."""
    leak_questions: int
    blocked_by: str | None


def _parse(text: str) -> dict[str, Any] | None:
    """The reply as a JSON object, or None; a code fence around it is tolerated."""
    body = text.strip()
    if body.startswith("```"):
        body = body.strip("`")
        body = body[body.find("{") :]
    try:
        value = json.loads(body)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _latest(session: AgentSession, tool: str) -> ToolResult | None:
    found = [r for r in session.tool_results if r.tool == tool]
    return found[-1] if found else None


def _candidates(session: AgentSession) -> dict[str, list[HeaderCandidate]]:
    """The rules' short list per role, from the session's own `find_roles` evidence."""
    roles = _latest(session, "find_roles")
    out: dict[str, list[HeaderCandidate]] = {}
    entries = roles.result.get("header_candidates") if roles is not None else None
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        reads = str(entry.get("label", "")).removeprefix("reads as ")
        out.setdefault(str(entry["kind"]), []).append(
            HeaderCandidate(
                column=str(entry["column"]),
                role=str(entry["kind"]),
                score=float(str(entry["score"])),
                confidence=str(entry["confidence"]),
                reads_as=tuple(w.strip() for w in reads.split(",") if w.strip()),
            )
        )
    return out


def _facts(ctx: AgentContext) -> dict[str, dict[str, Any]]:
    """Counts the profile measured for each column: all the model sees of the data besides names."""
    return {
        c.name: {"type": c.inferred_type.value, "distinct": c.distinct_count, "empty": c.null_count}
        for c in ctx.profile.columns
    }


def _render(
    ctx: AgentContext,
    session: AgentSession,
    task: str,
    payload: str,
    root: Path | None,
) -> tuple[Any, bool]:
    """The prompt for one task and whether the backstop had to mask something in it."""
    agent = ctx.config.agent
    knowledge = list(agent.knowledge) or [ctx.config.target.definition or ctx.config.description]
    rendered = render(
        load_prompt(ASSIST_PROMPT, root),
        {
            "agent_name": session.agent_name,
            "goal": agent.goal_for(ctx.config.target.definition),
            "entity": ctx.config.entity,
            "knowledge": knowledge,
            "task": task,
            "payload": payload,
        },
    )
    system, masked_system = assert_clean(rendered.system)
    user, masked_user = assert_clean(rendered.user)
    late = bool(masked_system or masked_user)
    if late:
        rendered = dataclasses.replace(rendered, system=system, user=user)
    return rendered, late


def _gate(ctx: AgentContext) -> Egress:
    agent = ctx.config.agent
    return Egress.build(
        tuple(str(c) for c in ctx.frame.columns),
        personal_columns=[column.name for column in ctx.profile.columns if column.pii_kinds],
        always_hide=agent.always_hide_columns,
        mode=agent.ai_data_access,
    )


def _payload(gate: Egress, ctx: AgentContext, value: Mapping[str, Any]) -> str:
    names = {str(column): gate.label(str(column)) for column in ctx.frame.columns}
    return json.dumps(
        for_prompt(gate.prepare(dict(value), extra_text_keys=_RESULT_TEXT), names), ensure_ascii=False
    )


# ---------------------------------------------------------------------------
# Rerank
# ---------------------------------------------------------------------------
def _rerank_payload(ctx: AgentContext, ranked: Mapping[str, Sequence[HeaderCandidate]]) -> dict[str, Any]:
    facts = _facts(ctx)
    return {
        "header_candidates": [
            {
                "kind": role,
                "column": candidate.column,
                "score": candidate.score,
                "label": "reads as " + ", ".join(candidate.reads_as),
                **facts.get(candidate.column, {}),
            }
            for role, candidates in ranked.items()
            for candidate in candidates
        ]
    }


def _applied_orders(
    reply: Mapping[str, Any] | None,
    ranked: Mapping[str, Sequence[HeaderCandidate]],
    gate: Egress,
    ctx: AgentContext,
) -> dict[str, tuple[HeaderCandidate, ...]]:
    """Each role's candidates in the model's order, where it gave one; the rules' order everywhere else."""
    orders = reply.get("orders") if reply is not None else None
    proposed: dict[str, list[str]] = {}
    for entry in orders if isinstance(orders, list) else []:
        if not isinstance(entry, dict) or entry.get("kind") not in ranked:
            continue
        names = entry.get("columns")
        if not isinstance(names, list):
            continue
        resolved = (
            resolve_column(str(gate.unalias(str(name))), ctx.frame.columns)
            for name in names
            if isinstance(name, str)
        )
        proposed.setdefault(str(entry["kind"]), []).extend(c for c in resolved if c is not None)
    return {role: reorder_within(ranked[role], proposed.get(role, [])) for role in ranked}


def _reordered_questions(
    questions: Sequence[Question], orders: Mapping[str, Sequence[HeaderCandidate]]
) -> tuple[Question, ...]:
    """Open questions that offer outcome or ID columns, their buttons in the model's order.

    Only the order of the same options changes: no option is added, removed or edited, and an answered
    question is left as the person left it.
    """
    out: list[Question] = []
    for question in questions:
        role = {o.proposal.path for o in question.options if o.proposal is not None and o.proposal.path}
        kind = {"target": "outcome", "primary_key": "id"}.get(next(iter(role), ""), "")
        if question.answer is not None or kind not in orders or len(role) != 1:
            out.append(question)
            continue
        position = {c.column: i for i, c in enumerate(orders[kind])}
        options = sorted(
            question.options,
            key=lambda o: (
                position.get(str(o.proposal.value), len(position))
                if o.proposal is not None
                else len(position)
            ),
        )
        out.append(question.model_copy(update={"options": tuple(options)}))
    return tuple(out)


# ---------------------------------------------------------------------------
# Leak reviewer
# ---------------------------------------------------------------------------
def _already_asked(session: AgentSession) -> set[str]:
    """Columns a question already offers to hide (the rule's leak question or an earlier review)."""
    return {
        o.proposal.step.column
        for q in session.questions
        for o in q.options
        if o.proposal is not None
        and o.proposal.step is not None
        and o.proposal.step.kind is RecipeStepKind.DROP_COLUMN
    } | {
        p.step.column
        for p in session.proposals
        if p.step is not None and p.step.kind is RecipeStepKind.DROP_COLUMN
    }


def _reviewable(ctx: AgentContext, session: AgentSession) -> list[str]:
    """Columns the reviewer may name: not the ID, the outcome, a column already hidden or asked about."""
    key, target = roles_of(session)
    hidden = {name.split(":", 1)[0] for name in session.engine_hidden}
    skip = {key, target, ctx.target, *_already_asked(session), *hidden}
    personal = {column.name for column in ctx.profile.columns if column.pii_kinds}
    always = {name.casefold() for name in ctx.config.agent.always_hide_columns}
    return [
        str(c)
        for c in ctx.frame.columns
        if str(c) not in skip and str(c) not in personal and str(c).casefold() not in always
    ]


def _leak_payload(ctx: AgentContext, columns: Sequence[str], target: str | None) -> dict[str, Any]:
    facts = _facts(ctx)
    return {
        "outcome_column": target,
        "columns": [{"column": name, **facts.get(name, {})} for name in columns],
    }


def _leak_question(column: str, why: str, evidence_id: str, question_id: str, proposal_id: str) -> Question:
    shown = display_name(column)
    return Question(
        question_id=question_id,
        text=f"The AI service's second opinion: '{shown}' may be recorded after the outcome. {why} Hide it?",
        options=(
            QuestionOption(
                option_id="hide",
                label="Hide it",
                effect="The model will not see this column.",
                proposal=Proposal(
                    proposal_id=proposal_id,
                    kind=ProposalKind.RECIPE_STEP,
                    title=f"Hide '{shown}'",
                    reason="You chose to hide it after the AI service's second opinion.",
                    step=RecipeStep(order=1, kind=RecipeStepKind.DROP_COLUMN, column=column),
                    evidence_ids=(evidence_id,),
                    confidence=AgentConfidence.CHECK,
                ),
            ),
            QuestionOption(
                option_id="keep",
                label="Keep it, it is known in advance",
                effect="Nothing changes.",
            ),
        ),
        blocking=False,
        evidence_ids=(evidence_id,),
    )


def _leak_findings(
    reply: Mapping[str, Any] | None, allowed: Sequence[str], gate: Egress, checker: _Checker
) -> list[tuple[str, str]]:
    """(column, sentence) for each column the model named that it may name, at most `MAX_LEAK_QUESTIONS`."""
    named = reply.get("columns") if reply is not None else None
    found: list[tuple[str, str]] = []
    for entry in named if isinstance(named, list) else []:
        if not isinstance(entry, dict) or not isinstance(entry.get("column"), str):
            continue
        column = resolve_column(gate.unalias(entry["column"]), allowed)
        if column is None or column in {c for c, _ in found}:
            continue
        said = gate.unalias(str(entry.get("why") or "")).strip()[:MAX_WHY_CHARS]
        why = scrub(said)
        # A sentence that held personal data (the scrub changed it), an invented number or a banned phrase
        # is not shown; the fixed sentence is.
        if (
            not why
            or why != said
            or checker.failure(why, target="helper suggestion", max_chars=MAX_WHY_CHARS)
        ):
            why = GENERIC_WHY
        found.append((column, why))
        if len(found) == MAX_LEAK_QUESTIONS:
            break
    return found


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------
def assist_session(
    ctx: AgentContext,
    session: AgentSession,
    *,
    meter: Any,
    guardrails: Guardrails,
    config_root: Path | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> AssistResult:
    """Ask the AI service for the two second opinions and fold them into `session`.

    Never raises for a model's mistake: a reply that is not what was asked for changes nothing. An AI
    service that does not answer, or a session out of calls, stops here with the session as it was.
    """
    agent = ctx.config.agent
    remaining = agent.max_llm_calls_per_session - session.llm_calls
    gate = _gate(ctx)
    earlier = {
        r.evidence_id.split("-")[0] for r in session.tool_results if r.tool in {RERANK_TOOL, LEAK_REVIEW_TOOL}
    }
    prefix = f"x{len(earlier) + 1}-"  # one prefix per round, so ids never repeat
    columns = tuple(str(c) for c in ctx.frame.columns)
    names = {c: gate.label(c) for c in columns}
    checker = _Checker(
        grounded=grounded_numbers(
            [
                *(f"{p.title} {p.reason}" for p in session.proposals),
                *(q.text for q in session.questions),
                *session.engine_hidden,
                *session.assumptions,
            ]
        ).union(*(evidence_numbers(r.result, r.typed_args) for r in session.tool_results)),
        names=(*columns, *names.values()),
        guardrails=guardrails,
    )
    calls = 0
    actions: list[str] = []
    errors: list[str] = []
    sent: list[SentItem] = []
    results: list[ToolResult] = []
    questions = session.questions
    new_questions: list[Question] = []
    reordered: list[str] = []
    blocked: str | None = None

    def ask(task: str, value: Mapping[str, Any], tool: str) -> dict[str, Any] | None:
        nonlocal calls, blocked
        if calls >= remaining:
            blocked = "budget"
            return None
        payload = _payload(gate, ctx, value)
        rendered, late = _render(ctx, session, task, payload, config_root)
        if late:
            errors.append(EGRESS_LATE_MASK)
            _LOGGER.warning("egress: an assist prompt was masked late")
        if len(rendered.system) + len(rendered.user) > MAX_ASSIST_PROMPT_CHARS:
            blocked = "prompt_too_large"
            return None
        sent.append(sent_item(gate, tool, {}, payload, extra_text_keys=_RESULT_TEXT))
        try:
            completion = meter.complete(rendered, GenerativePurpose.DATA_AGENT_ASSIST)
        except (LLMError, GenerativeError):
            blocked = "unavailable"
            return None
        calls += 1
        actions.append(tool)
        return _parse(completion.text)

    ranked = {role: c for role, c in _candidates(session).items() if len(c) >= 2}
    if ranked:
        reply = ask("rerank", _rerank_payload(ctx, ranked), RERANK_TOOL)
        if blocked is None:
            orders = _applied_orders(reply, ranked, gate, ctx)
            changed = [r for r, o in orders.items() if [c.column for c in o] != [c.column for c in ranked[r]]]
            reordered.extend(changed)
            results.append(
                ToolResult(
                    evidence_id=f"{prefix}e1",
                    tool=RERANK_TOOL,
                    args={},
                    result={
                        "header_candidates": [
                            {"kind": role, "column": c.column, "score": c.score, "confidence": c.confidence}
                            for role, ordered in orders.items()
                            for c in ordered
                        ]
                    },
                    created_at=clock(),
                )
            )
            if changed:
                questions = _reordered_questions(questions, {r: orders[r] for r in changed})
    if blocked is None:
        allowed = _reviewable(ctx, session)
        if allowed:
            _, target = roles_of(session)
            reply = ask("leaks", _leak_payload(ctx, allowed, target), LEAK_REVIEW_TOOL)
            if blocked is None:
                found = _leak_findings(reply, allowed, gate, checker)
                evidence_id = f"{prefix}e{len(results) + 1}"
                results.append(
                    ToolResult(
                        evidence_id=evidence_id,
                        tool=LEAK_REVIEW_TOOL,
                        args={},
                        result={"columns": [column for column, _ in found], "rows": len(found)},
                        created_at=clock(),
                    )
                )
                new_questions = [
                    _leak_question(column, why, evidence_id, f"{prefix}q{i}", f"{prefix}p{i}")
                    for i, (column, why) in enumerate(found, start=1)
                ]
    if not results and blocked is not None:
        text = {"budget": OUT_OF_BUDGET, "unavailable": UNAVAILABLE}.get(blocked, FALLBACK)
    else:
        text = _summary(reordered, new_questions)
    message = ChatMessage(
        role=ChatRole.AGENT,
        text=gate.person_text(text),
        evidence_ids=tuple(r.evidence_id for r in results),
        turn=TurnLog(llm_calls=calls, tools=tuple(actions), error_codes=tuple(errors), blocked_by=blocked),
        sent=tuple(sent[:MAX_SENT_ITEMS]),
        created_at=clock(),
    )
    if calls == 0 and not results:
        return AssistResult(session, 0, (), 0, blocked)
    updated = session.model_copy(
        update={
            "questions": (*questions, *new_questions),
            "tool_results": (*session.tool_results, *results),
            "transcript": (*session.transcript, message),
            "llm_calls": session.llm_calls + calls,
        }
    )
    return AssistResult(
        with_summary(updated, ctx, clock=clock), calls, tuple(reordered), len(new_questions), blocked
    )


def _summary(reordered: Iterable[str], questions: Sequence[Question]) -> str:
    parts: list[str] = []
    changed = list(reordered)
    if changed:
        parts.append(
            "I put the likely columns for "
            + ", ".join(r.replace("_", " ") for r in changed)
            + " in a new order."
        )
    if questions:
        parts.append(
            f"I added {len(questions)} question{'s' if len(questions) != 1 else ''} about columns that may be "
            "recorded after the outcome; nothing is hidden unless you say so."
        )
    return " ".join(parts) or "I looked again and have nothing to add to the rules' suggestions."
