"""The vision check refuses each planted bad image, with a reason, and passes the good ones."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from autonomy.assets.check import (
    CheckContext,
    CompositeChecker,
    FakeModelChecker,
    Observation,
    ReasonCode,
    RuleChecker,
    VisionChecker,
    default_checker,
)
from autonomy.assets.generate import (
    Flaw,
    GeneratedImage,
    ImageConfig,
    OverlayRole,
    OverlaySpec,
    TextLayer,
)
from autonomy.assets.pngio import make_png
from tests.v3.images.support import CONFIG, catalogue, make_image

CONTEXT = CheckContext(catalogue=catalogue(), config=CONFIG)
Plant = Callable[[GeneratedImage], GeneratedImage]


def with_layer_text(role: OverlayRole, text: str) -> Plant:
    def plant(image: GeneratedImage) -> GeneratedImage:
        layers = tuple(
            layer.model_copy(update={"text": text}) if layer.role is role else layer
            for layer in image.overlay.layers
        )
        return image.model_copy(update={"overlay": OverlaySpec(layers=layers)})

    return plant


def without_layer(role: OverlayRole) -> Plant:
    def plant(image: GeneratedImage) -> GeneratedImage:
        layers = tuple(layer for layer in image.overlay.layers if layer.role is not role)
        return image.model_copy(update={"overlay": OverlaySpec(layers=layers)})

    return plant


def with_extra_layer(role: OverlayRole, text: str) -> Plant:
    def plant(image: GeneratedImage) -> GeneratedImage:
        extra = TextLayer(role=role, text=text, x=0.5, y=0.5, size=0.05)
        return image.model_copy(update={"overlay": OverlaySpec(layers=(*image.overlay.layers, extra))})

    return plant


def with_fields(**fields: object) -> Plant:
    return lambda image: image.model_copy(update=fields)


# (name, how to spoil a good image, the reason the checker must give)
RULE_PLANTS: list[tuple[str, Plant, ReasonCode]] = [
    (
        "gif_bytes",
        with_fields(background=b"GIF89a" + b"\x00" * 40, mime_type="image/gif"),
        ReasonCode.BAD_FORMAT,
    ),
    ("not_an_image", with_fields(background=b"<html>oops</html>"), ReasonCode.BAD_FORMAT),
    ("label_disagrees", with_fields(mime_type="image/jpeg"), ReasonCode.BAD_FORMAT),
    (
        "damaged_png",
        lambda i: i.model_copy(update={"background": i.background[: len(i.background) // 2]}),
        ReasonCode.NOT_READABLE,
    ),
    ("declared_size_wrong", with_fields(width=641), ReasonCode.DIMENSION_MISMATCH),
    (
        "too_narrow",
        with_fields(background=make_png(320, 168, (30, 58, 138)), width=320, height=168),
        ReasonCode.TOO_SMALL,
    ),
    ("alt_text_blank", with_fields(alt_text="   "), ReasonCode.ALT_TEXT_MISSING),
    ("price_text_wrong", with_layer_text(OverlayRole.PRICE, "₹939.60"), ReasonCode.OVERLAY_TEXT_MISMATCH),
    ("offer_text_wrong", with_layer_text(OverlayRole.OFFER, "30% off"), ReasonCode.OVERLAY_TEXT_MISMATCH),
    (
        "was_price_wrong",
        with_layer_text(OverlayRole.WAS_PRICE, "₹1,299.00"),
        ReasonCode.OVERLAY_TEXT_MISMATCH,
    ),
    ("name_text_wrong", with_layer_text(OverlayRole.NAME, "Runner 9"), ReasonCode.OVERLAY_TEXT_MISMATCH),
    ("price_text_missing", without_layer(OverlayRole.PRICE), ReasonCode.OVERLAY_TEXT_MISSING),
    ("offer_text_missing", without_layer(OverlayRole.OFFER), ReasonCode.OVERLAY_TEXT_MISSING),
    ("price_twice", with_extra_layer(OverlayRole.PRICE, "₹1,039.60"), ReasonCode.OVERLAY_TEXT_UNEXPECTED),
    ("product_mismatch_metadata", with_fields(provider_shows_item_id="shoe-1"), ReasonCode.PRODUCT_MISMATCH),
    ("category_wrong", with_fields(category="gear"), ReasonCode.PRODUCT_MISMATCH),
    ("not_in_catalogue", with_fields(item_id="ghost"), ReasonCode.UNKNOWN_PRODUCT),
]

MODEL_FLAWS: list[tuple[Flaw, ReasonCode]] = [
    (Flaw.WRONG_PRODUCT, ReasonCode.WRONG_PRODUCT_SEEN),
    (Flaw.BROKEN_LOGO, ReasonCode.LOGO_BROKEN),
    (Flaw.GARBLED_TEXT, ReasonCode.TEXT_GARBLED),
    (Flaw.OFF_BRAND_COLOUR, ReasonCode.OFF_BRAND_COLOURS),
]


def good_images() -> list[GeneratedImage]:
    return [make_image(f"shoe-{i}", t) for i in range(3) for t in ("weekend-runners", "bargain-hunters")] + [
        make_image("bottle")
    ]


def test_good_images_pass_every_checker() -> None:
    for image in good_images():
        for checker in (RuleChecker(), FakeModelChecker(), default_checker()):
            result = checker.check(image, CONTEXT)
            assert result.passed, (image.item_id, result.words)
            assert result.words == "Passed."


@pytest.mark.parametrize(("name", "plant", "code"), RULE_PLANTS, ids=[p[0] for p in RULE_PLANTS])
def test_rule_checker_refuses_each_planted_bad_image(name: str, plant: Plant, code: ReasonCode) -> None:
    result = RuleChecker().check(plant(make_image()), CONTEXT)
    assert not result.passed, name
    assert code in {r.code for r in result.reasons}, (name, result.words)
    assert result.words != "Passed."


def test_a_file_over_the_size_limit_is_refused() -> None:
    small = CheckContext(
        catalogue=catalogue(), config=ImageConfig(enabled=True, width=640, height=336, max_bytes=100)
    )
    result = RuleChecker().check(make_image(), small)
    assert ReasonCode.TOO_LARGE in {r.code for r in result.reasons}


def test_an_extra_text_line_the_offer_does_not_call_for_is_refused() -> None:
    from autonomy.assets.generate import OfferSpec

    plain = make_image(offer=OfferSpec(currency="INR"))
    spoiled = with_extra_layer(OverlayRole.OFFER, "50% off")(plain)
    result = RuleChecker().check(spoiled, CONTEXT)
    assert ReasonCode.OVERLAY_TEXT_UNEXPECTED in {r.code for r in result.reasons}


@pytest.mark.parametrize(("flaw", "code"), MODEL_FLAWS, ids=[f.value for f, _ in MODEL_FLAWS])
def test_model_checker_refuses_each_planted_flaw(flaw: Flaw, code: ReasonCode) -> None:
    image = make_image(flaws={"shoe-0": [flaw]})
    result = FakeModelChecker().check(image, CONTEXT)
    assert code in {r.code for r in result.reasons}
    # The rule checks cannot see inside the picture, so they pass it: the model check is what catches it.
    assert RuleChecker().check(image, CONTEXT).passed


def test_a_model_planted_wrong_product_is_not_hidden_by_honest_metadata() -> None:
    image = make_image(flaws={"shoe-0": [Flaw.WRONG_PRODUCT]})
    assert image.provider_shows_item_id == "shoe-0"
    assert not default_checker().check(image, CONTEXT).passed


def test_composite_merges_reasons_once() -> None:
    image = make_image(flaws={"shoe-0": [Flaw.BROKEN_LOGO, Flaw.GARBLED_TEXT]})
    spoiled = with_fields(alt_text="")(image)
    result = CompositeChecker([RuleChecker(), FakeModelChecker(), FakeModelChecker()]).check(spoiled, CONTEXT)
    codes = [r.code for r in result.reasons]
    assert sorted(codes) == sorted(
        {ReasonCode.ALT_TEXT_MISSING, ReasonCode.LOGO_BROKEN, ReasonCode.TEXT_GARBLED}
    )
    with pytest.raises(ValueError):
        CompositeChecker([])


def test_fake_model_checker_takes_any_observer() -> None:
    seen = FakeModelChecker(lambda image: Observation(depicts_item_id="bottle", logo_intact=False))
    codes = {r.code for r in seen.check(make_image(), CONTEXT).reasons}
    assert codes == {ReasonCode.WRONG_PRODUCT_SEEN, ReasonCode.LOGO_BROKEN}


def test_checkers_share_the_protocol() -> None:
    for checker in (RuleChecker(), FakeModelChecker(), default_checker()):
        assert isinstance(checker, VisionChecker)


def planted_eval_set() -> list[tuple[str, GeneratedImage]]:
    """The fixed set: every rule plant on every shoe, and every model flaw on every shoe and the bottle."""
    planted: list[tuple[str, GeneratedImage]] = []
    for i in range(3):
        for name, plant, _ in RULE_PLANTS:
            planted.append((f"{name}/shoe-{i}", plant(make_image(f"shoe-{i}"))))
    for item in ("shoe-0", "shoe-1", "shoe-2", "bottle"):
        for flaw, _ in MODEL_FLAWS:
            planted.append((f"{flaw.value}/{item}", make_image(item, flaws={item: [flaw]})))
    return planted


def test_the_default_checker_catches_at_least_95_percent_of_a_fixed_eval_set() -> None:
    """Plan-v3 decision 5 pass test, with the fake model: the pipeline's catch rate, not a real model's."""
    checker = default_checker()
    planted = planted_eval_set()
    missed = [name for name, image in planted if checker.check(image, CONTEXT).passed]
    caught = len(planted) - len(missed)
    assert len(planted) >= 60
    assert caught / len(planted) >= 0.95, missed
    # And it does not cry wolf on the good ones.
    assert all(checker.check(image, CONTEXT).passed for image in good_images())
