"""The two who probes and the learned-baseline rows' place in the report (docs/v4/BASELINES.md)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from autonomy.decide.who import WhoConfig
from autonomy.gym import default_brief, default_content
from autonomy.gym.run import POLICIES
from autonomy.runtime import ablation
from scripts.v2_realbench import common as C  # noqa: N812

ROOT = Path(__file__).resolve().parents[3]


def _diff(name: str) -> dict[str, object]:
    def build(n: str) -> object:
        return ablation.build_variant(n, ablation.variant_briefs(n, [default_brief()]), default_content())

    return ablation.changed_switches(build(name), build("agent"))  # type: ignore[arg-type]


def test_the_defaults_are_what_every_earlier_run_used() -> None:
    cfg = WhoConfig()
    assert cfg.trait_harm is True and cfg.prune_without_harm is True


@pytest.mark.parametrize(
    ("name", "extra"),
    [
        ("agent_who_notrait", {"who_trait_harm": False}),
        ("agent_who_noprune", {"who_prune_without_harm": False}),
    ],
)
def test_a_probe_is_agent_who_with_exactly_one_sub_switch_off(name: str, extra: dict[str, object]) -> None:
    assert _diff(name) == {"lookalike": True, "who": True, **extra}
    assert _diff("agent_who") == {"lookalike": True, "who": True}
    assert ablation.uses_who(name)
    assert name in POLICIES  # the Gym takes it too


def test_the_bench_and_ci_take_both_probes() -> None:
    text = (ROOT / ".github" / "workflows" / "realbench.yml").read_text()
    [found] = re.findall(r"policy: \[([^\]]*)\]", text)
    assert [p.strip() for p in found.split(",")] == [
        "agent",
        "agent_v3",
        "agent_who_notrait",
        "agent_who_noprune",
    ]
    assert "agent_who_notrait" in ablation.names() and "agent_who_noprune" in ablation.names()


def test_learned_baselines_are_outside_the_holm_family_and_best_simple() -> None:
    est = {"point": 0.1, "low": 0.0, "high": 0.2}
    block = {
        "policies": {
            "send to none": {"agent_minus_this": est},
            C.LEARNED_BASELINES[0]: {"agent_minus_this": est},
            C.LEARNED_BASELINES[1]: {"agent_minus_this": est},
            "AGENT": {"agent_minus_this": est},
        }
    }
    assert list(C.agent_comparisons(block)) == ["agent minus send to none"]
