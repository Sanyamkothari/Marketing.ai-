"""Two clients on one deployment (G20): what one client's cases, remembered answers and chat hold is never visible to
the other, and a case cannot be made without saying whose it is."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from engine.doctor.case import (
    Case,
    CaseChatEntry,
    CaseError,
    CaseOption,
    CaseQuestion,
    CaseStore,
)
from engine.doctor.standing import StandingAnswerStore, standing_key
from engine.storage import LocalStorage

AT = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)
SECRET = "Sunrise Bank churn list for Mr Rao"


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path)


@pytest.fixture
def store(storage: LocalStorage) -> CaseStore:
    return CaseStore(storage, clock=lambda: AT)


def _question() -> CaseQuestion:
    return CaseQuestion(
        question_id="q.unknown_boolean.base.abc",
        code="UNKNOWN_BOOLEAN",
        column="dnd",
        text="What does haan mean?",
        options=(CaseOption(option_id="c0", label="Yes"), CaseOption(option_id="c1", label="No")),
        value_signature=("haan", "nahi"),
    )


def _chat(store: CaseStore, case: Case) -> Case:
    entry = CaseChatEntry(role="user", text=SECRET, at=AT, by="u-asha")
    return store.update(
        case.client_id,
        case.case_id,
        expected_version=case.version,
        actor="u-asha",
        change=lambda current: (current.model_copy(update={"chat": (*current.chat, entry)}), []),
    )


@pytest.mark.parametrize("client", ["", " ", "Has Space", "../other", "UPPER", "a" * 65])
def test_a_case_cannot_be_made_without_a_valid_client(
    store: CaseStore, storage: LocalStorage, client: str
) -> None:
    with pytest.raises(CaseError) as raised:
        store.create(client_id=client, created_by="u-asha")
    assert raised.value.code == "CASE_CLIENT_REQUIRED"
    assert not storage.list_keys("cases/")  # nothing was written


def test_one_clients_cases_are_not_listed_read_or_named_to_the_other(store: CaseStore) -> None:
    mine = store.create(client_id="sunrise-bank", created_by="u-asha", title=SECRET)
    theirs = store.create(client_id="blue-telco", created_by="u-ravi")

    assert [c.case_id for c in store.list("sunrise-bank")] == [mine.case_id]
    assert [c.case_id for c in store.list("blue-telco")] == [theirs.case_id]
    for client, case in (("blue-telco", mine), ("sunrise-bank", theirs)):
        with pytest.raises(CaseError) as raised:
            store.get(client, case.case_id)
        assert raised.value.code == "CASE_NOT_FOUND" and case.case_id not in raised.value.message
        with pytest.raises(CaseError):
            store.timeline(client, case.case_id)


def test_the_chat_the_ai_saw_stays_in_its_clients_folder(store: CaseStore, storage: LocalStorage) -> None:
    mine = _chat(store, store.create(client_id="sunrise-bank", created_by="u-asha"))
    store.create(client_id="blue-telco", created_by="u-ravi")

    holders = [key for key in storage.list_keys("") if SECRET.encode() in storage.read_bytes(key)]
    assert holders and all(key.startswith(f"cases/sunrise-bank/{mine.case_id}/") for key in holders)
    other = store.list("blue-telco")[0]
    assert other.chat == () and SECRET not in other.model_dump_json()


def test_a_remembered_answer_is_one_clients_and_is_never_found_or_revoked_by_the_other(
    storage: LocalStorage,
) -> None:
    answers = StandingAnswerStore(storage)
    remembered = answers.remember(
        "sunrise-bank",
        _question(),
        option_id="c0",
        effect_digest=None,
        meaning="haan means yes",
        title="haan means yes",
        actor="u-asha",
        at=AT,
    )

    assert answers.find("sunrise-bank", _question()) == remembered
    assert answers.find("blue-telco", _question()) is None
    assert answers.list("blue-telco") == ()
    assert answers.get("blue-telco", remembered.answer_id) is None
    with pytest.raises(Exception, match="no such remembered answer"):
        answers.revoke("blue-telco", remembered.answer_id, actor="u-ravi", at=AT)
    assert answers.find("sunrise-bank", _question()) is not None  # still standing for its own client

    assert standing_key("sunrise-bank").startswith("cases/sunrise-bank/")
    assert not any(key.startswith("cases/blue-telco/") for key in storage.list_keys(""))


def test_a_client_id_that_climbs_out_of_its_folder_finds_nothing(storage: LocalStorage) -> None:
    answers = StandingAnswerStore(storage)
    answers.remember(
        "sunrise-bank",
        _question(),
        option_id="c0",
        effect_digest=None,
        meaning="m",
        title="t",
        actor="u",
        at=AT,
    )
    assert answers.list("../sunrise-bank") == ()
    assert answers.list("sunrise-bank/../sunrise-bank") == ()
