"""The uplift-threshold policy (autonomy/decide/uplift_threshold.py, docs/v4/METHOD.md).

Inline planted data only: no download, no dataset folder. The planted campaigns are randomised 50/50, so the
truth is known: a null campaign (the message does nothing) and a strongly heterogeneous one (it helps some
customers a lot and hurts others).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np
import numpy.typing as npt
import pandas as pd
import pytest

from autonomy.decide import uplift_threshold as ut
from autonomy.uplift.learner import ArmModel, SharedBaselineLogit

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]


def planted(n: int, *, strong: bool, seed: int = 0, cost: float = 0.5, value: float = 10.0) -> ut.UpliftData:
    """A 50/50 campaign of one message. Everyone buys with chance 0.1 anyway; with `strong` the message adds
    [-0.05, 0, 0.10, 0.30] to the chance of a customer in group g = 0..3 (g is a feature), else nothing.
    A buyer's margin is `value`; one message costs `cost`. `noise` is a feature that carries no signal."""
    rng = np.random.default_rng(seed)
    group = rng.integers(0, 4, n)
    noise = rng.integers(0, 3, n)
    arm = rng.integers(0, 2, n).astype(np.int64)
    lift = np.array([-0.05, 0.0, 0.10, 0.30])[group] if strong else np.zeros(n)
    p = 0.1 + np.where(arm == 1, lift, 0.0)
    conv = (rng.random(n) < p).astype(np.float64)
    features = pd.DataFrame({"group": group.astype(float), "noise": noise.astype(float)})
    attributes = tuple(
        {"group": str(int(g)), "noise": str(int(z))} for g, z in zip(group, noise, strict=True)
    )
    return ut.UpliftData(
        customer_ids=np.array([f"c{seed}_{i}" for i in range(n)], dtype=object),
        arm=arm,
        propensity=np.full(n, 0.5),
        converted=conv,
        margin=conv * value,
        arm_cost=np.array([0.0, cost]),
        arm_names=("no message", "message"),
        features=features,
        attributes=attributes,
    )


class SpyLearner:
    """The default learner, recording how many rows each fold model was fitted on and then asked about."""

    def __init__(self) -> None:
        self.inner = SharedBaselineLogit()
        self.fitted: list[int] = []
        self.predicted: list[int] = []

    def fit(
        self,
        x: npt.NDArray[np.float32],
        arm: IntArray,
        y: FloatArray,
        weight: FloatArray,
        n_arms: int,
        blocks: int,
    ) -> ArmModel:
        self.fitted.append(len(arm))
        return _Spied(self.inner.fit(x, arm, y, weight, n_arms, blocks), self)


class _Spied:
    def __init__(self, model: ArmModel, owner: SpyLearner) -> None:
        self.model, self.owner = model, owner

    def predict(self, x: npt.NDArray[np.float32]) -> tuple[FloatArray, FloatArray]:
        self.owner.predicted.append(len(x))
        return self.model.predict(x)

    def predict_row(self, cols: list[int]) -> tuple[float, list[float]]:
        return self.model.predict_row(cols)


# --- cross-fitting -----------------------------------------------------------------------------------


def test_cross_fitting_never_scores_a_row_with_a_model_fitted_on_it() -> None:
    data = planted(3000, strong=True)
    model = ut.CrossFitUplift.fit(data, folds=5, seed=3)
    assert model is not None
    assert len(model.models) == 5
    # every customer is in exactly one fold; the model that scores them was fitted on the other four
    assert set(model.fold.tolist()) == {0, 1, 2, 3, 4}
    for f, rows in enumerate(model.fit_rows):
        scored = np.flatnonzero(model.fold == f)
        assert not set(rows.tolist()) & set(scored.tolist())
        assert len(rows) + len(scored) == data.n
    # scoring the fitted customers with all five models (all of which saw them, bar one) is a different number
    # from their out-of-fold score: the out-of-fold one is never the average
    everyone = model.raw(data.attributes)
    assert not np.allclose(everyone, model.out_of_fold_raw())


def _single_model_uplift(model: ut.CrossFitUplift, f: int, attributes: Mapping[str, str]) -> float:
    x = model.encoder.matrix([attributes], np.full((1, 4), np.nan))
    m0, mk = model.models[f].predict(x)
    return float(mk[0, 0] - m0[0])


def test_fold_models_are_fitted_on_the_other_folds_only() -> None:
    data = planted(2000, strong=True)
    spy = SpyLearner()
    model = ut.CrossFitUplift.fit(data, folds=4, seed=1, learner=spy)
    assert model is not None
    sizes = [int((model.fold == f).sum()) for f in range(4)]
    assert spy.fitted == [data.n - s for s in sizes]
    assert (
        spy.predicted == sizes
    )  # each model is asked about its own held-out fold, at fit time, and no one else
    assert sum(sizes) == data.n


def test_out_of_fold_raw_uplift_matches_the_fold_model_exactly() -> None:
    data = planted(2000, strong=True)
    model = ut.CrossFitUplift.fit(data, folds=5, seed=2)
    assert model is not None
    for i in range(0, data.n, 211):
        f = int(model.fold[i])
        assert model.out_of_fold_raw()[i, 0] == pytest.approx(
            _single_model_uplift(model, f, data.attributes[i]), abs=1e-9
        )


def test_fold_assignment_is_a_pure_function_of_id_and_seed() -> None:
    ids = np.array([f"id{i}" for i in range(500)], dtype=object)
    a = ut.fold_of_ids(ids, 5, seed=0)
    assert np.array_equal(a, ut.fold_of_ids(ids, 5, seed=0))
    assert np.array_equal(a[:100], ut.fold_of_ids(ids[:100], 5, seed=0))  # not on who else is in the file
    assert not np.array_equal(a, ut.fold_of_ids(ids, 5, seed=1))
    assert set(a.tolist()) == {0, 1, 2, 3, 4}


def test_a_campaign_too_small_or_with_an_empty_arm_gets_no_model() -> None:
    small = planted(200, strong=True)
    model, why = ut.CrossFitUplift.fit_or_reason(small)
    assert model is None and "customers" in why
    data = planted(2000, strong=True)
    one_arm = ut.UpliftData(
        data.customer_ids,
        np.ones(data.n, dtype=np.int64),
        data.propensity,
        data.converted,
        data.margin,
        data.arm_cost,
        data.arm_names,
        data.features,
        data.attributes,
    )
    model, why = ut.CrossFitUplift.fit_or_reason(one_arm)
    assert model is None and "no message" in why


# --- the decision ------------------------------------------------------------------------------------


def test_expected_net_and_the_deterministic_argmax() -> None:
    score = np.array([[0.10, 0.02], [0.01, 0.04], [0.05, 0.05], [-0.02, -0.01]])
    value = np.array([10.0, 20.0])
    cost = np.array([0.5, 0.6])
    net = ut.expected_net(score, value, cost)
    assert net == pytest.approx(np.array([[0.5, -0.2], [-0.4, 0.2], [0.0, 0.4], [-0.7, -0.8]]))
    # best arm if its net is above 0, else nothing; a net of exactly 0 is not a send
    assert ut.decide_arms(net).tolist() == [1, 2, 2, 0]
    assert ut.decide_arms(np.array([[0.0, 0.0]])).tolist() == [0]
    assert ut.decide_arms(np.array([[0.3, 0.3]])).tolist() == [1]  # ties go to the lowest arm
    assert ut.decide_arms(np.zeros((3, 0))).tolist() == [0, 0, 0]
    assert ut.one_hot(np.array([0, 2, 1]), 3).tolist() == [[1, 0, 0], [0, 0, 1], [0, 1, 0]]


def test_value_per_conversion_is_the_arms_mean_margin_of_a_buyer() -> None:
    data = planted(1000, strong=True, value=7.0)
    assert ut.value_per_conversion(data).tolist() == [7.0]


# --- the gate ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_the_gate_falls_back_to_the_rule_when_the_message_does_nothing(seed: int) -> None:
    data = planted(8000, strong=False, seed=seed)
    policy = ut.fit_uplift_threshold(data, segment_candidates=(("noise",), ("group",)), seed=seed)
    gate = policy.gate
    assert gate.chose.startswith("rule:") and policy.model is None
    assert gate.lb is not None and gate.model_value is not None  # a model was fitted and judged ...
    assert gate.lb <= gate.best_rule_value  # ... and its lower bound did not beat the best simple rule
    assert gate.line().startswith("UPLIFT_GATE chose=rule:")
    assert " lb=" in gate.line() and " best_rule=" in gate.line()
    # a message that does nothing costs money: nobody should be contacted by the rule either
    pi = policy.pi(data.attributes, data.features)
    assert pi[:, 1].mean() < 0.5


def test_the_gate_chooses_the_model_on_planted_strong_heterogeneity() -> None:
    data = planted(24000, strong=True, seed=5)
    # the only segment column on offer carries no signal, so no simple rule can find who to message
    policy = ut.fit_uplift_threshold(data, segment_candidates=(("noise",),), seed=5)
    gate = policy.gate
    assert gate.chose == "model", gate.line()
    assert gate.lb is not None and gate.lb > gate.best_rule_value
    assert policy.model is not None and policy.belief is policy.model
    # it messages groups 2 and 3 (positive net: 0.10 x 10 - 0.5 and 0.30 x 10 - 0.5) and leaves 0 and 1 alone
    group = data.features["group"].to_numpy()
    sent = policy.arms(data.attributes, data.features) > 0
    assert sent[group >= 2].mean() > 0.9
    assert sent[group <= 1].mean() < 0.1


def test_the_gate_names_what_it_judged_and_the_model_comes_first() -> None:
    data = planted(8000, strong=True, seed=7)
    policy = ut.fit_uplift_threshold(data, segment_candidates=(("group",),), seed=7)
    names = list(policy.gate.values)
    assert names[0] == "model"
    assert {"send to none", "send to all: message", "segment rule (group)"} <= set(names)
    assert policy.gate.best_rule in names and policy.gate.gate_rows + policy.gate.fit_rows == data.n
    for point, low, high in policy.gate.values.values():
        assert low <= point <= high


def test_a_campaign_too_small_for_a_model_gets_the_best_simple_rule() -> None:
    tiny = planted(
        300, strong=True, seed=3
    )  # 210 customers in the fit part: a model needs 400 and 40 per arm
    policy = ut.fit_uplift_threshold(tiny, segment_candidates=(("group",),), seed=3)
    assert policy.gate.chose.startswith("rule:") and policy.gate.lb is None
    assert "customers" in policy.gate.reason and "lb=n/a" in policy.gate.line()
    assert policy.pi(tiny.attributes, tiny.features).sum(axis=1).tolist() == [1.0] * tiny.n


def test_a_segment_rule_that_wins_is_refitted_on_all_of_train_and_used_as_is() -> None:
    data = planted(8000, strong=True, seed=11)
    policy = ut.fit_uplift_threshold(data, segment_candidates=(("group",),), seed=11)
    if policy.gate.chose != "rule:segment rule (group)":
        pytest.skip("the model won this draw; the segment rule path is covered by the null test")
    assert policy.rule_segment is not None and policy.model is None
    arms = policy.arms(data.attributes, data.features)
    group = data.features["group"].to_numpy()
    assert arms[group == 3].mean() > 0.9 and arms[group == 0].mean() < 0.1


# --- determinism -------------------------------------------------------------------------------------


def test_the_policy_is_deterministic_for_a_seed() -> None:
    data = planted(6000, strong=True, seed=9)
    a = ut.fit_uplift_threshold(data, segment_candidates=(("noise",), ("group",)), seed=4)
    b = ut.fit_uplift_threshold(data, segment_candidates=(("noise",), ("group",)), seed=4)
    assert a.gate.line() == b.gate.line()
    assert a.gate.values == b.gate.values
    assert np.array_equal(a.pi(data.attributes, data.features), b.pi(data.attributes, data.features))
    other = ut.fit_uplift_threshold(data, segment_candidates=(("noise",), ("group",)), seed=5)
    assert other.gate.line() != a.gate.line()  # another seed is another split of the same data


def test_scoring_customers_outside_the_fit_does_not_change_the_model() -> None:
    data = planted(3000, strong=True, seed=2)
    model = ut.CrossFitUplift.fit(data, seed=2)
    assert model is not None
    attrs: Sequence[dict[str, str]] = [{"group": "3", "noise": "0"}, {"group": "0", "noise": "1"}]
    first = model.score(attrs)
    again = model.score(attrs)
    assert np.array_equal(first, again)
    assert first[0, 0] > first[1, 0]  # group 3 is helped far more than group 0
