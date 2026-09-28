"""One chat turn with the helper (Plan G §8.4, DEC-1001): a JSON-action loop over `LLMClient.complete`.

The model replies with one JSON action per call - a read tool, `propose_setting`, or `reply` - and
the engine does the rest: it validates the action, runs the tool or checks the setting, feeds the
result back, and stops after `agent.max_tool_steps_per_turn` steps. The model never writes data:
the only change it can cause is a *pending* setting proposal, validated by the same schema rules as
the advisor's, for the person to approve.

What the person reads is checked before it is kept:

* **`numbers_grounded`** (`engine.agent.grounding`): every number must appear in this session's
  evidence, the proposals on screen or the person's own message;
* **the platform's guardrails** (`engine.generative.guardrails`): personal data, banned phrases and
  length, as for every generated text;
* a reply that fails either is replaced by a plain sentence and the advisor's first open item,
  and the rule that failed is recorded.

A malformed reply, an unknown tool or bad arguments are fed back once as an error the model can
correct; a second failure ends the turn with the same plain fallback. The session is never left
half-changed. Every call goes through `Meter`, so cost is metered by purpose (`data_agent`), and
the session's own `agent.max_llm_calls_per_session` stops a runaway chat.
"""

from __future__ import annotations

import json
from collections.abc import Callable
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
    ProposalState,
    ToolResult,
)
from engine.agent.grounding import grounded_numbers, ungrounded_numbers
from engine.agent.recommend import setting_allowed, settings_fields
from engine.agent.session import roles_of
from engine.agent.tools import TOOLS, AgentContext, AgentToolError, ToolKind, call_tool
from engine.config import ConfigError, advanced_settings_schema, resolve_config
from engine.generative.contracts import GenerativePurpose
from engine.generative.errors import GenerativeError
from engine.generative.guardrails import CheckContext, Guardrails
from engine.generative.prompts import load_prompt, render
from engine.llm import LLMError
from engine.utils.time import utc_now

__all__ = ["HELPER_PROMPT", "MAX_REPLY_CHARS", "TurnResult", "chat_turn"]

HELPER_PROMPT: Final[str] = "data_agent"
MAX_REPLY_CHARS: Final[int] = 800
MAX_MESSAGE_CHARS: Final[int] = 1_000
NEVER_SUGGESTED_BY_CHAT: Final[frozenset[str]] = frozenset({"prepare.exclude_columns"})
"""Hiding a column is a recipe step the person approves from the advisor, never a chat setting."""

FALLBACK: Final[str] = "I could not answer that from what I have checked."
UNAVAILABLE: Final[str] = (
    "The AI service is not answering right now. The suggestions on the screen still work without it."
)
OUT_OF_BUDGET: Final[str] = (
    "This setup has used all its questions to the AI service. The suggestions on the screen still work."
)


@dataclass(frozen=True)
class TurnResult:
    """What one message produced: the reply, the evidence gathered and any new pending proposal."""

    reply: ChatMessage
    tool_results: tuple[ToolResult, ...]
    proposals: tuple[Proposal, ...]
    llm_calls: int
    blocked_by: str | None


def _state(session: AgentSession) -> str:
    return json.dumps(
        {
            "stop_reason": session.stop_reason,
            "proposals": [
                {
                    "id": p.proposal_id,
                    "title": p.title,
                    "reason": p.reason,
                    "state": p.state.value,
                    "evidence_ids": list(p.evidence_ids),
                }
                for p in session.proposals
            ],
            "questions": [
                {"id": q.question_id, "text": q.text, "answered": q.answer is not None}
                for q in session.questions
            ],
            "hidden_columns": list(session.engine_hidden),
        },
        ensure_ascii=False,
    )


def _allowed_text(field: Any) -> str:
    if field.choices:
        return "choices: " + ", ".join(str(choice.value) for choice in field.choices)
    if field.min is not None or field.max is not None:
        return f"range {field.min} to {field.max}"
    return "true or false" if str(field.widget.value) == "checkbox" else "a column name"


def _settings(ctx: AgentContext, session: AgentSession) -> tuple[list[dict[str, str]], dict[str, Any]]:
    key, target = roles_of(session)
    schema = advanced_settings_schema(
        ctx.config, columns=tuple(str(c) for c in ctx.frame.columns), primary_key=key, target=target
    )
    fields = settings_fields(schema)
    listed = [
        {"path": path, "value": json.dumps(field.value), "allowed": _allowed_text(field)}
        for path, field in fields.items()
        if setting_allowed(path, field.value, fields) and path not in NEVER_SUGGESTED_BY_CHAT
    ]
    return listed, fields


def _tools() -> list[dict[str, str]]:
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "args": json.dumps(tool.args_model.model_json_schema().get("properties", {})),
        }
        for tool in TOOLS.values()
        if tool.kind is ToolKind.READ
    ]


def _parse(text: str) -> dict[str, Any]:
    body = text.strip()
    if body.startswith("```"):
        body = body.strip("`")
        body = body[body.find("{") :]
    try:
        value = json.loads(body)
    except ValueError as exc:
        raise AgentToolError("AGENT_REPLY_MALFORMED", "Reply with one JSON object and nothing else.") from exc
    if not isinstance(value, dict) or not isinstance(value.get("action"), str):
        raise AgentToolError("AGENT_REPLY_MALFORMED", 'The object needs an "action".')
    return value


def chat_turn(
    ctx: AgentContext,
    session: AgentSession,
    message: str,
    *,
    meter: Any,
    guardrails: Guardrails,
    config_root: Path | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> TurnResult:
    """Answer one message. Never raises for a model's mistake; the reply says what happened."""
    agent = ctx.config.agent
    prefix = f"c{len(session.transcript) // 2 + 1}-"
    results: list[ToolResult] = []
    proposals: list[Proposal] = []
    feedback: list[dict[str, Any]] = []
    calls = 0
    strikes = 0
    remaining = agent.max_llm_calls_per_session - session.llm_calls
    settings, fields = _settings(ctx, session)
    prompt = load_prompt(HELPER_PROMPT, config_root)
    knowledge = list(agent.knowledge) or [ctx.config.target.definition or ctx.config.description]

    def finish(text: str, evidence: tuple[str, ...] = (), blocked_by: str | None = None) -> TurnResult:
        return TurnResult(
            reply=ChatMessage(role=ChatRole.AGENT, text=text, evidence_ids=evidence, created_at=clock()),
            tool_results=tuple(results),
            proposals=tuple(proposals),
            llm_calls=calls,
            blocked_by=blocked_by,
        )

    for step in range(agent.max_tool_steps_per_turn + 1):
        if calls >= remaining:
            return finish(OUT_OF_BUDGET, blocked_by="budget")
        rendered = render(
            prompt,
            {
                "agent_name": session.agent_name,
                "goal": agent.goal_for(ctx.config.target.definition),
                "entity": ctx.config.entity,
                "knowledge": knowledge,
                "tools": _tools(),
                "settings": settings,
                "state": _state(session),
                "message": message[:MAX_MESSAGE_CHARS],
                "turn_results": json.dumps(feedback, ensure_ascii=False) if feedback else "none",
            },
        )
        try:
            completion = meter.complete(rendered, GenerativePurpose.DATA_AGENT)
        except (LLMError, GenerativeError):
            return finish(UNAVAILABLE, blocked_by="unavailable")
        calls += 1
        try:
            action = _parse(completion.text)
            name = str(action["action"])
            raw_args = action.get("args")
            args: dict[str, Any] = dict(raw_args) if isinstance(raw_args, dict) else {}
            if name == "reply":
                return _reply(ctx, session, message, action, results, proposals, guardrails, finish)
            if name == "propose_setting":
                result, proposal = _propose(
                    ctx, args, fields, f"{prefix}e{len(results) + 1}", f"{prefix}p{len(proposals) + 1}", clock
                )
                results.append(result)
                proposals.append(proposal)
                feedback.append(
                    {"evidence_id": result.evidence_id, "tool": "propose_setting", "result": result.result}
                )
                continue
            tool = TOOLS.get(name)
            if tool is None or tool.kind is not ToolKind.READ:
                raise AgentToolError("AGENT_TOOL_UNKNOWN", f"There is no tool called {name!r}.")
            result = call_tool(ctx, name, args, evidence_id=f"{prefix}e{len(results) + 1}")
            results.append(result)
            feedback.append({"evidence_id": result.evidence_id, "tool": name, "result": result.result})
        except AgentToolError as exc:
            strikes += 1
            if strikes > 1:
                return finish(FALLBACK, blocked_by=exc.code)
            feedback.append({"error": exc.code, "message": exc.message})
        del step
    return finish(FALLBACK, blocked_by="too_many_steps")


def _propose(
    ctx: AgentContext,
    args: dict[str, Any],
    fields: dict[str, Any],
    evidence_id: str,
    proposal_id: str,
    clock: Callable[[], datetime],
) -> tuple[ToolResult, Proposal]:
    path, value = str(args.get("path", "")), args.get("value")
    reason = str(args.get("reason") or "You asked for it.")[:200]
    if path in NEVER_SUGGESTED_BY_CHAT or not setting_allowed(path, value, fields) or path not in fields:
        raise AgentToolError(
            "AGENT_SETTING_NOT_ALLOWED", f"{path} = {value!r} is not a setting the helper may suggest."
        )
    try:
        resolve_config(ctx.use_case_id, {path: value}, root=ctx.config_root)
    except ConfigError as exc:
        raise AgentToolError(exc.code, exc.message) from exc
    field = fields[path]
    result = ToolResult(
        evidence_id=evidence_id,
        tool="propose_setting",
        args={"path": path, "value": value},
        result={"path": path, "current": field.value, "suggested": value, "label": field.label},
        created_at=clock(),
    )
    proposal = Proposal(
        proposal_id=proposal_id,
        kind=ProposalKind.SETTING,
        title=f"Change '{field.label}' to {value}",
        reason=reason,
        path=path,
        value=value,
        suggested_value=value,
        evidence_ids=(evidence_id,),
        confidence=AgentConfidence.CHECK,
        state=ProposalState.PENDING,
    )
    return result, proposal


def _reply(
    ctx: AgentContext,
    session: AgentSession,
    message: str,
    action: dict[str, Any],
    results: list[ToolResult],
    proposals: list[Proposal],
    guardrails: Guardrails,
    finish: Callable[..., TurnResult],
) -> TurnResult:
    text = str(action.get("text") or "").strip()
    cited = tuple(str(e) for e in action.get("evidence_ids", []) or [] if isinstance(e, str))
    known = {r.evidence_id for r in (*session.tool_results, *results)}
    evidence = tuple(e for e in cited if e in known)
    grounded = grounded_numbers(
        [
            *(r.result for r in (*session.tool_results, *results)),
            *(f"{p.title} {p.reason}" for p in (*session.proposals, *proposals)),
            *(q.text for q in session.questions),
            *session.engine_hidden,
            *session.assumptions,
            message,
        ]
    )
    if not text:
        return finish(FALLBACK, blocked_by="empty_output")
    if ungrounded_numbers(text, grounded):
        return finish(_fallback(session), blocked_by="numbers_grounded")
    verdict = guardrails.check(text, CheckContext(target="helper reply", max_chars=MAX_REPLY_CHARS))
    if not verdict.passed:
        return finish(_fallback(session), blocked_by=verdict.blocked_by)
    del ctx
    return finish(text, evidence)


def _fallback(session: AgentSession) -> str:
    pending = next((p for p in session.proposals if p.state is ProposalState.PENDING), None)
    if pending is None:
        return FALLBACK
    return f"{FALLBACK} Still to decide: {pending.title}."
