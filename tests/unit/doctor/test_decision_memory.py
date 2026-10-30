"""What the person decided stays decided: one memory, read by the one author (`engine.doctor.agent_apply.decided_before`).

* A person's "keep it as it is", said in a chat message, is recorded as their decision (`author.Keep`), cited to that
  message. It only ever turns a change down, so it can never change data or meaning. While it stands no author
  proposes that matter again, whatever the counts of the day, and unticking it brings the matter back.
* A person's yes or no to a change of a column of a file carries over to the table a combine made of that file, when
  it is the same column of the same source file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from engine.agent.contracts import AgentConfidence, ProposalSource, ProposalState
from engine.doctor.author import Create, ItemDraft, Keep, Refusal, Withdraw, apply_actions, item_id_of
from engine.doctor.case import (
    Case,
    CaseChatEntry,
    CaseFile,
    CaseItem,
    CaseStore,
    TimelineKind,
    payload_digest,
)
from engine.doctor.protocol import AGENT
from engine.doctor.screen import screen_view
from engine.doctor.subjects import KEEP_KIND, item_subject
from engine.doctor.visit import plan_change_items
from engine.storage import LocalStorage
from tests.fixtures.doctor_eval.chat_model import ChatScript, Seen
from tests.unit.doctor import chat_support as cs
from tests.unit.doctor.chat_support import FIRST, Talk
from tests.unit.doctor.test_author import (
    FILE,
    NOW,
    PERSON,
    ai,
    by_id,
    create,
    ctx,
    make_case,
    persons,
    stored,
)
from tests.unit.doctor.test_doctor_chat import told
from tests.unit.doctor.visit_support import ANALYST, World

JOINED = "up-joined"
ORDERS = "up-orders"
SAID = CaseChatEntry(
    role="user", text="Do not remove the duplicate rows; I want to keep them.", at=NOW, by=PERSON
)
ANSWERED = CaseChatEntry(role="agent", text="Ok.", at=NOW)


def note_draft(rows: int = 10) -> ItemDraft:
    """The detectors' note about exact copies: the counts are the day's, the matter is the file's."""
    return ItemDraft(
        kind="data_check",
        payload={
            "upload_id": FILE,
            "code": "DUPLICATE_ROWS",
            "level": "flag",
            "column": None,
            "numbers": {"rows": rows, "file": "orders.csv", "file_rows": 2000},
        },
        title=f"{rows} rows of orders.csv are exact copies of another row.",
        upload_id=FILE,
        confidence=AgentConfidence.CHECK,
    )


def note(rows: int = 10, *, state: ProposalState = ProposalState.PENDING, **more: Any) -> CaseItem:
    given = note_draft(rows)
    return CaseItem(
        item_id=item_id_of(given.kind, given.payload, None),
        kind=given.kind,
        title=given.title,
        source=ProposalSource.RULES,
        confidence=AgentConfidence.CHECK,
        state=state,
        upload_id=FILE,
        payload=dict(given.payload),
        payload_digest=payload_digest(given.payload),
        proposed_at=NOW,
        producer="author",
        **more,
    )


def propose_note(rows: int) -> Create:
    return Create(draft=note_draft(rows), reason="found", basis="r1")


def keep_of(case: Case) -> CaseItem:
    return next(i for i in case.items if i.kind == KEEP_KIND)


def said(*items: CaseItem) -> Case:
    return make_case(items).model_copy(update={"chat": (SAID,)})


# -- keeping things as they are ---------------------------------------------------------------------------------
def test_keeping_is_recorded_as_the_persons_decision_citing_their_message_and_takes_the_waiting_note_out() -> (
    None
):
    waiting = note()
    out = apply_actions(
        said(waiting), [Keep(item_id=waiting.item_id, message="msg:0", reason="they want the copies")], ai()
    )
    assert out.refused == []
    keep = keep_of(out.case)
    assert (keep.state, keep.decided_by, keep.decided_at) == (ProposalState.ACCEPTED, PERSON, NOW)
    assert keep.source is ProposalSource.AI, "the AI recorded it; the decision is the person's"
    assert "msg:0" in keep.evidence_ids and keep.payload["message"] == "msg:0"
    assert keep.payload["subject"] == item_subject(waiting) == "data_check@DUPLICATE_ROWS:up-1"
    assert keep.title.startswith("Keep as it is: ") and waiting.title in keep.title
    old = by_id(out.case, waiting.item_id)
    assert old.state is ProposalState.REJECTED and old.superseded_by == keep.item_id
    assert (
        old.withdrawn_because and old.decided_by == AGENT
    ), "the Doctor took it back; the person's word is the keep"
    assert any(e.kind is TimelineKind.ITEM_DECIDED and e.details.get("why") == "kept" for e in out.events)


def test_a_note_the_doctor_already_took_back_can_be_kept_too_and_saying_it_again_adds_nothing() -> None:
    gone = note(state=ProposalState.REJECTED, decided_by=AGENT, withdrawn_because="no longer proposed")
    case = said(gone)
    first = apply_actions(case, [Keep(item_id=gone.item_id, message="msg:0", reason="keep them")], ai())
    assert first.refused == [] and keep_of(first.case).decided_by == PERSON
    again = apply_actions(
        first.case, [Keep(item_id=gone.item_id, message="msg:0", reason="other words")], ai()
    )
    assert again.refused == []
    assert [i.kind for i in again.case.items].count(KEEP_KIND) == 1


@pytest.mark.parametrize(
    ("make", "code"),
    [
        (lambda: note(state=ProposalState.ACCEPTED), "KEEP_IS_IN_THE_PLAN"),
        (lambda: stored("parse_number", state=ProposalState.ACCEPTED), "KEEP_IS_IN_THE_PLAN"),
        (lambda: persons(), "KEEP_IS_IN_THE_PLAN"),
    ],
)
def test_a_change_in_the_plan_cannot_be_kept_that_way_because_taking_it_out_waits_for_the_persons_yes(
    make: Any, code: str
) -> None:
    target = make()
    out = apply_actions(said(target), [Keep(item_id=target.item_id, message="msg:0", reason="r")], ai())
    assert [r.code for _i, r in out.refused] == [code]
    assert out.case.items == (target,), "nothing was recorded"


def test_a_stop_a_build_a_setting_or_a_meaning_is_not_a_thing_a_message_can_turn_down() -> None:
    stop = note().model_copy(
        update={"item_id": "data_check.base.stop", "payload": {**note().payload, "level": "stop"}}
    )
    build = stored("sql_query", "t").model_copy(
        update={
            "item_id": "recipe_step.base.build",
            "payload": {"buildable": {"goal": "x"}, "step": {"kind": "sql_query"}},
        }
    )
    setting = note().model_copy(update={"item_id": "goal_setting.g.1", "kind": "goal_setting"})
    for target in (stop, build, setting):
        out = apply_actions(said(target), [Keep(item_id=target.item_id, message="msg:0", reason="r")], ai())
        assert [r.code for _i, r in out.refused] == ["KEEP_NOT_ALLOWED"], target.item_id
        assert not any(i.kind == KEEP_KIND for i in out.case.items)


@pytest.mark.parametrize("message", ["msg:1", "msg:7", "msg:x", "the person said so", "msg:0 and msg:1", ""])
def test_a_keep_must_cite_a_message_the_person_wrote(message: str) -> None:
    waiting = note()
    case = make_case([waiting]).model_copy(update={"chat": (SAID, ANSWERED)})
    out = apply_actions(case, [Keep(item_id=waiting.item_id, message=message, reason="r")], ai())
    assert [r.code for _i, r in out.refused] == ["KEEP_NEEDS_THE_PERSONS_MESSAGE"]
    assert out.case.items == (waiting,)


def test_a_keep_of_a_change_that_is_not_there_is_refused_with_the_ids_that_are() -> None:
    out = apply_actions(said(note()), [Keep(item_id="nope", message="msg:0", reason="r")], ai())
    refusal = out.refused[0][1]
    assert isinstance(refusal, Refusal) and refusal.code == "ITEM_NOT_FOUND" and refusal.how_to_fix


def test_the_default_author_does_not_propose_a_kept_matter_again_even_with_other_counts() -> None:
    waiting = note(10)
    kept = apply_actions(
        said(waiting), [Keep(item_id=waiting.item_id, message="msg:0", reason="r")], ai()
    ).case
    for rows in (10, 12, 300):  # the copies come back: the same matter, another count, another id
        out = apply_actions(kept, [propose_note(rows)], ctx("default", complete=True))
        assert out.refused == []
        assert [
            i.item_id
            for i in out.case.items
            if i.kind == "data_check" and i.state is not ProposalState.REJECTED
        ] == []
        assert not any(i.state is ProposalState.PENDING for i in out.case.items)


def test_the_ai_is_told_a_kept_matter_is_the_persons_to_undo() -> None:
    waiting = note(10)
    kept = apply_actions(
        said(waiting), [Keep(item_id=waiting.item_id, message="msg:0", reason="r")], ai()
    ).case
    out = apply_actions(kept, [propose_note(12)], ai())
    refusal = out.refused[0][1]
    assert refusal.code == "KEPT_BY_PERSON" and refusal.value == keep_of(kept).item_id and refusal.how_to_fix
    assert len(out.case.items) == len(kept.items), "nothing was added"


def test_unticking_the_keep_undoes_it_and_the_note_is_proposed_again() -> None:
    waiting = note(10)
    kept = apply_actions(
        said(waiting), [Keep(item_id=waiting.item_id, message="msg:0", reason="r")], ai()
    ).case
    undone = kept.model_copy(
        update={
            "items": tuple(
                (
                    i.model_copy(update={"state": ProposalState.REJECTED, "decided_by": PERSON})
                    if i.kind == KEEP_KIND
                    else i
                )
                for i in kept.items
            )
        }
    )
    out = apply_actions(undone, [propose_note(12)], ctx("default", complete=True))
    again = [i for i in out.case.items if i.kind == "data_check" and i.state is ProposalState.PENDING]
    assert len(again) == 1 and again[0].title.startswith("12 rows"), "the matter is open again"


def test_ticking_the_keep_again_stands_for_the_matter_once_more() -> None:
    waiting = note(10)
    kept = apply_actions(
        said(waiting), [Keep(item_id=waiting.item_id, message="msg:0", reason="r")], ai()
    ).case
    assert keep_of(kept).state is ProposalState.ACCEPTED
    out = apply_actions(kept, [propose_note(11)], ctx("default", complete=True))
    assert not any(i.state is ProposalState.PENDING for i in out.case.items)


def test_a_kept_matter_does_not_hold_back_another_matter() -> None:
    waiting = note(10)
    kept = apply_actions(
        said(waiting), [Keep(item_id=waiting.item_id, message="msg:0", reason="r")], ai()
    ).case
    out = apply_actions(kept, [create("parse_number", "spend")], ctx("default", complete=True))
    assert any(i.kind == "recipe_step" for i in out.case.items)


def test_a_keep_is_a_decision_not_a_change_of_the_plan() -> None:
    waiting = note(10)
    kept = apply_actions(
        said(waiting), [Keep(item_id=waiting.item_id, message="msg:0", reason="r")], ai()
    ).case
    assert plan_change_items(kept) == (), "it is on the screen as a decision, with a way to undo it"


def test_a_complete_round_of_the_default_author_leaves_the_persons_keep_alone() -> None:
    waiting = note(10)
    kept = apply_actions(
        said(waiting), [Keep(item_id=waiting.item_id, message="msg:0", reason="r")], ai()
    ).case
    out = apply_actions(kept, [], ctx("default", complete=True))
    assert keep_of(out.case) == keep_of(kept)


def test_withdraw_still_takes_a_change_out_of_the_plan_for_the_persons_yes() -> None:
    target = stored("parse_number", state=ProposalState.ACCEPTED)
    out = apply_actions(said(target), [Withdraw(item_id=target.item_id, reason="r", consequence="c")], ai())
    assert out.refused == [] and not any(i.kind == KEEP_KIND for i in out.case.items)


# -- a decision about a column of a file goes on to the table a combine made of it ---------------------------------
def joined_case(*items: CaseItem, combine: bool = True) -> Case:
    join = CaseItem(
        item_id="build_dataset.base.join",
        kind="build_dataset",
        title="Combine the files",
        source=ProposalSource.RULES,
        confidence=AgentConfidence.CHECK,
        state=ProposalState.ACCEPTED,
        upload_id=FILE,
        payload={"upload_ids": [FILE, ORDERS]},
        payload_digest="x",
        proposed_at=NOW,
        decided_by=PERSON,
        decided_at=NOW,
    )
    files = (
        CaseFile(upload_id=FILE, file_name="customers.csv", added_at=NOW, added_by=PERSON),
        CaseFile(upload_id=ORDERS, file_name="orders.csv", added_at=NOW, added_by=PERSON),
        CaseFile(
            upload_id=JOINED,
            file_name="customers with orders.csv",
            original_upload_id=FILE,
            added_at=NOW,
            added_by=PERSON,
        ),
    )
    return make_case([*([join] if combine else []), *items]).model_copy(update={"files": files})


def on_table(column: str = "signup_date", kind: str = "remove_column") -> Create:
    """The same fix, proposed for the joined table."""
    given = create(kind, column)
    payload = {**given.draft.payload, "upload_id": JOINED}
    return given.model_copy(
        update={"draft": given.draft.model_copy(update={"upload_id": JOINED, "payload": payload})}
    )


def decided(column: str = "signup_date", *, state: ProposalState = ProposalState.ACCEPTED) -> CaseItem:
    return stored("remove_column", column, state=state, decided_by=PERSON)


def test_a_yes_to_removing_a_column_of_a_file_stands_for_the_same_column_of_the_table_made_of_it() -> None:
    yes = decided()
    for author in ("default", "ai"):
        out = apply_actions(joined_case(yes), [on_table()], ctx(author))
        assert out.refused == []
        made = next(i for i in out.case.items if i.upload_id == JOINED)
        assert (made.state, made.decided_by, made.decided_at) == (ProposalState.ACCEPTED, PERSON, NOW), author
        assert f"carried:{yes.item_id}" in made.evidence_ids
        assert any(e.details.get("carried_from") == yes.item_id for e in out.events)


def test_a_no_to_a_change_of_a_column_of_a_file_stands_for_the_table_made_of_it_too() -> None:
    no = decided(state=ProposalState.REJECTED)
    out = apply_actions(joined_case(no), [on_table()], ctx("default", complete=True))
    made = next(i for i in out.case.items if i.upload_id == JOINED)
    assert (made.state, made.decided_by) == (ProposalState.REJECTED, PERSON), "not proposed again for a yes"
    assert made.withdrawn_because is None
    again = apply_actions(out.case, [on_table()], ctx("default", complete=True))
    assert [i.state for i in again.case.items if i.upload_id == JOINED] == [ProposalState.REJECTED]


@pytest.mark.parametrize(
    "case",
    [
        joined_case(decided("other_column")),  # another column
        joined_case(decided(), combine=False),  # no combine made the table
        joined_case(
            stored("remove_column", "signup_date", state=ProposalState.ACCEPTED)
        ),  # nobody's decision
        joined_case(stored("remove_column", "signup_date", state=ProposalState.PENDING)),  # not decided
        joined_case(stored("remove_column", "signup_date", state=ProposalState.REJECTED, decided_by=AGENT)),
    ],
    ids=["other column", "no combine", "nobody decided", "still pending", "taken back by the Doctor"],
)
def test_only_the_persons_own_decision_about_the_same_column_of_the_same_source_carries_over(
    case: Case,
) -> None:
    out = apply_actions(case, [on_table()], ctx("default"))
    made = next(i for i in out.case.items if i.upload_id == JOINED)
    assert (
        made.decided_by is None and made.state is not ProposalState.REJECTED
    ), "it is proposed as it always was"


def test_a_decision_about_the_orders_file_does_not_carry_over_to_the_customer_table() -> None:
    other = decided().model_copy(update={"upload_id": ORDERS})
    out = apply_actions(joined_case(other), [on_table()], ctx("default"))
    made = next(i for i in out.case.items if i.upload_id == JOINED)
    assert made.decided_by is None and made.state is not ProposalState.REJECTED


def test_a_carried_decision_is_the_persons_even_after_the_author_says_it_again() -> None:
    first = apply_actions(joined_case(decided()), [on_table()], ctx("default", complete=True))
    again = apply_actions(first.case, [on_table()], ctx("default", complete=True))
    made = [i for i in again.case.items if i.upload_id == JOINED]
    assert len(made) == 1 and made[0].decided_by == PERSON and made[0].state is ProposalState.ACCEPTED


# -- the AI records it, through the turn ---------------------------------------------------------------------------
@pytest.fixture
def world() -> World:
    return cs.orders_world()


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path / "data")


def with_note(talk: Talk, *, state: ProposalState = ProposalState.PENDING, **more: Any) -> CaseItem:
    """The case after a detector's note about exact copies of the first file."""
    given = note(10, state=state, **more).model_copy(
        update={"upload_id": FIRST, "payload": {**note().payload, "upload_id": FIRST}}
    )
    store = CaseStore(talk.storage, clock=talk.world.clock)
    talk.case = store.update(
        talk.case.client_id,
        talk.case.case_id,
        expected_version=talk.case.version,
        actor=ANALYST,
        change=lambda current: (current.model_copy(update={"items": (*current.items, given)}), []),
    )
    return given


def keeps(item: str, reason: str = "They said to keep the copies.") -> Any:
    def action(seen: Seen) -> dict[str, object]:
        row = next(p for p in seen.state["plan"] if item in p["what"])
        return {"action": "keep_as_is", "args": {"item": row["id"], "reason": reason}}

    return action


def test_the_ai_records_the_persons_keep_citing_their_message_and_it_shows_in_the_reply(
    storage: LocalStorage, world: World
) -> None:
    client = ChatScript(
        {
            "keep the copies": [
                keeps("exact copies"),
                {"action": "reply", "text": "I will leave the copies in. That is your decision."},
            ]
        }
    )
    talk = Talk(storage, world, client)
    waiting = with_note(talk)
    case = talk.say("Please keep the copies, I want them.")
    keep = keep_of(case)
    assert (keep.state, keep.decided_by) == (ProposalState.ACCEPTED, ANALYST)
    assert keep.payload["message"] == "msg:0" and "msg:0" in keep.evidence_ids
    assert case.chat[0].text.startswith("Please keep"), "the message it cites is the person's own"
    assert by_id(case, waiting.item_id).state is ProposalState.REJECTED
    assert talk.reply.item_ids == (keep.item_id,), "the reply names the decision it recorded"
    assert "decision" in told(client, "kept")["result"] or "message" in told(client, "kept")["result"]
    # Their decision stands against the detectors: the same copies, in other numbers, are not a proposal.
    again = talk.say("what is in the plan?")
    assert [i.state for i in again.items if i.kind == "data_check"] == [ProposalState.REJECTED]


def test_the_ai_cannot_use_a_keep_to_take_a_change_out_of_the_plan(
    storage: LocalStorage, world: World
) -> None:
    client = ChatScript({"keep the copies": [keeps("exact copies")] * 3})
    talk = Talk(storage, world, client)
    in_plan = with_note(talk, state=ProposalState.ACCEPTED, decided_by=ANALYST)
    case = talk.say("Please keep the copies, I want them.")
    assert not any(i.kind == KEEP_KIND for i in case.items), "nothing was recorded"
    assert by_id(case, in_plan.item_id).state is ProposalState.ACCEPTED, "and nothing was taken out"
    assert any(r.code == "KEEP_IS_IN_THE_PLAN" for r in talk.reply.refused), "the model was told how to do it"


def test_unticking_the_keep_in_the_screen_brings_the_note_back(storage: LocalStorage, world: World) -> None:
    client = ChatScript({"keep the copies": [keeps("exact copies"), {"action": "reply", "text": "Kept."}]})
    talk = Talk(storage, world, client)
    with_note(talk)
    case = talk.say("Please keep the copies, I want them.")
    keep = keep_of(case)
    review = screen_view(storage, case, config_root=None).review
    shown = [d for d in review.decisions if d.item_id == keep.item_id]
    assert (
        len(shown) == 1 and shown[0].decided_by == "person" and shown[0].answered and not shown[0].needs_yes
    )
    case = talk.decide(keep.item_id, False)  # Change: undo it
    assert by_id(case, keep.item_id).state is ProposalState.REJECTED
    assert not [
        d for d in screen_view(storage, case, config_root=None).review.decisions if d.item_id == keep.item_id
    ]
