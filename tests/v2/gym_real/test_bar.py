"""The honest bar: the staged verdict on synthetic scorecards, the best fixed rule (which must never
touch the truth), the segment oracle (which may read the truth only through segment averages), lost
value in the realised net, and the CLI's JSON report. All on stubs, so the file runs in seconds."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import numpy as np
import pytest

from autonomy.contracts import (
    Action,
    Audience,
    AudienceFilter,
    Brief,
    Channel,
    ChannelStats,
    ContentItem,
    CustomerState,
    DecisionContext,
    DecisionKind,
    Estimate,
    Event,
    EventType,
)
from autonomy.fakes import FakeEnvironment
from autonomy.gym import run as gym_run
from autonomy.gym.api import PERSONAS, GroundTruth, ScenarioSpec
from autonomy.gym.baselines import (
    BestFixedRule,
    SegmentOracle,
    frequency_cuts,
    observable_segment,
    purchase_frequency,
)
from autonomy.gym.scenarios import default_brief, default_content, get_scenario
from autonomy.gym.score import (
    BEST_FIXED,
    EVERYONE,
    ORACLE,
    RULE,
    SEGMENT_ORACLE,
    ScoreCard,
    SimWorld,
    StagedReport,
    _Ledger,
    best_fixed_factory,
    compare,
    segment_oracle_factory,
    sleeping_dog_signal,
    staged_criteria,
)

START = datetime(2026, 3, 2, tzinfo=UTC)  # a Monday
NOW = START + timedelta(hours=10)


# --- synthetic scorecards ----------------------------------------------------------------------


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
        "segment_oracle_value": 800.0,
        "best_fixed_rule_value": 550.0,
        "actions": 10,
        "contacts": 10,
        "contacts_per_customer_week": 0.1,
        "unsubscribe_rate": 0.0,
        "sleeping_dogs": 100,
        "sleeping_dogs_contacted": 30,
        "sleeping_dog_signal": 0.5,
        "violations": 0,
    }
    fields.update(kw)
    return ScoreCard.model_validate(fields)


REFS = {EVERYONE: 300.0, RULE: 100.0, BEST_FIXED: 550.0, SEGMENT_ORACLE: 800.0, ORACLE: 1000.0}


def world_cards(n: int = 10, agent: float = 700.0, **agent_kw: object) -> list[ScoreCard]:
    out: list[ScoreCard] = []
    for s in range(n):
        out.append(card("agent", s, agent, **agent_kw))
        for name, value in REFS.items():
            dogs = 95 if name == EVERYONE else 50
            out.append(card(name, s, value, sleeping_dogs_contacted=dogs))
    return out


def stages(report: StagedReport) -> dict[str, str]:
    return {st.name: st.status for st in report.stages}


def checks(report: StagedReport) -> dict[str, tuple[bool, bool]]:
    found = {c.name: (c.evaluated, c.passed) for st in report.stages for c in st.checks}
    found.update({c.name: (c.evaluated, c.passed) for c in report.stretch})
    return found


def test_a_good_agent_reaches_stage_3_without_the_stretch() -> None:
    report = staged_criteria(world_cards())
    assert report.policy == "agent"
    assert stages(report) == {"Safe": "PASS", "Useful": "PASS", "Smart": "PASS"}
    assert report.stage_reached == 3
    c = checks(report)
    assert c["breaker"] == (False, False)  # no planted-harm scenario: skipped, not failed
    assert c["null_loss"] == (False, False)
    assert c["oracle_capture"] == (True, False)  # 70% of the full oracle: reported, not a gate
    assert c["plan_sleeping_dogs"] == (True, False)
    seg = next(x for x in report.stages[2].checks if x.name == "segment_oracle_capture")
    assert "88%" in seg.detail and "best fixed rule 69%" in seg.detail  # context: what needs no learning
    verdict = report.verdict()
    assert verdict["stage_reached"] == 3 and verdict["stage_name"] == "Smart"
    json.dumps(verdict)  # JSON-ready
    lines = report.lines()
    assert lines[0].startswith("Stage 1 Safe") and "Stretch" in "\n".join(lines)
    assert lines[-1].startswith("Verdict: agent reaches Stage 3 (Smart)")


def test_every_stage_is_reported_even_after_a_failure() -> None:
    cards = world_cards()
    cards[0] = card("agent", 0, 700.0, violations=1, violation_examples=("Sent at 23:00.",))
    report = staged_criteria(cards)
    assert stages(report) == {"Safe": "FAIL", "Useful": "PASS", "Smart": "PASS"}
    assert report.stage_reached == 0
    assert report.lines()[-1].startswith("Verdict: agent does not reach Stage 1")


def test_useful_needs_the_best_fixed_rule_beaten_in_90_percent_of_seeds() -> None:
    cards = world_cards()
    cards[0] = card("agent", 0, 500.0)  # loses to the best fixed rule on 1 of 10 seeds
    assert checks(staged_criteria(cards))["beats_references"] == (True, True)
    cards[6] = card("agent", 1, 500.0)  # 2 of 10
    report = staged_criteria(cards)
    assert checks(report)["beats_references"] == (True, False)
    assert report.stage_reached == 1
    beat = next(c for c in report.stages[1].checks if c.name == "beats_references")
    assert "best fixed rule 8" in beat.detail and "send-to-all 10" in beat.detail


def test_useful_is_not_judged_without_the_best_fixed_rule() -> None:
    cards = [c for c in world_cards() if c.policy != BEST_FIXED]
    report = staged_criteria(cards)
    assert checks(report)["beats_references"] == (False, False)
    # Smart passes on these cards, but a policy whose usefulness was never measured is not "smart".
    assert stages(report) == {"Safe": "PASS", "Useful": "NOT JUDGED", "Smart": "PASS"}
    assert report.stage_reached == 1
    assert report.verdict()["stage_name"] == "Safe"


def test_smart_needs_80_percent_of_the_segment_oracle() -> None:
    report = staged_criteria(world_cards(agent=630.0))  # 79% of the segment oracle, beats every baseline
    assert stages(report)["Useful"] == "PASS"
    assert checks(report)["segment_oracle_capture"] == (True, False)
    assert report.stage_reached == 2
    # The segment oracle's own card counts when the policy's card does not carry its value.
    cards = world_cards(agent=660.0, segment_oracle_value=None)
    assert checks(staged_criteria(cards))["segment_oracle_capture"] == (True, True)


def test_sleeping_dogs_are_judged_only_where_they_are_observable() -> None:
    hidden = [c.model_copy(update={"sleeping_dog_signal": 0.0}) for c in world_cards()]
    report = staged_criteria(hidden)
    dog = next(c for c in report.stages[2].checks if c.name == "sleeping_dogs")
    assert not dog.evaluated and "no observable signal" in dog.detail
    assert report.stage_reached == 3  # judged on the segment oracle alone
    close = world_cards(sleeping_dogs_contacted=90)  # 90% against send-to-all's 95%: not clear
    assert checks(staged_criteria(close))["sleeping_dogs"] == (True, False)
    clear = world_cards(sleeping_dogs_contacted=85)
    assert checks(staged_criteria(clear))["sleeping_dogs"] == (True, True)


def null_cards(agent: float, days: int = 90) -> list[ScoreCard]:
    kw: dict[str, object] = {
        "scenario": "null_effect",
        "oracle_value": 0.0,
        "segment_oracle_value": 0.0,
        "days": days,
        "sleeping_dogs": 0,
        "sleeping_dogs_contacted": 0,
    }
    out = []
    for s in range(2):
        out += [
            card("agent", s, agent, **kw),
            card(EVERYONE, s, -6000.0, **kw),
            card(BEST_FIXED, s, -300.0, **kw),
        ]
    return out


def test_null_effect_allows_a_small_loss_scaled_to_customers_and_days() -> None:
    report = staged_criteria(null_cards(-150.0))  # allowed $0.25 x 800 customers = $200 a seed
    assert checks(report)["null_loss"] == (True, True)
    assert checks(report)["beats_references"] == (False, False)  # no money to make: not judged
    assert stages(report)["Smart"] == "NOT JUDGED"
    assert report.stage_reached == 2
    assert checks(staged_criteria(null_cards(-250.0)))["null_loss"] == (True, False)
    assert checks(staged_criteria(null_cards(-150.0, days=45)))["null_loss"] == (True, False)  # $100 allowed


def harm_cards(agent_profit: float, breaker: int | None, lost: float = 3000.0) -> list[ScoreCard]:
    kw: dict[str, object] = {"scenario": "complaint_storm", "planted_harm": True, "sleeping_dogs": 0}
    out = []
    for s in range(2):
        out.append(
            card(
                "agent",
                s,
                50.0,
                incremental_profit=Estimate(
                    point=agent_profit, low=agent_profit - 300, high=agent_profit + 300
                ),
                interval_covers_truth=True,
                breaker_batches=breaker,
                lost_value=40.0 if lost else 0.0,
                **kw,
            )
        )
        out.append(
            card(
                EVERYONE,
                s,
                900.0,
                incremental_profit=Estimate(point=-2000.0, low=-2500.0, high=-1500.0),
                lost_value=lost,
                **kw,
            )
        )
    return out


def test_planted_harm_is_judged_on_safety_and_losing_less() -> None:
    report = staged_criteria(harm_cards(-100.0, breaker=1))
    c = checks(report)
    assert c["breaker"] == (True, True) and c["harm_loses_less"] == (True, True)
    assert c["beats_references"] == (False, False)  # stopping cannot also capture value
    assert stages(report) == {"Safe": "PASS", "Useful": "PASS", "Smart": "NOT JUDGED"}
    assert report.stage_reached == 2
    assert "not judged on these cards: Smart" in report.lines()[-1]

    assert stages(staged_criteria(harm_cards(-100.0, breaker=None)))["Safe"] == "FAIL"
    assert checks(staged_criteria(harm_cards(-2500.0, breaker=1)))["harm_loses_less"] == (True, False)
    silent = staged_criteria(harm_cards(-100.0, breaker=1, lost=0.0))
    harm = next(x for x in silent.stages[1].checks if x.name == "harm_loses_less")
    assert "reported no lost value" in harm.detail


def test_sleeping_dog_signal_is_read_from_the_spec() -> None:
    assert sleeping_dog_signal(ScenarioSpec(name="x")) == 0.5
    assert sleeping_dog_signal(ScenarioSpec(name="x", sleeping_dog_signal=0.0)) == 0.0
    assert sleeping_dog_signal(get_scenario("sleeping_dogs_hidden")) == 0.0


# --- states and contexts -----------------------------------------------------------------------


def state(cid: str = "c00001", whatsapp: bool = True, email: bool = True, **kw: object) -> CustomerState:
    channels: dict[Channel, ChannelStats] = {}
    if email:
        channels[Channel.EMAIL] = ChannelStats(address_known=True, consent=True)
    if whatsapp:
        channels[Channel.WHATSAPP] = ChannelStats(address_known=True, consent=True)
    fields: dict[str, object] = {
        "customer_id": cid,
        "as_of": NOW,
        "channels": channels,
        "first_seen_at": START - timedelta(days=400),
        "purchases": 3,
        "last_purchase_at": START - timedelta(days=20),
    }
    fields.update(kw)
    return CustomerState.model_validate(fields)


def ctx(now: datetime = NOW, briefs: Sequence[Brief] | None = None) -> DecisionContext:
    return DecisionContext(
        now=now, briefs=tuple(briefs or (default_brief(),)), content=tuple(default_content())
    )


# --- the best fixed rule never reads the truth -------------------------------------------------


class NoTruthWorld:
    """A world whose truth cannot be read: any access fails the test."""

    reads = 0

    @property
    def truth(self) -> GroundTruth:
        NoTruthWorld.reads += 1
        raise AssertionError("BestFixedRule must not touch the truth")


def test_best_fixed_rule_never_touches_the_truth() -> None:
    policy = best_fixed_factory(cast(SimWorld, NoTruthWorld()))
    assert NoTruthWorld.reads == 0
    assert isinstance(policy, BestFixedRule)
    assert not any(isinstance(v, GroundTruth) for v in vars(policy).values())
    for i, (wa, em) in enumerate([(True, True), (False, True), (True, False), (False, False)]):
        policy.decide(state(f"c{i:05d}", whatsapp=wa, email=em), ctx())
    assert NoTruthWorld.reads == 0


def test_best_fixed_rule_whatsapp_else_email_no_offer_at_14() -> None:
    rule = BestFixedRule()
    d = rule.decide(state("c1"), ctx())
    assert d.kind is DecisionKind.ACT and d.action is not None
    assert d.action.channel is Channel.WHATSAPP and d.action.offer_id == "none"
    assert d.action.content_id == "gym_whatsapp_none_reminder"
    assert d.action.send_at == START + timedelta(hours=14)
    e = rule.decide(state("c2", whatsapp=False), ctx())
    assert e.action is not None and e.action.channel is Channel.EMAIL and e.action.offer_id == "none"
    ny = rule.decide(state("c3", timezone="America/New_York"), ctx())
    assert ny.action is not None and ny.action.send_at == START + timedelta(hours=19)  # 14:00 EST
    assert rule.decide(state("c4", whatsapp=False, email=False), ctx()).kind is DecisionKind.NOTHING


def test_best_fixed_rule_keeps_the_cap_the_gap_and_pending_sends() -> None:
    rule = BestFixedRule()
    assert rule.decide(state("c1"), ctx()).kind is DecisionKind.ACT
    assert rule.decide(state("c1"), ctx(NOW + timedelta(hours=1))).kind is DecisionKind.NOTHING  # pending
    sent = START + timedelta(hours=14)
    tomorrow = NOW + timedelta(days=1)
    after = state("c1", contacts_7d=1, last_contact_at=sent)
    assert rule.decide(after, ctx(tomorrow)).kind is DecisionKind.ACT
    assert rule.decide(state("c2", contacts_7d=2), ctx()).kind is DecisionKind.NOTHING  # cap
    recent = state("c3", last_contact_at=NOW - timedelta(hours=3))
    assert rule.decide(recent, ctx()).kind is DecisionKind.NOTHING  # an unsubscribe may still be on its way
    assert rule.decide(state("c4", open_complaint=True), ctx()).kind is DecisionKind.NOTHING


def test_best_fixed_rule_hour_is_configurable_and_avoids_quiet_hours() -> None:
    late = BestFixedRule(22).decide(state(), ctx())  # quiet from 21:00: first allowed hour is 09:00
    assert late.action is not None and late.action.send_at == START + timedelta(days=1, hours=9)
    early = BestFixedRule(18).decide(state(), ctx())
    assert early.action is not None and early.action.send_at == START + timedelta(hours=18)
    with pytest.raises(ValueError):
        BestFixedRule(24)


def test_best_fixed_rule_needs_a_brief_that_allows_no_offer() -> None:
    brief = default_brief()
    offers_only = brief.model_copy(
        update={
            "envelope": brief.envelope.model_copy(
                update={"offers": tuple(o for o in brief.envelope.offers if o.offer_id != "none")}
            )
        }
    )
    rule = BestFixedRule()
    assert rule.decide(state("c1"), ctx(briefs=[offers_only])).kind is DecisionKind.NOTHING
    d = rule.decide(state("c2"), ctx(briefs=[offers_only, brief]))
    assert d.action is not None and d.action.offer_id == "none"


def test_a_reused_best_fixed_rule_forgets_the_last_runs_sends() -> None:
    rule = BestFixedRule()
    later = NOW + timedelta(days=89)
    assert rule.decide(state("c1"), ctx(later)).kind is DecisionKind.ACT  # scheduled for day 89
    assert rule.decide(state("c1"), ctx(NOW)).kind is DecisionKind.ACT  # a new run starts on day 0


def test_best_fixed_rule_contacts_only_the_audience() -> None:
    brief = default_brief()
    buyers = brief.model_copy(
        update={
            "audience": Audience(
                description="Buyers", filters=(AudienceFilter(field="purchases", op="gte", value=1),)
            )
        }
    )
    rule = BestFixedRule()
    assert rule.decide(state("c1", purchases=0, last_purchase_at=None), ctx(briefs=[buyers])).action is None
    assert rule.decide(state("c2"), ctx(briefs=[buyers])).action is not None


# --- the segment oracle reads the truth only through segment averages ---------------------------


class SpyTruth:
    """Each customer's own value per channel, plus a bonus at 15:00 and a penalty for offers. Records
    every call; persona, sleeping-dog and best-action calls fail the test."""

    def __init__(self, own: dict[str, dict[Channel, float]], batch: bool = True) -> None:
        self.own = own
        self.ids = sorted(own)
        self.calls: list[str] = []
        self.batch = batch

    def _value(self, cid: str, action: Action) -> float:
        hour_bonus = 0.5 if action.send_at.hour == 15 else 0.0
        offer = -0.3 if action.offer_id != "none" else 0.0
        return self.own[cid].get(action.channel, -0.01) + hour_bonus + offer

    def index(self, customer_id: str) -> int:
        self.calls.append("index")
        return self.ids.index(customer_id)

    def net(self, idx: np.ndarray, action: Action, now: datetime) -> np.ndarray:
        self.calls.append("net")
        return np.array([self._value(self.ids[int(i)], action) for i in idx], dtype=np.float64)

    def persona(self, customer_id: str) -> str:
        raise AssertionError("the segment oracle must not read personas")

    def is_sleeping_dog(self, customer_id: str) -> bool:
        raise AssertionError("the segment oracle must not read sleeping-dog flags")

    def expected_net(self, customer_id: str, action: Action, now: datetime) -> float:
        self.calls.append("expected_net")
        return self._value(customer_id, action)

    def best_action(self, customer_id: str, candidates: Sequence[Action], now: datetime) -> Action | None:
        raise AssertionError("the segment oracle must not ask for a customer's best action")


class SingleTruth:
    """The same truth without the batch methods: the oracle must fall back to `expected_net`."""

    def __init__(self, spy: SpyTruth) -> None:
        self.spy = spy

    def persona(self, customer_id: str) -> str:
        return self.spy.persona(customer_id)

    def is_sleeping_dog(self, customer_id: str) -> bool:
        return self.spy.is_sleeping_dog(customer_id)

    def expected_net(self, customer_id: str, action: Action, now: datetime) -> float:
        return self.spy.expected_net(customer_id, action, now)

    def best_action(self, customer_id: str, candidates: Sequence[Action], now: datetime) -> Action | None:
        return self.spy.best_action(customer_id, candidates, now)


def segment_world() -> tuple[dict[str, dict[Channel, float]], list[CustomerState]]:
    new: dict[str, Any] = {
        "first_seen_at": START - timedelta(days=10),
        "purchases": 0,
        "last_purchase_at": None,
    }
    lapsed: dict[str, Any] = {"purchases": 0, "last_purchase_at": None}
    own: dict[str, dict[Channel, float]] = {}
    states: list[CustomerState] = []
    for i in range(3):  # new customers who answer WhatsApp
        own[f"n{i}"] = {Channel.WHATSAPP: 2.0, Channel.EMAIL: 0.0}
        states.append(state(f"n{i}", **new))
    own["n_email_lover"] = {Channel.WHATSAPP: 0.0, Channel.EMAIL: 3.0}
    states.append(state("n_email_lover", **new))
    own["n_email_only"] = {Channel.EMAIL: 1.0}
    states.append(state("n_email_only", whatsapp=False, **new))
    for i in range(3):  # lapsed customers nothing helps
        own[f"l{i}"] = {Channel.WHATSAPP: -1.0, Channel.EMAIL: -1.0}
        states.append(state(f"l{i}", **lapsed))
    return own, states


@pytest.mark.parametrize("batch", [True, False])
def test_segment_oracle_gives_each_observable_segment_one_action(batch: bool) -> None:
    own, states = segment_world()
    spy = SpyTruth(own)
    truth: GroundTruth = spy if batch else SingleTruth(spy)
    oracle = SegmentOracle(truth)
    c = ctx()
    oracle.prepare_day(states, c)
    decided = {s.customer_id: oracle.decide(s, c) for s in states}

    picks = {
        cid: (d.action.channel, d.action.offer_id, d.action.content_id, d.action.send_at)
        for cid, d in decided.items()
        if d.action is not None
    }
    # Same segment and reach, same action: the email lover gets WhatsApp, as their segment does.
    group = [picks[cid] for cid in ("n0", "n1", "n2", "n_email_lover")]
    assert len(set(group)) == 1
    channel, offer, _content, send_at = group[0]
    assert channel is Channel.WHATSAPP and offer == "none" and send_at == START + timedelta(hours=15)
    assert picks["n_email_only"][0] is Channel.EMAIL
    assert all(decided[f"l{i}"].kind is DecisionKind.NOTHING for i in range(3))  # mean below zero
    assert decided["n0"].expected_net == pytest.approx((2.0 * 3 + 0.0) / 4 + 0.5)  # the segment mean
    assert set(spy.calls) <= {"index", "net", "expected_net"}
    assert ("net" in spy.calls) is batch
    assert oracle.choices[("lapsed", ("email", "whatsapp"))] is None


def test_segment_oracle_without_a_plan_does_nothing() -> None:
    own, states = segment_world()
    oracle = SegmentOracle(SpyTruth(own))
    assert oracle.decide(states[0], ctx()).kind is DecisionKind.NOTHING
    oracle.prepare_day(states, ctx())
    assert oracle.decide(states[0], ctx()).kind is DecisionKind.ACT
    assert oracle.decide(states[0], ctx(NOW + timedelta(days=1))).kind is DecisionKind.NOTHING  # stale plan


def test_observable_segments() -> None:
    assert observable_segment(state(first_seen_at=NOW - timedelta(days=30)), NOW) == "new"
    assert observable_segment(state(last_purchase_at=NOW - timedelta(days=100)), NOW) == "lapsed"
    assert observable_segment(state(purchases=0, last_purchase_at=None), NOW) == "lapsed"
    assert observable_segment(state(purchases=1), NOW) == "light"
    assert observable_segment(state(purchases=4), NOW) == "medium"
    assert observable_segment(state(purchases=10), NOW) == "heavy"


def test_frequency_counts_from_the_start_of_the_data_like_the_agent() -> None:
    """A two-year customer with 180 days of imported orders is as heavy as their orders say, not
    diluted by the tenure before the data begins (the agent's segmenter does the same)."""
    old = state(first_seen_at=NOW - timedelta(days=720), purchases=3)
    data_start = NOW - timedelta(days=180)
    assert purchase_frequency(old, NOW) == pytest.approx(3 * 30 / 720)
    assert purchase_frequency(old, NOW, data_start) == pytest.approx(3 * 30 / 180)
    assert observable_segment(old, NOW) == "light"
    assert observable_segment(old, NOW, data_start=data_start) == "heavy"
    young = state(first_seen_at=NOW - timedelta(days=150), purchases=1)  # joined after the data began
    assert purchase_frequency(young, NOW, data_start) == pytest.approx(30 / 150)
    states = [
        state(f"c{i:03d}", purchases=1 + i % 9, first_seen_at=NOW - timedelta(days=200 + 10 * i))
        for i in range(60)
    ]
    assert frequency_cuts(states, NOW, data_start) != frequency_cuts(states, NOW)


def test_segment_oracle_factory_counts_from_the_scenario_history() -> None:
    world = StubWorld(TINY)
    oracle = segment_oracle_factory(cast(SimWorld, world))
    assert isinstance(oracle, SegmentOracle) and oracle.history_days == TINY.history_days


# --- lost value in the realised net ------------------------------------------------------------


def test_ledger_takes_lost_value_off_the_realised_net() -> None:
    ledger = _Ledger(start=START, channel_cost={Channel.EMAIL: 0.001}, offer_cost={}, horizon={})

    def ev(kind: EventType, attrs: dict[str, float | str]) -> Event:
        return Event(
            event_id=f"e{kind.value}{len(attrs)}",
            customer_id="c1",
            type=kind,
            ts=NOW,
            source="test",
            channel=Channel.EMAIL,
            decision_id="d1",
            attrs=attrs,
        )

    ledger.observe(
        [
            ev(EventType.UNSUBSCRIBE, {"lost_value": 12.5}),
            ev(EventType.COMPLAINT, {"lost_value": 5.0, "x": "y"}),
            ev(EventType.UNSUBSCRIBE, {}),  # a world that reports nothing charges nothing
        ]
    )
    assert ledger.lost["c1"] == pytest.approx(17.5)
    assert ledger.net["c1"] == pytest.approx(-17.5)
    assert "c1" in ledger.unsubscribed


# --- full runs on a stub world -----------------------------------------------------------------


class StubTruth:
    def __init__(self, env: FakeEnvironment) -> None:
        self.env = env

    def _i(self, customer_id: str) -> int:
        return int(customer_id[1:])

    def persona(self, customer_id: str) -> str:
        return PERSONAS[self._i(customer_id) % len(PERSONAS)]

    def is_sleeping_dog(self, customer_id: str) -> bool:
        return self._i(customer_id) % 10 == 0

    def expected_net(self, customer_id: str, action: Action, now: datetime) -> float:
        lift = float(self.env._uplift[action.channel][self._i(customer_id)])
        return lift * 5 * 24.0 - 0.001 - (0.6 if action.offer_id != "none" else 0.0)

    def best_action(self, customer_id: str, candidates: Sequence[Action], now: datetime) -> Action | None:
        if not candidates:
            return None
        best = max(candidates, key=lambda a: self.expected_net(customer_id, a, now))
        return best if self.expected_net(customer_id, best, now) > 0 else None


class StubWorld(FakeEnvironment):
    def __init__(self, spec: ScenarioSpec) -> None:
        super().__init__(n_customers=spec.n_customers, start=START, history_days=spec.history_days)
        self.spec = spec
        self.truth: GroundTruth = StubTruth(self)

    def violations(self) -> list[str]:
        return []


def stub_factory(spec: ScenarioSpec, briefs: Sequence[Brief], content: Sequence[ContentItem]) -> StubWorld:
    del briefs, content
    return StubWorld(spec)


TINY = ScenarioSpec(name="tiny", n_customers=60, history_days=60, days=6, effect_days=5)


def test_compare_puts_the_references_on_every_card() -> None:
    names = ["nothing", EVERYONE, RULE, BEST_FIXED, SEGMENT_ORACLE, ORACLE]
    policies = {n: gym_run.get_policy(n) for n in names}
    result = compare(TINY, policies, 1, world_factory=stub_factory)
    by = {c.policy: c for c in result.cards}
    assert list(by) == names
    seg, fixed = by[SEGMENT_ORACLE].true_value, by[BEST_FIXED].true_value
    assert seg > 0 and fixed != 0
    for c in result.cards:
        assert c.segment_oracle_value == seg and c.best_fixed_rule_value == fixed
        assert c.value_vs_best_fixed_rule == pytest.approx(c.true_value - fixed)
    assert by[SEGMENT_ORACLE].share_of_segment_oracle == pytest.approx(1.0)
    assert by["nothing"].true_value == 0.0
    report = staged_criteria(result.cards, policy="nothing")
    assert [st.number for st in report.stages] == [1, 2, 3]


def test_cli_writes_a_json_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "out" / "report.json"
    argv = ["--scenario", "smoke", "--policy", "nothing", "--seeds", "1", "--customers", "40", "--days", "4"]
    code = gym_run.main([*argv, "--fixed-hour", "16", "--report-json", str(path)], world_factory=stub_factory)
    out = capsys.readouterr().out
    assert code == 0
    assert "Against the pass bar" in out and "Stage 1 Safe" in out and "Verdict:" in out
    data = json.loads(path.read_text())
    assert {c["policy"] for c in data["cards"]} == {
        "nothing",
        EVERYONE,
        RULE,
        BEST_FIXED,
        SEGMENT_ORACLE,
        ORACLE,
    }
    assert isinstance(data["verdict"]["stage_reached"], int)
    assert [s["name"] for s in data["staged"]["stages"]] == ["Safe", "Useful", "Smart"]
    assert "passed" in data["plan_bar"]
