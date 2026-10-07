"""The holdout service in the score flow (Plan J M92).

Runs the score flow with `tests/unit/test_run_score.py`'s stage stubs - real actions and export,
fake ingest, predict and explain - as the consent-gate tests do. Under the default configuration the
service is not engaged at all (DEC-1302 (e)): no file, no setting, no database, and the stage table is
untouched; `assignment_frame` derives the table from the scores instead. With a persistent scope the
run writes `holdout_assignment.parquet` for every row and `holdout_assignment.json`, and refuses a
missing or changed salt at its first stage.
"""

from __future__ import annotations

import io
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

from engine import pipeline
from engine.config import ResolvedConfig, UseCaseConfig, resolve_config
from engine.contracts import RunRecord, RunState, StageKey
from engine.holdout.assign import (
    ASSIGNMENT_COLUMNS,
    HOLDOUT_ASSIGNMENT_FILENAME,
    assignment_frame,
    member_flags,
)
from engine.holdout.salt import HoldoutLedger
from engine.holdout.spec import (
    HOLDOUT_REPORT_FILENAME,
    HOLDOUT_SALT_CHANGED,
    HOLDOUT_SALT_MISSING,
    HoldoutAssignmentReport,
    HoldoutConfig,
)
from engine.platform_db import PLATFORM_DB_FILENAME, sqlite_engine
from engine.privacy.config import ErasureMode, load_privacy_config
from engine.privacy.layout import Store, StoreIndex, store_of
from engine.privacy.rewrite import FileKind, Matcher, rewrite_bytes
from engine.registry import LocalModelRegistry
from engine.storage import LocalStorage, run_key
from tests.unit.holdout.support import OTHER_SALT, SALT
from tests.unit.test_run_score import (
    PRIMARY_KEY,
    RUN_ID,
    SCHEMA_KEY,
    UPLOAD_KEY,
    USE_CASE,
    StageStubs,
    make_context,
    make_schema,
    pipeline_for,
    read_run,
    read_status,
    stage,
)

ROWS = 40
SUPPRESSED = 6  # the stub frame's consent column is false on every seventh row


@pytest.fixture
def resolved(config_root: Path) -> ResolvedConfig:
    return resolve_config(USE_CASE, root=config_root)


@pytest.fixture
def stubs(monkeypatch: pytest.MonkeyPatch) -> StageStubs:
    return StageStubs().install(monkeypatch)


@pytest.fixture(autouse=True)
def no_salt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MARKETING_AI_HOLDOUT_SALT", raising=False)


def persistent(
    resolved: ResolvedConfig, *, scope: str = "universal", fraction: float = 0.3, explore: float = 0.0
) -> UseCaseConfig:
    actions = resolved.config.actions.model_copy(
        update={"holdout": HoldoutConfig(scope=scope, fraction=fraction), "explore_fraction": explore}  # type: ignore[arg-type]
    )
    return resolved.config.model_copy(update={"actions": actions})


def store_at(root: Path) -> LocalStorage:
    store = LocalStorage(root)
    store.write_bytes(UPLOAD_KEY, b"customer_id,visits_last_7d\nC-00000,3\n")
    store.write_model(SCHEMA_KEY, make_schema())
    return store


def score(
    resolved: ResolvedConfig, root: Path, config: UseCaseConfig | None = None, run_id: str = RUN_ID
) -> LocalStorage:
    store = store_at(root)
    registry = LocalModelRegistry(root.parent / f"{root.name}-registry.db")
    context = replace(make_context(resolved, store, registry, config=config), run_id=run_id)
    pipeline_for(store, registry).run_score(context)
    return store


def read_table(store: LocalStorage, run_id: str = RUN_ID) -> pd.DataFrame:
    return pd.read_parquet(io.BytesIO(store.read_bytes(run_key(run_id, HOLDOUT_ASSIGNMENT_FILENAME))))


def read_scores(store: LocalStorage, run_id: str = RUN_ID) -> pd.DataFrame:
    return pd.read_parquet(io.BytesIO(store.read_bytes(run_key(run_id, "scores.parquet"))))


def test_the_default_configuration_does_not_engage_the_service(
    resolved: ResolvedConfig, stubs: StageStubs, tmp_path: Path
) -> None:
    store = score(resolved, tmp_path / "default")
    names = {key.rsplit("/", 1)[-1] for key in store.list_keys(f"runs/{RUN_ID}/")}
    assert HOLDOUT_ASSIGNMENT_FILENAME not in names and HOLDOUT_REPORT_FILENAME not in names
    assert not (tmp_path / "default" / PLATFORM_DB_FILENAME).exists(), "no database opened"
    assert HOLDOUT_ASSIGNMENT_FILENAME not in read_run(store).artefacts


def test_a_default_runs_table_is_derived_from_its_scores(
    resolved: ResolvedConfig, stubs: StageStubs, tmp_path: Path
) -> None:
    """DEC-1302 (e): the hand-off builder gets the default run's table without the run writing it."""
    store = score(resolved, tmp_path / "derived")
    scores = read_scores(store)
    table = assignment_frame(
        scores,
        resolved.config,
        primary_key=PRIMARY_KEY,
        row_key=PRIMARY_KEY,
        entity_key=None,
        run_id=RUN_ID,
        active=None,
        explore_fraction=0.0,
    ).table
    assert list(table.columns) == [PRIMARY_KEY, *ASSIGNMENT_COLUMNS]
    assert len(table.index) == ROWS, "suppressed rows included"
    assert table["holdout_member"].tolist() == scores["control_group"].tolist(), "today's control group"
    assert not table["explore"].any() and set(table["explore_probability"]) == {0.0}


def test_importing_the_pipeline_installs_the_service() -> None:
    """A dropped `install_holdout_service(_ScoreFlow)` would score persistent holdouts per run, silently."""
    from engine.uplift.flow import UpliftScoreFlow

    assert getattr(pipeline._ScoreFlow, "_holdout_service_installed", False) is True
    assert getattr(UpliftScoreFlow, "_holdout_service_installed", False) is True


def test_the_stage_table_is_untouched_by_default(resolved: ResolvedConfig, tmp_path: Path) -> None:
    store = store_at(tmp_path / "table")
    registry = LocalModelRegistry(tmp_path / "table-registry.db")
    flow = pipeline._ScoreFlow(pipeline_for(store, registry), make_context(resolved, store, registry))
    bodies = flow._bodies()
    assert [body.__name__ for _, body in bodies] == [
        "_ingest",
        "_validate_against_schema",
        "_prepare",
        "_predict",
        "_explain_rows",
        "_consent_gated_actions",
        "_export",
    ]


def test_a_universal_holdout_run_writes_every_row_and_its_spec(
    resolved: ResolvedConfig, stubs: StageStubs, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MARKETING_AI_HOLDOUT_SALT", SALT)
    store = score(resolved, tmp_path / "universal", persistent(resolved))
    record = read_run(store)
    assert record.state is RunState.DONE
    table = read_table(store)
    assert list(table.columns) == [PRIMARY_KEY, *ASSIGNMENT_COLUMNS]
    assert len(table.index) == ROWS, "suppressed rows included"
    expected = member_flags(table[PRIMARY_KEY].tolist(), salt=SALT, scope_key="universal", fraction=0.3)
    assert table["holdout_member"].tolist() == expected.tolist()
    scores = read_scores(store).set_index(PRIMARY_KEY).loc[table[PRIMARY_KEY]]
    eligible = scores["suppressed_reason"].isna().to_numpy()
    assert scores["control_group"].tolist() == (expected & eligible).tolist()
    assert int(scores["suppressed_reason"].notna().sum()) == SUPPRESSED
    report = store.read_model(run_key(RUN_ID, HOLDOUT_REPORT_FILENAME), HoldoutAssignmentReport)
    assert (report.spec.scope, report.spec.epoch, report.spec.fraction) == ("universal", 1, 0.3)
    assert report.rows == ROWS and report.holdout_members == int(expected.sum())
    assert report.control_rows == int((expected & eligible).sum())
    assert {HOLDOUT_ASSIGNMENT_FILENAME, HOLDOUT_REPORT_FILENAME} <= set(record.artefacts)
    assert SALT not in store.read_text(run_key(RUN_ID, HOLDOUT_REPORT_FILENAME))
    detail = stage(read_status(store), StageKey.ACTIONS).detail or ""
    assert "universal holdout, epoch 1" in detail


def test_two_runs_hold_out_the_same_customers(
    resolved: ResolvedConfig, stubs: StageStubs, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MARKETING_AI_HOLDOUT_SALT", SALT)
    config = persistent(resolved, scope="use_case")
    root = tmp_path / "monthly"
    first = score(resolved, root, config, run_id="r_20260901_00000001")
    second = score(resolved, root, config, run_id="r_20261001_00000002")
    left = read_table(first, "r_20260901_00000001")
    right = read_table(second, "r_20261001_00000002")
    assert left["holdout_member"].tolist() == right["holdout_member"].tolist()


def test_an_explore_slice_alone_writes_the_table_under_scope_run(
    resolved: ResolvedConfig, stubs: StageStubs, tmp_path: Path
) -> None:
    actions = resolved.config.actions.model_copy(update={"explore_fraction": 0.10})
    store = score(resolved, tmp_path / "explore", resolved.config.model_copy(update={"actions": actions}))
    table = read_table(store)
    scores = read_scores(store)
    assert table["holdout_member"].tolist() == scores["control_group"].tolist()
    assert set(table["explore_probability"]) <= {0.0, 0.10}
    assert not (tmp_path / "explore" / PLATFORM_DB_FILENAME).exists(), "scope run needs no ledger"


def test_a_missing_salt_fails_the_run_at_its_first_stage(
    resolved: ResolvedConfig, stubs: StageStubs, tmp_path: Path
) -> None:
    with pytest.raises(Exception) as caught:
        score(resolved, tmp_path / "nosalt", persistent(resolved))
    assert getattr(caught.value, "code", None) == HOLDOUT_SALT_MISSING
    store = LocalStorage(tmp_path / "nosalt")
    failed = stage(read_status(store), StageKey.INGEST)
    assert failed.state is RunState.FAILED
    assert failed.error is not None and failed.error.code == HOLDOUT_SALT_MISSING
    assert StageKey.PREDICT not in stubs.calls, "nothing is scored"


def test_a_changed_salt_refuses_scoring(
    resolved: ResolvedConfig, stubs: StageStubs, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "rotated"
    monkeypatch.setenv("MARKETING_AI_HOLDOUT_SALT", SALT)
    score(resolved, root, persistent(resolved), run_id="r_20260901_00000001")
    monkeypatch.setenv("MARKETING_AI_HOLDOUT_SALT", OTHER_SALT)
    with pytest.raises(Exception) as caught:
        score(resolved, root, persistent(resolved), run_id="r_20261001_00000002")
    assert getattr(caught.value, "code", None) == HOLDOUT_SALT_CHANGED
    first = LocalStorage(root).read_model(run_key("r_20260901_00000001", "run.json"), RunRecord)
    assert first.state is RunState.DONE, "the first run is untouched"


def test_the_file_is_a_registered_row_level_artefact_erasure_can_rewrite(
    resolved: ResolvedConfig,
    stubs: StageStubs,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config_root: Path,
) -> None:
    monkeypatch.setenv("MARKETING_AI_HOLDOUT_SALT", SALT)
    store = score(resolved, tmp_path / "erasure", persistent(resolved))
    key = run_key(RUN_ID, HOLDOUT_ASSIGNMENT_FILENAME)
    assert HOLDOUT_ASSIGNMENT_FILENAME in load_privacy_config(config_root).retention.row_level_run_artefacts
    assert store_of(key) is Store.SCORES
    assert StoreIndex(store).key_columns(key) == (PRIMARY_KEY,)
    victim = read_table(store)[PRIMARY_KEY].iloc[3]
    rewritten, hits = rewrite_bytes(
        FileKind.PARQUET,
        store.read_bytes(key),
        Matcher.for_id(str(victim)),
        (PRIMARY_KEY,),
        mode=ErasureMode.DELETE,
        tombstone="[erased]",
    )
    left = pd.read_parquet(io.BytesIO(rewritten))
    assert hits.rows == 1 and victim not in set(left[PRIMARY_KEY])
    assert list(left.columns) == [PRIMARY_KEY, *ASSIGNMENT_COLUMNS]


def test_a_run_that_fails_in_its_actions_stage_records_nothing(
    resolved: ResolvedConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MARKETING_AI_HOLDOUT_SALT", SALT)
    StageStubs(fail={StageKey.ACTIONS: RuntimeError("boom")}).install(monkeypatch)
    root = tmp_path / "failed"
    with pytest.raises(RuntimeError):
        score(resolved, root, persistent(resolved))
    ledger = HoldoutLedger(sqlite_engine(root / PLATFORM_DB_FILENAME))
    assert (
        ledger.fingerprint() is None and ledger.entries() == ()
    ), "no first use is recorded for a failed run"
