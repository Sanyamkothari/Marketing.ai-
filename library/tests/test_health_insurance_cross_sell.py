"""Health Insurance Cross-Sell: validate + a one-minute train on the committed sample.

Measured over three runs of this exact budget:

    test ROC-AUC   0.8662  0.8247  0.8073
    baseline       0.8503  0.8234  0.8127
    beats          yes     yes     no

The second run's margin is +0.0013. The strict comparison is not asserted; see DEC-410. The
full-data run, on all 381,109 rows, does beat its baseline (0.8555 against 0.8371) and is reported
in ../health-insurance-cross-sell/run_report.md.

Licence: reported as GNU GPL v2 and unverified. See ../health-insurance-cross-sell/LICENSE.txt
before this dataset appears in anything customer-facing.
"""

from __future__ import annotations

import pytest

from conftest import BASELINE_TOLERANCE, SampleRun, assert_trained_and_validated, train_on_sample

pytestmark = [pytest.mark.slow, pytest.mark.integration]

FLOOR = 0.72


@pytest.fixture(scope="module")
def sample_run() -> SampleRun:
    return train_on_sample(
        dataset="health-insurance-cross-sell",
        use_case="insurance-cross-sell",
        sample="health-insurance-cross-sell/sample.csv",
        primary_key="id",
        target="Response",
    )


def test_the_run_finished_and_validated(sample_run: SampleRun) -> None:
    assert_trained_and_validated(sample_run)


def test_the_published_file_raises_no_findings_at_all(sample_run: SampleRun) -> None:
    """Like the Telco file, this one is accepted exactly as published."""
    assert sample_run.codes == [], sample_run.validation["checks"]


def test_the_model_ranks_far_better_than_chance(sample_run: SampleRun) -> None:
    assert sample_run.metric("roc_auc") >= FLOOR, sample_run.metric("roc_auc")


def test_the_model_is_not_materially_worse_than_the_baseline(sample_run: SampleRun) -> None:
    model, baseline = sample_run.metric("roc_auc"), sample_run.baseline("roc_auc")
    assert model >= baseline - BASELINE_TOLERANCE, f"model {model} vs baseline {baseline}"
