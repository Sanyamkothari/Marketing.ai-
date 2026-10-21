"""Other learners behind autonomy.uplift's UpliftLearner: any plug-in, and v1's AutoML (v1bridge)."""

from __future__ import annotations

import numpy as np
import numpy.typing as npt
import pytest

from autonomy.bridges.uplift_bridge import V1UpliftLearner
from autonomy.contracts import CustomerState
from autonomy.uplift import campaign_table, fit_uplift
from autonomy.uplift.learner import sigmoid
from tests.v2.realfix.test_uplift_support import NOW, T0, campaign, content

KEY = "email|none|quality"


def _table(**kw: object):  # type: ignore[no-untyped-def]
    events, states = campaign(**kw)  # type: ignore[arg-type]
    table, notes = campaign_table(events, states, {c.content_id: c for c in content()}, NOW)
    assert table is not None, notes
    return table


def _who(tier: str) -> CustomerState:
    return CustomerState(
        customer_id=f"n_{tier}", as_of=NOW, preferences={"tier": tier, "age": "30"}, first_seen_at=T0
    )


class _Flat:
    """A learner that says every customer is alike: arm rates without features."""

    def predict(self, x: npt.NDArray[np.float32]) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
        n = x.shape[0]
        return np.full(n, 0.05), np.full((n, 1), 0.10)

    def predict_row(self, cols: list[int]) -> tuple[float, list[float]]:
        return 0.05, [0.10]


class FlatLearner:
    def fit(self, x, arm, y, weight, n_arms, blocks):  # type: ignore[no-untyped-def]
        return _Flat()


class RefusingLearner:
    def fit(self, x, arm, y, weight, n_arms, blocks):  # type: ignore[no-untyped-def]
        raise ValueError("not for this campaign")


def test_any_learner_plugs_in_and_gets_the_same_honesty() -> None:
    """A learner with no view of customers gets no slope (nothing to validate), only the measured
    average effect: the level always comes from the campaign, not from the learner."""
    table = _table(n=3000)
    model, notes = fit_uplift(table, learner=FlatLearner())
    assert model is not None, notes
    arm = model.report.arms[0]
    assert arm.slope_used == 0.0
    a, b = model.score(_who("a"), KEY, NOW), model.score(_who("b"), KEY, NOW)
    assert a is not None and b is not None
    assert a.mean == pytest.approx(b.mean) == pytest.approx(arm.ate)
    assert sigmoid(np.array([0.0]))[0] == pytest.approx(0.5)


def test_a_refusing_learner_means_no_model() -> None:
    model, notes = fit_uplift(_table(n=1500), learner=RefusingLearner())
    assert model is None
    assert any("refused" in n for n in notes)


def test_v1_learner_refuses_unequal_weights_before_importing_v1() -> None:
    x = np.ones((4, 2), dtype=np.float32)
    arm = np.array([0, 1, 0, 1], dtype=np.int64)
    with pytest.raises(ValueError, match="random assignment"):
        V1UpliftLearner().fit(x, arm, np.zeros(4), np.array([1.0, 2.0, 1.0, 1.0]), 1, 1)


@pytest.mark.v1bridge
def test_v1_xlearner_scores_through_the_same_interface() -> None:
    pytest.importorskip("lightgbm")
    table = _table(n=4000, lift_a=0.15, lift_b=0.0)
    model, notes = fit_uplift(table, learner=V1UpliftLearner(learner="x_learner", base_model="lightgbm"))
    assert model is not None, notes
    a, b = model.score(_who("a"), KEY, NOW), model.score(_who("b"), KEY, NOW)
    assert a is not None and b is not None
    assert a.mean > b.mean
