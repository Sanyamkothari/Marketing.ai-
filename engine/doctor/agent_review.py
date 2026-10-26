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
spending left (`AiGuard.ask`); at most `MAX_BLOCKING` blocking questions; a query's own checks (`expect`) are
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
    MAX_PROMPT_CHARS,
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
from engine.doctor.goal_brief import GOAL_BRIEF_TOOL
from engine.doctor.guards import ExpectationBook, Expectations, Finding
from engine.doctor.protocol import DataRequest, ProtocolError, doubt_ask, express_doubt
from engine.doctor.questions import QuestionError, build_ask, catalogue_lines
from engine.doctor.registry import (
    DataToolContext,
    DataToolKind,
    DataToolResult,
    Exposure,
    evidence_id_of,
)
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

__all__ = ["MAX_BLOCKING", "PROMPT", "agent_review", "lazy_review"]

_LOG = get_logger(__name__)

PROMPT: Final[str] = "data_doctor_agent"
MAX_BLOCKING: Final[int] = 3
"""The most blocking questions the AI may ask in one review; the next is dropped."""
STRIKES: Final[int] = 2
"""Bad replies (malformed, unknown action, refused argument) before the review ends."""
MAX_DOUBTS: Final[int] = 5
MAX_REQUESTS: Final[int] = 3
"""The most doubts and requests for more data the AI may add in one review; the next is refused."""
SUMMARY_CHARS: Final[int] = 800
EFFECT_CHARS: Final[int] = 160
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
    closed: set[str] = field(default_factory=set)
    asks: list[Ask] = field(default_factory=list)
    doubts: list[CaseDoubt] = field(default_factory=list)
    requests: list[DataRequest] = field(default_factory=list)
    blocking: int = 0
    feedback: list[dict[str, Any]] = field(default_factory=list)
    sent: list[Any] = field(default_factory=list)
    book: ExpectationBook = field(default_factory=ExpectationBook)
    orders: dict[str, int] = field(default_factory=dict)


def agent_review(
    context_for: Callable[[Case, tuple[LoadedFile, ...]], AgentContext],
    *,
    meter: AiGuard,
    guardrails: Guardrails,
    principal: Principal,
    config_root: Path | None = None,
    registry: Any = None,
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
    ) -> None:
        from engine.doctor.ai_guard import file_overviews

        self.case, self.brief, self.meter, self.registry = case, brief, meter, registry
        self.principal, self.config_root = principal, config_root
        self.ctxs = [context_for(case, (f,)) for f in files]
        overviews = file_overviews(self.ctxs)  # a file past the prompt's room is left out, and so unreachable
        self.files = files[: len(overviews)]
        self.ctxs = self.ctxs[: len(overviews)]
        self.gates = [_gate(ctx) for ctx in self.ctxs]
        self.sessions = [self._session(case, ctx) for ctx in self.ctxs]
        self.clock = self.ctxs[0].clock
        self.turn = _Turn()
        self.turn.sent.extend(sent for _overview, sent in overviews)
        self.overviews = [overview for overview, _sent in overviews]
        names = [str(c) for ctx in self.ctxs for c in ctx.frame.columns]
        names += [g.label(c) for g, ctx in zip(self.gates, self.ctxs, strict=True) for c in ctx.frame.columns]
        self.state = {
            "files": self.overviews,
            "plan_so_far": self._decided(),
            "open_questions": [q.code for q in case.questions if q.answer is None],
            "left": {"blocking_questions": MAX_BLOCKING},
        }
        self.checker = _Checker(
            grounded=grounded_numbers([self.state]), names=tuple(names), guardrails=guardrails
        )
        self.max_steps = self.ctxs[0].config.agent.max_tool_steps_per_turn

    # -- the files -------------------------------------------------------------------------------------
    @staticmethod
    def _session(case: Case, ctx: AgentContext) -> AgentSession:
        """A light session for Plan G's step checks: it holds the key and outcome the case decided, so a step can
        never touch them. No advisor runs."""
        now = ctx.clock()
        stub = ToolResult(evidence_id="decided", tool="get_profile", created_at=now)
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
                (ROLE_PRIMARY_KEY, _decided(case, "key", ctx.upload_id)),
                (ROLE_TARGET, _decided(case, "outcome_column", ctx.upload_id)),
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
            index = next((i for i, c in enumerate(self.ctxs) if c.upload_id == item.upload_id), None)
            step = item.payload.get("step")
            column = item.payload.get("column") or (step.get("column") if isinstance(step, dict) else None)
            if index is None or not isinstance(column, str):
                continue
            kind = step.get("kind") if isinstance(step, dict) else item.kind
            found.append(
                {
                    "file": f"F{index + 1}",
                    "kind": str(kind),
                    "column": self.gates[index].label(column),
                    "by": item.source.value,  # the AI sees its own items too, and whether each still waits
                    "state": item.state.value,
                }
            )
        return found

    def _index(self, action: dict[str, Any], args: dict[str, Any]) -> int:
        """The file an action is about (`F1`, `F2`, ...); with one file it may be left out."""
        given = args.pop("file", None) or action.get("file")
        if given is None and len(self.ctxs) == 1:
            return 0
        text = str(given or "")
        if text[:1] not in {"F", "f"} or not text[1:].isdigit() or not 1 <= int(text[1:]) <= len(self.ctxs):
            raise AgentToolError(
                "AGENT_FILE_UNKNOWN", f"Name a file as F1 to F{len(self.ctxs)}, not {text[:20]!r}."
            )
        return int(text[1:]) - 1

    def _context(self, index: int, **extra: Any) -> DataToolContext:
        from engine.doctor.visit import active_section

        return DataToolContext(
            agent=self.ctxs[index],
            principal=self.principal,
            caller="ai",
            egress=self.gates[index],
            files=self.files,
            case_items=active_section(self.case)[0],  # the plan so far, for `preview_working_copy`
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
                        "file": "F1..F" + str(len(self.ctxs)),
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
        if len(rendered.system) + len(rendered.user) > MAX_PROMPT_CHARS:
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
                if strikes >= STRIKES:
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
        gate = self.gates[index]
        args = dict(gate.unalias_args(args))
        if name == "find_values":
            _limit_searches(
                self.ctxs[index],
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
            made = self.registry.call(name, args, self._context(index))
            result = ToolResult(
                evidence_id=evidence_id,
                tool=name,
                args=made.input,
                supplied=args,
                result=made.output,
                created_at=made.computed_at,
            )
        else:
            result = self.registry.session_call(name, args, self._context(index), evidence_id=evidence_id)
        turn.looks.append(result)
        turn.files_of[evidence_id] = index
        self.checker.grounded = self.checker.grounded | evidence_numbers(result.result, result.typed_args)
        self._record(index, result, frozenset())

    def _record(self, index: int, result: ToolResult, keys: frozenset[str]) -> None:
        """Put one result in the next prompt through its file's gate, noting what is sent."""
        sent: list[SentItem | None] = []
        gate, feedback = self.gates[index], self.turn.feedback
        _record(gate, {}, feedback, sent, result, keys)
        feedback[-1]["file"] = f"F{index + 1}"
        self.turn.sent.extend(s for s in sent if s is not None)

    def _step(self, action: dict[str, Any], args: dict[str, Any]) -> None:
        turn = self.turn
        index = self._index(action, args)
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
        names = [f"F{i + 1}" for i in range(len(self.ctxs))]
        upload = {name: ctx.upload_id for name, ctx in zip(names, self.ctxs, strict=True)}
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
        given["description"] = str(args.get("description") or args.get("reason") or "")[:300]
        given["upload_ids"] = [upload[w] for w in wanted]
        if args.get("as_of_file") in upload:
            given["as_of_upload_id"] = upload[str(args["as_of_file"])]
        if "as_of" not in given and "as_of_rule" not in given and (decided := case_as_of(self.case)):
            given["as_of"] = (
                decided  # the person's own as-of answer is `$as_of` unless the step names another
            )
        name = str(given.get("output_name", ""))
        if name in turn.closed:
            raise AgentToolError(
                "AGENT_RETRIES_USED",
                f"'{name[:60]}' has failed its own checks too often; it is left for the person.",
            )
        first = next(i for i, ctx in enumerate(self.ctxs) if ctx.upload_id == given["upload_ids"][0])
        key = _decided(self.case, "key", given["upload_ids"][0])
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
                upload_id=ctx.upload_id,
                column=ctx.resolve(str(args["column"])) if isinstance(args.get("column"), str) else None,
                evidence_ids=tuple(_hashed(turn, cited)),
            )
        except QuestionError as exc:
            raise AgentToolError("AGENT_QUESTION_INVALID", str(exc)[:300]) from exc
        for text in (ask.text, ask.why, *(o.label for o in ask.options)):
            failure = self.checker.failure(text, target="doctor question", max_chars=400)
            if failure is not None:
                raise AgentToolError(
                    "AGENT_QUESTION_REFUSED",
                    f"The question failed a check ({failure}); use only numbers you looked up.",
                )
        if ask.blocking and turn.blocking >= MAX_BLOCKING:
            raise AgentToolError(
                "AGENT_BLOCKING_LIMIT",
                f"You may ask {MAX_BLOCKING} blocking questions in one review; this one is dropped.",
            )
        turn.blocking += int(ask.blocking)
        turn.asks.append(ask)
        turn.feedback.append({"asked": ask.code})

    def _checked(self, texts: Sequence[str], target: str, index: int) -> list[str]:
        """The model's own words for the person, each through the number and guardrail checks."""
        for text in texts:
            failure = self.checker.failure(text, target=target, max_chars=400) if text else None
            if failure is not None:
                raise AgentToolError(
                    "AGENT_TEXT_REFUSED",
                    f"The {target} failed a check ({failure}); use only numbers you looked up.",
                )
        return [self.gates[index].person_text(text) if text else "" for text in texts]

    def _doubt(self, action: dict[str, Any], args: dict[str, Any]) -> None:
        turn = self.turn
        index = self._index(action, args)
        ctx = self.ctxs[index]
        args = dict(self.gates[index].unalias_args(args))
        meaning = args.get("meaning") is True
        if len(turn.doubts) >= MAX_DOUBTS or (meaning and turn.blocking >= MAX_BLOCKING):
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
                upload_id=ctx.upload_id,
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
        if len(turn.requests) >= MAX_REQUESTS:
            raise AgentToolError(
                "AGENT_REQUEST_LIMIT", "That is enough requests for one review; this is dropped."
            )
        kind = args.get("kind", "more_data")
        columns = args.get("columns")
        named = [str(c).strip() for c in columns[:8]] if isinstance(columns, list) else []
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
                upload_id=self.ctxs[index].upload_id,
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
            ok = ok and self.checker.failure(text, target="doctor question", max_chars=EFFECT_CHARS) is None
            kept.append(self.gates[index].person_text(text) if ok else "")
        return kept

    def _finish(self, action: dict[str, Any]) -> str:
        text = str(action.get("text") or "").strip()
        if (
            not text
            or self.checker.failure(text, target="doctor summary", max_chars=SUMMARY_CHARS) is not None
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
            made = SqlStepProposal.model_validate(query.result.output)
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
        return items, out


_ISO_DATE: Final = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")


def case_as_of(case: Case) -> str | None:
    """The as-of date the person decided for this case, as `YYYY-MM-DD`, or None (then a query names its own).

    The newest accepted item that carries one (leaving out an unfinished last period sets it), else the answer to
    the as-of question (Q8): the date the person typed, else the one the chosen option names ("The latest date in
    the file: 2011-12-09").
    """
    for item in reversed(case.items):
        given = item.payload.get("as_of")
        if item.state is ProposalState.ACCEPTED and isinstance(given, str) and _ISO_DATE.fullmatch(given):
            return given
    for question in case.questions:
        if question.code != "Q08_AS_OF" or question.answer is None:
            continue
        typed = getattr(question, "answer_value", None)
        if isinstance(typed, str) and _ISO_DATE.fullmatch(typed):
            return typed  # the date the person typed beats the one an option names
        chosen = next((o for o in question.options if o.option_id == question.answer), None)
        found = _ISO_DATE.search(chosen.label) if chosen is not None else None
        if found is not None:
            return found.group(1)
    return None


def _decided(case: Case, kind: str, upload_id: str) -> str | None:
    for item in case.items:
        if item.kind == kind and item.upload_id == upload_id and item.state is ProposalState.ACCEPTED:
            column = item.payload.get("column")
            return column if isinstance(column, str) else None
    return None


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
