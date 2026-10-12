"""What the Data Doctor screen shows beside a case (Plan O M247, DEC-1817; the design §10.3, §13).

Through real cases (`screen_support`): the verdict and its next action for every state the screen meets, the exact
sentence Approve records (sent back word for word, it approves), the changes that need their own yes with the role
and the sentence, the remembered answers with their dates and whether they were revoked, what the hand-off starts
with, and the client's cases by goal.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.doctor import visit
from engine.doctor.case import CaseStore, VisitState
from engine.doctor.protocol import express_doubt, item_sentence
from engine.doctor.screen import build_screen, case_summaries, screen_view
from engine.storage import LocalStorage
from tests.unit.doctor import visit_support as vs
from tests.unit.doctor.screen_support import MEERA, scenario
from tests.unit.doctor.test_protocol_visit import _answer_key, _deps, _evidence, _found
from tests.unit.doctor.visit_support import ANALYST, CLIENT, GOAL_A, World


@pytest.fixture
def world() -> World:
    return vs.world()


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path / "data")


def test_a_case_without_a_file_waits_for_one(storage: LocalStorage, world: World) -> None:
    screen = scenario("empty", storage, world)
    assert screen.view.verdict.key == "needs_files"
    assert screen.view.verdict.next == "Add the file of your customers to start."
    assert screen.view.mode == "generic" and screen.view.goal_id is None
    assert [e.kind.value for e in screen.timeline] == ["case.created"]


def test_an_open_blocking_question_is_needs_answers(storage: LocalStorage, world: World) -> None:
    screen = scenario("question", storage, world)
    assert screen.case.state is VisitState.ASK
    assert screen.view.verdict.key == "needs_answers"
    assert screen.view.verdict.next == "Answer 1 question below to go on."
    assert screen.view.approval_sentence is None, "nothing to approve while a question blocks"
    codes = {q.code for q in screen.case.questions}
    assert {"KEY_AMBIGUOUS", "UNKNOWN_BOOLEAN", "DATA_REQUESTED"} <= codes and screen.case.doubts


def test_the_sentence_shown_is_the_one_approve_records(storage: LocalStorage, world: World) -> None:
    screen = scenario("approve", storage, world)
    sentence = screen.view.approval_sentence
    assert screen.view.verdict.key == "needs_approval" and sentence == visit.approval_sentence(screen.case)
    approved = visit.approve(
        CLIENT, screen.case.case_id, _deps(storage, world, _found()), expected_version=screen.case.version,
        actor=ANALYST, sentence=sentence,
    )  # fmt: skip
    assert approved.approvals[-1].text_shown == sentence


def test_approve_class_items_carry_their_role_sentence_and_whether_another_person_must_agree(
    storage: LocalStorage, world: World
) -> None:
    screen = scenario("approve_class", storage, world)
    by_kind = {
        next(i for i in screen.case.items if i.item_id == a.item_id).kind: a
        for a in screen.view.approve_class
    }
    consent, rows = by_kind["consent_column"], by_kind["recipe_step"]
    assert consent.role == "approver" and consent.why == "it decides who may be contacted"
    assert rows.role == "analyst" and rows.why == "it chooses which customers are learnt from"
    item = next(i for i in screen.case.items if i.item_id == rows.item_id)
    assert rows.sentence == item_sentence(item.title, item.payload)
    assert not rows.confirmed and rows.separate, "targeted advertising needs a second person"
    assert screen.view.verdict.key == "needs_approval"
    assert screen.view.verdict.next == "1 change needs your own yes before you approve."
    confirmed = visit.confirm(
        CLIENT, screen.case.case_id, rows.item_id, _deps(storage, world, _found()), principal=MEERA,
        sentence=rows.sentence, expected_version=screen.case.version,
    )  # fmt: skip
    after = screen_view(storage, confirmed, config_root=None)
    assert next(a for a in after.approve_class if a.item_id == rows.item_id).confirmed
    assert after.verdict.key == "needs_approval" and after.verdict.title == "Ready for your approval"


def test_ready_says_what_the_hand_off_starts_with(storage: LocalStorage, world: World) -> None:
    screen = scenario("ready", storage, world)
    assert screen.case.state is VisitState.FOLLOW_UP
    assert screen.view.verdict.key == "ready" and screen.view.mode == "goal" and screen.view.goal_id == GOAL_A
    start = screen.view.start
    assert start is not None and start.target == "runs" and start.goal_id == GOAL_A
    assert (start.upload_id, start.primary_key, start.outcome) == (vs.FIRST, "customer_id", "converted")


def test_not_ready_is_said_and_starts_nothing(storage: LocalStorage, world: World) -> None:
    screen = scenario("not_ready", storage, world)
    assert screen.view.verdict.key == "not_ready" and screen.view.start is None


def test_a_change_after_the_check_takes_the_verdict_back_to_the_visit(
    storage: LocalStorage, world: World
) -> None:
    screen = scenario("not_ready", storage, world)
    key = next(i for i in screen.case.items if i.kind == "as_of_column")
    unticked = visit.decide(
        CLIENT, screen.case.case_id, key.item_id, False, _deps(storage, world, _found()),
        expected_version=screen.case.version, actor=ANALYST,
    )  # fmt: skip
    assert screen_view(storage, unticked, config_root=None).verdict.key != "not_ready"


def test_a_remembered_answer_shows_its_date_and_whether_it_was_revoked(
    storage: LocalStorage, world: World, tmp_path: Path
) -> None:
    (used,) = scenario("standing", storage, world).view.standing
    assert used.option_label == "Yes: haa is yes, nahi is no" and used.column == "marketing_opt_in"
    assert used.answered_at.date().isoformat() == "2026-10-12" and not used.revoked
    (gone,) = scenario("revoked", LocalStorage(tmp_path / "other"), vs.world()).view.standing
    assert gone.answer_id == used.answer_id and gone.revoked


def test_generic_mode_done_asks_for_a_goal(storage: LocalStorage, world: World) -> None:
    deps = _deps(storage, world, _found())
    case = visit.resume(CLIENT, vs.open_case(storage, world).case_id, deps, actor=ANALYST)
    case = _answer_key(storage, world, case)
    case = visit.approve(CLIENT, case.case_id, deps, expected_version=case.version, actor=ANALYST)
    view = screen_view(storage, case, config_root=None)
    assert case.visit is not None and case.visit.hand_off is not None
    assert view.verdict.key == "choose_goal" and view.start is None
    assert view.missing_for == case.visit.missing_for


def test_a_stopped_visit_is_not_ready_with_its_code_behind_the_technical_switch(
    storage: LocalStorage, world: World
) -> None:
    result = _evidence({"rows": 312, "near": 2, "mean": 1_880})
    grounded = express_doubt(
        "UNIT_MISMATCH",
        because="“monthly_spend” is near 2 for 312 rows.",
        would_do="leave them as written.",
        if_not="the average would change from 1,880.",
        evidence={result.evidence_id: result.output},
        upload_id=vs.FIRST,
    )
    doubt = grounded.model_copy(update={"if_not": "the average rises from 1,880 to 2,450."})
    deps = _deps(storage, world, _found(doubts=(doubt,), evidence=(result,)))
    case = visit.resume(CLIENT, vs.open_case(storage, world).case_id, deps, actor=ANALYST)
    assert case.visit is not None and case.visit.stopped is not None
    view = screen_view(storage, case, config_root=None)
    assert view.verdict.key == "not_ready" and view.verdict.technical == "DOUBT_UNGROUNDED"


def test_the_clients_cases_newest_first_and_by_goal(storage: LocalStorage, world: World) -> None:
    ready = scenario("ready", storage, world)
    other = CaseStore(storage, clock=world.clock).create(client_id=CLIENT, created_by=ANALYST, title="Spare")
    every = case_summaries(storage, CLIENT)
    assert [c.case_id for c in every] == [other.case_id, ready.case.case_id]
    (mine,) = case_summaries(storage, CLIENT, goal_id=GOAL_A)
    assert mine.case_id == ready.case.case_id and mine.files == ("customers_oct.csv",)
    assert case_summaries(storage, CLIENT, goal_id="telco-churn") == ()
    assert case_summaries(storage, "c_nobody") == ()
    assert build_screen(storage, CLIENT, ready.case.case_id, config_root=None).case == ready.case
