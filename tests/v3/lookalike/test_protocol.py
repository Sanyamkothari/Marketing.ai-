"""The look-alike model is a `ResponseModel`, and with too little data it is the model it sits on."""

from __future__ import annotations

from datetime import timedelta

import numpy as np

from autonomy.decide.lookalike import LookalikeResponseModel, Outcome, profile_of
from autonomy.decide.models import BetaResponseModel, ResponseModel, ResponseObservation

from .synth import KEY, T0, WINDOW, chance, fitted, make_state, outcomes


def test_satisfies_the_response_model_protocol() -> None:
    model = LookalikeResponseModel()
    assert isinstance(model, ResponseModel)
    state = make_state(0, np.random.default_rng(0))
    mean, sd = model.predict(state, KEY)
    assert isinstance(mean, float) and isinstance(sd, float) and sd > 0
    assert 0.0 <= model.treated_rate(state, KEY) <= 1.0


def test_update_goes_to_the_prior() -> None:
    prior = BetaResponseModel()
    model = LookalikeResponseModel(prior)
    state = make_state(0, np.random.default_rng(0))
    before = model.predict(state, KEY)
    model.update([ResponseObservation(None, KEY, True, 1.0)] * 200)
    assert model.predict(state, KEY) == prior.predict(state, KEY)
    assert model.predict(state, KEY) != before


def test_too_little_data_is_exactly_the_prior() -> None:
    prior = BetaResponseModel()
    model = LookalikeResponseModel(prior)
    states, rows = outcomes(60, seed=3)  # far below the minimum rows
    model.observe(rows)
    assert not model.fit(T0 + timedelta(days=8))
    assert not model.ready
    for st in states[:5]:
        assert model.predict(st, KEY) == prior.predict(st, KEY)
        assert model.treated_rate(st, KEY) == prior.treated_rate(st, KEY)


def test_no_control_means_no_uplift_estimate() -> None:
    """Everyone contacted: the crowd cannot tell contact from no contact, so the prior answers."""
    prior = BetaResponseModel()
    model = LookalikeResponseModel(prior)
    states, rows = outcomes(1500, seed=4, holdout=0.0)
    model.observe(rows)
    assert not model.fit(T0 + timedelta(days=8))
    assert model.predict(states[0], KEY) == prior.predict(states[0], KEY)


def test_enough_data_moves_off_the_prior_towards_the_truth() -> None:
    model, states = fitted()
    prior = BetaResponseModel()
    err_model, err_prior = [], []
    for st in states[:150]:
        truth = chance(st, True) - chance(st, False)
        err_model.append(model.predict(st, KEY)[0] - truth)
        err_prior.append(prior.predict(st, KEY)[0] - truth)
    assert float(np.sqrt(np.mean(np.square(err_model)))) < float(np.sqrt(np.mean(np.square(err_prior))))


def test_an_outcome_can_carry_no_weight() -> None:
    model = LookalikeResponseModel()
    st = make_state(0, np.random.default_rng(0))
    assert model.observe([Outcome("c0", profile_of(st), KEY, True, T0, T0 + WINDOW, weight=0.0)]) == 0
    assert model.rows == 0
