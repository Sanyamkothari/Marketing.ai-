"""Erasure (DEC-741…743): a planted sentinel is gone from every byte afterwards, and the rest still reads."""

from __future__ import annotations

import io
import json
import shutil
from pathlib import Path

import pandas as pd
import pytest

from engine.access.roles import LOCAL_OPERATOR
from engine.audit.events import principal_hash
from engine.config import resolve_config
from engine.contracts import DatasetProfile, ScoringSummary, scores_csv_columns
from engine.platform_db import sqlite_engine
from engine.privacy import erasure as erasure_module
from engine.privacy.config import ErasureMode
from engine.privacy.consent import ConsentLedger
from engine.privacy.erasure import (
    clear_flag,
    erase,
    erasure_request,
    erasure_requests,
    find_principal,
    models_flagged_for_retraining,
    retrain_flags,
)
from engine.privacy.errors import PrivacyError
from engine.privacy.layout import Store
from engine.privacy.rewrite import FileKind, Matcher, rewrite_bytes, scan_bytes
from engine.storage import run_key
from tests.unit.production.sentinel_store import (
    CLIENT,
    MODEL_ON_DATASET,
    MODEL_ON_UPLOAD,
    SALT,
    SENTINEL,
    TRAIN_ON_UPLOAD,
    Planted,
    files_holding,
    plant,
)
from tests.unit.production.test_retention import ListAuditLog
from tests.unit.test_run_score import RUN_ID as SCORE_RUN_ID
from tests.unit.test_run_score import USE_CASE


@pytest.fixture
def planted(tmp_path: Path, config_root: Path, monkeypatch: pytest.MonkeyPatch) -> Planted:
    return plant(tmp_path / "data", config_root, monkeypatch)


def test_the_sentinel_really_is_everywhere_before(planted: Planted) -> None:
    """The guard against a vacuous pass: the search below must have had something to find."""
    holding = set(files_holding(planted.root, SENTINEL))
    stores = {
        "uploads/u_000000000002/source.csv",
        "uploads/u_000000000002/profile.json",
        "uploads/u_train/source.parquet",
        "clients/cl_1/sources/s_1/raw.csv",
        "clients/cl_1/sources/s_1/profile.json",
        "datasets/ds_1/dataset.parquet",
        "datasets/ds_1/sample.json",
        f"runs/{SCORE_RUN_ID}/scores.csv",
        f"runs/{SCORE_RUN_ID}/scores.parquet",
        f"runs/{SCORE_RUN_ID}/row_explanations.parquet",
        f"runs/{SCORE_RUN_ID}/scoring_summary.json",
        f"runs/{SCORE_RUN_ID}/copy_messages.csv",
        f"runs/{SCORE_RUN_ID}/root_cause_summary.json",
        f"runs/{TRAIN_ON_UPLOAD}/row_explanations.parquet",
        "indexes/ix_1/chunks.parquet",
    }
    assert stores <= holding, stores - holding
    assert any(key.startswith("llm_cache/") for key in holding)


def test_find_principal_names_every_store_and_the_exposed_models(planted: Planted) -> None:
    findings = find_principal(planted.storage, SENTINEL)
    assert set(findings.stores()) >= {
        Store.UPLOADS,
        Store.SOURCES,
        Store.DATASETS,
        Store.SCORES,
        Store.ROW_EXPLANATIONS,
        Store.COPY_MESSAGES,
        Store.RUN_ARTEFACTS,
        Store.INDEXES,
        Store.LLM_CACHE,
    }
    assert findings.models == (MODEL_ON_UPLOAD, MODEL_ON_DATASET)
    scores = next(loc for loc in findings.locations if loc.key == run_key(SCORE_RUN_ID, "scores.csv"))
    assert (scores.rows, scores.key_columns) == (1, ("customer_id",))
    raw = next(loc for loc in findings.locations if loc.key.endswith("raw.csv"))
    assert raw.rows == 2, "an event table's rows are matched on any cell when no key is recorded"
    assert SENTINEL not in findings.model_dump_json()


def test_erasure_leaves_no_byte_of_the_id_anywhere(planted: Planted, config_root: Path) -> None:
    engine = sqlite_engine(planted.platform_db)
    log = ListAuditLog()
    outcome = erase(
        planted.storage,
        SENTINEL,
        engine=engine,
        principal=LOCAL_OPERATOR,
        salt=SALT,
        client_id=CLIENT,
        config_root=config_root,
        audit_log=log,
    )
    assert outcome.status == "completed"
    assert outcome.rows_deleted >= 8
    engine.dispose()
    assert files_holding(planted.root, SENTINEL) == []
    assert find_principal(planted.storage, SENTINEL).locations == ()

    # the request is recorded, the audit event written, and neither holds the id
    hashed = principal_hash(SENTINEL, salt=SALT)
    stored = erasure_request(engine, outcome.request_id)
    assert stored is not None
    assert (stored.status, stored.principal_hash, stored.client_id) == ("completed", hashed, CLIENT)
    assert set(stored.store_counts) >= {"uploads", "scores", "llm_cache", "datasets", "sources"}
    assert erasure_requests(engine, principal_hash=hashed)[0].request_id == outcome.request_id
    (event,) = log.events
    assert (event.action, event.object_id, event.outcome) == (
        "privacy.erasure",
        outcome.request_id,
        "success",
    )
    assert event.details["principal_hash"] == hashed
    assert SENTINEL not in event.model_dump_json()

    # the consent evidence is kept (policy `keep`), under the hash
    assert len(ConsentLedger(engine, salt=SALT).history(SENTINEL)) == 2


def test_every_rewritten_file_is_still_read_by_the_product(planted: Planted, config_root: Path) -> None:
    storage = planted.storage
    before = pd.read_csv(io.BytesIO(storage.read_bytes(run_key(SCORE_RUN_ID, "scores.csv"))))
    erase(
        storage,
        SENTINEL,
        engine=sqlite_engine(planted.platform_db),
        principal=LOCAL_OPERATOR,
        salt=SALT,
        config_root=config_root,
    )
    config = resolve_config(USE_CASE, root=config_root).config
    after = pd.read_csv(io.BytesIO(storage.read_bytes(run_key(SCORE_RUN_ID, "scores.csv"))))
    assert list(after.columns) == list(scores_csv_columns(config, "customer_id"))
    assert len(after) == len(before) - 1
    assert "C-00001" in set(after["customer_id"]), "another customer's row went too"
    parquet = pd.read_parquet(io.BytesIO(storage.read_bytes(run_key(SCORE_RUN_ID, "scores.parquet"))))
    assert list(parquet.columns) == list(after.columns) and len(parquet) == len(after)
    explanations = pd.read_parquet(
        io.BytesIO(storage.read_bytes(run_key(SCORE_RUN_ID, "row_explanations.parquet")))
    )
    assert set(explanations.columns) >= {"primary_key", "reasons"} and len(explanations) == len(after)
    summary = storage.read_model(run_key(SCORE_RUN_ID, "scoring_summary.json"), ScoringSummary)
    assert all(row.primary_key != SENTINEL for row in summary.sample_rows)
    profile = storage.read_model("uploads/u_000000000002/profile.json", DatasetProfile)
    assert all(SENTINEL not in column.sample_values for column in profile.columns)
    bills = pd.read_csv(io.BytesIO(storage.read_bytes("clients/cl_1/sources/s_1/raw.csv")))
    assert list(bills["bill_id"]) == ["B-1", "B-4"]
    dataset = pd.read_parquet(io.BytesIO(storage.read_bytes("datasets/ds_1/dataset.parquet")))
    assert len(dataset) == 3
    chunks = pd.read_parquet(io.BytesIO(storage.read_bytes("indexes/ix_1/chunks.parquet")))
    assert list(chunks["text"]) == ["Ticket from [erased] about billing.", "General FAQ."]
    assert not storage.list_keys("llm_cache/"), "a cache entry that mentions the person is deleted"
    assert json.loads(storage.read_text("datasets/ds_1/sample.json"))[0]["customer_id"] == "C-00000"


def test_models_trained_on_the_person_are_flagged_for_the_next_cycle(
    planted: Planted, config_root: Path
) -> None:
    engine = sqlite_engine(planted.platform_db)
    outcome = erase(
        planted.storage, SENTINEL, engine=engine, principal=LOCAL_OPERATOR, salt=SALT, config_root=config_root
    )
    assert outcome.models_flagged == (MODEL_ON_UPLOAD, MODEL_ON_DATASET)
    assert models_flagged_for_retraining(engine) == tuple(sorted((MODEL_ON_UPLOAD, MODEL_ON_DATASET)))
    reasons = {flag.model_id: flag.reason for flag in retrain_flags(engine)}
    assert reasons[MODEL_ON_UPLOAD] == "training_input:upload"
    assert reasons[MODEL_ON_DATASET] == "training_input:dataset"
    # nothing retrained, nothing demoted: the model files are exactly as they were
    assert planted.storage.exists(run_key(TRAIN_ON_UPLOAD, "model/predictor.pkl"))
    assert clear_flag(engine, MODEL_ON_UPLOAD) == 1
    assert clear_flag(engine, MODEL_ON_UPLOAD) == 0
    assert models_flagged_for_retraining(engine) == (MODEL_ON_DATASET,)
    assert len(retrain_flags(engine, open_only=False)) == 2


def test_tombstone_mode_keeps_the_rows_without_the_identity(
    planted: Planted, config_root: Path, tmp_path: Path
) -> None:
    root = tmp_path / "configs"
    shutil.copytree(config_root, root)
    path = root / "privacy.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace("mode: delete", "mode: tombstone"), encoding="utf-8"
    )
    storage = planted.storage
    before = len(pd.read_csv(io.BytesIO(storage.read_bytes(run_key(SCORE_RUN_ID, "scores.csv")))))
    outcome = erase(
        storage,
        SENTINEL,
        engine=sqlite_engine(planted.platform_db),
        principal=LOCAL_OPERATOR,
        salt=SALT,
        config_root=root,
    )
    assert outcome.mode == ErasureMode.TOMBSTONE.value
    assert outcome.rows_tombstoned > 0 and outcome.rows_deleted == 0
    after = pd.read_csv(io.BytesIO(storage.read_bytes(run_key(SCORE_RUN_ID, "scores.csv"))))
    assert len(after) == before
    assert "[erased]" in set(after["customer_id"])
    assert files_holding(planted.root, SENTINEL) == []


def test_a_model_file_holding_the_id_is_reported_not_silently_kept(
    planted: Planted, config_root: Path
) -> None:
    key = run_key(TRAIN_ON_UPLOAD, "model/leaky.bin")
    planted.storage.write_bytes(key, b"\x00\x01" + SENTINEL.encode() + b"\x02")
    outcome = erase(
        planted.storage,
        SENTINEL,
        engine=sqlite_engine(planted.platform_db),
        principal=LOCAL_OPERATOR,
        salt=SALT,
        config_root=config_root,
    )
    assert outcome.status == "completed_with_exceptions"
    assert outcome.unrewritable_keys == (key,)
    assert MODEL_ON_UPLOAD in outcome.models_flagged


def test_a_rewrite_that_leaves_the_id_fails_the_request_and_records_it(
    planted: Planted, config_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        erasure_module,
        "rewrite_bytes",
        lambda kind, data, *args, **kwargs: (data, scan_bytes(kind, data, args[0], args[1])),
    )
    engine = sqlite_engine(planted.platform_db)
    log = ListAuditLog()
    with pytest.raises(PrivacyError) as excinfo:
        erase(
            planted.storage,
            SENTINEL,
            engine=engine,
            principal=LOCAL_OPERATOR,
            salt=SALT,
            config_root=config_root,
            audit_log=log,
            request_id="er_test_failed",
        )
    assert excinfo.value.code == "ERASURE_INCOMPLETE"
    stored = erasure_request(engine, "er_test_failed")
    assert stored is not None and (stored.status, stored.error_code) == ("failed", "ERASURE_INCOMPLETE")
    assert log.events[0].outcome == "failed"
    # the models are flagged before any file is rewritten (DEC-882): their training data held the
    # person whether or not the rewrite then succeeded, and a retry may no longer find them
    assert models_flagged_for_retraining(engine) == tuple(sorted((MODEL_ON_UPLOAD, MODEL_ON_DATASET)))
    assert stored.models_flagged == (MODEL_ON_UPLOAD, MODEL_ON_DATASET)


def test_an_empty_id_is_refused(planted: Planted, config_root: Path) -> None:
    with pytest.raises(PrivacyError) as excinfo:
        erase(
            planted.storage,
            "  ",
            engine=sqlite_engine(planted.platform_db),
            principal=LOCAL_OPERATOR,
            salt=SALT,
            config_root=config_root,
        )
    assert excinfo.value.code == "PRINCIPAL_ID_INVALID"


# ---------------------------------------------------------------------------
# The matching rules, one file at a time
# ---------------------------------------------------------------------------
def test_a_longer_id_that_starts_with_the_same_text_is_untouched() -> None:
    matcher = Matcher.for_id("C-104")
    data = b"customer_id,note\nC-104,called about C-104\nC-1042,referred by C-104\n"
    new, hits = rewrite_bytes(
        FileKind.CSV, data, matcher, ("customer_id",), mode=ErasureMode.DELETE, tombstone="[erased]"
    )
    assert new == b"customer_id,note\nC-1042,referred by [erased]\n"
    assert (hits.rows, hits.occurrences) == (1, 1)


def test_json_numbers_are_never_matched() -> None:
    matcher = Matcher.for_id("42")
    data = json.dumps(
        {"rows_scored": 42, "sample_rows": [{"primary_key": "42"}, {"primary_key": "7"}]}
    ).encode()
    new, hits = rewrite_bytes(
        FileKind.JSON, data, matcher, None, mode=ErasureMode.DELETE, tombstone="[erased]"
    )
    assert json.loads(new) == {"rows_scored": 42, "sample_rows": [{"primary_key": "7"}]}
    assert hits.rows == 1


def test_a_non_key_cell_is_masked_without_deleting_its_row() -> None:
    matcher = Matcher.for_id("C-9")
    data = b"customer_id;referred_by\r\nC-1;C-9\r\nC-9;\r\n"
    new, hits = rewrite_bytes(
        FileKind.CSV, data, matcher, ("customer_id",), mode=ErasureMode.DELETE, tombstone="[erased]"
    )
    assert new == b"customer_id;referred_by\r\nC-1;[erased]\r\n", "the delimiter and line ending are kept"
    assert (hits.rows, hits.cells) == (1, 1)
