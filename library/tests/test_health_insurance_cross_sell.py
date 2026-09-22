"""Health Insurance Cross-Sell: validate + a one-minute train on the committed sample.

Measured over three runs of this exact budget against the engine as it stands today:

    test ROC-AUC   0.8170  0.8437  0.8555
    baseline       0.8205  0.8327  0.8480
    margin        -0.0035 +0.0110 +0.0075
    beats          no      yes     yes

The margin changes sign, so the strict comparison is not asserted; see DEC-410. The full-data run,
on all 381,109 rows, does beat its baseline (0.8581 against 0.8380) and is reported in
../health-insurance-cross-sell/run_report.md.

FLOOR has 0.09 of headroom below the lowest of the three.

Licence: reported as GNU GPL v2 and unverified. See ../health-insurance-cross-sell/LICENSE.txt
before this dataset appears in anything customer-facing.
"""

from __future__ import annotations

import pytest

from conftest import BASELINE_TOLERANCE, SampleRun, assert_trained_and_validated, train_on_sample

pytestmark = [pytest.mark.slow, pytest.mark.integration]

FLOOR = 0.72


@pytest.fixture(scope="module")
def sample_run(tmp_path_factory: pytest.TempPathFactory) -> SampleRun:
    return train_on_sample(
        runs_dir=tmp_path_factory.mktemp("runs"),
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
