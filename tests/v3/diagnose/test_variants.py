"""The ablation variants (autonomy/runtime/ablation.py, docs/v3/DIAGNOSE.md): each builds, and differs from the
plain agent only in the one switch it is named for. The default build is unchanged."""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import pytest

from autonomy.gym import default_brief, default_content
from autonomy.gym.run import POLICIES
from autonomy.runtime import ablation
from autonomy.runtime.agent import AgentPolicy, build_agent_policy

ROOT = Path(__file__).resolve().parents[3]
V3_PARTS = ("lookalike", "who", "frequency", "moments", "journeys", "offers")
V3_ALL = (*V3_PARTS, "experiments")


def _build(name: str) -> AgentPolicy:
    return ablation.build_variant(name, ablation.variant_briefs(name, [default_brief()]), default_content())


def _diff(name: str) -> dict[str, object]:
    return ablation.changed_switches(_build(name), _build("agent"))


def test_every_variant_builds() -> None:
    for name in ablation.names():
        assert isinstance(_build(name), AgentPolicy), name
    assert set(ablation.names()) >= {"agent", "agent_v3"}
    assert len(ablation.names(reference=False)) == len(ablation.names()) - 2


def test_the_plain_agent_variant_is_the_default_build() -> None:
    plain = build_agent_policy([default_brief()], default_content())
    assert ablation.switches(plain) == ablation.switches(_build("agent"))
    assert ablation.switches(plain) == {
        "lookalike": False,
        "who": False,
        "frequency": False,
        "moments": False,
        "journeys": False,
        "offers": False,
        "experiments": False,
        "restraint": True,
        "weekly_budget": True,
        "exploration_floor": None,
        "attention_price": True,
        "weekly_cap": default_brief().envelope.max_contacts_per_week,
    }
    assert inspect.signature(build_agent_policy).parameters["attention_price"].default is True
    assert inspect.signature(AgentPolicy.__init__).parameters["attention_price"].default is True
    assert plain.policy.attention_price_fn is not None and plain.policy.channel_price_fn is not None


def test_the_v3_variant_is_the_preset_and_has_every_part_on() -> None:
    assert _diff("agent_v3") == dict.fromkeys(V3_ALL, True)
    preset = build_agent_policy([default_brief()], default_content(), v3=True)
    assert ablation.switches(preset) == ablation.switches(_build("agent_v3"))


@pytest.mark.parametrize("part", V3_PARTS)
def test_leave_one_out_is_the_preset_with_that_one_part_off(part: str) -> None:
    name = f"v3_no_{part}"
    assert name in ablation.VARIANTS
    v3, loo = ablation.switches(_build("agent_v3")), ablation.switches(_build(name))
    assert {k for k in v3 if v3[k] != loo[k]} == {part}
    assert loo[part] is False


@pytest.mark.parametrize(
    ("name", "on"),
    [
        ("agent_lookalike", {"lookalike"}),
        ("agent_who", {"lookalike", "who"}),  # who sits on the look-alike model, as the existing Gym policy
        ("agent_frequency", {"lookalike", "frequency"}),  # so does frequency
        ("agent_moments", {"moments"}),
        ("agent_journeys", {"journeys"}),
        ("agent_offers", {"offers"}),
    ],
)
def test_part_alone_turns_on_that_part_and_nothing_else(name: str, on: set[str]) -> None:
    assert _diff(name) == dict.fromkeys(on, True)


@pytest.mark.parametrize(
    ("name", "changed"),
    [
        ("agent_no_restraint", {"restraint": False}),
        ("agent_no_weekly_budget", {"weekly_budget": False}),
        ("agent_no_explore", {"exploration_floor": 0.0}),
        ("agent_no_attention_price", {"attention_price": False}),
        (
            "agent_no_guards",
            {"restraint": False, "weekly_budget": False, "exploration_floor": 0.0, "attention_price": False},
        ),
    ],
)
def test_a_guard_variant_relaxes_only_its_guard(name: str, changed: dict[str, object]) -> None:
    assert _diff(name) == changed


def test_loose_caps_raises_the_cap_and_what_the_agent_sizes_by_it() -> None:
    """The cap is not only the hard rule: the agent's `ContactPrice` reads it too, so the weekly-budget rhythm
    menu and the attention price's horizon move with it (docs/v3/DIAGNOSE.md). No other flag moves."""
    plain, loose = _build("agent"), _build("agent_loose_caps")
    assert loose.contact_price.cap() == 3 * plain.contact_price.cap()
    assert _diff("agent_loose_caps") == {"weekly_cap": 3 * plain.contact_price.cap()}
    [brief] = ablation.variant_briefs("agent_loose_caps", [default_brief()])
    assert brief.envelope.max_contacts_per_week == default_brief().envelope.max_contacts_per_week * 3
    assert brief.model_copy(update={"envelope": default_brief().envelope}) == default_brief()
    [same] = ablation.variant_briefs("agent", [default_brief()])
    assert same == default_brief()


def test_hard_safety_rules_are_never_a_switch() -> None:
    allowed = set(ablation.switches(_build("agent"))) | {
        "weekly_contact_cap",
        "who_trait_harm",
        "who_prune_without_harm",
    }
    for name in ablation.names():
        assert set(_diff(name)) <= allowed
    briefs = ablation.variant_briefs("agent_no_guards", [default_brief()])
    env, base = briefs[0].envelope, default_brief().envelope
    for field in ("quiet_start_hour", "quiet_end_hour", "holdout_share", "channels", "budget"):
        assert getattr(env, field) == getattr(base, field)


def test_attention_price_off_takes_both_price_hooks_out_of_the_auction() -> None:
    inner = _build("agent_no_attention_price").policy
    assert inner.attention_price_fn is None and inner.channel_price_fn is None
    assert inner.budget_fn is not None and inner.margin_fn is not None  # the other guards stay


def test_unknown_variant_and_clashing_switch_are_refused() -> None:
    with pytest.raises(KeyError):
        ablation.build_variant("nope", [default_brief()], default_content())
    with pytest.raises(ValueError, match="sets restraint"):
        ablation.build_variant("agent_no_restraint", [default_brief()], default_content(), restraint=True)


def test_the_gym_registers_every_variant_and_keeps_the_existing_factories() -> None:
    from autonomy.gym import run as gym_run

    for name in ablation.names():
        assert name in POLICIES, name
    assert POLICIES["agent_v3"] is gym_run.agent_v3_factory
    assert POLICIES["agent_lookalike"] is gym_run.agent_lookalike_factory
    assert POLICIES["agent_who"] is gym_run.agent_who_factory
    assert POLICIES["agent_frequency"] is gym_run.agent_frequency_factory
    assert POLICIES["agent"] is gym_run.agent_factory


def test_the_bench_takes_every_variant() -> None:
    text = (ROOT / "scripts" / "v2_realbench" / "run.py").read_text()
    assert "choices=(*ablation.names(), " in text and "variant=args.policy" in text


def test_the_diagnose_workflow_lists_every_variant_on_both_matrices() -> None:
    text = (ROOT / ".github" / "workflows" / "diagnose.yml").read_text()
    lists = re.findall(r"policy: \[([^\]]*)\]", text)
    assert len(lists) == 2  # the Gym matrix and the bench matrix
    for found in lists:
        # the two who probes (docs/v4/BASELINES.md) run in realbench.yml, not in the v3 diagnose matrices
        probes = {n for n, v in ablation.VARIANTS.items() if v.family == ablation.WHO_PROBE}
        assert [p.strip() for p in found.split(",")] == [n for n in ablation.names() if n not in probes]
    assert re.search(
        r"scenario: \[baseline_shop, whatsapp_fatigue, discount_cannibalisation, sleeping_dogs_15\]", text
    )
    assert "dataset: [hillstrom, x5]" in text
    assert "[diagnose]" in text and "workflow_dispatch" in text and 'branches: ["claude/**"]' in text
    assert "fail-fast: false" in text and text.count("continue-on-error: true") == 2
    assert text.count("timeout-minutes: 120") == 2
    assert "--seeds 3 --customers 3000" in text
    assert "group: diagnose-${{ github.ref }}-${{ github.sha }}" in text


def test_the_new_modules_import_only_what_the_v2_job_installs() -> None:
    for module in ("ablation.py", "diagnostics.py"):
        tree = ast.parse((ROOT / "autonomy" / "runtime" / module).read_text())
        roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                roots.add((node.module or "").split(".")[0])
        assert roots <= {"__future__", "collections", "dataclasses", "typing", "autonomy"}, module
