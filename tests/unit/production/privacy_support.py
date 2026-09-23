"""Builders for a store laid out exactly the way the product lays it out, for the M48 tests.

Every document is written through its real contract where one exists (`RunRecord`, `UploadRecord`,
`DatasetProfile`, `ScoringSummary`, `CopyMessage` columns, `RowExplanation` rows via the explain
stage's own writer), so a test that erases or expires something proves the product can still read
what is left - not merely that a hand-rolled JSON survived.
"""

from __future__ import annotations

import io
import json
from datetime import datetime
from typing import Any

import pandas as pd

from api.schemas import UploadRecord
from engine.config import ProblemType, RunMode
from engine.contracts import (
    ColumnProfile,
    ColumnType,
    DatasetFingerprint,
    DatasetProfile,
    Direction,
    Reason,
    RowExplanation,
    RunRecord,
    RunState,
)
from engine.generative.contracts import CopyMessage
from engine.stages import explain
from engine.storage import LocalStorage, run_key, upload_key

USE_CASE = "targeted-advertisement"


def fingerprint(rows: int, columns: tuple[str, ...]) -> DatasetFingerprint:
    return DatasetFingerprint(hash="sha256:v1:test", algorithm="sha256:v1", n_rows=rows, columns=columns)


def profile_of(frame: pd.DataFrame, *, upload_id: str, profiled_at: datetime) -> DatasetProfile:
    """A `DatasetProfile` whose samples and preview really hold the frame's first values."""
    columns = tuple(
        ColumnProfile(
            name=str(name),
            position=position,
            dtype=str(frame[name].dtype),
            inferred_type=ColumnType.STRING,
            null_count=0,
            null_rate=0.0,
            distinct_count=int(frame[name].nunique()),
            is_unique=bool(frame[name].is_unique),
            is_constant=False,
            sample_values=tuple(str(value) for value in frame[name].head(5)),
            looks_like_id=False,
            looks_like_time=False,
        )
        for position, name in enumerate(frame.columns)
    )
    return DatasetProfile(
        upload_id=upload_id,
        file_name="file.csv",
        file_size_bytes=100,
        file_format="csv",
        delimiter=",",
        encoding="utf-8",
        row_count=len(frame.index),
        column_count=len(frame.columns),
        columns=columns,
        primary_key_candidates=("customer_id",),
        time_column_candidates=(),
        target_candidate=None,
        preview_rows=tuple(
            tuple(str(value) for value in row) for row in frame.head(5).itertuples(index=False)
        ),
        missing_value_rate_pct=0.0,
        fingerprint=fingerprint(len(frame.index), tuple(str(c) for c in frame.columns)),
        profiled_at=profiled_at,
    )


def write_upload(
    storage: LocalStorage,
    upload_id: str,
    frame: pd.DataFrame,
    *,
    created_at: datetime,
    use_case: str = USE_CASE,
    file_format: str = "csv",
) -> None:
    """`uploads/<id>/` as `POST /uploads` writes it: the file, its profile, fingerprint and record."""
    if file_format == "csv":
        source = upload_key(upload_id, "source.csv")
        storage.write_bytes(source, frame.to_csv(index=False).encode("utf-8"))
    else:
        source = upload_key(upload_id, "source.parquet")
        buffer = io.BytesIO()
        frame.to_parquet(buffer, index=False)
        storage.write_bytes(source, buffer.getvalue())
    profile = profile_of(frame, upload_id=upload_id, profiled_at=created_at)
    storage.write_model(upload_key(upload_id, "profile.json"), profile)
    storage.write_model(upload_key(upload_id, "fingerprint.json"), profile.fingerprint)
    record = UploadRecord(
        upload_id=upload_id,
        use_case_id=use_case,
        mode=RunMode.TRAIN,
        file_name="file.csv",
        file_format="csv" if file_format == "csv" else "parquet",
        file_size_bytes=100,
        delimiter=",",
        encoding="utf-8",
        row_count=len(frame.index),
        column_count=len(frame.columns),
        source_key=source,
        profile_key=upload_key(upload_id, "profile.json"),
        fingerprint_key=upload_key(upload_id, "fingerprint.json"),
        fingerprint_hash="sha256:v1:test",
        created_at=created_at,
    )
    storage.write_model(upload_key(upload_id, "upload.json"), record)


def write_run(
    storage: LocalStorage,
    run_id: str,
    *,
    mode: RunMode,
    created_at: datetime,
    state: RunState = RunState.DONE,
    upload_id: str | None = None,
    dataset_id: str | None = None,
    client_id: str | None = None,
    model_version_id: str | None = None,
    retention_days: int | None = None,
    use_case: str = USE_CASE,
) -> None:
    """`run.json` (and a `run_config.json` carrying `retention_days` when one is given)."""
    record = RunRecord(
        run_id=run_id,
        use_case_id=use_case,
        use_case_name="Targeted Advertisement",
        mode=mode,
        state=state,
        created_at=created_at,
        upload_id=upload_id if dataset_id is None else None,
        dataset_id=dataset_id,
        client_id=client_id,
        file_name="file.csv",
        primary_key="customer_id",
        problem_type=ProblemType.BINARY_CLASSIFICATION,
        model_choice="automl",
        model_version_id=model_version_id,
        engine_version="0.1.0",
    )
    storage.write_model(run_key(run_id, "run.json"), record)
    if retention_days is not None:
        document = {"config": {"governance": {"retention_days": retention_days}}}
        storage.write_text(run_key(run_id, "run_config.json"), json.dumps(document))


def write_scores(storage: LocalStorage, run_id: str, keys: list[str]) -> None:
    """`scores.csv` and `scores.parquet` with the export stage's header."""
    frame = pd.DataFrame(
        {
            "customer_id": keys,
            "propensity": [0.5 + index / (10 * len(keys)) for index in range(len(keys))],
            "band": ["Medium"] * len(keys),
            "action": ["Monitor"] * len(keys),
            "reason_1": [f"visits ↑ ({index})" for index in range(len(keys))],
            "suppressed_reason": [None] * len(keys),
            "control_group": [False] * len(keys),
        }
    )
    storage.write_bytes(run_key(run_id, "scores.csv"), frame.to_csv(index=False).encode("utf-8"))
    buffer = io.BytesIO()
    frame.to_parquet(buffer, index=False)
    storage.write_bytes(run_key(run_id, "scores.parquet"), buffer.getvalue())


def write_row_explanations(storage: LocalStorage, run_id: str, keys: list[str]) -> None:
    """`row_explanations.parquet` through the explain stage's own writer (nested reasons and all)."""
    rows = [
        RowExplanation(
            primary_key=key,
            score=0.5,
            reasons=(
                Reason(
                    feature="visits", value="3", contribution=0.1, direction=Direction.UP, text="visits ↑ (3)"
                ),
            ),
        )
        for key in keys
    ]
    explain.write_row_explanations(rows, run_id=run_id, storage=storage)


def write_copy_messages(storage: LocalStorage, run_id: str, keys: list[str]) -> None:
    """`copy_messages.csv` with `CopyMessage`'s columns; the rendered text names the customer."""
    columns = list(CopyMessage.model_fields)
    rows: list[dict[str, Any]] = [
        {
            "entity_key": key,
            "band": "High",
            "channel": "sms",
            "variant": "A",
            "template_id": "t1",
            "rendered_text": f"Hi {key}, we miss you.",
            "status": "approved",
            "block_reason": "",
            "backend": "fake",
        }
        for key in keys
    ]
    storage.write_bytes(
        run_key(run_id, "copy_messages.csv"), pd.DataFrame(rows, columns=columns).to_csv(index=False).encode()
    )


def write_dataset(
    storage: LocalStorage,
    dataset_id: str,
    frame: pd.DataFrame,
    *,
    built_at: datetime,
    client_id: str,
    source_ids: tuple[str, ...] = (),
    use_case: str = USE_CASE,
) -> None:
    """`datasets/<id>/` - the frame, its 50-row sample and a manifest carrying the key and the lineage."""
    buffer = io.BytesIO()
    frame.to_parquet(buffer, index=False)
    storage.write_bytes(f"datasets/{dataset_id}/dataset.parquet", buffer.getvalue())
    sample = [{str(k): str(v) for k, v in row.items()} for row in frame.head(50).to_dict(orient="records")]
    storage.write_text(f"datasets/{dataset_id}/sample.json", json.dumps(sample, indent=2) + "\n")
    manifest = {
        "dataset_id": dataset_id,
        "client_id": client_id,
        "use_case": use_case,
        "primary_key": ["customer_id"],
        "source_fingerprints": {source: {"hash": "x"} for source in source_ids},
        "built_at": built_at.isoformat(),
    }
    storage.write_text(f"datasets/{dataset_id}/dataset_manifest.json", json.dumps(manifest, indent=2) + "\n")
    storage.write_text(f"datasets/{dataset_id}/features.sql", "SELECT customer_id FROM entity\n")


def write_source(
    storage: LocalStorage, client_id: str, source_id: str, frame: pd.DataFrame, *, created_at: datetime
) -> None:
    """`clients/<c>/sources/<s>/raw.csv` and a profile shaped like `SourceProfile` (samples included)."""
    base = f"clients/{client_id}/sources/{source_id}"
    storage.write_bytes(f"{base}/raw.csv", frame.to_csv(index=False).encode("utf-8"))
    profile = profile_of(frame, upload_id=source_id, profiled_at=created_at)
    document = {
        "source_id": source_id,
        "client_id": client_id,
        "file_name": "bills.csv",
        "profile": json.loads(profile.model_dump_json()),
    }
    storage.write_text(f"{base}/profile.json", json.dumps(document, indent=2) + "\n")


def customers(keys: list[str]) -> pd.DataFrame:
    """A small entity table."""
    return pd.DataFrame(
        {
            "customer_id": keys,
            "visits_last_7d": list(range(len(keys))),
            "plan_tier": ["basic" if index % 2 else "premium" for index in range(len(keys))],
        }
    )
