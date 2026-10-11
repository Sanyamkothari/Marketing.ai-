"""A planted best message is found, within its interval, and an equal one is not named (Plan K M140, DEC-1407).

Each simulated campaign is drawn the way a run with message arms is: a fifth of the customers are held back
(no message), and every other customer gets the control template or one of two variants by the engine's own
keyed draw (`engine.decide.variant_arms.draw_arms`, the run's seed and the customer's key). Each customer's
outcome happens at the rate planted for their arm. The campaign is then measured through the one path
(`measure_campaign` with `variant_measure_arguments`, as `engine.measurement.cycle.measure_recorded` does) and the
messages are compared with `compare_variant_arms`.

Checked, against the M95 band (four Monte Carlo standard errors, `tests/statistical/bands.py`):

* **found within its interval.** With variant B planted best, the 95% interval of B against no message covers
  B's true effect at the nominal rate, every pair's interval at the family's level covers its true difference
  together at least 95% of the time (Bonferroni over the three pairs), a message named the winner is never one
  that is not the best, and B is named in most campaigns (the power is printed);
* **no false winner.** With the three messages equally good, a winner is named in at most 5% of campaigns (the
  family's error rate the comparison claims).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import numpy as np
import pandas as pd
import pytest

from engine.decide.variant_arms import (
    CONTROL_TEMPLATE_LEVEL,
    FAMILY_ALPHA,
    NO_MESSAGE_LEVEL,
    VARIANT_ARM_COLUMN,
    compare_variant_arms,
    draw_arms,
    variant_measure_arguments,
)
from engine.measurement.measure import measure_campaign
from engine.measurement.simulate import AS_OF, OUTCOME_WINDOW_DAYS
from tests.statistical.bands import assert_share_in_band, band, seeds

pytestmark = pytest.mark.statistical

SIMS = 1_000
NOMINAL = 0.95
CUSTOMERS = 6_000
HELD_BACK = 0.20
BASE = 0.10
MESSAGES = (CONTROL_TEMPLATE_LEVEL, "variant_a", "variant_b")
PLANTED = {CONTROL_TEMPLATE_LEVEL: 0.02, "variant_a": 0.02, "variant_b": 0.08}
EQUAL = {CONTROL_TEMPLATE_LEVEL: 0.03, "variant_a": 0.03, "variant_b": 0.03}
KEYS = [f"C{index:07d}" for index in range(CUSTOMERS)]


def _campaign(effects: dict[str, float], seed: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    held = rng.random(CUSTOMERS) < HELD_BACK
    arm = np.full(CUSTOMERS, None, dtype=object)
    chosen = np.flatnonzero(~held)
    arm[chosen] = draw_arms([KEYS[i] for i in chosen], MESSAGES, seed=seed)
    rate = np.full(CUSTOMERS, BASE)
    for level, effect in effects.items():
        rate[arm == level] += effect
    assignment = pd.DataFrame(
        {
            "customer_id": KEYS,
            "control_group": held,
            "suppressed_reason": np.full(CUSTOMERS, "", dtype=object),
            "intended_treatment": np.ones(CUSTOMERS, dtype=bool),
            VARIANT_ARM_COLUMN: pd.Series(arm, dtype="string"),
        }
    )
    outcomes = pd.DataFrame(
        {"customer_id": KEYS, "converted": (rng.random(CUSTOMERS) < rate).astype(np.int64)}
    )
    return assignment, outcomes


def _measure(assignment: pd.DataFrame, outcomes: pd.DataFrame) -> Any:
    return measure_campaign(
        assignment,
        outcomes,
        run_id="simulated",
        primary_key="customer_id",
        outcome_column="converted",
        treatment_time=AS_OF - timedelta(days=OUTCOME_WINDOW_DAYS + 1),
        outcome_window_days=OUTCOME_WINDOW_DAYS,
        as_of=AS_OF,
        **variant_measure_arguments(assignment),
    )


def test_a_planted_best_message_is_found_within_its_interval() -> None:
    covered = together = found = wrong = 0
    for seed in seeds(140_001, SIMS):
        report = _measure(*_campaign(PLANTED, seed))
        assert report.arms is not None and [arm.arm for arm in report.arms] == list(MESSAGES)
        assert all(arm.control == NO_MESSAGE_LEVEL for arm in report.arms)
        best = report.arms[2].effect
        assert best is not None and best.ci_low is not None and best.ci_high is not None
        covered += best.ci_low <= PLANTED["variant_b"] <= best.ci_high
        comparison = compare_variant_arms(report)
        assert comparison is not None
        together += all(
            pair.difference.ci_low is not None
            and pair.difference.ci_high is not None
            and pair.difference.ci_low
            <= PLANTED[pair.first] - PLANTED[pair.second]
            <= pair.difference.ci_high
            for pair in comparison.pairs
        )
        found += comparison.winner == "variant_b"
        wrong += comparison.winner is not None and comparison.winner != "variant_b"
    print(f"\n[M140] planted best message named in {found / SIMS:.1%} of {SIMS} campaigns")
    assert_share_in_band(covered / SIMS, NOMINAL, SIMS, what="coverage of the best message's effect")
    low, _ = band(NOMINAL, SIMS)
    assert together / SIMS >= low, f"the pairs' intervals held together in only {together / SIMS:.4f}"
    assert wrong == 0, f"a message that is not the best was named the winner {wrong} times"
    assert found / SIMS >= 0.9, f"the planted best message was named in only {found / SIMS:.1%} of campaigns"


def test_no_winner_is_named_among_equally_good_messages_beyond_the_family_rate() -> None:
    named = 0
    for seed in seeds(140_002, SIMS):
        comparison = compare_variant_arms(_measure(*_campaign(EQUAL, seed)))
        assert comparison is not None
        named += comparison.winner is not None
    _, high = band(FAMILY_ALPHA, SIMS)
    print(f"\n[M140] a winner was named among equal messages in {named / SIMS:.2%} of {SIMS} campaigns")
    assert named / SIMS <= high, f"a winner was named in {named / SIMS:.4f} of campaigns with no best message"
