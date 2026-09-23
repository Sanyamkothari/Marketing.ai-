"""Retention review fixes (DEC-744): a run cannot extend retention, and 0 never means "forever"."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from engine.config import RunMode
from engine.privacy.retention import ZERO_DAYS_GRACE, plan_retention
from engine.storage import LocalStorage
from tests.unit.production.privacy_support import (
    customers,
    write_dataset,
    write_run,
    write_scores,
    write_upload,
)

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)


def test_a_run_override_cannot_keep_personal_data_past_the_use_cases_setting(
    tmp_path: Path, config_root: Path
) -> None:
    """The use case says 90 days; an Analyst's run asked for 730. After 100 days the upload, the
    dataset and the run's scores are all due."""
    storage = LocalStorage(tmp_path / "data")
    created = NOW - timedelta(days=100)
    write_upload(storage, "u_1", customers(["C-1"]), created_at=created)
    write_run(storage, "r_long", mode=RunMode.SCORE, created_at=created, upload_id="u_1", retention_days=730)
    write_scores(storage, "r_long", ["C-1"])
    write_dataset(storage, "ds_1", customers(["C-1"]), built_at=created, client_id="cl_1")
    write_run(storage, "r_ds", mode=RunMode.SCORE, created_at=created, dataset_id="ds_1", retention_days=730)

    plan = plan_retention(storage, config_root, NOW)
    owners = {item.owner_id for item in plan.items}
    assert {"u_1", "r_long", "ds_1"} <= owners
    assert all(item.retention_days == 90 for item in plan.items if item.owner_id in {"u_1", "r_long", "ds_1"})


def test_a_run_override_can_still_shorten_retention(tmp_path: Path, config_root: Path) -> None:
    storage = LocalStorage(tmp_path / "data")
    created = NOW - timedelta(days=40)
    write_upload(storage, "u_1", customers(["C-1"]), created_at=created)
    write_run(storage, "r_short", mode=RunMode.SCORE, created_at=created, upload_id="u_1", retention_days=30)
    write_scores(storage, "r_short", ["C-1"])
    plan = plan_retention(storage, config_root, NOW)
    assert "runs/r_short/scores.csv" in plan.planned_keys()


@pytest.fixture
def zero_root(config_root: Path, tmp_path: Path) -> Path:
    root = tmp_path / "configs"
    shutil.copytree(config_root, root)
    path = root / "use_cases" / "targeted_advertisement.yaml"
    path.write_text(
        path.read_text(encoding="utf-8") + "\ngovernance:\n  retention_days: 0\n", encoding="utf-8"
    )
    return root


def test_zero_deletes_an_input_nobody_ran_once_the_grace_has_passed(zero_root: Path, tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path / "zero")
    write_upload(storage, "u_abandoned", customers(["C-1"]), created_at=datetime(2025, 1, 1, tzinfo=UTC))
    write_upload(storage, "u_fresh", customers(["C-1"]), created_at=NOW - ZERO_DAYS_GRACE / 2)
    write_dataset(
        storage, "ds_abandoned", customers(["C-1"]), built_at=datetime(2025, 1, 1, tzinfo=UTC), client_id="c"
    )
    owners = {item.owner_id for item in plan_retention(storage, zero_root, NOW).items}
    assert {"u_abandoned", "ds_abandoned"} <= owners
    assert "u_fresh" not in owners, "an upload still on someone's Setup screen is kept"
