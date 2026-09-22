"""UCI Online Retail win-back: validate + a one-minute train on the committed sample.

**This is the dataset that does not work, and this module asserts that fact rather than hiding
it.** Unlike the other four, it makes no assertion at all about the model's score, because on this
data at this budget there is no score worth asserting.

Measured over six runs of exactly this budget (`strategy: fast`, `time_limit_minutes: 1`, all
1,463 prepared rows, of which 219 are the test split):

    test ROC-AUC   0.6434  0.6370  0.6339  0.5784  0.5920  0.5307
    baseline       0.6136  0.6146  0.6404  0.6009  0.5987  0.5934
    beats          yes     no      no      no      no      no

The spread is 0.53 to 0.64, and the gap to the baseline swings from +0.030 to -0.063. The full-data
run agrees with the majority: `model_beats_baseline: false`, PR-AUC 0.4982 against 0.5027.

Any floor low enough to pass reliably would be a number tuned until it stopped failing, which is
precisely what DEC-410 rules out. So this module asserts the things that *are* reproducible - the
pipeline runs end to end on a file built by aggregating a transaction log, the validator says the
one true thing there is to say about it, and the derived target is sound - and leaves the honest
negative where it belongs, in ../online-retail/run_report.md and in docs/LIBRARY.md.

The sample here is all 1,463 prepared rows: the whole file is already under the 5,000-row limit.
"""

from __future__ import annotations

import pytest

from conftest import SampleRun, assert_trained_and_validated, train_on_sample

pytestmark = [pytest.mark.slow, pytest.mark.integration]


@pytest.fixture(scope="module")
def sample_run() -> SampleRun:
    return train_on_sample(
        dataset="online-retail",
        use_case="retail-win-back",
        sample="online-retail/sample.csv",
        primary_key="customer_id",
        target="reactivated_90d",
    )


def test_the_run_finished_and_validated(sample_run: SampleRun) -> None:
    """A transaction log aggregated by fetch.py goes through the whole train flow."""
    assert_trained_and_validated(sample_run)


def test_the_single_snapshot_is_reported_as_a_constant_column(sample_run: SampleRun) -> None:
    """The one warning this library produces anywhere, and it is correct."""
    assert sample_run.codes == ["CONSTANT_COLUMN"], sample_run.validation["checks"]
    finding = sample_run.validation["checks"][0]
    assert finding["column"] == "snapshot_date"
    assert finding["severity"] == "warning"


def test_the_aggregation_produced_the_shape_the_contract_wants(sample_run: SampleRun) -> None:
    """One row per lapsed shopper, and the key, target and snapshot kept out of the features."""
    prepare = sample_run.results["prepare"]
    assert prepare["rows_in"] == prepare["rows_out"] == 1_463
    assert prepare["columns_in"] == 16
    assert len(prepare["feature_columns"]) == 13
    for reserved in ("customer_id", "reactivated_90d", "snapshot_date"):
        assert reserved not in prepare["feature_columns"]


def test_the_derived_target_carries_both_outcomes(sample_run: SampleRun) -> None:
    """`reactivated_90d` is built by fetch.py; if the windows were wrong this is where it shows."""
    positive_rate = sample_run.results["evaluation"]["positive_rate"]
    assert 0.30 < positive_rate < 0.55, positive_rate


def test_the_baseline_comparison_was_computed_and_is_reported(sample_run: SampleRun) -> None:
    """The comparison is not asserted in either direction - but it must exist and be readable.

    If this dataset ever starts beating its baseline reproducibly, that is a finding to write up
    in run_report.md, not something a passing test should quietly absorb.
    """
    model, baseline = sample_run.metric("roc_auc"), sample_run.baseline("roc_auc")
    assert 0.0 < model < 1.0
    assert 0.0 < baseline < 1.0
    assert isinstance(sample_run.beats_baseline, bool)
