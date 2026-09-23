"""Access requests (DEC-745): the export holds everything erasure would remove, and changes nothing."""

from __future__ import annotations

import csv
import io
import json
import zipfile
from pathlib import Path

import pytest

from engine.access.roles import LOCAL_OPERATOR
from engine.audit.events import principal_hash
from engine.platform_db import sqlite_engine
from engine.privacy.access_export import ACCESS_EXPORT_MEDIA_TYPE, export_principal
from engine.privacy.contracts import AccessExportManifest
from engine.privacy.erasure import erase
from engine.storage import run_key
from tests.unit.production.sentinel_store import (
    CLIENT,
    MODEL_ON_DATASET,
    MODEL_ON_UPLOAD,
    SALT,
    SENTINEL,
    Planted,
    files_holding,
    plant,
)
from tests.unit.test_run_score import RUN_ID as SCORE_RUN_ID


@pytest.fixture
def planted(tmp_path: Path, config_root: Path, monkeypatch: pytest.MonkeyPatch) -> Planted:
    return plant(tmp_path / "data", config_root, monkeypatch)


def open_zip(content: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(content))


def member_for(names: list[str], key: str) -> str:
    matches = [name for name in names if name.startswith("records/") and key.replace("/", "__") in name]
    assert len(matches) == 1, (key, matches)
    return matches[0]


def test_every_file_holding_the_person_is_in_the_export(planted: Planted) -> None:
    """The mirror of the erasure search: every file the byte search finds has a member in the archive."""
    holding = files_holding(planted.root, SENTINEL)
    assert holding, "nothing planted"
    export = export_principal(
        planted.storage, SENTINEL, salt=SALT, engine=sqlite_engine(planted.platform_db), client_id=CLIENT
    )
    archive = open_zip(export.content)
    names = archive.namelist()
    for key in holding:
        member = member_for(names, key)
        assert SENTINEL in archive.read(member).decode("utf-8"), member
    assert names[0] == "manifest.json"
    manifest = AccessExportManifest.model_validate_json(archive.read("manifest.json"))
    assert {location.key for location in manifest.locations} == set(holding)
    assert manifest.models == (MODEL_ON_UPLOAD, MODEL_ON_DATASET)
    assert manifest.principal_hash == principal_hash(SENTINEL, salt=SALT)
    assert tuple(names) == manifest.files
    assert export.media_type == ACCESS_EXPORT_MEDIA_TYPE
    assert export.filename.endswith(".zip")


def test_the_rows_come_with_their_own_header_and_only_the_persons_rows(planted: Planted) -> None:
    export = export_principal(planted.storage, SENTINEL, salt=SALT)
    archive = open_zip(export.content)
    member = member_for(archive.namelist(), run_key(SCORE_RUN_ID, "scores.csv"))
    rows = list(csv.reader(io.StringIO(archive.read(member).decode("utf-8"))))
    assert rows[0][:2] == ["customer_id", "propensity"]
    assert [row[0] for row in rows[1:]] == [SENTINEL]
    bills = list(
        csv.reader(
            io.StringIO(
                archive.read(member_for(archive.namelist(), "clients/cl_1/sources/s_1/raw.csv")).decode()
            )
        )
    )
    assert [row[0] for row in bills[1:]] == ["B-2", "B-3"]


def test_consent_and_erasure_history_are_included(planted: Planted, config_root: Path) -> None:
    engine = sqlite_engine(planted.platform_db)
    outcome = erase(
        planted.storage, SENTINEL, engine=engine, principal=LOCAL_OPERATOR, salt=SALT, config_root=config_root
    )
    archive = open_zip(
        export_principal(planted.storage, SENTINEL, salt=SALT, engine=engine, client_id=CLIENT).content
    )
    consent = json.loads(archive.read("consent_history.json"))
    assert [record["status"] for record in consent] == ["granted", "withdrawn"]
    erasures = json.loads(archive.read("erasure_history.json"))
    assert [record["request_id"] for record in erasures] == [outcome.request_id]
    assert not [
        name for name in archive.namelist() if name.startswith("records/")
    ], "nothing left after erasure"


def test_the_export_writes_nothing_to_the_store(planted: Planted) -> None:
    before = {key: planted.storage.read_bytes(key) for key in planted.storage.list_keys()}
    export_principal(planted.storage, SENTINEL, salt=SALT)
    after = {key: planted.storage.read_bytes(key) for key in planted.storage.list_keys()}
    assert after == before
