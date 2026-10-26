"""The fake provider, the exact overlay text and the SVG that draws it."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from decimal import Decimal

import pytest
from pydantic import ValidationError

from autonomy.assets.generate import (
    FakeImageProvider,
    GenerationJob,
    ImageConfig,
    ImageProvider,
    ImageRequest,
    ImagesDisabledError,
    OfferSpec,
    OverlayRole,
    StyleBrief,
    UnknownProductError,
    expected_texts,
    format_money,
    generate_image,
    render_svg,
    sale_price,
)
from autonomy.assets.pngio import read_info
from autonomy.catalog.products import Catalogue, Product
from tests.v3.images.support import CONFIG, NOW, OFFER, STYLE, catalogue, job, make_image

SVG = "{http://www.w3.org/2000/svg}"


def request() -> ImageRequest:
    return ImageRequest(
        item_id="shoe-0",
        product_name="Runner 0",
        category="footwear",
        photo=b"photo",
        style=STYLE,
        width=640,
        height=336,
    )


def test_fake_provider_is_a_provider_and_deterministic() -> None:
    provider = FakeImageProvider()
    assert isinstance(provider, ImageProvider)
    first, second = provider.generate(request()), FakeImageProvider().generate(request())
    assert first == second
    assert first.data == second.data
    info = read_info(first.data)
    assert info is not None
    assert (info.mime_type, info.width, info.height) == ("image/png", 640, 336)


def test_fake_provider_varies_with_the_product() -> None:
    other = request().model_copy(update={"item_id": "shoe-1"})
    assert FakeImageProvider().generate(request()).data != FakeImageProvider().generate(other).data


def test_the_same_job_gives_the_same_image_and_id() -> None:
    assert make_image() == make_image()
    assert make_image().image_id != make_image("shoe-1").image_id
    assert make_image().image_id != make_image(customer_type="bargain-hunters").image_id


def test_money_is_exact_to_the_cent() -> None:
    assert format_money(Decimal("1299.5"), "INR") == "₹1,299.50"
    assert format_money(Decimal("5"), "USD") == "$5.00"
    assert format_money(Decimal("12.5"), "CHF") == "CHF 12.50"
    # 19.99 less 15% is 16.9915; Decimal rounds it half up, and never goes through a float.
    assert sale_price(Decimal("19.99"), 15) == Decimal("16.99")
    assert sale_price(Decimal("0.10"), 5) == Decimal("0.10")
    assert sale_price(Decimal("2.50"), 50) == Decimal("1.25")


def test_overlay_text_is_the_catalogue_price_and_offer() -> None:
    image = make_image()
    texts = {layer.role: layer.text for layer in image.overlay.layers}
    assert texts == {
        OverlayRole.NAME: "Runner 0",
        OverlayRole.OFFER: "20% off",
        OverlayRole.WAS_PRICE: "₹1,299.50",
        OverlayRole.PRICE: "₹1,039.60",
    }
    assert image.alt_text == "Runner 0, 20% off, now ₹1,039.60"


def test_overlay_without_a_discount_shows_the_list_price_only() -> None:
    image = make_image(offer=OfferSpec(currency="INR"))
    assert [(layer.role, layer.text) for layer in image.overlay.layers] == [
        (OverlayRole.NAME, "Runner 0"),
        (OverlayRole.PRICE, "₹1,299.50"),
    ]


def test_no_price_on_file_shows_no_price_and_refuses_a_discount() -> None:
    free = Product(item_id="x", name="Mystery box", price=0.0)
    assert expected_texts(free, OfferSpec(currency="USD")) == {OverlayRole.NAME: "Mystery box"}
    with pytest.raises(UnknownProductError):
        expected_texts(free, OFFER)


def test_svg_carries_exactly_the_overlay_text_and_the_picture() -> None:
    image = make_image()
    svg = render_svg(image)
    assert svg == render_svg(image)
    root = ET.fromstring(svg)
    drawn = {t.get("data-role"): t.text for t in root.iter(f"{SVG}text")}
    assert drawn == {layer.role.value: layer.text for layer in image.overlay.layers}
    assert root.find(f"{SVG}title") is not None
    assert "data:image/png;base64," in svg


def test_svg_escapes_markup_in_names() -> None:
    odd = Product(item_id="odd", name='Tom & <Jerry> "mug"', category="gear", price=10.0)
    image = generate_image(
        job("odd"),
        catalogue=Catalogue([*catalogue(), odd]),
        provider=FakeImageProvider(),
        config=CONFIG,
        now=NOW,
    )
    root = ET.fromstring(render_svg(image))
    names = [t.text for t in root.iter(f"{SVG}text") if t.get("data-role") == "name"]
    assert names == ['Tom & <Jerry> "mug"']


def test_generation_is_off_by_default() -> None:
    assert ImageConfig().enabled is False
    with pytest.raises(ImagesDisabledError):
        generate_image(
            job(), catalogue=catalogue(), provider=FakeImageProvider(), config=ImageConfig(), now=NOW
        )


def test_unknown_product_is_refused_before_the_provider_is_asked() -> None:
    class Boom:
        provider_id = "boom"

        def generate(self, request: ImageRequest) -> None:
            raise AssertionError("the provider must not be called")

    with pytest.raises(UnknownProductError):
        generate_image(job("nope"), catalogue=catalogue(), provider=Boom(), config=CONFIG, now=NOW)  # type: ignore[arg-type]


def test_a_job_is_per_product_and_customer_type_never_per_person() -> None:
    with pytest.raises(ValidationError):
        GenerationJob.model_validate(
            {"item_id": "shoe-0", "customer_type": "t", "customer_id": "c_1", "offer": OFFER, "style": STYLE}
        )
    assert "customer_id" not in GenerationJob.model_fields


def test_bad_colours_are_refused_at_the_door() -> None:
    with pytest.raises(ValidationError):
        StyleBrief(category_style="x", palette=("blue",))
