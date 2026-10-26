"""The crowd's answer for a new customer, the person's own record moving it, and the spread."""

from __future__ import annotations

from datetime import timedelta

import numpy as np

from autonomy.contracts import CustomerState
from autonomy.decide.lookalike import Outcome, profile_of

from .synth import KEY, T0, WINDOW, fitted

LATER = T0 + timedelta(days=60)


def _twin(state: CustomerState, customer_id: str) -> CustomerState:
    return state.model_copy(update={"customer_id": customer_id})


def _evidence(state: CustomerState, bought: bool, n: int) -> list[Outcome]:
    prof = profile_of(state)
    return [
        Outcome(state.customer_id, prof, KEY, bought, T0, T0 + WINDOW + timedelta(days=i)) for i in range(n)
    ]


def test_a_new_customer_gets_the_crowds_estimate() -> None:
    model, states = fitted()
    a, b = _twin(states[0], "new_a"), _twin(states[0], "new_b")
    assert model.person("new_a") == (0.0, 0.0)
    assert model.predict(a, KEY) == model.predict(b, KEY)
    # the crowd's estimate, not the prior's: the model has learned from the shop
    assert model.estimate(a, KEY) is not None


def test_strong_own_evidence_moves_away_from_the_crowd() -> None:
    model, states = fitted()
    base = states[1]
    crowd = model.predict(_twin(base, "nobody"), KEY)[0]
    keen, deaf = _twin(base, "keen"), _twin(base, "deaf")
    model.observe(_evidence(keen, True, 30))  # bought after every one of 30 messages
    model.observe(_evidence(deaf, False, 30))  # never did
    model.fit(LATER)
    assert model.predict(keen, KEY)[0] > crowd > model.predict(deaf, KEY)[0]
    assert model.person("keen")[1] > 0 > model.person("deaf")[1]


def test_the_shift_grows_with_the_evidence_and_is_zero_without() -> None:
    model, states = fitted()
    base = states[2]
    shifts = []
    for n in (0, 2, 8, 30):
        cid = f"deaf_{n}"
        model.observe(_evidence(_twin(base, cid), False, n))
        model.fit(LATER)
        shifts.append(abs(model.person(cid)[1]))
    assert shifts[0] == 0.0
    assert shifts == sorted(shifts) and shifts[-1] > shifts[1] > 0.0


def test_two_messages_barely_move_a_customer() -> None:
    model, states = fitted()
    base = states[3]
    crowd = model.predict(_twin(base, "nobody"), KEY)[0]
    model.observe(_evidence(_twin(base, "two"), False, 2))
    model.observe(_evidence(_twin(base, "thirty"), False, 30))
    model.fit(LATER)
    two = abs(model.predict(_twin(base, "two"), KEY)[0] - crowd)
    thirty = abs(model.predict(_twin(base, "thirty"), KEY)[0] - crowd)
    assert two < 0.25 * thirty


def test_spread_shrinks_with_data() -> None:
    small, states = fitted(n=700, seed=5)
    big, _ = fitted(n=7000, seed=5)
    probe = [_twin(s, f"p{i}") for i, s in enumerate(states[:40])]
    sd_small = [e.sd for p in probe if (e := small.estimate(p, KEY)) is not None]
    sd_big = [e.sd for p in probe if (e := big.estimate(p, KEY)) is not None]
    assert len(sd_small) == len(sd_big) == 40
    assert float(np.mean(sd_big)) < 0.6 * float(np.mean(sd_small))


def test_a_customer_with_more_evidence_is_surer_of_their_own_response() -> None:
    model, states = fitted()
    base = states[4]
    few, many = _twin(base, "few"), _twin(base, "many")
    model.observe(_evidence(few, False, 2))
    model.observe(_evidence(many, False, 40))
    model.fit(LATER)
    sd_few, sd_many = model.estimate(few, KEY), model.estimate(many, KEY)
    assert sd_few is not None and sd_many is not None
    assert sd_many.sd < sd_few.sd
