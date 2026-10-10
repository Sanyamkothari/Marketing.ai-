"""Plan J M107 (DEC-1317): an upload the engine writes is an upload in every respect.

The monthly loop keeps a pulled outcomes table and a learned experiment as ordinary uploads, written by
`engine.measurement.cycle.store_frame_upload`, which may not import `api.schemas.UploadRecord` (the engine
never imports the API, DEC-327). These tests keep its `upload.json` field for field the API's record and
read it back through the API's own loader, and pin the file names to `api.routes.uploads`'s. They also pin
the query rule's date literal and the third dialect's window query.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
import pytest

from api.routes import uploads as upload_routes
from api.schemas import UploadRecord
from engine.config import RunMode, load_use_case
from engine.connections.snowflake import snowflake_window
from engine.connections.sql import date_literal
from engine.measurement import cycle
from engine.storage import LocalStorage, upload_key

NOW = datetime(2026, 9, 1, 9, 30, tzinfo=UTC)


def test_the_engine_upload_document_has_exactly_the_api_records_fields() -> None:
    assert set(cycle._UploadDocument.model_fields) == set(UploadRecord.model_fields)
    for name, field in UploadRecord.model_fields.items():
        assert cycle._UploadDocument.model_fields[name].annotation == field.annotation, name


def test_the_file_names_are_the_upload_routes_own() -> None:
    assert cycle.UPLOAD_RECORD_FILENAME == upload_routes.UPLOAD_RECORD_FILENAME
    assert cycle.UPLOAD_PROFILE_FILENAME == upload_routes.UPLOAD_PROFILE_FILENAME
    assert cycle.UPLOAD_FINGERPRINT_FILENAME == upload_routes.UPLOAD_FINGERPRINT_FILENAME
    assert cycle.UPLOAD_VALIDATION_FILENAME == upload_routes.UPLOAD_VALIDATION_FILENAME
    assert upload_routes.source_filename("parquet") == "source.parquet"


def test_a_stored_frame_reads_back_as_an_upload_record(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    config = load_use_case("win-back-campaign")
    frame = pd.DataFrame(
        {"customer_id": [f"C{i}" for i in range(50)], "reactivated_90d": [i % 2 for i in range(50)]}
    )
    stored = cycle.store_frame_upload(
        storage, config, frame, file_name="crm.outcomes", mode=RunMode.SCORE, now=NOW
    )
    record = upload_routes.load_upload(storage, stored.upload_id)
    assert isinstance(record, UploadRecord)
    assert (record.row_count, record.column_count, record.mode) == (50, 2, RunMode.SCORE)
    assert record.source_key == upload_key(stored.upload_id, "source.parquet") == stored.source_key
    assert record.synthetic is False and record.created_at == NOW
    assert upload_routes.load_upload_profile(storage, stored.upload_id).row_count == 50
    back = pd.read_parquet(storage.local_path(stored.source_key))
    pd.testing.assert_frame_equal(back, frame)


def test_a_date_literal_is_written_from_a_date_only() -> None:
    assert date_literal(date(2026, 2, 1)) == "'2026-02-01'"
    with pytest.raises(TypeError):
        date_literal(datetime(2026, 2, 1, 10, 0))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        date_literal("2026-02-01' OR '1'='1")  # type: ignore[arg-type]


def test_snowflake_quotes_the_declared_column_with_doubled_double_quotes() -> None:
    assert snowflake_window("PUBLIC", "Outcomes", 'Day"Of', date(2026, 2, 1), date(2026, 3, 1), 9) == (
        'SELECT * FROM "PUBLIC"."Outcomes" WHERE "Day""Of" >= \'2026-02-01\' AND "Day""Of" < \'2026-03-01\' LIMIT 9'
    )
