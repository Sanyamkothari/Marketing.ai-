"""The Gym wrapper of the Scientist (`scripts/v2_gym_scientist.py`): Shadow changes nothing, and a
history that proves nothing leaves the agent as it is."""

from __future__ import annotations

import numpy as np
import pandas as pd

from autonomy.contracts import Channel
from autonomy.gym.run import POLICIES
from autonomy.gym.scenarios import SCENARIOS, default_content
from autonomy.gym.score import run_policy
from autonomy.scientist import Campaign
from scripts import v2_gym_scientist as gs


def _campaign(*arm_names: str) -> Campaign:
    n = 6
    return Campaign(
        customer_ids=tuple(f"c{i}" for i in range(n)),
        arm=np.array([0, 1, 0, 1, 0, 1], dtype=np.int64),
        propensity=np.full(n, 0.5),
        arm_names=("no message", *arm_names),
        arm_cost=(0.0, *([0.0] * len(arm_names))),
        converted=np.zeros(n),
        margin=np.zeros(n),
        features=pd.DataFrame({"x": np.arange(n, dtype=float)}),
        randomised=True,
        source="test",
    )


def test_an_arm_is_the_first_library_item_with_its_channel_offer_and_theme() -> None:
    goals = {Channel.EMAIL: "goal_gym", Channel.WHATSAPP: "goal_gym"}
    acts = gs.arm_actions_of(
        _campaign("email|pct10|savings", "whatsapp|none|reminder"), default_content(), goals
    )
    assert acts is not None
    assert acts[1].content_id == "gym_email_pct10_savings" and acts[1].channel is Channel.EMAIL
    assert acts[2].content_id == "gym_whatsapp_none_reminder" and acts[2].offer_id == "none"


def test_an_arm_with_no_library_item_or_no_goal_is_not_guessed() -> None:
    goals = {Channel.EMAIL: "goal_gym"}
    assert (
        gs.arm_actions_of(_campaign("email|pct10|loyalty"), default_content(), goals) is None
    )  # no such item
    assert gs.arm_actions_of(_campaign("whatsapp|none|reminder"), default_content(), goals) is None  # no goal
    assert gs.arm_actions_of(_campaign("sms|none|reminder"), default_content(), goals) is None  # no channel
    assert gs.arm_actions_of(_campaign("nonsense"), default_content(), goals) is None


def test_shadow_and_a_history_that_proves_nothing_leave_the_agent_unchanged() -> None:
    gs.register()
    spec = SCENARIOS["smoke"].model_copy(update={"n_customers": 300})
    cards = {}
    for name in ("agent", "scientist_shadow", "scientist"):
        gs.LOG.clear()
        cards[name] = run_policy(spec, POLICIES[name], 0, policy_name=name, with_oracle=False)
        if name != "agent":
            assert gs.LOG[0]["ran"] == "agent" and gs.LOG[0]["outcome"] in ("no_campaign", "not_enough_data")
    ref = cards["agent"]
    for name in ("scientist_shadow", "scientist"):
        assert cards[name].true_value == ref.true_value and cards[name].actions == ref.actions
