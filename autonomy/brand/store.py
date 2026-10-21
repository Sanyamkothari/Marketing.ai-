"""Where a business's brand kit, its approvals and its product images live.

* `BrandKitStore`: the latest kit per business (`InMemoryBrandKitStore`, `SqlBrandKitStore`). Every
  approval is also kept as its own row (`approvals`), so who accepted which voice, and when, is
  never overwritten by a later read.
* `save_read(store, kit)`: store a fresh read, keeping the approval when nothing it covers changed
  (same fingerprint). A read that changes the voice, rules or assets needs a new approval.
* `approve(...)`: the one approval. Refused (`StaleKitError`) when the kit changed since it was shown.
* `record_assets(library, kit)`: the shop's product images as `autonomy.assets` records: DRAFT while
  the kit waits, APPROVED (with who approved) once the voice is accepted. The record each came
  from (`KitAsset.source`) stays on the kit; the asset id says it too (`shopify-<shop>-<product>-<media>`).
* `SqlAssetLibrary`: the `AssetLibrary` protocol over the deployment's database, per business.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from typing import Protocol

from sqlalchemy import Column, Index, MetaData, String, Table, Text, select
from sqlalchemy.engine import Engine

from autonomy.assets.library import Asset, AssetKind, AssetLibrary, AssetStatus, matches
from autonomy.brand.kit import BrandApproval, BrandKit
from autonomy.contracts.content import Theme
from autonomy.store.schema import UtcDateTime
from autonomy.store.sql import upsert

__all__ = [
    "BrandKitStore",
    "InMemoryBrandKitStore",
    "SqlAssetLibrary",
    "SqlBrandKitStore",
    "StaleKitError",
    "approve",
    "brand_metadata",
    "create_brand_tables",
    "record_assets",
    "save_read",
]


class StaleKitError(ValueError):
    """The kit changed after it was shown; the approval would cover something nobody saw."""


class BrandKitStore(Protocol):
    def get(self, business_id: str) -> BrandKit | None: ...

    def put(self, kit: BrandKit) -> None: ...

    def add_approval(self, business_id: str, approval: BrandApproval) -> None: ...

    def approvals(self, business_id: str) -> list[BrandApproval]: ...


class InMemoryBrandKitStore:
    def __init__(self) -> None:
        self._kits: dict[str, BrandKit] = {}
        self._approvals: dict[str, list[BrandApproval]] = {}
        self._lock = threading.Lock()

    def get(self, business_id: str) -> BrandKit | None:
        return self._kits.get(business_id)

    def put(self, kit: BrandKit) -> None:
        with self._lock:
            self._kits[kit.business_id] = kit

    def add_approval(self, business_id: str, approval: BrandApproval) -> None:
        with self._lock:
            self._approvals.setdefault(business_id, []).append(approval)

    def approvals(self, business_id: str) -> list[BrandApproval]:
        return list(self._approvals.get(business_id, []))


brand_metadata = MetaData()

brand_kit_table = Table(
    "brand_kit",
    brand_metadata,
    Column("business_id", String(200), primary_key=True),
    Column("body", Text, nullable=False),
    Column("updated_at", UtcDateTime, nullable=False),
)

brand_approval_table = Table(
    "brand_approval",
    brand_metadata,
    Column("business_id", String(200), primary_key=True),
    Column("approved_at", UtcDateTime, primary_key=True),
    Column("fingerprint", String(64), primary_key=True),
    Column("body", Text, nullable=False),
)

brand_asset_table = Table(
    "brand_asset",
    brand_metadata,
    Column("business_id", String(200), primary_key=True),
    Column("asset_id", String(200), primary_key=True),
    Column("status", String(20), nullable=False),
    Column("body", Text, nullable=False),
    Index("ix_brand_asset_status", "business_id", "status"),
)


def create_brand_tables(engine: Engine) -> None:
    """Create the brand tables (safe to call twice)."""
    brand_metadata.create_all(engine)


class SqlBrandKitStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        create_brand_tables(engine)

    def get(self, business_id: str) -> BrandKit | None:
        with self.engine.connect() as conn:
            body = conn.execute(
                select(brand_kit_table.c.body).where(brand_kit_table.c.business_id == business_id)
            ).scalar_one_or_none()
        return BrandKit.model_validate_json(body) if body else None

    def put(self, kit: BrandKit) -> None:
        with self.engine.begin() as conn:
            upsert(
                conn,
                brand_kit_table,
                [{"business_id": kit.business_id, "body": kit.model_dump_json(), "updated_at": kit.read_at}],
                keep=(),
            )

    def add_approval(self, business_id: str, approval: BrandApproval) -> None:
        with self.engine.begin() as conn:
            upsert(
                conn,
                brand_approval_table,
                [
                    {
                        "business_id": business_id,
                        "approved_at": approval.approved_at,
                        "fingerprint": approval.fingerprint,
                        "body": approval.model_dump_json(),
                    }
                ],
                keep=(),
            )

    def approvals(self, business_id: str) -> list[BrandApproval]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(brand_approval_table.c.body)
                .where(brand_approval_table.c.business_id == business_id)
                .order_by(brand_approval_table.c.approved_at)
            ).scalars()
            return [BrandApproval.model_validate_json(r) for r in rows]


VOICE_FINDINGS = frozenset(
    {
        "greeting",
        "sign_off",
        "subject_max",
        "sms_body_max",
        "max_emoji",
        "max_exclamations",
        "banned_words",
        "discount_kinds",
        "words_they_use",
        "tone",
    }
)
SHOP_FINDINGS = frozenset({"logo", "colours", "assets"})


def _keep_what_failed(previous: BrandKit, kit: BrandKit) -> BrandKit:
    """A read that failed part way never replaces what an earlier read saw in full.

    A Klaviyo outage or rate limit would otherwise drop the hard rules (fewer messages: "never uses"
    left out), and a Shopify refusal would empty the photos and rename the shop, each asking the
    owner to approve again for nothing. The earlier read's part stands, with a note."""
    update: dict[str, object] = {}
    notes = list(kit.notes)
    findings = list(kit.findings)
    if "klaviyo" in kit.incomplete and previous.messages_read > kit.messages_read:
        update.update(rules=previous.rules, style=previous.style, messages_read=previous.messages_read)
        findings = [f for f in previous.findings if f.key in VOICE_FINDINGS] + [
            f for f in findings if f.key not in VOICE_FINDINGS
        ]
        notes.append(
            f"Klaviyo could not be read in full this time; the voice read on {previous.read_at:%Y-%m-%d} "
            f"from {previous.messages_read} of your messages stands."
        )
    shop_failed = "shopify" in kit.incomplete and previous.shop_domain is not None
    products_failed = "shopify_products" in kit.incomplete and len(previous.assets) > len(kit.assets)
    if shop_failed:
        update.update(
            shop_name=previous.shop_name,
            shop_domain=previous.shop_domain,
            currency=previous.currency,
            logo=previous.logo,
            colours=previous.colours,
        )
    if shop_failed or products_failed:
        update.update(assets=previous.assets, product_types=previous.product_types, catalogue_read=False)
        keys = SHOP_FINDINGS if shop_failed else frozenset({"assets"})
        findings = [f for f in findings if f.key not in keys] + [
            f for f in previous.findings if f.key in keys
        ]
        notes.append(
            f"Shopify could not be read in full this time; the shop and its product photos read on "
            f"{previous.read_at:%Y-%m-%d} stand."
        )
    if not update:
        return kit
    update.update(findings=tuple(findings), notes=tuple(dict.fromkeys(notes)))
    return kit.model_copy(update=update)


def save_read(store: BrandKitStore, kit: BrandKit) -> BrandKit:
    """Store a fresh read; the earlier approval carries over only if it covers exactly this kit.

    Parts of the read that failed (`BrandKit.incomplete`) keep what the earlier read saw in full."""
    previous = store.get(kit.business_id)
    if previous is not None:
        kit = _keep_what_failed(previous, kit)
    if (
        previous is not None
        and previous.approval is not None
        and previous.approval.fingerprint == kit.fingerprint()
    ):
        kit = kit.model_copy(update={"approval": previous.approval})
    store.put(kit)
    return kit


def approve(
    store: BrandKitStore,
    business_id: str,
    *,
    approved_by: str,
    now: datetime,
    fingerprint: str,
    sample_ids: tuple[str, ...] = (),
    goal_id: str | None = None,
) -> BrandKit:
    """Accept the voice: the one approval, recorded with who, when, the kit's fingerprint and the samples shown."""
    kit = store.get(business_id)
    if kit is None:
        raise StaleKitError("there is no brand kit to approve yet; read it from your tools first")
    if kit.fingerprint() != fingerprint:
        raise StaleKitError("the brand kit changed since it was shown; look at it again before approving")
    who = approved_by.strip() or "the owner"
    approval = BrandApproval(
        approved_by=who[:200],
        approved_at=now,
        fingerprint=fingerprint,
        sample_ids=sample_ids,
        goal_id=goal_id,
    )
    kit = kit.model_copy(update={"approval": approval})
    store.put(kit)
    store.add_approval(business_id, approval)
    return kit


BRAND_KIT_MARK = "(brand kit, "
"""In `Asset.approved_by` of every photo this module approved, so it can find them again."""


def record_assets(library: AssetLibrary, kit: BrandKit) -> list[Asset]:
    """Put the kit's product images in an `AssetLibrary`: APPROVED with the kit's approval, else DRAFT.

    A product image already in the library under the same id keeps a person's later decision (a
    RETIRED asset stays retired). When the kit read the Shopify products in full, a photo this
    module approved earlier that is no longer among them (its product was archived, deleted or
    lost its image) goes back to DRAFT, so no message shows a product the shop no longer sells; it
    is approved again if the product comes back while the voice is approved."""
    approved = kit.approved and kit.approval is not None
    out: list[Asset] = []
    if kit.catalogue_read:
        current = {a.asset_id for a in kit.assets}
        for old in library.find(kind=AssetKind.IMAGE, approved_only=True):
            if (
                old.asset_id not in current
                and old.asset_id.startswith("shopify-")
                and BRAND_KIT_MARK in (old.approved_by or "")
            ):
                library.put(old.model_copy(update={"status": AssetStatus.DRAFT}))
    for item in kit.assets:
        existing = library.get(item.asset_id)
        if existing is not None and existing.status is AssetStatus.RETIRED:
            continue
        status = AssetStatus.APPROVED if approved else AssetStatus.DRAFT
        by = (
            f"{kit.approval.approved_by} {BRAND_KIT_MARK}{kit.approval.approved_at:%Y-%m-%d})"
            if approved and kit.approval is not None
            else None
        )
        asset = Asset(
            asset_id=item.asset_id,
            kind=AssetKind.IMAGE,
            url=item.url,
            alt_text=item.alt_text,
            width=item.width,
            height=item.height,
            item_id=item.item_id,
            category=item.category,
            status=status,
            approved_by=by,
            created_at=item.source.read_at,
        )
        library.put(asset)
        out.append(asset)
    return out


class SqlAssetLibrary:
    """`AssetLibrary` over SQL for one business. Lookups load that business's rows and filter in code."""

    def __init__(self, engine: Engine, business_id: str) -> None:
        self.engine = engine
        self.business_id = business_id
        create_brand_tables(engine)

    def put(self, asset: Asset) -> None:
        with self.engine.begin() as conn:
            upsert(
                conn,
                brand_asset_table,
                [
                    {
                        "business_id": self.business_id,
                        "asset_id": asset.asset_id,
                        "status": asset.status.value,
                        "body": json.dumps(asset.model_dump(mode="json"), sort_keys=True),
                    }
                ],
                keep=(),
            )

    def get(self, asset_id: str) -> Asset | None:
        with self.engine.connect() as conn:
            body = conn.execute(
                select(brand_asset_table.c.body).where(
                    brand_asset_table.c.business_id == self.business_id,
                    brand_asset_table.c.asset_id == asset_id,
                )
            ).scalar_one_or_none()
        return Asset.model_validate_json(body) if body else None

    def find(
        self,
        *,
        kind: AssetKind | None = None,
        item_id: str | None = None,
        category: str | None = None,
        theme: Theme | None = None,
        group_id: str | None = None,
        approved_only: bool = True,
    ) -> list[Asset]:
        query = select(brand_asset_table.c.body).where(brand_asset_table.c.business_id == self.business_id)
        if approved_only:
            query = query.where(brand_asset_table.c.status == AssetStatus.APPROVED.value)
        with self.engine.connect() as conn:
            assets = [Asset.model_validate_json(b) for b in conn.execute(query).scalars()]
        found = [
            a
            for a in assets
            if matches(
                a,
                kind=kind,
                item_id=item_id,
                category=category,
                theme=theme,
                group_id=group_id,
                approved_only=approved_only,
            )
        ]
        return sorted(found, key=lambda a: a.asset_id)
