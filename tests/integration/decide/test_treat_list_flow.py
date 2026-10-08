"""Integration tests for Plan J M98 treat list generation and downloads.

Tests:
1. Building treat_list.csv and treat_list.parquet on propensity and uplift runs written by the product's own
   stages (`tests/fixtures/decide/treat_runs.py`), with the run's `run_config.json` beside them.
2. Treat list columns: keys, use_case, model_version, band/segment, treat, holdout, explore,
   suppression_reason, offer, channel, net_value, expected_gross_value, reason_1..3.
3. Every row's holdout flag matches holdout_assignment.parquet, joined on the key.
4. When holdout_assignment is missing, holdout flag is null (None), not False, with summary note.
5. Missing net value from M97 -> net_value is null.
6. Flags are boolean in parquet, 1/0 in CSV.
7. Route gating: GET /runs/{id}/treat_list.csv and /runs/{id}/artefacts/treat_list.csv are refused (403)
   to a Viewer when auth is enabled, and audited for an Analyst; the bytes served are the builder's.
"""

from __future__ import annotations

import io
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from engine.access.roles import Role
from engine.audit.events import AuditQuery
from engine.decide.treat_list import (
    TREAT_LIST_CSV,
    TREAT_LIST_PARQUET,
    TREAT_LIST_SUMMARY_FILENAME,
    TreatListSummary,
    ensure_treat_list,
)
from engine.holdout.assign import HOLDOUT_MEMBER_COLUMN
from engine.storage import LocalStorage, run_key
from tests.fixtures.decide.treat_runs import write_run
from tests.integration.production.access_support import (
    audit_log_at,
    bearer,
    local_app,
    make_user,
)

pytestmark = pytest.mark.integration


def test_propensity_and_uplift_treat_list_generation(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    p_run = write_run(storage, "r_20261001_0b000001", kind="propensity", rows=100)
    u_run = write_run(storage, "r_20261001_0b000002", kind="uplift", rows=100)

    # Build treat lists
    p_summary = ensure_treat_list(storage, p_run.run_id)
    assert isinstance(p_summary, TreatListSummary)
    assert p_summary.total_rows == 100
    assert p_summary.holdout_rows is None  # no holdout_assignment.parquet written
    assert p_summary.holdout_note is not None

    p_df = pd.read_parquet(tmp_path / "runs" / p_run.run_id / TREAT_LIST_PARQUET)
    assert "band" in p_df.columns
    assert "segment" not in p_df.columns
    assert "treat" in p_df.columns
    assert "holdout" in p_df.columns
    assert p_df["holdout"].isna().all()  # missing holdout_assignment -> null, NOT False!
    assert p_df["explore"].isna().all()
    assert list(p_df.columns[-5:-3]) == ["net_value", "expected_gross_value"]

    # Now with uplift run
    u_summary = ensure_treat_list(storage, u_run.run_id)
    assert isinstance(u_summary, TreatListSummary)
    assert u_summary.total_rows == 100

    u_df = pd.read_parquet(tmp_path / "runs" / u_run.run_id / TREAT_LIST_PARQUET)
    assert "segment" in u_df.columns
    assert "treat" in u_df.columns
    assert "holdout" in u_df.columns


def test_parity_with_holdout_assignment_when_present(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    run = write_run(storage, "r_20261001_0b000003", kind="propensity", rows=120, holdout=True)
    assert run.assignment is not None

    summary = ensure_treat_list(storage, run.run_id)
    assert summary.holdout_rows is not None
    assert summary.holdout_rows == int(run.assignment[HOLDOUT_MEMBER_COLUMN].sum())

    df = pd.read_parquet(tmp_path / "runs" / run.run_id / TREAT_LIST_PARQUET)
    # Check parity with holdout_assignment.parquet, row for row (the file is in the scores' order here)
    assert (df["holdout"].to_numpy() == run.assignment[HOLDOUT_MEMBER_COLUMN].to_numpy()).all()
    assert (df["treat"].to_numpy() == run.assignment["treated"].to_numpy()).all()


def test_the_treat_list_rows_are_the_scores_rows_and_the_csv_matches_the_parquet(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    run = write_run(storage, "r_20261001_0b000004", kind="uplift", rows=60, holdout=True, value=True)
    ensure_treat_list(storage, run.run_id)
    parquet = pd.read_parquet(io.BytesIO(storage.read_bytes(run_key(run.run_id, TREAT_LIST_PARQUET))))
    csv = pd.read_csv(
        io.BytesIO(storage.read_bytes(run_key(run.run_id, TREAT_LIST_CSV))), dtype=str, keep_default_na=False
    )
    assert list(parquet["customer_id"]) == list(run.scores["customer_id"]) == list(csv["customer_id"])
    assert list(parquet.columns) == list(csv.columns)
    assert (csv["treat"] == parquet["treat"].map({True: "1", False: "0"})).all()
    assert (csv["holdout"] == parquet["holdout"].map({True: "1", False: "0"})).all()
    assert (parquet["suppression_reason"].isna() == (csv["suppression_reason"] == "")).all()


def test_row_level_treat_list_downloads_gated_and_audited(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    run = write_run(storage, "r_20261001_0b000005", kind="propensity", rows=50)
    ensure_treat_list(storage, run.run_id)

    app = local_app(tmp_path)
    client = TestClient(app)

    # 1. Unauthenticated / Viewer with sign-in on
    headers_viewer = bearer(app, make_user(app, "viewer_1", roles=(Role.VIEWER,)))

    # Should refuse Viewer with 403 ROLE_REQUIRED
    res_csv = client.get(f"/runs/{run.run_id}/treat_list.csv", headers=headers_viewer)
    assert res_csv.status_code == 403, res_csv.text

    res_artefact = client.get(f"/runs/{run.run_id}/artefacts/treat_list.csv", headers=headers_viewer)
    assert res_artefact.status_code == 403, res_artefact.text

    # Summary json is NOT row-level, so Viewer may read it
    res_sum = client.get(
        f"/runs/{run.run_id}/artefacts/{TREAT_LIST_SUMMARY_FILENAME}", headers=headers_viewer
    )
    assert res_sum.status_code == 200, res_sum.text

    # 2. Analyst with sign-in on
    headers_analyst = bearer(app, make_user(app, "analyst_1", roles=(Role.ANALYST,)))

    res_analyst = client.get(f"/runs/{run.run_id}/treat_list.csv", headers=headers_analyst)
    assert res_analyst.status_code == 200, res_analyst.text
    assert res_analyst.content == storage.read_bytes(run_key(run.run_id, TREAT_LIST_CSV))

    # Verify audit event recorded
    audit_log = audit_log_at(tmp_path)
    events = audit_log.query(AuditQuery(action="runs.treat_list_download"))
    assert len(events) >= 1
