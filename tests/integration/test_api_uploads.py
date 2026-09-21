"""`POST /uploads` and `GET /uploads/{id}/profile` end to end through `TestClient` (design §6.8).

`engine.stages.ingest` is landing in parallel, so the route logic is exercised against a small
in-module stand-in for its M2 surface: a real CSV goes in, a real `DatasetProfile` comes out, and
every `IngestError` code is raised where the real module would raise it. What is under test here is
the *route* - the size limit, the streaming write, the status codes, the error envelope, the upload
directory and the clean-up - none of which changes when the real ingest lands.

`tests/integration/test_api_runs.py` imports the fixtures and the stub installer from this module by
plain module import, as design §6 asks (never through `conftest.py`).
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from api.schemas import UploadRecord, UploadResponse
from engine.config import ColumnType, UseCaseConfig, load_use_case
from engine.contracts import CategoryCount, ColumnProfile, DatasetFingerprint, DatasetProfile
from engine.stages import ingest
from engine.storage import LocalStorage, Storage
from engine.utils.time import utc_now

pytestmark = pytest.mark.integration

DEMO_ID = "targeted-advertisement"
PLANNED_ID = "ai-onboarding-assistant"
UNKNOWN_ID = "not-a-use-case"

CLEAN_CSV = (
    "customer_id,snapshot_date,tenure_months,converted_30d\n"
    "C-1,2026-08-01,12,1\n"
    "C-2,2026-08-01,30,0\n"
    "C-3,2026-08-01,7,0\n"
)

INGEST_ERROR_STATUS = {
    "UPLOAD_EMPTY": 422,
    "UPLOAD_NO_COLUMNS": 422,
    "UPLOAD_NO_ROWS": 422,
    "UPLOAD_DUPLICATE_COLUMNS": 422,
    "UPLOAD_ENCODING_UNSUPPORTED": 422,
    "UPLOAD_UNREADABLE": 422,
    "UPLOAD_UNSUPPORTED_FORMAT": 415,
}

#: Names of the M2 ingest surface this module stands in for; used to skip the real-engine tests.
INGEST_NAMES = ("IngestError", "file_format_for", "read_upload", "profile_dataset", "ingest_detail")

REAL_INGEST = all(hasattr(ingest, name) for name in INGEST_NAMES)


# ---------------------------------------------------------------------------
# The stand-in for `engine.stages.ingest`'s M2 surface
# ---------------------------------------------------------------------------
class StubIngestError(Exception):
    """Mirrors design §1.2's `IngestError`: a code and a business-language message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class StubFrame:
    """The smallest thing the routes pass around: a header and the rows under it."""

    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]

    def __len__(self) -> int:
        return len(self.rows)


@dataclass(frozen=True)
class StubReadResult:
    """Mirrors design §1.3's `ReadResult`."""

    frame: StubFrame
    file_format: str
    delimiter: str | None
    encoding: str
    row_count: int
    row_count_estimated: bool = False
    truncated: bool = False


def stub_file_format_for(key: str) -> str:
    suffix = key.rsplit(".", 1)[-1].lower() if "." in key else ""
    if suffix == "csv":
        return "csv"
    if suffix in {"parquet", "pq"}:
        return "parquet"
    raise StubIngestError("UPLOAD_UNSUPPORTED_FORMAT", "Only CSV and Parquet files can be uploaded.")


def stub_read_upload(storage: Storage, key: str, **_kwargs: Any) -> StubReadResult:
    """Parse the stored bytes as a comma-separated table, raising where the real reader would."""
    payload = storage.read_bytes(key)
    if not payload:
        raise StubIngestError("UPLOAD_EMPTY", "The file is empty.")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise StubIngestError(
            "UPLOAD_ENCODING_UNSUPPORTED",
            "The file is not text the engine can read. Save it as UTF-8 CSV and upload it again.",
        ) from exc
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise StubIngestError("UPLOAD_EMPTY", "The file is empty.")
    header = tuple(name.strip() for name in lines[0].split(","))
    if not any(header):
        raise StubIngestError("UPLOAD_NO_COLUMNS", "The first row of the file does not contain column names.")
    if len(set(header)) != len(header):
        raise StubIngestError(
            "UPLOAD_DUPLICATE_COLUMNS",
            "The header repeats a column name. Every column needs a distinct name.",
        )
    rows = tuple(tuple(cell.strip() for cell in line.split(",")) for line in lines[1:])
    if not rows:
        raise StubIngestError("UPLOAD_NO_ROWS", "The file has a header row but no data rows.")
    if any(len(row) != len(header) for row in rows):
        raise StubIngestError(
            "UPLOAD_UNREADABLE", "The file could not be read as CSV. Check that it is a valid CSV file."
        )
    return StubReadResult(
        frame=StubFrame(columns=header, rows=rows),
        file_format="csv",
        delimiter=",",
        encoding="utf-8",
        row_count=len(rows),
    )


def stub_profile_dataset(
    frame: StubFrame,
    config: UseCaseConfig,
    *,
    upload_id: str,
    file_name: str,
    file_format: str,
    file_size_bytes: int,
    delimiter: str | None,
    encoding: str,
    row_count: int | None = None,
) -> DatasetProfile:
    """A real `DatasetProfile` over the stub frame: the shape the Setup screen renders."""
    total = len(frame) if row_count is None else row_count
    columns = tuple(_column_profile(frame, name, position) for position, name in enumerate(frame.columns))
    target = config.target.column if config.target.column in frame.columns else None
    return DatasetProfile(
        upload_id=upload_id,
        file_name=file_name,
        file_size_bytes=file_size_bytes,
        file_format="csv" if file_format == "csv" else "parquet",
        delimiter=delimiter,
        encoding=encoding,
        row_count=total,
        column_count=len(frame.columns),
        columns=columns,
        primary_key_candidates=tuple(column.name for column in columns if column.is_unique),
        time_column_candidates=tuple(column.name for column in columns if column.looks_like_time),
        target_candidate=target,
        preview_rows=frame.rows[:5],
        missing_value_rate_pct=0.0,
        fingerprint=DatasetFingerprint(
            hash=f"sha256:v1:{upload_id}",
            algorithm="sha256",
            n_rows=total,
            columns=frame.columns,
        ),
        profiled_at=utc_now(),
    )


def stub_ingest_detail(profile: DatasetProfile) -> str:
    return f"{profile.row_count} rows · {profile.column_count} columns · {profile.file_format.upper()}"


def _column_profile(frame: StubFrame, name: str, position: int) -> ColumnProfile:
    values = [row[position] for row in frame.rows]
    present = [value for value in values if value != ""]
    distinct = sorted(set(present))
    return ColumnProfile(
        name=name,
        position=position,
        dtype="object",
        inferred_type=ColumnType.STRING,
        null_count=len(values) - len(present),
        null_rate=(len(values) - len(present)) / len(values) if values else 0.0,
        distinct_count=len(distinct),
        is_unique=len(distinct) == len(present) and bool(present),
        is_constant=len(distinct) == 1,
        sample_values=tuple(present[:5]),
        top_categories=(
            tuple(
                CategoryCount(
                    value=value, count=present.count(value), share=present.count(value) / len(present)
                )
                for value in distinct[:10]
            )
            if present
            else ()
        ),
        looks_like_id=name.endswith("_id"),
        looks_like_time=any(token in name for token in ("date", "time", "day", "week", "month")),
    )


def install_ingest_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point `engine.stages.ingest`'s M2 surface at the stand-ins above, for this test only."""
    monkeypatch.setattr(ingest, "IngestError", StubIngestError, raising=False)
    monkeypatch.setattr(ingest, "ReadResult", StubReadResult, raising=False)
    monkeypatch.setattr(ingest, "file_format_for", stub_file_format_for, raising=False)
    monkeypatch.setattr(ingest, "read_upload", stub_read_upload, raising=False)
    monkeypatch.setattr(ingest, "profile_dataset", stub_profile_dataset, raising=False)
    monkeypatch.setattr(ingest, "ingest_detail", stub_ingest_detail, raising=False)


# ---------------------------------------------------------------------------
# Fixtures (design §6: owned here, imported by `test_api_runs.py`)
# ---------------------------------------------------------------------------
@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """A fresh artefact store per test, so `list_keys` assertions mean something."""
    directory = tmp_path / "data"
    directory.mkdir()
    return directory


@pytest.fixture
def client(config_root: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """One app over the checkout's `configs/` and a temporary data directory, with ingest stubbed."""
    install_ingest_stub(monkeypatch)
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as test_client:
        yield test_client


@pytest.fixture
def storage(data_dir: Path) -> LocalStorage:
    return LocalStorage(data_dir)


def upload_csv(
    client: TestClient, payload: bytes = CLEAN_CSV.encode(), *, name: str = "history.csv", mode: str = "train"
) -> Any:
    """`POST /uploads` with one in-memory file; returns the raw response."""
    return client.post(
        "/uploads",
        files={"file": (name, payload, "text/csv")},
        data={"use_case": DEMO_ID, "mode": mode},
    )


def small_config_root(config_root: Path, destination: Path, *, max_file_size_mb: int) -> Path:
    """A copy of `configs/` whose upload limit is `max_file_size_mb`, for the 413 test."""
    copied = destination / "configs"
    shutil.copytree(config_root, copied)
    engine_yaml = copied / "engine.yaml"
    engine_yaml.write_text(
        engine_yaml.read_text().replace("max_file_size_mb: 2048", f"max_file_size_mb: {max_file_size_mb}")
    )
    return copied


# ---------------------------------------------------------------------------
# POST /uploads
# ---------------------------------------------------------------------------
def test_upload_returns_201_with_the_profile_and_a_location_header(
    client: TestClient, storage: LocalStorage
) -> None:
    response = upload_csv(client)
    assert response.status_code == 201, response.text
    body = UploadResponse.model_validate(response.json())
    assert body.upload_id.startswith("u_")
    assert response.headers["Location"] == f"/uploads/{body.upload_id}/profile"
    assert body.profile.row_count == 3
    assert body.profile.column_count == 4
    assert body.profile.upload_id == body.upload_id
    assert body.profile.primary_key_candidates[0] == "customer_id"
    assert body.profile.target_candidate == load_use_case(DEMO_ID).target.column
    assert len(body.profile.preview_rows) == 3
    stored = set(storage.list_keys(f"uploads/{body.upload_id}/"))
    assert stored == {
        f"uploads/{body.upload_id}/{name}"
        for name in ("source.csv", "upload.json", "profile.json", "fingerprint.json")
    }


def test_upload_record_carries_everything_a_later_run_needs(
    client: TestClient, storage: LocalStorage
) -> None:
    upload_id = upload_csv(client).json()["upload_id"]
    record = storage.read_model(f"uploads/{upload_id}/upload.json", UploadRecord)
    assert record.upload_id == upload_id
    assert record.use_case_id == DEMO_ID
    assert record.mode.value == "train"
    assert record.file_name == "history.csv"
    assert record.file_format == "csv"
    assert record.file_size_bytes == len(CLEAN_CSV.encode())
    assert record.row_count == 3
    assert record.column_count == 4
    assert record.source_key == f"uploads/{upload_id}/source.csv"
    assert record.fingerprint_hash.startswith("sha256:v1:")


def test_upload_over_the_configured_limit_is_413_and_leaves_nothing_behind(
    config_root: Path, tmp_path: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_ingest_stub(monkeypatch)
    root = small_config_root(config_root, tmp_path, max_file_size_mb=1)
    store = LocalStorage(data_dir)
    with TestClient(create_app(config_root=root, data_dir=data_dir)) as client:
        response = client.post(
            "/uploads",
            files={"file": ("big.csv", b"a,b\n" + b"1,2\n" * 400_000, "text/csv")},
            data={"use_case": DEMO_ID, "mode": "train"},
        )
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "UPLOAD_TOO_LARGE"
    assert "1 MB limit" in response.json()["detail"]["message"]
    assert store.list_keys("uploads/") == ()


def test_upload_of_an_unsupported_type_is_415_and_stores_nothing(
    client: TestClient, storage: LocalStorage
) -> None:
    response = upload_csv(client, b"not a table", name="report.docx")
    assert response.status_code == 415
    assert response.json()["detail"]["code"] == "UPLOAD_UNSUPPORTED_FORMAT"
    assert storage.list_keys("uploads/") == ()


def test_malformed_csv_is_422_unreadable_and_leaves_no_orphan(
    client: TestClient, storage: LocalStorage
) -> None:
    response = upload_csv(client, b"a,b\n1,2,3\n")
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "UPLOAD_UNREADABLE"
    assert storage.list_keys("uploads/") == ()


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (b"", "UPLOAD_EMPTY"),
        (b",,\nx,y,z\n", "UPLOAD_NO_COLUMNS"),
        (b"a,b\n", "UPLOAD_NO_ROWS"),
        (b"a,a\n1,2\n", "UPLOAD_DUPLICATE_COLUMNS"),
        (b"\xff\xfe\x00a,b\n1,2\n", "UPLOAD_ENCODING_UNSUPPORTED"),
    ],
)
def test_every_ingest_error_becomes_its_documented_status_and_leaves_no_orphan(
    client: TestClient, storage: LocalStorage, payload: bytes, code: str
) -> None:
    response = upload_csv(client, payload)
    assert response.status_code == INGEST_ERROR_STATUS[code]
    assert response.json()["detail"]["code"] == code
    assert storage.list_keys("uploads/") == ()


def test_planned_and_unknown_use_cases_are_404_before_any_byte_is_stored(
    client: TestClient, storage: LocalStorage
) -> None:
    for use_case, code in ((PLANNED_ID, "USE_CASE_PLANNED"), (UNKNOWN_ID, "USE_CASE_NOT_FOUND")):
        response = client.post(
            "/uploads",
            files={"file": ("history.csv", CLEAN_CSV.encode(), "text/csv")},
            data={"use_case": use_case, "mode": "train"},
        )
        assert response.status_code == 404
        assert response.json()["detail"]["code"] == code
    assert storage.list_keys("uploads/") == ()


def test_every_error_body_is_the_m1_envelope(client: TestClient) -> None:
    response = upload_csv(client, b"", name="empty.csv")
    detail = response.json()["detail"]
    assert set(detail) == {"code", "message", "path"}
    assert isinstance(detail["message"], str) and detail["message"]


# ---------------------------------------------------------------------------
# GET /uploads/{id}/profile
# ---------------------------------------------------------------------------
def test_profile_is_byte_identical_to_the_stored_document(client: TestClient, storage: LocalStorage) -> None:
    upload_id = upload_csv(client).json()["upload_id"]
    response = client.get(f"/uploads/{upload_id}/profile")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.content == storage.read_bytes(f"uploads/{upload_id}/profile.json")
    assert json.loads(response.content)["upload_id"] == upload_id


def test_profile_of_an_unknown_upload_is_404(client: TestClient) -> None:
    response = client.get("/uploads/u_does_not_exist/profile")
    assert response.status_code == 404
    assert response.json()["detail"] == {
        "code": "UPLOAD_NOT_FOUND",
        "message": "No upload with id 'u_does_not_exist'.",
        "path": None,
    }


# ---------------------------------------------------------------------------
# The same route against the real ingest module, once it lands
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not REAL_INGEST, reason="engine.stages.ingest's M2 surface has not landed yet")
def test_real_ingest_profiles_a_clean_upload(config_root: Path, data_dir: Path) -> None:
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as client:
        response = upload_csv(client)
    assert response.status_code == 201, response.text
    profile = UploadResponse.model_validate(response.json()).profile
    assert profile.row_count == 3
    assert profile.column_count == 4
    assert profile.delimiter == ","
    assert profile.fingerprint.hash.startswith("sha256:v1:")
