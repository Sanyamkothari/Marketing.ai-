"""A campaign's row-level files are covered by erasure and retention (Plan J M94, DEC-1304 (j)).

`campaigns/<id>/assignment.parquet` and `outcomes.parquet` hold one row per customer. Erasure reads
the whole store (DEC-741), so it finds them anyway; what M94 adds is that they are matched on the
campaign's own key column (from `campaign.json`, `engine.privacy.layout.StoreIndex`) rather than on
any cell, are counted under their own store, and stay readable - the campaign can still be measured -
after the principal's rows are gone. Retention deletes them once the campaign has outlived its use
case's `governance.retention_days`, counted from the end of its outcome window, and keeps the record
and the aggregate report.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from engine.access.roles import LOCAL_OPERATOR
from engine.measurement.campaign import (
    ASSIGNMENT_FILENAME,
    CAMPAIGN_FILENAME,
    INTENDED_COLUMN,
    OUTCOMES_FILENAME,
    ROW_LEVEL_CAMPAIGN_FILES,
    Campaign,
    CampaignKind,
    CampaignStatus,
    InMemoryCampaignStore,
    assignment_counts,
    build_assignment,
    campaign_key,
    read_frame,
    save_campaign,
    write_frame,
)
from engine.measurement.measure import measure_campaign
from engine.platform_db import sqlite_engine
from engine.privacy.config import load_privacy_config
from engine.privacy.contracts import RetentionCategory
from engine.privacy.erasure import erase, find_principal
from engine.privacy.layout import Store, StoreIndex
from engine.privacy.retention import plan_retention, retention_days_by_use_case
from engine.storage import LocalStorage

SENTINEL = "ZQX-CAMPAIGN-4410"
SALT = "campaign-privacy-salt-0001"
CAMPAIGN_ID = "c_20261007_aaaa0001"
USE_CASE = "win-back-campaign"
SENT = datetime(2026, 5, 1, tzinfo=UTC)


def plant(root: Path) -> LocalStorage:
    storage = LocalStorage(root)
    keys = [f"C-{index:04d}" for index in range(40)] + [SENTINEL]
    scores = pd.DataFrame(
        {
            "customer_id": keys,
            "band": ["High"] * len(keys),
            "suppressed_reason": [None] * len(keys),
            "control_group": [index % 4 == 0 for index in range(len(keys))],
        }
    )
    assignment = build_assignment(scores, primary_key="customer_id")
    write_frame(storage, campaign_key(CAMPAIGN_ID, ASSIGNMENT_FILENAME), assignment)
    outcomes = pd.DataFrame(
        {"customer_id": keys, "reactivated_90d": [index % 3 == 0 for index in range(len(keys))]}
    )
    write_frame(storage, campaign_key(CAMPAIGN_ID, OUTCOMES_FILENAME), outcomes)
    campaign = Campaign(
        campaign_id=CAMPAIGN_ID,
        kind=CampaignKind.SCORED,
        name="Win-back",
        use_case_id=USE_CASE,
        run_ids=("r_20261007_00000001",),
        primary_key="customer_id",
        treatment_start=SENT,
        treatment_start_source="run_finished",
        outcome_window_days=90,
        population="eligible",
        causal=True,
        causal_basis="engine_random",
        counts=assignment_counts(assignment),
        status=CampaignStatus.LIVE,
        created_at=SENT,
        created_by="u_1",
    )
    save_campaign(InMemoryCampaignStore(), storage, campaign, create=True)
    return storage


def test_the_row_level_campaign_files_are_the_ones_privacy_yaml_lists() -> None:
    assert load_privacy_config().retention.row_level_campaign_artefacts == ROW_LEVEL_CAMPAIGN_FILES


def test_erasure_removes_the_principal_from_campaigns(tmp_path: Path, config_root: Path) -> None:
    storage = plant(tmp_path / "data")
    index = StoreIndex(storage)
    assert index.key_columns(campaign_key(CAMPAIGN_ID, ASSIGNMENT_FILENAME)) == ("customer_id",)
    info = index.campaigns[CAMPAIGN_ID]
    assert info.matures_at == SENT + timedelta(days=90) and info.run_ids == ("r_20261007_00000001",)

    findings = find_principal(storage, SENTINEL)
    found = {location.key: location for location in findings.locations}
    for name in (ASSIGNMENT_FILENAME, OUTCOMES_FILENAME):
        location = found[campaign_key(CAMPAIGN_ID, name)]
        assert (location.store, location.rows, location.key_columns) == (Store.CAMPAIGNS, 1, ("customer_id",))
    assert campaign_key(CAMPAIGN_ID, CAMPAIGN_FILENAME) not in found, "the record holds no customer id"

    engine = sqlite_engine(tmp_path / "data" / "platform.db")
    outcome = erase(
        storage,
        SENTINEL,
        engine=engine,
        principal=LOCAL_OPERATOR,
        salt=SALT,
        client_id="cl_1",
        config_root=config_root,
    )
    engine.dispose()
    assert outcome.status == "completed"
    assert find_principal(storage, SENTINEL).locations == ()
    for path in (tmp_path / "data" / "campaigns").rglob("*"):
        if path.is_file():
            assert SENTINEL.encode() not in path.read_bytes(), path
    # the files keep their schema and every other customer, so the campaign can still be measured
    assignment = read_frame(storage, campaign_key(CAMPAIGN_ID, ASSIGNMENT_FILENAME))
    outcomes = read_frame(storage, campaign_key(CAMPAIGN_ID, OUTCOMES_FILENAME))
    assert len(assignment.index) == 40 and len(outcomes.index) == 40
    report = measure_campaign(
        assignment,
        outcomes,
        run_id="r_20261007_00000001",
        primary_key="customer_id",
        outcome_column="reactivated_90d",
        intended_column=INTENDED_COLUMN,
        treatment_time=SENT,
        outcome_window_days=90,
        as_of=SENT + timedelta(days=120),
    )
    assert report.treated_rows + report.control_rows == 40


@pytest.mark.parametrize("extra_days", [-1, 1])
def test_retention_deletes_the_rows_after_the_window_and_the_retention_period(
    tmp_path: Path, config_root: Path, extra_days: int
) -> None:
    storage = plant(tmp_path / "data")
    by_use_case, default = retention_days_by_use_case(config_root)
    days = by_use_case.get(USE_CASE, default)
    moment = SENT + timedelta(days=90 + days + extra_days)
    plan = plan_retention(storage, config_root, now=moment)
    campaign_items = [item for item in plan.items if item.category is RetentionCategory.CAMPAIGN_ROW_LEVEL]
    if extra_days < 0:
        assert campaign_items == [], "kept until the window has closed and the retention period has passed"
        return
    assert sorted(item.key for item in campaign_items) == sorted(
        campaign_key(CAMPAIGN_ID, name) for name in ROW_LEVEL_CAMPAIGN_FILES
    )
    assert {item.owner_id for item in campaign_items} == {CAMPAIGN_ID}
    assert all(item.retention_days == days for item in campaign_items)
    kept = {item.key for item in plan.items}
    assert campaign_key(CAMPAIGN_ID, CAMPAIGN_FILENAME) not in kept


def test_a_privacy_file_without_the_campaign_list_still_loads(tmp_path: Path, config_root: Path) -> None:
    """The new list has a default: a deployment's own privacy.yaml from before M94 is still valid."""
    import shutil

    import yaml

    copy = tmp_path / "configs"
    shutil.copytree(config_root, copy)
    document = yaml.safe_load((copy / "privacy.yaml").read_text(encoding="utf-8"))
    del document["retention"]["row_level_campaign_artefacts"]
    (copy / "privacy.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    assert (
        load_privacy_config(copy).retention.row_level_campaign_artefacts == ()
    )  # cached per root: a fresh one
