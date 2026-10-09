"""Plan J M103 through the product's own API: audit a campaign another tool ran (`POST /campaigns/audit`).

What is checked is the milestone's acceptance list, on data built by the real code (the Phase 1 scoring
stages and the engine's own simulators), never on hand-written results:

* an assignment and an outcomes file built from an existing propensity scoring run reproduce that run's
  report field for field (except `run_id`, `campaign_id` and `computed_at`), and nothing is written to the
  run store;
* a planted randomised file recovers its effect inside the interval and is labelled **Causal**, because the
  engine tried to predict who was contacted from the customer details in the file and could not;
* a propensity-assigned file (who was contacted depends on the customer's own details, and so does the
  outcome) is labelled **Descriptive only**: not causal, no verdict, a sentence that does not say "caused";
* a file random by the person's statement that carries no customer details is labelled **Random by your
  statement, not verified**;
* results not yet in are `409 CAMPAIGN_NOT_MATURED` and nothing is stored;
* several offers are each measured against the shared control; the label survives a later measurement.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine.measurement.campaign import Campaign, campaign_key
from engine.measurement.simulate import AS_OF, OUTCOME_WINDOW_DAYS, multi_arm_campaign, population
from engine.pilot.plain import jargon_in
from engine.runs import RUN_FILENAME
from engine.storage import LocalStorage, run_key
from engine.uplift.contracts import IncrementalityReport
from tests.integration.measurement.support import (
    MATURE,
    SENT,
    TARGET,
    ScoredRun,
    ok,
    propensity_run,
    upload,
)

pytestmark = pytest.mark.integration

RUN_ID = "r_20261009_10300001"
SENT_ON = (AS_OF - timedelta(days=OUTCOME_WINDOW_DAYS)).isoformat()
SIM_START = "2026-04-01T00:00:00Z"
DIFFERS_ONLY_IN = {"run_id", "campaign_id", "computed_at"}


@dataclass(frozen=True)
class World:
    client: TestClient
    storage: LocalStorage
    data_dir: Path
    propensity: ScoredRun


@pytest.fixture(scope="module")
def world(config_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[World]:
    data_dir = tmp_path_factory.mktemp("audit") / "data"
    data_dir.mkdir()
    storage = LocalStorage(data_dir)
    propensity = propensity_run(storage, RUN_ID, rows=12_000)
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as client:
        yield World(client, storage, data_dir, propensity)


def _details(count: int, seed: int) -> dict[str, Any]:
    """Customer details unrelated to anything else: what a randomly chosen group looks like to the test."""
    rng = np.random.default_rng(seed)
    return {"age": rng.integers(18, 80, count), "region": rng.choice(list("ABCD"), count)}


def _audit(world: World, who: pd.DataFrame, what: pd.DataFrame, **extra: Any) -> Any:
    """POST /campaigns/audit on two uploads (`who` was in which group, `what` happened); the response."""
    a = upload(world.client, who, name="assignment.csv")
    o = upload(world.client, what, name="outcomes.csv")
    body: dict[str, Any] = {
        "primary_key": "customer_id",
        "assignment": {"upload_id": a, "arm_column": "group"},
        "outcomes": {
            "upload_id": o,
            "outcome_column": "converted",
            "treatment_date_column": "treatment_date",
        },
        "assignment_basis": "random",
        "treatment_start": SIM_START,
        "outcome_window_days": OUTCOME_WINDOW_DAYS,
        "as_of": AS_OF.isoformat(),
    }
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(body.get(key), dict):
            body[key] = {**body[key], **value}
        else:
            body[key] = value
    return world.client.post("/campaigns/audit", json=body)


def _simulated(
    seed: int, *, n: int = 12_000, effect: float = 0.04, **options: Any
) -> tuple[Any, pd.DataFrame]:
    campaign = population(n, 0.10, effect, seed=seed, control_share=0.2, **options)
    scores = campaign.scores
    frame = pd.DataFrame(
        {
            "customer_id": scores["customer_id"],
            "group": np.where(scores["control_group"], 0, 1),
            **_details(len(scores), seed),
        }
    )
    return campaign, frame


# --- 1. an audit of a scoring run's own assignment gives the run's own report ------------------------
def test_an_audit_reproduces_a_propensity_runs_report_field_for_field(world: World) -> None:
    run = world.propensity
    outcomes_id = upload(world.client, run.outcomes, name="run_outcomes.csv")
    original = IncrementalityReport.model_validate(
        ok(
            world.client.post(
                f"/runs/{RUN_ID}/campaign-results",
                json={
                    "upload_id": outcomes_id,
                    "outcome_column": TARGET,
                    "outcome_window_days": 90,
                    "as_of": MATURE.isoformat(),
                },
            )
        )
    )
    scores = run.scores
    assignment = pd.DataFrame(
        {
            "customer_id": scores["customer_id"],
            "group": np.where(scores["control_group"], 0, 1),
            "intended": scores["suppressed_reason"].isna(),
            "winback_prob": scores["winback_prob"],
            "band": scores["band"],
        }
    )
    runs_before = sorted(world.storage.list_keys("runs/"))
    response = _audit(
        world,
        assignment,
        run.outcomes,
        assignment={"intended_column": "intended"},
        outcomes={"outcome_column": TARGET, "treatment_date_column": None},
        treatment_start=SENT.isoformat(),
        outcome_window_days=90,
        as_of=MATURE.isoformat(),
    )
    view = ok(response, 201)
    audited = IncrementalityReport.model_validate(view["report"])
    assert audited.model_dump(exclude=DIFFERS_ONLY_IN) == original.model_dump(exclude=DIFFERS_ONLY_IN)
    assert audited.run_id == audited.campaign_id == view["campaign"]["campaign_id"]
    assert view["campaign"]["kind"] == "external" and view["campaign"]["run_ids"] == []
    assert view["audit"]["label"] == "Causal" and view["audit"]["causal"] is True
    # no run record: the run store is exactly what it was
    assert sorted(world.storage.list_keys("runs/")) == runs_before
    assert not world.storage.exists(run_key(audited.run_id, RUN_FILENAME))
    assert view["verdict"] is not None


# --- 2. a planted randomised file recovers its effect ------------------------------------------------
def test_a_planted_randomised_file_recovers_its_effect_inside_the_interval(world: World) -> None:
    campaign, assignment = _simulated(10301)
    view = ok(_audit(world, assignment, campaign.outcomes), 201)
    lift = view["report"]["absolute_lift"]
    assert lift["ci_low"] <= campaign.true_itt <= lift["ci_high"], (lift, campaign.true_itt)
    assert abs(lift["value"] - 0.04) < 0.02
    audit = view["audit"]
    assert (audit["causal_basis"], audit["label"], audit["causal"]) == ("verified_random", "Causal", True)
    assert audit["randomness"]["status"] == "passed" and audit["randomness"]["auc"] <= 0.6
    assert view["campaign"]["causal"] is True and view["campaign"]["causal_basis"] == "verified_random"
    assert view["report"]["causal"] is True
    assert view["verdict"]["kind"] == "added"


# --- 3. a propensity-assigned file is not causal -----------------------------------------------------
def _targeted(seed: int, *, n: int = 12_000) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Customers contacted because they looked likely to respond: who was contacted depends on `loyalty`,
    and so does the outcome, with no effect of the contact at all."""
    rng = np.random.default_rng(seed)
    loyalty = rng.normal(size=n)
    contacted = rng.random(n) < 1.0 / (1.0 + np.exp(-2.5 * loyalty))
    responds = rng.random(n) < 1.0 / (1.0 + np.exp(-(1.2 * loyalty - 2.0)))
    keys = np.char.add("T", np.char.zfill(np.arange(n).astype(str), 7))
    assignment = pd.DataFrame(
        {
            "customer_id": keys,
            "group": contacted.astype(int),
            "loyalty": loyalty,
            "tenure": rng.integers(1, 120, n),
        }
    )
    outcomes = pd.DataFrame(
        {
            "customer_id": keys,
            "converted": responds.astype(int),
            "treatment_date": SENT_ON,
        }
    )
    return assignment, outcomes


def test_a_propensity_assigned_file_is_labelled_not_causal_even_when_called_random(world: World) -> None:
    assignment, outcomes = _targeted(10302)
    view = ok(_audit(world, assignment, outcomes), 201)
    audit = view["audit"]
    assert audit["stated_basis"] == "random"
    assert (audit["causal_basis"], audit["label"], audit["causal"]) == (
        "not_random",
        "Descriptive only",
        False,
    )
    assert audit["randomness"]["status"] == "failed" and audit["randomness"]["auc"] > 0.6
    assert "loyalty" in audit["randomness"]["signals"]
    assert view["campaign"]["causal"] is False and view["campaign"]["causal_basis"] == "not_random"
    report = view["report"]
    assert report["causal"] is False
    assert report["absolute_lift"]["value"] > 0.1, "the groups differ a lot, and nothing was caused"
    assert (
        "caused" not in report["summary"] and "does not show what the campaign changed" in report["summary"]
    )
    assert view["verdict"] is None, "no 'the campaign added N' from groups that were not random"


def test_a_person_who_says_it_was_not_random_gets_descriptive_only_without_a_test(world: World) -> None:
    campaign, assignment = _simulated(10303)
    view = ok(_audit(world, assignment, campaign.outcomes, assignment_basis="not_random"), 201)
    audit = view["audit"]
    assert audit["label"] == "Descriptive only" and audit["causal"] is False
    assert audit["randomness"]["status"] == "not_run"
    assert view["verdict"] is None and view["report"]["causal"] is False


def test_random_by_statement_alone_is_labelled_as_such_and_never_causal(world: World) -> None:
    campaign, assignment = _simulated(10304)
    bare = assignment[["customer_id", "group"]]  # no customer details to test the claim with
    view = ok(_audit(world, bare, campaign.outcomes), 201)
    audit = view["audit"]
    assert audit["label"] == "Random by your statement, not verified"
    assert audit["causal_basis"] == "declared_random" and audit["causal"] is False
    assert audit["randomness"]["status"] == "not_run" and "customer details" in audit["randomness"]["reason"]
    assert view["campaign"]["causal"] is False
    verdict = view["verdict"]
    assert verdict is not None and verdict["detail"].startswith("Random by your statement, not verified")
    assert view["report"]["summary"].startswith("Random by your statement, not verified")
    # the engine has not verified how the groups were chosen: no word of cause, in the summary or the headline
    summary, headline = view["report"]["summary"], verdict["headline"]
    assert "If the groups were chosen at random as you said" in summary
    assert not any(word in summary.lower() for word in ("caused", "added", "prevented")), summary
    assert headline.startswith("Random by your statement, not verified: about ")
    assert not headline.startswith("The campaign")
    assert not any(word in headline.lower() for word in ("caused", "added", "prevented")), headline
    # the numbers are the same ones the verified file gets
    lift = view["report"]["absolute_lift"]
    assert lift["ci_low"] <= campaign.true_itt <= lift["ci_high"]


# --- dates, the window, and results not yet in --------------------------------------------------------
def test_results_not_yet_in_are_a_409_with_a_day_and_nothing_is_stored(world: World) -> None:
    campaign, assignment = _simulated(10305, n=4_000)
    start = datetime.now(UTC) - timedelta(days=10)
    keys_before = sorted(world.storage.list_keys("campaigns/"))
    count_before = len(world.client.get("/campaigns").json()["campaigns"])
    outcomes = campaign.outcomes.drop(columns=["treatment_date"])
    response = _audit(
        world,
        assignment,
        outcomes,
        outcomes={"treatment_date_column": None},
        treatment_start=start.isoformat(),
        as_of=None,
        outcome_window_days=90,
    )
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["detail"]["code"] == "CAMPAIGN_NOT_MATURED"
    assert body["results_available_on"] == (start + timedelta(days=90)).date().isoformat()
    assert sorted(world.storage.list_keys("campaigns/")) == keys_before
    assert len(world.client.get("/campaigns").json()["campaigns"]) == count_before


def test_the_date_each_customer_was_sent_can_come_from_the_assignment_file(world: World) -> None:
    campaign, assignment = _simulated(10306, n=6_000)
    dates = campaign.outcomes.set_index("customer_id")["treatment_date"]
    assignment = assignment.assign(sent=assignment["customer_id"].map(dates))
    outcomes = campaign.outcomes.drop(columns=["treatment_date"])
    response = _audit(
        world,
        assignment,
        outcomes,
        assignment={"sent_date_column": "sent"},
        outcomes={"treatment_date_column": None},
        treatment_start=None,
    )
    view = ok(response, 201)
    expected = pd.to_datetime(dates).min().tz_localize("UTC")
    assert pd.Timestamp(view["campaign"]["treatment_start"]) == expected
    assert view["audit"]["sent_dates"] == "assignment"
    assert view["campaign"]["treatment_start_source"] == "file", "taken from the file, not entered"
    # the same customers are measured as when the dates come with the outcomes
    direct = ok(_audit(world, assignment.drop(columns=["sent"]), campaign.outcomes), 201)
    for field in (
        "treated_rows",
        "control_rows",
        "treated_conversions",
        "control_conversions",
        "rows_immature",
    ):
        assert view["report"][field] == direct["report"][field], field


def test_a_date_given_in_both_files_or_nowhere_is_refused(world: World) -> None:
    campaign, assignment = _simulated(10307, n=2_000)
    both = assignment.assign(sent=SENT_ON)
    refused = _audit(world, both, campaign.outcomes, assignment={"sent_date_column": "sent"})
    assert refused.status_code == 422 and refused.json()["detail"]["code"] == "CAMPAIGN_INVALID"
    assert "both" in refused.json()["detail"]["message"]
    nowhere = _audit(
        world,
        assignment,
        campaign.outcomes.drop(columns=["treatment_date"]),
        outcomes={"treatment_date_column": None},
        treatment_start=None,
    )
    assert nowhere.status_code == 422
    assert nowhere.json()["detail"]["path"] == "treatment_start"


# --- several offers -------------------------------------------------------------------------------------
def test_several_offers_are_each_measured_against_the_shared_control(world: World) -> None:
    sim = multi_arm_campaign(12_000, 0.10, (0.03, 0.06), seed=10308)
    scores = sim.scores
    assignment = pd.DataFrame(
        {
            "customer_id": scores["customer_id"],
            "group": np.where(scores["control_group"], sim.levels[0], scores["offer"]),
            **_details(len(scores), 10308),
        }
    )
    view = ok(
        _audit(
            world,
            assignment,
            sim.outcomes,
            assignment={"control_value": sim.levels[0], "treated_values": list(sim.levels[1:])},
        ),
        201,
    )
    arms = view["report"]["arms"]
    assert [a["arm"] for a in arms] == list(sim.levels[1:])
    assert view["audit"]["offers"] == list(sim.levels[1:]) and view["audit"]["control_level"] == sim.levels[0]
    assert view["audit"]["label"] == "Causal"
    for arm, true in zip(arms, sim.effects, strict=True):
        lift = arm["effect"]
        assert lift["ci_low"] <= true <= lift["ci_high"], arm["arm"]
    # measuring it again later keeps the offers and the label
    again = ok(world.client.post(f"/campaigns/{view['campaign']['campaign_id']}/measure", json={}))
    assert [a["arm"] for a in again["report"]["arms"]] == list(sim.levels[1:])
    assert again["audit"]["label"] == view["audit"]["label"]


def test_measuring_a_descriptive_campaign_again_keeps_it_descriptive(world: World) -> None:
    assignment, outcomes = _targeted(10309, n=6_000)
    view = ok(_audit(world, assignment, outcomes), 201)
    cid = view["campaign"]["campaign_id"]
    again = ok(world.client.post(f"/campaigns/{cid}/measure", json={"as_of": AS_OF.isoformat()}))
    assert again["report"]["causal"] is False and again["verdict"] is None
    assert again["audit"]["label"] == "Descriptive only"
    read = ok(world.client.get(f"/campaigns/{cid}"))
    assert read["report"] == again["report"] and read["audit"] == again["audit"]


# --- amounts ---------------------------------------------------------------------------------------------
def test_an_amount_is_audited_with_a_difference_in_means_and_no_adjustment(world: World) -> None:
    from engine.measurement.simulate import REVENUE_COLUMN, revenue_campaign

    sim = revenue_campaign(8_000, 5.0, seed=10310, rho=0.6)
    scores = sim.scores
    assignment = pd.DataFrame(
        {
            "customer_id": scores["customer_id"],
            "group": np.where(scores["control_group"], 0, 1),
            **_details(len(scores), 10310),
        }
    )
    outcomes = sim.outcomes[["customer_id", REVENUE_COLUMN, "treatment_date"]]
    view = ok(
        _audit(
            world,
            assignment,
            outcomes,
            outcomes={"outcome_column": REVENUE_COLUMN, "outcome_kind": "continuous"},
        ),
        201,
    )
    report = view["report"]
    assert report["outcome_kind"] == "continuous" and report["mean_difference_ci"] is not None
    assert "adjusted_interval" not in report, "an audit has no registered covariate to adjust by"
    low, high = report["mean_difference_ci"]["ci_low"], report["mean_difference_ci"]["ci_high"]
    assert low <= sim.true_effect <= high
    # "Measure now" on the campaign's page posts nothing but a date: the amount is still read as an amount
    again = ok(
        world.client.post(
            f"/campaigns/{view['campaign']['campaign_id']}/measure", json={"as_of": AS_OF.isoformat()}
        )
    )
    assert again["report"]["outcome_kind"] == "continuous"
    assert again["report"]["mean_difference_ci"] == report["mean_difference_ci"]


# --- bad input is refused in plain words ----------------------------------------------------------------
def test_an_unreadable_group_column_is_a_422_that_says_what_to_do(world: World) -> None:
    campaign, assignment = _simulated(10311, n=2_000)
    odd = assignment.assign(group=np.where(assignment["group"] == 1, "cohort-b", "cohort-a"))
    response = _audit(world, odd, campaign.outcomes)
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "AUDIT_ARM_UNREADABLE" and "control_value" in detail["message"]
    named = ok(_audit(world, odd, campaign.outcomes, assignment={"control_value": "cohort-a"}), 201)
    assert named["report"]["treated_rows"] > 0


def test_a_customer_listed_twice_is_refused(world: World) -> None:
    campaign, assignment = _simulated(10312, n=2_000)
    twice = pd.concat([assignment, assignment.head(3)], ignore_index=True)
    response = _audit(world, twice, campaign.outcomes)
    assert response.status_code == 422 and "more than once" in response.json()["detail"]["message"]


def test_a_missing_upload_and_a_future_as_of_are_refused(world: World) -> None:
    body = {
        "primary_key": "customer_id",
        "assignment": {"upload_id": "u_missing000000", "arm_column": "group"},
        "outcomes": {"upload_id": "u_missing000001", "outcome_column": "converted"},
        "assignment_basis": "random",
    }
    assert world.client.post("/campaigns/audit", json=body).status_code == 404
    later = (datetime.now(UTC) + timedelta(days=3)).isoformat()
    refused = world.client.post("/campaigns/audit", json={**body, "as_of": later})
    assert refused.status_code == 422 and refused.json()["detail"]["code"] == "CAMPAIGN_INVALID"
    assert (
        world.client.post("/campaigns/audit", json={**body, "assignment_basis": "maybe"}).status_code == 422
    )
    no_basis = {key: value for key, value in body.items() if key != "assignment_basis"}
    assert world.client.post("/campaigns/audit", json=no_basis).status_code == 422, "never assumed"


# --- what is kept, and how it reads ----------------------------------------------------------------------
def test_an_audited_campaign_keeps_aggregate_records_and_the_row_files_privacy_covers(world: World) -> None:
    campaign, assignment = _simulated(10313, n=3_000)
    view = ok(_audit(world, assignment, campaign.outcomes), 201)
    cid = view["campaign"]["campaign_id"]
    names = {key.rsplit("/", 1)[-1] for key in world.storage.list_keys(f"campaigns/{cid}/")}
    assert {"assignment.parquet", "outcomes.parquet", "audit.json", "incrementality_report.json"} <= names
    for name in ("audit.json", "campaign.json", "incrementality_report.json"):
        text = world.storage.read_bytes(campaign_key(cid, name)).decode()
        assert "C00000" not in text, f"{name} holds a customer id"
    # the customer details are used for the test and never stored
    stored = pd.read_parquet(world.storage.local_path(campaign_key(cid, "assignment.parquet")))
    assert set(stored.columns) == {"customer_id", "arm", "intended", "band"}
    got = Campaign.model_validate_json(world.storage.read_bytes(campaign_key(cid, "campaign.json")))
    assert got.kind.value == "external" and got.run_ids == ()


def test_every_sentence_the_audit_writes_is_plain(world: World) -> None:
    campaign, assignment = _simulated(10314, n=3_000)
    verified = ok(_audit(world, assignment, campaign.outcomes), 201)
    declared = ok(_audit(world, assignment[["customer_id", "group"]], campaign.outcomes), 201)
    targeted, outcomes = _targeted(10314, n=3_000)
    descriptive = ok(_audit(world, targeted, outcomes), 201)
    for view in (verified, declared, descriptive):
        sentences = [
            view["audit"]["label"],
            view["audit"]["explanation"],
            *view["audit"]["notes"],
            view["audit"]["randomness"]["reason"] or "",
            view["report"]["summary"],
            *([view["verdict"]["headline"], view["verdict"]["detail"]] if view["verdict"] else []),
        ]
        for sentence in sentences:
            assert jargon_in(sentence) == (), sentence


def test_the_list_of_campaigns_shows_an_audited_one_beside_the_rest(world: World) -> None:
    campaign, assignment = _simulated(10315, n=2_000)
    view = ok(_audit(world, assignment, campaign.outcomes, name="Spring push, run in another tool"), 201)
    listed = ok(world.client.get("/campaigns"))["campaigns"]
    found = next(c for c in listed if c["campaign_id"] == view["campaign"]["campaign_id"])
    assert found["name"] == "Spring push, run in another tool" and found["kind"] == "external"
