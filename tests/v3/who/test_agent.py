"""The agent with the who switch: off by default and identical then; on, it shares one book."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from autonomy.decide.who import WhoBook, WhoConfig
from autonomy.gym import default_brief, default_content, get_scenario
from autonomy.gym.run import POLICIES, agent_factory, agent_who_factory
from autonomy.gym.score import run_policy
from autonomy.runtime.agent import AgentPolicy, LookalikeClickModel, build_agent_policy


def _agent(policy: object) -> AgentPolicy:
    assert isinstance(policy, AgentPolicy)
    return policy


def test_the_default_agent_has_no_who_book_and_its_bidders_ignore_scores():
    policy = build_agent_policy([default_brief()], default_content())
    assert policy.who is None
    assert all(b.who is None for b in policy.bidders.values())
    off = build_agent_policy([default_brief()], default_content(), who=False)
    assert off.who is None


def test_the_switch_builds_one_book_shared_by_every_bidder():
    policy = build_agent_policy([default_brief()], default_content(), who=True)
    assert isinstance(policy.who, WhoBook)
    assert all(b.who is policy.who for b in policy.bidders.values())
    config = WhoConfig(risk_urgency=1.0, value_cost_share=None)
    custom = build_agent_policy([default_brief()], default_content(), who=config)
    assert custom.who is not None and custom.who.config is config
    shared = WhoBook()
    again = build_agent_policy([default_brief()], default_content(), who=shared)
    assert again.who is shared


def test_the_default_gym_run_is_the_same_with_the_switch_spelled_off():
    spec = get_scenario("smoke", n_customers=120, days=7, history_days=30)

    def factory(world: object) -> AgentPolicy:
        return build_agent_policy([default_brief()], default_content(), who=False)

    plain = run_policy(spec, agent_factory, 0, with_oracle=False)
    off = run_policy(spec, factory, 0, with_oracle=False)
    assert plain.model_dump() == off.model_dump()


def test_the_gym_lists_agent_who_next_to_the_others():
    assert "agent" in POLICIES and POLICIES["agent_who"] is agent_who_factory
    policy = _agent(agent_who_factory(object()))  # type: ignore[arg-type]
    assert isinstance(policy.bundle.response, LookalikeClickModel)
    assert policy.who is not None
    assert _agent(agent_factory(object())).who is None  # type: ignore[arg-type]


@pytest.mark.slow
def test_the_who_agent_runs_in_the_gym_reads_the_history_and_repeats():
    spec = get_scenario("smoke", n_customers=200, days=20, history_days=210)
    built: list[AgentPolicy] = []

    def factory(world: object) -> AgentPolicy:
        policy = build_agent_policy([default_brief()], default_content(), lookalike=True, who=True)
        built.append(policy)
        return policy

    card = run_policy(spec, factory, 0, with_oracle=False)
    again = run_policy(spec, factory, 0, with_oracle=False)
    assert card.violations == 0
    assert card.model_dump() == again.model_dump()
    book = built[0].who
    assert book is not None and len(book.events) > 100  # the history was handed over and kept
    assert book.models.fitted_as_of is not None
    assert book.models.risk_check.reason and book.models.value_check.reason  # each says why
    model = built[0].bundle.response
    assert isinstance(model, LookalikeClickModel)
    assert model.lookalike.rows > 0 and model.lookalike.refused == 0  # the filled profile is point in time


def test_nothing_in_the_who_code_imports_more_than_numpy():
    root = Path(__file__).resolve().parents[3] / "autonomy" / "decide"
    banned = {"sklearn", "lightgbm", "scipy", "pandas", "autogluon"}
    for name in ("slow_models.py", "who.py"):
        for node in ast.walk(ast.parse((root / name).read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            assert not {n.split(".")[0] for n in names} & banned, name
