"""M36 item 3: `threshold.mode: auto`, defined precisely, can no longer call every row positive.

`auto` maximises the configured metric on validation; when that optimum flags more of the validation
rows than `evaluation.threshold.max_flagged_rate` allows (0.30 in `configs/engine.yaml`), or flags
every row, it falls back to the top decile of the validation scores and records `THRESHOLD_FALLBACK`
with its reason - in `scorer.json` and in the `threshold_detail` sentence `evaluation.json` and the
Model page carry (DEC-094).

The dataset that exposed it is the library's online-retail win-back file: a weak model over a 41 %
base rate, where the F1 optimum was "say yes to everyone" (recall 1.0, specificity 0.0 on the
full-data run). Its committed sample is the whole file, so the regression test below runs the
engine's own prepare, split and baseline on it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from engine.config import (
    EvaluationConfig,
    Metric,
    Recipe,
    ThresholdConfig,
    ThresholdMode,
    UseCaseConfig,
    load_use_case,
    recipe_from_config,
    resolve_config,
)
from engine.stages import prepare
from engine.stages.scorer import (
    FALLBACK_FLAGGED_RATE,
    THRESHOLD_FALLBACK,
    AutoGluonScorer,
    BaselineScorer,
    LabelValue,
    ThresholdFallback,
    choose_operating_point,
    choose_threshold,
    fit_baseline_scorer,
    threshold_metric,
)
from engine.stages.train import class_labels

LIBRARY = Path(__file__).resolve().parents[2] / "library"
CEILING = ThresholdConfig(max_flagged_rate=0.30)


def flagged(scores: np.ndarray, threshold: float) -> float:
    return float(np.mean(scores >= threshold))


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------
def test_the_shipped_ceiling_is_thirty_percent_and_configurable() -> None:
    assert load_use_case("telco-churn").evaluation.threshold.max_flagged_rate == 0.30
    assert ThresholdConfig().max_flagged_rate is None, "a hand-built config states no policy"
    assert ThresholdConfig(max_flagged_rate=0.5).max_flagged_rate == 0.5
    with pytest.raises(ValueError):
        ThresholdConfig(max_flagged_rate=0.0)


def test_an_optimum_within_the_ceiling_is_kept_exactly_as_before() -> None:
    scores = np.array([0.90, 0.80, 0.70, 0.60, 0.50, 0.40, 0.30, 0.20, 0.10, 0.05])
    actual = np.array([1, 1, 0, 0, 0, 0, 0, 0, 0, 0], dtype=bool)
    choice = choose_operating_point(scores, actual, CEILING)
    assert (choice.threshold, choice.mode) == (0.80, ThresholdMode.AUTO)
    assert choice.detail == "Auto (maximises F1 on validation): 0.80"
    assert choice.fallback is None


def test_an_optimum_above_the_ceiling_falls_back_to_the_top_decile() -> None:
    rng = np.random.default_rng(7)
    scores = rng.random(200)
    actual = rng.random(200) < 0.45  # a model that knows nothing, over a base rate near a half
    unbounded = choose_operating_point(scores, actual, ThresholdConfig())
    assert flagged(scores, unbounded.threshold) > 0.30
    choice = choose_operating_point(scores, actual, CEILING)
    assert choice.mode is ThresholdMode.AUTO
    assert choice.fallback is not None
    assert choice.fallback.code == THRESHOLD_FALLBACK
    assert choice.fallback.optimum_threshold == unbounded.threshold
    assert choice.fallback.max_flagged_rate == 0.30
    assert flagged(scores, choice.threshold) == pytest.approx(FALLBACK_FLAGGED_RATE)
    assert choice.fallback.fallback_flagged_rate == pytest.approx(flagged(scores, choice.threshold))
    assert THRESHOLD_FALLBACK in choice.detail
    assert "above the 30% ceiling" in choice.detail
    assert choice.detail.startswith("Auto, fell back to the top 10% of validation scores")


def test_flagging_every_row_falls_back_even_without_a_ceiling() -> None:
    """The exact shape online-retail produced: recall 1.0, specificity 0.0, precision = base rate."""
    rng = np.random.default_rng(3)
    actual = rng.random(219) < 0.411
    # scores that rank the negatives slightly above the positives: the best F1 is "everybody"
    scores = np.where(actual, rng.uniform(0.2, 0.5, 219), rng.uniform(0.3, 0.6, 219))
    choice = choose_operating_point(scores, actual, ThresholdConfig())
    assert choice.fallback is not None
    assert choice.fallback.optimum_flagged_rate == 1.0
    assert "flags every validation row" in choice.fallback.reason
    assert flagged(scores, choice.threshold) < 1.0


def test_a_model_that_scores_every_row_alike_falls_back_to_one_half() -> None:
    scores = np.full(50, 0.4)
    actual = np.array([True, False] * 25)
    choice = choose_operating_point(scores, actual, CEILING)
    assert choice.fallback is not None
    assert choice.threshold == 0.50
    assert "no top decile" in choice.fallback.reason


def test_the_configured_metric_is_the_one_maximised() -> None:
    scores = np.array([0.95, 0.90, 0.85, 0.80, 0.40, 0.35, 0.30, 0.20, 0.10, 0.05])
    actual = np.array([1, 1, 0, 1, 1, 0, 0, 0, 0, 0], dtype=bool)
    loose = ThresholdConfig(max_flagged_rate=1.0)
    assert threshold_metric(Metric.ROC_AUC) is Metric.F1  # a ranking metric: no threshold moves it
    assert threshold_metric(Metric.PR_AUC) is Metric.F1
    precision = choose_operating_point(scores, actual, loose, metric=Metric.PRECISION)
    assert precision.threshold == 0.90  # 2 of 2: the most rows at a precision of one
    assert precision.detail == "Auto (maximises precision on validation): 0.90"
    recall = choose_operating_point(scores, actual, loose, metric=Metric.RECALL)
    assert recall.threshold == 0.40  # the fewest rows that still catch every positive
    assert recall.detail == "Auto (maximises recall on validation): 0.40"


def test_a_threshold_is_never_rounded_above_the_score_it_cuts_at() -> None:
    """An isotonic step at 10/11 used to become 0.9091 and stop flagging the rows it was chosen for."""
    scores = np.array([10 / 11] * 3 + [0.2] * 7)
    actual = np.array([1, 1, 1, 0, 0, 0, 0, 0, 0, 0], dtype=bool)
    value, _, _ = choose_threshold(scores, actual, ThresholdConfig())
    assert value <= 10 / 11
    assert flagged(scores, value) == 0.3


# ---------------------------------------------------------------------------
# The artefacts
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Retail:
    """online-retail after the engine's own prepare and split, ready for the baseline to be fitted."""

    config: UseCaseConfig
    recipe: Recipe
    parts: dict[str, pd.DataFrame]
    classes: tuple[LabelValue, LabelValue]

    def baseline(self, evaluation: EvaluationConfig) -> BaselineScorer:
        scorer = fit_baseline_scorer(
            self.recipe,
            evaluation,
            train=self.parts["train"],
            validation=self.parts["validation"],
            classes=self.classes,
        )
        assert scorer is not None
        return scorer


@pytest.fixture(scope="module")
def retail() -> Retail:
    """online-retail through the engine's own prepare and split, as the library's run did."""
    config = resolve_config("retail-win-back", {}, root=LIBRARY / "configs").config
    frame = pd.read_csv(LIBRARY / "online-retail" / "sample.csv")
    target = "reactivated_90d"
    rows, plan = prepare.prepare_rows(frame, config, primary_key="customer_id", target=target)
    parts, _ = prepare.split_dataset(rows, config, run_id="r_20260923_0000feed", target=target)
    prepared, _ = prepare.fit_transforms(
        rows, config, plan, run_id="r_20260923_0000feed", fit_index=parts["train"].index
    )
    recipe = recipe_from_config(
        config, primary_key="customer_id", feature_columns=plan.feature_columns, seed=11, target=target
    )
    return Retail(
        config=config,
        recipe=recipe,
        parts={name: prepared.loc[part.index] for name, part in parts.items()},
        classes=class_labels(prepared.loc[parts["train"].index, target]),
    )


def test_online_retail_no_longer_calls_most_of_its_audience_positive(retail: Retail) -> None:
    """The library dataset that exposed it, through the shipped configuration (ceiling 0.30)."""
    evaluation = retail.config.evaluation
    assert evaluation.threshold.max_flagged_rate == 0.30
    before = retail.baseline(evaluation.model_copy(update={"threshold": ThresholdConfig()}))
    after = retail.baseline(evaluation)
    validation = retail.parts["validation"]
    # the old rule flagged well over half of a 219-row validation split ...
    assert flagged(before.score(validation).to_numpy(), before.threshold) > 0.5
    assert before.threshold_fallback is None
    # ... the new one refuses it, says why, and flags about a tenth. The isotonic calibrator's top
    # step holds 24 of the 219 rows, so "the top decile" is that step: the smallest group the
    # calibrated model can single out, and the record states the share the rule really flags.
    fallback = after.threshold_fallback
    assert isinstance(fallback, ThresholdFallback)
    assert fallback.optimum_threshold == before.threshold
    assert fallback.optimum_flagged_rate > 0.30
    rate = flagged(after.score(validation).to_numpy(), after.threshold)
    assert 0.0 < rate < 0.15
    assert rate == pytest.approx(fallback.fallback_flagged_rate, abs=1e-4)
    assert THRESHOLD_FALLBACK in after.threshold_detail
    on_test = after.score(retail.parts["test"]).to_numpy() >= after.threshold
    assert 0 < on_test.sum() < len(on_test), "the test split is neither all positive nor all negative"


def test_scorer_json_carries_the_record_only_when_there_is_one(retail: Retail) -> None:
    fell_back = retail.baseline(retail.config.evaluation)
    document = json.loads(fell_back.to_json())
    assert document["threshold_fallback"]["code"] == THRESHOLD_FALLBACK
    assert document["threshold_fallback"]["reason"]
    reread = AutoGluonScorer.from_json(object(), fell_back.to_json())  # type: ignore[arg-type]
    assert reread.threshold_fallback == fell_back.threshold_fallback

    manual = retail.config.evaluation.model_copy(
        update={"threshold": ThresholdConfig(mode=ThresholdMode.MANUAL, value=0.4)}
    )
    kept = retail.baseline(manual)
    assert "threshold_fallback" not in json.loads(kept.to_json()), "an unused field is not written"
