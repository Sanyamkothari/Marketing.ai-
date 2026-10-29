"""The AI goes through the author: one turn per event, asked only when something is owed (P6b).

`VisitDeps.author` is the one port of the AI. The visit asks it for a person's message and for what else is owed
(`engine.doctor.author.owed`), gives it the chosen goal's settings and the fact functions' suggestions, applies what it
made through the one function that writes the plan, and remembers what it answered so the same event is not owed twice.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from engine.doctor import visit
from engine.doctor.agent_turn import TurnResult
from engine.doctor.author import Create, item_id_of
from engine.doctor.case import Case, CaseStore, TimelineKind
from engine.doctor.diagnose import LoadedFile
from engine.doctor.goal_settings import GoalSettings
from engine.doctor.protocol import AGENT
from engine.storage import LocalStorage
from tests.unit.doctor import visit_support as vs
from tests.unit.doctor.test_visit import _MEMORY, _Cached
from tests.unit.doctor.visit_support import ANALYST, CLIENT, GOAL_A, World, open_case, option, question


@pytest.fixture
def world() -> World:
    return vs.world()


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path / "data")


class _Spy:
    """A connected AI service that records every event it is asked about and answers with `result`."""

    def __init__(self, result: TurnResult | None = None) -> None:
        self.events: list[visit.TurnEvent] = []
        self.result = vs.turn_of("Here.", model_calls=1, finished=True) if result is None else result

    def __call__(
        self, case: Case, files: tuple[LoadedFile, ...], brief: Any, event: visit.TurnEvent
    ) -> TurnResult | None:
        self.events.append(event)
        return self.result


def _deps(storage: LocalStorage, world: World, spy: _Spy) -> visit.VisitDeps:
    return vs.deps_for(storage, world, registry=_Cached(visit.visit_registry(), _MEMORY), author=spy)


def _answered_key(storage: LocalStorage, world: World, spy: _Spy) -> Case:
    case = visit.resume(CLIENT, open_case(storage, world).case_id, _deps(storage, world, spy), actor=ANALYST)
    q = question(case, "KEY_AMBIGUOUS")
    return visit.answer(
        CLIENT, case.case_id, q.question_id, option(q, "customer_id"), _deps(storage, world, spy),
        expected_version=case.version, actor=ANALYST,
    )  # fmt: skip


def test_the_ai_is_not_asked_while_a_blocking_question_is_open_and_then_once_for_the_file(
    storage: LocalStorage, world: World
) -> None:
    spy = _Spy()
    case = visit.resume(CLIENT, open_case(storage, world).case_id, _deps(storage, world, spy), actor=ANALYST)
    assert spy.events == [], "no AI on a guess: the key is asked first"
    q = question(case, "KEY_AMBIGUOUS")
    case = visit.answer(
        CLIENT, case.case_id, q.question_id, option(q, "customer_id"), _deps(storage, world, spy),
        expected_version=case.version, actor=ANALYST,
    )  # fmt: skip
    (event,) = spy.events
    assert [o.reason for o in event.owed] == ["new_file"] and event.message is None
    assert case.chat[-1].text == "Here." and case.counters.model_calls == 1
    again = visit.resume(CLIENT, case.case_id, _deps(storage, world, spy), actor=ANALYST)
    assert len(spy.events) == 1 and again.version >= case.version, "what was served is not owed again"


def test_one_turn_answers_the_message_and_whatever_else_is_owed(storage: LocalStorage, world: World) -> None:
    spy = _Spy()
    case = visit.resume(CLIENT, open_case(storage, world).case_id, _deps(storage, world, spy), actor=ANALYST)
    case = visit.chat(
        CLIENT, case.case_id, "What is here?", _deps(storage, world, spy),
        expected_version=case.version, actor=ANALYST,
    )  # fmt: skip
    (event,) = spy.events
    assert event.message == "What is here?"
    assert {o.reason for o in event.owed} == {"message", "new_file"}, "the file is looked at in the same turn"
    assert [(c.role, c.text) for c in case.chat] == [("user", "What is here?"), ("agent", "Here.")]
    q = question(case, "KEY_AMBIGUOUS")
    visit.answer(
        CLIENT, case.case_id, q.question_id, option(q, "customer_id"), _deps(storage, world, spy),
        expected_version=case.version, actor=ANALYST,
    )  # fmt: skip
    assert len(spy.events) == 1, "answering the key does not pay for a review of a file it has seen"


def test_the_turn_is_given_the_chosen_goals_settings_and_only_suggestions_that_are_not_in_the_plan(
    storage: LocalStorage, world: World
) -> None:
    spy = _Spy()
    case = _answered_key(storage, world, spy)
    case = visit.set_goal(
        CLIENT, case.case_id, GOAL_A, _deps(storage, world, spy),
        expected_version=case.version, actor=ANALYST,
    )  # fmt: skip
    case = visit.chat(
        CLIENT, case.case_id, "What can I set?", _deps(storage, world, spy),
        expected_version=case.version, actor=ANALYST,
    )  # fmt: skip
    event = spy.events[-1]
    assert {o.reason for o in event.owed} >= {"message", "goal_chosen"}
    assert isinstance(event.goal_settings, GoalSettings) and event.goal_settings.goal_id == GOAL_A
    in_plan = {i.item_id for i in case.items}
    assert all(isinstance(s.action, Create) for s in event.suggestions)
    for s in event.suggestions:
        assert isinstance(s.action, Create)
        assert item_id_of(s.action.draft.kind, s.action.draft.payload, s.action.draft.goal_id) not in in_plan


def test_a_review_the_client_has_not_allowed_writes_nothing_and_is_owed_again(
    storage: LocalStorage, world: World
) -> None:
    spy = _Spy(TurnResult(reply="The client has not said yes to the AI.", ai=False, blocked_by="no_consent"))
    case = _answered_key(storage, world, spy)
    assert case.chat == () and case.counters.model_calls == 0
    assert "author.new_file" not in (case.visit.done if case.visit else {})
    spoken = visit.chat(
        CLIENT,
        case.case_id,
        "Hello",
        _deps(storage, world, spy),
        expected_version=case.version,
        actor=ANALYST,
    )
    assert (
        spoken.chat[-1].text == "The client has not said yes to the AI."
        and spoken.chat[-1].blocked_by == "no_consent"
    )
    chats = [e for e in CaseStore(storage).timeline(CLIENT, case.case_id) if e.kind is TimelineKind.CHAT]
    assert chats[-1].details["ai"] is False


def test_what_the_engine_refused_in_the_turn_is_kept_on_the_reply_for_the_screen(
    storage: LocalStorage, world: World
) -> None:
    from engine.agent.contracts import Refusal
    from engine.doctor.screen import screen_view

    refused = (
        Refusal("FEATURE_IS_ID", "CustomerID names the rows.", "customer_id", "Keep it out of the features."),
        Refusal("ITEM_NOT_FOUND", "There is no change c9.", "c9", "Use one of c1."),
    )
    spy = _Spy(vs.turn_of("I cannot add that.", model_calls=1, refused=refused))
    case = _answered_key(storage, world, spy)
    case = visit.chat(
        CLIENT, case.case_id, "Use the ID as a feature", _deps(storage, world, spy),
        expected_version=case.version, actor=ANALYST,
    )  # fmt: skip
    (kept,) = case.chat[-1].refused
    assert (kept.code, kept.value, kept.how_to_fix) == (
        "FEATURE_IS_ID",
        "customer_id",
        "Keep it out of the features.",
    )
    view = screen_view(storage, case, config_root=None)
    card = next(
        c
        for c in view.conversation
        if getattr(c, "type", "") == "message" and c.role == "agent" and c.refused
    )
    assert [r.code for r in card.refused] == [
        "FEATURE_IS_ID"
    ], "an id the AI got wrong is not shown to the person"


def test_a_turn_whose_first_call_was_not_answered_did_not_happen_and_is_said_once(
    storage: LocalStorage, world: World
) -> None:
    down = TurnResult(
        reply="The AI service is not answering right now.",
        blocked_by="unavailable",
        error_codes=("LLM_UNAVAILABLE", "LLM_KEY_EXPIRED"),
    )
    spy = _Spy(down)
    case = _answered_key(storage, world, spy)
    (told,) = case.chat
    assert told.blocked_by == "unavailable" and "author.new_file" not in (
        case.visit.done if case.visit else {}
    )
    q = next(q for q in case.questions if q.code == "KEY_AMBIGUOUS")
    again = visit.answer(
        CLIENT, case.case_id, q.question_id, option(q, "account_no"), _deps(storage, world, spy),
        expected_version=case.version, actor=ANALYST,
    )  # fmt: skip
    assert len(spy.events) == 2, "it was asked again"
    assert again.chat == (told,), "and failed the same way, which the chat had said"


def test_nothing_the_ai_makes_is_decided_by_the_agent(storage: LocalStorage, world: World) -> None:
    spy = _Spy(vs.turn_of("Done.", model_calls=1))
    case = _answered_key(storage, world, spy)
    assert not [i for i in case.items if i.decided_by == AGENT]
