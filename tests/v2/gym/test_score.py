"""The scoreboard: interval maths, the three-way split, the pass bar, the scenario library, the
baselines and a full run, all on a small stub world so the tests take seconds."""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from autonomy.contracts import (
    Action,
    Brief,
    Channel,
    ChannelStats,
    ContentItem,
    CustomerState,
    DecisionContext,
    DecisionKind,
    Envelope,
    Estimate,
    Group,
)
from autonomy.fakes import FakeEnvironment
from autonomy.gym import run as gym_run
from autonomy.gym.api import PERSONAS, GroundTruth, ScenarioSpec
from autonomy.gym.baselines import (
    ContactEveryone,
    OraclePolicy,
    RuleBaseline,
    is_quiet,
    next_local_time,
    send_hour,
)
from autonomy.gym.scenarios import (
    PLANTED_HARM,
    SCENARIOS,
    default_brief,
    default_content,
    get_scenario,
)
from autonomy.gym.score import (
    EVERYONE,
    ORACLE,
    RULE,
    ScoreCard,
    assign_group,
    compare,
    difference_interval,
    pass_criteria,
    run_policy,
    split_shares,
    wilson_interval,
)

START = datetime(2026, 3, 2, tzinfo=UTC)


# --- a stub world: FakeEnvironment plus a truth read from its private uplifts ---------------------


class StubTruth:
    def __init__(self, env: FakeEnvironment, costs: dict[str, float]) -> None:
        self.env = env
        self.costs = costs

    def _i(self, customer_id: str) -> int:
        return int(customer_id[1:])

    def persona(self, customer_id: str) -> str:
        return PERSONAS[self._i(customer_id) % len(PERSONAS)]

    def is_sleeping_dog(self, customer_id: str) -> bool:
        return self._i(customer_id) % 10 == 0

    def expected_net(self, customer_id: str, action: Action, now: datetime) -> float:
        lift = float(self.env._uplift[action.channel][self._i(customer_id)])
        return lift * 5 * 24.0 - 0.001 - (0.1 * self.costs.get(action.offer_id, 0.0))

    def best_action(self, customer_id: str, candidates: Sequence[Action], now: datetime) -> Action | None:
        if not candidates:
            return None
        best = max(candidates, key=lambda a: self.expected_net(customer_id, a, now))
        return best if self.expected_net(customer_id, best, now) > 0 else None


class StubWorld(FakeEnvironment):
    def __init__(self, spec: ScenarioSpec, briefs: Sequence[Brief]) -> None:
        super().__init__(n_customers=spec.n_customers, start=START, history_days=spec.history_days)
        self.spec = spec
        costs = {o.offer_id: o.cost_per_redemption for b in briefs for o in b.envelope.offers}
        self.truth: GroundTruth = StubTruth(self, costs)

    def violations(self) -> list[str]:
        return []


def stub_factory(spec: ScenarioSpec, briefs: Sequence[Brief], content: Sequence[ContentItem]) -> StubWorld:
    del content
    return StubWorld(spec, briefs)


TINY = ScenarioSpec(name="tiny", n_customers=80, history_days=90, days=10, effect_days=5)


# --- interval maths ----------------------------------------------------------------------------


def test_normal_interval_matches_hand_maths() -> None:
    est = difference_interval([1.0, 2.0, 3.0, 4.0], [0.0, 1.0], scale=10)
    se = math.sqrt((5 / 3) / 4 + 0.5 / 2)
    assert est.point == pytest.approx(20.0)
    assert est.low == pytest.approx((2 - 1.959964 * se) * 10, rel=1e-5)
    assert est.high == pytest.approx((2 + 1.959964 * se) * 10, rel=1e-5)


def test_interval_needs_both_groups_and_handles_single_values() -> None:
    with pytest.raises(ValueError):
        difference_interval([], [1.0])
    est = difference_interval([3.0], [1.0])
    assert est == Estimate(point=2.0, low=2.0, high=2.0)


def test_normal_interval_covers_the_truth_about_95_percent() -> None:
    rng = np.random.default_rng(7)
    covered = 0
    reps = 400
    for _ in range(reps):
        t = rng.exponential(2.0, 400) + 0.3  # skewed, true difference 0.3
        c = rng.exponential(2.0, 100)
        est = difference_interval(t, c)
        covered += int(est.low <= 0.3 <= est.high)
    assert 0.92 <= covered / reps <= 0.98


def test_bootstrap_agrees_with_normal_on_large_groups() -> None:
    rng = np.random.default_rng(3)
    t, c = rng.normal(1.0, 1.0, 2000), rng.normal(0.0, 1.0, 500)
    normal = difference_interval(t, c, scale=100)
    boot = difference_interval(t, c, scale=100, method="bootstrap", n_boot=500, seed=1)
    assert boot.point == pytest.approx(normal.point)
    width = normal.high - normal.low
    assert abs(boot.low - normal.low) < 0.1 * width
    assert abs(boot.high - normal.high) < 0.1 * width


def test_wilson_interval() -> None:
    assert wilson_interval(0, 0) == (0.0, 1.0)
    lo, hi = wilson_interval(95, 100)
    assert lo == pytest.approx(0.8882, abs=1e-3)
    assert hi == pytest.approx(0.9785, abs=1e-3)
    assert wilson_interval(10, 10)[1] == 1.0


# --- the split ---------------------------------------------------------------------------------


def test_split_is_deterministic_and_matches_shares() -> None:
    ids = [f"c{i:06d}" for i in range(20_000)]
    a = [assign_group(c, holdout_share=0.1, baseline_share=0.15, salt="s") for c in ids]
    b = [assign_group(c, holdout_share=0.1, baseline_share=0.15, salt="s") for c in ids]
    other = [assign_group(c, holdout_share=0.1, baseline_share=0.15, salt="t") for c in ids]
    assert a == b
    assert a != other
    share = {g: a.count(g) / len(ids) for g in Group}
    assert share[Group.HOLDOUT] == pytest.approx(0.10, abs=0.01)
    assert share[Group.BASELINE] == pytest.approx(0.15, abs=0.01)
    assert share[Group.AGENT] == pytest.approx(0.75, abs=0.01)


def test_split_shares_come_from_the_brief() -> None:
    assert split_shares([]) == (0.10, 0.10)
    brief = default_brief()
    env = brief.envelope.model_copy(update={"holdout_share": 0.2, "baseline_share": 0.05})
    assert split_shares([brief.model_copy(update={"envelope": env})]) == (0.2, 0.05)


# --- the pass bar ------------------------------------------------------------------------------


def card(policy: str, seed: int, true_value: float, **kw: object) -> ScoreCard:
    fields: dict[str, object] = {
        "scenario": "baseline_shop",
        "policy": policy,
        "seed": seed,
        "days": 90,
        "n_customers": 1000,
        "n_agent": 800,
        "n_baseline": 100,
        "n_holdout": 100,
        "incremental_profit": Estimate(point=true_value, low=true_value - 50, high=true_value + 50),
        "true_value": true_value,
        "interval_covers_truth": True,
        "oracle_value": 1000.0,
        "oracle_share": true_value / 1000.0,
        "actions": 10,
        "contacts": 10,
        "contacts_per_customer_week": 0.1,
        "unsubscribe_rate": 0.0,
        "sleeping_dogs": 100,
        "sleeping_dogs_contacted": 5,
        "sleeping_dog_contact_share": 0.05,
        "violations": 0,
    }
    fields.update(kw)
    return ScoreCard.model_validate(fields)


def good_cards(n: int = 10) -> list[ScoreCard]:
    out = []
    for s in range(n):
        out += [card("agent", s, 900.0), card(EVERYONE, s, 300.0), card(RULE, s, 500.0)]
    return out


def checks_of(cards: list[ScoreCard], **kw: object) -> dict[str, tuple[bool, bool]]:
    report = pass_criteria(cards, **kw)
    return {c.name: (c.evaluated, c.passed) for c in report.checks}


def test_pass_criteria_all_pass() -> None:
    report = pass_criteria(good_cards())
    assert report.policy == "agent"
    assert report.passed
    assert not report.complete  # no planted-harm scenario, so the breaker is skipped
    assert {c.name: c.evaluated for c in report.checks}["breaker"] is False
    assert any("Overall" in line for line in report.lines())


def test_pass_criteria_fails_each_check() -> None:
    cards = [card("agent", c.seed, 700.0) if c.policy == "agent" else c for c in good_cards()]
    assert checks_of(cards)["oracle_capture"] == (True, False)  # 70% of the oracle, still beats both
    assert checks_of(cards)["beats_baselines"] == (True, True)

    cards = good_cards(20)
    cards[0] = card("agent", 0, 400.0)  # loses to the rule on 1 of 20 seeds: 95% still passes
    assert checks_of(cards)["beats_baselines"] == (True, True)
    cards[3] = card("agent", 1, 400.0)  # 2 of 20: 90%
    assert checks_of(cards)["beats_baselines"] == (True, False)

    cards = good_cards()
    cards[0] = card("agent", 0, 900.0, sleeping_dogs_contacted=90, sleeping_dog_contact_share=0.9)
    assert checks_of(cards)["sleeping_dogs"] == (True, False)

    cards = good_cards()
    cards[0] = card("agent", 0, 900.0, violations=1, violation_examples=("Sent at 23:00.",))
    assert checks_of(cards)["violations"] == (True, False)


def test_coverage_check_uses_wilson_band() -> None:
    cards = good_cards(300)  # 300 agent cards, all covered: Wilson lower bound ~99%, above the band
    assert checks_of(cards)["interval_coverage"] == (True, False)
    cards = [c.model_copy(update={"interval_covers_truth": c.seed % 20 != 0}) for c in cards]
    assert checks_of(cards)["interval_coverage"] == (True, True)  # 95% covered
    cards = [c.model_copy(update={"interval_covers_truth": c.seed % 3 != 0}) for c in cards]
    assert checks_of(cards)["interval_coverage"] == (True, False)  # 67% covered


def test_null_effect_and_breaker_and_missing_baselines() -> None:
    null = [
        card("agent", s, -2.0, scenario="null_effect", oracle_value=0.0, oracle_share=None) for s in range(3)
    ]
    assert checks_of(null)["oracle_capture"] == (True, True)  # loses under a cent per customer
    null[0] = card("agent", 0, -500.0, scenario="null_effect", oracle_value=0.0, oracle_share=None)
    assert checks_of(null)["oracle_capture"] == (True, False)
    assert checks_of(null)["beats_baselines"] == (False, False)

    harm = [card("agent", s, 0.0, planted_harm=True, breaker_batches=s) for s in range(3)]
    assert checks_of(harm)["breaker"] == (True, True)
    harm.append(card("agent", 3, 0.0, planted_harm=True, breaker_batches=None))
    assert checks_of(harm)["breaker"] == (True, False)


def test_pass_criteria_needs_a_clear_policy() -> None:
    with pytest.raises(ValueError):
        pass_criteria([*good_cards(), card("other", 0, 1.0)])
    assert pass_criteria([*good_cards(), card("other", 0, 1.0)], policy="other").policy == "other"


# --- the scenario library ----------------------------------------------------------------------


REQUIRED = {
    "baseline_shop",
    "sleeping_dogs_15",
    "discount_cannibalisation",
    "whatsapp_fatigue",
    "send_time_matters",
    "drift_midway",
    "small_business",
    "null_effect",
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_scenarios_are_valid(name: str) -> None:
    spec = SCENARIOS[name]
    assert spec.name == name and spec.description
    assert ScenarioSpec.model_validate(spec.model_dump()) == spec
    assert set(spec.persona_mix) <= set(PERSONAS)
    assert sum(spec.persona_mix.values()) == pytest.approx(1.0)
    assert set(spec.daily_purchase_rate) >= set(spec.persona_mix)
    assert all(0 <= p < 1 for p in spec.daily_purchase_rate.values())
    assert sum(spec.timezones.values()) == pytest.approx(1.0)
    for tz in spec.timezones:
        ZoneInfo(tz)
    for persona, row in spec.channel_uplift.items():
        assert persona in PERSONAS
        assert all(0 <= v < 0.1 for v in row.values())
    assert set(spec.offer_uplift) <= set(PERSONAS) and set(spec.theme_uplift) <= set(PERSONAS)
    if spec.drift_day is not None:
        assert 0 < spec.drift_day < spec.days


def test_library_has_the_named_scenarios() -> None:
    assert set(SCENARIOS) >= REQUIRED
    assert SCENARIOS["sleeping_dogs_15"].sleeping_dog_share == 0.15
    assert SCENARIOS["drift_midway"].drift_day == 45
    assert SCENARIOS["drift_midway"].drift_purchase_multiplier == 0.6
    assert SCENARIOS["small_business"].n_customers == 1500
    assert all(v == 0 for row in SCENARIOS["null_effect"].channel_uplift.values() for v in row.values())
    assert SCENARIOS["send_time_matters"].hour_sensitivity >= 0.8
    assert len(SCENARIOS["send_time_matters"].timezones) > 1
    assert set(SCENARIOS) >= PLANTED_HARM
    assert get_scenario("baseline_shop", n_customers=100).n_customers == 100
    with pytest.raises(KeyError):
        get_scenario("nope")


def test_default_brief_and_content_fit_together() -> None:
    brief, content = default_brief(), default_content()
    offers = {o.offer_id for o in brief.envelope.offers}
    assert brief.approved
    assert {c.offer_id for c in content} == offers
    pairs = {(c.channel, c.offer_id) for c in content}
    assert pairs == {(ch, o) for ch in brief.envelope.channels for o in offers}
    assert len({c.content_id for c in content}) == len(content)


# --- the baselines -----------------------------------------------------------------------------


def state(**kw: object) -> CustomerState:
    fields: dict[str, object] = {
        "customer_id": "c00001",
        "as_of": START,
        "channels": {
            Channel.EMAIL: ChannelStats(address_known=True, consent=True),
            Channel.WHATSAPP: ChannelStats(address_known=True, consent=True),
        },
        "purchases": 2,
        "last_purchase_at": START - timedelta(days=90),
    }
    fields.update(kw)
    return CustomerState.model_validate(fields)


def ctx(now: datetime = START + timedelta(hours=10)) -> DecisionContext:
    return DecisionContext(now=now, briefs=(default_brief(),), content=tuple(default_content()))


def test_time_helpers() -> None:
    now = datetime(2026, 3, 2, 10, 30, tzinfo=UTC)
    assert next_local_time(now, "UTC", 10) == datetime(2026, 3, 3, 10, tzinfo=UTC)
    assert next_local_time(now, "UTC", 12) == datetime(2026, 3, 2, 12, tzinfo=UTC)
    ny = next_local_time(now, "America/New_York", 10)  # 10:00 EST is 15:00 UTC
    assert ny == datetime(2026, 3, 2, 15, tzinfo=UTC)
    assert next_local_time(now, "Not/AZone", 11) == datetime(2026, 3, 2, 11, tzinfo=UTC)
    env = Envelope(channels=(Channel.EMAIL,), budget=1, quiet_start_hour=21, quiet_end_hour=9)
    assert is_quiet(22, env) and is_quiet(3, env) and not is_quiet(9, env) and not is_quiet(20, env)
    assert send_hour(23, env) == 9 and send_hour(14, env) == 14


def test_contact_everyone_uses_first_channel_and_biggest_offer() -> None:
    d = ContactEveryone().decide(state(), ctx())
    assert d.kind is DecisionKind.ACT and d.action is not None
    assert d.action.channel is Channel.EMAIL and d.action.offer_id == "pct10"
    assert d.action.content_id is not None and "pct10" in d.action.content_id
    assert d.action.send_at == START + timedelta(hours=10)
    capped = ContactEveryone().decide(state(contacts_7d=2), ctx())
    assert capped.kind is DecisionKind.NOTHING
    no_consent = state(channels={Channel.WHATSAPP: ChannelStats(address_known=True, consent=False)})
    assert ContactEveryone().decide(no_consent, ctx()).kind is DecisionKind.NOTHING


def test_rule_baseline_only_emails_lapsed_customers_every_14_days() -> None:
    rule = RuleBaseline()
    d = rule.decide(state(), ctx())
    assert d.action is not None and d.action.channel is Channel.EMAIL and d.action.offer_id == "pct10"
    assert rule.decide(state(last_purchase_at=START - timedelta(days=10)), ctx()).action is None
    assert rule.decide(state(purchases=0, last_purchase_at=None), ctx()).action is None
    recent = state(last_contact_at=START - timedelta(days=5))
    assert rule.decide(recent, ctx()).action is None
    assert rule.decide(state(last_contact_at=START - timedelta(days=15)), ctx()).action is not None


def test_oracle_picks_the_truths_best_allowed_action() -> None:
    class Truth:
        def persona(self, customer_id: str) -> str:
            return "new"

        def is_sleeping_dog(self, customer_id: str) -> bool:
            return False

        def expected_net(self, customer_id: str, action: Action, now: datetime) -> float:
            hour_bonus = 1.0 if action.send_at.hour == 19 else 0.0
            return (2.0 if action.channel is Channel.WHATSAPP else 1.0) + hour_bonus

        def best_action(self, customer_id: str, candidates: Sequence[Action], now: datetime) -> Action | None:
            return max(candidates, key=lambda a: self.expected_net(customer_id, a, now))

    d = OraclePolicy(Truth()).decide(state(), ctx())
    assert d.action is not None and d.action.channel is Channel.WHATSAPP
    assert d.action.send_at.hour == 19
    assert OraclePolicy(Truth()).candidates(state(), ctx(), 23) == []  # quiet hours


# --- full runs on the stub world ---------------------------------------------------------------


def test_run_policy_scores_a_run_and_repeats_exactly() -> None:
    a = run_policy(TINY, gym_run.get_policy(EVERYONE), 3, policy_name=EVERYONE, world_factory=stub_factory)
    b = run_policy(TINY, gym_run.get_policy(EVERYONE), 3, policy_name=EVERYONE, world_factory=stub_factory)
    assert a == b
    assert a.n_agent + a.n_baseline + a.n_holdout == a.n_customers == 80
    assert a.contacts > 0 and a.actions >= a.contacts
    assert a.violations == 0  # caps respected, holdout untouched
    assert a.oracle_value is not None and a.oracle_value > 0
    assert a.sleeping_dogs > 0 and a.sleeping_dog_contact_share is not None
    assert a.incremental_profit.low <= a.incremental_profit.point <= a.incremental_profit.high


def test_compare_shares_the_oracle_and_ranks_it_on_top() -> None:
    policies = {name: gym_run.get_policy(name) for name in (ORACLE, EVERYONE, RULE)}
    result = compare(TINY, policies, 2, world_factory=stub_factory)
    assert len(result.cards) == 6
    by = {(c.policy, c.seed): c for c in result.cards}
    for seed in (0, 1):
        oracle = by[(ORACLE, seed)]
        assert oracle.oracle_share == pytest.approx(1.0)
        assert by[(EVERYONE, seed)].oracle_value == oracle.true_value
        assert oracle.true_value >= by[(EVERYONE, seed)].true_value
    assert set(result.table()["policy"]) == {ORACLE, EVERYONE, RULE}
    assert len(result.summary()) == 3


def test_cli_prints_a_scoreboard(capsys: pytest.CaptureFixture[str]) -> None:
    code = gym_run.main(
        ["--scenario", "smoke", "--policy", "nothing", "--seeds", "1", "--customers", "60", "--days", "5"],
        world_factory=stub_factory,
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "more profit than the holdout" in out
    assert "Against the pass bar" in out
    assert gym_run.main(["--list"]) == 0


def test_registry_refuses_duplicates() -> None:
    with pytest.raises(ValueError):
        gym_run.register_policy(RULE, gym_run.get_policy(RULE))
    with pytest.raises(KeyError):
        gym_run.get_policy("missing")


def test_oracle_batch_plan_matches_one_by_one() -> None:
    class Batch:
        """A truth with the batch fast path; per-customer calls go through the same numbers."""

        def index(self, customer_id: str) -> int:
            return int(customer_id[1:])

        def net(self, idx: np.ndarray, action: Action, now: datetime) -> np.ndarray:
            base = 0.5 if action.channel is Channel.WHATSAPP else 0.2
            hour = 0.3 if action.send_at.hour == 18 else 0.0
            offer = {"none": 0.0, "freeship": 0.15, "pct10": 0.1}[action.offer_id]
            return (idx % 7 - 3) * 0.1 + base + hour + offer * (idx % 2) - 0.25

        def persona(self, customer_id: str) -> str:
            return "new"

        def is_sleeping_dog(self, customer_id: str) -> bool:
            return False

        def expected_net(self, customer_id: str, action: Action, now: datetime) -> float:
            return float(self.net(np.array([self.index(customer_id)]), action, now)[0])

        def best_action(self, customer_id: str, candidates: Sequence[Action], now: datetime) -> Action | None:
            best, value = None, 0.0
            for a in candidates:
                v = self.expected_net(customer_id, a, now)
                if v > value:
                    best, value = a, v
            return best

    class Single:
        """The same truth through the per-customer protocol only."""

        def __init__(self) -> None:
            self.inner = Batch()
            self.persona, self.is_sleeping_dog = self.inner.persona, self.inner.is_sleeping_dog
            self.expected_net, self.best_action = self.inner.expected_net, self.inner.best_action

    email_only = {Channel.EMAIL: ChannelStats(address_known=True, consent=True)}
    states = [
        state(
            customer_id=cid,
            channels=email_only if i % 4 == 0 else state().channels,
            contacts_7d=i % 5 // 4 * 2,
        )
        for i, cid in enumerate(f"c{i:05d}" for i in range(40))
    ]
    batch, single = OraclePolicy(Batch()), OraclePolicy(Single())
    batch.prepare_day(states, ctx())
    single.prepare_day(states, ctx())
    assert batch._plan and not single._plan
    acted = 0
    for s in states:
        a, b = batch.decide(s, ctx()), single.decide(s, ctx())
        assert a.action == b.action and a.expected_net == pytest.approx(b.expected_net)
        acted += a.action is not None
    assert 0 < acted < len(states)


def test_breaker_counts_a_real_stop_not_a_cap_pause() -> None:
    from autonomy.gym.score import _breaker_batches

    # Send-to-all under a 2-a-week cap: two busy days, five empty, again. Never a stop.
    cap_pattern = [290, 289, 0, 0, 0, 0, 0, 278, 272, 0, 0, 0, 0, 0, 268, 260, 0, 0, 0, 0, 250]
    assert _breaker_batches(cap_pattern, 0) is None
    # A breaker that trips after two batches and stays off.
    assert _breaker_batches([300, 120, 0, 0, 0, 0], 0) == 2
    # It went quiet once, sent again, then stopped for good: judged by the last send.
    assert _breaker_batches([300, 0, 50, 0, 0, 0], 0) == 3
    # Harm from day 2: batches count from there; a policy that never sends stops at once.
    assert _breaker_batches([9, 9, 9, 0, 0], 2) == 1
    assert _breaker_batches([0, 0, 0], 0) == 0
    assert _breaker_batches([5, 5], 5) is None


def test_oracle_batch_hour_probes_only_reachable_channels() -> None:
    class Batch:
        """WhatsApp is worth most at 18:00, email at 11:00; WhatsApp is worth more overall."""

        def index(self, customer_id: str) -> int:
            return int(customer_id[1:])

        def net(self, idx: np.ndarray, action: Action, now: datetime) -> np.ndarray:
            if action.channel is Channel.WHATSAPP:
                value = 2.0 if action.send_at.hour == 18 else 1.5
            else:
                value = 1.0 if action.send_at.hour == 11 else 0.5
            return np.full(len(idx), value)

        def persona(self, customer_id: str) -> str:
            return "new"

        def is_sleeping_dog(self, customer_id: str) -> bool:
            return False

        def expected_net(self, customer_id: str, action: Action, now: datetime) -> float:
            return float(self.net(np.array([self.index(customer_id)]), action, now)[0])

        def best_action(self, customer_id: str, candidates: Sequence[Action], now: datetime) -> Action | None:
            return max(candidates, key=lambda a: self.expected_net(customer_id, a, now), default=None)

    email_only = state(channels={Channel.EMAIL: ChannelStats(address_known=True, consent=True)})
    batch = OraclePolicy(Batch())
    batch.prepare_day([email_only], ctx())
    d = batch.decide(email_only, ctx())
    assert d.action is not None and d.action.channel is Channel.EMAIL
    assert d.action.send_at.hour == 11  # not WhatsApp's best hour


def test_cli_rejects_an_unknown_policy() -> None:
    with pytest.raises(SystemExit) as exc:
        gym_run.main(["--scenario", "smoke", "--policy", "missing"])
    assert exc.value.code == 2
