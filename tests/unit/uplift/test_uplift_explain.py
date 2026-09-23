"""Stage B: the uplift importance chart and the per-row "why persuadable" reasons."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from engine.contracts import Direction, FeatureImportance, ReasonMethod
from engine.stages.explain import IMPORTANCE_UNAVAILABLE_CAPTION, TOP_FEATURES
from engine.uplift.config import UpliftBaseModel, UpliftLearner
from engine.uplift.explain import UPLIFT_IMPORTANCE_CAPTION, uplift_importance, uplift_reasons
from engine.uplift.learners import make_learner
from tests.fixtures.make_uplift_data import PLANS, REGIONS, make_uplift_data


# ---------------------------------------------------------------------------
# uplift_importance - hand-computed
# ---------------------------------------------------------------------------
def test_importance_is_mean_absolute_contribution_normalised_to_100() -> None:
    contributions = np.array([[0.3, -0.1, 0.0], [-0.1, 0.1, 0.0], [0.3, -0.1, 0.0], [-0.1, 0.1, 0.0]])
    chart = uplift_importance(contributions, ["visits", "tenure", "noise"], run_id="run-1")
    assert isinstance(chart, FeatureImportance)
    assert chart.run_id == "run-1"
    assert chart.method == "shap"
    assert chart.computed_on == "test"
    assert chart.caption == UPLIFT_IMPORTANCE_CAPTION == "Mean |SHAP| on predicted uplift, test split (%)"
    assert chart.top_n == 3
    assert [item.feature for item in chart.items] == ["visits", "tenure", "noise"]
    assert [item.rank for item in chart.items] == [1, 2, 3]
    assert [item.importance for item in chart.items] == pytest.approx([0.2, 0.1, 0.0])
    assert [item.share_pct for item in chart.items] == [66.7, 33.3, 0.0]
    assert all(item.stddev is None and item.p_value is None for item in chart.items)


def test_importance_shares_sum_to_exactly_100() -> None:
    rng = np.random.default_rng(0)
    contributions = rng.normal(size=(50, 7))
    chart = uplift_importance(contributions, [f"f{i}" for i in range(7)], run_id="r")
    assert round(sum(item.share_pct for item in chart.items), 6) == 100.0


def test_importance_keeps_the_top_20() -> None:
    contributions = np.tile(np.arange(30, 0, -1, dtype=float), (4, 1))
    features = [f"f{i:02d}" for i in range(30)]
    chart = uplift_importance(contributions, features, run_id="r")
    assert chart.top_n == TOP_FEATURES == 20
    assert [item.feature for item in chart.items] == features[:20]


def test_importance_ties_are_ordered_by_name() -> None:
    chart = uplift_importance(np.ones((3, 3)), ["c", "a", "b"], run_id="r")
    assert [item.feature for item in chart.items] == ["a", "b", "c"]


def test_importance_all_zero_contributions_give_zero_shares() -> None:
    chart = uplift_importance(np.zeros((5, 2)), ["a", "b"], run_id="r")
    assert [item.share_pct for item in chart.items] == [0.0, 0.0]


def test_importance_without_rows_is_empty_not_invented() -> None:
    chart = uplift_importance(np.zeros((0, 2)), ["a", "b"], run_id="r")
    assert chart.items == ()
    assert chart.top_n == 0
    assert chart.caption == IMPORTANCE_UNAVAILABLE_CAPTION


def test_importance_refuses_a_width_mismatch_and_non_finite_values() -> None:
    with pytest.raises(ValueError, match="one column per feature"):
        uplift_importance(np.zeros((2, 3)), ["a", "b"], run_id="r")
    with pytest.raises(ValueError, match="non-finite"):
        uplift_importance(np.array([[np.nan, 0.0]]), ["a", "b"], run_id="r")


# ---------------------------------------------------------------------------
# uplift_reasons - hand-computed
# ---------------------------------------------------------------------------
def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "visits_30d": [9.0, 2.0, np.nan],
            "plan": pd.Categorical(["premium", "basic", None], categories=["basic", "plus", "premium"]),
            "noise_a": [0.5, -1.25, 0.0],
        }
    )


def test_reasons_strongest_first_with_the_house_text() -> None:
    contributions = np.array(
        [
            [0.12, -0.03, 0.001],
            [-0.05, 0.02, 0.0],
            [0.0, 0.0, 0.0],
        ]
    )
    explanations = uplift_reasons(
        contributions, _frame(), np.array([0.2, -0.01, 0.03]), ["C1", "C2", "C3"], top_n=2
    )
    assert [row.primary_key for row in explanations] == ["C1", "C2", "C3"]
    assert [row.score for row in explanations] == [0.2, -0.01, 0.03]
    assert all(row.method is ReasonMethod.TREE_SHAP for row in explanations)

    first = explanations[0].reasons
    assert [reason.text for reason in first] == ["visits_30d ↑ (9)", "plan = premium"]
    assert [reason.direction for reason in first] == [Direction.UP, Direction.DOWN]
    assert first[0].contribution == pytest.approx(0.12)
    assert first[0].value == "9"

    second = explanations[1].reasons
    assert [reason.text for reason in second] == ["visits_30d ↓ (2)", "plan = basic"]

    # All-zero contributions: the model does not tell this row from the average, so no reason.
    assert explanations[2].reasons == ()


def test_reasons_render_missing_values() -> None:
    contributions = np.array([[0.0] * 3, [0.0] * 3, [0.04, -0.02, 0.0]])
    explanations = uplift_reasons(contributions, _frame(), np.zeros(3), ["a", "b", "c"], top_n=3)
    assert [reason.text for reason in explanations[2].reasons] == ["visits_30d missing", "plan missing"]


def test_reasons_ties_follow_the_global_rank() -> None:
    frame = pd.DataFrame({"a": [1.0, 1.0], "b": [2.0, 2.0]})
    # b matters more over these rows, so on row 0's tie b comes first.
    contributions = np.array([[0.1, 0.1], [0.0, 0.5]])
    explanations = uplift_reasons(contributions, frame, np.zeros(2), ["k0", "k1"], top_n=2)
    assert [reason.feature for reason in explanations[0].reasons] == ["b", "a"]


def test_reasons_refuse_misaligned_inputs() -> None:
    frame = _frame()
    with pytest.raises(ValueError, match="same rows"):
        uplift_reasons(np.zeros((3, 3)), frame, np.zeros(2), ["a", "b", "c"], top_n=1)
    with pytest.raises(ValueError, match="one column per feature"):
        uplift_reasons(np.zeros((3, 2)), frame, np.zeros(3), ["a", "b", "c"], top_n=1)
    with pytest.raises(ValueError, match="top_n"):
        uplift_reasons(np.zeros((3, 3)), frame, np.zeros(3), ["a", "b", "c"], top_n=-1)


def test_reasons_with_no_rows() -> None:
    assert uplift_reasons(np.zeros((0, 3)), _frame().head(0), np.zeros(0), [], top_n=3) == ()


# ---------------------------------------------------------------------------
# End to end on a fitted learner
# ---------------------------------------------------------------------------
NUMERIC = ("age", "tenure_months", "visits_30d", "monthly_spend", "support_tickets_90d", "noise_a")


def _prepared(frame: pd.DataFrame) -> pd.DataFrame:
    columns: dict[str, pd.Series] = {name: frame[name].astype("float64") for name in NUMERIC}
    columns["plan"] = pd.Series(pd.Categorical(frame["plan"], categories=list(PLANS)), index=frame.index)
    columns["region"] = pd.Series(
        pd.Categorical(frame["region"], categories=list(REGIONS)), index=frame.index
    )
    return pd.DataFrame(columns)


def test_persuadables_are_explained_by_their_visits() -> None:
    data = make_uplift_data(12_000, seed=4)
    x = _prepared(data.frame)
    t = data.frame["treatment"].to_numpy(dtype=int)
    y = data.frame["reactivated_90d"].to_numpy(dtype=int)
    model = make_learner(UpliftLearner.X_LEARNER, UpliftBaseModel.LIGHTGBM, seed=3).fit(x, t, y)

    test = make_uplift_data(2_000, seed=5)
    x_test = _prepared(test.frame)
    uplift = model.predict(x_test).uplift
    contributions, _ = model.contributions(x_test)

    chart = uplift_importance(contributions, model.feature_columns, run_id="run-x")
    # Visits drive the persuadables; region and tenure together define the sleeping dogs.
    assert {item.feature for item in chart.items[:3]} == {"visits_30d", "region", "tenure_months"}

    keys = test.frame["customer_id"].tolist()
    explanations = uplift_reasons(contributions, x_test, uplift, keys, top_n=3)
    assert len(explanations) == len(x_test)
    persuadable = np.flatnonzero((test.truth["true_segment"] == "persuadable").to_numpy())
    top_features = [explanations[row].reasons[0].feature for row in persuadable if explanations[row].reasons]
    # The planted persuadables are defined by visits, so visits is their most common top reason.
    assert max(set(top_features), key=top_features.count) == "visits_30d"
    assert all(len(row.reasons) <= 3 for row in explanations)
