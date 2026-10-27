"""Named ablation variants of the agent, for CI to find which mechanism costs money (docs/v3/DIAGNOSE.md).

Each variant is a thin factory over `build_agent_policy` that changes one thing from the plain agent or
from the v3 preset, so a Gym or real-bench run of it, read next to `agent` (or `agent_v3`), says what that
one thing does. Three families:

* **leave-one-out** (`v3_no_<part>`): the v3 preset with one part off. If `agent_v3` loses and
  `v3_no_journeys` does not, journeys are what hurt;
* **part alone** (`agent_<part>`): the plain agent with one v3 part on. If every part alone ties the plain
  agent but the preset loses, the harm is in how they combine;
* **guards** (`agent_no_<guard>`, `agent_loose_caps`): the plain agent with one v2 guard relaxed. If the
  agent gains when a guard goes, that guard is holding back money. Hard safety rules (consent, quiet hours,
  unsubscribes, holdout) are never relaxed by any variant.

The variants only choose which switches `build_agent_policy` is called with. Nothing here changes what the
plain build does. `agent` and `agent_v3` are in the registry too, so callers can resolve every name in one
place. `switches(policy)` reads the built policy's flags back, which is how the tests (and the DIAG block)
show that a variant differs from the plain agent in exactly the switch it names.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from autonomy.contracts import Brief, ContentItem
from autonomy.runtime.v3 import DEFAULT_OFFER_PRICING, V3Preset

if TYPE_CHECKING:
    from autonomy.runtime.agent import AgentPolicy

__all__ = [
    "BASELINE_NAMES",
    "LOOSE_CAP_FACTOR",
    "VARIANTS",
    "Variant",
    "build_variant",
    "changed_switches",
    "describe",
    "loosen_caps",
    "names",
    "switches",
    "uses_who",
    "variant_briefs",
]

LOOSE_CAP_FACTOR = 3
"""`agent_loose_caps` multiplies the brief's weekly contact cap by this."""

LEAVE_ONE_OUT = "leave_one_out"
PART_ALONE = "part_alone"
GUARD = "guard"
REFERENCE = "reference"


@dataclass(frozen=True)
class Variant:
    """One named build: the keyword arguments of `build_agent_policy` that make it, and what it tests."""

    name: str
    family: str
    tests: str
    """The hypothesis this variant tests, in a sentence."""
    kwargs: Callable[[], dict[str, Any]]
    """The keyword arguments for `build_agent_policy`, built on call so a variant imports its piece lazily."""
    cap_factor: int = 1
    """What the brief's weekly contact cap is multiplied by (1: the brief as it is). The cap is not only the
    hard rule: the agent sizes its weekly-budget rhythms and the attention price's lost-value horizon by it
    (`ContactPrice.cap`), and journeys are filtered by it, so all of these move with it."""


def _preset(**off: Any) -> Callable[[], dict[str, Any]]:
    return lambda: {"v3": V3Preset(**off)}


def _moments_book() -> dict[str, Any]:
    from autonomy.decide.moments import MomentBook

    return {"moments": MomentBook.load()}


def _variants() -> dict[str, Variant]:
    v = [
        Variant("agent", REFERENCE, "the plain v2 agent: what every variant is read against", dict),
        Variant(
            "agent_v3", REFERENCE, "every v3 part on together: the preset that loses", lambda: {"v3": True}
        ),
        # leave-one-out: the preset with one part off
        Variant(
            "v3_no_lookalike",
            LEAVE_ONE_OUT,
            "does the look-alike response model (and the profile in front of it) cost the preset money",
            _preset(lookalike=False),
        ),
        Variant(
            "v3_no_who",
            LEAVE_ONE_OUT,
            "does leaving risk, 90-day value and the trait-pooled leave-alone cost money",
            _preset(who=False),
        ),
        Variant(
            "v3_no_frequency",
            LEAVE_ONE_OUT,
            "do the 3-week contact plans (frequency_plan holds) and look-alike tiredness cost money",
            _preset(frequency=False),
        ),
        Variant(
            "v3_no_moments",
            LEAVE_ONE_OUT,
            "do moments (raised bids while a payday or festival is live) cost money",
            _preset(moments=False),
        ),
        Variant(
            "v3_no_journeys",
            LEAVE_ONE_OUT,
            "do the multi-channel journeys (holds between steps, never_read endings) cost money",
            _preset(journeys=False),
        ),
        Variant(
            "v3_no_offers",
            LEAVE_ONE_OUT,
            "does the budget price on offers hold offers back",
            _preset(offers=False),
        ),
        # part alone on the plain agent
        Variant(
            "agent_lookalike",
            PART_ALONE,
            "the look-alike response model alone",
            lambda: {"lookalike": True},
        ),
        Variant(
            "agent_who",
            PART_ALONE,
            "the profile and decision 1 (on top of look-alike): leaving risk, value, leave-alone",
            lambda: {"lookalike": True, "who": True},
        ),
        Variant(
            "agent_frequency",
            PART_ALONE,
            "look-alike tiredness and 3-week contact plans (on top of look-alike)",
            lambda: {"lookalike": True, "frequency": True},
        ),
        Variant(
            "agent_moments",
            PART_ALONE,
            "the shipped moments alone (inert in a Gym run without --moments: the simple folder keeps no carts)",
            _moments_book,
        ),
        Variant("agent_journeys", PART_ALONE, "the shipped journeys alone", lambda: {"journeys": True}),
        Variant(
            "agent_offers",
            PART_ALONE,
            "the budget price for offers alone",
            lambda: {"offer_pricing": DEFAULT_OFFER_PRICING},
        ),
        # too many rules? one v2 guard relaxed on the plain agent
        Variant(
            "agent_no_restraint",
            GUARD,
            "does the lower-confidence-bound rule keep out sends that would have paid",
            lambda: {"restraint": False},
        ),
        Variant(
            "agent_no_weekly_budget",
            GUARD,
            "does each customer's own weekly rhythm hold back sends that would have paid",
            lambda: {"weekly_budget": False},
        ),
        Variant(
            "agent_no_explore",
            GUARD,
            "does the exploration floor (random rests and sends for learning) cost more than it teaches",
            lambda: {"exploration_floor": 0.0},
        ),
        Variant(
            "agent_no_attention_price",
            GUARD,
            "is the unsubscribe-and-complaint price in the auction too high for what it protects",
            lambda: {"attention_price": False},
        ),
        Variant(
            "agent_loose_caps",
            GUARD,
            f"does the brief's weekly contact cap (x{LOOSE_CAP_FACTOR} here) bind, with everything sized by it: "
            "the hard rule, the weekly-budget rhythm menu, the attention price's horizon and the journeys' "
            "cap filter (read it next to agent_no_weekly_budget)",
            dict,
            cap_factor=LOOSE_CAP_FACTOR,
        ),
        Variant(
            "agent_no_guards",
            GUARD,
            "all four relaxed together (restraint, weekly budget, exploration floor, attention price): "
            "the ceiling of 'fewer rules'",
            lambda: {
                "restraint": False,
                "weekly_budget": False,
                "exploration_floor": 0.0,
                "attention_price": False,
            },
        ),
    ]
    return {x.name: x for x in v}


VARIANTS: dict[str, Variant] = _variants()
"""Every variant by name, in the order the DIAG docs and the CI matrices list them."""

BASELINE_NAMES: tuple[str, ...] = ("agent", "agent_v3")


def names(*, reference: bool = True) -> tuple[str, ...]:
    """Every variant name; without `reference`, only the ones that change something."""
    return tuple(n for n, v in VARIANTS.items() if reference or v.family != REFERENCE)


def loosen_caps(brief: Brief, factor: int) -> Brief:
    """`brief` with its weekly contact cap multiplied by `factor` (nothing else about it changes)."""
    if factor == 1:
        return brief
    env = brief.envelope
    return brief.model_copy(
        update={
            "envelope": env.model_copy(update={"max_contacts_per_week": env.max_contacts_per_week * factor})
        }
    )


def variant_briefs(name: str, briefs: Sequence[Brief]) -> list[Brief]:
    """The briefs a run of the variant uses: the given ones, with the variant's cap factor applied."""
    factor = VARIANTS[name].cap_factor
    return [loosen_caps(b, factor) for b in briefs]


def build_variant(
    name: str,
    briefs: Sequence[Brief],
    content: Sequence[ContentItem],
    **extra: Any,
) -> AgentPolicy:
    """The named variant for these briefs and this library. `extra` goes to `build_agent_policy` as it is
    (`costs=` for the bench, say) and must not name a switch the variant sets. The briefs are used as given:
    apply `variant_briefs` first where the cap factor matters (the Gym and the bench do)."""
    from autonomy.runtime.agent import build_agent_policy

    try:
        variant = VARIANTS[name]
    except KeyError as exc:
        raise KeyError(f"unknown variant {name!r}; known: {', '.join(VARIANTS)}") from exc
    set_here = variant.kwargs()
    clash = sorted(set(extra) & set(set_here))
    if clash:
        raise ValueError(f"variant {name!r} sets {', '.join(clash)} itself")
    return build_agent_policy(list(briefs), list(content), **set_here, **extra)


def uses_who(name: str) -> bool:
    """Whether the variant has the who book on (the bench then hands it every customer's pre-campaign events)."""
    kwargs = VARIANTS[name].kwargs()
    preset = kwargs.get("v3")
    if isinstance(preset, V3Preset):
        return bool(preset.who)
    return bool(preset) or bool(kwargs.get("who"))


def switches(policy: AgentPolicy) -> dict[str, object]:
    """The built policy's flags, one per switch a variant moves, read back from the policy itself.

    `weekly_cap` is the cap the agent read from its briefs (`ContactPrice.cap`): it sizes the weekly-budget
    rhythms and the attention price's horizon as well as the hard rule, which checks the run's briefs;
    `variant_briefs` is what raises it."""
    inner = policy.policy
    moments = policy.moments
    return {
        "lookalike": policy.outcomes is not None,
        "who": policy.who is not None,
        "frequency": policy.frequency is not None,
        "moments": moments is not None and moments is not False,
        "journeys": inner.journeys is not None,
        "offers": policy.offer_pricing is not None,
        "experiments": policy.experiment_policy is not None,
        "restraint": policy.restraint,
        "weekly_budget": inner.budget_fn is not None,
        "exploration_floor": inner.exploration_floor,
        "attention_price": inner.attention_price_fn is not None and inner.channel_price_fn is not None,
        "weekly_cap": policy.contact_price.cap(),
    }


def changed_switches(policy: AgentPolicy, plain: AgentPolicy) -> dict[str, object]:
    """The switches of `policy` that differ from `plain`'s, with the policy's values."""
    a, b = switches(policy), switches(plain)
    return {k: a[k] for k in a if a[k] != b[k]}


def describe(names_: Sequence[str] | None = None) -> Mapping[str, str]:
    """name -> what it tests, for the docs and `--list`."""
    return {n: VARIANTS[n].tests for n in (names_ or VARIANTS)}
