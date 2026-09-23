"""`engine.uplift.champion`: the uplift champion rule, gate by gate (plan B §5).

The evaluations are built by hand, so each test controls exactly the fields the rule reads: the
causal flag, the AUUC with its interval, and the three hold-out counts that prove "same hold-out".
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from engine.uplift.champion import UpliftChampionDecision, decide_uplift_champion
from engine.uplift.config import UpliftBaseModel, UpliftLearner
from engine.uplift.contracts import ConfidenceValue, UpliftEvaluation


def _evaluation(
    auuc: float,
    ci_low: float | None,
    ci_high: float | None = None,
    *,
    causal: bool = True,
    rows: int = 1_000,
    treated: int = 500,
    fingerprint: str | None = None,
) -> UpliftEvaluation:
    ci_high = ci_high if ci_high is not None else (None if ci_low is None else auuc + (auuc - ci_low))
    zero = ConfidenceValue(value=0.0, ci_low=-0.01, ci_high=0.01)
    return UpliftEvaluation(
        run_id="run",
        learner=UpliftLearner.X_LEARNER,
        base_model=UpliftBaseModel.LIGHTGBM,
        rows_evaluated=rows,
        treated_rows=treated,
        control_rows=rows - treated,
        treated_rate=0.1,
        control_rate=0.1,
        average_treatment_effect=zero,
        auuc=ConfidenceValue(value=auuc, ci_low=ci_low, ci_high=ci_high),
        qini_coefficient=zero,
        uplift_at=(),
        deciles=(),
        bootstrap_samples=200,
        measurable_uplift=ci_low is not None and ci_low > 0,
        causal=causal,
        summary="-",
        evaluated_at=datetime(2026, 9, 1, tzinfo=UTC),
        holdout_fingerprint=fingerprint,
    )


def test_first_measurable_model_is_promoted() -> None:
    decision = decide_uplift_champion(_evaluation(0.02, 0.01), None, min_improvement_pct=5.0)
    assert decision == UpliftChampionDecision(promote=True, reason=decision.reason, improvement_pct=None)
    assert decision.reason.startswith("Promoted: there is no uplift champion yet")


def test_not_causal_is_never_promoted_even_without_a_champion() -> None:
    decision = decide_uplift_champion(_evaluation(0.5, 0.4, causal=False), None, min_improvement_pct=0.0)
    assert decision.promote is False
    assert "not randomly assigned" in decision.reason
    assert decision.improvement_pct is None


@pytest.mark.parametrize("ci_low", [None, 0.0, -0.003])
def test_no_measurable_uplift_is_never_promoted(ci_low: float | None) -> None:
    challenger = _evaluation(0.02, ci_low, 0.04)
    champion = _evaluation(-0.01, -0.02)
    for incumbent in (None, champion):
        decision = decide_uplift_champion(challenger, incumbent, min_improvement_pct=0.0)
        assert decision.promote is False
        assert "no measurable uplift" in decision.reason


def test_challenger_must_beat_the_champion_by_the_margin() -> None:
    # Binary-exact AUUCs, so "exactly meets the margin" is exact in floating point too.
    champion = _evaluation(0.5, 0.25)
    # +12.5% exactly meets a 12.5% rule.
    meets = decide_uplift_champion(_evaluation(0.5625, 0.25), champion, min_improvement_pct=12.5)
    assert meets.promote is True
    assert meets.improvement_pct == 12.5
    assert meets.reason.startswith("Promoted:")
    # +6.25% does not.
    short = decide_uplift_champion(_evaluation(0.53125, 0.25), champion, min_improvement_pct=12.5)
    assert short.promote is False
    assert short.improvement_pct == 6.25
    assert short.reason.startswith("Not promoted:")
    # A worse challenger never wins a 0% rule.
    worse = decide_uplift_champion(_evaluation(0.375, 0.125), champion, min_improvement_pct=0.0)
    assert worse.promote is False
    assert worse.improvement_pct == -25.0


def test_equal_auuc_wins_only_a_zero_percent_rule() -> None:
    champion = _evaluation(0.02, 0.01)
    assert decide_uplift_champion(_evaluation(0.02, 0.01), champion, min_improvement_pct=0.0).promote is True
    assert decide_uplift_champion(_evaluation(0.02, 0.01), champion, min_improvement_pct=1.0).promote is False


@pytest.mark.parametrize("champion_auuc", [0.0, -0.01])
def test_champion_at_or_below_zero_loses_to_any_strictly_greater_auuc(champion_auuc: float) -> None:
    champion = _evaluation(champion_auuc, champion_auuc - 0.01)
    decision = decide_uplift_champion(_evaluation(0.001, 0.0005), champion, min_improvement_pct=500.0)
    assert decision.promote is True
    assert decision.improvement_pct is None


def test_different_hold_outs_are_refused() -> None:
    challenger = _evaluation(0.03, 0.02)
    for champion in (
        _evaluation(0.02, 0.01, rows=999, treated=500),
        _evaluation(0.02, 0.01, rows=1_000, treated=499),
    ):
        with pytest.raises(ValueError, match="same hold-out"):
            decide_uplift_champion(challenger, champion, min_improvement_pct=5.0)


def test_equal_counts_on_different_customers_are_refused_by_the_fingerprint() -> None:
    # The critic's case (DEC-670): the counts agree, but the hold-outs are different customers.
    challenger = _evaluation(0.03, 0.02, fingerprint="a" * 64)
    with pytest.raises(ValueError, match="holdout_fingerprint"):
        decide_uplift_champion(
            challenger, _evaluation(0.02, 0.01, fingerprint="b" * 64), min_improvement_pct=5.0
        )


@pytest.mark.parametrize(
    ("mine", "theirs"), [("a" * 64, "a" * 64), ("a" * 64, None), (None, "a" * 64), (None, None)]
)
def test_the_fingerprint_is_compared_only_when_both_carry_one(mine: str | None, theirs: str | None) -> None:
    challenger = _evaluation(0.03, 0.02, fingerprint=mine)
    decision = decide_uplift_champion(
        challenger, _evaluation(0.02, 0.01, fingerprint=theirs), min_improvement_pct=5.0
    )
    assert decision.promote is True


def test_gates_run_before_the_hold_out_check() -> None:
    # A challenger that could never be promoted is refused for that reason, not for the counts.
    champion = _evaluation(0.02, 0.01, rows=10, treated=5)
    decision = decide_uplift_champion(_evaluation(0.03, -0.01), champion, min_improvement_pct=5.0)
    assert decision.promote is False
