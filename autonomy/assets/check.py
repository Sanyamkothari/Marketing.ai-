"""The vision check: a generated image is looked at before any person sees it, and a bad one is refused.

Two checkers share the `VisionChecker` protocol:

- `RuleChecker` needs no model. It checks what code can know: the file really is a PNG or JPEG of the
  declared size, it is wide enough and small enough, the alt text is there, the product is the
  catalogue's product, and the text drawn on it is exactly the text the catalogue and the offer give.
- `FakeModelChecker` stands in for a vision model. It asks an observer what the picture shows (which
  product, whether the logo is whole, whether the model wrote text of its own, which colours) and
  holds that against the brand. The default observer reads the notes `FakeImageProvider` leaves in
  its PNGs. A real model replaces the observer (decision V-2).

`CompositeChecker` runs several and merges their reasons. A result that is not `passed` carries every
reason, in words, so the refusal can be shown.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import Field

from autonomy.assets.generate import (
    NOTE_COLOUR,
    NOTE_DEPICTS,
    NOTE_LOGO,
    NOTE_TEXT,
    GeneratedImage,
    ImageConfig,
    OverlayRole,
    UnknownProductError,
    colour_distance,
    expected_texts,
    parse_hex,
)
from autonomy.assets.pngio import read_info, sniff_mime
from autonomy.catalog.products import Catalogue
from autonomy.contracts.base import Contract

__all__ = [
    "CheckContext",
    "CheckResult",
    "CompositeChecker",
    "FakeModelChecker",
    "Observation",
    "Reason",
    "ReasonCode",
    "RuleChecker",
    "VisionChecker",
    "default_checker",
    "observe_fake_png",
]


class ReasonCode(StrEnum):
    """Why an image was refused. The first group is rule checks, the second is what a model saw."""

    BAD_FORMAT = "bad_format"
    DIMENSION_MISMATCH = "dimension_mismatch"
    TOO_SMALL = "too_small"
    TOO_LARGE = "too_large"
    ALT_TEXT_MISSING = "alt_text_missing"
    UNKNOWN_PRODUCT = "unknown_product"
    PRODUCT_MISMATCH = "product_mismatch"
    OVERLAY_TEXT_MISSING = "overlay_text_missing"
    OVERLAY_TEXT_MISMATCH = "overlay_text_mismatch"
    OVERLAY_TEXT_UNEXPECTED = "overlay_text_unexpected"
    WRONG_PRODUCT_SEEN = "wrong_product_seen"
    LOGO_BROKEN = "logo_broken"
    TEXT_GARBLED = "text_garbled"
    OFF_BRAND_COLOURS = "off_brand_colours"
    NOT_READABLE = "not_readable"


class Reason(Contract):
    code: ReasonCode
    message: str


class CheckResult(Contract):
    """The verdict. `passed` is true only when there is no reason."""

    checker: str
    reasons: tuple[Reason, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.reasons

    @property
    def words(self) -> str:
        """One line for a person: 'Passed.' or every reason joined."""
        if self.passed:
            return "Passed."
        return " ".join(r.message for r in self.reasons)


@dataclass(frozen=True)
class CheckContext:
    """What a check holds an image against: the catalogue and the limits."""

    catalogue: Catalogue
    config: ImageConfig


@runtime_checkable
class VisionChecker(Protocol):
    name: str

    def check(self, image: GeneratedImage, context: CheckContext) -> CheckResult:
        """Look at the image; list every reason it must not be used. Never raises on a bad image."""
        ...


class RuleChecker:
    """Everything that can be checked without a model."""

    name = "rules"

    def check(self, image: GeneratedImage, context: CheckContext) -> CheckResult:
        reasons: list[Reason] = []

        def add(code: ReasonCode, message: str) -> None:
            reasons.append(Reason(code=code, message=message))

        self._file(image, context.config, add)
        if not image.alt_text.strip():
            add(ReasonCode.ALT_TEXT_MISSING, "The image has no alt text.")
        self._product_and_text(image, context.catalogue, add)
        return CheckResult(checker=self.name, reasons=tuple(reasons))

    @staticmethod
    def _file(image: GeneratedImage, config: ImageConfig, add: Callable[[ReasonCode, str], None]) -> None:
        sniffed = sniff_mime(image.background)
        if sniffed is None or sniffed not in config.allowed_mimes:
            allowed = ", ".join(config.allowed_mimes)
            add(
                ReasonCode.BAD_FORMAT,
                f"The file is {sniffed or 'not a known image type'}; allowed: {allowed}.",
            )
        elif sniffed != image.mime_type:
            add(ReasonCode.BAD_FORMAT, f"The file is {sniffed} but is labelled {image.mime_type}.")
        info = read_info(image.background)
        if info is None:
            add(ReasonCode.NOT_READABLE, "The file's header cannot be read; it may be damaged.")
        elif (info.width, info.height) != (image.width, image.height):
            add(
                ReasonCode.DIMENSION_MISMATCH,
                f"The file is {info.width}x{info.height} but is declared {image.width}x{image.height}.",
            )
        width = info.width if info is not None else image.width
        if width < config.min_width:
            add(
                ReasonCode.TOO_SMALL,
                f"The image is {width} px wide; at least {config.min_width} px is needed.",
            )
        if len(image.background) > config.max_bytes:
            add(
                ReasonCode.TOO_LARGE,
                f"The file is {len(image.background)} bytes; at most {config.max_bytes} bytes are allowed.",
            )

    @staticmethod
    def _product_and_text(
        image: GeneratedImage, catalogue: Catalogue, add: Callable[[ReasonCode, str], None]
    ) -> None:
        product = catalogue.get(image.item_id)
        if product is None:
            add(ReasonCode.UNKNOWN_PRODUCT, f"Product {image.item_id!r} is not in the catalogue.")
            return
        if image.provider_shows_item_id != image.item_id:
            add(
                ReasonCode.PRODUCT_MISMATCH,
                f"The image was made for {image.provider_shows_item_id!r}, not for {image.item_id!r}.",
            )
        if image.category != product.category:
            add(
                ReasonCode.PRODUCT_MISMATCH,
                f"The image is filed under category {image.category!r}; the catalogue has {product.category!r}.",
            )
        try:
            wanted = expected_texts(product, image.offer)
        except UnknownProductError as err:
            add(ReasonCode.OVERLAY_TEXT_MISMATCH, str(err))
            return
        found: dict[OverlayRole, str] = {}
        for layer in image.overlay.layers:
            if layer.role in found:
                add(ReasonCode.OVERLAY_TEXT_UNEXPECTED, f"The {layer.role.value} text appears twice.")
            found[layer.role] = layer.text
        for role, text in wanted.items():
            if role not in found:
                add(ReasonCode.OVERLAY_TEXT_MISSING, f"The {role.value} text {text!r} is missing.")
            elif found[role] != text:
                add(
                    ReasonCode.OVERLAY_TEXT_MISMATCH,
                    f"The {role.value} text reads {found[role]!r}; the offer says {text!r}.",
                )
        for role, text in found.items():
            if role not in wanted:
                add(
                    ReasonCode.OVERLAY_TEXT_UNEXPECTED,
                    f"The image carries {role.value} text {text!r} that the offer does not call for.",
                )


class Observation(Contract):
    """What a vision model reports about a picture."""

    depicts_item_id: str | None = Field(default=None, description="The product it recognises")
    logo_intact: bool = True
    garbled_text: bool = Field(
        default=False, description="Text the picture itself contains that is unreadable"
    )
    dominant_colour: str | None = Field(default=None, description="#rrggbb")


Observer = Callable[[GeneratedImage], Observation]


def observe_fake_png(image: GeneratedImage) -> Observation:
    """Read the notes `FakeImageProvider` left in a PNG, as a stand-in for looking at the pixels.

    A file that is not a readable PNG gives an Observation with nothing flagged: the rule checker
    already refuses such files.
    """
    info = read_info(image.background)
    if info is None or not info.text:
        return Observation(depicts_item_id=image.item_id)
    return Observation(
        depicts_item_id=info.text.get(NOTE_DEPICTS),
        logo_intact=info.text.get(NOTE_LOGO, "ok") == "ok",
        garbled_text=info.text.get(NOTE_TEXT, "none") != "none",
        dominant_colour=info.text.get(NOTE_COLOUR),
    )


class FakeModelChecker:
    """A deterministic model-based check: what the observer saw against what the brand needs."""

    name = "fake-model"

    def __init__(self, observer: Observer = observe_fake_png) -> None:
        self._observe = observer

    def check(self, image: GeneratedImage, context: CheckContext) -> CheckResult:
        seen = self._observe(image)
        reasons: list[Reason] = []
        if seen.depicts_item_id is not None and seen.depicts_item_id != image.item_id:
            reasons.append(
                Reason(
                    code=ReasonCode.WRONG_PRODUCT_SEEN,
                    message=f"The picture shows {seen.depicts_item_id!r}, not {image.item_id!r}.",
                )
            )
        if not seen.logo_intact:
            reasons.append(Reason(code=ReasonCode.LOGO_BROKEN, message="The logo in the picture is broken."))
        if seen.garbled_text:
            reasons.append(
                Reason(
                    code=ReasonCode.TEXT_GARBLED,
                    message="The picture contains text of its own; all words must be drawn by code.",
                )
            )
        if seen.dominant_colour is not None:
            seen_rgb = parse_hex(seen.dominant_colour)
            nearest = min(colour_distance(seen_rgb, parse_hex(c)) for c in image.style.palette)
            if nearest > context.config.colour_tolerance:
                reasons.append(
                    Reason(
                        code=ReasonCode.OFF_BRAND_COLOURS,
                        message=f"The picture's main colour {seen.dominant_colour} is not close to the brand colours.",
                    )
                )
        return CheckResult(checker=self.name, reasons=tuple(reasons))


class CompositeChecker:
    """Runs every checker and merges their reasons. A reason is kept once per code and message."""

    name = "composite"

    def __init__(self, checkers: Sequence[VisionChecker]) -> None:
        if not checkers:
            raise ValueError("a composite checker needs at least one checker")
        self._checkers = tuple(checkers)

    def check(self, image: GeneratedImage, context: CheckContext) -> CheckResult:
        seen: set[tuple[ReasonCode, str]] = set()
        reasons: list[Reason] = []
        for checker in self._checkers:
            for reason in checker.check(image, context).reasons:
                key = (reason.code, reason.message)
                if key not in seen:
                    seen.add(key)
                    reasons.append(reason)
        return CheckResult(checker=self.name, reasons=tuple(reasons))


def default_checker() -> VisionChecker:
    """The rule checks and the fake model check together."""
    return CompositeChecker([RuleChecker(), FakeModelChecker()])
