"""The uplift engine against the independent oracles' known answers (`tests/fixtures/uplift_oracle/`).

The fixtures were built by oracles written from the interface spec and the literature - Künzel et al.
2019 for the X-learner, Newcombe 1998a/b for the Wilson and hybrid-score intervals, the IPS / SNIPS /
DR definitions for off-policy evaluation - by code that never read `engine/uplift`, and several were
cross-checked there against scikit-uplift, causalml, econml, statsmodels, obp and DoubleML. Those
libraries are not installed here, so this file compares the engine with the stored numbers only: no
network, no extra dependency, and it runs in the fast suite.

Where the interface left a point open and the engine chose differently from the oracle, the test
holds the engine to its own recorded choice and names the DEC entry (or the docstring) that records
it, so the oracle's number is never silently loosened:

* 95% bands use the exact normal quantile `Z_95`, not 1.96 (DEC-627); OPE fixture intervals are
  rebuilt from the oracle's standard error with `Z_95`.
* An undefined pooled p-value is null, not 1.0 (DEC-628).
* When the budget and the cost cut end at the same row the stop reason is `value_below_cost`
  (DEC-622).
* Choosing nobody expects exactly 0 incremental conversions with a zero-width band
  (`engine.uplift.policy` docstring), where the oracle chose null.
* `policy_from_rule` refuses `top_share=0` and, given both rules, treats rows that pass both
  (`engine.uplift.ope.policy_from_rule` docstring).
* On a hold-out of under ten rows the empty deciles are left out (`decile_table` docstring).
* The bootstrap's draw order is the engine's own (DEC-605, DEC-610), so its intervals are compared
  with a 20,000-resample reference within 4 Monte Carlo standard deviations, never bit for bit.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime
from functools import cache
from itertools import pairwise
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from engine.uplift.champion import decide_uplift_champion
from engine.uplift.config import UpliftBaseModel, UpliftLearner, UpliftPolicyConfig, UpliftSegmentsConfig
from engine.uplift.contracts import ConfidenceValue, Segment, UpliftEvaluation
from engine.uplift.incrementality import (
    Z_95,
    measure_incrementality,
    newcombe_interval,
    two_proportion_p_value,
    wilson_interval,
)
from engine.uplift.learners import make_learner
from engine.uplift.metrics import (
    auuc_score,
    bootstrap_uplift_at,
    decile_table,
    evaluate_uplift,
    qini_coefficient,
    qini_points,
    top_rows,
    uplift_at_fraction,
)
from engine.uplift.ope import evaluate_policy, policy_from_rule
from engine.uplift.policy import choose_contacts, ranking, recommend_policy
from engine.uplift.segments import assign_segments, resolve_thresholds, segment_report

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "uplift_oracle"
NOW = datetime(2026, 9, 1, tzinfo=UTC)
FRACTIONS = ("0.1", "0.2", "0.3")


@cache
def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def near(actual: float | None, expected: float | None, tol: float) -> bool:
    if actual is None or expected is None:
        return actual is None and expected is None
    return math.isfinite(actual) and abs(actual - expected) <= tol


def arrays(case: dict[str, Any]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    inputs = case["inputs"]
    return (
        np.asarray(inputs["pred"], dtype=np.float64),
        np.asarray(inputs["t"], dtype=np.int64),
        np.asarray(inputs["y"], dtype=np.int64),
    )


def by_name(cases: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {case["name"]: case for case in cases}


# ===========================================================================
# Stage A: point metrics (metrics_oracle.py; exact fractions re-derived by the critic)
# ===========================================================================
METRIC_CASES = by_name(fixture("metrics.json")["cases"])
MEDIUM_CASES = [name for name, case in METRIC_CASES.items() if "bootstrap" in case]
TIE_CASES = ["ties6_stable_order", "constant_pred_n7", "medium_seed33_n300"]


@pytest.mark.parametrize("name", list(METRIC_CASES))
def test_auuc_and_qini_coefficient_match_the_oracle(name: str) -> None:
    case = METRIC_CASES[name]
    pred, t, y = arrays(case)
    expected = case["expected"]
    assert auuc_score(pred, t, y) == pytest.approx(expected["auuc"], abs=1e-10)
    assert qini_coefficient(pred, t, y) == pytest.approx(expected["qini_coefficient"], abs=1e-10)


@pytest.mark.parametrize("name", TIE_CASES)
def test_ties_keep_input_order_not_the_reversed_order(name: str) -> None:
    """DEC-613: a stable sort on `-pred`. The reversed order (`np.argsort(pred)[::-1]`, sklift's) differs."""
    case = METRIC_CASES[name]
    pred, t, y = arrays(case)
    expected = case["expected"]
    reversed_ties = expected["diagnostic_if_ties_reversed"]
    for function, key in ((auuc_score, "auuc"), (qini_coefficient, "qini_coefficient")):
        value = function(pred, t, y)
        assert value == pytest.approx(expected[key], abs=1e-10)
        if abs(reversed_ties[key] - expected[key]) > 1e-9:
            assert abs(value - reversed_ties[key]) > 1e-9, key
    # The policy's ranking without a scoring-time tie-break keeps input order too (DEC-606, DEC-613).
    assert ranking(pred).tolist() == expected["rank_order"]


@pytest.mark.parametrize("name", list(METRIC_CASES))
@pytest.mark.parametrize("fraction", FRACTIONS)
def test_uplift_at_fraction_uses_the_top_ceil_rows(name: str, fraction: str) -> None:
    case = METRIC_CASES[name]
    pred, t, y = arrays(case)
    expected = case["expected"]
    assert top_rows(float(fraction), len(pred)) == expected["top_counts"][fraction]
    assert near(uplift_at_fraction(pred, t, y, float(fraction)), expected["uplift_at"][fraction], 1e-10)


def curve_at(curve: dict[str, list[float]], fraction: float, key: str) -> float:
    """The oracle's per-k curve, joined by straight lines, at any fraction in [0, 1]."""
    values = curve[key]
    n = len(values) - 1
    x = min(max(fraction, 0.0), 1.0) * n
    low = min(math.floor(x), n)
    high = min(low + 1, n)
    weight = x - low
    return (1.0 - weight) * values[low] + weight * values[high]


@pytest.mark.parametrize("name", list(METRIC_CASES))
def test_qini_points_lie_on_the_oracle_curve_and_are_dense_enough(name: str) -> None:
    case = METRIC_CASES[name]
    pred, t, y = arrays(case)
    curve = case["expected"]["curve_every_k"]
    points = qini_points(pred, t, y, points=101)
    fractions = [point.fraction for point in points]
    assert fractions[0] == 0.0 and fractions[-1] == 1.0
    assert all(b > a for a, b in pairwise(fractions))
    n = len(pred)
    assert len(points) >= min(101, n + 1)
    assert max(b - a for a, b in pairwise(fractions)) <= 0.01 + 1.0 / n + 1e-12
    for point in points:
        for key in ("qini", "random", "uplift_curve"):
            assert getattr(point, key) == pytest.approx(curve_at(curve, point.fraction, key), abs=1e-9), (
                point.fraction,
                key,
            )


@pytest.mark.parametrize("name", list(METRIC_CASES))
def test_deciles_follow_array_split_of_the_ranked_rows(name: str) -> None:
    case = METRIC_CASES[name]
    pred, t, y = arrays(case)
    expected = [decile for decile in case["expected"]["deciles"] if decile["rows"] > 0]
    deciles = decile_table(pred, t, y)
    # Empty deciles (only possible below ten rows) are left out rather than given a made-up mean.
    assert len(deciles) == len(expected) == min(10, len(pred))
    for got, want in zip(deciles, expected, strict=True):
        for key in ("decile", "rows", "treated_rows", "control_rows"):
            assert getattr(got, key) == want[key], (want["decile"], key)
        for key in ("treated_rate", "control_rate", "observed_uplift", "predicted_uplift"):
            assert near(getattr(got, key), want[key], 1e-10), (want["decile"], key, getattr(got, key))


# ===========================================================================
# Stage A: bootstrap intervals (reference: 20,000 resamples; tolerance: 4 sd at 200 resamples)
# ===========================================================================
def within_reference(value: ConfidenceValue | None, reference: dict[str, Any], sd: dict[str, Any]) -> None:
    assert value is not None
    assert value.value == pytest.approx(reference["value"], abs=1e-10)
    for end in ("ci_low", "ci_high"):
        got = getattr(value, end)
        assert got is not None, end
        assert abs(got - reference[end]) <= 4 * sd[f"{end}_sd"] + 1e-12, (end, got, reference[end])


@pytest.mark.parametrize("name", MEDIUM_CASES)
@pytest.mark.parametrize("fraction", FRACTIONS)
def test_bootstrap_uplift_at_is_within_four_sd_of_the_reference(name: str, fraction: str) -> None:
    case = METRIC_CASES[name]
    pred, t, y = arrays(case)
    key = f"uplift_at_{fraction}"
    reference = case["bootstrap"]["reference_samples20000"][key]
    sd = case["bootstrap"]["endpoint_sd_at_200_samples"][key]
    value = bootstrap_uplift_at(pred, t, y, float(fraction), samples=200, seed=3)
    assert value is not None and value.value == case["expected"]["uplift_at"][fraction]
    within_reference(value, reference, sd)


@pytest.mark.parametrize("name", MEDIUM_CASES)
def test_evaluate_uplift_agrees_with_the_oracle_and_with_its_own_parts(name: str) -> None:
    case = METRIC_CASES[name]
    pred, t, y = arrays(case)
    bootstrap = case["bootstrap"]
    reference, sd = bootstrap["reference_samples20000"], bootstrap["endpoint_sd_at_200_samples"]
    evaluation, curve = evaluate_uplift(
        pred,
        t,
        y,
        run_id="oracle",
        learner=UpliftLearner.X_LEARNER,
        base_model=UpliftBaseModel.LIGHTGBM,
        bootstrap_samples=200,
        seed=3,
        causal=True,
        now=NOW,
    )
    for field, key in (
        ("auuc", "auuc"),
        ("qini_coefficient", "qini_coefficient"),
        ("average_treatment_effect", "ate"),
    ):
        within_reference(getattr(evaluation, field), reference[key], sd[key])
    assert evaluation.rows_evaluated == len(pred)
    assert evaluation.treated_rows == int((t == 1).sum())
    assert evaluation.control_rows == int((t == 0).sum())
    assert evaluation.treated_rate == pytest.approx(float(y[t == 1].mean()))
    assert evaluation.control_rate == pytest.approx(float(y[t == 0].mean()))
    at = {round(item.fraction, 10): item.uplift for item in evaluation.uplift_at}
    assert set(at) == {0.1, 0.2, 0.3}
    for fraction in FRACTIONS:
        key = f"uplift_at_{fraction}"
        within_reference(at[float(fraction)], reference[key], sd[key])
    assert len(evaluation.deciles) == 10
    assert evaluation.deciles == decile_table(pred, t, y)
    assert curve.rows_evaluated == len(pred)
    assert curve.points == qini_points(pred, t, y)
    auuc_low = evaluation.auuc.ci_low
    assert evaluation.measurable_uplift is (auuc_low is not None and auuc_low > 0)
    assert evaluation.summary.startswith(
        "Targeting by predicted uplift beats random targeting"
        if evaluation.measurable_uplift
        else "No measurable uplift"
    )


def test_an_undefined_resample_gives_a_null_interval_not_a_dropped_one() -> None:
    """DEC-611: P(one resample has no control row in the top 2) = 0.555, so 200 resamples hit one."""
    (case,) = [
        c for c in fixture("metrics.json")["bootstrap_extra"]["cases"] if c["name"].startswith("undef")
    ]
    pred, t, y = arrays(case)
    value = bootstrap_uplift_at(pred, t, y, case["fraction"], samples=case["samples"], seed=3)
    assert value is not None
    assert value.value == case["expected"]["value"]
    assert value.ci_low is None and value.ci_high is None


def test_the_bootstrap_keeps_arm_sizes() -> None:
    """DEC-605: a whole-table bootstrap loses the 2-row control arm in 12.9% of resamples."""
    (case,) = [c for c in fixture("metrics.json")["bootstrap_extra"]["cases"] if c["name"].startswith("arm")]
    pred, t, y = arrays(case)
    reference = case["reference_samples20000"]["uplift_at_1.0"]
    sd = case["endpoint_sd_at_200_samples"]["uplift_at_1.0"]
    within_reference(bootstrap_uplift_at(pred, t, y, 1.0, samples=200, seed=3), reference, sd)


# ===========================================================================
# Stage B: learners (learners_oracle.py; Künzel et al. 2019 for the X-learner weighting)
# ===========================================================================
KNOWN_ANSWERS = by_name(fixture("learners_known_answer.json")["known_answer"])
LEARNERS = {"s": UpliftLearner.S_LEARNER, "t": UpliftLearner.T_LEARNER, "x": UpliftLearner.X_LEARNER}


def known_answer_data(name: str) -> tuple[pd.DataFrame, np.ndarray, np.ndarray, pd.DataFrame, float]:
    case = KNOWN_ANSWERS[name]
    inputs = case["inputs"]
    frame = pd.DataFrame({"x": np.asarray(inputs["x"], dtype=np.float64)})
    query = pd.DataFrame({"x": np.asarray(case["query_X"]["x"], dtype=np.float64)})
    # The rows are the documented expansion of the cells; check it so the fixture cannot drift.
    cells = case["cells_(x,t,n,positives)"]
    assert len(frame) == sum(cell[2] for cell in cells)
    assert int(np.sum(inputs["y"])) == sum(cell[3] for cell in cells)
    t = np.asarray(inputs["t"], dtype=np.int64)
    y = np.asarray(inputs["y"], dtype=np.int64)
    return frame, t, y, query, float(case["tolerance_abs"])


def test_the_hyperparameters_the_weighting_fixture_needs() -> None:
    """The 2-cell answer holds only when min_child_samples is in [41, 299] (learners_NOTES.md)."""
    from engine.uplift.learners import lightgbm_params

    assert 41 <= lightgbm_params(0)["min_child_samples"] <= 299


def test_x_learner_weights_the_control_fit_effect_model_by_the_propensity() -> None:
    """Künzel et al. 2019: uplift = e·τ0 + (1 − e)·τ1 with e the training treated share.

    τ0 = (0.45, 0.05) is fit on the control rows and τ1 = 0.25 on the 40 treated rows, e = 40/640.
    The right weighting gives (0.2625, 0.2375); the reversed one would give (0.4375, 0.0625).
    """
    frame, t, y, query, tol = known_answer_data("x_weighting_direction_2cells")
    expected = KNOWN_ANSWERS["x_weighting_direction_2cells"]["expected"]
    model = make_learner(UpliftLearner.X_LEARNER, UpliftBaseModel.LIGHTGBM, seed=0).fit(frame, t, y)
    assert model.propensity == pytest.approx(expected["e"], abs=1e-12)
    prediction = model.predict(query)
    np.testing.assert_allclose(prediction.uplift, expected["x_learner"]["uplift"], atol=tol)
    wrong = np.asarray(expected["x_learner_if_weights_reversed (WRONG)"]["uplift"])
    assert np.max(np.abs(prediction.uplift - wrong)) > 0.1
    np.testing.assert_allclose(prediction.p_treated, expected["x_learner"]["p_treated"], atol=tol)
    np.testing.assert_allclose(prediction.p_control, expected["x_learner"]["p_control"], atol=tol)
    phi, expected_value = model.contributions(query)
    np.testing.assert_allclose(phi[:, 0], expected["x_contributions"]["phi_x"], atol=tol)
    assert expected_value == pytest.approx(expected["x_contributions"]["expected_value"], abs=tol)
    np.testing.assert_allclose(phi.sum(axis=1) + expected_value, prediction.uplift, atol=1e-9)


@pytest.mark.parametrize(("kind", "key"), [("t", "t_learner"), ("s", "s_learner")])
def test_t_and_s_learners_on_the_weighting_fixture(kind: str, key: str) -> None:
    """T gives μ1 − μ0 = (0.45, 0.05); S cannot split on a 40-row treated arm and gives exactly 0."""
    frame, t, y, query, tol = known_answer_data("x_weighting_direction_2cells")
    expected = KNOWN_ANSWERS["x_weighting_direction_2cells"]["expected"][key]
    prediction = (
        make_learner(LEARNERS[kind], UpliftBaseModel.LIGHTGBM, seed=0).fit(frame, t, y).predict(query)
    )
    np.testing.assert_allclose(prediction.uplift, expected["uplift"], atol=tol)
    np.testing.assert_allclose(prediction.uplift, prediction.p_treated - prediction.p_control, atol=1e-9)


@pytest.mark.parametrize("kind", list(LEARNERS))
@pytest.mark.parametrize("name", ["saturated_4cells", "constant_feature_ate"])
def test_every_learner_recovers_the_cell_differences(name: str, kind: str) -> None:
    frame, t, y, query, tol = known_answer_data(name)
    expected = KNOWN_ANSWERS[name]["expected"]["uplift_all_learners"]
    prediction = (
        make_learner(LEARNERS[kind], UpliftBaseModel.LIGHTGBM, seed=0).fit(frame, t, y).predict(query)
    )
    np.testing.assert_allclose(
        prediction.uplift, np.broadcast_to(expected, prediction.uplift.shape), atol=tol
    )
    assert prediction.uplift.dtype == np.float64 and len(prediction.uplift) == len(query)


def test_x_learner_on_a_constant_feature_puts_everything_in_the_expected_value() -> None:
    frame, t, y, query, tol = known_answer_data("constant_feature_ate")
    expected = KNOWN_ANSWERS["constant_feature_ate"]["expected"]
    model = make_learner(UpliftLearner.X_LEARNER, UpliftBaseModel.LIGHTGBM, seed=0).fit(frame, t, y)
    phi, expected_value = model.contributions(query)
    np.testing.assert_allclose(phi[:, 0], expected["x_contributions"]["phi_x"], atol=1e-12)
    assert expected_value == pytest.approx(expected["x_contributions"]["expected_value"], abs=tol)


# ===========================================================================
# Stage D: Wilson, Newcombe and the pooled z-test (intervals_oracle.py; Newcombe 1998a/b)
# ===========================================================================
INTERVALS = fixture("intervals.json")


def counts(case: dict[str, Any]) -> tuple[int, int, int, int]:
    return case["x1"], case["n1"], case["x2"], case["n2"]


def test_the_oracle_and_the_engine_use_the_same_quantile() -> None:
    assert INTERVALS["_meta"]["z"] == pytest.approx(Z_95, abs=1e-15)  # DEC-627


@pytest.mark.parametrize("case", INTERVALS["wilson"], ids=lambda c: f"{c['x']}/{c['n']}")
def test_wilson_interval(case: dict[str, Any]) -> None:
    low, high = wilson_interval(case["x"], case["n"])
    assert (low, high) == pytest.approx((case["lo"], case["hi"]), abs=1e-9)
    if "published" in case:
        assert (low, high) == pytest.approx(tuple(case["published"]), abs=case["published_abs_tol"])


@pytest.mark.parametrize("case", INTERVALS["newcombe"], ids=lambda c: "{}/{}-{}/{}".format(*counts(c)))
def test_newcombe_hybrid_score_interval(case: dict[str, Any]) -> None:
    difference, low, high = newcombe_interval(*counts(case))
    assert difference == pytest.approx(case["diff"], abs=1e-12)
    assert (low, high) == pytest.approx((case["lo"], case["hi"]), abs=1e-9)


PUBLISHED_NEWCOMBE = [case for case in INTERVALS["newcombe"] if "published" in case]


@pytest.mark.parametrize("case", PUBLISHED_NEWCOMBE, ids=lambda c: c["source"].split("(")[1][0])
def test_newcombe_1998b_table_ii_method_10(case: dict[str, Any]) -> None:
    """The published four-decimal values; a continuity-corrected method 11 would miss (a) and (d)."""
    assert len(PUBLISHED_NEWCOMBE) == 8
    got = newcombe_interval(*counts(case))
    assert got == pytest.approx(tuple(case["published"]), abs=case["published_abs_tol"])


@pytest.mark.parametrize("case", INTERVALS["ztest"], ids=lambda c: "{}/{}-{}/{}".format(*counts(c)))
def test_pooled_two_proportion_p_value(case: dict[str, Any]) -> None:
    p_value = two_proportion_p_value(*counts(case))
    if case["z"] is None:
        assert p_value is None  # DEC-628: undefined, not the oracle's conventional 1.0
    else:
        assert p_value == pytest.approx(case["p_value"], abs=1e-9)


def two_arm_frames(x1: int, n1: int, x2: int, n2: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    keys = [f"K{i:07d}" for i in range(n1 + n2)]
    scores = pd.DataFrame(
        {
            "customer_id": keys,
            "band": "High",
            "action": ["Treat"] * n1 + ["Control (hold out)"] * n2,
            "control_group": [False] * n1 + [True] * n2,
            "suppressed_reason": [None] * (n1 + n2),
        }
    )
    converted = [1] * x1 + [0] * (n1 - x1) + [1] * x2 + [0] * (n2 - x2)
    return scores, pd.DataFrame({"customer_id": keys, "converted": converted})


def measure(x1: int, n1: int, x2: int, n2: int) -> Any:
    scores, outcomes = two_arm_frames(x1, n1, x2, n2)
    return measure_incrementality(
        scores,
        outcomes,
        run_id="oracle",
        primary_key="customer_id",
        outcome_column="converted",
        treatment_time=datetime(2026, 1, 1, tzinfo=UTC),
        as_of=NOW,
    )


# The frame-building path is checked on every case up to 20,000 rows (the pure functions above cover
# all 47, including the 300,000-row ones) so that the fast suite stays fast.
REPORT_CASES = [case for case in INTERVALS["newcombe"] if case["n1"] + case["n2"] <= 20_000]


@pytest.mark.parametrize("case", REPORT_CASES, ids=lambda c: "{}/{}-{}/{}".format(*counts(c)))
def test_incrementality_report_numbers(case: dict[str, Any]) -> None:
    key = counts(case)
    x1, n1, x2, n2 = key
    ztest = {counts(c): c for c in INTERVALS["ztest"]}[key]
    relative = {counts(c): c for c in INTERVALS["relative_lift"]}[key]
    incremental = {counts(c): c for c in INTERVALS["incremental_conversions"]}[key]
    report = measure(*key)
    assert str(report.status).split(".")[-1].lower() == "mature" and report.results_available_on is None
    assert (report.treated_rows, report.treated_conversions) == (n1, x1)
    assert (report.control_rows, report.control_conversions) == (n2, x2)
    lift = report.absolute_lift
    assert lift is not None
    assert lift.value == pytest.approx(case["diff"], abs=1e-12)
    assert (lift.ci_low, lift.ci_high) == pytest.approx((case["lo"], case["hi"]), abs=1e-9)
    if ztest["z"] is None:
        assert report.p_value is None  # DEC-628
    else:
        assert report.p_value == pytest.approx(ztest["p_value"], abs=1e-9)
    assert near(report.relative_lift, relative["relative_lift"], 1e-12)
    conversions = report.incremental_conversions
    assert conversions is not None
    assert conversions.value == pytest.approx(incremental["value"], abs=1e-6)
    assert (conversions.ci_low, conversions.ci_high) == pytest.approx(
        (incremental["lo"], incremental["hi"]), abs=1e-6
    )


@pytest.mark.parametrize("case", INTERVALS["discordant"], ids=lambda c: "{}/{}-{}/{}".format(*counts(c)))
def test_the_summary_judges_significance_by_the_interval_alone(case: dict[str, Any]) -> None:
    """The Newcombe CI and the z-test can disagree; the sentence must follow one criterion (the CI)."""
    summary = measure(*counts(case)).summary
    assert ("includes zero" in summary) is (not case["ci_excludes_zero"]), summary


RULES = fixture("rules.json")
POPULATION = RULES["incrementality_population"]


@pytest.mark.parametrize("case", POPULATION["cases"], ids=lambda c: c["name"])
def test_incrementality_population_and_maturity(case: dict[str, Any]) -> None:
    scores = pd.DataFrame(
        [
            {
                "customer_id": row["key"],
                "band": row["band"],
                "control_group": row["control_group"],
                "suppressed_reason": row["suppressed_reason"],
                "action": (
                    "Suppressed"
                    if row["suppressed_reason"]
                    else ("Control (hold out)" if row["control_group"] else "Treat")
                ),
            }
            for row in POPULATION["scores"]
        ]
    )
    outcomes = pd.DataFrame(
        [
            {"customer_id": key, "y": value["y"], "d": value["d"]}
            for key, value in POPULATION["outcomes"].items()
        ]
    )
    options = dict(case["kwargs"])
    options["as_of"] = datetime.fromisoformat(options["as_of"])
    report = measure_incrementality(
        scores,
        outcomes,
        run_id="oracle",
        primary_key="customer_id",
        outcome_column=POPULATION["outcome_column"],
        treatment_time=datetime.fromisoformat(POPULATION["treatment_time"]),
        **options,
    )
    expected = case["expected"]
    for key in (
        "treated_rows",
        "treated_conversions",
        "control_rows",
        "control_conversions",
        "rows_immature",
        "rows_without_outcome",
        "rows_suppressed_or_untreated",
    ):
        assert getattr(report, key) == expected[key], key
    assert str(report.status).split(".")[-1].lower() == expected["status"]
    available = expected["results_available_on"]
    assert report.results_available_on == (None if available is None else date.fromisoformat(available))
    if expected["status"] == "immature":
        assert report.absolute_lift is None and report.p_value is None
        assert report.treated_rate is None and report.control_rate is None


# ===========================================================================
# Stage D: off-policy evaluation (ope_oracle.py, checked against exact Fractions)
# ===========================================================================
OPE = fixture("ope.json")
OPE_CASES = by_name(OPE["cases"])


def ope_report(case: dict[str, Any]) -> Any:
    inputs = case["inputs"]
    propensity = inputs["propensity"]
    return evaluate_policy(
        np.asarray(inputs["t"], dtype=np.int64),
        np.asarray(inputs["y"], dtype=np.int64),
        np.asarray(inputs["policy_treat"], dtype=np.float64),
        p_treated=np.asarray(inputs["p_treated"], dtype=np.float64),
        p_control=np.asarray(inputs["p_control"], dtype=np.float64),
        propensity=np.asarray(propensity, dtype=np.float64) if isinstance(propensity, list) else propensity,
        run_id="oracle",
        description="fixture",
        causal=True,
        now=NOW,
    )


def assert_band(value: ConfidenceValue, expected: dict[str, Any]) -> None:
    """Value to 1e-12; band = value ± Z_95·se (sample sd, ddof = 1; DEC-627 for the quantile)."""
    assert value.value == pytest.approx(expected["value"], abs=1e-12)
    if expected["se"] is None:
        assert value.ci_low is None and value.ci_high is None
    else:
        half = Z_95 * expected["se"]
        assert (value.ci_low, value.ci_high) == pytest.approx(
            (expected["value"] - half, expected["value"] + half), abs=1e-10
        )


@pytest.mark.parametrize("name", list(OPE_CASES))
def test_ope_exact_cases(name: str) -> None:
    case = OPE_CASES[name]
    report = ope_report(case)
    expected = case["expected_ddof1_z1.96"]
    defined = [method for method in ("ips", "snips", "dr") if expected[method]["value"] is not None]
    # An undefined estimator (SNIPS with Σw = 0) is left out, never reported as a number.
    assert [estimate.method for estimate in report.estimates] == defined
    for estimate in report.estimates:
        assert_band(estimate.value, expected[estimate.method])
    fields = case["expected_report_ddof1"]
    assert report.rows == fields["rows"] == len(case["inputs"]["t"])
    assert report.policy_treat_share == pytest.approx(fields["policy_treat_share"], abs=1e-12)
    assert report.logged_value == pytest.approx(fields["logged_value"], abs=1e-12)
    assert report.propensity == pytest.approx(float(np.mean(case["inputs"]["propensity"])), abs=1e-12)
    assert_band(report.treat_all_value, fields["treat_all_value"])
    assert_band(report.treat_none_value, fields["treat_none_value"])
    assert report.causal is True


def test_the_undefined_snips_case_is_in_the_fixtures() -> None:
    """Guards the regression above: the fixture must keep exercising Σw = 0."""
    case = OPE_CASES["snips_undefined_sum_w_zero"]
    assert case["expected_ddof1_z1.96"]["snips"]["value"] is None
    assert [estimate.method for estimate in ope_report(case).estimates] == ["ips", "dr"]


POLICY_RULES = OPE["policy_from_rule_cases"]


@pytest.mark.parametrize("index", range(len(POLICY_RULES)))
def test_policy_from_rule(index: int) -> None:
    case = POLICY_RULES[index]
    options = {key: case[key] for key in ("top_share", "min_uplift") if key in case}
    uplift = np.asarray(case["uplift"], dtype=np.float64)
    if options.get("top_share") == 0.0:
        # Engine choice (policy_from_rule docstring): "the top 0%" is refused, not "treat nobody".
        with pytest.raises(ValueError, match="top_share"):
            policy_from_rule(uplift, **options)
        return
    policy, description = policy_from_rule(uplift, **options)
    assert policy.astype(int).tolist() == case["expected"], case["note"]
    assert description.startswith("Treat ")


def test_policy_from_rule_needs_a_rule_and_combines_two_by_and() -> None:
    uplift = np.array([0.3, 0.1, 0.2, 0.05])
    with pytest.raises(ValueError):
        policy_from_rule(uplift)
    # Engine choice (policy_from_rule docstring): with both rules a row must pass both.
    policy, _ = policy_from_rule(uplift, top_share=0.5, min_uplift=0.25)
    assert policy.tolist() == [1.0, 0.0, 0.0, 0.0]


# ===========================================================================
# Stage A champion and Stage C segments and policy (rules_oracle.py)
# ===========================================================================
def evaluation(data: dict[str, Any] | None) -> UpliftEvaluation | None:
    if data is None:
        return None
    return UpliftEvaluation.model_construct(
        causal=data["causal"],
        rows_evaluated=data["rows_evaluated"],
        treated_rows=data["treated_rows"],
        control_rows=data["control_rows"],
        auuc=ConfidenceValue.model_construct(
            value=data["auuc"]["value"], ci_low=data["auuc"]["ci_low"], ci_high=None, confidence_level=0.95
        ),
    )


@pytest.mark.parametrize("case", RULES["champion"], ids=lambda c: c["name"])
def test_uplift_champion_rule(case: dict[str, Any]) -> None:
    challenger = evaluation(case["challenger"])
    assert challenger is not None
    champion = evaluation(case["champion"])
    if case["expected"] == "ValueError":
        with pytest.raises(ValueError):
            decide_uplift_champion(challenger, champion, min_improvement_pct=case["min_improvement_pct"])
        return
    decision = decide_uplift_champion(challenger, champion, min_improvement_pct=case["min_improvement_pct"])
    assert decision.promote is case["expected"]["promote"]
    assert near(decision.improvement_pct, case["expected"]["improvement_pct"], 1e-9)


@pytest.mark.parametrize("case", RULES["segments"], ids=lambda c: c["name"])
def test_segments_thresholds_labels_and_report(case: dict[str, Any]) -> None:
    config = UpliftSegmentsConfig(
        **{key: value for key, value in case["config"].items() if value is not None}
    )
    thresholds = resolve_thresholds(config, base_rate=case["base_rate"])
    for key, value in case["expected_thresholds"].items():
        assert getattr(thresholds, key) == value, key
    uplift = np.asarray(case["uplift"], dtype=np.float64)
    p_control = np.asarray(case["p_control"], dtype=np.float64)
    segments = assign_segments(uplift, p_control, thresholds)
    assert [segment.value for segment in segments.tolist()] == case["expected_segments"]
    report = segment_report(
        uplift,
        np.asarray(case["p_treated"], dtype=np.float64),
        p_control,
        segments,
        thresholds,
        run_id="oracle",
        computed_on="test",
        causal=True,
    )
    assert report.rows == len(uplift)
    assert len(report.segments) == len(case["expected_summary"])
    for got, want in zip(report.segments, case["expected_summary"], strict=True):
        assert got.segment.value == want["segment"]
        assert got.rows == want["rows"]
        assert got.share_pct == pytest.approx(want["share_pct"], abs=1e-12)
        for key in ("mean_predicted_uplift", "mean_p_treated", "mean_p_control"):
            assert near(getattr(got, key), want[key], 1e-12), (want["segment"], key)


@pytest.mark.parametrize("case", RULES["choose_contacts"], ids=lambda c: c["name"])
def test_choose_contacts(case: dict[str, Any]) -> None:
    segments = np.array([Segment(value) for value in case["segments"]], dtype=object)
    eligible = None if case["eligible"] is None else np.asarray(case["eligible"], dtype=bool)
    selected, reason = choose_contacts(
        np.asarray(case["uplift"], dtype=np.float64),
        segments,
        UpliftPolicyConfig(**case["policy"]),
        eligible=eligible,
    )
    assert selected.tolist() == case["expected_mask"]
    assert not bool((selected & (np.asarray(case["segments"]) == Segment.SLEEPING_DOG.value)).any())
    if case["name"] == "budget_and_value_bind_on_the_same_row":
        assert case["expected_stop_reason"] == "budget"  # the oracle's choice
        assert reason.value == "value_below_cost"  # DEC-622: a bigger budget would not change N
    else:
        assert reason.value == case["expected_stop_reason"]


def padded(case: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    """The chosen rows as persuadables, padded with lost causes up to `rows`."""
    chosen = len(case["uplift"])
    uplift = np.asarray(case["uplift"] + [0.0] * (case["rows"] - chosen), dtype=np.float64)
    segments = np.array(
        [Segment.PERSUADABLE] * chosen + [Segment.LOST_CAUSE] * (case["rows"] - chosen), dtype=object
    )
    return uplift, segments


def test_recommend_policy_arithmetic_uses_the_observed_uplift() -> None:
    (case,) = [c for c in RULES["recommend_policy"] if c["name"] == "arithmetic"]
    uplift, segments = padded(case)
    asked: list[float] = []
    returns = case["observed_top_share_returns"]

    def observed(fraction: float) -> ConfidenceValue:
        asked.append(fraction)
        return ConfidenceValue(**returns)

    recommendation, _ = recommend_policy(
        uplift,
        segments,
        UpliftPolicyConfig(
            cost_per_contact=case["cost_per_contact"], value_per_conversion=case["value_per_conversion"]
        ),
        run_id="oracle",
        computed_on="test",
        causal=True,
        observed_top_share=observed,
    )
    expected = case["expected"]
    assert asked == [pytest.approx(case["observed_top_share_called_with_fraction"], abs=1e-15)]
    assert recommendation.rows == expected["rows"]
    assert recommendation.contacts_recommended == expected["contacts_recommended"]
    conversions = recommendation.expected_incremental_conversions
    assert conversions is not None
    for key in ("value", "ci_low", "ci_high"):
        assert getattr(conversions, key) == pytest.approx(expected["expected_incremental_conversions"][key])
    for key in ("predicted_incremental_conversions", "expected_cost", "expected_value", "expected_net_value"):
        assert getattr(recommendation, key) == pytest.approx(expected[key], abs=1e-12), key


def test_recommend_policy_without_a_holdout_measurement() -> None:
    (case,) = [c for c in RULES["recommend_policy"] if c["name"] == "no_holdout_measurement"]
    uplift, segments = padded(case)
    recommendation, _ = recommend_policy(
        uplift,
        segments,
        UpliftPolicyConfig(
            cost_per_contact=case["cost_per_contact"], value_per_conversion=case["value_per_conversion"]
        ),
        run_id="oracle",
        computed_on="test",
        causal=True,
        observed_top_share=None,
    )
    expected = case["expected"]
    assert recommendation.contacts_recommended == expected["contacts_recommended"]
    assert recommendation.expected_incremental_conversions is None
    assert recommendation.expected_value is None and recommendation.expected_net_value is None
    assert recommendation.expected_cost == pytest.approx(expected["expected_cost"])
    assert recommendation.predicted_incremental_conversions == pytest.approx(
        expected["predicted_incremental_conversions"]
    )


def test_recommend_policy_with_nobody_chosen() -> None:
    """Oracle choice: null. Engine choice (policy module docstring): exactly 0, zero-width band."""
    (case,) = [c for c in RULES["recommend_policy"] if c["name"] == "nothing_chosen"]
    assert case["expected"]["expected_incremental_conversions"] is None and case["depends_on_choice"]
    returns = case["observed_top_share_returns"]
    recommendation, selected = recommend_policy(
        np.asarray(case["uplift"] + [0.0] * (case["rows"] - 1)),
        np.array([Segment.LOST_CAUSE] * case["rows"], dtype=object),
        UpliftPolicyConfig(),
        run_id="oracle",
        computed_on="test",
        causal=True,
        observed_top_share=lambda fraction: ConfidenceValue(**returns),
    )
    assert not selected.any() and recommendation.contacts_recommended == 0
    assert recommendation.expected_incremental_conversions == ConfidenceValue(
        value=0.0, ci_low=0.0, ci_high=0.0
    )
    assert recommendation.expected_cost is None and recommendation.expected_value is None
    assert recommendation.predicted_incremental_conversions == 0.0


@pytest.mark.parametrize(
    "case",
    RULES["share_to_holdout_count"]["cases"],
    ids=lambda c: f"{c['n_chosen']}of{c['rows']}-holdout{c['n_holdout']}",
)
def test_the_policy_share_asks_the_holdout_about_exactly_the_right_rows(case: dict[str, Any]) -> None:
    """`ceil(N·n_h / rows)` in integers; a float ceil takes 8 rows for 7/25 of 25."""
    fraction = case["n_chosen"] / case["rows"]
    assert top_rows(fraction, case["n_holdout"]) == case["expected_holdout_count"]
