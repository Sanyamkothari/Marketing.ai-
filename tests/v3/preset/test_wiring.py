"""The preset where it is switched on: the runtime setting, the Gym policy, the real bench, and the imports."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from autonomy.gym import default_brief, default_content, get_scenario
from autonomy.gym import run as gym_run
from autonomy.gym.run import (
    AGENT_NO_MOMENTS,
    AGENT_V3,
    POLICIES,
    agent_factory,
    agent_v3_factory,
    main,
    no_moments_agent_factory,
    v3_no_moments_agent_factory,
)
from autonomy.gym.score import run_policy
from autonomy.runtime.agent import AgentPolicy, LookalikeClickModel
from autonomy.runtime.settings import RuntimeSettings, agent_policy_factory, build_runtime
from autonomy.runtime.v3 import V3Preset
from autonomy.store import memory_engine
from scripts.v2_realbench import common as bench
from tests.v2.realfix.test_bench import _synthetic


def _agent(policy: object) -> AgentPolicy:
    assert isinstance(policy, AgentPolicy)
    return policy


# --- the runtime ---------------------------------------------------------------------------------


def test_the_runtime_setting_is_off_by_default_and_read_from_the_environment() -> None:
    assert RuntimeSettings().v3 is False and RuntimeSettings().v3_preset() is None
    assert RuntimeSettings.from_env({"AUTONOMY_V3": "true"}).v3 is True
    assert RuntimeSettings.from_env({}).v3 is False
    one = RuntimeSettings(v3=True, countries=("IN",)).v3_preset()
    assert one == V3Preset(moments_country="IN")
    assert RuntimeSettings(v3=True, countries=("IN", "US")).v3_preset() == V3Preset()  # no single calendar


def test_the_runtime_setting_turns_on_the_preset_and_the_profile_and_keeps_the_scale_fixes() -> None:
    off = build_runtime(RuntimeSettings(database_url="sqlite://"), engine=memory_engine())
    on = build_runtime(RuntimeSettings(database_url="sqlite://", v3=True), engine=memory_engine())
    assert off.config.nightly_profile is False and on.config.nightly_profile is True
    for runtime in (off, on):  # the sweep's batches and the bounded report are the same either way
        assert runtime.config.sweep_batch == 5_000 and runtime.config.report_decisions == 1_000
    brief, content = default_brief(), default_content()
    plain = _agent(off.policy_factory([brief], content))
    assert plain.who is None and plain.experiment_policy is None and plain.policy.journeys is None
    agent = _agent(on.policy_factory([brief], content))
    assert isinstance(agent.bundle.response, LookalikeClickModel)
    assert agent.who is not None and agent.planner is not None and agent.moments
    assert agent.policy.journeys is not None and agent.offer_pricing is not None
    assert agent.experiment_policy is not None and agent.experiment_policy.book.running() == []
    assert agent.policy.baseline_policy is not None  # the simple rule for the baseline group is still set


def test_the_factory_takes_a_preset() -> None:
    agent = _agent(agent_policy_factory([default_brief()], default_content(), v3=V3Preset(journeys=False)))
    assert agent.who is not None and agent.policy.journeys is None


# --- the Gym -------------------------------------------------------------------------------------


def test_the_gym_lists_agent_v3_next_to_the_others() -> None:
    assert POLICIES[AGENT_V3] is agent_v3_factory
    assert {"agent", "agent_lookalike", "agent_who", "agent_frequency"} <= set(POLICIES)
    v3 = _agent(agent_v3_factory(object()))  # type: ignore[arg-type]
    assert v3.who is not None and v3.planner is not None and v3.policy.journeys is not None
    assert _agent(agent_factory(object())).experiment_policy is None  # type: ignore[arg-type]
    control = _agent(v3_no_moments_agent_factory(object()))  # type: ignore[arg-type]
    assert control.moments is False and control.who is not None and control.policy.journeys is not None


class _ComparedError(Exception):
    """Raised by the stand-in `compare` once it has seen what the CLI hands it."""


def test_the_gym_cli_takes_agent_v3_with_moments_and_refuses_other_policies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def compare(spec: object, policies: dict[str, object], seeds: object, **kw: object) -> None:
        seen.update(policies)
        seen["folder_factory"] = kw.get("folder_factory")
        raise _ComparedError

    monkeypatch.setattr(gym_run, "compare", compare)
    with pytest.raises(_ComparedError):
        main(["--scenario", "smoke", "--policy", "agent_v3", "--moments", "--seeds", "1"])
    assert seen[AGENT_V3] is agent_v3_factory  # the preset as it is: its moments are on already
    assert seen[AGENT_NO_MOMENTS] is v3_no_moments_agent_factory  # the control is the preset less moments
    assert seen["folder_factory"] is not None  # folded with the full StateFolder
    seen.clear()
    with pytest.raises(_ComparedError):
        main(["--scenario", "smoke", "--policy", "agent", "--moments", "--seeds", "1"])
    assert seen[AGENT_NO_MOMENTS] is no_moments_agent_factory  # the plain agent keeps its own control
    with pytest.raises(SystemExit):
        main(["--scenario", "smoke", "--policy", "rule_baseline", "--moments"])


def test_the_gym_workflow_scores_agent_v3_on_every_scenario() -> None:
    text = (Path(__file__).resolve().parents[3] / ".github" / "workflows" / "gym.yml").read_text()
    match = re.search(r"policy: \[([^\]]*)\]", text)
    assert match is not None and "agent_v3" in [p.strip() for p in match.group(1).split(",")]
    assert "--policy agent_v3 --moments" in text


@pytest.mark.slow
def test_the_v3_agent_runs_in_the_gym_and_repeats() -> None:
    """The local smoke run: every part together in a 200-customer world (CI scores it against `agent`)."""
    spec = get_scenario("baseline_shop", n_customers=200, days=40)
    first = run_policy(spec, agent_v3_factory, 0, with_oracle=False)
    again = run_policy(spec, agent_v3_factory, 0, with_oracle=False)
    assert first.violations == 0
    assert first.model_dump() == again.model_dump()


# --- the real bench ------------------------------------------------------------------------------


def test_the_bench_hands_the_who_book_the_test_customers_pre_campaign_events() -> None:
    data, calls = _synthetic()
    out = bench.evaluate_dataset(data, seeds=1, timings=bench.Timings(quiet=True), v3=True)
    _train, test = bench.split_by_hash(data.customer_ids)
    assert out["agent"]["v3"] is True
    coverage = next(x for x in out["agent"]["learned"] if x.startswith("who: customers read"))
    prepared = int(re.search(r"'prepared': (\d+)", coverage).group(1))  # type: ignore[union-attr]
    with_profile = int(re.search(r"'profile': (\d+)", coverage).group(1))  # type: ignore[union-attr]
    assert prepared == with_profile == len(test)  # decision 1 is exercised on the TEST customers
    assert set(calls[0]).isdisjoint(data.customer_ids[test].tolist())  # their campaign events never went in
    plain = bench.evaluate_dataset(data, seeds=1, timings=bench.Timings(quiet=True))
    assert plain["agent"]["v3"] is False and not any(x.startswith("who:") for x in plain["agent"]["learned"])


def test_the_bench_workflow_has_a_policy_axis() -> None:
    text = (Path(__file__).resolve().parents[3] / ".github" / "workflows" / "realbench.yml").read_text()
    assert "policy: [agent, agent_v3" in text and "--policy ${{ matrix.policy }}" in text


# --- the imports ---------------------------------------------------------------------------------


def test_the_preset_module_imports_only_what_the_v2_job_installs() -> None:
    tree = ast.parse((Path(__file__).resolve().parents[3] / "autonomy" / "runtime" / "v3.py").read_text())
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            roots.add((node.module or "").split(".")[0])
    assert roots <= {"__future__", "dataclasses", "typing", "autonomy"}
