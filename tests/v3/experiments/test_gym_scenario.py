"""The Gym scenario with one planted better offer, the question menu, and the harness at smoke size."""

from __future__ import annotations

import pytest

from autonomy.contracts import NO_OFFER
from autonomy.experiments.gymrun import experiment_spec, planted_truth, run_gym_experiment
from autonomy.gym.scenarios import (
    BETTER_OFFER_ID,
    SCENARIOS,
    WORSE_OFFER_ID,
    get_scenario,
)
from autonomy.scientist.questions import (
    QuestionError,
    QuestionKind,
    check_arm,
    check_question,
    describe_question,
)
from tests.v3.experiments.helpers import FREE_SHIP, TEN_OFF, brief


def test_the_scenario_is_the_last_one_registered_and_plants_one_better_offer() -> None:
    assert list(SCENARIOS)[-1] == "better_offer_planted"
    spec = get_scenario("better_offer_planted")
    for persona, row in spec.offer_uplift.items():
        assert row["free_shipping"] == 3.0, persona
        assert row["percent_off"] == 1.0, persona
    assert (FREE_SHIP.offer_id, TEN_OFF.offer_id) == (BETTER_OFFER_ID, WORSE_OFFER_ID)


def test_in_the_truth_free_shipping_is_clearly_worth_more_than_ten_percent_off() -> None:
    truth = planted_truth(0, n_customers=300)
    better, worse, plain = (truth.mean_net[o] for o in ("freeship", "pct10", "none"))
    assert truth.n_customers == 300
    assert better > plain + 0.2  # free shipping adds real profit to a plain message
    assert worse < plain  # 10% off adds nothing and costs its redemption
    assert better - worse > 0.5
    assert truth.share_better > 0.5


def test_the_menu_checks_the_arms_against_the_envelope() -> None:
    env = brief().envelope
    check_question(QuestionKind.OFFER, ["pct10", "freeship"], env)
    check_question(QuestionKind.OFFER, ["none", "pct10", "freeship"], env)
    check_question(QuestionKind.CHANNEL, ["email", "whatsapp"], env)
    with pytest.raises(QuestionError, match="2 to 4 arms"):
        check_question(QuestionKind.OFFER, ["pct10"], env)
    with pytest.raises(QuestionError, match="different"):
        check_question(QuestionKind.OFFER, ["pct10", "pct10"], env)
    with pytest.raises(QuestionError, match="not inside"):
        check_question(QuestionKind.OFFER, ["pct10", "gift"], env)
    with pytest.raises(QuestionError, match="not a usable arm id"):
        check_question(QuestionKind.OFFER, ["pct10", "holdout"], env)
    with pytest.raises(QuestionError, match="not a usable arm id"):
        check_arm(QuestionKind.OFFER, "a|b", env)
    assert NO_OFFER.offer_id == "none"
    assert describe_question(QuestionKind.OFFER, "lapsed customers").startswith(
        "Which offer earns more profit"
    )


def test_the_harness_spec_is_registered_with_its_rules_written_down() -> None:
    spec = experiment_spec()
    assert spec.reference_arm == WORSE_OFFER_ID
    assert spec.arm_ids == (WORSE_OFFER_ID, BETTER_OFFER_ID)
    assert spec.share <= 0.10 and spec.pick_rule.alpha == 0.05


def test_the_harness_runs_end_to_end_at_smoke_size() -> None:
    """200 customers x 14 days: far too small to find anything; checks only that the wiring runs, that the
    agent acts inside its arms, and that the rules in the book are the ones registered."""
    result = run_gym_experiment(0, n_customers=200, days=14, share=0.10, min_per_arm=30)
    assert result.rules_unchanged
    assert result.promoted_worse is False
    assert result.stats.get("outside_arm", 0) == 0
    assert (
        result.stats["assigned:offer_test_1:freeship"] > 0 and result.stats["assigned:offer_test_1:pct10"] > 0
    )
    assert result.reading.holdout_n > 0 and all(a.n > 0 for a in result.reading.arms)


@pytest.mark.slow
def test_the_better_offer_is_found_within_four_weeks_on_nine_of_ten_seeds() -> None:
    """The pass test of plan-v3 foundation C. Minutes of compute, so it runs in the nightly job only."""
    results = [run_gym_experiment(seed, n_customers=5000, days=29) for seed in range(10)]
    assert not any(r.promoted_worse for r in results)
    assert all(r.rules_unchanged for r in results)
    assert sum(r.found_better for r in results) >= 9
