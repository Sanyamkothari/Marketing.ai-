"""One AI turn (`DoctorTurn`): every action that changes the case goes through the author (`apply_actions`), a change
has the same handle in every turn, and the AI's own words are never cut.

The model is a turn-keyed scripted one (`ChatScript`); the case is a real one on disk, so what the author makes of an
action is read off the turn's result."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from engine.agent.contracts import AgentConfidence, ProposalSource, ProposalState
from engine.agent.tools import AgentContext
from engine.config import RunMode, load_use_case
from engine.doctor import visit
from engine.doctor.agent_apply import roles_of
from engine.doctor.agent_turn import DoctorTurn, TurnResult, handle_of
from engine.doctor.author import ApplyContext, Create, ItemDraft, Withdraw, apply_actions
from engine.doctor.case import Case, CaseChatEntry
from engine.doctor.diagnose import LoadedFile
from engine.doctor.protocol import DEFAULT_SETTINGS
from engine.storage import LocalStorage
from tests.fixtures.doctor_eval.chat_model import ChatScript, Seen
from tests.unit.agent.egress_canary import guardrails
from tests.unit.doctor import chat_support as cs
from tests.unit.doctor.chat_support import Talk
from tests.unit.doctor.test_doctor_chat import SCRIPT, TEST_ACCOUNTS, look, reply, rows_step
from tests.unit.doctor.test_visit import _MEMORY, _Cached
from tests.unit.doctor.visit_support import ANALYST, GOAL_A, World


@pytest.fixture
def world() -> World:
    return cs.orders_world()


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path / "data")


def _said(case: Case, message: str) -> Case:
    """The case as the visit hands it to the turn: the person's message is its last chat entry."""
    entry = CaseChatEntry(role="user", text=message, at=case.created_at, by=ANALYST)
    return case.model_copy(update={"chat": (*case.chat, entry)})


def _turn(case: Case, world: World, client: ChatScript, **extra: Any) -> DoctorTurn:
    def context_for(_case: Case, files: tuple[LoadedFile, ...]) -> AgentContext:
        return AgentContext(
            use_case_id=GOAL_A,
            config=load_use_case(GOAL_A),
            config_root=None,
            upload_id=files[0].upload_id,
            mode=RunMode.TRAIN,
            profile=files[0].profile,
            frame=files[0].frame,
        )

    return DoctorTurn(
        case,
        tuple(world.load(case)),
        None,
        context_for,
        cs.guard_for(client),
        guardrails(),
        cs.ASHA,
        None,
        _Cached(visit.visit_registry(), _MEMORY),
        chat_steps=8,
        **extra,
    )


def _ctx(turn: DoctorTurn, case: Case) -> ApplyContext:
    return ApplyContext(
        now=turn.now,
        author="ai",
        roles=roles_of(case),
        settings=DEFAULT_SETTINGS,
        requested_by=ANALYST,
    )


def _with(case: Case, **updates: Any) -> Case:
    return case.model_copy(update=updates)


def _applied_filter(talk: Talk, world: World) -> tuple[Case, TurnResult]:
    """A first turn that puts a filter for the test accounts in the plan."""
    client = ChatScript({TEST_ACCOUNTS: SCRIPT[TEST_ACCOUNTS]})
    case = _said(talk.case, TEST_ACCOUNTS)
    return case, _turn(case, world, client).run(TEST_ACCOUNTS)


# ---------------------------------------------------------------------------
# Every action goes through the author
# ---------------------------------------------------------------------------
def test_a_change_the_ai_proposes_becomes_a_create_and_the_author_places_it(
    storage: LocalStorage, world: World
) -> None:
    talk = Talk(storage, world, None)
    case = _said(talk.case, TEST_ACCOUNTS)
    turn = _turn(case, world, ChatScript({TEST_ACCOUNTS: SCRIPT[TEST_ACCOUNTS]}))
    done = turn.run(TEST_ACCOUNTS)
    (create,) = [a for a in done.actions if isinstance(a, Create)]
    assert create.draft.kind == "recipe_step" and create.draft.confidence is AgentConfidence.CHECK
    assert create.reason == "You asked for this." and create.basis
    (item,) = [i for i in done.case.items if i.source is ProposalSource.AI]
    # Placed by code, never decided here: in the plan, nobody's decision yet, made by the one author for the person.
    assert item.state is ProposalState.ACCEPTED and item.decided_by is None
    assert (
        item.producer == "author"
        and item.requested_by == ANALYST
        and item.confidence is AgentConfidence.CHECK
    )
    assert done.items == (item,) and done.withdrawn == ()
    assert done.reply.startswith("Done: I left out the test accounts")
    # The visit applies the same actions to the fresh case and gets the same plan: `apply_actions` is pure.
    again = apply_actions(case, done.actions, _ctx(turn, case))
    assert again.case.items == done.case.items


def test_replacing_a_change_the_person_decided_waits_for_their_yes(
    storage: LocalStorage, world: World
) -> None:
    talk = Talk(storage, world, None)
    case, first = _applied_filter(talk, world)
    (mine,) = [i for i in first.case.items if i.source is ProposalSource.AI]
    theirs = mine.model_copy(update={"decided_by": ANALYST})
    case = _with(case, items=(*case.items, theirs))
    swap = ChatScript(
        {
            "only this year": [
                look("describe_dates", "order_date"),
                lambda seen: rows_step(
                    "order_date", "before", "2026-01-01", replaces=handle_of("c", theirs.item_id)
                )(seen),
                reply("I proposed this year's orders instead; it waits for your yes."),
            ]
        }
    )
    done = _turn(case, world, swap).run("only this year please")
    old = next(i for i in done.case.items if i.item_id == theirs.item_id)
    (new,) = [i for i in done.case.items if i.replaces == theirs.item_id]
    assert old.state is ProposalState.ACCEPTED, "what the person decided is not changed"
    assert new.state is ProposalState.PENDING and new.decided_by is None, "the new one waits for their yes"
    assert done.withdrawn == (theirs.item_id,)
    told = next(r for r in swap.seen[-1].results if r.get("id") == handle_of("c", new.item_id))
    assert "waits for the person's yes" in told["result"], "the model is told it is a swap that waits"


def test_replacing_a_change_that_only_waits_swaps_it_at_once(storage: LocalStorage, world: World) -> None:
    talk = Talk(storage, world, None)
    case, first = _applied_filter(talk, world)
    (mine,) = [i for i in first.case.items if i.source is ProposalSource.AI]
    waiting = mine.model_copy(update={"state": ProposalState.PENDING})
    case = _with(case, items=(*case.items, waiting))
    swap = ChatScript(
        {
            "only this year": [
                look("describe_dates", "order_date"),
                lambda seen: rows_step(
                    "order_date", "before", "2026-01-01", replaces=handle_of("c", waiting.item_id)
                )(seen),
                reply("Done: I swapped my filter for this year's orders."),
            ]
        }
    )
    done = _turn(case, world, swap).run("only this year please")
    old = next(i for i in done.case.items if i.item_id == waiting.item_id)
    assert old.state is ProposalState.REJECTED and old.withdrawn_because, "taken out now, with its reason"
    (new,) = [
        i
        for i in done.case.items
        if i.superseded_by is None and i.source is ProposalSource.AI and i is not old
    ]
    assert old.superseded_by == new.item_id and new.replaces is None


@pytest.mark.parametrize("state", ["waiting", "in plan"])
def test_withdraw_takes_out_what_waits_at_once_and_asks_for_a_yes_for_the_rest(
    storage: LocalStorage, world: World, state: str
) -> None:
    talk = Talk(storage, world, None)
    case, first = _applied_filter(talk, world)
    (mine,) = [i for i in first.case.items if i.source is ProposalSource.AI]
    target = mine.model_copy(update={"state": ProposalState.PENDING if state == "waiting" else mine.state})
    case = _with(case, items=(*case.items, target))
    handle = handle_of("c", target.item_id)
    client = ChatScript(
        {
            "take it out": [
                {
                    "action": "withdraw",
                    "args": {
                        "item": handle,
                        "reason": "You asked.",
                        "consequence": "The test accounts stay.",
                    },
                },
                reply("Done."),
            ]
        }
    )
    done = _turn(case, world, client).run("take it out")
    left = next(i for i in done.case.items if i.item_id == target.item_id)
    asks = [i for i in done.case.items if i.kind == "withdraw"]
    assert done.withdrawn == (target.item_id,) and [a for a in done.actions if isinstance(a, Withdraw)]
    if state == "waiting":
        assert left.state is ProposalState.REJECTED and not asks
    else:
        assert left.state is ProposalState.ACCEPTED, "still in the plan until they say yes"
        (ask,) = asks
        assert ask.state is ProposalState.PENDING and ask.replaces == target.item_id
        assert ask.payload["consequence"] == "The test accounts stay."


def test_a_refusal_goes_back_to_the_model_with_what_to_do_and_a_second_try_works(
    storage: LocalStorage, world: World
) -> None:
    talk = Talk(storage, world, None)
    case, first = _applied_filter(talk, world)
    (mine,) = [i for i in first.case.items if i.source is ProposalSource.AI]
    case = _with(case, items=(*case.items, mine))
    client = ChatScript(
        {
            "take it out": [
                {"action": "withdraw", "args": {"item": "c00000", "reason": "Gone.", "consequence": "None."}},
                {
                    "action": "withdraw",
                    "args": {
                        "item": handle_of("c", mine.item_id),
                        "reason": "Gone.",
                        "consequence": "The test accounts stay.",
                    },
                },
                reply("Done."),
            ]
        }
    )
    done = _turn(case, world, client).run("take it out")
    told = next(r for r in client.seen[1].results if r.get("error") == "ITEM_NOT_FOUND")
    assert "c00000" in told["message"] and told["how_to_fix"].startswith("Use the id of a change in the plan")
    assert handle_of("c", mine.item_id) in told["how_to_fix"], "the valid handles are named"
    assert [r.code for r in done.refused] == ["ITEM_NOT_FOUND"] and done.refused[0].how_to_fix
    assert done.withdrawn == (mine.item_id,), "and the second try was applied"


def test_a_suggestion_a_fact_tool_made_is_the_only_way_to_be_sure(
    storage: LocalStorage, world: World
) -> None:
    talk = Talk(storage, world, None)
    step = {
        "order": 1,
        "kind": "normalise_text",
        "column": "channel",
        "params": {"strip": True},
        "reason": "r",
    }
    draft = ItemDraft(
        kind="recipe_step",
        payload={"upload_id": cs.FIRST, "path": None, "value": None, "step": step},
        title="Trim the spaces in 'channel'",
        upload_id=cs.FIRST,
        confidence=AgentConfidence.SURE,
    )

    class Suggestion:
        id = "s-trim"
        reason = "Spaces differ between spellings of one channel."
        action = Create(draft=draft, reason="Spaces differ.", basis="s:s-trim")

    client = ChatScript(
        {
            "tidy": [
                {"action": "adopt_suggestion", "args": {"id": "s-nothing", "reason": "Tidy."}},
                lambda seen: {
                    "action": "adopt_suggestion",
                    "args": {"id": handle_of("s", "s-trim"), "reason": "It is a real difference."},
                },
                reply("I trimmed the channel spellings."),
            ]
        }
    )
    turn = _turn(talk.case, world, client, suggestions=[Suggestion()])
    assert turn.state["suggested"][0]["id"] == handle_of("s", "s-trim")
    done = turn.run("tidy the channel")
    refused = next(r for r in client.seen[1].results if r.get("error") == "ITEM_NOT_FOUND")
    assert handle_of("s", "s-trim") in refused["how_to_fix"], "the real suggestions are named"
    (item,) = [i for i in done.case.items if i.source is ProposalSource.AI]
    assert item.confidence is AgentConfidence.SURE, "sure, because a fact tool suggested it"
    assert item.state is ProposalState.ACCEPTED and item.decided_by is None


# ---------------------------------------------------------------------------
# Stable handles
# ---------------------------------------------------------------------------
def test_a_change_and_a_question_keep_their_handle_from_one_turn_to_the_next(
    storage: LocalStorage, world: World
) -> None:
    talk = Talk(storage, world, None)
    first = _turn(talk.case, world, ChatScript({}))
    plan = {row["id"]: row for row in first.state["plan"]}
    questions = {row["id"] for row in first.state["questions"]}
    assert plan and all(h.startswith("c") and len(h) == 6 for h in plan)
    assert all(h.startswith("q") and len(h) == 6 for h in questions)
    assert {handle_of("c", i.item_id) for i in talk.case.items} >= set(plan)
    # A change is made; the turn after reads every earlier handle unchanged, and the new change has its own.
    _case, done = _applied_filter(talk, world)
    second = _turn(done.case, world, ChatScript({}))
    again = {row["id"]: row for row in second.state["plan"]}
    assert set(plan) <= set(again), "no handle moved"
    assert all(again[h]["what"] == plan[h]["what"] for h in plan)
    (new,) = set(again) - set(plan)
    assert new == handle_of("c", done.items[0].item_id)
    assert {row["id"] for row in second.state["questions"]} >= questions
    assert first.handles[new if new in first.handles else next(iter(plan))]


def test_a_raw_id_names_a_change_too(storage: LocalStorage, world: World) -> None:
    talk = Talk(storage, world, None)
    case, first = _applied_filter(talk, world)
    (mine,) = [i for i in first.case.items if i.source is ProposalSource.AI]
    case = _with(case, items=(*case.items, mine.model_copy(update={"state": ProposalState.PENDING})))
    client = ChatScript(
        {
            "take it out": [
                {
                    "action": "withdraw",
                    "args": {"item": mine.item_id, "reason": "Gone.", "consequence": "They stay."},
                },
                reply("Done."),
            ]
        }
    )
    done = _turn(case, world, client).run("take it out")
    assert done.withdrawn == (mine.item_id,)


def test_two_changes_that_share_a_handle_are_told_apart_and_withdrawn_one_by_one(
    monkeypatch: pytest.MonkeyPatch, storage: LocalStorage, world: World
) -> None:
    from engine.doctor import agent_turn

    talk = Talk(storage, world, None)
    case, first = _applied_filter(talk, world)
    (mine,) = [i for i in first.case.items if i.source is ProposalSource.AI]
    one = mine.model_copy(update={"state": ProposalState.PENDING})
    two = one.model_copy(update={"item_id": one.item_id + "-twin", "title": "The twin change"})
    case = _with(case, items=(*case.items, one, two))
    real = agent_turn.handle_of

    def clash(prefix: str, ident: str, width: int = 5) -> str:
        if ident in (one.item_id, two.item_id):
            return prefix + "same0" + ("x" * (width - 5) if ident == one.item_id else ident[-(width - 5) :])
        return real(prefix, ident, width)

    monkeypatch.setattr(agent_turn, "handle_of", clash)
    turn = _turn(case, world, ChatScript({}))
    shown = {row["id"]: row["what"] for row in turn.state["plan"]}
    first_handle = next(h for h, what in shown.items() if what == one.title)
    second_handle = next(h for h, what in shown.items() if what == "The twin change")
    assert first_handle != second_handle and turn.handles[second_handle] == two.item_id
    assert turn.handles[first_handle] == one.item_id
    client = ChatScript(
        {
            "take it out": [
                {
                    "action": "withdraw",
                    "args": {"item": second_handle, "reason": "You asked.", "consequence": "The twin stays."},
                },
                reply("Done."),
            ]
        }
    )
    done = _turn(case, world, client).run("take it out")
    assert done.withdrawn == (two.item_id,)
    left = {i.item_id: i for i in done.case.items}
    assert (
        left[two.item_id].state is ProposalState.REJECTED and left[one.item_id].state is ProposalState.PENDING
    )


# ---------------------------------------------------------------------------
# No clipping
# ---------------------------------------------------------------------------
def _long(size: int) -> str:
    sentence = "The customer table is built from the receipts and it needs three answers from you. "
    return (sentence * (size // len(sentence) + 1))[: size - 1].rstrip() + "."


def test_the_ais_reply_is_returned_whole(storage: LocalStorage, world: World) -> None:
    talk = Talk(storage, world, None)
    text = _long(1900)
    done = _turn(talk.case, world, ChatScript({"explain": [reply(text)]})).run("explain the plan")
    assert done.reply == text and done.finished and done.blocked_by is None


def test_a_reply_that_is_too_long_is_refused_with_the_limit_not_cut(
    storage: LocalStorage, world: World
) -> None:
    talk = Talk(storage, world, None)
    client = ChatScript({"explain": [reply(_long(2100)), reply("Short version.")]})
    done = _turn(talk.case, world, client).run("explain the plan")
    assert done.reply == "Short version."
    told = next(r for r in client.seen[1].results if r.get("error") == "AGENT_TEXT_REFUSED")
    assert "2000" in told["message"] and "fewer words" in told["message"]


def test_a_query_description_that_is_too_long_is_refused_not_cut(storage: LocalStorage, world: World) -> None:
    talk = Talk(storage, world, None)
    long_description = "Add up the order amount of every customer " * 12

    def query(_seen: Seen) -> dict[str, Any]:
        return {
            "action": "propose_sql",
            "args": {
                "description": long_description,
                "sql": "select customer_id, sum(order_amount) as total from t_orders group by customer_id",
                "output_name": "totals",
                "files": ["F1"],
                "expect": {"keys_unique": True},
            },
        }

    client = ChatScript({"totals": [query, reply("I will say it shorter.")]})
    done = _turn(talk.case, world, client).run("add up the totals")
    told = next(r for r in client.seen[1].results if r.get("error") == "AGENT_DESCRIPTION_TOO_LONG")
    assert str(len(long_description.strip())) in told["message"] or "300" in told["message"]
    assert not [
        i for i in done.case.items if i.source is ProposalSource.AI
    ], "nothing was made from a cut text"
