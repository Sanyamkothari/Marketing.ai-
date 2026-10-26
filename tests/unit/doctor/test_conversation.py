"""The case as one conversation (`view.conversation`, `engine.doctor.conversation`).

Through the real cases of `screen_support`: the cards the chat-first screen draws, in reading order, for the rules
path (and, with the AI's items and chat entries put in by hand, the AI path - both leave their work in the same
places, so the cards are the same). The checks: the first look says what was looked at; a fix is a change card with
Undo; a question is chips with the safe "I don't know"; the goals (or the fit, with the goal chosen first); the one
ready card holds the exact sentence with the stops and the own-yes changes inside it; a chat reply's changes follow
the reply; a suggestion is offered only for an open question and records nothing.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import JsonValue

from engine.access.roles import Principal, Role
from engine.agent.contracts import AgentConfidence, ProposalSource, ProposalState
from engine.doctor.case import Case, CaseChatEntry, CaseItem, TimelineEvent, TimelineKind, payload_digest
from engine.doctor.conversation import (
    ChangeCard,
    FirstLookCard,
    FitCard,
    GoalsCard,
    MessageCard,
    QuestionCard,
    ReadyCard,
    RequestCard,
    build_conversation,
)
from engine.doctor.plan_check import plan_digest
from engine.doctor.screen import CaseScreen, screen_view
from engine.doctor.visit import active_section, plan_change_count
from engine.doctor.visit_contracts import Acknowledgement, PlanCheck, PlanFinding
from engine.storage import LocalStorage
from tests.unit.doctor import visit_support as vs
from tests.unit.doctor.screen_support import scenario
from tests.unit.doctor.visit_support import World


@pytest.fixture
def world() -> World:
    return vs.world()


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path / "data")


def _types(screen: CaseScreen) -> list[str]:
    return [card.type for card in screen.view.conversation]


def _of(screen: CaseScreen, kind: type) -> list[Any]:
    return [card for card in screen.view.conversation if isinstance(card, kind)]


def test_a_case_with_no_file_has_no_conversation_yet(storage: LocalStorage, world: World) -> None:
    assert scenario("empty", storage, world).view.conversation == ()


def test_the_first_look_comes_first_and_says_what_was_looked_at(storage: LocalStorage, world: World) -> None:
    screen = scenario("question", storage, world)
    first = screen.view.conversation[0]
    assert isinstance(first, FirstLookCard)
    assert first.rows == 1200 and [f.name for f in first.files] == ["customers_oct.csv"]
    assert first.fixes_count == 0 and first.questions_count == 2
    assert "I looked at 1 file: customers_oct.csv (1,200 rows)." in first.sentence
    assert "2 questions are only yours to answer." in first.sentence
    assert _types(screen).count("first_look") == 1


def test_questions_are_chips_and_i_dont_know_is_the_safe_choice(storage: LocalStorage, world: World) -> None:
    screen = scenario("question", storage, world)
    by_code = {q.code: q for q in screen.case.questions}
    cards = {c.question_id: c for c in _of(screen, QuestionCard)}
    key = cards[by_code["KEY_AMBIGUOUS"].question_id]
    assert key.blocking and key.answer is None
    assert [o.label for o in key.options] == [
        "customer_id",
        "account_no",
    ], "the question's own I don't know is apart"
    assert key.dont_know is not None and key.dont_know.id == "none" and key.dont_know.label == "I don't know"
    boolean = cards[by_code["UNKNOWN_BOOLEAN"].question_id]
    assert boolean.options[0].label == "Yes: haa is yes, nahi is no"
    assert boolean.options[0].rows_changed == 600 and boolean.options[0].effect
    assert boolean.why == "600 of 1,200 values are written that way."
    safe = boolean.dont_know
    assert safe is not None and safe.id == "c1" and safe.label == "I don't know"
    assert (
        "safe choice: Leave them as written" in safe.effect
    ), "I don't know takes the safe choice, never a guess"


def test_doubts_and_requests_are_cards_of_their_own(storage: LocalStorage, world: World) -> None:
    screen = scenario("question", storage, world)
    (doubt,) = [c for c in screen.view.conversation if c.type == "doubt"]
    assert doubt.because.startswith("“monthly_spend” is near 2,000")
    (request,) = _of(screen, RequestCard)
    assert [o.id for o in request.options] == ["will_add", "cannot"]
    assert "Win back needs it" in request.text


def test_with_no_goal_the_goals_are_cards_and_a_goal_chosen_first_is_a_fit(
    storage: LocalStorage, world: World, tmp_path: Path
) -> None:
    generic = scenario("question", storage, world)
    (goals,) = _of(generic, GoalsCard)
    assert goals.cards and goals.cards[0].best and goals.cards[0].goal_id
    assert goals.cards[0].customers == 1200 and goals.more_count == 0
    assert not _of(generic, FitCard)
    chosen = scenario("approve_class", LocalStorage(tmp_path / "goal"), vs.world())
    (fit,) = _of(chosen, FitCard)
    assert fit.goal_id == "targeted-advertisement" and fit.ok and fit.name == "Find customers likely to buy"
    assert not _of(chosen, GoalsCard), "a goal chosen first is not asked for again"


def test_a_fix_is_a_change_card_with_undo_and_who_made_it(storage: LocalStorage, world: World) -> None:
    screen = scenario("approve_class", storage, world)
    changes = _of(screen, ChangeCard)
    assert changes and all(c.state == "accepted" and c.undo for c in changes)
    assert {c.by for c in changes} == {
        "rules"
    }, "the fixes the rules ticked; the person's answers are on their questions"
    shown = {c.item_id for c in changes}
    inside = {a.item_id for a in screen.view.approve_class}
    assert shown.isdisjoint(inside), "a change that needs its own yes is inside the ready card, not a card"
    assert not any(i.kind == "data_check" for i in screen.case.items if i.item_id in shown)


def test_an_answer_is_told_once_on_its_question_and_is_not_a_fix(storage: LocalStorage, world: World) -> None:
    screen = scenario("approve_class", storage, world)
    answered = [q for q in screen.case.questions if q.answer is not None]
    assert answered, "the fixture has answered questions"
    made = {
        i.item_id
        for i in screen.case.items
        if any(i.item_id.startswith(f"{q.question_id}:") for q in answered)
    }
    assert made, "each answer made an item"
    assert made.isdisjoint(
        {c.item_id for c in _of(screen, ChangeCard)}
    ), "no second card repeating the answer"
    first = screen.view.conversation[0]
    assert isinstance(first, FirstLookCard)
    assert len(_of(screen, ChangeCard)) == 1
    assert (
        first.fixes_count == 4
    ), "the plan's one count: the card plus the three answers' fixes (told on their questions)"
    assert "I made 4 changes on a copy" in first.sentence
    # what an answer changed, measured by our code, is told on the question
    q = next(q for q in answered if q.code == "KEY_AMBIGUOUS")
    items = tuple(
        (
            i.model_copy(update={"payload": {**i.payload, "counts": "1,200 rows, none left out"}})
            if i.item_id.startswith(f"{q.question_id}:")
            else i
        )
        for i in screen.case.items
    )
    again = screen_view(storage, screen.case.model_copy(update={"items": items}), config_root=None)
    card = next(
        c for c in again.conversation if isinstance(c, QuestionCard) and c.question_id == q.question_id
    )
    assert card.answer == q.answer and card.counts == "1,200 rows, none left out"


def test_a_change_someone_else_ticked_is_not_called_the_readers_choice(
    storage: LocalStorage, world: World
) -> None:
    screen = scenario("approve_class", storage, world)
    item = next(i for i in screen.case.items if i.item_id.startswith("as_of_column"))
    mine = item.model_copy(update={"decided_by": "u-me"})
    case = screen.case.model_copy(
        update={"items": tuple(mine if i is item else i for i in screen.case.items)}
    )
    who = lambda p: next(  # noqa: E731
        c.by
        for c in screen_view(storage, case, config_root=None, principal=p).conversation
        if isinstance(c, ChangeCard) and c.item_id == item.item_id
    )
    me = Principal(user_id="u-me", username="me", roles=frozenset({Role.ANALYST}))
    other = Principal(user_id="u-other", username="other", roles=frozenset({Role.ANALYST}))
    assert who(me) == "you" and who(other) == "someone" and who(None) == "you"


def test_a_change_with_its_own_yes_is_a_change_once_agreed(storage: LocalStorage, world: World) -> None:
    screen = scenario("approve_class", storage, world)
    consent = next(a for a in screen.view.approve_class if a.item_id.startswith("consent_column"))
    assert consent.item_id not in {
        c.item_id for c in _of(screen, ChangeCard)
    }, "waiting for its yes: in the ready card"
    agreed = tuple(a.model_copy(update={"confirmed": a is consent}) for a in screen.view.approve_class)
    view = screen.view.model_copy(update={"approve_class": agreed})
    ticked = tuple(
        (
            i.model_copy(update={"state": ProposalState.ACCEPTED, "decided_by": "u-me"})
            if i.item_id == consent.item_id
            else i
        )
        for i in screen.case.items
    )
    cards = build_conversation(screen.case.model_copy(update={"items": ticked}), view, screen.timeline)
    (card,) = [c for c in cards if isinstance(c, ChangeCard) and c.item_id == consent.item_id]
    assert card.state == "accepted" and card.undo, "agreed: a change card with Undo, like any other"
    ready = cards[-1]
    assert isinstance(ready, ReadyCard) and consent.item_id not in {c.item_id for c in ready.confirmations}


def test_the_ready_card_holds_the_exact_sentence_and_the_own_yes_changes(
    storage: LocalStorage, world: World
) -> None:
    screen = scenario("approve_class", storage, world)
    ready = screen.view.conversation[-1]
    assert isinstance(ready, ReadyCard) and ready.stage == "approve"
    assert ready.sentence == screen.view.approval_sentence, "the sentence the server checks, word for word"
    assert {c.item_id for c in ready.confirmations} == {a.item_id for a in screen.view.approve_class}
    consent = next(c for c in ready.confirmations if c.role == "approver")
    assert consent.sentence == next(
        a.sentence for a in screen.view.approve_class if a.item_id == consent.item_id
    )
    assert not consent.required, "not ticked: left out unless agreed"
    assert [s.id for s in ready.speeds] == ["quick", "balanced", "thorough"]
    assert [s.id for s in ready.speeds if s.recommended] == ["balanced"]
    assert not ready.blocked and ready.stops == ()
    assert _types(screen).index("ready") == len(_types(screen)) - 1, "the ready card is last"


def test_a_ticked_change_this_reader_cannot_agree_to_blocks_the_approval_and_says_why(
    storage: LocalStorage, world: World
) -> None:
    screen = scenario("approve_class_own", storage, world)
    ready = screen.view.conversation[-1]
    assert isinstance(ready, ReadyCard)
    assert {c.cannot_agree for c in ready.confirmations} == {"role", "separation"}
    assert all(not c.can_agree for c in ready.confirmations)
    required = [c for c in ready.confirmations if c.required]
    assert [c.cannot_agree for c in required] == [
        "separation"
    ], "the ticked rows change needs someone else's yes"
    assert ready.blocked, "so this reader cannot approve; the unticked consent change is only left out"


def test_an_open_stop_is_a_checkbox_inside_the_ready_card(storage: LocalStorage, world: World) -> None:
    screen = scenario("approve", storage, world)
    case = screen.case
    items, _ = active_section(case)
    assert case.visit is not None
    stop = PlanFinding(
        key="k-stop",
        level="stop",
        code="STEP_LOSES_CUSTOMERS",
        text="Two thirds are lost.",
        detail="Most from step 2.",
    )
    check = PlanCheck(digest=plan_digest(case.files, items), findings=(stop,))
    counted = case.model_copy(update={"visit": case.visit.model_copy(update={"plan_check": check})})
    ready = screen_view(storage, counted, config_root=None).conversation[-1]
    assert isinstance(ready, ReadyCard)
    assert [(s.key, s.text, s.detail) for s in ready.stops] == [
        ("k-stop", "Two thirds are lost.", "Most from step 2.")
    ]
    said = case.visit.model_copy(update={"plan_check": check})
    ack = Acknowledgement(key="k-stop", by="u", at=case.updated_at)
    after = case.model_copy(update={"visit": said.model_copy(update={"acknowledged": (ack,)})})
    again = screen_view(storage, after, config_root=None).conversation[-1]
    assert (
        isinstance(again, ReadyCard) and again.stops == ()
    ), "a stop that was said yes to is not asked again"
    stale = counted.model_copy(update={"visit": counted.visit.model_copy(update={"plan_check": check.model_copy(update={"digest": "old"})})})  # type: ignore[union-attr]
    old = screen_view(storage, stale, config_root=None).conversation[-1]
    assert isinstance(old, ReadyCard) and old.stops == (), "the stops of another plan are never shown"


def test_once_approved_the_card_is_the_start_and_names_what_was_left_out(
    storage: LocalStorage, world: World
) -> None:
    screen = scenario("ready", storage, world)
    ready = screen.view.conversation[-1]
    assert isinstance(ready, ReadyCard) and ready.stage == "start"
    assert ready.sentence.startswith("Prepare 1 file with 4 changes")
    assert ready.left_out == ("“marketing_opt_in” says who agreed to be contacted.",)
    assert screen.view.start is not None


def test_not_ready_is_a_card_with_the_checks_own_reasons(storage: LocalStorage, world: World) -> None:
    screen = scenario("not_ready", storage, world)
    ready = screen.view.conversation[-1]
    assert isinstance(ready, ReadyCard) and ready.stage in {"blocked", "approve"}
    assert ready.notes == tuple(screen.view.verdict.reasons)


def _chat(case: Case, *entries: Any) -> Case:
    return case.model_copy(update={"chat": tuple(entries)})


def test_a_reply_brings_its_changes_after_it_and_a_suggestion_records_nothing(
    storage: LocalStorage, world: World
) -> None:
    screen = scenario("question", storage, world)
    case = screen.case
    q = next(q for q in case.questions if q.code == "UNKNOWN_BOOLEAN")
    at = case.updated_at + timedelta(minutes=5)
    item = CaseItem(
        item_id="ai.yes_no",
        kind="recipe_step",
        title="Read “haa” and “nahi” as yes and no",
        source=ProposalSource.AI,
        confidence=AgentConfidence.CHECK,
        payload={},
        payload_digest=payload_digest({}),
        proposed_at=at,
    )
    items = (*case.items, item)
    user = SimpleNamespace(
        role="user",
        text="haa means yes",
        at=at,
        by="u",
        sent=(),
        item_ids=(),
        withdrawn=(),
        tools=(),
        suggestion=None,
    )
    suggestion = SimpleNamespace(
        question_id=q.question_id, option_id="c0", label="Yes: haa is yes, nahi is no"
    )
    reply = SimpleNamespace(
        role="agent",
        text="I understood: haa is yes. Use this?",
        at=at + timedelta(seconds=2),
        by=None,
        sent=(),
        item_ids=(item.item_id,),
        withdrawn=(),
        tools=("get_profile", "propose_step"),
        suggestion=suggestion,
    )
    asked = _chat(case.model_copy(update={"items": items}), user, reply)
    view = screen_view(storage, asked, config_root=None, timeline=screen.timeline)
    cards = view.conversation
    messages = [c for c in cards if isinstance(c, MessageCard)]
    assert [m.role for m in messages] == ["user", "agent"]
    agent = messages[1]
    assert agent.tools == ("get_profile", "propose_step")
    assert agent.suggestion is not None and agent.suggestion.option_id == "c0" and not agent.suggestion.used
    assert agent.suggestion.question.startswith("In “marketing_opt_in”")
    assert (
        next(x for x in asked.questions if x.question_id == q.question_id).answer is None
    ), "nothing recorded"
    after = cards[cards.index(agent) + 1]
    assert isinstance(after, ChangeCard) and after.item_id == item.item_id and after.source == "ai"
    # a suggestion for a question that is not there, or an answer it does not offer, is dropped
    gone = SimpleNamespace(
        **{**reply.__dict__, "suggestion": SimpleNamespace(question_id="nope", option_id="c0", label="x")}
    )
    odd = SimpleNamespace(
        **{
            **reply.__dict__,
            "suggestion": SimpleNamespace(question_id=q.question_id, option_id="zz", label="x"),
        }
    )
    for entry in (gone, odd):
        shown = screen_view(storage, _chat(case, entry), config_root=None).conversation
        assert next(c for c in shown if isinstance(c, MessageCard)).suggestion is None


def test_a_question_asked_during_a_reply_is_drawn_after_the_reply(
    storage: LocalStorage, world: World
) -> None:
    screen = scenario("question", storage, world)
    case = screen.case
    q = next(q for q in case.questions if q.code == "UNKNOWN_BOOLEAN")
    at = case.updated_at + timedelta(minutes=5)
    base = {"by": None, "sent": (), "item_ids": (), "withdrawn": (), "tools": (), "suggestion": None}
    user = SimpleNamespace(role="user", text="check the yes/no column", at=at, **base)
    reply = SimpleNamespace(
        role="agent", text="I have a question for you.", at=at + timedelta(seconds=3), **base
    )
    asked = TimelineEvent(
        event_id="ev_" + "a" * 20,
        case_id=case.case_id,
        client_id=case.client_id,
        version=case.version,
        at=at + timedelta(seconds=1),
        actor="agent:data-doctor",
        kind=TimelineKind.QUESTION_ASKED,
        item_id=q.question_id,
    )
    cards = screen_view(
        storage,
        _chat(case, user, reply),
        config_root=None,
        timeline=(*[e for e in screen.timeline if e.item_id != q.question_id], asked),
    ).conversation
    where = {
        (c.role if isinstance(c, MessageCard) else getattr(c, "question_id", None)): i
        for i, c in enumerate(cards)
    }
    assert (
        where["user"] < where["agent"] < where[q.question_id]
    ), "the reply first, then the question it raised"


def test_the_ai_path_fills_the_same_cards(storage: LocalStorage, world: World) -> None:
    """An AI step the agent applied (decided by the agent), one waiting for a yes, one the person undid and one the
    agent took back: the cards say who made each and what can be done; a take-back is not a card."""
    screen = scenario("approve", storage, world)
    case = screen.case
    base = next(i for i in case.items if i.kind not in {"data_check"})
    now = case.updated_at

    def ai(item_id: str, state: ProposalState, by: str | None, title: str) -> Any:
        return base.model_copy(
            update={
                "item_id": item_id,
                "title": title,
                "source": ProposalSource.AI,
                "state": state,
                "decided_by": by,
                "decided_at": now if by else None,
                "payload": {"counts": "312 values → 1,200 · none unreadable"},
            }
        )

    items = (
        ai("ai.applied", ProposalState.ACCEPTED, "agent:data-doctor", "Read the dates day first"),
        ai("ai.waiting", ProposalState.PENDING, None, "Use only this year's orders"),
        ai("ai.undone", ProposalState.REJECTED, "u-asha", "Drop the test customers"),
        ai("ai.taken_back", ProposalState.REJECTED, "agent:data-doctor", "A step the agent withdrew"),
    )
    view = screen_view(storage, case.model_copy(update={"items": (*case.items, *items)}), config_root=None)
    cards = {c.item_id: c for c in view.conversation if isinstance(c, ChangeCard)}
    assert (
        cards["ai.applied"].by == "agent" and cards["ai.applied"].undo and cards["ai.applied"].source == "ai"
    )
    assert cards["ai.applied"].counts == "312 values → 1,200 · none unreadable"
    assert (
        cards["ai.waiting"].state == "pending"
        and not cards["ai.waiting"].undo
        and cards["ai.waiting"].by is None
    )
    assert cards["ai.undone"].state == "rejected", "undone by the person: shown, so it can be done again"
    assert "ai.taken_back" not in cards, "taken back by the agent: its message says so, it is not a card"
    first = view.conversation[0]
    assert isinstance(first, FirstLookCard) and first.fixes_count == plan_change_count(
        case.model_copy(update={"items": (*case.items, *items)})
    )
    assert first.fixes_count >= sum(1 for c in cards.values() if c.state == "accepted")


def test_the_conversation_is_built_from_the_case_alone(storage: LocalStorage, world: World) -> None:
    screen = scenario("approve_class", storage, world)
    again = build_conversation(
        screen.case, screen.view.model_copy(update={"conversation": ()}), screen.timeline
    )
    assert [c.model_dump() for c in again] == [c.model_dump() for c in screen.view.conversation]
    # a chat entry as the case stores it today (no item_ids / suggestion fields needed) is a plain message
    plain = _chat(screen.case, CaseChatEntry(role="user", text="why?", at=screen.case.updated_at))
    (message,) = [
        c for c in build_conversation(plain, screen.view, screen.timeline) if isinstance(c, MessageCard)
    ]
    assert message.item_ids == () and message.suggestion is None and message.text == "why?"
    assert message.blocked_by is None and message.error_codes == ()


def test_a_reply_the_ai_did_not_write_says_why_on_its_card(storage: LocalStorage, world: World) -> None:
    screen = scenario("approve_class", storage, world)
    down = CaseChatEntry(
        role="agent",
        text="The AI service is not answering right now.",
        at=screen.case.updated_at,
        blocked_by="unavailable",
        error_codes=("LLM_UNAVAILABLE", "LLM_KEY_EXPIRED"),
    )
    (message,) = [
        c
        for c in build_conversation(_chat(screen.case, down), screen.view, screen.timeline)
        if isinstance(c, MessageCard)
    ]
    assert message.blocked_by == "unavailable"
    assert message.error_codes == ("LLM_UNAVAILABLE", "LLM_KEY_EXPIRED")


def _applied_removal(case: Case, state: ProposalState = ProposalState.ACCEPTED) -> CaseItem:
    """What the AI's 'drop the test accounts' makes: a step that leaves rows out, so it needs the person's own yes
    at the approval, applied for them (decided by the agent) with one Undo."""
    step: dict[str, JsonValue] = {
        "kind": "remove_rows",
        "column": "customer_id",
        "operator": "starts_with",
        "values": ["999"],
    }
    return CaseItem(
        item_id="ai.test_accounts",
        kind="recipe_step",
        title="Leave out the test accounts",
        source=ProposalSource.AI,
        confidence=AgentConfidence.CHECK,
        state=state,
        upload_id=case.files[0].upload_id,
        payload={"step": step, "counts": "1,200 rows in, 1,180 out"},
        payload_digest=payload_digest({"step": step}),
        proposed_at=case.updated_at,
        decided_by="agent:data-doctor",
        decided_at=case.updated_at,
    )


def test_a_change_the_ai_applied_that_needs_its_own_yes_is_a_card_with_undo_while_a_question_is_open(
    storage: LocalStorage, world: World
) -> None:
    screen = scenario("question", storage, world)
    assert any(q.blocking and q.answer is None for q in screen.case.questions)
    case = screen.case.model_copy(update={"items": (*screen.case.items, _applied_removal(screen.case))})
    view = screen_view(storage, case, config_root=None)
    assert any(a.item_id == "ai.test_accounts" and not a.confirmed for a in view.approve_class)
    (card,) = [c for c in view.conversation if isinstance(c, ChangeCard) and c.item_id == "ai.test_accounts"]
    assert card.undo and card.by == "agent" and card.counts == "1,200 rows in, 1,180 out"
    first = view.conversation[0]
    assert isinstance(first, FirstLookCard) and first.fixes_count == 1, "counted in the checklist"
    assert not _of(view_screen(view), ReadyCard), "no approval card while a question is open"


def view_screen(view: Any) -> Any:
    return SimpleNamespace(view=view)


def test_the_ready_card_repeats_an_applied_change_as_its_checkbox_marked_shown(
    storage: LocalStorage, world: World
) -> None:
    screen = scenario("approve_class", storage, world)
    case = screen.case.model_copy(update={"items": (*screen.case.items, _applied_removal(screen.case))})
    view = screen_view(storage, case, config_root=None)
    ready = view.conversation[-1]
    assert isinstance(ready, ReadyCard)
    by_id = {c.item_id: c for c in ready.confirmations}
    assert by_id["ai.test_accounts"].shown and by_id["ai.test_accounts"].required
    consent = next(c for i, c in by_id.items() if i.startswith("consent_column"))
    assert not consent.shown, "waiting for its yes: only the ready card has it"
    assert "ai.test_accounts" in {c.item_id for c in view.conversation if isinstance(c, ChangeCard)}


def test_a_pending_change_that_needs_its_own_yes_is_still_only_in_the_ready_card(
    storage: LocalStorage, world: World
) -> None:
    screen = scenario("approve_class", storage, world)
    pending = _applied_removal(screen.case, ProposalState.PENDING).model_copy(
        update={"decided_by": None, "decided_at": None}
    )
    case = screen.case.model_copy(update={"items": (*screen.case.items, pending)})
    view = screen_view(storage, case, config_root=None)
    assert "ai.test_accounts" not in {c.item_id for c in view.conversation if isinstance(c, ChangeCard)}
    ready = view.conversation[-1]
    assert (
        isinstance(ready, ReadyCard)
        and not next(c for c in ready.confirmations if c.item_id == "ai.test_accounts").shown
    )


def test_approve_is_held_until_a_goal_is_chosen_and_while_the_last_check_found_something(
    storage: LocalStorage, world: World, tmp_path: Path
) -> None:
    no_goal = scenario("approve", storage, world)
    ready = no_goal.view.conversation[-1]
    assert isinstance(ready, ReadyCard) and ready.stage == "approve"
    assert ready.hold == "Choose what the campaign is for first."
    goal = scenario("approve_class", LocalStorage(tmp_path / "goal"), vs.world())
    ok = goal.view.conversation[-1]
    assert isinstance(ok, ReadyCard) and ok.stage == "approve" and ok.hold == ""
    failed = scenario("not_ready", LocalStorage(tmp_path / "nr"), vs.world())
    blocked = failed.view.conversation[-1]
    assert isinstance(blocked, ReadyCard) and blocked.stage == "approve"
    assert blocked.hold == "Fix what the last check found first."


def test_what_was_said_before_a_join_is_not_drawn_beside_the_table_it_made(
    storage: LocalStorage, world: World
) -> None:
    screen = scenario("approve_class", storage, world)
    case = screen.case
    (only,) = case.files
    gone = "up-before-join"
    old_step = next(i for i in case.items if i.kind == "recipe_step")
    old = old_step.model_copy(
        update={"item_id": "recipe_step.base.old", "upload_id": gone, "title": "Old fix"}
    )
    join = old_step.model_copy(
        update={
            "item_id": "build_dataset.base.j",
            "kind": "build_dataset",
            "upload_id": gone,
            "title": "Combine the files",
            "payload": {"upload_ids": [gone, "up-orders"]},
        }
    )
    outcome = next(i for i in case.items if i.kind == "outcome_column").model_copy(
        update={"item_id": "outcome_column.carried", "upload_id": gone}
    )
    carried = case.model_copy(update={"items": (*case.items, old, join, outcome)})
    cards = {
        c.item_id: c
        for c in screen_view(storage, carried, config_root=None).conversation
        if isinstance(c, ChangeCard)
    }
    assert "recipe_step.base.old" not in cards, "an earlier fix on a file the join gave way to"
    assert "build_dataset.base.j" in cards, "the join itself stays"
    assert "outcome_column.carried" in cards, "the person's decision on what the data means goes on the table"
    assert only.upload_id not in {gone}


def test_approving_with_no_goal_chosen_starts_nothing_and_goes_back_to_asking_for_a_goal(
    storage: LocalStorage, world: World
) -> None:
    """Approve is held in the screen until a goal is chosen; if the call is made anyway (an older screen), the data
    is approved for later, no campaign can start (there is no goal) and the goal cards are offered again."""
    from engine.doctor import visit
    from engine.doctor.screen import build_screen
    from tests.unit.doctor.test_protocol_visit import _deps, _found

    screen = scenario("approve", storage, world)
    case = screen.case
    assert case.state.value == "approve" and screen.view.goal_id is None
    after = visit.approve(
        vs.CLIENT,
        case.case_id,
        _deps(storage, world, _found()),
        expected_version=case.version,
        actor=vs.ANALYST,
        sentence=screen.view.approval_sentence,
    )
    assert after.visit is not None and after.visit.hand_off is not None
    assert after.visit.hand_off.target == "choose_goal" and after.visit.hand_off.run_id is None
    again = build_screen(storage, vs.CLIENT, after.case_id, config_root=None)
    assert again.view.start is None, "nothing to start"
    assert _of(again, GoalsCard) and not _of(again, ReadyCard), "the goals are offered again"


# ---------------------------------------------------------------------------
# Each thing is said once (v2e)
# ---------------------------------------------------------------------------
def test_one_count_of_changes_in_the_first_look_the_checklist_and_the_sentence(
    storage: LocalStorage, world: World
) -> None:
    import re

    screen = scenario("approve_class", storage, world)
    first = screen.view.conversation[0]
    ready = screen.view.conversation[-1]
    assert isinstance(first, FirstLookCard) and isinstance(ready, ReadyCard)
    said = re.search(r"with (\d+) changes?", ready.sentence)
    assert said is not None
    assert first.fixes_count == plan_change_count(screen.case) == int(said.group(1))
    assert f"I made {first.fixes_count} changes on a copy" in first.sentence
    # an undo moves all three together
    undone = tuple(
        (
            i.model_copy(update={"state": ProposalState.REJECTED, "decided_by": "u-me"})
            if i.item_id.startswith("as_of_column")
            else i
        )
        for i in screen.case.items
    )
    again = screen_view(storage, screen.case.model_copy(update={"items": undone}), config_root=None)
    first2, ready2 = again.conversation[0], again.conversation[-1]
    assert isinstance(first2, FirstLookCard) and isinstance(ready2, ReadyCard)
    assert first2.fixes_count == first.fixes_count - 1
    assert f"with {first.fixes_count - 1} changes" in ready2.sentence


def test_how_long_training_may_take_is_not_asked_in_the_chat_but_stays_in_the_case(
    storage: LocalStorage, world: World, tmp_path: Path
) -> None:
    screen = scenario("question", storage, world)
    case = screen.case
    base = next(q for q in case.questions if q.code == "UNKNOWN_BOOLEAN")
    budget = base.model_copy(
        update={
            "question_id": "q.training_budget.base.0001",
            "code": "TRAINING_BUDGET",
            "text": "How long may training take?",
            "answer": None,
        }
    )
    asked = case.model_copy(update={"questions": (*case.questions, budget)})
    view = screen_view(storage, asked, config_root=None)
    texts = [c.text for c in view.conversation if isinstance(c, QuestionCard)]
    assert "How long may training take?" not in texts, "the ready card's speeds are the one place for it"
    first = view.conversation[0]
    assert (
        isinstance(first, FirstLookCard) and first.questions_count == 2
    ), "and it is not counted as a question"
    assert any(q.code == "TRAINING_BUDGET" for q in asked.questions), "the plan view still has it"
    ready = scenario("approve_class", LocalStorage(tmp_path / "goal"), vs.world()).view.conversation[-1]
    assert isinstance(ready, ReadyCard) and [s.id for s in ready.speeds] == ["quick", "balanced", "thorough"]


def test_an_answer_the_person_gave_is_agreed_already_not_asked_again(
    storage: LocalStorage, world: World
) -> None:
    screen = scenario("approve_class", storage, world)
    ready = screen.view.conversation[-1]
    assert isinstance(ready, ReadyCard)
    by_id = {c.item_id: c for c in ready.confirmations}
    answered = next(c for i, c in by_id.items() if i.startswith("q.impossible_age"))
    assert answered.required and answered.answered and answered.can_agree
    consent = next(c for i, c in by_id.items() if i.startswith("consent_column"))
    assert not consent.answered and not consent.required, "a left-out change is still the person's to include"


def _reply_with_suggestion(
    case: Case, q: Any, at: Any, text: str = "I understood: haa is yes. Use this?"
) -> Any:
    return SimpleNamespace(
        role="agent",
        text=text,
        at=at,
        by=None,
        sent=(),
        item_ids=(),
        withdrawn=(),
        tools=(),
        suggestion=SimpleNamespace(
            question_id=q.question_id, option_id="c0", label="Yes: haa is yes, nahi is no"
        ),
    )


def test_a_suggested_answer_is_offered_once_in_its_reply_and_the_question_card_waits(
    storage: LocalStorage, world: World
) -> None:
    screen = scenario("question", storage, world)
    case = screen.case
    q = next(q for q in case.questions if q.code == "UNKNOWN_BOOLEAN")
    at = case.updated_at + timedelta(minutes=5)
    reply = _reply_with_suggestion(case, q, at)
    cards = screen_view(storage, _chat(case, reply), config_root=None).conversation
    assert q.question_id not in {
        c.question_id for c in cards if isinstance(c, QuestionCard)
    }, "not a third time"
    (message,) = [c for c in cards if isinstance(c, MessageCard)]
    assert message.suggestion is not None and not message.suggestion.used
    assert [o.id for o in message.suggestion.others] == ["c1", "c1"], "the other answers stay one tap away"
    assert message.suggestion.others[-1].label == "I don't know"
    first = cards[0]
    assert isinstance(first, FirstLookCard) and first.questions_count == 2, "still an open question"
    # answered: the question card is back, with its answer, and the reply has nothing left to offer
    answered = case.model_copy(
        update={
            "questions": tuple(
                x.model_copy(update={"answer": "c0"}) if x.question_id == q.question_id else x
                for x in case.questions
            )
        }
    )
    after = screen_view(storage, _chat(answered, reply), config_root=None).conversation
    card = next(c for c in after if isinstance(c, QuestionCard) and c.question_id == q.question_id)
    assert card.answer == "c0"
    (done,) = [c for c in after if isinstance(c, MessageCard)]
    assert done.suggestion is not None and done.suggestion.used and done.suggestion.others == ()


def test_only_the_newest_suggestion_for_a_question_offers_the_other_answers(
    storage: LocalStorage, world: World
) -> None:
    screen = scenario("question", storage, world)
    case = screen.case
    q = next(q for q in case.questions if q.code == "UNKNOWN_BOOLEAN")
    at = case.updated_at + timedelta(minutes=5)
    old = _reply_with_suggestion(case, q, at)
    new = _reply_with_suggestion(case, q, at + timedelta(minutes=1), "Changed my mind: haa is yes.")
    cards = screen_view(storage, _chat(case, old, new), config_root=None).conversation
    first, second = [c for c in cards if isinstance(c, MessageCard)]
    assert first.suggestion is not None and first.suggestion.others == ()
    assert second.suggestion is not None and second.suggestion.others


def test_a_later_reply_that_suggests_another_question_first_still_supersedes_the_older_suggestion(
    storage: LocalStorage, world: World
) -> None:
    screen = scenario("question", storage, world)
    case = screen.case
    open_qs = [q for q in case.questions if q.answer is None and len(q.options) >= 2]
    first_q = next(q for q in open_qs if q.code == "UNKNOWN_BOOLEAN")
    other_q = next(q for q in open_qs if q is not first_q)
    at = case.updated_at + timedelta(minutes=5)
    old = _reply_with_suggestion(case, first_q, at)
    new = _reply_with_suggestion(case, other_q, at + timedelta(minutes=1), "Two answers.")
    new.suggestion = SimpleNamespace(
        question_id=other_q.question_id, option_id=other_q.options[0].option_id, label="Yes"
    )
    new.suggestions = (
        new.suggestion,
        SimpleNamespace(question_id=first_q.question_id, option_id="c1", label="No"),
    )
    cards = screen_view(storage, _chat(case, old, new), config_root=None).conversation
    older, newer = [c for c in cards if isinstance(c, MessageCard)]
    assert older.suggestion is not None and older.suggestion.others == (), "superseded: not offered as live"
    assert newer.suggestion is not None and newer.suggestion.question_id == other_q.question_id
    asked = {c.question_id for c in cards if isinstance(c, QuestionCard)}
    assert first_q.question_id in asked, "its question card is back: no reply shows its live answer"
    assert other_q.question_id not in asked
