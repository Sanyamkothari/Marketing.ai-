"""Data Doctor's cases under retention and erasure (Plan O M231, DEC-1801).

Acceptance (Plan O §M231): erasing an upload removes its file entry and evidence; erasing a person removes
`correct_cells` corrections and standing answers holding their id; the retention job keeps the timeline for
the configured days and deletes data with its upload.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from engine.access.roles import LOCAL_OPERATOR
from engine.agent.contracts import AgentConfidence, ProposalSource
from engine.doctor.case import (
    Case,
    CaseFile,
    CaseItem,
    CaseStore,
    StandingAnswerUse,
    TimelineKind,
    case_dir,
    case_key,
    evidence_key,
    payload_digest,
    timeline_key,
)
from engine.platform_db import sqlite_engine
from engine.privacy.config import load_privacy_config
from engine.privacy.contracts import RetentionAction, RetentionCategory
from engine.privacy.erasure import erase, find_principal
from engine.privacy.layout import Store, StoreIndex, store_of
from engine.privacy.retention import RETENTION_JOB, apply_retention, plan_retention
from engine.storage import LocalStorage
from tests.unit.production.privacy_support import customers, write_upload

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
SALT = "test-salt-for-cases-0123456789abcdef"
PERSON = "C-104"
NUMERIC_PERSON = "104"


def _keys(item: CaseItem) -> list[Any]:
    """The `key` of every correction in a `correct_cells` item."""
    payload: Any = json.loads(json.dumps(item.payload))
    return [cell["key"] for cell in payload["step"]["params"]["cells"]]


def _correction(item_id: str, upload_id: str, keys: list[object], at: datetime) -> CaseItem:
    payload: dict[str, Any] = {
        "kind": "recipe_step",
        "step": {
            "kind": "correct_cells",
            "column": "plan",
            "params": {
                "key_column": "customer_id",
                "cells": [{"key": k, "old": "pp", "new": "Prepaid"} for k in keys],
            },
        },
    }
    return CaseItem(
        item_id=item_id,
        kind="correct_cells",
        source=ProposalSource.PERSON,
        confidence=AgentConfidence.SURE,
        upload_id=upload_id,
        payload=payload,
        payload_digest=payload_digest(payload),
        proposed_at=at,
    )


def build_case(storage: LocalStorage, *, at: datetime, uploads: tuple[str, ...] = ("u_old", "u_new")) -> str:
    """A case on two uploads: corrections naming two customers, a standing answer, evidence per upload."""
    store = CaseStore(storage, clock=lambda: at)
    files = [CaseFile(upload_id=u, file_name=f"{u}.csv", added_at=at, added_by="asha") for u in uploads]
    case = store.create(client_id="default", created_by="asha", files=files)
    version = case.version
    for upload_id in uploads:
        version = store.put_evidence(
            "default",
            case.case_id,
            expected_version=version,
            actor="asha",
            evidence_id=f"ev_{upload_id}",
            tool="profile",
            upload_id=upload_id,
            result={"rows": 3},
        ).version

    def add_items(current: Case) -> tuple[Case, tuple[()]]:
        items = (
            _correction("fix_named", uploads[0], [PERSON, "C-7"], at),
            _correction("fix_numeric", uploads[-1], [104], at),
        )
        answers = (
            StandingAnswerUse(
                answer_id="sa_1",
                detector="UNKNOWN_BOOLEAN",
                upload_id=uploads[-1],
                column="plan",
                value_signature=("haan", "nahi"),
                option_id="yes_no",
                applied_at=at,
            ),
            StandingAnswerUse(
                answer_id="sa_2",
                detector="KEY_FORMAT_CHANGED",
                upload_id=uploads[-1],
                column="customer_id",
                value_signature=(NUMERIC_PERSON,),
                option_id="strip",
                applied_at=at,
            ),
        )
        return current.model_copy(update={"items": items, "standing_answers_used": answers}), ()

    store.update("default", case.case_id, expected_version=version, actor="asha", change=add_items)
    return case.case_id


def test_the_cases_store_is_known_to_the_layout(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path / "data")
    case_id = build_case(storage, at=NOW)
    assert store_of(case_key("default", case_id)) is Store.CASES
    assert store_of(timeline_key("default", case_id)) is Store.CASES
    info = StoreIndex(storage).cases[case_key("default", case_id)]
    assert info.upload_ids == ("u_old", "u_new") and info.client_id == "default"
    assert dict(info.evidence_uploads) == {"ev_u_old": "u_old", "ev_u_new": "u_new"}


def test_the_processing_log_setting_defaults_to_a_year(config_root: Path) -> None:
    assert load_privacy_config(config_root).processing_log_days == 365


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------
def _store_with_expiring_upload(root: Path, *, case_at: datetime) -> tuple[LocalStorage, str]:
    storage = LocalStorage(root)
    write_upload(storage, "u_old", customers(["C-1", "C-2"]), created_at=NOW - timedelta(days=400))
    write_upload(storage, "u_new", customers(["C-1", "C-2"]), created_at=NOW - timedelta(days=5))
    return storage, build_case(storage, at=case_at)


def test_an_expired_upload_takes_its_file_entry_and_evidence_and_the_timeline_stays(
    tmp_path: Path, config_root: Path
) -> None:
    storage, case_id = _store_with_expiring_upload(tmp_path / "data", case_at=NOW - timedelta(days=30))
    plan = plan_retention(storage, config_root, NOW)
    case_items = [item for item in plan.items if item.owner_id == case_id]
    assert {(item.category, item.action, item.key) for item in case_items} == {
        (RetentionCategory.CASE_DATA, RetentionAction.DELETE, evidence_key("default", case_id, "ev_u_old")),
        (RetentionCategory.CASE_DATA, RetentionAction.FORGET_UPLOAD, case_key("default", case_id)),
    }
    assert [item.upload_id for item in case_items if item.action is RetentionAction.FORGET_UPLOAD] == [
        "u_old"
    ]
    timeline_before = CaseStore(storage).timeline("default", case_id)

    apply_retention(plan, storage, None, RETENTION_JOB, now=NOW)

    case = CaseStore(storage).get("default", case_id)
    assert case.upload_ids() == ("u_new",)
    assert [item.item_id for item in case.items] == ["fix_numeric"], "the gone upload's items go with it"
    assert [ref.evidence_id for ref in case.evidence] == ["ev_u_new"]
    assert not storage.exists(evidence_key("default", case_id, "ev_u_old"))
    assert storage.exists(evidence_key("default", case_id, "ev_u_new"))
    timeline = CaseStore(storage).timeline("default", case_id)
    assert timeline[: len(timeline_before)] == timeline_before, "the processing log is kept, append-only"
    forgotten = timeline[-1]
    assert (forgotten.kind, forgotten.upload_id, forgotten.actor) == (
        TimelineKind.FILE_FORGOTTEN,
        "u_old",
        RETENTION_JOB.user_id,
    )
    assert plan_retention(storage, config_root, NOW).items == tuple(
        item for item in plan_retention(storage, config_root, NOW).items if item.owner_id != case_id
    ), "a second pass has nothing left to do for the case"


def test_the_whole_case_goes_once_the_processing_log_outlives_its_days(
    tmp_path: Path, config_root: Path
) -> None:
    storage, case_id = _store_with_expiring_upload(tmp_path / "data", case_at=NOW - timedelta(days=30))
    keep = plan_retention(storage, config_root, NOW + timedelta(days=334))  # 364 days after the last change
    assert not [i for i in keep.items if i.category is RetentionCategory.CASE_LOG]
    due = plan_retention(storage, config_root, NOW + timedelta(days=336))
    log_keys = {item.key for item in due.items if item.category is RetentionCategory.CASE_LOG}
    assert log_keys == set(storage.list_keys(case_dir("default", case_id)))
    assert {item.retention_days for item in due.items if item.category is RetentionCategory.CASE_LOG} == {365}
    apply_retention(due, storage, None, RETENTION_JOB, now=NOW + timedelta(days=336))
    assert storage.list_keys(case_dir("default", case_id)) == ()


def test_the_processing_log_days_are_configurable(tmp_path: Path, config_root: Path) -> None:
    root = tmp_path / "configs"
    shutil.copytree(config_root, root)
    path = root / "privacy.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["processing_log_days"] = 400
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    storage, case_id = _store_with_expiring_upload(tmp_path / "data", case_at=NOW - timedelta(days=30))
    later = NOW + timedelta(days=336)
    assert not [
        i for i in plan_retention(storage, root, later).items if i.category is RetentionCategory.CASE_LOG
    ]
    assert [
        i for i in plan_retention(storage, root, NOW + timedelta(days=371)).items if i.owner_id == case_id
    ]


def test_a_dry_run_lists_exactly_what_the_real_run_does_to_a_case(tmp_path: Path, config_root: Path) -> None:
    storage, _ = _store_with_expiring_upload(tmp_path / "data", case_at=NOW - timedelta(days=30))
    plan = plan_retention(storage, config_root, NOW)
    result = apply_retention(plan, storage, None, RETENTION_JOB, now=NOW)
    assert set(result.deleted) == set(plan.planned_keys(RetentionAction.DELETE))
    assert set(result.stripped) == set(plan.planned_keys(RetentionAction.STRIP_SAMPLES)) | set(
        plan.planned_keys(RetentionAction.FORGET_UPLOAD)
    )


def test_a_plan_with_no_case_is_unchanged_in_its_stored_form(tmp_path: Path, config_root: Path) -> None:
    """No case, no new field: an upload's retention item serialises exactly as before M231."""
    storage = LocalStorage(tmp_path / "data")
    write_upload(storage, "u_old", customers(["C-1"]), created_at=NOW - timedelta(days=400))
    plan = plan_retention(storage, config_root, NOW)
    assert plan.items and all("upload_id" not in json.loads(item.model_dump_json()) for item in plan.items)


# ---------------------------------------------------------------------------
# Erasure
# ---------------------------------------------------------------------------
@pytest.fixture
def erasure_store(tmp_path: Path) -> tuple[LocalStorage, str, Path]:
    storage = LocalStorage(tmp_path / "data")
    case_id = build_case(storage, at=NOW - timedelta(days=2))
    return storage, case_id, tmp_path / "platform.db"


def test_erasing_a_person_removes_their_corrections_and_standing_answers(
    erasure_store: tuple[LocalStorage, str, Path], config_root: Path
) -> None:
    storage, case_id, platform_db = erasure_store
    before = CaseStore(storage).get("default", case_id)
    findings = find_principal(storage, PERSON)
    assert case_key("default", case_id) in {location.key for location in findings.locations}

    outcome = erase(
        storage,
        PERSON,
        engine=sqlite_engine(platform_db),
        principal=LOCAL_OPERATOR,
        salt=SALT,
        config_root=config_root,
    )
    assert outcome.status == "completed"
    case = CaseStore(storage).get("default", case_id)  # still a valid case
    assert case.version == before.version + 1
    named = next(item for item in case.items if item.item_id == "fix_named")
    assert _keys(named) == ["C-7"], "only that person's correction went"
    assert named.payload_digest == payload_digest(named.payload)
    assert PERSON.encode() not in storage.read_bytes(case_key("default", case_id))
    assert CaseStore(storage).timeline("default", case_id)[-1].kind is TimelineKind.ERASURE


def test_a_numeric_id_is_erased_from_corrections_where_it_is_the_rows_key(
    erasure_store: tuple[LocalStorage, str, Path], config_root: Path
) -> None:
    """A bare number is matched only in a key column (DEC-737); a correction's `key` is the row's ID column."""
    storage, case_id, platform_db = erasure_store
    findings = find_principal(storage, NUMERIC_PERSON)
    location = next(loc for loc in findings.locations if loc.key == case_key("default", case_id))
    assert location.rewritable and location.rows == 2

    erase(
        storage,
        NUMERIC_PERSON,
        engine=sqlite_engine(platform_db),
        principal=LOCAL_OPERATOR,
        salt=SALT,
        config_root=config_root,
    )
    case = CaseStore(storage).get("default", case_id)
    assert [item.item_id for item in case.items] == ["fix_named"], "an item left with no correction goes"
    assert [answer.answer_id for answer in case.standing_answers_used] == ["sa_1"]
    assert _keys(case.items[0]) == [PERSON, "C-7"]


def test_a_case_that_does_not_hold_the_person_is_not_touched(
    erasure_store: tuple[LocalStorage, str, Path], config_root: Path
) -> None:
    storage, case_id, platform_db = erasure_store
    before = storage.read_bytes(case_key("default", case_id))
    erase(
        storage,
        "C-99999",
        engine=sqlite_engine(platform_db),
        principal=LOCAL_OPERATOR,
        salt=SALT,
        config_root=config_root,
    )
    assert storage.read_bytes(case_key("default", case_id)) == before
