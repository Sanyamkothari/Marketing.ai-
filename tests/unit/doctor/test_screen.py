"""What the Data Doctor screen shows beside a case (Plan O M247, DEC-1817; the design §10.3, §13).

Through real cases (`screen_support`): the verdict and its next action for every state the screen meets, the exact
sentence Approve records (sent back word for word, it approves), the changes that need their own yes with the role
and the sentence, the remembered answers with their dates and whether they were revoked, what the hand-off starts
with, and the client's cases by goal.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from engine.doctor import visit
from engine.doctor.case import Case, CaseError, CaseStore, VisitState
from engine.doctor.goal_names import goal_display_name
from engine.doctor.protocol import express_doubt, item_sentence
from engine.doctor.readiness_contracts import DataReadinessReport
from engine.doctor.screen import WARNINGS_NOTE, CaseScreen, build_screen, case_summaries, screen_view
from engine.doctor.standing import answer_id_for
from engine.doctor.visit import VerifyResult
from engine.storage import LocalStorage
from tests.unit.doctor import visit_support as vs
from tests.unit.doctor.screen_support import MEERA, scenario
from tests.unit.doctor.test_protocol_visit import ASHA, RAVI, _answer_key, _deps, _evidence, _found
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
    assert rows.role == "analyst" and rows.why == "it changes which customers the model learns from"
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


def test_a_change_after_the_check_keeps_the_last_failure_and_its_reason_on_screen(
    storage: LocalStorage, world: World
) -> None:
    screen = scenario("not_ready", storage, world)
    key = next(i for i in screen.case.items if i.kind == "as_of_column")
    unticked = visit.decide(
        CLIENT, screen.case.case_id, key.item_id, False, _deps(storage, world, _found()),
        expected_version=screen.case.version, actor=ANALYST,
    )  # fmt: skip
    verdict = screen_view(storage, unticked, config_root=None).verdict
    # The check no longer counts for the changed plan, but the person is still told why it failed, not "approve" bare.
    assert verdict.key == "not_ready" and verdict.reasons == ("as the test says",)


def _unticked(storage: LocalStorage, world: World, screen: CaseScreen) -> Case:
    key = next(i for i in screen.case.items if i.kind == "as_of_column")
    return visit.decide(
        CLIENT, screen.case.case_id, key.item_id, False, _deps(storage, world, _found()),
        expected_version=screen.case.version, actor=ANALYST,
    )  # fmt: skip


def _approve_again(storage: LocalStorage, world: World, case: Case, verify: Any) -> Case:
    deps = replace(_deps(storage, world, _found()), verify=verify)
    return visit.approve(
        CLIENT, case.case_id, deps, expected_version=case.version, actor=ANALYST,
        sentence=visit.approval_sentence(case),
    )  # fmt: skip


def test_a_new_approval_that_passes_clears_the_old_failure(storage: LocalStorage, world: World) -> None:
    screen = scenario("not_ready", storage, world)
    world.verdict = "ready"
    approved = _approve_again(storage, world, _unticked(storage, world, screen), world.verify)
    assert screen_view(storage, approved, config_root=None).verdict.key != "not_ready"


def test_a_failed_check_without_a_report_says_its_own_sentence_not_an_older_checks_reasons(
    storage: LocalStorage, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    screen = scenario("not_ready", storage, world)
    old = SimpleNamespace(
        requirements=(), probes=(SimpleNamespace(status="fail", fix="the OLD plan's reason", message="x"),),
        verdict_reason="old",
    )  # fmt: skip
    real = storage.read_model

    def read_model(key: str, model: Any) -> Any:
        return old if model is DataReadinessReport else real(key, model)

    monkeypatch.setattr(storage, "read_model", read_model)
    # The first check's own report still names its reason while nothing newer has been checked.
    assert screen_view(storage, screen.case, config_root=None).verdict.reasons == ("the OLD plan's reason",)

    def no_report(case: Case, brief: Any, files: Any) -> VerifyResult:
        return VerifyResult(
            verdict="not_ready", reason="VISIT_NO_OUTCOME: no file has the goal's outcome yet.", ref=None
        )

    again = _approve_again(storage, world, _unticked(storage, world, screen), no_report)
    verdict = screen_view(storage, again, config_root=None).verdict
    assert verdict.key == "not_ready" and verdict.reasons == ("no file has the goal's outcome yet.",)


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
    assert mine.upload_ids == tuple(
        dict.fromkeys(f.upload_id for f in ready.case.files)
    ), "the Data page groups by these"
    assert case_summaries(storage, CLIENT, goal_id="telco-churn") == ()
    assert case_summaries(storage, "c_nobody") == ()
    assert build_screen(storage, CLIENT, ready.case.case_id, config_root=None).case == ready.case


# ---------------------------------------------------------------------------
# M247 review: the goal's name, who may agree, answers remembered here, the plain note
# ---------------------------------------------------------------------------
def test_the_approval_sentence_names_the_goal_as_the_screens_do(storage: LocalStorage, world: World) -> None:
    """The sentence a person approves word for word names the goal ("Find customers likely to buy"), not its id."""
    screen = scenario("not_ready", storage, world)
    sentence = screen.view.approval_sentence
    assert sentence.startswith("Prepare 1 file with 4 changes for the goal “Find customers likely to buy”. ")
    assert (
        " The model learns from this: “converted” = 1 is a yes: " in sentence
    ), "the outcome, in plain words"
    assert sentence.endswith(" customers. Nothing else changes.")
    assert GOAL_A not in sentence
    assert sentence == visit.approval_sentence(screen.case), "the server shows and checks the same sentence"
    stale = sentence.replace("“Find customers likely to buy”", GOAL_A)
    with pytest.raises(CaseError) as changed:
        visit.approve(
            CLIENT, screen.case.case_id, _deps(storage, world, _found()), expected_version=screen.case.version,
            actor=ANALYST, sentence=stale,
        )  # fmt: skip
    assert (
        changed.value.code == "APPROVAL_SENTENCE_CHANGED"
    ), "a sentence read before the change is refused once"


def test_a_goal_without_a_display_name_is_named_by_its_configured_name() -> None:
    assert (
        goal_display_name("targeted-advertisement", "Targeted Advertisement")
        == "Find customers likely to buy"
    )
    assert goal_display_name("bank-term-deposit", "Term deposit sign-ups") == "Term deposit sign-ups"
    assert goal_display_name("bank-term-deposit", None) == "bank-term-deposit"


def test_who_may_give_the_own_yes_is_said_before_the_click(storage: LocalStorage, world: World) -> None:
    """The analyst who answered may give neither yes: the consent change needs the Approver role, and the rows
    change needs someone other than who answered (separation). `confirm` refuses exactly those."""
    own = scenario("approve_class_own", storage, world)
    by_kind = {
        next(i for i in own.case.items if i.item_id == a.item_id).kind: a for a in own.view.approve_class
    }
    consent, rows = by_kind["consent_column"], by_kind["recipe_step"]
    assert (consent.can_agree, consent.cannot_agree) == (False, "role")
    assert (rows.can_agree, rows.cannot_agree) == (False, "separation")
    with pytest.raises(CaseError) as refused:
        visit.confirm(
            CLIENT, own.case.case_id, rows.item_id, _deps(storage, world, _found()), principal=ASHA,
            sentence=rows.sentence, expected_version=own.case.version,
        )  # fmt: skip
    assert refused.value.code == "POLICY_ROLE_SEPARATION", "the screen said what the server does"
    meera = screen_view(storage, own.case, config_root=None, principal=MEERA)
    assert {a.item_id: a.can_agree for a in meera.approve_class} == {
        consent.item_id: False,
        rows.item_id: True,
    }
    ravi = screen_view(storage, own.case, config_root=None, principal=RAVI)
    assert next(a for a in ravi.approve_class if a.item_id == consent.item_id).can_agree
    unjudged = screen_view(storage, own.case, config_root=None)
    assert all(
        a.can_agree and a.cannot_agree is None for a in unjudged.approve_class
    ), "no reader: not judged"


def test_an_answer_remembered_here_says_so_on_its_question_and_can_be_revoked_here(
    storage: LocalStorage, world: World
) -> None:
    screen = scenario("approve", storage, world)
    boolean = next(q for q in screen.case.questions if q.code == "UNKNOWN_BOOLEAN")
    (mine,) = screen.view.remembered
    assert mine.question_id == boolean.question_id and not mine.revoked
    assert mine.answered_at.date().isoformat() == "2026-10-12"
    assert mine.answer_id == answer_id_for(CLIENT, boolean)
    revoked = visit.revoke_standing(
        CLIENT, screen.case.case_id, mine.answer_id, _deps(storage, world, _found()),
        expected_version=screen.case.version, actor=ANALYST,
    )  # fmt: skip
    after = screen_view(storage, revoked, config_root=None)
    assert [(r.question_id, r.revoked) for r in after.remembered] == [(boolean.question_id, True)]
    still = next(q for q in revoked.questions if q.question_id == boolean.question_id)
    assert still.answer == "c0", "revoking stops it standing next time; this case keeps the answer"


def test_ready_with_notes_always_carries_a_plain_note(storage: LocalStorage, world: World) -> None:
    screen = scenario("ready_with_warnings", storage, world)
    assert (
        screen.view.verdict.key == "ready_with_warnings" and screen.view.verdict.title == "Ready, with notes"
    )
    assert screen.view.verdict.note == WARNINGS_NOTE
    assert (
        scenario("ready", LocalStorage(storage.root.parent / "ready"), vs.world()).view.verdict.note is None
    )


def test_a_stored_reason_is_shown_without_its_code() -> None:
    from engine.doctor.screen import _plain

    said = "Not ready: X_CONSENT_VOCAB: 4,000 of 4,000 values are not a yes or no; A_B_C: Fix the dates."
    assert _plain(said) == "4,000 of 4,000 values are not a yes or no; Fix the dates."
    assert _plain("The file is empty.") == "The file is empty."
