"""What the Data Doctor's AI is told, and refused, in plain and checkable words (clean run 3, the AI side).

The chosen goal is named with what a yes means; the build's parameters are in the state; the next AI turn is told to
propose a missing build; a refusal names what tripped it and what to do; the AI may repeat the settings of its own
accepted changes and number its lists, but never state a number about the data that nobody measured; the state it
reads is true (who decided, how many blocking questions are left, what an option adds, what a receipt counted on);
a review whose state has not changed since it last finished is not asked again.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from engine.agent.contracts import ProposalSource, ProposalState
from engine.agent.grounding import grounded_numbers, ungrounded_numbers
from engine.doctor import visit
from engine.doctor.actions import prompt_variables
from engine.doctor.agent_review import own_numbers
from engine.doctor.build_facts import build_is_owed, goal_line
from engine.doctor.case import Case, CaseStore
from engine.doctor.checks_time_keys import key_quality
from engine.doctor.guards import make_receipt
from engine.doctor.limits import limits
from engine.doctor.visit_contracts import CaseVisit
from engine.storage import LocalStorage
from tests.fixtures.doctor_eval.chat_model import ChatScript
from tests.unit.doctor import chat_support as cs
from tests.unit.doctor import test_agent_review as ar
from tests.unit.doctor import visit_support as vs
from tests.unit.doctor.chat_support import Talk
from tests.unit.doctor.visit_support import GOAL_A, World


@pytest.fixture
def world() -> World:
    return cs.orders_world()


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path / "data")


def reply(text: str) -> dict[str, object]:
    return {"action": "reply", "text": text}


# ---------------------------------------------------------------------------
# Numbers: a list is not a measurement, the AI's own accepted settings are its to repeat
# ---------------------------------------------------------------------------
def test_list_numbers_are_not_claims_but_a_number_about_the_data_still_is() -> None:
    known = grounded_numbers([{"customers": 1134}])
    numbered = "Four things are left: (1) the key, (2) the date,\n3. the budget, and 4) the build."
    assert ungrounded_numbers(numbered, known) == []
    assert ungrounded_numbers("About 1,134 customers (2) and 90% of them ordered (3)", known) == ["90%"]
    assert ungrounded_numbers("Lost (35) customers", known) == ["35"], "past a list's length it is a number"


def test_the_numbers_of_an_accepted_change_are_the_ais_own_settings_but_its_query_text_is_not() -> None:
    args = {
        "sql": "SELECT 1 FROM t WHERE x > 777",
        "description": "Next 555 days",
        "outcome_window_days": 90,
        "as_of": "2011-09-01",
        "expect": {"max_keys_lost_pct": 5, "keys_unique": True, "rows_out_equals": 4372},
    }
    mine = grounded_numbers(own_numbers(args))
    assert ungrounded_numbers("The window is 90 days, cut at 2011-09-01.", mine) == []
    assert ungrounded_numbers("The build makes 4,372 customers, at most 5% lost.", mine) == [
        "4,372",
        "5%",
    ], "an `expect` bound is a prediction about the data, not a setting"
    assert ungrounded_numbers("It keeps 777 and 555 customers.", mine) == ["777", "555"]


# ---------------------------------------------------------------------------
# The state
# ---------------------------------------------------------------------------
def test_the_chat_prompt_names_the_chosen_goal_and_what_a_yes_means(
    storage: LocalStorage, world: World
) -> None:
    client = ChatScript({"hello": [reply("Hi.")]})
    talk = Talk(storage, world, client)
    talk.goal()
    talk.say("hello")
    prompt = client.calls[-1].system
    brief = visit.stored_brief(storage, talk.case)
    assert brief is not None
    assert goal_line(brief, "x") in prompt, "the system text carries the goal and the definition of a yes"
    assert "see what these files can be used for" not in prompt
    chosen = client.seen[-1].state["chosen_goal"]
    assert chosen["goal"] == brief.goal_name and chosen["a_yes_means"] == brief.outcome.definition


def test_the_state_counts_the_blocking_room_left_and_says_what_an_option_adds(
    storage: LocalStorage, world: World
) -> None:
    client = ChatScript({"hello": [reply("Hi.")]})
    talk = Talk(storage, world, client, key=None)
    talk.say("hello")
    state = client.seen[-1].state
    waiting = len(visit.open_blocking(talk.case))
    assert waiting > 0
    assert state["left"]["blocking_questions"] == limits(None).asks.blocking - waiting
    key = vs.question(talk.case, "KEY_AMBIGUOUS")
    talk.answer(key.question_id, vs.option(key, "customer_id"))
    talk.say("hello again")
    room = client.seen[-1].state["left"]["blocking_questions"]
    assert (
        room
        == limits(None).asks.blocking - len(visit.open_blocking(talk.case))
        > state["left"]["blocking_questions"]
    )


def test_a_change_the_rules_made_and_the_visit_left_out_is_not_the_ais_decision(
    storage: LocalStorage, world: World
) -> None:
    client = ChatScript({"hello": [reply("Hi.")]})
    talk = Talk(storage, world, client)
    first = talk.case.items[0]
    assert first.source is ProposalSource.RULES
    left_out = first.model_copy(update={"state": ProposalState.REJECTED, "decided_by": visit.AGENT})
    items = (left_out, *talk.case.items[1:])
    CaseStore(storage).update(
        vs.CLIENT,
        talk.case.case_id,
        expected_version=talk.case.version,
        actor=vs.ANALYST,
        change=lambda c: (c.model_copy(update={"items": items}), ()),
    )
    talk.case = CaseStore(storage).get(vs.CLIENT, talk.case.case_id)
    talk.say("hello")
    plan = client.seen[-1].state["plan"]
    assert next(p for p in plan if p["what"].startswith(first.title[:40]))["decided_by"] == "the rules"


def test_a_buildable_goal_with_no_build_is_owed_until_a_query_waits_or_is_planned(
    storage: LocalStorage, world: World
) -> None:
    talk = Talk(storage, world, ChatScript({}))
    talk.goal()
    buildable = {"use_cases": [{"use_case_id": GOAL_A, "status": "buildable"}]}
    case: Case = talk.case.model_copy(update={"diagnosis": buildable})
    assert build_is_owed(case)
    assert not build_is_owed(talk.case), "a goal that is not buildable has no build to propose"


# ---------------------------------------------------------------------------
# Refusals that let the AI recover
# ---------------------------------------------------------------------------
def test_a_refused_quick_question_names_the_word_and_a_question_about_rows_is_allowed(
    storage: LocalStorage, world: World
) -> None:
    client = ChatScript(
        {
            "ask me": [
                {"action": "ask", "args": {"text": "Is it a purchase?", "options": ["Yes", "No"]}},
                {
                    "action": "ask",
                    "args": {
                        "text": "Some rows have a negative quantity. Are they cancellations of earlier purchases?",
                        "options": ["Cancellations", "Mistakes", "I do not know"],
                    },
                },
                reply("Asked."),
            ]
        }
    )
    talk = Talk(storage, world, client)
    case = talk.say("ask me what you need")
    refused = next(r for r in client.seen[1].results if r.get("error") == "AGENT_QUESTION_REFUSED")
    assert "'purchase'" in refused["message"] and "question list" in refused["message"]
    asked = next(q for q in case.questions if q.code == "CHAT_ASK")
    assert [o.label for o in asked.options] == [
        "Cancellations",
        "Mistakes",
        "I don't know",
    ], "one way to not know"


def test_the_action_contract_states_the_limits_of_a_goal_reason_and_a_quick_question() -> None:
    lines = " ".join(prompt_variables("chat")["actions"])
    effect = limits(None).turn.effect_chars
    assert f"at most {effect} characters" in lines
    assert "{effect_chars}" not in lines and "{short_chars}" not in lines


# ---------------------------------------------------------------------------
# Counting customers: a rollup to one row per customer is not a loss
# ---------------------------------------------------------------------------
def test_a_result_that_names_the_id_differently_still_counts_customers_not_rows() -> None:
    base = pd.DataFrame({"CustomerID": [1.0, 1.0, 2.0, 3.0, 3.0, 3.0], "Quantity": [1, 2, 3, 4, 5, 6]})
    out = pd.DataFrame({"customer_id": [1, 2, 3], "orders": [2, 1, 3]})
    receipt = make_receipt(1, "sql_query", {"t": base}, out, key="CustomerID")
    assert (receipt.keys_in, receipt.keys_out, receipt.keys_lost) == (3, 3, 0)
    assert receipt.key_in_result == "customer_id" and receipt.lost == 0
    assert "customer_id" in receipt.counted_by and "CustomerID" in receipt.counted_by


def test_a_result_without_the_id_is_not_scored_as_loss_and_says_what_to_keep() -> None:
    from engine.doctor.guards import receipt_findings

    base = pd.DataFrame({"CustomerID": [1, 1, 2, 3, 3, 3], "Quantity": [1, 2, 3, 4, 5, 6]})
    out = pd.DataFrame({"who": ["a", "b", "c"], "orders": [2, 1, 3]})  # a count that repeats ids' values
    receipt = make_receipt(1, "sql_query", {"t": base}, out, key="CustomerID")
    assert receipt.keys_in is None and receipt.key_missing == "CustomerID"
    (finding,) = [f for f in receipt_findings(receipt) if f.code == "STEP_NOT_COUNTED"]
    assert "Keep CustomerID" in finding.message


def test_the_key_report_reads_in_plain_words() -> None:
    check = next(
        c for c in key_quality(pd.Series(["a", "a", "b"] * 20), "CustomerID") if c.code == "KEY_REPORT"
    )
    assert "different from each other" not in check.text
    assert check.text.startswith("'CustomerID' has 2 different values in 60 filled rows")


# ---------------------------------------------------------------------------
# The review: receipts in the state, and no second call on a state it finished on
# ---------------------------------------------------------------------------
class Spy(ar.Script):
    """The scripted review model that keeps the prompts it was sent."""

    def __init__(self, actions: Any) -> None:
        super().__init__(actions)
        self.users: list[str] = []

    def _body(self, system: str, user: str) -> str:
        self.users.append(user)
        return super()._body(system, user)


def test_the_review_reads_the_receipt_of_its_plan_and_is_not_asked_again_on_a_state_it_finished_on(
    storage: LocalStorage,
) -> None:
    world = vs.world()
    client = Spy(
        [
            ar._sql({"keys_unique": True, "max_keys_lost_pct": 0}),
            {"action": "finish", "text": "I added up the orders for each customer."},
        ]
    )
    case = vs.open_case(storage, world)
    files = ar._files(world)
    first = ar._review(ar._guard(client))(case, files, None)
    assert first.state_digest and first.model_calls == 2
    # the same query now sits in the plan: the next review reads its receipt, with what it was counted on
    planned = [i.model_copy(update={"state": ProposalState.PENDING}) for i in first.items]
    case = case.model_copy(update={"items": (*case.items, *planned)})
    again = ar._review(ar._guard(client))(case, files, None)
    state = json.loads(client.users[-1].split("State of this case")[1].split("\n", 1)[1].split("\n\n")[0])
    receipt = next(p["receipt"] for p in state["plan_so_far"] if "receipt" in p)
    assert receipt["rows_made"] == 1_200 and receipt["customers_lost"] == 0
    assert receipt["counted_on"] == "the whole file" and again.state_digest != first.state_digest
    # a tick that changes nothing it reads: same state, same digest, no call
    ticked = case.model_copy(update={"visit": CaseVisit(done={"agent_state": again.state_digest})})
    calls = len(client.users)
    quiet = ar._review(ar._guard(client))(ticked, files, None)
    assert quiet.unchanged and quiet.model_calls == 0 and len(client.users) == calls


def test_a_result_without_the_id_still_counts_the_rows_it_drops() -> None:
    base = pd.DataFrame({"CustomerID": list(range(100)), "Quantity": list(range(100))})
    out = pd.DataFrame({"Quantity": list(range(6))})
    receipt = make_receipt(1, "sql_query", {"t": base}, out, key="CustomerID")
    assert receipt.key_missing == "CustomerID" and receipt.lost == 94
