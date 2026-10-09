"""Plan J M104 (DEC-1314): the Value Proof Pack of a campaign measured on an amount (M102, DEC-1312).

The campaign is the revenue campaign of `tests/integration/measurement/test_campaign_amounts.py` (its world
reused by name): measured through the API on revenue, adjusted by last period's revenue, which its test plan
registered before the outcomes were read. What is checked is that the pack's rupees rest on the adjusted
interval the report gives, that every number is traced and printed only from a figure, and that a value of
less than a rupee per unit of the amount is printed with its paise, never rounded to ₹0.
"""

# ruff: noqa: F811, F401 - the imported pytest fixture is used by name
from __future__ import annotations

import json
from typing import Any

import pytest

from engine.measurement.simulate import COVARIATE_COLUMN
from tests.integration.measurement.support import ok
from tests.integration.measurement.test_campaign_amounts import (
    COVARIATE,
    World,
    _campaign,
    _measure,
    _plan,
    world,
)
from tests.integration.pilot.test_proof_pack import _section, assert_no_stray_digits, assert_traced

pytestmark = pytest.mark.integration

AMOUNT_INPUTS = {"value_per_outcome": 0.3, "contact_cost": 0.25, "offer_cost": 1.5, "outcome_is_good": True}
"""Thirty paise of margin per rupee of revenue, a quarter-rupee contact, a ₹1.50 offer."""


def _line(view: dict[str, Any], key: str, label: str) -> dict[str, Any]:
    return next(line for line in _section(view, key)["lines"] if line["label"] == label)


def test_an_amount_campaigns_net_value_rests_on_the_adjusted_interval(world: World) -> None:
    campaign_id = _campaign(world, **COVARIATE)
    _plan(world, campaign_id, covariate_column=COVARIATE_COLUMN, expected_rho2=0.36)
    ok(_measure(world, campaign_id))
    ok(world.client.put(f"/pilot/proof/{campaign_id}/value", json=AMOUNT_INPUTS))
    view = ok(world.client.get(f"/pilot/proof/{campaign_id}", params={"format": "json"}))
    report = json.loads(world.storage.read_bytes(f"campaigns/{campaign_id}/incrementality_report.json"))
    campaign = json.loads(world.storage.read_bytes(f"campaigns/{campaign_id}/campaign.json"))
    assert view["outcome_kind"] == "continuous" and view["claim"] == "proven"
    adjusted = report["adjusted_interval"]
    assert adjusted is not None

    # The difference per customer is the adjusted one, read from the report's own adjusted interval.
    difference = _line(
        view,
        "incremental",
        "Difference per contacted customer (adjusted for the earlier amount registered in the plan)",
    )
    assert difference["value"]["sources"][0]["field"] == "adjusted_interval.value"
    assert difference["low"]["value"] == adjusted["ci_low"]

    # Net value: the adjusted range times the contacted customers times the value of one unit, less costs.
    rows, value = report["treated_rows"], AMOUNT_INPUTS["value_per_outcome"]
    spent = (
        campaign["counts"]["intended_treated"] * AMOUNT_INPUTS["contact_cost"]
        + report["treated_conversions"] * AMOUNT_INPUTS["offer_cost"]
    )
    net = _line(view, "net_value", "Net value")
    assert net["low"]["value"] == pytest.approx(adjusted["ci_low"] * rows * value - spent)
    assert net["high"]["value"] == pytest.approx(adjusted["ci_high"] * rows * value - spent)
    assert net["value"]["value"] == pytest.approx(adjusted["value"] * rows * value - spent)
    assert any(source["field"] == "adjusted_interval.ci_low" for source in net["low"]["sources"])

    # The value of one unit of the amount keeps its paise and says what it is the value of.
    unit = _line(view, "net_value", "Value of one unit of the amount, as entered")
    assert unit["value"]["text"] == "₹0.30"
    assert _line(view, "net_value", "Cost of one contact, as entered")["value"]["text"] == "₹0.25"
    assert not [
        line
        for line in _section(view, "net_value")["lines"]
        if line["label"].startswith("Value of one extra outcome")
    ]

    # Naive credit is the whole amount of the contacted customers; the method names the adjustment.
    naive = _line(view, "credit", "Naive credit: the whole amount of every contacted customer")
    assert naive["value"]["value"] == pytest.approx(report["treated_mean"] * rows)
    method = {line["label"]: line for line in _section(view, "method")["lines"]}
    assert method["Adjusted for each customer's earlier amount in"]["value"]["value"] == COVARIATE_COLUMN
    assert (
        method["Share of the noise the adjustment removed"]["value"]["value"] == report["variance_reduction"]
    )

    assert_traced(world.storage.root, view)
    assert_no_stray_digits(world.client, campaign_id, view)
