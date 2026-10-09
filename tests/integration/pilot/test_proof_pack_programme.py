"""Plan J M104 (DEC-1314): the Value Proof Pack of a programme readout (M103's `POST /campaigns/programme`).

The universal holdout is the one a real Phase 1 scoring pass recorded (the world of
`tests/integration/measurement/test_programme_readout.py`, reused by name). A programme is engine-random, so
its pack is proven; it compares everyone with the universal control group, so it has no groups of customers to
read one by one and its backfire section says so instead of inventing one.
"""

# ruff: noqa: F811, F401 - the imported pytest fixture is used by name
from __future__ import annotations

import json

import pytest

from tests.integration.measurement.support import ok
from tests.integration.measurement.test_programme_readout import World, _body, _outcomes, world
from tests.integration.pilot.test_proof_pack import (
    VALUE_INPUTS,
    _section,
    assert_no_stray_digits,
    assert_traced,
    page_text,
)

pytestmark = pytest.mark.integration


def test_a_programme_readouts_pack_is_proven_traced_and_has_no_groups(world: World) -> None:
    outcomes, _ = _outcomes(world, seed=104)
    campaign_id = ok(world.client.post("/campaigns/programme", json=_body(world, outcomes)), 201)["campaign"][
        "campaign_id"
    ]
    groups = json.loads(world.storage.read_bytes(f"campaigns/{campaign_id}/segment_effects.json"))
    assert groups["cells"] == [] and groups["family_size"] == 0
    view = ok(world.client.get(f"/pilot/proof/{campaign_id}", params={"format": "json"}))
    assert view["kind"] == "programme" and view["claim"] == "proven"
    backfire = _section(view, "backfire")
    assert backfire["status"] == "not_measured"
    assert "has no groups of customers to read one by one" in backfire["reason"]
    assert _section(view, "plan")["status"] == "not_measured"
    assert "programme readout covers a period" in _section(view, "plan")["reason"]
    assert_traced(world.data_dir, view)
    page = page_text(world.client.get(f"/pilot/proof/{campaign_id}").text)
    assert "the effect of the whole programme" in page
    assert_no_stray_digits(world.client, campaign_id, view)


def test_a_programme_pack_never_prices_contacts_or_offers_it_did_not_measure(world: World) -> None:
    """Everyone outside the control group is the programme's side, contacted or not: nothing records who was
    contacted or took an offer, so no cost, and no net value, is drawn from that count."""
    outcomes, _ = _outcomes(world, seed=1041)
    campaign_id = ok(world.client.post("/campaigns/programme", json=_body(world, outcomes)), 201)["campaign"][
        "campaign_id"
    ]
    ok(world.client.put(f"/pilot/proof/{campaign_id}/value", json=VALUE_INPUTS))
    view = ok(world.client.get(f"/pilot/proof/{campaign_id}", params={"format": "json"}))
    net = _section(view, "net_value")
    assert net["status"] == "not_measured"
    assert "does not record who outside the control group was contacted or who took an offer" in net["reason"]
    assert "Net value" not in view["headline"]
    labels = [
        line["label"]
        for key in ("incremental", "gross", "credit", "test_cost")
        for line in _section(view, key)["lines"]
    ]
    assert not [label for label in labels if "ontacted" in label], "nobody is called contacted"
    gross = _section(view, "gross")["lines"][0]["label"]
    assert gross == "Customers outside the control group with the outcome (gross)"
    naive = _section(view, "credit")["lines"][0]["label"]
    assert naive == "Naive credit: every customer outside the control group who had the outcome"
    credit = {line["label"]: line for line in _section(view, "credit")["lines"]}
    assert credit["Measured credit in rupees"]["value"] is not None, "what it changed is still valued"
    assert_traced(world.data_dir, view)
    assert_no_stray_digits(world.client, campaign_id, view)
