"""F5: complete memo keys and reconciled proposals (`engine.doctor.reconcile`, `subjects`, `memo`).

Each producer emits its set; the store adds, supersedes or retracts by subject and never touches what the person owns.
The visit-level tests are the class of fault, not one instance: the same events in any order give the same open
items, questions and doubts; "no column has the outcome" and "which column is the outcome?" are never open together;
a replaced query is never in the plan beside its replacement; a change the person left out does not come back from
the review in other words.
"""

from __future__ import annotations

import copy
import itertools
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from engine.agent.contracts import AgentConfidence, ProposalSource, ProposalState
from engine.doctor import visit
from engine.doctor.case import Case, CaseDoubt, CaseItem, CaseOption, CaseQuestion
from engine.doctor.reconcile import Emission, reconcile
from engine.doctor.subjects import doubt_subject, item_subject, options_digest, question_subject
from engine.storage import LocalStorage
from tests.unit.doctor import test_visit_prepared_outcome as po
from tests.unit.doctor.test_visit_prepared_outcome import GOAL, STEP
from tests.unit.doctor.visit_support import ANALYST, CLIENT, FIRST, World, deps_for, open_case

NOW = datetime(2026, 1, 1, tzinfo=UTC)
world = po.world
storage = po.storage


def _query(
    sql: str, *, name: str = "customer_training", columns: list[str] | None = None, **extra: Any
) -> Any:
    step = copy.deepcopy(STEP)
    step["params"]["sql"] = sql
    step["params"]["output_name"] = name
    step["column"] = name
    step["params"].update(extra)
    return visit._item(
        "recipe_step",
        {"step": step, "stage": "post_build", "columns": columns or ["customer_id", "spend"]},  # type: ignore[dict-item]
        title=f"Table {name}",
        upload_id=None,
        confidence=AgentConfidence.CHECK,
        evidence=(),
        now=NOW,
    )


def _ai(item: CaseItem) -> CaseItem:
    return item.model_copy(update={"source": ProposalSource.AI})


def _fix(column: str, kind: str, reason: str = "") -> CaseItem:
    step = {"order": 1, "kind": kind, "column": column, "reason": reason, "params": {}}
    return visit._item(
        "recipe_step",
        {"step": step},  # type: ignore[dict-item]
        title=f"{kind} {column}",
        upload_id=FIRST,
        confidence=AgentConfidence.CHECK,
        evidence=(),
        now=NOW,
    )


def _case(storage: LocalStorage, world: World, items: tuple[CaseItem, ...] = ()) -> Case:
    case = open_case(storage, world)
    return case.model_copy(update={"items": items})


def _q(
    code: str, labels: list[str], *, answer: str | None = None, producer: str | None = None
) -> CaseQuestion:
    return CaseQuestion(
        question_id=f"q.{code.lower()}.base.abc",
        code=code,
        text="Which?",
        options=tuple(CaseOption(option_id=f"c{n}", label=label) for n, label in enumerate(labels)),
        blocking=True,
        answer=answer,
        producer=producer,
    )


def _doubt(name: str) -> CaseDoubt:
    return CaseDoubt(doubt_id=f"d.{name}.goal", because="b", would_do="w", if_not="i")


# -- subjects --------------------------------------------------------------------------------------------------
def test_subjects_are_derived_from_kind_and_payload() -> None:
    assert item_subject(_query("SELECT 1")) == "sql@customer_training"
    assert item_subject(_fix("amount", "parse_number")) == f"fix@amount:parse_number#{FIRST}"
    key = visit._item(
        "key",
        {"column": "id"},
        title="k",
        upload_id=FIRST,
        confidence=AgentConfidence.SURE,
        evidence=(),
        now=NOW,
    )
    assert item_subject(key) == f"key@{FIRST}"
    outcome = visit._item(
        "outcome_column",
        {"column": "y"},
        title="o",
        upload_id=FIRST,
        confidence=AgentConfidence.SURE,
        evidence=(),
        now=NOW,
        goal_id="g",
    )
    assert item_subject(outcome) == "outcome@g"
    assert doubt_subject(_doubt("outcome_missing")) == "data_check@outcome_missing.goal"
    assert question_subject(_q("KEY_AMBIGUOUS", ["a"])) == "question@KEY_AMBIGUOUS:-"


def test_steps_that_add_up_are_not_one_subject() -> None:
    one, two = _fix("status", "remove_rows", "a"), _fix("status", "remove_rows", "b")
    assert item_subject(one) == item_subject(two), "wording is not part of what it does"
    other = visit._item(
        "recipe_step",
        {"step": {"order": 1, "kind": "remove_rows", "column": "status", "params": {"values": ["x"]}}},
        title="t",
        upload_id=FIRST,
        confidence=AgentConfidence.CHECK,
        evidence=(),
        now=NOW,
    )
    assert item_subject(other) != item_subject(one), "two filters on one column are two matters"


def test_the_options_digest_ignores_the_order_of_the_buttons() -> None:
    assert options_digest(_q("X", ["a", "b"])) == options_digest(_q("X", ["b", "a"]))
    assert options_digest(_q("X", ["a", "b"])) != options_digest(_q("X", ["a", "c"]))


# -- the store -------------------------------------------------------------------------------------------------
def test_a_new_payload_for_the_same_subject_supersedes_a_waiting_proposal(
    storage: LocalStorage, world: World
) -> None:
    old, new = _ai(_query("SELECT 1")), _ai(_query("SELECT 2"))
    case = _case(storage, world, (old,))
    done = reconcile(case, Emission(producer="chat", items=[new]), now=NOW)
    by_id = {i.item_id: i for i in done.items}
    assert by_id[new.item_id].state is ProposalState.PENDING
    gone = by_id[old.item_id]
    assert (
        gone.state is ProposalState.REJECTED and gone.superseded_by == new.item_id and gone.withdrawn_because
    )


def test_a_replacement_of_a_person_owned_item_is_a_swap_never_both_active(
    storage: LocalStorage, world: World
) -> None:
    mine = _ai(_query("SELECT 1")).model_copy(
        update={"state": ProposalState.ACCEPTED, "decided_by": ANALYST, "decided_at": NOW}
    )
    new = _ai(_query("SELECT 2"))
    case = _case(storage, world, (mine,))
    done = reconcile(case, Emission(producer="review", items=[new], complete=True), now=NOW)
    by_id = {i.item_id: i for i in done.items}
    assert by_id[mine.item_id].state is ProposalState.ACCEPTED, "the person's change stands until they say"
    assert by_id[new.item_id].state is ProposalState.PENDING and by_id[new.item_id].replaces == mine.item_id


def test_a_complete_emission_retracts_what_the_producer_no_longer_emits_but_not_the_persons(
    storage: LocalStorage, world: World
) -> None:
    waiting = _fix("amount", "parse_number").model_copy(update={"producer": "rules"})
    kept = _fix("date", "parse_date").model_copy(update={"producer": "rules"})
    theirs = _fix("zip", "normalise_text").model_copy(
        update={"producer": "rules", "state": ProposalState.ACCEPTED, "decided_by": ANALYST}
    )
    chat = _fix("age", "parse_number").model_copy(update={"producer": "chat"})
    case = _case(storage, world, (waiting, kept, theirs, chat))
    done = reconcile(case, Emission(producer="rules", items=[kept], complete=True), now=NOW)
    state = {i.item_id: i for i in done.items}
    assert state[waiting.item_id].withdrawn_because and state[waiting.item_id].state is ProposalState.REJECTED
    assert state[kept.item_id].state is ProposalState.PENDING
    assert state[theirs.item_id].state is ProposalState.ACCEPTED
    assert (
        state[chat.item_id].state is ProposalState.PENDING
    ), "another producer's change is not this one's to take back"


def test_a_producer_that_adds_as_it_goes_retracts_nothing(storage: LocalStorage, world: World) -> None:
    waiting = _fix("amount", "parse_number").model_copy(update={"producer": "chat"})
    case = _case(storage, world, (waiting,))
    done = reconcile(case, Emission(producer="chat", items=[]), now=NOW)
    assert done.items[0].state is ProposalState.PENDING


def test_a_change_the_person_left_out_does_not_come_back_in_other_words(
    storage: LocalStorage, world: World
) -> None:
    no = _ai(_query("SELECT 1", tried_after=10)).model_copy(
        update={"state": ProposalState.REJECTED, "decided_by": ANALYST}
    )
    again = _ai(_query("SELECT 1", tried_after=99))
    again = again.model_copy(update={"item_id": "other-id"})
    case = _case(storage, world, (no,))
    done = reconcile(case, Emission(producer="review", items=[again], complete=True), now=NOW)
    assert [i.item_id for i in done.items] == [no.item_id] and not done.added
    assert done.twins["other-id"] is not None and done.twins["other-id"].item_id == no.item_id


def test_a_withdrawn_proposal_the_producer_emits_again_comes_back(
    storage: LocalStorage, world: World
) -> None:
    item = _fix("amount", "parse_number").model_copy(update={"producer": "rules"})
    case = _case(storage, world, (item,))
    gone = reconcile(case, Emission(producer="rules", items=[], complete=True), now=NOW)
    assert gone.items[0].withdrawn_because
    back = reconcile(case, Emission(producer="rules", items=[item], complete=True), now=NOW, before=gone)
    assert back.items[0].state is ProposalState.PENDING and back.items[0].withdrawn_because is None


def test_a_superseded_proposal_is_not_brought_back_over_its_replacement(
    storage: LocalStorage, world: World
) -> None:
    old, new = _ai(_query("SELECT 1")), _ai(_query("SELECT 2"))
    case = _case(storage, world, (old.model_copy(update={"producer": "rules"}),))
    first = reconcile(case, Emission(producer="chat", items=[new]), now=NOW)
    again = reconcile(case, Emission(producer="rules", items=[old], complete=True), now=NOW, before=first)
    states = {i.item_id: i.state for i in again.items}
    assert states[new.item_id] is ProposalState.PENDING and states[old.item_id] is ProposalState.REJECTED


def test_a_question_whose_options_changed_is_asked_again_if_nobody_answered_it(
    storage: LocalStorage, world: World
) -> None:
    case = _case(storage, world).model_copy(update={"questions": (_q("OUTCOME_COLUMN", ["a", "b"]),)})
    done = reconcile(
        case,
        Emission(producer="rules", questions=[(_q("OUTCOME_COLUMN", ["a", "c"]), ())], complete=True),
        now=NOW,
    )
    assert [o.label for o in done.questions[0].options] == ["a", "c"]
    assert done.reasked == {done.questions[0].question_id}


def test_an_answered_question_stays_unless_the_option_chosen_is_gone(
    storage: LocalStorage, world: World
) -> None:
    answered = _q("OUTCOME_COLUMN", ["a", "b"], answer="c1").model_copy(update={"answered_at": NOW})
    case = _case(storage, world).model_copy(update={"questions": (answered,)})
    stays = reconcile(
        case, Emission(producer="rules", questions=[(_q("OUTCOME_COLUMN", ["a", "b", "z"]), ())]), now=NOW
    )
    assert stays.questions[0].answer == "c1" and not stays.reasked
    gone = reconcile(
        case,
        Emission(producer="rules", questions=[(_q("OUTCOME_COLUMN", ["a", "z"]), ())]),
        now=NOW,
        refit=visit._no_longer_fits,
    )
    assert gone.questions[0].answer is None and "was not used" in gone.questions[0].why


def test_a_question_its_producer_no_longer_asks_is_taken_back_but_not_an_answered_one(
    storage: LocalStorage, world: World
) -> None:
    open_q = _q("DATA_REQUESTED", ["a"], producer="rules")
    done_q = _q("OTHER", ["a"], answer="c0", producer="rules").model_copy(
        update={"question_id": "q.other.base.x"}
    )
    case = _case(storage, world).model_copy(update={"questions": (open_q, done_q)})
    done = reconcile(case, Emission(producer="rules", complete=True), now=NOW)
    assert [q.question_id for q in done.questions] == ["q.other.base.x"]


def test_a_doubt_its_producer_no_longer_finds_is_resolved_and_holds_again_when_found(
    storage: LocalStorage, world: World
) -> None:
    case = _case(storage, world).model_copy(
        update={"doubts": (_doubt("outcome_missing").model_copy(update={"producer": "rules"}),)}
    )
    gone = reconcile(case, Emission(producer="rules", complete=True), now=NOW)
    assert gone.doubts[0].status == "resolved" and gone.doubts[0].closed_because
    back = reconcile(
        case,
        Emission(producer="rules", doubts=[_doubt("outcome_missing")], complete=True),
        now=NOW,
        before=gone,
    )
    assert back.doubts[0].status == "open"


def test_a_doubt_about_a_change_that_was_taken_back_is_closed(storage: LocalStorage, world: World) -> None:
    old, new = _ai(_query("SELECT 1")), _ai(_query("SELECT 2"))
    doubt = CaseDoubt(doubt_id="d1", because="b", would_do="w", if_not="i", item_id=old.item_id)
    case = _case(storage, world, (old,)).model_copy(update={"doubts": (doubt,)})
    done = reconcile(case, Emission(producer="chat", items=[new]), now=NOW)
    assert done.doubts[0].status == "superseded"


def test_the_same_emissions_in_any_order_give_the_same_plan(storage: LocalStorage, world: World) -> None:
    case = _case(storage, world)
    first = [_ai(_query("SELECT 1")), _ai(_query("SELECT 2", name="other"))]
    results = []
    for order in itertools.permutations(first):
        done = reconcile(case, Emission(producer="chat", items=list(order)), now=NOW)
        results.append(sorted((i.item_id, i.state.value) for i in done.items))
    assert all(r == results[0] for r in results)


# -- inside the visit ------------------------------------------------------------------------------------------
def _chat(storage: LocalStorage, world: World, case: Case, *items: Any, text: str = "Build.") -> Case:
    return visit.chat(
        CLIENT,
        case.case_id,
        text,
        po._deps(storage, world, tuple(items)),
        expected_version=case.version,
        actor=ANALYST,
    )


def _goal_first(storage: LocalStorage, world: World) -> Case:
    case = visit.resume(CLIENT, open_case(storage, world).case_id, po._deps(storage, world), actor=ANALYST)
    return visit.set_goal(
        CLIENT, case.case_id, GOAL, po._deps(storage, world), expected_version=case.version, actor=ANALYST
    )


def _shape(case: Case) -> Any:
    """What the person sees open: waiting items, unanswered questions, open doubts, by subject."""
    return (
        sorted(
            item_subject(i)
            for i in case.items
            if i.state is ProposalState.PENDING and not i.withdrawn_because
        ),
        sorted(q.question_id for q in case.questions if q.answer is None),
        sorted(d.doubt_id for d in case.doubts if d.status == "open"),
    )


def test_the_goal_rules_see_a_table_the_chat_builds_after_the_goal_was_chosen(
    storage: LocalStorage, world: World
) -> None:
    case = _goal_first(storage, world)
    assert any(d.doubt_id.startswith("d.outcome_missing") for d in case.doubts if d.status == "open")
    case = _chat(
        storage,
        world,
        case,
        po._sql_item(world, ["customer_id", "ordered_again_90d"], two_valued=["ordered_again_90d"]),
    )
    codes = [q.code for q in case.questions if q.answer is None]
    assert "OUTCOME_COLUMN" in codes and not [c for c in codes if c.startswith("DATA_REQ")]
    assert not [d for d in case.doubts if d.status == "open" and d.doubt_id.startswith("d.outcome_missing")]


def test_no_column_has_the_outcome_and_which_column_is_the_outcome_are_never_open_together(
    storage: LocalStorage, world: World
) -> None:
    for build in (True, False):
        case = _goal_first(storage, world)
        if build:
            case = _chat(
                storage,
                world,
                case,
                po._sql_item(world, ["customer_id", "ordered_again_90d"], two_valued=["ordered_again_90d"]),
            )
        open_codes = [q.code for q in case.questions if q.answer is None]
        missing = any(d.doubt_id.startswith("d.outcome_missing") for d in case.doubts if d.status == "open")
        assert not (missing and "OUTCOME_COLUMN" in open_codes)
        assert missing != ("OUTCOME_COLUMN" in open_codes)


def test_the_same_events_in_either_order_leave_the_same_open_items(tmp_path: Path, world: World) -> None:
    built = po._sql_item(world, ["customer_id", "ordered_again_90d"], two_valued=["ordered_again_90d"])
    shapes = []
    for order in ("goal_then_chat", "chat_then_goal"):
        store = LocalStorage(tmp_path / order)
        case = visit.resume(CLIENT, open_case(store, world).case_id, po._deps(store, world), actor=ANALYST)
        if order == "goal_then_chat":
            case = visit.set_goal(
                CLIENT,
                case.case_id,
                GOAL,
                po._deps(store, world),
                expected_version=case.version,
                actor=ANALYST,
            )
            case = _chat(store, world, case, built)
        else:
            case = _chat(store, world, case, built)
            case = visit.set_goal(
                CLIENT,
                case.case_id,
                GOAL,
                po._deps(store, world),
                expected_version=case.version,
                actor=ANALYST,
            )
        shapes.append(_shape(case))
    assert shapes[0] == shapes[1]


def test_a_replaced_query_never_leaves_both_in_the_plan(storage: LocalStorage, world: World) -> None:
    case = visit.resume(CLIENT, open_case(storage, world).case_id, po._deps(storage, world), actor=ANALYST)
    first = _ai(_query("SELECT customer_id, 1 AS y FROM invoices"))
    case = _chat(storage, world, case, first)
    second = _ai(_query("SELECT customer_id, 2 AS y FROM invoices"))
    case = _chat(storage, world, case, second)
    live = [
        i
        for i in case.items
        if item_subject(i) == "sql@customer_training" and i.state is not ProposalState.REJECTED
    ]
    assert [i.item_id for i in live] == [second.item_id]
    (old,) = (i for i in case.items if i.item_id == first.item_id)
    assert old.superseded_by == second.item_id


def test_a_swap_for_the_persons_query_waits_and_their_yes_leaves_the_old_one_out(
    storage: LocalStorage, world: World
) -> None:
    case = visit.resume(CLIENT, open_case(storage, world).case_id, po._deps(storage, world), actor=ANALYST)
    first = _ai(_query("SELECT customer_id, 1 AS y FROM invoices"))
    case = _chat(storage, world, case, first)
    case = visit.decide(
        CLIENT,
        case.case_id,
        first.item_id,
        True,
        po._deps(storage, world),
        expected_version=case.version,
        actor=ANALYST,
    )
    second = _ai(_query("SELECT customer_id, 2 AS y FROM invoices"))
    case = _chat(storage, world, case, second)
    by_id = {i.item_id: i for i in case.items}
    assert by_id[first.item_id].state is ProposalState.ACCEPTED, "never replaced without the person's yes"
    assert (
        by_id[second.item_id].state is ProposalState.PENDING
        and by_id[second.item_id].replaces == first.item_id
    )
    case = visit.decide(
        CLIENT,
        case.case_id,
        second.item_id,
        True,
        po._deps(storage, world),
        expected_version=case.version,
        actor=ANALYST,
    )
    by_id = {i.item_id: i for i in case.items}
    assert by_id[second.item_id].state is ProposalState.ACCEPTED
    assert by_id[first.item_id].state is ProposalState.REJECTED, "the two are never both in the plan"


def test_a_change_the_person_rejected_does_not_come_back_from_the_review_in_other_words(
    storage: LocalStorage, world: World
) -> None:
    case = visit.resume(CLIENT, open_case(storage, world).case_id, po._deps(storage, world), actor=ANALYST)
    first = _ai(_query("SELECT customer_id, 1 AS y FROM invoices", tried_after=5))
    case = _chat(storage, world, case, first)
    case = visit.decide(
        CLIENT,
        case.case_id,
        first.item_id,
        False,
        po._deps(storage, world),
        expected_version=case.version,
        actor=ANALYST,
    )
    reworded = _ai(_query("SELECT customer_id, 1 AS y FROM invoices", tried_after=900)).model_copy(
        update={"item_id": "ai.reworded.1", "title": "The same table, said differently"}
    )

    def review(_case: Case, _files: Any, _brief: Any) -> visit.Detected:
        return visit.Detected(items=(reworded,), model_calls=1)

    deps = deps_for(storage, world, agent=review)
    case = visit.resume(CLIENT, case.case_id, deps, actor=ANALYST)
    case = visit.set_goal(CLIENT, case.case_id, GOAL, deps, expected_version=case.version, actor=ANALYST)
    assert "ai.reworded.1" not in {i.item_id for i in case.items}
    assert not [
        i
        for i in case.items
        if i.state is ProposalState.PENDING and item_subject(i) == "sql@customer_training"
    ]


def test_the_review_runs_again_when_the_plan_changes(storage: LocalStorage, world: World) -> None:
    calls: list[int] = []

    def review(case: Case, _files: Any, _brief: Any) -> visit.Detected:
        calls.append(len(case.items))
        return visit.Detected(model_calls=1)

    deps = deps_for(storage, world, agent=review)
    case = visit.resume(CLIENT, open_case(storage, world).case_id, deps, actor=ANALYST)
    first = len(calls)
    assert first >= 1
    case = visit.chat(
        CLIENT,
        case.case_id,
        "hi",
        po._deps(storage, world, (_ai(_query("SELECT 1")),)),
        expected_version=case.version,
        actor=ANALYST,
    )
    item = next(i for i in case.items if item_subject(i) == "sql@customer_training")
    visit.decide(CLIENT, case.case_id, item.item_id, True, deps, expected_version=case.version, actor=ANALYST)
    assert len(calls) > first, "an accepted change is a new plan for the review"


# -- review, memo, producers, answers, swaps (the fixer's round on F5) -----------------------------------------
def _decide(storage: LocalStorage, world: World, case: Case, item_id: str, accept: bool = True) -> Case:
    return visit.decide(
        CLIENT,
        case.case_id,
        item_id,
        accept,
        po._deps(storage, world),
        expected_version=case.version,
        actor=ANALYST,
    )


def test_a_stored_case_without_producers_adopts_them_and_the_contradiction_clears(
    storage: LocalStorage, world: World
) -> None:
    old = _ai(_query("SELECT 1"))
    question = _q("OUTCOME_COLUMN", ["a"])
    doubt = _doubt("outcome_missing")
    case = _case(storage, world, (old,)).model_copy(update={"questions": (question,), "doubts": (doubt,)})
    again = Emission(producer="rules", items=[old], questions=[(question, ())], doubts=[doubt], complete=True)
    first = reconcile(case, again, now=NOW)
    assert first.items[0].producer == "rules" and first.questions[0].producer == "rules"
    assert first.doubts[0].producer == "rules"
    nothing = reconcile(case, Emission(producer="rules", complete=True), now=NOW, before=first)
    assert nothing.items[0].withdrawn_because and not nothing.questions
    assert nothing.doubts[0].status == "resolved"


def test_a_retraction_is_on_the_timeline(storage: LocalStorage, world: World) -> None:
    case = _case(storage, world).model_copy(
        update={
            "questions": (_q("DATA_REQUESTED", ["a"], producer="rules"),),
            "doubts": (_doubt("x").model_copy(update={"producer": "rules"}),),
        }
    )
    done = reconcile(case, Emission(producer="rules", complete=True), now=NOW)
    assert {e.item_id for e in done.events} == {case.questions[0].question_id, "d.x.goal"}


def test_what_the_producer_did_not_evaluate_is_not_taken_back(storage: LocalStorage, world: World) -> None:
    kept = _q("AS_OF_COLUMN", ["a"], producer="rules")
    case = _case(storage, world).model_copy(
        update={
            "questions": (kept, _q("OTHER", ["a"], producer="rules")),
            "doubts": (_doubt("consent_missing").model_copy(update={"producer": "rules"}),),
        }
    )
    done = reconcile(
        case,
        Emission(producer="rules", complete=True, unevaluated=("AS_OF_COLUMN", "d.consent_missing.")),
        now=NOW,
    )
    assert [q.code for q in done.questions] == ["AS_OF_COLUMN"]
    assert done.doubts[0].status == "open"


def test_the_goal_rules_keep_the_consent_doubt_while_the_outcome_is_undecided(
    storage: LocalStorage, world: World
) -> None:
    case = _goal_first(storage, world)
    gone = {d.doubt_id for d in case.doubts if d.status == "resolved"}
    assert not [d for d in gone if d.startswith("d.consent_missing")]


def test_a_question_taken_back_and_asked_again_counts_once_for_the_file(
    storage: LocalStorage, world: World
) -> None:
    case = _goal_first(storage, world)
    counted = dict(case.counters.questions_per_file)
    case = _chat(
        storage,
        world,
        case,
        po._sql_item(world, ["customer_id", "ordered_again_90d"], two_valued=["ordered_again_90d"]),
    )
    case = _chat(storage, world, case, text="Again.")
    assert all(case.counters.questions_per_file.get(k, 0) <= v + 1 for k, v in counted.items())


def test_the_other_review_proposal_stays_when_the_person_ticks_one(
    storage: LocalStorage, world: World
) -> None:
    a, b = _ai(_fix("age", "fill_nulls")), _ai(_fix("income", "fill_nulls"))
    seen: list[int] = []

    def review(_case: Case, _files: Any, _brief: Any) -> visit.Detected:
        seen.append(1)
        return (
            visit.Detected(items=(a, b), model_calls=1) if len(seen) == 1 else visit.Detected(model_calls=1)
        )

    deps = deps_for(storage, world, agent=review)
    case = visit.resume(CLIENT, open_case(storage, world).case_id, deps, actor=ANALYST)
    assert {a.item_id, b.item_id} <= {i.item_id for i in case.items if i.state is ProposalState.PENDING}
    case = visit.decide(
        CLIENT, case.case_id, a.item_id, True, deps, expected_version=case.version, actor=ANALYST
    )
    assert len(seen) >= 2, "a tick is a new plan for the review"
    by_id = {i.item_id: i for i in case.items}
    assert by_id[a.item_id].state is ProposalState.ACCEPTED
    assert by_id[b.item_id].state is ProposalState.PENDING and not by_id[b.item_id].withdrawn_because


def test_ticking_the_old_query_after_its_swap_leaves_the_swap_out(
    storage: LocalStorage, world: World
) -> None:
    case = visit.resume(CLIENT, open_case(storage, world).case_id, po._deps(storage, world), actor=ANALYST)
    first = _ai(_query("SELECT customer_id, 1 AS y FROM invoices"))
    case = _chat(storage, world, case, first)
    case = _decide(storage, world, case, first.item_id)
    second = _ai(_query("SELECT customer_id, 2 AS y FROM invoices"))
    case = _chat(storage, world, case, second)
    case = _decide(storage, world, case, second.item_id)
    case = _decide(storage, world, case, first.item_id)
    live = [i.item_id for i in case.items if i.kind == "recipe_step" and i.state is ProposalState.ACCEPTED]
    assert live == [first.item_id]


def test_confirming_a_swap_leaves_the_old_query_out(storage: LocalStorage, world: World) -> None:
    from engine.access.roles import Principal, Role
    from engine.doctor.protocol import item_sentence

    case = visit.resume(CLIENT, open_case(storage, world).case_id, po._deps(storage, world), actor=ANALYST)
    first = _ai(_query("SELECT customer_id, 1 AS y FROM invoices"))
    case = _chat(storage, world, case, first)
    case = _decide(storage, world, case, first.item_id)
    second = _ai(_query("SELECT customer_id, 2 AS y FROM invoices"))
    case = _chat(storage, world, case, second)
    swap = next(i for i in case.items if i.item_id == second.item_id)
    assert swap.replaces == first.item_id
    meera = Principal(user_id="u-meera", username="meera", roles=frozenset({Role.ANALYST}), kind="user")
    case = visit.confirm(
        CLIENT,
        case.case_id,
        second.item_id,
        po._deps(storage, world),
        principal=meera,
        sentence=item_sentence(swap.title, swap.payload),
        expected_version=case.version,
    )
    by_id = {i.item_id: i for i in case.items}
    assert by_id[second.item_id].state is ProposalState.ACCEPTED
    assert by_id[first.item_id].state is ProposalState.REJECTED


def test_an_answer_given_again_leaves_the_change_of_the_earlier_one_out() -> None:
    from engine.doctor.swaps import leave_out_of_answer

    old = _ai(_fix("age", "fill_nulls")).model_copy(
        update={"item_id": "q.outcome:c0", "state": ProposalState.ACCEPTED}
    )
    other = _ai(_fix("income", "fill_nulls"))
    done = leave_out_of_answer([old, other], "q.outcome", "q.outcome:c1", now=NOW)
    assert done[0].state is ProposalState.REJECTED and done[0].superseded_by == "q.outcome:c1"
    assert done[0].withdrawn_because and done[1] is other


def test_the_chat_does_not_blame_the_person_for_a_change_the_doctor_withdrew(
    storage: LocalStorage, world: World
) -> None:
    case = visit.resume(CLIENT, open_case(storage, world).case_id, po._deps(storage, world), actor=ANALYST)
    first = _ai(_query("SELECT customer_id, 1 AS y FROM invoices"))
    case = _chat(storage, world, case, first)
    second = _ai(_query("SELECT customer_id, 2 AS y FROM invoices"))
    case = _chat(storage, world, case, second)  # `first` is superseded by the Doctor
    case = _chat(storage, world, case, first)
    assert "left out earlier" not in case.chat[-1].text


def test_the_rules_run_again_when_the_plan_changes(
    storage: LocalStorage, world: World, monkeypatch: Any
) -> None:
    runs: list[int] = []
    real = visit._goal_rules

    def spy(run: Any, case: Case, brief: Any, step: Any) -> None:
        runs.append(len(case.items))
        real(run, case, brief, step)

    monkeypatch.setattr(visit, "_goal_rules", spy)
    case = _goal_first(storage, world)
    before = len(runs)
    case = _chat(storage, world, case, _ai(_query("SELECT 1")))
    after_chat = len(runs)
    item = next(i for i in case.items if item_subject(i) == "sql@customer_training")
    _decide(storage, world, case, item.item_id)
    assert after_chat > before and len(runs) > after_chat, "a tick that changes what the rules see runs them"


def test_emissions_from_different_producers_give_the_same_plan_in_any_order(
    storage: LocalStorage, world: World
) -> None:
    case = _case(storage, world)
    emissions = [
        Emission(producer="chat", items=[_ai(_query("SELECT 1"))]),
        Emission(producer="review", items=[_ai(_query("SELECT 2"))], complete=True),
        Emission(producer="detectors", items=[_ai(_query("SELECT 3", name="other"))]),
    ]
    shapes = []
    for order in itertools.permutations(emissions):
        work = None
        for e in order:
            work = reconcile(case, e, now=NOW, before=work)
        assert work is not None
        shapes.append(sorted((item_subject(i), i.state.value) for i in work.items))
    assert all(s == shapes[0] for s in shapes)
