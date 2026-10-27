"""The two who probes and the learned-baseline rows' place in the report (docs/v4/BASELINES.md)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from autonomy.decide.bidders import contact_harm_of, leave_alone_retry
from autonomy.decide.who import WhoBook, WhoConfig
from autonomy.gym import default_brief, default_content
from autonomy.gym.run import POLICIES
from autonomy.runtime import ablation
from scripts.v2_realbench import common as C  # noqa: N812
from tests.v3.who.test_bidders import NOW, bidder, bids_of, state
from tests.v3.who.test_trait_harm import BACKGROUND, population

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
        "agent_who",
        "agent_who_notrait",
        "agent_who_noprune",
    ]
    assert {"agent_who", "agent_who_notrait", "agent_who_noprune"} <= set(ablation.names())


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


def _dog_book(*, prune_without_harm: bool) -> WhoBook:
    """A who book whose trait groups leave the customer "dog" alone (as in tests/v3/who/test_bidders.py)."""
    cfg = WhoConfig(
        models=False, risk_urgency=0.0, value_cost_share=None, prune_without_harm=prune_without_harm
    )
    book = WhoBook(cfg)
    records, keys = population({**BACKGROUND, "dogs|heavy": (60, 0.4), "neutral|heavy": (60, 1.0)})
    book.traits.fit(records, keys)
    book._keys.update({"dog": "dogs|heavy"})
    return book


def test_the_noprune_gate_skips_the_leave_alone_step_only_without_a_harm_model() -> None:
    """With who on and no harm model the default runs the leave-alone step (the dog gets no bid);
    prune_without_harm=False skips it, as before who existed (the dog is bid for)."""
    assert not leave_alone_retry("dog", NOW)
    plain = bidder(prune_z=2.0)
    assert plain.harm is None and contact_harm_of(plain.response_model) is None
    assert bids_of(plain, state("dog"))  # nothing else holds them back
    assert bids_of(bidder(_dog_book(prune_without_harm=True), prune_z=2.0), state("dog")) == []
    assert bids_of(bidder(_dog_book(prune_without_harm=False), prune_z=2.0), state("dog"))
