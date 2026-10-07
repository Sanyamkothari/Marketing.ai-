"""Plan J M94 through the product's own API: one campaign record, one measurement path, a registered plan.

The scoring runs are written straight into the store (`support.py`): an uplift run and a Phase 1
propensity run over win-back campaigns with a planted effect. What is checked is the milestone's
acceptance list:

* on an uplift run with every row mature and a fixed `as_of`, `POST /campaigns/{id}/measure`, `POST
  /runs/{id}/campaign-results` and `POST /runs/{id}/measure` give the same report except
  `computed_at` and `campaign_id`; on a propensity run the campaign equals `/campaign-results` called
  with the campaign's treat bands (and, with none, intent to treat);
* outcomes before maturity are `409 CAMPAIGN_NOT_MATURED` with a date and no number, and nothing is
  stored; a treatment start entered days later moves that date;
* the plan: the audit event's `after_hash` is `content_hash(plan)`, an identical re-POST returns the
  stored plan, a different one is `409 TEST_PLAN_EXISTS`, an amendment writes version 2 with
  `amends` and keeps version 1; measuring before the analysis date is an early look with no verdict;
  a measurement that differs from the plan is `409 TEST_PLAN_CHANGED`.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine.audit.events import AuditQuery, content_hash
from engine.audit.store import SqlAuditLog
from engine.measurement.campaign import Campaign, SqlCampaignStore, campaign_key
from engine.measurement.plan import TestPlan
from engine.platform_db import PLATFORM_DB_FILENAME, sqlite_engine
from engine.storage import LocalStorage
from engine.uplift.contracts import IncrementalityReport
from tests.integration.measurement.support import (
    MATURE,
    SENT,
    TARGET,
    ScoredRun,
    ok,
    propensity_run,
    uplift_run,
    upload,
)

pytestmark = pytest.mark.integration

UPLIFT_RUN = "r_20261007_94000001"
PROPENSITY_RUN = "r_20261007_94000002"
DIFFERS_ONLY_IN = {"computed_at", "campaign_id"}


@dataclass(frozen=True)
class World:
    client: TestClient
    storage: LocalStorage
    data_dir: Path
    uplift: ScoredRun
    propensity: ScoredRun
    uplift_outcomes: str
    propensity_outcomes: str


@pytest.fixture(scope="module")
def world(config_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[World]:
    data_dir = tmp_path_factory.mktemp("campaigns") / "data"
    data_dir.mkdir()
    storage = LocalStorage(data_dir)
    uplift = uplift_run(storage, UPLIFT_RUN)
    propensity = propensity_run(storage, PROPENSITY_RUN)
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as client:
        yield World(
            client=client,
            storage=storage,
            data_dir=data_dir,
            uplift=uplift,
            propensity=propensity,
            uplift_outcomes=upload(client, uplift.outcomes, name="uplift_outcomes.csv"),
            propensity_outcomes=upload(client, propensity.outcomes, name="propensity_outcomes.csv"),
        )


def _create(world: World, run_id: str, **extra: Any) -> dict[str, Any]:
    return ok(world.client.post("/campaigns", json={"run_id": run_id, **extra}), 201)


def _with_outcomes(world: World, run_id: str, upload_id: str, **extra: Any) -> str:
    campaign_id = str(_create(world, run_id, **extra)["campaign"]["campaign_id"])
    ok(world.client.post(f"/campaigns/{campaign_id}/outcomes", json={"upload_id": upload_id}))
    return campaign_id


def _measure(world: World, campaign_id: str, as_of: datetime = MATURE, **extra: Any) -> Any:
    return world.client.post(f"/campaigns/{campaign_id}/measure", json={"as_of": as_of.isoformat(), **extra})


def _campaign_results(world: World, run_id: str, upload_id: str, **extra: Any) -> IncrementalityReport:
    body = {
        "upload_id": upload_id,
        "outcome_column": TARGET,
        "outcome_window_days": 90,
        "as_of": MATURE.isoformat(),
        **extra,
    }
    return IncrementalityReport.model_validate(
        ok(world.client.post(f"/runs/{run_id}/campaign-results", json=body))
    )


def _same(left: IncrementalityReport, right: IncrementalityReport) -> None:
    assert left.model_dump(exclude=DIFFERS_ONLY_IN) == right.model_dump(exclude=DIFFERS_ONLY_IN)


def _plan_body(**extra: Any) -> dict[str, Any]:
    return {
        "metric": "reactivated within 90 days",
        "outcome_column": TARGET,
        "analysis_date": "2026-08-15",
        "mde_pp": 2.0,
        "base_rate": 0.1,
        **extra,
    }


# ---------------------------------------------------------------------------
# One measurement path
# ---------------------------------------------------------------------------
def test_an_uplift_campaign_measures_exactly_what_the_run_measures(world: World) -> None:
    campaign_id = _with_outcomes(world, UPLIFT_RUN, world.uplift_outcomes)
    view = ok(_measure(world, campaign_id))
    campaign = Campaign.model_validate(view["campaign"])
    assert campaign.population == "intended" and campaign.treatment_start == SENT
    assert campaign.outcome_window_days == 90, "the use case's own window"
    report = IncrementalityReport.model_validate(view["report"])
    assert report.campaign_id == campaign_id and report.run_id == UPLIFT_RUN
    assert report.treated_rows > 0 and report.control_rows > 0 and report.rows_immature == 0
    run_report = _campaign_results(world, UPLIFT_RUN, world.uplift_outcomes)
    _same(report, run_report)
    # the campaign's estimate is the run's own intended-treatment estimate
    scores = world.uplift.scores
    intended = scores["intended_treatment"] & scores["suppressed_reason"].isna()
    assert report.treated_rows + report.control_rows <= int(intended.sum())
    assert view["verdict"] is not None and report.early_look is False and report.test_plan_hash is None
    # step 4 (`POST /runs/{id}/measure`) measures through the same function: the same report again
    step4 = ok(
        world.client.post(
            f"/runs/{UPLIFT_RUN}/measure",
            json={"upload_id": world.uplift_outcomes, "as_of": MATURE.isoformat(), "outcome_window_days": 90},
        )
    )
    _same(IncrementalityReport.model_validate(step4["report"]), report)


def test_a_propensity_campaign_equals_campaign_results_with_its_treat_bands(world: World) -> None:
    bands = ["High", "Medium"]
    campaign_id = _with_outcomes(world, PROPENSITY_RUN, world.propensity_outcomes, bands=bands)
    view = ok(_measure(world, campaign_id))
    assert view["campaign"]["population"] == "bands" and view["campaign"]["bands"] == bands
    report = IncrementalityReport.model_validate(view["report"])
    _same(report, _campaign_results(world, PROPENSITY_RUN, world.propensity_outcomes, bands=bands))
    assert report.treated_rows > 0 and report.control_rows > 0


def test_a_propensity_campaign_without_bands_is_intent_to_treat(world: World) -> None:
    campaign_id = _with_outcomes(world, PROPENSITY_RUN, world.propensity_outcomes)
    report = IncrementalityReport.model_validate(ok(_measure(world, campaign_id))["report"])
    _same(report, _campaign_results(world, PROPENSITY_RUN, world.propensity_outcomes))


def test_bands_do_not_apply_to_an_uplift_run(world: World) -> None:
    response = world.client.post("/campaigns", json={"run_id": UPLIFT_RUN, "bands": ["High"]})
    assert response.status_code == 422 and response.json()["detail"]["code"] == "CAMPAIGN_INVALID"


def test_the_record_and_its_files_are_where_the_privacy_jobs_look(world: World) -> None:
    created = _create(world, PROPENSITY_RUN)
    campaign_id = created["campaign"]["campaign_id"]
    for name in ("campaign.json", "assignment.parquet"):
        assert world.storage.exists(campaign_key(campaign_id, name)), name
    store = SqlCampaignStore(sqlite_engine(world.data_dir / PLATFORM_DB_FILENAME))
    stored = store.get(campaign_id)
    assert stored is not None and stored == Campaign.model_validate(created["campaign"])
    listed = ok(world.client.get("/campaigns", params={"run_id": PROPENSITY_RUN}))["campaigns"]
    assert campaign_id in {item["campaign_id"] for item in listed}
    assert all(item["run_ids"] == [PROPENSITY_RUN] for item in listed)
    assert ok(world.client.get(f"/campaigns/{campaign_id}"))["report"] is None


# ---------------------------------------------------------------------------
# Treatment time and maturity
# ---------------------------------------------------------------------------
def test_outcomes_before_maturity_give_a_date_and_never_a_partial_number(world: World) -> None:
    campaign_id = _with_outcomes(world, PROPENSITY_RUN, world.propensity_outcomes)
    early = _measure(world, campaign_id, as_of=SENT + timedelta(days=30))
    assert early.status_code == 409
    body = early.json()
    assert body["detail"]["code"] == "CAMPAIGN_NOT_MATURED"
    assert body["results_available_on"] == (SENT + timedelta(days=90)).date().isoformat()
    assert set(body) == {"detail", "results_available_on"}, "no rate, no lift, no count"
    assert not world.storage.exists(campaign_key(campaign_id, "incrementality_report.json"))
    assert ok(world.client.get(f"/campaigns/{campaign_id}"))["campaign"]["status"] == "live"


def test_per_row_dates_partly_mature_are_refused_too(world: World) -> None:
    frame = world.propensity.outcomes.copy()
    frame["sent_on"] = [
        (SENT + timedelta(days=60 if index % 2 else 0)).date().isoformat() for index in range(len(frame))
    ]
    dated = upload(world.client, frame, name="dated_outcomes.csv")
    campaign_id = str(_create(world, PROPENSITY_RUN)["campaign"]["campaign_id"])
    ok(
        world.client.post(
            f"/campaigns/{campaign_id}/outcomes",
            json={"upload_id": dated, "treatment_date_column": "sent_on"},
        )
    )
    # a run report would show the mature half; a campaign shows nothing until every row is in
    partial = _measure(world, campaign_id, as_of=SENT + timedelta(days=100))
    assert partial.status_code == 409 and partial.json()["detail"]["code"] == "CAMPAIGN_NOT_MATURED"
    assert partial.json()["results_available_on"] == (SENT + timedelta(days=150)).date().isoformat()


def test_a_treatment_start_entered_later_moves_the_maturity_date(world: World) -> None:
    sent_later = SENT + timedelta(days=7)
    campaign_id = _with_outcomes(
        world, PROPENSITY_RUN, world.propensity_outcomes, treatment_start=sent_later.isoformat()
    )
    view = ok(world.client.get(f"/campaigns/{campaign_id}"))
    assert view["campaign"]["treatment_start_source"] == "entered"
    at_run_window = _measure(world, campaign_id, as_of=SENT + timedelta(days=91))
    assert at_run_window.status_code == 409, "the run's finish time would have called it mature"
    assert (
        at_run_window.json()["results_available_on"] == (sent_later + timedelta(days=90)).date().isoformat()
    )
    assert _measure(world, campaign_id, as_of=sent_later + timedelta(days=90)).status_code == 200


def test_a_campaign_cannot_go_out_before_its_list_was_made(world: World) -> None:
    response = world.client.post(
        "/campaigns",
        json={"run_id": PROPENSITY_RUN, "treatment_start": (SENT - timedelta(days=1)).isoformat()},
    )
    assert response.status_code == 422 and response.json()["detail"]["path"] == "treatment_start"


def test_missing_things_are_named(world: World) -> None:
    missing = world.client.get("/campaigns/c_20261007_00000000")
    assert missing.status_code == 404 and missing.json()["detail"]["code"] == "CAMPAIGN_NOT_FOUND"
    campaign_id = _create(world, PROPENSITY_RUN)["campaign"]["campaign_id"]
    no_outcomes = _measure(world, campaign_id)
    assert (
        no_outcomes.status_code == 409 and no_outcomes.json()["detail"]["code"] == "CAMPAIGN_OUTCOMES_MISSING"
    )
    not_scored = world.client.post("/campaigns", json={"run_id": "r_20261007_deadbeef"})
    assert not_scored.status_code == 404


# ---------------------------------------------------------------------------
# The registered test plan
# ---------------------------------------------------------------------------
def _audit_events(world: World, campaign_id: str) -> list[Any]:
    log = SqlAuditLog(sqlite_engine(world.data_dir / PLATFORM_DB_FILENAME))
    return list(log.query(AuditQuery(object_type="campaign", object_id=campaign_id)))


def test_the_plan_is_fixed_in_the_audit_trail(world: World) -> None:
    campaign_id = _with_outcomes(world, PROPENSITY_RUN, world.propensity_outcomes)
    response = world.client.post(f"/campaigns/{campaign_id}/plan", json=_plan_body())
    plan = TestPlan.model_validate(ok(response, 201))
    assert plan.version == 1 and plan.amends is None and plan.campaign_id == campaign_id
    assert plan.holdout_fraction > 0 and plan.n_holdout > 0 and plan.n_treat > plan.n_holdout
    (event,) = [e for e in _audit_events(world, campaign_id) if e.action == "campaigns.plan"]
    assert event.after_hash == content_hash(plan)
    assert event.after_hash != content_hash(response.content), "the plan's hash, not the response bytes'"
    assert event.details["plan_hash"] == plan.plan_hash
    stored = world.storage.read_model(campaign_key(campaign_id, "test_plan.json"), TestPlan)
    assert stored == plan and content_hash(stored) == event.after_hash

    # identical re-POST: the stored plan, the same hash, no new version
    again = world.client.post(f"/campaigns/{campaign_id}/plan", json=_plan_body())
    assert TestPlan.model_validate(ok(again)) == plan
    assert ok(world.client.get(f"/campaigns/{campaign_id}/plan"))["versions"] == [
        plan.model_dump(mode="json")
    ]

    # a different body: 409, nothing written
    other = world.client.post(f"/campaigns/{campaign_id}/plan", json=_plan_body(mde_pp=3.0))
    assert other.status_code == 409 and other.json()["detail"]["code"] == "TEST_PLAN_EXISTS"
    assert world.storage.read_model(campaign_key(campaign_id, "test_plan.json"), TestPlan) == plan

    # an amendment: version 2 with `amends`, version 1 still readable
    amended = TestPlan.model_validate(
        ok(
            world.client.post(
                f"/campaigns/{campaign_id}/plan/amendments",
                json={**_plan_body(analysis_date="2026-08-20"), "reason": "The sponsor moved the review."},
            ),
            201,
        )
    )
    assert (amended.version, amended.amends, amended.amendment_reason) == (
        2,
        plan.plan_hash,
        "The sponsor moved the review.",
    )
    assert amended.plan_hash != plan.plan_hash
    view = ok(world.client.get(f"/campaigns/{campaign_id}/plan"))
    assert [TestPlan.model_validate(v) for v in view["versions"]] == [plan, amended]
    assert TestPlan.model_validate(view["plan"]) == amended
    assert world.storage.read_model(campaign_key(campaign_id, "test_plan_v1.json"), TestPlan) == plan
    amend_events = [e for e in _audit_events(world, campaign_id) if e.action == "campaigns.plan_amend"]
    assert [e.after_hash for e in amend_events] == [content_hash(amended)]
    assert (
        ok(world.client.get(f"/campaigns/{campaign_id}"))["campaign"]["test_plan_hash"] == amended.plan_hash
    )


def test_an_amendment_needs_a_plan_and_a_change(world: World) -> None:
    campaign_id = _with_outcomes(world, PROPENSITY_RUN, world.propensity_outcomes)
    body = {**_plan_body(), "reason": "No reason."}
    none = world.client.post(f"/campaigns/{campaign_id}/plan/amendments", json=body)
    assert none.status_code == 404 and none.json()["detail"]["code"] == "TEST_PLAN_NOT_FOUND"
    ok(world.client.post(f"/campaigns/{campaign_id}/plan", json=_plan_body()), 201)
    same = world.client.post(f"/campaigns/{campaign_id}/plan/amendments", json=body)
    assert same.status_code == 422 and same.json()["detail"]["code"] == "TEST_PLAN_INVALID"


def test_an_analysis_date_before_the_outcomes_are_in_is_refused(world: World) -> None:
    campaign_id = _create(world, PROPENSITY_RUN)["campaign"]["campaign_id"]
    response = world.client.post(
        f"/campaigns/{campaign_id}/plan", json=_plan_body(analysis_date="2026-06-01")
    )
    assert response.status_code == 422 and response.json()["detail"]["path"] == "analysis_date"


def test_measuring_before_the_analysis_date_is_an_early_look_with_no_verdict(world: World) -> None:
    campaign_id = _with_outcomes(world, PROPENSITY_RUN, world.propensity_outcomes)
    plan = TestPlan.model_validate(
        ok(
            world.client.post(f"/campaigns/{campaign_id}/plan", json=_plan_body(analysis_date="2026-09-15")),
            201,
        )
    )
    early = ok(_measure(world, campaign_id, as_of=datetime(2026, 9, 1, tzinfo=UTC)))
    report = IncrementalityReport.model_validate(early["report"])
    assert report.early_look is True and report.test_plan_hash == plan.plan_hash
    assert report.summary.startswith("Early look, before the planned analysis date of 15 Sep 2026")
    assert early["verdict"] is None, "no final verdict on an early look"
    assert early["campaign"]["status"] == "live"
    # the numbers are the same numbers; only the label and the verdict wait for the date
    final = ok(_measure(world, campaign_id, as_of=datetime(2026, 9, 15, tzinfo=UTC)))
    late = IncrementalityReport.model_validate(final["report"])
    assert late.early_look is False and final["verdict"] is not None
    assert final["campaign"]["status"] == "measured"
    assert (late.treated_rows, late.control_rows, late.absolute_lift) == (
        report.treated_rows,
        report.control_rows,
        report.absolute_lift,
    )


def test_a_measurement_that_moves_the_goalposts_is_refused(world: World) -> None:
    campaign_id = _with_outcomes(world, PROPENSITY_RUN, world.propensity_outcomes)
    ok(world.client.post(f"/campaigns/{campaign_id}/plan", json=_plan_body()), 201)
    for change in ({"outcome_window_days": 60}, {"covariate_column": "tenure_months"}):
        refused = _measure(world, campaign_id, **change)
        assert refused.status_code == 409, change
        assert refused.json()["detail"]["code"] == "TEST_PLAN_CHANGED"
        assert next(iter(change)) in refused.json()["detail"]["message"]
    assert _measure(world, campaign_id).status_code == 200, "measured as planned"


def test_an_underpowered_plan_is_a_warning_never_a_block(world: World) -> None:
    campaign_id = _with_outcomes(world, PROPENSITY_RUN, world.propensity_outcomes)
    plan = TestPlan.model_validate(
        ok(world.client.post(f"/campaigns/{campaign_id}/plan", json=_plan_body(mde_pp=0.2)), 201)
    )
    assert plan.warnings == ("PLAN_UNDERPOWERED",)
    assert plan.achieved_power is not None and plan.achieved_power < 0.8
    assert _measure(world, campaign_id, as_of=datetime(2026, 8, 15, tzinfo=UTC)).status_code == 200


def test_a_plan_cannot_be_registered_after_the_result_was_read(world: World) -> None:
    campaign_id = _with_outcomes(world, PROPENSITY_RUN, world.propensity_outcomes)
    ok(_measure(world, campaign_id))
    late = world.client.post(f"/campaigns/{campaign_id}/plan", json=_plan_body())
    assert late.status_code == 409 and late.json()["detail"]["code"] == "TEST_PLAN_INVALID"
