"""Plan J M100 part B: the explore slice (M92) on a run that chooses the offer, under a total budget.

The same product API, catalogue, use case and planted population as
`tests/integration/decide/test_offer_choice_run.py`, with `actions.explore_fraction: 0.10` (a use-case
setting, so its own config root and its own model). The first review found that an explored customer the
choice left without an offer was given the runner-up, outside the budget, with nothing saying so: the
list could cost more than `uplift.policy.total_budget` while `offer_choice.json` reported a spend within
it, its `offer_counts` stopped matching the choice's `offered_rows`, and a customer dropped for the budget
got their second-best offer rather than their best. Checked here, exactly against the run's own files:

* an explored customer dropped for the budget is given the offer the choice preferred for them;
* one with no offer worth its cost is given the best offer they could be given (eligible, not a sleeping
  dog for it);
* `offer_counts` is the choice's `offered_rows`, the explored offers are counted apart, and their cost
  is `explore_cost`, outside the budget, with a note saying so.

The tests fail on 53af5d5.
"""

# ruff: noqa: F811, F401 - the imported pytest fixtures are used by name
from __future__ import annotations

import io
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import numpy as np
import pandas as pd
import pytest

from tests.integration.decide.test_offer_choice_run import (
    COSTS,
    LABELS,
    App,
    Runs,
    Scored,
    app,
    make_root,
    score_twice,
)
from tests.integration.uplift.test_uplift_api import run_artefact

pytestmark = pytest.mark.integration

EXPLORE_FRACTION: Final[float] = 0.10


@pytest.fixture(scope="module")
def root(config_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    target = tmp_path_factory.mktemp("offer-choice-explore-root") / "configs"
    return make_root(config_root, target, explore_fraction=EXPLORE_FRACTION)


@dataclass(frozen=True)
class Choice:
    scored: Scored
    rows: pd.DataFrame
    """`offer_choice.parquet`, indexed by customer."""
    listed: dict[str, Any]
    """`treat_list_summary.json`."""
    extra: pd.Series
    """Explored customers given an offer the choice did not give them."""


def _choice(app: App, scored: Scored) -> Choice:
    rows = pd.read_parquet(io.BytesIO(run_artefact(app, scored.run_id, "offer_choice.parquet")))
    rows = rows.astype({"customer_id": str}).set_index("customer_id").loc[scored.treat.index]
    listed = json.loads(run_artefact(app, scored.run_id, "treat_list_summary.json"))
    treat = scored.treat
    extra = (treat["treat"] == "1") & (treat["explore"] == "1") & (rows["offer_arm"] == 0)
    return Choice(scored, rows, listed, extra)


@pytest.fixture(scope="module")
def explored(app: App) -> Iterator[tuple[Runs, Choice, Choice]]:
    runs = score_twice(app, "0e1100")
    yield runs, _choice(app, runs.open), _choice(app, runs.budgeted)


def _cost(scored: Scored, arm: pd.Series) -> pd.Series:
    """Each customer's expected cost of the offer `arm` names (contact + offer x p_treated), in rupees."""
    cost = pd.Series(0.0, index=arm.index)
    for k, (contact, offer) in COSTS.items():
        mine = arm == k
        cost[mine] = contact + offer * scored.scores.loc[mine[mine].index, f"p_treated_arm_{k}"]
    return cost


def test_explored_customers_are_given_offers_outside_the_choice(
    explored: tuple[Runs, Choice, Choice],
) -> None:
    _, _, budgeted = explored
    treat = budgeted.scored.treat
    assert (treat["explore"] == "1").sum() > 100
    assert budgeted.extra.sum() > 20
    # Only customers the choice could reach: never suppressed, held back, or on a channel they refused.
    assert (budgeted.rows.loc[budgeted.extra, "explore_arm"] > 0).all()
    scores = budgeted.scored.scores
    assert not scores.loc[budgeted.extra, "control_group"].astype(bool).any()
    assert scores.loc[budgeted.extra, "suppressed_reason"].isna().all()


def test_an_explored_customer_dropped_for_the_budget_gets_their_best_offer(
    explored: tuple[Runs, Choice, Choice],
) -> None:
    _, opened, budgeted = explored
    rows, treat = budgeted.rows, budgeted.scored.treat
    dropped = budgeted.extra & (rows["offer_reason"] == "over_budget")
    assert dropped.sum() > 10
    # The run without a budget gave the same customer the offer the choice prefers; compare where
    # neither run held them back (each run draws its own control group).
    open_both = ~opened.scored.scores["control_group"].astype(bool) & ~budgeted.scored.scores[
        "control_group"
    ].astype(bool)
    compared = dropped & open_both
    assert compared.sum() > 10
    preferred = opened.rows.loc[compared, "offer_arm"]
    assert (preferred > 0).all()
    assert (rows.loc[compared, "explore_arm"] == preferred).all()
    assert (treat.loc[compared, "offer"] == preferred.map(LABELS)).all()


def test_an_explored_customer_with_no_offer_worth_its_cost_gets_the_best_one_they_can_be_given(
    explored: tuple[Runs, Choice, Choice],
) -> None:
    _, _, budgeted = explored
    rows, treat = budgeted.rows, budgeted.scored.treat
    below = budgeted.extra & (rows["offer_reason"] == "below_cost")
    assert below.sum() > 10
    assert (rows.loc[below, "explore_arm"] == rows.loc[below, "runner_up_arm"]).all()
    assert (treat.loc[below, "offer"] == rows.loc[below, "runner_up_arm"].map(LABELS)).all()
    # The offer they are given is shown as theirs, not again as the runner-up.
    assert (treat.loc[below, "runner_up_offer"] == "").all()


def test_the_budget_holds_for_the_choice_and_exploration_is_reported_apart(
    explored: tuple[Runs, Choice, Choice],
) -> None:
    runs, _, budgeted = explored
    scored, rows, listed = budgeted.scored, budgeted.rows, budgeted.listed
    summary = scored.summary
    chosen_cost = float(_cost(scored, rows["offer_arm"]).sum())
    assert chosen_cost <= runs.budget + 1e-6
    assert abs(summary["spent"] - chosen_cost) < 0.01 and summary["spent"] <= runs.budget + 1e-6
    # offer_counts are the choice's own offers, as offer_choice.json counts them.
    offered = {item["label"]: item["offered_rows"] for item in summary["arms"] if item["offered_rows"]}
    assert listed["offer_counts"] == offered
    # The explored offers are counted and priced apart, outside the budget.
    given = budgeted.extra
    counted = scored.treat.loc[given, "offer"].value_counts().to_dict()
    assert listed["explore_offer_counts"] == counted
    explore_cost = float(_cost(scored, rows.loc[given, "explore_arm"]).sum())
    assert np.isclose(listed["explore_cost"], explore_cost, atol=0.01)
    assert np.isclose(rows.loc[given, "explore_total_cost"].sum(), explore_cost, atol=0.01)
    assert "outside the campaign budget" in listed["explore_note"]
    # What the whole list costs is the choice's spend plus the explored offers'.
    treated = scored.treat["treat"] == "1"
    on_list = _cost(scored, scored.treat["offer"].where(treated, "").map({v: k for k, v in LABELS.items()}))
    assert np.isclose(float(on_list.sum()), summary["spent"] + listed["explore_cost"], atol=0.02)
