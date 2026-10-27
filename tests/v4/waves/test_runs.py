"""Whole runs through the Gym's loop: the protocol, the null world, the oracle, the holdout, the score."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pytest

from autonomy.contracts import Brief, ContentItem
from autonomy.gym.waves.config import get_world
from autonomy.gym.waves.data import WavesData
from autonomy.gym.waves.policies import (
    BEST_FIXED,
    ORACLE,
    SEND_ALL_EMAIL,
    SEND_ALL_SMS,
    SEND_NONE,
    WEEKLY_SMS,
    policy_names,
)
from autonomy.gym.waves.runner import RunResult, run_one

DAYS = 28
SIMPLE = (SEND_NONE, SEND_ALL_SMS, SEND_ALL_EMAIL, WEEKLY_SMS, BEST_FIXED)


def run(data, world: str, policy: str, seed: int = 0, days: int = DAYS) -> RunResult:
    return run_one(data, get_world(world), policy, seed, days=days)


def test_the_null_world_gives_every_policy_no_incremental_margin_and_the_oracle_nothing(
    data,
) -> None:
    for policy in (*SIMPLE, ORACLE):
        row = run(data, "null", policy).row
        assert row["gross_margin"] == 0.0, policy
        assert row["incremental_purchases"] == 0 and row["real_receipts_removed"] == 0, policy
        # whatever a policy spends is pure loss: the value is minus its costs, never above zero
        assert row["true_value"] <= 0.0, policy
        assert abs(row["true_value"] + row["message_cost"] + row["unsubscribe_cost_expected"]) < 1e-6, policy
    oracle = run(data, "null", ORACLE).row
    assert oracle["true_value"] == 0.0 and oracle["contacts"] == 0
    assert run(data, "null", SEND_ALL_SMS).row["true_value"] < 0.0


def test_the_oracle_is_at_least_as_good_as_every_simple_policy_on_a_planted_world(
    data,
) -> None:
    for world in ("heterogeneous", "sleeping_dogs", "fatigue_heavy"):
        oracle = run(data, world, ORACLE).row["true_value"]
        assert oracle > 0, world
        for policy in SIMPLE:
            value = run(data, world, policy).row["true_value"]
            assert oracle >= value - 1e-6, (world, policy, oracle, value)


def test_sending_to_everyone_loses_to_the_oracle_by_a_wide_margin_where_dogs_exist(
    data,
) -> None:
    oracle = run(data, "sleeping_dogs", ORACLE).row
    blast = run(data, "sleeping_dogs", SEND_ALL_SMS).row
    assert oracle["true_value"] > 1.2 * blast["true_value"]
    assert oracle["contacts"] < blast["contacts"]


def test_the_holdout_is_never_contacted_and_no_rule_is_broken(data) -> None:
    for policy in (SEND_ALL_SMS, SEND_ALL_EMAIL, BEST_FIXED, ORACLE):
        row = run(data, "heterogeneous", policy).row
        assert row["n_holdout"] > 0 and row["holdout_contacted"] == 0, policy
        assert row["violations"] == 0, policy
        assert row["n_agent"] + row["n_holdout"] == row["customers"]
    # the holdout is the fixed 10%: the same customers whatever the policy or the world
    a, b = run(data, "null", SEND_NONE).row, run(data, "heterogeneous", SEND_ALL_SMS).row
    assert a["n_holdout"] == b["n_holdout"]


def test_the_weekly_cap_binds_the_blast_and_the_weekly_rule_sends_half_as_much(data) -> None:
    blast = run(data, "heterogeneous", SEND_ALL_SMS).row
    weekly = run(data, "heterogeneous", WEEKLY_SMS).row
    assert 1.6 < blast["contacts_per_customer_week"] <= 2.0 + 1e-9
    assert 0.8 < weekly["contacts_per_customer_week"] <= 1.0 + 1e-9


def test_the_best_fixed_rule_sends_sms_first_here(data) -> None:
    fixed = run(data, "heterogeneous", BEST_FIXED).row
    assert fixed["sms_contacts"] > 0.9 * fixed["contacts"]


def test_the_score_is_exact_given_the_contacts_and_repeatable(data) -> None:
    a, b = run(data, "heterogeneous", SEND_ALL_SMS), run(data, "heterogeneous", SEND_ALL_SMS)
    assert a.row["true_value"] == b.row["true_value"]
    assert np.array_equal(a.by_customer, b.by_customer)
    assert abs(a.by_customer.sum() - a.row["true_value"]) < 1e-6
    holdout = a.by_customer == 0
    assert holdout.sum() >= a.row["n_holdout"]
    # different seeds are different worlds
    assert run(data, "heterogeneous", SEND_ALL_SMS, seed=1).row["true_value"] != a.row["true_value"]


def test_a_seven_day_reading_of_the_same_contacts_is_bigger_than_the_truth(data) -> None:
    row = run(data, "heterogeneous", SEND_ALL_SMS).row
    assert row["true_value_7d_reading"] > row["true_value"]
    flat = run_one(
        data, get_world("heterogeneous").with_overrides(pull_forward=0.0), SEND_ALL_SMS, 0, days=DAYS
    ).row
    assert flat["true_value"] > row["true_value"]


def test_the_measured_holdout_estimate_is_reported_beside_the_true_value(data) -> None:
    row = run(data, "heterogeneous", SEND_ALL_SMS).row
    assert row["measured_low"] < row["measured"] < row["measured_high"]
    assert "measured_over_true" in row


def test_every_policy_name_is_known() -> None:
    names = set(policy_names())
    assert {
        SEND_NONE,
        SEND_ALL_SMS,
        SEND_ALL_EMAIL,
        WEEKLY_SMS,
        BEST_FIXED,
        ORACLE,
        "uplift_waves",
        "agent",
        "agent_greedy",
    } <= names


@pytest.mark.slow
def test_the_agent_and_an_ablation_variant_run_unchanged_in_the_waves_world(data) -> None:
    agent = run(data, "heterogeneous", "agent", days=21)
    greedy = run(data, "heterogeneous", "agent_greedy", days=21)
    for result in (agent, greedy):
        assert result.row["violations"] == 0 and result.row["holdout_contacted"] == 0
        assert result.row["contacts"] > 0
        assert (
            result.row["true_value"] <= run(data, "heterogeneous", ORACLE, days=21).row["true_value"] + 1e-6
        )


@pytest.mark.slow
def test_uplift_waves_randomises_a_slice_refits_weekly_and_never_touches_the_holdout() -> None:
    from autonomy.gym.waves.data import build_waves_data, fixture_tables

    receipts, clients, cutoff = fixture_tables(1800, seed=5)
    big = build_waves_data(receipts, clients, cutoff=cutoff)
    result = run_one(big, get_world("heterogeneous"), "uplift_waves", 0, days=49)
    row = result.row
    assert row["holdout_contacted"] == 0 and row["violations"] == 0
    diag = row["policy_diagnostics"]
    assert diag["slice_rows"] > 100 and diag["default_rule"] == "weekly_sms"
    assert all(f["day"] % 7 == 0 and f["day"] >= 21 for f in diag["fits"])
    assert row["contacts"] > 0


def test_uplift_waves_randomises_every_reachable_customer_and_sends_its_default_before_the_first_fit(
    data: WavesData, monkeypatch: pytest.MonkeyPatch
) -> None:
    import autonomy.gym.waves.uplift as uplift

    made: list[uplift.UpliftWavesPolicy] = []
    original = uplift.UpliftWavesPolicy

    def build(brief: Brief, content: Sequence[ContentItem]) -> uplift.UpliftWavesPolicy:
        policy = original(brief, content, slice_share=0.5)  # a big slice, so a short run fills it
        made.append(policy)
        return policy

    monkeypatch.setattr(uplift, "UpliftWavesPolicy", build)
    row = run(data, "heterogeneous", "uplift_waves", days=14).row
    policy = made[-1]
    one_channel = [r for r in policy._rows if r.propensity == 0.5]
    both = [r for r in policy._rows if abs(r.propensity - 1 / 3) < 1e-12]
    assert one_channel and both  # SMS-only customers are randomised too, between no message and an SMS
    assert {r.arm for r in one_channel} <= {0, 1} and {r.arm for r in both} == {0, 1, 2}
    # no fit before day 21: the default rule (one SMS a week) is what runs outside the slice
    assert not policy.fits and row["sms_contacts"] > len(policy._rows)
    assert row["contacts_per_customer_week"] > 0.5
    assert row["holdout_contacted"] == 0 and row["violations"] == 0
