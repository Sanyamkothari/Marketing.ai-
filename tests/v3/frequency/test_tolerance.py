"""Tolerance from look-alikes: the tiredness model learns per profile neighbourhood.

Pure maths on planted data; no Gym. Each test runs in well under a second.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from autonomy.contracts import Channel, ChannelStats, CustomerState
from autonomy.decide.frequency import LookalikeHazard, ToleranceConfig
from autonomy.decide.lookalike import profile_of
from autonomy.decide.models import HazardModel

NOW = datetime(2026, 10, 20, 7, 0, tzinfo=UTC)
REACHABLE = ChannelStats(address_known=True, consent=True, delivers=20, clicks=2)


def person(customer_id: str, group: str, **kw: object) -> CustomerState:
    """Two neighbourhoods that differ in what the profile shows: `tired` have bought long ago and
    have been sent a lot; `calm` bought recently and are sent little."""
    base: dict[str, object] = {
        "customer_id": customer_id,
        "as_of": NOW,
        "channels": {Channel.EMAIL: REACHABLE},
        "first_seen_at": NOW - timedelta(days=400),
        "purchases": 2,
        "revenue": 80.0,
    }
    if group == "tired":
        base.update(segment="lapsed", last_purchase_at=NOW - timedelta(days=300), contacts_30d=6)
    else:
        base.update(segment="heavy", last_purchase_at=NOW - timedelta(days=10), contacts_30d=1)
    base.update(kw)
    return CustomerState.model_validate(base)


def feed(
    model: LookalikeHazard,
    *,
    tired_rate: float,
    calm_rate: float,
    n: int = 1500,
    seed: int = 0,
    night: str = "",
) -> None:
    """Deliver `n` first-of-the-week messages to each group and let the planted rate cost the contact."""
    rng = np.random.default_rng(seed)
    for group, rate in (("tired", tired_rate), ("calm", calm_rate)):
        for i in range(n):
            key = f"{night}{group}{i}"
            lost = bool(rng.random() < rate)
            model.observe_send(Channel.EMAIL, 0)
            if lost:
                model.observe_event(Channel.EMAIL, 0)
            model.record_send(key, key, profile_of(person(key, group)), Channel.EMAIL, 0)
            if lost:
                model.record_loss(key)


def test_before_any_data_it_answers_exactly_like_the_plain_hazard() -> None:
    plain, look = HazardModel(), LookalikeHazard()
    for n in range(5):
        for state in (person("a", "tired"), person("b", "calm")):
            assert look.rate_for(state, Channel.EMAIL, n) == plain.rate(Channel.EMAIL, n)
            assert look.rate_for(state, Channel.WHATSAPP, n) == plain.rate(Channel.WHATSAPP, n)
    assert look.rate(Channel.EMAIL, 1) == plain.rate(Channel.EMAIL, 1)
    assert not look.ready


def test_too_few_lost_contacts_leave_the_plain_answer() -> None:
    look = LookalikeHazard(ToleranceConfig(min_events=20.0))
    feed(look, tired_rate=0.004, calm_rate=0.0, n=1000)  # about four events in all
    assert look.fit() is False and not look.ready
    state = person("x", "tired")
    assert look.rate_for(state, Channel.EMAIL, 0) == look.rate(Channel.EMAIL, 0)


def test_a_planted_tired_group_is_priced_higher_than_a_calm_one() -> None:
    look = LookalikeHazard()
    feed(look, tired_rate=0.05, calm_rate=0.002)
    assert look.fit() is True and look.ready
    tired = look.rate_for(person("new_t", "tired"), Channel.EMAIL, 0)
    calm = look.rate_for(person("new_c", "calm"), Channel.EMAIL, 0)
    # a customer nobody has sent anything to yet gets their neighbourhood's rate
    assert tired > 4 * calm
    assert tired == pytest.approx(0.05, rel=0.5)
    assert calm < look.rate(Channel.EMAIL, 0) < tired


def test_the_answer_is_still_non_decreasing_in_the_weekly_count() -> None:
    look = LookalikeHazard()
    feed(look, tired_rate=0.05, calm_rate=0.002)
    look.fit()
    for group in ("tired", "calm"):
        state = person("p", group)
        rates = [look.rate_for(state, Channel.EMAIL, n) for n in range(6)]
        assert rates == sorted(rates)
        assert all(0 < r <= 0.5 for r in rates)


def test_the_weekly_count_in_the_profile_does_not_double_count() -> None:
    look = LookalikeHazard()
    feed(look, tired_rate=0.05, calm_rate=0.002)
    look.fit()
    a = look.multiplier(person("same", "tired", contacts_7d=0))
    b = look.multiplier(person("same", "tired", contacts_7d=3))
    assert a == pytest.approx(b)


def test_a_customers_own_record_moves_them_off_their_neighbourhood() -> None:
    look = LookalikeHazard()
    feed(look, tired_rate=0.02, calm_rate=0.02)  # no difference between the groups
    # "loyal" has been sent 400 emails and never left. "soon" lost the email contact on the third
    # email and is still reachable on WhatsApp: a lost contact stops that channel (the rules block
    # it), so a customer has at most one loss per channel.
    for i in range(400):
        look.record_send(f"loyal{i}", "loyal", profile_of(person("loyal", "calm")), Channel.EMAIL, 0)
    for i in range(3):
        key = f"soon{i}"
        look.record_send(key, "soon", profile_of(person("soon", "calm")), Channel.EMAIL, 0)
    look.record_loss("soon2")
    look.fit()
    strangers = look.rate_for(person("stranger", "calm"), Channel.WHATSAPP, 0)
    loyal = look.rate_for(person("loyal", "calm"), Channel.WHATSAPP, 0)
    soon = look.rate_for(person("soon", "calm"), Channel.WHATSAPP, 0)
    assert loyal < strangers < soon
    # two messages barely move a customer
    look.record_send("thin0", "thin", profile_of(person("thin", "calm")), Channel.EMAIL, 0)
    look.record_send("thin1", "thin", profile_of(person("thin", "calm")), Channel.EMAIL, 0)
    look.fit()
    thin = look.rate_for(person("thin", "calm"), Channel.EMAIL, 0)
    assert thin == pytest.approx(look.rate_for(person("stranger", "calm"), Channel.EMAIL, 0), rel=0.02)


def test_a_full_store_keeps_refitting_as_the_crowd_changes() -> None:
    cfg = ToleranceConfig(max_rows=1000, refit_min_new=50, min_events=5.0)
    look = LookalikeHazard(cfg)
    feed(look, tired_rate=0.12, calm_rate=0.002, n=500)
    assert look.fit() is True

    def gap() -> float:
        return look.multiplier(person("t", "tired")) / look.multiplier(person("c", "calm"))

    first = gap()
    assert first > 4.0
    for night in range(5):  # the store is full: each night's 1000 sends push the oldest out
        feed(look, tired_rate=0.02, calm_rate=0.002, n=500, seed=night + 1, night=f"n{night}-")
        assert look.fit() is True and look.rows == 1000
    assert gap() < first / 2  # the tired group calmed down, and the crowd's picture followed


def test_it_does_not_invent_a_difference_between_identical_groups() -> None:
    look = LookalikeHazard()
    feed(look, tired_rate=0.02, calm_rate=0.02, seed=3)
    look.fit()
    tired = look.rate_for(person("t", "tired"), Channel.EMAIL, 0)
    calm = look.rate_for(person("c", "calm"), Channel.EMAIL, 0)
    assert tired / calm == pytest.approx(1.0, abs=0.35)


def test_the_multiplier_is_bounded() -> None:
    look = LookalikeHazard(ToleranceConfig(max_multiplier=3.0))
    feed(look, tired_rate=0.2, calm_rate=0.0005)
    look.fit()
    for group in ("tired", "calm"):
        assert 1 / 3.0 - 1e-9 <= look.multiplier(person("b", group)) <= 3.0 + 1e-9


def test_duplicate_sends_and_unknown_losses_are_ignored() -> None:
    look = LookalikeHazard()
    profile = profile_of(person("a", "calm"))
    look.record_send("k", "a", profile, Channel.EMAIL, 0)
    look.record_send("k", "a", profile, Channel.EMAIL, 0)
    look.record_loss("nobody")
    assert look.rows == 1


def test_the_config_refuses_nonsense() -> None:
    with pytest.raises(ValueError):
        ToleranceConfig(min_events=0)
    with pytest.raises(ValueError):
        ToleranceConfig(max_multiplier=0.5)
