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
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine.measurement.simulate import COVARIATE_COLUMN, REVENUE_COLUMN
from tests.integration.measurement.support import ok, propensity_run
from tests.integration.measurement.test_campaign_amounts import COVARIATE, _campaign, _measure, _plan
from tests.integration.measurement.test_campaign_amounts import RUN as REVENUE_RUN
from tests.integration.measurement.test_campaign_amounts import World as AmountsWorld
from tests.integration.measurement.test_campaign_amounts import world as amounts_world
from tests.integration.measurement.test_campaign_summary import _scored, _simulated_audit
from tests.integration.measurement.test_programme_readout import World as ProgrammeWorld
from tests.integration.measurement.test_programme_readout import _body, _outcomes
from tests.integration.measurement.test_programme_readout import world as programme_world
from tests.integration.pilot.test_proof_pack import VALUE_INPUTS, _banded_outcomes, assert_traced

pytestmark = pytest.mark.integration

AMOUNT_INPUTS = {"value_per_outcome": 0.3, "contact_cost": 0.25, "offer_cost": 1.5, "outcome_is_good": True}


def test_an_amount_a_yes_no_campaign_and_rupees_are_three_totals_never_added(
    amounts_world: AmountsWorld,
) -> None:
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
    assert set(totals) == {"outcomes:converted", f"amount:{REVENUE_COLUMN}", "rupees"}

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
    assert [line["campaign_id"] for line in totals["outcomes:converted"]["campaigns"]] == [yes_no]
    assert totals["outcomes:converted"]["total"]["value"] == pytest.approx(
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
    assert (
        view["claim"] == "proven"
    ), "a programme is engine-random, so it could be added; it covers the campaigns"
    summary = ok(world.client.get("/campaigns/summary"))
    assert summary["proven"]["totals"] == []
    apart = {line["campaign_id"]: line for line in summary["proven"]["apart"]}
    assert apart[campaign_id]["kind"] == "programme"
    assert apart[campaign_id]["lower_bounds"], "what it shows is shown"
    assert "same customers" in apart[campaign_id]["reason"]
    assert_traced(world.data_dir, summary)


def test_yes_no_outcomes_of_different_columns_are_never_summed(config_root: Path, tmp_path: Path) -> None:
    """A win-back's reactivations and a bank's deposits are different outcomes: each column has its own total."""
    with TestClient(create_app(config_root=config_root, data_dir=tmp_path / "data")) as client:
        reactivated = _simulated_audit(
            client, "Win-back", 10511, contamination=0.0, outcome_column="reactivated"
        )
        deposits = _simulated_audit(
            client, "Deposits", 10512, contamination=0.0, outcome_column="deposit_made"
        )
        again = _simulated_audit(
            client, "Win-back again", 10513, contamination=0.0, outcome_column="reactivated"
        )
        summary = ok(client.get("/campaigns/summary"))
        totals = {total["unit"]: total for total in summary["proven"]["totals"]}
        assert set(totals) == {"outcomes:reactivated", "outcomes:deposit_made"}, "two columns, two totals"
        assert [total["unit"] for total in summary["proven"]["totals"]] == [
            "outcomes:deposit_made",
            "outcomes:reactivated",
        ], "in a stable order"
        assert {line["campaign_id"] for line in totals["outcomes:reactivated"]["campaigns"]} == {
            reactivated,
            again,
        }
        assert [line["campaign_id"] for line in totals["outcomes:deposit_made"]["campaigns"]] == [deposits]
        for column, total in (
            ("reactivated", totals["outcomes:reactivated"]),
            ("deposit_made", totals["outcomes:deposit_made"]),
        ):
            assert total["unit_column"]["text"] == column
            assert total["label"] == (
                f"at least {total['total']['text']} extra {column} outcomes, the sum of each campaign's lower bound"
            )
            assert total["total"]["value"] == pytest.approx(
                sum(line["lower_bound"]["value"] for line in total["campaigns"])
            )
        assert_traced(tmp_path / "data", summary)
