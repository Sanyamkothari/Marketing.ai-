"""The Data Doctor's chat: one friendly conversation that looks, changes the working copy, asks and replies.

A chat turn is the AI review's loop (`engine.doctor.agent_review._Review`) with a person's message in front of it
and a `reply` at the end. The model gets a short prompt (`data_doctor_chat`: a friendly assistant with tools), the
last twelve turns of the conversation, and the case as it stands - every file as F1..Fn, what the rules decided,
each change with its id and state, the questions with their answers, the doubts - so it can talk about what it did
before. It answers with one JSON action per call:

* the look tools, `propose_step` (also leaving rows out by a rule), `propose_sql`, `join_files` and `stack_files`
  make **changes to the working copy**: pending items carrying the receipt our code counted. The visit applies the
  clean ones for the person to undo (`engine.doctor.visit._converse`); the model never ticks anything;
* `ask` raises a catalogue question, or a quick question with its own short options (no numbers); `suggest_answer`
  points at one answer of an open question from what the person typed. It never records it: only the person's click
  does. `express_doubt` and `request_data` are the review's;
* `withdraw` takes back one of the agent's **own** earlier changes, and `replaces` on a new change swaps it;
* `reply` ends the turn with a short message in plain words.

Held in code, not in the prompt: the egress gate of each file, the number check (a number in what the person reads
is in a tool result or in their own words), the guardrails, at most `data_doctor.ai_limits.max_chat_steps` actions,
two bad replies end the turn. Everything the review refuses, the chat refuses.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from engine.agent.contracts import AgentConfidence, ProposalSource, ProposalState, ToolResult
from engine.agent.grounding import evidence_numbers, grounded_numbers
from engine.agent.loop import MAX_MESSAGE_CHARS, MAX_REPLY_CHARS, UNAVAILABLE, _parse
from engine.agent.tools import AgentContext, AgentToolError
from engine.agent.untrusted import clean_text, for_prompt
from engine.doctor.agent_apply import person_owns
from engine.doctor.agent_review import MAX_BLOCKING, STRIKES, _NoAnswerError, _Review
from engine.doctor.protocol import DONT_KNOW, Ask, AskOption, question_id_of
from engine.doctor.questions import QuestionError, build_ask, catalogue_lines
from engine.doctor.visit_contracts import OptionEffect
from engine.generative.contracts import GenerativePurpose
from engine.generative.prompts import load_prompt
from engine.storage import StorageError
from engine.utils.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from engine.access.roles import Principal
    from engine.doctor.ai_guard import AiGuard
    from engine.doctor.case import Case, CaseDoubt, CaseItem
    from engine.doctor.diagnose import LoadedFile
    from engine.doctor.protocol import DataRequest
    from engine.doctor.registry import DataToolResult
    from engine.doctor.visit import ConversePort, ConverseResult
    from engine.generative.guardrails import Guardrails
    from engine.storage import Storage

__all__ = ["CHAT_PROMPT", "ChatTurn", "doctor_converse"]

_LOG = get_logger(__name__)

CHAT_PROMPT: Final[str] = "data_doctor_chat"
HISTORY_TURNS: Final[int] = 12
"""How many earlier messages of the conversation the model reads."""
STATE_ITEMS: Final[int] = 40
STATE_QUESTIONS: Final[int] = 12
STATE_DOUBTS: Final[int] = 5
RELOADED_LOOKS: Final[int] = 6
"""How many looks of earlier turns the model may cite again."""
MAX_QUICK: Final[int] = 2
"""The most quick questions (with the model's own options) one message may raise."""
SHORT: Final[int] = 220
QUICK_CODE: Final[str] = "CHAT_ASK"
OUTCOME_QUESTIONS: Final[frozenset[str]] = frozenset({"Q24", "Q24_WHAT_COUNTS"})
MAX_OUTCOMES: Final[int] = 3
"""The most answers (rules the person might mean) one "what counts as a yes" question offers."""
_DIGIT: Final = re.compile(r"\d")
_MEANING_TOPICS: Final = re.compile(
    r"(consent|opt[\W_]*(in|out)|opted|agree|contact|unsubscri|outcome|counts? as|a yes|a sale|purchas|bought"
    r"|churn|convert|identif|customer[\W_]*id|\bids?\b|\bdates?\b|day first|month first)",
    re.IGNORECASE,
)
"""What a quick question may not be about: who agreed to be contacted, the outcome, the ID, the date. Those are the
catalogue's questions, which only the person answers."""
OUT_OF_STEPS: Final[str] = (
    "That took more steps than I may take in one message. What I did so far is above; ask me to go on if you want more."
)
NOTHING_SAID: Final[str] = "I could not answer that from what I have checked."


@dataclass(frozen=True)
class ChatTurn:
    """What one chat turn made. The visit decides what becomes part of the plan."""

    reply: str
    items: tuple[CaseItem, ...] = ()
    evidence: tuple[DataToolResult, ...] = ()
    asks: tuple[Ask, ...] = ()
    doubts: tuple[CaseDoubt, ...] = ()
    requests: tuple[DataRequest, ...] = ()
    withdrawn: tuple[str, ...] = ()
    suggestion: tuple[str, str] | None = None
    """`(question_id, option_id)` the model pointed at; the visit checks the question is open."""
    tools: tuple[str, ...] = ()
    sent: tuple[dict[str, Any], ...] = ()
    blocked_by: str | None = None


@dataclass
class _Chosen:
    """What the model has done in this turn beyond looking."""

    withdrawn: list[str] = field(default_factory=list)
    replaces: list[str] = field(default_factory=list)
    combined: list[CaseItem] = field(default_factory=list)
    asked: dict[str, Ask] = field(default_factory=dict)
    quick: int = 0
    suggestion: tuple[str, str] | None = None
    used: list[str] = field(default_factory=list)
    reported: set[str] = field(default_factory=set)
    applied: list[CaseItem] = field(default_factory=list)
    """The changes this turn the visit will apply, so the next is judged with them in place."""


class _Chat(_Review):
    purpose = GenerativePurpose.DOCTOR_CHAT

    def __init__(self, *args: Any, chat_steps: int, storage: Storage | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.max_steps = chat_steps
        self.chosen = _Chosen()
        # What the model calls a change (`c1`) or a question (`k1`) -> the id in the case. The real ids are content
        # hashes, which the egress check would take for a secret.
        self.handles: dict[str, str] = {}
        # The plan and the questions say what the rules decided and what is still open, so the review's own two
        # lists of that are left out.
        self.state = {
            "files": self.overviews,
            **self._chat_state(),
            "earlier_looks": self._reload_looks(storage),
            "left": self.state["left"],
        }
        self.checker.grounded = self.checker.grounded | grounded_numbers([_without_ids(self.state)])

    # -- what the model reads ----------------------------------------------------------------------
    def _file_gate(self, upload_id: str | None) -> Any:
        index = next((i for i, ctx in enumerate(self.ctxs) if ctx.upload_id == upload_id), 0)
        return self.gates[index]

    def _text(self, upload_id: str | None, value: str) -> str:
        """`value` for the prompt: through its file's gate, and for text about no one file (or the conversation)
        through every file's gate, so a column name of any file the person typed or a title carried is aliased.
        """
        if upload_id is not None and any(ctx.upload_id == upload_id for ctx in self.ctxs):
            return str(self._file_gate(upload_id).text(value))
        for gate in self.gates[1:]:
            value = gate.apply_aliases(value)
        return str(self.gates[0].text(value))

    def _chat_state(self) -> dict[str, Any]:
        """The case as the model may read it: every change with its id, the questions with their answers."""
        from engine.doctor.visit import AGENT, active_section

        items, questions = active_section(self.case)
        plan: list[dict[str, Any]] = []
        for number, item in enumerate(items[-STATE_ITEMS:], start=1):
            self.handles[f"c{number}"] = item.item_id
            decided = (
                "you" if item.decided_by == AGENT else ("the person" if item.decided_by is not None else None)
            )
            shown = {
                ProposalState.ACCEPTED: "in the plan",
                ProposalState.PENDING: "waiting for the person",
                ProposalState.REJECTED: "left out",
            }[item.state]
            tried = item.payload.get("tried")
            plan.append(
                {
                    "id": f"c{number}",
                    "what": self._text(item.upload_id, clean_text(item.title, SHORT)),
                    "made_by": {"rules": "the rules", "ai": "you", "person": "the person"}[item.source.value],
                    "state": shown,
                    **({"decided_by": decided} if decided else {}),
                    **({"tried_on_first_rows": tried} if isinstance(tried, dict) else {}),
                }
            )
        asked: list[dict[str, Any]] = []
        for number, q in enumerate(
            sorted(questions, key=lambda q: q.answer is not None)[:STATE_QUESTIONS], start=1
        ):
            self.handles[f"k{number}"] = q.question_id
            options = [
                {"id": o.option_id, "label": self._text(q.upload_id, clean_text(o.label, SHORT))}
                for o in q.options
            ]
            chosen = next((o["label"] for o in options if o["id"] == q.answer), None)
            asked.append(
                {
                    "id": f"k{number}",
                    "text": self._text(q.upload_id, clean_text(q.text, SHORT)),
                    "options": options,
                    "answer": chosen,
                }
            )
        doubts = [
            self._text(d.upload_id, clean_text(d.because, SHORT)) for d in self.case.doubts[-STATE_DOUBTS:]
        ]
        return {"plan": plan, "questions": asked, "doubts": doubts}

    def _reload_looks(self, storage: Storage | None) -> list[dict[str, Any]]:
        """The latest looks of earlier turns, back from the case's stored evidence, so the model can cite them
        (`evidence_ids`) and state their counts. Only the look and its column go in the prompt, not the result.
        """
        from engine.agent.tools import TOOLS
        from engine.doctor.case import evidence_key

        if storage is None:
            return []
        shown: list[dict[str, Any]] = []
        refs = [r for r in self.case.evidence if r.tool in TOOLS and r.upload_id is not None]
        for ref in refs[-RELOADED_LOOKS:]:
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

    def _history(self) -> str:
        """The last turns of the conversation before the message now being answered."""
        earlier = self.case.chat[:-1][-HISTORY_TURNS:]
        if not earlier:
            return "(this is the first message)"
        turns = [
            {
                "who": "person" if entry.role == "user" else "you",
                "text": self._text(None, clean_text(entry.text, 800)),
            }
            for entry in earlier
        ]
        return json.dumps(turns, ensure_ascii=False)

    # -- the loop ----------------------------------------------------------------------------------
    def talk(self, message: str) -> ChatTurn:
        from engine.agent.egress import MAX_SENT_ITEMS, STATE_TOOL, scrub, sent_item

        turn, strikes, reply, blocked = self.turn, 0, "", None
        text = self._text(
            None, scrub(message[:MAX_MESSAGE_CHARS])
        )  # names of any file, as the state shows them
        self.checker.grounded = self.checker.grounded | grounded_numbers([text])
        prompt = load_prompt(CHAT_PROMPT, self.config_root)
        tools = self._tools()
        goal = self.brief.goal_name if self.brief is not None else "see what these files can be used for"
        history = self._history()
        self.checker.grounded = self.checker.grounded | grounded_numbers([history])  # counts it gave earlier
        # What the person is told the AI looked at: the case as the model reads it, and the conversation.
        shown = json.dumps(self.state, ensure_ascii=False) + "\n" + history
        self.turn.sent.append(sent_item(self.gates[0], STATE_TOOL, {}, shown))
        for _ in range(self.max_steps + 1):
            variables = {
                "goal": goal,
                "tools": tools,
                "questions": catalogue_lines(chat=True),
                "state": json.dumps(self.state, ensure_ascii=False),
                "conversation": history,
                "message": text,
                "turn_results": (
                    json.dumps(for_prompt(turn.feedback, {}), ensure_ascii=False) if turn.feedback else "none"
                ),
            }
            try:
                answer = self._complete(prompt, variables)
            except _NoAnswerError as stop:
                blocked = stop.reason
                break
            try:
                action = _plain_or_action(answer)
                if str(action["action"]) == "reply":
                    reply = self._reply(action)
                    break
                self._act(action)
            except AgentToolError as exc:
                strikes += 1
                if strikes >= STRIKES:
                    blocked = exc.code
                    break
                turn.feedback.append(self.gates[0].prepare({"error": exc.code, "message": exc.message}))
            except Exception as exc:  # a model's odd input must never cost a paid turn that nobody records
                _LOG.warning("doctor chat step failed: %s", type(exc).__name__)
                blocked = "failed"
                break
        items, evidence = self._items()
        made = {item.item_id for item in items}
        chosen = self.chosen
        withdrawn = [i for i in dict.fromkeys([*chosen.withdrawn, *chosen.replaces]) if i not in made]
        if not reply:
            reply = (
                UNAVAILABLE
                if blocked == "unavailable"
                else (OUT_OF_STEPS if blocked is None else NOTHING_SAID)
            )
        return ChatTurn(
            reply=reply,
            items=tuple(items),
            evidence=tuple(evidence),
            asks=tuple(turn.asks),
            doubts=tuple(turn.doubts),
            requests=tuple(turn.requests),
            withdrawn=tuple(withdrawn),
            suggestion=chosen.suggestion,
            tools=tuple(dict.fromkeys(chosen.used)),
            sent=tuple(s.model_dump(mode="json") for s in turn.sent[:MAX_SENT_ITEMS]),
            blocked_by=blocked,
        )

    def _reply(self, action: dict[str, Any]) -> str:
        said = str(action.get("text") or "").strip()
        if not said:
            raise AgentToolError("AGENT_TEXT_REFUSED", "Say something to the person in `text`.")
        failure = self.checker.failure(said, target="doctor reply", max_chars=MAX_REPLY_CHARS)
        if failure is not None:
            raise AgentToolError(
                "AGENT_TEXT_REFUSED",
                f"The reply failed a check ({failure}); use only numbers from a tool result or the person's words.",
            )
        return self.gates[0].person_text(said)

    # -- the actions -------------------------------------------------------------------------------
    def _act(self, action: dict[str, Any]) -> None:
        name = str(action["action"])
        raw = action.get("args")
        args = dict(raw) if isinstance(raw, dict) else {}
        given = args.get("replaces")
        replaces = (
            self._own(given).item_id if isinstance(given, str) else None
        )  # refused before anything is made
        if name == "withdraw":
            self._withdraw(args)
        elif name == "suggest_answer":
            self._suggest(args)
        elif name == "ask":
            self._ask(action, args)
        elif name in {"join_files", "stack_files"}:
            self._combine(name, args)
        else:
            super()._act(action)
        if replaces is not None:
            self.chosen.replaces.append(replaces)
        self.chosen.used.append("ask" if name == "ask_question" else name)
        if name in _MAKES_CHANGES:
            self._report()

    def _report(self) -> None:
        """Tell the model, before it writes its reply, which of its new changes the visit will apply for the person
        and which wait for their tick (and why): what it says it did is what happens (`agent_apply`). Each change
        is judged once, with the ones already applied in this turn in place."""
        from engine.doctor.agent_apply import agent_applies, roles_of, twin_of
        from engine.doctor.visit import active_section

        items, _evidence = self._items()
        gone = {*self.chosen.withdrawn, *self.chosen.replaces}
        plan = [
            i.model_copy(update={"state": ProposalState.REJECTED}) if i.item_id in gone else i
            for i in active_section(self.case)[0]
        ]
        fresh: list[CaseItem] = []
        for item in items:
            if item.item_id in self.chosen.reported:
                continue
            twin = twin_of(self.case.items, item)
            if twin is None:
                fresh.append(item)
                continue
            self.chosen.reported.add(item.item_id)
            said = {"change": self._text(item.upload_id, clean_text(item.title, 600))}
            if twin.state is ProposalState.REJECTED:
                who = "the person took" if person_owns(self.case, twin) else "you took"
                said["result"] = (
                    f"That was left out earlier ({who} it back); it stays out unless the person ticks it."
                )
            else:
                said["result"] = "That change is already in the case."
            self.turn.feedback.append(said)
        decided = agent_applies(
            self.files,
            self.case.files,
            [*plan, *self.chosen.applied],
            fresh,
            self.clock(),
            roles_of(self.case, self.config_root),
        )
        for item, waits in decided:
            self.chosen.reported.add(item.item_id)
            said = {"change": self._text(item.upload_id, clean_text(item.title, 600))}
            if waits is None:
                self.chosen.applied.append(item)
                said["result"] = "I will apply it for the person, who can undo it with one click."
            else:
                why = self._text(item.upload_id, clean_text(waits, SHORT))
                self.checker.grounded = self.checker.grounded | grounded_numbers([why])
                said["result"] = f"It waits for the person's tick, because {why}"
            self.turn.feedback.append(said)

    def _own(self, handle: str) -> CaseItem:
        """One of the agent's own changes (by the handle the state gave it) that it may take back: waiting, or
        applied by the agent itself."""
        item_id = self.handles.get(handle)
        item = next((i for i in self.case.items if i.item_id == item_id), None)
        if item is None or item.source is not ProposalSource.AI:
            raise AgentToolError("AGENT_ITEM_UNKNOWN", "You have no change of your own with that id.")
        if item.state is ProposalState.REJECTED:
            raise AgentToolError("AGENT_ITEM_UNKNOWN", "That change is already left out.")
        if person_owns(self.case, item):
            raise AgentToolError(
                "AGENT_ITEM_PERSONS",
                "The person ticked that change or gave it their own yes; only they can take it back.",
            )
        return item

    def _withdraw(self, args: dict[str, Any]) -> None:
        item = self._own(str(args.get("item_id", "")))
        if item.item_id not in self.chosen.withdrawn:
            self.chosen.withdrawn.append(item.item_id)
        self.turn.feedback.append({"withdrawn": str(args.get("item_id"))})

    def _step(self, action: dict[str, Any], args: dict[str, Any]) -> None:
        """A typed change, also by leaving out the rows a rule names; what it measured grounds the reply."""
        from engine.agent.loop import DOCTOR_STEP_KINDS, _propose_step

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
            kinds=DOCTOR_STEP_KINDS,
        )
        turn.steps.append((index, result, proposal))
        turn.looks.append(result)
        turn.files_of[result.evidence_id] = index
        self.checker.grounded = self.checker.grounded | evidence_numbers(result.result, result.typed_args)
        self._record(index, result, _STEP_KEYS)

    # -- questions ---------------------------------------------------------------------------------
    def _ask(self, action: dict[str, Any], args: dict[str, Any]) -> None:
        """A catalogue question, or a quick one (`text` and `options`); its id and answers go back to the model
        so it can suggest one."""
        if str(args.get("question", "")) in OUTCOME_QUESTIONS:
            self._outcome_ask(action, args)
        elif "question" in args:
            super()._ask(action, args)
            self.turn.feedback.pop()  # the review's one-word note; the chat's carries the ids
        else:
            self._quick(action, args)
        ask = self.turn.asks[-1]
        qid = question_id_of(ask.code, ask.subject, ask.goal_id)
        handle = next((h for h, known in self.handles.items() if known == qid), None)
        if handle is None:
            handle = f"k{sum(1 for h in self.handles if h.startswith('k')) + 1}"
            self.handles[handle] = qid
        self.chosen.asked[qid] = ask
        gate = self._file_gate(ask.upload_id)
        self.turn.feedback.append(
            {
                "asked": ask.code,
                "question_id": handle,
                "options": [{"id": o.effect.option_id, "label": gate.text(o.label)} for o in ask.options],
            }
        )

    def _outcome_ask(self, action: dict[str, Any], args: dict[str, Any]) -> None:
        """ "What counts as a yes": each answer is built by our code (`engine.doctor.outcome_answer`), with the counts
        it measured, from what the model names: a column of the file and its yes values (`outcomes[i]` =
        `{"column", "yes": [...]}`), or the column ONE query it proposed (`propose_sql`) builds on the customer table
        (`{"column", "sql": "<output_name>"}`). The person's click records the outcome; the query step waits for it.
        """
        from engine.doctor.agent_review import _decided
        from engine.doctor.outcome_answer import column_offer, query_item, sql_offer
        from engine.doctor.outcome_definition import OutcomeError
        from engine.doctor.tools_sql import SqlStepProposal
        from engine.doctor.visit import active_goal

        goal = active_goal(self.case)
        if goal is None:
            raise AgentToolError(
                "AGENT_QUESTION_REFUSED",
                "The outcome belongs to the goal: ask it after the person has chosen what the campaign is for.",
            )
        index = self._index(action, args)
        ctx, file, gate = self.ctxs[index], self.files[index], self.gates[index]
        args = dict(gate.unalias_args(args))
        rules, specs = args.get("choices"), args.get("outcomes")
        if (
            not isinstance(rules, list)
            or not isinstance(specs, list)
            or not 1 <= len(rules) == len(specs) <= MAX_OUTCOMES
            or not all(isinstance(r, str) and r.strip() for r in rules)
            or not all(isinstance(spec, dict) for spec in specs)
        ):
            raise AgentToolError(
                "AGENT_QUESTION_INVALID",
                f"Q24 needs 1 to {MAX_OUTCOMES} `choices` (the rule in the person's words) and one `outcomes` entry "
                'for each: {"column": "...", "yes": ["..."]} for a column of the file, or {"column": "...", '
                '"sql": "<output_name of your propose_sql>"} for a column your one query builds.',
            )
        key = _decided(self.case, "key", ctx.upload_id) or next(
            (c.key_columns[0] for c in self.case.files if c.upload_id == ctx.upload_id and c.key_columns),
            None,
        )
        marked = {c.name for c in ctx.profile.columns if c.pii_kinds} | set(
            ctx.config.agent.always_hide_columns
        )
        offers = []
        for rule, spec in zip(rules, specs, strict=True):
            failure = self.checker.failure(rule, target="doctor question", max_chars=400)
            if failure is not None:
                raise AgentToolError(
                    "AGENT_QUESTION_REFUSED",
                    f"The rule failed a check ({failure}); use the person's own numbers.",
                )
            words = gate.person_text(rule)
            try:
                if "sql" in spec:
                    if key is None:
                        raise AgentToolError("AGENT_QUESTION_INVALID", "Settle which column is the ID first.")
                    name = str(spec["sql"])
                    made = self.turn.queries.get(name)
                    step: Any = None
                    if made is not None:
                        step = SqlStepProposal.model_validate(made.result.output).step
                    elif (earlier := query_item(self.case.items, name)) is not None:
                        step = earlier.payload.get("step")
                    if not isinstance(step, dict):
                        raise AgentToolError(
                            "AGENT_QUESTION_INVALID", f"Propose the query '{name[:40]}' first (propose_sql)."
                        )
                    offers.append(
                        sql_offer(step, str(spec.get("column", "")), self.files, file, key, rule=words)
                    )
                else:
                    column = ctx.resolve(str(spec.get("column", "")))
                    if column == key or column in marked:
                        raise AgentToolError(
                            "AGENT_QUESTION_REFUSED",
                            "The ID or a column of personal data is never the outcome.",
                        )
                    yes = spec.get("yes")
                    offers.append(
                        column_offer(
                            ctx.frame,
                            column,
                            yes if isinstance(yes, list) else [],
                            upload_id=ctx.upload_id,
                            rule=words,
                        )
                    )
            except OutcomeError as exc:
                raise AgentToolError("AGENT_OUTCOME_REFUSED", f"{exc.code}: {exc.message}"[:400]) from exc
        if self.turn.blocking >= MAX_BLOCKING:
            raise AgentToolError("AGENT_BLOCKING_LIMIT", "That is enough blocking questions for one message.")
        self.turn.blocking += 1
        try:
            ask = build_ask(
                "Q24",
                {"file": file.file_name or "this file", "value": "|".join(o.says for o in offers)},
                rows=len(ctx.frame),
                choices=[o.label for o in offers],
                effects=[o.effect for o in offers],
                choice_says=[o.effect_text for o in offers],
                upload_id=ctx.upload_id,
                goal_id=goal.goal_id,
            )
        except QuestionError as exc:
            raise AgentToolError("AGENT_QUESTION_INVALID", str(exc)[:300]) from exc
        self.turn.asks.append(ask)

    def _quick(self, action: dict[str, Any], args: dict[str, Any]) -> None:
        index = self._index(action, args)
        ctx = self.ctxs[index]
        args = dict(self.gates[index].unalias_args(args))
        text, why = str(args.get("text") or "").strip(), str(args.get("why") or "").strip()
        given = args.get("options")
        options = [o.strip() for o in given if isinstance(o, str)] if isinstance(given, list) else []
        if not text or not 2 <= len(options) <= 4 or len(set(options)) != len(options) or not all(options):
            raise AgentToolError(
                "AGENT_QUESTION_INVALID",
                "A quick question needs `text` and 2 to 4 different `options`, each a short sentence.",
            )
        if self.chosen.quick >= MAX_QUICK:
            raise AgentToolError("AGENT_BLOCKING_LIMIT", "That is enough quick questions for one message.")
        for part in (text, why, *options):
            if len(part) > SHORT:
                raise AgentToolError("AGENT_QUESTION_INVALID", "Keep the question and its options short.")
            if _MEANING_TOPICS.search(part):
                raise AgentToolError(
                    "AGENT_QUESTION_REFUSED",
                    "A quick question is not about consent, the outcome, the ID or the date. "
                    "Use the question list for those.",
                )
            if _DIGIT.search(part):
                raise AgentToolError(
                    "AGENT_QUESTION_REFUSED",
                    "A quick question states no numbers. For what counts as a yes use the question list.",
                )
            failure = self.checker.failure(part, target="doctor question", max_chars=400) if part else None
            if failure is not None:
                raise AgentToolError(
                    "AGENT_QUESTION_REFUSED", f"The question failed a check ({failure}); reword it."
                )
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
        self.turn.asks.append(ask)

    def _suggest(self, args: dict[str, Any]) -> None:
        """Point at one answer of an open question. Nothing is recorded; the person's click does that."""
        if self.chosen.suggestion is not None:
            raise AgentToolError("AGENT_SUGGESTION_LIMIT", "Suggest one answer per message.")
        qid = self.handles.get(str(args.get("question_id", "")), "")
        oid = str(args.get("option_id", ""))
        labels: dict[str, str] = {}
        if qid in self.chosen.asked:
            labels = {o.effect.option_id: o.label for o in self.chosen.asked[qid].options if not o.safe}
        else:
            question = next(
                (q for q in self.case.questions if q.question_id == qid and q.answer is None), None
            )
            if question is not None:
                labels = {
                    o.option_id: o.label for o in question.options if not o.safe and o.label != DONT_KNOW
                }
        if oid not in labels:
            raise AgentToolError(
                "AGENT_SUGGESTION_UNKNOWN", "Suggest an answer of a question that is open, by its ids."
            )
        self.chosen.suggestion = (qid, oid)
        self.turn.feedback.append({"suggested": str(args.get("question_id"))})

    # -- files -------------------------------------------------------------------------------------
    def _combine(self, name: str, args: dict[str, Any]) -> None:
        """A join or a stack of the named files: the rules' own plan, counts and guards on exactly those files."""
        from engine.doctor.combine_run import JOIN_KIND, STACK_KIND
        from engine.doctor.combining import propose_combination

        wanted = args.get("files")
        taken = [str(f) for f in wanted] if isinstance(wanted, list) else []
        indexes = [self._index({}, {"file": f}) for f in taken]
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
            self.turn.asks.extend(asks)
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
        self.turn.asks.extend(asks)

    def _payload_extra(self, result: ToolResult) -> dict[str, Any]:
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

    def _items(self) -> tuple[list[CaseItem], list[DataToolResult]]:
        items, evidence = super()._items()
        items.extend(self.chosen.combined)
        return items, evidence


_STEP_KEYS: Final[frozenset[str]] = frozenset({"kind", "column"})
_MAKES_CHANGES: Final[frozenset[str]] = frozenset(
    {"propose_step", "propose_sql", "join_files", "stack_files"}
)


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
    """CONVERSE as the Data Doctor's chat: the review's loop with the conversation in front of it.

    The AI is asked only when the client has allowed it and the case has spending left (`AiGuard.ask`); otherwise the
    reply says so and the visit goes on with its rules. A turn is `data_doctor.ai_limits.max_chat_steps` actions at most.
    """

    def converse(case: Case, message: str, files: tuple[LoadedFile, ...]) -> ConverseResult:
        from engine.doctor.ai_guard import AI_LIMIT_REPLY, AI_NO_CONSENT_REPLY
        from engine.doctor.visit import ConverseResult, visit_registry

        allowed = meter.ask(case)
        if allowed != "ok":
            return ConverseResult(
                reply=AI_NO_CONSENT_REPLY if allowed == "no_consent" else AI_LIMIT_REPLY, ai=False
            )
        if not files:
            return ConverseResult(reply="Add a file first, and I will look at it with you.", ai=False)
        calls, tokens = meter.calls_used(), meter.tokens_used()
        chat = _Chat(
            case,
            files,
            None,
            context_for,
            meter,
            guardrails,
            principal,
            config_root,
            registry or visit_registry(),
            chat_steps=meter.chat_steps,
            storage=storage,
        )
        done = chat.talk(message)
        spent = done.blocked_by == "unavailable" and meter.spent()
        return ConverseResult(
            reply=AI_LIMIT_REPLY if spent else done.reply,
            items=done.items,
            evidence=done.evidence,
            sent=done.sent,
            asks=done.asks,
            doubts=done.doubts,
            requests=done.requests,
            withdrawn=done.withdrawn,
            suggestion=done.suggestion,
            tools=done.tools,
            model_calls=meter.calls_used() - calls,
            tokens=meter.tokens_used() - tokens,
        )

    return converse
