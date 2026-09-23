"""Where personal data lives in the artefact store, and how each place records its own key.

Retention, erasure and the access export all need the same map of the store, so it is written once
here rather than three times. It was read off the code that writes each directory, not guessed
(DEC-735):

======================================  =====================================  =========================
Key                                     Written by                             Dated / keyed by
======================================  =====================================  =========================
`uploads/<id>/source.{csv,parquet}`     `api/routes/uploads.py`                `upload.json` created_at;
`uploads/<id>/{upload,profile,...}`                                            key = the consuming run's
                                                                               `run.json` primary_key
`clients/<c>/sources/<s>/raw.*`,        `api/routes/sources.py`                the `SourceSpec` row's
`.../profile.json`                                                             created_at (clients.db),
                                                                               else the profile's time;
                                                                               key unknown (event rows)
`datasets/<id>/*`                       `engine/onboarding/datasets.py`        manifest built_at and
                                                                               primary_key
`runs/<id>/*`                           `engine/pipeline.py`, `engine/runs.py`,  `run.json` created_at and
                                        `engine/generative/*`                   primary_key
`runs/<id>/model/**`, `models/**`       `engine/stages/train.py`, registry     binary, never rewritten
`indexes/<id>/*`                        `engine/generative/index.py`           knowledge documents
`llm_cache/<xx>/<key>.json`             `engine/generative/cache.py`           a cache entry: deleted,
                                                                               never edited
======================================  =====================================  =========================

Root-level files (`registry.db`, `clients.db`, `platform.db` and their journals) are databases, not
artefacts, and are never scanned as files: nothing in them holds a data principal's raw id (the
Phase 4b tables hold hashes, DEC-733).

Documents are read as raw JSON here, not through their pydantic models, on purpose: a retention or
erasure pass must not stop at the first record written by an older engine whose schema has since
grown a required field. What is read is only ever a date, an id or a column name.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

from engine.registry import aware_utc
from engine.storage import Storage, StorageError
from engine.utils.logging import get_logger

__all__ = [
    "TERMINAL_STATES",
    "VERSION_PURGE_PREFIXES",
    "DatasetInfo",
    "RunInfo",
    "Store",
    "StoreIndex",
    "UploadInfo",
    "is_artefact_key",
    "parse_time",
    "purge_old_versions",
    "read_json",
    "store_of",
]

_LOGGER = get_logger(__name__)

VERSION_PURGE_PREFIXES: Final[tuple[str, ...]] = ("uploads/", "runs/")
"""Where erasure and retention delete old object versions: the row-level prefixes the deployment
grants `s3:DeleteObjectVersion` on (`infra.naming.ROW_LEVEL_PREFIXES`). Elsewhere the lifecycle
backstop's `NoncurrentVersionExpiration` applies (DEC-708)."""

TERMINAL_STATES: Final[frozenset[str]] = frozenset({"done", "failed", "cancelled"})
"""A run in one of these states will write nothing more, so nothing it reads is still in use."""


class Store(StrEnum):
    """The stores M48 names: where a principal's data can be, as the erasure report counts them."""

    UPLOADS = "uploads"
    SOURCES = "sources"
    DATASETS = "datasets"
    SCORES = "scores"
    ROW_EXPLANATIONS = "row_explanations"
    COPY_MESSAGES = "copy_messages"
    RUN_ARTEFACTS = "run_artefacts"
    MODELS = "models"
    INDEXES = "indexes"
    LLM_CACHE = "llm_cache"
    OTHER = "other"


_RUN_FILE_STORES: Final[dict[str, Store]] = {
    "scores.csv": Store.SCORES,
    "scores.parquet": Store.SCORES,
    "row_explanations.parquet": Store.ROW_EXPLANATIONS,
    "copy_messages.csv": Store.COPY_MESSAGES,
}

_ROW_KEYS: Final[dict[str, tuple[str, ...]]] = {
    "row_explanations.parquet": ("primary_key",),  # `RowExplanation.primary_key`
    "copy_messages.csv": ("entity_key",),  # `CopyMessage.entity_key`
}
"""Run files whose key column is fixed by their row contract rather than by the run's primary key."""


def store_of(key: str) -> Store:
    """Which store a storage key belongs to."""
    parts = key.split("/")
    head = parts[0]
    if head == "uploads":
        return Store.UPLOADS
    if head == "clients" and len(parts) > 2 and parts[2] == "sources":
        return Store.SOURCES
    if head == "datasets":
        return Store.DATASETS
    if head == "models" or (head == "runs" and len(parts) > 3 and parts[2] == "model"):
        return Store.MODELS
    if head == "runs":
        return _RUN_FILE_STORES.get(parts[-1], Store.RUN_ARTEFACTS)
    if head == "indexes":
        return Store.INDEXES
    if head == "llm_cache":
        return Store.LLM_CACHE
    return Store.OTHER


def is_artefact_key(key: str) -> bool:
    """False for root-level database files and for an atomic write's temporary file."""
    name = key.rsplit("/", 1)[-1]
    return "/" in key and not (name.startswith(".") and name.endswith(".tmp"))


def purge_old_versions(storage: Storage, key: str) -> int:
    """Delete the old versions of a key erasure or retention just rewrote or deleted (DEC-708).

    Only a store that keeps versions has any (`S3Storage.purge_noncurrent_versions`); a local store
    has none. A failure is logged, never raised: the rewrite itself has already succeeded.
    """
    purge = getattr(storage, "purge_noncurrent_versions", None)
    if purge is None or not key.startswith(VERSION_PURGE_PREFIXES):
        return 0
    try:
        return int(purge(key))
    except StorageError as exc:
        _LOGGER.warning("privacy.versions could not delete old versions code=%s", exc.code)
        return 0


def read_json(storage: Storage, key: str) -> dict[str, Any] | None:
    """A JSON object from the store, or None when it is missing or not an object."""
    try:
        loaded = json.loads(storage.read_bytes(key))
    except (StorageError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def parse_time(value: object) -> datetime | None:
    """An ISO timestamp from a document as an aware UTC datetime, or None."""
    if not isinstance(value, str):
        return None
    try:
        return aware_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _key_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list | tuple):
        return tuple(str(item) for item in value)
    return ()


@dataclass(frozen=True)
class RunInfo:
    """What `run.json` says about one run, read leniently."""

    run_id: str
    mode: str
    state: str
    created_at: datetime | None
    use_case_id: str | None
    upload_id: str | None
    dataset_id: str | None
    client_id: str | None
    primary_key: tuple[str, ...]
    model_version_id: str | None

    @property
    def terminal(self) -> bool:
        """Whether the run has finished, one way or another."""
        return self.state in TERMINAL_STATES


@dataclass(frozen=True)
class UploadInfo:
    """What `upload.json` says about one upload."""

    upload_id: str
    created_at: datetime | None
    use_case_id: str | None


@dataclass(frozen=True)
class DatasetInfo:
    """What `dataset_manifest.json` says about one built dataset."""

    dataset_id: str
    created_at: datetime | None
    use_case_id: str | None
    client_id: str | None
    primary_key: tuple[str, ...]
    source_ids: tuple[str, ...]


class StoreIndex:
    """The store's runs, uploads and datasets, read once; answers "what is this key's primary key"."""

    def __init__(self, storage: Storage, keys: tuple[str, ...] | None = None) -> None:
        self._keys = keys if keys is not None else storage.list_keys()
        self.runs: dict[str, RunInfo] = {}
        self.uploads: dict[str, UploadInfo] = {}
        self.datasets: dict[str, DatasetInfo] = {}
        for key in self._keys:
            parts = key.split("/")
            if len(parts) == 3 and parts[0] == "runs" and parts[2] == "run.json":
                self._add_run(storage, key, parts[1])
            elif len(parts) == 3 and parts[0] == "uploads" and parts[2] == "upload.json":
                self._add_upload(storage, key, parts[1])
            elif len(parts) == 3 and parts[0] == "datasets" and parts[2] == "dataset_manifest.json":
                self._add_dataset(storage, key, parts[1])

    @property
    def keys(self) -> tuple[str, ...]:
        """Every key the index was built from."""
        return self._keys

    def _add_run(self, storage: Storage, key: str, run_id: str) -> None:
        doc = read_json(storage, key)
        if doc is None:
            _LOGGER.warning("privacy.layout: run %s has an unreadable run.json", run_id)
            return
        self.runs[run_id] = RunInfo(
            run_id=run_id,
            mode=str(doc.get("mode", "")),
            state=str(doc.get("state", "")),
            created_at=parse_time(doc.get("created_at")),
            use_case_id=_str_or_none(doc.get("use_case_id")),
            upload_id=_str_or_none(doc.get("upload_id")),
            dataset_id=_str_or_none(doc.get("dataset_id")),
            client_id=_str_or_none(doc.get("client_id")),
            primary_key=_key_tuple(doc.get("primary_key")),
            model_version_id=_str_or_none(doc.get("model_version_id")),
        )

    def _add_upload(self, storage: Storage, key: str, upload_id: str) -> None:
        doc = read_json(storage, key) or {}
        self.uploads[upload_id] = UploadInfo(
            upload_id=upload_id,
            created_at=parse_time(doc.get("created_at")),
            use_case_id=_str_or_none(doc.get("use_case_id")),
        )

    def _add_dataset(self, storage: Storage, key: str, dataset_id: str) -> None:
        doc = read_json(storage, key) or {}
        sources = doc.get("source_fingerprints")
        self.datasets[dataset_id] = DatasetInfo(
            dataset_id=dataset_id,
            created_at=parse_time(doc.get("built_at")),
            use_case_id=_str_or_none(doc.get("use_case")),
            client_id=_str_or_none(doc.get("client_id")),
            primary_key=_key_tuple(doc.get("primary_key")),
            source_ids=tuple(sorted(sources)) if isinstance(sources, dict) else (),
        )

    def runs_reading_upload(self, upload_id: str) -> tuple[RunInfo, ...]:
        """Every run that consumed the upload."""
        return tuple(run for run in self.runs.values() if run.upload_id == upload_id)

    def runs_reading_dataset(self, dataset_id: str) -> tuple[RunInfo, ...]:
        """Every run that consumed the built dataset."""
        return tuple(run for run in self.runs.values() if run.dataset_id == dataset_id)

    def key_columns(self, key: str) -> tuple[str, ...] | None:
        """The primary-key columns of a tabular file, from its own record; None when none says.

        None is an answer, not a failure: `engine.privacy.rewrite` then treats any cell equal to the
        id as marking the row, which is right for a client's raw event table.
        """
        parts = key.split("/")
        if parts[0] == "runs" and len(parts) == 3:
            fixed = _ROW_KEYS.get(parts[2])
            if fixed is not None:
                return fixed
            run = self.runs.get(parts[1])
            return run.primary_key if run is not None and run.primary_key else None
        if parts[0] == "datasets" and len(parts) == 3:
            dataset = self.datasets.get(parts[1])
            return dataset.primary_key if dataset is not None and dataset.primary_key else None
        if parts[0] == "uploads" and len(parts) == 3:
            columns = sorted({name for run in self.runs_reading_upload(parts[1]) for name in run.primary_key})
            return tuple(columns) or None
        return None


def _str_or_none(value: object) -> str | None:
    """A non-empty string field, stripped, or None."""
    if not isinstance(value, str):
        return None
    return value.strip() or None
