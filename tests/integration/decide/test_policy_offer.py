"""DEC-1311 (af)-(aj): the offer the policy would choose without the hold-out, in a real scoring run of several offers.

The same product API, catalogue, use case and planted population as
`tests/integration/decide/test_offer_choice_run.py`; nothing writes a run artefact by hand. A run of several
offers chooses each customer's offer after the hold-out has set some customers aside, so a customer held back
has no offer in the list. A campaign compares the customers the list contacted with those it held back and must
cut both by one rule; it used to cut them by the first offer's `intended_treatment` (DEC-668 (3)), which leaves a
customer whose best offer is another one out of both arms. Checked here, against the run's own files and an
independent computation from its scores:

* `offer_choice.parquet` says, for every customer, the offer the policy would choose if nobody were held back,
  with its net value, and its existing columns are untouched;
* without a budget that is the rule's answer for everyone; under a budget the customers the run contacted are
  all intended, and the cut is the last of them in the budget walk's own order, for held-back customers too;
* the treat list gives such a held-back row the offer's net value and leaves its offer and channel empty;
* a campaign made on the single run (`POST /campaigns`, no arbitration) has the customers the policy meant to
  contact in its arms: every contacted customer in the treated arm, every held-back one the policy meant to
  contact in the hold-out arm.

Every test here fails on 943e7df, where the file has no such columns and the campaign is cut by the first offer.
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

from engine.decide.treat_list import policy_intended
from engine.measurement.campaign import ASSIGNMENT_FILENAME, campaign_key, read_frame
from tests.integration.decide.test_offer_choice_run import (
    CHANNEL,
    COSTS,
    LABELS,
    VALUE,
    App,
    Runs,
    Scored,
    _expected_money,
    _opted_in,
    app,
    make_root,
    score_twice,
)
from tests.integration.uplift.test_uplift_api import run_artefact

pytestmark = pytest.mark.integration

EXISTING_COLUMNS: Final[tuple[str, ...]] = (
    "customer_id",
    "offer_arm",
    "offer_level",
    "offer_action_id",
    "offer_label",
    "offer_channel",
    "offer_net_value",
    "offer_total_cost",
    "runner_up_arm",
    "runner_up_label",
    "runner_up_channel",
    "runner_up_net_value",
    "offer_reason",
    "explore_arm",
    "explore_label",
    "explore_channel",
    "explore_net_value",
    "explore_total_cost",
)
"""The file's columns before DEC-1311 (af), in order: the new ones sit between `offer_reason` and `explore_arm`."""
NEW_COLUMNS: Final[tuple[str, ...]] = (
    "policy_offer_arm",
    "policy_offer_label",
    "policy_offer_net_value",
    "policy_intended",
)


@pytest.fixture(scope="module")
def root(config_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    return make_root(config_root, tmp_path_factory.mktemp("policy-offer-root") / "configs")


@pytest.fixture(scope="module")
def runs(app: App) -> Runs:
    return score_twice(app, "0e1200")


@dataclass(frozen=True)
class Policy:
    scored: Scored
    rows: pd.DataFrame
    """`offer_choice.parquet`, indexed by customer."""
    held: pd.Series
    """The control group (Phase 1's per-run hold-out): kept back from every offer."""
    suppressed: pd.Series
    wanted: pd.Series
    """The offer the rule gives each customer when nothing is held back, worked out here from the scores."""
    money: pd.DataFrame
    """Each offer's net value per customer, from the scores."""
    ratio: pd.Series
    """The net value per rupee of the wanted offer (the budget walk's key)."""


def _policy(app: App, runs: Runs, scored: Scored) -> Policy:
    rows = pd.read_parquet(io.BytesIO(run_artefact(app, scored.run_id, "offer_choice.parquet")))
    rows = rows.astype({"customer_id": str}).set_index("customer_id").loc[scored.treat.index]
    scores = scored.scores.loc[scored.treat.index]
    held = scores["control_group"].astype(bool)
    reason = scores["suppressed_reason"]
    suppressed = reason.notna() & (reason != "")
    cut = runs.card.segment_thresholds.sleeping_dog_max_uplift
    sms, email = _opted_in(scored, runs.campaign)
    reach = {1: sms, 2: email}
    money = pd.DataFrame({k: _expected_money(scored, k) for k in (1, 2)})
    price = pd.DataFrame(
        {k: COSTS[k][0] + COSTS[k][1] * scores[f"p_treated_arm_{k}"].astype(float) for k in (1, 2)}
    )
    ok = pd.DataFrame(
        {
            k: reach[k] & (scores[f"uplift_arm_{k}"].astype(float) > cut) & (money[k] >= 0) & ~suppressed
            for k in (1, 2)
        }
    )
    ranked = money.where(ok, -np.inf)
    wanted = pd.Series(np.where(ok.any(axis=1), ranked.idxmax(axis=1), 0), index=money.index).astype(int)
    pick = np.maximum(wanted.to_numpy() - 1, 0)
    ratio = pd.Series(
        np.where(
            wanted > 0,
            money.to_numpy()[np.arange(len(money)), pick] / price.to_numpy()[np.arange(len(money)), pick],
            np.nan,
        ),
        index=money.index,
    )
    return Policy(scored, rows, held, suppressed, wanted, money, ratio)


def _contacted(policy: Policy) -> pd.Series:
    """Customers given an offer by the choice itself (explore offers are outside the policy)."""
    return policy.rows["offer_arm"] > 0


# ---------------------------------------------------------------------------
# The file
# ---------------------------------------------------------------------------
def test_the_file_keeps_its_columns_and_adds_the_policys_offer_before_the_explore_ones(
    app: App, runs: Runs
) -> None:
    for scored in (runs.open, runs.budgeted):
        rows = pd.read_parquet(io.BytesIO(run_artefact(app, scored.run_id, "offer_choice.parquet")))
        head, tail = EXISTING_COLUMNS[:13], EXISTING_COLUMNS[13:]
        assert tail[0] == "explore_arm" and len(tail) == 5
        # Every column keeps its name and its place from the start up to `offer_reason`, and from the end (the
        # explore columns stay the last five); the four new columns sit between them.
        assert tuple(rows.columns) == (*head, *NEW_COLUMNS, *tail)
        assert rows["policy_intended"].dtype == bool
        assert ((rows["policy_offer_arm"] > 0) == rows["policy_intended"]).all()


def test_without_a_budget_every_customer_has_the_offer_the_rule_gives_when_nobody_is_held_back(
    app: App, runs: Runs
) -> None:
    policy = _policy(app, runs, runs.open)
    rows = policy.rows
    assert policy.held.sum() > 500 and (policy.held & ~policy.suppressed).sum() > 500
    assert (rows["policy_offer_arm"] == policy.wanted).all()
    # The run gave the held-back customers nothing; the policy would have given most of them an offer.
    assert (rows.loc[policy.held, "offer_arm"] == 0).all()
    would = policy.held & ~policy.suppressed & (rows["policy_offer_arm"] > 0)
    assert would.sum() > 300
    # Its value is that offer's own net value, and its label is the offer's.
    for k in (1, 2):
        mine = would & (rows["policy_offer_arm"] == k)
        assert mine.sum() > 50, k
        assert np.allclose(rows.loc[mine, "policy_offer_net_value"], policy.money.loc[mine, k], atol=1e-6)
        assert (rows.loc[mine, "policy_offer_label"] == LABELS[k]).all()
    assert rows.loc[~would & ~(rows["policy_offer_arm"] > 0), "policy_offer_net_value"].isna().all()


def test_a_customer_the_run_gave_an_offer_has_that_offer_as_the_policys(app: App, runs: Runs) -> None:
    for scored in (runs.open, runs.budgeted):
        policy = _policy(app, runs, scored)
        given = _contacted(policy)
        assert given.sum() > 1000
        assert (policy.rows.loc[given, "policy_offer_arm"] == policy.rows.loc[given, "offer_arm"]).all()
        assert np.allclose(
            policy.rows.loc[given, "policy_offer_net_value"], policy.rows.loc[given, "offer_net_value"]
        )


def test_suppression_channel_consent_and_sleeping_dogs_still_count_for_a_held_back_customer(
    app: App, runs: Runs
) -> None:
    policy = _policy(app, runs, runs.open)
    rows = policy.rows
    assert not rows.loc[policy.suppressed, "policy_intended"].any()
    sms, email = _opted_in(runs.open, runs.campaign)
    would = rows["policy_intended"]
    assert not (would & (rows["policy_offer_arm"] == 1) & ~sms).any(), "offer A is sent only by SMS"
    assert not (would & (rows["policy_offer_arm"] == 2) & ~email).any(), "offer B is sent only by email"
    cut = runs.card.segment_thresholds.sleeping_dog_max_uplift
    for k in (1, 2):
        dog = runs.open.scores.loc[rows.index, f"uplift_arm_{k}"].astype(float) <= cut
        assert not (would & (rows["policy_offer_arm"] == k) & dog).any()
    nobody = ~sms & ~email
    assert nobody.sum() > 300 and not would[nobody].any()


def test_under_a_budget_every_contacted_customer_is_intended_and_the_cut_is_one_place_in_one_ranking(
    app: App, runs: Runs
) -> None:
    policy = _policy(app, runs, runs.budgeted)
    rows = policy.rows
    given = _contacted(policy)
    assert given.sum() > 1000 and runs.budgeted.summary["spent"] <= runs.budget + 1e-6
    assert rows.loc[given, "policy_intended"].all()
    wanted = policy.wanted > 0
    # The customers the budget turned away rank below every customer it took, held back or not.
    cut = policy.ratio[given].min()
    beyond = wanted & ~rows["policy_intended"]
    assert beyond.sum() > 500
    assert policy.ratio[beyond].max() <= cut + 1e-9
    # And a customer who ranks above the cut is intended whether or not they were held back.
    above = wanted & (policy.ratio > cut + 1e-9)
    assert rows.loc[above, "policy_intended"].all()
    assert (above & policy.held).sum() > 100, "held-back customers are cut at the same place"
    assert not (rows["policy_intended"] & ~wanted).any()
    # A run without a budget has more customers intended than the budgeted one (the budget cut is real).
    assert rows["policy_intended"].sum() < _policy(app, runs, runs.open).rows["policy_intended"].sum()


def test_the_hold_out_is_a_random_sample_of_the_customers_the_policy_meant_to_contact(
    app: App, runs: Runs
) -> None:
    """The hold-out is drawn from every eligible customer, so its share among the intended is the overall one:
    the property that makes the campaign an experiment."""
    for scored in (runs.open, runs.budgeted):
        policy = _policy(app, runs, scored)
        free = ~policy.suppressed
        overall = float(policy.held[free].mean())
        chosen = policy.rows["policy_intended"]
        share = float(policy.held[chosen].mean())
        sigma = (overall * (1 - overall) / chosen.sum()) ** 0.5
        assert abs(share - overall) < 4 * sigma, (scored.run_id, share, overall)


# ---------------------------------------------------------------------------
# The treat list
# ---------------------------------------------------------------------------
def test_a_held_back_row_carries_the_policys_net_value_and_no_offer_or_channel(app: App, runs: Runs) -> None:
    policy = _policy(app, runs, runs.open)
    treat = runs.open.treat.loc[policy.rows.index]
    would = policy.held & ~policy.suppressed & policy.rows["policy_intended"]
    assert would.sum() > 300
    assert (treat.loc[would, "treat"] == "0").all()
    assert (treat.loc[would, "offer"] == "").all() and (treat.loc[would, "channel"] == "").all()
    value = pd.to_numeric(treat["net_value"].replace("", np.nan))
    assert np.allclose(value[would], policy.rows.loc[would, "policy_offer_net_value"].round(2), atol=0.006)
    # Nobody else who is held back or turned away has a value: only the policy's intended offers are priced.
    rest = ~would & (treat["treat"] == "0")
    assert value[rest].isna().all()
    listed = json.loads(run_artefact(app, runs.open.run_id, "treat_list_summary.json"))
    assert listed["held_back_value_rows"] == int(would.sum())
    assert "not an action taken" in listed["held_back_value_note"]
    # The list's own total is of the customers it treats, not of those held back.
    treated = treat["treat"] == "1"
    assert listed["treat_rows"] == int(treated.sum())
    assert listed["net_value_total"] == pytest.approx(float(value[treated].sum()), abs=0.5)


def test_the_policy_intended_the_arbitration_reads_is_the_offer_choices(app: App, runs: Runs) -> None:
    policy = _policy(app, runs, runs.open)
    intended = policy_intended(app.storage, runs.open.run_id)
    assert intended.dtype == bool
    assert (intended.reindex(policy.rows.index) == policy.rows["policy_intended"]).all()
    first_offer = runs.open.scores.loc[policy.rows.index, "intended_treatment"].astype(bool)
    assert (intended.reindex(policy.rows.index) & ~first_offer).sum() > 300, "the first offer misses them"


# ---------------------------------------------------------------------------
# Arbitration: a held-back row competes by value, not by the order of the request
# ---------------------------------------------------------------------------
def test_a_held_back_multi_offer_row_competes_by_its_would_be_net_value_not_by_request_order(
    app: App, runs: Runs
) -> None:
    """`comparable_keys` (DEC-1311 (n)) on the multi-offer run's own treat list and a rival list derived from it
    (the same customers, the same flags, every net value scaled). A held-back multi-offer row used to carry no net
    value, so the customers held back and intended by both use cases went to whichever list came first."""
    from engine.decide.arbitrate import ArbitrationConfig, comparable_keys
    from engine.storage import run_key

    scored = runs.open
    policy = _policy(app, runs, scored)
    listed = pd.read_parquet(io.BytesIO(app.storage.read_bytes(run_key(scored.run_id, "treat_list.parquet"))))
    listed = listed.astype({"customer_id": str})
    held = policy.held.reindex(listed["customer_id"]).to_numpy(dtype=bool)
    multi = listed.assign(holdout=held, explore=False)
    flags = policy_intended(app.storage, scored.run_id)
    wanted = (multi["treat"] | (multi["holdout"] & multi["customer_id"].map(flags).fillna(False))).to_numpy()
    positive = (multi["net_value"] > 0).to_numpy()
    held_wanted = wanted & held & positive
    assert held_wanted.sum() > 300 and (wanted & ~held & positive).sum() > 1000
    keys = multi["customer_id"].to_numpy()
    for factor, winner in ((0.5, "multi"), (2.0, "rival")):
        rival = multi.assign(use_case="rival", net_value=multi["net_value"] * factor)
        for order in ((multi, rival), (rival, multi)):
            names = ["multi" if frame is multi else "rival" for frame in order]
            found = comparable_keys(list(order), [flags, flags], ArbitrationConfig(), ("customer_id",))
            scopes = dict(zip(names, found, strict=True))
            # Whoever's offer is worth more takes the customer, treated or held back, whichever list is first.
            assert set(scopes[winner]) == set(keys[wanted & positive]), (factor, names)
            assert not set(scopes["rival" if winner == "multi" else "multi"]) & set(keys[wanted & positive])
            assert set(keys[held_wanted]) <= set(scopes[winner])


# ---------------------------------------------------------------------------
# A campaign made on the single run (no arbitration)
# ---------------------------------------------------------------------------
def _campaign(app: App, run_id: str) -> tuple[dict[str, Any], pd.DataFrame]:
    response = app.client.post("/campaigns", json={"run_id": run_id})
    assert response.status_code == 201, response.text
    body = response.json()
    campaign = body["campaign"]
    assignment = read_frame(app.storage, campaign_key(campaign["campaign_id"], ASSIGNMENT_FILENAME))
    return campaign, assignment.astype({"customer_id": str}).set_index("customer_id")


def test_a_campaign_on_the_single_run_is_measured_within_the_customers_the_policy_meant_to_contact(
    app: App, runs: Runs
) -> None:
    """Item 4 of the gap: `POST /campaigns` took the first offer's `intended_treatment` too."""
    for scored in (runs.open, runs.budgeted):
        policy = _policy(app, runs, scored)
        campaign, assignment = _campaign(app, scored.run_id)
        assignment = assignment.loc[policy.rows.index]
        assert campaign["population"] == "intended" and campaign["intended_source"] == "offer_choice"
        intended = assignment["intended"]
        assert (intended == (policy.rows["policy_intended"] & ~policy.suppressed)).all()
        treat = scored.treat.loc[policy.rows.index]
        contacted = (treat["treat"] == "1") & ~(scored.treat.loc[policy.rows.index, "explore"] == "1")
        contacted &= policy.rows["offer_arm"] > 0
        # Every customer the list contacts with the policy's offer is in the treated arm...
        assert contacted.sum() > 1000
        assert (assignment.loc[contacted, "arm"] == "treated").all() and intended[contacted].all()
        # ...and every held-back customer the policy meant to contact is in the hold-out arm.
        would = policy.held & ~policy.suppressed & policy.rows["policy_intended"]
        assert would.sum() > 300
        assert (assignment.loc[would, "arm"] == "holdout").all() and intended[would].all()
        counts = campaign["counts"]
        assert counts["intended_holdout"] == int(would.sum())
        assert counts["intended_treated"] == int((intended & (assignment["arm"] == "treated")).sum())
        # Before the fix some of the contacted customers were in neither arm.
        first_offer = scored.scores.loc[policy.rows.index, "intended_treatment"].astype(bool)
        assert (contacted & ~first_offer).sum() > 100


def test_a_campaign_record_made_without_the_offer_choice_leaves_the_new_field_out() -> None:
    """A run of one offer, or a risk model, has a campaign exactly as before: nothing new in its record."""
    from engine.measurement.campaign import Campaign

    record = Campaign.model_validate(_minimal_campaign())
    assert record.intended_source is None
    assert "intended_source" not in record.model_dump(mode="json")
    chosen = Campaign.model_validate({**_minimal_campaign(), "intended_source": "offer_choice"})
    assert chosen.model_dump(mode="json")["intended_source"] == "offer_choice"


def _minimal_campaign() -> dict[str, Any]:
    from datetime import UTC, datetime

    now = datetime(2026, 10, 9, tzinfo=UTC)
    return {
        "campaign_id": "c_20261009_00000000",
        "kind": "scored",
        "name": "x",
        "primary_key": "customer_id",
        "treatment_start": now,
        "treatment_start_source": "run_finished",
        "population": "intended",
        "causal": False,
        "causal_basis": "not_random",
        "counts": {
            "rows": 0,
            "suppressed": 0,
            "treated": 0,
            "holdout": 0,
            "intended": 0,
            "intended_treated": 0,
            "intended_holdout": 0,
        },
        "status": "live",
        "created_at": now,
        "created_by": "test",
    }
