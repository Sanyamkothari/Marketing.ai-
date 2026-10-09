"""Plan J M105 (DEC-1315): the value proven to date keeps one total per unit, and adds no programme.

* an amount (revenue) campaign, a yes/no campaign and their value in rupees each have a total of their own: the
  amount's total is read from the adjusted interval its test plan registered, in the amount's own unit, and
  none of the three is added to another;
* a programme readout measures everyone outside the universal control group, so it covers the customers of
  the campaigns it sits beside: it is listed apart and never added, whatever its causal basis.

The worlds are the revenue campaign of `test_campaign_amounts.py` and the universal holdout of
`test_programme_readout.py`, reused by name; every campaign is measured through the API.
"""

# ruff: noqa: F811, F401 - the imported pytest fixtures are used by name
from __future__ import annotations

import json
from typing import Any

import pytest

from engine.measurement.simulate import COVARIATE_COLUMN, REVENUE_COLUMN
from tests.integration.measurement.support import ok, propensity_run
from tests.integration.measurement.test_campaign_amounts import COVARIATE, RUN as REVENUE_RUN
from tests.integration.measurement.test_campaign_amounts import World as AmountsWorld
from tests.integration.measurement.test_campaign_amounts import _campaign, _measure, _plan
from tests.integration.measurement.test_campaign_amounts import world as amounts_world
from tests.integration.measurement.test_campaign_summary import _banded_outcomes, _scored
from tests.integration.measurement.test_programme_readout import World as ProgrammeWorld
from tests.integration.measurement.test_programme_readout import _body, _outcomes
from tests.integration.measurement.test_programme_readout import world as programme_world
from tests.integration.pilot.test_proof_pack import VALUE_INPUTS, assert_traced

pytestmark = pytest.mark.integration

AMOUNT_INPUTS = {"value_per_outcome": 0.3, "contact_cost": 0.25, "offer_cost": 1.5, "outcome_is_good": True}


def test_an_amount_a_yes_no_campaign_and_rupees_are_three_totals_never_added(amounts_world: AmountsWorld) -> None:
    world = amounts_world
    revenue = _campaign(world, **COVARIATE)
    _plan(world, revenue, covariate_column=COVARIATE_COLUMN, expected_rho2=0.36)
    ok(_measure(world, revenue))
    ok(world.client.put(f"/pilot/proof/{revenue}/value", json=AMOUNT_INPUTS))
    run = propensity_run(world.storage, "r_20261010_10510001", rows=6_000)
    yes_no = _scored(world.client, "r_20261010_10510001", "Yes or no", _banded_outcomes(run.scores, seed=21))
    ok(world.client.put(f"/pilot/proof/{yes_no}/value", json=VALUE_INPUTS))

    summary = ok(world.client.get("/campaigns/summary"))
    totals = {total["unit"]: total for total in summary["proven"]["totals"]}
    assert set(totals) == {"outcomes", f"amount:{REVENUE_COLUMN}", "rupees"}

    # The amount is added in its own unit, from the adjusted interval its plan registered.
    report = json.loads(world.storage.read_bytes(f"campaigns/{revenue}/incrementality_report.json"))
    expected = report["adjusted_interval"]["ci_low"] * report["treated_rows"]
    amount = totals[f"amount:{REVENUE_COLUMN}"]
    assert amount["total"]["value"] == pytest.approx(expected)
    assert [line["campaign_id"] for line in amount["campaigns"]] == [revenue]
    assert amount["label"] == (
        f"at least {amount['total']['text']} in {REVENUE_COLUMN}, the sum of each campaign's lower bound"
    )

    # The yes/no total holds the yes/no campaign only.
    yes_no_report = json.loads(world.storage.read_bytes(f"campaigns/{yes_no}/incrementality_report.json"))
    assert [line["campaign_id"] for line in totals["outcomes"]["campaigns"]] == [yes_no]
    assert totals["outcomes"]["total"]["value"] == pytest.approx(
        yes_no_report["incremental_conversions"]["ci_low"]
    )

    # Rupees are net of what contacts and offers cost, one line per campaign that priced it.
    assert {line["campaign_id"] for line in totals["rupees"]["campaigns"]} == {revenue, yes_no}
    assert totals["rupees"]["total"]["value"] == pytest.approx(
        sum(line["lower_bound"]["value"] for line in totals["rupees"]["campaigns"])
    )
    assert_traced(world.storage.root, summary)


def test_a_programme_readout_is_listed_apart_and_never_added(programme_world: ProgrammeWorld) -> None:
    world = programme_world
    outcomes, _ = _outcomes(world, seed=1051)
    campaign_id = ok(world.client.post("/campaigns/programme", json=_body(world, outcomes)), 201)["campaign"][
        "campaign_id"
    ]
    view = ok(world.client.get("/pilot/proof/" + campaign_id, params={"format": "json"}))
    assert view["claim"] == "proven", "a programme is engine-random, so it could be added; it covers the campaigns"
    summary = ok(world.client.get("/campaigns/summary"))
    assert summary["proven"]["totals"] == []
    apart = {line["campaign_id"]: line for line in summary["proven"]["apart"]}
    assert apart[campaign_id]["kind"] == "programme"
    assert apart[campaign_id]["lower_bounds"], "what it shows is shown"
    assert "same customers" in apart[campaign_id]["reason"]
    assert_traced(world.data_dir, summary)
