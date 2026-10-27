"""Discount code to offer: the one place that says which of the agent's offers a purchase used.

The agent names its offers with its own ids (`pct10`). A shop's checkout reports the code the
customer typed (`COMEBACK10`), and the Shopify connector puts that code in the purchase's
`attrs["offer_id"]` (all codes, comma-joined, in `attrs["discount_codes"]`). Matching the two by
string equality never works on a real shop, so each `OfferOption` names the exact `codes` or the
`code_prefixes` (for per-send unique codes such as `COMEBACK10-A7K2`) that identify it, and an
`OfferCodeTable` maps a purchase's attrs to offer ids through them. The spend ledger, the agent's
offer-spend tracking and the proof's rewards all read purchases through this table.

A token on a purchase is resolved in this order: the agent's own offer id (the Gym and any
connector that already reports it), an exact code, then the longest matching prefix. Codes and
prefixes ignore case, as Shopify's do. A token that matches no offer is an *unattributed discount*:
callers count it (`UnattributedDiscount`), they never drop it silently.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from autonomy.contracts.events import AttrValue
from autonomy.contracts.goals import NO_OFFER, OfferOption

__all__ = ["OfferCodeTable", "UnattributedBook", "UnattributedDiscount", "redemption_tokens"]


def redemption_tokens(attrs: Mapping[str, AttrValue]) -> tuple[str, ...]:
    """The discount tokens a purchase carries: its `offer_id`, then each code of `discount_codes`.

    "none" and empty values are not tokens. Order is kept and repeats are dropped."""
    found: list[str] = []
    offer = attrs.get("offer_id")
    if isinstance(offer, str):
        found.append(offer)
    codes = attrs.get("discount_codes")
    if isinstance(codes, str):
        found.extend(codes.split(","))
    out: dict[str, None] = {}
    for raw in found:
        token = raw.strip()
        if token and token != NO_OFFER.offer_id:
            out.setdefault(token)
    return tuple(out)


class OfferCodeTable:
    """Maps a purchase's discount tokens to the offer ids of one set of offers (one brief's envelope)."""

    def __init__(self, offers: Iterable[OfferOption]) -> None:
        self._ids: set[str] = set()
        self._codes: dict[str, str] = {}
        prefixes: list[tuple[str, str]] = []
        for o in offers:
            if o.offer_id == NO_OFFER.offer_id:
                continue
            self._ids.add(o.offer_id)
            for code in o.codes:
                self._codes.setdefault(code.casefold(), o.offer_id)
            prefixes.extend((p.casefold(), o.offer_id) for p in o.code_prefixes)
        # The longest prefix wins, so "COMEBACK10-" is not taken for "COMEBACK".
        self._prefixes: tuple[tuple[str, str], ...] = tuple(
            sorted(prefixes, key=lambda p: (-len(p[0]), p[0]))
        )

    def offer_for(self, token: str) -> str | None:
        """The offer id this token redeems, or None when no offer names it."""
        if token in self._ids:
            return token
        folded = token.casefold()
        found = self._codes.get(folded)
        if found is not None:
            return found
        for prefix, offer_id in self._prefixes:
            if folded.startswith(prefix):
                return offer_id
        return None

    def offers_for(self, attrs: Mapping[str, AttrValue]) -> tuple[str, ...]:
        """The offer ids a purchase redeemed, in token order, without repeats."""
        out: dict[str, None] = {}
        for token in redemption_tokens(attrs):
            offer_id = self.offer_for(token)
            if offer_id is not None:
                out.setdefault(offer_id)
        return tuple(out)

    def unmatched(self, attrs: Mapping[str, AttrValue]) -> tuple[str, ...]:
        """The tokens of a purchase that no offer in this table names."""
        return tuple(t for t in redemption_tokens(attrs) if self.offer_for(t) is None)


@dataclass(frozen=True)
class UnattributedDiscount:
    """Discount money taken by orders whose code matches none of the agent's offers.

    `code` is the code as the shop reported it (the first one seen, in its own case); `discount` is
    the sum of the orders' `attrs["discounts"]` where the shop reported it."""

    code: str
    orders: int
    discount: float


class UnattributedBook:
    """A running count of the unattributed discounts a reader has seen, one row per code (any case)."""

    def __init__(self) -> None:
        self._rows: dict[str, tuple[str, int, float]] = {}

    def add(self, code: str, attrs: Mapping[str, AttrValue]) -> None:
        """Count one order that used `code`, with the money the shop says it took off (`discounts`)."""
        money = attrs.get("discounts")
        taken = float(money) if isinstance(money, int | float) and not isinstance(money, bool) else 0.0
        first, orders, total = self._rows.get(code.casefold(), (code, 0, 0.0))
        self._rows[code.casefold()] = (first, orders + 1, total + max(taken, 0.0))

    def rows(self) -> list[UnattributedDiscount]:
        """Most orders first, then by code."""
        found = [
            UnattributedDiscount(code, orders, round(total, 2)) for code, orders, total in self._rows.values()
        ]
        return sorted(found, key=lambda r: (-r.orders, r.code.casefold()))

    def clear(self) -> None:
        self._rows.clear()
