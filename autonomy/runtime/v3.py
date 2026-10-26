"""The plan-v3 preset: one switch that turns on every v3 piece of the agent together (docs/v3/PRESET.md).

Each v3 piece is opt-in on `build_agent_policy`. `V3Preset` names them all, so a deployment, the Gym and the
real bench can ask for "v3" as a whole and be compared with the plain v2 agent:

* `lookalike`: the look-alike response model (docs/v3/LOOKALIKE.md);
* `who`: leaving risk, 90-day value and sleeping dogs by traits, which also puts the profile in front of the
  look-alike model and the tiredness model (docs/v3/WHO.md);
* `frequency`: tiredness from look-alikes and 3-week contact plans (docs/v3/FREQUENCY.md);
* `moments`: the shipped `MomentBook` (docs/v3/MOMENTS.md), with `moments_country` as the shop's calendar;
* `journeys`: the shipped journeys (docs/v3/JOURNEYS.md);
* `offers`: the budget price for offers (docs/v3/OFFERS.md);
* `experiments`: the `ExperimentPolicy` wrapper with a book that has no experiment running, so it changes
  nothing until one is started (docs/v3/EXPERIMENTS.md).

Each field is True (the piece with its defaults), False (off) or the piece's own config. Switching one off
here leaves the others on, which is how a part is measured alone. Passing the same piece to
`build_agent_policy` by name (`moments=`, `journeys=`, ...) wins over the preset's. With `v3=False`, the
default, nothing here runs and every number is unchanged.

The order in which the pieces compose is fixed by the code they hook into and is written out in
docs/v3/PRESET.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from autonomy.decide.frequency import FrequencyConfig
from autonomy.decide.journeys import JourneyConfig, JourneyRunner, load_journeys
from autonomy.decide.lookalike import LookalikeConfig
from autonomy.decide.pacing import OfferPricing
from autonomy.decide.who import WhoBook, WhoConfig

if TYPE_CHECKING:
    from autonomy.decide.moments import MomentBook
    from autonomy.experiments.book import ExperimentBook

__all__ = ["DEFAULT_OFFER_PRICING", "V3Parts", "V3Preset", "resolve_v3"]

DEFAULT_OFFER_PRICING = OfferPricing(initial=0.0, renew=True)
"""The preset's budget price for offers. It starts at 0, which prices offers at what they cost: the price rises
only when a day's offer spend is above its target, so a budget that does not bind changes nothing, where the
module default of 0.5 would hold offers back for the first week. The mandate's own money rule is the
backstop for the first days of a budget that does bind. `renew` plans another period when the first has run
and budget is left (without it every offer is priced at the ceiling from day 31 on). Design defaults, not
measurements."""


@dataclass(frozen=True)
class V3Preset:
    """Which v3 pieces `build_agent_policy(..., v3=...)` turns on. All on by default."""

    lookalike: bool | LookalikeConfig = True
    who: bool | WhoConfig = True
    frequency: bool | FrequencyConfig = True
    moments: bool = True
    moments_country: str | None = None
    """The shop's country, for customers whose file names none (`MomentBook.load(default_country=...)`).
    None: such customers get no payday or festival moment."""
    journeys: bool | JourneyConfig = True
    offers: bool | OfferPricing = True
    experiments: bool | ExperimentBook = True


@dataclass(frozen=True)
class V3Parts:
    """A preset turned into the values `AgentPolicy` takes; each None or False is a part that is off."""

    lookalike: bool | LookalikeConfig
    who: bool | WhoConfig | WhoBook
    frequency: bool | FrequencyConfig
    moments: MomentBook | Literal[False] | None
    journeys: JourneyRunner | None
    offer_pricing: OfferPricing | None
    experiments: ExperimentBook | None


def resolve_v3(
    v3: bool | V3Preset,
    *,
    lookalike: bool | LookalikeConfig = False,
    who: bool | WhoConfig | WhoBook = False,
    frequency: bool | FrequencyConfig = False,
    moments: MomentBook | Literal[False] | None = None,
    journeys: bool | JourneyConfig | JourneyRunner = False,
    offer_pricing: OfferPricing | None = None,
) -> V3Parts:
    """What `build_agent_policy` builds for `v3` and the pieces it was also given by name.

    A piece given by name wins over the preset's: `moments=False` keeps moments off, a `journeys=` runner is
    used as it is. A piece left at its default (the off value) takes the preset's. With `v3=False` the
    pieces given by name are used as they are and the preset adds nothing."""
    preset = v3 if isinstance(v3, V3Preset) else V3Preset() if v3 else None
    if preset is None:
        return V3Parts(lookalike, who, frequency, moments, _runner(journeys), offer_pricing, None)
    if moments is None:  # the preset has a say either way: off is False, which beats `use_moments`' default
        if preset.moments:
            from autonomy.decide.moments import MomentBook

            moments = MomentBook.load(default_country=preset.moments_country)
        else:
            moments = False
    if offer_pricing is None and preset.offers:
        offer_pricing = preset.offers if isinstance(preset.offers, OfferPricing) else DEFAULT_OFFER_PRICING
    experiments: ExperimentBook | None = None
    if preset.experiments:
        from autonomy.experiments.book import ExperimentBook

        experiments = (
            preset.experiments if isinstance(preset.experiments, ExperimentBook) else ExperimentBook()
        )
    return V3Parts(
        lookalike=lookalike or preset.lookalike,
        who=who or preset.who,
        frequency=frequency or preset.frequency,
        moments=moments,
        journeys=_runner(journeys or preset.journeys),
        offer_pricing=offer_pricing,
        experiments=experiments,
    )


def _runner(journeys: bool | JourneyConfig | JourneyRunner) -> JourneyRunner | None:
    """A runner for `journeys`: True is the shipped `configs/v3/journeys.yaml`, False is none."""
    if isinstance(journeys, JourneyRunner):
        return journeys
    if isinstance(journeys, JourneyConfig):
        return JourneyRunner(journeys)
    return JourneyRunner(load_journeys()) if journeys else None
