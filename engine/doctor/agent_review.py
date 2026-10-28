"""The AI review: one more check in the Doctor's detector slot, run once after the rules' TREAT pass.

The model reads every file of the case (personal data hidden by each file's own egress gate), looks with the
registry's look tools, and answers with one JSON action per call (the `data_doctor_agent` prompt):

* a typed fix (`propose_step`, Plan G's own checks and a 1,000-row trial) or one sandboxed query
  (`propose_sql`, the registry's `propose_sql_step`) becomes a **pending, never-ticked** item carrying the receipt
  and findings our code counted;
* `ask_question` builds a catalogue question (`questions.build_ask`) and hands it to the protocol, which budgets
  it and adds "I don't know". Whether it blocks is the catalogue's decision, never the model's;
* `express_doubt` is a grounded three-part doubt the person reads under "Not sure"; one about meaning also opens a
  blocking question, so approval waits for the person; `request_data` asks the person for a missing file or column
  (a non-blocking question). Neither changes any data;
* `finish` ends the review with a short summary.

Held in code, not in the prompt: the review does not run unless the client has allowed the AI and the case has
spending left (`AiGuard.ask`); at most `data_doctor.limits.asks.blocking` blocking questions; a query's own checks (`expect`) are
locked at the first try and it may be retried twice (`guards.ExpectationBook`), after which it is shown with a stop
finding instead of dropped; at most `agent.max_tool_steps_per_turn` actions; two bad replies end the review.
Nothing the model writes is evidence: numbers in what the person reads must be in a tool result.

The loop is `engine.agent.loop.chat_turn`'s, cut to one file-spanning pass: it reuses that module's parser, checker,
step proposal and search limit, the egress gate, and the metered, guarded AI service.
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from pydantic import ValidationError

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
    SentItem,
    SessionStatus,
    ToolResult,
)
from engine.agent.egress import (
    EGRESS_LATE_MASK,
    MAX_SENT_ITEMS,
    Egress,
    assert_clean,
)
from engine.agent.grounding import evidence_numbers, grounded_numbers
from engine.agent.loop import (
    STEP_TEXT_KEYS,
    UNAVAILABLE,
    _Checker,
    _limit_searches,
    _parse,
    _propose_step,
    _record,
    ai_failure_codes,
    unavailable_reply,
)
from engine.agent.tools import AgentContext, AgentToolError
from engine.agent.untrusted import for_prompt
from engine.doctor.actions import prompt_variables
from engine.doctor.decisions import decisions
from engine.doctor.goal_brief import GOAL_BRIEF_TOOL
from engine.doctor.guards import ExpectationBook, Expectations, Finding
from engine.doctor.limits import limits
from engine.doctor.protocol import DataRequest, ProtocolError, doubt_ask, express_doubt
from engine.doctor.questions import QuestionError, build_ask, catalogue_lines
from engine.doctor.registry import (
    DataToolContext,
    DataToolKind,
    DataToolResult,
    Exposure,
    evidence_id_of,
)
from engine.doctor.row_contracts import StepProposal
from engine.doctor.tools_features import parse_query_output
from engine.doctor.tools_preview import M_PREVIEW_TOOLS
from engine.doctor.tools_sql import SqlStepProposal
from engine.generative.contracts import GenerativePurpose
from engine.generative.errors import GenerativeError
from engine.generative.guardrails import Guardrails
from engine.generative.prompts import Prompt, load_prompt, render
from engine.llm import LLMError
from engine.utils.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from engine.doctor.ai_guard import AiGuard
    from engine.doctor.case import Case, CaseDoubt, CaseItem
    from engine.doctor.diagnose import LoadedFile
    from engine.doctor.goal_brief import GoalBrief
    from engine.doctor.protocol import Ask
    from engine.doctor.visit import Detected, DetectorPort
    from engine.storage import Storage

__all__ = ["PROMPT", "agent_review", "lazy_review"]

_LOG = get_logger(__name__)

PROMPT: Final[str] = "data_doctor_agent"
_DIGIT: Final = re.compile(r"\d")
SQL_ARGS: Final[frozenset[str]] = frozenset(
    {"sql", "output_name", "as_of", "as_of_rule", "as_of_column", "reads_future", "outcome_window_days"}
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


@dataclass
class _Sql:
    """One query the model proposed: the registry's answer to its latest try, and the findings to show."""

    result: DataToolResult
    findings: tuple[Finding, ...] = ()


@dataclass
class _Turn:
    """The review's working memory: what the model has seen and made. Never stored."""

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
        calls, tokens = meter.calls_used(), meter.tokens_used()
        used = _Review(
            case,
            files,
            brief,
            context_for,
            meter,
            guardrails,
            principal,
            config_root,
            registry or visit_registry(),
            storage=storage,
        )
        found = used.run()
        return dataclasses.replace(
            found, model_calls=meter.calls_used() - calls, tokens=meter.tokens_used() - tokens
        )

    return review


def _active_goal(case: Case) -> Any:
    """The case's active goal (`visit.active_goal`, which this module cannot import at load time)."""
    return next((goal for goal in case.goals if goal.active), None)


class _Review:
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
        storage: Storage | None = None,
    ) -> None:
        from engine.doctor.ai_guard import file_overviews
        from engine.doctor.current_tables import current_for, labels_for

        self.case, self.brief, self.meter, self.registry = case, brief, meter, registry
        self._context_for = context_for
        self._raw: dict[int, tuple[AgentContext, Any]] = {}
        self.principal, self.config_root = principal, config_root
        self.storage = storage
        # The AI looks at the current tables: F1..Fn are the files as the plan's accepted fixes leave them, T1..Tn the
        # tables its queries build. The uploads (`self.files`) stay what the tools that read a file by id are given.
        current = current_for(case, files, context_for)
        self.built = current.built
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
        self.state = {
            "files": self.overviews,
            "plan_so_far": self._decided(),
            "open_questions": [q.code for q in case.questions if q.answer is None],
            "left": {"blocking_questions": limits(self.ctxs[0].config).asks.blocking},
        }
        self.checker = _Checker(
            grounded=grounded_numbers([self.state]), names=tuple(names), guardrails=guardrails
        )
        self.limits = limits(self.ctxs[0].config)
        self.max_steps = self.limits.turn.review_steps

    # -- the files -------------------------------------------------------------------------------------
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
            session_id="dd-review",
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

    def _decided(self) -> list[dict[str, str]]:
        """What the rules already put in the plan, by file and column (names through the file's gate)."""
        found: list[dict[str, str]] = []
        for item in self.case.items[:60]:
            index = next(
                (i for i, c in enumerate(self.ctxs[: self.nfiles]) if c.upload_id == item.upload_id), None
            )
            step = item.payload.get("step")
            column = item.payload.get("column") or (step.get("column") if isinstance(step, dict) else None)
            whole = item.upload_id is None and isinstance(step, dict)  # the AI's queries are over the files
            if whole:
                index = 0 if self.nfiles == 1 else None
                column = (
                    column if isinstance(column, str) else str(item.payload.get("output_name") or item.title)
                )
            elif index is None or not isinstance(column, str):
                continue
            kind = step.get("kind") if isinstance(step, dict) else item.kind
            found.append(
                {
                    "file": self.labels[index] if index is not None else "all files",
                    "kind": str(kind),
                    "column": self.gates[index].label(column) if index is not None else column[:60],
                    "by": item.source.value,  # the AI sees its own items too, and whether each still waits
                    "state": item.state.value,
                }
            )
        return found

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

    # -- the loop --------------------------------------------------------------------------------------
    purpose: GenerativePurpose = GenerativePurpose.DOCTOR_AGENT
    replacing: tuple[str, ...] = ()
    """Items the chat is swapping out or taking back in the action being run (a query's trial leaves them out)."""

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
        return bool(tool.session_tool or tool.name in M_PREVIEW_TOOLS)

    def _complete(self, prompt: Prompt, variables: dict[str, Any]) -> str:
        """One call to the model on `prompt` rendered with `variables`, after the last egress check. Raises
        `_NoAnswerError` when the prompt is too long for one call or the AI service cannot answer (unavailable, or out
        of money): what was made so far stands and the rules go on."""
        rendered = render(prompt, variables)
        system, masked_system = assert_clean(rendered.system)
        user, masked_user = assert_clean(rendered.user)
        if masked_system or masked_user:
            _LOG.warning("%s: a review prompt was masked late", EGRESS_LATE_MASK)
            rendered = dataclasses.replace(rendered, system=system, user=user)
        if len(rendered.system) + len(rendered.user) > self.limits.turn.prompt_chars:
            raise _NoAnswerError("prompt_too_large")
        try:
            return self.meter.complete(rendered, self.purpose).text
        except (LLMError, GenerativeError) as exc:
            failed = _NoAnswerError("unavailable", exc)
            _LOG.warning("%s: the AI call failed (%s)", self.purpose.value, ", ".join(failed.codes))
            raise failed from exc

    def run(self) -> Detected:
        from engine.doctor.visit import Detected

        turn, strikes, summary = self.turn, 0, ""
        failed: _NoAnswerError | None = None  # why the first call got no answer
        prompt = load_prompt(PROMPT, self.config_root)
        tools = self._tools()
        goal = self.brief.goal_name if self.brief is not None else "see what these tables can be used for"
        for step in range(self.max_steps + 1):
            variables = {
                "goal": goal,
                "tools": tools,
                **prompt_variables("review", self.ctxs[0].config, config_root=self.config_root),
                "questions": catalogue_lines(),
                "state": json.dumps(self.state, ensure_ascii=False),
                "turn_results": (
                    json.dumps(for_prompt(turn.feedback, {}), ensure_ascii=False) if turn.feedback else "none"
                ),
            }
            try:
                text = self._complete(prompt, variables)
            except _NoAnswerError as stop:
                if stop.reason == "unavailable" and step == 0:
                    failed = stop  # no review happened at all: the visit says why, and asks again later
                break  # unavailable or out of money: what was made so far stands, the rules go on
            try:
                action = _parse(text)
                if str(action["action"]) == "finish":
                    summary = self._finish(action)
                    break
                self._act(action)
            except AgentToolError as exc:
                strikes += 1
                if strikes >= self.limits.turn.strikes:
                    break
                turn.feedback.append(self.gates[0].prepare({"error": exc.code, "message": exc.message}))
            except Exception as exc:  # a model's odd input must never cost a paid review that nobody records
                _LOG.warning("doctor review step failed: %s", type(exc).__name__)
                break
        items, evidence = self._items()
        questions = len(turn.asks) + len(turn.requests)
        return Detected(
            asks=tuple(turn.asks),
            doubts=tuple(turn.doubts),
            requests=tuple(turn.requests),
            items=tuple(items),
            evidence=tuple(evidence),
            summary=summary
            or (
                _summary(len(items), questions, len(turn.doubts)) if items or questions or turn.doubts else ""
            ),
            sent=tuple(s.model_dump(mode="json") for s in turn.sent[:MAX_SENT_ITEMS]),
            blocked_by=None if failed is None else failed.reason,
            error_codes=() if failed is None else failed.codes,
            reply="" if failed is None else failed.reply,
        )

    def _act(self, action: dict[str, Any]) -> None:
        name = str(action["action"])
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
        elif name == "read_guide":
            self._guide(args)
        elif name == "ask_question":
            self._ask(action, args)
        elif name == "express_doubt":
            self._doubt(action, args)
        elif name == "request_data":
            self._request(action, args)
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
        if name in M_PREVIEW_TOOLS or name == GOAL_BRIEF_TOOL:  # a registry READ tool of its own
            made = self.registry.call(name, args, self._context(index, raw=raw))
            result = ToolResult(
                evidence_id=evidence_id,
                tool=name,
                args=made.input,
                supplied=args,
                result=made.output,
                created_at=made.computed_at,
            )
        else:
            result = self.registry.session_call(
                name, args, self._context(index, raw=raw), evidence_id=evidence_id
            )
        turn.looks.append(result)
        turn.files_of[evidence_id] = index
        self.checker.grounded = self.checker.grounded | evidence_numbers(result.result, result.typed_args)
        self._record(index, result, frozenset(), gate=gate)

    def _record(self, index: int, result: ToolResult, keys: frozenset[str], *, gate: Any = None) -> None:
        """Put one result in the next prompt through its file's gate, noting what is sent."""
        sent: list[SentItem | None] = []
        gate, feedback = gate or self.gates[index], self.turn.feedback
        _record(gate, {}, feedback, sent, result, keys)
        feedback[-1]["file"] = self.labels[index]
        self.turn.sent.extend(s for s in sent if s is not None)

    def _step(self, action: dict[str, Any], args: dict[str, Any]) -> None:
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
        )
        turn.steps.append((index, result, proposal))
        turn.looks.append(result)  # a later step may cite what this one measured
        turn.files_of[result.evidence_id] = index
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
            raise AgentToolError("AGENT_EXPECT_INVALID", str(exc).splitlines()[-1][:200]) from exc
        given: dict[str, Any] = {k: v for k, v in args.items() if k in SQL_ARGS}
        given["description"] = str(args.get("description") or args.get("reason") or "")[
            : self.limits.turn.description_chars
        ]
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
        order = turn.orders.setdefault(name, len(turn.orders) + 1)
        changed = turn.book.declare(order, expectations)
        proposal = SqlStepProposal.model_validate(result.output)
        outcome = turn.book.check(order, proposal.receipt)
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
                },
                extra_text_keys=frozenset({"message", "code", "level", "key", "column", "kind"}),
            ),
        }
        self.checker.grounded = self.checker.grounded | grounded_numbers([shown["result"]])
        turn.feedback.append(shown)

    def _guide(self, args: dict[str, Any]) -> None:
        """`read_guide`: the guide's rules and a section, each part once per turn (a repeat says it is above).

        A lookup the guides cannot answer is a read-only miss, not a strike: the refusal, with the valid names or
        sections, goes to the model as feedback.
        """
        from engine.doctor.guides import ALREADY_READ, RULES, read_guide
        from engine.doctor.lessons import approved_lessons

        name, section = args.get("name"), args.get("section")
        lessons = approved_lessons(self.storage)
        try:
            key, title, lines = read_guide(name, section, self.config_root, lessons=lessons)
        except AgentToolError as exc:
            self.turn.feedback.append(
                self.gates[0].prepare({"tool": "read_guide", "error": exc.code, "message": exc.message})
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

    def _ask(self, action: dict[str, Any], args: dict[str, Any]) -> None:
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
                raise AgentToolError(
                    "AGENT_QUESTION_REFUSED",
                    f"The question failed a check ({failure}); use only numbers you looked up.",
                )
        if ask.blocking and turn.blocking >= self.limits.asks.blocking:
            raise AgentToolError(
                "AGENT_BLOCKING_LIMIT",
                f"You may ask {self.limits.asks.blocking} blocking questions in one review; this one is dropped.",
            )
        turn.blocking += int(ask.blocking)
        turn.asks.append(ask)
        turn.feedback.append({"asked": ask.code})

    def _checked(self, texts: Sequence[str], target: str, index: int) -> list[str]:
        """The model's own words for the person, each through the number and guardrail checks."""
        for text in texts:
            failure = (
                self.checker.failure(text, target=target, max_chars=self.limits.turn.question_chars)
                if text
                else None
            )
            if failure is not None:
                raise AgentToolError(
                    "AGENT_TEXT_REFUSED",
                    f"The {target} failed a check ({failure}); use only numbers you looked up.",
                )
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
                "AGENT_DOUBT_LIMIT", "That is enough doubts for one review; this one is dropped."
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
            turn.asks.append(doubt_ask(doubt))
        turn.feedback.append({"doubt": "noted"})

    def _request(self, action: dict[str, Any], args: dict[str, Any]) -> None:
        turn = self.turn
        index = self._index(action, args)
        args = dict(self.gates[index].unalias_args(args))
        if len(turn.requests) >= self.limits.asks.requests:
            raise AgentToolError(
                "AGENT_REQUEST_LIMIT", "That is enough requests for one review; this is dropped."
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
        turn.requests.append(
            DataRequest(
                kind=kind,
                what=what,
                why=why,
                columns=tuple(shape),
                per=per or "one row per customer",
                ask_whom=whom or None,
                upload_id=self._upload_of(index),
            )
        )
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

    def _finish(self, action: dict[str, Any]) -> str:
        text = str(action.get("text") or "").strip()
        if (
            not text
            or self.checker.failure(text, target="doctor summary", max_chars=self.limits.turn.summary_chars)
            is not None
        ):
            return ""  # the review's own sentence is used instead
        return self.gates[0].person_text(text)

    # -- what the review made --------------------------------------------------------------------------
    def _payload_extra(self, result: ToolResult) -> dict[str, Any]:
        """More for a typed change's payload (the chat adds what its trial counted); nothing for the review."""
        del result
        return {}

    def _items(self) -> tuple[list[CaseItem], list[DataToolResult]]:
        """Pending, never-ticked items and the results they rest on (content-hash ids)."""
        from engine.doctor.visit import _item, _warnings_of

        turn, now = self.turn, self.clock()
        evidence = {
            r.evidence_id: _data_result(r, self.ctxs[turn.files_of[r.evidence_id]], now) for r in turn.looks
        }
        hashed = {key: value.evidence_id for key, value in evidence.items()}
        items: list[CaseItem] = []
        for index, result, proposal in turn.steps:
            ctx = self.ctxs[index]
            items.append(
                _item(
                    "recipe_step",
                    {
                        "upload_id": ctx.upload_id,
                        "path": None,
                        "value": None,
                        "step": proposal.step.model_dump(mode="json") if proposal.step is not None else None,
                        **_warnings_of(proposal.evidence_ids, {result.evidence_id: result}),
                        **self._payload_extra(result),
                    },
                    title=proposal.title,
                    upload_id=ctx.upload_id,
                    confidence=AgentConfidence.CHECK,
                    evidence=tuple(hashed.get(e, e) for e in proposal.evidence_ids),
                    now=now,
                    source=ProposalSource.AI,
                )
            )
        out = list(evidence.values())
        for query in turn.queries.values():
            made = parse_query_output(query.result.output)
            payload = made.payload()
            payload["findings"] = [f.model_dump(mode="json") for f in query.findings]
            payload["needs_yes"] = made.needs_yes or any(f.level == "stop" for f in query.findings)
            items.append(
                _item(
                    "recipe_step",
                    payload,
                    title=made.title,
                    upload_id=None,
                    confidence=AgentConfidence.CHECK,
                    evidence=(query.result.evidence_id,),
                    now=now,
                    source=ProposalSource.AI,
                )
            )
            out.append(query.result)
        for index, combined in turn.combines.values():
            step = StepProposal.model_validate(combined.output["step"])
            items.append(
                _item(
                    "recipe_step",
                    {
                        "upload_id": self.ctxs[index].upload_id,
                        "path": None,
                        "value": None,
                        "step": step.as_step(1).model_dump(mode="json"),
                    },
                    title=step.title,
                    upload_id=self.ctxs[index].upload_id,
                    confidence=AgentConfidence.CHECK,
                    evidence=(combined.evidence_id,),
                    now=now,
                    source=ProposalSource.AI,
                )
            )
            out.append(combined)
        return items, out


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
            if result.tool in TOOLS or result.tool in M_PREVIEW_TOOLS or result.tool == GOAL_BRIEF_TOOL
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


def _summary(items: int, questions: int, doubts: int = 0) -> str:
    """The review in our own words, when the model's summary is missing or failed a check."""
    unsure = f", and {doubts} thing{'s' if doubts != 1 else ''} it is not sure about" if doubts else ""
    return (
        f"I looked through your files with the AI: {items} suggested change{'s' if items != 1 else ''} "
        f"and {questions} question{'s' if questions != 1 else ''}{unsure}. Nothing is ticked; you decide."
    )
