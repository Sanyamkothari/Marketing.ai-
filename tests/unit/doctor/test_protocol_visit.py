"""The protocol inside the visit (Plan O M246, DEC-1816; the design §6.5-§6.8, §7.2).

Acceptance (Plan O §M246), through a real case:

* An ambiguity below the impact threshold is decided, with a stated, revocable assumption.
* A person cannot approve their own APPROVE-class item when separation is on.
* Ledger entries verify; with the ledger off, the timeline holds the same record.
* Every number in a doubt is grounded (a detector's ungrounded doubt stops the visit with `DOUBT_UNGROUNDED`).

And the protocol's other parts: answers remembered per client and applied next time, visibly and revocably, never
for APPROVE-class items; the exact sentence at Approve; the question budget; requests for more data; approved AI
patterns counted towards a candidate rule ([REQ] 11). The detectors here are fakes in the shape M245's will have.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue

from engine.access.roles import Principal, Role
from engine.agent.contracts import AgentConfidence, ProposalSource, ProposalState
from engine.agentic.ledger import DecisionLedger, FakeSigner, verify_ledger
from engine.doctor import visit
from engine.doctor.case import Case, CaseError, CaseItem, CaseStore, TimelineKind, VisitState, payload_digest
from engine.doctor.diagnose import LoadedFile
from engine.doctor.goal_brief import GoalBrief
from engine.doctor.patterns import candidate_rules, record_approved
from engine.doctor.protocol import Ask, AskOption, DataRequest, express_doubt, ledger_port
from engine.doctor.registry import DataToolKind, DataToolResult, Exposure
from engine.doctor.standing import StandingAnswerStore
from engine.doctor.visit import Detected, VisitDeps
from engine.doctor.visit_contracts import OptionEffect
from engine.platform_db import PLATFORM_DB_FILENAME, sqlite_engine
from engine.storage import LocalStorage
from tests.unit.doctor import visit_support as vs
from tests.unit.doctor.test_visit import _MEMORY, _Cached
from tests.unit.doctor.visit_support import ANALYST, CLIENT, FIRST, GOAL_A, World, open_case, option, question

ASHA = Principal(user_id=ANALYST, username="asha", roles=frozenset({Role.ANALYST}), kind="user")
MEERA = Principal(user_id="u-meera", username="meera", roles=frozenset({Role.ANALYST}), kind="user")
RAVI = Principal(user_id="u-ravi", username="ravi", roles=frozenset({Role.APPROVER}), kind="user")


@pytest.fixture
def world() -> World:
    return vs.world()


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path / "data")


Detector = Callable[[Case, tuple[LoadedFile, ...], GoalBrief | None], Detected]


def _deps(storage: LocalStorage, w: World, *detectors: Detector, **extra: Any) -> VisitDeps:
    return vs.deps_for(
        storage, w, registry=_Cached(visit.visit_registry(), _MEMORY), detectors=tuple(detectors), **extra
    )


def _found(
    *asks: Ask, doubts: Sequence[Any] = (), requests: Sequence[DataRequest] = (), evidence: Sequence[Any] = ()
) -> Detector:
    def detect(case: Case, files: tuple[LoadedFile, ...], brief: GoalBrief | None) -> Detected:
        del case, files, brief
        return Detected(asks=asks, doubts=tuple(doubts), requests=tuple(requests), evidence=tuple(evidence))

    return detect


def _step_effect(option_id: str, kind: str, column: str, **params: Any) -> OptionEffect:
    return OptionEffect(
        option_id=option_id,
        kind="recipe_step",
        title=f"{kind} on “{column}”",
        payload={"upload_id": FIRST, "step": {"kind": kind, "column": column, "params": params}},
    )


def _boolean_ask(*, effect_kind: str = "map_boolean", rows_disagree: int = 600) -> Ask:
    """A yes/no column written `haa` / `nahi` (an unknown spelling): read them as yes and no, or leave as text."""
    return Ask(
        code="UNKNOWN_BOOLEAN",
        subject={"column": "marketing_opt_in", "values": ["haa", "nahi"]},
        text="In “marketing_opt_in”, do “haa” and “nahi” mean yes and no?",
        why=f"{rows_disagree:,} of 1,200 values are written that way.",
        options=(
            AskOption(
                "Yes: haa is yes, nahi is no",
                _step_effect(
                    "c0", effect_kind, "marketing_opt_in", true_values=["haa"], false_values=["nahi"]
                ),
                rows_changed=rows_disagree,
            ),
            AskOption("Leave them as written", OptionEffect(option_id="c1"), rows_changed=0, safe=True),
        ),
        rows=1_200,
        rows_disagree=rows_disagree,
        upload_id=FIRST,
        column="marketing_opt_in",
        value_signature=("haa", "nahi"),
    )


def _open(storage: LocalStorage, w: World, *detectors: Detector, client: str = CLIENT) -> Case:
    created = CaseStore(storage, clock=w.clock).create(
        client_id=client, created_by=ANALYST, files=[vs.case_file(FIRST, w)]
    )
    return visit.resume(client, created.case_id, _deps(storage, w, *detectors), actor=ANALYST)


def _answer_key(storage: LocalStorage, w: World, case: Case, *detectors: Detector) -> Case:
    q = question(case, "KEY_AMBIGUOUS")
    return visit.answer(
        case.client_id, case.case_id, q.question_id, option(q, "customer_id"), _deps(storage, w, *detectors),
        expected_version=case.version, actor=ANALYST,
    )  # fmt: skip


def _by_code(case: Case, code: str) -> Any:
    return next(q for q in case.questions if q.code == code)


def _events(storage: LocalStorage, case: Case, kind: TimelineKind) -> list[Any]:
    return [e for e in CaseStore(storage).timeline(case.client_id, case.case_id) if e.kind is kind]


# ---------------------------------------------------------------------------
# ASK in the visit: an assumption below the threshold; the budget; requests for data
# ---------------------------------------------------------------------------
def test_an_ambiguity_below_the_impact_threshold_is_decided_with_a_stated_revocable_assumption(
    storage: LocalStorage, world: World
) -> None:
    small = _found(_boolean_ask(rows_disagree=3))  # 3 of 1,200 rows (0.25%): below 0.5%
    case = _open(storage, world, small)
    assumed = _by_code(case, "UNKNOWN_BOOLEAN")
    assert assumed.assumed and assumed.answer == "c1", "the rules took the safe choice and said so"
    assert case.counters.questions_per_file.get(FIRST, 0) == 1, "only the key question was asked"
    (event,) = _events(storage, case, TimelineKind.ASSUMED)
    assert event.details["option"] == "c1"
    case = visit.answer(
        CLIENT, case.case_id, assumed.question_id, "c0", _deps(storage, world, small),
        expected_version=case.version, actor=ANALYST,
    )  # fmt: skip
    changed = _by_code(case, "UNKNOWN_BOOLEAN")
    assert changed.answer == "c0" and not changed.assumed and changed.answered_by == ANALYST
    assert any(
        i.item_id == f"{changed.question_id}:c0" and i.state is ProposalState.ACCEPTED for i in case.items
    )


def test_a_material_ambiguity_is_one_question_with_i_dont_know(storage: LocalStorage, world: World) -> None:
    case = _open(storage, world, _found(_boolean_ask()))
    asked = _by_code(case, "UNKNOWN_BOOLEAN")
    assert asked.answer is None and asked.why == "600 of 1,200 values are written that way."
    assert [o.label for o in asked.options][-1] == "Leave them as written" and asked.options[-1].safe


def test_questions_past_the_files_budget_take_the_safe_choice(storage: LocalStorage, world: World) -> None:
    asks = [
        replace(_boolean_ask(), code="NEW_CATEGORY", subject={"column": f"c{index}"}, column=f"c{index}")
        for index in range(12)
    ]
    case = _open(storage, world, _found(*asks))
    new = [q for q in case.questions if q.code == "NEW_CATEGORY"]
    assert len(new) == 12
    assert (
        sum(1 for q in new if q.answer is None) == 9
    ), "10 questions a file: the key question and 9 of these"
    assert all(q.assumed and q.answer == "c1" for q in new if q.answer is not None)
    assert case.counters.questions_per_file[FIRST] == 10


def test_a_detectors_request_for_more_data_is_one_question_that_does_not_block(
    storage: LocalStorage, world: World
) -> None:
    request = DataRequest(
        kind="more_data",
        what="the date of each customer's last order",
        why="Win back needs it.",
        columns=("customer_id", "last_order_date"),
        ask_whom="your billing team",
        upload_id=FIRST,
    )
    case = _open(storage, world, _found(requests=(request,)))
    asked = _by_code(case, "DATA_REQUESTED")
    assert not asked.blocking and asked.text.startswith("Ask your billing team for the date")
    assert _events(storage, case, TimelineKind.DATA_REQUESTED)


# ---------------------------------------------------------------------------
# Doubts: grounded, or the visit stops
# ---------------------------------------------------------------------------
def _evidence(output: dict[str, Any]) -> DataToolResult:
    from engine.doctor.registry import data_evidence_id

    return DataToolResult(
        evidence_id=data_evidence_id("profile_column", {"column": "monthly_spend"}, output),
        tool="profile_column",
        kind=DataToolKind.READ,
        exposure=Exposure.AGGREGATE,
        input={"column": "monthly_spend", "upload_id": FIRST},
        output=output,
        computed_at=vs.T0,
    )


def test_every_number_in_a_doubt_is_grounded(storage: LocalStorage, world: World) -> None:
    result = _evidence({"rows": 312, "near": 2, "median": 2_000, "mean": 1_880, "mean_if_thousands": 2_090})
    doubt = express_doubt(
        "UNIT_MISMATCH",
        because="“monthly_spend” is near 2,000 for most rows and near 2 for 312 rows, which could be thousands.",
        would_do="leave these 312 values as written and flag the column.",
        if_not="if they are thousands, the average rises from 1,880 to 2,090.",
        evidence={result.evidence_id: result.output},
        upload_id=FIRST,
    )
    case = _open(storage, world, _found(doubts=(doubt,), evidence=(result,)))
    assert [d.doubt_id for d in case.doubts] == [doubt.doubt_id]
    assert _events(storage, case, TimelineKind.DOUBT)
    loose = doubt.model_copy(
        update={"doubt_id": "d.loose", "if_not": "the average rises from 1,880 to 2,450."}
    )
    stopped = _open(storage, world, _found(doubts=(loose,), evidence=(result,)))
    assert stopped.visit is not None and stopped.visit.stopped is not None
    assert stopped.visit.stopped.code == "DOUBT_UNGROUNDED" and not stopped.doubts


# ---------------------------------------------------------------------------
# Standing answers: remembered per client, applied next time visibly, revocable, never APPROVE-class
# ---------------------------------------------------------------------------
def test_an_answer_is_remembered_for_the_client_and_applied_next_time_visibly_and_revocably(
    storage: LocalStorage, world: World
) -> None:
    detector = _found(_boolean_ask())
    first = _open(storage, world, detector)
    asked = _by_code(first, "UNKNOWN_BOOLEAN")
    first = visit.answer(
        CLIENT, first.case_id, asked.question_id, "c0", _deps(storage, world, detector),
        expected_version=first.version, actor=ANALYST,
    )  # fmt: skip
    store = StandingAnswerStore(storage)
    (remembered,) = store.list(CLIENT)
    assert remembered.answered_by == ANALYST and remembered.value_signature == ("haa", "nahi")
    assert _events(storage, first, TimelineKind.STANDING_REMEMBERED)

    second = _open(storage, world, detector)  # next month's case of the same client
    applied = _by_code(second, "UNKNOWN_BOOLEAN")
    assert applied.answer == "c0" and applied.standing_answer_id == remembered.answer_id
    (item,) = [i for i in second.items if i.standing_answer_id == remembered.answer_id]
    assert item.state is ProposalState.ACCEPTED and item.title.startswith("Using your answer from 12 Oct")
    assert [u.answer_id for u in second.standing_answers_used] == [remembered.answer_id]
    assert _events(storage, second, TimelineKind.STANDING_APPLIED)
    assert store.list(CLIENT)[0].used_in == (second.case_id,)

    revoked = visit.revoke_standing(
        CLIENT, second.case_id, remembered.answer_id, _deps(storage, world, detector),
        expected_version=second.version, actor=ANALYST,
    )  # fmt: skip
    again = _by_code(revoked, "UNKNOWN_BOOLEAN")
    assert again.answer is None and again.standing_answer_id is None, "revoking asks the question again"
    assert not [i for i in revoked.items if i.standing_answer_id == remembered.answer_id]
    assert revoked.standing_answers_used[0].revoked_at is not None
    third = _open(storage, world, detector)
    assert _by_code(third, "UNKNOWN_BOOLEAN").answer is None, "a revoked answer is asked again next time"

    other = _open(storage, world, detector, client="c_other_1")
    assert _by_code(other, "UNKNOWN_BOOLEAN").answer is None, "another client's answers never apply"


def test_an_approve_class_answer_is_never_stored_as_standing(storage: LocalStorage, world: World) -> None:
    detector = _found(_boolean_ask(effect_kind="remove_rows"))
    case = _open(storage, world, detector)
    asked = _by_code(case, "UNKNOWN_BOOLEAN")
    visit.answer(
        CLIENT, case.case_id, asked.question_id, "c0", _deps(storage, world, detector),
        expected_version=case.version, actor=ANALYST,
    )  # fmt: skip
    assert StandingAnswerStore(storage).list(CLIENT) == ()


# ---------------------------------------------------------------------------
# APPROVE: own yes, role, exact sentence, separation of duties, the record
# ---------------------------------------------------------------------------
def _remove_rows_ask() -> Ask:
    return Ask(
        code="IMPOSSIBLE_AGE",
        subject={"column": "age"},
        text="Leave out the rows whose age is over 120?",
        why="",
        options=(
            AskOption("Leave them out", _step_effect("c0", "remove_rows", "age"), rows_changed=40),
            AskOption("Keep them", OptionEffect(option_id="c1"), rows_changed=0, safe=True),
        ),
        rows=1_200,
        upload_id=FIRST,
        column="age",
    )


def _at_approval(storage: LocalStorage, w: World, *detectors: Detector, **extra: Any) -> Case:
    """Goal A, the key and the outcome answered, the analyst's own remove-rows answer ticked: waiting at APPROVE."""
    deps = _deps(storage, w, *detectors, **extra)
    case = visit.resume(CLIENT, open_case(storage, w).case_id, deps, actor=ANALYST)
    case = _answer_key(storage, w, case, *detectors)
    case = visit.set_goal(CLIENT, case.case_id, GOAL_A, deps, expected_version=case.version, actor=ANALYST)
    q = question(case, "OUTCOME_COLUMN")
    case = visit.answer(
        CLIENT, case.case_id, q.question_id, option(q, "converted"), deps, expected_version=case.version,
        actor=ANALYST,
    )  # fmt: skip
    asked = _by_code(case, "IMPOSSIBLE_AGE")
    case = visit.answer(
        CLIENT, case.case_id, asked.question_id, "c0", deps, expected_version=case.version, actor=ANALYST
    )
    assert case.state is VisitState.APPROVE, case.state
    return case


def _item(case: Case) -> CaseItem:
    return next(i for i in case.items if i.payload.get("step", {}).get("kind") == "remove_rows")  # type: ignore[union-attr]


def test_a_person_cannot_approve_their_own_approve_class_item_when_separation_is_on(
    storage: LocalStorage, world: World
) -> None:
    detector = _found(_remove_rows_ask())
    deps = _deps(storage, world, detector)
    case = _at_approval(storage, world, detector)
    item = _item(case)
    assert item.decided_by == ANALYST
    with pytest.raises(CaseError) as missing:
        visit.approve(CLIENT, case.case_id, deps, expected_version=case.version, actor=ANALYST)
    assert missing.value.code == "APPROVAL_CONFIRMATION_MISSING"
    with pytest.raises(CaseError) as own:
        visit.confirm(
            CLIENT, case.case_id, item.item_id, deps, principal=ASHA, sentence=item.title,
            expected_version=case.version,
        )  # fmt: skip
    assert own.value.code == "POLICY_ROLE_SEPARATION"
    with pytest.raises(CaseError) as changed:
        visit.confirm(
            CLIENT, case.case_id, item.item_id, deps, principal=MEERA, sentence="Leave out some rows.",
            expected_version=case.version,
        )  # fmt: skip
    assert changed.value.code == "APPROVAL_SENTENCE_CHANGED" and item.title in changed.value.message
    assert CaseStore(storage).get(CLIENT, case.case_id).version == case.version, "a refusal records nothing"
    case = visit.confirm(
        CLIENT, case.case_id, item.item_id, deps, principal=MEERA, sentence=item.title,
        expected_version=case.version,
    )  # fmt: skip
    yes = case.approvals[-1]
    assert yes.approve_class and yes.approved_by == "u-meera" and yes.text_shown == item.title
    assert yes.role == "analyst" and yes.receipt_id == f"timeline:{yes.approval_id}"
    assert visit.unconfirmed(case) == ()
    with pytest.raises(CaseError) as wrong:
        visit.approve(
            CLIENT, case.case_id, deps, expected_version=case.version, actor=ANALYST, sentence="Prepare it."
        )
    assert wrong.value.code == "APPROVAL_SENTENCE_CHANGED"
    case = visit.approve(
        CLIENT, case.case_id, deps, expected_version=case.version, actor=ANALYST,
        sentence=visit.approval_sentence(case),
    )  # fmt: skip
    assert case.state is VisitState.FOLLOW_UP and case.approvals[-1].approved_by == ANALYST


def test_an_approver_item_needs_the_approver_role(storage: LocalStorage, world: World) -> None:
    ask = replace(
        _remove_rows_ask(),
        options=(
            AskOption(
                "Keep it for the model",
                OptionEffect(
                    option_id="c0",
                    kind="pii_keep",
                    title="Keep “age” for the model.",
                    payload={"column": "age"},
                ),
                rows_changed=1_200,
            ),
            AskOption("Hide it", OptionEffect(option_id="c1"), rows_changed=0, safe=True),
        ),
    )
    detector = _found(ask)
    deps = _deps(storage, world, detector)
    case = _at_approval(storage, world, detector)
    item = next(i for i in case.items if i.kind == "pii_keep")
    with pytest.raises(CaseError) as refused:
        visit.confirm(
            CLIENT, case.case_id, item.item_id, deps, principal=MEERA, sentence=item.title,
            expected_version=case.version,
        )  # fmt: skip
    assert refused.value.code == "TOOL_ROLE_REQUIRED"
    case = visit.confirm(
        CLIENT, case.case_id, item.item_id, deps, principal=RAVI, sentence=item.title,
        expected_version=case.version,
    )  # fmt: skip
    assert case.approvals[-1].role == "approver"


def _without(details: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {k: v for k, v in details.items() if k not in keys and v is not None}


def test_ledger_entries_verify_and_without_the_ledger_the_timeline_holds_the_same_record(
    tmp_path: Path, world: World
) -> None:
    detector = _found(_remove_rows_ask())
    records: dict[str, list[dict[str, Any]]] = {}
    for mode in ("ledger", "timeline"):
        storage = LocalStorage(tmp_path / mode)
        engine = sqlite_engine(tmp_path / f"{mode}-{PLATFORM_DB_FILENAME}")
        signer = FakeSigner()
        ledger = ledger_port(engine, signer, "p" * 64) if mode == "ledger" else None
        case = _at_approval(storage, world, detector, ledger=ledger)
        deps = _deps(storage, world, detector, ledger=ledger)
        item = _item(case)
        case = visit.confirm(
            CLIENT, case.case_id, item.item_id, deps, principal=MEERA, sentence=item.title,
            expected_version=case.version,
        )  # fmt: skip
        case = visit.approve(CLIENT, case.case_id, deps, expected_version=case.version, actor=ANALYST)
        events = [
            _without(dict(e.details), "approval", "role", "receipt", "items", "goal")
            for e in CaseStore(storage).timeline(CLIENT, case.case_id)
            if e.kind in {TimelineKind.CONFIRMED, TimelineKind.APPROVAL}
        ]
        records[mode] = events
        if mode == "ledger":
            entries = list(DecisionLedger(engine, signer=signer, policy_version="p" * 64).entries())
            assert [e.kind for e in entries] == ["data.decision", "data.prescription.approved"]
            assert verify_ledger(engine, signer).ok
            assert all(
                e.signature is not None and e.key_id == signer.key_id for e in entries
            ), "an approval is signed, like every approval of Plan K's ledger"
            bodies = [_without(dict(e.body), "policy_version", "case_id") for e in entries]
            assert bodies == events, "the timeline holds what the ledger holds"
            assert all(e.body["case_id"] == case.case_id for e in entries)
            assert [a.receipt_id for a in case.approvals[-2:]] == [f"ledger:{e.entry_hash}" for e in entries]
        else:
            assert [a.receipt_id for a in case.approvals[-2:]] == [
                f"timeline:{a.approval_id}" for a in case.approvals[-2:]
            ]
    assert records["ledger"] == records["timeline"], "with the ledger off, the timeline holds the same record"


# ---------------------------------------------------------------------------
# [REQ] 11: approved AI patterns become candidate rules for the product team
# ---------------------------------------------------------------------------
def test_a_pattern_approved_again_and_again_becomes_a_candidate_rule(storage: LocalStorage) -> None:
    step = ("map_boolean", {"true_values": ["Haa", "haan"], "false_values": ["nahi"]})
    same = ("map_boolean", {"true_values": ["haan", "haa "], "false_values": ["Nahi"]})
    record_approved(storage, [step], place="case_a", client_id="c1", at=vs.T0)
    record_approved(storage, [step], place="case_a", client_id="c1", at=vs.T0)  # the same place counts once
    record_approved(storage, [same], place="case_b", client_id="c2", at=vs.T0)
    assert candidate_rules(storage) == ()
    (pattern,) = record_approved(storage, [step], place="up-3", client_id="c1", at=vs.T0)
    assert pattern.approvals == 3 and pattern.clients == ("c1", "c2") and pattern.candidate
    assert [p.pattern_id for p in candidate_rules(storage)] == [pattern.pattern_id]


def test_approving_an_ai_fix_in_a_case_counts_its_pattern(storage: LocalStorage, world: World) -> None:
    step: dict[str, JsonValue] = {
        "kind": "map_boolean",
        "column": "marketing_opt_in",
        "params": {"true_values": ["1"], "false_values": ["0"]},
    }
    payload: dict[str, JsonValue] = {"upload_id": FIRST, "path": None, "value": None, "step": step}

    def author(case: Case, files: tuple[LoadedFile, ...], brief: Any, event: visit.TurnEvent) -> Any:
        del files, brief, event
        item = CaseItem(
            item_id="recipe_step.base.ai1",
            kind="recipe_step",
            title="Read “marketing_opt_in” as yes and no",
            source=ProposalSource.AI,
            confidence=AgentConfidence.CHECK,
            upload_id=FIRST,
            payload=payload,
            payload_digest=payload_digest(payload),
            proposed_at=vs.T0,
        )
        return vs.turn_of("Here is a fix.", items=(item,))

    deps = _deps(storage, world, author=author)
    case = visit.resume(CLIENT, open_case(storage, world).case_id, deps, actor=ANALYST)
    case = _answer_key(storage, world, case)
    case = visit.chat(
        CLIENT, case.case_id, "Fix the yes/no column", deps, expected_version=case.version, actor=ANALYST
    )
    (ai,) = [i for i in case.items if i.source is ProposalSource.AI]
    assert ai.state is ProposalState.PENDING, "new patterns only with permission"
    case = visit.decide(
        CLIENT, case.case_id, ai.item_id, True, deps, expected_version=case.version, actor=ANALYST
    )
    visit.approve(CLIENT, case.case_id, deps, expected_version=case.version, actor=ANALYST)
    from engine.doctor.patterns import PATTERNS_KEY, ApprovedPatterns

    (pattern,) = storage.read_model(PATTERNS_KEY, ApprovedPatterns).patterns
    assert pattern.kind == "map_boolean" and pattern.places == (case.case_id,) and not pattern.candidate


# ---------------------------------------------------------------------------
# Review fixes: a record only for a write that goes ahead, once; standing answers keep their meaning; everyone
# who proposed or answered an item is kept from approving it
# ---------------------------------------------------------------------------
class _Racing(CaseStore):
    """A case store where another writer gets in first (`before`), or the write fails after the change (`commit`)."""

    def __init__(self, storage: LocalStorage, w: World, race: str) -> None:
        super().__init__(storage, clock=w.clock)
        self.race = race

    def update(self, client_id: str, case_id: str, *, expected_version: int, actor: str, change: Any) -> Case:
        if self.race == "before":
            self.race = ""
            super().update(
                client_id,
                case_id,
                expected_version=expected_version,
                actor="u-other",
                change=lambda c: (c, []),
            )
        return super().update(
            client_id, case_id, expected_version=expected_version, actor=actor, change=change
        )

    def _commit(self, case: Case, drafts: Any, *, actor: str) -> Case:
        if self.race == "commit":
            self.race = ""
            raise OSError("the disk filled up")
        return super()._commit(case, drafts, actor=actor)


def _racing(deps: VisitDeps, storage: LocalStorage, w: World, race: str) -> VisitDeps:
    return replace(deps, store=_Racing(storage, w, race))


def _ledger_entries(engine: Any, kind: str) -> list[Any]:
    return list(DecisionLedger(engine, signer=FakeSigner(), policy_version="p" * 64).entries(kind=kind))


def test_an_approval_another_writer_got_in_before_writes_no_ledger_entry(
    tmp_path: Path, storage: LocalStorage, world: World
) -> None:
    detector = _found(_remove_rows_ask())
    engine = sqlite_engine(tmp_path / PLATFORM_DB_FILENAME)
    ledger = ledger_port(engine, FakeSigner(), "p" * 64)
    case = _at_approval(storage, world, detector, ledger=ledger)
    item = _item(case)
    deps = _deps(storage, world, detector, ledger=ledger)
    with pytest.raises(CaseError) as raced:
        visit.confirm(
            CLIENT, case.case_id, item.item_id, _racing(deps, storage, world, "before"), principal=MEERA,
            sentence=item.title, expected_version=case.version,
        )  # fmt: skip
    assert raced.value.code == "CASE_VERSION_CONFLICT"
    assert _ledger_entries(engine, "data.decision") == [], "no record of an approval the case never stored"


def test_a_retried_approval_whose_write_failed_finds_its_ledger_entry_instead_of_adding_one(
    tmp_path: Path, storage: LocalStorage, world: World
) -> None:
    detector = _found(_remove_rows_ask())
    engine = sqlite_engine(tmp_path / PLATFORM_DB_FILENAME)
    ledger = ledger_port(engine, FakeSigner(), "p" * 64)
    case = _at_approval(storage, world, detector, ledger=ledger)
    item = _item(case)
    deps = _deps(storage, world, detector, ledger=ledger)
    with pytest.raises(OSError):
        visit.confirm(
            CLIENT, case.case_id, item.item_id, _racing(deps, storage, world, "commit"), principal=MEERA,
            sentence=item.title, expected_version=case.version, op_id="op-yes",
        )  # fmt: skip
    assert CaseStore(storage).get(CLIENT, case.case_id).version == case.version, "the case was not written"
    case = visit.confirm(
        CLIENT, case.case_id, item.item_id, deps, principal=MEERA, sentence=item.title,
        expected_version=case.version, op_id="op-yes",
    )  # fmt: skip
    (entry,) = _ledger_entries(engine, "data.decision")
    assert case.approvals[-1].receipt_id == f"ledger:{entry.entry_hash}"
    assert entry.body["approval_id"] == case.approvals[-1].approval_id
    with pytest.raises(OSError):
        visit.approve(
            CLIENT, case.case_id, _racing(deps, storage, world, "commit"), expected_version=case.version,
            actor=ANALYST, op_id="op-approve",
        )  # fmt: skip
    case = visit.approve(
        CLIENT, case.case_id, deps, expected_version=case.version, actor=ANALYST, op_id="op-approve"
    )
    (approved,) = _ledger_entries(engine, "data.prescription.approved")
    assert [a.receipt_id for a in case.approvals] == [
        f"ledger:{entry.entry_hash}",
        f"ledger:{approved.entry_hash}",
    ]
    assert verify_ledger(engine, FakeSigner()).ok


def test_a_refused_answer_is_not_remembered_for_the_client(storage: LocalStorage, world: World) -> None:
    detector = _found(_boolean_ask())
    case = _open(storage, world, detector)
    asked = _by_code(case, "UNKNOWN_BOOLEAN")
    with pytest.raises(CaseError) as raced:
        visit.answer(
            CLIENT, case.case_id, asked.question_id, "c0",
            _racing(_deps(storage, world, detector), storage, world, "before"),
            expected_version=case.version, actor=ANALYST,
        )  # fmt: skip
    assert raced.value.code == "CASE_VERSION_CONFLICT"
    assert (
        StandingAnswerStore(storage).list(CLIENT) == ()
    ), "nothing applies later from an answer never stored"


def test_a_refused_revoke_leaves_the_standing_answer_standing(storage: LocalStorage, world: World) -> None:
    detector = _found(_boolean_ask())
    first = _open(storage, world, detector)
    asked = _by_code(first, "UNKNOWN_BOOLEAN")
    visit.answer(
        CLIENT, first.case_id, asked.question_id, "c0", _deps(storage, world, detector),
        expected_version=first.version, actor=ANALYST,
    )  # fmt: skip
    second = _open(storage, world, detector)
    (remembered,) = StandingAnswerStore(storage).list(CLIENT)
    with pytest.raises(CaseError):
        visit.revoke_standing(
            CLIENT, second.case_id, remembered.answer_id,
            _racing(_deps(storage, world, detector), storage, world, "before"),
            expected_version=second.version, actor=ANALYST,
        )  # fmt: skip
    assert StandingAnswerStore(storage).list(CLIENT, include_revoked=False) == (remembered,)


def test_a_standing_answer_is_not_applied_when_the_same_button_now_makes_another_change(
    storage: LocalStorage, world: World
) -> None:
    first_detector = _found(_boolean_ask())
    first = _open(storage, world, first_detector)
    asked = _by_code(first, "UNKNOWN_BOOLEAN")
    visit.answer(
        CLIENT, first.case_id, asked.question_id, "c0", _deps(storage, world, first_detector),
        expected_version=first.version, actor=ANALYST,
    )  # fmt: skip
    (remembered,) = StandingAnswerStore(storage).list(CLIENT)
    same_label = _boolean_ask()
    swapped = replace(
        same_label,
        options=(
            AskOption(
                same_label.options[0].label,  # the same words on the button ...
                _step_effect(  # ... but now it reads the spellings the other way round
                    "c0", "map_boolean", "marketing_opt_in", true_values=["nahi"], false_values=["haa"]
                ),
                rows_changed=600,
            ),
            same_label.options[1],
        ),
    )
    second = _open(storage, world, _found(swapped))
    again = _by_code(second, "UNKNOWN_BOOLEAN")
    assert again.answer is None and again.standing_answer_id is None, "the question is asked again"
    assert again.why.startswith("Your answer from 12 Oct") and "was not used" in again.why
    assert not [i for i in second.items if i.standing_answer_id == remembered.answer_id]
    (unfit,) = _events(storage, second, TimelineKind.STANDING_UNFIT)
    assert unfit.item_id == remembered.answer_id
    third = _open(storage, world, first_detector)  # the same change as before, on next month's file
    assert _by_code(third, "UNKNOWN_BOOLEAN").standing_answer_id == remembered.answer_id


KIRAN = Principal(user_id="u-kiran", username="kiran", roles=frozenset({Role.ANALYST}), kind="user")


def test_the_person_who_answered_an_items_question_cannot_confirm_it_after_someone_else_reticks_it(
    storage: LocalStorage, world: World
) -> None:
    detector = _found(_remove_rows_ask())
    deps = _deps(storage, world, detector)
    case = _at_approval(storage, world, detector)  # Asha answered the remove-rows question
    item = _item(case)
    for accept in (False, True):  # Meera unticks it and ticks it again: she decided it last
        case = visit.decide(
            CLIENT, case.case_id, item.item_id, accept, deps, expected_version=case.version, actor="u-meera"
        )
    item = next(i for i in case.items if i.item_id == item.item_id)
    assert item.decided_by == "u-meera" and item.state is ProposalState.ACCEPTED
    for person in (ASHA, MEERA):
        with pytest.raises(CaseError) as refused:
            visit.confirm(
                CLIENT, case.case_id, item.item_id, deps, principal=person, sentence=item.title,
                expected_version=case.version,
            )  # fmt: skip
        assert refused.value.code == "POLICY_ROLE_SEPARATION", person.user_id
    case = visit.confirm(
        CLIENT, case.case_id, item.item_id, deps, principal=KIRAN, sentence=item.title,
        expected_version=case.version,
    )  # fmt: skip
    assert case.approvals[-1].approved_by == "u-kiran"
