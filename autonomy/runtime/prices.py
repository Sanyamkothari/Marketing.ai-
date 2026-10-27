"""A shop's money: its currency, what one message costs in it, and the figures that scale with an order.

Every built-in money number of the agent is dollar-sized: a WhatsApp message costs 0.03
(`autonomy.decide.bidders.DEFAULT_CHANNEL_COSTS`), a complaint 2.0, a "high value" customer is worth 50
and an order is worth 50 until the shop's own purchases are learned. A rupee shop that ran on them
priced WhatsApp about 30 times too cheap, and the spend ledger and the overspend breaker used the same
table, so the budget the marketer signed was not enforced on WhatsApp (docs/v3/ARCHITECTURE_REVIEW.md
section 4 row 1; docs/v3/CURRENCY.md).

`ShopPrices` is the production runtime's one source for those numbers (`build_runtime` makes it from
`RuntimeSettings` and, where there is one, the mandate catalogue):

* **channel costs**: the catalogue's provider prices (`autonomy.mandate.catalogue.ChannelRate`, the
  cheapest provider per channel in the shop's currency) where it lists the channel, else
  `RuntimeSettings.channel_costs`. Push, web and webhook messages are free unless a price is given.
* **the usual order value** (`typical_order_value`, a per-shop setting): the order assumed before the
  shop's own purchases are learned, and the unit of the journeys' value bars.
* **the cost of a complaint**: `complaint_cost`, else `COMPLAINT_SHARE_OF_ORDER` of the usual order.

`check_goal` refuses a goal that cannot be priced: an error that says what is missing, never a silent
default. The Gym and the bench never build a `ShopPrices`; they pass their own costs to the agent
(`build_agent_policy(costs=...)`) and keep every number they had.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from autonomy.contracts import Brief, Channel, Goal

if TYPE_CHECKING:
    from autonomy.mandate.catalogue import CatalogueVersion

__all__ = [
    "COMPLAINT_SHARE_OF_ORDER",
    "FREE_CHANNELS",
    "PAID_CHANNELS",
    "MissingShopMoneyError",
    "ShopPrices",
    "money_prefix",
    "parse_channel_costs",
]

PAID_CHANNELS: frozenset[Channel] = frozenset({Channel.EMAIL, Channel.WHATSAPP, Channel.SMS})
"""Channels a shop pays a provider for. A goal that may use one needs its price."""
FREE_CHANNELS: frozenset[Channel] = frozenset(Channel) - PAID_CHANNELS
"""Push, web and webhook: free unless the shop says otherwise."""

COMPLAINT_SHARE_OF_ORDER = 0.04
"""What one complaint costs, as a share of the shop's usual order: 2.0 on the reference order of 50, the
figure the agent always used (`autonomy.runtime.agent.COMPLAINT_COST`). A shop that knows its own sets
`complaint_cost`."""

_ENV_ORDER = ", ".join(c.value for c in sorted(PAID_CHANNELS, key=lambda c: c.value))


class MissingShopMoneyError(ValueError):
    """A goal that cannot run because the shop's money is not set in its currency."""


_SYMBOLS: Mapping[str, str] = {"USD": "$", "INR": "₹", "EUR": "€", "GBP": "£", "JPY": "¥"}


def money_prefix(currency: str) -> str:
    """How money is written before the number in a decision's reason: "₹" for INR, "CHF " for a code with no symbol."""
    code = currency.upper()
    return _SYMBOLS.get(code, f"{code} ")


def parse_channel_costs(raw: str) -> dict[Channel, float]:
    """`email=0.12,whatsapp=0.85` as channel costs. Raises ValueError for anything else."""
    out: dict[Channel, float] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        name, sep, number = part.partition("=")
        if not sep:
            raise ValueError(f"a channel cost is channel=price, e.g. whatsapp=0.85, not {part!r}")
        try:
            channel = Channel(name.strip().lower())
            price = float(number)
        except ValueError as err:
            raise ValueError(
                f"a channel cost is channel=price (channels: {_ENV_ORDER}, ...), not {part!r}"
            ) from err
        if not math.isfinite(price) or price < 0:
            raise ValueError(f"the price of {channel.value} must be a number of zero or more, not {number!r}")
        out[channel] = price
    return out


@dataclass(frozen=True)
class ShopPrices:
    """The shop's currency and the money figures that depend on it. See the module docstring.

    `currency` None means "the catalogue's, when it lists exactly one"; with neither, no goal can run.
    `catalogue` is read at every use, so a version published later is priced from then on.
    """

    currency: str | None = None
    channel_costs: Mapping[Channel, float] = field(default_factory=dict)
    typical_order_value: float | None = None
    complaint_cost_override: float | None = None
    catalogue: Callable[[], CatalogueVersion | None] | None = None

    def __post_init__(self) -> None:
        for channel, price in self.channel_costs.items():
            if not math.isfinite(price) or price < 0:
                raise ValueError(f"the price of {channel.value} must be a number of zero or more")
        if self.typical_order_value is not None and not (
            math.isfinite(self.typical_order_value) and self.typical_order_value > 0
        ):
            raise ValueError("typical_order_value must be a positive number")
        if self.complaint_cost_override is not None and not (
            math.isfinite(self.complaint_cost_override) and self.complaint_cost_override >= 0
        ):
            raise ValueError("complaint_cost must be a number of zero or more")

    # --- the numbers ---------------------------------------------------------------------------

    def _version(self) -> CatalogueVersion | None:
        return self.catalogue() if self.catalogue is not None else None

    def shop_currency(self) -> str | None:
        """The shop's currency: the one set, else the single currency the catalogue prices messages in."""
        if self.currency is not None:
            return self.currency
        version = self._version()
        found = {rate.currency for rate in version.channels} if version is not None else set()
        return next(iter(found)) if len(found) == 1 else None

    def costs(self) -> dict[Channel, float]:
        """The price of one message per channel, in the shop's currency.

        Free channels cost 0; `channel_costs` prices the rest; the catalogue's cheapest rate in the
        shop's currency replaces a channel's price where it lists one. A paid channel with no price is
        absent from the result (never defaulted)."""
        currency = self.shop_currency()
        out: dict[Channel, float] = dict.fromkeys(FREE_CHANNELS, 0.0)
        if currency is None:
            return out
        out.update(self.channel_costs)
        version = self._version()
        if version is not None:
            for channel in Channel:
                rate = version.channel_rate(channel, currency=currency)
                if rate is not None:
                    out[channel] = rate.unit_cost
        return out

    def complaint_cost(self) -> float | None:
        """What one complaint costs in the shop's currency; None until the usual order value is known."""
        if self.complaint_cost_override is not None:
            return self.complaint_cost_override
        if self.typical_order_value is None:
            return None
        return COMPLAINT_SHARE_OF_ORDER * self.typical_order_value

    # --- refusing what cannot be priced --------------------------------------------------------

    def problems(self, goal: Goal, brief: Brief | None = None) -> list[str]:
        """Why this goal cannot be priced, in plain words (empty when it can)."""
        currency = self.shop_currency()
        out: list[str] = []
        if currency is None:
            out.append(
                "the shop's currency is not set (AUTONOMY_CURRENCY, e.g. INR) and the catalogue does not "
                "name a single one"
            )
        elif goal.currency.upper() != currency:
            out.append(f"the goal is in {goal.currency} but the shop's prices are in {currency}")
        else:
            table = self.costs()
            wanted = brief.envelope.channels if brief is not None else tuple(PAID_CHANNELS)
            missing = sorted({c.value for c in wanted if c in PAID_CHANNELS and c not in table})
            if missing:
                out.append(
                    f"no {currency} price for {', '.join(missing)}: set AUTONOMY_CHANNEL_COSTS "
                    f"(e.g. whatsapp=0.85) or publish the provider rates in the action catalogue"
                )
        if self.typical_order_value is None:
            out.append(
                "the shop's usual order value is not set (AUTONOMY_TYPICAL_ORDER_VALUE, in the shop's "
                "currency): the order value assumed for new customers and the value bars of the journeys "
                "are set from it"
            )
        return out

    def check_goal(self, goal: Goal, brief: Brief | None = None) -> None:
        """Raise `MissingShopMoneyError` if this goal cannot be priced in the shop's currency."""
        found = self.problems(goal, brief)
        if found:
            raise MissingShopMoneyError(
                f"goal {goal.goal_id} cannot run until the shop's money is set: " + "; ".join(found) + "."
            )
