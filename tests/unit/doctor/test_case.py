"""The case (Plan O M231, DEC-1801): the contract round-trips, one writer wins, clients do not see each other.

Acceptance (Plan O §M231): the round-trip property test on `Case`; two concurrent writers, the second
refused with `CASE_VERSION_CONFLICT` (threads, and separate processes that share no lock but the store's);
another client's case is `CASE_NOT_FOUND`. Also: the timeline survives a crash between the two writes, a
crashed writer's lock is broken, the adapters are read-only, and the switch reads off by default.
"""

from __future__ import annotations

import json
import multiprocessing
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from engine.agent.contracts import (
    AgentConfidence,
    AgentSession,
    ProposalSource,
    ProposalState,
    SessionStatus,
)
from engine.doctor.case import (
    LOCK_FILENAME,
    Case,
    CaseApproval,
    CaseChatEntry,
    CaseError,
    CaseFile,
    CaseItem,
    CaseOption,
    CaseQuestion,
    CaseSource,
    CaseStore,
    ChatSuggestion,
    StandingAnswerUse,
    TimelineEvent,
    TimelineKind,
    VisitState,
    case_dir,
    case_from_diagnosis,
    case_from_session,
    case_key,
    forget_upload,
    payload_digest,
    remove_principal,
    rewrite_case_document,
    timeline_key,
)
from engine.storage import LocalStorage

T0 = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)


def a_file(upload_id: str, at: datetime = T0) -> CaseFile:
    return CaseFile(upload_id=upload_id, file_name=f"{upload_id}.csv", added_at=at, added_by="asha")


@pytest.fixture
def store(tmp_path: Path) -> CaseStore:
    return CaseStore(LocalStorage(tmp_path / "data"), lock_wait_seconds=0.5)


# ---------------------------------------------------------------------------
# Round trip (property)
# ---------------------------------------------------------------------------
_text = st.text(
    alphabet=st.characters(blacklist_categories=["Cs"], blacklist_characters="\x00"), max_size=30
).map(str.strip)
_ident = st.from_regex(r"[a-z0-9][a-z0-9_]{0,10}", fullmatch=True)
_times = st.datetimes(min_value=datetime(2020, 1, 1), max_value=datetime(2030, 1, 1), timezones=st.just(UTC))
_json_scalar = st.one_of(st.none(), st.booleans(), st.integers(-(10**6), 10**6), _text)
_payload: st.SearchStrategy[dict[str, Any]] = st.dictionaries(
    _ident, st.one_of(_json_scalar, st.lists(_json_scalar, max_size=3)), max_size=4
)


@st.composite
def cases(draw: st.DrawFn) -> Case:
    created = draw(_times)
    later = created + timedelta(seconds=draw(st.integers(0, 10**6)))
    uploads = draw(st.lists(_ident, max_size=4, unique=True))
    files = tuple(
        CaseFile(
            upload_id=f"u_{name}",
            file_name=draw(_text),
            sheet=draw(st.none() | _text),
            key_columns=tuple(draw(st.lists(_text, max_size=2))),
            added_at=created,
            added_by=draw(_ident),
        )
        for name in uploads
    )
    items = []
    for index, payload in enumerate(draw(st.lists(_payload, max_size=4))):
        items.append(
            CaseItem(
                item_id=f"it_{index}",
                kind=draw(st.sampled_from(["recipe_step", "setting", "role"])),
                title=draw(_text),
                source=draw(st.sampled_from(list(ProposalSource))),
                confidence=draw(st.sampled_from(list(AgentConfidence))),
                state=draw(st.sampled_from(list(ProposalState))),
                payload=payload,
                payload_digest=payload_digest(payload),
                proposed_at=created,
            )
        )
    questions = tuple(
        CaseQuestion(
            question_id=f"q_{index}",
            text=draw(_text),
            options=(CaseOption(option_id="a", label="Yes"), CaseOption(option_id="b", label="No")),
            answer=draw(st.sampled_from([None, "a", "b"])),
        )
        for index in range(draw(st.integers(0, 3)))
    )
    events = tuple(
        TimelineEvent(
            event_id="ev_" + f"{index:020x}",
            case_id="case_0123456789abcdef",
            client_id="default",
            version=1,
            at=later,
            actor=draw(_ident),
            kind=draw(st.sampled_from(list(TimelineKind))),
            details={"n": draw(st.integers(0, 100)), "flag": draw(st.booleans()), "code": draw(_ident)},
        )
        for index in range(draw(st.integers(0, 3)))
    )
    return Case(
        case_id="case_0123456789abcdef",
        client_id=draw(st.sampled_from(["default", "c_acme_1"])),
        created_by=draw(_ident),
        created_at=created,
        updated_at=later,
        state=draw(st.sampled_from(list(VisitState))),
        version=draw(st.integers(1, 10_000)),
        title=draw(_text),
        files=files,
        items=tuple(items),
        questions=questions,
        approvals=tuple(
            CaseApproval(approval_id=f"ap_{i}", approved_by="asha", approved_at=later, text_shown=draw(_text))
            for i in range(draw(st.integers(0, 2)))
        ),
        standing_answers_used=tuple(
            StandingAnswerUse(
                answer_id=f"sa_{i}",
                detector="UNKNOWN_BOOLEAN",
                value_signature=tuple(draw(st.lists(_text, max_size=3))),
                option_id="yes",
                applied_at=later,
            )
            for i in range(draw(st.integers(0, 2)))
        ),
        chat=tuple(
            CaseChatEntry(role=draw(st.sampled_from(["user", "agent"])), text=draw(_text), at=later)
            for _ in range(draw(st.integers(0, 2)))
        ),
        last_events=events,
    )


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(case=cases())
def test_a_case_survives_a_round_trip_through_its_stored_form(case: Case) -> None:
    stored = case.model_dump_json(indent=2, by_alias=True)
    again = Case.model_validate_json(stored)
    assert again == case
    assert again.model_dump_json(indent=2, by_alias=True) == stored, "the stored form is stable"


@settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(case=cases())
def test_a_case_survives_the_store(tmp_path: Path, case: Case) -> None:
    storage = LocalStorage(tmp_path / "rt")
    storage.write_bytes(case_key(case.client_id, case.case_id), case.model_dump_json().encode())
    assert CaseStore(storage).get(case.client_id, case.case_id) == case


def test_a_case_refuses_repeated_ids_and_two_active_goals() -> None:
    with pytest.raises(ValueError, match="file ids repeat"):
        Case(
            case_id="case_0123456789abcdef",
            client_id="default",
            created_by="asha",
            created_at=T0,
            updated_at=T0,
            version=1,
            files=(a_file("u_1"), a_file("u_1")),
        )
    with pytest.raises(ValueError, match="only an adapter"):
        Case(
            case_id="case_0123456789abcdef",
            client_id="default",
            created_by="asha",
            created_at=T0,
            updated_at=T0,
            version=1,
            read_only=True,
        )


def test_the_payload_digest_is_the_same_whatever_the_key_order() -> None:
    assert payload_digest({"a": 1, "b": [1, "x"]}) == payload_digest({"b": [1, "x"], "a": 1})
    assert payload_digest({"a": 1}) != payload_digest({"a": 2})


# ---------------------------------------------------------------------------
# The store: create, read, version check
# ---------------------------------------------------------------------------
def test_a_new_case_is_version_one_with_its_events(store: CaseStore) -> None:
    case = store.create(client_id="default", created_by="asha", title="Win back", files=[a_file("u_1")])
    assert case.version == 1 and case.state is VisitState.INTAKE and case.source is CaseSource.CASE
    assert store.get("default", case.case_id) == case
    kinds = [event.kind for event in store.timeline("default", case.case_id)]
    assert kinds == [TimelineKind.CASE_CREATED, TimelineKind.FILE_ADDED]
    assert all(
        event.actor == "asha" and event.version == 1 for event in store.timeline("default", case.case_id)
    )
    assert store.storage.exists(case_key("default", case.case_id))
    assert not store.storage.exists(case_dir("default", case.case_id) + LOCK_FILENAME), "the lock is released"


def test_adding_a_file_bumps_the_version_and_logs_who_did_it(store: CaseStore) -> None:
    case = store.create(client_id="default", created_by="asha")
    after = store.add_file("default", case.case_id, expected_version=1, actor="ravi", file=a_file("u_2"))
    assert after.version == 2 and after.upload_ids() == ("u_2",)
    last = store.timeline("default", case.case_id)[-1]
    assert (last.kind, last.actor, last.upload_id, last.version) == (
        TimelineKind.FILE_ADDED,
        "ravi",
        "u_2",
        2,
    )


def test_a_stale_writer_is_refused_and_changes_nothing(store: CaseStore) -> None:
    case = store.create(client_id="default", created_by="asha")
    store.add_file("default", case.case_id, expected_version=1, actor="asha", file=a_file("u_1"))
    with pytest.raises(CaseError) as refused:
        store.add_file("default", case.case_id, expected_version=1, actor="ravi", file=a_file("u_2"))
    assert refused.value.code == "CASE_VERSION_CONFLICT"
    kept = store.get("default", case.case_id)
    assert kept.version == 2 and kept.upload_ids() == ("u_1",)
    assert len(store.timeline("default", case.case_id)) == 2


def test_two_concurrent_writers_one_wins_the_other_gets_a_conflict(store: CaseStore) -> None:
    case = store.create(client_id="default", created_by="asha")
    barrier = threading.Barrier(2)
    outcomes: dict[str, str] = {}

    def writer(name: str) -> None:
        barrier.wait()
        try:
            store.add_file("default", case.case_id, expected_version=1, actor=name, file=a_file(f"u_{name}"))
            outcomes[name] = "ok"
        except CaseError as exc:
            outcomes[name] = exc.code

    threads = [threading.Thread(target=writer, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes.values()) == ["CASE_VERSION_CONFLICT", "ok"]
    final = store.get("default", case.case_id)
    assert final.version == 2 and len(final.files) == 1


def _process_writer(root: str, case_id: str, name: str, start: Any, results: Any) -> None:
    store = CaseStore(LocalStorage(Path(root)), lock_wait_seconds=10)
    start.wait()
    try:
        store.add_file("default", case_id, expected_version=1, actor=name, file=a_file(f"u_{name}"))
        results.put((name, "ok"))
    except CaseError as exc:
        results.put((name, exc.code))


def test_two_processes_share_no_lock_but_the_stores_and_still_one_wins(tmp_path: Path) -> None:
    root = tmp_path / "data"
    case = CaseStore(LocalStorage(root)).create(client_id="default", created_by="asha")
    context = multiprocessing.get_context("spawn")
    start = context.Barrier(2)
    results = context.Queue()
    workers = [
        context.Process(target=_process_writer, args=(str(root), case.case_id, name, start, results))
        for name in ("p1", "p2")
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(60)
    outcomes = sorted(results.get(timeout=5)[1] for _ in workers)
    assert outcomes == ["CASE_VERSION_CONFLICT", "ok"]
    assert CaseStore(LocalStorage(root)).get("default", case.case_id).version == 2


def test_a_lock_left_by_a_crashed_writer_is_broken_once_stale(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path / "data")
    case = CaseStore(storage).create(client_id="default", created_by="asha")
    lock = case_dir("default", case.case_id) + LOCK_FILENAME
    storage.write_bytes(lock, json.dumps({"holder": "dead", "at": T0.isoformat()}).encode())
    fresh = CaseStore(storage, lock_wait_seconds=0.2, clock=lambda: T0 + timedelta(seconds=5))
    with pytest.raises(CaseError) as busy:
        fresh.add_file("default", case.case_id, expected_version=1, actor="asha", file=a_file("u_1"))
    assert busy.value.code == "CASE_VERSION_CONFLICT", "a live lock is waited on, then refused"
    late = CaseStore(storage, lock_wait_seconds=0.2, clock=lambda: T0 + timedelta(minutes=5))
    assert (
        late.add_file("default", case.case_id, expected_version=1, actor="asha", file=a_file("u_1")).version
        == 2
    )


def test_a_crash_between_the_case_and_the_timeline_loses_no_event(store: CaseStore) -> None:
    case = store.create(client_id="default", created_by="asha")
    after = store.add_file("default", case.case_id, expected_version=1, actor="asha", file=a_file("u_1"))
    key = timeline_key("default", case.case_id)
    lines = store.storage.read_text(key).splitlines()
    store.storage.write_text(key, "\n".join(lines[:-1]) + "\n")  # as if the process died before appending
    events = store.timeline("default", case.case_id)
    assert [event.event_id for event in events][-1] == after.last_events[0].event_id
    store.add_file("default", case.case_id, expected_version=2, actor="asha", file=a_file("u_2"))
    on_disk = [json.loads(line)["kind"] for line in store.storage.read_text(key).splitlines()]
    assert on_disk == ["case.created", "file.added", "file.added"], "the next write replays the lost event"


def test_a_crash_then_a_privacy_rewrite_still_loses_no_event(store: CaseStore) -> None:
    """Review M231: a retention or erasure rewrite replays the crash-pending events before replacing them."""
    case = store.create(client_id="default", created_by="asha")
    store.add_file("default", case.case_id, expected_version=1, actor="asha", file=a_file("u_1"))
    key = timeline_key("default", case.case_id)
    lines = store.storage.read_text(key).splitlines()
    store.storage.write_text(key, "\n".join(lines[:-1]) + "\n")  # the process died before appending
    rewrite_case_document(
        store.storage,
        case_key("default", case.case_id),
        lambda document: forget_upload(document, "u_1"),
        actor="system:retention",
        kind=TimelineKind.FILE_FORGOTTEN,
        upload_id="u_1",
    )
    on_disk = [json.loads(line)["kind"] for line in store.storage.read_text(key).splitlines()]
    assert on_disk == ["case.created", "file.added", "file.forgotten"]
    assert [event.kind for event in store.timeline("default", case.case_id)] == [
        TimelineKind.CASE_CREATED,
        TimelineKind.FILE_ADDED,
        TimelineKind.FILE_FORGOTTEN,
    ]


def test_a_privacy_rewrite_that_would_leave_an_invalid_case_writes_nothing(store: CaseStore) -> None:
    """Review M231: `rewrite_case_document` never writes a `case.json` that is no longer a `Case`."""
    case = store.create(client_id="default", created_by="asha", files=[a_file("u_1")])
    key = case_key("default", case.case_id)
    before = store.storage.read_bytes(key)

    def breaks(document: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
        return {**document, "case_id": "[erased]"}, {"cells": 1}

    with pytest.raises(CaseError) as refused:
        rewrite_case_document(store.storage, key, breaks, actor="system:erasure", kind=TimelineKind.ERASURE)
    assert refused.value.code == "CASE_UNREADABLE"
    assert store.storage.read_bytes(key) == before


def test_timeline_events_are_appended_not_the_file_rewritten(store: CaseStore) -> None:
    """Review M231: each commit appends to `timeline.jsonl` in place, so a case's lifetime stays linear."""
    case = store.create(client_id="default", created_by="asha")
    path = store.storage.local_path(timeline_key("default", case.case_id))
    inode = path.stat().st_ino
    version = case.version
    for n in range(5):
        version = store.add_file(
            "default", case.case_id, expected_version=version, actor="asha", file=a_file(f"u_{n}")
        ).version
    assert path.stat().st_ino == inode, "appended in place, not rewritten through a temp file"
    assert len(store.timeline("default", case.case_id)) == 6


def test_a_torn_last_timeline_line_does_not_swallow_the_next_event(store: CaseStore) -> None:
    case = store.create(client_id="default", created_by="asha")
    path = store.storage.local_path(timeline_key("default", case.case_id))
    with path.open("ab") as handle:
        handle.write(b'{"event_id": "ev_')  # a crash mid-append
    after = store.add_file("default", case.case_id, expected_version=1, actor="asha", file=a_file("u_1"))
    kinds = [event.kind for event in store.timeline("default", case.case_id)]
    assert kinds == [TimelineKind.CASE_CREATED, TimelineKind.FILE_ADDED]
    assert after.last_events[0].event_id in path.read_text(encoding="utf-8").splitlines()[-1]


def test_an_unreadable_stored_case_answers_a_code_not_a_crash(store: CaseStore) -> None:
    """Review M231: a `case.json` that fails validation is `CASE_UNREADABLE`, never an unhandled error."""
    case = store.create(client_id="default", created_by="asha")
    key = case_key("default", case.case_id)
    document = json.loads(store.storage.read_bytes(key))
    document["questions"] = [{"question_id": "", "text": "?"}]
    store.storage.write_bytes(key, json.dumps(document).encode("utf-8"))
    with pytest.raises(CaseError) as unreadable:
        store.get("default", case.case_id)
    assert unreadable.value.code == "CASE_UNREADABLE"
    with pytest.raises(CaseError):
        store.timeline("default", case.case_id)
    assert store.list("default") == ()


def test_adding_the_same_file_twice_keeps_one_entry(store: CaseStore) -> None:
    case = store.create(client_id="default", created_by="asha", files=[a_file("u_1")])
    again = store.add_file("default", case.case_id, expected_version=1, actor="asha", file=a_file("u_1"))
    assert again.upload_ids() == ("u_1",) and again.version == 2


# ---------------------------------------------------------------------------
# Per-client scope
# ---------------------------------------------------------------------------
def test_another_clients_case_is_not_found_and_is_never_named(store: CaseStore) -> None:
    case = store.create(client_id="c_acme_1", created_by="asha")
    for client in ("c_other_2", "default"):
        with pytest.raises(CaseError) as missing:
            store.get(client, case.case_id)
        assert missing.value.code == "CASE_NOT_FOUND" and case.case_id not in missing.value.message
        with pytest.raises(CaseError) as also:
            store.add_file(client, case.case_id, expected_version=1, actor="x", file=a_file("u_1"))
        assert also.value.code == "CASE_NOT_FOUND"
    assert store.list("c_other_2") == () and [c.case_id for c in store.list("c_acme_1")] == [case.case_id]


@pytest.mark.parametrize("client", ["../default", "Default", "", "a/b"])
def test_a_client_id_that_is_not_a_path_segment_finds_nothing(store: CaseStore, client: str) -> None:
    case = store.create(client_id="default", created_by="asha")
    with pytest.raises(CaseError) as missing:
        store.get(client, case.case_id)
    assert missing.value.code == "CASE_NOT_FOUND"


def test_a_case_document_moved_under_another_client_is_not_served(store: CaseStore) -> None:
    case = store.create(client_id="c_acme_1", created_by="asha")
    store.storage.write_bytes(
        case_key("c_other_2", case.case_id), store.storage.read_bytes(case_key("c_acme_1", case.case_id))
    )
    with pytest.raises(CaseError) as missing:
        store.get("c_other_2", case.case_id)
    assert missing.value.code == "CASE_NOT_FOUND"


# ---------------------------------------------------------------------------
# Adapters: read-only
# ---------------------------------------------------------------------------
def _session() -> AgentSession:
    return AgentSession.model_validate(
        {
            "session_id": "s1",
            "upload_id": "u_000000000001",
            "use_case_id": "telco-churn",
            "mode": "train",
            "agent_name": "Helper",
            "status": SessionStatus.NEEDS_REVIEW.value,
            "tool_results": [
                {
                    "evidence_id": "ev1",
                    "tool": "profile",
                    "result": {"rows": 10},
                    "created_at": T0.isoformat(),
                }
            ],
            "proposals": [
                {
                    "proposal_id": "p1",
                    "kind": "recipe_step",
                    "title": "Read 'plan' as yes or no",
                    "reason": "Two values",
                    "step": {"order": 1, "kind": "map_boolean", "column": "plan", "params": {}},
                    "evidence_ids": ["ev1"],
                    "confidence": "check",
                }
            ],
            "questions": [
                {
                    "question_id": "q1",
                    "text": "Which column is the outcome?",
                    "options": [{"option_id": "a", "label": "churn"}, {"option_id": "b", "label": "none"}],
                }
            ],
            "created_at": T0.isoformat(),
            "updated_at": (T0 + timedelta(minutes=3)).isoformat(),
        }
    )


def test_a_guided_session_opens_as_a_read_only_case() -> None:
    case = case_from_session(_session(), file_name="bills.csv")
    assert case.read_only and case.source is CaseSource.GUIDED_SESSION
    assert case.case_id == "guided_u_000000000001" and case.upload_ids() == ("u_000000000001",)
    assert case.state is VisitState.TREAT and case.goals[0].goal_id == "telco-churn"
    assert [item.kind for item in case.items] == ["map_boolean"]
    assert case.items[0].payload_digest == payload_digest(case.items[0].payload)
    assert [question.question_id for question in case.questions] == ["q1"]
    assert [ref.evidence_id for ref in case.evidence] == ["ev1"]
    assert Case.model_validate_json(case.model_dump_json()) == case


def test_a_guided_session_reply_that_the_ai_did_not_write_says_why_in_the_case_chat() -> None:
    """The read-only case shows the Guided-setup chat as the live Doctor chat does: with why the AI was silent."""
    document = _session().model_dump(mode="json")
    document["transcript"] = [
        {"role": "user", "text": "Which column matters?", "created_at": T0.isoformat()},
        {
            "role": "agent",
            "text": "The AI service is not answering right now.",
            "turn": {
                "llm_calls": 0,
                "error_codes": ["LLM_UNAVAILABLE", "LLM_KEY_EXPIRED"],
                "blocked_by": "unavailable",
            },
            "created_at": T0.isoformat(),
        },
        {"role": "agent", "text": "Done.", "turn": {"llm_calls": 1}, "created_at": T0.isoformat()},
        {  # the model named a tool that does not exist, was refused, and then answered
            "role": "agent",
            "text": "Here is what I found.",
            "turn": {"llm_calls": 2, "error_codes": ["AGENT_TOOL_UNKNOWN"]},
            "created_at": T0.isoformat(),
        },
        {  # a refusal first, then the AI service failed: only the failure is the cause
            "role": "agent",
            "text": "The AI service is not answering right now.",
            "turn": {
                "llm_calls": 1,
                "error_codes": ["AGENT_TOOL_UNKNOWN", "LLM_REFUSED", "LLM_OUT_OF_ROOM"],
                "blocked_by": "unavailable",
            },
            "created_at": T0.isoformat(),
        },
    ]
    session = AgentSession.model_validate(document)
    chat = case_from_session(session).chat
    assert [(entry.blocked_by, entry.error_codes) for entry in chat] == [
        (None, ()),  # a person's message has no turn
        ("unavailable", ("LLM_UNAVAILABLE", "LLM_KEY_EXPIRED")),
        (None, ()),  # a turn the AI answered
        (None, ()),  # an answered turn that met a refusal: not "the AI did not answer"
        ("unavailable", ("LLM_REFUSED", "LLM_OUT_OF_ROOM")),
    ]
    assert Case.model_validate_json(case_from_session(session).model_dump_json()).chat == chat


def test_a_diagnosis_opens_as_the_same_read_only_case_for_the_same_files() -> None:
    diagnosis = {
        "files": [
            {"upload_id": "u_a", "file_name": "customers.csv", "kind": "entity", "id_column": "customer_id"},
            {"upload_id": "u_b", "file_name": "bills.csv", "kind": "bills", "id_column": None},
        ],
        "use_cases": [],
    }
    first = case_from_diagnosis(diagnosis, created_at=T0)
    assert first.read_only and first.source is CaseSource.DOCTOR_DIAGNOSIS
    assert first.upload_ids() == ("u_a", "u_b") and first.files[0].key_columns == ("customer_id",)
    assert first.diagnosis == diagnosis
    assert case_from_diagnosis(diagnosis, created_at=T0 + timedelta(days=1)).case_id == first.case_id
    reordered = case_from_diagnosis({**diagnosis, "files": diagnosis["files"][::-1]}, created_at=T0)
    assert reordered.case_id == first.case_id, "the same files in another order are the same case"
    assert reordered.upload_ids() == ("u_b", "u_a"), "the files keep the diagnosis's order"


def test_a_guided_session_with_a_long_file_name_still_opens() -> None:
    """Review M231: `Case.title` holds 200 characters; an upload's name has no limit."""
    name = "x" * 230 + ".csv"
    case = case_from_session(_session(), file_name=name)
    assert case.title == name[:200] and case.files[0].file_name == name


def test_an_adapters_case_cannot_be_changed_through_the_store(store: CaseStore) -> None:
    case = case_from_session(_session())
    store.storage.write_bytes(case_key("default", case.case_id), case.model_dump_json().encode())
    with pytest.raises(CaseError) as refused:
        store.add_file("default", case.case_id, expected_version=1, actor="asha", file=a_file("u_9"))
    assert refused.value.code == "CASE_READ_ONLY"


# ---------------------------------------------------------------------------
# Privacy helpers on the raw document
# ---------------------------------------------------------------------------
def _document_with_corrections() -> dict[str, Any]:
    cells = [
        {"key": "C-104", "old": "x", "new": "y"},
        {"key": 104, "old": "1", "new": "2"},
        {"key": "C-7", "old": "a", "new": "b"},
    ]
    payload = {
        "kind": "recipe_step",
        "step": {"kind": "correct_cells", "column": "plan", "params": {"key_column": "id", "cells": cells}},
    }
    only = {
        "kind": "recipe_step",
        "step": {
            "kind": "correct_cells",
            "column": "plan",
            "params": {"key_column": "id", "cells": [{"key": "104", "old": "1", "new": "2"}]},
        },
    }
    return {
        "items": [
            {
                "item_id": "fix",
                "upload_id": "u_1",
                "payload": payload,
                "payload_digest": payload_digest(payload),
            },
            {"item_id": "only", "upload_id": "u_1", "payload": only, "payload_digest": payload_digest(only)},
            {"item_id": "other", "upload_id": "u_2", "payload": {"kind": "setting"}, "payload_digest": "x"},
        ],
        "standing_answers_used": [
            {"answer_id": "s1", "upload_id": "u_1", "value_signature": ["104"]},
            {"answer_id": "s2", "upload_id": "u_2", "value_signature": ["haan", "nahi"]},
        ],
    }


def test_a_numeric_id_takes_its_corrections_and_standing_answers_with_it() -> None:
    cleaned, counts = remove_principal(_document_with_corrections(), "104")
    assert counts == {"corrections": 2, "items": 1, "standing_answers": 1}
    assert [item["item_id"] for item in cleaned["items"]] == ["fix", "other"]
    kept = cleaned["items"][0]
    assert [cell["key"] for cell in kept["payload"]["step"]["params"]["cells"]] == ["C-104", "C-7"]
    assert kept["payload_digest"] == payload_digest(kept["payload"]), "the digest follows the payload"
    assert [answer["answer_id"] for answer in cleaned["standing_answers_used"]] == ["s2"]


def test_forgetting_an_upload_keeps_the_rest_of_the_case() -> None:
    document = _document_with_corrections()
    document.update(
        files=[{"upload_id": "u_1"}, {"upload_id": "u_2"}],
        evidence=[{"evidence_id": "e1", "upload_id": "u_1"}, {"evidence_id": "e2", "upload_id": "u_2"}],
        prescriptions=[{"prescription_hash": "h", "upload_ids": ["u_1", "u_2"]}],
        approvals=[{"approval_id": "a1", "item_ids": ["fix"]}],
        chat=[{"role": "user", "text": "hi"}],
    )
    out, counts = forget_upload(document, "u_1")
    assert [file["upload_id"] for file in out["files"]] == ["u_2"]
    assert [item["item_id"] for item in out["items"]] == ["other"]
    assert [ref["evidence_id"] for ref in out["evidence"]] == ["e2"]
    assert out["prescriptions"][0]["orphaned"] is True
    assert out["approvals"] == document["approvals"], "who approved what stays in the processing log"
    assert out["chat"], "the chat stays while a file remains"
    assert counts["files"] == 1 and counts["items"] == 2 and counts["prescriptions_orphaned"] == 1
    last, _ = forget_upload(out, "u_2")
    assert last["files"] == [] and last["chat"] == []


def test_a_chat_entry_written_before_the_agent_changed_the_plan_reads_and_dumps_as_it_did() -> None:
    old = {"role": "agent", "text": "Hello.", "at": "2026-10-12T09:00:00Z", "by": None, "sent": []}
    entry = CaseChatEntry.model_validate(old)
    assert (entry.item_ids, entry.withdrawn, entry.tools, entry.suggestion) == ((), (), (), None)
    assert json.loads(entry.model_dump_json()) == old, "the new fields are left out of a dump while unset"


def test_a_chat_entry_says_why_the_ai_did_not_answer_and_only_then() -> None:
    entry = CaseChatEntry(
        role="agent",
        text="The AI service is not answering right now.",
        at=T0,
        blocked_by="unavailable",
        error_codes=("LLM_UNAVAILABLE", "LLM_KEY_EXPIRED"),
    )
    dumped = json.loads(entry.model_dump_json())
    assert (dumped["blocked_by"], dumped["error_codes"]) == (
        "unavailable",
        ["LLM_UNAVAILABLE", "LLM_KEY_EXPIRED"],
    )
    assert CaseChatEntry.model_validate_json(entry.model_dump_json()) == entry
    answered = json.loads(CaseChatEntry(role="agent", text="Done.", at=T0).model_dump_json())
    assert "blocked_by" not in answered and "error_codes" not in answered


def test_a_chat_entry_carries_what_the_agent_did_in_its_reply() -> None:
    entry = CaseChatEntry(
        role="agent",
        text="Done.",
        at=T0,
        item_ids=("recipe_step.base.aaa",),
        withdrawn=("recipe_step.base.bbb",),
        tools=("find_values", "propose_step"),
        suggestion=ChatSuggestion(question_id="q.q24.base.x", option_id="c0", label="An order over 500"),
    )
    again = CaseChatEntry.model_validate_json(entry.model_dump_json())
    assert again == entry and again.suggestion is not None and again.suggestion.option_id == "c0"
