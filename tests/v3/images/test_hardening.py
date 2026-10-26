"""The check fails closed, and the overlay stays exact and legible at the edges: tiny prices, long names,
non-Latin ids, the wrong currency, a thin strip. Includes plants the parametrized check tests do not name."""

from __future__ import annotations

from decimal import Decimal

import pytest

from autonomy.assets.check import (
    CheckContext,
    FakeModelChecker,
    Observation,
    ReasonCode,
    RuleChecker,
    default_checker,
    observe_fake_png,
)
from autonomy.assets.generate import (
    NOTE_DEPICTS,
    FakeImageProvider,
    GeneratedImage,
    OfferSpec,
    OverlayRole,
    OverlaySpec,
    UnknownProductError,
    expected_texts,
    generate_image,
    layout_problems,
)
from autonomy.assets.pngio import make_png, read_info
from autonomy.catalog.products import Catalogue, Product
from tests.v3.images.support import CONFIG, NOW, catalogue, job, make_image

CONTEXT = CheckContext(catalogue=catalogue(), config=CONFIG)


def codes(image: GeneratedImage, context: CheckContext = CONTEXT) -> set[ReasonCode]:
    return {r.code for r in default_checker().check(image, context).reasons}


TWENTY_OFF = OfferSpec(currency="INR", discount_pct=20)


def make_for(product: Product, offer: OfferSpec = TWENTY_OFF) -> GeneratedImage:
    return generate_image(
        job(product.item_id, offer=offer),
        catalogue=Catalogue([product]),
        provider=FakeImageProvider(),
        config=CONFIG,
        now=NOW,
    )


# --- the model check fails closed ------------------------------------------------------------------


def test_a_png_without_notes_is_refused_by_the_default_checker() -> None:
    plain = make_png(CONFIG.width, CONFIG.height, (255, 0, 255))  # magenta, far off the brand palette
    image = make_image().model_copy(update={"background": plain})
    assert observe_fake_png(image) == Observation(seen=False)
    assert RuleChecker().check(image, CONTEXT).passed  # nothing a rule can see is wrong
    assert ReasonCode.NOT_SEEN in codes(image)


def test_a_jpeg_the_fake_model_cannot_read_is_refused() -> None:
    jpeg = (
        b"\xff\xd8\xff\xc0\x00\x11\x08" + (336).to_bytes(2, "big") + (640).to_bytes(2, "big") + b"\x00" * 12
    )
    image = make_image().model_copy(update={"background": jpeg, "mime_type": "image/jpeg"})
    assert read_info(jpeg) is not None
    assert RuleChecker().check(image, CONTEXT).passed
    assert ReasonCode.NOT_SEEN in codes(image)


def test_an_observer_that_cannot_tell_the_product_or_colour_is_refused() -> None:
    blind = FakeModelChecker(lambda image: Observation())
    reasons = blind.check(make_image(), CONTEXT).reasons
    assert [r.code for r in reasons] == [ReasonCode.NOT_SEEN, ReasonCode.NOT_SEEN]


# --- money at the edges ----------------------------------------------------------------------------


@pytest.mark.parametrize("price", [0.004, 0.001])
def test_a_price_that_rounds_to_zero_is_no_price(price: float) -> None:
    product = Product(item_id="tiny", name="Tiny", price=price)
    assert expected_texts(product, OfferSpec(currency="USD")) == {OverlayRole.NAME: "Tiny"}
    with pytest.raises(UnknownProductError):
        expected_texts(product, OfferSpec(currency="USD", discount_pct=20))


def test_a_discount_that_does_not_lower_the_price_to_the_cent_is_refused() -> None:
    with pytest.raises(UnknownProductError):
        expected_texts(
            Product(item_id="c", name="Cent", price=0.01), OfferSpec(currency="USD", discount_pct=20)
        )
    texts = expected_texts(
        Product(item_id="d", name="Dime", price=0.10), OfferSpec(currency="USD", discount_pct=20)
    )
    assert (texts[OverlayRole.WAS_PRICE], texts[OverlayRole.PRICE]) == ("$0.10", "$0.08")


def test_the_list_price_is_rounded_before_the_discount() -> None:
    texts = expected_texts(
        Product(item_id="r", name="R", price=10.005), OfferSpec(currency="USD", discount_pct=50)
    )
    # 10.005 rounds half up to 10.01; half of that is 5.005, which rounds half up to 5.01.
    assert (texts[OverlayRole.WAS_PRICE], texts[OverlayRole.PRICE]) == ("$10.01", "$5.01")
    assert Decimal("5.01") == Decimal(texts[OverlayRole.PRICE].removeprefix("$"))


# --- the currency is the shop's --------------------------------------------------------------------


def test_an_offer_in_another_currency_is_refused_when_the_shop_currency_is_known() -> None:
    usd = make_image(offer=OfferSpec(currency="USD", discount_pct=20))
    assert RuleChecker().check(usd, CONTEXT).passed  # no shop currency given: not checked
    inr_shop = CheckContext(catalogue=catalogue(), config=CONFIG, currency="INR")
    assert ReasonCode.CURRENCY_MISMATCH in codes(usd, inr_shop)
    assert default_checker().check(make_image(), inr_shop).passed


# --- non-Latin ids ---------------------------------------------------------------------------------


def test_a_non_latin_item_id_is_written_and_read_back() -> None:
    product = Product(item_id="जूता-1", name="जूता", category="footwear", price=799.0)
    image = make_for(product)
    info = read_info(image.background)
    assert info is not None and info.text[NOTE_DEPICTS] == "जूता-1"
    assert default_checker().check(image, CheckContext(catalogue=Catalogue([product]), config=CONFIG)).passed


# --- layout ----------------------------------------------------------------------------------------


def test_a_long_name_is_shrunk_and_the_offer_moves_to_its_own_line() -> None:
    product = Product(
        item_id="long", name="Ultra Cushion Trail Runner Pro Max Edition", category="footwear", price=2499.0
    )
    image = make_for(product)
    layers = {layer.role: layer for layer in image.overlay.layers}
    assert layers[OverlayRole.NAME].text == "Ultra Cushion Trail Runner Pro Max Edition"  # never cut
    assert layers[OverlayRole.NAME].size < 0.07
    assert layers[OverlayRole.OFFER].y > layers[OverlayRole.NAME].y
    assert layout_problems(image.overlay, image.width, image.height) == []
    assert default_checker().check(image, CheckContext(catalogue=Catalogue([product]), config=CONFIG)).passed


def test_a_name_too_long_to_fit_is_refused_not_cut() -> None:
    product = Product(item_id="huge", name="Trail " * 30, category="footwear", price=2499.0)
    image = make_for(product)
    result = RuleChecker().check(image, CheckContext(catalogue=Catalogue([product]), config=CONFIG))
    assert ReasonCode.OVERLAY_LAYOUT in {r.code for r in result.reasons}
    assert "runs off" in result.words


def test_overlapping_lines_are_refused() -> None:
    image = make_image()
    stacked = tuple(
        layer.model_copy(update={"x": 0.04, "y": 0.5, "anchor": "start"}) for layer in image.overlay.layers
    )
    spoiled = image.model_copy(update={"overlay": OverlaySpec(layers=stacked)})
    result = RuleChecker().check(spoiled, CONTEXT)
    assert ReasonCode.OVERLAY_LAYOUT in {r.code for r in result.reasons}
    assert "overlap" in result.words


def test_a_thin_strip_is_too_small() -> None:
    strip = make_png(640, 64, (30, 58, 138))
    image = make_image().model_copy(update={"background": strip, "height": 64})
    result = RuleChecker().check(image, CONTEXT)
    assert ReasonCode.TOO_SMALL in {r.code for r in result.reasons}
    assert "64 px tall" in result.words


# --- held-out plants: none of these is in the parametrized check tests -------------------------------


def held_out() -> list[tuple[str, GeneratedImage, CheckContext]]:
    stale_cat = Catalogue(
        [p if p.item_id != "shoe-0" else p.model_copy(update={"price": 999.0}) for p in catalogue()]
    )
    inr_shop = CheckContext(catalogue=catalogue(), config=CONFIG, currency="INR")
    good = make_image()
    return [
        (
            "notes_stripped",
            good.model_copy(update={"background": make_png(640, 336, (30, 58, 138))}),
            CONTEXT,
        ),
        ("stale_price", good, CheckContext(catalogue=stale_cat, config=CONFIG)),
        ("wrong_currency", make_image(offer=OfferSpec(currency="USD", discount_pct=20)), inr_shop),
        ("no_text_layers", good.model_copy(update={"overlay": OverlaySpec(layers=())}), CONTEXT),
        (
            "blind_observer_png",
            good.model_copy(update={"background": make_png(640, 336, (255, 0, 255))}),
            CONTEXT,
        ),
        (
            "thin_strip",
            good.model_copy(update={"background": make_png(640, 64, (30, 58, 138)), "height": 64}),
            CONTEXT,
        ),
    ]


def test_held_out_plants_the_parametrized_tests_do_not_name() -> None:
    missed = [name for name, image, context in held_out() if default_checker().check(image, context).passed]
    assert missed == []
