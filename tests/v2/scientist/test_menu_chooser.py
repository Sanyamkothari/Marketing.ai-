"""The runtime hook: Shadow first, live only when set, never past a hard rule, always logged."""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from autonomy.contracts import (
    Channel,
    ChannelStats,
    CustomerState,
    Decision,
    DecisionContext,
    DecisionKind,
    Group,
)
from autonomy.runtime.agent import build_agent_policy
from autonomy.scientist import ArmAction, BlanketPolicy, Campaign, ScientistChooser, SegmentRule
from autonomy.scientist.chooser import BUDGET_CODE
from tests.v2.realfix.test_uplift_support import GOAL, NOW, brief, campaign, content, reachable

ARM_ACTIONS = {1: ArmAction(GOAL, Channel.EMAIL, content_id="c_quality")}


def campaign_of_two_arms() -> Campaign:
    n = 40
    return Campaign(
        customer_ids=tuple(f"k{i}" for i in range(n)),
        arm=np.array([i % 2 for i in range(n)], dtype=np.int64),
        propensity=np.full(n, 0.5),
        arm_names=("no message", "email|none|quality"),
        arm_cost=(0.0, 0.5),
        converted=np.zeros(n),
        margin=np.zeros(n),
        features=pd.DataFrame({"tier": ["a", "b"] * (n // 2), "age": [30.0] * n}),
    )


def chooser(floor: float = 0.0, **kwargs: object) -> ScientistChooser:
    fitted = BlanketPolicy(1, exploration_floor=floor).fit(campaign_of_two_arms())
    return ScientistChooser(policy=fitted, arm_actions=ARM_ACTIONS, **kwargs)  # type: ignore[arg-type]


def agent_and_ctx() -> tuple[object, DecisionContext]:
    library = [c for c in content() if c.content_id == "c_quality"]
    policy = build_agent_policy([brief()], library, costs={Channel.EMAIL: 0.5})
    return policy, DecisionContext(now=NOW, briefs=(brief(),), content=tuple(library), seed=3)


def test_without_a_chooser_the_agent_is_exactly_as_before() -> None:
    plain, ctx = agent_and_ctx()
    hooked, _ = agent_and_ctx()
    hooked.use_chooser(None)  # type: ignore[attr-defined]
    for i in range(20):
        st = reachable(f"p{i}", "a" if i % 2 else "b")
        a = plain.decide(st, ctx)  # type: ignore[attr-defined]
        b = hooked.decide(st, ctx)  # type: ignore[attr-defined]
        assert a.model_dump() == b.model_dump()


def test_shadow_returns_the_agents_decision_and_logs_what_the_policy_would_do() -> None:
    plain, ctx = agent_and_ctx()
    shadowed, _ = agent_and_ctx()
    ch = chooser()
    shadowed.use_chooser(ch)  # type: ignore[attr-defined]
    assert ch.mode == "shadow"
    for i in range(15):
        st = reachable(f"s{i}", "a")
        want = plain.decide(st, ctx)  # type: ignore[attr-defined]
        got = shadowed.decide(st, ctx)  # type: ignore[attr-defined]
        assert got.model_dump() == want.model_dump()  # not one byte of the agent's decision changes
    assert len(ch.shadow_log) == 15
    rec = ch.shadow_log[0]
    assert rec.policy_arm == 1 and rec.policy_probability == 1.0
    assert rec.customer_id == "s0" and rec.made_at == NOW and isinstance(rec.differs, bool)
    assert not shadowed.alerts  # type: ignore[attr-defined]


def test_live_returns_the_policys_decision_with_its_probability_and_reason() -> None:
    policy, ctx = agent_and_ctx()
    ch = chooser(floor=0.2)
    ch.set_mode("live", at=NOW, reason="shadow week agreed with the agent")
    policy.use_chooser(ch)  # type: ignore[attr-defined]
    st = reachable("L1", "a")
    d: Decision = policy.decide(st, ctx)  # type: ignore[attr-defined]
    assert d.policy_version == "scientist-v1" and d.group is Group.AGENT
    assert d.kind is DecisionKind.ACT and d.action is not None and d.action.content_id == "c_quality"
    assert d.probability == pytest.approx(0.8 + 0.2 / 2)  # the chance of the option taken, floor included
    assert "Selected policy" in d.reason
    assert ch.shadow_log == []  # live decisions are returned, not shadowed
    # the exploration floor sends some customers the other option, and says so with its own probability
    kinds = {policy.decide(reachable(f"L{i}", "a"), ctx).kind for i in range(2, 80)}  # type: ignore[attr-defined]
    assert kinds == {DecisionKind.ACT, DecisionKind.NOTHING}


def test_live_never_overrides_a_hard_rule() -> None:
    policy, ctx = agent_and_ctx()
    ch = chooser()
    ch.set_mode("live", at=NOW, reason="test")
    policy.use_chooser(ch)  # type: ignore[attr-defined]
    no_consent = reachable("n1", "a").model_copy(
        update={"channels": {Channel.EMAIL: ChannelStats(address_known=True, consent=False)}}
    )
    d = policy.decide(no_consent, ctx)  # type: ignore[attr-defined]
    assert d.kind is DecisionKind.NOTHING and d.action is None
    assert d.probability == 1.0 and d.blocked_by  # masked to the allowed options, renormalised
    capped = reachable("n2", "a").model_copy(update={"contacts_7d": 2})  # at the envelope's weekly cap
    d2 = policy.decide(capped, ctx)  # type: ignore[attr-defined]
    assert d2.kind is DecisionKind.NOTHING and d2.blocked_by


def test_the_weekly_contact_budget_setting_is_enforced() -> None:
    policy, ctx = agent_and_ctx()
    ch = chooser(weekly_contact_budget=1)
    ch.set_mode("live", at=NOW, reason="test")
    policy.use_chooser(ch)  # type: ignore[attr-defined]
    one = reachable("b1", "a").model_copy(update={"contacts_7d": 1})  # the envelope allows 2, the setting 1
    d = policy.decide(one, ctx)  # type: ignore[attr-defined]
    assert d.kind is DecisionKind.NOTHING and BUDGET_CODE in d.blocked_by
    assert policy.decide(reachable("b2", "a"), ctx).kind is DecisionKind.ACT  # type: ignore[attr-defined]


def test_holdout_and_baseline_customers_never_reach_the_chooser() -> None:
    policy, ctx = agent_and_ctx()
    seen: list[str] = []

    def spy(state: CustomerState, c: DecisionContext, agent: Decision) -> Decision:
        seen.append(state.customer_id)
        return agent

    policy.use_chooser(spy)  # type: ignore[attr-defined]
    for group, cid in ((Group.HOLDOUT, "h1"), (Group.BASELINE, "b1"), (Group.AGENT, "a1")):
        policy.decide(reachable(cid, "a").model_copy(update={"group": group}), ctx)  # type: ignore[attr-defined]
    assert seen == ["a1"]
    # and the chooser itself hands a non-agent customer straight back
    ch = chooser()
    ch.set_mode("live", at=NOW, reason="test")
    held = reachable("h2", "a").model_copy(update={"group": Group.HOLDOUT})
    plain = policy.policy.decide(held, ctx)  # type: ignore[attr-defined]
    assert ch(held, ctx, plain) is plain


def test_a_chooser_that_raises_leaves_the_agents_decision_and_leaves_a_note() -> None:
    plain, ctx = agent_and_ctx()
    broken, _ = agent_and_ctx()

    def boom(state: CustomerState, c: DecisionContext, agent: Decision) -> Decision:
        raise RuntimeError("the policy file is corrupt")

    broken.use_chooser(boom)  # type: ignore[attr-defined]
    st = reachable("x1", "a")
    assert broken.decide(st, ctx).model_dump() == plain.decide(st, ctx).model_dump()  # type: ignore[attr-defined]
    assert any("the policy file is corrupt" in a and "x1" in a for a in broken.alerts)  # type: ignore[attr-defined]


def warm_agent_and_ctx() -> tuple[object, DecisionContext]:
    """An agent warm-started on a past campaign where tier a responds, so it e-mails tier a."""
    events, states = campaign(n=3000, lift_a=0.15, lift_b=0.0, base=0.05)
    library = [c for c in content() if c.content_id == "c_quality"]
    policy = build_agent_policy([brief()], library, costs={Channel.EMAIL: 1.0})
    policy.nightly([], events, NOW)
    ctx = DecisionContext(now=NOW, briefs=(brief(),), content=tuple(library), seed=1)
    policy.prepare_day(list(states.values()), ctx)
    return policy, ctx


def test_a_replaced_decision_does_not_leave_the_agents_send_pending() -> None:
    """The agent's own send is dropped when live replaces it, so it must not count towards the cap."""
    agent_only, ctx = warm_agent_and_ctx()
    st = reachable("r1", "a")
    own = agent_only.decide(st, ctx)  # type: ignore[attr-defined]
    assert own.kind is DecisionKind.ACT  # the agent wants to send ...
    assert agent_only.policy.pending_sends(st, ctx.now) == 1  # type: ignore[attr-defined]  # ... and counts it as pending
    policy, ctx2 = warm_agent_and_ctx()
    nobody = BlanketPolicy(0).fit(campaign_of_two_arms())  # a policy that contacts nobody
    policy.use_chooser(ScientistChooser(policy=nobody, arm_actions=ARM_ACTIONS, mode="live"))  # type: ignore[attr-defined]
    d = policy.decide(st, ctx2)  # type: ignore[attr-defined]
    assert d.kind is DecisionKind.NOTHING and d.policy_version == "scientist-v1"
    assert policy.policy.pending_sends(st, ctx2.now) == 0  # type: ignore[attr-defined]


def test_live_respects_sends_already_scheduled_for_the_customer() -> None:
    """The state does not show a scheduled send yet; the weekly cap must still count it (the envelope allows 2)."""
    policy, ctx = warm_agent_and_ctx()
    ch = chooser()
    ch.set_mode("live", at=NOW, reason="test")
    policy.use_chooser(ch)  # type: ignore[attr-defined]
    st = reachable("cap1", "a")
    kinds = []
    for hour in range(5):
        later = ctx.model_copy(update={"now": ctx.now + timedelta(hours=hour)})
        kinds.append(policy.decide(st, later).kind)  # type: ignore[attr-defined]
    assert kinds.count(DecisionKind.ACT) == 2  # never more than the envelope's weekly cap
    assert kinds[:2] == [DecisionKind.ACT, DecisionKind.ACT]


def test_behaviour_columns_of_a_history_campaign_are_read_from_the_customers_state() -> None:
    """A campaign built from the shop's history has `behaviour_*` feature columns; a customer's
    preferences do not, so the chooser must fill them from the state or every customer looks alike."""
    rng = np.random.default_rng(0)
    n = 4000
    purchases = rng.integers(0, 6, n).astype(float)
    arm = rng.choice(2, size=n)
    conv = (rng.uniform(size=n) < 0.05 + np.where((arm == 1) & (purchases >= 3), 0.4, 0.0)).astype(float)
    camp = Campaign(
        customer_ids=tuple(f"k{i}" for i in range(n)),
        arm=arm.astype(np.int64),
        propensity=np.full(n, 0.5),
        arm_names=("no message", "email|none|quality"),
        arm_cost=(0.0, 0.5),
        converted=conv,
        margin=10.0 * conv,
        features=pd.DataFrame({"behaviour_purchases": purchases, "tier": rng.choice(["a", "b"], n)}),
    )
    fitted = SegmentRule(columns=("behaviour_purchases",), exploration_floor=0.0).fit(camp)
    ch = ScientistChooser(policy=fitted, arm_actions=ARM_ACTIONS, mode="live")
    policy, ctx = agent_and_ctx()
    policy.use_chooser(ch)  # type: ignore[attr-defined]
    few = reachable("bh0", "a").model_copy(update={"purchases": 0})
    many = reachable("bh5", "a").model_copy(update={"purchases": 5, "revenue": 50.0})
    assert policy.decide(few, ctx).kind is DecisionKind.NOTHING  # type: ignore[attr-defined]
    assert policy.decide(many, ctx).kind is DecisionKind.ACT  # type: ignore[attr-defined]


def test_modes_are_logged_with_a_reason_and_reversible() -> None:
    ch = chooser()
    assert ch.mode == "shadow" and ch.mode_log == []
    with pytest.raises(ValueError, match="reason"):
        ch.set_mode("live", at=NOW, reason="  ")
    with pytest.raises(ValueError):
        ch.set_mode("sideways", at=NOW, reason="x")  # type: ignore[arg-type]
    ch.set_mode("live", at=NOW, reason="shadow week matched the forecast")
    ch.set_mode("live", at=NOW + timedelta(days=1), reason="again")  # no change, no entry
    ch.set_mode("shadow", at=NOW + timedelta(days=9), reason="monitor asked for a re-run")
    assert [(m.mode, m.reason) for m in ch.mode_log] == [
        ("live", "shadow week matched the forecast"),
        ("shadow", "monitor asked for a re-run"),
    ]
    assert ch.mode == "shadow"


def test_a_selection_becomes_a_shadow_chooser_refitted_on_the_train_part() -> None:
    from autonomy.scientist import SegmentRule, UpliftPolicy, run_scientist
    from autonomy.scientist.chooser import chooser_from_selection
    from tests.v2.scientist.test_menu_campaign import make_campaign

    c = make_campaign(40000, seed=41)
    arms = {
        1: ArmAction(GOAL, Channel.EMAIL, content_id="c_quality"),
        2: ArmAction(GOAL, Channel.EMAIL, content_id="c_novelty"),
    }
    sel = run_scientist(c, [SegmentRule(), UpliftPolicy()])
    assert sel.outcome == "candidate"
    ch = chooser_from_selection(sel, c, arms)
    assert ch is not None and ch.mode == "shadow" and ch.mode_log == []  # shadow first, nothing logged yet
    probs = ch.policy.probabilities(c.features.iloc[:50].reset_index(drop=True), None)
    assert probs.shape == (50, 3) and np.allclose(probs.sum(axis=1), 1.0)
    with pytest.raises(ValueError, match="cover the arms"):
        chooser_from_selection(sel, c, {1: arms[1]})
    # a state with the campaign's features is judged as the fitted policy judges the same row
    state = reachable("v1", "a").model_copy(update={"preferences": {"x1": "0.9", "x2": "a", "x3": "0.1"}})
    row = ch.policy.row_features(state.preferences)
    assert list(row.columns) == ["x1", "x2", "x3"] and float(row.iloc[0]["x1"]) == 0.9
