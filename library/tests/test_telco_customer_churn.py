"""Telco Customer Churn: validate + a one-minute train on the committed sample.

Measured over three runs of this exact budget (`strategy: fast`, `time_limit_minutes: 1`, the
5,000-row `sample.csv`):

    test ROC-AUC   0.8444  0.8363  0.8550
    baseline       0.8334  0.8376  0.8472
    beats          yes     no      yes

So the strict comparison is not asserted here; see DEC-410. The full-data run *does* beat its
baseline (0.8573 against 0.8535) and is reported in ../telco-customer-churn/run_report.md.
"""

from __future__ import annotations

import pytest

from conftest import BASELINE_TOLERANCE, SampleRun, assert_trained_and_validated, train_on_sample

pytestmark = [pytest.mark.slow, pytest.mark.integration]

FLOOR = 0.76


@pytest.fixture(scope="module")
def sample_run() -> SampleRun:
    return train_on_sample(
        dataset="telco-customer-churn",
        use_case="telco-churn",
        sample="telco-customer-churn/sample.csv",
        primary_key="customerID",
        target="Churn",
        config_root=None,  # this use case ships in the repo's own configs/
    )


def test_the_run_finished_and_validated(sample_run: SampleRun) -> None:
    assert_trained_and_validated(sample_run)


def test_the_published_file_raises_no_findings_at_all(sample_run: SampleRun) -> None:
    """The point of this dataset: the Kaggle file is accepted exactly as published."""
    assert sample_run.codes == [], sample_run.validation["checks"]


def test_the_model_ranks_far_better_than_chance(sample_run: SampleRun) -> None:
    assert sample_run.metric("roc_auc") >= FLOOR, sample_run.metric("roc_auc")


def test_the_model_is_not_materially_worse_than_the_baseline(sample_run: SampleRun) -> None:
    model, baseline = sample_run.metric("roc_auc"), sample_run.baseline("roc_auc")
    assert model >= baseline - BASELINE_TOLERANCE, f"model {model} vs baseline {baseline}"
