"""The Data Doctor's chat: one conversation that looks, changes the working copy, asks, suggests and replies.

The owner's example runs end to end with a turn-keyed scripted model: the person says what counts as a sale, and the
agent asks it as a question and suggests the answer (never recording it); the person says to drop the test accounts,
and the agent applies a filter for them with the counts; the person changes their mind, and the agent takes its own
filter back and applies the new one. Around it: what the agent may apply and what waits for a tick, what it can take
back (only its own), what it sees (the conversation, the case, its earlier looks), what it is held to (steps, numbers,
the files' own gates, the case's spending), and that without an AI service the chat is unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from engine.agent.contracts import ProposalSource, ProposalState
from engine.agent.loop import UNAVAILABLE
from engine.doctor import agent_chat, visit
from engine.doctor.case import Case, CaseChatEntry, TimelineKind, VisitState
from engine.doctor.config import AiLimitsConfig
from engine.doctor.limits import limits
from engine.doctor.visit import AGENT, AI_NEEDED_REPLY
from engine.generative.errors import BUDGET_EXCEEDED
from engine.llm import AwsSignInExpiredError, LLMError
from engine.llm_http import PROBLEMS, LLMHttpError
from engine.storage import LocalStorage
from tests.fixtures.doctor_eval.chat_model import ChatScript, Seen
from tests.unit.doctor import chat_support as cs
from tests.unit.doctor import visit_support as vs
from tests.unit.doctor.chat_support import Down, Talk, ai_items
from tests.unit.doctor.visit_support import ANALYST, World

SALE = "a sale only counts if the order was over 500"
TEST_ACCOUNTS = "drop test accounts, ids start with 999"
THIS_YEAR = "actually only this year's orders"


@pytest.fixture
def world() -> World:
    return cs.orders_world()


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path / "data")


def look(tool: str, column: str | None = None, **more: object) -> dict[str, object]:
    return {"action": tool, "file": "F1", "args": {**({"column": column} if column else {}), **more}}


def rows_step(column: str, operator: str, *values: str, replaces: str | None = None) -> object:
    """A `remove_rows` proposal citing the look just made."""

    def step(seen: Seen) -> dict[str, object]:
        return {
            "action": "propose_step",
            "file": "F1",
            "args": {
                "kind": "remove_rows",
                "column": column,
                "params": {"operator": operator, "values": list(values)},
                "reason": "You asked for this.",
                "evidence_ids": [seen.last("evidence_id")],
                **({"replaces": replaces} if replaces else {}),
            },
        }

    return step


def reply(text: str) -> dict[str, object]:
    return {"action": "reply", "text": text}


def handle_of(seen: Seen, containing: str) -> str:
    """The handle the case gave a change (`c3`), found by what it says."""
    return next(p["id"] for p in seen.state["plan"] if containing in p["what"])


def asked_sale(seen: Seen) -> dict[str, object]:
    return {"action": "suggest_answer", "args": {"question_id": seen.last("question_id"), "option_id": "c0"}}


SCRIPT: dict[str, list[object]] = {
    SALE: [
        look("describe_numbers", "order_amount"),
        {
            "action": "ask",
            "file": "F1",
            "args": {
                "question": "Q24",
                "values": {"file": "orders.csv"},
                "choices": ["An order over 500 is a sale", "Any order is a sale"],
            },
        },
        asked_sale,
        reply("I understood: a sale is an order over 500. Use this?"),
    ],
    TEST_ACCOUNTS: [
        look("find_values", "customer_id", contains="999"),
        rows_step("customer_id", "starts_with", "999"),
        reply("Done: I left out the test accounts whose id starts with 999. You can undo it."),
    ],
    THIS_YEAR: [
        look("describe_dates", "order_date"),
        lambda seen: rows_step(
            "order_date", "before", "2026-01-01", replaces=handle_of(seen, "starts with 999")
        )(seen),
        reply("Understood: I took my earlier filter back and now only this year's orders stay."),
    ],
}


# ---------------------------------------------------------------------------
# The owner's example conversation
# ---------------------------------------------------------------------------
def test_the_owners_conversation_suggests_an_answer_applies_a_filter_and_revises_it(
    storage: LocalStorage, world: World
) -> None:
    client = ChatScript(SCRIPT)
    talk = Talk(storage, world, client)

    # (1) What counts as a sale is the person's: the agent asks it, suggests the answer, and records nothing.
    case = talk.say(SALE)
    sale = next(q for q in case.questions if q.code == "Q24_WHAT_COUNTS")
    assert sale.answer is None and not sale.blocking, "meaning: only the person's click answers it"
    assert "does not change what the model learns" in sale.why
    assert [o.label for o in sale.options][:2] == ["An order over 500 is a sale", "Any order is a sale"]
    suggestion = talk.reply.suggestion
    assert suggestion is not None
    assert (suggestion.question_id, suggestion.option_id) == (sale.question_id, "c0")
    assert suggestion.label == "An order over 500 is a sale"
    assert talk.reply.tools == ("describe_numbers", "ask", "suggest_answer")
    assert not ai_items(case), "nothing was changed in the plan for a question of meaning"
    sent = {item["tool"] for item in talk.reply.sent}
    assert {
        "get_profile",
        "advisor_state",
        "describe_numbers",
    } <= sent, "the screen can say what the AI was shown"

    # (2) Test accounts: a typed filter the guards pass is applied for the person, with its counts.
    case = talk.say(TEST_ACCOUNTS)
    (first,) = ai_items(case)
    assert first.state is ProposalState.ACCEPTED and first.decided_by == AGENT, "applied by the agent"
    assert (
        first.source is ProposalSource.AI
        and first.title == "Leave out rows where 'customer_id' starts with 999"
    )
    assert talk.reply.item_ids == (first.item_id,) and talk.reply.tools == ("find_values", "propose_step")
    assert first.payload["tried"] == {"rows": 1000, "changed": 31, "failed": 0}, "the card has its counts"
    decided = [
        e for e in talk.timeline() if e.kind is TimelineKind.ITEM_DECIDED and e.item_id == first.item_id
    ]
    assert decided and decided[-1].details == {"state": "accepted", "by": "agent"}

    # (3) The person changes their mind: the agent withdraws its own filter and applies the new one.
    case = talk.say(THIS_YEAR)
    by_id = {i.item_id: i for i in ai_items(case)}
    old, new = by_id[first.item_id], next(i for i in by_id.values() if i.item_id != first.item_id)
    assert old.state is ProposalState.REJECTED and old.decided_by == AGENT, "withdrawn"
    assert new.state is ProposalState.ACCEPTED and new.decided_by == AGENT, "applied"
    assert talk.reply.withdrawn == (old.item_id,) and talk.reply.item_ids == (new.item_id,)
    withdrawn = [
        e for e in talk.timeline() if e.kind is TimelineKind.ITEM_DECIDED and e.item_id == old.item_id
    ]
    assert withdrawn[-1].details == {"state": "rejected", "by": "agent", "withdrawn": True}

    # Whatever the agent did, the approval still asks the person: leaving rows out needs their own yes.
    assert case.state in (VisitState.ASK, VisitState.APPROVE)
    assert visit.unconfirmed(case) == (new,)
    assert client.asked == 4 + 3 + 3


def test_the_person_can_undo_what_the_agent_applied_and_it_is_not_put_back(
    storage: LocalStorage, world: World
) -> None:
    client = ChatScript(
        {
            **SCRIPT,
            "do it again": [
                look("find_values", "customer_id", contains="999"),
                rows_step("customer_id", "starts_with", "999"),
                reply("Done, the test accounts are out again."),
            ],
        }
    )
    talk = Talk(storage, world, client)
    case = talk.say(TEST_ACCOUNTS)
    (applied,) = ai_items(case)
    case = talk.decide(applied.item_id, False)  # Undo
    undone = ai_items(case)[0]
    assert undone.state is ProposalState.REJECTED and undone.decided_by == ANALYST

    case = talk.say("please do it again")
    assert ai_items(case)[0].state is ProposalState.REJECTED, "the person's undo stands"
    assert (
        "left out earlier; tick it again if you want it" in talk.reply.text
    ), "and the reply does not claim it"
    assert talk.reply.item_ids == (), "no change is attributed to this reply"


def test_the_agent_cannot_take_back_a_change_the_person_ticked_themselves(
    storage: LocalStorage, world: World
) -> None:
    def withdraw_it(seen: Seen) -> dict[str, object]:
        return {"action": "withdraw", "args": {"item_id": handle_of(seen, "starts with 999")}}

    client = ChatScript(
        {
            **SCRIPT,
            "take it back": [withdraw_it, reply("I could not take that back; it is yours to untick.")],
        }
    )
    talk = Talk(storage, world, client)
    case = talk.say(TEST_ACCOUNTS)
    (item,) = ai_items(case)
    case = talk.decide(item.item_id, False)
    case = talk.decide(item.item_id, True)  # the person ticks it themselves
    assert ai_items(case)[0].decided_by == ANALYST

    case = talk.say("take it back")
    assert ai_items(case)[0].state is ProposalState.ACCEPTED, "the person's tick stands"
    assert talk.reply.withdrawn == ()
    assert any(r.get("error") == "AGENT_ITEM_PERSONS" for r in client.seen[-1].results)


def test_without_an_ai_service_the_chat_says_so_and_nothing_else_changes(
    storage: LocalStorage, world: World
) -> None:
    talk = Talk(storage, world, None)
    before = talk.case
    case = talk.say(TEST_ACCOUNTS)
    assert case.chat[-1].text == AI_NEEDED_REPLY and case.chat[-1].role == "agent"
    assert case.chat[-1].item_ids == () and case.chat[-1].withdrawn == () and case.chat[-1].suggestion is None
    assert case.items == before.items and case.questions == before.questions and case.doubts == before.doubts
    assert case.counters.model_calls == before.counters.model_calls
    assert case.state is before.state
    dumped = json.loads(case.chat[-1].model_dump_json())
    assert set(dumped) == {
        "role",
        "text",
        "at",
        "by",
        "sent",
    }, "the new fields are left out of a dump while unset"


# ---------------------------------------------------------------------------
# What the agent may apply, and what waits for the person
# ---------------------------------------------------------------------------
def told(client: ChatScript, key: str = "change") -> dict[str, str]:
    """What the model was told about its change before it wrote the reply."""
    return next(r for r in client.seen[-1].results if key in r)


def test_a_filter_that_would_stop_the_plan_waits_for_the_persons_tick_and_the_model_is_told_why(
    storage: LocalStorage, world: World
) -> None:
    client = ChatScript(
        {
            "only recent": [
                look("describe_dates", "order_date"),
                rows_step("order_date", "before", "2026-02-20"),
                reply("That would leave out too many customers, so it waits for your tick."),
            ]
        }
    )
    talk = Talk(storage, world, client)
    case = talk.say("only recent orders please")
    (item,) = ai_items(case)
    assert item.state is ProposalState.PENDING and item.decided_by is None
    assert item.payload["tried"]["changed"] > 100
    assert "more than 5%" in told(client)["result"], "the stop's own sentence, with our count"
    assert told(client)["result"].startswith("It waits for the person's tick, because ")
    assert talk.reply.item_ids == (item.item_id,)


def test_a_change_that_touches_most_of_a_column_waits_for_the_persons_tick(
    storage: LocalStorage, world: World
) -> None:
    client = ChatScript(
        {
            "older": [
                look("describe_dates", "order_date"),
                rows_step("order_date", "before", "2026-05-01"),
                reply("That is a lot of rows, so it waits for your tick."),
            ]
        }
    )
    talk = Talk(storage, world, client)
    (item,) = ai_items(talk.say("drop the older orders"))
    assert item.state is ProposalState.PENDING and item.payload["warnings"]
    assert "changes many of the values" in told(client)["result"]


def test_a_change_of_meaning_waits_for_the_persons_tick_and_a_tidy_one_does_not(
    storage: LocalStorage, world: World
) -> None:
    """Merging spellings gives values a new meaning: pending whatever the guards say. Trimming spaces does not."""

    def step(kind: str, params: dict[str, object]) -> dict[str, object]:
        return {
            "action": "propose_step",
            "file": "F1",
            "args": {
                "kind": kind,
                "column": "channel",
                "params": params,
                "reason": "Spellings.",
                "evidence_ids": ["r1"],
            },
        }

    client = ChatScript(
        {
            "merge": [
                look("value_counts", "channel"),
                step("map_values", {"mapping": {"email ": "Email", "EMAIL": "Email"}}),
                step("normalise_text", {"strip": True}),
                reply("I tidied the spaces and left the merge for you."),
            ]
        }
    )
    talk = Talk(storage, world, client)
    case = talk.say("merge the channel spellings")
    by_kind = {i.payload["step"]["kind"]: i for i in ai_items(case)}
    assert by_kind["map_values"].state is ProposalState.PENDING, "a merge is the person's to tick"
    assert by_kind["normalise_text"].state is ProposalState.ACCEPTED, "trimming is applied for them"
    assert by_kind["normalise_text"].decided_by == AGENT


def test_the_outcome_never_chooses_which_rows_are_kept(storage: LocalStorage, world: World) -> None:
    client = ChatScript(
        {
            "converted": [
                look("describe_outcome", "converted"),
                rows_step("converted", "equals", "0"),
                reply("I cannot leave out rows by the outcome; I can look at it with you."),
            ]
        }
    )
    talk = Talk(storage, world, client)
    talk.goal()
    outcome = vs.question(talk.case, "OUTCOME_COLUMN")
    talk.answer(outcome.question_id, vs.option(outcome, "converted"))
    case = talk.say("remove the converted ones")
    assert not ai_items(case), "refused: the outcome is never used to choose rows"
    assert any(r.get("error") == "RECIPE_STEP_INVALID" for r in client.seen[-1].results)


def test_leaving_rows_out_stays_a_change_the_person_confirms_at_the_approval(
    storage: LocalStorage, world: World
) -> None:
    """The agent applies the filter; the approval still needs the person's own yes to it (APPROVE-class)."""
    talk = Talk(storage, world, ChatScript(SCRIPT))
    talk.say(TEST_ACCOUNTS)
    (applied,) = ai_items(talk.case)
    assert applied.state is ProposalState.ACCEPTED
    assert visit.unconfirmed(talk.case) == (applied,)


def test_the_chat_proposes_a_lesson_only_once_the_case_is_ready(storage: LocalStorage, world: World) -> None:
    """The chat runs in CONVERSE, so readiness is the case's verdict for the plan as it stands, not its state."""
    from engine.doctor.lessons import LessonStore

    lesson = "Ask which column is the outcome before building any model."
    propose = {"action": "propose_lesson", "args": {"text": lesson}}
    client = ChatScript({"early": [propose, reply("Noted.")], "later": [propose, reply("Noted.")]})
    talk = Talk(storage, world, client)
    talk.say("early")
    assert LessonStore(storage).all() == (), "no verdict yet: not ready"
    assert any("not yet" in str(r) for r in client.seen[-1].results)

    talk.goal()
    outcome = vs.question(talk.case, "OUTCOME_COLUMN")
    talk.answer(outcome.question_id, vs.option(outcome, "converted"))
    talk.case = visit.approve(
        cs.CLIENT, talk.case.case_id, talk.deps(), expected_version=talk.case.version, actor=ANALYST
    )
    assert talk.case.state is VisitState.FOLLOW_UP and talk.case.readiness
    talk.say("later")
    stored = LessonStore(storage).all()
    assert [x.text for x in stored] == [lesson] and stored[0].status == "proposed"
    assert stored[0].case_id == talk.case.case_id


# ---------------------------------------------------------------------------
# Asking, suggesting, doubting
# ---------------------------------------------------------------------------
def test_a_quick_question_has_the_models_own_options_and_no_numbers(
    storage: LocalStorage, world: World
) -> None:
    ask = {"text": "Should I keep the old orders?", "options": ["Keep them", "Leave them out"]}
    client = ChatScript(
        {
            "old orders": [
                {"action": "ask", "args": {"text": "Keep the 42 old orders?", "options": ["Yes", "No"]}},
                {"action": "ask", "args": ask},
                lambda seen: {
                    "action": "suggest_answer",
                    "args": {"question_id": seen.last("question_id"), "option_id": "c1"},
                },
                reply("Do you want the old orders kept? I would leave them out."),
            ]
        }
    )
    talk = Talk(storage, world, client)
    case = talk.say("what about the old orders")
    asked = next(q for q in case.questions if q.code == "CHAT_ASK")
    assert asked.text == ask["text"] and not asked.blocking and asked.recommended is None
    assert [o.label for o in asked.options] == ["Keep them", "Leave them out", "I don't know"]
    assert any(
        r.get("error") == "AGENT_QUESTION_REFUSED" for r in client.seen[1].results
    ), "a number is refused"
    suggestion = talk.reply.suggestion
    assert suggestion is not None and suggestion.label == "Leave them out" and asked.answer is None
    assert case.state is VisitState.APPROVE or case.state is VisitState.ASK


def test_a_quick_question_may_be_the_first_thing_the_agent_does(storage: LocalStorage, world: World) -> None:
    client = ChatScript(
        {
            "channel": [
                {
                    "action": "ask",
                    "args": {
                        "text": "Which channel do you mean?",
                        "options": ["Email", "SMS"],
                        "why": "Both exist.",
                    },
                },
                reply("Which channel do you mean?"),
            ]
        }
    )
    talk = Talk(storage, world, client)
    case = talk.say("send it by channel")
    asked = next(q for q in case.questions if q.code == "CHAT_ASK")
    assert asked.why == "Both exist." and talk.reply.tools == ("ask",)
    told_back = client.seen[-1].results[0]
    assert told_back["asked"] == "CHAT_ASK" and told_back["question_id"] == "k2"
    assert [o["id"] for o in told_back["options"]] == ["c0", "c1"], "the model learns the ids to suggest by"


def test_the_answer_is_recorded_only_by_the_persons_click(storage: LocalStorage, world: World) -> None:
    talk = Talk(storage, world, ChatScript(SCRIPT))
    case = talk.say(SALE)
    suggestion = talk.reply.suggestion
    assert suggestion is not None
    assert next(q for q in case.questions if q.question_id == suggestion.question_id).answer is None
    case = talk.answer(suggestion.question_id, suggestion.option_id)
    answered = next(q for q in case.questions if q.question_id == suggestion.question_id)
    assert answered.answer == "c0" and answered.answered_by == ANALYST, "the person's click"


@pytest.mark.parametrize("handle", ["k99", "k1"])
def test_a_suggestion_for_a_question_that_is_not_open_is_refused(
    storage: LocalStorage, world: World, handle: str
) -> None:
    """`k99` is nothing; `k1` is the key question, which the person has answered already."""
    client = ChatScript(
        {
            "suggest": [
                {"action": "suggest_answer", "args": {"question_id": handle, "option_id": "c0"}},
                reply("Nothing to suggest."),
            ]
        }
    )
    talk = Talk(storage, world, client)
    talk.say("suggest something")
    assert talk.reply.suggestion is None and talk.reply.text == "Nothing to suggest."
    assert [r["error"] for r in client.seen[-1].results if "error" in r] == ["AGENT_SUGGESTION_UNKNOWN"]


def test_a_doubt_about_meaning_and_a_request_for_data_reach_the_case(
    storage: LocalStorage, world: World
) -> None:
    client = ChatScript(
        {
            "unsure": [
                look("describe_numbers", "order_amount"),
                lambda seen: {
                    "action": "express_doubt",
                    "file": "F1",
                    "args": {
                        "because": "the amounts could be in rupees or in paise",
                        "would_do": "read them as rupees",
                        "if_not": "the amounts would be far too large",
                        "evidence_ids": [seen.last("evidence_id")],
                        "meaning": True,
                    },
                },
                {
                    "action": "request_data",
                    "file": "F1",
                    "args": {
                        "kind": "more_data",
                        "what": "the refunds",
                        "why": "to tell which orders were real sales",
                    },
                },
                reply("I am not sure about the amounts, and a refunds file would help."),
            ]
        }
    )
    talk = Talk(storage, world, client)
    case = talk.say("I am unsure about the amounts")
    assert [d.would_do for d in case.doubts] == ["read them as rupees"]
    codes = {q.code for q in case.questions if q.answer is None}
    assert any("DOUBT" in code for code in codes) and any(
        "DATA" in code or "REQUEST" in code for code in codes
    )
    assert talk.reply.tools == ("describe_numbers", "express_doubt", "request_data")


# ---------------------------------------------------------------------------
# Review fixes: meaning columns, the person's own yes, an undo that sticks
# ---------------------------------------------------------------------------
def rename_step(column: str, new_column: str) -> object:
    def step(seen: Seen) -> dict[str, object]:
        return {
            "action": "propose_step",
            "file": "F1",
            "args": {
                "kind": "rename_column",
                "column": column,
                "new_column": new_column,
                "params": {},
                "reason": "A shorter name.",
                "evidence_ids": [seen.last("evidence_id")],
            },
        }

    return step


def test_the_agent_never_renames_the_opt_in_column_even_before_a_goal_is_chosen(
    storage: LocalStorage, world: World
) -> None:
    """Downstream, a missing opt-out column is only a warning: opted-out customers could reach the list."""
    client = ChatScript(
        {
            "tidy": [
                look("value_counts", "marketing_opt_in"),
                rename_step("marketing_opt_in", "opted"),
                reply("That one waits for you."),
            ]
        }
    )
    talk = Talk(storage, world, client)
    (item,) = ai_items(talk.say("tidy the opt in column"))
    assert item.state is ProposalState.PENDING and item.decided_by is None
    assert "who may be contacted" in told(client)["result"]


@pytest.mark.parametrize(("column", "new"), [("converted", "sold"), ("snapshot_date", "as_of")])
def test_the_agent_never_changes_the_outcome_or_the_date_column_once_a_goal_is_chosen(
    storage: LocalStorage, world: World, column: str, new: str
) -> None:
    client = ChatScript(
        {
            "tidy": [
                look("value_counts", column),
                rename_step(column, new),
                reply("That one waits for you."),
            ]
        }
    )
    talk = Talk(storage, world, client)
    talk.goal()
    (item,) = ai_items(talk.say("tidy it"))
    assert item.state is ProposalState.PENDING, column
    assert "what the model learns" in told(client)["result"]


def test_the_agent_cannot_take_back_a_change_the_person_gave_their_own_yes(
    storage: LocalStorage, world: World
) -> None:
    from engine.doctor.protocol import item_sentence
    from tests.unit.doctor.chat_support import ASHA

    def withdraw_it(seen: Seen) -> dict[str, object]:
        return {"action": "withdraw", "args": {"item_id": handle_of(seen, "starts with 999")}}

    client = ChatScript(
        {**SCRIPT, "take it back": [withdraw_it, reply("I could not take that back; it is yours.")]}
    )
    talk = Talk(storage, world, client)
    (item,) = ai_items(talk.say(TEST_ACCOUNTS))
    assert item.decided_by == AGENT
    talk.case = visit.confirm(
        vs.CLIENT, talk.case.case_id, item.item_id, talk.deps(), principal=ASHA,
        sentence=item_sentence(item.title, item.payload), expected_version=talk.case.version,
    )  # fmt: skip
    (mine,) = ai_items(talk.case)
    assert (
        visit.confirmed(talk.case, mine) and mine.decided_by == ANALYST
    ), "the yes makes the tick the person's"

    case = talk.say("take it back")
    assert ai_items(case)[0].state is ProposalState.ACCEPTED, "their yes stands"
    assert talk.reply.withdrawn == ()
    assert any(r.get("error") == "AGENT_ITEM_PERSONS" for r in client.seen[-1].results)


def test_an_undo_sticks_when_the_agent_proposes_the_same_filter_in_other_words(
    storage: LocalStorage, world: World
) -> None:
    client = ChatScript(
        {
            **SCRIPT,
            "do it again": [
                look("find_values", "customer_id", contains="999"),
                lambda seen: {
                    "action": "propose_step",
                    "file": "F1",
                    "args": {
                        "kind": "remove_rows",
                        "column": "customer_id",
                        "params": {"operator": "starts_with", "values": ["999"]},
                        "reason": "Test ids.",  # another reason: another id, the same change
                        "evidence_ids": [seen.last("evidence_id")],
                    },
                },
                reply("Done, the test accounts are out again."),
            ],
        }
    )
    talk = Talk(storage, world, client)
    (applied,) = ai_items(talk.say(TEST_ACCOUNTS))
    talk.decide(applied.item_id, False)  # Undo

    case = talk.say("please do it again")
    (only,) = ai_items(case)
    assert only.state is ProposalState.REJECTED and only.decided_by == ANALYST, "no second, applied copy"
    assert "left out earlier; tick it again if you want it" in talk.reply.text
    assert talk.reply.item_ids == ()
    assert "took" in told(client)["result"] and "back" in told(client)["result"], "the model is told too"


def test_whoever_asked_for_a_change_in_the_chat_counts_as_its_proposer(
    storage: LocalStorage, world: World
) -> None:
    """With separation of duties on, the person who typed "drop test accounts" cannot also give its own yes."""
    talk = Talk(storage, world, ChatScript(SCRIPT))
    (item,) = ai_items(talk.say(TEST_ACCOUNTS))
    assert item.decided_by == AGENT
    assert visit.item_proposers(talk.case, item) == (ANALYST,)


def test_a_quick_question_is_not_about_consent_the_outcome_the_id_or_the_date(
    storage: LocalStorage, world: World
) -> None:
    client = ChatScript(
        {
            "ask me": [
                {
                    "action": "ask",
                    "args": {"text": "Did they agree to be contacted?", "options": ["Yes", "No"]},
                },
                {
                    "action": "ask",
                    "args": {"text": "Is the date day first?", "options": ["Day first", "Month first"]},
                },
                reply("I will use the question list for that."),
            ]
        }
    )
    talk = Talk(storage, world, client)
    case = talk.say("ask me what you need")
    assert not [q for q in case.questions if q.code == "CHAT_ASK"]
    assert any(r.get("error") == "AGENT_QUESTION_REFUSED" for r in client.seen[-1].results)


def test_a_suggestion_whose_question_was_settled_meanwhile_is_said_in_the_reply(
    storage: LocalStorage, world: World
) -> None:
    from engine.doctor.visit import ConverseResult

    talk = Talk(storage, world, None)
    talk.converse = lambda _case, _message, _files: ConverseResult(
        reply="Use this?", suggestion=("q.gone", "c0")
    )
    talk.say("anything")
    assert talk.reply.suggestion is None
    assert "no longer open" in talk.reply.text


# ---------------------------------------------------------------------------
# What the agent sees
# ---------------------------------------------------------------------------
def test_the_next_turn_reads_the_conversation_the_case_and_its_earlier_looks(
    storage: LocalStorage, world: World
) -> None:
    client = ChatScript(
        {
            **SCRIPT,
            "why did you": [
                lambda seen: {
                    "action": "propose_step",
                    "file": "F1",
                    "args": {
                        "kind": "remove_rows",
                        "column": "customer_id",
                        "params": {"operator": "starts_with", "values": ["10000"]},
                        "reason": "Your other test accounts.",
                        "evidence_ids": ["e1"],  # a look from an earlier turn
                    },
                },
                reply("I explained, and also left out your other test accounts."),
            ],
        }
    )
    talk = Talk(storage, world, client)
    talk.say(TEST_ACCOUNTS)
    talk.say("why did you drop those?")
    seen = client.seen[-2]
    assert [h["who"] for h in seen.history] == ["person", "you"], "the conversation so far"
    assert seen.history[0]["text"] == TEST_ACCOUNTS and "test accounts" in seen.history[1]["text"]
    plan = {p["id"]: p for p in seen.state["plan"]}
    assert any(p["what"].endswith("starts with 999") and p["decided_by"] == "you" for p in plan.values())
    assert seen.state["earlier_looks"] == [
        {"id": "e1", "tool": "find_values", "file": "F1", "column": "customer_id"}
    ]
    # It cited only an earlier look (e1) and the change was made: grounded, applied, and the look is not stored twice.
    assert not any(r.get("error") for r in client.seen[-1].results)
    second = [
        i for i in ai_items(talk.case) if i.title == "Leave out rows where 'customer_id' starts with 10000"
    ]
    assert len(second) == 1 and second[0].state is ProposalState.ACCEPTED and second[0].decided_by == AGENT
    assert (
        len(talk.case.evidence) == 5
    ), "the reloaded look hashes back to its stored id; only the new step is added"


def test_a_reply_with_a_number_nobody_looked_up_is_refused_and_the_next_one_is_used(
    storage: LocalStorage, world: World
) -> None:
    client = ChatScript(
        {
            "how many": [
                reply("There are 7,777 test accounts."),
                reply("I did not count them yet; I can look."),
            ]
        }
    )
    talk = Talk(storage, world, client)
    talk.say("how many test accounts are there")
    assert talk.reply.text == "I did not count them yet; I can look."
    assert any(r.get("error") == "AGENT_TEXT_REFUSED" for r in client.seen[1].results)


def _told(refusal: dict[str, Any]) -> str:
    """What a refusal tells the AI: its four fields read as one text."""
    assert set(refusal) >= {"error", "what_failed", "value", "how_to_fix"}
    return f"{refusal['what_failed']} {refusal['how_to_fix']}"


def test_a_refused_number_is_named_to_the_ai_and_a_turn_that_still_fails_tells_the_person_why(
    storage: LocalStorage, world: World
) -> None:
    client = ChatScript({"how many": [reply("There are 7,777 test accounts.")] * 6})
    talk = Talk(storage, world, client)
    talk.say("how many test accounts are there")
    told = [_told(r) for seen in client.seen for r in seen.results if r.get("error")]
    assert told and all("'7,777'" in m and "no tool result" in m for m in told), "the AI learns which number"
    assert "a number that I have not looked up" in talk.reply.text and "7,777" not in talk.reply.text
    assert "Ask me again" in talk.reply.text
    assert talk.reply.text != "I could not answer that from what I have checked."


def test_a_turn_that_ends_on_a_refused_action_names_the_action_and_the_refusal(
    storage: LocalStorage, world: World
) -> None:
    bad = {"action": "withdraw", "args": {"item_id": "c99"}}
    client = ChatScript({"prepare": [bad] * 6})
    talk = Talk(storage, world, client)
    talk.say("prepare the file for that then")
    text = talk.reply.text
    assert text.startswith("I tried to take a change back.") and "refused" in text
    assert text.endswith("Ask me again and I will try another way.")
    assert talk.reply.blocked_by is not None


def test_an_answer_in_plain_sentences_is_a_reply_and_is_checked_like_one(
    storage: LocalStorage, world: World
) -> None:
    client = ChatScript(
        {
            "hello": ["Hello! I have not looked at anything yet."],
            "numbers": ["There are 9,999 of them.", reply("I have not counted them.")],
        }
    )
    talk = Talk(storage, world, client)
    talk.say("hello there")
    assert talk.reply.text == "Hello! I have not looked at anything yet."
    talk.say("any numbers?")
    assert talk.reply.text == "I have not counted them.", "the plain answer had a number nobody looked up"


def test_two_bad_replies_end_the_turn_with_a_plain_sentence(storage: LocalStorage, world: World) -> None:
    client = ChatScript({"nonsense": [{"action": "dance"}, {"action": "sing"}, {"action": "hum"}]})
    talk = Talk(storage, world, client)
    talk.say("nonsense")
    assert "sing" not in talk.reply.text, "the model's own words are never echoed to the person"
    assert "What I tried was refused: I asked for something that does not exist here" in talk.reply.text
    assert talk.reply.text.endswith("Ask me again and I will try another way."), "never a bare refusal"
    assert talk.case.state in (VisitState.ASK, VisitState.APPROVE), "the visit goes on"


def test_a_refused_action_never_repeats_the_model_s_text_or_number_to_the_person(
    storage: LocalStorage, world: World
) -> None:
    odd = {"action": "4,372 customers dropped"}
    client = ChatScript({"odd": [odd, odd, odd]})
    talk = Talk(storage, world, client)
    talk.say("odd")
    assert "4,372" not in talk.reply.text
    assert "I tried to act on that" in talk.reply.text and "does not exist here" in talk.reply.text


def test_a_turn_takes_at_most_the_configured_number_of_steps(storage: LocalStorage, world: World) -> None:
    client = ChatScript({"look at everything": [look("describe_missing")] * 20})
    talk = Talk(storage, world, client, limits=AiLimitsConfig(max_chat_steps=2))
    talk.say("look at everything")
    assert (
        client.asked == 4
    ), "two actions, the call that had to be the reply, and the last word (not a reply)"
    assert "more steps than I may take" in talk.reply.text and talk.reply.tools == ("describe_missing",)


def test_the_spending_limit_stops_a_turn_between_calls_and_says_so(
    storage: LocalStorage, world: World
) -> None:
    client = ChatScript({"look": [look("describe_missing")] * 5})
    talk = Talk(storage, world, client, limits=AiLimitsConfig(max_calls_per_case=1))
    talk.say("look")
    assert client.asked == 1
    assert "used up what this case may spend" in talk.reply.text
    assert talk.case.counters.model_calls == 1
    assert (talk.reply.blocked_by, talk.reply.error_codes) == ("budget", (BUDGET_EXCEEDED,))


def test_an_ai_service_that_fails_is_told_with_why_in_the_reply_the_entry_and_the_timeline(
    storage: LocalStorage, world: World
) -> None:
    sentence, fix = PROBLEMS["key_expired"]
    talk = Talk(storage, world, Down(LLMHttpError("key_expired")))
    talk.say("look at the orders")
    reply = talk.reply
    assert reply.text == f"{UNAVAILABLE} {sentence} {fix}", "the lead, then the client's own fixed words"
    assert (reply.blocked_by, reply.error_codes) == ("unavailable", ("LLM_UNAVAILABLE", "LLM_KEY_EXPIRED"))
    (chat,) = [e for e in talk.timeline() if e.kind is TimelineKind.CHAT and e.details["role"] == "agent"]
    assert chat.details["ai"] is True, "an AI service was asked; it did not answer"
    assert chat.details["blocked_by"] == "unavailable"
    assert chat.details["error_codes"] == "LLM_UNAVAILABLE,LLM_KEY_EXPIRED"
    assert talk.case.state is not VisitState.CONVERSE, "the visit goes on with its rules"


def test_a_lapsed_aws_sign_in_is_told_with_its_own_sentence_and_code(
    storage: LocalStorage, world: World
) -> None:
    error = AwsSignInExpiredError()
    talk = Talk(storage, world, Down(error))
    talk.say("look at the orders")
    reply = talk.reply
    assert reply.text == f"{UNAVAILABLE} {error.message} {error.fix}"
    assert (reply.blocked_by, reply.error_codes) == (
        "unavailable",
        ("LLM_UNAVAILABLE", "LLM_AWS_SIGN_IN_EXPIRED"),
    )
    (chat,) = [e for e in talk.timeline() if e.kind is TimelineKind.CHAT and e.details["role"] == "agent"]
    assert chat.details["error_codes"] == "LLM_UNAVAILABLE,LLM_AWS_SIGN_IN_EXPIRED"


def test_an_ai_service_whose_failure_has_no_fixed_words_of_ours_adds_none_to_the_reply(
    storage: LocalStorage, world: World
) -> None:
    quoted = "the prompt said customer zq-private asked about sk-live-123"
    talk = Talk(storage, world, Down(LLMError("LLM_REFUSED", quoted)))
    talk.say("look at the orders")
    reply = talk.reply
    assert reply.text == UNAVAILABLE
    assert (reply.blocked_by, reply.error_codes) == ("unavailable", ("LLM_REFUSED",))
    stored = reply.model_dump_json() + "".join(e.model_dump_json() for e in talk.timeline())
    assert "zq-private" not in stored and "sk-live" not in stored


def test_a_step_that_breaks_is_named_failed_and_every_cause_the_chat_can_give_is_documented(
    storage: LocalStorage, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(self: object, action: dict[str, object]) -> None:
        raise RuntimeError("an odd action nobody planned for")

    monkeypatch.setattr(agent_chat._Chat, "_act", broken)
    talk = Talk(storage, world, ChatScript({"look": [look("describe_missing")]}))
    talk.say("look")
    assert (talk.reply.blocked_by, talk.reply.error_codes) == ("failed", ())
    (chat,) = [e for e in talk.timeline() if e.kind is TimelineKind.CHAT and e.details["role"] == "agent"]
    assert chat.details["blocked_by"] == "failed" and "error_codes" not in chat.details
    said = CaseChatEntry.model_fields["blocked_by"].description or ""
    assert "failed" in said, "a consumer that reads the field's description finds this cause there"
    for cause in ("no_consent", "budget", "unavailable", "prompt_too_large"):
        assert cause in said, cause


def test_a_chat_the_ai_answered_names_no_cause(storage: LocalStorage, world: World) -> None:
    talk = Talk(storage, world, ChatScript({"look": [reply("Nothing to report.")]}))
    talk.say("look")
    assert (talk.reply.blocked_by, talk.reply.error_codes) == (None, ())
    dumped = json.loads(talk.reply.model_dump_json())
    assert "blocked_by" not in dumped and "error_codes" not in dumped, "left out of the JSON while unset"
    (chat,) = [e for e in talk.timeline() if e.kind is TimelineKind.CHAT and e.details["role"] == "agent"]
    assert "blocked_by" not in chat.details and "error_codes" not in chat.details


def test_no_consent_means_no_call(storage: LocalStorage, world: World) -> None:
    client = ChatScript({"look": [look("describe_missing")]})
    talk = Talk(storage, world, client, consent=False)
    talk.say("look")
    assert client.asked == 0 and "has not been allowed to read this client's data" in talk.reply.text
    assert (talk.reply.blocked_by, talk.reply.error_codes) == ("no_consent", ())


# ---------------------------------------------------------------------------
# Several files, and the files' own gates
# ---------------------------------------------------------------------------
def test_a_stack_of_two_files_goes_through_the_chat_with_the_rules_counts(storage: LocalStorage) -> None:
    world = cs.monthly_world()
    client = ChatScript(
        {
            "stack": [
                {"action": "stack_files", "args": {"files": ["F1", "F2"]}},
                reply("I stacked the two halves into one table of 1,200 rows."),
            ]
        }
    )
    talk = Talk(storage, world, client, files=tuple(world.files), key=None)
    case = talk.say("stack the two parts")
    (item,) = ai_items(case)
    assert item.kind == "stack_files" and item.source is ProposalSource.AI
    assert (item.payload["rows_in"], item.payload["rows_out"]) == (1_200, 1_200), "counted by our code"
    assert item.state is ProposalState.ACCEPTED and item.decided_by == AGENT, "nobody lost, no yes needed"
    assert talk.reply.tools == ("stack_files",) and talk.reply.item_ids == (item.item_id,)


def test_a_join_or_stack_the_rules_do_not_find_is_a_plain_refusal(
    storage: LocalStorage, world: World
) -> None:
    client = ChatScript(
        {
            "stack": [
                {"action": "stack_files", "args": {"files": ["F1"]}},
                reply("There is only one file here."),
            ]
        }
    )
    talk = Talk(storage, world, client)
    talk.say("stack them")
    assert any(r.get("error") == "AGENT_FILE_UNKNOWN" for r in client.seen[-1].results)
    assert not ai_items(talk.case)


def test_a_file_whose_name_is_personal_data_is_masked_in_every_prompt(storage: LocalStorage) -> None:
    from tests.unit.doctor.test_ai_guard import EMAIL

    world = vs.world()
    client = ChatScript({"look": [look("describe_missing")]})
    talk = Talk(storage, world, client, files=(vs.FIRST, vs.SECOND), key=None)
    frame = vs.customers(rows=300, seed=3)
    frame.insert(0, f"contact {EMAIL}", [f"c{i}@example.org" for i in range(len(frame))])
    config = world.offers[0].config
    world.files["up-pii"] = vs.loaded("up-pii", f"{EMAIL}.csv", frame, config)
    case = visit_add(talk, "up-pii", world)
    del case
    talk.say("look")
    sent = "\n".join(client.sent_text) + "\n".join(json.dumps(item) for item in talk.reply.sent)
    assert "example.org" not in sent and EMAIL not in sent
    assert '"file": 3' in sent, "the third file was shown to the model, through its own gate"


def visit_add(talk: Talk, upload_id: str, world: World) -> Case:
    talk.case = visit.add_files(
        vs.CLIENT, talk.case.case_id, [vs.case_file(upload_id, world)], talk.deps(),
        expected_version=talk.case.version, actor=ANALYST,
    )  # fmt: skip
    return talk.case


def test_the_conversation_is_masked_with_every_files_gate_not_only_the_first(storage: LocalStorage) -> None:
    """A column name of the second file that the person typed (it is not a plain label) never reaches the model."""
    world = vs.world()
    odd = "ref#code!x"
    frame = vs.customers(rows=300, seed=3)
    frame[odd] = [f"r{i}" for i in range(len(frame))]
    config = world.offers[0].config
    world.files["up-odd"] = vs.loaded("up-odd", "odd.csv", frame, config)
    client = ChatScript({"look": [look("describe_missing")], "again": [look("describe_missing")]})
    talk = Talk(storage, world, client, files=(vs.FIRST, vs.SECOND), key=None)
    visit_add(talk, "up-odd", world)
    talk.say(f"look at {odd}")
    talk.say("and again")
    assert odd not in "\n".join(client.sent_text) and "4000123456" not in "\n".join(client.sent_text)
    assert "column_" in json.dumps(
        client.seen[-1].history
    ), "the alias of the third file's column, not the name"


# ---------------------------------------------------------------------------
# Several suggestions in one turn, and the last word
# ---------------------------------------------------------------------------
def _ask_sale(file: str = "orders.csv") -> dict[str, object]:
    return {
        "action": "ask",
        "file": "F1",
        "args": {
            "question": "Q24",
            "values": {"file": file},
            "choices": ["An order over 500 is a sale", "Any order is a sale"],
        },
    }


def _suggest(option: str) -> object:
    return lambda seen: {
        "action": "suggest_answer",
        "args": {"question_id": seen.last("question_id"), "option_id": option},
    }


def test_two_suggestions_for_two_questions_in_one_turn_are_both_recorded(
    storage: LocalStorage, world: World
) -> None:
    client = ChatScript(
        {
            "two things": [
                look("describe_numbers", "order_amount"),
                _ask_sale(),
                _suggest("c0"),
                {
                    "action": "ask",
                    "file": "F1",
                    "args": {
                        "question": "Q17",
                        "values": {"file": "orders.csv"},
                        "choices": ["Every order has an amount", "Some orders have none"],
                    },
                },
                _suggest("c1"),
                reply("Two answers to check."),
            ]
        }
    )
    talk = Talk(storage, world, client)
    talk.say("two things, a sale is over 500")
    assert len(talk.reply.suggestions) == 2
    assert talk.reply.suggestion == talk.reply.suggestions[0]
    assert {s.option_id for s in talk.reply.suggestions} == {"c0", "c1"}
    assert not [r for r in client.seen[-1].results if r.get("error")], "no limit of one"
    assert client.asked == 6, "the reply was the AI's own: no extra call"


def test_a_second_suggestion_for_the_same_question_replaces_the_first(
    storage: LocalStorage, world: World
) -> None:
    client = ChatScript(
        {
            "again": [
                look("describe_numbers", "order_amount"),
                _ask_sale(),
                _suggest("c0"),
                _suggest("c1"),
                reply("Changed my mind."),
            ]
        }
    )
    talk = Talk(storage, world, client)
    talk.say("again, a sale is over 500")
    assert [s.option_id for s in talk.reply.suggestions] == ["c1"]


LAST_WORD = "Here is what happened: I ran out of steps before I could finish."


def last_word_seen(client: ChatScript) -> Seen:
    return client.seen[-1]


def test_a_turn_that_runs_out_of_steps_gets_one_last_call_and_its_reply_is_used(
    storage: LocalStorage, world: World
) -> None:
    client = ChatScript({"look at everything": [look("describe_missing")] * 3 + [reply(LAST_WORD)]})
    talk = Talk(storage, world, client, limits=AiLimitsConfig(max_chat_steps=2))
    talk.say("look at everything")
    # two actions, the call that had to be the reply (a third look), then the last word
    assert client.asked == 4
    told = [r for r in last_word_seen(client).results if "turn_ending" in r]
    assert told and "reply" in told[0]["now"]
    assert talk.reply.text == LAST_WORD
    assert talk.reply.blocked_by is None


def test_an_invalid_last_word_falls_back_to_the_fixed_sentence(storage: LocalStorage, world: World) -> None:
    client = ChatScript({"look at everything": [look("describe_missing")] * 5})
    talk = Talk(storage, world, client, limits=AiLimitsConfig(max_chat_steps=2))
    talk.say("look at everything")
    assert client.asked == 4
    assert "more steps than I may take" in talk.reply.text


def test_a_number_nobody_looked_up_in_the_last_word_falls_back(storage: LocalStorage, world: World) -> None:
    client = ChatScript({"look at everything": [look("describe_missing")] * 3 + [reply("I saw 9,999 gaps.")]})
    talk = Talk(storage, world, client, limits=AiLimitsConfig(max_chat_steps=2))
    talk.say("look at everything")
    assert "more steps than I may take" in talk.reply.text


def test_two_refusals_end_the_turn_and_the_ai_gets_the_last_word(storage: LocalStorage, world: World) -> None:
    client = ChatScript(
        {"nonsense": [{"action": "dance"}, {"action": "sing"}, reply("Sorry, I could not do that.")]}
    )
    talk = Talk(storage, world, client)
    talk.say("nonsense")
    assert talk.reply.text == "Sorry, I could not do that."
    assert client.asked == 3
    assert talk.reply.blocked_by is None, "the AI wrote this reply itself"


def test_the_last_word_is_told_what_the_second_refusal_said(storage: LocalStorage, world: World) -> None:
    client = ChatScript(
        {
            "numbers": [
                reply("I found 9,999 gaps."),
                reply("I found 9,999 gaps."),
                reply("I could not state that number."),
            ]
        }
    )
    talk = Talk(storage, world, client)
    talk.say("numbers")
    assert talk.reply.text == "I could not state that number."
    errors = [r for r in last_word_seen(client).results if r.get("error")]
    assert len(errors) == 2 and all(_told(r) for r in errors), "the second refusal's message is there too"


def test_no_last_word_when_the_ai_service_is_the_reason(storage: LocalStorage, world: World) -> None:
    client = Down(LLMHttpError("key_expired"))
    talk = Talk(storage, world, client)
    talk.say("look")
    assert talk.reply.blocked_by == "unavailable"
    assert client.attempts == 1, "a failing service is asked once, not twice"


def test_no_last_word_when_the_case_spending_ran_out(
    storage: LocalStorage, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    tried: list[str | None] = []
    monkeypatch.setattr(agent_chat._Chat, "_last_word", lambda self, *a: tried.append("x") or "never")
    client = ChatScript({"look": [look("describe_missing")] * 5})
    talk = Talk(storage, world, client, limits=AiLimitsConfig(max_calls_per_case=1))
    talk.say("look")
    assert client.asked == 1 and talk.reply.blocked_by == "budget"
    assert not tried, "the last word is not even tried once the limit is reached"


def test_no_last_word_when_the_steps_used_up_the_spending(
    storage: LocalStorage, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    tried: list[str] = []
    monkeypatch.setattr(agent_chat._Chat, "_last_word", lambda self, *a: tried.append("x") or "never")
    client = ChatScript({"look": [look("describe_missing")] * 5})
    limits = AiLimitsConfig(max_chat_steps=2, max_calls_per_case=3)
    talk = Talk(storage, world, client, limits=limits)
    talk.say("look")
    assert client.asked == 3 and not tried, "three calls spent the limit: no fourth for the last word"
    assert "more steps than I may take" in talk.reply.text


def test_no_last_word_without_consent(storage: LocalStorage, world: World) -> None:
    client = ChatScript({"look": [reply("never")]})
    talk = Talk(storage, world, client, consent=False)
    talk.say("look")
    assert client.asked == 0 and talk.reply.blocked_by == "no_consent"


def _long_reply(size: int) -> str:
    """A grounded reply (no number in it) of exactly `size` characters."""
    sentence = "The customer table is built from the receipts and it needs three answers from you. "
    return (sentence * (size // len(sentence) + 1))[: size - 1].rstrip() + "."


def test_a_long_reply_that_explains_a_plan_is_accepted_and_stored_whole(
    storage: LocalStorage, world: World
) -> None:
    text = _long_reply(1500)
    talk = Talk(storage, world, ChatScript({"explain": [reply(text)]}))
    talk.say("explain the plan")
    assert talk.reply.text == text and len(talk.reply.text) > 800
    assert talk.case.chat[-1].text == text, "stored whole"


def test_a_reply_over_the_doctor_ceiling_is_refused_and_the_model_is_told_the_limit(
    storage: LocalStorage, world: World
) -> None:
    client = ChatScript({"explain": [reply(_long_reply(2100)), reply("Short version.")]})
    talk = Talk(storage, world, client)
    talk.say("explain the plan")
    assert talk.reply.text == "Short version."
    told = [r for r in client.seen[1].results if r.get("error") == "AGENT_TEXT_REFUSED"]
    assert told and str(limits().turn.chat_reply_chars) in _told(told[0])


def test_an_over_long_reply_that_also_has_a_wrong_number_is_told_both(
    storage: LocalStorage, world: World
) -> None:
    long_wrong = "I found 9,999 gaps. " + _long_reply(2100)
    client = ChatScript({"explain": [reply(long_wrong), reply("Short version.")]})
    talk = Talk(storage, world, client)
    talk.say("explain the plan")
    told = [r for r in client.seen[1].results if r.get("error") == "AGENT_TEXT_REFUSED"]
    assert told and "2000" in _told(told[0]) and "numbers_grounded" in _told(told[0])


def test_the_last_word_is_told_the_limit_when_a_reply_is_refused_only_for_length(
    storage: LocalStorage, world: World
) -> None:
    client = ChatScript(
        {"explain": [reply(_long_reply(2100)), reply(_long_reply(2100)), reply("Short version.")]}
    )
    talk = Talk(storage, world, client)
    talk.say("explain the plan")
    assert talk.reply.text == "Short version."
    told = [r for r in last_word_seen(client).results if r.get("error") == "AGENT_TEXT_REFUSED"]
    assert told and all("2000" in _told(r) for r in told)


def test_a_long_earlier_reply_is_shown_whole_to_the_model_on_the_next_turn(
    storage: LocalStorage, world: World
) -> None:
    text = _long_reply(1500)[:-1] + " END-MARKER."
    client = ChatScript({"explain": [reply(text)], "yes do those": [reply("Done.")]})
    talk = Talk(storage, world, client)
    talk.say("explain the plan")
    talk.say("yes do those")
    assert "END-MARKER" in str(client.seen[-1].history)


def test_guided_setup_keeps_its_shorter_reply_ceiling() -> None:
    from engine.agent.loop import MAX_REPLY_CHARS
    from engine.generative.guardrails import CheckContext, Guardrails, load_policy

    assert MAX_REPLY_CHARS == 800 < limits().turn.chat_reply_chars
    guard = Guardrails(load_policy())
    text = _long_reply(900)
    assert not guard.check(text, CheckContext(target="helper reply", max_chars=MAX_REPLY_CHARS)).passed
    assert guard.check(
        text, CheckContext(target="doctor reply", max_chars=limits().turn.chat_reply_chars)
    ).passed


def test_an_invalid_expect_tells_the_ai_which_fields_and_why_without_a_link_or_a_value() -> None:
    from pydantic import ValidationError

    from engine.doctor.agent_review import _expect_problems
    from engine.doctor.guards import Expectations

    with pytest.raises(ValidationError) as caught:
        Expectations.model_validate({"rows_out_min": "lots-of-rows-7", "rows_out": 5, "keys_unique": []})
    said = _expect_problems(caught.value)
    assert "`rows_out_min`" in said and "`rows_out`" in said and "`keys_unique`" in said
    assert "Allowed fields:" in said and "max_keys_repeated" in said
    assert "http" not in said and "lots-of-rows-7" not in said
