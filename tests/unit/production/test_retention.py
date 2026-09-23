"""The retention job (DEC-736): what is due, what is kept, and a dry run that equals the real run."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from engine.audit.events import AuditEvent, AuditQuery
from engine.config import RunMode
from engine.contracts import DatasetProfile, RunState
from engine.privacy.contracts import RetentionAction, RetentionCategory
from engine.privacy.retention import RETENTION_JOB, apply_retention, plan_retention, strip_fields
from engine.storage import LocalStorage, run_key
from scripts import run_retention
from tests.unit.production.privacy_support import (
    customers,
    write_copy_messages,
    write_dataset,
    write_row_explanations,
    write_run,
    write_scores,
    write_source,
    write_upload,
)

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
OLD = NOW - timedelta(days=200)
RECENT = NOW - timedelta(days=10)


class ListAuditLog:
    """The `AuditLog` protocol over a list: append and read, nothing else."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def append(self, event: AuditEvent) -> None:
        self.events.append(event)

    def query(self, query: AuditQuery) -> tuple[AuditEvent, ...]:
        del query
        return tuple(self.events)

    def count(self, query: AuditQuery) -> int:
        del query
        return len(self.events)


def build_store(root: Path) -> LocalStorage:
    """A store with something due, something recent and something in use in every category."""
    storage = LocalStorage(root)
    keys = ["C-1", "C-2", "C-3"]
    # uploads
    write_upload(storage, "u_old", customers(keys), created_at=OLD)
    write_upload(storage, "u_new", customers(keys), created_at=RECENT)
    write_upload(storage, "u_inuse", customers(keys), created_at=OLD)
    write_upload(storage, "u_override", customers(keys), created_at=NOW - timedelta(days=50))
    # runs
    write_run(
        storage, "r_old_train", mode=RunMode.TRAIN, created_at=OLD, upload_id="u_old", model_version_id="m_1"
    )
    storage.write_bytes(run_key("r_old_train", "model/predictor.pkl"), b"\x80\x04model-bytes")
    storage.write_text(run_key("r_old_train", "run_manifest.json"), "{}\n")
    write_row_explanations(storage, "r_old_train", keys)
    storage.write_model(run_key("r_old_train", "profile.json"), _profile(storage, "u_old"))

    write_run(storage, "r_old_score", mode=RunMode.SCORE, created_at=OLD, upload_id="u_old")
    write_scores(storage, "r_old_score", keys)
    write_row_explanations(storage, "r_old_score", keys)
    write_copy_messages(storage, "r_old_score", keys)
    storage.write_text(
        run_key("r_old_score", "scoring_summary.json"),
        json.dumps({"rows_scored": 3, "sample_rows": [{"primary_key": "C-1"}]}, indent=2) + "\n",
    )
    storage.write_text(run_key("r_old_score", "drift.json"), json.dumps({"max_psi": 0.1}) + "\n")

    write_run(storage, "r_new", mode=RunMode.SCORE, created_at=RECENT, upload_id="u_new")
    write_scores(storage, "r_new", keys)
    write_run(
        storage, "r_running", mode=RunMode.SCORE, created_at=OLD, state=RunState.RUNNING, upload_id="u_inuse"
    )
    write_scores(storage, "r_running", keys)
    write_run(
        storage,
        "r_override",
        mode=RunMode.SCORE,
        created_at=NOW - timedelta(days=50),
        upload_id="u_override",
        retention_days=30,
    )
    write_scores(storage, "r_override", keys)
    # onboarding
    write_source(storage, "cl_1", "s_old", customers(keys), created_at=OLD)
    write_source(storage, "cl_1", "s_new", customers(keys), created_at=RECENT)
    write_dataset(storage, "ds_old", customers(keys), built_at=OLD, client_id="cl_1", source_ids=("s_old",))
    write_dataset(
        storage, "ds_new", customers(keys), built_at=RECENT, client_id="cl_1", source_ids=("s_new",)
    )
    return storage


def _profile(storage: LocalStorage, upload_id: str) -> DatasetProfile:
    return storage.read_model(f"uploads/{upload_id}/profile.json", DatasetProfile)


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return build_store(tmp_path / "data")


def test_the_plan_lists_exactly_what_has_outlived_its_setting(
    storage: LocalStorage, config_root: Path
) -> None:
    plan = plan_retention(storage, config_root, NOW)
    deleted = set(plan.planned_keys(RetentionAction.DELETE))
    stripped = set(plan.planned_keys(RetentionAction.STRIP_SAMPLES))
    assert {key for key in deleted if key.startswith("uploads/")} == set(storage.list_keys("uploads/u_old/"))
    assert {key for key in deleted if key.startswith("datasets/")} == set(
        storage.list_keys("datasets/ds_old/")
    )
    assert {key for key in deleted if key.startswith("clients/")} == set(
        storage.list_keys("clients/cl_1/sources/s_old/")
    )
    assert {key for key in deleted if key.startswith("runs/")} == {
        "runs/r_old_train/row_explanations.parquet",
        "runs/r_old_score/scores.csv",
        "runs/r_old_score/scores.parquet",
        "runs/r_old_score/row_explanations.parquet",
        "runs/r_old_score/copy_messages.csv",
        "runs/r_override/scores.csv",  # its own run_config said 30 days
        "runs/r_override/scores.parquet",
    }
    assert stripped == {"runs/r_old_train/profile.json", "runs/r_old_score/scoring_summary.json"}
    skipped = {(skip.owner_id, skip.reason_code) for skip in plan.skipped}
    assert ("u_inuse", "IN_USE") in skipped


def test_models_manifests_and_aggregate_reports_are_never_planned(
    storage: LocalStorage, config_root: Path
) -> None:
    planned = set(plan_retention(storage, config_root, NOW).planned_keys(RetentionAction.DELETE))
    for kept in (
        "runs/r_old_train/model/predictor.pkl",
        "runs/r_old_train/run.json",
        "runs/r_old_train/run_manifest.json",
        "runs/r_old_score/run.json",
        "runs/r_old_score/drift.json",
        "runs/r_old_score/scoring_summary.json",
    ):
        assert kept not in planned, kept


def test_an_input_is_kept_for_the_longest_promise_any_of_its_runs_made(
    storage: LocalStorage, config_root: Path
) -> None:
    """u_override's run asked for 30 days, its use case for 90: the upload is kept for 90."""
    plan = plan_retention(storage, config_root, NOW)
    assert not [key for key in plan.planned_keys() if key.startswith("uploads/u_override/")]
    assert all(item.retention_days == 90 for item in plan.items if item.owner_id == "u_old")


def test_a_dry_run_equals_the_real_run(storage: LocalStorage, config_root: Path, tmp_path: Path) -> None:
    dry = plan_retention(storage, config_root, NOW)
    before = {key: storage.read_bytes(key) for key in storage.list_keys()}
    plan = plan_retention(storage, config_root, NOW)
    assert plan.items == dry.items, "planning twice must give the same plan"
    log = ListAuditLog()
    result = apply_retention(plan, storage, log, RETENTION_JOB, now=NOW)
    assert set(result.deleted) == set(dry.planned_keys(RetentionAction.DELETE))
    assert set(result.stripped) == set(dry.planned_keys(RetentionAction.STRIP_SAMPLES))
    assert result.already_gone == ()
    after = set(storage.list_keys())
    assert after == set(before) - set(result.deleted)
    for key in after - set(result.stripped):
        assert storage.read_bytes(key) == before[key], f"{key} changed but was not in the plan"
    # the same pass again finds nothing left to do
    assert plan_retention(storage, config_root, NOW).items == ()
    # one audit event, with counts and no key
    (event,) = log.events
    assert event.action == "privacy.retention.apply"
    assert event.actor_id == RETENTION_JOB.user_id
    assert event.details["deleted"] == len(result.deleted)
    assert "C-1" not in event.model_dump_json()


def test_a_kept_report_loses_its_samples_and_still_validates(
    storage: LocalStorage, config_root: Path
) -> None:
    apply_retention(plan_retention(storage, config_root, NOW), storage, None, RETENTION_JOB, now=NOW)
    profile = storage.read_model(run_key("r_old_train", "profile.json"), DatasetProfile)
    assert profile.preview_rows == ()
    assert all(column.sample_values == () for column in profile.columns)
    summary = json.loads(storage.read_text(run_key("r_old_score", "scoring_summary.json")))
    assert summary == {"rows_scored": 3, "sample_rows": []}


def test_items_are_dated_from_their_records_not_the_files(storage: LocalStorage, config_root: Path) -> None:
    """Every file was written seconds ago; only the records say they are old."""
    plan = plan_retention(storage, config_root, NOW)
    assert {item.created_at for item in plan.items} == {OLD, NOW - timedelta(days=50)}


def test_an_undated_owner_is_skipped_not_deleted(storage: LocalStorage, config_root: Path) -> None:
    storage.write_text("uploads/u_old/upload.json", "{}")
    plan = plan_retention(storage, config_root, NOW)
    assert not [key for key in plan.planned_keys() if key.startswith("uploads/u_old/")]
    assert ("u_old", "UNDATED") in {(skip.owner_id, skip.reason_code) for skip in plan.skipped}


def test_strip_fields_empties_lists_and_nulls_scalars() -> None:
    document = {"a": [{"b": [1, 2], "c": "x"}], "d": "keep"}
    assert strip_fields(document, ["a.*.b", "a.*.c", "missing.path"]) == {
        "a": [{"b": [], "c": None}],
        "d": "keep",
    }


# ---------------------------------------------------------------------------
# retention_days = 0: "delete uploads right after the run"
# ---------------------------------------------------------------------------
@pytest.fixture
def zero_root(config_root: Path, tmp_path: Path) -> Path:
    root = tmp_path / "configs"
    shutil.copytree(config_root, root)
    path = root / "use_cases" / "targeted_advertisement.yaml"
    path.write_text(
        path.read_text(encoding="utf-8") + "\ngovernance:\n  retention_days: 0\n", encoding="utf-8"
    )
    return root


def test_zero_deletes_an_input_once_a_finished_run_has_read_it(zero_root: Path, tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path / "zero")
    just_now = NOW - timedelta(minutes=5)
    write_upload(storage, "u_read", customers(["C-1"]), created_at=just_now)
    write_upload(storage, "u_unread", customers(["C-1"]), created_at=just_now)
    write_run(storage, "r_done", mode=RunMode.SCORE, created_at=just_now, upload_id="u_read")
    write_scores(storage, "r_done", ["C-1"])
    plan = plan_retention(storage, zero_root, NOW)
    owners = {item.owner_id for item in plan.items}
    assert "u_read" in owners
    assert "r_done" in owners
    assert "u_unread" not in owners, "an upload nobody has run yet is still on someone's Setup screen"


def test_the_client_store_row_goes_with_the_raw_source(storage: LocalStorage, config_root: Path) -> None:
    class Store:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        def get_source(self, source_id: str) -> object:
            from engine.clients import ClientStoreError

            raise ClientStoreError("NOT_FOUND", "no row", entity="source", entity_id=source_id)

        def delete_source(self, source_id: str) -> None:
            self.deleted.append(source_id)

    store = Store()
    plan = plan_retention(storage, config_root, NOW, client_store=store)  # type: ignore[arg-type]
    result = apply_retention(plan, storage, None, RETENTION_JOB, client_store=store, now=NOW)  # type: ignore[arg-type]
    assert store.deleted == ["s_old"]
    assert result.registry_rows_deleted == 1


# ---------------------------------------------------------------------------
# The CLI: a dry run by default
# ---------------------------------------------------------------------------
def test_the_cli_is_a_dry_run_unless_told_otherwise(
    storage: LocalStorage, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MARKETING_AI_DATA_DIR", str(storage.root))
    # `main` configures the root logger for a real process; in this one it would leave a handler
    # behind for `tests/unit/test_logging_audit.py` to find.
    monkeypatch.setattr(run_retention, "configure_logging", lambda *args, **kwargs: None)
    before = set(storage.list_keys())
    assert run_retention.main(["--json", "--now", NOW.isoformat()]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert set(storage.list_keys()) == before, "a dry run deleted something"
    assert "runs/r_old_score/scores.csv" in printed["keys"]
    assert printed["counts"][RetentionCategory.UPLOAD.value] == len(storage.list_keys("uploads/u_old/"))

    log = ListAuditLog()
    assert run_retention.main(["--apply", "--json", "--now", NOW.isoformat()], audit_log=log) == 0
    applied = json.loads(capsys.readouterr().out)
    assert set(applied["result"]["deleted"]) | set(applied["result"]["stripped"]) == set(printed["keys"])
    assert not storage.exists("runs/r_old_score/scores.csv")
    assert len(log.events) == 1
