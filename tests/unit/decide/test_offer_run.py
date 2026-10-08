"""Plan J M100 part B: the offer choice a scoring run makes (`engine.decide.offer_run`), unit by unit.

The real run is `tests/integration/decide/test_offer_choice_run.py`; these pin the pieces it is built
from: where each offer's costs come from, which offers a customer is eligible for, the budget in rupees
and in contacts, what happens with no value to choose by, and the two settings that name offers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

from engine.config import ConfigError, load_use_case
from engine.decide.catalogue import CatalogueStamp, StampedAction, validate_action_ids
from engine.decide.offer_choice import NO_OFFER
from engine.decide.offer_run import OFFER_REASON_TEXT, decide_offers, plan_arms
from engine.pilot.plain import jargon_in
from engine.pilot.roi import ValueCosts
from engine.uplift.config import UpliftConfig, UpliftPolicyConfig

LEVELS = ("none", "offer_a", "offer_b")
STAMP = CatalogueStamp(
    run_id="r_20261008_0e400001",
    catalogue_sha256="0" * 64,
    planned_channels={"a_sms": ("sms",), "b_email": ("email", "sms")},
    created_at=datetime(2026, 10, 8, tzinfo=UTC),
    actions={
        "a_sms": StampedAction(label="Offer A", channels=("sms",), offer_cost=10.0, contact_cost=0.5),
        "b_email": StampedAction(
            label="Offer B", channels=("email", "sms"), offer_cost=50.0, contact_cost=0.2
        ),
    },
)
MAPPED = {"offer_a": "a_sms", "offer_b": "b_email"}


def _policy(**fields: object) -> UpliftPolicyConfig:
    return UpliftPolicyConfig.model_validate({"value_per_conversion": 1000.0, **fields})


# ---------------------------------------------------------------------------
# Costs per offer
# ---------------------------------------------------------------------------
def test_a_catalogue_offer_is_priced_from_the_run_s_stamp() -> None:
    arms, notes = plan_arms(
        LEVELS,
        _policy(arm_action_ids=MAPPED),
        stamp=STAMP,
        configured_channels=("sms", "email"),
        value_costs=None,
    )
    assert notes == ()
    assert [(a.label, a.channels, a.costs.offer_cost, a.contact_cost, a.cost_source) for a in arms] == [
        ("Offer A", ("sms",), 10.0, 0.5, "catalogue"),
        ("Offer B", ("email", "sms"), 50.0, 0.2, "catalogue"),
    ]


def test_an_offer_without_an_action_is_priced_as_m97_prices_the_first_offer() -> None:
    policy = _policy(arm_action_ids={"offer_a": "a_sms"}, cost_per_contact=2.0)
    arms, _ = plan_arms(LEVELS, policy, stamp=STAMP, configured_channels=("sms", "email"), value_costs=None)
    b = arms[1]
    assert (b.label, b.channels, b.action_id) == ("offer_b", ("sms", "email"), None)
    assert (b.costs.offer_cost, b.contact_cost, b.cost_source) == (0.0, 2.0, "run_settings")
    file_costs = ValueCosts(offer_cost=5.0, contact_cost=0.86)
    arms, _ = plan_arms(LEVELS, _policy(), stamp=None, configured_channels=(), value_costs=file_costs)
    assert [(a.costs.offer_cost, a.contact_cost, a.cost_source) for a in arms] == [
        (5.0, 0.86, "value_settings"),
        (5.0, 0.86, "value_settings"),
    ]


def test_an_offer_whose_action_the_stamp_lacks_is_said_and_priced_from_the_settings() -> None:
    policy = _policy(arm_action_ids={"offer_a": "gone", "offer_x": "a_sms"})
    arms, notes = plan_arms(LEVELS, policy, stamp=STAMP, configured_channels=(), value_costs=None)
    assert arms[0].cost_source == "run_settings"
    assert any("'offer_x'" in note for note in notes) and any("'gone'" in note for note in notes)


def test_the_catalogue_s_contact_cost_wins_over_the_run_s_cost_per_contact() -> None:
    policy = _policy(arm_action_ids=MAPPED, cost_per_contact=99.0)
    arms, _ = plan_arms(LEVELS, policy, stamp=STAMP, configured_channels=(), value_costs=None)
    uplift = np.array([[0.1, 0.1]])
    taken = np.array([[0.2, 0.2]])
    rows = decide_offers(
        uplift,
        taken,
        arms=arms,
        policy=policy,
        suppressed=[False],
        control=[False],
        sleeping_dog_max=-0.01,
    )
    assert rows.money.cost[0].tolist() == pytest.approx([0.5 + 10.0 * 0.2, 0.2 + 50.0 * 0.2])


# ---------------------------------------------------------------------------
# Eligibility, channels and the budget
# ---------------------------------------------------------------------------
def _rows(**policy_fields: object) -> tuple[object, np.ndarray]:
    policy = _policy(arm_action_ids=MAPPED, **policy_fields)
    arms, _ = plan_arms(LEVELS, policy, stamp=STAMP, configured_channels=("sms", "email"), value_costs=None)
    #               A      B      customer
    uplift = np.array(
        [
            [0.30, 0.20],  # 0: both pay, A more
            [0.30, 0.20],  # 1: no SMS: only B (by email)
            [0.30, 0.20],  # 2: no SMS, no email: nothing
            [-0.2, 0.20],  # 3: a sleeping dog for A
            [-0.2, -0.2],  # 4: a sleeping dog for both
            [0.30, 0.20],  # 5: suppressed
            [0.30, 0.20],  # 6: held back as control
            [0.30, 0.01],  # 7: no SMS, B does not pay
        ]
    )
    taken = np.full(uplift.shape, 0.3)
    sms = np.array([True, False, False, True, True, True, True, False])
    email = np.array([True, True, False, True, True, True, True, True])
    rows = decide_offers(
        uplift,
        taken,
        arms=arms,
        policy=policy,
        suppressed=np.array([False] * 5 + [True, False, False]),
        control=np.array([False] * 6 + [True, False]),
        sleeping_dog_max=-0.01,
        contactable={"sms": sms, "email": email},
    )
    return rows, uplift


def test_eligibility_is_contactability_suppression_and_the_control_group() -> None:
    rows, _ = _rows()
    choice = rows.choice  # type: ignore[attr-defined]
    assert choice.arm.tolist() == [1, 2, NO_OFFER, 2, NO_OFFER, NO_OFFER, NO_OFFER, NO_OFFER]
    assert rows.channel.tolist() == ["sms", "email", None, "email", None, None, None, None]  # type: ignore[attr-defined]
    assert choice.reason[2] == "no_eligible_offer" and choice.reason[4] == "sleeping_dog"
    assert choice.reason[7] == "below_cost"
    assert choice.runner_up_arm[0] == 2 and rows.runner_up_channel[0] == "email"  # type: ignore[attr-defined]
    # B is sent by email first and SMS second: customer 3 has both, so email.
    assert rows.eligible[:, 0].tolist() == [True, False, False, True, True, False, False, False]  # type: ignore[attr-defined]


def test_a_budget_in_rupees_and_a_budget_in_contacts_both_hold() -> None:
    rows, _ = _rows(total_budget=4.0)
    choice = rows.choice  # type: ignore[attr-defined]
    assert choice.spent <= 4.0 and choice.arm.tolist()[0] == 1  # A costs 3.5, B 15.2
    assert choice.reason[1] == "over_budget" and choice.reason[3] == "over_budget"
    rows, _ = _rows(budget_contacts=1)
    choice = rows.choice  # type: ignore[attr-defined]
    assert int((choice.arm != NO_OFFER).sum()) == 1 and choice.arm[0] == 1  # the best per rupee


def test_with_no_value_there_is_nothing_to_choose_by() -> None:
    policy = UpliftPolicyConfig.model_validate({"arm_action_ids": MAPPED})
    arms, _ = plan_arms(LEVELS, policy, stamp=STAMP, configured_channels=(), value_costs=None)
    with pytest.raises(ValueError, match="value_per_conversion"):
        decide_offers(
            np.zeros((1, 2)),
            np.zeros((1, 2)),
            arms=arms,
            policy=policy,
            suppressed=[False],
            control=[False],
            sleeping_dog_max=-0.01,
        )


def test_the_reasons_are_plain_words() -> None:
    for text in OFFER_REASON_TEXT.values():
        assert not jargon_in(text), (text, jargon_in(text))
        assert "uplift" not in text.lower() and "feature" not in text.lower()


# ---------------------------------------------------------------------------
# The two settings
# ---------------------------------------------------------------------------
def test_arm_action_ids_name_offers_never_the_control() -> None:
    config = UpliftConfig.model_validate(
        {"treatment_levels": [0, 1, 2], "policy": {"arm_action_ids": {1.0: "a", "2": "b"}}}
    )
    assert config.policy.arm_action_ids == {"1": "a", "2": "b"}
    with pytest.raises(ValueError, match="control"):
        UpliftConfig.model_validate({"treatment_levels": [0, 1, 2], "policy": {"arm_action_ids": {0: "a"}}})
    with pytest.raises(ValueError, match="not one of the offers"):
        UpliftConfig.model_validate({"treatment_levels": [0, 1, 2], "policy": {"arm_action_ids": {3: "a"}}})


def test_an_unknown_offer_action_fails_config_load(tmp_path: Path, config_root: Path) -> None:
    import shutil

    root = tmp_path / "configs"
    shutil.copytree(config_root, root)
    (root / "decide" / "catalogue.yaml").write_text(
        "actions:\n  - {action_id: a_sms, label: A, channels: [sms]}\n", encoding="utf-8"
    )
    base = load_use_case("win-back-campaign", root)
    policy = base.uplift.policy.model_copy(update={"arm_action_ids": {"offer_a": "missing"}})
    config = base.model_copy(update={"uplift": base.uplift.model_copy(update={"policy": policy})})
    with pytest.raises(ConfigError) as caught:
        validate_action_ids(config, root=root)
    assert caught.value.code == "CATALOGUE_ACTION_UNKNOWN"
    assert caught.value.path == "uplift.policy.arm_action_ids.offer_a"


def test_unset_offer_settings_are_left_out_of_the_saved_configuration() -> None:
    dumped = UpliftPolicyConfig().model_dump(mode="json")
    assert "arm_action_ids" not in dumped and "total_budget" not in dumped
    dumped = _policy(arm_action_ids=MAPPED, total_budget=100.0).model_dump(mode="json")
    assert dumped["arm_action_ids"] == MAPPED and dumped["total_budget"] == 100.0


# ---------------------------------------------------------------------------
# The treat list's reading of the choice
# ---------------------------------------------------------------------------
def _objects(*values: object) -> np.ndarray:
    return np.array(values, dtype=object)


def test_the_treat_list_follows_the_choice_and_an_explored_customer_gets_the_best_offer_there_is() -> None:
    """M92's explore slice is treated outside the policy: a customer the choice left without an offer
    gets the best offer they could be given (the runner-up), never one they are a sleeping dog for or
    cannot be reached on - the choice's runner-up never is; with no such offer they are not treated."""
    import pandas as pd

    from engine.decide.treat_list import _apply_offer_choice, _OfferChoice

    chosen = _OfferChoice(
        covered=np.array([True, True, True, True, True, False]),
        arm=np.array([1, 0, 0, 0, 2, 0]),
        label=_objects("A", None, None, None, "B", None),
        channel=_objects("sms", None, None, None, "email", None),
        net_value=np.array([10.0, np.nan, np.nan, np.nan, 7.0, np.nan]),
        runner_up_arm=np.array([2, 2, 0, 1, 0, 0]),
        runner_up_label=_objects("B", "B", None, "A", None, None),
        runner_up_channel=_objects("email", "email", None, "sms", None, None),
        runner_up_value=np.array([3.0, -4.0, np.nan, -1.0, np.nan, np.nan]),
        reason=_objects("offer", "below_cost", "sleeping_dog", "over_budget", "offer", None),
    )
    applied = _apply_offer_choice(
        chosen,
        suppressed=np.array([False, False, False, False, True, False]),
        held_out=np.zeros(6, dtype=bool),
        explore=np.array([False, True, True, False, False, False]),
        fallback_treat=np.array([False, False, False, False, False, True]),
        fallback_offer=pd.Series(["x"] * 6, dtype="object"),
        fallback_channel=pd.Series(["y"] * 6, dtype="object"),
        fallback_net_value=pd.Series([1.0] * 6),
        contactability_written=True,
    )
    assert applied.treat.tolist() == [True, True, False, False, False, True]
    assert applied.offer.tolist() == ["A", "B", None, None, None, "x"]
    assert applied.channel.tolist() == ["sms", "email", None, None, None, "y"]
    assert applied.net_value.tolist()[:2] == [10.0, -4.0]
    assert applied.runner_up_offer.tolist() == ["B", None, None, "A", None, None]
    reasons = applied.offer_reason.tolist()
    assert reasons[0] is None and reasons[1] is None and reasons[4] is None, "treated or suppressed"
    assert "less likely to respond" in reasons[2] and "budget" in reasons[3]
    assert applied.offer_counts == {"A": 1, "B": 1} and applied.uncontactable_rows == 0
