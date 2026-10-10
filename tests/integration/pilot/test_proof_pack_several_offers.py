"""Plan J M104 fix (DEC-1314 (r)-(w)): a Value Proof Pack of several offers adds every offer, and charges every offer.

M100 keeps the first offer in a report's own fields (DEC-668 (3)), and the Pack used to read those fields for its
headline, "Extra outcomes", the value of what the campaign changed and the net value, while its cost of contacts
counted every offer's contacts (the Hillstrom audit's finding, DEC-1322 (g)). Every money line now covers the same
scope: every offer together against the shared control, read from the measurement's own combined comparison
(`incrementality_report.json` `offers_combined`), with its interval, and every offer's contacts and offers taken.

The campaigns are built through the product's own API (`POST /uploads`, `POST /campaigns/audit`,
`PUT /pilot/proof/{id}/value`, `GET /pilot/proof/{id}`) on a planted file of three offers and a shared control;
nothing is written by hand except, in the one test that says so, removing the combined comparison from a stored
report to stand for a report measured before it existed.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine.measurement.simulate import AS_OF, OUTCOME_WINDOW_DAYS, multi_arm_campaign
from engine.storage import LocalStorage
from tests.integration.measurement.support import ok, upload
from tests.integration.pilot.test_proof_pack import _section, assert_no_stray_digits, assert_traced

pytestmark = pytest.mark.integration

VALUE_INPUTS = {"value_per_outcome": 2000.0, "offer_cost": 150.0, "contact_cost": 1.5}
EFFECTS = (0.02, 0.04, 0.06)
"""Three offers, planted to add two, four and six points to an eight-point base rate."""


@dataclass(frozen=True)
class World:
    client: TestClient
    storage: LocalStorage
    data_dir: Path


@pytest.fixture(scope="module")
def world(config_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[World]:
    data_dir = tmp_path_factory.mktemp("proof-offers") / "data"
    data_dir.mkdir()
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as client:
        yield World(client, LocalStorage(data_dir), data_dir)


def _three_offers(world: World, *, seed: int) -> str:
    """`POST /campaigns/audit` of a planted file: three offers and one shared control, verified random."""
    sim = multi_arm_campaign(12_000, 0.08, EFFECTS, seed=seed, control_share=0.25)
    rng = np.random.default_rng(seed)
    frame = pd.DataFrame(
        {
            "customer_id": sim.scores["customer_id"],
            "group": np.where(sim.scores["control_group"], sim.levels[0], sim.scores["offer"]),
            "age": rng.integers(18, 80, len(sim.scores)),
        }
    )
    who = upload(world.client, frame, name="assignment.csv")
    what = upload(world.client, sim.outcomes, name="outcomes.csv")
    body = {
        "primary_key": "customer_id",
        "assignment": {
            "upload_id": who,
            "arm_column": "group",
            "control_value": sim.levels[0],
            "treated_values": list(sim.levels[1:]),
        },
        "outcomes": {
            "upload_id": what,
            "outcome_column": "converted",
            "treatment_date_column": "treatment_date",
        },
        "assignment_basis": "random",
        "treatment_start": "2026-04-01T00:00:00Z",
        "outcome_window_days": OUTCOME_WINDOW_DAYS,
        "as_of": AS_OF.isoformat(),
    }
    campaign_id = str(ok(world.client.post("/campaigns/audit", json=body), 201)["campaign"]["campaign_id"])
    ok(world.client.put(f"/pilot/proof/{campaign_id}/value", json=VALUE_INPUTS))
    return campaign_id


@pytest.fixture(scope="module")
def combined(world: World) -> str:
    return _three_offers(world, seed=1104)


def _view(world: World, campaign_id: str) -> dict[str, Any]:
    return dict(ok(world.client.get(f"/pilot/proof/{campaign_id}", params={"format": "json"})))


def _stored(world: World, campaign_id: str, name: str) -> dict[str, Any]:
    return dict(json.loads(world.storage.read_bytes(f"campaigns/{campaign_id}/{name}")))


def _line(view: dict[str, Any], key: str, starts: str) -> dict[str, Any]:
    return next(line for line in _section(view, key)["lines"] if line["label"].startswith(starts))


def test_the_measurement_adds_every_offer_against_the_shared_control_once(
    world: World, combined: str
) -> None:
    report = _stored(world, combined, "incrementality_report.json")
    arms = report["arms"]
    assert len(arms) == 3
    whole = report["offers_combined"]
    assert whole["offers"] == [arm["arm"] for arm in arms]
    assert whole["control"] == arms[0]["control"] and whole["method"] == "pooled"
    # Every contacted customer of every offer, and the one shared held-back group counted once.
    assert whole["treated_rows"] == sum(arm["treated_rows"] for arm in arms)
    assert whole["treated_conversions"] == sum(arm["treated_conversions"] for arm in arms)
    assert whole["control_rows"] == arms[0]["control_rows"] == report["control_rows"]
    # The pooled difference times every contacted customer is exactly the sum of the offers' own extra outcomes.
    extra = whole["incremental_conversions"]
    assert extra["value"] == pytest.approx(sum(arm["incremental_conversions"]["value"] for arm in arms))
    assert extra["ci_low"] < extra["value"] < extra["ci_high"]
    assert extra["ci_low"] > arms[0]["incremental_conversions"]["value"], "the planted offers add up"


def test_the_headline_and_extra_outcomes_add_every_offer(world: World, combined: str) -> None:
    view = _view(world, combined)
    report = _stored(world, combined, "incrementality_report.json")
    whole = report["offers_combined"]["incremental_conversions"]
    gain = _section(view, "incremental")["lines"][-1]
    assert gain["label"] == "Extra outcomes because of the campaign, every offer together"
    assert gain["value"]["value"] == pytest.approx(whole["value"])
    assert (gain["low"]["value"], gain["high"]["value"]) == (whole["ci_low"], whole["ci_high"])
    assert gain["value"]["sources"][0]["field"] == "offers_combined.incremental_conversions.value"
    assert gain["value"]["value"] > report["incremental_conversions"]["value"], "more than the first offer's"
    assert view["headline"].startswith(
        f"Extra outcomes because of the campaign, every offer together: {gain['value']['text']} "
        f"(likely {gain['low']['text']} to {gain['high']['text']})."
    )
    # Gross and naive credit count every contacted customer, the same scope as the measured credit.
    gross = _line(view, "gross", "Contacted customers with the outcome (gross)")
    assert gross["value"]["value"] == report["offers_combined"]["treated_conversions"]
    naive = _line(view, "credit", "Naive credit: every contacted customer")
    assert naive["value"]["value"] == report["offers_combined"]["treated_conversions"]
    assert any("every offer together" in note for note in _section(view, "incremental")["notes"])


def test_the_net_value_values_every_offer_and_charges_every_offers_contacts_and_offers(
    world: World, combined: str
) -> None:
    view = _view(world, combined)
    report = _stored(world, combined, "incrementality_report.json")
    campaign = _stored(world, combined, "campaign.json")
    whole = report["offers_combined"]
    net = _section(view, "net_value")
    assert net["status"] == "measured"
    lines = {line["label"]: line for line in net["lines"]}
    value = lines["Value of what the campaign changed, every offer together"]
    extra = whole["incremental_conversions"]
    for end, field in (("value", "value"), ("low", "ci_low"), ("high", "ci_high")):
        assert value[end]["value"] == pytest.approx(extra[field] * 2000.0)
    # Every e-mail of every offer is paid for, and every offer taken by a contacted customer of any offer.
    paid = campaign["counts"]["intended_treated"]
    assert paid == sum(arm["treated_rows"] for arm in report["arms"]) == whole["treated_rows"]
    contacts = lines["Cost of contacts, for every customer meant to be contacted"]
    assert contacts["value"]["value"] == pytest.approx(paid * 1.5)
    offers = lines["Cost of offers taken, by the contacted customers measured"]
    assert offers["value"]["value"] == pytest.approx(whole["treated_conversions"] * 150.0)
    spent = paid * 1.5 + whole["treated_conversions"] * 150.0
    line = lines["Net value"]
    assert line["low"]["value"] == pytest.approx(extra["ci_low"] * 2000.0 - spent)
    assert line["high"]["value"] == pytest.approx(extra["ci_high"] * 2000.0 - spent)
    assert view["headline"].endswith(f"Net value {line['low']['text']} to {line['high']['text']}.")
    assert any("every offer together" in note for note in net["notes"])


def test_the_held_back_customers_forgone_gain_says_it_assumes_the_contacted_mix(
    world: World, combined: str
) -> None:
    """The pooled lift weighs each offer by its share of the contacted customers, so the held-back customers'
    forgone gain is what they would have added given the offers in that same mix, and the label says so."""
    view = _view(world, combined)
    report = _stored(world, combined, "incrementality_report.json")
    whole = report["offers_combined"]
    forgone = _line(view, "test_cost", "What they would have added")
    assert forgone["label"] == (
        "What they would have added had they been given the offers in the same mix as the contacted customers"
    )
    assert forgone["value"]["value"] == pytest.approx(whole["control_rows"] * whole["absolute_lift"]["value"])
    assert forgone["value"]["sources"][0]["field"].startswith("offers_combined.")


def test_every_number_of_a_pack_of_several_offers_is_traced(world: World, combined: str) -> None:
    view = _view(world, combined)
    assert view["claim"] == "proven"
    assert_traced(world.data_dir, view)
    assert_no_stray_digits(world.client, combined, view)


def test_the_value_proven_to_date_counts_every_offers_lower_bound(world: World, combined: str) -> None:
    report = _stored(world, combined, "incrementality_report.json")
    summary = ok(world.client.get("/campaigns/summary"))
    bounds = [
        line["lower_bound"]["value"]
        for total in summary["proven"]["totals"]
        if total["unit"].startswith("outcomes:")
        for line in total["campaigns"]
        if line["campaign_id"] == combined
    ]
    assert bounds == [pytest.approx(report["offers_combined"]["incremental_conversions"]["ci_low"])]


def test_a_report_measured_before_the_offers_were_added_together_states_its_scope(world: World) -> None:
    """A report of several offers without `offers_combined` (measured before it existed) is never summed across
    offers: the headline, the extra outcomes and the costs are the first offer's, and say so."""
    campaign_id = _three_offers(world, seed=2104)
    key = f"campaigns/{campaign_id}/incrementality_report.json"
    report = json.loads(world.storage.read_bytes(key))
    report.pop("offers_combined", None)  # stands for a report written before the combined comparison existed
    world.storage.write_bytes(key, json.dumps(report, indent=2).encode())
    view = _view(world, campaign_id)
    first = report["arms"][0]
    gain = _section(view, "incremental")["lines"][-1]
    assert gain["label"] == "Extra outcomes because of the first offer alone"
    assert gain["value"]["value"] == pytest.approx(first["incremental_conversions"]["value"])
    assert view["headline"].startswith("Extra outcomes because of the first offer alone:")
    assert "not added in" in view["headline"]
    lines = {line["label"]: line for line in _section(view, "net_value")["lines"]}
    contacts = lines["Cost of contacts, for the customers given the first offer measured"]
    assert contacts["value"]["value"] == pytest.approx(first["treated_rows"] * 1.5)
    offers = lines["Cost of offers taken, by the customers given the first offer measured"]
    assert offers["value"]["value"] == pytest.approx(first["treated_conversions"] * 150.0)
    assert "Value of what the first offer alone changed" in lines
    for figure_line in (gain, contacts, offers, lines["Net value"]):
        for end in ("value", "low", "high"):
            figure = figure_line.get(end)
            for source in (figure or {}).get("sources", ()):
                assert not source["field"].startswith("arms.1") and not source["field"].startswith("arms.2")
    assert_traced(world.data_dir, view)
    assert_no_stray_digits(world.client, campaign_id, view)
