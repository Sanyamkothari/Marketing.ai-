"""The agent: the default is unchanged, and the switch turns on look-alike tiredness and 3-week plans."""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from autonomy.contracts import (
    Action,
    Bid,
    Channel,
    ChannelStats,
    CustomerState,
    DecisionContext,
    DecisionKind,
    Event,
    EventType,
    Group,
)
from autonomy.decide import rules
from autonomy.decide.frequency import FrequencyConfig, FrequencyPlanner, LookalikeHazard, ToleranceConfig
from autonomy.decide.models import HazardModel
from autonomy.decide.policy import AutonomousPolicy
from autonomy.fakes.samples import sample_brief, sample_content
from autonomy.gym import default_brief, default_content
from autonomy.gym.run import POLICIES, agent_factory, agent_frequency_factory
from autonomy.runtime.agent import (
    AgentPolicy,
    ContactPrice,
    LookalikeClickModel,
    build_agent_policy,
)

NOW = datetime(2026, 10, 20, 7, 0, tzinfo=UTC)
GOAL = "goal_winback"
REACHABLE = ChannelStats(address_known=True, consent=True, delivers=10, clicks=1)


def _agent(policy: object) -> AgentPolicy:
    assert isinstance(policy, AgentPolicy)
    return policy


def customer(i: int, group: str = "calm", **kw: object) -> CustomerState:
    base: dict[str, object] = {
        "customer_id": f"{group}{i}",
        "as_of": NOW,
        "group": Group.AGENT,
        "channels": {Channel.EMAIL: REACHABLE},
        "first_seen_at": NOW - timedelta(days=400),
        "purchases": 2,
        "revenue": 80.0,
        "last_purchase_at": NOW - timedelta(days=90),
    }
    if group == "tired":
        base.update(segment="lapsed", last_purchase_at=NOW - timedelta(days=300), contacts_30d=6)
    base.update(kw)
    return CustomerState.model_validate(base)


def ctx(now: datetime = NOW, seed: int = 0) -> DecisionContext:
    return DecisionContext(now=now, briefs=(default_brief(),), content=tuple(default_content()), seed=seed)


# --- off by default -------------------------------------------------------------------------------


def test_the_default_agent_has_the_plain_hazard_and_no_plan() -> None:
    policy = build_agent_policy([default_brief()], default_content())
    assert type(policy.contact_price.hazard) is HazardModel
    assert type(policy.contact_price.complaints) is HazardModel
    assert policy.planner is None and policy.frequency is None
    assert policy.policy.plan_fn is None


@pytest.mark.parametrize("flag", [False, None])
def test_a_falsy_switch_is_the_default(flag: object) -> None:
    policy = build_agent_policy([default_brief()], default_content(), frequency=flag)  # type: ignore[arg-type]
    assert type(policy.contact_price.hazard) is HazardModel and policy.planner is None


def test_default_decisions_are_identical_with_the_switch_off() -> None:
    plain = build_agent_policy([default_brief()], default_content())
    off = build_agent_policy([default_brief()], default_content(), frequency=False)
    for i in range(40):
        s = customer(i, contacts_7d=i % 2)
        a, b = plain.decide(s, ctx(seed=i)), off.decide(s, ctx(seed=i))
        assert a.model_dump() == b.model_dump()


def test_tolerance_alone_before_any_data_gives_the_default_decisions_exactly() -> None:
    plain = build_agent_policy([default_brief()], default_content())
    tolerant = build_agent_policy([default_brief()], default_content(), frequency=FrequencyConfig(plans=None))
    assert isinstance(tolerant.contact_price.hazard, LookalikeHazard) and tolerant.planner is None
    for i in range(40):
        s = customer(i, contacts_7d=i % 2)
        assert plain.decide(s, ctx(seed=i)).model_dump() == tolerant.decide(s, ctx(seed=i)).model_dump()


def test_a_customer_with_a_free_week_is_decided_as_before_with_plans_on() -> None:
    plain = build_agent_policy([default_brief()], default_content())
    planned = build_agent_policy([default_brief()], default_content(), frequency=True)
    for i in range(40):
        s = customer(i)
        a, b = plain.decide(s, ctx(seed=i)), planned.decide(s, ctx(seed=i))
        assert a.model_dump() == b.model_dump()


# --- the switch -----------------------------------------------------------------------------------


def test_the_switch_builds_look_alike_tiredness_and_the_plan_gate() -> None:
    policy = build_agent_policy([default_brief()], default_content(), frequency=True)
    assert isinstance(policy.contact_price.hazard, LookalikeHazard)
    assert isinstance(policy.contact_price.complaints, LookalikeHazard)
    assert policy.contact_price.complaints.prior[0] == pytest.approx(0.0005)  # its own base rate
    assert isinstance(policy.planner, FrequencyPlanner)
    assert policy.policy.plan_fn == policy.planner.gate


def test_each_part_can_be_switched_off_apart() -> None:
    no_plan = build_agent_policy([default_brief()], default_content(), frequency=FrequencyConfig(plans=None))
    assert no_plan.planner is None and no_plan.policy.plan_fn is None
    no_tolerance = build_agent_policy(
        [default_brief()], default_content(), frequency=FrequencyConfig(tolerance=None)
    )
    assert type(no_tolerance.contact_price.hazard) is HazardModel and no_tolerance.planner is not None


def test_a_config_changes_the_numbers() -> None:
    cfg = FrequencyConfig(tolerance=ToleranceConfig(min_events=7.0))
    policy = build_agent_policy([default_brief()], default_content(), frequency=cfg)
    assert isinstance(policy.contact_price.hazard, LookalikeHazard)
    assert policy.contact_price.hazard.config.min_events == 7.0


def test_the_gym_lists_agent_frequency_beside_the_others() -> None:
    assert POLICIES["agent_frequency"] is agent_frequency_factory
    assert "agent" in POLICIES and "agent_lookalike" in POLICIES
    policy = _agent(agent_frequency_factory(object()))  # type: ignore[arg-type]
    assert isinstance(policy.bundle.response, LookalikeClickModel)  # look-alike response on
    assert isinstance(policy.contact_price.hazard, LookalikeHazard) and policy.planner is not None
    assert type(_agent(agent_factory(object())).contact_price.hazard) is HazardModel  # type: ignore[arg-type]


# --- the agent feeds the tolerance from what it sees -----------------------------------------------


def test_the_agent_learns_tolerance_from_its_own_sends_and_losses() -> None:
    policy = build_agent_policy(
        [default_brief()],
        default_content(),
        frequency=FrequencyConfig(tolerance=ToleranceConfig(min_events=5.0), plans=None),
    )
    hazard = policy.contact_price.hazard
    assert isinstance(hazard, LookalikeHazard)
    rng = np.random.default_rng(1)
    decisions, events = [], {}
    for i in range(300):
        group = "tired" if i % 2 == 0 else "calm"
        s = customer(i, group)
        d = policy.decide(s, ctx(seed=i))
        if d.kind is not DecisionKind.ACT or d.action is None:
            continue
        decisions.append(d)
        sent = [
            Event(
                event_id=f"s{i}",
                customer_id=s.customer_id,
                type=EventType.SEND,
                ts=d.action.send_at,
                source="t",
                channel=Channel.EMAIL,
                decision_id=d.decision_id,
            ),
            Event(
                event_id=f"d{i}",
                customer_id=s.customer_id,
                type=EventType.DELIVER,
                ts=d.action.send_at + timedelta(minutes=1),
                source="t",
                channel=Channel.EMAIL,
                decision_id=d.decision_id,
            ),
        ]
        if group == "tired" and rng.random() < 0.3:
            sent.append(
                Event(
                    event_id=f"u{i}",
                    customer_id=s.customer_id,
                    type=EventType.UNSUBSCRIBE,
                    ts=d.action.send_at + timedelta(hours=2),
                    source="t",
                    channel=Channel.EMAIL,
                    decision_id=d.decision_id,
                )
            )
        events[s.customer_id] = sent
    assert len(decisions) > 100
    later = NOW + timedelta(days=3)
    policy.nightly(decisions, events, later)
    assert hazard.rows == len(decisions)  # every delivered send came in with its profile
    assert hazard.ready  # enough lost contacts for the crowd's picture
    tired = hazard.rate_for(customer(999, "tired"), Channel.EMAIL, 0)
    calm = hazard.rate_for(customer(999, "calm"), Channel.EMAIL, 0)
    assert tired > 2 * calm
    assert not policy._tired_profiles  # delivered sends hand their profile over and drop it


# --- the planted tired group gets fewer messages ----------------------------------------------------


class Bidder:
    goal_id = GOAL

    def bids(self, state: CustomerState, ctx: DecisionContext) -> list[Bid]:
        return [
            Bid(
                goal_id=GOAL,
                action=Action(goal_id=GOAL, channel=Channel.EMAIL, content_id="ci_0", send_at=ctx.now),
                uplift=0.03,
                value_if_converted=100.0,
                channel_cost=0.01,
                expected_net=0.03 * 100.0 - 0.01,
            )
        ]


def planted(model: LookalikeHazard, tired_rate: float, calm_rate: float, n: int = 1200) -> None:
    from autonomy.decide.lookalike import profile_of

    rng = np.random.default_rng(0)
    for group, rate in (("tired", tired_rate), ("calm", calm_rate)):
        for i in range(n):
            key = f"{group}{i}"
            lost = bool(rng.random() < rate)
            model.observe_send(Channel.EMAIL, 0)
            if lost:
                model.observe_event(Channel.EMAIL, 0)
            model.record_send(key, key, profile_of(customer(i, group)), Channel.EMAIL, 0)
            if lost:
                model.record_loss(key)
    model.fit()


def messages(price: ContactPrice, group: str, plans: bool) -> int:
    planner = FrequencyPlanner(price)
    policy = AutonomousPolicy(
        [Bidder()],
        price.floor,
        0.0,
        channel_price_fn=price.prices,
        plan_fn=planner.gate if plans else None,
        mc_draws=50,
    )
    context = DecisionContext(
        now=NOW, briefs=(sample_brief(NOW),), content=tuple(sample_content(NOW)), seed=0
    )
    return sum(policy.decide(customer(i, group), context).kind is DecisionKind.ACT for i in range(60))


@pytest.mark.parametrize("plans", [False, True])
def test_a_planted_tired_group_gets_fewer_messages(plans: bool) -> None:
    hazard, complaints = LookalikeHazard(), LookalikeHazard(base_rate=0.0005)
    planted(hazard, tired_rate=0.04, calm_rate=0.002)
    price = ContactPrice(lambda n: 0.7**n, lambda: 3, hazard=hazard, complaints=complaints)
    calm, tired = messages(price, "calm", plans), messages(price, "tired", plans)
    assert calm == 60 and tired < calm / 2
    # the plain curve cannot tell them apart: the same bidder sends to everyone
    flat = ContactPrice(lambda n: 0.7**n, lambda: 3)
    assert messages(flat, "tired", plans) == messages(flat, "calm", plans) == 60


def test_the_package_imports_nothing_the_v2_ci_job_lacks() -> None:
    banned = {"sklearn", "lightgbm", "scipy", "torch", "xgboost", "autogluon"}
    path = Path(__file__).resolve().parents[3] / "autonomy" / "decide" / "frequency.py"
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        names = (
            [a.name for a in node.names]
            if isinstance(node, ast.Import)
            else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
        )
        assert not {n.split(".")[0] for n in names} & banned, path
    assert rules.FREQUENCY_PLAN in rules.ALL_CODES
