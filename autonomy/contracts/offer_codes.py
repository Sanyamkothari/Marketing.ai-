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
callers count it (`UnattributedDiscount`), they never drop it silently. That holds for every token of
an order, also when another code of the same order is known (`unknown_tokens`).

An envelope refuses codes that could be read two ways (`autonomy.contracts.goals.Envelope`): one code
or prefix on two offers, or a code that is another offer's id. A short prefix still takes every shop
code that starts with it (`W` takes `WELCOME15`); the exact codes of the offers are read first.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime

from autonomy.contracts.events import AttrValue, Event
from autonomy.contracts.goals import NO_OFFER, OfferOption

__all__ = [
    "OfferCodeTable",
    "UnattributedBook",
    "UnattributedDiscount",
    "redemption_tokens",
    "unknown_tokens",
]


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


def unknown_tokens(attrs: Mapping[str, AttrValue], tables: Iterable[OfferCodeTable]) -> tuple[str, ...]:
    """The tokens of a purchase that no table names: its unattributed discount codes.

    Every token is checked, so an order that used a known code and an unknown one still reports the
    unknown one."""
    found = tuple(tables)
    return tuple(t for t in redemption_tokens(attrs) if all(table.offer_for(t) is None for table in found))


@dataclass(frozen=True)
class UnattributedDiscount:
    """Discount money taken by orders whose code matches none of the agent's offers.

    `code` is the code as the shop reported it (the first one seen, in its own case). `discount` is
    the orders' `attrs["discounts"]` where the shop reported it. The shop reports one total per order,
    so an order with several codes gives each of its codes an equal share of that total (an order with
    COMEBACK10 and FRIEND20 and 12.00 off counts 6.00 for FRIEND20)."""

    code: str
    orders: int
    discount: float


class UnattributedBook:
    """The unattributed discounts a reader has seen: one entry per order and code, one row per code
    (any case). Adding the same order twice counts it once."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], tuple[datetime, str, float]] = {}

    def add(self, order: Event, unknown: Iterable[str]) -> None:
        """Count `order` for each of its `unknown` codes, with its equal share of the money the shop
        says it took off (`discounts`)."""
        tokens = redemption_tokens(order.attrs)
        if not tokens:
            return
        money = order.attrs.get("discounts")
        taken = float(money) if isinstance(money, int | float) and not isinstance(money, bool) else 0.0
        share = max(taken, 0.0) / len(tokens)
        for code in unknown:
            self._entries.setdefault((order.event_id, code.casefold()), (order.ts, code, share))

    def rows(self) -> list[UnattributedDiscount]:
        """Most orders first, then by code."""
        by_code: dict[str, tuple[str, int, float]] = {}
        for (_, folded), (_, code, share) in self._entries.items():
            first, orders, total = by_code.get(folded, (code, 0, 0.0))
            by_code[folded] = (first, orders + 1, total + share)
        found = [
            UnattributedDiscount(code, orders, round(total, 2)) for code, orders, total in by_code.values()
        ]
        return sorted(found, key=lambda r: (-r.orders, r.code.casefold()))

    def prune(self, before: datetime) -> None:
        """Forget the orders placed before `before`, so the book covers a window, not all time."""
        self._entries = {k: v for k, v in self._entries.items() if v[0] >= before}

    def clear(self) -> None:
        self._entries.clear()
