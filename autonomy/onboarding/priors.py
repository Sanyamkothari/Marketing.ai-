"""The documented starting points onboarding may use, each with where it is written down.

Onboarding sizes goals and proposes limits before the agent has sent anything, so some numbers have
to come from outside the shop's own data. Only the ones below may be used, and every proposal that
uses one names it (`CitedNumber.cite()`), so a person can check it. A number that is in neither the
shop's data nor this list is never shown.

* **Message lift** (`MESSAGE_LIFT`): one e-mail raised the chance of a purchase within 14 days by
  +0.45 points (95% range +0.23 to +0.67) over sending nothing, in the randomised Hillstrom e-mail
  test, measured on 32,000 customers no model had seen (`library/hillstrom-email/run_report.md`,
  "Off-policy value on the evaluation rows"; `docs/LIBRARY.md` section 7). The no-e-mail group bought
  at 0.57% (`MESSAGE_LIFT_BASE`). We use the conservative figure of that report (the chosen list),
  not the +0.69 points of the blanket men's e-mail, which was the better of the two e-mails tested.
* **Refill boost** (`refill_factor`): an open refill window raises a refill message's lift by up to
  +100%, shrunk towards none when the cycle model gives a low chance of a refill
  (`autonomy.decide.bidders.REFILL_LIFT` and `REFILL_P14_HALF`, from the catalogue backtest in
  `docs/v2/CATALOG.md`: an open window lifts the 14-day rebuy rate 1.5x to 2x).
* **Default margin** (`DEFAULT_MARGIN_RATE`): 40%, the margin the decision engine assumes for a
  customer with no margin data (`autonomy.decide.models.ValueModel.default_margin_rate`). Always
  flagged when used, and the shop is asked for its own once.
* **Message cost** (`CHANNEL_COSTS`): `autonomy.decide.bidders.DEFAULT_CHANNEL_COSTS` (an e-mail
  $0.001, an SMS $0.02).
* **Unsubscribe chance** (`UNSUBSCRIBE_PRIOR`): 0.5% of messages lose the contact before the shop
  has measured its own rate (`autonomy.decide.models.HAZARD_BASE_RATE`, docs/v2/CONTACT_LESS.md).
* **Starting limits**: two messages a week (`autonomy.mandate.FrequencyCap`), quiet from 21:00 to
  09:00 local (`autonomy.mandate.QuietHours`), and 10% of people never contacted so the proof has
  a comparison (`autonomy.contracts.Envelope.holdout_share`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from autonomy.contracts import Channel, Envelope
from autonomy.decide.bidders import DEFAULT_CHANNEL_COSTS, REFILL_LIFT, REFILL_P14_HALF
from autonomy.decide.models import HAZARD_BASE_RATE, ValueModel
from autonomy.mandate import FrequencyCap, QuietHours

__all__ = [
    "CHANNEL_COSTS",
    "DEFAULT_FREQUENCY",
    "DEFAULT_MARGIN_RATE",
    "DEFAULT_QUIET",
    "HOLDOUT_SHARE",
    "MESSAGE_LIFT",
    "MESSAGE_LIFT_BASE",
    "MESSAGE_LIFT_DAYS",
    "UNSUBSCRIBE_PRIOR",
    "CitedNumber",
    "lift_for",
    "rate_over",
    "refill_factor",
]


@dataclass(frozen=True)
class CitedNumber:
    """A documented number with its 95% range (equal to the point when it is a plain default)."""

    key: str
    point: float
    low: float
    high: float
    words: str
    source: str

    def cite(self) -> str:
        return f"{self.words} ({self.source})"


MESSAGE_LIFT: Final[CitedNumber] = CitedNumber(
    key="message_lift/email/14d",
    point=0.0045,
    low=0.0023,
    high=0.0067,
    words="One e-mail added 0.45 points (0.23 to 0.67) to the chance of a purchase within 14 days, "
    "against sending nothing, in the randomised Hillstrom e-mail test",
    source="library/hillstrom-email/run_report.md",
)
"""Absolute lift of one message over none, per person, in the 14 days after it."""

MESSAGE_LIFT_BASE: Final[float] = 0.0057
"""The no-e-mail group's purchase rate in that test (0.57%, same report). A shop's own base rate below
this caps the lift at the same relative size (0.45 / 0.57 = +79%), so a cold audience is never
credited more than the test measured relative to its own buying."""

DEFAULT_MARGIN_RATE: Final[float] = ValueModel().default_margin_rate
CHANNEL_COSTS: Final = DEFAULT_CHANNEL_COSTS
UNSUBSCRIBE_PRIOR: Final[float] = HAZARD_BASE_RATE
DEFAULT_FREQUENCY: Final[FrequencyCap] = FrequencyCap()
DEFAULT_QUIET: Final[QuietHours] = QuietHours()
HOLDOUT_SHARE: Final[float] = Envelope(channels=(Channel.EMAIL,), budget=0.0).holdout_share


def refill_factor(p14: float) -> float:
    """How much an open refill window raises a refill message's lift (`decide.bidders.refill_boost`)."""
    p = min(max(p14, 0.0), 1.0)
    return 1.0 + REFILL_LIFT * p / (p + REFILL_P14_HALF)


MESSAGE_LIFT_DAYS: Final[int] = 14
"""The window the test measured both its lift and its base rate over."""


def rate_over(rate: float, from_days: float, to_days: float) -> float:
    """A purchase rate measured over `from_days` restated over `to_days`, at a constant daily chance."""
    p = min(max(rate, 0.0), 1.0)
    if p >= 1.0 or from_days <= 0:
        return p
    return 1.0 - float((1.0 - p) ** (to_days / from_days))


def lift_for(base_rate: float, prior_lift: float, *, base_days: float = 30.0) -> float:
    """The lift one message is credited with for an audience whose own purchase rate over
    `base_days` is `base_rate`: the prior's absolute lift, but never more than the same relative
    rise the test measured on its own base (see `MESSAGE_LIFT_BASE`). The audience's rate is first
    restated over the test's 14 days, so the two bases are compared over the same window (a 30-day
    rate is about twice a 14-day one, and comparing them as they are would loosen the cap twofold)."""
    relative = prior_lift / MESSAGE_LIFT_BASE
    base14 = rate_over(base_rate, base_days, MESSAGE_LIFT_DAYS)
    return max(0.0, min(prior_lift, relative * base14))
