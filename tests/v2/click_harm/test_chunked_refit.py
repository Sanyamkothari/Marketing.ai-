"""The contact-harm refit in blocks, and leaving alone read on the same number the logs count.

The refit reads a customers x 241 ratio grid; at 100,000 customers the whole grid is about 190 MB.
It now goes through the customers a block at a time, so peak memory is a few blocks whatever the
population, and gives the same answers as reading them all at once."""

from __future__ import annotations

import math

import numpy as np
import numpy.typing as npt
import pytest

from autonomy.decide.bidders import (
    HARM_BLOCK,
    LEAVE_ALONE_CREDIBLE,
    ContactHarm,
    leave_alone,
)
from autonomy.decide.bidders import _OwnRecords as OwnRecords
from autonomy.learning.observations import ContactEvidence


def population(n: int = 400, seed: int = 11) -> dict[str, ContactEvidence]:
    """A shop where a fifth of customers buy a third as much after a message, with long records,
    and a holdout-free history (some customers have no purchases at all)."""
    rng = np.random.default_rng(seed)
    out: dict[str, ContactEvidence] = {}
    for i in range(n):
        bad = bool(rng.random() < 0.2)
        rate = float(rng.gamma(2.0, 0.04))
        sends = int(rng.integers(10, 40))
        exposed = min(sends * 7.0 * 0.6, 300.0)
        open_ = 365.0 - exposed
        ratio = 0.33 if bad else 1.25
        out[f"c{i:04d}"] = ContactEvidence(
            exposed_days=exposed,
            open_days=open_,
            exposed_purchases=float(rng.poisson(rate * ratio * exposed)),
            open_purchases=float(rng.poisson(rate * open_)),
            sends=float(sends),
            clicks=float(rng.binomial(sends, 0.01 if bad else 0.2)),
            stops=float(rng.random() < (0.2 if bad else 0.03)),
            direct_visits=float(rng.poisson(365 * (0.04 if bad else 0.02))),
        )
    return out


class Counting(ContactHarm):
    """Records the largest number of grid rows any one step of the refit asked for."""

    def __init__(self, block: int) -> None:
        super().__init__(block=block)
        self.most_rows = 0

    def _rows(self, records: OwnRecords, idx: npt.NDArray[np.int64]) -> npt.NDArray[np.float64]:
        self.most_rows = max(self.most_rows, len(idx))
        return super()._rows(records, idx)


def fitted(evidence: dict[str, ContactEvidence], block: int) -> ContactHarm:
    harm = ContactHarm(block=block)
    harm.observe(evidence)
    harm.refit()
    return harm


@pytest.mark.parametrize("block", [1, 7, 64, 399])
def test_the_refit_in_blocks_gives_the_same_answers_as_all_at_once(block: int) -> None:
    evidence = population()
    whole, parts = fitted(evidence, block=10**6), fitted(evidence, block=block)
    assert whole.fit is not None and parts.fit is not None
    assert parts.fit.tau == whole.fit.tau and parts.fit.coefficients == whole.fit.coefficients
    assert parts.fit.spread == pytest.approx(whole.fit.spread, abs=1e-12)
    assert parts.fit.band_ratios == pytest.approx(whole.fit.band_ratios, abs=1e-12)
    assert parts.fit.band_dents == pytest.approx(whole.fit.band_dents, abs=1e-12)
    assert dict(parts.fit.segment_ratios) == pytest.approx(dict(whole.fit.segment_ratios), abs=1e-12)
    for cid in evidence:
        assert parts.p_harm(cid) == pytest.approx(whole.p_harm(cid), abs=1e-12)
        assert parts.dent(cid) == pytest.approx(whole.dent(cid), abs=1e-12)
        assert parts.near_dent(cid) == pytest.approx(whole.near_dent(cid), abs=1e-12)
        assert parts.posterior(cid) == pytest.approx(whole.posterior(cid), abs=1e-12)
        assert parts.purchase_shift(cid, 0.1, 0.03, 0.01) == pytest.approx(
            whole.purchase_shift(cid, 0.1, 0.03, 0.01), abs=1e-12
        )
    # the same people are left alone
    left = {c for c in evidence if leave_alone(whole, c, 1.0)}
    assert left == {c for c in evidence if leave_alone(parts, c, 1.0)}
    assert left, "the population is meant to have customers clearly put off"


def test_no_step_of_the_refit_holds_more_than_two_blocks_of_the_grid() -> None:
    evidence = population(n=500)
    harm = Counting(block=16)
    harm.observe(evidence)
    harm.refit()
    assert harm.fitted and 16 <= harm.most_rows <= 2 * 16
    # the neighbours' window (a band's worth: 25 customers either side) is wider than a block,
    # and still read through blocks
    assert 500 // (2 * harm.bands) > 16
    assert HARM_BLOCK * 241 * 8 <= 8_000_000  # a default block's rows are a few MB


def test_block_must_be_positive() -> None:
    with pytest.raises(ValueError):
        ContactHarm(block=0)


# --- leaving alone and the count in the logs agree -----------------------------------------------------


def test_leave_alone_and_likely_agree_at_the_bar_on_a_fitted_model() -> None:
    harm = fitted(population(), block=HARM_BLOCK)
    ids = sorted(harm.evidence)
    left = [c for c in ids if leave_alone(harm, c, 1.0)]
    assert left
    assert harm.likely(LEAVE_ALONE_CREDIBLE) == len(left)
    assert all(harm.p_harm(c) >= LEAVE_ALONE_CREDIBLE for c in left)
    assert all(harm.p_harm(c) < LEAVE_ALONE_CREDIBLE for c in ids if c not in left)
    assert f"{len(left)} look clearly put off" in harm.describe()


class OnTheBar(ContactHarm):
    """A posterior whose Normal summary and exact grid fall on either side of the bar: a skewed
    grid posterior with exactly 90% below a ratio of 1, whose mean and sd alone put 85% there."""

    def __init__(self) -> None:
        super().__init__()
        sd = 0.5
        mean = -sd * 1.0364  # P(Normal < 0) = 0.85
        self._post = {"c1": (mean, sd), "c2": (mean, sd)}
        self._p = {"c1": LEAVE_ALONE_CREDIBLE, "c2": LEAVE_ALONE_CREDIBLE - 1e-9}


def test_leave_alone_reads_the_exact_grid_not_the_normal_summary() -> None:
    harm = OnTheBar()
    assert harm.p_below("c1", 1.0) == pytest.approx(0.85, abs=0.001)
    assert leave_alone(harm, "c1", 1.0) and not leave_alone(harm, "c2", 1.0)
    assert harm.likely(LEAVE_ALONE_CREDIBLE) == 1  # exactly the one left alone
    assert not leave_alone(harm, "c1", 0.0)  # nothing at stake


def test_a_hand_made_posterior_without_a_grid_still_leaves_alone() -> None:
    harm = ContactHarm()
    harm._post = {"c1": (math.log(0.5), 0.2)}
    assert harm.p_harm("c1") == pytest.approx(harm.p_below("c1", 1.0))
    assert leave_alone(harm, "c1", 1.0) and harm.likely(LEAVE_ALONE_CREDIBLE) == 1
    assert harm.p_harm("stranger") == 0.0 and not leave_alone(harm, "stranger", 1.0)
