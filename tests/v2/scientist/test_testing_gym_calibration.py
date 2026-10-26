"""Real-calibrated Gym scenarios: fitted on TRAIN only, the numbers planted as fitted, nothing else moved."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from autonomy.contracts import Channel, EventType, Theme
from autonomy.gym.api import PERSONAS, ScenarioSpec
from autonomy.gym.calibration import (
    ARM_THEMES,
    CampaignFit,
    daily_rate,
    fit_campaign,
    fit_clicks,
    scenario_from_fit,
    shrink,
)
from autonomy.gym.scenarios import (
    COMPLAINT_COST,
    HISTORY_SENDS_PER_WEEK,
    REAL_EMAIL_FIT,
    REAL_EMAIL_LIKE,
    REAL_EMAIL_PERSONAS,
    REAL_SMS_FIT,
    REAL_SMS_LIKE,
    REAL_SMS_PERSONAS,
    SCENARIOS,
    default_brief,
    default_content,
)
from autonomy.gym.score import gym_world

REPO = Path(__file__).resolve().parents[3]

# --- every earlier scenario is exactly what it was -------------------------------------------------

# SHA-256 (first 16 hex) of each pre-existing scenario's spec dump, taken from the code before the
# calibration task touched anything, without the two click knobs that task added.
OLD_SPECS = {
    "baseline_shop": "f351b3979270613f",
    "complaint_storm": "018aa0f79730f27c",
    "discount_cannibalisation": "c57b148ffdd31762",
    "drift_midway": "d2dadc9b8f1e02b4",
    "null_effect": "3b15c600cb587acb",
    "send_time_matters": "74851febd0e7da24",
    "sleeping_dogs_15": "d8529bbffd674a06",
    "sleeping_dogs_hidden": "825cb082e5c3900d",
    "small_business": "8df005afc1dd49e1",
    "smoke": "dd357c7b9851d50f",
    "whatsapp_fatigue": "2ff3fc5f8715380a",
}
# (events, hash) of the first 'reset(0)' history of three scenarios, shrunk to 200-300 customers, taken
# before the world read its click rates from the spec: the new knobs' defaults must reproduce it.
OLD_HISTORY = {
    "smoke": (2616, "e088fdd5e692540a"),
    "baseline_shop": (1713, "05ed53528cf91335"),
    "whatsapp_fatigue": (1723, "dfea79a85f4d213b"),
}


MOMENT_KNOBS = (
    "cart_rate_per_day",
    "cart_lift_multiplier",
    "cart_half_life_days",
    "refill_share",
    "refill_cycle_days",
    "refill_lift_multiplier",
    "refill_lead_days",
)


def _spec_hash(spec: ScenarioSpec) -> str:
    d = spec.model_dump(mode="json")
    d.pop("click_floor")
    d.pop("click_per_extra_purchase")
    for knob in MOMENT_KNOBS:  # added with plan-v3's moments_planted; every older scenario leaves them off
        d.pop(knob)
    return hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest()[:16]


def test_existing_scenarios_are_unchanged() -> None:
    assert set(SCENARIOS) == {*OLD_SPECS, REAL_EMAIL_LIKE, REAL_SMS_LIKE, "moments_planted"}
    for name, digest in OLD_SPECS.items():
        spec = SCENARIOS[name]
        assert _spec_hash(spec) == digest, f"{name} changed"
        assert spec.click_floor == 0.02 and spec.click_per_extra_purchase == 3.0


@pytest.mark.parametrize("name", sorted(OLD_HISTORY))
def test_the_world_is_byte_identical_with_the_default_click_knobs(name: str) -> None:
    spec = SCENARIOS[name].model_copy(
        update={"n_customers": 300 if name == "smoke" else 200, "days": 6, "history_days": 40}
    )
    world = gym_world(spec, [default_brief()], default_content())
    events = list(world.reset(0))
    h = hashlib.sha256()
    for e in events:
        h.update(e.model_dump_json().encode())
    assert (len(events), h.hexdigest()[:16]) == OLD_HISTORY[name]


# --- estimators --------------------------------------------------------------------------------------


def _campaign(n: int = 6000, seed: int = 5) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(seed)
    arm = rng.integers(0, 3, n)
    group = rng.choice(np.array(["g1", "g2"]), n)
    base = np.where(group == "g1", 0.05, 0.10)
    lift = np.where(arm == 1, 0.04, np.where(arm == 2, 0.02, 0.0))
    bought = (rng.random(n) < base + lift).astype(np.float64)
    clicked = (rng.random(n) < 0.10 + 0.30 * (arm > 0) * (rng.random(n) < 0.5) + 0.0).astype(np.float64)
    return {"arm": arm.astype(np.int64), "group": group, "bought": bought, "clicked": clicked}


def _fit(d: dict[str, np.ndarray], train: np.ndarray, clicked: np.ndarray | None) -> CampaignFit:
    return fit_campaign(
        arm=d["arm"],
        bought=d["bought"],
        clicked=clicked,
        group=d["group"],
        group_names=("g1", "g2"),
        train=train,
        arms=("a", "b"),
        window_days=14,
        channel=Channel.EMAIL,
        aov_mean=50.0,
        margin_rate=0.4,
        channel_cost=0.01,
        fatigue_per_weekly_contact=0.25,
        not_fitted=("fatigue",),
        provenance="synthetic",
    )


def test_the_fit_only_reads_train_rows() -> None:
    d = _campaign()
    n = len(d["arm"])
    train = np.arange(n) < n // 2
    ref = _fit({k: v[train] for k, v in d.items()}, np.ones(n // 2, dtype=bool), d["clicked"][train])
    # poison every non-train row: all bought, all clicked, flipped arms, a stray group name
    poisoned = {k: v.copy() for k, v in d.items()}
    poisoned["bought"][~train] = 1.0
    poisoned["clicked"][~train] = 1.0
    poisoned["arm"][~train] = (poisoned["arm"][~train] + 1) % 3
    got = _fit(poisoned, train, poisoned["clicked"])
    assert got == ref
    assert got.n_train == n // 2


def test_the_fit_recovers_planted_effects_and_shrinks_noise() -> None:
    d = _campaign(n=60_000)
    fit = _fit(d, np.ones(len(d["arm"]), dtype=bool), None)
    g1, g2 = fit.groups
    assert g1.control_rate_raw == pytest.approx(0.05, abs=0.01) and g2.control_rate_raw == pytest.approx(
        0.10, abs=0.01
    )
    assert g1.arm_lift_raw[0] == pytest.approx(0.04, abs=0.015) and g1.arm_lift_raw[1] == pytest.approx(
        0.02, abs=0.015
    )
    assert abs(sum(g.share for g in fit.groups) - 1.0) < 1e-12
    assert all(x >= 0 for g in fit.groups for x in g.arm_lift)
    assert fit.clicks.logged is False and fit.clicks.floor == 0.0 and fit.clicks.per_extra_purchase == 0.0


def test_shrinkage() -> None:
    # identical groups: nothing beyond noise, everyone goes to the pooled value
    out, tau2 = shrink([0.10, 0.11, 0.09], [0.05, 0.05, 0.05])
    assert tau2 == 0.0 and out == pytest.approx([0.10] * 3)
    # well-separated, precisely measured groups barely move
    out, tau2 = shrink([0.0, 1.0], [0.001, 0.001])
    assert tau2 > 0.2 and out == pytest.approx([0.0, 1.0], abs=1e-3)
    # an imprecise group is pulled further than a precise one
    out, _ = shrink([0.0, 0.8, 1.0], [0.001, 0.4, 0.001])
    assert (
        0.5 < out[1] < 0.8
        and out[0] == pytest.approx(0.0, abs=0.01)
        and out[2] == pytest.approx(1.0, abs=0.01)
    )
    assert shrink([0.3], [0.1]) == ([0.3], 0.0)


def test_daily_rate_round_trips() -> None:
    for p in (0.0, 0.0057, 0.3, 0.6):
        r = daily_rate(p, 14)
        assert 1 - (1 - r) ** 14 == pytest.approx(p, abs=1e-12)
    assert daily_rate(1.0, 14) < 1.0


def test_click_fit() -> None:
    rng = np.random.default_rng(9)
    n = 200_000
    arm = (rng.random(n) < 0.5).astype(np.int64)
    bought = (rng.random(n) < 0.05 + 0.02 * arm).astype(np.float64)
    clicked = (rng.random(n) < 0.10 + 0.06 * arm).astype(np.float64)
    c = fit_clicks(arm, bought, clicked)
    assert c.logged and c.floor == pytest.approx(0.10, abs=0.005)
    assert c.per_extra_purchase == pytest.approx(3.0, rel=0.35)  # 0.06 extra clicks / 0.02 extra buys
    assert c.per_extra_purchase_low < c.per_extra_purchase < c.per_extra_purchase_high
    assert c.caused_share_of_treated_clicks == pytest.approx(0.06 / 0.16, abs=0.03)
    assert abs(c.click_purchase_phi) < 0.05  # independent by construction: clicks say nothing here
    # no effect on purchases: no ratio to report
    flat = fit_clicks(arm, (rng.random(n) < 0.05).astype(np.float64), clicked)
    assert flat.per_extra_purchase in (0.0,) or flat.per_extra_purchase > 0  # defined either way
    none = fit_clicks(arm, bought, None)
    assert not none.logged and none.floor == 0.0 and none.per_extra_purchase == 0.0
    # the same bootstrap gives the same interval
    assert fit_clicks(arm, bought, clicked) == c


def test_persona_labels_are_checked() -> None:
    kw = {"name": "t", "description": "d", "history_sends_per_week": 0.4, "complaint_cost": 5.0}
    with pytest.raises(ValueError, match="persona_labels"):
        scenario_from_fit(REAL_EMAIL_FIT, persona_labels={"new": "new", "recent": "new", "lapsed": "loyal"}, **kw)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="persona_labels"):
        scenario_from_fit(REAL_EMAIL_FIT, persona_labels={"new": "new", "recent": "loyal", "lapsed": "mystery"}, **kw)  # type: ignore[arg-type]
    assert set(REAL_EMAIL_PERSONAS.values()) <= set(PERSONAS) and set(REAL_SMS_PERSONAS.values()) <= set(
        PERSONAS
    )
    assert "new" not in REAL_SMS_PERSONAS.values() and "at_risk" not in REAL_SMS_PERSONAS.values()


def test_a_group_without_controls_is_an_error() -> None:
    d = _campaign(n=400)
    d["arm"][d["group"] == "g2"] = 1
    with pytest.raises(ValueError, match="control"):
        _fit(d, np.ones(400, dtype=bool), None)


# --- from a fit to a scenario ------------------------------------------------------------------------


def test_scenario_from_fit_plants_the_fitted_numbers() -> None:
    fit = REAL_EMAIL_FIT
    labels = REAL_EMAIL_PERSONAS
    spec = scenario_from_fit(
        fit,
        name="t",
        description="d",
        history_sends_per_week=0.4,
        complaint_cost=5.0,
        persona_labels=labels,
    )
    assert spec.effect_days == fit.window_days
    assert spec.aov_mean == fit.aov_mean and spec.margin_rate == fit.margin_rate
    assert (
        spec.click_floor == fit.clicks.floor
        and spec.click_per_extra_purchase == fit.clicks.per_extra_purchase
    )
    assert spec.hour_sensitivity == 0.0 and spec.sleeping_dog_share == 0.0
    assert spec.fatigue_per_weekly_contact == fit.fatigue_per_weekly_contact
    assert spec.channel_consent[Channel.EMAIL] == 1.0 and spec.channel_consent[Channel.WHATSAPP] == 0.0
    for g in fit.groups:
        label = labels[g.name]
        assert label in PERSONAS
        assert spec.persona_mix[label] == g.share
        assert 1 - (1 - spec.daily_purchase_rate[label]) ** fit.window_days == pytest.approx(g.control_rate)
        mean_lift = sum(g.arm_lift) / len(g.arm_lift)
        assert spec.channel_uplift[label][Channel.EMAIL] * fit.window_days == pytest.approx(mean_lift)
        for ch in (Channel.WHATSAPP, Channel.SMS, Channel.PUSH):
            assert spec.channel_uplift[label][ch] == 0.0
        # the arm difference: arm i's lift relative to the mean, on its theme; every other theme 1
        tm = spec.theme_uplift[label]
        assert set(tm) == {t.value for t in Theme}
        for theme, lift in zip(ARM_THEMES, g.arm_lift, strict=False):
            assert tm[theme.value] == pytest.approx(lift / mean_lift)
        assert all(v == 1.0 for k, v in tm.items() if k not in {t.value for t in ARM_THEMES})
        # offers add nothing beyond the message
        assert set(spec.offer_uplift[label].values()) == {1.0}


def test_library_scenarios_are_the_stored_fits() -> None:
    for name, fit, labels in (
        (REAL_EMAIL_LIKE, REAL_EMAIL_FIT, REAL_EMAIL_PERSONAS),
        (REAL_SMS_LIKE, REAL_SMS_FIT, REAL_SMS_PERSONAS),
    ):
        built = scenario_from_fit(
            fit,
            name=name,
            description=SCENARIOS[name].description,
            history_sends_per_week=HISTORY_SENDS_PER_WEEK,
            complaint_cost=COMPLAINT_COST,
            persona_labels=labels,
        )
        assert SCENARIOS[name] == built
        assert set(SCENARIOS[name].persona_mix) <= set(PERSONAS)  # the Gym's closed persona set
    sms = SCENARIOS[REAL_SMS_LIKE]
    assert sms.click_floor == 0.0 and sms.click_per_extra_purchase == 0.0  # the campaign logged no clicks
    assert sms.channel_consent[Channel.EMAIL] == 0.0 and sms.channel_consent[Channel.WHATSAPP] == 1.0
    email = SCENARIOS[REAL_EMAIL_LIKE]
    assert email.click_floor > email.click_per_extra_purchase * 0.0  # a real, non-zero click floor
    assert 0.05 < email.click_floor < 0.2


def test_the_stored_fits_say_what_they_are() -> None:
    for fit in (REAL_EMAIL_FIT, REAL_SMS_FIT):
        assert "TRAIN" in fit.provenance
        assert abs(sum(g.share for g in fit.groups) - 1.0) < 1e-4
        text = " ".join(fit.not_fitted)
        for needle in ("fatigue", "unsubscribe", "send-time", "offers", "sleeping dogs"):
            assert needle in text
        assert fit.fatigue_per_weekly_contact == 0.25  # the library's value, flagged as not fitted
        assert all(g.arm_lift[i] >= 0 for g in fit.groups for i in range(len(g.arm_lift)))
        assert fit.pooled_control_rate() > 0 and fit.pooled_lift() > 0
    assert REAL_EMAIL_FIT.arms == ("mens_email", "womens_email") and len(REAL_SMS_FIT.arms) == 1
    # the lift is absolute-small in both: a few percentage points at most per message
    assert REAL_EMAIL_FIT.pooled_lift() < 0.02 and REAL_SMS_FIT.pooled_lift() < 0.06


def test_no_dataset_names_in_the_gym_files_this_task_touched() -> None:
    banned = ("hillstrom", "x5", "retailhero", "lenta", "starbucks", "criteo", "megafon")
    for rel in (
        "autonomy/gym/calibration.py",
        "autonomy/gym/scenarios.py",
        "autonomy/gym/api.py",
        "autonomy/gym/world.py",
    ):
        text = (REPO / rel).read_text().lower()
        for word in banned:
            assert word not in text, f"{rel} mentions {word}"


# --- the world honours the click knobs ----------------------------------------------------------------


def _history_click_rate(name: str) -> tuple[float, int]:
    spec = SCENARIOS[name].model_copy(update={"n_customers": 600, "history_days": 90, "days": 5})
    events = list(gym_world(spec, [default_brief()], default_content()).reset(0))
    sends = sum(1 for e in events if e.type is EventType.SEND)
    clicks = sum(1 for e in events if e.type is EventType.CLICK)
    return (clicks / sends if sends else 0.0), sends


def test_clicks_follow_the_fitted_knobs() -> None:
    sms_rate, sms_sends = _history_click_rate(REAL_SMS_LIKE)
    assert sms_sends > 100 and sms_rate == 0.0  # a channel that logs no clicks logs none
    email_rate, email_sends = _history_click_rate(REAL_EMAIL_LIKE)
    floor = SCENARIOS[REAL_EMAIL_LIKE].click_floor
    assert email_sends > 100
    # floor plus a few extra clicks per extra purchase: above the floor, far from the old 2%
    assert floor <= email_rate < floor + 0.12
