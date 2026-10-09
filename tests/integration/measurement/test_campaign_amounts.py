"""Plan J M102 (DEC-1312) through the product's own API: a campaign measured on revenue, adjusted by an
amount from before the campaign that its test plan registered in advance.

The scoring run is written straight into the store from `engine.measurement.simulate.revenue_campaign`
(its scores, and a `run.json` that finished before every customer's treatment date); the outcomes file
the simulator drew - revenue, the treatment date, last period's revenue and the date it was measured up
to - is uploaded as a person would. Nothing here is a hand-written result.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine.config import ProblemType
from engine.measurement.campaign import REPORT_FILENAME, campaign_key
from engine.measurement.simulate import (
    AS_OF,
    COVARIATE_COLUMN,
    COVARIATE_DATE_COLUMN,
    REVENUE_COLUMN,
    TREATMENT_DATE_COLUMN,
    SimulatedRevenueCampaign,
    revenue_campaign,
)
from engine.pilot.plain import jargon_in
from engine.pilot.roi import RoiInputs, compute_roi
from engine.runs import RUN_FILENAME
from engine.stages import export
from engine.storage import LocalStorage, run_key
from engine.uplift.contracts import IncrementalityReport
from tests.integration.measurement.support import ok, record, upload

pytestmark = pytest.mark.integration

RUN = "r_20261009_10200001"
FINISHED = AS_OF - timedelta(days=100)
"""Before the earliest simulated treatment date (90 days before AS_OF)."""


@dataclass(frozen=True)
class World:
    client: TestClient
    storage: LocalStorage
    campaign: SimulatedRevenueCampaign
    outcomes: str


@pytest.fixture(scope="module")
def world(config_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[World]:
    logging.disable(logging.CRITICAL)
    data_dir = tmp_path_factory.mktemp("amounts") / "data"
    data_dir.mkdir()
    storage = LocalStorage(data_dir)
    campaign = revenue_campaign(20_000, 3.0, seed=102_101, rho=0.6)
    buffer = io.BytesIO()
    campaign.scores.to_parquet(buffer, index=False)
    storage.write_bytes(run_key(RUN, export.SCORES_PARQUET), buffer.getvalue())
    finished = record(RUN, problem_type=ProblemType.BINARY_CLASSIFICATION, rows=len(campaign.scores))
    storage.write_model(
        run_key(RUN, RUN_FILENAME),
        finished.model_copy(
            update={
                "created_at": FINISHED - timedelta(minutes=5),
                "started_at": FINISHED - timedelta(minutes=4),
                "finished_at": FINISHED,
            }
        ),
    )
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as client:
        yield World(client, storage, campaign, upload(client, campaign.outcomes, name="revenue.csv"))
    logging.disable(logging.NOTSET)


def _campaign(world: World, **outcomes: Any) -> str:
    created = ok(world.client.post("/campaigns", json={"run_id": RUN, "outcome_window_days": 30}), 201)
    campaign_id = str(created["campaign"]["campaign_id"])
    body = {
        "upload_id": world.outcomes,
        "outcome_column": REVENUE_COLUMN,
        "treatment_date_column": TREATMENT_DATE_COLUMN,
        **outcomes,
    }
    ok(world.client.post(f"/campaigns/{campaign_id}/outcomes", json=body))
    return campaign_id


def _plan(world: World, campaign_id: str, **extra: Any) -> dict[str, Any]:
    body = {
        "metric": "revenue in the 30 days after the campaign",
        "outcome_column": REVENUE_COLUMN,
        "outcome_kind": "continuous",
        "analysis_date": AS_OF.date().isoformat(),
        "mde_value": 2.0,
        "outcome_sd": 40.0,
        **extra,
    }
    return dict(ok(world.client.post(f"/campaigns/{campaign_id}/plan", json=body), 201))


def _measure(world: World, campaign_id: str, **extra: Any) -> Any:
    return world.client.post(f"/campaigns/{campaign_id}/measure", json={"as_of": AS_OF.isoformat(), **extra})


COVARIATE = {"covariate_column": COVARIATE_COLUMN, "covariate_date_column": COVARIATE_DATE_COLUMN}


def test_a_revenue_campaign_is_measured_adjusted_and_priced(world: World) -> None:
    campaign_id = _campaign(world, **COVARIATE)
    plan = _plan(world, campaign_id, covariate_column=COVARIATE_COLUMN, expected_rho2=0.36)
    assert plan["expected_rho2"] == 0.36 and plan["achieved_power"] is not None
    measured = ok(_measure(world, campaign_id))
    report = IncrementalityReport.model_validate(measured["report"])
    assert report.outcome_kind == "continuous" and report.test_plan_hash == plan["plan_hash"]
    assert report.adjusted_interval is not None and report.mean_difference_ci is not None
    assert report.variance_reduction is not None and abs(report.variance_reduction - 0.36) <= 0.03
    assert report.covariate_column == COVARIATE_COLUMN
    verdict = measured["verdict"]
    assert verdict is not None and verdict["kind"] in {"added", "no_clear_effect"}
    assert not jargon_in(report.summary)
    stored = world.storage.read_model(campaign_key(campaign_id, REPORT_FILENAME), IncrementalityReport)
    assert stored.adjusted_lift == report.adjusted_lift
    view = compute_roi(
        world.storage, RUN, campaign_id=campaign_id, inputs=RoiInputs(value_per_outcome=1.0, contact_cost=1.0)
    )
    assert view.campaign_id == campaign_id and view.status == "measured" and view.adjusted is True
    assert view.incremental is not None
    assert view.incremental.value == pytest.approx(report.adjusted_interval.value * report.treated_rows)


def test_a_covariate_dated_after_the_campaign_is_refused(world: World, tmp_path: Path) -> None:
    leaked = world.campaign.outcomes.copy()
    leaked[COVARIATE_DATE_COLUMN] = leaked[TREATMENT_DATE_COLUMN]  # measured on the day of contact
    upload_id = upload(world.client, leaked, name="revenue_leaked.csv")
    created = ok(world.client.post("/campaigns", json={"run_id": RUN, "outcome_window_days": 30}), 201)
    campaign_id = str(created["campaign"]["campaign_id"])
    ok(
        world.client.post(
            f"/campaigns/{campaign_id}/outcomes",
            json={
                "upload_id": upload_id,
                "outcome_column": REVENUE_COLUMN,
                "treatment_date_column": TREATMENT_DATE_COLUMN,
                **COVARIATE,
            },
        )
    )
    _plan(world, campaign_id, covariate_column=COVARIATE_COLUMN)
    refused = _measure(world, campaign_id)
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["code"] == "COVARIATE_NOT_BEFORE_CAMPAIGN"
    assert not world.storage.exists(campaign_key(campaign_id, REPORT_FILENAME))


def test_a_covariate_needs_its_date_when_the_outcomes_are_given(world: World) -> None:
    created = ok(world.client.post("/campaigns", json={"run_id": RUN}), 201)
    campaign_id = str(created["campaign"]["campaign_id"])
    refused = world.client.post(
        f"/campaigns/{campaign_id}/outcomes",
        json={
            "upload_id": world.outcomes,
            "outcome_column": REVENUE_COLUMN,
            "covariate_column": COVARIATE_COLUMN,
        },
    )
    assert refused.status_code == 422
    assert refused.json()["detail"]["code"] == "COVARIATE_NOT_BEFORE_CAMPAIGN"


def test_only_the_registered_covariate_and_kind_are_measured(world: World) -> None:
    campaign_id = _campaign(world, **COVARIATE)
    _plan(world, campaign_id)  # an amount, with no covariate registered
    adjusted = _measure(world, campaign_id, covariate_column=COVARIATE_COLUMN)
    assert adjusted.status_code == 409 and adjusted.json()["detail"]["code"] == "TEST_PLAN_CHANGED"
    as_binary = _measure(world, campaign_id, outcome_kind="binary")
    assert as_binary.status_code == 409 and "outcome_kind" in as_binary.json()["detail"]["message"]
    plain = IncrementalityReport.model_validate(ok(_measure(world, campaign_id))["report"])
    assert plain.outcome_kind == "continuous" and plain.adjusted_interval is None


def test_without_a_plan_a_covariate_is_refused_and_an_amount_is_measured(world: World) -> None:
    campaign_id = _campaign(world, **COVARIATE)
    refused = _measure(world, campaign_id, outcome_kind="continuous", covariate_column=COVARIATE_COLUMN)
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "TEST_PLAN_CHANGED"
    report = IncrementalityReport.model_validate(
        ok(_measure(world, campaign_id, outcome_kind="continuous"))["report"]
    )
    assert report.mean_difference_ci is not None and report.adjusted_interval is None


def test_step_four_measures_an_amount_and_does_not_offer_to_learn_from_it(world: World) -> None:
    body = {
        "upload_id": world.outcomes,
        "outcome_column": REVENUE_COLUMN,
        "outcome_kind": "continuous",
        "outcome_window_days": 30,
        "as_of": AS_OF.isoformat(),
    }
    view = ok(world.client.post(f"/runs/{RUN}/measure", json=body))
    report = IncrementalityReport.model_validate(view["report"])
    assert report.outcome_kind == "continuous" and report.mean_difference_ci is not None
    assert view["verdict"] is not None and view["verdict"]["kind"] != "not_enough"
    assert view["learn"]["ready"] is False and "yes/no outcome" in view["learn"]["reason"]
    learn = world.client.post(f"/runs/{RUN}/measure/learn", json={})
    assert learn.status_code == 409 and "yes/no outcome" in learn.json()["detail"]["message"]


def test_a_covariate_dated_the_day_of_contact_is_refused_without_a_date_column(world: World) -> None:
    """Review fix (DEC-1312): with no per-row treatment date, the campaign's treatment start (10:00 here)
    is the contact; a covariate dated that same day is measured up to its end, so it is refused."""
    sent = FINISHED + timedelta(hours=10)
    created = ok(
        world.client.post(
            "/campaigns",
            json={"run_id": RUN, "outcome_window_days": 30, "treatment_start": sent.isoformat()},
        ),
        201,
    )
    campaign_id = str(created["campaign"]["campaign_id"])
    same_day = world.campaign.outcomes.drop(columns=[TREATMENT_DATE_COLUMN]).assign(
        **{COVARIATE_DATE_COLUMN: sent.date().isoformat()}
    )
    upload_id = upload(world.client, same_day, name="revenue_same_day.csv")
    body = {"upload_id": upload_id, "outcome_column": REVENUE_COLUMN, **COVARIATE}
    ok(world.client.post(f"/campaigns/{campaign_id}/outcomes", json=body))
    _plan(world, campaign_id, covariate_column=COVARIATE_COLUMN)
    refused = _measure(world, campaign_id)
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["code"] == "COVARIATE_NOT_BEFORE_CAMPAIGN"
    assert not world.storage.exists(campaign_key(campaign_id, REPORT_FILENAME))


def test_a_yes_no_campaign_ignores_a_covariate_named_without_a_plan(world: World) -> None:
    """Review fix (DEC-1312): as before M102, a yes/no measurement with a covariate and no plan is measured
    (the covariate is never used on a yes/no outcome), not refused as a change of plan."""
    bought = world.campaign.outcomes.assign(
        bought=(world.campaign.outcomes[REVENUE_COLUMN] > 100).astype(int)
    )
    upload_id = upload(world.client, bought, name="bought.csv")
    created = ok(world.client.post("/campaigns", json={"run_id": RUN, "outcome_window_days": 30}), 201)
    campaign_id = str(created["campaign"]["campaign_id"])
    body = {
        "upload_id": upload_id,
        "outcome_column": "bought",
        "treatment_date_column": TREATMENT_DATE_COLUMN,
    }
    ok(world.client.post(f"/campaigns/{campaign_id}/outcomes", json=body))
    named = IncrementalityReport.model_validate(
        ok(_measure(world, campaign_id, covariate_column=COVARIATE_COLUMN))["report"]
    )
    plain = IncrementalityReport.model_validate(ok(_measure(world, campaign_id))["report"])
    assert named.outcome_kind is None and named.treated_rate is not None, "a yes/no report, as before M102"
    assert named.model_dump(exclude={"computed_at"}) == plain.model_dump(exclude={"computed_at"})


def test_the_plan_slider_of_an_amount_shows_the_change_in_the_average(world: World) -> None:
    """Review fix (DEC-1312): the slider's points of a plan on an amount are `mde_continuous` with the
    plan's spread and expected rho², the change the measurement will be able to see."""
    from engine.measurement.planner import mde_continuous

    campaign_id = _campaign(world, **COVARIATE)
    _plan(world, campaign_id, covariate_column=COVARIATE_COLUMN, expected_rho2=0.36)
    preview = ok(world.client.get(f"/campaigns/{campaign_id}/plan-preview", params={"base_rate": 0.2}))
    assert preview["reason"] is None and preview["points"] and preview["base_rate"] is None
    for point in preview["points"]:
        expected = mde_continuous(point["n_treat"], point["n_control"], 40.0, 0.05, 0.8, rho2=0.36).absolute
        assert expected is not None
        assert point["mde_pp"] is None and point["mde_amount"] == pytest.approx(round(expected, 4))
    assert "average amount" in preview["basis"] and not jargon_in(preview["basis"])
    unsized = _campaign(world)
    _plan(world, unsized, outcome_sd=None, mde_value=None)
    empty = ok(world.client.get(f"/campaigns/{unsized}/plan-preview"))
    assert empty["points"] == [] and "spread" in empty["reason"]
