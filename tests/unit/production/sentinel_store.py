"""A data directory with one distinctive principal id planted in every store the product has.

The erasure test proves the id is gone from every byte of the directory afterwards; the access-export
test proves every place it was is in the export. Both need the same store, and the store is only
convincing if it is laid out by the product's own writers - so the scoring run here is a real
`Pipeline.run_score` (with `tests/unit/test_run_score.py`'s stubs for the three expensive stages),
the row explanations go through the explain stage's writer, and the LLM cache entry through
`CompletionCache.put`.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from engine.config import RunMode, resolve_config
from engine.generative.cache import CACHE_DIRNAME, CompletionCache, cache_key
from engine.llm import LLMCompletion
from engine.platform_db import PLATFORM_DB_FILENAME, sqlite_engine
from engine.privacy.consent import ConsentLedger
from engine.privacy.contracts import ConsentStatus
from engine.registry import LocalModelRegistry
from engine.storage import LocalStorage, run_key
from tests.unit.production.privacy_support import (
    customers,
    write_copy_messages,
    write_dataset,
    write_row_explanations,
    write_run,
    write_source,
    write_upload,
)
from tests.unit.test_run_score import RUN_ID as SCORE_RUN_ID
from tests.unit.test_run_score import (
    SCHEMA_KEY,
    UPLOAD_ID,
    USE_CASE,
    StageStubs,
    make_frame,
    make_schema,
    run_flow,
)

SENTINEL = "ZQX-SENTINEL-7731"
SALT = "acme"
CLIENT = "cl_1"
CREATED = datetime(2026, 9, 1, tzinfo=UTC)

TRAIN_ON_UPLOAD = "r_20260901_train_upl"
TRAIN_ON_DATASET = "r_20260902_train_ds"
TRAIN_UNRELATED = "r_20260903_train_other"
MODEL_ON_UPLOAD = "m_targeted_advertisement_7"
MODEL_ON_DATASET = "m_targeted_advertisement_8"
MODEL_UNRELATED = "m_targeted_advertisement_9"


@dataclass(frozen=True)
class Planted:
    root: Path
    storage: LocalStorage

    @property
    def platform_db(self) -> Path:
        return self.root / PLATFORM_DB_FILENAME


def plant(root: Path, config_root: Path, monkeypatch: pytest.MonkeyPatch) -> Planted:
    """Every store, each holding the sentinel alongside other customers' rows."""
    storage = LocalStorage(root)
    keys = ["C-00000", "C-00001", SENTINEL, "C-00003"]

    # A real scoring run: scores.csv/.parquet, row_explanations.parquet, scoring_summary.json (sample rows).
    frame = make_frame()
    frame.loc[2, "customer_id"] = SENTINEL
    stubs = StageStubs(frame=frame).install(monkeypatch)
    del stubs
    write_upload(storage, UPLOAD_ID, frame.drop(columns=["marketing_opt_in"]), created_at=CREATED)
    storage.write_model(SCHEMA_KEY, make_schema())
    run_flow(resolve_config(USE_CASE, root=config_root), storage, LocalModelRegistry(root / "registry.db"))
    write_copy_messages(storage, SCORE_RUN_ID, keys)
    storage.write_text(
        run_key(SCORE_RUN_ID, "root_cause_summary.json"),
        json.dumps({"evidence": [{"id": "e1", "text": f"Customer {SENTINEL} complained twice."}]}, indent=2)
        + "\n",
    )

    # A training run on an upload that held the person, with its own sample of explanations.
    write_upload(storage, "u_train", customers(keys), created_at=CREATED, file_format="parquet")
    write_run(
        storage,
        TRAIN_ON_UPLOAD,
        mode=RunMode.TRAIN,
        created_at=CREATED,
        upload_id="u_train",
        model_version_id=MODEL_ON_UPLOAD,
    )
    write_row_explanations(storage, TRAIN_ON_UPLOAD, keys)
    storage.write_bytes(run_key(TRAIN_ON_UPLOAD, "model/predictor.pkl"), b"\x80\x04\x95model-weights-only")

    # Onboarding: a raw event table (the person appears on two bills), and a dataset built from it.
    bills = pd.DataFrame(
        {
            "bill_id": ["B-1", "B-2", "B-3", "B-4"],
            "customer_id": ["C-00000", SENTINEL, SENTINEL, "C-00001"],
            "amount": [10.0, 20.0, 30.0, 40.0],
        }
    )
    write_source(storage, CLIENT, "s_1", bills, created_at=CREATED)
    write_dataset(storage, "ds_1", customers(keys), built_at=CREATED, client_id=CLIENT, source_ids=("s_1",))
    write_run(
        storage,
        TRAIN_ON_DATASET,
        mode=RunMode.TRAIN,
        created_at=CREATED,
        dataset_id="ds_1",
        client_id=CLIENT,
        model_version_id=MODEL_ON_DATASET,
    )

    # A training run that never saw the person: its model must not be flagged.
    write_upload(storage, "u_other", customers(["C-00000", "C-00001"]), created_at=CREATED)
    write_run(
        storage,
        TRAIN_UNRELATED,
        mode=RunMode.TRAIN,
        created_at=CREATED,
        upload_id="u_other",
        model_version_id=MODEL_UNRELATED,
    )

    # A knowledge index chunk and an LLM cache entry that mention the person inside prose.
    chunks = pd.DataFrame(
        {"chunk_id": ["c1", "c2"], "text": [f"Ticket from {SENTINEL} about billing.", "General FAQ."]}
    )
    buffer = io.BytesIO()
    chunks.to_parquet(buffer, index=False)
    storage.write_bytes("indexes/ix_1/chunks.parquet", buffer.getvalue())
    cache = CompletionCache(root / CACHE_DIRNAME)
    key = cache_key(content_hash="p", system="s", user="u", model_id="fake", temperature=0.2)
    cache.put(
        key,
        LLMCompletion(
            text=f"Offer {SENTINEL} a discount.",
            model_id="fake",
            input_tokens=1,
            output_tokens=1,
            cost_estimate_usd=None,
            stop_reason="end_turn",
        ),
    )

    # The consent ledger (hashed).
    ledger = ConsentLedger(sqlite_engine(root / PLATFORM_DB_FILENAME), salt=SALT)
    for status in (ConsentStatus.GRANTED, ConsentStatus.WITHDRAWN):
        ledger.record(
            client_id=CLIENT,
            principal_id=SENTINEL,
            purpose="marketing_communication",
            status=status,
            source="test",
            recorded_at=CREATED + timedelta(days=1 if status is ConsentStatus.WITHDRAWN else 0),
        )
    return Planted(root=root, storage=storage)


def files_holding(root: Path, needle: str) -> list[str]:
    """Every file under `root` whose bytes, or whose decoded Parquet cells, contain `needle`."""
    found: list[str] = []
    raw = needle.encode("utf-8")
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        data = path.read_bytes()
        if raw in data:
            found.append(path.relative_to(root).as_posix())
            continue
        if path.suffix == ".parquet":
            table = pd.read_parquet(path)
            cells = (str(value) for column in table.columns for value in table[column].tolist())
            if any(needle in cell for cell in cells):
                found.append(path.relative_to(root).as_posix())
    return found
