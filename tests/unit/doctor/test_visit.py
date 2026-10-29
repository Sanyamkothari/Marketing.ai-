"""The visit and the goal brief (Plan O M235, DEC-1805; the design §3.3-§3.4).

Acceptance (Plan O §M235):

* Kill the process at every state; the visit resumes with nothing lost or repeated.
* With no AI service, every state completes.
* Setting a goal mid-case keeps every decided item and asks nothing again.
* Two goals on one case keep separate goal sections; switching back restores the first.
* `brief_hash` is stable for an unchanged configuration (also `test_goal_brief.py`).

Also: generic mode says what the files can power and what is missing; CONVERSE is the Doctor's chat, whose
suggestions land pending and the clean ones are applied by the agent (`test_doctor_chat.py`); a retried action is applied once; VERIFY sends a not-ready case back to
TREAT with its reason; forgetting an upload forgets it in the visit.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from engine.agent.contracts import AgentConfidence, ProposalSource, ProposalState
from engine.config import load_use_case
from engine.doctor import visit
from engine.doctor.case import (
    Case,
    CaseError,
    CaseStore,
    TimelineKind,
    VisitState,
    case_dir,
    forget_upload,
)
from engine.doctor.diagnose import LoadedFile, Offer
from engine.doctor.goal_brief import load_goal_brief
from engine.doctor.registry import DataToolContext, DataToolRegistry, DataToolResult
from engine.doctor.visit import AI_NEEDED_REPLY, VisitDeps, active_section, mode_of, open_blocking
from engine.storage import LocalStorage
from tests.unit.doctor import visit_support as vs
from tests.unit.doctor.visit_support import (
    ANALYST,
    CLIENT,
    FIRST,
    GOAL_A,
    GOAL_B,
    SCRIPT,
    World,
    deps_for,
    open_case,
    option,
    question,
    summary,
    timeline_summary,
)

ALL_STATES = {state.value for state in VisitState}


def _content(frame: pd.DataFrame) -> str:
    """What a frame holds (names, types and every value), as a digest: two files under one upload id differ here."""
    digest = hashlib.sha256(
        json.dumps([list(map(str, frame.columns)), list(map(str, frame.dtypes))]).encode()
    )
    digest.update(pd.util.hash_pandas_object(frame, index=True).to_numpy().tobytes())
    return digest.hexdigest()


class _Cached(DataToolRegistry):
    """The visit's registry, remembering each answer: the tools are deterministic, so a test that runs the
    same visit many times need not compute the same diagnosis again. Every call still goes through `call`.

    The key names what the tool reads, by CONTENT: the memory is shared by every module that imports it, in one
    process, and other modules put other files under the same upload ids (`test_agent_review`'s `_dirty` world is
    `up-customers-oct` with a `city` column). Keyed by upload id alone, an answer computed for that other file was
    handed to this module's visit whenever both ran in one worker, and the in-process run no longer matched a
    fresh process that computed it (`test_a_real_process_killed_mid_visit_resumes`)."""

    def __init__(self, inner: DataToolRegistry, memory: dict[str, DataToolResult]) -> None:
        super().__init__()
        self._tools = inner._tools
        self._memory = memory

    def call(self, name: str, given: Mapping[str, Any], ctx: DataToolContext) -> DataToolResult:
        agent = ctx.agent
        key = json.dumps(
            [
                name,
                dict(given),
                [[f.upload_id, f.file_name, _content(f.frame)] for f in ctx.files],
                None if agent is None else [agent.upload_id, agent.use_case_id, _content(agent.frame)],
                [[offer.config.model_dump_json(), offer.stage] for offer in ctx.offers],
            ],
            sort_keys=True,
            default=str,
        )
        if key not in self._memory:
            self._memory[key] = super().call(name, given, ctx)
        return self._memory[key]


_MEMORY: dict[str, DataToolResult] = {}


@pytest.fixture
def world() -> World:
    return vs.world()


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path / "data")


def _deps(storage: LocalStorage, w: World, **extra: Any) -> VisitDeps:
    return deps_for(storage, w, registry=_Cached(visit.visit_registry(), _MEMORY), **extra)


def _states(storage: LocalStorage, case: Case) -> list[str]:
    return [
        str(e.details["to"])
        for e in CaseStore(storage).timeline(CLIENT, case.case_id)
        if e.kind is TimelineKind.STATE_CHANGED
    ]


def _reload_id(storage: LocalStorage, case_id: str) -> Case:
    return CaseStore(storage).get(CLIENT, case_id)


def _reload(storage: LocalStorage, case: Case) -> Case:
    return CaseStore(storage).get(CLIENT, case.case_id)


def _generic(storage: LocalStorage, w: World) -> Case:
    """A case resumed to its first rest in generic mode, the key question answered."""
    case = visit.resume(CLIENT, open_case(storage, w).case_id, _deps(storage, w), actor=ANALYST)
    assert case.state is VisitState.ASK and mode_of(case) == "generic"
    q = question(case, "KEY_AMBIGUOUS")
    return visit.answer(
        CLIENT, case.case_id, q.question_id, option(q, "customer_id"), _deps(storage, w),
        expected_version=case.version, actor=ANALYST,
    )  # fmt: skip


def _goal(storage: LocalStorage, w: World, case: Case, goal_id: str | None) -> Case:
    return visit.set_goal(
        CLIENT, case.case_id, goal_id, _deps(storage, w), expected_version=case.version, actor=ANALYST
    )


def _answer(storage: LocalStorage, w: World, case: Case, code: str, column: str) -> Case:
    q = question(case, code)
    return visit.answer(
        CLIENT, case.case_id, q.question_id, option(q, column), _deps(storage, w),
        expected_version=case.version, actor=ANALYST,
    )  # fmt: skip


# ---------------------------------------------------------------------------
# Generic mode
# ---------------------------------------------------------------------------
def test_without_a_goal_it_says_what_the_files_can_power_and_what_is_missing(
    storage: LocalStorage, world: World
) -> None:
    faults = Offer(config=load_use_case("fault-prediction"), stage="Operate")
    w = World(files=world.files, offers=(*world.offers, faults))
    case = visit.resume(CLIENT, open_case(storage, w).case_id, _deps(storage, w), actor=ANALYST)
    assert mode_of(case) == "generic" and not case.goals
    assert case.diagnosis is not None
    listed: Any = case.diagnosis["use_cases"]
    verdicts = {v["use_case_id"]: v["status"] for v in listed}
    assert set(verdicts) == {GOAL_A, GOAL_B, "fault-prediction"}
    assert verdicts["fault-prediction"] == "missing"
    assert case.visit is not None and case.visit.missing_for == ("fault-prediction",)
    request = case.visit.data_request
    assert request is not None and request in {e.evidence_id for e in case.evidence}
    # The general question it asks does not depend on a goal; nothing goal-specific is asked.
    assert [q.code for q in case.questions] == ["KEY_AMBIGUOUS"] and case.questions[0].goal_id is None
    assert case.files[0].role is not None, "UNDERSTAND recorded what the file is"


# ---------------------------------------------------------------------------
# With no AI service, every state completes
# ---------------------------------------------------------------------------
def test_with_no_ai_service_every_state_completes(storage: LocalStorage, world: World) -> None:
    case_id = open_case(storage, world).case_id
    _script(storage, world, case_id, stale=30.0)
    case = _reload_id(storage, case_id)
    assert set(_states(storage, case)) == ALL_STATES, "every state was entered and left or rested in"
    assert case.state is VisitState.APPROVE and case.visit is not None and case.visit.stopped is None
    user, agent = case.chat
    assert (
        user.role == "user" and "98765" not in user.text
    ), "the person's message is masked before it is kept"
    assert agent.role == "agent" and agent.text == AI_NEEDED_REPLY and agent.sent == ()
    assert case.counters.model_calls == 0
    assert case.approvals and case.approvals[0].approved_by == ANALYST and case.approvals[0].text_shown
    assert case.prescriptions and case.readiness[0].verdict == "ready"
    assert case.visit.hand_off is not None and case.visit.hand_off.target == "runs"
    assert case.visit.taken_in == (vs.FIRST, vs.SECOND), "next month's file was read from INTAKE"


# ---------------------------------------------------------------------------
# Setting a goal mid-case: nothing decided is lost, nothing is asked again
# ---------------------------------------------------------------------------
def test_setting_a_goal_mid_case_keeps_every_decided_item_and_asks_nothing_again(
    storage: LocalStorage, world: World
) -> None:
    case = _generic(storage, world)
    assert case.state is VisitState.APPROVE and mode_of(case) == "generic"
    before_items = {i.item_id: i for i in case.items}
    before_questions = {q.question_id: q for q in case.questions}
    assert any(i.kind == "key" and i.state is ProposalState.ACCEPTED for i in before_items.values())
    case = _goal(storage, world, case, GOAL_A)
    assert mode_of(case) == "goal" and case.goals[0].brief_hash == load_goal_brief(GOAL_A).brief_hash
    assert "goal_brief" in _states(storage, case)
    for item_id, item in before_items.items():
        assert next(i for i in case.items if i.item_id == item_id) == item, "a decided item is kept as it was"
    for question_id, kept in before_questions.items():
        assert next(q for q in case.questions if q.question_id == question_id) == kept
    new = [q for q in case.questions if q.question_id not in before_questions]
    assert [q.code for q in new] == ["OUTCOME_COLUMN"], "only the goal's own question is new"
    assert all(q.goal_id == GOAL_A for q in new)
    # Choosing the same goal again asks nothing at all.
    case = _answer(storage, world, case, "OUTCOME_COLUMN", "converted")
    asked = len(case.questions)
    case = _goal(storage, world, case, GOAL_A)
    assert len(case.questions) == asked and not open_blocking(case)


# ---------------------------------------------------------------------------
# Two goals, two sections
# ---------------------------------------------------------------------------
def test_two_goals_keep_separate_sections_and_switching_back_restores_the_first(
    storage: LocalStorage, world: World
) -> None:
    case = _goal(storage, world, _generic(storage, world), GOAL_A)
    case = _answer(storage, world, case, "OUTCOME_COLUMN", "converted")
    section_a = [i for i in case.items if i.goal_id == GOAL_A]
    consent = next(i for i in section_a if i.kind == "consent_column")
    case = visit.decide(
        CLIENT, case.case_id, consent.item_id, True, _deps(storage, world),
        expected_version=case.version, actor=ANALYST,
    )  # fmt: skip
    section_a = [i for i in case.items if i.goal_id == GOAL_A]
    questions_a = [q for q in case.questions if q.goal_id == GOAL_A]
    hash_a = case.goals[0].brief_hash

    case = _goal(storage, world, case, GOAL_B)
    assert [g.goal_id for g in case.goals if g.active] == [GOAL_B]
    q = question(case, "OUTCOME_COLUMN")
    assert q.goal_id == GOAL_B and {o.label for o in q.options} >= {"churned"}
    case = _answer(storage, world, case, "OUTCOME_COLUMN", "churned")
    items, _questions = active_section(case)
    assert not [i for i in items if i.goal_id == GOAL_A], "the first goal's section is set aside"
    assert [i for i in case.items if i.goal_id == GOAL_A] == section_a, "set aside, not deleted"
    outcome_b = next(i for i in items if i.kind == "outcome_column" and i.goal_id == GOAL_B)
    assert outcome_b.payload["column"] == "churned"
    with pytest.raises(CaseError) as refused:
        visit.decide(
            CLIENT, case.case_id, consent.item_id, False, _deps(storage, world),
            expected_version=case.version, actor=ANALYST,
        )  # fmt: skip
    assert refused.value.code == "VISIT_UNKNOWN_CHOICE"
    section_b = [i for i in case.items if i.goal_id == GOAL_B]

    case = _goal(storage, world, case, GOAL_A)
    assert [i for i in case.items if i.goal_id == GOAL_A] == section_a, "switching back restores the first"
    assert [q for q in case.questions if q.goal_id == GOAL_A] == questions_a
    assert [i for i in case.items if i.goal_id == GOAL_B] == section_b, "the second is kept aside in turn"
    assert next(g for g in case.goals if g.goal_id == GOAL_A).brief_hash == hash_a, "brief_hash is stable"
    assert not open_blocking(case) and case.state is VisitState.APPROVE
    items, _ = active_section(case)
    assert {i.goal_id for i in items} == {None, GOAL_A}


# ---------------------------------------------------------------------------
# Kill the process at every state
# ---------------------------------------------------------------------------
class _Killed(BaseException):
    """The process dies: nothing after this point runs (not even an `except Exception`)."""


class _Crashing(LocalStorage):
    """Counts writes of `case.json`; at the `kill_at`-th the process dies, before or after the write."""

    def __init__(self, root: Path, kill_at: int, *, after: bool) -> None:
        super().__init__(root)
        self.kill_at = kill_at
        self.after = after
        self.states: list[str] = []

    def write_bytes(self, key: str, data: bytes) -> None:
        if key.endswith("/case.json"):
            self.states.append(json.loads(data)["state"])
            if len(self.states) == self.kill_at:
                if self.after:
                    super().write_bytes(key, data)  # case.json is written; the timeline is not
                raise _Killed
        super().write_bytes(key, data)


def _run(root: Path, w: World, kill_at: int, *, after: bool) -> tuple[Case, _Crashing]:
    """Run the script, dying at `kill_at`; then a new process resumes and the client sends what it had not."""
    crashing = _Crashing(root, kill_at, after=after)
    w.ticks = 0
    case_id = (
        CaseStore(LocalStorage(root), clock=w.clock)
        .create(client_id=CLIENT, created_by=ANALYST, files=[vs.case_file(FIRST, w)])
        .case_id
    )
    try:
        _script(crashing, w, case_id, stale=30.0)
    except _Killed:
        lock = case_dir(CLIENT, case_id) + "case.lock"
        storage = LocalStorage(root)
        if not storage.exists(lock):  # a dead process leaves its lock behind
            storage.write_bytes(lock, json.dumps({"holder": "dead", "at": vs.T0.isoformat()}).encode())
        restarted = LocalStorage(root)
        visit.resume(CLIENT, case_id, _deps(restarted, w, stale=0.0), actor=ANALYST)
        _script(restarted, w, case_id, stale=0.0)
    return CaseStore(LocalStorage(root)).get(CLIENT, case_id), crashing


def _script(storage: LocalStorage, w: World, case_id: str, *, stale: float) -> None:
    for _name, op, act in SCRIPT:
        case = CaseStore(storage).get(CLIENT, case_id)
        if op is not None and case.visit is not None and op in case.visit.ops:
            continue  # the client reloads the case and sees its action was written
        act(_deps(storage, w, stale=stale), case)


@pytest.mark.parametrize("after", [False, True], ids=["before-the-write", "after-the-write"])
def test_kill_the_process_at_every_state_and_the_visit_resumes_with_nothing_lost_or_repeated(
    tmp_path: Path, world: World, after: bool
) -> None:
    clean, counted = _run(tmp_path / "clean", world, 0, after=after)
    expected = summary(clean)
    expected_timeline = timeline_summary(LocalStorage(tmp_path / "clean"), clean)
    writes = len(counted.states)
    assert set(counted.states) == ALL_STATES, "the script writes every state"
    covered: set[str] = set()
    for kill_at in range(1, writes + 1):
        root = tmp_path / f"kill-{kill_at}"
        case, crashing = _run(root, world, kill_at, after=after)
        where = f"killed at write {kill_at} ({counted.states[kill_at - 1]})"
        assert summary(case) == expected, where
        assert timeline_summary(LocalStorage(root), case) == expected_timeline, where
        covered.add(crashing.states[kill_at - 1])
    assert covered == ALL_STATES, "a kill was tried at every state"


def test_a_real_process_killed_mid_visit_resumes(tmp_path: Path, world: World) -> None:
    """The same, with real processes: the child dies with `os._exit` right after a write (lock left behind)."""
    clean, counted = _run(tmp_path / "clean", world, 0, after=True)
    points = [counted.states.index("verify") + 1, counted.states.index("converse") + 1]
    for kill_at in points:
        data = tmp_path / f"real-{kill_at}"
        command = [sys.executable, "-m", "tests.unit.doctor.visit_support", str(data), str(kill_at)]
        died = subprocess.run(command, capture_output=True, text=True, cwd=Path.cwd(), timeout=600)
        assert died.returncode == 17, died.stderr[-2000:]
        lived = subprocess.run(
            [*command[:-1], "0"], capture_output=True, text=True, cwd=Path.cwd(), timeout=600
        )
        assert lived.returncode == 0, lived.stderr[-2000:]
        storage = LocalStorage(data)
        (case,) = CaseStore(storage).list(CLIENT)
        assert summary(case) == summary(clean)
        assert timeline_summary(storage, case) == timeline_summary(LocalStorage(tmp_path / "clean"), clean)


def test_the_shared_tool_memory_never_hands_one_files_answer_to_another_file_under_the_same_id(
    tmp_path: Path, world: World
) -> None:
    """The memory `_Cached` keeps is shared by every module that imports it. Another module's file under the same
    upload id (here: the October customers with a `city` column, as `test_agent_review` builds) answered first, and
    the visit of this module's file must still read its own file: the same case as with no memory at all."""
    frame = vs.customers(seed=1)
    frame["city"] = ["Pune", "pune ", "Mumbai"] * (len(frame) // 3)
    other = World(
        files={FIRST: vs.loaded(FIRST, "customers_oct.csv", frame, world.offers[0].config)},
        offers=world.offers,
    )
    memory: dict[str, DataToolResult] = {}

    def first_rest(name: str, w: World, registry: DataToolRegistry | None) -> Case:
        storage = LocalStorage(tmp_path / name)
        w.ticks = 0
        extra = {} if registry is None else {"registry": registry}
        case = open_case(storage, w)
        return visit.resume(CLIENT, case.case_id, deps_for(storage, w, **extra), actor=ANALYST)

    first_rest("other", other, _Cached(visit.visit_registry(), memory))
    remembered = first_rest("cached", world, _Cached(visit.visit_registry(), memory))
    fresh = first_rest("fresh", world, None)
    assert summary(remembered) == summary(fresh)


# ---------------------------------------------------------------------------
# Idempotency, refusals, VERIFY, forgetting
# ---------------------------------------------------------------------------
def test_a_retried_action_is_applied_once(storage: LocalStorage, world: World) -> None:
    case = visit.resume(CLIENT, open_case(storage, world).case_id, _deps(storage, world), actor=ANALYST)
    q = question(case, "KEY_AMBIGUOUS")
    first = visit.answer(
        CLIENT, case.case_id, q.question_id, option(q, "customer_id"), _deps(storage, world),
        expected_version=case.version, actor=ANALYST, op_id="answer-1",
    )  # fmt: skip
    again = visit.answer(
        CLIENT, case.case_id, q.question_id, option(q, "account_no"), _deps(storage, world),
        expected_version=case.version, actor=ANALYST, op_id="answer-1",
    )  # fmt: skip
    assert again == first, "the retry with the same key changes nothing"
    with pytest.raises(CaseError) as stale:
        visit.answer(
            CLIENT, case.case_id, q.question_id, option(q, "account_no"), _deps(storage, world),
            expected_version=case.version, actor=ANALYST,
        )  # fmt: skip
    assert stale.value.code == "CASE_VERSION_CONFLICT"


REST_STATES = {VisitState.ASK, VisitState.APPROVE, VisitState.FOLLOW_UP}


def _answer_key(storage: LocalStorage, w: World, case: Case, op_id: str, *, stale: float = 30.0) -> Case:
    q = question(case, "KEY_AMBIGUOUS")
    return visit.answer(
        CLIENT, case.case_id, q.question_id, option(q, "customer_id"), _deps(storage, w, stale=stale),
        expected_version=case.version, actor=ANALYST, op_id=op_id,
    )  # fmt: skip


def test_a_retried_action_whose_process_died_before_the_visit_moved_on_ends_at_a_rest(
    tmp_path: Path, world: World
) -> None:
    """The action is written, then the process dies before the machine advances; the client retries with the
    same key. The retry applies nothing twice and the visit goes on to its next rest, as if nothing had died.
    """
    clean_storage = LocalStorage(tmp_path / "clean")
    clean = visit.resume(
        CLIENT, open_case(clean_storage, world).case_id, _deps(clean_storage, world), actor=ANALYST
    )
    clean = _answer_key(clean_storage, world, clean, "answer-1")

    root = tmp_path / "data"
    storage = LocalStorage(root)
    case = visit.resume(CLIENT, open_case(storage, world).case_id, _deps(storage, world), actor=ANALYST)
    with pytest.raises(_Killed):
        _answer_key(_Crashing(root, 1, after=True), world, case, "answer-1")
    died = _reload(storage, case)
    assert died.visit is not None and "answer-1" in died.visit.ops, "the action was written"
    assert died.state not in REST_STATES and died.visit.waiting_for is None, "and the visit did not move on"

    retried = _answer_key(LocalStorage(root), world, case, "answer-1", stale=0.0)
    assert retried.state in REST_STATES and retried.visit is not None and retried.visit.waiting_for
    assert retried == _reload(storage, retried)
    assert summary(retried) == summary(clean), "nothing lost or repeated"
    assert timeline_summary(storage, retried) == timeline_summary(clean_storage, clean)


def test_an_action_raced_by_another_writer_is_applied_and_never_answers_a_conflict(
    storage: LocalStorage, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another writer (a resume) moves the case on between the action's write and its advance: the action is
    applied, so the person is not told the case changed since they loaded it."""
    case = visit.resume(CLIENT, open_case(storage, world).case_id, _deps(storage, world), actor=ANALYST)
    real = visit._advance
    raced: list[int] = []

    def racing(run: Any, current: Case) -> Case:
        if not raced:  # the first advance after the action's write: another writer gets there first
            raced.append(current.version)
            other = visit.resume(CLIENT, current.case_id, _deps(storage, world), actor=ANALYST)
            assert other.version > current.version
        return real(run, current)

    monkeypatch.setattr(visit, "_advance", racing)
    answered = _answer_key(storage, world, case, "answer-1")
    assert raced, "the race happened"
    assert answered.state in REST_STATES and answered == _reload(storage, answered)
    q = next(q for q in answered.questions if q.code == "KEY_AMBIGUOUS")
    assert q.answer is not None and q.answered_by == ANALYST


def test_a_visit_resumed_in_converse_is_answered_by_the_ai_service(
    storage: LocalStorage, world: World
) -> None:
    """The process dies during the AI call (the case rests in CONVERSE with the message unanswered); the next
    call that resumes the visit carries a lazy CONVERSE port and the AI service answers the message."""
    case = _generic(storage, world)
    asked: list[str] = []

    def dying(case: Case, message: str, files: tuple[LoadedFile, ...]) -> visit.ConverseResult:
        raise _Killed

    def answering(case: Case, message: str, files: tuple[LoadedFile, ...]) -> visit.ConverseResult:
        asked.append(message)
        return visit.ConverseResult(reply="Here is what I found.", model_calls=1)

    with pytest.raises(_Killed):
        visit.chat(
            CLIENT, case.case_id, "What is in this file?", _deps(storage, world, converse=dying),
            expected_version=case.version, actor=ANALYST, op_id="chat-1",
        )  # fmt: skip
    died = _reload(storage, case)
    assert died.state is VisitState.CONVERSE and died.chat[-1].role == "user"

    built: list[int] = []

    def build() -> visit.ConversePort:
        built.append(1)
        return answering

    resumed = visit.resume(
        CLIENT, case.case_id, _deps(storage, world, converse=visit.lazy_converse(build)), actor=ANALYST
    )
    assert asked == ["What is in this file?"] and built == [1]
    assert [(c.role, c.text) for c in resumed.chat[-2:]] == [
        ("user", "What is in this file?"),
        ("agent", "Here is what I found."),
    ]
    assert resumed.state in REST_STATES
    chats = [e for e in CaseStore(storage).timeline(CLIENT, case.case_id) if e.kind is TimelineKind.CHAT]
    assert chats[-1].details["ai"] is True


def test_why_the_ai_did_not_answer_is_kept_in_the_reply_and_the_timeline(
    storage: LocalStorage, world: World
) -> None:
    case = _generic(storage, world)

    def down(case: Case, message: str, files: tuple[LoadedFile, ...]) -> visit.ConverseResult:
        return visit.ConverseResult(
            reply="The AI service is not answering right now.",
            blocked_by="unavailable",
            error_codes=("LLM_UNAVAILABLE", "LLM_KEY_EXPIRED"),
        )

    asked = visit.chat(
        CLIENT, case.case_id, "What is in this file?", _deps(storage, world, converse=down),
        expected_version=case.version, actor=ANALYST, op_id="chat-1",
    )  # fmt: skip
    entry = asked.chat[-1]
    assert (entry.blocked_by, entry.error_codes) == ("unavailable", ("LLM_UNAVAILABLE", "LLM_KEY_EXPIRED"))
    chats = [e for e in CaseStore(storage).timeline(CLIENT, case.case_id) if e.kind is TimelineKind.CHAT]
    assert chats[-1].details["blocked_by"] == "unavailable"
    assert chats[-1].details["error_codes"] == "LLM_UNAVAILABLE,LLM_KEY_EXPIRED"
    assert chats[-1].details["ai"] is True


def test_a_lazy_converse_port_connects_only_when_a_chat_turn_runs(
    storage: LocalStorage, world: World
) -> None:
    built: list[int] = []

    def build() -> visit.ConversePort | None:
        built.append(1)
        return None  # no AI service connected

    case = visit.resume(
        CLIENT, open_case(storage, world).case_id,
        _deps(storage, world, converse=visit.lazy_converse(build)), actor=ANALYST,
    )  # fmt: skip
    assert built == [], "a visit that does not chat never connects"
    case = visit.chat(
        CLIENT, case.case_id, "Hello", _deps(storage, world, converse=visit.lazy_converse(build)),
        expected_version=case.version, actor=ANALYST,
    )  # fmt: skip
    assert built == [1] and case.chat[-1].text == AI_NEEDED_REPLY
    chats = [e for e in CaseStore(storage).timeline(CLIENT, case.case_id) if e.kind is TimelineKind.CHAT]
    assert chats[-1].details["ai"] is False, "the timeline says no AI service answered"


def test_refusals_name_their_code(storage: LocalStorage, world: World) -> None:
    case = visit.resume(CLIENT, open_case(storage, world).case_id, _deps(storage, world), actor=ANALYST)
    calls: dict[str, Callable[[], Case]] = {
        "VISIT_NOT_WAITING": lambda: visit.approve(
            CLIENT, case.case_id, _deps(storage, world), expected_version=case.version, actor=ANALYST
        ),
        "VISIT_UNKNOWN_CHOICE": lambda: visit.answer(
            CLIENT,
            case.case_id,
            "q.nothing",
            "c0",
            _deps(storage, world),
            expected_version=case.version,
            actor=ANALYST,
        ),
    }
    for code, call in calls.items():
        with pytest.raises(CaseError) as refused:
            call()
        assert refused.value.code == code
    with pytest.raises(Exception) as unknown:
        visit.set_goal(
            CLIENT,
            case.case_id,
            "no-such-goal",
            _deps(storage, world),
            expected_version=case.version,
            actor=ANALYST,
        )
    assert getattr(unknown.value, "code", "") == "USE_CASE_NOT_FOUND"
    assert _reload(storage, case) == case, "a refusal writes nothing"


def test_a_case_with_no_files_waits_for_them(storage: LocalStorage, world: World) -> None:
    empty = CaseStore(storage, clock=world.clock).create(client_id=CLIENT, created_by=ANALYST)
    case = visit.resume(CLIENT, empty.case_id, _deps(storage, world), actor=ANALYST)
    assert case.state is VisitState.INTAKE and case.visit is not None and case.visit.waiting_for == "files"
    assert (
        visit.resume(CLIENT, empty.case_id, _deps(storage, world), actor=ANALYST) == case
    ), "resting writes nothing"
    case = visit.add_files(
        CLIENT, case.case_id, [vs.case_file(FIRST, world)], _deps(storage, world),
        expected_version=case.version, actor=ANALYST,
    )  # fmt: skip
    assert case.state is VisitState.ASK and case.visit is not None and case.visit.taken_in == (FIRST,)


def test_verify_sends_a_not_ready_case_back_to_treat_with_its_reason(
    storage: LocalStorage, world: World
) -> None:
    world.verdict = "not_ready"
    case = _goal(storage, world, _generic(storage, world), GOAL_A)
    case = _answer(storage, world, case, "OUTCOME_COLUMN", "converted")
    case = visit.approve(
        CLIENT, case.case_id, _deps(storage, world), expected_version=case.version, actor=ANALYST
    )
    assert case.state is VisitState.APPROVE, "back through TREAT and PREVIEW, waiting for the person again"
    events = CaseStore(storage).timeline(CLIENT, case.case_id)
    readiness = [e for e in events if e.kind is TimelineKind.READINESS]
    assert readiness and readiness[-1].details["verdict"] == "not_ready"
    assert not [e for e in events if e.kind is TimelineKind.HAND_OFF]
    states = _states(storage, case)
    assert states[states.index("verify") + 1] == "treat"


def test_forgetting_an_upload_forgets_it_in_the_visit(storage: LocalStorage, world: World) -> None:
    case = visit.resume(CLIENT, open_case(storage, world).case_id, _deps(storage, world), actor=ANALYST)
    assert case.visit is not None and case.visit.offers
    document, counts = forget_upload(case.model_dump(mode="json"), FIRST)
    assert counts["files"] == 1 and counts["questions"] == 1
    assert document["visit"]["taken_in"] == [] and document["visit"]["offers"] == {}
    Case.model_validate(document)


def test_old_cases_and_their_json_are_unchanged_by_the_new_fields(
    storage: LocalStorage, world: World
) -> None:
    """A case M231 wrote (no visit, no goal sections) reads and dumps exactly as before."""
    created = CaseStore(storage, clock=world.clock).create(client_id=CLIENT, created_by=ANALYST)
    dumped = json.loads(created.model_dump_json())
    assert "visit" not in dumped
    assert Case.model_validate(dumped) == created


# ---------------------------------------------------------------------------
# CONVERSE: the chat is the Doctor's own (`test_doctor_chat.py` runs it with a scripted model)
# ---------------------------------------------------------------------------
def test_a_port_that_returns_items_gets_them_pending_and_never_ticked_by_the_model(
    storage: LocalStorage, world: World
) -> None:
    """Whatever a CONVERSE port returns lands as the AI's own, `check` and pending; only the visit's guard run
    (`agent_apply`) may apply it, and a port that hands back an already ticked item changes nothing about that.
    """
    case = _generic(storage, world)
    ticked = visit._item(
        "recipe_step",
        {"upload_id": FIRST, "path": None, "value": None, "step": None},
        title="A fix the port ticked itself",
        upload_id=FIRST,
        confidence=AgentConfidence.SURE,
        evidence=(),
        now=world.clock(),
        ticked=True,
    )

    def port(case: Case, message: str, files: tuple[LoadedFile, ...]) -> visit.ConverseResult:
        return visit.ConverseResult(reply="Here you are.", items=(ticked,), model_calls=1)

    case = visit.chat(
        CLIENT, case.case_id, "Anything to fix?", _deps(storage, world, converse=port),
        expected_version=case.version, actor=ANALYST,
    )  # fmt: skip
    (item,) = (i for i in case.items if i.title == ticked.title)
    assert item.source is ProposalSource.AI and item.confidence is AgentConfidence.CHECK
    assert item.state is ProposalState.PENDING and item.decided_by is None, "not a kind the agent applies"
    assert case.chat[-1].item_ids == (item.item_id,)


def test_a_file_added_while_the_visit_waits_for_approval_is_read_before_anything_is_approved(
    storage: LocalStorage, world: World
) -> None:
    """The Data Doctor page adds a file with `POST /cases/{id}/files` (the plain store) and then resumes the visit:
    the plan the person approves must cover that file too, not rest on the first one."""
    case = _generic(storage, world)
    assert case.state is VisitState.APPROVE and case.visit is not None
    assert case.visit.taken_in == (FIRST,)
    added = CaseStore(storage, clock=world.clock).add_file(
        CLIENT,
        case.case_id,
        expected_version=case.version,
        actor=ANALYST,
        file=vs.case_file(vs.SECOND, world),
    )
    resumed = visit.resume(CLIENT, added.case_id, _deps(storage, world), actor=ANALYST)
    assert resumed.visit is not None and resumed.visit.taken_in == (FIRST, vs.SECOND)


# ---------------------------------------------------------------------------
# C1: a unique spend or score does not compete with the ID-named column for "names each row"
# ---------------------------------------------------------------------------


def test_an_id_named_column_narrows_the_key_candidates() -> None:
    from engine.doctor.tools_facts import id_named

    unique = [{"column": "spend"}, {"column": "customer_id"}, {"column": "score"}]
    assert id_named(unique) == [{"column": "customer_id"}]


def test_two_id_named_columns_still_compete_and_none_leaves_all() -> None:
    from engine.doctor.tools_facts import id_named

    two = [{"column": "customer_id"}, {"column": "account_no"}, {"column": "spend"}]
    assert [k["column"] for k in id_named(two)] == ["customer_id", "account_no"]
    plain = [{"column": "spend"}, {"column": "score"}]
    assert id_named(plain) == plain


# ---------------------------------------------------------------------------
# The history warnings are in the plan the person reads BEFORE approving
# ---------------------------------------------------------------------------
def _five_months(storage: LocalStorage) -> tuple[World, Case]:
    import pandas as pd

    config = load_use_case(GOAL_A)
    frame = vs.customers(seed=1)
    frame["snapshot_date"] = pd.date_range("2026-01-01", "2026-05-31", periods=len(frame)).strftime(
        "%Y-%m-%d"
    )
    w = World(
        files={FIRST: vs.loaded(FIRST, "customers.csv", frame, config)},
        offers=(Offer(config=config, stage="Acquire"),),
    )
    case = _goal(storage, w, _generic(storage, w), GOAL_A)
    return w, _answer(storage, w, case, "OUTCOME_COLUMN", "converted")


def test_a_short_history_is_a_flag_in_the_plan_at_approve_and_in_the_ledger(storage: LocalStorage) -> None:
    from engine.doctor.ledger import screen_ledger

    _, case = _five_months(storage)
    assert case.state is VisitState.APPROVE and case.visit is not None and case.visit.plan_check is not None
    flags = [f for f in case.visit.plan_check.findings if f.code.startswith("HISTORY_")]
    assert [(f.code, f.level, f.hard) for f in flags] == [("HISTORY_ONE_SEASON", "flag", False)]
    ledger = screen_ledger(case)
    assert ledger is not None and "HISTORY_ONE_SEASON" in json.dumps(ledger, default=str)


def test_a_window_longer_than_the_history_allows_is_a_too_short_flag(storage: LocalStorage) -> None:
    w, case = _five_months(storage)
    window = case.items[0].model_copy(
        update={
            "item_id": "window-90",
            "kind": "setting",
            "goal_id": GOAL_A,
            "state": ProposalState.ACCEPTED,
            "payload": {"path": "targeting.outcome_window_days", "value": 90},
        }
    )
    changed = case.model_copy(update={"items": (*case.items, window)})
    found = visit._history_findings(changed, GOAL_A, w.load(changed))
    assert {f.code for f in found} == {"HISTORY_TOO_SHORT", "HISTORY_ONE_SEASON"}


def test_the_history_check_reads_the_snapshot_a_future_reading_query_sets(storage: LocalStorage) -> None:
    w, case = _five_months(storage)
    window = case.items[0].model_copy(
        update={
            "item_id": "window-90",
            "kind": "setting",
            "goal_id": GOAL_A,
            "state": ProposalState.ACCEPTED,
            "payload": {"path": "targeting.outcome_window_days", "value": 90},
        }
    )
    step = {
        "order": 1,
        "kind": "sql_query",
        "column": "totals",
        "params": {
            "sql": "SELECT customer_id FROM this",
            "inputs": ["this"],
            "output_name": "totals",
            "description": "The outcome.",
            "reads_future": True,
            "as_of": "2026-01-15",
        },
    }
    query = case.items[0].model_copy(
        update={
            "item_id": "query-1",
            "kind": "step",
            "goal_id": GOAL_A,
            "upload_id": FIRST,
            "state": ProposalState.ACCEPTED,
            "payload": {"step": step},
        }
    )
    changed = case.model_copy(update={"items": (*case.items, window, query)})
    reading, _ = visit._history_reading(changed, GOAL_A, w.load(changed))
    assert reading is not None
    short = next(f for f in reading.findings if f.code == "HISTORY_TOO_SHORT")
    assert short.text.startswith("The training date leaves 14 days of history before it")


def test_a_yes_no_text_outcome_reaches_approve_with_no_step_that_cannot_run(storage: LocalStorage) -> None:
    """The visit used to offer (and pre-tick) a yes/no recoding of the outcome column, which the plan check then
    refused (`PLAN_STEP_CANNOT_RUN`). Once the column is the outcome, the Doctor takes such a step back itself.
    """
    import numpy as np

    from engine.doctor.case import CaseStore

    config = load_use_case(GOAL_A)
    frame = vs.customers(seed=3)
    frame["converted"] = np.random.default_rng(4).choice(["yes", "no"], size=len(frame))
    w = World(
        files={FIRST: vs.loaded(FIRST, "customers.csv", frame, config)},
        offers=(Offer(config=config, stage="Acquire"),),
    )
    case = _generic(storage, w)
    template = next(i for i in case.items if i.kind == "key")
    step = {"order": 1, "kind": "map_boolean", "column": "converted", "params": {}}
    recode = template.model_copy(
        update={
            "item_id": "recipe_step.base.converted",
            "kind": "recipe_step",
            "title": "Turn the yes / no values in 'converted' into 1 and 0",
            "payload": {"upload_id": FIRST, "step": step},
            "state": ProposalState.ACCEPTED,  # pre-ticked, as the rules did
            "decided_by": None,
        }
    )
    case = CaseStore(storage).update(
        CLIENT, case.case_id, expected_version=case.version, actor=ANALYST,
        change=lambda c: (c.model_copy(update={"items": (*c.items, recode)}), ()),
    )  # fmt: skip
    case = _goal(storage, w, case, GOAL_A)
    if any(q.code == "OUTCOME_COLUMN" and q.answer is None for q in case.questions):
        case = _answer(storage, w, case, "OUTCOME_COLUMN", "converted")
    taken = next(i for i in case.items if i.item_id == "recipe_step.base.converted")
    assert taken.state is ProposalState.REJECTED and "outcome column" in (taken.withdrawn_because or "")
    assert case.state is VisitState.APPROVE and case.visit is not None and case.visit.plan_check is not None
    assert "PLAN_STEP_CANNOT_RUN" not in {f.code for f in case.visit.plan_check.findings}
