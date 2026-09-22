"""UCI Default of Credit Card Clients: validate + a one-minute train on the committed sample.

The one dataset in the library whose margin over the baseline is clear of the noise, so this is
the one test that asserts the strict comparison the brief asks for. Measured over three runs of
this exact budget:

    test ROC-AUC   0.7509  0.7651  0.7746
    baseline       0.7057  0.7146  0.7288
    beats          yes     yes     yes      (margins +0.0452, +0.0505, +0.0458)

An order of magnitude clear of the largest shortfall seen anywhere else in the library (0.0065).
The full-data run agrees: 0.7869 against 0.7245. See DEC-410.
"""

from __future__ import annotations

import pytest

from conftest import BASELINE_TOLERANCE, SampleRun, assert_trained_and_validated, train_on_sample

pytestmark = [pytest.mark.slow, pytest.mark.integration]

FLOOR = 0.70


@pytest.fixture(scope="module")
def sample_run() -> SampleRun:
    return train_on_sample(
        dataset="uci-credit-default",
        use_case="card-default-propensity",
        sample="uci-credit-default/sample.csv",
        primary_key="ID",
        target="default_payment_next_month",
    )


def test_the_run_finished_and_validated(sample_run: SampleRun) -> None:
    assert_trained_and_validated(sample_run)


def test_the_model_ranks_far_better_than_chance(sample_run: SampleRun) -> None:
    assert sample_run.metric("roc_auc") >= FLOOR, sample_run.metric("roc_auc")


def test_the_model_beats_the_baseline(sample_run: SampleRun) -> None:
    """Strictly, not within a tolerance: this dataset's margin is reproducible."""
    model, baseline = sample_run.metric("roc_auc"), sample_run.baseline("roc_auc")
    assert model > baseline, f"model {model} vs baseline {baseline}"
    assert sample_run.beats_baseline


def test_the_model_is_not_materially_worse_than_the_baseline(sample_run: SampleRun) -> None:
    model, baseline = sample_run.metric("roc_auc"), sample_run.baseline("roc_auc")
    assert model >= baseline - BASELINE_TOLERANCE, f"model {model} vs baseline {baseline}"
