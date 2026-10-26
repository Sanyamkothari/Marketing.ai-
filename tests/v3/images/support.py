"""Shared builders for the generated-image tests."""

from __future__ import annotations

from datetime import UTC, datetime

from autonomy.assets.approval import ImageDesk
from autonomy.assets.check import VisionChecker, default_checker
from autonomy.assets.generate import (
    FakeImageProvider,
    Flaw,
    GeneratedImage,
    GenerationJob,
    ImageConfig,
    OfferSpec,
    StyleBrief,
    generate_image,
)
from autonomy.assets.library import InMemoryAssetLibrary
from autonomy.catalog.products import Catalogue, Product

NOW = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)
STYLE = StyleBrief(category_style="sporty, bold", season="diwali", palette=("#1e3a8a", "#f59e0b"))
OFFER = OfferSpec(currency="INR", discount_pct=20)
# Small pictures keep the tests fast: still wider than the 600 px minimum.
CONFIG = ImageConfig(enabled=True, width=640, height=336)


def catalogue(n: int = 3) -> Catalogue:
    products = [
        Product(item_id=f"shoe-{i}", name=f"Runner {i}", category="footwear", price=1299.5 + i)
        for i in range(n)
    ]
    products.append(Product(item_id="bottle", name="Steel bottle", category="gear", price=499.0))
    return Catalogue(products)


def job(
    item_id: str = "shoe-0", customer_type: str = "weekend-runners", offer: OfferSpec = OFFER
) -> GenerationJob:
    return GenerationJob(item_id=item_id, customer_type=customer_type, offer=offer, style=STYLE)


def make_image(
    item_id: str = "shoe-0",
    customer_type: str = "weekend-runners",
    *,
    flaws: dict[str, list[Flaw]] | None = None,
    cat: Catalogue | None = None,
    offer: OfferSpec = OFFER,
) -> GeneratedImage:
    return generate_image(
        job(item_id, customer_type, offer),
        catalogue=cat or catalogue(),
        provider=FakeImageProvider(flaws),
        config=CONFIG,
        now=NOW,
    )


def desk(
    *,
    library: InMemoryAssetLibrary | None = None,
    checker: VisionChecker | None = None,
    config: ImageConfig = CONFIG,
    cat: Catalogue | None = None,
) -> ImageDesk:
    return ImageDesk(
        library if library is not None else InMemoryAssetLibrary(),
        catalogue=cat or catalogue(),
        checker=checker or default_checker(),
        config=config,
        url_for=lambda image: f"https://cdn.example.com/generated/{image.image_id}.png",
        clock=lambda: NOW,
    )
