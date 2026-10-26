"""The always-valid interval keeps its error rate under repeated looks; the plain one does not."""

from __future__ import annotations

import numpy as np
import pytest

from autonomy.experiments.sequential import half_width, sequential_difference
from autonomy.proof.estimate import Z_95


def test_it_is_wider_than_the_single_look_interval_and_shrinks_with_data() -> None:
    plain = Z_95 * np.sqrt(2 / 1000)
    wide = half_width(2 / 1000, prior_sd=0.1, alpha=0.05)
    assert wide > plain
    assert half_width(2 / 4000, prior_sd=0.1, alpha=0.05) < wide


def test_a_smaller_alpha_is_wider() -> None:
    assert half_width(0.002, 0.1, 0.01) > half_width(0.002, 0.1, 0.05)


def test_it_refuses_nonsense_settings() -> None:
    with pytest.raises(ValueError, match="strictly between"):
        half_width(0.1, 0.1, 0.0)
    with pytest.raises(ValueError, match="above zero"):
        half_width(0.1, 0.0, 0.05)
    assert sequential_difference(np.array([1.0]), np.array([1.0, 2.0]), prior_sd=1.0, alpha=0.05) is None


def test_a_real_difference_is_found_and_none_is_found_when_there_is_none() -> None:
    rng = np.random.default_rng(0)
    a, b = rng.normal(1.0, 1.0, 500), rng.normal(0.0, 1.0, 500)
    found = sequential_difference(a, b, prior_sd=1.0, alpha=0.05)
    assert found is not None and found.low > 0
    c = rng.normal(0.0, 1.0, 500)
    null = sequential_difference(c, b, prior_sd=1.0, alpha=0.05)
    assert null is not None and null.low < 0 < null.high


def test_peeking_every_fifty_customers_keeps_the_false_alarm_rate_near_alpha() -> None:
    """No true difference; look after every 50 customers per arm up to 2,000 (40 looks) and call it a
    finding the first time the interval excludes zero. The plain 95% interval does that in a large share of
    runs; the always-valid one stays at or below alpha (0.05)."""
    rng = np.random.default_rng(7)
    runs, looks, step = 400, 40, 50
    a = rng.normal(0.0, 1.0, (runs, looks * step))
    b = rng.normal(0.0, 1.0, (runs, looks * step))
    n = np.arange(step, looks * step + 1, step)
    ma, mb = np.cumsum(a, axis=1)[:, n - 1] / n, np.cumsum(b, axis=1)[:, n - 1] / n
    va = (np.cumsum(a**2, axis=1)[:, n - 1] - n * ma**2) / (n - 1)
    vb = (np.cumsum(b**2, axis=1)[:, n - 1] - n * mb**2) / (n - 1)
    variance = (va + vb) / n
    diff = ma - mb
    plain_alarm = (np.abs(diff) > Z_95 * np.sqrt(variance)).any(axis=1).mean()
    valid_alarm = (
        np.array(
            [
                [abs(diff[r, k]) > half_width(variance[r, k], 0.3, 0.05) for k in range(looks)]
                for r in range(runs)
            ]
        )
        .any(axis=1)
        .mean()
    )
    assert plain_alarm > 0.15
    assert valid_alarm <= 0.05
