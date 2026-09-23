"""Erasure and access export with ids that are also numbers, or that the file spells differently (DEC-737).

Found by review: erasing `5` used to mask every visit count of 5 in other customers' rows, turn
`2.5` into `2.[erased]`, delete every row of a keyless file holding a 5, and export other customers'
whole rows. Erasing `00104` missed the scores, which carry `104` because ingest reads the key as a
number. A categorical Parquet column kept the erased id in its dictionary page, and a tombstoned JSON
row kept its other text.
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from engine.access.roles import LOCAL_OPERATOR
from engine.config import RunMode
from engine.platform_db import sqlite_engine
from engine.privacy.access_export import export_principal
from engine.privacy.config import ErasureMode
from engine.privacy.consent import principal_key
from engine.privacy.erasure import erase, find_principal
from engine.privacy.rewrite import FileKind, Matcher, rewrite_bytes, scan_bytes
from engine.storage import LocalStorage
from tests.unit.production.privacy_support import write_run, write_scores, write_upload

CREATED = datetime(2026, 9, 1, tzinfo=UTC)
SALT = "local"


def numeric_store(root: Path) -> LocalStorage:
    storage = LocalStorage(root)
    frame = pd.DataFrame(
        {
            "customer_id": [1, 2, 3, 4, 5, 6],
            "visits": [5, 3, 5, 0, 2, 5],
            "spend": [2.5, 10.0, 0.5, 7.5, 3.0, 12.5],
            "note": ["ok (5)", "x", "y", "z", "w", "v"],
        }
    )
    write_upload(storage, "u_1", frame, created_at=CREATED)
    write_run(
        storage, "r_train", mode=RunMode.TRAIN, created_at=CREATED, upload_id="u_1", model_version_id="m_1"
    )
    return storage


def test_erasing_a_numeric_id_touches_only_that_customer(tmp_path: Path) -> None:
    storage = numeric_store(tmp_path / "data")
    outcome = erase(
        storage, "5", engine=sqlite_engine(tmp_path / "platform.db"), principal=LOCAL_OPERATOR, salt=SALT
    )

    assert outcome.status == "completed"
    after = pd.read_csv(io.BytesIO(storage.read_bytes("uploads/u_1/source.csv")))
    assert after["customer_id"].tolist() == [1, 2, 3, 4, 6]
    assert after["visits"].tolist() == [5, 3, 5, 0, 5], "other customers' counts of 5 are theirs"
    assert after["spend"].tolist() == [2.5, 10.0, 0.5, 7.5, 12.5], "2.5 is not a mention of 5"
    assert after["note"].tolist() == ["ok (5)", "x", "y", "z", "v"]
    assert str(after["visits"].dtype) == "int64" and str(after["spend"].dtype) == "float64"
    profile = json.loads(storage.read_bytes("uploads/u_1/profile.json"))
    assert [row[0] for row in profile["preview_rows"]] == ["1", "2", "3", "4"], "the 5th preview row is gone"
    assert outcome.models_flagged == ("m_1",)


def test_a_numeric_id_in_a_file_with_no_recorded_key_is_reported_not_rewritten(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path / "data")
    frame = pd.DataFrame({"customer_id": [1, 5], "visits": [5, 3]})
    write_upload(storage, "u_unread", frame, created_at=CREATED)  # no run has read it: no key recorded
    before = storage.read_bytes("uploads/u_unread/source.csv")

    findings = find_principal(storage, "5")
    review = {location.key for location in findings.locations if location.needs_review}
    assert "uploads/u_unread/source.csv" in review
    assert findings.models == ()

    outcome = erase(
        storage, "5", engine=sqlite_engine(tmp_path / "platform.db"), principal=LOCAL_OPERATOR, salt=SALT
    )
    assert outcome.status == "completed_with_exceptions"
    assert "uploads/u_unread/source.csv" in outcome.unrewritable_keys
    assert storage.read_bytes("uploads/u_unread/source.csv") == before


def test_the_access_export_hands_over_only_the_customers_own_rows(tmp_path: Path) -> None:
    storage = numeric_store(tmp_path / "data")
    export = export_principal(storage, "5", salt=SALT)
    archive = zipfile.ZipFile(io.BytesIO(export.content))
    (member,) = [name for name in archive.namelist() if name.endswith("source.csv.csv")]
    rows = list(pd.read_csv(io.BytesIO(archive.read(member)))["customer_id"])
    assert rows == [5]


def test_access_export_leaves_out_a_row_that_only_mentions_the_id(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path / "data")
    frame = pd.DataFrame({"customer_id": ["C-1", "ZZ-9", "C-3"], "referred_by": ["ZZ-9", "", "x"]})
    write_upload(storage, "u_1", frame, created_at=CREATED)
    write_run(storage, "r_train", mode=RunMode.TRAIN, created_at=CREATED, upload_id="u_1")
    archive = zipfile.ZipFile(io.BytesIO(export_principal(storage, "ZZ-9", salt=SALT).content))
    (member,) = [name for name in archive.namelist() if name.endswith("source.csv.csv")]
    assert list(pd.read_csv(io.BytesIO(archive.read(member)))["customer_id"]) == ["ZZ-9"]


def test_a_zero_padded_id_reaches_the_scores_that_carry_its_number(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path / "data")
    frame = pd.DataFrame({"customer_id": ["00104", "00105"], "visits": [1, 2]})
    write_upload(storage, "u_1", frame, created_at=CREATED)
    write_run(storage, "r_score", mode=RunMode.TRAIN, created_at=CREATED, upload_id="u_1")
    write_scores(storage, "r_score", ["104", "105"])  # ingest read the key column as integers

    assert principal_key("00104") == principal_key(104) == "104"
    outcome = erase(
        storage, "00104", engine=sqlite_engine(tmp_path / "platform.db"), principal=LOCAL_OPERATOR, salt=SALT
    )
    assert outcome.status == "completed"
    scores = pd.read_csv(io.BytesIO(storage.read_bytes("runs/r_score/scores.csv")))
    assert scores["customer_id"].tolist() == [105]
    source = storage.read_bytes("uploads/u_1/source.csv").decode()
    assert "00104" not in source and "00105" in source


def test_a_categorical_parquet_column_forgets_the_erased_id(tmp_path: Path) -> None:
    frame = pd.DataFrame({"customer_id": pd.Categorical(["A-1", "ZZ-9", "A-3"]), "score": [0.1, 0.2, 0.3]})
    buffer = io.BytesIO()
    frame.to_parquet(buffer, index=False)
    matcher = Matcher.for_id("ZZ-9")
    new, hits = rewrite_bytes(
        FileKind.PARQUET,
        buffer.getvalue(),
        matcher,
        ("customer_id",),
        mode=ErasureMode.DELETE,
        tombstone="[erased]",
    )
    assert hits.rows == 1
    assert b"ZZ-9" not in new
    column = pq.read_table(pa.BufferReader(new)).column("customer_id")
    assert "ZZ-9" not in column.chunk(0).dictionary.to_pylist()
    assert not scan_bytes(FileKind.PARQUET, new, matcher, ("customer_id",)).any

    # a dictionary entry nobody uses is still the id in the file, and is found
    unused = pa.table({"customer_id": pa.array(["A-1", "ZZ-9"]).dictionary_encode()}).filter(
        pa.array([True, False])
    )
    sink = io.BytesIO()
    pq.write_table(unused, sink)
    assert scan_bytes(FileKind.PARQUET, sink.getvalue(), matcher, ("customer_id",)).any


def test_a_tombstoned_json_row_keeps_no_other_text() -> None:
    document = [
        {"customer_id": "ZZ-9", "city": "Pune", "note": "asked about loan", "visits": 3},
        {"customer_id": "A-1", "city": "Goa", "note": "fine", "visits": 1},
    ]
    new, hits = rewrite_bytes(
        FileKind.JSON,
        json.dumps(document).encode(),
        Matcher.for_id("ZZ-9"),
        ("customer_id",),
        mode=ErasureMode.TOMBSTONE,
        tombstone="[erased]",
    )
    assert hits.rows == 1
    rows = json.loads(new)
    assert rows[0] == {"customer_id": "[erased]", "city": "", "note": "", "visits": 3}
    assert rows[1] == document[1]


def test_consent_granted_to_a_zero_padded_id_reaches_the_gate_that_sees_its_number(tmp_path: Path) -> None:
    from engine.privacy.consent import ConsentLedger
    from engine.privacy.contracts import ConsentStatus

    ledger = ConsentLedger(sqlite_engine(tmp_path / "platform.db"), salt=SALT)
    ledger.record(
        client_id="acme",
        principal_id="00104",
        purpose="marketing_communication",
        status=ConsentStatus.GRANTED,
        source="test",
        recorded_at=CREATED,
    )
    verdict = ledger.classify("acme", "marketing_communication", [104, "105"], CREATED)
    assert verdict.valid == frozenset({"104"})
    assert verdict.missing == frozenset({"105"})
