"""The one approval screen (`view.review`, `engine.doctor.review`), through real cases (`screen_support`)."""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.agent.contracts import ProposalState
from engine.doctor.case import Case, CaseItem
from engine.doctor.review import ReviewApprove, build_review
from engine.doctor.screen import CaseScreen
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


def _with(screen: CaseScreen, items: list[CaseItem]) -> Case:
    return screen.case.model_copy(update={"items": tuple(items)})


def test_an_open_question_is_listed_with_its_options_and_holds_approve(
    storage: LocalStorage, world: World
) -> None:
    screen = scenario("question", storage, world)
    review = screen.view.review
    assert review is not None
    open_ids = {d.question_id for d in review.decisions if not d.answered}
    assert open_ids, "the key question is waiting"
    assert all(d.options for d in review.decisions if d.question_id in open_ids)
    assert review.approve.stage == "waiting" and not review.approve.enabled
    assert any("not answered" in w for w in review.approve.waiting)
    assert not any(
        d.question_id and "REQUEST" in d.title for d in review.decisions
    ), "a request is not a decision"


def test_without_a_goal_approve_waits_for_one(storage: LocalStorage, world: World) -> None:
    review = scenario("approve", storage, world).view.review
    assert review is not None
    assert review.approve.stage == "approve" and not review.approve.enabled
    assert review.approve.waiting == ("Choose what the campaign is for.",)


def test_everything_answered_enables_approve_with_the_exact_sentence(
    storage: LocalStorage, world: World
) -> None:
    screen = scenario("approve_class", storage, world)
    review = screen.view.review
    assert review is not None
    assert review.approve.stage == "approve"
    assert review.approve.sentence == screen.view.approval_sentence
    assert review.approve.enabled and review.approve.waiting == ()
    assert all(d.answered for d in review.decisions if d.question_id)
    answered = [d for d in review.decisions if d.question_id and d.decided_by == "person"]
    assert answered and not any(d.needs_yes for d in answered), "the person's own answer needs no second yes"


def test_the_plan_is_one_line_per_change(storage: LocalStorage, world: World) -> None:
    screen = scenario("approve", storage, world)
    review = screen.view.review
    assert review is not None
    from engine.doctor.visit import CARRIED_KINDS, plan_change_items

    accepted = [i for i in screen.case.items if i.state is ProposalState.ACCEPTED]
    changes = [i.item_id for i in plan_change_items(screen.case) if i.kind not in CARRIED_KINDS]
    assert changes, "the scenario's plan has changes"
    assert [line.item_id for line in review.plan] == changes
    assert set(changes) <= {i.item_id for i in accepted}
    assert all(line.text for line in review.plan)
    by_id = {i.item_id: i for i in screen.case.items}
    assert all(line.proposed_by == by_id[line.item_id].proposed_by for line in review.plan)
    assert {line.proposed_by for line in review.plan} <= {"default", "ai", "person"}


def test_a_change_needing_its_own_yes_is_an_agreement_with_who_may_give_it(
    storage: LocalStorage, world: World
) -> None:
    screen = scenario("approve_class_own", storage, world)
    review = screen.view.review
    assert review is not None
    assert review.agreements
    by_item = {a.item_id: a for a in review.agreements}
    assert by_item == {a.item_id: by_item[a.item_id] for a in screen.view.approve_class if not a.confirmed}
    refused = [a for a in review.agreements if not a.can_agree]
    assert refused and all(a.cannot_agree in {"role", "separation"} for a in refused)
    assert any(a.required for a in refused), "the scenario has a required change this person cannot agree"
    assert not review.approve.enabled and "needs someone else's own yes" in " ".join(review.approve.waiting)


def test_a_rule_assumed_decision_needs_the_persons_yes(storage: LocalStorage, world: World) -> None:
    screen = scenario("assumed", storage, world)
    review = screen.view.review
    assert review is not None
    assumed = [d for d in review.decisions if d.decided_by == "rules"]
    assert assumed and all(d.needs_yes and d.answered and d.current for d in assumed)
    assert all(d.question_id for d in assumed), "it can be changed by answering its question"


def test_a_remembered_answer_is_marked_as_earlier(storage: LocalStorage, world: World) -> None:
    screen = scenario("standing", storage, world)
    review = screen.view.review
    assert review is not None
    earlier = [d for d in review.decisions if d.decided_by == "earlier"]
    assert earlier and all(d.needs_yes for d in earlier)


def test_a_hard_stop_is_distinct_and_has_a_fix_and_no_yes(
    storage: LocalStorage, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    screen = scenario("approve", storage, world)
    from engine.doctor import ledger as ledger_mod

    hard = ledger_mod.Finding(
        key="a" * 16,
        code="CONSENT_MISSING",
        subject="plan",
        level="hard",
        source="guard",
        text="Nobody agreed to be contacted in 40 rows.",
        resolutions=ledger_mod._resolutions(hard=True, item_id=None),
    )
    stop = ledger_mod.Finding(
        key="b" * 16,
        code="ROWS_LOST",
        subject="plan",
        level="stop",
        source="guard",
        text="The plan leaves out 30% of the rows.",
        resolutions=ledger_mod._resolutions(hard=False, item_id=None),
    )
    monkeypatch.setattr("engine.doctor.review.findings_of", lambda case: (hard, stop))
    review = build_review(screen.case, screen.view)
    first, second = review.problems
    assert first.hard and not first.acknowledgeable and first.fix == "Change the plan or the file"
    assert not second.hard and second.acknowledgeable
    assert not review.approve.enabled and "1 rule is not met." in review.approve.waiting


def test_a_stop_nobody_can_acknowledge_holds_approve_and_offers_retry(
    storage: LocalStorage, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    screen = scenario("approve", storage, world)
    from engine.doctor import ledger as ledger_mod

    stopped = ledger_mod.Finding(
        key="c" * 16,
        code="VISIT_STOPPED",
        subject="visit",
        level="stop",
        source="visit",
        text="The last check could not finish.",
        resolutions=(
            ledger_mod.Resolution(kind="retry", label="Try again"),
            ledger_mod.Resolution(kind="fix", label="Add a different file or change the plan"),
        ),
    )
    monkeypatch.setattr("engine.doctor.review.findings_of", lambda case: (stopped,))
    review = build_review(screen.case, screen.view)
    (problem,) = review.problems
    assert not problem.hard and not problem.acknowledgeable
    assert "Try again" in problem.fix
    assert not review.approve.enabled and "1 rule is not met." in review.approve.waiting


def test_a_pending_swap_shows_old_beside_new_and_holds_approve(storage: LocalStorage, world: World) -> None:
    screen = scenario("approve", storage, world)
    old = next(i for i in screen.case.items if i.state is ProposalState.ACCEPTED)
    new = old.model_copy(
        update={
            "item_id": "swap-1",
            "title": "A better version of the step",
            "state": ProposalState.PENDING,
            "replaces": old.item_id,
            "decided_by": None,
        }
    )
    case = _with(screen, [*screen.case.items, new])
    review = build_review(case, screen.view)
    (swap,) = review.swaps
    assert (swap.old_item_id, swap.new_item_id, swap.state) == (old.item_id, "swap-1", "pending")
    assert swap.old_title == (old.title or old.kind) and swap.new_title == "A better version of the step"
    assert not review.approve.enabled
    assert any("old and the new" in w for w in review.approve.waiting)


def test_a_typed_answer_carries_its_value_kind(storage: LocalStorage, world: World) -> None:
    from engine.doctor.case import CaseOption, CaseQuestion

    screen = scenario("question", storage, world)
    typed = CaseQuestion(
        question_id="q.typed_window.base",
        code="Q_TYPED",
        text="How many days should the outcome window be?",
        options=(
            CaseOption(option_id="days", label="This many days", value_kind="number"),
            CaseOption(option_id="when", label="Until this date", value_kind="date"),
        ),
    )
    case = screen.case.model_copy(update={"questions": (*screen.case.questions, typed)})
    review = build_review(case, screen.view)
    shown = next(d for d in review.decisions if d.question_id == "q.typed_window.base")
    assert [(o.id, o.value_kind) for o in shown.options] == [("days", "number"), ("when", "date")]
    assert shown.kind == "date"


def test_review_is_served_in_the_screen_payload(storage: LocalStorage, world: World) -> None:
    screen = scenario("approve", storage, world)
    body = screen.model_dump(mode="json")
    assert set(body["view"]["review"]) == {"plan", "decisions", "problems", "agreements", "swaps", "approve"}
    assert ReviewApprove.model_validate(body["view"]["review"]["approve"]).stage == "approve"


def test_a_non_blocking_open_question_holds_neither_approve(storage: LocalStorage, world: World) -> None:
    from engine.doctor.case import CaseOption, CaseQuestion
    from engine.doctor.conversation import _hold, approval_holds

    screen = scenario("approve_class", storage, world)
    aside = CaseQuestion(
        question_id="q.aside.base",
        code="Q_ASIDE",
        text="Should the notes column be kept for later?",
        options=(
            CaseOption(option_id="keep", label="Keep it"),
            CaseOption(option_id="drop", label="Drop it"),
        ),
        blocking=False,
    )
    case = screen.case.model_copy(update={"questions": (*screen.case.questions, aside)})
    review = build_review(case, screen.view)
    assert any(d.question_id == "q.aside.base" and not d.answered for d in review.decisions)
    assert approval_holds(case, screen.view) == () and _hold(case, screen.view) == ""
    assert review.approve.enabled and review.approve.waiting == ()


def test_a_blocking_open_question_holds_both(storage: LocalStorage, world: World) -> None:
    from engine.doctor.conversation import _hold, approval_holds

    screen = scenario("question", storage, world)
    review = screen.view.review
    assert review is not None
    assert "questions" in approval_holds(screen.case, screen.view) and _hold(screen.case, screen.view)
    assert not review.approve.enabled
