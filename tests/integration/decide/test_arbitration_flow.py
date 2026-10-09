"""Integration tests for Plan J M101 arbitration API and campaign measurement (DEC-1311).

Acceptance tests:
- POST /decide/arbitrate (Analyst) runs arbitration over selected use cases.
- Writes arbitrated_treat_list.csv and .parquet.
- Each use case's campaign measures only its winning rows.
- A conflicts summary on Results: how many customers qualified for more than one action and what was dropped.
- Row-level downloads protected (Viewer refused, Analyst audited).
"""

from __future__ import annotations

import io
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from engine.access.roles import Role
from engine.audit.events import AuditQuery
from engine.contracts import RunRecord
from engine.decide.arbitrate import (
    ARBITRATED_TREAT_LIST_CSV,
    ARBITRATED_TREAT_LIST_PARQUET,
    ARBITRATION_SUMMARY_FILENAME,
)
from engine.decide.treat_list import ensure_treat_list
from engine.measurement.campaign import InMemoryCampaignStore
from engine.storage import LocalStorage, run_key
from tests.fixtures.decide.treat_runs import write_run
from tests.integration.production.access_support import (
    audit_log_at,
    bearer,
    local_app,
    make_user,
)

pytestmark = pytest.mark.integration


def test_arbitrate_api_and_campaign_creation(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    campaign_store = InMemoryCampaignStore()

    r1 = write_run(storage, "r_20261001_arb001", kind="propensity", rows=50)
    r2 = write_run(storage, "r_20261001_arb002", kind="uplift", rows=50, value=True)

    # Set r2 to a different use case
    rec2 = storage.read_model(run_key(r2.run_id, "run.json"), RunRecord)
    rec2_updated = rec2.model_copy(
        update={"use_case_id": "targeted-advertisement", "use_case_name": "Targeted Advertisement"}
    )
    storage.write_model(run_key(r2.run_id, "run.json"), rec2_updated)

    ensure_treat_list(storage, r1.run_id)
    ensure_treat_list(storage, r2.run_id)

    app = local_app(tmp_path)
    app.state.campaign_store = campaign_store

    client = TestClient(app, raise_server_exceptions=False)
    u_viewer = make_user(app, "viewer", [Role.VIEWER])
    u_analyst = make_user(app, "analyst", [Role.ANALYST])

    # 2. Check access policy: Viewer is refused (403)
    res_viewer = client.post(
        "/decide/arbitrate",
        json={"use_cases": ["win-back-campaign", "targeted-advertisement"]},
        headers=bearer(app, u_viewer),
    )
    assert res_viewer.status_code == 403

    # 3. Analyst executes arbitration
    res_analyst = client.post(
        "/decide/arbitrate",
        json={"use_cases": ["win-back-campaign", "targeted-advertisement"]},
        headers=bearer(app, u_analyst),
    )
    assert res_analyst.status_code == 200, res_analyst.text
    body = res_analyst.json()

    assert "summary" in body
    summary = body["summary"]
    assert summary["total_customers"] == 50
    assert "campaign_ids" in body
    assert len(body["campaign_ids"]) == 2

    # 4. Verify arbitrated files were written into the runs (where retention finds them), not at the store root
    assert body["summary"]["run_ids"] == [r1.run_id, r2.run_id]
    for run_id in (r1.run_id, r2.run_id):
        assert storage.exists(run_key(run_id, ARBITRATED_TREAT_LIST_CSV))
        assert storage.exists(run_key(run_id, ARBITRATED_TREAT_LIST_PARQUET))
        assert storage.exists(run_key(run_id, ARBITRATION_SUMMARY_FILENAME))
    assert storage.exists(f"decide/{ARBITRATION_SUMMARY_FILENAME}")
    assert not storage.exists(f"decide/{ARBITRATED_TREAT_LIST_CSV}")
    assert not storage.exists(f"decide/{ARBITRATED_TREAT_LIST_PARQUET}")

    arb_df = pd.read_parquet(
        io.BytesIO(storage.read_bytes(run_key(r1.run_id, ARBITRATED_TREAT_LIST_PARQUET)))
    )
    assert len(arb_df) == 50
    # No customer has more than one treat action
    treated = arb_df[arb_df["treat"]]
    assert treated["customer_id"].is_unique

    # 5. Verify campaigns measure ONLY winning rows
    c_ids = body["campaign_ids"]
    for cid in c_ids:
        campaign = campaign_store.get(cid)
        assert campaign is not None
        uc_id = campaign.use_case_id
        # Expected winners for this use case
        expected_winners = int((arb_df["treat"] & (arb_df["winning_use_case"] == uc_id)).sum())
        assert campaign.counts.intended_treated == expected_winners


def test_get_decide_conflicts_and_downloads(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    campaign_store = InMemoryCampaignStore()

    r1 = write_run(storage, "r_20261001_arb003", kind="propensity", rows=30)
    r2 = write_run(storage, "r_20261001_arb004", kind="propensity", rows=30)

    rec2 = storage.read_model(run_key(r2.run_id, "run.json"), RunRecord)
    rec2_updated = rec2.model_copy(
        update={"use_case_id": "targeted-advertisement", "use_case_name": "Targeted Advertisement"}
    )
    storage.write_model(run_key(r2.run_id, "run.json"), rec2_updated)

    ensure_treat_list(storage, r1.run_id)
    ensure_treat_list(storage, r2.run_id)

    app = local_app(tmp_path)
    app.state.campaign_store = campaign_store

    client = TestClient(app, raise_server_exceptions=False)
    u_viewer = make_user(app, "viewer", [Role.VIEWER])
    u_analyst = make_user(app, "analyst", [Role.ANALYST])

    # Run arbitration
    client.post(
        "/decide/arbitrate",
        json={"use_cases": ["win-back-campaign", "targeted-advertisement"]},
        headers=bearer(app, u_analyst),
    )

    # Viewer can see conflicts summary
    res_conflicts = client.get("/decide/conflicts", headers=bearer(app, u_viewer))
    assert res_conflicts.status_code == 200
    assert "customers_with_conflicts" in res_conflicts.json()

    # Viewer is refused download of arbitrated CSV (row-level data)
    res_csv_viewer = client.get("/decide/arbitrated-treat-list.csv", headers=bearer(app, u_viewer))
    assert res_csv_viewer.status_code == 403

    # Analyst can download arbitrated CSV
    res_csv_analyst = client.get("/decide/arbitrated-treat-list.csv", headers=bearer(app, u_analyst))
    assert res_csv_analyst.status_code == 200
    assert b"customer_id" in res_csv_analyst.content
    assert b"winning_use_case" in res_csv_analyst.content

    # Audit log records the row-level download
    events = audit_log_at(tmp_path).query(AuditQuery(limit=100))
    analyst_downloads = [e for e in events if e.actor_id == u_analyst and e.outcome == "success"]
    assert len(analyst_downloads) >= 1
