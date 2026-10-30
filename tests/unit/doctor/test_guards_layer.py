"""The guard layer (`engine.doctor.guards_layer`): each code refuses what must never happen, with the name it is about
and a fix that is never empty; the allowed twin of each goes through."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from engine.agent.contracts import AgentConfidence, ProposalState
from engine.doctor.agent_apply import Roles
from engine.doctor.author import (
    ApplyContext,
    AskPerson,
    AuthorAction,
    Create,
    ItemDraft,
    Withdraw,
    apply_actions,
)
from engine.doctor.case import Case
from engine.doctor.guards_layer import HOW_TO_FIX, check_action
from engine.doctor.protocol import Ask, AskOption
from engine.doctor.visit_contracts import OptionEffect
from tests.unit.doctor.test_author import (
    FILE,
    PERSON,
    Window,
    ai,
    create,
    ctx,
    make_case,
    setting,
    step,
    stored,
    waiting,
)


def with_roles(roles: Roles, **more: Any) -> ApplyContext:
    return replace(ai(**more), roles=roles)


def refused(case: Case, action: AuthorAction, context: ApplyContext) -> Any:
    """The refusal of `action`, after checking that it names a fix."""
    refusal = check_action(case, action, context)
    assert refusal is not None and refusal.how_to_fix and refusal.what_failed
    return refusal


def a_draft(kind: str, payload: dict[str, Any], title: str = "a change") -> ItemDraft:
    return ItemDraft(
        kind=kind, payload=payload, title=title, upload_id=FILE, confidence=AgentConfidence.CHECK
    )


def test_every_code_of_the_layer_has_a_default_fix() -> None:
    assert set(HOW_TO_FIX) == {
        "ITEM_NOT_FOUND",
        "APPROVAL_IS_PERSONS",
        "OUTCOME_IS_PERSONS",
        "FEATURE_IS_ID",
        "STEP_ON_ROLE_COLUMN",
        "SETTING_VALUE_NOT_ALLOWED",
        "LEFT_OUT_BY_PERSON",
    }
    assert all(HOW_TO_FIX.values())


def test_item_not_found_names_the_handle_and_lists_the_valid_ones() -> None:
    there = waiting()
    case = make_case([there])
    gone = refused(case, Withdraw(item_id="nope", reason="r", consequence="c"), ai())
    assert gone.code == "ITEM_NOT_FOUND" and gone.value == "nope" and there.item_id in gone.how_to_fix
    swap = refused(case, create("parse_date", "ship", replaces="nope"), ai())
    assert swap.code == "ITEM_NOT_FOUND" and swap.value == "nope"
    assert check_action(case, Withdraw(item_id=there.item_id, reason="r", consequence="c"), ai()) is None


def test_item_not_found_when_the_item_is_in_another_goals_section() -> None:
    other = waiting().model_copy(update={"goal_id": "other"})
    case = make_case([other], goal="g")
    assert refused(case, Withdraw(item_id=other.item_id, reason="r", consequence="c"), ai()).code == (
        "ITEM_NOT_FOUND"
    )
    base = waiting()
    assert (
        check_action(
            make_case([base], goal="g"), Withdraw(item_id=base.item_id, reason="r", consequence="c"), ai()
        )
        is None
    )


def test_a_proposal_that_carries_a_decision_is_refused() -> None:
    payload = step("parse_number")
    decided = Create(draft=a_draft("recipe_step", {**payload, "decided_by": PERSON}), reason="r", basis="r1")
    refusal = refused(make_case(), decided, ai())
    assert refusal.code == "APPROVAL_IS_PERSONS" and refusal.value == "decided_by"
    forged = {**payload, "step": {**payload["step"], "decided_by": "user"}}
    assert refused(
        make_case(), Create(draft=a_draft("recipe_step", forged), reason="r", basis="r1"), ai()
    ).value == ("step.decided_by")
    agent = {**payload, "step": {**payload["step"], "decided_by": "agent"}}
    assert (
        check_action(make_case(), Create(draft=a_draft("recipe_step", agent), reason="r", basis="r1"), ai())
        is None
    )


def test_the_ai_does_not_set_the_outcome_but_a_rules_suggestion_it_adopts_may_be_proposed() -> None:
    outcome = a_draft("outcome_column", {"upload_id": FILE, "column": "Country"})
    own = refused(make_case(), Create(draft=outcome, reason="r", basis="msg:1"), ai())
    assert own.code == "OUTCOME_IS_PERSONS" and own.value == "Country"
    suggested = Create(draft=outcome, reason="r", basis="s:abc")
    assert check_action(make_case(), suggested, ai(suggested=frozenset({"abc"}))) is None
    made_up = refused(make_case(), Create(draft=outcome, reason="r", basis="s:made_up"), ai())
    assert made_up.code == "OUTCOME_IS_PERSONS", "a suggestion id the turn did not hold is the AI's own word"
    assert (
        check_action(make_case(), Create(draft=outcome, reason="r", basis="r1"), ctx()) is None
    ), "a rule's own"


def test_a_question_of_the_ai_does_not_answer_itself_with_the_outcome() -> None:
    effect = OptionEffect(option_id="c0", kind="outcome_column", payload={"column": "Country"})
    option = AskOption(label="Country", effect=effect)

    def question(proven: str | None) -> AskPerson:
        return AskPerson(
            ask=Ask(code="Q", subject={}, text="t", why="w", options=(option,), rows=1, proven=proven)
        )

    assert refused(make_case(), question("c0"), ai()).code == "OUTCOME_IS_PERSONS"
    assert check_action(make_case(), question(None), ai()) is None


def test_the_customer_id_is_not_a_feature() -> None:
    roles = Roles(ids=frozenset({"customer_id"}))
    as_feature = a_draft("recipe_step", {"features": [{"name": "recency_days"}, {"name": "Customer ID"}]})
    refusal = refused(make_case(), Create(draft=as_feature, reason="r", basis="r1"), with_roles(roles))
    assert refusal.code == "FEATURE_IS_ID" and refusal.value == "Customer ID"
    fine = a_draft("recipe_step", {"features": [{"name": "recency_days"}, {"name": "orders_90d"}]})
    assert check_action(make_case(), Create(draft=fine, reason="r", basis="r1"), with_roles(roles)) is None
    key_setting = setting("customer_id")
    assert refused(make_case(goal="g"), key_setting, with_roles(roles, goal=Window())).code == "FEATURE_IS_ID"


def test_a_step_on_a_role_column_that_would_skip_the_tick_is_refused() -> None:
    roles = Roles(protected=frozenset({"outcome"}))
    build = {**step("rename_column", "outcome"), "buildable": {"outcome": "x"}, "needs_yes": True}
    skipping = Create(draft=a_draft("recipe_step", build), reason="r", basis="r1")
    refusal = refused(make_case(), skipping, with_roles(roles))
    assert refusal.code == "STEP_ON_ROLE_COLUMN" and refusal.value == "outcome"
    waits = Create(draft=a_draft("recipe_step", step("rename_column", "outcome")), reason="r", basis="r1")
    assert (
        check_action(make_case(), waits, with_roles(roles)) is None
    ), "it waits for the tick, which is allowed"
    other = {**build, "step": {**build["step"], "column": "spend"}}
    assert (
        check_action(
            make_case(),
            Create(draft=a_draft("recipe_step", other), reason="r", basis="r1"),
            with_roles(roles),
        )
        is None
    )


def test_a_setting_value_outside_what_the_goal_allows_is_refused() -> None:
    refusal = refused(make_case(goal="g"), setting("45"), ai(goal=Window()))
    assert refusal.code == "SETTING_VALUE_NOT_ALLOWED" and refusal.value == "window_days"
    assert "30, 60, 90" in refusal.how_to_fix
    assert check_action(make_case(goal="g"), setting("60"), ai(goal=Window())) is None


def test_what_the_person_left_out_is_not_proposed_again_unless_it_is_different() -> None:
    no = stored("parse_number", "spend", state=ProposalState.REJECTED, decided_by=PERSON)
    refusal = refused(make_case([no]), create("parse_number", "spend", reason="in other words"), ai())
    assert refusal.code == "LEFT_OUT_BY_PERSON" and refusal.value == no.item_id
    assert check_action(make_case([no]), create("parse_number", "other_column"), ai()) is None


def test_a_setting_the_person_left_out_is_not_proposed_again() -> None:
    first = apply_actions(make_case(goal="g"), [setting("90")], ai(goal=Window())).case
    [ninety] = first.items
    said_no = ninety.model_copy(update={"state": ProposalState.REJECTED, "decided_by": PERSON})
    case = first.model_copy(update={"items": (said_no,)})
    assert refused(case, setting("90"), ai(goal=Window())).code == "LEFT_OUT_BY_PERSON"
    assert check_action(case, setting("60"), ai(goal=Window())) is None


def test_a_refused_action_is_not_applied_and_the_next_one_still_runs() -> None:
    outcome = Create(
        draft=a_draft("outcome_column", {"upload_id": FILE, "column": "Country"}), reason="r", basis="msg:1"
    )
    out = apply_actions(make_case(), [outcome, create("parse_date", "date")], ai())
    assert [(i, r.code) for i, r in out.refused] == [(0, "OUTCOME_IS_PERSONS")]
    assert [i.kind for i in out.case.items] == ["recipe_step"]
