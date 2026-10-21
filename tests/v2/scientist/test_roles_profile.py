"""The data profile: numbers only, hand-computed on a tiny campaign.

Pass/fail checks for this step (written before the code; the roles and the exchange have theirs in
`test_roles_roles.py` and `test_roles_exchange.py`):

P1  `ruff check` and `black --check` on autonomy/scientist/profile.py, run.py, autonomy/agents/
    scientist_roles.py, llm_exchange.py and tests/v2/scientist/test_roles_*.py exit 0.
P2  `timeout 300 python3 -m pytest tests/v2/scientist/test_roles_*.py -q -p no:cacheprovider` and the same
    with /tmp/claude-0/py311/bin/python: all pass, seconds each.
P3  mypy --strict (3.11) on the same source files: zero errors.
P4  The profile has no raw rows or ids: the JSON of a profile built from customers named "SECRET_..."
    contains none of their ids or event ids (test_profile_carries_no_ids).
P5  Every number the profile reports equals the hand-computed value on the fixture campaign (rates, lift
    and its standard error, extra purchases per click and its interval, SRM p, currencies).
P6  Roles: a model answer that breaks the menu, a range, the mandate or the profile's limits is rejected
    and the safe default used (source == "fallback", problems listed); a valid answer is used as given.
P7  The evidence rule equals the hand-computed pick (best corrected lower bound above zero, else
    keep_current); the Judge can only agree or veto with a concrete flag, never pick another candidate;
    its prompt never contains the proposer's rationale or the diagnosis.
P8  Exchange: RecordingLLM saves {id, role, system, prompt, schema} and raises AnswerPending; ReplayLLM
    answers by id; the CLI round-trips diagnose -> propose -> judge with exit codes 0 / 3 / 2, no network.
P9  `grep -niE 'hillstrom|x5|retailhero|lenta|starbucks|criteo|megafon'` on the new autonomy files: 0 lines.

NOT a finding: style preferences, speculative generality (more kinds, more pitfalls, an exact
small-sample test), the thresholds in scientist_roles.py (written before any evidence, not tuned),
or that the profile reads events rather than a database.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from autonomy.contracts import Channel, Event, EventType
from autonomy.scientist.profile import (
    DataProfile,
    build_profile,
    chi2_sf,
    describe_features,
    srm_p_value,
)

T0 = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
SIGNUP = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
Z = 1.959963984540054

# Campaign fixture: 1,000 customers; arm a 300 (p .3), arm b 300 (p .3), 400 unsent (implied .4).
# Buyers in the 14-day window: a 45 (30 of its 90 clickers), b 36 (12 of its 60 clickers), unsent 40.
N_A, N_B, N_C = 300, 300, 400
CLICK_A, CLICK_B = 90, 60
BUY_A, BUY_B, BUY_C = 45, 36, 40
BUY_A_CLICKERS, BUY_B_CLICKERS = 30, 12


class _Ids:
    def __init__(self) -> None:
        self.n = 0

    def __call__(self) -> str:
        self.n += 1
        return f"EVT_SECRET_{self.n}"


def make_events(
    *,
    with_control: bool = True,
    clicks: bool = True,
    extra_currency: bool = False,
    skew_split: bool = False,
    window_closed: bool = True,
) -> list[Event]:
    eid = _Ids()
    out: list[Event] = []

    def add(cid: str, typ: EventType, ts: datetime, **kw: object) -> None:
        out.append(Event(event_id=eid(), customer_id=cid, type=typ, ts=ts, source="test", **kw))  # type: ignore[arg-type]

    arms = [("arm_a", N_A, CLICK_A, BUY_A, BUY_A_CLICKERS), ("arm_b", N_B, CLICK_B, BUY_B, BUY_B_CLICKERS)]
    idx = 0
    for arm, n, n_click, n_buy, n_buy_clickers in arms:
        for i in range(n):
            cid = f"SECRET_cust_{idx:04d}"
            idx += 1
            add(cid, EventType.SIGNUP, SIGNUP)
            add(
                cid,
                EventType.SEND,
                T0,
                channel=Channel.EMAIL,
                attrs={"content_id": arm, "logged_probability": 0.3, "campaign_id": "camp1"},
            )
            clicked = i < n_click
            if clicked and clicks:
                add(cid, EventType.CLICK, T0 + timedelta(hours=2), channel=Channel.EMAIL)
            # buyers: the first n_buy_clickers clickers, then non-clickers from the end
            buys = (clicked and i < n_buy_clickers) or (not clicked and i >= n - (n_buy - n_buy_clickers))
            if buys:
                add(cid, EventType.PURCHASE, T0 + timedelta(days=3), value=20.0, attrs={"currency": "USD"})
    n_control = N_C if with_control else 0
    if skew_split:
        n_control = 900
    for i in range(n_control):
        cid = f"SECRET_cust_{idx:04d}"
        idx += 1
        add(cid, EventType.SIGNUP, SIGNUP)
        if i < BUY_C:
            add(cid, EventType.PURCHASE, T0 + timedelta(days=5), value=20.0, attrs={"currency": "USD"})
    if extra_currency:
        add(
            "SECRET_cust_0000",
            EventType.PURCHASE,
            T0 + timedelta(days=6),
            value=9.0,
            attrs={"currency": "EUR"},
        )
    # a purchase BEFORE the campaign (never counted in the window) and a late event that closes it
    add(
        "SECRET_cust_0001",
        EventType.PURCHASE,
        SIGNUP + timedelta(days=20),
        value=7.0,
        attrs={"currency": "USD"},
    )
    if window_closed:
        add("SECRET_cust_0000", EventType.VISIT, T0 + timedelta(days=30))
    return out


@pytest.fixture(scope="module")
def profile() -> DataProfile:
    return build_profile(make_events(), window_days=14.0)


def test_counts_and_history(profile: DataProfile) -> None:
    assert profile.customers == 1000
    assert profile.events_by_type["send"] == 600
    assert profile.events_by_type["signup"] == 1000
    assert profile.events_by_type["click"] == CLICK_A + CLICK_B
    assert profile.events_by_type["purchase"] == BUY_A + BUY_B + BUY_C + 1
    assert profile.sends_total == 600
    assert profile.campaigns_logged == 1
    assert profile.history_days == pytest.approx(89.0)  # sign-up on 1 Jan, last event on 31 Mar


def test_outcome_rates_lift_and_standard_error(profile: DataProfile) -> None:
    o = profile.outcome
    assert o is not None
    p_t, p_c = (BUY_A + BUY_B) / 600, BUY_C / 400
    assert o.sent_purchase_rate == pytest.approx(p_t)  # 81 / 600 = 0.135
    assert o.unsent_purchase_rate == pytest.approx(p_c)  # 0.10
    assert o.lift == pytest.approx(0.035)
    se = math.sqrt(p_t * (1 - p_t) / 600 + p_c * (1 - p_c) / 400)
    assert o.lift_se == pytest.approx(se)
    assert o.relative_lift == pytest.approx(0.35)
    assert o.purchases == 121  # the pre-campaign purchase is outside every window
    assert o.mean_order_value == pytest.approx(20.0)


def test_control_group_and_design(profile: DataProfile) -> None:
    c = profile.control
    assert c is not None
    assert c.unsent_customers == 400 and c.unsent_share == pytest.approx(0.4)
    assert c.exists and c.randomised and c.design_probabilities_logged and c.design_is_fixed
    assert c.implied_control_share == pytest.approx(0.4)
    assert c.srm_p_value == pytest.approx(1.0)  # 300 / 300 / 400 against .3 / .3 / .4 exactly
    assert profile.campaign_customers_sent == 600
    assert profile.campaign_channels == ("email",)
    assert [a.arm for a in profile.arms] == ["arm_a", "arm_b"]


def test_arms(profile: DataProfile) -> None:
    a, b = profile.arms
    assert (a.customers, b.customers) == (300, 300)
    assert a.purchase_rate == pytest.approx(0.15) and b.purchase_rate == pytest.approx(0.12)
    assert a.click_rate == pytest.approx(0.3) and b.click_rate == pytest.approx(0.2)
    assert a.design_probability == pytest.approx(0.3)
    assert a.mean_order_value == pytest.approx(20.0)


def test_click_to_purchase_inside_and_outside_the_control(profile: DataProfile) -> None:
    k = profile.clicks
    assert k is not None and k.clicks_logged
    assert k.click_rate == pytest.approx(150 / 600)
    assert k.purchase_rate_clickers == pytest.approx(42 / 150)
    assert k.purchase_rate_sent_non_clickers == pytest.approx(39 / 450)
    assert k.purchase_rate_unsent == pytest.approx(0.10)
    per = k.extra_purchases_per_click
    assert per is not None
    se = math.sqrt(0.135 * 0.865 / 600 + 0.1 * 0.9 / 400)
    assert per.estimate == pytest.approx(0.035 / 0.25)  # 0.14 extra purchases per click
    assert per.low == pytest.approx(0.14 - Z * se / 0.25)
    assert per.high == pytest.approx(0.14 + Z * se / 0.25)


def test_currencies_single_and_mixed(profile: DataProfile) -> None:
    assert profile.currencies == {"USD": 122}  # 45 + 36 + 40 in the window, 1 before it
    assert not profile.mixed_currencies and profile.purchases_without_currency == 0
    mixed = build_profile(make_events(extra_currency=True))
    assert mixed.mixed_currencies and mixed.currencies["EUR"] == 1


def test_no_control_group_is_reported() -> None:
    p = build_profile(make_events(with_control=False))
    assert p.control is not None and p.control.unsent_customers == 0 and not p.control.exists
    assert p.outcome is not None and p.outcome.unsent_purchase_rate is None and p.outcome.lift is None
    assert p.clicks is not None and p.clicks.extra_purchases_per_click is None


def test_sample_ratio_mismatch_is_detected() -> None:
    p = build_profile(make_events(skew_split=True))  # 900 unsent where the design implies 400
    assert p.control is not None and p.control.srm_p_value is not None
    assert p.control.srm_p_value < 1e-10


def test_no_clicks_logged() -> None:
    p = build_profile(make_events(clicks=False))
    assert p.clicks is not None and not p.clicks.clicks_logged
    assert p.clicks.click_rate is None and p.clicks.extra_purchases_per_click is None
    assert all(a.click_rate is None for a in p.arms)


def test_open_campaign_is_not_analysed() -> None:
    """No event after last send + window: the window is still open, so no campaign is analysed."""
    p = build_profile(make_events(window_closed=False))
    assert not p.campaign_found and p.control is None and p.outcome is None and p.arms == ()


def test_as_of_closes_a_campaign_whose_history_ends_early() -> None:
    events = make_events(window_closed=False)
    assert not build_profile(events).campaign_found
    closed = build_profile(events, as_of=T0 + timedelta(days=15))
    assert closed.campaign_found and closed.outcome is not None
    assert closed.outcome.lift == pytest.approx(0.035)
    assert not build_profile(events, as_of=T0 + timedelta(days=13)).campaign_found


def test_profile_carries_no_ids(profile: DataProfile) -> None:
    text = profile.to_prompt_json()
    assert "SECRET" not in text and "EVT_" not in text
    assert DataProfile.model_validate_json(text).to_prompt_json() == text  # numbers and names round-trip
    assert len(text) < len(profile.model_dump_json())  # rounded floats keep the prompt small


def test_profile_is_deterministic() -> None:
    a = build_profile(make_events()).to_prompt_json()
    b = build_profile(list(reversed(make_events()))).to_prompt_json()
    assert a == b


def test_describe_features_keeps_names_and_counts_not_values() -> None:
    cols: dict[str, list[object]] = {
        "recency": [1, 2, 3, 3, None],
        "zip": ["urban", "rural", "urban", None, "suburb"],
        "flag": [True, False, True, True, False],
    }
    info = {f.name: f for f in describe_features(cols)}
    assert info["recency"].kind == "numeric" and info["recency"].distinct == 3
    assert info["recency"].missing_share == pytest.approx(0.2)
    assert info["zip"].kind == "categorical" and info["zip"].distinct == 3
    assert info["flag"].kind == "categorical"
    assert "urban" not in "".join(f.model_dump_json() for f in info.values())


def test_chi_square_known_values() -> None:
    assert chi2_sf(3.841458820694124, 1) == pytest.approx(0.05, abs=1e-6)
    assert chi2_sf(5.991464547107979, 2) == pytest.approx(0.05, abs=1e-6)
    assert chi2_sf(9.487729036781154, 4) == pytest.approx(0.05, abs=1e-6)
    assert chi2_sf(0.0, 3) == 1.0
    # observed 30 / 70 against 50 / 50: chi2 = 16, df 1, p = 6.334248e-05
    assert srm_p_value([30, 70], [0.5, 0.5]) == pytest.approx(6.334248e-05, rel=1e-4)
    with pytest.raises(ValueError):
        srm_p_value([5], [1.0])
