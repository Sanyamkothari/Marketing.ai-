"""Plan J M100 part B acceptance: the offer is chosen inside a real scoring run of several offers.

Everything here goes through the product's own API, as a user's files would (the real upload, checks,
uplift train flow and score flow, and the treat list route); nothing writes a run artefact by hand. The
config root is a copy of `configs/` with:

* an action catalogue (`decide/catalogue.yaml`) of two actions: offer A sent only by SMS (cheap) and
  offer B sent only by email (an expensive offer);
* `win-back-campaign` as a campaign-effect use case of three levels (`none`, `offer_a`, `offer_b`), each
  offer mapped to its catalogue action by `uplift.policy.arm_action_ids`, a value per response, and
  per-channel consent columns (`actions.suppression.channels`: `sms_opt_in`, `email_opt_in`).

The API is started with `create_app(config_root=...)` and **no** `MARKETING_AI_CONFIG_DIR`, so the run
must take the catalogue it is checked against from that root (the M99 gap DEC-1309 left open).

The population (`tests/fixtures/decide/offer_population.py`) plants who answers which offer. Checked
against the planted truth, and exactly against the run's own files: each segment gets its responding
offer; a sleeping dog never gets an offer it is a sleeping dog for, and one for every offer gets none;
nobody gets an offer on a channel they are not contactable on, and a customer opted out of offer A's
only channel gets offer B when B pays, else no offer; the runner-up's value is the other offer's net
value; a total budget holds. Every test here fails on 91cc0b4, where the run chooses no offer.
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import numpy as np
import pandas as pd
import pytest
import yaml
from fastapi.testclient import TestClient

from api.main import create_app
from engine.contracts import RunRecord
from engine.uplift.contracts import UpliftModelCard
from engine.uplift.flow import model_card_key
from tests.fixtures.decide.offer_population import LEVELS, OfferPopulation, offer_population
from tests.integration.uplift.test_uplift_api import (
    FAST_OVERRIDES,
    USE_CASE,
    App,
    finish,
    run_artefact,
    start_score,
    start_uplift,
    upload,
)

pytestmark = pytest.mark.integration

TARGET: Final[str] = "reactivated_90d"
TREATMENT: Final[str] = "offer"
TRAIN_ROWS: Final[int] = 24_000
SCORE_ROWS: Final[int] = 9_000
VALUE: Final[float] = 1000.0
CATALOGUE: Final[str] = """\
# Part B's test catalogue: offer A by SMS only, offer B by email only.
actions:
  - action_id: offer_a_sms
    label: Offer A by SMS
    channels: [sms]
    offer_cost: 10.0
    contact_cost: 0.5
  - action_id: offer_b_email
    label: Offer B by email
    channels: [email]
    offer_cost: 300.0
    contact_cost: 0.25
"""
COSTS: Final[dict[int, tuple[float, float]]] = {1: (0.5, 10.0), 2: (0.25, 300.0)}
"""Per offer position: (contact cost, offer cost), as the catalogue above gives them."""
LABELS: Final[dict[int, str]] = {1: "Offer A by SMS", 2: "Offer B by email"}
CHANNEL: Final[dict[int, str]] = {1: "sms", 2: "email"}


@pytest.fixture(scope="module")
def root(config_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    target = tmp_path_factory.mktemp("offer-choice-root") / "configs"
    shutil.copytree(config_root, target)
    assert not (target / "decide" / "catalogue.yaml").exists(), "the repository ships no catalogue"
    (target / "decide" / "catalogue.yaml").write_text(CATALOGUE, encoding="utf-8")
    path = target / "use_cases" / "win_back_campaign.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["problem_type"] = "uplift"
    document["model_search"] = {"metric": "auuc", "metric_choices": ["auuc"]}
    uplift = document.setdefault("uplift", {})
    uplift["treatment_levels"] = list(LEVELS)
    uplift.setdefault("policy", {}).update(
        {
            "value_per_conversion": VALUE,
            "arm_action_ids": {"offer_a": "offer_a_sms", "offer_b": "offer_b_email"},
        }
    )
    document.setdefault("actions", {}).setdefault("suppression", {})["channels"] = {
        "sms": {"consent_column": "sms_opt_in"},
        "email": {"consent_column": "email_opt_in"},
    }
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return target


@pytest.fixture(scope="module")
def app(root: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[App]:
    data_dir = tmp_path_factory.mktemp("offer-choice") / "data"
    data_dir.mkdir()
    with pytest.MonkeyPatch.context() as patch:
        patch.delenv("MARKETING_AI_CONFIG_DIR", raising=False)  # the root is the app's own, only
        with TestClient(create_app(config_root=root, data_dir=data_dir)) as client:
            yield App(client=client, data_dir=data_dir)


@dataclass(frozen=True)
class Scored:
    run_id: str
    record: RunRecord
    scores: pd.DataFrame
    """`scores.parquet`, indexed by customer."""
    treat: pd.DataFrame
    """`treat_list.csv` as text, indexed by customer."""
    summary: dict[str, Any]
    """`offer_choice.json`."""


@dataclass(frozen=True)
class Runs:
    train: RunRecord
    card: UpliftModelCard
    campaign: OfferPopulation
    open: Scored
    """No budget."""
    budgeted: Scored
    budget: float


def _score(app: App, campaign: OfferPopulation, train: RunRecord, run_id: str, **overrides: Any) -> Scored:
    frame = campaign.frame.drop(columns=[TARGET, TREATMENT])
    body: dict[str, Any] = {
        "upload_id": upload(app, frame, mode="score"),
        "primary_key": "customer_id",
        "model_version_id": train.model_version_id,
    }
    if overrides:
        body["overrides"] = overrides
    record = finish(app, start_score(app, body, run_id=run_id))
    scores = pd.read_parquet(io.BytesIO(run_artefact(app, run_id, "scores.parquet")))
    text = run_artefact(app, run_id, "treat_list.csv").decode("utf-8")
    treat = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)
    summary = json.loads(run_artefact(app, run_id, "offer_choice.json"))
    return Scored(
        run_id,
        record,
        scores.astype({"customer_id": str}).set_index("customer_id"),
        treat.set_index("customer_id"),
        summary,
    )


@pytest.fixture(scope="module")
def runs(app: App) -> Runs:
    planted = offer_population(TRAIN_ROWS, seed=51)
    overrides = {key: dict(value) for key, value in FAST_OVERRIDES.items()}
    overrides["uplift"] = {**overrides["uplift"], "bootstrap_samples": 100}
    body = {
        "use_case": USE_CASE,
        "upload_id": upload(app, planted.frame, mode="train"),
        "primary_key": "customer_id",
        "target": TARGET,
        "treatment_column": TREATMENT,
        "overrides": overrides,
    }
    train = finish(app, start_uplift(app, body, run_id="r_20261008_0e100001"))
    version = app.registry.get(train.model_version_id or "")
    card = app.storage.read_model(model_card_key(version.predictor_key), UpliftModelCard)
    campaign = offer_population(SCORE_ROWS, seed=52)
    opened = _score(app, campaign, train, "r_20261008_0e100002")
    budget = round(0.4 * float(opened.summary["spent"]), 2)
    budgeted = _score(
        app, campaign, train, "r_20261008_0e100003", uplift={"policy": {"total_budget": budget}}
    )
    return Runs(train, card, campaign, opened, budgeted, budget)


# ---------------------------------------------------------------------------
# What the run uses: the catalogue it was checked against, and no consent column as a model input
# ---------------------------------------------------------------------------
def test_the_catalogue_stamped_is_the_catalogue_checked(app: App, root: Path, runs: Runs) -> None:
    """DEC-1309's open gap: `create_app(config_root=X)` without `MARKETING_AI_CONFIG_DIR` checked the
    use case's action ids against X but stamped (and priced) from the checkout's `configs/`, which has
    no catalogue. The run now stamps X's."""
    stamp = json.loads(run_artefact(app, runs.open.run_id, "catalogue_stamp.json"))
    assert stamp["catalogue_sha256"] == hashlib.sha256(CATALOGUE.encode("utf-8")).hexdigest()
    assert stamp["planned_channels"] == {"offer_a_sms": ["sms"], "offer_b_email": ["email"]}
    assert runs.open.summary["catalogue_sha256"] == stamp["catalogue_sha256"]


def test_a_channel_consent_column_is_never_a_model_input(runs: Runs) -> None:
    """DEC-1309's open gap: the training file carries `sms_opt_in` and `email_opt_in`, which the use case
    names as channel consent columns; they are left out of the model like the consent column."""
    assert "offer_affinity" in runs.card.feature_columns
    assert not {"sms_opt_in", "email_opt_in"} & set(runs.card.feature_columns)


# ---------------------------------------------------------------------------
# The choice, exactly against the run's own files
# ---------------------------------------------------------------------------
def _arm(scored: Scored) -> pd.Series:
    """The offer position each customer was given (0 for none), read from the treat list's labels."""
    by_label = {label: k for k, label in LABELS.items()}
    offered = scored.treat["offer"].where(scored.treat["treat"] == "1", "")
    return offered.map(lambda label: by_label.get(label, 0)).astype(int)


def _expected_money(scored: Scored, k: int) -> pd.Series:
    contact, offer = COSTS[k]
    lift = scored.scores[f"uplift_arm_{k}"].astype(float)
    taken = scored.scores[f"p_treated_arm_{k}"].astype(float)
    return lift * VALUE - (contact + offer * taken)


def _opted_in(scored: Scored, campaign: OfferPopulation) -> tuple[pd.Series, pd.Series]:
    frame = campaign.frame.set_index("customer_id").loc[scored.treat.index]
    return frame["sms_opt_in"] == "true", frame["email_opt_in"] == "yes"


def test_nobody_gets_an_offer_on_a_channel_they_cannot_be_reached_on(runs: Runs) -> None:
    for scored in (runs.open, runs.budgeted):
        arm = _arm(scored)
        sms, email = _opted_in(scored, runs.campaign)
        assert (arm == 1).any() and (arm == 2).any()
        assert not ((arm == 1) & ~sms).any(), "offer A is sent only by SMS"
        assert not ((arm == 2) & ~email).any(), "offer B is sent only by email"
        treated = scored.treat["treat"] == "1"
        for k, channel in CHANNEL.items():
            assert (scored.treat.loc[arm == k, "channel"] == channel).all()
        assert (scored.treat.loc[~treated, "channel"] == "").all()
        both_out = ~sms & ~email
        assert both_out.any() and not treated[both_out].any()


def test_a_sleeping_dog_never_gets_that_offer_and_one_for_every_offer_gets_none(runs: Runs) -> None:
    cut = runs.card.segment_thresholds.sleeping_dog_max_uplift
    scored = runs.open
    arm = _arm(scored)
    dogs = np.column_stack([scored.scores[f"uplift_arm_{k}"].to_numpy() <= cut for k in (1, 2)])
    given = arm.to_numpy() > 0
    assert not dogs[given, arm.to_numpy()[given] - 1].any()
    every = dogs.all(axis=1)
    assert every.sum() > 500 and not given[every].any()
    planted = runs.campaign.segment[
        pd.Index(runs.campaign.frame["customer_id"]).get_indexer(scored.treat.index)
    ]
    assert (arm.to_numpy()[planted == "sleeping_dogs"] == 0).mean() > 0.97


def test_each_segment_gets_its_responding_offer(runs: Runs) -> None:
    scored = runs.open
    arm = _arm(scored).to_numpy()
    sms, email = (mask.to_numpy() for mask in _opted_in(scored, runs.campaign))
    segment = runs.campaign.segment[
        pd.Index(runs.campaign.frame["customer_id"]).get_indexer(scored.treat.index)
    ]
    # Customers held back as the run's control group get nothing, whoever they are.
    held = scored.scores["control_group"].astype(bool).to_numpy()
    assert (arm[held] == 0).all()
    reachable = sms & email & ~held
    a_only, b_only, both = (reachable & (segment == name) for name in ("a_only", "b_only", "both"))
    # Most of each segment gets the offer it answers; the rest sit near the planted boundaries, where a
    # model of 24,000 customers cannot tell the segments apart.
    assert (arm[a_only] == 1).mean() > 0.9
    assert (arm[b_only] == 2).mean() > 0.85
    assert (arm[both] == 1).mean() > 0.85, "A is worth more than B to them"


def test_opted_out_of_offer_a_s_only_channel_gets_b_when_it_pays_else_nothing(runs: Runs) -> None:
    scored = runs.open
    arm = _arm(scored)
    sms, email = _opted_in(scored, runs.campaign)
    open_ = ~scored.scores["control_group"].astype(bool)
    out = ~sms & email & open_
    net_b = _expected_money(scored, 2)
    dog_b = scored.scores["uplift_arm_2"] <= runs.card.segment_thresholds.sleeping_dog_max_uplift
    # Exactly: offer B when it is worth its cost and they are not a sleeping dog for it; else nothing.
    assert ((arm[out] == 2) == ((net_b[out] >= 0) & ~dog_b[out])).all()
    assert not (arm[out] == 1).any()
    # Against the planted truth: those who answer both offers get B; those who answer only A get nothing.
    segment = pd.Series(
        runs.campaign.segment[pd.Index(runs.campaign.frame["customer_id"]).get_indexer(scored.treat.index)],
        index=scored.treat.index,
    )
    # B is worth its cost to most of those who answer both; it is not worth it to most of those who
    # answer only A (the model's per-customer estimate of B scatters around zero for them).
    assert (arm[out & (segment == "both")] == 2).mean() > 0.75
    assert (arm[out & (segment == "a_only")] == 0).mean() > 0.6
    nothing = out & (arm == 0) & (segment == "a_only")
    reasons = set(scored.treat.loc[nothing, "offer_reason"])
    assert reasons and all(reason for reason in reasons), "a customer with no offer is told why"


def test_the_net_value_and_the_runner_up_are_each_offer_s_own(runs: Runs) -> None:
    scored = runs.open
    arm = _arm(scored)
    treated = scored.treat["treat"] == "1"
    money = {k: _expected_money(scored, k).round(2) for k in (1, 2)}
    net = pd.to_numeric(scored.treat["net_value"].replace("", np.nan))
    for k in (1, 2):
        mine = treated & (arm == k)
        assert np.allclose(net[mine], money[k][mine], atol=0.011)
    # The runner-up is the other offer, when the customer could be given it; its value is that offer's.
    runner = pd.to_numeric(scored.treat["runner_up_net_value"].replace("", np.nan))
    has_runner = scored.treat["runner_up_offer"] != ""
    assert has_runner[treated].mean() > 0.3
    for k, other in ((1, 2), (2, 1)):
        mine = treated & (arm == k) & has_runner
        assert (scored.treat.loc[mine, "runner_up_offer"] == LABELS[other]).all()
        assert np.allclose(runner[mine], money[other][mine], atol=0.011)
        assert (runner[mine] <= net[mine] + 0.011).all(), "the offer given is the better one"


def test_the_choice_summary_counts_what_the_treat_list_holds(app: App, runs: Runs) -> None:
    scored = runs.open
    arm = _arm(scored)
    summary = scored.summary
    assert summary["chosen"] is True and summary["levels"] == list(LEVELS)
    offers = {item["level"]: item for item in summary["arms"]}
    assert offers["offer_a"]["label"] == LABELS[1] and offers["offer_b"]["label"] == LABELS[2]
    assert offers["offer_a"]["offered_rows"] == int((arm == 1).sum())
    assert offers["offer_b"]["offered_rows"] == int((arm == 2).sum())
    assert (offers["offer_a"]["offer_cost"], offers["offer_a"]["contact_cost"]) == (10.0, 0.5)
    assert offers["offer_b"]["cost_source"] == "catalogue"
    listed = json.loads(run_artefact(app, scored.run_id, "treat_list_summary.json"))
    assert listed["offer_counts"] == {LABELS[1]: int((arm == 1).sum()), LABELS[2]: int((arm == 2).sum())}
    assert listed["channel_rows"] == {"sms": int((arm == 1).sum()), "email": int((arm == 2).sum())}
    assert summary["no_offer_rows"] == int((arm == 0).sum())


# ---------------------------------------------------------------------------
# The budget
# ---------------------------------------------------------------------------
def test_a_total_budget_holds_and_never_switches_an_offer(runs: Runs) -> None:
    scored = runs.budgeted
    arm = _arm(scored)
    spent = sum(
        float(
            (COSTS[k][0] + COSTS[k][1] * scored.scores.loc[arm[arm == k].index, f"p_treated_arm_{k}"]).sum()
        )
        for k in (1, 2)
    )
    assert spent <= runs.budget + 1e-6
    assert scored.summary["spent"] <= runs.budget + 1e-6 and scored.summary["budget"] == runs.budget
    assert spent > 0.9 * runs.budget, "the greedy walk spends the budget"
    # Each run draws its own control group; compare the customers neither run held back.
    both_open = ~scored.scores["control_group"].astype(bool) & ~runs.open.scores["control_group"].astype(bool)
    unlimited = _arm(runs.open).where(both_open, 0)
    given = (arm > 0) & both_open
    assert (arm[given] == unlimited[given]).all(), "an offer is never switched to fit the budget"
    dropped = (unlimited > 0) & ~given
    assert dropped.sum() > 1000
    reasons = set(scored.treat.loc[dropped, "offer_reason"])
    assert len(reasons) == 1 and "budget" in reasons.pop()
    # Greedy by net value per rupee: what was given earns more per rupee than what was dropped.
    ratio = pd.Series(np.nan, index=scored.treat.index)
    for k in (1, 2):
        money = _expected_money(runs.open, k)
        cost = COSTS[k][0] + COSTS[k][1] * runs.open.scores[f"p_treated_arm_{k}"]
        ratio[unlimited == k] = (money / cost)[unlimited == k]
    assert ratio[given].median() > ratio[dropped].median()


def test_a_run_without_a_budget_spends_what_the_offers_cost(runs: Runs) -> None:
    assert runs.open.summary["budget"] is None
    assert runs.open.summary["spent"] > runs.budget
