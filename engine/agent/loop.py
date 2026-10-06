"""One chat turn with the helper (Plan G §8.4, DEC-1001): a JSON-action loop over `LLMClient.complete`.

The model replies with one JSON action per call - a read tool, `propose_setting`, or `reply` - and
the engine does the rest: it validates the action, runs the tool or checks the setting, feeds the
result back, and stops after `agent.max_tool_steps_per_turn` steps. The model never writes data:
the only change it can cause is a *pending* setting proposal, validated by the same schema rules as
the advisor's, for the person to approve.

What the person reads is checked before it is kept:

* **`numbers_grounded`** (`engine.agent.grounding`): every number must appear in this session's
  evidence, the advisor's proposals and questions on screen, or the person's own message. What the
  model itself wrote earlier - the reason it gave for a suggested setting - is never evidence, so a
  number cannot be laundered through one step into the next (M77);
* **the platform's guardrails** (`engine.generative.guardrails`): personal data, banned phrases and
  length, as for every generated text. The reason given with a suggested setting passes the same
  two checks, and is replaced by a plain sentence when it fails (M77);
* a reply that fails either is replaced by a plain sentence and the advisor's first open item,
  and the rule that failed is recorded.

What reaches the prompt is treated as untrusted (M77): the person's message is masked
(`engine.agent.egress.scrub`, which includes `engine.pii.redact_text`), and every string from the
file - column names, cell examples, check messages - passes **one default-deny gate**,
`engine.agent.egress` (Plan G §7.6): a string is a label only under a known key, every other string
is a cell value that is masked (or, in `summaries_only`, reduced to its shape), personal-data and
`always_hide_columns` columns show no value, and a column name that is not ordinary words becomes an
alias the reply translates back. The gate output then passes `engine.agent.untrusted.for_prompt`,
which strips invisible and reordering characters, cuts long names and long lists, and the prompt says
that file content is data, never instructions. The rendered prompt is re-scanned last
(`egress.assert_clean`): anything that still matches a scanner is masked and recorded as
`EGRESS_LATE_MASK`. A prompt that would still be longer than `MAX_PROMPT_CHARS` is not sent. Each
reply records what was put in its prompts (`ChatMessage.sent`).

A malformed reply, an unknown tool or bad arguments are fed back once as an error the model can
correct; a second failure ends the turn with the same plain fallback. Anything else a step raises
ends the turn plainly too (`AGENT_TURN_FAILED`), so the calls already made are returned and metered
rather than lost in a 500 (review fix). The session is never left
half-changed. Every call goes through `Meter`, so cost is metered by purpose (`data_agent`), and
the session's own `agent.max_llm_calls_per_session` stops a runaway chat. Each reply carries a
`TurnLog` - calls, actions, refusals and the rule that replaced it - and no content: an action name
that is not a known tool, `propose_setting` or `reply` is logged as `unknown`, never verbatim.

An *Ask your data* session (`AgentSession.explore`, Plan I DEC-1250/1251) runs the same turn read-only:
no setting is listed, the state block is one fixed sentence, and `propose_setting` is refused with
`AGENT_EXPLORE_READ_ONLY` like any other refused action.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable, Iterable
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
    SentItem,
    ToolResult,
    TurnLog,
)
from engine.agent.egress import (
    EGRESS_LATE_MASK,
    MAX_SENT_ITEMS,
    STATE_TOOL,
    Egress,
    assert_clean,
    scrub,
    sent_item,
)
from engine.agent.explore import EXPLORE_NOTE
from engine.agent.grounding import evidence_numbers, grounded_numbers, ungrounded_numbers
from engine.agent.recommend import setting_allowed, settings_fields
from engine.agent.session import roles_of
from engine.agent.tools import TOOLS, AgentContext, AgentToolError, ToolKind, call_tool
from engine.agent.untrusted import display_name, for_prompt, resolve_column
from engine.config import ConfigError, advanced_settings_schema, resolve_config
from engine.generative.contracts import GenerativePurpose
from engine.generative.errors import GenerativeError
from engine.generative.guardrails import CheckContext, Guardrails
from engine.generative.prompts import load_prompt, render
from engine.llm import LLMError
from engine.utils.logging import get_logger
from engine.utils.time import utc_now

__all__ = [
    "CHAT_REASON",
    "HELPER_PROMPT",
    "MAX_PROMPT_CHARS",
    "MAX_REPLY_CHARS",
    "PROPOSE_TOOL",
    "TurnResult",
    "chat_turn",
]

_LOGGER = get_logger(__name__)

HELPER_PROMPT: Final[str] = "data_agent"
PROPOSE_TOOL: Final[str] = "propose_setting"
MAX_REPLY_CHARS: Final[int] = 800
MAX_MESSAGE_CHARS: Final[int] = 1_000
MAX_REASON_CHARS: Final[int] = 200
MAX_PROMPT_CHARS: Final[int] = 60_000
"""A rendered prompt (system and user) longer than this is not sent; the turn ends plainly."""
NEVER_SUGGESTED_BY_CHAT: Final[frozenset[str]] = frozenset({"prepare.exclude_columns"})
"""Hiding a column is a recipe step the person approves from the advisor, never a chat setting."""

FALLBACK: Final[str] = "I could not answer that from what I have checked."
TURN_FAILED: Final[str] = "AGENT_TURN_FAILED"
"""What ended a turn whose step raised something unexpected; the calls it made are still counted."""
LOGGED_UNKNOWN: Final[str] = "unknown"
"""What the turn log records for an action name that is not a tool, `propose_setting` or `reply`."""
CHAT_REASON: Final[str] = "You asked for this change."
"""The reason shown with a chat suggestion whose own reason failed a check."""
STATE_TEXT_KEYS: Final[frozenset[str]] = frozenset(
    {"stop_reason", "id", "title", "reason", "state", "evidence_ids", "text", "hidden_columns"}
)
"""Keys of `_state` that hold sentences our own rules wrote (the gate still aliases and scans them)."""
SETTING_TEXT_KEYS: Final[frozenset[str]] = frozenset({"path", "value", "allowed"})
PROPOSE_TEXT_KEYS: Final[frozenset[str]] = frozenset({"path", "current", "suggested", "value"})
MAX_FIND_VALUES_PER_COLUMN_TURN: Final[int] = 5
MAX_FIND_VALUES_PER_COLUMN_SESSION: Final[int] = 15
"""`find_values` on one column: at most this many calls in one turn and in one session. A search is the
one tool whose answer depends on text the model chooses, so many searches of one column are how a value is
read out a piece at a time (review finding 7); a legitimate look needs a handful."""
EXPLORE_READ_ONLY: Final[str] = "AGENT_EXPLORE_READ_ONLY"
"""The refusal of `propose_setting` in an Ask-your-data session (DEC-1251): it only answers."""
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


def _logged(name: str) -> str:
    """The action name as the turn log keeps it: a known action, never text the model made up."""
    return name if name in TOOLS or name in {PROPOSE_TOOL, "reply"} else LOGGED_UNKNOWN


def _chat_made(session: AgentSession) -> frozenset[str]:
    """Ids of evidence the model's own `propose_setting` actions produced."""
    return frozenset(r.evidence_id for r in session.tool_results if r.tool == PROPOSE_TOOL)


def _advisor_proposals(session: AgentSession) -> list[Proposal]:
    """The proposals the rules made (or a person chose from a question), never the chat's own."""
    chat = _chat_made(session)
    return [p for p in session.proposals if not set(p.evidence_ids) & chat]


def _step_columns(proposal: Proposal) -> list[str]:
    return [proposal.step.column] if proposal.step is not None and proposal.step.column else []


def _state(session: AgentSession, gate: Egress) -> dict[str, Any]:
    """The advisor's suggestions and questions as the model may read them (§7.7).

    An Ask-your-data session has none: its state is one fixed sentence saying so (`EXPLORE_NOTE`).

    Each suggestion's sentences are made together (`Egress.sentences`): the cells it quotes become
    their shape or a hidden marker, and one that is about a hidden column hides every quoted example
    in all its sentences, not only in the one that names the column. The person's screen keeps the
    full text; this is the copy for the model.
    """
    if session.explore:
        return {"text": EXPLORE_NOTE, "proposals": [], "questions": [], "hidden_columns": []}
    proposals: list[dict[str, Any]] = []
    for p in session.proposals:
        title, reason = gate.sentences([p.title, p.reason], p.examples, columns=_step_columns(p))
        proposals.append(
            {
                "id": p.proposal_id,
                "title": title,
                "reason": reason,
                "state": p.state.value,
                "evidence_ids": list(p.evidence_ids),
            }
        )
    questions: list[dict[str, Any]] = []
    for q in session.questions:
        columns = [c for option in q.options if option.proposal for c in _step_columns(option.proposal)]
        (text,) = gate.sentences([q.text], q.examples, columns=columns)
        questions.append({"id": q.question_id, "text": text, "answered": q.answer is not None})
    return {
        "stop_reason": session.stop_reason,
        "proposals": proposals,
        "questions": questions,
        "hidden_columns": list(session.engine_hidden),
    }


COLUMN_CHOICES_NOTE: Final[str] = "one of the column names listed under 'Columns a setting may name' below"
_COLUMN_WIDGETS: Final[frozenset[str]] = frozenset({"column-select", "column-multi-select"})


def _column_valued(field: Any) -> bool:
    """A setting whose choices are the file's own column names (its headers are data, not our words)."""
    return bool(field.choices) and str(field.widget.value) in _COLUMN_WIDGETS


def _allowed_text(field: Any) -> str:
    if _column_valued(field):
        return COLUMN_CHOICES_NOTE
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
    """Each read tool with its whole argument schema: properties *and* which are required."""
    tools: list[dict[str, str]] = []
    for tool in TOOLS.values():
        if tool.kind is not ToolKind.READ:
            continue
        schema = tool.args_model.model_json_schema()
        args = {"properties": schema.get("properties", {}), "required": schema.get("required", [])}
        tools.append({"name": tool.name, "description": tool.description, "args": json.dumps(args)})
    return tools


def _parse(text: str) -> dict[str, Any]:
    body = text.strip()
    if body.startswith("```"):
        body = body.strip("`")
        body = body[body.find("{") :]
    try:
        value = json.loads(body)
    except (ValueError, RecursionError) as exc:  # a reply nested ~1,000 deep exhausts the decoder
        raise AgentToolError("AGENT_REPLY_MALFORMED", "Reply with one JSON object and nothing else.") from exc
    if not isinstance(value, dict) or not isinstance(value.get("action"), str):
        raise AgentToolError("AGENT_REPLY_MALFORMED", 'The object needs an "action".')
    return value


@dataclass
class _Checker:
    """The two checks every piece of model-written text passes: grounded numbers, then guardrails."""

    grounded: frozenset[float]
    names: tuple[str, ...]
    guardrails: Guardrails

    def failure(self, text: str, *, target: str, max_chars: int) -> str | None:
        if ungrounded_numbers(text, self.grounded, self.names):
            return "numbers_grounded"
        verdict = self.guardrails.check(text, CheckContext(target=target, max_chars=max_chars))
        return None if verdict.passed else (verdict.blocked_by or "guardrails")


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
    message = scrub(message[:MAX_MESSAGE_CHARS])
    results: list[ToolResult] = []
    proposals: list[Proposal] = []
    feedback: list[dict[str, Any]] = []
    actions: list[str] = []
    errors: list[str] = []
    calls = 0
    strikes = 0
    remaining = agent.max_llm_calls_per_session - session.llm_calls
    # An Ask-your-data session lists no setting: there is nothing the model could suggest (DEC-1251).
    settings, fields = ([], {}) if session.explore else _settings(ctx, session)
    prompt = load_prompt(HELPER_PROMPT, config_root)
    knowledge = list(agent.knowledge) or [ctx.config.target.definition or ctx.config.description]
    columns = tuple(str(c) for c in ctx.frame.columns)
    gate = Egress.build(
        columns,
        personal_columns=[column.name for column in ctx.profile.columns if column.pii_kinds],
        always_hide=agent.always_hide_columns,
        mode=agent.ai_data_access,
        known_text=[str(field.label) for field in fields.values()],
    )
    names = {column: gate.label(column) for column in columns}
    advisor = _advisor_proposals(session)
    chat = _chat_made(session)
    checker = _Checker(
        grounded=grounded_numbers(
            [
                *(f"{p.title} {p.reason}" for p in advisor),
                *(q.text for q in session.questions),
                *session.engine_hidden,
                *session.assumptions,
                message,
            ]
        ).union(
            *(
                evidence_numbers(r.result, r.typed_args)
                for r in session.tool_results
                if r.evidence_id not in chat
            )
        ),
        names=(*columns, *names.values()),
        guardrails=guardrails,
    )
    state = json.dumps(
        for_prompt(gate.prepare(_state(session, gate), extra_text_keys=STATE_TEXT_KEYS), names),
        ensure_ascii=False,
    )
    shown_settings = [
        for_prompt(gate.prepare(setting, extra_text_keys=SETTING_TEXT_KEYS), names) for setting in settings
    ]
    # The file's own headers a setting may name go in the user section, under the untrusted-data rule.
    column_choices = json.dumps(
        for_prompt(
            [
                gate.prepare(
                    {"path": path, "choices": [choice.value for choice in field.choices or ()]},
                    extra_text_keys=SETTING_TEXT_KEYS,
                )
                for path, field in fields.items()
                if _column_valued(field)
            ],
            names,
        ),
        ensure_ascii=False,
    )
    items: list[SentItem | None] = []  # aligned with `feedback`: what each tool result put in a prompt
    sent: list[SentItem] = []
    recorded: set[int] = set()
    # The helper's suggestions and questions quote real cells and name the columns; they go into every
    # prompt of the turn, so the person is told about them once, first.
    state_item = sent_item(
        gate, STATE_TOOL, {}, state + "\n" + column_choices, extra_text_keys=STATE_TEXT_KEYS
    )
    state_recorded = False

    def finish(text: str, evidence: tuple[str, ...] = (), blocked_by: str | None = None) -> TurnResult:
        log = TurnLog(llm_calls=calls, tools=tuple(actions), error_codes=tuple(errors), blocked_by=blocked_by)
        return TurnResult(
            reply=ChatMessage(
                role=ChatRole.AGENT,
                text=gate.person_text(text),  # aliases back to the file's own names, where those are clean
                evidence_ids=evidence,
                turn=log,
                sent=tuple(sent[:MAX_SENT_ITEMS]),
                created_at=clock(),
            ),
            tool_results=tuple(results),
            proposals=tuple(proposals),
            llm_calls=calls,
            blocked_by=blocked_by,
        )

    for step in range(agent.max_tool_steps_per_turn + 1):
        if calls >= remaining:
            return finish(OUT_OF_BUDGET, blocked_by="budget")
        variables = {
            "agent_name": session.agent_name,
            "goal": agent.goal_for(ctx.config.target.definition),
            "entity": ctx.config.entity,
            "knowledge": knowledge,
            "tools": _tools(),
            "settings": shown_settings,
            "state": state,
            "column_choices": column_choices,
            "message": message,
            "turn_results": (
                json.dumps(for_prompt(feedback, names), ensure_ascii=False) if feedback else "none"
            ),
        }
        rendered = render(prompt, variables)
        included = list(range(len(feedback)))
        if len(rendered.system) + len(rendered.user) > MAX_PROMPT_CHARS and feedback:
            # Too much looked up this turn: keep the newest result only, then give up if still too long.
            variables["turn_results"] = json.dumps(for_prompt(feedback[-1:], names), ensure_ascii=False)
            rendered = render(prompt, variables)
            included = [len(feedback) - 1]
        # The last check before anything leaves: whatever still matches a scanner is masked, not sent.
        system, masked_system = assert_clean(rendered.system)
        user, masked_user = assert_clean(rendered.user)
        if masked_system or masked_user:
            rendered = dataclasses.replace(rendered, system=system, user=user)
            errors.append(EGRESS_LATE_MASK)
            _LOGGER.warning("egress: a prompt was masked late (%d matches)", masked_system + masked_user)
        if len(rendered.system) + len(rendered.user) > MAX_PROMPT_CHARS:
            return finish(FALLBACK, blocked_by="prompt_too_large")
        if not state_recorded:
            state_recorded = True
            sent.append(state_item)
        for index in included:
            item = items[index]
            if item is not None and index not in recorded:
                recorded.add(index)
                sent.append(item)
        try:
            completion = meter.complete(rendered, GenerativePurpose.DATA_AGENT)
        except (LLMError, GenerativeError):
            return finish(UNAVAILABLE, blocked_by="unavailable")
        calls += 1
        try:
            action = _parse(completion.text)
            name = str(action["action"])
            actions.append(_logged(name))
            raw_args = action.get("args")
            args: dict[str, Any] = dict(gate.unalias_args(raw_args)) if isinstance(raw_args, dict) else {}
            if name == "reply":
                return _reply(session, action, results, checker, finish)
            if name == PROPOSE_TOOL and session.explore:
                raise AgentToolError(
                    EXPLORE_READ_ONLY,
                    "This chat only answers questions about the data; it cannot suggest a setting. Reply instead.",
                )
            if name == PROPOSE_TOOL:
                result, proposal = _propose(
                    ctx,
                    args,
                    fields,
                    checker,
                    f"{prefix}e{len(results) + 1}",
                    f"{prefix}p{len(proposals) + 1}",
                    clock,
                )
                results.append(result)
                proposals.append(proposal)
                _record(gate, names, feedback, items, result, PROPOSE_TEXT_KEYS)
                continue
            tool = TOOLS.get(name)
            if tool is None or tool.kind is not ToolKind.READ:
                raise AgentToolError("AGENT_TOOL_UNKNOWN", f"There is no tool called {name[:40]!r}.")
            if name == "find_values":
                _limit_searches(ctx, session, results, args)
            result = call_tool(ctx, name, args, evidence_id=f"{prefix}e{len(results) + 1}")
            results.append(result)
            checker.grounded = checker.grounded | evidence_numbers(result.result, result.typed_args)
            _record(gate, names, feedback, items, result, frozenset())
        except AgentToolError as exc:
            errors.append(exc.code)
            strikes += 1
            if strikes > 1:
                return finish(FALLBACK, blocked_by=exc.code)
            feedback.append(gate.prepare({"error": exc.code, "message": exc.message}))
            items.append(None)
        except Exception as exc:
            # A model's odd input must never cost a paid call that nobody records (DEC-1017, DEC-1029).
            _LOGGER.warning("agent chat step failed: %s", type(exc).__name__)
            errors.append(TURN_FAILED)
            return finish(FALLBACK, blocked_by=TURN_FAILED)
        del step
    return finish(FALLBACK, blocked_by="too_many_steps")


def _limit_searches(
    ctx: AgentContext, session: AgentSession, results: list[ToolResult], args: dict[str, Any]
) -> None:
    """Refuse one more `find_values` on a column that was searched `MAX_FIND_VALUES_PER_COLUMN_*` times."""
    column = args.get("column")
    if not isinstance(column, str):
        return  # the tool reports bad arguments itself
    name = ctx.resolve(column)

    def searches(found: Iterable[ToolResult]) -> int:
        return sum(
            1
            for r in found
            if r.tool == "find_values"
            and isinstance(r.args.get("column"), str)
            and resolve_column(str(r.args["column"]), ctx.frame.columns) == name
        )

    if (
        searches(results) >= MAX_FIND_VALUES_PER_COLUMN_TURN
        or searches(session.tool_results) + searches(results) >= MAX_FIND_VALUES_PER_COLUMN_SESSION
    ):
        raise AgentToolError(
            "AGENT_TOOL_LIMIT",
            f"find_values has been used on '{display_name(name)}' as often as one session allows; "
            "use value_counts or sample_rows instead.",
        )


def _record(
    gate: Egress,
    names: dict[str, str],
    feedback: list[dict[str, Any]],
    items: list[SentItem | None],
    result: ToolResult,
    text_keys: frozenset[str],
) -> None:
    """Put one tool result in this turn's feedback through the gate, and note what will be sent."""
    prepared = gate.prepare(result.result, extra_text_keys=text_keys)
    feedback.append({"evidence_id": result.evidence_id, "tool": result.tool, "result": prepared})
    payload = json.dumps(for_prompt(prepared, names), ensure_ascii=False)
    items.append(sent_item(gate, result.tool, result.args, payload, extra_text_keys=text_keys))


def _propose(
    ctx: AgentContext,
    args: dict[str, Any],
    fields: dict[str, Any],
    checker: _Checker,
    evidence_id: str,
    proposal_id: str,
    clock: Callable[[], datetime],
) -> tuple[ToolResult, Proposal]:
    path, value = str(args.get("path", "")), args.get("value")
    if path in NEVER_SUGGESTED_BY_CHAT or not setting_allowed(path, value, fields) or path not in fields:
        raise AgentToolError(
            "AGENT_SETTING_NOT_ALLOWED",
            f"{path[:80]} = {value!r:.80} is not a setting the helper may suggest.",
        )
    try:
        resolve_config(ctx.use_case_id, {path: value}, root=ctx.config_root)
    except ConfigError as exc:
        raise AgentToolError(exc.code, exc.message) from exc
    reason = str(args.get("reason") or CHAT_REASON).strip()[:MAX_REASON_CHARS] or CHAT_REASON
    if checker.failure(reason, target="helper suggestion", max_chars=MAX_REASON_CHARS) is not None:
        reason = CHAT_REASON  # the model's own words are shown only when they pass the reply's checks
    field = fields[path]
    shown = display_name(value) if isinstance(value, str) else value
    result = ToolResult(
        evidence_id=evidence_id,
        tool=PROPOSE_TOOL,
        args={"path": path, "value": value},
        result={"path": path, "current": field.value, "suggested": value, "label": field.label},
        created_at=clock(),
    )
    proposal = Proposal(
        proposal_id=proposal_id,
        kind=ProposalKind.SETTING,
        title=f"Change '{field.label}' to {shown}",
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
    session: AgentSession,
    action: dict[str, Any],
    results: list[ToolResult],
    checker: _Checker,
    finish: Callable[..., TurnResult],
) -> TurnResult:
    text = str(action.get("text") or "").strip()
    raw = action.get("evidence_ids")  # anything but a list of strings cites nothing
    cited = tuple(e for e in raw if isinstance(e, str)) if isinstance(raw, list) else ()
    known = {r.evidence_id for r in (*session.tool_results, *results)}
    evidence = tuple(e for e in cited if e in known)
    if not text:
        return finish(FALLBACK, blocked_by="empty_output")
    failure = checker.failure(text, target="helper reply", max_chars=MAX_REPLY_CHARS)
    if failure is not None:
        return finish(_fallback(session), blocked_by=failure)
    return finish(text, evidence)


def _fallback(session: AgentSession) -> str:
    pending = next((p for p in session.proposals if p.state is ProposalState.PENDING), None)
    if pending is None:
        return FALLBACK
    return f"{FALLBACK} Still to decide: {pending.title}."
