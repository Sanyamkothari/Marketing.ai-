"""Plan J M103 through the API: who was actually contacted (the contact file).

A contact file gives any campaign - one of our own scoring runs' or an audited one - its contact rate, the
contamination of the held-back group and, labelled secondary, the effect on the customers who were really
contacted. What is checked:

* injected contact and contamination counts are reported exactly (the file is built with known counts, and
  the readout's counts and rates are those, to the customer);
* with the simulator's own receipt (60% of the contacted group reached, 10% of the held-back ones leaked
  to) the effect on the contacted recovers the planted effect inside its interval, and the main result is
  byte-identical to the audit without a contact file;
* with almost nobody reached the effect on the contacted is withheld with its reason, never invented;
* a send log that lists only the customers it sent to is read as such when the person says so;
* the same file on one of our own scored campaigns, and again after a new measurement.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine.measurement.campaign import campaign_key
from engine.measurement.simulate import AS_OF, OUTCOME_WINDOW_DAYS, population
from engine.pilot.plain import jargon_in
from engine.storage import LocalStorage
from tests.integration.measurement.support import MATURE, ScoredRun, ok, propensity_run, upload

pytestmark = pytest.mark.integration

RUN_ID = "r_20261009_10310001"
SIM_START = "2026-04-01T00:00:00Z"


@dataclass(frozen=True)
class World:
    client: TestClient
    storage: LocalStorage
    run: ScoredRun


@pytest.fixture(scope="module")
def world(config_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[World]:
    data_dir = tmp_path_factory.mktemp("contacts") / "data"
    data_dir.mkdir()
    storage = LocalStorage(data_dir)
    run = propensity_run(storage, RUN_ID, rows=6_000)
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as client:
        yield World(client, storage, run)


def _files(sim: Any) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The assignment (id, group, two unrelated details) and the outcomes of a simulated campaign."""
    rng = np.random.default_rng(1)
    scores = sim.scores
    assignment = pd.DataFrame(
        {
            "customer_id": scores["customer_id"],
            "group": np.where(scores["control_group"], 0, 1),
            "age": rng.integers(18, 80, len(scores)),
            "region": rng.choice(list("ABCD"), len(scores)),
        }
    )
    return assignment, sim.outcomes


def _body(world: World, assignment: pd.DataFrame, outcomes: pd.DataFrame, **extra: Any) -> dict[str, Any]:
    a = upload(world.client, assignment, name="assignment.csv")
    o = upload(world.client, outcomes, name="outcomes.csv")
    return {
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
        **extra,
    }


def _contact(world: World, frame: pd.DataFrame, **extra: Any) -> dict[str, Any]:
    return {
        "upload_id": upload(world.client, frame, name="contacts.csv"),
        "contacted_column": "contacted",
        **extra,
    }


def _audit(
    world: World, sim: Any, contact_frame: pd.DataFrame | None, **contact_extra: Any
) -> dict[str, Any]:
    assignment, outcomes = _files(sim)
    body = _body(world, assignment, outcomes)
    if contact_frame is not None:
        body["contact"] = _contact(world, contact_frame, **contact_extra)
    view: dict[str, Any] = ok(world.client.post("/campaigns/audit", json=body), 201)
    return view


# --- exact counts ------------------------------------------------------------------------------------
def test_injected_contact_and_contamination_counts_are_reported_exactly(world: World) -> None:
    sim = population(10_000, 0.10, 0.04, seed=10321, control_share=0.2)
    control = sim.scores["control_group"].to_numpy()
    rng = np.random.default_rng(7)
    treated_at = np.flatnonzero(~control)
    held_at = np.flatnonzero(control)
    reached = rng.choice(treated_at, size=4_700, replace=False)  # exactly 4,700 of 8,000
    leaked = rng.choice(held_at, size=130, replace=False)  # exactly 130 of 2,000
    contacted = np.zeros(len(control), dtype=int)
    contacted[reached] = 1
    contacted[leaked] = 1
    contacts = pd.DataFrame({"customer_id": sim.scores["customer_id"], "contacted": contacted})
    view = _audit(world, sim, contacts)
    readout = view["contacts"]
    assert (readout["treated_customers"], readout["treated_listed"], readout["treated_contacted"]) == (
        8_000,
        8_000,
        4_700,
    )
    assert (readout["holdout_customers"], readout["holdout_listed"], readout["holdout_contacted"]) == (
        2_000,
        2_000,
        130,
    )
    assert readout["contact_rate"] == 4_700 / 8_000 == 0.5875
    assert readout["contamination"] == 130 / 2_000 == 0.065
    assert readout["contact_rate_reason"] is None and readout["contamination_reason"] is None
    assert readout["rate_difference"]["value"] == pytest.approx(0.5875 - 0.065)
    assert any("130 of 2,000 held-back customers (6.5%)" in note for note in readout["notes"])
    # stored with no customer id, and the file itself is one more row-level file of the campaign
    cid = view["campaign"]["campaign_id"]
    stored = world.storage.read_bytes(campaign_key(cid, "contact_readout.json")).decode()
    assert "C00000" not in stored
    assert world.storage.exists(campaign_key(cid, "contact.parquet"))


def test_the_effect_on_the_contacted_recovers_the_planted_effect_and_leaves_the_main_result_alone(
    world: World,
) -> None:
    sim = population(40_000, 0.10, 0.05, seed=10322, control_share=0.2, compliance=0.6, contamination=0.1)
    contacts = pd.DataFrame(
        {"customer_id": sim.scores["customer_id"], "contacted": sim.received_treatment.astype(int)}
    )
    with_file = _audit(world, sim, contacts)
    without = _audit(world, sim, None)
    ignore = {"run_id", "campaign_id", "computed_at"}
    assert {k: v for k, v in with_file["report"].items() if k not in ignore} == {
        k: v for k, v in without["report"].items() if k not in ignore
    }, "the main result is the same with or without the contact file"
    readout = with_file["contacts"]
    complier = readout["complier"]
    assert complier is not None and readout["complier_reason"] is None
    assert complier["secondary"] is True and complier["unit"] == "rate"
    effect = complier["effect"]
    assert effect["ci_low"] <= 0.05 <= effect["ci_high"], effect
    assert abs(effect["value"] - 0.05) < 0.02
    assert "Secondary result, not the headline" in complier["label"]
    # the main result is intent to treat: about (0.6 - 0.1) x 0.05
    lift = with_file["report"]["absolute_lift"]
    assert lift["ci_low"] <= 0.025 <= lift["ci_high"]
    first = complier["first_stage"]
    assert first["ci_low"] <= 0.5 <= first["ci_high"]
    assert complier["treated_rows"] == with_file["report"]["treated_rows"]
    assert complier["control_rows"] == with_file["report"]["control_rows"]
    for sentence in (complier["label"], *readout["notes"]):
        assert jargon_in(sentence) == (), sentence


def test_when_almost_nobody_was_reached_the_effect_on_the_contacted_is_withheld_with_its_reason(
    world: World,
) -> None:
    sim = population(6_000, 0.10, 0.05, seed=10323, control_share=0.2, compliance=0.03, contamination=0.03)
    contacts = pd.DataFrame(
        {"customer_id": sim.scores["customer_id"], "contacted": sim.received_treatment.astype(int)}
    )
    readout = _audit(world, sim, contacts)["contacts"]
    assert readout["complier"] is None
    assert "too small to tell apart from chance" in readout["complier_reason"]
    assert readout["contact_rate"] is not None and readout["contamination"] is not None
    assert any("Only" in note for note in readout["notes"]), "the low reach is said in words"


# --- a send log that lists only the customers it sent to ---------------------------------------------------
def test_a_send_log_of_the_contacted_is_read_as_such_only_when_the_person_says_so(world: World) -> None:
    sim = population(8_000, 0.10, 0.05, seed=10324, control_share=0.2, compliance=0.7, contamination=0.05)
    log = pd.DataFrame({"customer_id": sim.scores["customer_id"][sim.received_treatment], "contacted": "yes"})
    default = _audit(world, sim, log)["contacts"]
    assert default["treated_listed"] == default["treated_contacted"], "the unlisted are unknown, not counted"
    assert any("send log" in note for note in default["notes"]), "the all-contacted file is flagged"
    assert default["complier"] is None or default["complier"]["treated_rows"] < 6_400
    stated = _audit(world, sim, log, unlisted_customers="not_contacted")["contacts"]
    control = sim.scores["control_group"].to_numpy()
    assert stated["treated_listed"] == int((~control).sum()) == 6_400
    assert stated["treated_contacted"] == int((sim.received_treatment & ~control).sum())
    assert stated["contact_rate"] == stated["treated_contacted"] / 6_400
    assert stated["unlisted_customers"] == "not_contacted"
    assert stated["complier"] is not None


# --- on one of our own scored campaigns --------------------------------------------------------------------
def test_a_contact_file_on_a_scored_campaign_gives_the_wald_effect_and_survives_a_new_measurement(
    world: World,
) -> None:
    run = world.run
    outcomes_id = upload(world.client, run.outcomes, name="run_outcomes.csv")
    created = ok(world.client.post("/campaigns", json={"run_id": RUN_ID}), 201)
    cid = created["campaign"]["campaign_id"]
    ok(world.client.post(f"/campaigns/{cid}/outcomes", json={"upload_id": outcomes_id}))
    measured = ok(world.client.post(f"/campaigns/{cid}/measure", json={"as_of": MATURE.isoformat()}))
    assert "contacts" not in measured, "no contact file yet: the answer reads as it always did"

    scores = run.scores
    eligible = scores["suppressed_reason"].isna()
    everyone_sent = pd.DataFrame(
        {"customer_id": scores["customer_id"], "contacted": (eligible & ~scores["control_group"]).astype(int)}
    )
    view = ok(
        world.client.post(f"/campaigns/{cid}/contacts", json=_contact(world, everyone_sent)),
    )
    readout = view["contacts"]
    assert readout["contact_rate"] == 1.0 and readout["contamination"] == 0.0
    assert (
        readout["treated_contacted"]
        == readout["treated_listed"]
        == view["campaign"]["counts"]["intended_treated"]
    )
    complier = readout["complier"]
    assert complier is not None
    lift = view["report"]["absolute_lift"]
    assert complier["effect"]["value"] == pytest.approx(
        lift["value"], abs=1e-9
    ), "everyone reached: the same number"
    assert complier["effect"]["ci_low"] == pytest.approx(lift["ci_low"], abs=0.004)
    assert complier["effect"]["ci_high"] == pytest.approx(lift["ci_high"], abs=0.004)
    # measured again later: the readout is recomputed from the same file
    again = ok(world.client.post(f"/campaigns/{cid}/measure", json={"as_of": MATURE.isoformat()}))
    assert again["contacts"]["computed_at"] > readout["computed_at"]
    assert again["contacts"]["complier"]["effect"] == complier["effect"]


def test_a_contact_file_before_any_measurement_gives_the_rates_and_says_why_there_is_no_effect(
    world: World,
) -> None:
    created = ok(world.client.post("/campaigns", json={"run_id": RUN_ID}), 201)
    cid = created["campaign"]["campaign_id"]
    scores = world.run.scores
    frame = pd.DataFrame({"customer_id": scores["customer_id"], "contacted": ~scores["control_group"]})
    view = ok(world.client.post(f"/campaigns/{cid}/contacts", json=_contact(world, frame)))
    readout = view["contacts"]
    assert readout["contact_rate"] is not None and readout["complier"] is None
    assert "not been measured" in readout["complier_reason"]


# --- bad files ----------------------------------------------------------------------------------------------
def test_a_contact_file_that_cannot_be_read_is_a_422_in_plain_words(world: World) -> None:
    created = ok(world.client.post("/campaigns", json={"run_id": RUN_ID}), 201)
    cid = created["campaign"]["campaign_id"]
    scores = world.run.scores
    twice = pd.DataFrame({"customer_id": scores["customer_id"], "contacted": 1})
    twice = pd.concat([twice, twice.head(2)], ignore_index=True)
    response = world.client.post(f"/campaigns/{cid}/contacts", json=_contact(world, twice))
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "CONTACT_FILE_UNREADABLE"
    assert "more than once" in response.json()["detail"]["message"]
    odd = pd.DataFrame({"customer_id": scores["customer_id"], "contacted": "maybe"})
    response = world.client.post(f"/campaigns/{cid}/contacts", json=_contact(world, odd))
    assert response.status_code == 422 and "neither yes nor no" in response.json()["detail"]["message"]
    named = ok(
        world.client.post(f"/campaigns/{cid}/contacts", json=_contact(world, odd, contacted_label="maybe"))
    )
    assert named["contacts"]["treated_contacted"] == named["contacts"]["treated_listed"]
    missing = world.client.post(
        f"/campaigns/{cid}/contacts", json=_contact(world, odd, contacted_column="sent")
    )
    assert missing.status_code == 422
    unknown = world.client.post(
        "/campaigns/c_20200101_deadbeef/contacts", json={"upload_id": "u_x", "contacted_column": "c"}
    )
    assert unknown.status_code == 404 and unknown.json()["detail"]["code"] == "CAMPAIGN_NOT_FOUND"
