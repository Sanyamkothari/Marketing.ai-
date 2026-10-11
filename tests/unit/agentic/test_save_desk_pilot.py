"""The save-desk pilot design: randomised card access across staff, and its power with clusters (Plan K M144).

A design, not a result. These tests pin what the design file promises: staff (the cluster) are drawn once, the
split is exact and reproducible, the example file is a plan the product accepts, the power allows for the
clustering the product's measurement does not, and an active pilot serves the card only to the drawn staff.
"""

from __future__ import annotations

import math
import random
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from engine.agentic.errors import AgenticError
from engine.agentic.save_desk import (
    PILOT_FILE,
    PilotDesign,
    PilotPlanning,
    assign_card_access,
    card_access_allowed,
    load_pilot,
    pilot_power,
)
from engine.measurement import planner
from engine.measurement.plan import TestPlanInput

REPO_ROOT = Path(__file__).resolve().parents[3]
EXAMPLE = REPO_ROOT / "configs" / PILOT_FILE


def _staff(count: int) -> list[str]:
    return [f"u_staff_{position:03d}" for position in range(count)]


def _planning(**change: Any) -> PilotPlanning:
    base: dict[str, Any] = {
        "staff_count": 200,
        "calls_per_staff": 30,
        "base_retention": 0.6,
        "assumed_icc": 0.05,
        "icc_source": "a test's assumption",
    }
    return PilotPlanning.model_validate(base | change)


# ---------------------------------------------------------------------------
# The randomisation: the staff member is the cluster
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("count", [2, 3, 10, 17, 40, 201])
def test_the_split_is_exact_not_a_coin_flip_per_person(count: int) -> None:
    arms = assign_card_access(_staff(count), salt="a-pilot-salt")
    with_card = sum(arm == "card" for arm in arms.values())
    assert with_card == min(max(1, math.floor(count / 2 + 0.5)), count - 1)
    assert set(arms) == set(_staff(count)) and set(arms.values()) == {"card", "no_card"}


def test_the_share_is_honoured_and_at_least_one_staff_member_is_in_each_arm() -> None:
    arms = assign_card_access(_staff(20), salt="a-pilot-salt", card_share=0.25)
    assert sum(arm == "card" for arm in arms.values()) == 5
    tiny = assign_card_access(_staff(4), salt="a-pilot-salt", card_share=0.01)
    assert sum(arm == "card" for arm in tiny.values()) == 1
    big = assign_card_access(_staff(4), salt="a-pilot-salt", card_share=0.99)
    assert sum(arm == "no_card" for arm in big.values()) == 1


def test_the_draw_does_not_depend_on_the_order_the_list_came_in_and_is_reproducible() -> None:
    staff = _staff(60)
    shuffled = staff[:]
    random.Random(4).shuffle(shuffled)
    assert assign_card_access(staff, salt="s-1-salt") == assign_card_access(shuffled, salt="s-1-salt")
    assert assign_card_access(staff, salt="s-1-salt") == assign_card_access(staff, salt="s-1-salt")
    assert assign_card_access(staff, salt="s-1-salt") != assign_card_access(staff, salt="s-2-salt")


def test_nobody_can_pick_their_own_arm_by_choosing_an_id() -> None:
    """The arm depends on the salt, which the staff do not know: ids that sort together are not drawn together."""
    arms = assign_card_access([f"u_{position}" for position in range(100)], salt="private-salt-x")
    first_ten = [arms[f"u_{position}"] for position in range(10)]
    assert len(set(first_ten)) == 2


def test_a_staff_list_that_cannot_be_randomised_is_refused() -> None:
    for bad, why in (
        ([], "at least two"),
        (["a"], "at least two"),
        (["a", "a"], "repeated"),
        (["a", " "], "empty"),
    ):
        with pytest.raises(ValueError, match=why):
            assign_card_access(bad, salt="a-pilot-salt")
    with pytest.raises(ValueError, match="between 0 and 1"):
        assign_card_access(_staff(4), salt="a-pilot-salt", card_share=1.0)


# ---------------------------------------------------------------------------
# The planner's power, with clusters
# ---------------------------------------------------------------------------
def test_the_power_is_the_planners_on_the_effective_number_of_customers() -> None:
    planning = _planning()
    power = pilot_power(planning)
    deff = 1.0 + (planning.calls_per_staff - 1) * planning.assumed_icc
    assert power.design_effect == pytest.approx(deff)
    assert (power.staff_card, power.staff_no_card) == (100, 100)
    assert (power.customers_card, power.customers_no_card) == (3_000, 3_000)
    assert power.effective_customers_card == math.floor(3_000 / deff)
    direct = planner.mde_two_proportions(
        power.effective_customers_card, power.effective_customers_no_card, 0.6, 0.05, 0.8
    )
    assert power.smallest_detectable_change_pp == pytest.approx(direct.points)
    assert power.reason is None and "not measured" in power.assumption


def test_clustering_makes_the_pilot_less_sensitive_than_independent_customers_would_be() -> None:
    power = pilot_power(_planning())
    assert power.smallest_detectable_change_pp is not None and power.ignoring_clusters_pp is not None
    assert power.smallest_detectable_change_pp > power.ignoring_clusters_pp * 1.4
    no_clusters = pilot_power(_planning(assumed_icc=0.0))
    assert no_clusters.design_effect == 1.0
    assert no_clusters.smallest_detectable_change_pp == pytest.approx(no_clusters.ignoring_clusters_pp)


def test_more_calls_per_staff_member_do_not_buy_back_the_staff_you_lack() -> None:
    """The number of staff, not of calls, limits a cluster pilot: tripling the calls helps far less than tripling staff."""
    base = pilot_power(_planning(staff_count=60, calls_per_staff=30)).smallest_detectable_change_pp
    more_calls = pilot_power(_planning(staff_count=60, calls_per_staff=90)).smallest_detectable_change_pp
    more_staff = pilot_power(_planning(staff_count=180, calls_per_staff=30)).smallest_detectable_change_pp
    assert base is not None and more_calls is not None and more_staff is not None
    assert more_staff < more_calls < base


def test_the_staff_needed_for_a_target_are_the_plain_sample_times_the_design_effect_over_the_calls() -> None:
    planning = _planning()
    power = pilot_power(planning, target_pp=5.0)
    plain = planner.n_for_mde(0.6, 0.05, 0.05, 0.8)
    assert plain.n_treat is not None
    assert power.staff_card_for_target == math.ceil(plain.n_treat * power.design_effect / 30)
    assert power.staff_no_card_for_target == power.staff_card_for_target, "an even split needs the same staff"


def test_an_uneven_split_needs_different_staff_in_each_arm() -> None:
    """With 30% of staff given the card the two arms are not the same size, and nor is the staff each needs."""
    planning = _planning()
    power = pilot_power(planning, card_share=0.3, target_pp=5.0)
    ratio = power.staff_no_card / power.staff_card
    sizes = planner.n_for_mde(0.6, 0.05, 0.05, 0.8, control_ratio=ratio)
    assert sizes.n_treat is not None and sizes.n_control is not None
    assert power.staff_card_for_target == math.ceil(sizes.n_treat * power.design_effect / 30)
    assert power.staff_no_card_for_target == math.ceil(sizes.n_control * power.design_effect / 30)
    assert power.staff_card_for_target is not None and power.staff_no_card_for_target is not None
    assert power.staff_no_card_for_target > power.staff_card_for_target


def test_a_target_the_rate_cannot_move_by_is_null_with_the_planners_reason() -> None:
    power = pilot_power(_planning(base_retention=0.97), target_pp=20.0)
    assert power.staff_card_for_target is None and power.staff_no_card_for_target is None and power.reason


def test_the_planning_inputs_are_checked() -> None:
    for bad in ({"staff_count": 3}, {"base_retention": 1.0}, {"assumed_icc": 1.0}, {"icc_source": ""}):
        with pytest.raises(ValidationError):
            _planning(**bad)


# ---------------------------------------------------------------------------
# The shipped example and the file's rules
# ---------------------------------------------------------------------------
def test_the_shipped_example_is_a_template_the_product_accepts() -> None:
    design = load_pilot(REPO_ROOT / "configs")
    assert design is not None
    assert design.status == "example" and design.unit == "staff_member"
    assert design.card_access == () and design.no_card == ()
    plan = TestPlanInput.model_validate(design.test_plan)
    assert plan.outcome_window_days == design.outcome_window_days == 30
    assert plan.base_rate == design.planning.base_retention
    power = pilot_power(design.planning, card_share=design.card_share)
    assert power.smallest_detectable_change_pp is not None
    assert (
        plan.mde_pp is not None and plan.mde_pp >= power.smallest_detectable_change_pp
    ), "the registered effect must be one the clustered pilot can see, not the optimistic one"
    assert (
        "no clustered standard errors" in design.measurement_note
        and "no result of this pilot is claimed" in design.measurement_note
    )


def test_the_example_says_in_words_that_it_is_not_a_result() -> None:
    text = EXAMPLE.read_text(encoding="utf-8")
    assert "TEMPLATE, NOT A RESULT" in text and "ASSUMPTION" in text


def test_a_plan_the_product_would_refuse_is_refused_in_the_design() -> None:
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["test_plan"]["mde_pp"] = -1
    with pytest.raises(ValidationError):
        PilotDesign.model_validate(document)
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["unit"] = "customer"
    with pytest.raises(ValidationError):
        PilotDesign.model_validate(document)


def _active(card: list[str], plain: list[str]) -> PilotDesign:
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document.update(status="active", card_access=card, no_card=plain)
    return PilotDesign.model_validate(document)


def test_an_active_pilot_needs_both_arms_filled_and_disjoint() -> None:
    with pytest.raises(ValidationError, match="both arms"):
        _active([], [])
    with pytest.raises(ValidationError, match="both arms"):
        _active(["u_a"], [])
    with pytest.raises(ValidationError, match="in both arms"):
        _active(["u_a", "u_b"], ["u_b", "u_c"])


def test_access_is_open_until_a_pilot_is_active_and_then_only_for_the_drawn_staff() -> None:
    assert card_access_allowed(None, "u_anyone")
    example = load_pilot(REPO_ROOT / "configs")
    assert example is not None and card_access_allowed(example, "u_anyone")
    active = _active(["u_card"], ["u_plain"])
    assert card_access_allowed(active, "u_card")
    assert not card_access_allowed(active, "u_plain")
    assert not card_access_allowed(active, "u_unlisted"), "a person who was never drawn is not served either"


def test_a_pilot_file_that_does_not_read_is_config_invalid(tmp_path: Path) -> None:
    assert load_pilot(tmp_path) is None
    (tmp_path / "agentic").mkdir()
    (tmp_path / "agentic" / "save_desk_pilot.yaml").write_text("status: active\n", encoding="utf-8")
    with pytest.raises(AgenticError) as caught:
        load_pilot(tmp_path)
    assert caught.value.code == "CONFIG_INVALID"


def test_the_numbers_the_document_quotes_are_the_ones_the_code_computes() -> None:
    design = load_pilot(REPO_ROOT / "configs")
    assert design is not None
    power = pilot_power(design.planning, card_share=design.card_share, target_pp=5.0)
    text = " ".join((REPO_ROOT / "docs" / "AGENTIC.md").read_text(encoding="utf-8").split())
    assert power.smallest_detectable_change_pp is not None and power.ignoring_clusters_pp is not None
    assert f"design effect is {power.design_effect:.2f}" in text
    assert f"{power.smallest_detectable_change_pp:.1f} points" in text
    assert f"would have seen {power.ignoring_clusters_pp:.1f}" in text
    assert design.card_share == 0.5 and power.staff_card_for_target == power.staff_no_card_for_target
    assert f"needs {power.staff_card_for_target} staff in each arm" in text
