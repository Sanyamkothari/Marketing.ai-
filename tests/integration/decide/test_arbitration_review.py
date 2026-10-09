"""`POST /decide/arbitrate` on real scoring runs (Plan J M101, DEC-1311; Claude Code's review).

The runs are written by the product's own code (`tests/fixtures/decide/treat_runs.py`) and their treat
lists built by `build_treat_list`. These tests check what a person gets back: the campaigns each measure
exactly the rows their use case won (with a one-column and a composite key), the configuration comes from
the app's config root (none by default), and the order of the use cases is the tie-break.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine.access.roles import Role
from engine.decide.arbitrate import ARBITRATED_TREAT_LIST_PARQUET
from engine.decide.treat_list import ensure_treat_list
from engine.measurement.campaign import (
    ASSIGNMENT_FILENAME,
    InMemoryCampaignStore,
    campaign_key,
    read_frame,
)
from engine.storage import LocalStorage
from tests.fixtures.decide.arbitration_runs import run_for_use_case
from tests.integration.production.access_support import bearer, local_app, make_user

pytestmark = pytest.mark.integration


def _prepare(
    tmp_path: Path, runs: list[tuple[str, dict[str, Any]]]
) -> tuple[FastAPI, TestClient, InMemoryCampaignStore, list[str]]:
    storage = LocalStorage(tmp_path)
    run_ids = []
    for use_case, options in runs:
        run_id = run_for_use_case(storage, use_case, **options)
        ensure_treat_list(storage, run_id)
        run_ids.append(run_id)
    store = InMemoryCampaignStore()
    app = local_app(tmp_path)
    app.state.campaign_store = store
    return app, TestClient(app, raise_server_exceptions=False), store, run_ids


def _arbitrate(
    app: FastAPI, client: TestClient, body: dict[str, Any], tmp_path: Path
) -> tuple[dict[str, Any], pd.DataFrame]:
    analyst = make_user(app, "arbitrator", [Role.ANALYST])
    response = client.post("/decide/arbitrate", json=body, headers=bearer(app, analyst))
    assert response.status_code == 200, response.text
    storage = LocalStorage(tmp_path)
    arbitrated = pd.read_parquet(io.BytesIO(storage.read_bytes(f"decide/{ARBITRATED_TREAT_LIST_PARQUET}")))
    return response.json(), arbitrated


@pytest.mark.parametrize("composite", [False, True], ids=["one-column-key", "composite-key"])
def test_each_campaign_measures_exactly_the_rows_its_use_case_won(tmp_path: Path, composite: bool) -> None:
    """The treated arm of a use case's campaign is its winning rows, with a one-column and a composite key.

    Fails on 98b2959 for a composite key: the route named the winners by the first key column only, so no
    row of the scores matched and every campaign measured nobody.
    """
    options: dict[str, Any] = {"kind": "propensity", "rows": 60, "composite": composite, "value": True}
    # The composite-key fixture is a propensity run; a one-column key also gets an uplift run.
    second = options if composite else {"kind": "uplift", "rows": 60, "value": True}
    app, client, store, _ = _prepare(
        tmp_path,
        [("uc-first", options | {"holdout": True}), ("uc-second", second), ("uc-third", options)],
    )
    root = tmp_path / "config-root"
    (root / "decide").mkdir(parents=True)
    priorities = {"uc-second": 2.0, "uc-third": 3.0} if composite else {"uc-second": 2.0}
    lines = "".join(f"  {name}:\n    priority: {value}\n" for name, value in priorities.items())
    (root / "decide" / "arbitration.yaml").write_text(f"use_cases:\n{lines}", encoding="utf-8")
    app.state.config_root = root
    body, arbitrated = _arbitrate(app, client, {"use_cases": ["uc-first", "uc-second", "uc-third"]}, tmp_path)
    key = ["customer_id", "snapshot_date"] if composite else ["customer_id"]

    winners = arbitrated[arbitrated["treat"]]
    assert not winners.duplicated(subset=key).any(), "no customer has two actions"
    assert winners["winning_use_case"].nunique() >= (1 if composite else 2)
    storage = LocalStorage(tmp_path)
    for campaign_id in body["campaign_ids"]:
        campaign = store.get(campaign_id)
        assert campaign is not None
        assignment = read_frame(storage, campaign_key(campaign_id, ASSIGNMENT_FILENAME))
        measured = assignment[assignment["arm"] == "treated"]
        won = winners[winners["winning_use_case"] == campaign.use_case_id]
        assert set(map(tuple, measured[key].astype(str).to_numpy())) == set(
            map(tuple, won[key].astype(str).to_numpy())
        ), campaign.use_case_id
        # Customers that lost are in neither arm, and none of them is counted as intended.
        lost = assignment[(assignment["arm"] == "suppressed") & assignment["intended"]]
        assert lost.empty
        assert campaign.counts.intended_treated <= len(won)


def test_without_a_config_file_every_use_case_has_priority_one_and_a_customer_one_action(
    tmp_path: Path,
) -> None:
    app, client, _, _ = _prepare(
        tmp_path,
        [("uc-one", {"kind": "propensity", "rows": 40}), ("uc-two", {"kind": "propensity", "rows": 40})],
    )
    empty_root = tmp_path / "empty-config-root"
    empty_root.mkdir()
    app.state.config_root = empty_root
    _, arbitrated = _arbitrate(app, client, {"use_cases": ["uc-one", "uc-two"]}, tmp_path)
    treated = arbitrated[arbitrated["treat"]]
    assert treated["customer_id"].is_unique
    assert (treated["priority_weight"] == 1.0).all()


def test_a_config_file_in_the_config_root_sets_priorities_and_channel_caps(tmp_path: Path) -> None:
    app, client, _, _ = _prepare(
        tmp_path,
        [
            ("uc-one", {"kind": "uplift", "rows": 60, "value": True, "channels": True}),
            ("uc-two", {"kind": "uplift", "rows": 60, "value": True, "channels": True}),
        ],
    )
    root = tmp_path / "config-root"
    (root / "decide").mkdir(parents=True)
    (root / "decide" / "arbitration.yaml").write_text(
        "schema_version: 1\nuse_cases:\n  uc-two:\n    priority: 3.0\nchannel_caps:\n  sms: 2\n",
        encoding="utf-8",
    )
    app.state.config_root = root
    body, arbitrated = _arbitrate(app, client, {"use_cases": ["uc-one", "uc-two"]}, tmp_path)
    treated = arbitrated[arbitrated["treat"]]
    assert set(treated["winning_use_case"]) == {"uc-two"}, "uc-two has the higher priority and equal values"
    assert int((treated["channel"] == "sms").sum()) == 2
    assert body["summary"]["channel_capped_count"] > 0


def test_with_no_use_cases_named_the_order_is_by_use_case_id(tmp_path: Path) -> None:
    """The tie-break is the request's use case order; with none named it is the use case id, on every call."""
    app, client, _, _ = _prepare(
        tmp_path,
        [("uc-b", {"kind": "propensity", "rows": 30}), ("uc-a", {"kind": "propensity", "rows": 30})],
    )
    _, arbitrated = _arbitrate(app, client, {}, tmp_path)
    contested = arbitrated[arbitrated["treat"] & arbitrated["losing_actions"].notna()]
    assert len(contested) > 0
    # Both lists want the same customers and neither has a value: uc-a, first by id, wins every one.
    assert set(contested["winning_use_case"]) == {"uc-a"}


def test_two_runs_of_one_use_case_are_refused(tmp_path: Path) -> None:
    app, client, _, run_ids = _prepare(
        tmp_path,
        [("uc-same", {"kind": "propensity", "rows": 30}), ("uc-same", {"kind": "propensity", "rows": 30})],
    )
    analyst = make_user(app, "repeater", [Role.ANALYST])
    response = client.post("/decide/arbitrate", json={"run_ids": run_ids}, headers=bearer(app, analyst))
    assert response.status_code == 422
    assert "ARBITRATION_USE_CASE_REPEATED" in response.text
