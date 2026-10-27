"""The calibration gate and the evidence-gated Autopilot (autonomy/decide/calibration.py,
autonomy/runtime/evidence_gate.py, api/v2/deps.py `Services.approve`; docs/v4/CALIBRATION.md)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from api.v2.deps import (
    REASON_PROOF_GATE,
    REASON_UNPROVEN_START,
    ConflictError,
    Services,
    build_demo_services,
)
from autonomy.contracts import (
    Action,
    AutonomyLevel,
    Channel,
    Decision,
    DecisionKind,
    Estimate,
    Group,
    GoalStatus,
    ProofSummary,
)
from autonomy.decide.calibration import calibration_status, proof_calibration
from autonomy.runtime.evidence_gate import MIN_WINDOW_DAYS, autopilot_gate, unproven_start
from autonomy.runtime.promotion import PromotionRule

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


# --- calibration_status -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expected", "measured", "ci", "status"),
    [
        (1.0, 1.0, (0.8, 1.2), "ok"),
        (1.14, 1.0, (0.8, 1.2), "ok"),  # DIAGNOSE_RESULTS H5: Gym whatsapp_fatigue, 1.14x
        (1.37, 1.0, (0.8, 1.2), "ok"),  # baseline_shop, 1.37x
        (1.52, 1.0, (0.8, 1.2), "over_1_5x"),  # discount_cannibalisation, 1.52x
        (2.0, 1.0, (0.8, 1.2), "over_1_5x"),  # exactly 2x is not more than 2x
        (2.1, 1.0, (0.8, 1.2), "over_2x"),
        (1.0, 0.0, (-0.2, 0.2), "over_2x"),  # it expects a gain and the holdout shows none
        (1.0, -0.4, (-0.6, -0.2), "over_2x"),
        (0.5, 1.0, (0.8, 1.2), "ok"),  # under-valuing is not a problem this gate guards
        (None, 1.0, (0.8, 1.2), "not_enough_data"),
        (0.0, 1.0, (0.8, 1.2), "not_enough_data"),  # it expects nothing: nothing to check
        (1.0, None, (0.8, 1.2), "not_enough_data"),
        (1.0, 1.0, None, "not_enough_data"),
        (1.0, 1.0, (-2.0, 4.0), "not_enough_data"),  # a range that cannot tell zero from double
        (1.0, 1.0, (float("nan"), 1.0), "not_enough_data"),
    ],
)
def test_calibration_status(
    expected: float | None, measured: float | None, ci: tuple[float, float] | None, status: str
) -> None:
    assert calibration_status(expected, measured, ci) == status


def _act(i: int, expected: float, group: Group = Group.AGENT) -> Decision:
    return Decision(
        decision_id=f"d{i}",
        customer_id=f"c{i}",
        made_at=NOW,
        kind=DecisionKind.ACT,
        action=Action(goal_id="g", channel=Channel.EMAIL, send_at=NOW),
        group=group,
        probability=1.0,
        expected_net=expected,
        reason="-",
        policy_version="t",
    )


def _proof(profit: Estimate | None, calibration: str | None = "ok") -> ProofSummary:
    return ProofSummary(
        goal_id=None,
        as_of=NOW,
        agent_n=500,
        baseline_n=50,
        holdout_n=50,
        incremental_revenue=None,
        incremental_profit=profit,
        lift_vs_baseline=None,
        calibration=calibration,  # type: ignore[arg-type]
    )


def test_proof_calibration_divides_the_incremental_profit_by_the_actions() -> None:
    acts = [_act(i, 1.0) for i in range(100)] + [_act(1000, 9.0, Group.BASELINE)]  # the baseline's are not the agent's
    steady = _proof(Estimate(point=90.0, low=70.0, high=110.0))  # 0.9 per action against an expected 1.0
    assert proof_calibration(steady, acts) == "ok"
    inflated = _proof(Estimate(point=30.0, low=10.0, high=50.0))  # 0.3 per action: 3.3x
    assert proof_calibration(inflated, acts) == "over_2x"
    assert proof_calibration(_proof(None), acts) == "not_enough_data"
    assert proof_calibration(steady, []) == "not_enough_data"


# --- the gate ---------------------------------------------------------------------------------------

PAYS = Estimate(point=500.0, low=120.0, high=880.0)


def test_the_window_is_the_promotion_rules_minimum() -> None:
    assert MIN_WINDOW_DAYS == PromotionRule().min_days


def test_gate_permits_on_planted_evidence() -> None:
    for calibration in ("ok", "over_1_5x"):
        verdict = autopilot_gate(_proof(PAYS, calibration), 20.0)
        assert verdict.allowed, verdict.reason


@pytest.mark.parametrize(
    ("proof", "days", "words"),
    [
        (None, 30.0, "needs proof first"),
        (_proof(None), 30.0, "needs proof first"),
        (_proof(PAYS), None, "at least 14 days"),
        (_proof(PAYS), 9.0, "has 9"),
        (_proof(Estimate(point=300.0, low=-50.0, high=650.0)), 30.0, "not clearly above zero"),
        (_proof(Estimate(point=-300.0, low=-500.0, high=-90.0)), 30.0, "not clearly above zero"),
        (_proof(PAYS, "over_2x"), 30.0, "more than twice"),
        (_proof(PAYS, "not_enough_data"), 30.0, "cannot yet check"),
        (_proof(PAYS, None), 30.0, "cannot yet check"),
    ],
)
def test_gate_refuses_on_planted_evidence_and_says_why(proof: ProofSummary | None, days: float | None, words: str) -> None:
    verdict = autopilot_gate(proof, days)
    assert not verdict.allowed
    assert words in verdict.reason
    assert verdict.reason.endswith("The goal stays on Approve each batch.")


def test_unproven_start_is_email_only_and_needs_the_owners_confirmation() -> None:
    assert unproven_start([Channel.EMAIL], confirmed=True).allowed
    assert not unproven_start([Channel.EMAIL], confirmed=False).allowed
    assert not unproven_start([Channel.EMAIL, Channel.WHATSAPP], confirmed=True).allowed
    assert not unproven_start([Channel.WHATSAPP], confirmed=True).allowed
    assert not unproven_start([], confirmed=True).allowed


# --- Services.approve -------------------------------------------------------------------------------


def _services(channels: tuple[Channel, ...] = (Channel.EMAIL,)) -> tuple[Services, str, list[datetime]]:
    """A demo shop with a fresh draft goal whose plan sends on `channels`, and a clock the test can move."""
    clock = [NOW]
    services = build_demo_services(NOW, n_customers=30, days=3)
    services.clock = lambda: clock[0]
    goal, brief = services.create_goal("Get one-time buyers to buy again", 1000)
    envelope = brief.envelope.model_copy(update={"channels": channels})
    services.workspace.put_brief(brief.model_copy(update={"envelope": envelope}))
    return services, goal.goal_id, clock


def _with_proof(services: Services, proof: ProofSummary | None) -> None:
    services.proof = lambda goal_id: proof  # type: ignore[method-assign]


def test_autopilot_is_refused_without_proof_and_the_goal_does_not_move() -> None:
    services, goal_id, _ = _services()
    _with_proof(services, None)
    with pytest.raises(ConflictError, match="needs proof first"):
        services.approve(goal_id, AutonomyLevel.AUTOPILOT)
    assert services.get_goal(goal_id).status is GoalStatus.DRAFT
    assert services.get_goal(goal_id).autonomy is AutonomyLevel.SHADOW
    assert services.audit_log == []


def test_a_running_goal_stays_on_approve_when_autopilot_is_refused() -> None:
    services, goal_id, clock = _services()
    services.approve(goal_id, AutonomyLevel.APPROVE)
    clock[0] = NOW + timedelta(days=30)
    _with_proof(services, _proof(PAYS, "over_2x"))
    with pytest.raises(ConflictError, match="more than twice"):
        services.approve(goal_id, AutonomyLevel.AUTOPILOT)
    assert services.get_goal(goal_id).autonomy is AutonomyLevel.APPROVE
    assert services.get_goal(goal_id).status is GoalStatus.ACTIVE


def test_autopilot_needs_the_minimum_window_even_with_good_proof() -> None:
    services, goal_id, clock = _services()
    services.approve(goal_id, AutonomyLevel.APPROVE)
    clock[0] = NOW + timedelta(days=5)
    _with_proof(services, _proof(PAYS))
    with pytest.raises(ConflictError, match="at least 14 days"):
        services.approve(goal_id, AutonomyLevel.AUTOPILOT)


def test_autopilot_is_allowed_on_proof_and_logged() -> None:
    services, goal_id, clock = _services()
    services.approve(goal_id, AutonomyLevel.APPROVE)
    clock[0] = NOW + timedelta(days=20)
    _with_proof(services, _proof(PAYS, "over_1_5x"))
    goal, _ = services.approve(goal_id, AutonomyLevel.AUTOPILOT)
    assert goal.autonomy is AutonomyLevel.AUTOPILOT
    [entry] = services.audit_log
    assert entry.reason == REASON_PROOF_GATE and entry.goal_id == goal_id
    assert entry.from_level is AutonomyLevel.APPROVE and entry.to_level is AutonomyLevel.AUTOPILOT
    assert entry.evidence["incremental_profit_low"] == PAYS.low


def test_other_levels_are_never_gated() -> None:
    services, goal_id, _ = _services()
    _with_proof(services, None)
    assert services.approve(goal_id, AutonomyLevel.SHADOW)[0].autonomy is AutonomyLevel.SHADOW
    assert services.approve(goal_id, AutonomyLevel.APPROVE)[0].autonomy is AutonomyLevel.APPROVE


def test_email_may_start_on_autopilot_only_with_the_owners_confirmation_and_it_is_recorded() -> None:
    services, goal_id, _ = _services((Channel.EMAIL,))
    _with_proof(services, None)
    with pytest.raises(ConflictError):  # no confirmation: the same refusal as any goal
        services.approve(goal_id, AutonomyLevel.AUTOPILOT)
    sunk = []
    services.audit_sink = sunk.append
    goal, _ = services.approve(goal_id, AutonomyLevel.AUTOPILOT, start_without_proof=True)
    assert goal.autonomy is AutonomyLevel.AUTOPILOT and goal.status is GoalStatus.ACTIVE
    [entry] = services.audit_log
    assert entry.reason == REASON_UNPROVEN_START and entry.evidence == {"confirmed": 1.0}
    assert "confirmed" in entry.words and sunk == [entry]


def test_whatsapp_never_starts_without_proof_even_when_confirmed() -> None:
    for channels in ((Channel.WHATSAPP,), (Channel.EMAIL, Channel.WHATSAPP)):
        services, goal_id, _ = _services(channels)
        _with_proof(services, None)
        with pytest.raises(ConflictError, match="email alone"):
            services.approve(goal_id, AutonomyLevel.AUTOPILOT, start_without_proof=True)
        assert services.get_goal(goal_id).autonomy is AutonomyLevel.SHADOW
        assert services.audit_log == []


def test_the_confirmation_is_for_a_start_not_for_a_goal_already_running() -> None:
    services, goal_id, clock = _services()
    services.approve(goal_id, AutonomyLevel.APPROVE)
    clock[0] = NOW + timedelta(days=1)
    _with_proof(services, None)
    with pytest.raises(ConflictError, match="first time"):
        services.approve(goal_id, AutonomyLevel.AUTOPILOT, start_without_proof=True)
    assert services.get_goal(goal_id).autonomy is AutonomyLevel.APPROVE
