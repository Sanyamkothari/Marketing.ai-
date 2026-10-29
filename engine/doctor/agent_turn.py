"""The Data Doctor's AI, as one turn: one loop, one prompt (`data_doctor`), one action set (`engine.doctor.actions`).

Every event is a turn of the same loop: a person's message, an answer, a tick, a new file, a goal chosen. The model
reads the case as it stands (`DoctorTurn.state`), looks with the registry's look tools, and answers with one JSON
action per call; `reply` ends the turn. What it can do:

* look: the registry's read tools, `read_guide`;
* change the plan: `propose_step`, `propose_sql`, `propose_features`, `propose_combine_rows`, `propose_amount_outcome`,
  `propose_test_column`, `join_files` and `stack_files` run their tool exactly as before and the answer becomes an
  :class:`~engine.doctor.author.ItemDraft`; `adopt_suggestion` takes a change a fact tool suggested as it stands;
  `propose_setting` sets one of the goal's settings; `withdraw`, and `replaces` on any of them, take out any item;
* ask: `ask` (a catalogue question or a quick one), `express_doubt`, `request_data`, `propose_unpivot`;
* point: `suggest_answer`, `propose_goal`, `propose_lesson`.

**One code path writes the plan.** Every action that changes the case becomes an :data:`~engine.doctor.author.AuthorAction`
and goes through :func:`engine.doctor.author.apply_actions` on the turn's working case, with
``ApplyContext(author="ai", suggested=...)``: the author only proposes, whether a change is in the plan at once or waits
for the person's tick is code's call, a change that is in the plan or the person's waits for their yes whoever made it,
and the AI is *sure* of a change only when it adopts a suggestion a fact tool made this turn. A refusal goes straight
back to the model (what failed, the name or count it is about, how to fix it) so it can put it right within the turn.
The result carries the applied actions (:attr:`TurnResult.actions`), which the visit applies again to the fresh case
under its lock: `apply_actions` is pure, so that gives the same plan.

Held in code, not in the prompt: the egress gate of each file (customer cell values never reach the model or the logs),
the number check (a number in what the person reads is in a tool result, the state or their own words), the
guardrails, at most `data_doctor.limits.turn.review_steps` actions for an event and `ai_limits.max_chat_steps` for a
message, and `strikes` refusals end the turn.

**The last word.** A turn answering a message that would end without the AI's own reply (its steps used up, a refusal
or two bad actions, an `AGENT_*` limit) gets one more call, told plainly why the turn is ending and that only a `reply`
is accepted now. It is never made when the AI service is the reason. The AI's own text is never cut: a reply that is
too long is refused with the limit, so the model says it in fewer words.

**Handles.** A change, a question and a suggestion are named in the state by a handle that does not move between
turns: `c`, `q` or `s` and five characters of the hash of its id (:func:`handle_of`). The raw id is accepted too.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Final, Protocol

from pydantic import JsonValue, TypeAdapter, ValidationError

from engine.access.roles import Principal
from engine.agent.advisor import ROLE_PRIMARY_KEY, ROLE_TARGET
from engine.agent.contracts import (
    AgentConfidence,
    AgentSession,
    DecidedBy,
    Proposal,
    ProposalKind,
    ProposalSource,
    ProposalState,
    Refusal,
    SentItem,
    SessionStatus,
    ToolResult,
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
from engine.agent.grounding import evidence_numbers, grounded_numbers
from engine.agent.loop import (
    DOCTOR_STEP_KINDS,
    MAX_MESSAGE_CHARS,
    STEP_TEXT_KEYS,
    UNAVAILABLE,
    _Checker,
    _limit_searches,
    _parse,
    _propose_step,
    _record,
    ai_failure_codes,
    person_reason,
    refused_sentence,
    unavailable_reply,
)
from engine.agent.tools import AgentContext, AgentToolError
from engine.agent.untrusted import clean_text, for_prompt
from engine.doctor.actions import ALIASES, prompt_variables
from engine.doctor.agent_apply import Roles, person_owns, roles_of, twin_of, waits_because
from engine.doctor.author import (
    ApplyContext,
    AskPerson,
    AuthorAction,
    Create,
    Doubt,
    ItemDraft,
    Request,
    SetSetting,
    SettingsView,
    Withdraw,
    apply_actions,
    item_id_of,
)
from engine.doctor.build_facts import build_facts, decided_numbers, goal_line, to_do_now
from engine.doctor.case import Case, CaseDoubt, CaseItem, payload_digest
from engine.doctor.decisions import decisions
from engine.doctor.goal_brief import GOAL_BRIEF_TOOL
from engine.doctor.guards import ExpectationBook, Expectations, Finding
from engine.doctor.lessons import approved_lessons
from engine.doctor.limits import limits
from engine.doctor.protocol import (
    AGENT,
    DEFAULT_SETTINGS,
    DONT_KNOW,
    Ask,
    AskOption,
    DataRequest,
    ProtocolError,
    ProtocolSettings,
    doubt_ask,
    express_doubt,
    question_id_of,
)
from engine.doctor.questions import QuestionError, build_ask, catalogue_lines
from engine.doctor.registry import (
    DataToolContext,
    DataToolKind,
    DataToolResult,
    Exposure,
    evidence_id_of,
)
from engine.doctor.row_contracts import StepProposal
from engine.doctor.subjects import SETTING_KIND
from engine.doctor.tools_facts import M_FACT_TOOLS
from engine.doctor.tools_features import parse_query_output
from engine.doctor.tools_history import M_HISTORY_TOOLS
from engine.doctor.tools_preview import M_PREVIEW_TOOLS
from engine.doctor.tools_sql import SqlStepProposal
from engine.doctor.visit_contracts import OptionEffect
from engine.generative.contracts import GenerativePurpose
from engine.generative.errors import GenerativeError
from engine.generative.guardrails import Guardrails
from engine.generative.prompts import Prompt, load_prompt, render
from engine.llm import LLMError
from engine.storage import StorageError
from engine.utils.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from engine.doctor.ai_guard import AiGuard
    from engine.doctor.diagnose import LoadedFile
    from engine.doctor.goal_brief import GoalBrief
    from engine.storage import Storage

__all__ = [
    "MAKES_CHANGES",
    "NO_LAST_WORD",
    "OUT_OF_STEPS",
    "PROMPT",
    "SQL_ARGS",
    "DoctorTurn",
    "TurnResult",
    "handle_of",
    "own_numbers",
]

_LOG = get_logger(__name__)

PROMPT: Final[str] = "data_doctor"
QUICK_CODE: Final[str] = "CHAT_ASK"
HANDLE_CHARS: Final[int] = 5
TITLE_CHARS: Final[int] = 600
"""How much of a change's title the state shows: more than any title the engine or the model writes."""
SUGGESTIONS_SHOWN: Final[int] = 8
_DIGIT: Final = re.compile(r"\d")
_NUMBER_WORD: Final = re.compile(r"\d[\d,.]*\S*")
_MEANING_TOPICS: Final = re.compile(
    r"(consent|opt[\W_]*(in|out)|opted|agree|contact|unsubscri|outcome|counts? as|a yes"
    r"|churn|convert|identif|customer[\W_]*id|\bids?\b|\bdates?\b|day first|month first)",
    re.IGNORECASE,
)
"""What a quick question may not be about: who agreed to be contacted, the outcome, the ID, the date. Those are the
catalogue's questions, which only the person answers."""
_SALE_WORDS: Final = re.compile(r"(a sale|purchas|bought)", re.IGNORECASE)
"""Words that define an outcome ("did they purchase?") and so are kept out of a quick question, except in a question
about what some rows are (`_ABOUT_ROWS`): "what are the negative rows on C invoices?" is the returns guide's own."""
_ABOUT_ROWS: Final = re.compile(
    r"(negative|\bcredit|cancel+ation|\breturn|\brefund|duplicate|repeated rows?)", re.IGNORECASE
)
_NO_ANSWER: Final = re.compile(r"^\s*i\s*(do\s*not|don.?t)\s*know\.?\s*$", re.IGNORECASE)
"""The AI's own "I do not know" option: the protocol adds its "I don't know", so a second one is dropped."""
_ARGUMENT_TEXT: Final[frozenset[str]] = frozenset({"sql", "description", "reason", "text", "title"})
SQL_ARGS: Final[frozenset[str]] = frozenset(
    {"sql", "output_name", "as_of", "as_of_rule", "as_of_column", "reads_future", "outcome_window_days"}
)
MAKES_CHANGES: Final[frozenset[str]] = frozenset(
    {
        "propose_step",
        "propose_sql",
        "propose_features",
        "propose_combine_rows",
        "propose_amount_outcome",
        "propose_test_column",
        "join_files",
        "stack_files",
    }
)
"""The actions that put a change in front of the person: each is told, before the reply, whether it is in the plan or
waits for their tick."""
MAKES_CHANGES_WITH_ARGS: Final[frozenset[str]] = frozenset(
    {*MAKES_CHANGES - {"join_files", "stack_files"}, "propose_unpivot"}
)
"""The actions whose accepted arguments are the AI's own settings (a window, a date): it may repeat them."""
OUT_OF_STEPS: Final[str] = (
    "That took more steps than I may take in one message. What I did so far is above; ask me to go on if you want more."
)
NO_LAST_WORD: Final[frozenset[str]] = frozenset({"unavailable", "prompt_too_large", "failed"})
"""Why a turn ended without a reply that is not worth one more call: the AI service did not answer (or the case's
money ran out), the prompt cannot be sent, or a step broke in a way nobody can explain to the person."""
NOTHING_SAID: Final[str] = "I could not answer that from what I have checked."
STOPPED: Final[str] = (
    "I started on that, but a step failed before I could finish it, so nothing was changed. "
)
TOO_BIG: Final[str] = (
    "There is too much in this case for me to read in one message, so I could not act on that. "
)
ASK_AGAIN: Final[str] = "Ask me again and I will try once more."
_DOING: Final[dict[str, str]] = {
    "propose_sql": "build a table with a query",
    "propose_step": "prepare the file",
    "propose_amount_outcome": "set up the outcome",
    "propose_test_column": "set up the test column",
    "join_files": "join the files",
    "stack_files": "stack the files",
    "withdraw": "take a change out",
    "propose_setting": "set that",
    "adopt_suggestion": "use that suggestion",
    "ask": "ask you a question",
    "suggest_answer": "point at an answer",
    "propose_lesson": "note a lesson",
}
"""What the AI was doing when a refusal ended the turn, in the person's words."""
_SHOWN: Final = {
    ProposalState.ACCEPTED: "in plan",
    ProposalState.PENDING: "waiting",
    ProposalState.REJECTED: "left out",
}
_BY: Final = {"rules": "default", "ai": "you", "person": "person"}
_ACTION: Final[TypeAdapter[AuthorAction]] = TypeAdapter(AuthorAction)


def handle_of(prefix: str, ident: str, width: int = HANDLE_CHARS) -> str:
    """The name the model reads for a change (`c`), a question (`q`) or a suggestion (`s`): `width` characters of the
    hash of its id. It depends on the id alone, so it is the same in every turn."""
    return prefix + hashlib.sha256(ident.encode("utf-8")).hexdigest()[:width]


class _NoAnswerError(Exception):
    """The model cannot be asked now: `reason` is `prompt_too_large` or `unavailable` (no service, or no money).

    For `unavailable`, `failure` is what the AI service raised: `codes` say why (the failure's own code, then
    `LLM_KEY_EXPIRED` ...; see `engine.agent.loop.ai_failure_codes`) and `reply` is what the person reads (the
    lead sentence, and the HTTP client's own fixed sentence and fix when it has one). Neither holds a word the
    service wrote.
    """

    def __init__(self, reason: str, failure: LLMError | GenerativeError | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.codes: tuple[str, ...] = () if failure is None else ai_failure_codes(failure)
        self.reply: str = UNAVAILABLE if failure is None else unavailable_reply(failure)


class _RefusedError(AgentToolError):
    """The author refused an action (`apply_actions`): what failed, the name or count it is about, how to fix it."""

    def __init__(self, refusal: Refusal) -> None:
        super().__init__(refusal.code, refusal=refusal)


@dataclass
class _Sql:
    """One query the model proposed: the registry's answer to its latest try, and the findings to show."""

    result: DataToolResult
    findings: tuple[Finding, ...] = ()


@dataclass
class _Turn:
    """The turn's working memory: what the model has seen and made. Never stored."""

    looks: list[ToolResult] = field(default_factory=list)
    files_of: dict[str, int] = field(default_factory=dict)
    """Look evidence id -> index of the file it read."""
    steps: list[tuple[int, ToolResult, Proposal]] = field(default_factory=list)
    queries: dict[str, _Sql] = field(default_factory=dict)
    """By output name: the latest try of each query."""
    combines: dict[int, tuple[int, DataToolResult]] = field(default_factory=dict)
    """By file index: the latest `propose_combine_rows` answer (file index, result)."""
    closed: set[str] = field(default_factory=set)
    asks: list[Ask] = field(default_factory=list)
    doubts: list[CaseDoubt] = field(default_factory=list)
    requests: list[DataRequest] = field(default_factory=list)
    blocking: int = 0
    feedback: list[dict[str, Any]] = field(default_factory=list)
    sent: list[Any] = field(default_factory=list)
    book: ExpectationBook = field(default_factory=ExpectationBook)
    orders: dict[str, int] = field(default_factory=dict)
    guides: dict[str, set[str]] = field(default_factory=dict)
    """Guide -> the parts already in this turn's results (its section titles, `Rules`, and `*` for all of it)."""


@dataclass
class _Chosen:
    """What the model has done in this turn beyond looking."""

    combined: list[CaseItem] = field(default_factory=list)
    """Items a tool made whole (a join, a stack, a test column), to go through the author like any other."""
    asked: dict[str, Ask] = field(default_factory=dict)
    quick: int = 0
    suggestions: dict[str, str] = field(default_factory=dict)
    """Question id -> the option the model points at; a second suggestion for a question replaces the first."""
    goal: tuple[str, str] | None = None
    """`(goal_id, reason)` of the goal the model proposes; a second proposal replaces the first."""
    used: list[str] = field(default_factory=list)
    reported: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class _Kept:
    """An action the turn keeps, with the key of what it makes (a later try of the same query replaces it) and the
    ids it made or changed."""

    action: AuthorAction
    key: tuple[str, str] | None
    made: tuple[str, ...]


@dataclass(frozen=True)
class _Mark:
    """What the turn's memory held before an action, so what the action added can be read off after it."""

    steps: int
    queries: dict[str, _Sql]
    combines: dict[int, tuple[int, DataToolResult]]
    combined: int
    asks: int
    doubts: int
    requests: int


class Suggested(Protocol):
    """A change a fact tool suggested (`engine.doctor.tools_facts`): its stable id, the action it would take and why."""

    @property
    def id(self) -> str: ...

    @property
    def action(self) -> AuthorAction: ...

    @property
    def reason(self) -> str: ...


@dataclass(frozen=True)
class _Suggestion:
    """A suggestion read off a fact tool's answer."""

    id: str
    action: AuthorAction
    reason: str


@dataclass(frozen=True)
class TurnResult:
    """What one turn made. The visit applies `actions` to the fresh case (`apply_actions`) and writes the rest."""

    reply: str
    actions: tuple[AuthorAction, ...] = ()
    """The actions that changed the case and were applied, in order; a refused one is not here (`refused`)."""
    case: Case | None = None
    """The case with `actions` applied: what `apply_actions` makes of them."""
    refused: tuple[Refusal, ...] = ()
    """Every refusal of the turn, in order; the model was told each one."""
    items: tuple[CaseItem, ...] = ()
    """The changes it put forward, as the author made them (or found them already there)."""
    withdrawn: tuple[str, ...] = ()
    """Ids of the items it took out, or asked the person to take out."""
    evidence: tuple[DataToolResult, ...] = ()
    suggestions: tuple[tuple[str, str], ...] = ()
    """`(question_id, option_id)` for each open question the model pointed at, one per question; the visit checks the
    question is still open."""
    goal: tuple[str, str] | None = None
    """`(goal_id, reason)`: the goal it proposes while no goal is chosen; only the person's click sets it."""
    tools: tuple[str, ...] = ()
    sent: tuple[dict[str, Any], ...] = ()
    blocked_by: str | None = None
    """Why the model's own answer is missing: `unavailable` (the AI service did not answer, or the case's spending
    ran out), `prompt_too_large`, the code of a refusal it met twice, or `failed`; None when it replied."""
    error_codes: tuple[str, ...] = ()
    """Why the AI service did not answer, when it did not: the failure's own code (`LLM_UNAVAILABLE`; `LLM_REFUSED`
    for a model that ran out of room; `BUDGET_EXCEEDED`), then the kind the HTTP client found (`LLM_KEY_EXPIRED`,
    `LLM_OUT_OF_ROOM` ...): `engine.agent.loop.ai_failure_codes`."""
    model_calls: int = 0
    tokens: int = 0
    answered: int = 0
    """How many calls the model answered."""
    finished: bool = False
    """The model ended the turn with `reply` (not a limit, a refusal or an AI service that stopped answering)."""
    state_digest: str = ""

    @property
    def asks(self) -> tuple[Ask, ...]:
        return tuple(a.ask for a in self.actions if isinstance(a, AskPerson))

    @property
    def doubts(self) -> tuple[CaseDoubt, ...]:
        return tuple(a.doubt for a in self.actions if isinstance(a, Doubt))

    @property
    def requests(self) -> tuple[DataRequest, ...]:
        return tuple(a.request for a in self.actions if isinstance(a, Request))

    @property
    def suggestion(self) -> tuple[str, str] | None:
        """The first suggestion, for a reader that shows one."""
        return self.suggestions[0] if self.suggestions else None


def _active_goal(case: Case) -> Any:
    """The case's active goal (`visit.active_goal`, which this module cannot import at load time)."""
    return next((goal for goal in case.goals if goal.active), None)


def _expect_problems(exc: ValidationError) -> str:
    """What was wrong with an `expect`, field by field, for the AI to fix: the field paths and the messages, never
    the values sent (they may be cell text) and never a documentation link."""
    found = [
        f"`{'.'.join(str(part) for part in e['loc']) or 'expect'}`: {str(e['msg'])[:80]}"
        for e in exc.errors(include_url=False, include_input=False, include_context=False)
    ]
    allowed = ", ".join(sorted(Expectations.model_fields))
    return f"`expect` is not valid: {'; '.join(found[:6])}. Allowed fields: {allowed}."[:600]


_NAME_WINDOW: Final = re.compile(r"_(\d{1,4})(?:d|_?days?)(?![A-Za-z0-9])")
"""The window a feature's name states (`orders_365d`, `spend_90_days`): only that suffix, never other digits."""


def own_numbers(value: Any, key: str = "") -> list[Any]:
    """The numbers an accepted action's arguments set (`outcome_window_days: 90`, `as_of: "2011-09-01"`): settings
    the AI chose and the engine took, which it may repeat. The `expect` bounds are predictions about the data, not
    settings, so they are never read. Text (a query, a reason) is not read for numbers, and a number a look tool was
    given is never one of these."""
    if isinstance(value, bool) or value is None:
        return []
    if isinstance(value, (int, float)):
        return [value]
    if isinstance(value, str):
        return [value] if key in {"as_of", "training_as_of"} else []
    if isinstance(value, dict):
        named = value.get("name") if key == "features" else None
        window = [int(n) for n in _NAME_WINDOW.findall(named)] if isinstance(named, str) else []
        return window + [
            n
            for k, v in value.items()
            if k not in _ARGUMENT_TEXT and k != "expect"
            for n in own_numbers(v, str(k))
        ]
    if isinstance(value, (list, tuple)):
        return [n for v in value for n in own_numbers(v, key)]
    return []


def _gate(ctx: AgentContext) -> Egress:
    agent = ctx.config.agent
    return Egress.build(
        [str(c) for c in ctx.frame.columns],
        personal_columns=[column.name for column in ctx.profile.columns if column.pii_kinds],
        always_hide=agent.always_hide_columns,
        shape_only=ctx.long_text,
        mode=agent.ai_data_access,
    )


def _data_result(result: ToolResult, ctx: AgentContext, now: Any) -> DataToolResult:
    from engine.agent.tools import TOOLS

    return DataToolResult(
        evidence_id=evidence_id_of(result),
        tool=result.tool,
        kind=(
            DataToolKind.READ
            if result.tool in TOOLS
            or result.tool in (*M_PREVIEW_TOOLS, *M_HISTORY_TOOLS, *M_FACT_TOOLS)
            or result.tool == GOAL_BRIEF_TOOL
            else DataToolKind.PROPOSE
        ),
        exposure=Exposure.SESSION_ONLY,
        input={**result.args, "upload_id": ctx.upload_id},
        output=dict(result.result),
        computed_at=now,
    )


def _hashed(turn: _Turn, cited: Sequence[str]) -> list[str]:
    by_id = {r.evidence_id: r for r in turn.looks}
    return [evidence_id_of(by_id[e]) for e in cited if e in by_id]


def _plain_or_action(answer: str) -> dict[str, Any]:
    """The model's JSON action; an answer in plain sentences (a friendly model forgets the format) is a reply, and
    goes through the same checks as one."""
    text = answer.strip()
    if text and not text.startswith(("{", "`", "[")):
        return {"action": "reply", "text": text}
    return _parse(answer)


def _without_ids(value: Any) -> Any:
    """`value` without the ids the model quotes back (a hash's digits are not measurements it may state)."""
    if isinstance(value, dict):
        return {k: _without_ids(v) for k, v in value.items() if k != "id"}
    if isinstance(value, list):
        return [_without_ids(v) for v in value]
    return value


def _whole_word(part: str, found: re.Match[str]) -> str:
    """The whole word of `part` that `found` is in (`purchases` for `purchas`), for a refusal to name."""
    start, end = found.span()
    while start > 0 and part[start - 1].isalnum():
        start -= 1
    while end < len(part) and part[end].isalnum():
        end += 1
    return part[start:end]


def _where(part: str, text: str, why: str) -> str:
    """Which piece of a quick question (its text, its reason or one of its options) a refusal is about."""
    return "the question" if part is text else ("the reason" if part is why else f"the option “{part[:60]}”")


def _audiences_of(verdict: dict[str, Any]) -> dict[str, Any]:
    """A buildable goal that names a narrower audience: both readings' counts (`BuildableOutcome.audiences`), so the
    AI can say which one the goal describes and why. Counts and ids only; the person picks."""
    built = verdict.get("buildable")
    found = built.get("audiences") if isinstance(built, dict) else None
    if not isinstance(found, list) or not found:
        return {}
    return {
        "audiences": [
            {
                "audience": str(a.get("audience_id")),
                "quiet_days": a.get("quiet_days"),
                "customers": a.get("customers"),
                "yes_count": a.get("yes"),
            }
            for a in found
            if isinstance(a, dict)
        ]
    }


def _what_of(action: AuthorAction) -> str:
    """What a suggested action does, in a few words for the state."""
    match action:
        case Create():
            return action.draft.title
        case Withdraw():
            return f"Take out {action.item_id}: {action.consequence}"
        case SetSetting():
            return f"Use {action.value} for {action.setting.replace('_', ' ')}."
        case AskPerson():
            return action.ask.text
        case Doubt():
            return action.doubt.because
        case Request():
            return action.request.what


class DoctorTurn:
    """One turn of the Doctor's AI over one case (see the module docstring)."""

    purpose: GenerativePurpose = GenerativePurpose.DOCTOR

    def __init__(
        self,
        case: Case,
        files: tuple[LoadedFile, ...],
        brief: GoalBrief | None,
        context_for: Callable[[Case, tuple[LoadedFile, ...]], AgentContext],
        meter: AiGuard,
        guardrails: Guardrails,
        principal: Principal,
        config_root: Path | None,
        registry: Any,
        *,
        chat_steps: int,
        storage: Storage | None = None,
        owed: Sequence[str] = (),
        suggestions: Sequence[Suggested] = (),
        goal_settings: SettingsView | None = None,
        protocol: ProtocolSettings = DEFAULT_SETTINGS,
    ) -> None:
        from engine.doctor.ai_guard import file_overviews
        from engine.doctor.current_tables import current_for, labels_for

        self.case, self.brief, self.meter, self.registry = case, brief, meter, registry
        self._context_for = context_for
        self._raw: dict[int, tuple[AgentContext, Any]] = {}
        self.principal, self.config_root = principal, config_root
        self.storage = storage
        self.chat_steps = chat_steps
        self.owed = tuple(owed)
        self.suggestions = {s.id: s for s in suggestions}
        self.goal_settings = goal_settings
        self.protocol = protocol
        # The AI looks at the current tables: F1..Fn are the files as the plan's accepted fixes leave them, T1..Tn the
        # tables its queries build. The uploads (`self.files`) stay what the tools that read a file by id are given.
        current = current_for(case, files, context_for)
        self.built = current.built
        self.current = current
        self.labels = labels_for(current)
        self.ctxs = [context_for(case, (t,)) for t in current.tables]
        overviews = file_overviews(
            self.ctxs, self.labels
        )  # a table past the prompt's room is left out, and so unreachable
        self.nfiles = min(len(overviews), len(files))
        self.files = files[: self.nfiles]
        self.ctxs = self.ctxs[: len(overviews)]
        self.labels = self.labels[: len(overviews)]
        self.built = self.built[: max(0, len(overviews) - self.nfiles)]
        self.gates = [_gate(ctx) for ctx in self.ctxs]
        self.sessions = [self._session(case, ctx) for ctx in self.ctxs]
        self.clock = self.ctxs[0].clock
        self.now = self.clock()
        self.turn = _Turn()
        self.turn.sent.extend(sent for _overview, sent in overviews)
        self.overviews = [overview for overview, _sent in overviews]
        for built, overview in zip(self.built, self.overviews[self.nfiles :], strict=False):
            gate = self.gates[self.nfiles + self.built.index(built)]
            overview["built_by_your_query"] = True
            overview["key"] = gate.label(built.key) if built.key else None
            overview["time_column"] = gate.label(built.time_column) if built.time_column else None
        names = [str(c) for ctx in self.ctxs for c in ctx.frame.columns]
        names += [g.label(c) for g, ctx in zip(self.gates, self.ctxs, strict=True) for c in ctx.frame.columns]
        self.limits = limits(self.ctxs[0].config)
        self.chosen = _Chosen()
        self.replacing: tuple[str, ...] = ()
        """Items the turn is swapping out or taking back in the action being run (a query's trial leaves them out)."""
        self.kept: list[_Kept] = []
        self.work_case: Case = case
        """The case with the turn's kept actions applied (`apply_actions`)."""
        self.refusals: list[Refusal] = []
        self.left_out: list[CaseItem] = []
        """Changes the person left out earlier that the model put forward again: not put back."""
        self.finished = False
        self.answered = 0
        self._roles: Roles | None = None
        # Handles are the same in every turn. The state shows the latest of each; any item or question of the case
        # (and any suggestion) can be named by its handle, so a change the state window left out still can be.
        self.handles: dict[str, str] = {}
        """Handle -> id. Two ids that share a handle each keep their own: the later one gets a longer handle."""
        self._named: dict[tuple[str, str], str] = {}
        for item in case.items:
            self._name("c", item.item_id)
        for q in case.questions:
            self._name("q", q.question_id)
        for suggestion_id in self.suggestions:
            self._name("s", suggestion_id)
        self.checker = _Checker(grounded=frozenset(), names=tuple(names), guardrails=guardrails)
        self.state: dict[str, Any] = {
            "files": self.overviews,
            **self._goal_state(),
            **self._plan_state(),
            "earlier_looks": self._reload_looks(storage),
            "left": {"blocking_questions": self._blocking_room()},
        }
        self.checker.grounded = self.checker.grounded | grounded_numbers(
            [_without_ids(self.state), decided_numbers(case), self._suggested_numbers()]
        )

    # -- what the model reads ----------------------------------------------------------------------
    def _name(self, prefix: str, ident: str) -> str:
        """The handle the model reads for an id, and the only way one is made: the first id to take a handle keeps it
        and an id that shares it gets a longer one, so a handle always names one id."""
        if (named := self._named.get((prefix, ident))) is not None:
            return named
        width = HANDLE_CHARS
        while (handle := handle_of(prefix, ident, width)) in self.handles and self.handles[handle] != ident:
            width += 1
        self.handles[handle] = ident
        self._named[(prefix, ident)] = handle
        return handle

    def _blocking_room(self) -> int:
        """How many more blocking questions may be asked: the limit less those already waiting for an answer."""
        from engine.doctor.visit import open_blocking

        return max(0, self.limits.asks.blocking - len(open_blocking(self.case)))

    def _gate_of(self, upload_id: str | None) -> Any:
        index = next((i for i, ctx in enumerate(self.ctxs) if ctx.upload_id == upload_id), 0)
        return self.gates[index]

    def _text(self, upload_id: str | None, value: str) -> str:
        """`value` for the prompt: through its file's gate, and for text about no one file (or the conversation)
        through every file's gate, so a column name of any file the person typed or a title carried is aliased.
        """
        if upload_id is not None and any(ctx.upload_id == upload_id for ctx in self.ctxs):
            return str(self._gate_of(upload_id).text(value))
        for gate in self.gates[1:]:
            value = gate.apply_aliases(value)
        return str(self.gates[0].text(value))

    def state_digest(self) -> str:
        """A short digest of the state the AI reads: the same state gives the same digest, so a turn that already
        finished on it need not be asked again."""
        from engine.agentic.canonical import sha256_hex

        return sha256_hex(json.dumps(self.state, sort_keys=True, ensure_ascii=False, default=str).encode())[
            :16
        ]

    @staticmethod
    def _session(case: Case, ctx: AgentContext) -> AgentSession:
        """A light session for Plan G's step checks: it holds the key and outcome the case decided, so a step can
        never touch them. No advisor runs."""
        now = ctx.clock()
        stub = ToolResult(evidence_id="decided", tool="get_profile", created_at=now)
        decided = decisions(case)
        roles = [
            Proposal(
                proposal_id=f"role-{path}",
                kind=ProposalKind.ROLE,
                title=f"{path} decided",
                reason="decided in the case",
                path=path,
                value=column,
                evidence_ids=("decided",),
                confidence=AgentConfidence.SURE,
                state=ProposalState.ACCEPTED,
                decided_by=DecidedBy.USER,
            )
            for path, column in (
                (ROLE_PRIMARY_KEY, decided.column_of_upload("key", ctx.upload_id)),
                (ROLE_TARGET, decided.outcome_of_upload(ctx.upload_id)),
            )
            if column is not None
        ]
        return AgentSession(
            session_id="dd-turn",
            upload_id=ctx.upload_id,
            use_case_id=ctx.use_case_id,
            mode=ctx.mode.value,
            agent_name="Data Doctor",
            status=SessionStatus.NEEDS_REVIEW,
            proposals=tuple(roles),
            tool_results=(stub,) if roles else (),
            created_at=now,
            updated_at=now,
        )

    def _goal_state(self) -> dict[str, Any]:
        """The chosen goal with what a yes means, its settings (with the values each allows), the minimums, the build's
        parameters when its outcome is built from events, and what to do first; or, while no goal is chosen, the goals
        that fit the files."""
        from engine.doctor.visit import active_goal

        goal = active_goal(self.case)
        todo = [*self.owed]
        owed_build = to_do_now(self.case)
        if owed_build:
            todo.append(owed_build)
        out: dict[str, Any] = {}
        if goal is None:
            out.update(self._goals_that_fit())
        else:
            facts = build_facts(
                self.case,
                self.brief,
                self.current.files,
                self.config_root,
                lambda upload_id, name: self._gate_of(upload_id).label(name),
            )
            chosen = facts.get("chosen_goal")
            if isinstance(chosen, dict):
                shown: dict[str, Any] = {
                    "name": chosen.get("goal"),
                    "a_yes_means": chosen.get("a_yes_means"),
                    "outcome_column": chosen.get("outcome_column"),
                    "settings": self._settings_of(goal.goal_id),
                    "minimums": self._minimums(goal.goal_id),
                }
                build = facts.get("goal_build")
                if isinstance(build, dict):
                    shown["snapshot_rule"] = build.get("snapshot_rule")
                    shown["build"] = {k: v for k, v in build.items() if k != "snapshot_rule"}
                out["goal"] = shown
        if todo:
            out["to_do_now"] = todo
        return out

    def _minimums(self, goal_id: str) -> dict[str, Any]:
        from engine.config import ConfigError, load_use_case

        try:
            validation = load_use_case(goal_id, self.config_root).validation
        except (ConfigError, OSError, ValueError):
            return {}
        return {"rows": validation.min_rows, "positives": validation.min_positive}

    def _settings_of(self, goal_id: str) -> list[dict[str, Any]]:
        """The goal's settings the case holds a value for, who set each (the person, a change waiting for their yes, or
        the author's default) and the values the goal's config allows."""
        from engine.doctor.visit import active_section

        shown: dict[str, dict[str, Any]] = {}
        for item in active_section(self.case)[0]:
            payload = item.payload
            if item.kind != SETTING_KIND or payload.get("goal_id") != goal_id:
                continue
            if item.state is ProposalState.REJECTED:
                continue
            name = str(payload.get("setting"))
            set_by = (
                "person"
                if person_owns(self.case, item)
                else ("pending" if item.state is ProposalState.PENDING else "default")
            )
            if name in shown and set_by == "pending":
                continue  # the value in force stays; the one waiting for a yes is its swap
            shown[name] = {"name": name, "value": payload.get("value"), "set_by": set_by}
        if self.goal_settings is not None:
            for name, row in shown.items():
                row["allowed"] = [str(v) for v in self.goal_settings.allowed(name)]
        return list(shown.values())

    def _goals_that_fit(self) -> dict[str, Any]:
        """While no goal is chosen, the goals that fit the files (`propose_goal` names one by its id): what the rules
        counted, in their own words. Nothing from a cell; a buildable goal's counts are those of the build."""
        from engine.doctor.visit import fitting_goals

        fitting = fitting_goals(self.case)
        if not fitting:
            return {}
        return {
            "goals_that_fit": [
                {
                    "goal_id": str(u.get("use_case_id")),
                    "status": str(u.get("status")),
                    "customers": u.get("customers"),
                    "yes_count": u.get("yes_count"),
                    **_audiences_of(u),
                }
                for u in fitting[:6]
            ]
        }

    def _suggested_numbers(self) -> list[str]:
        """What the suggestions say, whose counts the model may repeat."""
        return [self._text(None, f"{_what_of(s.action)} {s.reason}") for s in self.suggestions.values()]

    def _plan_state(self) -> dict[str, Any]:
        """The case as the model may read it: every change with its handle, who made it and what its yes rests on, the
        questions with their answers, the suggestions, the stops and the doubts."""
        from engine.doctor.visit import active_section

        items, questions = active_section(self.case)
        plan = [self._plan_row(item) for item in items[-self.limits.turn.state_items :]]
        asked: list[dict[str, Any]] = []
        for q in sorted(questions, key=lambda q: q.answer is not None)[: self.limits.turn.state_questions]:
            options = [
                {
                    "id": o.option_id,
                    "label": self._text(q.upload_id, clean_text(o.label, self.limits.turn.short_chars)),
                    # What choosing it adds, when it adds a step: two choices that read alike (the same minutes)
                    # are told apart by it. An answer that only records itself says nothing more.
                    **(
                        {"adds": self._text(q.upload_id, clean_text(o.effect, self.limits.turn.short_chars))}
                        if o.effect and o.effect_digest is not None
                        else {}
                    ),
                }
                for o in q.options
            ]
            chosen = next((o["label"] for o in options if o["id"] == q.answer), None)
            if chosen is not None and q.answer_value is not None:
                chosen = f"{chosen}: {self._text(q.upload_id, clean_text(q.answer_value, self.limits.turn.short_chars))}"
            asked.append(
                {
                    "id": self._name("q", q.question_id),
                    "text": self._text(q.upload_id, clean_text(q.text, self.limits.turn.short_chars)),
                    "options": options,
                    "answer": chosen,
                }
            )
        doubts = [
            self._text(d.upload_id, clean_text(d.because, self.limits.turn.short_chars))
            for d in [d for d in self.case.doubts if d.status == "open"][-self.limits.turn.state_doubts :]
        ]
        out: dict[str, Any] = {"plan": plan, "questions": asked, "stops": self._stops(), "doubts": doubts}
        suggested = list(self.suggestions.values())
        if suggested:
            out["suggested"] = [
                {
                    "id": self._name("s", s.id),
                    "what": self._text(None, clean_text(_what_of(s.action), TITLE_CHARS)),
                    "reason": self._text(None, clean_text(s.reason, self.limits.turn.short_chars)),
                }
                for s in suggested[:SUGGESTIONS_SHOWN]
            ]
            if len(suggested) > SUGGESTIONS_SHOWN:
                out["suggested_left_out"] = len(suggested) - SUGGESTIONS_SHOWN
        return out

    def _plan_row(self, item: CaseItem) -> dict[str, Any]:
        """One change as the model reads it: its handle, what it does, its state, who made it and who decided it, the
        receipt our code counted, what a trial on the first rows found, and the table a query makes."""
        index = next(
            (i for i, c in enumerate(self.ctxs[: self.nfiles]) if c.upload_id == item.upload_id), None
        )
        step = item.payload.get("step")
        column = item.payload.get("column") or (step.get("column") if isinstance(step, dict) else None)
        tried = item.payload.get("tried")
        made = item.payload.get("columns")
        decided = (
            "approval"
            if any(item.item_id in a.item_ids for a in self.case.approvals)
            else ("person" if item.decided_by not in (None, AGENT) else "none")
        )
        return {
            "id": self._name("c", item.item_id),
            "what": self._text(item.upload_id, clean_text(item.title, TITLE_CHARS)),
            "kind": str(step.get("kind")) if isinstance(step, dict) else item.kind,
            **self._where(item, index, step, column),
            "state": _SHOWN[item.state],
            "by": _BY[item.source.value],
            "decided_by": decided,
            **({"replaces": self._name("c", item.replaces)} if item.replaces else {}),
            **({"tried_on_first_rows": tried} if isinstance(tried, dict) else {}),
            **({"receipt": view} if (view := self._receipt_view(item)) is not None else {}),
            # A query's own table, so the model sees what it built (names only, through the file's gate).
            **(
                {
                    "makes_table": {
                        "name": self._text(
                            item.upload_id, str(step.get("column", ""))[: self.limits.turn.short_chars]
                        ),
                        "columns": [
                            self._text(item.upload_id, str(c)[: self.limits.turn.short_chars])
                            for c in made[: self.limits.turn.made_columns]
                        ],
                    }
                }
                if isinstance(made, list)
                and isinstance(step, dict)
                and step.get("kind") == "sql_query"
                and str(step.get("column", "")) not in {b.name for b in self.built}
                else {}
            ),
        }

    def _where(self, item: CaseItem, index: int | None, step: Any, column: Any) -> dict[str, str]:
        """The file and the column a change is about, names through the file's gate; a query over the files is about
        "all files"."""
        if index is not None and isinstance(column, str):
            return {"file": self.labels[index], "column": self.gates[index].label(column)}
        if item.upload_id is None and isinstance(step, dict):  # the AI's queries are over the files
            named = column if isinstance(column, str) else str(item.payload.get("output_name") or item.title)
            return {"file": "all files", "column": named[:60]}
        return {}

    def _stops(self) -> list[dict[str, Any]]:
        """What keeps approval waiting, one row per kind of stop about one thing, each with how to fix it."""
        from engine.doctor.ledger import findings_of

        rows: dict[str, dict[str, Any]] = {}
        for found in findings_of(self.case):
            if found.source == "doubt" or found.level not in {"stop", "hard"} or found.acknowledged:
                continue
            fix = (
                "Change the plan or the file."
                if found.level == "hard"
                else "The person can accept this and go on, or change the plan or the file."
            )
            rows.setdefault(
                found.key,
                {
                    "code": found.code,
                    "what_failed": self._text(None, clean_text(found.text, self.limits.turn.short_chars)),
                    "value": None if found.count is None else str(found.count),
                    "how_to_fix": fix,
                },
            )
        return list(rows.values())[: self.limits.turn.state_items]

    def _receipt_view(self, item: CaseItem) -> dict[str, Any] | None:
        """What our code counted for a query in the plan, so the AI can quote it without looking it up again: the
        rows read and made, the customers before, after, lost and repeated, what the guards found, and whether the
        counts are of the whole file or of a sample (then they are estimates: say "about")."""
        receipt = item.payload.get("receipt")
        if not isinstance(receipt, dict):
            return None
        rows_in = receipt.get("rows_in")
        view: dict[str, Any] = {
            "rows_read": next(iter(rows_in.values()), None) if isinstance(rows_in, dict) else None,
            "rows_made": receipt.get("rows_out"),
        }
        if receipt.get("keys_in") is not None:
            view.update(
                customers_before=receipt.get("keys_in"),
                customers_after=receipt.get("keys_out"),
                customers_lost=receipt.get("keys_lost"),
                customers_repeated=receipt.get("keys_repeated"),
            )
        sampled = bool(item.payload.get("sampled")) or "sentence_estimate" in item.payload
        whole = self._whole_file_step(item)
        if whole is not None:  # the plan check counted every row: those are the numbers
            sampled = False
            view.update(
                rows_read=whole.get("rows_in"),
                rows_made=whole.get("rows_out"),
                **(
                    {
                        "customers_before": whole.get("customers_in"),
                        "customers_after": whole.get("customers_out"),
                        "customers_lost": whole.get("customers_lost"),
                    }
                    if whole.get("customers_in") is not None
                    else {}
                ),
            )
        view["counted_on"] = (
            f"a sample of {self.limits.trial.dry_run_rows:,} rows of each file: these counts are estimates, say "
            "'about' or wait for the whole file's count"
            if sampled
            else "the whole file"
        )
        findings = item.payload.get("findings")
        found = [
            {
                "level": str(f.get("level")),
                "code": str(f.get("code")),
                "says": self._text(item.upload_id, clean_text(str(f.get("message", "")), 240)),
            }
            for f in (findings if isinstance(findings, list) else [])
            if isinstance(f, dict) and f.get("level") in {"stop", "flag"}
        ]
        if found:
            view["findings"] = found[:4]
        return view

    def _whole_file_step(self, item: CaseItem) -> dict[str, Any] | None:
        """The plan check's row for `item`, when it counted every row of the files (its `basis` is set)."""
        check = self.case.visit.plan_check if self.case.visit is not None else None
        if check is None or check.basis is None:
            return None
        steps = check.ledger.get("steps")
        return next(
            (
                row
                for row in (steps if isinstance(steps, list) else [])
                if isinstance(row, dict) and row.get("item_id") == item.item_id
            ),
            None,
        )

    def _reload_looks(self, storage: Storage | None) -> list[dict[str, Any]]:
        """The latest looks of earlier turns, back from the case's stored evidence, so the model can cite them
        (`evidence_ids`) and state their counts. Only the look and its column go in the prompt, not the result.
        """
        from engine.agent.tools import TOOLS
        from engine.doctor.case import evidence_key

        if storage is None:
            return []
        shown: list[dict[str, Any]] = []
        looks = {*TOOLS, *M_PREVIEW_TOOLS, *M_HISTORY_TOOLS, *M_FACT_TOOLS, GOAL_BRIEF_TOOL}
        refs = [r for r in self.case.evidence if r.tool in looks and r.upload_id is not None]
        for ref in refs[-self.limits.turn.reloaded_looks :]:
            index = next((i for i, ctx in enumerate(self.ctxs) if ctx.upload_id == ref.upload_id), None)
            if index is None:
                continue
            try:
                document = json.loads(
                    storage.read_text(evidence_key(self.case.client_id, self.case.case_id, ref.evidence_id))
                )
                inputs, output = document["result"]["input"], document["result"]["output"]
                handle = f"e{len(shown) + 1}"
                result = ToolResult(
                    evidence_id=handle,
                    tool=ref.tool,
                    args={k: v for k, v in inputs.items() if k != "upload_id"},
                    result=output,
                    created_at=ref.created_at,
                )
            except (StorageError, ValueError, KeyError, TypeError):
                continue  # a look that cannot be read back is simply not offered
            self.turn.looks.append(result)
            self.turn.files_of[handle] = index
            self.checker.grounded = self.checker.grounded | evidence_numbers(result.result, result.typed_args)
            column = result.args.get("column")
            shown.append(
                {
                    "id": handle,
                    "tool": ref.tool,
                    "file": f"F{index + 1}",
                    **({"column": self.gates[index].label(column)} if isinstance(column, str) else {}),
                }
            )
        return shown

    def _history(self, answering: bool) -> str:
        """The last turns of the conversation (before the message now being answered, when there is one). Each is shown
        whole: the AI's own words are never cut."""
        earlier = (self.case.chat[:-1] if answering else self.case.chat)[-self.limits.turn.history_turns :]
        if not earlier:
            return "(this is the first message)" if answering else "(nothing has been said yet)"
        turns = [
            {
                "who": "person" if entry.role == "user" else "you",
                "text": self._text(
                    None, clean_text(entry.text, max(self.limits.turn.chat_reply_chars, MAX_MESSAGE_CHARS))
                ),
            }
            for entry in earlier
        ]
        return json.dumps(turns, ensure_ascii=False)

    # -- the files ---------------------------------------------------------------------------------
    def _index(self, action: dict[str, Any], args: dict[str, Any]) -> int:
        """The table an action is about: a file (`F1`, `F2`, ...) or a table a query built (`T1`, ...); with one
        table it may be left out."""
        given = args.pop("file", None) or action.get("file")
        if given is None and len(self.ctxs) == 1:
            return 0
        text = str(given or "").strip().upper()
        if text not in self.labels:
            raise AgentToolError(
                "AGENT_FILE_UNKNOWN",
                f"Name a table among {', '.join(self.labels)}, not {str(given or '')[:20]!r}.",
            )
        return self.labels.index(text)

    def _file_only(self, index: int) -> None:
        """A change is made to a file; a table a query built changes only by changing its query."""
        if index >= self.nfiles:
            raise AgentToolError(
                "AGENT_TABLE_BUILT",
                f"{self.labels[index]} is a table a query built; change the query, or fix its source file.",
            )

    def _upload_of(self, index: int) -> str | None:
        """The upload a table's changes and questions are recorded against (a built table: the file it is made from)."""
        if index < self.nfiles:
            return self.ctxs[index].upload_id
        return self.built[index - len(self.files)].origin.upload_id

    def _raw_of(self, index: int) -> tuple[AgentContext, Any]:
        """The upload as it came in (`raw: true` on a look), with its own gate from its own profile."""
        if index not in self._raw:
            ctx = self._context_for(self.case, (self.files[index],))
            self._raw[index] = (ctx, _gate(ctx))
        return self._raw[index]

    def _context(self, index: int, *, raw: bool = False, **extra: Any) -> DataToolContext:
        from engine.doctor.visit import active_section

        agent, egress = self._raw_of(index) if raw else (self.ctxs[index], self.gates[index])
        return DataToolContext(
            agent=agent,
            principal=self.principal,
            caller="ai",
            egress=egress,
            files=self.files,
            case_items=active_section(self.case)[0],  # the plan so far, for `preview_working_copy`
            as_of=decisions(self.case).as_of_date,
            training_as_of=decisions(self.case).training_as_of_date,
            config_root=self.config_root,
            clock=self.clock,
            actor="agent:data-doctor",
            **extra,
        )

    # -- the loop ----------------------------------------------------------------------------------
    def _tools(self) -> list[dict[str, str]]:
        """Each look tool with its arguments, for the prompt; a tool is read on a file named `F1` .. `Fn`."""
        return [
            {
                "name": t.name,
                "description": t.description,
                "args": json.dumps(
                    {
                        "properties": t.input_model.model_json_schema().get("properties", {}),
                        "file": "/".join(self.labels),
                    }
                ),
            }
            for t in self.registry.listed_for(self._context(0), DataToolKind.READ)
            if self._callable(t)
        ]

    def _callable(self, tool: Any) -> bool:
        """Whether `_act` dispatches this READ tool: the prompt lists exactly these, no more (a listed tool that
        answers AGENT_TOOL_UNKNOWN costs the model a call). `load_goal_brief` exists only while a goal is active.
        """
        if tool.kind is not DataToolKind.READ:
            return False
        if tool.name == GOAL_BRIEF_TOOL:
            return _active_goal(self.case) is not None
        return bool(tool.session_tool or tool.name in (*M_PREVIEW_TOOLS, *M_HISTORY_TOOLS, *M_FACT_TOOLS))

    def _complete(self, prompt: Prompt, variables: dict[str, Any]) -> str:
        """One call to the model on `prompt` rendered with `variables`, after the last egress check. Raises
        `_NoAnswerError` when the prompt is too long for one call or the AI service cannot answer (unavailable, or out
        of money): what was made so far stands."""
        rendered = render(prompt, variables)
        system, masked_system = assert_clean(rendered.system)
        user, masked_user = assert_clean(rendered.user)
        if masked_system or masked_user:
            _LOG.warning("%s: a prompt was masked late", EGRESS_LATE_MASK)
            rendered = dataclasses.replace(rendered, system=system, user=user)
        if len(rendered.system) + len(rendered.user) > self.limits.turn.prompt_chars:
            raise _NoAnswerError("prompt_too_large")
        try:
            return self.meter.complete(rendered, self.purpose).text
        except (LLMError, GenerativeError) as exc:
            failed = _NoAnswerError("unavailable", exc)
            _LOG.warning("%s: the AI call failed (%s)", self.purpose.value, ", ".join(failed.codes))
            raise failed from exc

    def _event(self, said: str | None) -> str:
        """Why the model is asked now: what is owed, in words, and what the person said."""
        parts = []
        if self.owed:
            parts.append("Owed now: " + " ".join(self.owed))
        if said is not None:
            parts.append(f"THE PERSON SAYS:\n{said}")
        return "\n".join(parts) or "Nothing was said. Look at the case and do what is owed."

    def run(self, message: str | None = None) -> TurnResult:
        """Ask the model until it replies, a limit is reached or it cannot be asked. `message` is what the person
        said, when the turn answers one."""
        turn, strikes, reply, blocked = self.turn, 0, "", None
        calls, tokens = self.meter.calls_used(), self.meter.tokens_used()
        refusal: AgentToolError | None = (
            None  # the last thing the engine refused, for the person if no reply comes
        )
        wanted = ""  # the last action the AI tried
        why: tuple[str, ...] = ()  # the codes and the sentence of an AI service that did not answer
        lead = UNAVAILABLE
        answering = message is not None
        self.max_steps = self.chat_steps if answering else self.limits.turn.review_steps
        said = None if message is None else self._text(None, scrub(message[:MAX_MESSAGE_CHARS]))
        history = self._history(answering)  # names of any file, as the state shows them
        self.checker.grounded = self.checker.grounded | grounded_numbers([history, said or ""])
        prompt = load_prompt(PROMPT, self.config_root)
        tools = self._tools()
        goal = goal_line(self.brief, "see what these files can be used for")
        event = self._event(said)
        # What the person is told the AI looked at: the case as the model reads it, and the conversation.
        shown = json.dumps(self.state, ensure_ascii=False) + "\n" + history
        turn.sent.append(sent_item(self.gates[0], STATE_TOOL, {}, shown))
        lessons = approved_lessons(self.storage)
        self.checker.grounded = self.checker.grounded | grounded_numbers([list(lessons)])

        def variables_for(results: list[dict[str, Any]]) -> dict[str, Any]:
            return {
                "goal": goal,
                "tools": tools,
                **prompt_variables(self.ctxs[0].config, config_root=self.config_root, lessons=lessons),
                "questions": catalogue_lines(chat=True),
                "event": event,
                "conversation": history,
                "state": json.dumps(self.state, ensure_ascii=False),
                "turn_results": (
                    json.dumps(for_prompt(results, {}), ensure_ascii=False) if results else "none"
                ),
            }

        for _ in range(self.max_steps + 1):
            try:
                answer = self._complete(prompt, variables_for(turn.feedback))
            except _NoAnswerError as stop:
                blocked, why, lead = stop.reason, stop.codes, stop.reply
                break
            self.answered += 1
            wanted = ""  # a refusal of a reply, or of an unreadable one, did not come from an action
            try:
                action = _plain_or_action(answer)
                name = ALIASES.get(str(action["action"]), str(action["action"]))
                if name == "reply":
                    reply = self._reply(action, required=answering)
                    self.finished = True
                    break
                wanted = name
                self._take(name, action)
            except AgentToolError as exc:
                strikes += 1
                refusal = exc
                if isinstance(exc, _RefusedError):
                    self.refusals.append(exc.refusal)
                # The message goes into the feedback even when this is the last strike: the last word reads it, so
                # a reply refused for its number is not written again with the same number.
                turn.feedback.append(
                    self.gates[0].prepare(exc.refusal.feedback(), extra_text_keys=("value",))
                )
                if strikes >= self.limits.turn.strikes:
                    blocked = exc.code
                    break
            except Exception as exc:  # a model's odd input must never cost a paid turn that nobody records
                _LOG.warning("doctor turn step failed: %s", type(exc).__name__)
                blocked = "failed"
                break
        if not reply and answering and blocked not in NO_LAST_WORD and not self._spent():
            reply = self._last_word(prompt, variables_for, blocked)
            if reply:
                blocked = None  # the AI wrote this reply itself: nothing blocked it
        if not reply:
            if blocked == "unavailable":
                reply = lead
            elif answering:
                reply = self._unsaid(blocked, refusal, wanted)
        return self._result(reply, blocked, why, calls, tokens)

    def _result(
        self, reply: str, blocked: str | None, why: tuple[str, ...], calls: int, tokens: int
    ) -> TurnResult:
        turn, chosen = self.turn, self.chosen
        items, withdrawn = self._proposed()
        return TurnResult(
            reply=reply,
            actions=tuple(k.action for k in self.kept),
            case=self.work_case,
            refused=tuple(self.refusals),
            items=tuple(items),
            withdrawn=tuple(withdrawn),
            evidence=tuple(self._evidence()[0].values()),
            suggestions=tuple(chosen.suggestions.items()),
            goal=chosen.goal,
            tools=tuple(dict.fromkeys(chosen.used)),
            sent=tuple(s.model_dump(mode="json") for s in turn.sent[:MAX_SENT_ITEMS]),
            blocked_by=blocked,
            error_codes=why,
            model_calls=self.meter.calls_used() - calls,
            tokens=self.meter.tokens_used() - tokens,
            answered=self.answered,
            finished=self.finished and blocked is None,
            state_digest=self.state_digest(),
        )

    def _unsaid(self, blocked: str | None, refusal: AgentToolError | None, wanted: str) -> str:
        """What the person reads when the turn ended without a reply of the AI's own: never a bare "could not
        answer". A refusal is named with what would fix it, an action that was asked for is not hidden, and the
        person may ask again."""
        if blocked is None:
            return OUT_OF_STEPS
        if blocked == "failed":
            return STOPPED + ASK_AGAIN
        if blocked == "prompt_too_large":
            return TOO_BIG + "Ask about one file or one step at a time."
        if refusal is None:
            return f"{NOTHING_SAID} {ASK_AGAIN}"
        did = f"I tried to {_DOING.get(wanted, 'act on that')}. " if wanted else ""
        return self.gates[0].person_text(did + refused_sentence(person_reason(refusal)))

    def _spent(self) -> bool:
        """Whether the case's spending limit is reached (a meter without a limit never is)."""
        spent = getattr(self.meter, "spent", None)
        return bool(spent()) if callable(spent) else False

    def _last_word(
        self,
        prompt: Any,
        variables_for: Callable[[list[dict[str, Any]]], dict[str, Any]],
        blocked: str | None,
    ) -> str:
        """One more call, so the AI can tell the person why its turn is ending. The model is told the reason and that
        only a `reply` is accepted; the reply passes the same checks as any. An empty string when the call fails or
        what came back is not a valid reply: the fixed sentence then stands. Counted in the meter like any call.
        """
        why = (
            "You have used all the steps you may take in one message."
            if blocked is None
            else f"Your last actions were refused ({blocked}) and this message ends now."
        )
        told = [
            *self.turn.feedback,
            self.gates[0].prepare(
                {
                    "turn_ending": why,
                    "now": 'Only {"action":"reply","text":"..."} is accepted now. Tell the person plainly what '
                    "you did, what you could not do and why, and what they can ask next.",
                }
            ),
        ]
        try:
            action = _plain_or_action(self._complete(prompt, variables_for(told)))
            if ALIASES.get(str(action["action"]), str(action["action"])) != "reply":
                return ""
            return self._reply(action, required=True)
        except (_NoAnswerError, AgentToolError):
            return ""
        except Exception as exc:  # a model's odd input must never cost the turn its fixed reply
            _LOG.warning("doctor turn last word failed: %s", type(exc).__name__)
            return ""

    def _reply(self, action: dict[str, Any], *, required: bool) -> str:
        """The AI's reply, whole: it passes the number and guardrail checks and is never cut. A reply that is too
        long is refused with the limit, so the model says the same in fewer words. A turn no one is waiting on (an
        event, not a message) may end with nothing to say, and words that fail a check are left out, not refused.
        """
        said = str(action.get("text") or "").strip()
        if not said:
            if required:
                raise AgentToolError("AGENT_TEXT_REFUSED", "Say something to the person in `text`.")
            return ""
        ceiling = self.limits.turn.chat_reply_chars
        failure = self.checker.failure(said, target="doctor reply", max_chars=ceiling)
        if failure is not None and not required:
            return ""  # no one is waiting for these words: the caller says it in its own
        if failure is not None:
            if len(said) > ceiling:  # too long: the model is told the limit so that it can shorten
                other = self.checker.failure(said, target="doctor reply", max_chars=len(said))
                also = (
                    f" It also failed a check ({other}); use only numbers from a tool result or the person's words."
                    if other
                    else ""
                )
                raise AgentToolError(
                    "AGENT_TEXT_REFUSED",
                    what_failed=f"The reply is {len(said)} characters; the most allowed is {ceiling}.",
                    value=f"{len(said)} characters",
                    how_to_fix=f"Say the same in fewer words, with the result first.{also}",
                )
            raise self.checker.refuse("AGENT_TEXT_REFUSED", "reply", said, failure)
        return self.gates[0].person_text(said)

    def _own_settings(self, action: dict[str, Any]) -> None:
        """The numbers of a change the engine accepted are the AI's own settings (the window it put in its query's
        arguments): it may say them. A number it gave a look tool, or one in its query's text, is not one of them.
        """
        raw = action.get("args")
        name = ALIASES.get(str(action.get("action")), str(action.get("action")))
        if name == "propose_sql" and self.sql_failed:
            return  # a query whose own checks failed is not accepted: its settings are not yet the build's
        if name in MAKES_CHANGES_WITH_ARGS and isinstance(raw, dict):
            self.checker.grounded = self.checker.grounded | grounded_numbers(own_numbers(raw))

    sql_failed: bool = False
    """The latest `propose_sql` failed its own `expect` checks."""

    def _take(self, name: str, action: dict[str, Any]) -> None:
        """Run one action: its tool or check, then the author for what it changed."""
        raw = action.get("args")
        given = raw.get("replaces") if isinstance(raw, dict) else None
        replaced = (
            self._target(given).item_id if isinstance(given, str) else None
        )  # refused before anything is made
        gone = [i.item_id for i in self._taken_out()]
        self.replacing = tuple(dict.fromkeys([*gone, *([replaced] if replaced else [])]))
        turn = self.turn
        mark = _Mark(
            len(turn.steps),
            dict(turn.queries),
            dict(turn.combines),
            len(self.chosen.combined),
            len(turn.asks),
            len(turn.doubts),
            len(turn.requests),
        )
        self._act(action)
        self._flush(mark, action, replaced)
        self._own_settings(action)
        self.chosen.used.append(name)

    # -- the actions -------------------------------------------------------------------------------
    def _act(self, action: dict[str, Any]) -> None:
        name = ALIASES.get(str(action["action"]), str(action["action"]))
        raw = action.get("args")
        args = dict(raw) if isinstance(raw, dict) else {}
        if name == "propose_step":
            self._step(action, args)
        elif name == "propose_sql":
            self._sql(args)
        elif name == "propose_features":
            self._features(action, args)
        elif name == "propose_combine_rows":
            self._combine_rows(action, args)
        elif name == "propose_unpivot":
            self._unpivot(action, args)
        elif name in {"propose_amount_outcome", "propose_test_column"}:
            from engine.doctor.agent_amount import run_amount_outcome, run_test_column

            index = self._index(action, args)
            run = run_amount_outcome if name == "propose_amount_outcome" else run_test_column
            run(self, index, dict(self.gates[index].unalias_args(args)))
        elif name in {"join_files", "stack_files"}:
            self._combine(name, args)
        elif name == "read_guide":
            self._guide(args)
        elif name == "ask":
            self._ask(action, args)
        elif name == "express_doubt":
            self._doubt(action, args)
        elif name == "request_data":
            self._request(action, args)
        elif name == "withdraw":
            self._withdraw(args)
        elif name == "propose_setting":
            self._setting(args)
        elif name == "adopt_suggestion":
            self._adopt(args)
        elif name == "suggest_answer":
            self._suggest(args)
        elif name == "propose_lesson":
            self._lesson(args)
        elif name == "propose_goal":
            self._goal(args)
        elif (tool := self.registry.find(name, self._context(0))) is not None and self._callable(tool):
            self._look(name, action, args)
        else:
            raise AgentToolError("AGENT_TOOL_UNKNOWN", f"There is no action called {name[:40]!r} here.")

    def _look(self, name: str, action: dict[str, Any], args: dict[str, Any]) -> None:
        turn = self.turn
        index = self._index(action, args)
        raw = bool(args.pop("raw", False)) and index < self.nfiles  # the upload as it came in, explicitly
        gate = self._raw_of(index)[1] if raw else self.gates[index]
        args = dict(gate.unalias_args(args))
        if name == "find_values":
            _limit_searches(
                self._raw_of(index)[0] if raw else self.ctxs[index],
                self.sessions[index],
                [r for r in turn.looks if turn.files_of[r.evidence_id] == index],
                args,
            )
        if name == GOAL_BRIEF_TOOL:  # only the active goal's brief: the goal comes from the case
            goal = _active_goal(self.case)
            if goal is None or args.setdefault("goal_id", goal.goal_id) != goal.goal_id:
                raise AgentToolError("AGENT_GOAL_UNKNOWN", "Only the active goal's brief can be loaded.")
            # the visit's own mode, as the frozen brief has it: the AI is never told the outcome is optional
            args["mode"] = (
                self.brief.mode if self.brief is not None and self.brief.goal_id == goal.goal_id else "train"
            )
        evidence_id = f"r{len(turn.looks) + 1}"
        result: ToolResult
        if (
            name in (*M_PREVIEW_TOOLS, *M_HISTORY_TOOLS, *M_FACT_TOOLS) or name == GOAL_BRIEF_TOOL
        ):  # a registry READ tool of its own
            made = self.registry.call(name, args, self._context(index, raw=raw))
            result = ToolResult(
                evidence_id=evidence_id,
                tool=name,
                args=made.input,
                supplied=args,
                result=made.output,
                created_at=made.computed_at,
            )
            self._collect_suggestions(made.output)
        else:
            result = self.registry.session_call(
                name, args, self._context(index, raw=raw), evidence_id=evidence_id
            )
        turn.looks.append(result)
        turn.files_of[evidence_id] = index
        self.checker.grounded = self.checker.grounded | evidence_numbers(result.result, result.typed_args)
        self._record(index, result, frozenset(), gate=gate)

    def _collect_suggestions(self, output: Any) -> None:
        """The changes a fact tool suggests (`suggested`: `{id, action, reason}` each): the only ones the AI may adopt
        and be sure of. One that does not read as an action is not offered."""
        found = output.get("suggested") if isinstance(output, dict) else None
        for row in found if isinstance(found, list) else []:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                continue
            try:
                suggestion = _Suggestion(
                    str(row["id"]), _ACTION.validate_python(row.get("action")), str(row.get("reason", ""))
                )
            except ValidationError:
                continue
            self.suggestions.setdefault(suggestion.id, suggestion)
            self._name("s", suggestion.id)

    def _record(self, index: int, result: ToolResult, keys: frozenset[str], *, gate: Any = None) -> None:
        """Put one result in the next prompt through its file's gate, noting what is sent."""
        sent: list[SentItem | None] = []
        gate, feedback = gate or self.gates[index], self.turn.feedback
        _record(gate, {}, feedback, sent, result, keys)
        feedback[-1]["file"] = self.labels[index]
        self.turn.sent.extend(s for s in sent if s is not None)

    def _step(self, action: dict[str, Any], args: dict[str, Any]) -> None:
        """A typed change, also by leaving out the rows a rule names; what it measured grounds the reply."""
        turn = self.turn
        index = self._index(action, args)
        self._file_only(index)
        args = dict(self.gates[index].unalias_args(args))
        mine = [r for r in turn.looks if turn.files_of[r.evidence_id] == index]
        result, proposal = _propose_step(
            self.ctxs[index],
            self.sessions[index],
            args,
            mine,
            self.checker,
            f"s{len(turn.steps) + 1}",
            f"p{len(turn.steps) + 1}",
            self.clock,
            kinds=DOCTOR_STEP_KINDS,
        )
        turn.steps.append((index, result, proposal))
        turn.looks.append(result)  # a later step may cite what this one measured
        turn.files_of[result.evidence_id] = index
        self.checker.grounded = self.checker.grounded | evidence_numbers(result.result, result.typed_args)
        self._record(index, result, STEP_TEXT_KEYS)

    def _sql(self, args: dict[str, Any]) -> None:
        turn = self.turn
        names = self.labels  # a query reads the files and the tables other queries built (`T1`..)
        upload = {name: ctx.upload_id for name, ctx in zip(names, self.ctxs, strict=False)}
        wanted = args.get("files") or names[:1]
        if not isinstance(wanted, list) or not all(w in upload for w in wanted):
            raise AgentToolError("AGENT_FILE_UNKNOWN", f"`files` lists tables among {', '.join(names)}.")
        expect = args.get("expect")
        if not isinstance(expect, dict) or not expect:
            raise AgentToolError(
                "AGENT_EXPECT_MISSING",
                "State what must hold before the query runs: `expect` with rows_out_equals, rows_out_min, "
                "rows_out_max, keys_unique, totals_kept, max_keys_lost_pct or max_keys_repeated.",
            )
        try:
            expectations = Expectations.model_validate(expect)
        except ValidationError as exc:
            raise AgentToolError("AGENT_EXPECT_INVALID", _expect_problems(exc)) from exc
        given: dict[str, Any] = {k: v for k, v in args.items() if k in SQL_ARGS}
        description = str(args.get("description") or args.get("reason") or "")
        if len(description) > self.limits.turn.description_chars:
            raise AgentToolError(
                "AGENT_DESCRIPTION_TOO_LONG",
                f"The description is {len(description)} characters; the most allowed is "
                f"{self.limits.turn.description_chars}. Say what the query does in fewer words.",
            )
        given["description"] = description
        given["upload_ids"] = [upload[w] for w in wanted]
        if args.get("as_of_file") in upload:
            given["as_of_upload_id"] = upload[str(args["as_of_file"])]
        # the person's own as-of answer is `$as_of` for the step unless it names another: the trial and the run
        # bind it (`DataToolContext.as_of`, `materialize`), so it is not frozen into the step
        name = str(given.get("output_name", ""))
        if name in turn.closed:
            raise AgentToolError(
                "AGENT_RETRIES_USED",
                f"'{name[:60]}' has failed its own checks too often; it is left for the person.",
            )
        first = next(i for i, ctx in enumerate(self.ctxs) if ctx.upload_id == given["upload_ids"][0])
        home = self._upload_of(first)
        key = decisions(self.case).key(home) if home else None
        result = self.registry.call(
            "propose_sql_step", given, self._context(first, primary_key=key, replacing=self.replacing)
        )
        proposal = SqlStepProposal.model_validate(result.output)
        if proposal.receipt.key_missing:
            kept = proposal.receipt.key_missing
            raise AgentToolError(
                "AGENT_KEY_DROPPED",
                f"The query's result has no column that holds the customer ID {kept}, under that name or another, "
                f"so the customers it keeps cannot be counted. Keep {kept} in the result (select it, or group by "
                "it) and propose it again.",
            )
        order = turn.orders.setdefault(name, len(turn.orders) + 1)
        changed = turn.book.declare(order, expectations)
        outcome = turn.book.check(order, proposal.receipt)
        self.sql_failed = bool(outcome.failures)
        prior = turn.queries[name].findings if name in turn.queries else ()
        rewrites = [f for f in prior if f.code.startswith("EXPECTATION_CHANGED")]  # a rewrite stays flagged
        findings = (*proposal.findings, *rewrites, *changed, *outcome.findings)
        turn.queries[name] = _Sql(result, tuple(findings))
        if outcome.must_ask:
            turn.closed.add(name)
        shown = {
            "evidence_id": result.evidence_id,
            "file": names[first],
            "tool": "propose_sql",
            "result": self.gates[first].prepare(
                {
                    "rows_in": proposal.rows_in,
                    "rows_out": proposal.rows_out,
                    "columns_out": proposal.columns_out,
                    "receipt": proposal.receipt.model_dump(mode="json"),
                    "findings": [f.model_dump(mode="json") for f in findings],
                    "failed_checks": list(outcome.failures),
                    "retries_left": outcome.retries_left,
                    **(
                        {
                            "counted_on": f"a sample of {self.limits.trial.dry_run_rows:,} rows of each file: "
                            "these counts are estimates, say 'about' or wait for the whole file's count"
                        }
                        if proposal.sampled
                        else {"counted_on": "the whole file"}
                    ),
                },
                extra_text_keys=frozenset(
                    {"message", "code", "level", "key", "column", "kind", "counted_on"}
                ),
            ),
        }
        # what the AI expected and did not get is its prediction, not a fact: only what our code counted is grounded
        counted = {k: v for k, v in shown["result"].items() if k != "failed_checks"}
        counted["findings"] = [
            f
            for f in counted.get("findings", [])
            if not (isinstance(f, dict) and f.get("code") == "EXPECTATION_FAILED")
        ]
        self.checker.grounded = self.checker.grounded | grounded_numbers([counted])
        turn.feedback.append(shown)

    def _guide(self, args: dict[str, Any]) -> None:
        """`read_guide`: the guide's rules and a section, each part once per turn (a repeat says it is above).

        A lookup the guides cannot answer is a read-only miss, not a strike: the refusal, with the valid names or
        sections, goes to the model as feedback.
        """
        from engine.doctor.guides import ALREADY_READ, RULES, read_guide

        name, section = args.get("name"), args.get("section")
        lessons = approved_lessons(self.storage)
        try:
            key, title, lines = read_guide(name, section, self.config_root, lessons=lessons)
        except AgentToolError as exc:
            self.turn.feedback.append(
                self.gates[0].prepare(
                    {"tool": "read_guide", **exc.refusal.feedback()}, extra_text_keys=("value",)
                )
            )
            return
        carried = self.turn.guides.setdefault(key, set())
        shown: dict[str, Any] = {"tool": "read_guide", "guide": key, "section": title or "all"}
        wanted = "*" if title is None else title
        if "*" in carried or wanted in carried:
            shown["text"] = ALREADY_READ
        else:
            if title is not None and RULES in carried:
                lines = read_guide(key, title, self.config_root, rules=False, lessons=lessons)[2]
            carried.add(wanted)
            carried.add(RULES)
            shown["text"] = lines
        self.turn.feedback.append(shown)

    def _unpivot(self, action: dict[str, Any], args: dict[str, Any]) -> None:
        from engine.doctor.agent_unpivot import run_unpivot

        index = self._index(action, args)
        run_unpivot(self, index, dict(self.gates[index].unalias_args(args)))

    def _features(self, action: dict[str, Any], args: dict[str, Any]) -> None:
        from engine.doctor.agent_features import run_features

        index = self._index(action, args)
        run_features(self, index, dict(self.gates[index].unalias_args(args)))

    def _combine_rows(self, action: dict[str, Any], args: dict[str, Any]) -> None:
        from engine.doctor.agent_reshape import run_combine_rows

        index = self._index(action, args)
        self._file_only(index)  # rows of a file, never of a table a query built
        run_combine_rows(self, index, dict(self.gates[index].unalias_args(args)))

    def _combine(self, name: str, args: dict[str, Any]) -> None:
        """A join or a stack of the named files: the rules' own plan, counts and guards on exactly those files."""
        from engine.doctor.combine_run import JOIN_KIND, STACK_KIND
        from engine.doctor.combining import propose_combination

        wanted = args.get("files")
        taken = [str(f) for f in wanted] if isinstance(wanted, list) else []
        indexes = [self._index({}, {"file": f}) for f in taken]
        for index in indexes:
            self._file_only(index)  # a join or a stack is of files, never of a table a query built
        if len(set(indexes)) < 2:
            raise AgentToolError(
                "AGENT_FILE_UNKNOWN", 'Name two or more different files, such as ["F1", "F2"].'
            )
        kind = JOIN_KIND if name == "join_files" else STACK_KIND
        found, asks = propose_combination(
            self.case, tuple(self.files[i] for i in sorted(set(indexes))), self.clock()
        )
        mine = [item for item in found if item.kind == kind]
        if (
            not mine and asks
        ):  # the rules need an answer first (two exports disagree): it is asked, not guessed
            for ask in asks:
                self._record_ask(ask)
            self.turn.feedback.append(
                {
                    "tool": name,
                    "result": "I need the person's answer before these files can be combined; I asked.",
                }
            )
            return
        if not mine:
            raise AgentToolError(
                "AGENT_COMBINE_NONE",
                f"The rules find no {'join' if kind == JOIN_KIND else 'stack'} for these files: "
                "their customer IDs do not meet, or they are not the same report.",
            )
        gate = self.gates[0]
        for item in mine:
            self.chosen.combined.append(
                item.model_copy(
                    update={
                        "source": ProposalSource.AI,
                        "state": ProposalState.PENDING,
                        "confidence": AgentConfidence.CHECK,
                    }
                )
            )
            self.checker.grounded = self.checker.grounded | grounded_numbers(
                [gate.text(clean_text(item.title, 600))]
            )
        for ask in asks:
            self._record_ask(ask)

    # -- questions ---------------------------------------------------------------------------------
    def _ask(self, action: dict[str, Any], args: dict[str, Any]) -> None:
        """A catalogue question, or a quick one (`text` and `options`); its id and answers go back to the model
        so it can suggest one."""
        ask = self._catalogue_ask(action, args) if "question" in args else self._quick(action, args)
        qid = question_id_of(ask.code, ask.subject, ask.goal_id)
        self._name("q", qid)
        self.chosen.asked[qid] = ask
        gate = self._gate_of(ask.upload_id)
        self.turn.feedback.append(
            {
                "asked": ask.code,
                "question_id": self._name("q", qid),
                "options": [{"id": o.effect.option_id, "label": gate.text(o.label)} for o in ask.options],
            }
        )

    def _catalogue_ask(self, action: dict[str, Any], args: dict[str, Any]) -> Ask:
        turn = self.turn
        index = self._index(action, args)
        ctx = self.ctxs[index]
        args = dict(self.gates[index].unalias_args(args))
        values, choices = args.get("values"), args.get("choices")
        evidence = args.get("evidence_ids")
        known = {r.evidence_id for r in turn.looks}
        cited = tuple(e for e in evidence if e in known) if isinstance(evidence, list) else ()
        try:
            ask = build_ask(
                str(args.get("question", "")),
                dict(values) if isinstance(values, dict) else {},
                rows=len(ctx.frame),
                choices=[str(c) for c in choices] if isinstance(choices, list) else (),
                choice_says=self._says(index, args),
                upload_id=self._upload_of(index),
                column=ctx.resolve(str(args["column"])) if isinstance(args.get("column"), str) else None,
                evidence_ids=tuple(_hashed(turn, cited)),
            )
        except QuestionError as exc:
            raise AgentToolError(
                "AGENT_QUESTION_INVALID", str(exc)[: self.limits.turn.description_chars]
            ) from exc
        for text in (ask.text, ask.why, *(o.label for o in ask.options)):
            failure = self.checker.failure(
                text, target="doctor question", max_chars=self.limits.turn.question_chars
            )
            if failure is not None:
                raise self.checker.refuse("AGENT_QUESTION_REFUSED", "question", text, failure)
        if ask.blocking and turn.blocking >= self.limits.asks.blocking:
            raise AgentToolError(
                "AGENT_BLOCKING_LIMIT",
                f"You may ask {self.limits.asks.blocking} blocking questions in one turn; this one is dropped.",
            )
        turn.blocking += int(ask.blocking)
        self._record_ask(ask)
        return ask

    def _quick(self, action: dict[str, Any], args: dict[str, Any]) -> Ask:
        index = self._index(action, args)
        ctx = self.ctxs[index]
        args = dict(self.gates[index].unalias_args(args))
        text, why = str(args.get("text") or "").strip(), str(args.get("why") or "").strip()
        given = args.get("options")
        # "I do not know" is added to every question by the protocol: the AI's own copy would show twice.
        options = (
            [o.strip() for o in given if isinstance(o, str) and not _NO_ANSWER.match(o)]
            if isinstance(given, list)
            else []
        )
        if (
            not text
            or not self.limits.asks.quick_options_min <= len(options) <= self.limits.asks.quick_options_max
            or len(set(options)) != len(options)
            or not all(options)
        ):
            raise AgentToolError(
                "AGENT_QUESTION_INVALID",
                f"A quick question needs `text` and {self.limits.asks.quick_options_min} to {self.limits.asks.quick_options_max} different `options`, each a short sentence.",
            )
        if self.chosen.quick >= self.limits.asks.quick:
            raise AgentToolError("AGENT_BLOCKING_LIMIT", "That is enough quick questions for one message.")
        for part in (text, why, *options):
            if len(part) > self.limits.turn.short_chars:
                raise AgentToolError("AGENT_QUESTION_INVALID", "Keep the question and its options short.")
            about_rows = bool(_ABOUT_ROWS.search(text))
            tripped = _MEANING_TOPICS.search(part) or (None if about_rows else _SALE_WORDS.search(part))
            if tripped:
                word = _whole_word(part, tripped)
                raise AgentToolError(
                    "AGENT_QUESTION_REFUSED",
                    what_failed=f"The word '{word}' in {_where(part, text, why)} is about consent, the outcome, "
                    "the ID or the date, which only the question list asks.",
                    value=word,
                    how_to_fix="Say it without that word (a quick question about what some rows are, such as "
                    "negative or cancelled ones, is fine), or use the question list.",
                )
            if number := _NUMBER_WORD.search(part):
                raise AgentToolError(
                    "AGENT_QUESTION_REFUSED",
                    what_failed=f"The number '{number.group(0)}' in {_where(part, text, why)}: a quick question "
                    "states no numbers.",
                    value=number.group(0),
                    how_to_fix="Take the number out; for what counts as a yes use the question list.",
                )
            failure = (
                self.checker.failure(
                    part, target="doctor question", max_chars=self.limits.turn.question_chars
                )
                if part
                else None
            )
            if failure is not None:
                raise self.checker.refuse("AGENT_QUESTION_REFUSED", "question", part, failure)
        gate = self.gates[index]
        shown = [gate.person_text(o) for o in options]
        ask = Ask(
            code=QUICK_CODE,
            subject={"text": text},
            text=gate.person_text(text),
            why=gate.person_text(why),
            options=tuple(
                AskOption(label=label, effect=OptionEffect(option_id=f"c{n}"))
                for n, label in enumerate(shown)
            ),
            rows=len(ctx.frame),
            upload_id=ctx.upload_id,
        )
        self.chosen.quick += 1
        self._record_ask(ask)
        return ask

    def _record_ask(self, ask: Ask) -> None:
        """A question for the person; the author settles it (proven, assumed or asked within the file's budget) once
        the action that asked it is done (`_flush`)."""
        self.turn.asks.append(ask)

    def _suggest(self, args: dict[str, Any]) -> None:
        """Point at one answer of an open question. Nothing is recorded; the person's click does that."""
        raw_qid = str(args.get("question_id", ""))
        qid = self.handles.get(raw_qid, "")
        oid = str(args.get("option_id", ""))
        question: Any = self.chosen.asked.get(qid) or next(
            (q for q in self.case.questions if q.question_id == qid and q.answer is None), None
        )
        labels = (
            {
                getattr(o, "option_id", None) or o.effect.option_id: o.label
                for o in question.options
                if o.label != DONT_KNOW
            }
            if question is not None
            else {}
        )
        if oid not in labels:
            open_q = [
                self._name("q", known)
                for known in dict.fromkeys(
                    [*self.chosen.asked, *(q.question_id for q in self.case.questions if q.answer is None)]
                )
            ]
            raise AgentToolError(
                "ITEM_NOT_FOUND",
                what_failed=(
                    f"There is no open question '{raw_qid[:40]}' with a choice '{oid[:40]}'."
                    if question is None
                    else f"'{oid[:40]}' is not a choice of question '{raw_qid[:40]}'."
                ),
                value=oid[:40] if question is not None else raw_qid[:40],
                how_to_fix="Suggest an answer of a question that is open, by its ids. "
                f"Open questions: {', '.join(open_q) or 'none'}."
                + (f" Choices of '{raw_qid[:40]}': {', '.join(sorted(labels))}." if labels else ""),
            )
        self.chosen.suggestions[qid] = oid
        self.turn.feedback.append({"suggested": raw_qid})

    def _checked(self, texts: Sequence[str], target: str, index: int) -> list[str]:
        """The model's own words for the person, each through the number and guardrail checks."""
        for text in texts:
            failure = (
                self.checker.failure(text, target=target, max_chars=self.limits.turn.question_chars)
                if text
                else None
            )
            if failure is not None:
                raise self.checker.refuse("AGENT_TEXT_REFUSED", target, text, failure)
        return [self.gates[index].person_text(text) if text else "" for text in texts]

    def _doubt(self, action: dict[str, Any], args: dict[str, Any]) -> None:
        turn = self.turn
        index = self._index(action, args)
        args = dict(self.gates[index].unalias_args(args))
        meaning = args.get("meaning") is True
        if len(turn.doubts) >= self.limits.asks.doubts or (
            meaning and turn.blocking >= self.limits.asks.blocking
        ):
            raise AgentToolError(
                "AGENT_DOUBT_LIMIT", "That is enough doubts for one turn; this one is dropped."
            )
        parts = [str(args.get(key) or "").strip() for key in ("because", "would_do", "if_not")]
        because, would_do, if_not = self._checked(parts, "doctor doubt", index)
        evidence = args.get("evidence_ids")
        known = {r.evidence_id: r for r in turn.looks}
        cited = (
            {evidence_id_of(known[e]): known[e].result for e in evidence if e in known}
            if isinstance(evidence, list)
            else {}
        )
        try:
            doubt = express_doubt(
                "AI_DOUBT",
                because=because,
                would_do=would_do,
                if_not=if_not,
                evidence=cited,
                names=self.checker.names,
                upload_id=self._upload_of(index),
            )
        except ProtocolError as exc:
            raise AgentToolError("AGENT_DOUBT_REFUSED", exc.message) from exc
        if doubt.doubt_id in {d.doubt_id for d in turn.doubts}:
            return
        turn.doubts.append(doubt)
        if meaning:  # only a doubt about meaning stops approval; it never changes data
            turn.blocking += 1
            self._record_ask(doubt_ask(doubt))
        turn.feedback.append({"doubt": "noted"})

    def _request(self, action: dict[str, Any], args: dict[str, Any]) -> None:
        turn = self.turn
        index = self._index(action, args)
        args = dict(self.gates[index].unalias_args(args))
        if len(turn.requests) >= self.limits.asks.requests:
            raise AgentToolError(
                "AGENT_REQUEST_LIMIT", "That is enough requests for one turn; this is dropped."
            )
        kind = args.get("kind", "more_data")
        columns = args.get("columns")
        named = (
            [str(c).strip() for c in columns[: self.limits.asks.request_columns]]
            if isinstance(columns, list)
            else []
        )
        given = [str(args.get(k) or "").strip() for k in ("what", "why", "per", "ask_whom")]
        if kind not in {"more_data", "different_file"} or not given[0] or not given[1]:
            raise AgentToolError(
                "AGENT_REQUEST_INVALID",
                "A request needs `kind` (more_data or different_file), `what` is missing, and `why` it is needed.",
            )
        what, why, per, whom, *shape = self._checked([*given, *named], "doctor request", index)
        request = DataRequest(
            kind=kind,
            what=what,
            why=why,
            columns=tuple(shape),
            per=per or "one row per customer",
            ask_whom=whom or None,
            upload_id=self._upload_of(index),
        )
        turn.requests.append(request)
        turn.feedback.append({"requested": kind})

    def _says(self, index: int, args: dict[str, Any]) -> list[str]:
        """The model's sentence for what each of its `choices` does, kept only where it is short, states no number
        and passes the checks; any other choice gets the catalogue's own sentence (`build_ask` falls back, it never
        invents)."""
        given = args.get("effects")
        if not isinstance(given, list):
            return []
        kept: list[str] = []
        for item in given:
            text = item.strip() if isinstance(item, str) else ""
            ok = bool(text) and not _DIGIT.search(text)
            ok = (
                ok
                and self.checker.failure(
                    text, target="doctor question", max_chars=self.limits.turn.effect_chars
                )
                is None
            )
            kept.append(self.gates[index].person_text(text) if ok else "")
        return kept

    # -- pointing ----------------------------------------------------------------------------------
    def _ready(self) -> bool:
        """The active goal's latest readiness verdict is ready (with or without warnings) for the plan as it stands.
        The verdict and its plan digest say it, whatever state the visit is in.
        """
        from engine.doctor.visit import active_goal, prescription_digest, stored_brief

        goal = active_goal(self.case)
        ref = (
            None
            if goal is None
            else next((r for r in self.case.readiness if r.goal_id == goal.goal_id), None)
        )
        if ref is None or ref.verdict not in {"ready", "ready_with_warnings"} or self.storage is None:
            return False
        now = prescription_digest(self.case, stored_brief(self.storage, self.case))
        return (
            ref.plan_digest
            or (self.case.prescriptions[-1].prescription_hash if self.case.prescriptions else "")
        ) == now

    def _lesson(self, args: dict[str, Any]) -> None:
        """`propose_lesson`: a general rule for later cases, kept as proposed once the case is ready. Only the owner
        approves it (`POST /doctor/lessons/{id}/approve`); a lesson with a number, a quote or a name is refused.
        """
        from engine.doctor.lessons import LessonError, LessonStore

        if self.storage is None or not self._ready():
            self.turn.feedback.append({"lesson": "not yet: propose lessons once the case is ready"})
            return
        try:
            lesson = LessonStore(self.storage).propose(
                args.get("text"), self.case.case_id, self._case_words()
            )
        except LessonError as exc:
            self.turn.feedback.append(
                self.gates[0].prepare(Refusal(exc.code, exc.message).feedback(), extra_text_keys=("value",))
            )
            return
        self.turn.feedback.append({"lesson": "proposed: waits for the owner", "status": lesson.status})

    def _goal(self, args: dict[str, Any]) -> None:
        """`propose_goal`: point at one goal that fits the files (ready, check, or buildable from their events) while
        no goal is chosen. Nothing is chosen: the reply carries a "Choose this goal" button and only the click sets it
        (the same call as choosing from the goal cards)."""
        from engine.doctor.visit import active_goal, fitting_goals

        goal = str(args.get("goal_id") or "").strip()
        if active_goal(self.case) is not None:
            raise AgentToolError("AGENT_GOAL_SET", "A goal is already chosen; only the person changes it.")
        if goal not in {str(u.get("use_case_id")) for u in fitting_goals(self.case)}:
            raise AgentToolError(
                "AGENT_GOAL_UNKNOWN",
                "Name a goal that fits these files, by its id (`goals_that_fit` in the case state).",
            )
        reason = str(args.get("reason") or "").strip()
        if not reason:
            raise AgentToolError("AGENT_REASON_MISSING", "Say why this goal, in a sentence.")
        failure = self.checker.failure(reason, target="doctor reply", max_chars=self.limits.turn.effect_chars)
        if failure is not None:
            raise AgentToolError(
                "AGENT_TEXT_REFUSED",
                f"The reason failed a check ({failure}); at most {self.limits.turn.effect_chars} characters, "
                "and only numbers from a tool result or the person's words.",
            )
        self.chosen.goal = (goal, self.gates[0].person_text(reason))
        self.turn.feedback.append({"goal": "proposed: waits for the person's click", "goal_id": goal})

    def _case_words(self) -> list[str]:
        """The case's column names, file names and profile top values, for the lesson check only (never sent)."""
        words: list[str] = [f.file_name for f in self.case.files if f.file_name]
        words += [f.file_name.rsplit(".", 1)[0] for f in self.case.files if f.file_name]
        for ctx in self.ctxs:
            for column in ctx.profile.columns:
                words.append(column.name)
                words += [top.value for top in column.top_categories]
        return words

    # -- the author --------------------------------------------------------------------------------
    def _ctx(self) -> ApplyContext:
        """What the AI's actions are applied under: it is never sure of a change but by a suggestion a fact tool made
        this turn, and the person whose message caused the turn is recorded as who asked."""
        asked = self.case.chat[-1] if self.case.chat and self.case.chat[-1].role == "user" else None
        if self._roles is None:
            self._roles = roles_of(self.case, self.config_root)
        return ApplyContext(
            now=self.now,
            author="ai",
            roles=self._roles,
            settings=self.protocol,
            goal=self.goal_settings,
            complete=False,
            requested_by=None if asked is None else asked.by,
            suggested=frozenset(self.suggestions),
        )

    def _apply(
        self,
        action: AuthorAction,
        *,
        key: tuple[str, str] | None = None,
        drop: Iterable[int] = (),
    ) -> Refusal | None:
        """Put `action` through the author on the turn's working case. A refused action is not kept (the caller tells
        the model); a later try of the same query (`key`) replaces the earlier one, and `drop` cancels actions of
        this turn that the new one takes back, so the plan holds one try, not both."""
        ctx = self._ctx()
        gone = set(drop)
        redo = bool(gone) or (key is not None and any(k.key == key for k in self.kept))
        if redo:
            kept = [k for n, k in enumerate(self.kept) if n not in gone and (key is None or k.key != key)]
            work, rebuilt = self.case, []
            for earlier in kept:  # the actions that stay, in order, from the case the turn began with
                applied = apply_actions(work, [earlier.action], ctx)
                if applied.refused:
                    continue  # it leaned on a try that is gone
                rebuilt.append(_Kept(earlier.action, earlier.key, tuple(applied.made)))
                work = applied.case
        else:
            work, rebuilt = self.work_case, list(self.kept)
        applied = apply_actions(work, [action], ctx)
        if applied.refused:
            return applied.refused[0][1]
        rebuilt.append(_Kept(action, key, tuple(applied.made)))
        self.kept, self.work_case = rebuilt, applied.case
        return None

    def _target(self, given: object) -> CaseItem:
        """The change a handle or id names, among the case's and this turn's: a refusal that lists the valid handles
        when there is none, and when it is already left out."""
        text = str(given or "").strip()
        item_id = self.handles.get(text, text)
        found = next((i for i in self.work_case.items if i.item_id == item_id), None)
        if found is None:
            live = [
                self._name("c", i.item_id)
                for i in self.work_case.items
                if i.state is not ProposalState.REJECTED
            ]
            raise _RefusedError(
                Refusal(
                    code="ITEM_NOT_FOUND",
                    what_failed=f"There is no change “{text[:40]}” in the plan.",
                    value=text[:40],
                    how_to_fix="Use the id of a change in the plan, as the state shows it"
                    + (f": {', '.join(live[:8])}." if live else "; the plan is empty."),
                )
            )
        if found.state is ProposalState.REJECTED:
            raise _RefusedError(
                Refusal(
                    code="ITEM_ALREADY_LEFT_OUT",
                    what_failed=f"“{found.title[:80]}” is already left out of the plan.",
                    value=self._name("c", found.item_id),
                    how_to_fix="Nothing to take out or replace; check the plan for what is still in it.",
                )
            )
        return found

    def _made_here(self, item_id: str) -> int | None:
        """The position of the kept action of this turn that made the item, if one did."""
        return next(
            (n for n, k in enumerate(self.kept) if item_id in k.made and item_id not in self._began()), None
        )

    def _began(self) -> set[str]:
        return {i.item_id for i in self.case.items}

    def _taken_out(self) -> list[CaseItem]:
        """The items the turn has taken out or asked to take out so far."""
        ids = [
            *(k.action.item_id for k in self.kept if isinstance(k.action, Withdraw)),
            *(k.action.replaces for k in self.kept if isinstance(k.action, Create) and k.action.replaces),
        ]
        return [i for i in self.case.items if i.item_id in set(ids)]

    def _withdraw(self, args: dict[str, Any]) -> None:
        """`withdraw`: take any change out of the plan, with its reason. What the turn made itself is simply not
        made; one that waits is taken out now; one in the plan, or the person's, waits for their yes."""
        item = self._target(args.get("item"))
        reason, consequence = (
            str(args.get("reason") or "").strip(),
            str(args.get("consequence") or "").strip(),
        )
        if not reason or not consequence:
            raise AgentToolError(
                "AGENT_REASON_MISSING",
                "Say why (`reason`) and what changes in the training table without it (`consequence`).",
            )
        here = self._made_here(item.item_id)
        if here is not None:
            self._apply_cancelling(here)
            self.turn.feedback.append(
                {
                    "withdrawn": self._name("c", item.item_id),
                    "result": "It was made in this very turn, so it is not made.",
                }
            )
            return
        action = Withdraw(item_id=item.item_id, reason=reason, consequence=consequence)
        refusal = self._apply(action)
        if refusal is not None:
            raise _RefusedError(self._for_the_model(refusal))
        waiting = any(i.kind == "withdraw" and i.replaces == item.item_id for i in self.work_case.items)
        self.turn.feedback.append(
            {
                "withdrawn": self._name("c", item.item_id),
                "result": (
                    "It is in the plan or the person's, so taking it out waits for their yes."
                    if waiting
                    else "It was waiting, so it is taken out now."
                ),
            }
        )

    def _apply_cancelling(self, position: int) -> None:
        """Take back an action of this turn: the turn goes on as if it had never been made."""
        kept = [k for n, k in enumerate(self.kept) if n != position]
        work, rebuilt = self.case, []
        ctx = self._ctx()
        for earlier in kept:
            applied = apply_actions(work, [earlier.action], ctx)
            if applied.refused:
                continue
            rebuilt.append(_Kept(earlier.action, earlier.key, tuple(applied.made)))
            work = applied.case
        self.kept, self.work_case = rebuilt, work

    def _setting(self, args: dict[str, Any]) -> None:
        """`propose_setting`: one setting of the chosen goal to a value its config allows."""
        goal = _active_goal(self.case)
        if goal is None:
            raise AgentToolError("AGENT_GOAL_UNKNOWN", "No goal is chosen, so there is no setting to set.")
        setting, value = str(args.get("setting") or "").strip(), str(args.get("value") or "").strip()
        reason = str(args.get("reason") or "").strip()
        if not (setting and value and reason):
            raise AgentToolError("AGENT_REASON_MISSING", "A setting needs `setting`, `value` and `reason`.")
        refusal = self._apply(
            SetSetting(goal_id=goal.goal_id, setting=setting, value=value, reason=reason),
            key=("setting", f"{goal.goal_id}:{setting}"),
        )
        if refusal is not None:
            raise _RefusedError(self._for_the_model(refusal))
        self.turn.feedback.append(
            {
                "setting": setting,
                "result": "Proposed: it waits for the person's yes, and the build is made again.",
            }
        )

    def _adopt(self, args: dict[str, Any]) -> None:
        """`adopt_suggestion`: a change a fact tool suggested this turn, as it stands. Only this makes the AI sure of
        it; the author checks the basis against the suggestions of the turn."""
        given = str(args.get("id") or "").strip()
        reason = str(args.get("reason") or "").strip()
        suggestion = self.suggestions.get(self.handles.get(given, given))
        if suggestion is None:
            offered = [self._name("s", s) for s in self.suggestions]
            raise _RefusedError(
                Refusal(
                    code="ITEM_NOT_FOUND",
                    what_failed=f"There is no suggestion “{given[:40]}” this turn.",
                    value=given[:40],
                    how_to_fix="Use an id under `suggested` in the state: "
                    + (", ".join(offered[:SUGGESTIONS_SHOWN]) or "there are none")
                    + ".",
                )
            )
        if not reason:
            raise AgentToolError("AGENT_REASON_MISSING", "Say why you adopt it, in a sentence.")
        action = suggestion.action
        if isinstance(action, (Create, Withdraw, SetSetting)):
            action = action.model_copy(update={"reason": reason})
        if isinstance(action, Create):
            landed = self._put(
                action.draft,
                reason=action.reason,
                replaced=action.replaces,
                key=("suggestion", suggestion.id),
                basis=action.basis,
            )
            if landed is not None:
                self._tell([landed])
        else:
            self._author(action)
            self.turn.feedback.append({"adopted": given})

    def _for_the_model(self, refusal: Refusal) -> Refusal:
        """A refusal with the ids it names as the model reads them (handles), not the case's own."""
        if refusal.code == "ITEM_NOT_FOUND":
            live = [
                self._name("c", i.item_id)
                for i in self.work_case.items
                if i.state is not ProposalState.REJECTED
            ]
            return replace(
                refusal,
                value=refusal.value[:40],
                how_to_fix="Use the id of a change in the plan, as the state shows it"
                + (f": {', '.join(live[:8])}." if live else "; the plan is empty."),
            )
        if refusal.value and refusal.value in self._began() | {i.item_id for i in self.work_case.items}:
            return replace(refusal, value=self._name("c", refusal.value))
        return refusal

    # -- what an action made -----------------------------------------------------------------------
    def _candidate(self, draft: ItemDraft) -> CaseItem:
        """The change a draft stands for, as the author makes it (its id is its content): to find its twin in the
        plan and to judge it."""
        return CaseItem(
            item_id=item_id_of(draft.kind, draft.payload, draft.goal_id),
            kind=draft.kind,
            title=draft.title,
            source=ProposalSource.AI,
            confidence=draft.confidence,
            state=ProposalState.PENDING,
            upload_id=draft.upload_id,
            evidence_ids=draft.evidence_ids,
            payload=dict(draft.payload),
            payload_digest=payload_digest(draft.payload),
            proposed_at=self.now,
            goal_id=draft.goal_id,
            assumption=draft.assumption,
        )

    def _evidence(self) -> tuple[dict[str, DataToolResult], dict[str, str]]:
        """The looks of the turn as the case keeps them, and each look's hashed evidence id."""
        turn = self.turn
        evidence = {
            r.evidence_id: _data_result(r, self.ctxs[turn.files_of[r.evidence_id]], self.now)
            for r in turn.looks
        }
        for query in turn.queries.values():
            evidence[query.result.evidence_id] = query.result
        for _index, combined in turn.combines.values():
            evidence[combined.evidence_id] = combined
        return evidence, {key: value.evidence_id for key, value in evidence.items()}

    def _basis(self, drafts_evidence: Sequence[str]) -> str:
        """What a change rests on: the evidence it cites, else the person's message, else the event."""
        if drafts_evidence:
            return drafts_evidence[0]
        asked = self.case.chat[-1] if self.case.chat and self.case.chat[-1].role == "user" else None
        return f"msg:{len(self.case.chat) - 1}" if asked is not None else "event"

    def _flush(self, mark: _Mark, action: dict[str, Any], replaced: str | None) -> None:
        """What the action added to the turn's memory (a step, a query, a join) becomes a `Create`, and goes through
        the author. The model is told, for each, whether it is in the plan or waits for the person's tick."""
        from engine.doctor.tools_sql import query_uploads
        from engine.doctor.visit import _warnings_of

        turn = self.turn
        raw = action.get("args")
        args = raw if isinstance(raw, dict) else {}
        reason = str(args.get("reason") or args.get("description") or "")
        drafts: list[tuple[ItemDraft, tuple[str, str] | None]] = []
        new_steps = turn.steps[mark.steps :]
        hashed = self._evidence()[1] if new_steps else {}
        for index, result, proposal in new_steps:
            ctx = self.ctxs[index]
            payload: dict[str, JsonValue] = {
                "upload_id": ctx.upload_id,
                "path": None,
                "value": None,
                "step": proposal.step.model_dump(mode="json") if proposal.step is not None else None,
                **_warnings_of(proposal.evidence_ids, {result.evidence_id: result}),
                **self._payload_extra(result),
            }
            drafts.append(
                (
                    ItemDraft(
                        kind="recipe_step",
                        payload=payload,
                        title=proposal.title,
                        upload_id=ctx.upload_id,
                        confidence=AgentConfidence.CHECK,
                        evidence_ids=tuple(hashed.get(e, e) for e in proposal.evidence_ids),
                    ),
                    None,
                )
            )
        for name, query in turn.queries.items():
            if mark.queries.get(name) is query:
                continue
            made = parse_query_output(query.result.output)
            body = made.payload()
            body["findings"] = [f.model_dump(mode="json") for f in query.findings]
            body["needs_yes"] = made.needs_yes or any(f.level == "stop" for f in query.findings)
            # Recorded against the upload its tables come from, so a case of several files builds it (an item of no
            # upload is used only when the case has one file); a query over several files names them all.
            reads = query_uploads(made.step, self.files, self.work_case.items)
            if len(reads) > 1:
                body["input_uploads"] = reads
            drafts.append(
                (
                    ItemDraft(
                        kind="recipe_step",
                        payload=body,
                        title=made.title,
                        upload_id=reads[0] if reads else None,
                        confidence=AgentConfidence.CHECK,
                        evidence_ids=(query.result.evidence_id,),
                    ),
                    ("sql", name),
                )
            )
        for index, (_file, combined) in turn.combines.items():
            if mark.combines.get(index) is turn.combines[index]:
                continue
            step = StepProposal.model_validate(combined.output["step"])
            ctx = self.ctxs[index]
            drafts.append(
                (
                    ItemDraft(
                        kind="recipe_step",
                        payload={
                            "upload_id": ctx.upload_id,
                            "path": None,
                            "value": None,
                            "step": step.as_step(1).model_dump(mode="json"),
                        },
                        title=step.title,
                        upload_id=ctx.upload_id,
                        confidence=AgentConfidence.CHECK,
                        evidence_ids=(combined.evidence_id,),
                    ),
                    ("combine", str(index)),
                )
            )
        for item in self.chosen.combined[mark.combined :]:
            drafts.append(
                (
                    ItemDraft(
                        kind=item.kind,
                        payload=dict(item.payload),
                        title=item.title,
                        upload_id=item.upload_id,
                        goal_id=item.goal_id,
                        confidence=AgentConfidence.CHECK,
                        evidence_ids=item.evidence_ids,
                        assumption=item.assumption,
                    ),
                    None,
                )
            )
        landed: list[CaseItem] = []
        for draft, key in drafts:
            put = self._put(draft, reason=reason, replaced=replaced, key=key)
            if put is not None:
                landed.append(put)
        if landed:
            self._tell(landed)
        # What the action asked or doubted goes through the author too (the model has been told already).
        for ask in turn.asks[mark.asks :]:
            self._author(AskPerson(ask=ask))
        for doubt in turn.doubts[mark.doubts :]:
            self._author(Doubt(doubt=doubt))
        for request in turn.requests[mark.requests :]:
            self._author(Request(request=request))

    def _author(self, action: AuthorAction) -> None:
        """An action that asks or doubts, through the author; a refusal goes back to the model."""
        refusal = self._apply(action)
        if refusal is not None:
            raise _RefusedError(self._for_the_model(refusal))

    def _payload_extra(self, result: ToolResult) -> dict[str, JsonValue]:
        """What the trial on the first rows counted, so the change's card has its counts before any preview: how
        many rows it tried, how many values or rows it changed, how many it could not read."""
        counted = result.result
        return {
            "tried": {
                key: counted[key]
                for key in ("rows", "changed", "failed")
                if isinstance(counted.get(key), int)
            }
        }

    def _put(
        self,
        draft: ItemDraft,
        *,
        reason: str,
        replaced: str | None,
        key: tuple[str, str] | None,
        basis: str | None = None,
    ) -> CaseItem | None:
        """One change through the author. The candidate that lands is returned; None when the change is already in
        the case or was left out by the person (the model is told which)."""
        candidate = self._candidate(draft)
        twin = twin_of(self.work_case.items, candidate)
        if twin is not None and twin.state is ProposalState.REJECTED and twin.withdrawn_because is None:
            who = "the person took" if person_owns(self.case, twin) else "you took"
            self.left_out.append(twin)
            self.chosen.reported.add(candidate.item_id)
            self.turn.feedback.append(
                {
                    "change": self._text(draft.upload_id, clean_text(draft.title, TITLE_CHARS)),
                    "result": f"That was left out earlier ({who} it back); it stays out unless the person ticks it.",
                }
            )
            return None
        here = self._made_here(replaced) if replaced else None
        action = Create(
            draft=draft,
            reason=reason,
            replaces=None if here is not None else replaced,
            basis=basis or self._basis(draft.evidence_ids),
        )
        refusal = self._apply(action, key=key, drop=() if here is None else (here,))
        if refusal is not None:
            raise _RefusedError(self._for_the_model(refusal))
        if twin is not None:
            self.chosen.reported.add(candidate.item_id)
            self.turn.feedback.append(
                {
                    "change": self._text(draft.upload_id, clean_text(draft.title, TITLE_CHARS)),
                    "result": "That change is already in the case.",
                }
            )
            return None
        return next((i for i in self.work_case.items if i.item_id == candidate.item_id), candidate)

    def _tell(self, landed: Sequence[CaseItem]) -> None:
        """Tell the model, before it writes its reply, which of its new changes the person's approval will find in the
        plan and which wait for their tick (and why): what it says it did is what happens. The author has placed each
        one already (`waits_because`); this only says so."""
        roles = roles_of(self.case, self.config_root)
        for item in landed:
            if item.item_id in self.chosen.reported:
                continue
            self.chosen.reported.add(item.item_id)
            said = {
                "id": self._name("c", item.item_id),
                "change": self._text(item.upload_id, clean_text(item.title, TITLE_CHARS)),
            }
            if item.replaces is not None:
                # a swap of what is in the plan or the person's: only their yes takes the old out
                said["result"] = (
                    "It takes the place of a change that is in the plan or the person's, so it waits for "
                    "the person's yes; the old one stays until then."
                )
            elif item.state is ProposalState.ACCEPTED:
                said["result"] = "It is in the plan; the person can untick it with one click."
            else:
                waits = waits_because(item.model_copy(update={"state": ProposalState.PENDING}), roles)
                why = self._text(
                    item.upload_id,
                    clean_text(waits or "it needs the person's tick", self.limits.turn.short_chars),
                )
                self.checker.grounded = self.checker.grounded | grounded_numbers([why])
                said["result"] = f"It waits for the person's tick, because {why}"
            self.turn.feedback.append(said)

    def _proposed(self) -> tuple[list[CaseItem], list[str]]:
        """The changes the turn put forward (as the author made them, or found them), and the ids it took out."""
        began = self._began()
        present = {i.item_id: i for i in self.work_case.items}
        items: list[CaseItem] = []
        withdrawn: list[str] = []
        for kept in self.kept:
            action = kept.action
            if isinstance(action, Create):
                withdrawn.extend([action.replaces] if action.replaces else [])
            elif isinstance(action, Withdraw):
                withdrawn.append(action.item_id)
            if not isinstance(action, (Create, Withdraw, SetSetting)):
                continue
            new = [present[i] for i in kept.made if i not in began and i in present]
            if not new and isinstance(action, Create):  # already in the case: the twin stands for it
                twin = twin_of(self.case.items, self._candidate(action.draft))
                new = [] if twin is None else [twin]
            items.extend(i for i in new if i not in items)
        items.extend(i for i in self.left_out if i not in items)
        return items, list(dict.fromkeys(withdrawn))
