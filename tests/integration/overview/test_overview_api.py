"""Plan M M175 (DEC-1605) through the product's own API: `GET /overview`, the Overview page's four parts in one call.

Every campaign is made by the real routes (the campaign record, its test plan, its outcomes, its measurement) over
scoring runs the Phase 1 and uplift stages wrote, exactly as `tests/integration/measurement/test_campaign_summary.py`
does. What is checked is the milestone's list:

* the answer holds nothing the summary route and the campaign list do not: its cards are the first five of
  `GET /campaigns/summary`'s, its headline is one of that summary's totals untouched, and a fresh install gets empty,
  honest parts and a checklist with nothing ticked;
* the live campaigns are the ones not yet measured, newest first, each with a status and a next step read from its
  registered test plan (results not due, due, outcomes added, no plan);
* the checklist is ticked from real state (an upload, a campaign, a measured campaign);
* a figure of the summary that cannot be traced leaves the cards and the headline out and the rest in;
* the route is Viewer, so signed out it is 401 and a Viewer may read it.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.access_policy import policy_for
from api.main import create_app
from engine.access.roles import Role
from engine.measurement.overview import CAMPAIGNS_SHOWN, CARDS_SHOWN
from engine.measurement.summary import SummaryTraceError
from engine.storage import LocalStorage
from tests.integration.measurement.support import (
    MATURE,
    USE_CASE,
    ScoredRun,
    ok,
    propensity_run,
    uplift_run,
    upload,
)
from tests.integration.pilot.test_proof_pack import VALUE_INPUTS
from tests.integration.production.access_support import bearer, local_app, make_user

pytestmark = pytest.mark.integration

RUN_UPLIFT = "r_20261010_17500001"
RUN_BANDED = "r_20261010_17500002"
PAST = "2026-08-30"
POWER = {"mde_pp": 8.0, "base_rate": 0.15}


def _create(client: TestClient, run_id: str, name: str) -> str:
    created = ok(client.post("/campaigns", json={"run_id": run_id, "name": name}), 201)
    return str(created["campaign"]["campaign_id"])


def _plan(client: TestClient, campaign_id: str, analysis_date: str) -> None:
    body = {"metric": "Came back", "outcome_column": "converted", "analysis_date": analysis_date, **POWER}
    ok(client.post(f"/campaigns/{campaign_id}/plan", json=body), 201)


def _outcomes(client: TestClient, campaign_id: str, outcomes: pd.DataFrame) -> None:
    upload_id = upload(client, outcomes)
    ok(
        client.post(
            f"/campaigns/{campaign_id}/outcomes", json={"upload_id": upload_id, "outcome_column": "converted"}
        )
    )


def _converted(run: ScoredRun) -> pd.DataFrame:
    return run.outcomes.rename(columns={"reactivated_90d": "converted"})


@pytest.fixture
def empty(config_root: Path, tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(config_root=config_root, data_dir=tmp_path / "data")) as client:
        yield client


@pytest.fixture(scope="module")
def world(
    config_root: Path, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[TestClient, dict[str, str]]]:
    data_dir = tmp_path_factory.mktemp("overview") / "data"
    data_dir.mkdir()
    storage = LocalStorage(data_dir)
    uplift = uplift_run(storage, RUN_UPLIFT, rows=6_000)
    propensity_run(storage, RUN_BANDED, rows=4_000)
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as client:
        ids: dict[str, str] = {}
        ids["measured"] = _create(client, RUN_UPLIFT, "Win-back measured")
        _outcomes(client, ids["measured"], _converted(uplift))
        ok(client.post(f"/campaigns/{ids['measured']}/measure", json={"as_of": MATURE.isoformat()}))
        ok(client.put(f"/pilot/roi/{RUN_UPLIFT}", json=VALUE_INPUTS))
        ids["no_plan"] = _create(client, RUN_BANDED, "Live without a plan")
        ids["due"] = _create(client, RUN_BANDED, "Results are due")
        _plan(client, ids["due"], PAST)
        ids["in_a_while"] = _create(client, RUN_BANDED, "Results are not due")
        _plan(client, ids["in_a_while"], (datetime.now(UTC) + timedelta(days=30)).date().isoformat())
        ids["outcomes_in"] = _create(client, RUN_BANDED, "Outcomes added, not read")
        _plan(client, ids["outcomes_in"], PAST)
        _outcomes(client, ids["outcomes_in"], _converted(uplift))
        yield client, ids


def test_a_fresh_install_gets_empty_parts_and_nothing_ticked(empty: TestClient) -> None:
    response = empty.get("/overview")
    body = ok(response)
    assert body["cards"] == [] and body["cards_total"] == 0
    assert body["headline"] is None
    assert body["summary_checked"] is True
    assert body["campaigns"] == [] and body["campaigns_total"] == 0
    assert body["checklist"] == {"connected": False, "campaign": False, "results": False}
    assert response.headers["cache-control"] == "no-store"


def test_an_upload_ticks_the_first_item_and_only_it(empty: TestClient) -> None:
    upload(empty, pd.DataFrame({"customer_id": ["a", "b"], "converted": [0, 1]}))
    assert ok(empty.get("/overview"))["checklist"] == {"connected": True, "campaign": False, "results": False}


def test_the_headline_is_the_summarys_own_total_and_the_cards_are_its_own(
    world: tuple[TestClient, dict[str, str]],
) -> None:
    client, ids = world
    summary = ok(client.get("/campaigns/summary"))
    overview = ok(client.get("/overview"))
    totals = {t["unit"]: t for t in summary["proven"]["totals"]}
    assert "rupees" in totals, "the world proves value in rupees, which the headline prefers"
    assert overview["headline"]["unit"] == "rupees"
    assert overview["headline"]["total"] == totals["rupees"]["total"], "the very same traced figure"
    assert overview["headline"]["label"] == totals["rupees"]["label"]
    assert overview["headline"]["rule"] == summary["proven"]["rule"]
    assert overview["headline"]["campaigns_counted"] == len(totals["rupees"]["campaigns"]) >= 1
    assert overview["cards"] == summary["cards"][:CARDS_SHOWN]
    assert overview["cards_total"] == len(summary["cards"])
    assert ids["measured"] in {line["campaign_id"] for line in totals["rupees"]["campaigns"]}


def test_the_live_campaigns_are_the_unmeasured_ones_with_a_status_and_a_next_step_from_their_plan(
    world: tuple[TestClient, dict[str, str]],
) -> None:
    client, ids = world
    overview = ok(client.get("/overview"))
    by_id = {c["campaign_id"]: c for c in overview["campaigns"]}
    assert ids["measured"] not in by_id, "a measured campaign is not live"
    assert overview["campaigns_total"] == 4 and len(overview["campaigns"]) == 4 <= CAMPAIGNS_SHOWN
    assert [c["campaign_id"] for c in overview["campaigns"]] == [
        c["campaign_id"]
        for c in ok(client.get("/campaigns"))["campaigns"]
        if c["campaign_id"] != ids["measured"]
    ], "newest first, as the campaign list"
    assert (by_id[ids["no_plan"]]["status"], by_id[ids["no_plan"]]["next_step"]) == ("live", "plan")
    assert by_id[ids["no_plan"]]["results_date"] is None
    assert (by_id[ids["in_a_while"]]["status"], by_id[ids["in_a_while"]]["next_step"]) == ("live", "wait")
    assert (
        by_id[ids["in_a_while"]]["results_date"]
        == (datetime.now(UTC) + timedelta(days=30)).date().isoformat()
    )
    assert (by_id[ids["due"]]["status"], by_id[ids["due"]]["next_step"]) == (
        "waiting_for_results",
        "add_results",
    )
    assert by_id[ids["due"]]["results_date"] == PAST
    assert (by_id[ids["outcomes_in"]]["status"], by_id[ids["outcomes_in"]]["next_step"]) == (
        "waiting_for_results",
        "measure",
    )
    assert by_id[ids["due"]]["name"] == "Results are due"
    assert by_id[ids["due"]]["use_case_id"] == USE_CASE


def test_the_checklist_is_ticked_from_real_state(world: tuple[TestClient, dict[str, str]]) -> None:
    client, _ = world
    assert ok(client.get("/overview"))["checklist"] == {"connected": True, "campaign": True, "results": True}


def test_a_campaign_with_outcomes_but_no_report_counts_as_having_results(
    config_root: Path, tmp_path: Path
) -> None:
    storage = LocalStorage(tmp_path / "data")
    uplift = uplift_run(storage, RUN_UPLIFT, rows=3_000)
    with TestClient(create_app(config_root=config_root, data_dir=tmp_path / "data")) as client:
        campaign_id = _create(client, RUN_UPLIFT, "Only one")
        assert ok(client.get("/overview"))["checklist"]["results"] is False
        _outcomes(client, campaign_id, _converted(uplift))
        assert ok(client.get("/overview"))["checklist"]["results"] is True


def test_the_lists_are_capped_and_say_how_many_there_are(config_root: Path, tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path / "data")
    propensity_run(storage, RUN_BANDED, rows=2_000)
    with TestClient(create_app(config_root=config_root, data_dir=tmp_path / "data")) as client:
        for index in range(CAMPAIGNS_SHOWN + 2):
            _create(client, RUN_BANDED, f"Campaign {index}")
        overview = ok(client.get("/overview"))
    assert len(overview["campaigns"]) == CAMPAIGNS_SHOWN
    assert overview["campaigns_total"] == CAMPAIGNS_SHOWN + 2


def test_a_figure_that_does_not_trace_leaves_cards_and_headline_out_but_not_the_campaigns(
    world: tuple[TestClient, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, ids = world

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise SummaryTraceError(["a number did not resolve"])

    monkeypatch.setattr("api.routes.overview.build_summary", refuse)
    body = ok(client.get("/overview"))
    assert body["summary_checked"] is False
    assert body["cards"] == [] and body["cards_total"] == 0 and body["headline"] is None
    assert {c["campaign_id"] for c in body["campaigns"]} >= {ids["due"], ids["no_plan"]}
    assert body["checklist"]["campaign"] is True


def test_the_route_is_viewer_and_a_signed_out_caller_is_refused(tmp_path: Path) -> None:
    policy = policy_for("GET", "/overview")
    assert policy is not None and policy.role is Role.VIEWER and policy.action == "overview.read"
    assert not policy.audit_reads, "it holds no customer row"
    app = local_app(tmp_path)
    client = TestClient(app, raise_server_exceptions=False)
    refused = client.get("/overview")
    assert refused.status_code == 401 and refused.json()["detail"]["code"] == "AUTH_REQUIRED"
    viewer = make_user(app, "overview-viewer", [Role.VIEWER])
    seen = client.get("/overview", headers=bearer(app, viewer))
    assert seen.status_code == 200, seen.text
    assert seen.json()["checklist"] == {"connected": False, "campaign": False, "results": False}
