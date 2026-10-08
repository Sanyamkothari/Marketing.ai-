"""Plan J M100 acceptance: the planted three-arm population through the product's own pipeline.

`engine.measurement.simulate.multi_arm_population` deals customers at random to no offer, offer A and
offer B; one segment answers A, another answers B, and the sleeping dogs are put off by both. It is
uploaded, trained and scored through the API exactly as a user's file would be (the real upload,
checks, uplift train flow and score flow), with `uplift.treatment_levels: [none, offer_a, offer_b]`, on
a config root whose `win-back-campaign` is configured as uplift - so an empty champion slot would be
taken by a run of one treatment, and is not taken by this one.

Checked against the planted truth, never against the reports themselves: each offer's effect is inside
its interval; choosing the offer per customer beats the first offer alone; the per-arm scores let
`engine.decide.offer_choice` give each segment its responding offer and the sleeping dogs no offer,
within a budget and never on an ineligible offer; promotion is refused, by the run and by hand
(`MULTI_ARM_PROMOTION_REFUSED`); and each offer's measured lift against the shared control covers its
true effect. Every test here fails on the commit before M100 (the levels setting does not exist).
"""

from __future__ import annotations

import io
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine.contracts import ModelStatus, RunRecord
from engine.decide.offer_choice import NO_OFFER, arm_net_values, choose_offers
from engine.measurement.measure import measure_campaign
from engine.measurement.simulate import (
    AS_OF,
    MULTI_ARM_LEVELS,
    OUTCOME_WINDOW_DAYS,
    SimulatedMultiArmPopulation,
    multi_arm_population,
)
from engine.pilot.roi import ValueCosts
from engine.uplift.config import UpliftPolicyConfig
from engine.uplift.contracts import (
    ARM_POLICY_VALUE_FILENAME,
    ArmPolicyValue,
    UpliftModelCard,
)
from engine.uplift.flow import arm_columns, model_card_key, scores_columns
from tests.integration.uplift.test_uplift_api import (
    FAST_OVERRIDES,
    USE_CASE,
    App,
    finish,
    run_artefact,
    start_score,
    start_uplift,
    uplift_artefact,
    upload,
)

pytestmark = pytest.mark.integration

TARGET: Final[str] = "reactivated_90d"
TREATMENT: Final[str] = "offer"
TRAIN_ROWS: Final[int] = 24_000
SCORE_ROWS: Final[int] = 9_000
LEVELS: Final[list[str]] = list(MULTI_ARM_LEVELS)


@pytest.fixture(scope="module")
def app(config_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[App]:
    root = tmp_path_factory.mktemp("multi-arm-root") / "configs"
    shutil.copytree(config_root, root)
    path = root / "use_cases" / "win_back_campaign.yaml"
    text = path.read_text(encoding="utf-8")
    choices = "model_search:\n  metric_choices: [roc_auc, recall, f1]"
    assert choices in text
    path.write_text(
        text.replace(
            choices, "problem_type: uplift\n\nmodel_search:\n  metric: auuc\n  metric_choices: [auuc]"
        ),
        encoding="utf-8",
    )
    data_dir = tmp_path_factory.mktemp("multi-arm") / "data"
    data_dir.mkdir()
    with TestClient(create_app(config_root=root, data_dir=data_dir)) as client:
        yield App(client=client, data_dir=data_dir)


@dataclass(frozen=True)
class Runs:
    planted: SimulatedMultiArmPopulation
    train: RunRecord
    campaign: SimulatedMultiArmPopulation
    score: RunRecord
    scores: pd.DataFrame


def _body(upload_id: str) -> dict[str, Any]:
    overrides = {key: dict(value) for key, value in FAST_OVERRIDES.items()}
    overrides["uplift"] = {**overrides["uplift"], "treatment_levels": LEVELS, "bootstrap_samples": 100}
    return {
        "use_case": USE_CASE,
        "upload_id": upload_id,
        "primary_key": "customer_id",
        "target": TARGET,
        "treatment_column": TREATMENT,
        "overrides": overrides,
    }


@pytest.fixture(scope="module")
def runs(app: App) -> Runs:
    planted = multi_arm_population(TRAIN_ROWS, seed=31, outcome_column=TARGET, treatment_column=TREATMENT)
    upload_id = upload(app, planted.frame, mode="train")
    train = finish(app, start_uplift(app, _body(upload_id), run_id="r_20261008_0d100001"))
    campaign = multi_arm_population(SCORE_ROWS, seed=32, outcome_column=TARGET, treatment_column=TREATMENT)
    score_frame = campaign.frame.drop(columns=[TARGET, TREATMENT])
    score_upload = upload(app, score_frame, mode="score")
    body = {
        "upload_id": score_upload,
        "primary_key": "customer_id",
        "model_version_id": train.model_version_id,
    }
    score = finish(app, start_score(app, body, run_id="r_20261008_0d100002"))
    scores = pd.read_parquet(io.BytesIO(run_artefact(app, score.run_id, "scores.parquet")))
    return Runs(planted=planted, train=train, campaign=campaign, score=score, scores=scores)


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------
def test_every_offer_is_checked_and_evaluated_against_the_shared_control(app: App, runs: Runs) -> None:
    validation = uplift_artefact(app, runs.train.run_id, "uplift_validation.json")
    assert validation.passed and validation.causal
    assert validation.arms is not None and [a.arm for a in validation.arms] == ["offer_a", "offer_b"]
    evaluation = uplift_artefact(app, runs.train.run_id, "uplift_evaluation.json")
    assert evaluation.arms is not None and [a.position for a in evaluation.arms] == [1, 2]
    first = evaluation.arms[0]
    # DEC-668 (3): the report's own fields are the first offer against the control, repeated in arms[0].
    assert first.effect == evaluation.average_treatment_effect and first.auuc == evaluation.auuc
    assert (first.treated_rows, first.control_rows) == (evaluation.treated_rows, evaluation.control_rows)
    assert evaluation.arms[1].control_rows == evaluation.control_rows  # one shared control


def test_each_offers_effect_is_recovered_inside_its_interval(app: App, runs: Runs) -> None:
    evaluation = uplift_artefact(app, runs.train.run_id, "uplift_evaluation.json")
    assert evaluation.arms is not None
    for summary in evaluation.arms:
        truth = runs.planted.true_ate(summary.position)
        effect = summary.effect
        assert effect is not None and effect.ci_low is not None and effect.ci_high is not None
        assert effect.ci_low <= truth <= effect.ci_high, (summary.arm, truth, effect)
        assert summary.measurable_uplift, summary.arm  # each offer's model finds its responders


def test_choosing_the_offer_per_customer_beats_the_first_offer_alone(app: App, runs: Runs) -> None:
    value = uplift_artefact(app, runs.train.run_id, ARM_POLICY_VALUE_FILENAME)
    assert isinstance(value, ArmPolicyValue)
    assert value.arms == tuple(LEVELS) and sum(value.arm_rows) == value.rows
    assert value.best_offer_better, value.summary
    assert value.difference.ci_low is not None and value.difference.ci_low > 0.0
    assert value.policy_shares["offer_b"] > 0.25 and value.policy_shares["offer_a"] > 0.25


def test_the_segments_and_the_policy_list_every_offer(app: App, runs: Runs) -> None:
    segments = uplift_artefact(app, runs.train.run_id, "segments.json")
    policy = uplift_artefact(app, runs.train.run_id, "policy_recommendation.json")
    assert segments.arms is not None and policy.arms is not None
    assert [s.rows for s in segments.arms[0].segments or ()] == [s.rows for s in segments.segments]
    assert policy.arms[0].contacts_recommended == policy.contacts_recommended
    assert all(a.contacts_recommended is not None and a.contacts_recommended > 0 for a in policy.arms)
    assert all(a.expected_incremental_conversions is not None for a in policy.arms)


def test_a_model_of_several_offers_is_never_promoted(app: App, runs: Runs) -> None:
    assert runs.train.champion is False
    assert app.registry.get_champion(USE_CASE) is None  # an empty slot on a use case configured as uplift
    version = app.registry.get(runs.train.model_version_id or "")
    assert version.status is ModelStatus.CANDIDATE
    status = app.client.get(f"/runs/{runs.train.run_id}").json()["status"]
    register = next(stage for stage in status["stages"] if stage["key"] == "register")
    assert "chooses between several offers" in register["detail"]
    card = app.storage.read_model(model_card_key(version.predictor_key), UpliftModelCard)
    assert card.treatment_levels == tuple(LEVELS)
    # DEC-668 (3), review finding: training_rows, like propensity, is the first offer's and the
    # control's rows, so propensity x training_rows is a whole count of the first offer's customers.
    evaluation = uplift_artefact(app, runs.train.run_id, "uplift_evaluation.json")
    first_and_control = evaluation.treated_rows + evaluation.control_rows
    assert card.training_rows < 0.8 * TRAIN_ROWS
    assert abs(card.training_rows / first_and_control - 0.7 / 0.3) < 0.1  # the split's 70/30
    treated = card.propensity * card.training_rows
    assert abs(treated - round(treated)) < 1e-6
    response = app.client.post(
        f"/models/{version.model_id}/promote", json={"promoted_by": "test", "reason": "by hand"}
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "MULTI_ARM_PROMOTION_REFUSED"
    assert app.registry.get_champion(USE_CASE) is None


def test_the_promote_route_still_refuses_when_the_model_card_cannot_be_read(app: App, runs: Runs) -> None:
    """Review finding: the hand-promotion guard failed open on an unreadable card. Without the card the
    run's `run_config.json` (`uplift.treatment_levels`) says the model has several offers; without
    either, the promotion is refused as unchecked. Both files are put back afterwards."""
    version = app.registry.get(runs.train.model_version_id or "")
    card_key = model_card_key(version.predictor_key)
    card_bytes = app.storage.read_bytes(card_key)
    config_bytes = app.storage.read_bytes(version.run_config_key)

    def promote() -> Any:
        return app.client.post(
            f"/models/{version.model_id}/promote", json={"promoted_by": "test", "reason": "by hand"}
        )

    try:
        app.storage.delete(card_key)
        response = promote()
        assert response.status_code == 409, response.text
        detail = response.json()["detail"]
        assert detail["code"] == "MULTI_ARM_PROMOTION_REFUSED"
        assert "chooses between several offers" in detail["message"]
        app.storage.write_bytes(card_key, b"{not json")  # a corrupt card is no better than a lost one
        assert promote().status_code == 409
        app.storage.write_bytes(version.run_config_key, b"{not json")
        response = promote()
        assert response.status_code == 409, response.text
        detail = response.json()["detail"]
        assert detail["code"] == "MULTI_ARM_PROMOTION_REFUSED" and "cannot be checked" in detail["message"]
    finally:
        app.storage.write_bytes(card_key, card_bytes)
        app.storage.write_bytes(version.run_config_key, config_bytes)
    assert app.registry.get_champion(USE_CASE) is None


def test_a_run_ranked_by_value_weights_every_offers_policy_and_its_value(app: App) -> None:
    """M97's value path through every offer: each offer's policy is ranked by net value and quoted from
    the value-weighted hold-out, and the best-offer comparison weights conversions by value."""
    planted = multi_arm_population(12_000, seed=34, outcome_column=TARGET, treatment_column=TREATMENT)
    frame = planted.frame.assign(
        monthly_value=np.round(np.random.default_rng(34).gamma(2.0, 400.0, size=len(planted.frame)), 2)
    )
    body = _body(upload(app, frame, mode="train"))
    body["overrides"]["uplift"]["policy"] = {"value_column": "monthly_value", "margin_pct": 40.0}
    run = finish(app, start_uplift(app, body, run_id="r_20261008_0d100003"))
    policy = uplift_artefact(app, run.run_id, "policy_recommendation.json")
    assert policy.arms is not None and policy.contact_cost is not None
    assert policy.arms[0].expected_net_value == policy.expected_net_value
    assert all(a.expected_net_value is not None for a in policy.arms)
    value = uplift_artefact(app, run.run_id, ARM_POLICY_VALUE_FILENAME)
    assert value.value_weighted and value.value_column == "monthly_value" and value.values_missing == 0
    assert value.best_offer_better, value.summary


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def test_the_scores_carry_every_offers_prediction_and_the_contact_list_does_not(app: App, runs: Runs) -> None:
    csv = pd.read_csv(io.BytesIO(run_artefact(app, runs.score.run_id, "scores.csv")), nrows=5)
    reasons = tuple(name for name in csv.columns if name.startswith("reason_"))
    assert tuple(csv.columns) == scores_columns("customer_id", reasons)
    expected = (*scores_columns("customer_id", reasons), *arm_columns(1), *arm_columns(2))
    assert tuple(runs.scores.columns) == expected
    assert np.array_equal(runs.scores["uplift_arm_1"].to_numpy(), runs.scores["uplift"].to_numpy())
    assert np.array_equal(runs.scores["p_treated_arm_1"].to_numpy(), runs.scores["p_treated"].to_numpy())
    policy = uplift_artefact(app, runs.score.run_id, "policy_recommendation.json")
    segments = uplift_artefact(app, runs.score.run_id, "segments.json")
    assert policy.arms is not None and segments.arms is not None and len(policy.arms) == 2
    assert policy.arms[0].contacts_recommended == policy.contacts_recommended
    assert policy.arms[1].expected_incremental_conversions is None and policy.arms[1].note


def _offer_inputs(app: App, runs: Runs) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    version = app.registry.get(runs.train.model_version_id or "")
    card = app.storage.read_model(model_card_key(version.predictor_key), UpliftModelCard)
    scores = runs.scores
    uplift = scores[["uplift_arm_1", "uplift_arm_2"]].to_numpy()
    treated = scores[["p_treated_arm_1", "p_treated_arm_2"]].to_numpy()
    dogs = uplift <= card.segment_thresholds.sleeping_dog_max_uplift
    segment = runs.campaign.segment[
        pd.Index(runs.campaign.frame["customer_id"]).get_indexer(scores["customer_id"].astype(str))
    ]
    return uplift, treated, dogs, segment


def test_offer_choice_gives_each_segment_its_offer_and_sleeping_dogs_none(app: App, runs: Runs) -> None:
    uplift, treated, dogs, segment = _offer_inputs(app, runs)
    costs = ValueCosts(contact_cost=1.0, offer_cost=10.0)
    money = arm_net_values(
        uplift, UpliftPolicyConfig(value_per_conversion=1000.0), arm_costs=[costs, costs], p_treated=treated
    )
    choice = choose_offers(money.net_value, money.cost, sleeping_dog=dogs)
    a, b, dog = (segment == name for name in ("offer_a_responders", "offer_b_responders", "sleeping_dogs"))
    # Most of each segment gets the offer it answers; the rest sit near the planted segment boundaries,
    # where a model of 24,000 customers cannot tell the two apart.
    assert (choice.arm[a] == 1).mean() > 0.8 and (choice.arm[a] == 2).mean() < 0.1
    assert (choice.arm[b] == 2).mean() > 0.8 and (choice.arm[b] == 1).mean() < 0.1
    assert (choice.arm[dog] == NO_OFFER).mean() > 0.95
    given = choice.arm != NO_OFFER
    assert not dogs[given, choice.arm[given] - 1].any()


def test_offer_choice_on_the_scores_respects_eligibility_and_the_budget(app: App, runs: Runs) -> None:
    uplift, treated, dogs, segment = _offer_inputs(app, runs)
    money = arm_net_values(
        uplift,
        UpliftPolicyConfig(value_per_conversion=1000.0),
        arm_costs=[
            ValueCosts(contact_cost=1.0, offer_cost=10.0),
            ValueCosts(contact_cost=1.0, offer_cost=20.0),
        ],
        p_treated=treated,
    )
    rng = np.random.default_rng(9)
    eligible = np.column_stack([np.ones(len(uplift), dtype=bool), rng.random(len(uplift)) < 0.5])
    budget = 5_000.0
    choice = choose_offers(money.net_value, money.cost, sleeping_dog=dogs, eligible=eligible, budget=budget)
    given = choice.arm != NO_OFFER
    assert eligible[given, choice.arm[given] - 1].all()
    assert not ((choice.arm == 2) & ~eligible[:, 1]).any()
    assert choice.spent <= budget and choice.spent > 0.9 * budget
    assert "over_budget" in choice.reason
    b = segment == "offer_b_responders"
    # A B-responder who may get B and fits the budget gets B, never A.
    assert not ((choice.arm == 1) & b & eligible[:, 1] & (choice.preferred_arm == 2)).any()


# ---------------------------------------------------------------------------
# Measurement: each offer against the shared control
# ---------------------------------------------------------------------------
def test_each_offers_measured_lift_covers_its_true_effect(runs: Runs) -> None:
    """The scored customers are dealt at random to no offer, A and B, and their outcomes drawn from the
    planted truth; `measure_campaign` measures each offer against the shared control."""
    campaign = runs.campaign
    rng = np.random.default_rng(33)
    n = len(campaign.frame)
    arm = rng.permutation(np.arange(n) % 3)
    lift = np.where(arm == 0, 0.0, campaign.tau[np.arange(n), np.maximum(arm - 1, 0)])
    converted = (rng.random(n) < np.clip(campaign.base + lift, 0.0, 1.0)).astype(int)
    keys = campaign.frame["customer_id"].to_numpy()
    assignment = pd.DataFrame(
        {
            "customer_id": keys,
            "control_group": arm == 0,
            "suppressed_reason": "",
            "offer": np.asarray(LEVELS, dtype=object)[arm],
        }
    )
    outcomes = pd.DataFrame({"customer_id": keys, TARGET: converted})
    report = measure_campaign(
        assignment,
        outcomes,
        run_id=runs.score.run_id,
        primary_key="customer_id",
        outcome_column=TARGET,
        treatment_time=AS_OF,
        outcome_window_days=OUTCOME_WINDOW_DAYS,
        as_of=AS_OF + pd.Timedelta(days=OUTCOME_WINDOW_DAYS),
        arm_column="offer",
        arms=LEVELS[1:],
        control_level=LEVELS[0],
    )
    assert report.arms is not None
    for summary in report.arms:
        mine = (arm == 0) | (arm == summary.position)
        truth = float(campaign.tau[mine & (arm != 0), summary.position - 1].mean())
        effect = summary.effect
        assert effect is not None and effect.ci_low is not None and effect.ci_high is not None
        assert effect.ci_low <= truth <= effect.ci_high, (summary.arm, truth, effect)
