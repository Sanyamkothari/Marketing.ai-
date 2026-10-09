"""Plan J M103: the three new campaign routes are audited, and the audit never holds a customer id (DEC-705, DEC-746).

With sign-in on, an Analyst audits a campaign, reads the programme and adds a contact file; each writes one
event that names the campaign (the object), the route's action and a short token - the causal basis, or the
universal holdout's epoch - and nothing else: not an id, not a column name, not a file name.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from api.access_policy import policy_for
from engine.access.roles import Role
from engine.audit.events import AuditQuery
from engine.config import load_use_case
from engine.holdout.salt import resolve_holdout
from engine.holdout.spec import HoldoutConfig
from engine.measurement.simulate import AS_OF, OUTCOME_WINDOW_DAYS, population
from engine.platform_db import PLATFORM_DB_FILENAME, sqlite_engine
from engine.settings import Settings
from tests.integration.measurement.support import USE_CASE
from tests.integration.production.access_support import audit_log_at, bearer, local_app, make_user

BEFORE_PERIOD = datetime(2025, 12, 15, 9, 0, tzinfo=UTC)
"""The universal holdout was first used before the programme's period began."""

pytestmark = pytest.mark.integration

SALT = "audit-trail-salt-000001"


def _upload(client: TestClient, headers: dict[str, str], frame: pd.DataFrame) -> str:
    response = client.post(
        "/uploads",
        files={"file": ("f.csv", frame.to_csv(index=False).encode(), "text/csv")},
        data={"use_case": USE_CASE, "mode": "score"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return str(response.json()["upload_id"])


def test_the_policies_are_analyst_and_audited() -> None:
    for method, path, action in (
        ("POST", "/campaigns/audit", "campaigns.audit"),
        ("POST", "/campaigns/programme", "campaigns.programme"),
        ("POST", "/campaigns/{campaign_id}/contacts", "campaigns.contacts"),
    ):
        policy = policy_for(method, path)
        assert policy is not None and policy.role is Role.ANALYST and policy.action == action
        assert policy.object_type == "campaign" and policy.purpose


def test_each_route_writes_one_event_with_the_campaign_and_a_short_token(tmp_path: Path) -> None:
    app = local_app(tmp_path, holdout_salt=SecretStr(SALT))
    analyst = bearer(app, make_user(app, "audit-trail-analyst", [Role.ANALYST]))
    config = load_use_case(USE_CASE)
    config = config.model_copy(
        update={
            "actions": config.actions.model_copy(
                update={"holdout": HoldoutConfig(scope="universal", fraction=0.1)}
            )
        }
    )
    resolve_holdout(
        config,
        Settings(data_dir=tmp_path, holdout_salt=SecretStr(SALT)),
        sqlite_engine(tmp_path / PLATFORM_DB_FILENAME),
        at=BEFORE_PERIOD,
        record=True,
    )
    sim = population(3_000, 0.10, 0.05, seed=10341, control_share=0.2)
    assignment = pd.DataFrame(
        {
            "customer_id": sim.scores["customer_id"],
            "group": np.where(sim.scores["control_group"], 0, 1),
            "age": np.random.default_rng(1).integers(18, 80, len(sim.scores)),
        }
    )
    contacts = pd.DataFrame(
        {"customer_id": sim.scores["customer_id"], "contacted": sim.received_treatment.astype(int)}
    )
    with TestClient(app) as client:
        a, o, c = (_upload(client, analyst, f) for f in (assignment, sim.outcomes, contacts))
        audit = client.post(
            "/campaigns/audit",
            headers=analyst,
            json={
                "primary_key": "customer_id",
                "assignment": {"upload_id": a, "arm_column": "group"},
                "outcomes": {
                    "upload_id": o,
                    "outcome_column": "converted",
                    "treatment_date_column": "treatment_date",
                },
                "assignment_basis": "random",
                "treatment_start": "2026-04-01T00:00:00Z",
                "outcome_window_days": OUTCOME_WINDOW_DAYS,
                "as_of": AS_OF.isoformat(),
            },
        )
        assert audit.status_code == 201, audit.text
        cid = audit.json()["campaign"]["campaign_id"]
        contact = client.post(
            f"/campaigns/{cid}/contacts",
            headers=analyst,
            json={"upload_id": c, "contacted_column": "contacted"},
        )
        assert contact.status_code == 200, contact.text
        outcomes_only = pd.DataFrame({"customer_id": sim.scores["customer_id"], "converted": 0})
        programme = client.post(
            "/campaigns/programme",
            headers=analyst,
            json={
                "period": {"start": "2026-01-01", "end": "2026-03-31"},
                "primary_key": "customer_id",
                "outcome": {
                    "upload_id": _upload(client, analyst, outcomes_only),
                    "outcome_column": "converted",
                },
            },
        )
        assert programme.status_code == 201, programme.text
        pid = programme.json()["campaign"]["campaign_id"]

    log = audit_log_at(tmp_path)
    for action, object_id, token in (
        ("campaigns.audit", cid, "verified_random"),
        ("campaigns.contacts", cid, None),
        ("campaigns.programme", pid, "universal_epoch_1"),
    ):
        events = log.query(AuditQuery(action=action))
        assert len(events) == 1, action
        event = events[0]
        assert (event.object_type, event.object_id, event.outcome) == ("campaign", object_id, "success")
        assert event.details.get("outcome") == token if token else "outcome" not in event.details
        blob = event.model_dump_json()
        assert "C00000" not in blob and "customer_id" not in blob and "f.csv" not in blob
