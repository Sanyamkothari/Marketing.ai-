"""The agent: the default is unchanged, and the switch puts the look-alike model on top."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from autonomy.contracts import Channel, ChannelStats, CustomerState
from autonomy.decide.lookalike import LookalikeConfig, LookalikeResponseModel
from autonomy.decide.models import ResponseModel
from autonomy.gym import default_brief, default_content, get_scenario
from autonomy.gym.run import agent_factory
from autonomy.gym.score import run_policy
from autonomy.runtime.agent import AgentPolicy, ClickResponseModel, LookalikeClickModel, build_agent_policy
from autonomy.runtime.settings import RuntimeSettings, agent_policy_factory

NOW = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)


def _agent(policy: object) -> AgentPolicy:
    assert isinstance(policy, AgentPolicy)
    return policy


def test_the_default_agent_is_the_plain_click_model() -> None:
    policy = build_agent_policy([default_brief()], default_content())
    assert type(policy.bundle.response) is ClickResponseModel
    assert policy.outcomes is None


def test_the_switch_builds_the_lookalike_model_with_the_click_model_as_its_prior() -> None:
    policy = build_agent_policy([default_brief()], default_content(), lookalike=True)
    model = policy.bundle.response
    assert isinstance(model, LookalikeClickModel) and isinstance(model, ClickResponseModel)
    assert isinstance(model, ResponseModel) and isinstance(model.lookalike, LookalikeResponseModel)
    assert policy.outcomes is not None
    custom = build_agent_policy(
        [default_brief()], default_content(), lookalike=LookalikeConfig(min_treated=50.0)
    )
    assert isinstance(custom.bundle.response, LookalikeClickModel)
    assert custom.bundle.response.lookalike.config.min_treated == 50.0


def test_before_any_data_it_answers_exactly_like_the_click_model() -> None:
    state = CustomerState(
        customer_id="c1",
        as_of=NOW,
        purchases=2,
        revenue=100.0,
        first_seen_at=NOW - timedelta(days=100),
        last_purchase_at=NOW - timedelta(days=10),
        channels={Channel.EMAIL: ChannelStats(address_known=True, consent=True)},
    )
    plain, look = ClickResponseModel(), LookalikeClickModel()
    for key in ("email|none|reminder", "email|pct10|savings"):
        assert look.predict(state, key) == plain.predict(state, key)
        assert look.treated_rate(state, key) == plain.treated_rate(state, key)
        assert look.option_std(state, key) == plain.option_std(state, key)


def test_the_runtime_switch_is_off_by_default_and_reaches_the_agent() -> None:
    assert RuntimeSettings().lookalike_model is False
    brief, content = default_brief(), default_content()
    assert type(_agent(agent_policy_factory([brief], content)).bundle.response) is ClickResponseModel
    assert isinstance(
        _agent(agent_policy_factory([brief], content, lookalike=True)).bundle.response, LookalikeClickModel
    )


def test_the_default_gym_run_repeats() -> None:
    spec = get_scenario("smoke", n_customers=120, days=7, history_days=30)
    first = run_policy(spec, agent_factory, 0, with_oracle=False)
    again = run_policy(spec, agent_factory, 0, with_oracle=False)
    assert first.model_dump() == again.model_dump()


@pytest.mark.slow
def test_the_lookalike_agent_runs_in_the_gym_and_learns_from_it() -> None:
    spec = get_scenario("smoke", n_customers=200, days=40, history_days=30)
    built: list[AgentPolicy] = []

    def factory(world: object) -> AgentPolicy:
        policy = build_agent_policy(
            [default_brief()],
            default_content(),
            lookalike=LookalikeConfig(min_treated=40.0, min_control=10.0),
        )
        built.append(policy)
        return policy

    card = run_policy(spec, factory, 0, with_oracle=False)
    again = run_policy(spec, factory, 0, with_oracle=False)
    assert card.violations == 0
    assert card.model_dump() == again.model_dump()  # repeatable
    model = built[0].bundle.response
    assert isinstance(model, LookalikeClickModel)
    assert model.lookalike.rows > 0 and model.lookalike.refused == 0
    assert model.lookalike.ready  # the crowd's fit is in use, not only gathering rows
    state = CustomerState(
        customer_id="probe",
        as_of=NOW,
        purchases=3,
        revenue=150.0,
        first_seen_at=NOW - timedelta(days=300),
        last_purchase_at=NOW - timedelta(days=20),
        channels={Channel.EMAIL: ChannelStats(address_known=True, consent=True)},
    )
    key = "email|none|reminder"
    assert model.predict(state, key) != ClickResponseModel.predict(model, state, key)


def test_the_gym_cli_lists_agent_lookalike_next_to_agent() -> None:
    from autonomy.gym.run import POLICIES, agent_lookalike_factory

    assert "agent" in POLICIES and POLICIES["agent_lookalike"] is agent_lookalike_factory
    policy = _agent(agent_lookalike_factory(object()))  # type: ignore[arg-type]
    assert isinstance(policy.bundle.response, LookalikeClickModel)
    assert type(_agent(agent_factory(object())).bundle.response) is ClickResponseModel  # type: ignore[arg-type]


def test_autonomy_imports_neither_scikit_learn_nor_lightgbm() -> None:
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[3] / "autonomy"
    banned = {"sklearn", "lightgbm"}
    offenders = []
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(n.split(".")[0] in banned for n in names):
                offenders.append(str(path))
    assert offenders == []
