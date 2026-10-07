"""The statistical harness works: collected only on request, marked, seeded (Plan J M90).

M95 puts the real coverage, false-positive and power tests beside this file. This one proves the
mechanism: it runs under `make test-statistical` and nowhere else, and it is deterministic.
"""

from __future__ import annotations

import numpy as np
import pytest

pytestmark = pytest.mark.statistical

SEED = 20261007


def test_a_fixed_seed_gives_the_same_draws_twice() -> None:
    first = np.random.default_rng(SEED).binomial(1, 0.05, size=10_000)
    second = np.random.default_rng(SEED).binomial(1, 0.05, size=10_000)
    assert (first == second).all()


def test_the_simulated_rate_is_within_four_standard_errors() -> None:
    n, p = 100_000, 0.05
    rate = np.random.default_rng(SEED).binomial(1, p, size=n).mean()
    assert abs(rate - p) <= 4 * np.sqrt(p * (1 - p) / n)
