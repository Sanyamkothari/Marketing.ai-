"""Plan J M97 end to end: a model trained and scored ranked by customer value (DEC-1307).

Through the product's own API, as `test_uplift_api.py` drives it (the same helpers, a fresh app): an
uplift run on `win-back-campaign` with `uplift.policy.value_column: monthly_spend`, scored on a
campaign file where a few customers have no `monthly_spend`; and a second, default-configured run whose
hold-out carries no values. Checked here and not in the unit tests: what the flows write (the hold-out's
value column, the scores' `customer_value`/`net_value`), that one missing value does not fail a scoring
run, that a hold-out without values quotes no money, that the budget curve replays both runs, the
`value` alias, and that the run manifest keeps its AUUC metric when the interval has no bounds (review
finding 3). From the second review: the costs read from `configs/pilot/value.yaml` are recorded and the
budget curve replays them, never the file as it reads later; a recorded cost the replay does not
reproduce answers 409; an unreadable hold-out on a list ranked by value gives its reason.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine.config import Metric
from engine.contracts import RunManifest, RunRecord
from engine.pilot.roi import ValueCosts
from engine.storage import StorageError, run_key
from engine.uplift.contracts import (
    ConfidenceValue,
    PolicyRecommendation,
    ProfitCurve,
    Segment,
    UpliftEvaluation,
)
from engine.uplift.flow import HOLDOUT_COLUMNS, HOLDOUT_VALUE_COLUMN, UPLIFT_HOLDOUT_FILENAME
from engine.uplift.policy import HOLDOUT_UNREADABLE_NOTE
from tests.fixtures.make_uplift_data import make_uplift_data, make_winback_campaign
from tests.integration.uplift.test_uplift_api import (
    PRIMARY_KEY,
    App,
    finish,
    run_artefact,
    scoring_frame,
    start_score,
    start_uplift,
    uplift_artefact,
    uplift_body,
    upload,
)

pytestmark = pytest.mark.integration

VALUE = "monthly_spend"
VALUE_POLICY: dict[str, Any] = {"value_column": VALUE, "margin_pct": 30.0}
MISSING = (5, 17, 404)
"""Campaign rows whose `monthly_spend` is blanked: three of 2,000, under the 10 % limit."""


@pytest.fixture(scope="module")
def app(config_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[App]:
    data_dir = tmp_path_factory.mktemp("uplift-net-value") / "data"
    data_dir.mkdir()
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as client:
        yield App(client=client, data_dir=data_dir)


@dataclass(frozen=True)
class Runs:
    train: RunRecord
    train_frame: pd.DataFrame
    score: RunRecord
    scores: pd.DataFrame


@pytest.fixture(scope="module")
def valued(app: App) -> Runs:
    frame = make_uplift_data(6_000, seed=21).frame
    upload_id = upload(app, frame, mode="train")
    body = uplift_body(upload_id, uplift={"policy": VALUE_POLICY})
    train = finish(app, start_uplift(app, body, run_id="r_20261008_0e000001"))

    campaign = scoring_frame(make_winback_campaign(2_000, seed=23))
    campaign[VALUE] = campaign[VALUE].astype("object")
    for index in MISSING:
        campaign.loc[index, VALUE] = None
    score_upload = upload(app, campaign, mode="score")
    score_body = {
        "upload_id": score_upload,
        "primary_key": PRIMARY_KEY,
        "model_version_id": train.model_version_id,
        "overrides": {"uplift": {"policy": VALUE_POLICY}},
    }
    score = finish(app, start_score(app, score_body, run_id="r_20261008_0e000002"))
    scores = pd.read_parquet(io.BytesIO(run_artefact(app, score.run_id, "scores.parquet")))
    return Runs(train=train, train_frame=frame, score=score, scores=scores)


AUUC_WRITES: list[dict[str, float]] = []
"""Every `add_metrics` call of the `unbounded` run that carried the AUUC (review finding 3)."""


@pytest.fixture(scope="module")
def unbounded(app: App) -> RunRecord:
    """A default-configured run whose AUUC interval is made to have no bounds (review finding 3)."""
    import engine.pipeline as pipeline
    import engine.uplift.metrics as metrics

    real = metrics.evaluate_uplift
    add_metrics = pipeline._ManifestBuilder.add_metrics

    def recorded(self: Any, values: dict[str, float], *, prefix: str = "") -> None:
        if Metric.AUUC.value in values and not prefix:
            AUUC_WRITES.append(dict(values))
        add_metrics(self, values, prefix=prefix)

    def without_bounds(*args: Any, **kwargs: Any) -> tuple[UpliftEvaluation, Any]:
        evaluation, curve = real(*args, **kwargs)
        auuc = ConfidenceValue(value=evaluation.auuc.value, ci_low=None, ci_high=None)
        return evaluation.model_copy(update={"auuc": auuc}), curve

    upload_id = upload(app, make_uplift_data(6_000, seed=31).frame, mode="train")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(metrics, "evaluate_uplift", without_bounds)
        patch.setattr(pipeline._ManifestBuilder, "add_metrics", recorded)
        return finish(app, start_uplift(app, uplift_body(upload_id), run_id="r_20261008_0e000003"))


# ---------------------------------------------------------------------------
# Training ranked by value
# ---------------------------------------------------------------------------
def test_the_hold_out_carries_the_real_values_and_nothing_else_changes(app: App, valued: Runs) -> None:
    holdout = pd.read_parquet(
        io.BytesIO(app.storage.read_bytes(run_key(valued.train.run_id, UPLIFT_HOLDOUT_FILENAME)))
    )
    assert tuple(holdout.columns) == (*HOLDOUT_COLUMNS, HOLDOUT_VALUE_COLUMN)
    truth = valued.train_frame.set_index(PRIMARY_KEY)[VALUE]
    assert np.array_equal(
        holdout[HOLDOUT_VALUE_COLUMN].to_numpy(), truth.loc[holdout["primary_key"]].to_numpy()
    )


def test_the_training_recommendation_keeps_conversions_and_money_apart(app: App, valued: Runs) -> None:
    policy = uplift_artefact(app, valued.train.run_id, "policy_recommendation.json")
    assert isinstance(policy, PolicyRecommendation)
    assert policy.values_missing == 0 and policy.money_note is None
    expected = policy.expected_incremental_conversions
    assert expected is not None and policy.contacts_recommended > 0
    # Conversions, never rupees: at most one extra conversion per customer contacted.
    assert 0 < expected.value <= policy.contacts_recommended
    # Money from the value-weighted uplift × 30 % margin, less ₹0.86 a contact from value.yaml.
    assert policy.expected_cost == pytest.approx(policy.contacts_recommended * 0.86)
    assert policy.expected_value is not None and policy.expected_net_value is not None
    assert policy.expected_net_value == pytest.approx(policy.expected_value - policy.expected_cost)


def test_the_training_budget_curve_replays_the_value_ranking(app: App, valued: Runs) -> None:
    response = app.client.get(f"/runs/{valued.train.run_id}/uplift/profit-curve")
    assert response.status_code == 200, response.text
    curve = ProfitCurve.model_validate(response.json())
    policy = uplift_artefact(app, valued.train.run_id, "policy_recommendation.json")
    assert curve.value_weighted and curve.value_basis == "each customer's monthly_spend × 30% margin"
    assert not curve.overridden
    assert curve.configured.contacts == policy.contacts_recommended
    assert curve.configured.expected_incremental_conversions == policy.expected_incremental_conversions
    assert curve.configured.expected_value == policy.expected_value
    assert curve.configured.expected_cost == policy.expected_cost
    stricter = app.client.get(f"/runs/{valued.train.run_id}/uplift/profit-curve", params={"min_roi": 50})
    assert stricter.status_code == 200, stricter.text
    strict = ProfitCurve.model_validate(stricter.json())
    assert strict.overridden and strict.max_contacts <= curve.max_contacts


# ---------------------------------------------------------------------------
# Scoring ranked by value, with a few customers missing their value
# ---------------------------------------------------------------------------
def test_a_missing_value_does_not_fail_the_scoring_run_and_is_counted(app: App, valued: Runs) -> None:
    policy = uplift_artefact(app, valued.score.run_id, "policy_recommendation.json")
    assert isinstance(policy, PolicyRecommendation)
    assert policy.values_missing == len(MISSING)
    assert (
        policy.money_note is not None
        and f"{len(MISSING)} of 2000 customers have no '{VALUE}'" in policy.money_note
    )
    assert policy.expected_value is not None  # under the limit: the money is shown
    scores = valued.scores
    assert {"customer_value", "net_value"} <= set(scores.columns)
    assert int(scores["customer_value"].isna().sum()) == len(MISSING)
    blank = scores["customer_value"].isna()
    assert not (scores.loc[blank, "action"] == "Treat").any()
    assert not ((scores["segment"] == Segment.SLEEPING_DOG.value) & (scores["action"] == "Treat")).any()


def test_the_scoring_budget_curve_replays_the_contact_list(app: App, valued: Runs) -> None:
    response = app.client.get(f"/runs/{valued.score.run_id}/uplift/profit-curve")
    assert response.status_code == 200, response.text
    curve = ProfitCurve.model_validate(response.json())
    policy = uplift_artefact(app, valued.score.run_id, "policy_recommendation.json")
    assert curve.value_weighted and curve.values_missing == len(MISSING)
    assert (
        curve.configured.contacts
        == policy.contacts_recommended
        == int((valued.scores["action"] == "Treat").sum())
    )
    assert curve.configured.expected_value == policy.expected_value
    assert curve.money_note == policy.money_note


# ---------------------------------------------------------------------------
# A model whose hold-out has no values; the manifest; the `value` alias
# ---------------------------------------------------------------------------
def test_the_manifest_keeps_the_auuc_metric_when_its_interval_has_no_bounds(
    app: App, unbounded: RunRecord
) -> None:
    manifest = RunManifest.model_validate_json(run_artefact(app, unbounded.run_id, "run_manifest.json"))
    assert Metric.AUUC.value in manifest.metrics
    assert "qini_coefficient" in manifest.metrics and "average_treatment_effect" in manifest.metrics
    assert "auuc_ci_low" not in manifest.metrics and "auuc_ci_high" not in manifest.metrics
    # Once, after the loop over the bounds - not once per bound (the dedent the review found).
    assert len(AUUC_WRITES) == 1
    holdout = pd.read_parquet(
        io.BytesIO(app.storage.read_bytes(run_key(unbounded.run_id, UPLIFT_HOLDOUT_FILENAME)))
    )
    assert tuple(holdout.columns) == HOLDOUT_COLUMNS  # a default run's hold-out is unchanged


def test_scoring_by_value_with_a_hold_out_without_values_quotes_no_money(
    app: App, unbounded: RunRecord
) -> None:
    campaign = scoring_frame(make_winback_campaign(1_000, seed=29))
    score_upload = upload(app, campaign, mode="score")
    body = {
        "upload_id": score_upload,
        "primary_key": PRIMARY_KEY,
        "model_version_id": unbounded.model_version_id,
        "overrides": {"uplift": {"policy": {**VALUE_POLICY, "cost_per_contact": 1.0}}},
    }
    score = finish(app, start_score(app, body, run_id="r_20261008_0e000004"))
    policy = uplift_artefact(app, score.run_id, "policy_recommendation.json")
    assert isinstance(policy, PolicyRecommendation)
    assert policy.expected_value is None and policy.expected_net_value is None
    assert policy.expected_incremental_conversions is None
    assert policy.expected_cost == pytest.approx(policy.contacts_recommended * 1.0)
    assert (
        policy.money_note is not None
        and "training hold-out has no 'monthly_spend' values" in policy.money_note
    )
    curve = app.client.get(f"/runs/{score.run_id}/uplift/profit-curve")
    assert curve.status_code == 200, curve.text
    assert ProfitCurve.model_validate(curve.json()).optimum_note == policy.money_note


def test_value_is_an_alias_of_value_per_conversion_and_not_both(app: App, unbounded: RunRecord) -> None:
    url = f"/runs/{unbounded.run_id}/uplift/profit-curve"
    by_alias = app.client.get(url, params={"cost_per_contact": 1.0, "value": 40.0})
    by_name = app.client.get(url, params={"cost_per_contact": 1.0, "value_per_conversion": 40.0})
    assert by_alias.status_code == by_name.status_code == 200, (by_alias.text, by_name.text)
    assert by_alias.json() == by_name.json()
    both = app.client.get(url, params={"value": 40.0, "value_per_conversion": 40.0})
    assert both.status_code == 422, both.text
    assert both.json()["detail"]["code"] == "PROFIT_CURVE_QUERY_INVALID"


# ---------------------------------------------------------------------------
# Second review: value.yaml costs are recorded and replayed; an unreadable hold-out says why
# ---------------------------------------------------------------------------
MONEY_FIELDS = (
    "contacts",
    "expected_incremental_conversions",
    "expected_cost",
    "expected_value",
    "expected_net_value",
    "net_value_low",
    "net_value_high",
)
"""What the curve's configured point and the recommendation both carry, under the same names."""


def _recommendation_fields(policy: PolicyRecommendation) -> dict[str, object]:
    fields = {name: getattr(policy, name) for name in MONEY_FIELDS if name != "contacts"}
    return {"contacts": policy.contacts_recommended, **fields}


@pytest.mark.parametrize("which", ["train", "score"])
def test_the_curve_replays_the_recorded_costs_after_value_yaml_is_edited(
    app: App, valued: Runs, which: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = valued.train if which == "train" else valued.score
    policy = uplift_artefact(app, run.run_id, "policy_recommendation.json")
    assert isinstance(policy, PolicyRecommendation)
    # The client edits value.yaml after the run: a dearer contact and an offer cost.
    monkeypatch.setattr(
        "engine.pilot.roi.lookup_value_costs",
        lambda *_a, **_k: ValueCosts(contact_cost=3.0, offer_cost=25.0),
    )
    response = app.client.get(f"/runs/{run.run_id}/uplift/profit-curve")
    assert response.status_code == 200, response.text
    curve = ProfitCurve.model_validate(response.json())
    assert not curve.overridden
    configured = {name: getattr(curve.configured, name) for name in MONEY_FIELDS}
    assert configured == _recommendation_fields(policy)  # `==`: the same floats, field by field
    # The costs the run used are on both artefacts: value.yaml's ₹0.86 contact, no offer cost.
    assert (policy.contact_cost, policy.offer_cost) == (0.86, 0.0)
    assert (curve.contact_cost, curve.offer_cost) == (0.86, 0.0)


def test_a_recorded_cost_the_replay_does_not_reproduce_answers_409(app: App, valued: Runs) -> None:
    key = run_key(valued.score.run_id, "policy_recommendation.json")
    original = app.storage.read_bytes(key)
    policy = PolicyRecommendation.model_validate_json(original)
    assert policy.expected_cost is not None
    tampered = policy.model_copy(update={"expected_cost": policy.expected_cost + 1.0})
    app.storage.write_bytes(key, tampered.model_dump_json().encode("utf-8"))
    try:
        response = app.client.get(f"/runs/{valued.score.run_id}/uplift/profit-curve")
    finally:
        app.storage.write_bytes(key, original)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "PROFIT_CURVE_UNAVAILABLE"


def test_an_unreadable_hold_out_on_a_list_ranked_by_value_gives_its_reason(
    app: App, valued: Runs, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreadable(*_args: Any, **_kwargs: Any) -> pd.DataFrame:
        raise StorageError("KEY_NOT_FOUND", "the hold-out is gone")

    monkeypatch.setattr("engine.uplift.flow.read_holdout", unreadable)
    monkeypatch.setattr("api.routes.uplift.read_holdout", unreadable)
    campaign = scoring_frame(make_winback_campaign(1_000, seed=37))
    body = {
        "upload_id": upload(app, campaign, mode="score"),
        "primary_key": PRIMARY_KEY,
        "model_version_id": valued.train.model_version_id,
        "overrides": {"uplift": {"policy": VALUE_POLICY}},
    }
    score = finish(app, start_score(app, body, run_id="r_20261008_0e000005"))
    policy = uplift_artefact(app, score.run_id, "policy_recommendation.json")
    assert isinstance(policy, PolicyRecommendation)
    assert policy.expected_incremental_conversions is None and policy.expected_value is None
    assert policy.money_note == HOLDOUT_UNREADABLE_NOTE
    response = app.client.get(f"/runs/{score.run_id}/uplift/profit-curve")
    assert response.status_code == 200, response.text
    curve = ProfitCurve.model_validate(response.json())
    assert curve.money_note == policy.money_note
    assert curve.configured.expected_value is None and curve.optimum is None
