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

import pytest

from engine.agent.contracts import ProposalSource, ProposalState
from engine.doctor import registry as doctor_registry
from engine.doctor import visit
from engine.doctor.case import Case, TimelineKind, VisitState
from engine.doctor.config import AiLimitsConfig
from engine.doctor.visit import AGENT, AI_NEEDED_REPLY
from engine.storage import LocalStorage
from tests.fixtures.doctor_eval.chat_model import ChatScript, Seen
from tests.unit.doctor import chat_support as cs
from tests.unit.doctor import visit_support as vs
from tests.unit.doctor.chat_support import Talk, ai_items
from tests.unit.doctor.visit_support import ANALYST, World

SALE = "a yes is a customer who converted, the converted column is 1"
TEST_ACCOUNTS = "drop test accounts, ids start with 999"
THIS_YEAR = "actually only this year's orders"


@pytest.fixture(autouse=True)
def unified(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Doctor's routes exist only with `data_doctor.unified` on, and so do the policy's checks of an AI's change."""
    monkeypatch.setattr(doctor_registry, "unified_on", lambda _config: True)


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
        look("describe_numbers", "visits_90d"),
        {
            "action": "ask",
            "file": "F1",
            "args": {
                "question": "Q24",
                "values": {"file": "orders.csv"},
                "choices": ["a yes = a customer who converted"],
                "outcomes": [{"column": "converted", "yes": ["1"]}],
            },
        },
        asked_sale,
        reply("I understood: a yes is a customer who converted. Use this?"),
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

    # (1) What counts as a yes is the person's: the agent asks it, suggests the answer, and records nothing. It is
    # blocking (it decides what the model learns) and our code counted the yes rate on the label.
    talk.goal()
    case = talk.say(SALE)
    sale = next(q for q in case.questions if q.code == "Q24_WHAT_COUNTS")
    assert sale.answer is None and sale.blocking, "meaning: only the person's click answers it; Approve waits"
    assert "decides what the model learns" in sale.why
    assert sale.options[0].label.startswith("a yes = a customer who converted: ")
    assert " of 1,200 customers" in sale.options[0].label
    suggestion = talk.reply.suggestion
    assert suggestion is not None
    assert (suggestion.question_id, suggestion.option_id) == (sale.question_id, "c0")
    assert suggestion.label == sale.options[0].label
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
    talk.goal()
    case = talk.say(SALE)
    suggestion = talk.reply.suggestion
    assert suggestion is not None
    assert next(q for q in case.questions if q.question_id == suggestion.question_id).answer is None
    assert not [
        i for i in case.items if i.kind == "outcome_column" and "yes" in i.title
    ], "not before the click"
    case = talk.answer(suggestion.question_id, suggestion.option_id)
    answered = next(q for q in case.questions if q.question_id == suggestion.question_id)
    assert answered.answer == "c0" and answered.answered_by == ANALYST, "the person's click"
    (outcome,) = [i for i in case.items if i.kind == "outcome_column" and i.state is ProposalState.ACCEPTED]
    assert outcome.decided_by == ANALYST and outcome.payload["column"] == "converted"
    assert outcome.payload["definition"]["map"] == [
        {"value": "0", "meaning": "no"},
        {"value": "1", "meaning": "yes"},
    ]


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
                        "params": {"operator": "starts_with", "values": ["99901"]},
                        "reason": "Your other test accounts.",
                        "evidence_ids": ["e1"],  # a look from an earlier turn
                    },
                },
                reply("I explained, and also left out ids starting 99901."),
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
        i for i in ai_items(talk.case) if i.title == "Leave out rows where 'customer_id' starts with 99901"
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
    client = ChatScript({"nonsense": [{"action": "dance"}, {"action": "sing"}, reply("never reached")]})
    talk = Talk(storage, world, client)
    talk.say("nonsense")
    assert talk.reply.text == "I could not answer that from what I have checked."
    assert talk.case.state in (VisitState.ASK, VisitState.APPROVE), "the visit goes on"


def test_a_turn_takes_at_most_the_configured_number_of_steps(storage: LocalStorage, world: World) -> None:
    client = ChatScript({"look at everything": [look("describe_missing")] * 20})
    talk = Talk(storage, world, client, limits=AiLimitsConfig(max_chat_steps=2))
    talk.say("look at everything")
    assert client.asked == 3, "two actions and the call that had to be the reply"
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


def test_no_consent_means_no_call(storage: LocalStorage, world: World) -> None:
    client = ChatScript({"look": [look("describe_missing")]})
    talk = Talk(storage, world, client, consent=False)
    talk.say("look")
    assert client.asked == 0 and "has not been allowed to read this client's data" in talk.reply.text


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
