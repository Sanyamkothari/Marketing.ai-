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

When the AI service does not answer, the turn ends with `blocked_by="unavailable"` and says why twice: the codes
(`ai_failure_codes`: the exception's own, and for the HTTP clients `LLM_` and the kind, such as `LLM_KEY_EXPIRED`) join
`TurnLog.error_codes`, and the reply adds the client's own fixed sentence and fix (`unavailable_reply`). Nothing the
service wrote is kept: the sentences are `engine.llm_http.PROBLEMS` (or, for a lapsed AWS sign-in, the ones of
`engine.llm.AwsSignInExpiredError`), chosen by the kind. A call the provider billed and then could not use (a model that
spent its whole allowance thinking) still counts as a call of the turn (`billed_call`), as the meter counts it.

An *Ask your data* session (`AgentSession.explore`, Plan I DEC-1250/1251) runs the same turn read-only:
no setting is listed, the state block is one fixed sentence, and `propose_setting` is refused with
`AGENT_EXPLORE_READ_ONLY` like any other refused action.

Plan O M232 (DEC-1802): the turn calls every tool through Data Doctor's registry
(`engine.doctor.registry`): the tools listed to the model are the registry's READ tools for the AI in this
session, a read is `session_call` and a suggestion is `call`, with the turn's own handlers bound to it
(`_RegistryTurn`). The registry checks the person's role on every call (`TOOL_ROLE_REQUIRED`) when the route
passes who is asking (`principal`). Everything else - the ids, the prompts, the results - is what it was.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from engine.access.roles import Principal
from engine.agent.contracts import (
    AgentConfidence,
    AgentSession,
    ChatMessage,
    ChatRole,
    Proposal,
    ProposalKind,
    ProposalSource,
    ProposalState,
    RecipeStep,
    RecipeStepKind,
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
from engine.agent.proposals import roles_of
from engine.agent.recommend import setting_allowed, settings_fields
from engine.agent.tools import TOOLS, AgentContext, AgentToolError, ToolKind
from engine.agent.untrusted import display_name, for_prompt, resolve_column
from engine.config import ConfigError, advanced_settings_schema, resolve_config
from engine.doctor.limits import limits
from engine.generative.contracts import GenerativePurpose
from engine.generative.errors import GenerativeError
from engine.generative.guardrails import CheckContext, Guardrails
from engine.generative.prompts import load_prompt, render
from engine.llm import AwsSignInExpiredError, LLMError
from engine.llm_http import LLMHttpError
from engine.utils.logging import get_logger
from engine.utils.time import utc_now

__all__ = [
    "CHAT_REASON",
    "HELPER_PROMPT",
    "MAX_PROMPT_CHARS",
    "MAX_REPLY_CHARS",
    "PROPOSE_TOOL",
    "STEP_TOOL",
    "TurnResult",
    "ai_failure_codes",
    "billed_call",
    "chat_turn",
    "unavailable_reply",
]

_LOGGER = get_logger(__name__)

HELPER_PROMPT: Final[str] = "data_agent"
PROPOSE_TOOL: Final[str] = "propose_setting"
STEP_TOOL: Final[str] = "propose_step"
"""Plan L M152 (DEC-1520): the chat suggests a data fix from the fixed list, checked like every step."""
CHAT_STEP_KINDS: Final[frozenset[RecipeStepKind]] = frozenset(
    {
        RecipeStepKind.PARSE_NUMBER,
        RecipeStepKind.PARSE_DATE,
        RecipeStepKind.MAP_BOOLEAN,
        RecipeStepKind.NORMALISE_TEXT,
        RecipeStepKind.SET_MISSING,
        RecipeStepKind.MAP_VALUES,
        RecipeStepKind.SET_MISSING_TEXT,
        RecipeStepKind.CONVERT_UNITS,
        RecipeStepKind.SPLIT_COLUMN,
        RecipeStepKind.VALID_RANGE,
        RecipeStepKind.FILL_MISSING,
        RecipeStepKind.RENAME_COLUMN,
        RecipeStepKind.DROP_COLUMN,
    }
)
"""What Guided setup's chat may suggest. Combining rows and formulas stay the rules'; correcting a cell and
leaving rows out stay the person's own (DEC-1520)."""
DOCTOR_STEP_KINDS: Final[frozenset[RecipeStepKind]] = CHAT_STEP_KINDS | {RecipeStepKind.REMOVE_ROWS}
"""What the Data Doctor's chat may suggest: the same, and leaving out the rows a rule names ("drop the test
accounts"). Leaving rows out is an APPROVE-class change, so the person's own yes at the approval stands."""
STEP_SAMPLE_ROWS: Final[int] = 1_000
"""Rows a chat suggestion is tried on before it is shown, so its effect is measured, not claimed."""
CHAT_STEP_TITLES: Final[dict[RecipeStepKind, str]] = {
    RecipeStepKind.PARSE_NUMBER: "Turn '{c}' into numbers",
    RecipeStepKind.PARSE_DATE: "Read '{c}' as dates",
    RecipeStepKind.MAP_BOOLEAN: "Read '{c}' as yes/no",
    RecipeStepKind.NORMALISE_TEXT: "Tidy the spellings in '{c}'",
    RecipeStepKind.SET_MISSING: "Treat codes in '{c}' as missing",
    RecipeStepKind.MAP_VALUES: "Merge spellings in '{c}' that mean the same",
    RecipeStepKind.SET_MISSING_TEXT: "Treat words meaning 'no value' in '{c}' as empty",
    RecipeStepKind.CONVERT_UNITS: "Put '{c}' in one unit",
    RecipeStepKind.SPLIT_COLUMN: "Take part of '{c}' into a new column",
    RecipeStepKind.VALID_RANGE: "Treat impossible values in '{c}' as unknown",
    RecipeStepKind.FILL_MISSING: "Fill the empty cells of '{c}'",
    RecipeStepKind.RENAME_COLUMN: "Rename '{c}'",
    RecipeStepKind.DROP_COLUMN: "Hide '{c}'",
    RecipeStepKind.REMOVE_ROWS: "Leave out rows by '{c}'",
}
_TURN: Final = limits().turn  # `data_doctor.limits.turn` of the engine config: Guided setup shares them
MAX_REPLY_CHARS: Final[int] = _TURN.guided_reply_chars
MAX_MESSAGE_CHARS: Final[int] = 1_000
MAX_REASON_CHARS: Final[int] = 200
MAX_PROMPT_CHARS: Final[int] = _TURN.prompt_chars
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


def ai_failure_codes(exc: LLMError | GenerativeError) -> tuple[str, ...]:
    """The codes that say why a call to the AI service failed: for `TurnLog.error_codes` and the Doctor's chat.

    The exception's own `code` first (`LLM_UNAVAILABLE`, `LLM_REFUSED`, `BUDGET_EXCEEDED`), then - for a failure that
    knows its cause: the HTTP clients' (:class:`engine.llm_http.LLMHttpError`) and Bedrock's lapsed sign-in
    (:class:`engine.llm.AwsSignInExpiredError`) - `LLM_` and the upper-cased kind (`LLM_KEY_EXPIRED`,
    `LLM_OUT_OF_ROOM`, `LLM_RATE_LIMITED`, `LLM_AWS_SIGN_IN_EXPIRED`). The second is what tells an expired key from an
    unreachable server, which share `LLM_UNAVAILABLE`; when it repeats the first (`LLM_TOO_LONG`) it is not listed
    twice. Both are written in this repository, never taken from anything the service answered (`engine.llm_http`,
    rule 2).
    """
    codes = [exc.code]
    if isinstance(exc, LLMHttpError | AwsSignInExpiredError):
        codes.append(f"LLM_{exc.kind.upper()}")
    return tuple(dict.fromkeys(codes))


def unavailable_reply(exc: LLMError | GenerativeError) -> str:
    """What a person reads when the AI service did not answer: :data:`UNAVAILABLE`, then why when we can say it.

    A failure of the HTTP clients carries a sentence and a fix that `engine.llm_http.PROBLEMS` holds ("The AI service
    says the key has expired. Create a new key ..."), and a lapsed AWS sign-in has its own fixed pair
    (:class:`engine.llm.AwsSignInExpiredError`), so those are added. No other failure adds its own words: an
    `LLMError` may be raised by any client with any text (the Bedrock one names the AWS profile it was given) and a
    `GenerativeError` fills a template with names from the data, so for those the reply stays the lead alone and the
    reason is the code in the turn's log.
    """
    if isinstance(exc, LLMHttpError | AwsSignInExpiredError):
        return f"{UNAVAILABLE} {exc.message} {exc.fix}"
    return UNAVAILABLE


def billed_call(exc: LLMError | GenerativeError) -> int:
    """1 for a failed call the provider billed anyway (`LLMHttpError.billed`), else 0: what to add to a turn's calls.

    The meter has already counted it (`engine.generative.budget.Meter.complete`); the turn's own count, which is
    what `agent.max_llm_calls_per_session` and the case's counters are made of, must agree, or a person who retries
    a model that cannot answer is never stopped by the session's ceiling.
    """
    return int(isinstance(exc, LLMHttpError) and exc.billed)


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
    return name if name in TOOLS or name in {PROPOSE_TOOL, STEP_TOOL, "reply"} else LOGGED_UNKNOWN


def _chat_made(session: AgentSession) -> frozenset[str]:
    """Ids of evidence the model's own `propose_setting` and `propose_step` actions produced."""
    return frozenset(r.evidence_id for r in session.tool_results if r.tool in {PROPOSE_TOOL, STEP_TOOL})


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

    def why(self, text: str, failure: str, *, person: bool = False) -> str:
        """`failure` in words the AI can act on: for ungrounded numbers, which ones and how to fix them. With
        `person`, in words for the person instead."""
        if failure != "numbers_grounded":
            return "it did not pass a safety check" if person else f"The text failed a check ({failure})."
        if person:  # the number itself is not repeated: it is the one nobody checked
            return "my answer quoted a number that I have not looked up in this case"
        loose = list(dict.fromkeys(ungrounded_numbers(text, self.grounded, self.names)))
        shown = ", ".join(f"'{token}'" for token in loose[:5]) or "a number"
        return (
            f"The text states {shown}, which no tool result, earlier message or the case state shows. Remove it, "
            "or call a look tool first and quote what it returns; a number you worked out yourself is not accepted."
        )


def chat_turn(
    ctx: AgentContext,
    session: AgentSession,
    message: str,
    *,
    meter: Any,
    guardrails: Guardrails,
    config_root: Path | None = None,
    clock: Callable[[], datetime] = utc_now,
    principal: Principal | None = None,
    case_files: Sequence[tuple[dict[str, Any], SentItem]] = (),
) -> TurnResult:
    """Answer one message. Never raises for a model's mistake; the reply says what happened.

    `principal` is who is asking; the registry checks their role on every
    tool call (Plan O M232). `case_files` are the case's other files, each already
    made safe by its own gate (`engine.doctor.ai_guard.file_overviews`) with the note of what it sends.
    """
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
        shape_only=ctx.long_text,
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
    shown_state = for_prompt(gate.prepare(_state(session, gate), extra_text_keys=STATE_TEXT_KEYS), names)
    state = json.dumps(shown_state, ensure_ascii=False)
    prompt_state = json.dumps(
        (
            {**shown_state, "other_files": [overview for overview, _sent in case_files]}
            if case_files
            else shown_state
        ),
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
    doctor = _registry_turn(
        ctx, session, principal, gate, fields, checker, results, proposals, prefix, config_root, clock
    )

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
            "tools": doctor.tools(),
            "settings": shown_settings,
            "state": prompt_state,
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
            sent.extend(note for _overview, note in case_files)
        for index in included:
            item = items[index]
            if item is not None and index not in recorded:
                recorded.add(index)
                sent.append(item)
        try:
            completion = meter.complete(rendered, GenerativePurpose.DATA_AGENT)
        except (LLMError, GenerativeError) as exc:
            # Why, as codes in the turn's log and, when the client has its own fixed sentence, in the reply.
            # `blocked_by` stays `unavailable`: the Doctor's `chat_turn` tool reports it and callers key on it.
            codes = ai_failure_codes(exc)
            errors.extend(codes)
            calls += billed_call(exc)
            _LOGGER.warning("agent chat: the AI call failed (%s)", ", ".join(codes))
            return finish(unavailable_reply(exc), blocked_by="unavailable")
        calls += 1
        try:
            action = _parse(completion.text)
            name = str(action["action"])
            actions.append(_logged(name))
            raw_args = action.get("args")
            args: dict[str, Any] = dict(gate.unalias_args(raw_args)) if isinstance(raw_args, dict) else {}
            if name == "reply":
                return _reply(session, action, results, checker, finish)
            if name in {PROPOSE_TOOL, STEP_TOOL} and session.explore:
                raise AgentToolError(
                    EXPLORE_READ_ONLY,
                    "This chat only answers questions about the data; it cannot suggest a setting. Reply instead.",
                )
            if name in {PROPOSE_TOOL, STEP_TOOL}:
                result, proposal = doctor.propose(name, args)
                results.append(result)
                proposals.append(proposal)
                _record(
                    gate,
                    names,
                    feedback,
                    items,
                    result,
                    PROPOSE_TEXT_KEYS if name == PROPOSE_TOOL else STEP_TEXT_KEYS,
                )
                continue
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
            if name == STEP_TOOL:
                result, proposal = _propose_step(
                    ctx,
                    session,
                    args,
                    results,
                    checker,
                    f"{prefix}e{len(results) + 1}",
                    f"{prefix}p{len(proposals) + 1}",
                    clock,
                )
                results.append(result)
                proposals.append(proposal)
                _record(gate, names, feedback, items, result, STEP_TEXT_KEYS)
                continue
            if not doctor.reads(name):
                raise AgentToolError("AGENT_TOOL_UNKNOWN", f"There is no tool called {name[:40]!r}.")
            if name == "find_values":
                _limit_searches(ctx, session, results, args)
            evidence_id = f"{prefix}e{len(results) + 1}"
            result = doctor.read(name, args, evidence_id)
            results.append(result)
            checker.grounded = checker.grounded | evidence_numbers(result.result, result.typed_args)
            _record(gate, names, feedback, items, result, frozenset())
        except AgentToolError as exc:
            errors.append(exc.code)
            strikes += 1
            if strikes > 1:
                return finish(f"{FALLBACK} {refused_sentence(exc.message)}", blocked_by=exc.code)
            feedback.append(gate.prepare({"error": exc.code, "message": exc.message}))
            items.append(None)
        except Exception as exc:
            # A model's odd input must never cost a paid call that nobody records (DEC-1017, DEC-1029).
            _LOGGER.warning("agent chat step failed: %s", type(exc).__name__)
            errors.append(TURN_FAILED)
            return finish(FALLBACK, blocked_by=TURN_FAILED)
        del step
    return finish(FALLBACK, blocked_by="too_many_steps")


@dataclass
class _RegistryTurn:
    """One chat turn's binding to Data Doctor's registry (Plan O M232).

    The registry decides which tools the model may use here and whether `principal` may call them; Plan G's
    look tools run exactly as `call_tool` runs them, and the two suggestions run this turn's own handlers
    (`_propose`, `_propose_step`), bound through `propose` (the registry's `ChatTurn`).
    """

    registry: Any
    context: Any
    ctx: AgentContext
    session: AgentSession
    fields: dict[str, Any]
    checker: _Checker
    results: list[ToolResult]
    proposals: list[Proposal]
    prefix: str
    clock: Callable[[], datetime]
    made: list[tuple[ToolResult, Proposal]] = dataclasses.field(default_factory=list)

    def tools(self) -> list[dict[str, str]]:
        """What `_tools(ctx)` lists, from the registry: each READ tool this session's AI may call."""
        from engine.doctor.registry import DataToolKind

        listed: list[dict[str, str]] = []
        for tool in self.registry.listed_for(self.context, DataToolKind.READ):
            schema = tool.input_model.model_json_schema()
            args = {"properties": schema.get("properties", {}), "required": schema.get("required", [])}
            listed.append({"name": tool.name, "description": tool.description, "args": json.dumps(args)})
        return listed

    def reads(self, name: str) -> bool:
        from engine.doctor.registry import DataToolKind

        tool = self.registry.find(name, self.context)
        return tool is not None and tool.kind is DataToolKind.READ

    def read(self, name: str, args: dict[str, Any], evidence_id: str) -> ToolResult:
        result: ToolResult = self.registry.session_call(name, args, self.context, evidence_id=evidence_id)
        return result

    def propose(self, name: str, args: dict[str, Any]) -> tuple[ToolResult, Proposal]:
        """As the model: through the registry (role, input), which calls back into `ChatTurn.propose`.

        The registry decides who may suggest and records the suggestion; Plan G's handler gets `args` exactly
        as the model sent them (`_Bound` keeps them), never the registry's validated copy. Once the handler has
        made a suggestion, the registry's record of it cannot change or drop it. `made` is emptied whatever
        happens, so nothing from one call is left for the next.
        """
        from engine.doctor.tools import PROPOSE_ARG_FIELDS

        given = {key: value for key, value in args.items() if key in PROPOSE_ARG_FIELDS[name]}
        bound = dataclasses.replace(self.context, turn=_Bound(self, args))
        try:
            self.registry.call(name, given, bound)
        except (AgentToolError, ValueError):
            if not self.made:
                raise  # refused before or by the handler: the role, the input, or Plan G's own checks
        finally:
            made, self.made = self.made[-1:], []
        return made[0]

    def handle(self, name: str, args: dict[str, Any]) -> tuple[ToolResult, Proposal]:
        evidence_id = f"{self.prefix}e{len(self.results) + 1}"
        proposal_id = f"{self.prefix}p{len(self.proposals) + 1}"
        if name == PROPOSE_TOOL:
            made = _propose(self.ctx, args, self.fields, self.checker, evidence_id, proposal_id, self.clock)
        else:
            made = _propose_step(
                self.ctx,
                self.session,
                args,
                self.results,
                self.checker,
                evidence_id,
                proposal_id,
                self.clock,
            )
        self.made.append(made)
        return made


@dataclass(frozen=True)
class _Bound:
    """The registry's `ChatTurn`: its `propose` runs the turn's own handler on the model's own arguments."""

    turn: _RegistryTurn
    sent: dict[str, Any]
    """The arguments as the model sent them: what Plan G's handler reads, so nothing is stripped or reshaped."""

    def propose(self, name: str, args: dict[str, Any]) -> tuple[ToolResult, Proposal]:
        del args  # the registry's validated copy; the handler reads what the model sent
        return self.turn.handle(name, self.sent)


def _registry_turn(
    ctx: AgentContext,
    session: AgentSession,
    principal: Principal | None,
    gate: Egress,
    fields: dict[str, Any],
    checker: _Checker,
    results: list[ToolResult],
    proposals: list[Proposal],
    prefix: str,
    config_root: Path | None,
    clock: Callable[[], datetime],
) -> _RegistryTurn:
    """The turn's binding to Data Doctor's registry."""
    from engine.doctor import registry as doctor_registry

    context = doctor_registry.DataToolContext(
        agent=ctx,
        principal=principal,
        caller="ai",
        egress=gate,
        session=session,
        config_root=config_root,
        clock=clock,
    )
    return _RegistryTurn(
        registry=doctor_registry.data_registry(),
        context=context,
        ctx=ctx,
        session=session,
        fields=fields,
        checker=checker,
        results=results,
        proposals=proposals,
        prefix=prefix,
        clock=clock,
    )


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
    gate = _policy_gate(ctx, None, "data.setting", settings=fields)
    # Plan O M233 (DEC-1803): the setting allow-list, read from the data policy as well (same code).
    from engine.doctor.policy import SETTING, SettingChange

    gate(SETTING, setting=SettingChange(path=path, value=value))
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
        source=ProposalSource.AI,
    )
    return result, proposal


STEP_TEXT_KEYS: Final[frozenset[str]] = frozenset({"kind", "column"})


def _policy_gate(
    ctx: AgentContext, session: AgentSession | None, kind: str, *, settings: dict[str, Any] | None = None
) -> Callable[..., tuple[Any, ...]]:
    """Plan O M233 (DEC-1803): the data policy's check of `kind` for an AI suggestion.

    Each of this module's own checks is followed by the same rule read from `configs/agentic/policy.yaml`,
    raising the same code; a kind with no rule set is refused.
    """
    from engine.doctor.policy import agent_gate

    key, target = roles_of(session) if session is not None else (None, None)
    return agent_gate(
        ctx,
        kind,
        source="ai",
        primary_key=key,
        target=target,
        raise_as=AgentToolError,
        settings=settings,
    )


def _looked_at(result: ToolResult, column: str) -> bool:
    """True when a look-tool result is about `column` (named in its arguments)."""
    args = result.args
    named = [args.get(key) for key in ("column", "left", "right", "where_column")]
    listed = args.get("columns")
    return (
        column in [str(v) for v in named if v is not None]
        or (isinstance(listed, list) and column in [str(v) for v in listed])
        or (result.tool == "find_format_issues" and not listed)
    )


def _propose_step(
    ctx: AgentContext,
    session: AgentSession,
    args: dict[str, Any],
    turn_results: list[ToolResult],
    checker: _Checker,
    evidence_id: str,
    proposal_id: str,
    clock: Callable[[], datetime],
    kinds: frozenset[RecipeStepKind] = CHAT_STEP_KINDS,
) -> tuple[ToolResult, Proposal]:
    """A chat suggestion of one data fix: allowed, grounded in a look at the column, checked and measured.

    Refused with a code the model can correct (once) when the kind is not the chat's to suggest, the column
    holds personal data, no cited look-tool result is about the column, the recipe engine refuses the step,
    or the step changes nothing on the file's first `STEP_SAMPLE_ROWS` rows. A suggestion that passes is
    pending, `check`, labelled as the AI's, and never ticked for the person.
    """
    from engine.agent.advisor import ordered_steps, recipe_steps
    from engine.agent.recipe import EXCEL_SERIAL_PARAMS, RecipeError, check_recipe, run_recipe
    from engine.doctor import policy as data_policy

    try:
        kind = RecipeStepKind(str(args.get("kind", "")))
    except ValueError:
        kind = None
    if kind is None or kind not in kinds:
        raise AgentToolError(
            "AGENT_STEP_NOT_ALLOWED",
            f"{str(args.get('kind'))[:40]!r} is not a fix the chat may suggest. Allowed: "
            + ", ".join(sorted(k.value for k in kinds)),
        )
    # Plan O M233 (DEC-1803): each check below is followed by the same rule of the data policy
    # (`configs/agentic/policy.yaml`, `data.recipe_step`), with the same code.
    gate = _policy_gate(ctx, session, "data.recipe_step")
    gate(data_policy.STEP_KIND, steps=(data_policy.StepFacts(kind, str(args.get("column", ""))),))
    column = ctx.resolve(str(args.get("column", "")))
    personal = {c.name for c in ctx.profile.columns if c.pii_kinds} | set(
        ctx.config.agent.always_hide_columns
    )
    if column in personal and kind is not RecipeStepKind.DROP_COLUMN:
        raise AgentToolError("AGENT_STEP_NOT_ALLOWED", "A column holding personal data can only be hidden.")
    gate(data_policy.PERSONAL, steps=(data_policy.StepFacts(kind, column),))
    cited = (
        [str(e) for e in args.get("evidence_ids", []) if isinstance(e, str)]
        if isinstance(args.get("evidence_ids"), list)
        else []
    )
    known = {r.evidence_id: r for r in (*session.tool_results, *turn_results)}
    looks = [
        known[e] for e in cited if e in known and known[e].tool in TOOLS and _looked_at(known[e], column)
    ]
    if not looks:
        raise AgentToolError(
            "AGENT_STEP_UNGROUNDED",
            f"Look at '{display_name(column)}' with a tool first, and cite that result in evidence_ids.",
        )
    gate(data_policy.GROUNDED, steps=(data_policy.StepFacts(kind, column),), looked_at=bool(looks))
    raw_params = args.get("params")
    params: dict[str, Any] = dict(raw_params) if isinstance(raw_params, dict) else {}
    gated = sorted(EXCEL_SERIAL_PARAMS & params.keys())
    if gated:
        # Plan O M240: Excel day numbers are the Data Doctor's own fix (found by `find_excel_serial_dates`);
        # the chat has no such parameter: M233's `data.recipe_step` rule set does not allow it either (DEC-1803).
        raise AgentToolError("RECIPE_STEP_INVALID", f"Step 1 ({kind.value}): unknown parameter {gated[0]!r}")
    new_column = args.get("new_column")
    facts = data_policy.StepFacts(
        kind,
        column,
        params,
        str(new_column) if isinstance(new_column, str) and new_column.strip() else None,
    )
    gate(data_policy.STEP_PARAMS, data_policy.SPELLINGS, steps=(facts,), looked_at=True)
    try:
        step = RecipeStep(
            order=1,
            kind=kind,
            column=column,
            new_column=str(new_column) if isinstance(new_column, str) and new_column.strip() else None,
            params=params,
        )
    except ValueError as exc:
        raise AgentToolError("AGENT_TOOL_ARGS_INVALID", str(exc).splitlines()[-1][:200]) from exc
    key, target = roles_of(session)
    agent = ctx.config.agent
    accepted = recipe_steps(
        p
        for p in session.proposals
        if p.state is ProposalState.ACCEPTED and p.kind is ProposalKind.RECIPE_STEP
    )
    try:
        check_recipe(
            ordered_steps([*accepted, step]),
            columns=[str(c) for c in ctx.frame.columns],
            primary_key=key,
            target=target,
            levels=agent.levels,
        )
        gate(
            data_policy.PROTECTED,
            data_policy.LEVEL,
            steps=(data_policy.StepFacts.of(step),),
            looked_at=True,
        )
        trial = run_recipe(
            ctx.frame.head(STEP_SAMPLE_ROWS),
            [step],
            upload_id=ctx.upload_id,
            primary_key=key,
            target=target,
            levels=agent.levels,
            max_failure_pct=agent.max_conversion_failure_pct,
        )
    except RecipeError as exc:
        raise AgentToolError(exc.code, exc.message) from exc
    receipt = trial.receipt.steps[0]
    if receipt.changed == 0 and kind not in (RecipeStepKind.DROP_COLUMN, RecipeStepKind.RENAME_COLUMN):
        raise AgentToolError(
            "AGENT_STEP_NO_EFFECT",
            f"That fix changes nothing in the first {min(STEP_SAMPLE_ROWS, len(ctx.frame))} rows of "
            f"'{display_name(column)}'. Look again, or reply instead.",
        )
    # Tried on the first rows (the trial above), held to decision O10, never ticked: the approval's
    # "new patterns, with permission" for an AI's fix.
    effect = data_policy.effect_of(
        step, ctx.frame.head(STEP_SAMPLE_ROWS), changed=receipt.changed, failed=receipt.failed
    )
    # Plan O M246 (DEC-1816): O10's warning (above 30% of the column) travels with the suggestion.
    warned = gate(
        data_policy.TRIED,
        data_policy.CHANGE_SHARE,
        data_policy.NEVER_TICKED,
        steps=(data_policy.StepFacts.of(step),),
        looked_at=True,
        effect=effect,
    )
    reason = str(args.get("reason") or CHAT_REASON).strip()[:MAX_REASON_CHARS] or CHAT_REASON
    if checker.failure(reason, target="helper suggestion", max_chars=MAX_REASON_CHARS) is not None:
        reason = CHAT_REASON
    measured = (
        f" On the first {receipt.rows} rows it changes {receipt.changed} value"
        f"{'' if receipt.changed == 1 else 's'}"
        + (f" and cannot read {receipt.failed}" if receipt.failed else "")
        + "."
    )
    result = ToolResult(
        evidence_id=evidence_id,
        tool=STEP_TOOL,
        args={"kind": kind.value, "column": column, "params": dict(params)},
        result={
            "kind": kind.value,
            "column": column,
            "rows": receipt.rows,
            "changed": receipt.changed,
            "failed": receipt.failed,
            **({"warnings": [str(w.detail["warning"]) for w in warned]} if warned else {}),
        },
        created_at=clock(),
    )
    proposal = Proposal(
        proposal_id=proposal_id,
        kind=ProposalKind.RECIPE_STEP,
        title=(
            _rows_title(column, params)
            if kind is RecipeStepKind.REMOVE_ROWS
            else CHAT_STEP_TITLES[kind].format(c=display_name(column))
        ),
        reason=(reason + measured)[: MAX_REASON_CHARS + len(measured)],
        step=step.model_copy(update={"reason": reason}),
        evidence_ids=(evidence_id, *(r.evidence_id for r in looks)),
        confidence=AgentConfidence.CHECK,
        state=ProposalState.PENDING,
        source=ProposalSource.AI,
    )
    return result, proposal


_ROWS_RULES: Final[dict[str, str]] = {
    "equals": "is {v}",
    "in": "is one of {v}",
    "starts_with": "starts with {v}",
    "is_empty": "is empty",
    "before": "is before {v}",
    "after": "is after {v}",
}


def _rows_title(column: str, params: dict[str, Any]) -> str:
    """ "Leave out rows where 'customer_id' starts with 999": the rule a `remove_rows` step names, in words."""
    values = params.get("values")
    shown = ", ".join(display_name(v) for v in values[:5]) if isinstance(values, list) else ""
    rule = _ROWS_RULES.get(str(params.get("operator")), "matches a rule").format(v=shown)
    return f"Leave out rows where '{display_name(column)}' {rule}"


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
        return finish(
            f"{_fallback(session)} {refused_sentence(checker.why(text, failure, person=True))}",
            blocked_by=failure,
        )
    return finish(text, evidence)


def refused_sentence(message: str, limit: int = 320) -> str:
    """What the person is told when a turn ends on a refusal: what was refused and why (the engine's own message,
    which says what would fix it), and that they can ask again."""
    said = " ".join(message.split())[:limit].rstrip(" .")
    return f"What I tried was refused: {said}. Ask me again and I will try another way."


def _fallback(session: AgentSession) -> str:
    pending = next((p for p in session.proposals if p.state is ProposalState.PENDING), None)
    if pending is None:
        return FALLBACK
    return f"{FALLBACK} Still to decide: {pending.title}."
