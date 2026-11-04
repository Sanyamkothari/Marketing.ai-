"""One AI turn (`DoctorTurn`): every action that changes the case goes through the author (`apply_actions`), a change
has the same handle in every turn, and the AI's own words are never cut.

The model is a turn-keyed scripted one (`ChatScript`); the case is a real one on disk, so what the author makes of an
action is read off the turn's result."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter

from engine.agent.contracts import AgentConfidence, ProposalSource, ProposalState
from engine.agent.tools import AgentContext
from engine.config import RunMode, load_use_case
from engine.doctor import visit
from engine.doctor.agent_apply import roles_of
from engine.doctor.agent_turn import DoctorTurn, TurnResult, handle_of
from engine.doctor.author import ApplyContext, AuthorAction, Create, ItemDraft, Withdraw, apply_actions
from engine.doctor.case import Case, CaseChatEntry
from engine.doctor.diagnose import LoadedFile
from engine.doctor.protocol import DEFAULT_SETTINGS
from engine.doctor.registry import DataToolContext, DataToolRegistry, DataToolResult
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


def _turn(
    case: Case, world: World, client: ChatScript, registry: DataToolRegistry | None = None, **extra: Any
) -> DoctorTurn:
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
        registry or _Cached(visit.visit_registry(), _MEMORY),
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


def test_an_action_that_carries_the_reply_ends_the_turn_without_another_call(
    storage: LocalStorage, world: World
) -> None:
    talk = Talk(storage, world, None)
    case, first = _applied_filter(talk, world)
    (mine,) = [i for i in first.case.items if i.source is ProposalSource.AI]
    case = _with(case, items=(*case.items, mine))
    client = ChatScript(
        {
            "take it out": [
                {
                    "action": "withdraw",
                    "args": {
                        "item": handle_of("c", mine.item_id),
                        "reason": "You asked.",
                        "consequence": "The test accounts stay.",
                    },
                    "reply": "I proposed taking it out; it waits for your yes.",
                },
            ]
        }
    )
    done = _turn(case, world, client).run("take it out")
    assert len(client.seen) == 1, "one call: the action and the words for the person"
    assert done.reply == "I proposed taking it out; it waits for your yes." and done.finished
    assert done.withdrawn == (mine.item_id,)


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
    assert told["value"] == "c00000" and told["how_to_fix"].startswith("Use the id of a change in the plan")
    assert handle_of("c", mine.item_id) in told["how_to_fix"], "the valid handles are named"
    assert [r.code for r in done.refused] == ["ITEM_NOT_FOUND"] and done.refused[0].how_to_fix
    assert done.withdrawn == (mine.item_id,), "and the second try was applied"


def test_an_answer_with_several_actions_runs_the_first_and_asks_for_the_rest_one_at_a_time(
    storage: LocalStorage, world: World
) -> None:
    """A broad request ("understand the data, pick the right things, build features") makes a model send several
    actions in one answer; the first runs through every check, and the model is told to send the others next.
    """
    import json

    talk = Talk(storage, world, None)
    case, first = _applied_filter(talk, world)
    (mine,) = [i for i in first.case.items if i.source is ProposalSource.AI]
    case = _with(case, items=(*case.items, mine))
    withdraw = {
        "action": "withdraw",
        "args": {
            "item": handle_of("c", mine.item_id),
            "reason": "Gone.",
            "consequence": "The test accounts stay.",
        },
    }
    client = ChatScript({"take it out": [json.dumps([withdraw, reply("Done.")]), reply("Done.")]})
    done = _turn(case, world, client).run("take it out")
    told = next(r for r in client.seen[1].results if r.get("error") == "AGENT_ONE_ACTION_PER_REPLY")
    assert told["value"] == "1" and "withdraw" in told["what_failed"] and told["how_to_fix"]
    assert done.withdrawn == (mine.item_id,), "the first action ran"
    assert (
        done.reply == "Done." and done.finished
    ), "and the turn ended with the model's own reply, not a refusal"


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


class _Suggesting(_Cached):
    """The visit's registry, where `find_test_columns` also suggests the change `draft` makes (as a fact tool does)."""

    def __init__(self, draft: ItemDraft) -> None:
        inner = _Cached(visit.visit_registry(), _MEMORY)
        super().__init__(inner, _MEMORY)
        self.row = {
            "id": "s-trim",
            "action": TypeAdapter(AuthorAction).dump_python(
                Create(draft=draft, reason="Spaces differ.", basis="s:s-trim"), mode="json"
            ),
            "reason": "Spaces differ between spellings of one channel.",
        }

    def call(self, name: str, given: Mapping[str, Any], ctx: DataToolContext) -> DataToolResult:
        made = super().call(name, given, ctx)
        if name != "find_test_columns":
            return made
        return made.model_copy(update={"output": {**made.output, "suggested": [self.row]}})


def test_a_suggestion_collected_mid_turn_is_as_sure_where_the_visit_applies_it_as_in_the_turn(
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
    client = ChatScript(
        {
            "tidy": [
                look("find_test_columns"),
                {
                    "action": "adopt_suggestion",
                    "args": {"id": handle_of("s", "s-trim"), "reason": "It is a real difference."},
                },
                reply("I trimmed the channel spellings."),
            ]
        }
    )
    turn = _turn(_said(talk.case, "tidy"), world, client, _Suggesting(draft))
    done = turn.run("tidy the channel")
    assert done.suggested == {"s-trim"}, "the turn held it, though the event brought none"
    (item,) = [i for i in done.case.items if i.source is ProposalSource.AI]
    assert item.confidence is AgentConfidence.SURE and item.state is ProposalState.ACCEPTED
    told = next(r for r in client.seen[-1].results if r.get("result") and "in the plan" in str(r["result"]))
    assert told["result"].startswith("It is in the plan")
    # The visit applies the same actions to the fresh case under the turn's suggestions and places it the same way.
    again = apply_actions(talk.case, done.actions, replace(_ctx(turn, talk.case), suggested=done.suggested))
    (placed,) = [i for i in again.case.items if i.source is ProposalSource.AI]
    assert (placed.state, placed.confidence) == (item.state, item.confidence)
    bare = apply_actions(talk.case, done.actions, _ctx(turn, talk.case))
    (unsure,) = [i for i in bare.case.items if i.source is ProposalSource.AI]
    assert (
        unsure.confidence is AgentConfidence.CHECK
    ), "without the turn's suggestions the same action is a guess"


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
    assert "2000" in told["what_failed"] and "fewer words" in told["how_to_fix"]


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
    assert str(len(long_description.strip())) in told["what_failed"] or "300" in told["what_failed"]
    assert not [
        i for i in done.case.items if i.source is ProposalSource.AI
    ], "nothing was made from a cut text"


# ---------------------------------------------------------------------------
# A live model's answers: the reader takes what a model really writes
# ---------------------------------------------------------------------------
def _run(
    world: World, storage: LocalStorage, steps: list[Any], message: str = "explain the plan"
) -> tuple[TurnResult, ChatScript]:
    talk = Talk(storage, world, None)
    client = ChatScript({"explain": steps})
    return _turn(_said(talk.case, message), world, client).run(message), client


def test_a_reply_with_real_newlines_is_the_reply(storage: LocalStorage, world: World) -> None:
    done, _ = _run(world, storage, ['{"action": "reply", "text": "First line.\nSecond line."}'])
    assert done.reply == "First line.\nSecond line." and done.blocked_by is None


def test_a_reply_is_read_from_args_text_or_message(storage: LocalStorage, world: World) -> None:
    done, _ = _run(world, storage, ['{"action": "reply", "args": {"text": "From the args."}}'])
    assert done.reply == "From the args."
    done, _ = _run(world, storage, ['{"action": "reply", "message": "From the message."}'])
    assert done.reply == "From the message."


def test_an_action_with_its_arguments_beside_it_is_run(storage: LocalStorage, world: World) -> None:
    flat = '<think>hmm</think>{"name": "describe_numbers", "file": "F1", "column": "order_amount"}'
    done, client = _run(world, storage, [flat, reply("Looked.")])
    assert done.tools == ("describe_numbers",) and done.reply == "Looked." and not done.refused
    assert client.asked == 2


def test_json_without_an_action_is_never_the_reply(storage: LocalStorage, world: World) -> None:
    done, client = _run(world, storage, ['{"summary": "all fine"}', reply("Plain words.")])
    assert done.reply == "Plain words." and "summary" not in done.reply
    assert client.asked == 2


@pytest.mark.parametrize("none", ["none", "", None, "null"])
def test_a_replaces_that_names_nothing_is_no_replaces(
    storage: LocalStorage, world: World, none: str | None
) -> None:
    def step(seen: Any) -> dict[str, Any]:
        made = dict(SCRIPT[TEST_ACCOUNTS][1](seen)) if callable(SCRIPT[TEST_ACCOUNTS][1]) else {}
        made["args"] = {**made["args"], "replaces": none}
        return made

    talk = Talk(storage, world, None)
    client = ChatScript({TEST_ACCOUNTS: [SCRIPT[TEST_ACCOUNTS][0], step]})
    done = _turn(_said(talk.case, TEST_ACCOUNTS), world, client).run(TEST_ACCOUNTS)
    assert [a for a in done.actions if isinstance(a, Create)] and not done.refused


def test_a_reply_refused_after_a_refused_look_is_still_rewritten_once(
    storage: LocalStorage, world: World
) -> None:
    done, client = _run(
        world,
        storage,
        [look("describe_numbers", "no_such_column"), reply(_long(2100)), reply("Short version.")],
    )
    assert done.reply == "Short version." and done.blocked_by is None and client.asked == 3


def test_strikes_are_failures_in_a_row_an_action_that_worked_clears_them(
    storage: LocalStorage, world: World
) -> None:
    bad, good = look("describe_numbers", "no_such_column"), look("describe_numbers", "order_amount")
    done, client = _run(world, storage, [bad, good, bad, good, reply("Looked twice.")])
    assert done.reply == "Looked twice." and done.blocked_by is None and client.asked == 5


def test_two_refused_replies_in_a_row_still_end_the_turn(storage: LocalStorage, world: World) -> None:
    done, client = _run(world, storage, [reply(_long(2100)), reply(_long(2200)), reply(_long(2300))])
    assert done.blocked_by == "AGENT_TEXT_REFUSED"
    assert client.asked == 3  # two replies, then the last word


# ---------------------------------------------------------------------------
# A bounded prompt and bounded steps
# ---------------------------------------------------------------------------
def _with_prompt_room(turn: DoctorTurn, chars: int) -> DoctorTurn:
    narrow = turn.limits.turn.model_copy(update={"prompt_chars": chars})
    turn.limits = turn.limits.model_copy(update={"turn": narrow})
    return turn


def _sizes(world: World, storage: LocalStorage, steps: list[Any]) -> list[int]:
    _, client = _run(world, storage, steps)
    return [len(text) for text in client.sent_text]


LOOKS = [look("value_counts", "customer_id"), look("describe_numbers", "order_amount")]


def test_older_look_results_are_cut_to_stubs_before_a_prompt_is_refused_as_too_big(
    storage: LocalStorage, world: World
) -> None:
    steps = [*LOOKS, look("describe_missing"), reply("Looked at three things.")]
    sizes = _sizes(world, storage, steps)
    client = ChatScript({"explain": list(steps)})
    talk = Talk(storage, world, None)
    turn = _with_prompt_room(_turn(_said(talk.case, "explain the plan"), world, client), sizes[3] - 2)
    done = turn.run("explain the plan")
    assert done.reply == "Looked at three things." and done.blocked_by is None
    results = client.seen[3].results
    assert [set(r) for r in results[:2]] == [{"evidence_id", "tool", "file"}] * 2
    assert "result" in results[2]  # the newest look stays whole


def test_a_prompt_too_big_to_cut_gets_its_last_word_and_says_what_was_done_before(
    storage: LocalStorage, world: World
) -> None:
    steps = [
        look("find_values", "customer_id", contains="999"),
        rows_step("customer_id", "starts_with", "999"),
    ]
    sizes = _sizes(world, storage, [*steps, reply("Done.")])
    client = ChatScript({"explain": list(steps)})
    talk = Talk(storage, world, None)
    turn = _with_prompt_room(_turn(_said(talk.case, "explain the plan"), world, client), sizes[2] - 2)
    done = turn.run("explain the plan")
    assert done.blocked_by == "prompt_too_large" and client.asked == 2
    assert "What I did before that is above." in done.reply and "too much in this case" in done.reply


def test_a_prompt_too_big_with_nothing_done_does_not_claim_work_above(
    storage: LocalStorage, world: World
) -> None:
    sizes = _sizes(world, storage, [LOOKS[0], reply("Done.")])
    client = ChatScript({"explain": [LOOKS[0]]})
    talk = Talk(storage, world, None)
    turn = _with_prompt_room(_turn(_said(talk.case, "explain the plan"), world, client), sizes[1] - 2)
    done = turn.run("explain the plan")
    assert done.blocked_by == "prompt_too_large" and "is above" not in done.reply


def test_every_prompt_says_how_many_steps_are_left(storage: LocalStorage, world: World) -> None:
    _, client = _run(world, storage, [LOOKS[0], reply("Done.")])
    assert "Steps left: 8 of 8." in client.sent_text[0] and "Steps left: 7 of 8." in client.sent_text[1]


def test_the_stopped_sentence_depends_on_whether_earlier_actions_were_kept(
    storage: LocalStorage, world: World
) -> None:
    talk = Talk(storage, world, None)
    turn = _turn(_said(talk.case, "explain the plan"), world, ChatScript({}))
    assert "nothing was changed" in turn._unsaid("failed", None, "")
    turn.kept.append(object())  # type: ignore[arg-type]
    said = turn._unsaid("failed", None, "")
    assert "nothing was changed" not in said and "What I did before that is above." in said


# ---------------------------------------------------------------------------
# What the AI service really sends back
# ---------------------------------------------------------------------------
class _Cutting(ChatScript):
    """An AI service whose first answers stop at the length limit with their text cut."""

    def __init__(self, turns: Mapping[str, Any], cuts: int = 1) -> None:
        super().__init__(turns)
        self.cuts = cuts

    def complete(self, *args: Any, **kwargs: Any) -> Any:
        done = super().complete(*args, **kwargs)
        if self.cuts:
            self.cuts -= 1
            return done.model_copy(update={"stop_reason": "length"})
        return done


def test_an_answer_cut_at_the_length_limit_is_told_so_not_called_a_format_error(
    storage: LocalStorage, world: World
) -> None:
    talk = Talk(storage, world, None)
    client = _Cutting({"explain": ['{"action": "reply", "text": "A long answer that', reply("Short.")]})
    done = _turn(_said(talk.case, "explain the plan"), world, client).run("explain the plan")
    assert done.reply == "Short." and "AGENT_REPLY_MALFORMED" not in [
        r.get("error") for r in client.seen[1].results
    ]
    (told,) = client.seen[1].results
    assert told["error"] == "AGENT_ANSWER_CUT" and "cut at the length limit" in told["what_failed"]
    assert "shorter" in told["how_to_fix"] and "no reasoning" in told["how_to_fix"]


def test_an_empty_answer_has_its_own_code_and_sentence(storage: LocalStorage, world: World) -> None:
    from engine.llm import LLMError
    from tests.unit.doctor.chat_support import Down

    talk = Talk(storage, world, None)
    client = Down(LLMError("LLM_REFUSED", "The model returned nothing to read."))
    done = _turn(_said(talk.case, "explain the plan"), world, client).run("explain the plan")
    assert done.blocked_by == "unavailable" and "LLM_EMPTY_ANSWER" in done.error_codes
    assert "answered, but with nothing in it" in done.reply and "not answering" not in done.reply


def test_the_cases_spending_limit_ends_a_turn_with_the_limit_sentence(
    storage: LocalStorage, world: World
) -> None:
    from engine.doctor.ai_guard import AI_LIMIT_REPLY
    from engine.doctor.config import AiLimitsConfig

    talk = Talk(storage, world, None)
    client = ChatScript({"explain": [look("describe_missing")]})
    turn = _turn(_said(talk.case, "explain the plan"), world, client)
    turn.meter = cs.guard_for(client, limits=AiLimitsConfig(max_calls_per_case=1))
    done = turn.run("explain the plan")
    assert done.blocked_by == "unavailable" and done.reply == AI_LIMIT_REPLY
