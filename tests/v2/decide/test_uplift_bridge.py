"""Cross-checks of v2's proof numbers against v1's OPE and mean difference."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest

from autonomy.bridges.uplift_bridge import compare_mean_difference, compare_ope

pytestmark = pytest.mark.v1bridge


def test_binary_ope_matches_v1() -> None:
    rng = np.random.default_rng(9)
    n = 3000
    t = (rng.random(n) < 0.4).astype(float)
    mu0 = rng.uniform(0.05, 0.2, n)
    mu1 = np.clip(mu0 + rng.normal(0.03, 0.02, n), 0, 1)
    y = (rng.random(n) < np.where(t == 1, mu1, mu0)).astype(float)
    policy = (mu1 - mu0 > 0.03).astype(float)
    check = compare_ope(
        t, y, policy, p_treated=mu1, p_control=mu0, propensity=0.4, now=datetime(2026, 1, 1, tzinfo=UTC)
    )
    assert set(check.v1) == set(check.v2) == {"ips", "snips", "dr"}
    assert check.max_gap() < 1e-9
    for method in ("ips", "dr"):
        assert check.v1[method].low == pytest.approx(check.v2[method].low, abs=1e-9)
        assert check.v1[method].high == pytest.approx(check.v2[method].high, abs=1e-9)


def test_mean_difference_close_to_v1() -> None:
    rng = np.random.default_rng(4)
    a, b = rng.gamma(2.0, 20.0, 500), rng.gamma(2.0, 18.0, 400)
    check = compare_mean_difference(a, b)
    assert check.v1 is not None and check.v2 is not None
    assert check.v1.point == pytest.approx(check.v2.point)
    # t vs normal quantile: v1 is a touch wider, within a few percent at these sizes.
    assert check.v1.low <= check.v2.low and check.v1.high >= check.v2.high
    assert check.v2.high - check.v2.low == pytest.approx(check.v1.high - check.v1.low, rel=0.01)
