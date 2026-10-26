"""The Action Catalogue: a version-controlled list of everything the agent is allowed to reach for.

A mandate says *how much* the agent may do (budget, margin floor, quiet hours, caps). The catalogue
says *with what*: the offers it may make, the channels it may use and what each costs, and the
provider templates that were approved for sending. Both have to hold for an action to go out.

Why versions: an auditor (or a CFO) asks "what was the agent allowed to send on 3 March?". So the
catalogue is never edited in place. `publish()` writes version N+1 next to the old ones, records
what changed (`diff_from_previous`) and chains each version to the one before by hash, so a quietly
rewritten history is detectable (`verify_history()`). A mandate can pin the version it was certified
against, and the runtime checks actions against exactly that version.

Two stores ship here: `InMemoryCatalogueStore` for tests and the Gym, and `JsonDirCatalogueStore`,
one JSON file per version, written once and never overwritten. Time is always passed in.
"""

from __future__ import annotations

import hashlib
import os
import threading
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

from pydantic import AwareDatetime, BaseModel, Field, model_validator

from autonomy.contracts import NO_OFFER, Action, Channel, Contract, OfferKind, OfferOption
from autonomy.mandate.signing import GENESIS_HASH, canonical_bytes

ChangeKind = Literal["offer", "channel", "template"]
ChangeType = Literal["added", "removed", "changed"]


class CatalogueError(ValueError):
    """A catalogue request that cannot be honoured (unknown version, duplicate id, nothing to publish)."""


class ChannelRate(Contract):
    """What one message on a channel costs through one provider, e.g. WhatsApp via Gupshup at 0.78 INR."""

    channel: Channel
    unit_cost: float = Field(ge=0, allow_inf_nan=False)
    currency: str = Field(pattern=r"^[A-Z]{3}$", description="ISO code, e.g. INR")
    provider: str = Field(min_length=1)

    @property
    def key(self) -> str:
        return f"{self.channel.value}:{self.provider}"


class CatalogueTemplate(Contract):
    """A provider-side template that passed review (WhatsApp templates must be pre-approved by Meta)."""

    template_id: str = Field(min_length=1)
    channel: Channel
    provider_template_name: str = Field(min_length=1)
    approved_at: AwareDatetime
    locale: str = Field(default="en", min_length=2)


class CatalogueChange(Contract):
    """One line of a diff: which entry, and whether it was added, removed or changed (and which fields)."""

    kind: ChangeKind
    key: str
    change: ChangeType
    fields: tuple[str, ...] = Field(default=(), description="For 'changed': the fields that differ")


class CatalogueDiff(Contract):
    """What changed between two catalogue versions. `from_version` None means "from nothing"."""

    from_version: int | None
    to_version: int
    changes: tuple[CatalogueChange, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.changes

    def summary(self) -> str:
        """One plain sentence, e.g. 'v2 -> v3: 1 offer added, 1 template removed.'"""
        start = "nothing" if self.from_version is None else f"v{self.from_version}"
        if not self.changes:
            return f"{start} -> v{self.to_version}: no changes."
        counts: dict[tuple[str, str], int] = {}
        for c in self.changes:
            counts[(c.kind, c.change)] = counts.get((c.kind, c.change), 0) + 1
        parts = [f"{n} {kind}{'' if n == 1 else 's'} {change}" for (kind, change), n in counts.items()]
        return f"{start} -> v{self.to_version}: {', '.join(parts)}."


class CatalogueContent(Contract):
    """The publishable part of a catalogue: offers, channel rates and approved templates."""

    offers: tuple[OfferOption, ...] = ()
    channels: tuple[ChannelRate, ...] = ()
    templates: tuple[CatalogueTemplate, ...] = ()

    @model_validator(mode="after")
    def _unique(self) -> CatalogueContent:
        _no_duplicates("offer", [o.offer_id for o in self.offers])
        _no_duplicates("channel rate", [c.key for c in self.channels])
        _no_duplicates("template", [t.template_id for t in self.templates])
        return self


class CatalogueVersion(Contract):
    """One immutable published version. `content_hash` covers every field except itself."""

    version: int = Field(ge=1)
    created_at: AwareDatetime
    created_by: str = Field(min_length=1)
    offers: tuple[OfferOption, ...] = ()
    channels: tuple[ChannelRate, ...] = ()
    templates: tuple[CatalogueTemplate, ...] = ()
    diff_from_previous: CatalogueDiff
    previous_hash: str = Field(default=GENESIS_HASH, description="content_hash of version - 1")
    content_hash: str = ""

    @property
    def content(self) -> CatalogueContent:
        return CatalogueContent(offers=self.offers, channels=self.channels, templates=self.templates)

    def offer(self, offer_id: str) -> OfferOption | None:
        return next((o for o in self.offers if o.offer_id == offer_id), None)

    def template(self, template_id: str) -> CatalogueTemplate | None:
        return next((t for t in self.templates if t.template_id == template_id), None)

    def channel_rate(
        self, channel: Channel, provider: str | None = None, *, currency: str | None = None
    ) -> ChannelRate | None:
        """The rate for a channel; with no provider, the cheapest one listed (what the agent would pick).

        With `currency`, only rates in that currency count: a cheaper rate in another currency is
        not comparable, and must never be counted against a budget in this one.
        """
        rates = [
            c
            for c in self.channels
            if c.channel is channel and provider in (None, c.provider) and currency in (None, c.currency)
        ]
        return min(rates, key=lambda c: (c.unit_cost, c.provider)) if rates else None

    def unknown_templates(self, template_ids: Iterable[str]) -> tuple[str, ...]:
        """The ids a mandate names that this version does not hold, in the order given."""
        known = {t.template_id for t in self.templates}
        return tuple(dict.fromkeys(t for t in template_ids if t not in known))


def compute_hash(version: CatalogueVersion) -> str:
    """sha256 over canonical JSON of the version without its own hash.

    Uses the same canonical form as mandate hashes (`autonomy.mandate.signing.canonical_json`), so
    there is one spelling of "canonical" in this package.
    """
    body = version.model_dump(mode="json", exclude={"content_hash"})
    return hashlib.sha256(canonical_bytes(body)).hexdigest()


def diff_content(
    old: CatalogueContent | None, new: CatalogueContent, *, from_version: int | None, to_version: int
) -> CatalogueDiff:
    """Compare two catalogues entry by entry (offers by id, rates by channel:provider, templates by id)."""
    base = old or CatalogueContent()
    changes = [
        *_diff_keyed("offer", {o.offer_id: o for o in base.offers}, {o.offer_id: o for o in new.offers}),
        *_diff_keyed("channel", {c.key: c for c in base.channels}, {c.key: c for c in new.channels}),
        *_diff_keyed(
            "template",
            {t.template_id: t for t in base.templates},
            {t.template_id: t for t in new.templates},
        ),
    ]
    return CatalogueDiff(from_version=from_version, to_version=to_version, changes=tuple(changes))


# --- stores ---------------------------------------------------------------------------------------


class CatalogueStore(Protocol):
    """Where versions live. Append-only: there is no update or delete."""

    def versions(self) -> tuple[CatalogueVersion, ...]:
        """Every version, oldest first."""
        ...

    def append(self, version: CatalogueVersion) -> None:
        """Store a new version; refuse one whose number is already taken."""
        ...


class InMemoryCatalogueStore:
    """Versions in a list. For tests, the Gym and the API's default in-memory state."""

    def __init__(self) -> None:
        self._versions: list[CatalogueVersion] = []

    def versions(self) -> tuple[CatalogueVersion, ...]:
        return tuple(self._versions)

    def append(self, version: CatalogueVersion) -> None:
        if any(v.version == version.version for v in self._versions):
            raise CatalogueError(f"catalogue version {version.version} already exists")
        self._versions.append(version)


class JsonDirCatalogueStore:
    """One `catalogue_v000001.json` file per version in a folder.

    A file is written to a temporary name and then hard-linked into place, which fails if the name is
    taken, so a version is written exactly once and a crash never leaves a half-written version.
    """

    PATTERN = "catalogue_v*.json"

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.folder.mkdir(parents=True, exist_ok=True)

    def _path(self, version: int) -> Path:
        return self.folder / f"catalogue_v{version:06d}.json"

    def versions(self) -> tuple[CatalogueVersion, ...]:
        found = [
            CatalogueVersion.model_validate_json(p.read_text(encoding="utf-8"))
            for p in sorted(self.folder.glob(self.PATTERN))
        ]
        return tuple(sorted(found, key=lambda v: v.version))

    def append(self, version: CatalogueVersion) -> None:
        final = self._path(version.version)
        tmp = self.folder / f".{final.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        tmp.write_text(version.model_dump_json(indent=2) + "\n", encoding="utf-8")
        try:
            os.link(tmp, final)
        except FileExistsError as exc:
            raise CatalogueError(f"catalogue version {version.version} already exists") from exc
        finally:
            tmp.unlink(missing_ok=True)


# --- the registry ---------------------------------------------------------------------------------


class ActionCatalogue:
    """Publish, read and compare catalogue versions over any `CatalogueStore`."""

    def __init__(self, store: CatalogueStore | None = None) -> None:
        self.store: CatalogueStore = store if store is not None else InMemoryCatalogueStore()
        self._lock = threading.Lock()

    def history(self) -> tuple[CatalogueVersion, ...]:
        return self.store.versions()

    def latest(self) -> CatalogueVersion | None:
        versions = self.store.versions()
        return versions[-1] if versions else None

    def get(self, version: int | Literal["latest"] = "latest") -> CatalogueVersion:
        """One version by number, or the latest. Raises `CatalogueError` when there is none."""
        if version == "latest":
            found = self.latest()
            if found is None:
                raise CatalogueError("no catalogue has been published yet")
            return found
        for v in self.store.versions():
            if v.version == version:
                return v
        raise CatalogueError(f"catalogue version {version} does not exist")

    def publish(
        self,
        content: CatalogueContent,
        *,
        created_by: str,
        now: datetime,
        allow_unchanged: bool = False,
    ) -> CatalogueVersion:
        """Write `content` as the next version and return it.

        Publishing the same content again is refused unless `allow_unchanged`: a version that changes
        nothing only makes the history harder to read.
        """
        if now.tzinfo is None:
            raise CatalogueError("publish times must carry a time zone")
        with self._lock:
            previous = self.latest()
            number = 1 if previous is None else previous.version + 1
            diff = diff_content(
                previous.content if previous else None,
                content,
                from_version=previous.version if previous else None,
                to_version=number,
            )
            if previous is not None and diff.empty and not allow_unchanged:
                raise CatalogueError(f"nothing changed since catalogue v{previous.version}")
            if previous is not None and now < previous.created_at:
                raise CatalogueError("a new catalogue version cannot predate the one before it")
            draft = CatalogueVersion(
                version=number,
                created_at=now,
                created_by=created_by,
                offers=content.offers,
                channels=content.channels,
                templates=content.templates,
                diff_from_previous=diff,
                previous_hash=previous.content_hash if previous else GENESIS_HASH,
            )
            published = draft.model_copy(update={"content_hash": compute_hash(draft)})
            self.store.append(published)
            return published

    def diff(self, a: int | Literal["latest"], b: int | Literal["latest"] = "latest") -> CatalogueDiff:
        """What changed going from version `a` to version `b` (either direction)."""
        old, new = self.get(a), self.get(b)
        return diff_content(old.content, new.content, from_version=old.version, to_version=new.version)

    def verify_history(self) -> list[str]:
        """Problems with the stored history, in plain words; empty when it is intact."""
        problems: list[str] = []
        expected_prev = GENESIS_HASH
        for i, v in enumerate(self.store.versions(), start=1):
            if v.version != i:
                problems.append(f"expected version {i}, found {v.version}")
            if v.previous_hash != expected_prev:
                problems.append(f"v{v.version} does not chain to the version before it")
            if v.content_hash != compute_hash(v):
                problems.append(f"v{v.version} was changed after it was published")
            expected_prev = v.content_hash
        return problems


# --- checking an action against a version ---------------------------------------------------------

CATALOGUE_TEMPLATE_UNKNOWN = "catalogue_template_unknown"
CATALOGUE_TEMPLATE_CHANNEL = "catalogue_template_channel_mismatch"
CATALOGUE_OFFER_UNKNOWN = "catalogue_offer_unknown"
CATALOGUE_CHANNEL_UNPRICED = "catalogue_channel_unpriced"
CATALOGUE_CURRENCY_MISMATCH = "catalogue_currency_mismatch"


def check_action_against_catalogue(
    version: CatalogueVersion, action: Action, *, currency: str | None = None
) -> list[str]:
    """Every way an action reaches outside this catalogue version, as codes; empty means it is inside.

    The mandate's own check (`autonomy.mandate.enforce.check_action`) asks "is this template and offer
    approved by the signers?"; this one asks "does the thing exist, on this channel, at a known
    price?". `currency`, when given, is the mandate's: a channel priced in another currency cannot be
    counted against its budget, so it is flagged rather than silently mixed.
    """
    codes: list[str] = []
    if action.content_id is not None:
        tpl = version.template(action.content_id)
        if tpl is None:
            codes.append(CATALOGUE_TEMPLATE_UNKNOWN)
        elif tpl.channel is not action.channel:
            codes.append(CATALOGUE_TEMPLATE_CHANNEL)
    if action.offer_id != NO_OFFER.offer_id and version.offer(action.offer_id) is None:
        codes.append(CATALOGUE_OFFER_UNKNOWN)
    if version.channel_rate(action.channel) is None:
        codes.append(CATALOGUE_CHANNEL_UNPRICED)
    elif currency is not None and version.channel_rate(action.channel, currency=currency) is None:
        codes.append(CATALOGUE_CURRENCY_MISMATCH)
    return codes


def catalogue_costs(
    version: CatalogueVersion, action: Action, *, currency: str | None = None
) -> tuple[float, float]:
    """(offer cost, channel cost) the catalogue lists for an action; 0 for what it does not list.

    With `currency` (the mandate's), the channel cost is the cheapest rate in that currency only,
    so a rate in another currency is never mixed into the mandate's spend.
    """
    offer = version.offer(action.offer_id)
    rate = version.channel_rate(action.channel, currency=currency)
    return (offer.cost_per_redemption if offer else 0.0, rate.unit_cost if rate else 0.0)


# --- helpers --------------------------------------------------------------------------------------


def _no_duplicates(what: str, keys: Sequence[str]) -> None:
    dupes = sorted(k for k, n in Counter(keys).items() if n > 1)
    if dupes:
        raise ValueError(f"duplicate {what}: {', '.join(dupes)}")


def _diff_keyed(
    kind: ChangeKind, old: Mapping[str, BaseModel], new: Mapping[str, BaseModel]
) -> list[CatalogueChange]:
    changes: list[CatalogueChange] = []
    for key in sorted(old.keys() | new.keys()):
        before, after = old.get(key), new.get(key)
        if before is None:
            changes.append(CatalogueChange(kind=kind, key=key, change="added"))
        elif after is None:
            changes.append(CatalogueChange(kind=kind, key=key, change="removed"))
        else:
            b, a = before.model_dump(mode="json"), after.model_dump(mode="json")
            fields = tuple(sorted(f for f in b.keys() | a.keys() if b.get(f) != a.get(f)))
            if fields:
                changes.append(CatalogueChange(kind=kind, key=key, change="changed", fields=fields))
    return changes


# --- offer cost models (v3) -----------------------------------------------------------------------


def offer_cost(offer: OfferOption, basket_value: float | None = None) -> float:
    """What one redemption of this offer costs the business, in the offer's own currency.

    An explicit `cost_per_redemption` always wins: it is what the marketer configured, and every v2
    envelope carries one. Without it the cost comes from the kind:

    * percent off: `amount` percent of `basket_value` (0 when the basket is unknown);
    * amount off: `amount`;
    * free shipping: `shipping_cost`, what shipping one order costs us;
    * bundle: `bundle_discount`, the money taken off the bundle;
    * loyalty: `points x point_value x redemption_rate`, because points that are never redeemed cost
      nothing;
    * gift and early access: nothing but the configured `cost_per_redemption` (none set: 0);
    * none and content: 0.
    """
    if offer.cost_per_redemption > 0:
        return offer.cost_per_redemption
    if offer.kind is OfferKind.PERCENT_OFF:
        return offer.amount / 100.0 * basket_value if basket_value is not None and basket_value > 0 else 0.0
    if offer.kind is OfferKind.AMOUNT_OFF:
        return offer.amount
    if offer.kind is OfferKind.FREE_SHIPPING:
        return offer.shipping_cost
    if offer.kind is OfferKind.BUNDLE:
        return offer.bundle_discount
    if offer.kind is OfferKind.LOYALTY:
        return offer.points * offer.point_value * offer.redemption_rate
    return 0.0


def priced_offer(offer: OfferOption, basket_value: float | None = None) -> OfferOption:
    """The same offer with `cost_per_redemption` filled in from `offer_cost`, for a catalogue version
    or an envelope that should state each cost plainly."""
    cost = offer_cost(offer, basket_value)
    return (
        offer if cost == offer.cost_per_redemption else offer.model_copy(update={"cost_per_redemption": cost})
    )


def catalogue_offer_cost(
    version: CatalogueVersion, offer_id: str, basket_value: float | None = None
) -> float:
    """`offer_cost` of a catalogue offer; 0 for an id the version does not list (and for "none")."""
    offer = version.offer(offer_id)
    return 0.0 if offer is None else offer_cost(offer, basket_value)


RelatedItems = Callable[[str, int], Sequence[tuple[str, float]]]
"""(item id, how many) -> items bought with it, best first: `CoPurchaseModel.related` fits."""


def bundle_offer(
    offer_id: str,
    label: str,
    anchor_item_id: str,
    *,
    related: RelatedItems,
    prices: Mapping[str, float],
    percent_off: float,
    size: int = 3,
    needs_approval: bool = False,
) -> OfferOption | None:
    """A bundle of `anchor_item_id` plus the items most often bought with it, at `percent_off`.

    `related` is the co-purchase recommender's `related`; `prices` maps item id to list price. Items
    without a price are skipped (a discount on an unpriced item cannot be costed). The bundle's cost
    is `percent_off` of the listed prices of the items it holds. None when the anchor has no price or
    fewer than two priced items are found: one item is not a bundle.
    """
    if not 0 < percent_off <= 100 or size < 2:
        raise ValueError("a bundle needs 0 < percent_off <= 100 and a size of at least 2")
    anchor = prices.get(anchor_item_id)
    if anchor is None or anchor <= 0:
        return None
    items = [anchor_item_id]
    for item_id, _score in related(anchor_item_id, size * 3):
        price = prices.get(item_id)
        if item_id not in items and price is not None and price > 0:
            items.append(item_id)
        if len(items) == size:
            break
    if len(items) < 2:
        return None
    list_price = sum(prices[i] for i in items)
    return OfferOption(
        offer_id=offer_id,
        label=label,
        kind=OfferKind.BUNDLE,
        amount=percent_off,
        bundle_item_ids=tuple(items),
        bundle_discount=round(list_price * percent_off / 100.0, 2),
        needs_approval=needs_approval,
    )
