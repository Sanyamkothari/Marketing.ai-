"""The DIAG block (autonomy/runtime/diagnostics.py): reason codes, the tally, the Gym smoke and the bench."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from autonomy.contracts import (
    Action,
    Channel,
    CustomerState,
    Decision,
    DecisionContext,
    DecisionKind,
    Group,
)
from autonomy.gym import default_brief, default_content, get_scenario
from autonomy.gym.run import main as gym_main
from autonomy.gym.score import observing, run_policy
from autonomy.runtime import ablation
from autonomy.runtime.diagnostics import DecisionTally, RunRecorder, format_block, reason_code
from scripts.v2_realbench import common as bench
from tests.v2.realfix.test_bench import _synthetic

NOW = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)


def _nothing(reason: str, *blocked: str, journey_id: str | None = None, ended: str | None = None) -> Decision:
    return Decision(
        decision_id="d",
        customer_id="c",
        made_at=NOW,
        kind=DecisionKind.NOTHING,
        probability=1.0,
        reason=reason,
        blocked_by=blocked,
        policy_version="t",
        journey_id=journey_id,
        journey_ended=ended,
    )


def _act(net: float = 0.5, price: float = 0.1) -> Decision:
    return Decision(
        decision_id="a",
        customer_id="c",
        made_at=NOW,
        kind=DecisionKind.ACT,
        action=Action(goal_id="g", channel=Channel.EMAIL, offer_id="none", send_at=NOW),
        probability=0.5,
        expected_net=net,
        attention_price=price,
        reason="x",
        policy_version="t",
    )


def test_reason_codes_name_why_a_decision_did_nothing() -> None:
    assert reason_code(_act()) == "act"
    assert reason_code(_nothing("Left alone: already reached the weekly contact limit.", "weekly_cap")) == (
        "blocked:weekly_cap"
    )
    assert (
        reason_code(_nothing("Left alone: waiting is worth more", "frequency_plan"))
        == "blocked:frequency_plan"
    )
    assert reason_code(_nothing("Left alone: x", "experiment_arm", "weekly_cap")) == "experiment_arm"
    assert (
        reason_code(_nothing("Left alone: no active goal has anything to offer this customer.")) == "no_bids"
    )
    assert reason_code(
        _nothing("Left alone this time: testing whether a rest beats a message for them; ...")
    ) == ("rest_draw")
    assert reason_code(_nothing("Left alone this time: not sure enough yet that a message pays off")) == (
        "restraint_hold"
    )
    assert reason_code(
        _nothing("Left alone: no message is worth more than the cost of contacting (best $1)")
    ) == ("price_beats_value")
    assert reason_code(_nothing("Left alone: no message is worth more than the cost of contacting.")) == (
        "net_not_positive"
    )
    assert reason_code(_nothing("A journey holds them", journey_id="j")) == "journey_hold"
    assert reason_code(_nothing("something new")) == "other"


def test_the_tally_counts_kinds_codes_and_parts_and_merges() -> None:
    state = CustomerState(customer_id="c", as_of=NOW, group=Group.AGENT, contacts_7d=1)
    ctx = DecisionContext(now=NOW, briefs=(default_brief(),))  # cap 2
    t = DecisionTally()
    t.add(_act(0.5, 0.1))
    t.add(_act(1.5, 0.3))
    t.add(
        _nothing("Left alone: cap", "weekly_cap"), state, ctx
    )  # 1 contact, cap 2: the customer's own rhythm
    t.add(_nothing("Left alone: cap", "weekly_cap"), state.model_copy(update={"contacts_7d": 2}), ctx)
    t.add(_nothing("Left alone: plan", "frequency_plan"))
    t.add(_nothing("hold", journey_id="j"))
    t.add(_nothing("ended", ended="j:never_read"))
    assert (t.total, t.acts, t.nothing) == (7, 2, 5)
    assert t.mean_expected_net == pytest.approx(1.0) and t.mean_attention_price == pytest.approx(0.2)
    assert t.blocked["weekly_cap"] == 2 and t.blocked["frequency_plan"] == 1
    assert t.parts["weekly_cap_hold_below_cap"] == 1 and t.parts["weekly_cap_hold_at_cap"] == 1
    assert t.parts["frequency_plan_hold"] == 1 and t.parts["journey_hold"] == 1
    assert t.parts["journey_ended:never_read"] == 1
    other = DecisionTally()
    other.add(_act(2.0, 0.0))
    t.merge(other)
    assert t.acts == 3 and t.mean_expected_net == pytest.approx(4.0 / 3)
    block = format_block(
        "run x", t, changed={"restraint": False}, snapshot={"who_left_alone": 3}, extra={"k": 1}
    )
    lines = block.splitlines()
    assert lines and all(line.startswith("DIAG ") for line in lines)
    assert "restraint=False" in block and "who_left_alone=3" in block and "k=1" in block
    assert "decisions total=8 act=3" in block


def test_a_tally_with_nothing_in_it_still_prints() -> None:
    block = format_block("empty", DecisionTally(), changed=None)
    assert "decisions total=0 act=0" in block and "n/a (not the agent)" in block


def test_observing_changes_no_score_and_sees_only_the_policy_under_test() -> None:
    spec = get_scenario("baseline_shop", n_customers=120, days=5)
    plain = run_policy(
        spec, lambda _w: ablation.build_variant("agent", [default_brief()], default_content()), 0
    )
    recorder = RunRecorder("watched")
    with observing(recorder):
        watched = run_policy(
            spec,
            lambda _w: ablation.build_variant("agent", [default_brief()], default_content()),
            0,
            policy_name="watched",
        )
    assert watched.model_copy(update={"policy": plain.policy}) == plain  # the oracle ran inside, unobserved
    assert recorder.tally.acts == watched.actions and recorder.tally.nothing > 0 and recorder.runs == 1
    after = DecisionTally()
    after.merge(recorder.tally)
    run_policy(spec, lambda _w: ablation.build_variant("agent", [default_brief()], default_content()), 0)
    assert recorder.tally.total == after.total  # the observer is off outside the `with`


@pytest.mark.parametrize(
    "policy",
    ["agent_no_attention_price", pytest.param("v3_no_journeys", marks=pytest.mark.slow)],
)
def test_the_gym_smoke_prints_a_diag_block(policy: str, capsys: pytest.CaptureFixture[str]) -> None:
    """200 customers x 14 days, one seed, the policy alone: the DIAG block is printed after the scorecard."""
    argv = ["--scenario", "baseline_shop", "--policy", policy, "--seeds", "1", "--customers", "200"]
    code = gym_main([*argv, "--days", "14", "--no-compare"])
    out = capsys.readouterr().out
    assert code == 0
    diag = [line for line in out.splitlines() if line.startswith("DIAG ")]
    assert len(diag) >= 6
    text = "\n".join(diag)
    for needle in (
        f"policy={policy}",
        "decisions total=",
        "nothing by reason",
        "blocked_by (any decision)",
        "figures",
    ):
        assert needle in text, needle
    assert "contacts_per_customer_week=" in text and "true_value_per_action=" in text
    total = int(text.split("decisions total=")[1].split()[0])
    assert total > 0
    changed = text.split("switches differing from the plain agent: ")[1].splitlines()[0]
    if policy == "agent_no_attention_price":
        assert changed == "attention_price=False"
    else:  # the preset less journeys: five parts on, journeys not among them
        assert "journeys" not in changed and "lookalike=True" in changed and "experiments=True" in changed


def test_the_bench_prints_a_diag_block_for_a_variant() -> None:
    data, _ = _synthetic()
    out = bench.evaluate_dataset(data, seeds=1, timings=bench.Timings(quiet=True), variant="agent_no_explore")
    assert out["agent"]["variant"] == "agent_no_explore" and out["agent"]["v3"] is False
    text = out["agent"]["diag_text"]
    assert text and all(line.startswith("DIAG ") for line in text.splitlines())
    assert "exploration_floor=0.0" in text and "expected_over_measured=" in text
    assert out["agent"]["diag"]["total"] == out["test"]  # one decision per test customer and seed
    default = bench.evaluate_dataset(data, seeds=1, timings=bench.Timings(quiet=True))
    assert default["agent"]["variant"] == "agent" and "switches differing from the plain agent: none" in (
        default["agent"]["diag_text"]
    )


def test_the_bench_cli_takes_a_variant_and_prints_diag(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.v2_realbench import run as bench_run

    monkeypatch.setattr(bench_run, "load", lambda name, data_dir, timings, limit=None: _synthetic()[0])
    code = bench_run.main(["--dataset", "hillstrom", "--policy", "v3_no_journeys", "--seeds", "1", "--quiet"])
    out = capsys.readouterr().out
    assert code == 0
    assert "DIAG run bench dataset=synthetic policy=v3_no_journeys" in out
    assert "journeys=False" not in out.split("switches differing from the plain agent: ")[1].splitlines()[0]
    with pytest.raises(SystemExit):
        bench_run.main(["--dataset", "hillstrom", "--policy", "not_a_variant"])
