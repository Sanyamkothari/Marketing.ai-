"""UCI Default of Credit Card Clients: validate + a one-minute train on the committed sample.

The one dataset in the library whose margin over the baseline is clear of the noise, so this is
the one test that asserts the strict comparison the brief asks for. Measured over three runs of
this exact budget against the engine as it stands today:

    test ROC-AUC   0.7611  0.7820  0.7897
    baseline       0.7080  0.7298  0.7092
    margin        +0.0531 +0.0522 +0.0805
    beats          yes     yes     yes

Six for six across both engines, and the smallest margin ever measured here (+0.0452) is more than
eight times the largest shortfall on the datasets that share its tolerance (0.0054, on
health-insurance). The full-data run agrees:
0.7957 against 0.7279, the widest gap in the library. See DEC-410.

FLOOR has 0.06 of headroom below the lowest of the three.
"""

from __future__ import annotations

import pytest

from conftest import BASELINE_TOLERANCE, SampleRun, assert_trained_and_validated, train_on_sample

pytestmark = [pytest.mark.slow, pytest.mark.integration]

FLOOR = 0.70


@pytest.fixture(scope="module")
def sample_run(tmp_path_factory: pytest.TempPathFactory) -> SampleRun:
    return train_on_sample(
        runs_dir=tmp_path_factory.mktemp("runs"),
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
