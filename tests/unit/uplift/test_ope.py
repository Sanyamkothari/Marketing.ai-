"""`engine.uplift.ope`: IPS, SNIPS and DR estimates of a policy on logged randomised data.

A tiny hand-worked log pins every formula. Coverage is then checked against the generator's truth:
on many independent randomised logs, the true value of a known policy - computed from the planted
outcome probabilities, never from outcomes - must fall inside the 95 % interval in the large
majority of seeds, for IPS and DR, and DR's interval must be the narrower one when the outcome model
is good. A deliberately wrong outcome model checks the "doubly robust" half of DR's name.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from engine.uplift.contracts import OpeReport
from engine.uplift.incrementality import Z_95
from engine.uplift.ope import evaluate_policy, policy_from_rule
from tests.fixtures.make_uplift_data import make_uplift_data

RUN_ID = "r_20260921_cccccccc"
NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


def estimate(report: OpeReport, method: str) -> tuple[float, float, float]:
    (match,) = [item for item in report.estimates if item.method == method]
    assert match.value.ci_low is not None and match.value.ci_high is not None
    return match.value.value, match.value.ci_low, match.value.ci_high


# ---------------------------------------------------------------------------
# Hand-worked formulas
# ---------------------------------------------------------------------------
def test_hand_worked_log() -> None:
    t = np.array([1, 1, 0, 0])
    y = np.array([1, 0, 1, 0])
    pi = np.array([1.0, 0.0, 0.5, 0.0])
    mu1 = np.array([0.6, 0.2, 0.4, 0.3])
    mu0 = np.array([0.3, 0.1, 0.5, 0.2])
    e = 0.5
    report = evaluate_policy(
        t,
        y,
        pi,
        p_treated=mu1,
        p_control=mu0,
        propensity=e,
        run_id=RUN_ID,
        description="x",
        causal=True,
        now=NOW,
    )
    # w = π(A)/p(A): row0 1/0.5 = 2, row1 0/0.5 = 0, row2 0.5/0.5 = 1, row3 1/0.5 = 2.
    w = np.array([2.0, 0.0, 1.0, 2.0])
    ips_terms = w * y  # [2, 0, 1, 0]
    ips, ips_lo, ips_hi = estimate(report, "ips")
    assert ips == pytest.approx(0.75)
    half = Z_95 * ips_terms.std(ddof=1) / 2.0
    assert (ips_lo, ips_hi) == pytest.approx((0.75 - half, 0.75 + half))

    snips, snips_lo, snips_hi = estimate(report, "snips")
    ratio = 3.0 / 5.0
    assert snips == pytest.approx(ratio)
    residual = w * y - ratio * w
    half = Z_95 * math.sqrt(residual.var(ddof=1) / 4) / w.mean()
    assert (snips_lo, snips_hi) == pytest.approx((ratio - half, ratio + half))

    # q̂π = π·μ1 + (1−π)·μ0 = [0.6, 0.1, 0.45, 0.2]; q̂(A) = [0.6, 0.2, 0.5, 0.2]
    # terms = q̂π + w·(y − q̂(A)) = [0.6+0.8, 0.1, 0.45+0.5, 0.2−0.4] = [1.4, 0.1, 0.95, −0.2]
    dr_terms = np.array([1.4, 0.1, 0.95, -0.2])
    dr, dr_lo, dr_hi = estimate(report, "dr")
    assert dr == pytest.approx(dr_terms.mean())
    half = Z_95 * dr_terms.std(ddof=1) / 2.0
    assert (dr_lo, dr_hi) == pytest.approx((dr_terms.mean() - half, dr_terms.mean() + half))

    assert [item.method for item in report.estimates] == ["ips", "snips", "dr"]
    assert report.rows == 4
    assert report.policy_treat_share == pytest.approx(0.375)
    assert report.propensity == pytest.approx(0.5)
    assert report.logged_value == pytest.approx(0.5)
    # treat_all: terms = μ1 + t/e·(y − μ1) = [0.6+0.8, 0.2−0.4, 0.4, 0.3] → mean 0.475
    assert report.treat_all_value.value == pytest.approx(0.475)
    # treat_none: terms = μ0 + (1−t)/(1−e)·(y − μ0) = [0.3, 0.1, 0.5+1.0, 0.2−0.4] → mean 0.425
    assert report.treat_none_value.value == pytest.approx(0.425)
    assert report.computed_at == NOW
    assert OpeReport.model_validate_json(report.model_dump_json()) == report


def test_per_row_propensity_is_accepted() -> None:
    t = np.array([1, 0, 1, 0])
    y = np.array([1, 1, 0, 0])
    pi = np.ones(4)
    report = evaluate_policy(
        t,
        y,
        pi,
        p_treated=np.full(4, 0.5),
        p_control=np.full(4, 0.5),
        propensity=np.array([0.25, 0.5, 0.5, 0.75]),
        run_id=RUN_ID,
        description="treat everyone",
        causal=False,
    )
    # IPS terms: t·y/e = [4, 0, 0, 0] → 1.0
    assert estimate(report, "ips")[0] == pytest.approx(1.0)
    assert report.propensity == pytest.approx(0.5)
    assert report.causal is False


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"propensity": 1.0}, "strictly between 0 and 1"),
        ({"propensity": 0.0}, "strictly between 0 and 1"),
        ({"p_treated": np.array([0.1, 1.2, 0.3, 0.4])}, "p_treated"),
        ({"p_control": np.array([0.1, 0.2, 0.3])}, "p_control"),
        ({"policy_treat": np.array([0.0, 1.0, 2.0, 0.0])}, "policy_treat"),
        ({"y": np.array([0, 1, 2, 0])}, "y must hold only 0 and 1"),
        ({"y": np.array([0, 1, 1])}, "same rows"),
    ],
)
def test_bad_inputs_are_refused(kwargs: dict[str, object], message: str) -> None:
    options: dict[str, object] = {
        "t": np.array([1, 0, 1, 0]),
        "y": np.array([1, 0, 0, 1]),
        "policy_treat": np.array([1.0, 0.0, 1.0, 0.0]),
        "p_treated": np.full(4, 0.4),
        "p_control": np.full(4, 0.3),
        "propensity": 0.5,
        "run_id": RUN_ID,
        "description": "x",
        "causal": True,
    }
    options.update(kwargs)
    with pytest.raises(ValueError, match=message):
        evaluate_policy(**options)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# policy_from_rule
# ---------------------------------------------------------------------------
def test_policy_from_rule_top_share_uses_a_stable_ranking() -> None:
    uplift = np.array([0.1, 0.3, 0.3, -0.2, 0.05])
    policy, description = policy_from_rule(uplift, top_share=0.25)  # ceil(1.25) = 2 rows
    assert policy.tolist() == [0.0, 1.0, 1.0, 0.0, 0.0]
    assert policy.dtype == np.float64
    assert description == "Treat the top 25% of customers by predicted uplift"

    policy, _ = policy_from_rule(np.array([0.2, 0.2, 0.2]), top_share=0.34)
    assert policy.tolist() == [1.0, 1.0, 0.0]


def test_policy_from_rule_min_uplift_and_both() -> None:
    uplift = np.array([0.1, 0.3, 0.02, -0.2, 0.05])
    policy, description = policy_from_rule(uplift, min_uplift=0.05)
    assert policy.tolist() == [1.0, 1.0, 0.0, 0.0, 1.0]
    assert description == "Treat customers with a predicted uplift of at least +5.0 points"

    policy, description = policy_from_rule(uplift, top_share=0.4, min_uplift=0.2)
    assert policy.tolist() == [0.0, 1.0, 0.0, 0.0, 0.0]
    assert "top 40%" in description and "at least +20.0 points" in description


def test_policy_from_rule_refuses_nonsense() -> None:
    with pytest.raises(ValueError, match="top_share, min_uplift or both"):
        policy_from_rule(np.array([0.1]))
    with pytest.raises(ValueError, match="top_share"):
        policy_from_rule(np.array([0.1]), top_share=0.0)
    with pytest.raises(ValueError, match="finite"):
        policy_from_rule(np.array([0.1, np.nan]), top_share=0.5)


# ---------------------------------------------------------------------------
# Coverage against the generator's truth
# ---------------------------------------------------------------------------
def _cells(frame: pd.DataFrame) -> pd.Series:
    """The outcome model's cells: the features the planted segments depend on, visits capped."""
    sleeping = (frame["region"] == "north") & (frame["tenure_months"] >= 36)
    premium = frame["plan"] == "premium"
    visits = frame["visits_30d"].clip(upper=12)
    return sleeping.astype(int).astype(str) + premium.astype(int).astype(str) + "_" + visits.astype(str)


def _fit_cell_model(seed: int) -> dict[int, pd.Series]:
    """Per-arm outcome rate per cell, fitted on an INDEPENDENT sample (never the evaluated rows)."""
    train = make_uplift_data(20_000, seed=10_000 + seed).frame
    cells = _cells(train)
    return {
        arm: train.loc[train["treatment"] == arm, "reactivated_90d"]
        .groupby(cells[train["treatment"] == arm])
        .mean()
        for arm in (0, 1)
    }


def _predict(model: dict[int, pd.Series], frame: pd.DataFrame, arm: int) -> np.ndarray:
    fallback = float(model[arm].mean())
    return _cells(frame).map(model[arm]).fillna(fallback).to_numpy(dtype=np.float64)


SEEDS = range(30)


def test_ips_and_dr_cover_the_true_policy_value_and_dr_is_narrower() -> None:
    ips_hits = dr_hits = snips_hits = 0
    narrower = 0
    for seed in SEEDS:
        data = make_uplift_data(4_000, seed=seed)
        frame, truth = data.frame, data.truth
        policy = (truth["true_segment"] == "persuadable").to_numpy(dtype=np.float64)
        p1 = truth["p_treated"].to_numpy()
        p0 = truth["p_control"].to_numpy()
        true_value = float(np.mean(policy * p1 + (1.0 - policy) * p0))

        model = _fit_cell_model(seed)
        report = evaluate_policy(
            frame["treatment"].to_numpy(),
            frame["reactivated_90d"].to_numpy(),
            policy,
            p_treated=_predict(model, frame, 1),
            p_control=_predict(model, frame, 0),
            propensity=0.5,
            run_id=RUN_ID,
            description="Treat the true persuadables",
            causal=True,
        )
        _, ips_lo, ips_hi = estimate(report, "ips")
        _, snips_lo, snips_hi = estimate(report, "snips")
        _, dr_lo, dr_hi = estimate(report, "dr")
        ips_hits += ips_lo <= true_value <= ips_hi
        snips_hits += snips_lo <= true_value <= snips_hi
        dr_hits += dr_lo <= true_value <= dr_hi
        narrower += (dr_hi - dr_lo) < (ips_hi - ips_lo)

    # Nominal coverage is 95 %: 28.5 of 30 expected; 26 leaves room for sampling noise.
    assert ips_hits >= 26, ips_hits
    assert snips_hits >= 26, snips_hits
    assert dr_hits >= 26, dr_hits
    assert narrower == len(SEEDS)


def test_dr_stays_unbiased_with_a_wrong_outcome_model() -> None:
    hits = 0
    for seed in SEEDS:
        data = make_uplift_data(4_000, seed=seed, treat_share=0.3)
        frame, truth = data.frame, data.truth
        policy = (truth["true_segment"] == "persuadable").to_numpy(dtype=np.float64)
        true_value = float(
            np.mean(policy * truth["p_treated"].to_numpy() + (1.0 - policy) * truth["p_control"].to_numpy())
        )
        wrong = np.full(len(frame), 0.5)  # the outcome model knows nothing
        report = evaluate_policy(
            frame["treatment"].to_numpy(),
            frame["reactivated_90d"].to_numpy(),
            policy,
            p_treated=wrong,
            p_control=wrong,
            propensity=0.3,
            run_id=RUN_ID,
            description="Treat the true persuadables",
            causal=True,
        )
        _, lo, hi = estimate(report, "dr")
        hits += lo <= true_value <= hi
    assert hits >= 26, hits


def test_treat_all_and_treat_none_recover_the_arm_rates() -> None:
    data = make_uplift_data(20_000, seed=5)
    frame, truth = data.frame, data.truth
    model = _fit_cell_model(5)
    policy, description = policy_from_rule(truth["true_uplift"].to_numpy(), top_share=0.2)
    report = evaluate_policy(
        frame["treatment"].to_numpy(),
        frame["reactivated_90d"].to_numpy(),
        policy,
        p_treated=_predict(model, frame, 1),
        p_control=_predict(model, frame, 0),
        propensity=0.5,
        run_id=RUN_ID,
        description=description,
        causal=True,
    )
    for value, truth_column in (
        (report.treat_all_value, "p_treated"),
        (report.treat_none_value, "p_control"),
    ):
        assert value.ci_low is not None and value.ci_high is not None
        assert value.ci_low <= float(truth[truth_column].mean()) <= value.ci_high
    # Treating the top 20 % by true uplift beats treating no one.
    dr_value = estimate(report, "dr")[0]
    assert dr_value > report.treat_none_value.value
    assert report.policy_treat_share == pytest.approx(0.2)
