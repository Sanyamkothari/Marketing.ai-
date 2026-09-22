"""UCI Bank Marketing: validate + a one-minute train on the committed sample.

Measured over three runs of this exact budget:

    test ROC-AUC   0.9347  0.9350  0.9388
    baseline       0.9267  0.9305  0.9254
    beats          yes     yes     yes      (margins +0.0080, +0.0045, +0.0134)

Three for three, but the smallest margin is 0.0045, which is inside the noise of a sixty-second
search. The strict comparison is therefore not asserted; see DEC-410.

These runs include `duration`, the column that is only known once the call has ended. That is
deliberate: this test checks the engine still trains on the published file. The realistic model,
with `prepare.exclude_columns: ["duration"]`, is in ../uci-bank-marketing/run_report.md.
"""

from __future__ import annotations

import pytest

from conftest import BASELINE_TOLERANCE, SampleRun, assert_trained_and_validated, train_on_sample

pytestmark = [pytest.mark.slow, pytest.mark.integration]

FLOOR = 0.88


@pytest.fixture(scope="module")
def sample_run() -> SampleRun:
    return train_on_sample(
        dataset="uci-bank-marketing",
        use_case="bank-term-deposit",
        sample="uci-bank-marketing/sample.csv",
        primary_key="client_id",
        target="y",
    )


def test_the_run_finished_and_validated(sample_run: SampleRun) -> None:
    assert_trained_and_validated(sample_run)


def test_the_string_target_is_read_without_a_run_override(sample_run: SampleRun) -> None:
    """`y` holds "yes"/"no"; `target.positive_label: "yes"` in the use-case file is all it takes."""
    assert sample_run.results["evaluation"]["positive_rate"] == pytest.approx(0.115, abs=0.02)


def test_the_model_ranks_far_better_than_chance(sample_run: SampleRun) -> None:
    assert sample_run.metric("roc_auc") >= FLOOR, sample_run.metric("roc_auc")


def test_the_model_is_not_materially_worse_than_the_baseline(sample_run: SampleRun) -> None:
    model, baseline = sample_run.metric("roc_auc"), sample_run.baseline("roc_auc")
    assert model >= baseline - BASELINE_TOLERANCE, f"model {model} vs baseline {baseline}"
