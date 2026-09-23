"""The consent ledger in the score flow (DEC-732): gated when a ledger exists, Phase 1 byte for byte when not.

The score flow runs with `tests/unit/test_run_score.py`'s stage stubs - real actions and export, fake
ingest, predict and explain - so every number asserted here was produced by the shipped actions and
export stages from the ledger's verdict.
"""

from __future__ import annotations

import io
from datetime import timedelta
from pathlib import Path

import pandas as pd
import pytest

from engine import pipeline
from engine.config import ResolvedConfig, resolve_config
from engine.contracts import SCORE_ARTEFACTS, RunRecord, ScoringSummary
from engine.platform_db import PLATFORM_DB_FILENAME, sqlite_engine
from engine.privacy.consent import LEDGER_CONSENT_COLUMN, ConsentLedger
from engine.privacy.contracts import CONSENT_REPORT_FILENAME, ConsentReport, ConsentStatus
from engine.registry import LocalModelRegistry
from engine.storage import LocalStorage, run_key
from engine.utils.time import utc_now
from tests.unit.test_run_score import (
    PRIMARY_KEY,
    RUN_ID,
    SCHEMA_KEY,
    UPLOAD_KEY,
    USE_CASE,
    StageStubs,
    make_schema,
    run_flow,
)

CLIENT = "acme"
PURPOSE = "marketing_communication"
ROWS = 40


@pytest.fixture
def resolved(config_root: Path) -> ResolvedConfig:
    return resolve_config(USE_CASE, root=config_root)


@pytest.fixture
def stubs(monkeypatch: pytest.MonkeyPatch) -> StageStubs:
    return StageStubs().install(monkeypatch)


@pytest.fixture(autouse=True)
def deployment_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """The run names no client (an upload run), so the deployment's client id is the ledger's."""
    monkeypatch.setenv("MARKETING_AI_CLIENT_ID", CLIENT)


def make_store(root: Path) -> LocalStorage:
    store = LocalStorage(root)
    store.write_bytes(UPLOAD_KEY, b"customer_id,visits_last_7d\nC-00000,3\n")
    store.write_model(SCHEMA_KEY, make_schema())
    return store


def score(resolved: ResolvedConfig, root: Path) -> LocalStorage:
    store = make_store(root)
    run_flow(resolved, store, LocalModelRegistry(root.parent / f"{root.name}-registry.db"))
    return store


def run_files(store: LocalStorage) -> dict[str, bytes]:
    return {key: store.read_bytes(key) for key in store.list_keys(f"runs/{RUN_ID}/")}


def summary(store: LocalStorage) -> ScoringSummary:
    return store.read_model(run_key(RUN_ID, "scoring_summary.json"), ScoringSummary)


def scores(store: LocalStorage) -> pd.DataFrame:
    return pd.read_csv(io.BytesIO(store.read_bytes(run_key(RUN_ID, "scores.csv"))))


def ledger_at(root: Path) -> ConsentLedger:
    return ConsentLedger(sqlite_engine(root / PLATFORM_DB_FILENAME), salt=CLIENT)


def record(ledger: ConsentLedger, principal: str, status: str, **kwargs: object) -> None:
    ledger.record(
        client_id=str(kwargs.get("client_id", CLIENT)),
        principal_id=principal,
        purpose=str(kwargs.get("purpose", PURPOSE)),
        status=ConsentStatus(status),
        source="test",
        recorded_at=utc_now() - timedelta(days=10),
        expires_at=kwargs.get("expires_at"),  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# No ledger: Phase 1, byte for byte
# ---------------------------------------------------------------------------
def comparable(files: dict[str, bytes]) -> dict[str, bytes]:
    """The run files whose bytes carry no clock: scores, explanations. The JSON ones are compared parsed."""
    return {key: data for key, data in files.items() if key.endswith((".csv", ".parquet"))}


def test_without_a_ledger_the_run_is_phase_1_byte_for_byte(
    resolved: ResolvedConfig, stubs: StageStubs, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gated = score(resolved, tmp_path / "gated")
    with monkeypatch.context() as patch:
        patch.setattr(pipeline._ScoreFlow, "_actions", pipeline._PHASE1_SCORE_ACTIONS)
        phase1 = score(resolved, tmp_path / "phase1")
    assert sorted(run_files(gated)) == sorted(run_files(phase1))
    assert comparable(run_files(gated)) == comparable(run_files(phase1))
    ignore = {"scored_at"}
    left = summary(gated).model_dump(exclude=ignore)
    right = summary(phase1).model_dump(exclude=ignore)
    assert left == right
    assert not (tmp_path / "gated" / PLATFORM_DB_FILENAME).exists(), "looking for a ledger created a database"
    names = {key.rsplit("/", 1)[-1] for key in run_files(gated)}
    assert CONSENT_REPORT_FILENAME not in names
    assert names <= SCORE_ARTEFACTS


def test_a_ledger_for_another_purpose_changes_nothing(
    resolved: ResolvedConfig, stubs: StageStubs, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ledger for *another client* no longer changes nothing: see the fail-closed test (DEC-738)."""
    root = tmp_path / "other"
    ledger = ledger_at(root)
    record(ledger, "C-00001", "granted", purpose="account_servicing")
    gated = score(resolved, root)
    with monkeypatch.context() as patch:
        patch.setattr(pipeline._ScoreFlow, "_actions", pipeline._PHASE1_SCORE_ACTIONS)
        phase1 = score(resolved, tmp_path / "phase1")
    assert comparable(run_files(gated)) == comparable(run_files(phase1))
    assert not gated.exists(run_key(RUN_ID, CONSENT_REPORT_FILENAME))


def test_a_use_case_with_no_purpose_is_never_gated(
    config_root: Path, stubs: StageStubs, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The use case is mapped to no purpose in privacy.yaml, so even a full ledger is not consulted."""
    root = tmp_path / "unmapped"
    ledger = ledger_at(root)
    record(ledger, "C-00001", "granted")
    from engine.config import config_root as resolve_root
    from engine.privacy import config as privacy_config

    privacy = privacy_config.load_privacy_config()
    unmapped = privacy.model_copy(
        update={"use_case_purposes": {k: v for k, v in privacy.use_case_purposes.items() if k != USE_CASE}}
    )
    monkeypatch.setitem(privacy_config._CACHE, resolve_root(None), unmapped)
    gated = score(resolve_config(USE_CASE, root=config_root), root)
    assert not gated.exists(run_key(RUN_ID, CONSENT_REPORT_FILENAME))


# ---------------------------------------------------------------------------
# A ledger: principals without valid consent are suppressed and counted
# ---------------------------------------------------------------------------
def plant_ledger(root: Path) -> None:
    ledger = ledger_at(root)
    for index in range(ROWS):
        key = f"C-{index:05d}"
        if index % 4 == 0:
            record(ledger, key, "granted")
        elif index % 4 == 1:
            record(ledger, key, "granted")
            record(ledger, key, "withdrawn")
        elif index % 4 == 2:
            record(ledger, key, "granted", expires_at=utc_now() - timedelta(days=1))
        # index % 4 == 3: no record at all


def test_principals_without_valid_consent_are_suppressed_and_counted(
    resolved: ResolvedConfig, stubs: StageStubs, tmp_path: Path
) -> None:
    root = tmp_path / "ledger"
    plant_ledger(root)
    store = score(resolved, root)
    report = store.read_model(run_key(RUN_ID, CONSENT_REPORT_FILENAME), ConsentReport)
    assert report.purpose == PURPOSE
    assert report.client_id == CLIENT
    assert report.consent_column == LEDGER_CONSENT_COLUMN
    assert report.replaced_file_column is False
    assert report.principals_checked == ROWS
    assert report.principals_with_valid_consent == 10
    assert (report.excluded_withdrawn, report.excluded_expired, report.excluded_no_consent) == (10, 10, 10)
    assert report.excluded_total == 30

    counts = {item.reason: item.rows for item in summary(store).suppressed}
    assert counts["consent_false"] == 30, "the Phase 1 rule and its counts report the ledger's exclusions"
    frame = scores(store)
    consented = {f"C-{index:05d}" for index in range(0, ROWS, 4)}
    suppressed = frame.loc[frame["suppressed_reason"] == "consent_false", PRIMARY_KEY]
    assert set(suppressed) == {f"C-{index:05d}" for index in range(ROWS)} - consented
    assert LEDGER_CONSENT_COLUMN not in frame.columns, "the synthetic column is never exported"
    record_ = store.read_model(run_key(RUN_ID, "run.json"), RunRecord)
    assert CONSENT_REPORT_FILENAME in record_.artefacts


def test_a_configured_consent_column_becomes_a_ledger_lookup(
    config_root: Path, stubs: StageStubs, tmp_path: Path
) -> None:
    """`marketing_opt_in` is true on six of seven rows in the file; the ledger's verdict replaces it,
    ANDed with the file's own value (DEC-738): rows 0 and 28 hold a ledger grant but are false in the
    file, so they join the 30 without valid consent."""
    resolved = resolve_config(USE_CASE, {"governance.consent_column": "marketing_opt_in"}, root=config_root)
    root = tmp_path / "column"
    plant_ledger(root)
    store = score(resolved, root)
    report = store.read_model(run_key(RUN_ID, CONSENT_REPORT_FILENAME), ConsentReport)
    assert report.consent_column == "marketing_opt_in"
    assert report.replaced_file_column is True
    counts = {item.reason: item.rows for item in summary(store).suppressed}
    assert counts["consent_false"] == 32
    assert report.excluded_file_opt_out == 2
    assert report.excluded_total == 32
    assert "opted_out" in counts


def test_the_gate_reads_the_deployment_settings_the_api_reads(
    stubs: StageStubs, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DEC-796: a deployment keeps `client_id` in Parameter Store, not in the environment.

    The seam asks `load_settings()` - the API's loader, which honours `MARKETING_AI_SETTINGS_SOURCE`
    - so a client id that only the parameter source knows still finds the ledger, and the run is
    gated with the same salt the API hashed the ledger with.
    """
    from engine.privacy import consent
    from engine.settings import Settings

    monkeypatch.delenv("MARKETING_AI_CLIENT_ID")
    root = tmp_path / "from-parameters"
    plant_ledger(root)
    store = make_store(root)
    assert consent.consent_gate_for_run(store, use_case_id=USE_CASE, client_id=None) is None
    monkeypatch.setattr(consent, "load_settings", lambda: Settings(client_id=CLIENT))
    gate = consent.consent_gate_for_run(store, use_case_id=USE_CASE, client_id=None)
    assert gate is not None
    assert (gate.client_id, gate.purpose) == (CLIENT, PURPOSE)


# ---------------------------------------------------------------------------
# Review fixes (DEC-738, DEC-739)
# ---------------------------------------------------------------------------
def test_a_client_with_no_ledger_is_gated_closed_when_the_purpose_has_one(
    resolved: ResolvedConfig, stubs: StageStubs, tmp_path: Path
) -> None:
    """An Analyst cannot switch consent off by onboarding the same customers under a new client."""
    root = tmp_path / "fresh-client"
    ledger = ledger_at(root)
    record(ledger, "C-00001", "granted", client_id="someone-else")
    store = score(resolved, root)
    report = store.read_model(run_key(RUN_ID, CONSENT_REPORT_FILENAME), ConsentReport)
    assert (report.client_id, report.principals_with_valid_consent) == (CLIENT, 0)
    assert report.excluded_total == ROWS
    counts = {item.reason: item.rows for item in summary(store).suppressed}
    assert counts["consent_false"] == ROWS


def test_a_ledger_under_the_deployment_client_gates_an_onboarding_clients_run(tmp_path: Path) -> None:
    """Consent recorded with the client field left empty lands under `Settings.client_id`; a dataset
    or scheduled run names its onboarding client `c_<slug>_<n>` and must still be gated."""
    from engine.privacy import consent
    from engine.settings import Settings

    root = tmp_path / "deployment-client"
    ledger = ledger_at(root)
    record(ledger, "C-00001", "withdrawn", client_id="acme")
    settings = Settings(client_id="acme", data_dir=root)
    gate = consent.consent_gate_for_run(
        make_store(root), use_case_id=USE_CASE, client_id="c_acme_1", config=settings
    )
    assert gate is not None
    assert (gate.client_id, gate.purpose) == ("acme", PURPOSE)


def test_the_gate_finds_a_postgres_ledger_beside_a_local_artefact_volume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """docker-compose: artefacts on a local volume, metadata in Postgres. The API writes the ledger
    to Postgres, so the job must read it there, not look for a `platform.db` in the volume."""
    from engine.privacy import consent
    from engine.settings import Settings

    root = tmp_path / "compose"
    sentinel = ledger_at(tmp_path / "stands-for-postgres").engine
    seen: list[Settings] = []

    def fake_platform_engine(settings: Settings, *, data_dir: Path | None = None) -> object:
        seen.append(settings)
        return sentinel

    monkeypatch.setattr(consent, "platform_engine", fake_platform_engine)
    settings = Settings(
        data_dir=root, metadata_backend="postgres", postgres_dsn="postgresql+psycopg://u:p@localhost:5/db"
    )
    assert consent.ledger_engine_for(make_store(root), settings) is sentinel
    assert seen == [settings]
    assert not (root / PLATFORM_DB_FILENAME).exists()
