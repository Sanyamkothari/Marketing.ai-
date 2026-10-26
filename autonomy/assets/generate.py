"""Generated images: a provider makes the picture, code draws every number and word on it.

Decision 5 of plan-v3 (and V-2 in docs/v3/DECISIONS.md). The flow for one job, which is one
product x one customer type, never one person:

1. `generate_image` takes the product from the catalogue and asks an `ImageProvider` for a banner
   background (the product photo and a `StyleBrief` go in; image bytes and metadata come out).
2. `compose_overlay` writes the price, the old price, the offer and the product name as a structured
   `OverlaySpec`. The text comes from the catalogue and the offer with `Decimal` arithmetic, never
   from the model, so the numbers on the image are exact. `render_svg` draws that spec over the
   background when a preview or a send needs it.
3. The result is a `GeneratedImage`. It is not usable yet: `autonomy.assets.check` looks at it and
   `autonomy.assets.approval` holds it until a person approves it.

Only `FakeImageProvider` exists, and nothing here touches the network. A real model is a second class
with the same `generate` method (decision V-2). Generation is off unless `ImageConfig.enabled`.
"""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Collection, Mapping
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Protocol, runtime_checkable
from xml.sax.saxutils import escape, quoteattr

from pydantic import AwareDatetime, Field, field_validator

from autonomy.assets.pngio import make_png
from autonomy.catalog.products import Catalogue, Product
from autonomy.contracts import Theme, stable_id
from autonomy.contracts.base import Contract

__all__ = [
    "DEFAULT_HEIGHT",
    "DEFAULT_WIDTH",
    "NOTE_COLOUR",
    "NOTE_DEPICTS",
    "NOTE_LOGO",
    "NOTE_TEXT",
    "FakeImageProvider",
    "Flaw",
    "GeneratedImage",
    "GenerationJob",
    "ImageConfig",
    "ImageProvider",
    "ImageRequest",
    "ImagesDisabledError",
    "OfferSpec",
    "OverlayRole",
    "OverlaySpec",
    "ProviderImage",
    "StyleBrief",
    "TextLayer",
    "UnknownProductError",
    "alt_text_for",
    "compose_overlay",
    "expected_texts",
    "format_money",
    "generate_image",
    "layout_problems",
    "render_svg",
    "sale_price",
    "text_box",
]

DEFAULT_WIDTH = 1200
DEFAULT_HEIGHT = 628
"""A 1.91:1 banner, the shape email and WhatsApp header images are drawn for (docs/v2/FORMATS.md)."""
MAX_BATCH = 20
"""The most thumbnails one approval screen shows (plan-v3 decision 5, step 5)."""

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
_SYMBOLS = {"USD": "$", "INR": "₹", "EUR": "€", "GBP": "£"}
_CENT = Decimal("0.01")


class ImagesDisabledError(RuntimeError):
    """Generation was asked for while `ImageConfig.enabled` is false (the default)."""


class UnknownProductError(LookupError):
    """The job names a product that is not in the catalogue, or cannot show the offer asked for."""


class ImageConfig(Contract):
    """Switches and limits. Off by default: nothing is generated until a person turns it on."""

    enabled: bool = False
    width: int = Field(default=DEFAULT_WIDTH, ge=64, le=4000)
    height: int = Field(default=DEFAULT_HEIGHT, ge=64, le=4000)
    min_width: int = Field(default=600, ge=1, description="Narrower images look poor in email")
    min_height: int = Field(default=300, ge=1, description="A thin strip has no room for the price lines")
    max_bytes: int = Field(default=5 * 1024 * 1024, ge=1, description="WhatsApp's hard limit is 5 MB")
    allowed_mimes: tuple[str, ...] = ("image/png", "image/jpeg")
    batch_size: int = Field(default=MAX_BATCH, ge=1, le=MAX_BATCH)
    colour_tolerance: float = Field(
        default=60.0, ge=0, description="How far (RGB distance) the picture may sit from the brand colours"
    )


class StyleBrief(Contract):
    """How the banner should look: the category's style, the season, the brand's colours."""

    category_style: str = Field(min_length=1, description="e.g. 'sporty, bold'")
    season: str | None = Field(default=None, description="e.g. 'diwali', 'winter sale'")
    theme: Theme | None = None
    palette: tuple[str, ...] = Field(min_length=1, description="Brand colours as #rrggbb, main one first")
    text_colour: str = "#ffffff"

    @field_validator("palette")
    @classmethod
    def _palette_is_hex(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for colour in value:
            if not _HEX.fullmatch(colour):
                raise ValueError(f"{colour!r} is not a #rrggbb colour")
        return value

    @field_validator("text_colour")
    @classmethod
    def _text_colour_is_hex(cls, value: str) -> str:
        if not _HEX.fullmatch(value):
            raise ValueError(f"{value!r} is not a #rrggbb colour")
        return value


class OfferSpec(Contract):
    """The offer to print: a percentage off the catalogue price, or none. The money is code's job."""

    currency: str = Field(pattern=r"^[A-Z]{3}$", description="ISO 4217 code, e.g. INR")
    discount_pct: int | None = Field(default=None, ge=1, le=90)


class GenerationJob(Contract):
    """One product for one customer type. There is no customer field: images are never per person."""

    item_id: str = Field(min_length=1)
    customer_type: str = Field(min_length=1, description="A customer type id, never a person")
    offer: OfferSpec
    style: StyleBrief
    photo: bytes = Field(default=b"", repr=False, description="The catalogue product photo, if any")


class OverlayRole(StrEnum):
    """What a piece of overlay text is. The checker holds each role to the exact expected text."""

    NAME = "name"
    OFFER = "offer"
    WAS_PRICE = "was_price"
    PRICE = "price"


class TextLayer(Contract):
    """One line of text, positioned as fractions of the image so it scales to any size."""

    role: OverlayRole
    text: str = Field(min_length=1)
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1, description="The text baseline")
    size: float = Field(gt=0, le=1, description="Font size as a share of the image height")
    anchor: str = Field(default="start", pattern=r"^(start|end|middle)$")
    colour: str = "#ffffff"
    strike: bool = False


class OverlaySpec(Contract):
    """Everything code writes on the image, as data. Rendered at preview and send time."""

    layers: tuple[TextLayer, ...]


class ProviderImage(Contract):
    """What an image provider returns: the picture and what it says about it."""

    data: bytes = Field(repr=False)
    mime_type: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    provider_id: str = Field(min_length=1)
    shows_item_id: str = Field(min_length=1, description="The product the provider says it drew")


class ImageRequest(Contract):
    """What a provider is asked for: a banner background for one product in one style."""

    item_id: str = Field(min_length=1)
    product_name: str
    category: str | None = None
    photo: bytes = Field(default=b"", repr=False)
    style: StyleBrief
    width: int = Field(gt=0)
    height: int = Field(gt=0)


@runtime_checkable
class ImageProvider(Protocol):
    """Product photo and style brief in; image bytes and metadata out. The model is swappable.

    A real implementation is chosen when phase 4 starts (decision V-2). It must not draw prices or
    words: `compose_overlay` does that.
    """

    provider_id: str

    def generate(self, request: ImageRequest) -> ProviderImage:
        """One background for the request. The same request should give the same kind of image."""
        ...


class GeneratedImage(Contract):
    """A finished candidate: background, overlay and the facts a check or an approver needs.

    It carries no approval state. Whether it may be used is decided by `autonomy.assets.approval`.
    """

    image_id: str
    item_id: str
    customer_type: str
    category: str | None
    background: bytes = Field(repr=False)
    mime_type: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    alt_text: str
    overlay: OverlaySpec
    offer: OfferSpec
    style: StyleBrief
    provider_id: str
    provider_shows_item_id: str
    created_at: AwareDatetime


# --- the overlay text: exact, from the catalogue and the offer ---------------------------------------


def format_money(value: Decimal, currency: str) -> str:
    """`1,299.00` with the currency's symbol, or `CHF 1,299.00` when no symbol is known."""
    amount = f"{value.quantize(_CENT, rounding=ROUND_HALF_UP):,.2f}"
    symbol = _SYMBOLS.get(currency)
    return f"{symbol}{amount}" if symbol else f"{currency} {amount}"


def _list_price(product: Product) -> Decimal:
    return Decimal(str(product.price))


def sale_price(list_price: Decimal, discount_pct: int) -> Decimal:
    """The price after the percentage off, to the cent (halves round up)."""
    return (list_price * (100 - discount_pct) / 100).quantize(_CENT, rounding=ROUND_HALF_UP)


def expected_texts(product: Product, offer: OfferSpec) -> dict[OverlayRole, str]:
    """The only text the image may carry, by role, worked out from the catalogue and the offer.

    The price is rounded to the cent first. No price on file, or one that rounds to 0.00, means no
    price is shown. A discount without a price, or one whose sale price does not come out below the
    list price once rounded (20% off 0.01), cannot be shown, so it raises `UnknownProductError` rather
    than print a number nobody can stand behind.
    """
    texts: dict[OverlayRole, str] = {}
    if product.name.strip():
        texts[OverlayRole.NAME] = product.name.strip()
    price = _list_price(product).quantize(_CENT, rounding=ROUND_HALF_UP)
    if price <= 0:
        if offer.discount_pct is not None:
            raise UnknownProductError(
                f"{product.item_id!r} has no price in the catalogue; cannot show a discount"
            )
        return texts
    if offer.discount_pct is not None:
        now = sale_price(price, offer.discount_pct)
        if not 0 < now < price:
            raise UnknownProductError(
                f"{offer.discount_pct}% off {format_money(price, offer.currency)} does not give a lower "
                "price to the cent; cannot show the discount"
            )
        texts[OverlayRole.OFFER] = f"{offer.discount_pct}% off"
        texts[OverlayRole.WAS_PRICE] = format_money(price, offer.currency)
        texts[OverlayRole.PRICE] = format_money(now, offer.currency)
    else:
        texts[OverlayRole.PRICE] = format_money(price, offer.currency)
    return texts


def alt_text_for(product: Product, offer: OfferSpec) -> str:
    """A plain description for screen readers and blocked images, built from the same exact texts."""
    texts = expected_texts(product, offer)
    parts = [texts.get(OverlayRole.NAME) or product.item_id]
    if OverlayRole.OFFER in texts:
        parts.append(texts[OverlayRole.OFFER])
    if OverlayRole.PRICE in texts:
        parts.append(
            f"now {texts[OverlayRole.PRICE]}" if OverlayRole.OFFER in texts else texts[OverlayRole.PRICE]
        )
    return ", ".join(parts)


_LAYOUT: dict[OverlayRole, tuple[float, float, float, str, bool]] = {
    # role: (x, baseline y, font size / height, anchor, struck through)
    OverlayRole.NAME: (0.04, 0.14, 0.07, "start", False),
    OverlayRole.OFFER: (0.96, 0.14, 0.08, "end", False),
    OverlayRole.WAS_PRICE: (0.04, 0.76, 0.05, "start", True),
    OverlayRole.PRICE: (0.04, 0.92, 0.12, "start", False),
}


_OFFER_SECOND_LINE = 0.27
"""Where the offer goes when it would collide with a long name on the top line."""
_MIN_SIZE = 0.035
"""The smallest font (share of the height) a line is shrunk to so that it fits across the image."""
_MARGIN = 0.04
_CHAR_WIDTH = 0.6
"""A bold sans-serif character is about 0.6 of the font size wide; used to estimate a line's width."""
_ASCENT, _DESCENT = 0.8, 0.2


def text_box(layer: TextLayer, width: int, height: int) -> tuple[float, float, float, float]:
    """The estimated box a line takes, in pixels: (left, top, right, bottom). No font is measured."""
    font = layer.size * height
    span = len(layer.text) * font * _CHAR_WIDTH
    x = layer.x * width
    left = x if layer.anchor == "start" else x - span if layer.anchor == "end" else x - span / 2
    base = layer.y * height
    return left, base - font * _ASCENT, left + span, base + font * _DESCENT


def _overlaps(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def layout_problems(overlay: OverlaySpec, width: int, height: int) -> list[str]:
    """Lines that run off the image or over each other, in words; empty when every line is clear."""
    problems: list[str] = []
    boxes = [(layer, text_box(layer, width, height)) for layer in overlay.layers]
    for layer, (left, top, right, bottom) in boxes:
        if left < 0 or top < 0 or right > width or bottom > height:
            problems.append(f"The {layer.role.value} text {layer.text!r} runs off the image.")
    for i, (a, box_a) in enumerate(boxes):
        for b, box_b in boxes[i + 1 :]:
            if _overlaps(box_a, box_b):
                problems.append(f"The {a.role.value} and {b.role.value} texts overlap.")
    return problems


def compose_overlay(
    product: Product,
    offer: OfferSpec,
    style: StyleBrief,
    *,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
) -> OverlaySpec:
    """The overlay for a product and an offer: `expected_texts` placed on a fixed layout.

    A line too long for the image is shrunk (down to `_MIN_SIZE`) to fit inside the margins, and the
    offer moves to a second line when it would run into the name. A name so long it still does not
    fit is left as it is; `layout_problems` (and the rule check) then refuse the image.
    """
    layers: list[TextLayer] = []
    for role, text in expected_texts(product, offer).items():
        x, y, size, anchor, strike = _LAYOUT[role]
        room = (1 - 2 * _MARGIN) * width
        needed = len(text) * size * height * _CHAR_WIDTH
        if needed > room:
            size = max(_MIN_SIZE, size * room / needed)
        layers.append(
            TextLayer(
                role=role,
                text=text,
                x=x,
                y=y,
                size=round(size, 4),
                anchor=anchor,
                colour=style.text_colour,
                strike=strike,
            )
        )
    by_role = {layer.role: layer for layer in layers}
    name, top_offer = by_role.get(OverlayRole.NAME), by_role.get(OverlayRole.OFFER)
    if (
        name is not None
        and top_offer is not None
        and _overlaps(text_box(name, width, height), text_box(top_offer, width, height))
    ):
        moved = top_offer.model_copy(update={"y": _OFFER_SECOND_LINE})
        layers = [moved if layer is top_offer else layer for layer in layers]
    return OverlaySpec(layers=tuple(layers))


def render_svg(image: GeneratedImage) -> str:
    """The banner as one SVG: the background picture with the overlay text drawn on top.

    The text is XML-escaped but otherwise printed as the overlay holds it. Same image, same string.
    SVG is the preview form; an email needs a raster file, which a send-time step makes from this.
    """
    w, h = image.width, image.height
    picture = base64.b64encode(image.background).decode("ascii")
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" aria-label={quoteattr(image.alt_text)}>',
        f"<title>{escape(image.alt_text)}</title>",
        f'<image x="0" y="0" width="{w}" height="{h}" xlink:href="data:{image.mime_type};base64,{picture}"/>',
    ]
    for layer in image.overlay.layers:
        decoration = ' text-decoration="line-through"' if layer.strike else ""
        lines.append(
            f'<text data-role="{layer.role.value}" x="{layer.x * w:.1f}" y="{layer.y * h:.1f}" '
            f'font-size="{layer.size * h:.1f}" font-family="Helvetica, Arial, sans-serif" font-weight="bold" '
            f'text-anchor="{layer.anchor}" fill="{layer.colour}"{decoration}>{escape(layer.text)}</text>'
        )
    lines.append("</svg>")
    return "\n".join(lines)


# --- the fake provider ------------------------------------------------------------------------------

NOTE_DEPICTS = "fake:depicts"
NOTE_LOGO = "fake:logo"
NOTE_TEXT = "fake:text"
NOTE_COLOUR = "fake:colour"
"""PNG notes the fake provider leaves in its pictures and the fake vision model reads back, standing in
for what a real model would see in the pixels."""


class Flaw(StrEnum):
    """Mistakes the fake provider can be told to make, so the checker can be tested against them."""

    WRONG_PRODUCT = "wrong_product"
    BROKEN_LOGO = "broken_logo"
    GARBLED_TEXT = "garbled_text"
    OFF_BRAND_COLOUR = "off_brand_colour"


def parse_hex(colour: str) -> tuple[int, int, int]:
    """`#rrggbb` as three 0-255 numbers."""
    return int(colour[1:3], 16), int(colour[3:5], 16), int(colour[5:7], 16)


def colour_distance(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    """Straight-line distance between two RGB colours, 0 to about 441."""
    return float(sum((x - y) ** 2 for x, y in zip(a, b, strict=True)) ** 0.5)


def _hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def _clamp(value: int) -> int:
    return max(0, min(255, value))


class FakeImageProvider:
    """A deterministic stand-in: a gradient in the brand's main colour, with notes about what it "drew".

    The same request always gives the same bytes. `flaws` maps an item id to mistakes to plant in its
    pictures; the fake vision model (`autonomy.assets.check.FakeModelChecker`) reads them back. No
    network, no files, no randomness.
    """

    provider_id = "fake-image-v1"

    def __init__(self, flaws: Mapping[str, Collection[Flaw]] | None = None) -> None:
        self._flaws: dict[str, frozenset[Flaw]] = {k: frozenset(v) for k, v in (flaws or {}).items()}

    def generate(self, request: ImageRequest) -> ProviderImage:
        flaws = self._flaws.get(request.item_id, frozenset())
        digest = hashlib.sha256(
            b"\x1f".join(
                [
                    request.item_id.encode(),
                    request.style.model_dump_json().encode(),
                    hashlib.sha256(request.photo).digest(),
                ]
            )
        ).digest()
        palette = [parse_hex(c) for c in request.style.palette]
        base = tuple(_clamp(c + (digest[i] % 9) - 4) for i, c in enumerate(palette[0]))
        main: tuple[int, int, int] = (base[0], base[1], base[2])
        if Flaw.OFF_BRAND_COLOUR in flaws:
            candidates = [(255, 0, 255), (0, 255, 0), (255, 255, 0), (0, 255, 255)]
            main = max(candidates, key=lambda c: min(colour_distance(c, p) for p in palette))
        bottom = (round(main[0] * 0.7), round(main[1] * 0.7), round(main[2] * 0.7))
        depicts = f"other-{request.item_id}" if Flaw.WRONG_PRODUCT in flaws else request.item_id
        notes = {
            NOTE_DEPICTS: depicts,
            NOTE_LOGO: "broken" if Flaw.BROKEN_LOGO in flaws else "ok",
            NOTE_TEXT: "garbled" if Flaw.GARBLED_TEXT in flaws else "none",
            NOTE_COLOUR: _hex(main),
        }
        data = make_png(request.width, request.height, main, bottom, text=notes)
        return ProviderImage(
            data=data,
            mime_type="image/png",
            width=request.width,
            height=request.height,
            provider_id=self.provider_id,
            shows_item_id=request.item_id,
        )


# --- the pipeline step ------------------------------------------------------------------------------


def generate_image(
    job: GenerationJob,
    *,
    catalogue: Catalogue,
    provider: ImageProvider,
    config: ImageConfig,
    now: datetime,
) -> GeneratedImage:
    """Make one candidate image for a product x customer type. Nothing is checked or approved here.

    Raises `ImagesDisabledError` unless `config.enabled`, and `UnknownProductError` when the product
    is not in the catalogue. `now` is passed in, never read from the clock. The id depends only on
    the job, the exact text drawn (so a catalogue price change names a new image), the provider and
    a hash of the picture it returned (so a provider whose output varies gives a new id each time).
    """
    if not config.enabled:
        raise ImagesDisabledError("image generation is off; set ImageConfig.enabled to turn it on")
    product = catalogue.get(job.item_id)
    if product is None:
        raise UnknownProductError(f"{job.item_id!r} is not in the catalogue")
    overlay = compose_overlay(product, job.offer, job.style, width=config.width, height=config.height)
    alt_text = alt_text_for(product, job.offer)
    made = provider.generate(
        ImageRequest(
            item_id=product.item_id,
            product_name=product.name,
            category=product.category,
            photo=job.photo,
            style=job.style,
            width=config.width,
            height=config.height,
        )
    )
    return GeneratedImage(
        image_id=stable_id(
            "img",
            job.item_id,
            job.customer_type,
            job.offer.model_dump_json(),
            job.style.model_dump_json(),
            "\x1f".join(layer.text for layer in overlay.layers),
            made.provider_id,
            hashlib.sha256(made.data).hexdigest(),
        ),
        item_id=product.item_id,
        customer_type=job.customer_type,
        category=product.category,
        background=made.data,
        mime_type=made.mime_type,
        width=made.width,
        height=made.height,
        alt_text=alt_text,
        overlay=overlay,
        offer=job.offer,
        style=job.style,
        provider_id=made.provider_id,
        provider_shows_item_id=made.shows_item_id,
        created_at=now,
    )
