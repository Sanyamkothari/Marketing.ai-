"""Plan J M100: several treatments against one shared control - configuration, checks and learners.

DEC-668's path, piece by piece: `uplift.treatment_levels` (control first; empty is today's binary run
and leaves the configuration's dump unchanged), `TREATMENT_NOT_BINARY` as "a value outside the
configured levels", `TREATMENT_ARM_TOO_SMALL` and the randomness check per arm, and one model per arm
whose first treatment is exactly the binary learner on the same rows. Every test here fails on the
commit before M100.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from engine.config import load_use_case
from engine.contracts import Severity
from engine.measurement.simulate import MULTI_ARM_LEVELS, MULTI_ARM_SEGMENTS, multi_arm_population
from engine.uplift.checks import run_uplift_checks
from engine.uplift.config import UpliftBaseModel, UpliftConfig, UpliftLearner, level_text
from engine.uplift.data import coerce_arms, split_holdout
from engine.uplift.learners import (
    MultiArmUpliftModel,
    load_model,
    make_learner,
    make_multi_arm_learner,
    save_model,
)

LEVELS = MULTI_ARM_LEVELS


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def test_one_treatment_level_is_the_default_and_leaves_the_dump_unchanged() -> None:
    config = UpliftConfig()
    assert config.treatment_levels == () and not config.multi_arm
    assert "treatment_levels" not in config.model_dump(mode="json")


def test_several_levels_name_the_control_first_and_are_recorded() -> None:
    config = UpliftConfig(treatment_levels=("none", "offer_a", "offer_b"))
    assert config.multi_arm
    assert config.model_dump(mode="json")["treatment_levels"] == ["none", "offer_a", "offer_b"]
    assert UpliftConfig(treatment_levels=[0, 1, 2]).treatment_levels == ("0", "1", "2")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("levels", "words"),
    [
        (("none", "offer"), "at least two treatment values"),
        (("none", "a", " A "), "same value twice"),
        (("none", "", "b"), "empty"),
    ],
)
def test_bad_levels_are_refused_in_plain_words(levels: tuple[str, ...], words: str) -> None:
    with pytest.raises(ValidationError, match=words):
        UpliftConfig(treatment_levels=levels)


def test_a_shipped_use_case_has_one_treatment_level(config_root: Path) -> None:
    config = load_use_case("win_back_campaign", root=config_root)
    assert config.uplift.treatment_levels == ()


def test_levels_and_cells_compare_by_one_spelling() -> None:
    assert level_text(1) == level_text(1.0) == level_text(" 1 ") == "1"
    assert level_text(True) == "true" and level_text("Offer_A ") == "offer_a"
    codes, bad = coerce_arms(pd.Series(["none", "OFFER_A", " offer_b", "none"]), LEVELS)
    assert bad == 0 and codes is not None and codes.tolist() == [0, 1, 2, 0]
    codes, bad = coerce_arms(pd.Series(["none", None, "offer_c"]), LEVELS)
    assert codes is None and bad == 2


def test_whole_numbers_written_as_text_or_floats_are_one_level() -> None:
    """Review finding: `"1.0"` text cells and YAML levels `[0.0, 1.0, 2.0]` matched no level."""
    assert level_text("1.0") == level_text(1) == level_text(1.0) == "1"
    assert level_text(" -2.00 ") == "-2" and level_text("1.5") == "1.5" and level_text("1e0") == "1e0"
    codes, bad = coerce_arms(pd.Series(["0", "1.0", "2"]), ("0", "1", "2"))
    assert bad == 0 and codes is not None and codes.tolist() == [0, 1, 2]
    floats = UpliftConfig(treatment_levels=[0.0, 1.0, 2.0])  # type: ignore[arg-type]
    assert floats.treatment_levels == ("0", "1", "2")
    assert UpliftConfig(treatment_levels=[0, 1, 2.5]).treatment_levels == ("0", "1", "2.5")  # type: ignore[arg-type]
    codes, bad = coerce_arms(pd.Series([0.0, 1.0, 2.0, 1.0]), floats.treatment_levels)
    assert bad == 0 and codes is not None and codes.tolist() == [0, 1, 2, 1]
    with pytest.raises(ValidationError, match="same value twice"):
        UpliftConfig(treatment_levels=("0", "1", "1.0"))


def test_the_split_keeps_every_arm_and_the_binary_split_is_unchanged() -> None:
    rng = np.random.default_rng(1)
    t = rng.integers(0, 2, 2_000)
    y = rng.integers(0, 2, 2_000)
    before = split_holdout(t, y, test_fraction=0.3, seed=5)
    assert all(
        (a == b).all()
        for a, b in zip(before, split_holdout(t, y, test_fraction=0.3, seed=5, arm_count=2), strict=True)
    )
    arms = rng.integers(0, 3, 3_000)
    y3 = rng.integers(0, 2, 3_000)
    train, test = split_holdout(arms, y3, test_fraction=0.3, seed=5, arm_count=3)
    assert len(np.intersect1d(train, test)) == 0 and len(train) + len(test) == 3_000
    for k in range(3):
        share = np.isin(test, np.flatnonzero(arms == k)).sum() / (arms == k).sum()
        assert abs(share - 0.3) < 0.01


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------
def _config(config_root: Path, **uplift: object):  # type: ignore[no-untyped-def]
    base = load_use_case("win_back_campaign", root=config_root)
    settings = {
        "treatment_column": "offer",
        "treatment_levels": LEVELS,
        "min_arm_rows": 200,
        "min_arm_positives": 20,
        **uplift,
    }
    return base.model_copy(update={"uplift": UpliftConfig(**settings)})  # type: ignore[arg-type]


def _check(frame: pd.DataFrame, config_root: Path, **uplift: object):  # type: ignore[no-untyped-def]
    return run_uplift_checks(
        frame,
        _config(config_root, **uplift),
        primary_key="customer_id",
        target="converted",
        upload_id="u_test",
        seed=3,
    ).report


def test_a_random_three_arm_file_passes_and_reports_every_arm(config_root: Path) -> None:
    frame = multi_arm_population(3_000, seed=11).frame
    report = _check(frame, config_root)
    assert report.passed and report.causal, report.checks
    assert report.arms is not None and [a.arm for a in report.arms] == ["offer_a", "offer_b"]
    counts = frame["offer"].value_counts()
    # The report's own fields are the first treatment against the control (DEC-668 (3)).
    assert report.treated_rows == counts["offer_a"] and report.control_rows == counts["none"]
    assert report.randomness_auc == report.arms[0].randomness_auc
    assert report.arms[1].treated_rows == counts["offer_b"] and report.arms[1].control_rows == counts["none"]
    assert all(a.randomness_auc is not None and a.randomness_auc < 0.6 for a in report.arms)


def test_a_value_outside_the_levels_is_treatment_not_binary_in_its_new_words(config_root: Path) -> None:
    frame = multi_arm_population(600, seed=12).frame
    frame.loc[:4, "offer"] = "offer_c"
    report = _check(frame, config_root)
    assert not report.passed
    finding = next(c for c in report.checks if c.code == "TREATMENT_NOT_BINARY")
    assert "configured treatment values ('none', 'offer_a' and 'offer_b')" in finding.message
    assert finding.details["bad_values"] == 5 and finding.details["treatment_levels"] == list(LEVELS)


def test_the_binary_rule_still_holds_without_levels(config_root: Path) -> None:
    frame = multi_arm_population(600, seed=12).frame
    report = _check(frame, config_root, treatment_levels=())
    finding = next(c for c in report.checks if c.code == "TREATMENT_NOT_BINARY")
    assert "should be 1 for treated customers and 0 for held-out customers" in finding.message
    assert report.arms is None and "arms" not in report.model_dump(mode="json")


def test_a_small_second_offer_is_too_small_even_when_the_first_is_not(config_root: Path) -> None:
    frame = multi_arm_population(3_000, seed=13).frame
    small = frame.index[frame["offer"] == "offer_b"][150:]
    frame = frame.drop(index=small)
    report = _check(frame, config_root)
    finding = next(c for c in report.checks if c.code == "TREATMENT_ARM_TOO_SMALL")
    assert "the 'offer_b' group has 150 customers" in finding.message
    assert "offer_a" not in finding.message
    assert [arm["level"] for arm in finding.details["arms"]] == list(LEVELS)


def test_a_targeted_second_offer_is_not_random_while_the_first_is(config_root: Path) -> None:
    frame = multi_arm_population(4_000, seed=14).frame
    # Offer B went to the high-affinity customers only: who got it can be predicted.
    targeted = (frame["offer"] != "none") & (frame["offer_affinity"] > 0.5)
    frame.loc[targeted, "offer"] = "offer_b"
    frame.loc[(frame["offer"] == "offer_b") & (frame["offer_affinity"] <= 0.5), "offer"] = "offer_a"
    report = _check(frame, config_root)
    not_random = [c for c in report.checks if c.code == "TREATMENT_NOT_RANDOM"]
    assert not report.causal and not report.passed
    assert any(
        c.details.get("arm") == "offer_b" and c.message.startswith("For the 'offer_b' group")
        for c in not_random
    )
    assert report.arms is not None and report.arms[1].randomness_auc is not None
    assert report.arms[1].randomness_auc > 0.6
    assert all(c.severity is Severity.ERROR for c in not_random)


# ---------------------------------------------------------------------------
# The learners
# ---------------------------------------------------------------------------
def _features(population_rows: int, seed: int):  # type: ignore[no-untyped-def]
    planted = multi_arm_population(population_rows, seed=seed)
    frame = planted.frame
    X = frame[["offer_affinity", "tenure_months", "noise_1", "noise_2"]].astype("float64")  # noqa: N806
    codes, _ = coerce_arms(frame["offer"], LEVELS)
    assert codes is not None
    return planted, X, codes, frame["converted"].to_numpy()


@pytest.mark.parametrize("learner", [UpliftLearner.T_LEARNER, UpliftLearner.X_LEARNER])
def test_the_first_arm_is_exactly_the_binary_learner_on_its_rows(learner: UpliftLearner) -> None:
    _, X, codes, y = _features(6_000, 21)  # noqa: N806
    model = make_multi_arm_learner(learner, UpliftBaseModel.LIGHTGBM, levels=LEVELS, seed=4)
    model.fit_arms(X, codes, y)
    rows = np.flatnonzero(codes <= 1)
    binary = make_learner(learner, UpliftBaseModel.LIGHTGBM, seed=4).fit(
        X.iloc[rows], (codes[rows] == 1).astype(int), y[rows]
    )
    both = model.predict_arms(X)
    assert np.array_equal(both.uplift[:, 0], binary.predict(X).uplift)
    assert np.array_equal(model.predict(X).uplift, binary.predict(X).uplift)
    assert model.propensity == binary.propensity
    contributions, _ = model.contributions(X.head(300))
    expected, _ = binary.contributions(X.head(300))
    assert np.allclose(contributions, expected) and model.explanation_method == binary.explanation_method


@pytest.mark.parametrize("learner", list(UpliftLearner))
def test_each_offer_finds_the_segment_that_answers_it(learner: UpliftLearner) -> None:
    planted, X, codes, y = _features(12_000, 22)  # noqa: N806
    model = make_multi_arm_learner(learner, UpliftBaseModel.LIGHTGBM, levels=LEVELS, seed=4)
    model.fit_arms(X, codes, y)
    predicted = model.predict_arms(X)
    assert predicted.arms == 2 and predicted.uplift.shape == (12_000, 2)
    a, b, _, dogs = (planted.segment == name for name in MULTI_ARM_SEGMENTS)
    assert predicted.uplift[a, 0].mean() > predicted.uplift[a, 1].mean() + 0.05
    assert predicted.uplift[b, 1].mean() > predicted.uplift[b, 0].mean() + 0.05
    assert (predicted.uplift[dogs].mean(axis=0) < -0.03).all()


def test_the_s_learner_has_the_arm_as_a_feature() -> None:
    _, X, codes, y = _features(3_000, 23)  # noqa: N806
    model = make_multi_arm_learner(UpliftLearner.S_LEARNER, UpliftBaseModel.LIGHTGBM, levels=LEVELS, seed=4)
    model.fit_arms(X, codes, y)
    assert model.arm_features == ("__arm_1__", "__arm_2__")  # type: ignore[attr-defined]
    predicted = model.predict_arms(X)
    assert np.allclose(predicted.uplift, predicted.p_treated - predicted.p_control[:, None])


def test_a_model_of_several_offers_saves_and_loads_whole() -> None:
    _, X, codes, y = _features(3_000, 24)  # noqa: N806
    model = make_multi_arm_learner(UpliftLearner.X_LEARNER, UpliftBaseModel.LIGHTGBM, levels=LEVELS, seed=4)
    model.fit_arms(X, codes, y)
    with tempfile.TemporaryDirectory() as directory:
        save_model(model, Path(directory))
        loaded = load_model(Path(directory))
    assert isinstance(loaded, MultiArmUpliftModel) and loaded.levels == LEVELS
    assert np.array_equal(loaded.predict_arms(X).uplift, model.predict_arms(X).uplift)


def test_a_model_of_several_offers_refuses_a_binary_fit_and_bad_arms() -> None:
    _, X, codes, y = _features(600, 25)  # noqa: N806
    model = make_multi_arm_learner(UpliftLearner.T_LEARNER, UpliftBaseModel.LIGHTGBM, levels=LEVELS, seed=4)
    with pytest.raises(TypeError, match="fit_arms"):
        model.fit(X, (codes == 1).astype(int), y)
    with pytest.raises(ValueError, match="codes 0"):
        model.fit_arms(X, np.where(codes == 2, 3, codes), y)
    with pytest.raises(ValueError, match="treatment 2 arm is empty"):
        model.fit_arms(X, np.where(codes == 2, 1, codes), y)
    with pytest.raises(ValueError, match="at least two treatments"):
        make_multi_arm_learner(UpliftLearner.T_LEARNER, UpliftBaseModel.LIGHTGBM, levels=("a", "b"), seed=1)


# ---------------------------------------------------------------------------
# The planted population
# ---------------------------------------------------------------------------
def test_the_planted_population_is_deterministic_and_randomised() -> None:
    one, two = multi_arm_population(3_000, seed=5), multi_arm_population(3_000, seed=5)
    assert one.frame.equals(two.frame)
    assert np.bincount(one.arm).tolist() == [1_000, 1_000, 1_000]
    a = one.segment == "offer_a_responders"
    assert one.tau[a, 0].tolist() == [0.25] * int(a.sum()) and (one.tau[a, 1] == 0).all()
    dogs = one.segment == "sleeping_dogs"
    assert (one.tau[dogs] == -0.25).all()
    assert one.true_ate(1) == pytest.approx(0.35 * 0.25 - 0.15 * 0.25, abs=0.01)
