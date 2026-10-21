"""Holm-Bonferroni across the bench's comparisons: known values, raw and corrected side by side."""

from __future__ import annotations

import copy
import math
from typing import Any

import pytest

from autonomy.proof.estimate import Z_95
from scripts.v2_realbench.common import (
    NON_INFERIORITY_SHARE,
    add_multiplicity,
    bonferroni_interval,
    holm_adjust,
    multiplicity,
    normal_two_sided_p,
)
from scripts.v2_realbench.run import render, render_multiplicity


def est(point: float, se: float) -> dict[str, float]:
    """A paired difference with a normal 95% interval, as `ope.mean_interval` reports it."""
    return {"point": point, "low": point - Z_95 * se, "high": point + Z_95 * se}


def test_holm_adjust_known_values() -> None:
    # sorted: 0.005 x 4 = 0.02, 0.01 x 3 = 0.03, 0.03 x 2 = 0.06, 0.04 x 1 = 0.04 -> running max 0.06
    assert holm_adjust([0.01, 0.04, 0.03, 0.005]) == pytest.approx([0.03, 0.06, 0.06, 0.02])
    # the textbook case: m = 3
    assert holm_adjust([0.01, 0.02, 0.03]) == pytest.approx([0.03, 0.04, 0.04])
    # capped at 1, and never below the raw p
    assert holm_adjust([0.5, 0.9]) == pytest.approx([1.0, 1.0])
    assert holm_adjust([0.2]) == [0.2]
    assert holm_adjust([]) == []
    ps = [0.001, 0.2, 0.049, 0.0004, 0.6]
    adj = holm_adjust(ps)
    assert all(a >= p for a, p in zip(adj, ps, strict=True))
    order = sorted(range(len(ps)), key=lambda i: ps[i])
    assert [adj[i] for i in order] == sorted(adj)  # monotone in the raw order


def test_normal_p_value() -> None:
    assert normal_two_sided_p(Z_95) == pytest.approx(0.05, abs=1e-6)
    assert normal_two_sided_p(0.0) == pytest.approx(1.0)
    assert normal_two_sided_p(-2.5) == normal_two_sided_p(2.5)
    assert normal_two_sided_p(10.0) < 1e-20


def test_bonferroni_interval_widens_with_the_family() -> None:
    e = est(0.3, 0.1)
    same = bonferroni_interval(e, 1)
    assert same["low"] == pytest.approx(e["low"], abs=1e-6) and same["high"] == pytest.approx(
        e["high"], abs=1e-6
    )
    wide = bonferroni_interval(e, 10)
    assert wide["level"] == pytest.approx(0.995)
    assert wide["point"] == e["point"]
    z10 = 2.8070337683  # standard normal quantile at 1 - 0.05 / 20
    assert wide["high"] - wide["point"] == pytest.approx(z10 * 0.1, rel=1e-6)
    assert wide["low"] < e["low"] and wide["high"] > e["high"]


def test_multiplicity_reports_raw_and_corrected() -> None:
    fam = {"a": est(0.30, 0.10), "b": est(0.21, 0.10), "c": est(0.01, 0.10)}
    out = multiplicity(fam)
    assert out["m"] == 3 and out["alpha"] == 0.05
    a, b, c = (out["comparisons"][k] for k in "abc")
    # raw: a (z 3.0) and b (z 2.1) are significant; c is not
    assert [a["significant_raw"], b["significant_raw"], c["significant_raw"]] == [True, True, False]
    assert a["raw"]["p"] == pytest.approx(math.erfc(3.0 / math.sqrt(2)))
    # Holm: a stays (adjusted p 0.0081), b goes (0.036 x ... = 0.07 > 0.05)
    assert a["holm_p"] == pytest.approx(3 * a["raw"]["p"])
    assert a["significant_holm"] is True and b["significant_holm"] is False
    assert b["holm_p"] == pytest.approx(max(2 * b["raw"]["p"], a["holm_p"]))
    # the corrected interval is wider than the raw one and keeps the point
    for c_ in (a, b, c):
        assert c_["corrected"]["low"] < c_["raw"]["low"] and c_["corrected"]["high"] > c_["raw"]["high"]
        assert c_["corrected"]["level"] == pytest.approx(1 - 0.05 / 3)
    assert multiplicity({})["m"] == 0


def test_a_degenerate_interval_does_not_divide_by_zero() -> None:
    out = multiplicity({"same": est(0.0, 0.0), "diff": est(1.0, 0.0)})
    assert out["comparisons"]["same"]["raw"]["p"] == 1.0
    assert out["comparisons"]["diff"]["raw"]["p"] == 0.0


def _policy(value: float, vs_agent: tuple[float, float]) -> dict[str, Any]:
    return {"dr": est(value, 0.01), "agent_minus_this": est(*vs_agent)}


def _block(
    best: str, best_value: float, agent_vs_best: tuple[float, float], signal_low: float, within: bool | None
) -> dict[str, Any]:
    return {
        "policies": {
            "send to none": _policy(1.0, (0.40, 0.05)),
            best: _policy(best_value, agent_vs_best),
            "T-learner (logistic)": _policy(best_value, (0.0, 0.05)),
            "AGENT": {"dr": est(best_value, 0.01), "agent_minus_this": est(0.0, 0.0)},
        },
        "best_simple": {"name": best},
        "feature_signal": {
            "t_learner_minus_best_simple": {
                "point": signal_low + 0.1,
                "low": signal_low,
                "high": signal_low + 0.2,
            }
        },
        "calibration": {"within_2x": within},
    }


def synthetic_report() -> dict[str, Any]:
    return {
        "datasets": {
            "d1": _block("send to all", 10.0, (-0.10, 0.02), -0.05, True),
            "d2": _block("segment rule", 2.0, (0.50, 0.05), 0.20, False),
            "broken": {"error": "MemoryError: x"},
        }
    }


def test_add_multiplicity_builds_both_families_and_the_corrected_verdicts() -> None:
    rep = synthetic_report()
    add_multiplicity(rep)
    # agent vs every other policy: 3 per dataset (none, best, T-learner); the 'AGENT' row is not a comparison
    assert rep["datasets"]["d1"]["multiplicity"]["m"] == 3
    assert rep["multiplicity"]["m"] == 6
    assert "d1: agent minus send to all" in rep["multiplicity"]["comparisons"]
    assert "AGENT" not in " ".join(rep["multiplicity"]["comparisons"])
    assert "multiplicity" not in rep["datasets"]["broken"]
    v1, v2 = rep["datasets"]["d1"]["verdict_corrected"], rep["datasets"]["d2"]["verdict_corrected"]
    # d1: agent is -0.10 +/- 0.02: corrected low about -0.10 - 2.64 x 0.02 = -0.153; tolerance 2% x 10 = 0.2
    assert v1["tolerance"] == pytest.approx(NON_INFERIORITY_SHARE * 10.0)
    assert v1["corrected_low_vs_best"] == pytest.approx(-0.10 - 0.02 * 2.6383, rel=1e-3)  # z at 1 - 0.05 / 12
    assert v1["non_inferior_to_best_simple"] is True
    assert v1["features_carry_signal"] is False and v1["beats_best_simple_where_signal"] is None
    # d2: signal shown (T-learner low 0.20 > 0): the agent must beat the best simple policy, and does
    assert v2["features_carry_signal"] is True and v2["beats_best_simple_where_signal"] is True
    assert v2["calibration_within_2x"] is False and v1["calibration_within_2x"] is True


def test_non_inferiority_fails_when_the_agent_is_clearly_worse() -> None:
    rep = synthetic_report()
    rep["datasets"]["d1"] = _block("send to all", 10.0, (-1.0, 0.05), -0.05, True)
    add_multiplicity(rep)
    assert rep["datasets"]["d1"]["verdict_corrected"]["non_inferior_to_best_simple"] is False


def test_the_report_family_is_wider_than_one_dataset_alone() -> None:
    both = synthetic_report()
    add_multiplicity(both)
    alone = copy.deepcopy(synthetic_report())
    del alone["datasets"]["d2"]
    add_multiplicity(alone)
    low_both = both["datasets"]["d1"]["verdict_corrected"]["corrected_low_vs_best"]
    low_alone = alone["datasets"]["d1"]["verdict_corrected"]["corrected_low_vs_best"]
    assert low_both < low_alone  # more comparisons, more conservative


def test_render_shows_raw_and_corrected_side_by_side() -> None:
    rep = synthetic_report()
    add_multiplicity(rep)
    text = render_multiplicity(rep)
    for needle in ("Raw diff [95% CI]", "Raw p", "Holm p", "Corrected interval", "Significant (raw / Holm)"):
        assert needle in text
    assert "d1: agent minus send to all" in text and "Non-inferior" in text
    assert "Holm-Bonferroni" in text
    # the full scoreboard render ends with the same table once a report carries the family
    errored_only = {**rep, "datasets": {k: v for k, v in rep["datasets"].items() if "error" in v}}
    assert "### Multiple comparisons" in render(errored_only)


def test_the_bench_json_paths_the_targets_use_exist() -> None:
    rep = synthetic_report()
    add_multiplicity(rep)
    v = rep["datasets"]["d1"]["verdict_corrected"]
    for key in ("non_inferior_to_best_simple", "beats_best_simple_where_signal", "calibration_within_2x"):
        assert key in v
