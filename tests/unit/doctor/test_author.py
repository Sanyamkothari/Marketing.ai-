"""The one author (`engine.doctor.author`): the only code that writes a plan's items and questions.

The author only proposes. Whether a change is in the plan or waits is code's call; nothing the person decided is
changed directly (replacing or withdrawing it is a proposal that waits for their yes); the default author takes back
what it no longer suggests, the AI only by name; and the same actions give the same case.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from engine.agent.contracts import AgentConfidence, ProposalSource, ProposalState
from engine.doctor import author
from engine.doctor.agent_apply import NO_ROLES, Roles, meaning_key, person_owns, waits_because
from engine.doctor.author import (
    ApplyContext,
    AskPerson,
    AuthorAction,
    Create,
    Doubt,
    ItemDraft,
    Refusal,
    Request,
    SetSetting,
    Withdraw,
    apply_actions,
)
from engine.doctor.case import (
    Case,
    CaseDoubt,
    CaseGoal,
    CaseItem,
    CaseQuestion,
    TimelineKind,
    payload_digest,
)
from engine.doctor.plan_check import recipe_steps_of
from engine.doctor.protocol import DEFAULT_SETTINGS, Ask, AskOption, DataRequest
from engine.doctor.reconcile import NO_LONGER_EMITTED
from engine.doctor.standing import StandingAnswerStore, meaning_of
from engine.doctor.subjects import item_subject
from engine.doctor.swaps import leave_out_for
from engine.doctor.visit_contracts import OptionEffect
from engine.storage import LocalStorage

NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
LATER = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
FILE = "up-1"
PERSON = "u-asha"
CLIENT = "default"
ON = ["01", "02", "03", "04", "05", "06", "07", "08", "09", "10", "11", "12"]


def make_case(
    items: Sequence[CaseItem] = (), questions: Sequence[CaseQuestion] = (), goal: str | None = None
) -> Case:
    return Case(
        case_id="case_0123456789abcdef",
        client_id=CLIENT,
        created_by=PERSON,
        created_at=NOW,
        updated_at=NOW,
        version=1,
        items=tuple(items),
        questions=tuple(questions),
        goals=(CaseGoal(goal_id=goal, active=True, set_at=NOW, set_by=PERSON),) if goal else (),
    )


def step(kind: str, column: str = "spend", **params: Any) -> dict[str, Any]:
    return {"upload_id": FILE, "step": {"order": 1, "kind": kind, "column": column, "params": params}}


def draft(
    kind: str = "parse_number",
    column: str = "spend",
    *,
    confidence: AgentConfidence = AgentConfidence.CHECK,
    **params: Any,
) -> ItemDraft:
    return ItemDraft(
        kind="recipe_step",
        payload=step(kind, column, **params),
        title=f"{kind} {column}",
        upload_id=FILE,
        confidence=confidence,
    )


def create(
    kind: str = "parse_number",
    column: str = "spend",
    *,
    replaces: str | None = None,
    confidence: AgentConfidence = AgentConfidence.CHECK,
    basis: str = "r1",
    reason: str = "because",
    **params: Any,
) -> Create:
    return Create(
        draft=draft(kind, column, confidence=confidence, **params),
        reason=reason,
        replaces=replaces,
        basis=basis,
    )


def ctx(author_kind: str = "default", **more: Any) -> ApplyContext:
    return ApplyContext(
        now=more.pop("now", NOW),
        author=author_kind,  # type: ignore[arg-type]
        roles=NO_ROLES,
        settings=DEFAULT_SETTINGS,
        **more,
    )


def ai(**more: Any) -> ApplyContext:
    return ctx("ai", **more)


def stored(
    kind: str = "parse_number",
    column: str = "spend",
    *,
    state: ProposalState = ProposalState.PENDING,
    decided_by: str | None = None,
    source: ProposalSource = ProposalSource.RULES,
    producer: str | None = "author",
    **params: Any,
) -> CaseItem:
    """An item as an earlier `apply_actions` (or a person's tick) left it."""
    given = draft(kind, column, **params)
    return CaseItem(
        item_id=author.item_id_of(given.kind, given.payload, None),
        kind=given.kind,
        title=given.title,
        source=source,
        confidence=AgentConfidence.CHECK,
        state=state,
        upload_id=FILE,
        payload=dict(given.payload),
        payload_digest=payload_digest(given.payload),
        proposed_at=NOW,
        decided_by=decided_by,
        decided_at=NOW if decided_by else None,
        producer=producer,
    )


def waiting(column: str = "date") -> CaseItem:
    """A change that waits for a tick whoever proposes it (how a date is read)."""
    return stored("parse_date", column)


def in_plan(column: str = "spend", **more: Any) -> CaseItem:
    """In the plan, and nobody's decision."""
    return stored("parse_number", column, state=ProposalState.ACCEPTED, **more)


def persons(column: str = "spend") -> CaseItem:
    return stored("parse_number", column, state=ProposalState.ACCEPTED, decided_by=PERSON)


def by_id(case: Case, item_id: str) -> CaseItem:
    return next(i for i in case.items if i.item_id == item_id)


def active(case: Case) -> list[CaseItem]:
    return [i for i in case.items if i.state is not ProposalState.REJECTED]


def made_by(case: Case, old: CaseItem) -> list[CaseItem]:
    """The items the author added to `case` beside `old`."""
    return [i for i in case.items if i.item_id != old.item_id]


# -- placement --------------------------------------------------------------------------------------------------
def test_a_change_the_rules_allow_goes_into_the_plan_undecided_and_one_that_waits_waits() -> None:
    out = apply_actions(make_case(), [create("parse_number"), create("parse_date", "date")], ai())
    plain, date = sorted(out.case.items, key=lambda i: i.title)[::-1]
    assert plain.title == "parse_number spend" and plain.state is ProposalState.ACCEPTED
    assert date.state is ProposalState.PENDING
    for item in out.case.items:
        assert item.decided_by is None and item.decided_at is None, "the author never decides"
        assert item.producer == "author" and item.source is ProposalSource.AI
        assert item.evidence_ids == ("r1",), "the basis is kept with the evidence"
    assert [e.kind for e in out.events].count(TimelineKind.ITEM_PROPOSED) == 2
    assert out.refused == [] and set(out.made) == {i.item_id for i in out.case.items}


def test_who_wrote_it_is_the_source_and_the_default_author_may_say_what_the_values_prove() -> None:
    sure = create("parse_date", "date", confidence=AgentConfidence.SURE, basis="s:abc123")
    by_default = apply_actions(make_case(), [sure], ctx()).case.items[0]
    assert by_default.source is ProposalSource.RULES and by_default.confidence is AgentConfidence.SURE
    assert (
        by_default.state is ProposalState.ACCEPTED
    ), "proven by the values: in the plan, the person may untick it"
    adopted = apply_actions(make_case(), [sure], ai(suggested=frozenset({"abc123"}))).case.items[0]
    assert adopted.source is ProposalSource.AI and adopted.state is ProposalState.ACCEPTED
    made_up = apply_actions(make_case(), [sure], ai()).case.items[0]
    assert made_up.confidence is AgentConfidence.CHECK, "an `s:` basis names a suggestion a tool really made"
    assert made_up.state is ProposalState.PENDING
    own = create("parse_date", "date", confidence=AgentConfidence.SURE, basis="r2")
    claimed = apply_actions(make_case(), [own], ai()).case.items[0]
    assert claimed.confidence is AgentConfidence.CHECK, "the AI is never sure on its own say"
    assert claimed.state is ProposalState.PENDING


@pytest.mark.parametrize("who", ["ai", "default"])
def test_being_sure_never_puts_a_step_on_a_column_that_holds_a_meaning_into_the_plan(who: str) -> None:
    roles = Roles(protected=frozenset({"opt_in"}))
    context = ApplyContext(
        now=NOW,
        author=who,  # type: ignore[arg-type]
        roles=roles,
        settings=DEFAULT_SETTINGS,
        suggested=frozenset({"abc123"}),
    )
    for basis in ("s:abc123", "s:made-up"):
        out = apply_actions(
            make_case(),
            [
                create(
                    "remove_rows", "opt_in", confidence=AgentConfidence.SURE, basis=basis, operator="equals"
                )
            ],
            context,
        )
        assert [i.state for i in out.case.items] == [ProposalState.PENDING], (who, basis)


def test_the_goals_own_build_is_in_the_plan_and_any_other_change_that_needs_a_yes_waits() -> None:
    build = {"step": {"order": 1, "kind": "sql_query", "column": "t", "params": {}}, "needs_yes": True}
    given = ItemDraft(
        kind="recipe_step",
        payload={**build, "buildable": {"goal": "g"}},
        title="Build",
        confidence=AgentConfidence.CHECK,
    )
    other = given.model_copy(
        update={"payload": {**build, "step": {**build["step"], "column": "u"}}, "title": "Other"}
    )
    out = apply_actions(
        make_case(),
        [Create(draft=given, reason="r", basis="s:1"), Create(draft=other, reason="r", basis="r1")],
        ai(),
    )
    states = {i.title: i.state for i in out.case.items}
    assert states == {"Build": ProposalState.ACCEPTED, "Other": ProposalState.PENDING}
    assert waits_because(next(i for i in out.case.items if i.title == "Other")) == "it needs your own yes"


# -- replace -----------------------------------------------------------------------------------------------------
def test_replacing_a_waiting_change_swaps_at_once() -> None:
    old = waiting("spend")
    out = apply_actions(
        make_case([old]), [create("parse_number", replaces=old.item_id, reason="spend is a number")], ai()
    )
    gone, new = by_id(out.case, old.item_id), made_by(out.case, old)[0]
    assert gone.state is ProposalState.REJECTED and gone.withdrawn_because == "spend is a number"
    assert gone.superseded_by == new.item_id
    assert new.state is ProposalState.ACCEPTED and new.replaces is None
    assert old.item_id in out.made and new.item_id in out.made


def test_replacing_a_change_in_the_plan_that_nobody_decided_is_a_swap_that_waits_for_the_persons_yes() -> (
    None
):
    old = in_plan("spend")
    new = create("normalise_text", "spend", strip=True, replaces=old.item_id, reason="trim it too")
    out = apply_actions(make_case([old]), [new], ai())
    assert by_id(out.case, old.item_id) == old, "the plan does not change without a yes"
    added = made_by(out.case, old)[0]
    assert added.state is ProposalState.PENDING and added.replaces == old.item_id and added.decided_by is None


def test_a_replacement_that_must_wait_does_not_take_a_change_out_of_the_plan() -> None:
    old = in_plan("spend")
    out = apply_actions(make_case([old]), [create("parse_date", "spend", replaces=old.item_id)], ai())
    kept, new = by_id(out.case, old.item_id), made_by(out.case, old)[0]
    assert kept == old, "still in the plan: a swap waits for the person"
    assert new.state is ProposalState.PENDING and new.replaces == old.item_id


def test_replacing_what_the_person_decided_is_a_swap_that_waits_for_their_yes() -> None:
    old = persons("spend")
    out = apply_actions(
        make_case([old]), [create("normalise_text", "spend", strip=True, replaces=old.item_id)], ai()
    )
    assert by_id(out.case, old.item_id) == old, "the person's decision is not touched"
    new = made_by(out.case, old)[0]
    assert new.state is ProposalState.PENDING and new.replaces == old.item_id and new.decided_by is None
    # Their yes to the swap leaves the old one out, as it does for every swap.
    yes = new.model_copy(update={"state": ProposalState.ACCEPTED, "decided_by": PERSON, "decided_at": LATER})
    left, _events = leave_out_for(out.case.items, yes, actor=PERSON, now=LATER)
    assert (
        left[old.item_id].state is ProposalState.REJECTED and left[old.item_id].superseded_by == new.item_id
    )


def test_what_an_approval_covered_is_the_persons_even_without_a_tick() -> None:
    from engine.doctor.case import CaseApproval

    old = in_plan("spend")
    case = make_case([old]).model_copy(
        update={
            "approvals": (
                CaseApproval(approval_id="ap1", item_ids=(old.item_id,), approved_by=PERSON, approved_at=NOW),
            )
        }
    )
    assert person_owns(case, old)
    out = apply_actions(case, [create("normalise_text", "spend", strip=True, replaces=old.item_id)], ai())
    assert by_id(out.case, old.item_id).state is ProposalState.ACCEPTED
    assert made_by(out.case, old)[0].replaces == old.item_id


def test_replacing_an_item_that_is_not_there_or_already_out_is_refused_with_what_to_do() -> None:
    gone = stored("parse_number", "spend", state=ProposalState.REJECTED, decided_by=PERSON)
    out = apply_actions(
        make_case([gone]),
        [create("parse_date", "ship", replaces="nope"), create("parse_date", "date", replaces=gone.item_id)],
        ai(),
    )
    assert [(i, r.code) for i, r in out.refused] == [(0, "ITEM_NOT_FOUND"), (1, "ITEM_ALREADY_LEFT_OUT")]
    assert all(r.how_to_fix for _i, r in out.refused)
    assert out.case.items == (gone,)


# -- withdraw ----------------------------------------------------------------------------------------------------
def test_withdrawing_a_waiting_change_leaves_it_out_now_with_the_reason() -> None:
    old = waiting("date")
    out = apply_actions(
        make_case([old]), [Withdraw(item_id=old.item_id, reason="no dates here", consequence="c")], ai()
    )
    gone = by_id(out.case, old.item_id)
    assert gone.state is ProposalState.REJECTED and gone.withdrawn_because == "no dates here"
    assert len(out.case.items) == 1
    assert any(e.item_id == old.item_id and e.kind is TimelineKind.ITEM_DECIDED for e in out.events)


@pytest.mark.parametrize("make_old", [in_plan, persons], ids=["in the plan", "the person's"])
def test_withdrawing_a_change_in_the_plan_or_the_persons_is_a_proposal_that_waits_for_their_yes(
    make_old: Any,
) -> None:
    old = make_old("spend")
    out = apply_actions(
        make_case([old]),
        [Withdraw(item_id=old.item_id, reason="keep the text", consequence="spend stays text")],
        ai(requested_by=PERSON),
    )
    assert by_id(out.case, old.item_id) == old, "nothing is taken out until they say yes"
    ask = made_by(out.case, old)[0]
    assert ask.kind == "withdraw" and ask.state is ProposalState.PENDING and ask.replaces == old.item_id
    assert ask.payload["item_id"] == old.item_id and ask.payload["consequence"] == "spend stays text"
    assert ask.requested_by == PERSON and ask.decided_by is None
    assert item_subject(ask) == f"withdraw@{old.item_id}"
    # Their yes takes the old one out, through the swap rule every tick uses.
    yes = ask.model_copy(update={"state": ProposalState.ACCEPTED, "decided_by": PERSON, "decided_at": LATER})
    left, _events = leave_out_for(out.case.items, yes, actor=PERSON, now=LATER)
    assert left[old.item_id].state is ProposalState.REJECTED


def test_the_default_author_takes_back_at_once_what_it_put_in_the_plan_itself_but_not_what_is_the_persons() -> (
    None
):
    old = in_plan("spend")
    out = apply_actions(make_case([old]), [Withdraw(item_id=old.item_id, reason="r", consequence="c")], ctx())
    gone = by_id(out.case, old.item_id)
    assert gone.state is ProposalState.REJECTED and gone.withdrawn_because == "r"
    mine = persons("spend")
    kept = apply_actions(
        make_case([mine]), [Withdraw(item_id=mine.item_id, reason="r", consequence="c")], ctx()
    )
    assert by_id(kept.case, mine.item_id) == mine and made_by(kept.case, mine)[0].kind == "withdraw"


def test_a_withdraw_proposal_is_not_a_recipe_step() -> None:
    old = in_plan("spend")
    out = apply_actions(make_case([old]), [Withdraw(item_id=old.item_id, reason="r", consequence="c")], ai())
    ask = made_by(out.case, old)[0]
    accepted = ask.model_copy(update={"state": ProposalState.ACCEPTED})
    steps = recipe_steps_of([accepted, old], FILE, only_file=True)
    assert [item_id for _step, item_id in steps] == [old.item_id]


def test_asking_to_withdraw_again_in_other_words_is_the_same_proposal_and_a_no_stands() -> None:
    old = persons("spend")
    first = apply_actions(
        make_case([old]), [Withdraw(item_id=old.item_id, reason="a", consequence="b")], ai()
    ).case
    ask = made_by(first, old)[0]
    again = apply_actions(first, [Withdraw(item_id=old.item_id, reason="other", consequence="words")], ai())
    assert again.case == first and again.refused == []
    said_no = ask.model_copy(
        update={"state": ProposalState.REJECTED, "decided_by": PERSON, "decided_at": LATER}
    )
    declined = first.model_copy(update={"items": (old, said_no)})
    out = apply_actions(declined, [Withdraw(item_id=old.item_id, reason="x", consequence="y")], ai())
    assert [r.code for _i, r in out.refused] == ["LEFT_OUT_BY_PERSON"] and out.case == declined


def test_withdrawing_what_is_not_there_is_refused() -> None:
    out = apply_actions(make_case([waiting()]), [Withdraw(item_id="x", reason="r", consequence="c")], ai())
    [(index, refusal)] = out.refused
    assert index == 0 and refusal.code == "ITEM_NOT_FOUND" and waiting().item_id in refusal.how_to_fix


# -- what the person left out ------------------------------------------------------------------------------------
def test_a_twin_of_what_the_person_left_out_is_refused_and_the_next_action_still_runs() -> None:
    no = stored("parse_number", "spend", state=ProposalState.REJECTED, decided_by=PERSON)
    again = create("parse_number", "spend", reason="in other words")
    other = create("parse_date", "date")
    out = apply_actions(make_case([no]), [again, other], ai())
    [(index, refusal)] = out.refused
    assert index == 0 and refusal.code == "LEFT_OUT_BY_PERSON" and refusal.how_to_fix
    assert by_id(out.case, no.item_id) == no
    assert [i.title for i in made_by(out.case, no)] == ["parse_date date"]


def test_the_default_author_does_not_bring_back_what_the_person_left_out_either() -> None:
    no = stored("parse_number", "spend", state=ProposalState.REJECTED, decided_by=PERSON)
    out = apply_actions(make_case([no]), [create("parse_number", "spend")], ctx(complete=True))
    assert out.case.items == (no,) and [r.code for _i, r in out.refused] == ["LEFT_OUT_BY_PERSON"]


def test_what_the_author_took_back_itself_may_come_back() -> None:
    back = stored("parse_number", "spend").model_copy(
        update={
            "state": ProposalState.REJECTED,
            "withdrawn_because": NO_LONGER_EMITTED,
            "decided_by": "agent:data-doctor",
        }
    )
    out = apply_actions(make_case([back]), [create("parse_number", "spend")], ctx())
    assert out.refused == [] and active(out.case) and by_id(out.case, back.item_id).withdrawn_because is None


# -- complete and not --------------------------------------------------------------------------------------------
def test_a_stale_undecided_item_is_taken_back_only_when_the_emission_is_complete() -> None:
    pending, plan = waiting("date"), in_plan("spend")
    case = make_case([pending, plan, persons("amount")])
    adds = apply_actions(case, [], ctx(complete=False))
    assert adds.case == case, "an author that adds as it goes takes nothing back"
    complete = apply_actions(case, [], ctx(complete=True))
    for stale in (pending, plan):
        gone = by_id(complete.case, stale.item_id)
        assert gone.state is ProposalState.REJECTED and gone.withdrawn_because == NO_LONGER_EMITTED
    assert (
        by_id(complete.case, persons("amount").item_id).state is ProposalState.ACCEPTED
    ), "the person's stands"
    assert any(e.details.get("why") == "retracted" for e in complete.events)


def test_the_default_author_keeps_what_it_says_again_and_never_takes_back_the_ais() -> None:
    keep = create("parse_number", "spend")
    kept = apply_actions(make_case(), [keep], ctx(complete=True)).case
    ais = stored("parse_date", "date", source=ProposalSource.AI)
    case = kept.model_copy(update={"items": (*kept.items, ais)})
    out = apply_actions(case, [keep], ctx(complete=True))
    assert out.case == case, "said again: kept; the AI's: only by name"


def test_items_stored_by_the_old_rules_are_taken_back_by_the_default_author_and_the_detectors_by_their_round() -> (
    None
):
    old_rules = waiting("date").model_copy(update={"producer": "rules"})
    old_detector = waiting("stock").model_copy(update={"producer": "detectors"})
    fixed = in_plan("spend").model_copy(update={"producer": "detectors"})
    reviewed = stored("parse_date", "ship", source=ProposalSource.AI, producer="review")
    chatted = stored("parse_date", "bill", source=ProposalSource.AI, producer="chat")
    case = make_case([old_rules, old_detector, fixed, reviewed, chatted])
    out = apply_actions(case, [], ctx(complete=True))
    assert by_id(out.case, old_rules.item_id).state is ProposalState.REJECTED
    assert by_id(out.case, old_detector.item_id) == old_detector, "the detectors' round is not the rules'"
    assert by_id(out.case, reviewed.item_id) == reviewed and by_id(out.case, chatted.item_id) == chatted
    # Their own round takes back what they no longer find, but a fix of theirs that is in the plan stays: the files
    # they read already have it, so they cannot find it again.
    theirs = apply_actions(case, [], ctx(complete=True, producer=author.DETECTORS))
    assert by_id(theirs.case, old_detector.item_id).state is ProposalState.REJECTED
    assert by_id(theirs.case, old_rules.item_id) == old_rules
    assert by_id(theirs.case, fixed.item_id) == fixed
    # The AI's own turn takes back nothing it does not name.
    assert apply_actions(case, [], ai()).case == case


def test_a_question_the_old_rules_asked_and_no_longer_stand_behind_is_taken_back() -> None:
    old = CaseQuestion(
        question_id="q.build_audience.g.abc", code="BUILD_AUDIENCE", text="Who?", producer="rules"
    )
    answered = old.model_copy(update={"question_id": "q.other.g.abc", "answer": "a"})
    out = apply_actions(make_case(questions=[old, answered]), [], ctx(complete=True))
    assert [q.question_id for q in out.case.questions] == ["q.other.g.abc"]
    assert apply_actions(make_case(questions=[old]), [], ai()).case.questions == (old,)


# -- determinism -------------------------------------------------------------------------------------------------
def _date_order(rows_disagree: int = 1_204, recommended: str | None = None) -> Ask:
    def effect(option_id: str) -> OptionEffect:
        return OptionEffect(
            option_id=option_id,
            kind="recipe_step",
            title=f"Read it {option_id} first",
            payload={
                "upload_id": FILE,
                "step": {"kind": "parse_date", "column": "joined"},
                "order": option_id,
            },
        )

    return Ask(
        code="DATE_ORDER",
        subject={"upload_id": FILE, "column": "joined"},
        text="In 'joined', which comes first: the day or the month?",
        why="Both readings are valid for some values.",
        options=(
            AskOption("Day first", effect("day"), rows_changed=3_000),
            AskOption("Month first", effect("month"), rows_changed=3_000),
        ),
        rows=3_000,
        rows_disagree=rows_disagree,
        upload_id=FILE,
        column="joined",
        blocking=True,
        recommended=recommended,
    )


def _everything() -> list[AuthorAction]:
    return [
        create("parse_number", "spend"),
        create("parse_date", "date"),
        AskPerson(ask=_date_order()),
        Doubt(doubt=CaseDoubt(doubt_id="d.thin.g", because="b", would_do="w", if_not="i")),
        Request(request=DataRequest(kind="more_data", what="the last order date", why="the goal needs it")),
        Withdraw(item_id="nope", reason="r", consequence="c"),
    ]


@pytest.mark.parametrize("who", ["ai", "default"])
def test_the_same_actions_give_the_same_case_and_applying_them_again_changes_nothing(who: str) -> None:
    case = make_case([waiting("ship"), persons("amount")])
    context = ctx(who, complete=who == "default")
    first = apply_actions(case, _everything(), context)
    assert apply_actions(case, _everything(), context).case == first.case, "deterministic"
    second = apply_actions(first.case, _everything(), context)
    assert second.case == first.case, "idempotent"
    assert [e for e in second.events if e.kind is not TimelineKind.ITEM_DECIDED] == []
    assert [code for _i, code in ((i, r.code) for i, r in second.refused)] == ["ITEM_NOT_FOUND"]


def test_the_result_does_not_depend_on_the_order_the_creates_come_in() -> None:
    actions = [
        create("parse_number", "spend"),
        create("parse_date", "date"),
        create("parse_number", "amount"),
    ]
    forward = apply_actions(make_case(), actions, ai()).case
    backward = apply_actions(make_case(), actions[::-1], ai()).case
    assert sorted(i.item_id for i in forward.items) == sorted(i.item_id for i in backward.items)


# -- questions, doubts, requests ----------------------------------------------------------------------------------
def test_an_ask_goes_through_the_protocol_and_its_options_are_kept_for_the_answer() -> None:
    out = apply_actions(make_case(), [AskPerson(ask=_date_order())], ai())
    [question] = out.case.questions
    assert question.code == "DATE_ORDER" and question.answer is None and question.blocking
    assert question.producer == "author"
    assert out.case.visit is not None and set(out.case.visit.offers) == {question.question_id}
    assert [e.option_id for e in out.case.visit.offers[question.question_id]][:2] == ["day", "month"]
    assert out.case.counters.questions_per_file == {FILE: 1}
    assert [e.kind for e in out.events] == [TimelineKind.QUESTION_ASKED] and out.made == [
        question.question_id
    ]


def test_a_small_ambiguity_is_a_stated_assumption_in_the_plan_and_never_decided_by_the_agent() -> None:
    out = apply_actions(make_case(), [AskPerson(ask=_date_order(rows_disagree=3, recommended="day"))], ctx())
    [question] = out.case.questions
    [item] = out.case.items
    assert question.assumed and question.answer is not None
    assert item.state is ProposalState.ACCEPTED and item.assumption
    assert item.decided_by is None and item.source is ProposalSource.RULES
    assert out.case.counters.questions_per_file == {}, "an assumption is not a question asked"


def test_the_budget_cuts_the_default_authors_questions_short_but_not_the_answer_to_a_message() -> None:
    def spread(n: int) -> list[AuthorAction]:
        return [
            AskPerson(
                ask=_date_order().__class__(
                    **{
                        **_date_order().__dict__,
                        "subject": {"upload_id": FILE, "column": f"c{k}"},
                        "column": f"c{k}",
                        "blocking": False,
                    }
                )
            )
            for k in range(n)
        ]

    over = apply_actions(make_case(), spread(14), ctx())
    asked = [q for q in over.case.questions if not q.assumed]
    assert len(asked) <= DEFAULT_SETTINGS.max_questions_per_file < 14
    reply = apply_actions(make_case(), spread(14), ai(requested_by=PERSON))
    assert len([q for q in reply.case.questions if not q.assumed]) == 14


def test_a_doubt_and_a_data_request_are_recorded_once() -> None:
    both = [
        Doubt(doubt=CaseDoubt(doubt_id="d.thin.g", because="b", would_do="w", if_not="i")),
        Request(request=DataRequest(kind="more_data", what="the last order date", why="the goal needs it")),
    ]
    out = apply_actions(make_case(), both, ai())
    [doubt] = out.case.doubts
    [request] = out.case.questions
    assert doubt.producer == "author" and request.code == "DATA_REQUESTED"
    assert {e.kind for e in out.events} == {TimelineKind.DOUBT, TimelineKind.DATA_REQUESTED}
    assert apply_actions(out.case, both, ai()).case == out.case


# -- settings ----------------------------------------------------------------------------------------------------
class Window:
    def allowed(self, setting: str) -> list[int]:
        return [30, 60, 90] if setting == "window_days" else []


def setting(value: str, setting: str = "window_days") -> SetSetting:
    return SetSetting(goal_id="g", setting=setting, value=value, reason="the person asked")


def test_a_setting_outside_what_the_goal_allows_is_refused_naming_what_is_allowed() -> None:
    out = apply_actions(make_case(goal="g"), [setting("45"), setting("1", "colour")], ai(goal=Window()))
    assert [r.code for _i, r in out.refused] == ["SETTING_VALUE_NOT_ALLOWED"] * 2
    assert "30, 60, 90" in out.refused[0][1].how_to_fix and "colour" in out.refused[1][1].how_to_fix
    assert out.case.items == ()
    assert [r.code for _i, r in apply_actions(make_case(goal="g"), [setting("60")], ai()).refused] == [
        "SETTING_VALUE_NOT_ALLOWED"
    ], "with no goal there is nothing to set"


def test_a_setting_is_a_proposal_that_replaces_the_one_before_and_a_persons_waits_for_their_yes() -> None:
    first = apply_actions(make_case(goal="g"), [setting("90")], ai(goal=Window())).case
    [ninety] = first.items
    assert ninety.kind == "goal_setting" and ninety.state is ProposalState.PENDING and ninety.goal_id == "g"
    assert item_subject(ninety) == "setting@g:window_days"
    swapped = apply_actions(first, [setting("60")], ai(goal=Window())).case
    assert (
        by_id(swapped, ninety.item_id).state is ProposalState.REJECTED
    ), "a waiting setting is swapped at once"
    [sixty] = made_by(swapped, ninety)
    assert sixty.payload["value"] == "60"
    # Once the person has said yes to 60, a change to 30 waits for their yes.
    yes = sixty.model_copy(
        update={"state": ProposalState.ACCEPTED, "decided_by": PERSON, "decided_at": LATER}
    )
    decided = swapped.model_copy(
        update={"items": tuple(yes if i.item_id == sixty.item_id else i for i in swapped.items)}
    )
    out = apply_actions(decided, [setting("30")], ai(goal=Window()))
    thirty = next(i for i in out.case.items if i.payload.get("value") == "30")
    assert thirty.state is ProposalState.PENDING and thirty.replaces == sixty.item_id
    assert by_id(out.case, sixty.item_id).state is ProposalState.ACCEPTED
    assert apply_actions(out.case, [setting("30")], ai(goal=Window())).case == out.case


def test_a_setting_said_again_is_one_item_whatever_the_reason() -> None:
    once = apply_actions(make_case(goal="g"), [setting("60")], ai(goal=Window())).case
    twice = apply_actions(once, [setting("60").model_copy(update={"reason": "again"})], ai(goal=Window()))
    assert twice.case == once


# -- the guard layer's hook ---------------------------------------------------------------------------------------
def test_a_refusal_from_the_guards_skips_that_action_and_the_next_one_still_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def check(_case: Case, action: AuthorAction, _ctx: ApplyContext) -> Refusal | None:
        if isinstance(action, Create) and action.draft.title == "parse_number spend":
            return Refusal(code="STEP_ON_ROLE_COLUMN", what_failed="no", how_to_fix="use another column")
        return None

    monkeypatch.setattr(author, "check_action", check)
    out = apply_actions(make_case(), [create("parse_number", "spend"), create("parse_date", "date")], ai())
    assert [(i, r.code) for i, r in out.refused] == [(0, "STEP_ON_ROLE_COLUMN")]
    assert [i.title for i in out.case.items] == ["parse_date date"]


def test_every_refusal_the_author_makes_says_what_failed_and_how_to_fix_it() -> None:
    gone = stored("parse_number", "spend", state=ProposalState.REJECTED, decided_by=PERSON)
    no = stored("parse_date", "date", state=ProposalState.REJECTED, decided_by=PERSON)
    made = [
        apply_actions(
            make_case([gone, no]),
            [
                create("parse_date", "ship", replaces="nope"),
                create("parse_date", "ship", replaces=gone.item_id),
                Withdraw(item_id="nope", reason="r", consequence="c"),
                Withdraw(item_id=gone.item_id, reason="r", consequence="c"),
                create("parse_date", "date", reason="in other words"),
                setting("45"),
            ],
            ai(goal=Window()),
        )
    ]
    refusals = [r for out in made for _i, r in out.refused]
    assert {r.code for r in refusals} == {
        "ITEM_NOT_FOUND",
        "ITEM_ALREADY_LEFT_OUT",
        "LEFT_OUT_BY_PERSON",
        "SETTING_VALUE_NOT_ALLOWED",
    }
    assert len(refusals) == 6 and all(r.what_failed and r.how_to_fix for r in refusals)


# -- standing answers ---------------------------------------------------------------------------------------------
def test_a_standing_answer_is_applied_as_the_persons_earlier_decision(tmp_path: Path) -> None:
    store = StandingAnswerStore(LocalStorage(tmp_path))
    ask = _date_order()
    first = apply_actions(make_case(), [AskPerson(ask=ask)], ai())
    question = first.case.questions[0]
    option = next(o for o in question.options if o.label == "Day first")
    effect = next(e for e in first.case.visit.offers[question.question_id] if e.option_id == option.option_id)  # type: ignore[union-attr]
    store.remember(
        CLIENT,
        question,
        option_id=option.option_id,
        effect_digest=option.effect_digest,
        meaning=meaning_of(effect.kind, effect.payload),
        title="Read dates day first",
        actor=PERSON,
        at=NOW,
    )
    out = apply_actions(make_case(), [AskPerson(ask=ask)], ai(now=LATER, standing=store))
    [answered] = out.case.questions
    [item] = out.case.items
    assert (
        answered.answer == option.option_id and answered.standing_answer_id and answered.answered_by == PERSON
    )
    assert item.state is ProposalState.ACCEPTED and item.decided_by == PERSON, "their earlier decision"
    assert item.standing_answer_id == answered.standing_answer_id and item.title.startswith(
        "Using your answer"
    )
    assert len(out.case.standing_answers_used) == 1
    assert out.case.counters.questions_per_file == {}, "a standing answer is not a question asked"
    assert [e.kind for e in out.events if e.kind is not TimelineKind.ITEM_PROPOSED] == [
        TimelineKind.STANDING_APPLIED
    ]
    [after] = out.after
    after(out.case)
    after(out.case)  # counted once per case
    assert store.get(CLIENT, answered.standing_answer_id or "").used_count == 1  # type: ignore[union-attr]
    # The same button doing something else is asked again, saying why, never applied.
    changed = _date_order().__class__(
        **{
            **ask.__dict__,
            "options": (
                AskOption(
                    "Day first",
                    OptionEffect(option_id="day", kind="recipe_step", title="x", payload={"other": 1}),
                ),
                ask.options[1],
            ),
        }
    )
    asked = apply_actions(make_case(), [AskPerson(ask=changed)], ai(now=LATER, standing=store))
    assert asked.case.questions[0].answer is None
    assert "was not used" in asked.case.questions[0].why
    assert [e.kind for e in asked.events if e.kind is TimelineKind.STANDING_UNFIT]


# -- what a change means -----------------------------------------------------------------------------------------
def test_a_withdraw_and_a_setting_mean_what_they_take_out_and_the_value() -> None:
    old = in_plan("spend")
    case = apply_actions(
        make_case([old]), [Withdraw(item_id=old.item_id, reason="a", consequence="b")], ai()
    ).case
    one = made_by(case, old)[0]
    other = one.model_copy(
        update={"item_id": "withdraw.base.other", "payload": {**one.payload, "reason": "z"}}
    )
    assert meaning_key(one) == meaning_key(other)
    s60 = apply_actions(make_case(goal="g"), [setting("60")], ai(goal=Window())).case.items[0]
    s90 = apply_actions(make_case(goal="g"), [setting("90")], ai(goal=Window())).case.items[0]
    assert meaning_key(s60) != meaning_key(s90)


def test_a_question_a_fact_function_asks_settles_with_exactly_one_i_dont_know() -> None:
    from engine.doctor.tools_facts import Found

    found = Found()
    found.ask(
        "KEY_AMBIGUOUS",
        {"upload_id": FILE},
        text="Which column names each row?",
        choices=[
            (
                "a",
                OptionEffect(
                    option_id="c0", kind="key", title="a", payload={"upload_id": FILE, "column": "a"}
                ),
            ),
            (
                "b",
                OptionEffect(
                    option_id="c1", kind="key", title="b", payload={"upload_id": FILE, "column": "b"}
                ),
            ),
            ("I don't know", OptionEffect(option_id="none")),
        ],
        upload_id=FILE,
        reason="two columns are different on every row",
    )
    out = apply_actions(make_case(), [found.suggested[0].action], ctx(complete=True))
    [question] = out.case.questions
    assert [o.label for o in question.options].count("I don't know") == 1
    assert question.answer is None, "the person is asked"
