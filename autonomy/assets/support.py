"""Which formats each channel can carry, the limits that come with them, and asset checks.

The agent will learn which format works best, but it may only choose among formats the channel
can actually deliver, with an asset the channel will accept. This module is that boundary:

- `SUPPORT`: the channel x format matrix. Each cell is a `FormatSpec` with its hard limits
  (a send fails or the provider rejects it) and its guidance (it sends, but looks worse).
- `formats_for(channel)`: the formats a channel can carry, in a stable order.
- `validate(asset, channel)`: every problem with sending this asset on this channel, as data.
  Errors block the send; warnings are advice for the person reviewing the library.
- `validate_carousel(cards, channel)`: the same for a whole set of carousel cards.

Limits are the providers' published ones where they exist (WhatsApp Cloud API media limits,
Firebase's 1 MB notification image) and conservative guidance elsewhere (email). They are data
here, not code, so the Critic, the Creative and the docs all read the same numbers.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from types import MappingProxyType
from urllib.parse import urlsplit

from autonomy.assets.library import Asset, AssetKind
from autonomy.contracts.base import Channel
from autonomy.contracts.content import Format

MB = 1024 * 1024

CAROUSEL_TEMPLATE = "carousel_template"
"""The flag that unlocks WhatsApp carousels: the brand has a carousel template approved by Meta."""

ASPECT_TOLERANCE = 0.10
"""How far (relative) an image may be from a recommended aspect ratio before we say so."""

SAME_ASPECT_TOLERANCE = 0.02
"""How far apart two carousel cards' aspect ratios may be and still count as the same."""


class Severity(StrEnum):
    ERROR = "error"  # the send would fail, break a rule, or show customers something broken
    WARNING = "warning"  # it sends, but it may look poor; worth a person's look


class ProblemCode(StrEnum):
    FORMAT_NOT_SUPPORTED = "format_not_supported"
    NEEDS_FLAG = "needs_flag"
    NO_ASSET_NEEDED = "no_asset_needed"
    WRONG_KIND = "wrong_kind"
    NOT_APPROVED = "not_approved"
    MISSING_ALT_TEXT = "missing_alt_text"
    NOT_HTTPS = "not_https"
    FILE_TYPE = "file_type"
    FILE_TYPE_UNKNOWN = "file_type_unknown"
    TOO_LARGE = "too_large"
    LARGE = "large"
    SIZE_UNKNOWN = "size_unknown"
    TOO_LONG = "too_long"
    LONG = "long"
    DURATION_UNKNOWN = "duration_unknown"
    DIMENSIONS_UNKNOWN = "dimensions_unknown"
    ASPECT_RATIO = "aspect_ratio"
    TOO_NARROW = "too_narrow"
    CARD_COUNT = "card_count"
    MIXED_ASPECT = "mixed_aspect"


@dataclass(frozen=True)
class Problem:
    """One thing wrong with sending an asset on a channel, in words a marketer can act on."""

    code: ProblemCode
    severity: Severity
    message: str
    asset_id: str | None = None

    @property
    def blocking(self) -> bool:
        return self.severity is Severity.ERROR


@dataclass(frozen=True)
class AspectRatio:
    label: str  # how people say it, e.g. "1.91:1"
    value: float  # width / height


@dataclass(frozen=True)
class FormatSpec:
    """What one channel needs to carry one format. `None` means "no limit of this kind"."""

    channel: Channel
    format: Format
    asset_kinds: frozenset[AssetKind] = frozenset()  # empty: the format carries no asset
    mime_types: frozenset[str] = frozenset()  # empty: any type the channel's client can show
    max_bytes: int | None = None  # hard: the provider rejects bigger files
    recommended_max_bytes: int | None = None  # guidance: slow to load above this
    max_duration_s: float | None = None
    recommended_max_duration_s: float | None = None
    aspect_ratios: tuple[AspectRatio, ...] = ()  # guidance: any one of these looks right
    min_width: int | None = None  # guidance: narrower looks blurry
    min_cards: int | None = None  # carousels only, hard
    max_cards: int | None = None  # carousels only, hard
    same_aspect_cards: bool = False  # carousels: every card must share one aspect ratio
    requires_flag: str | None = None  # the brand setting that unlocks this cell
    note: str = ""

    @property
    def needs_asset(self) -> bool:
        return bool(self.asset_kinds)


_JPEG_PNG = frozenset({"image/jpeg", "image/png"})
_EMAIL_IMAGES = frozenset({"image/jpeg", "image/png", "image/gif"})
_WEB_IMAGES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})
_WA_VIDEO = frozenset({"video/mp4", "video/3gpp"})

_IMAGE = frozenset({AssetKind.IMAGE})
_CARDS = frozenset({AssetKind.CAROUSEL_CARD, AssetKind.IMAGE})
_VIDEO = frozenset({AssetKind.VIDEO})

_SQUARE = AspectRatio("1:1", 1.0)
_LANDSCAPE = AspectRatio("1.91:1", 1.91)
_WIDE = AspectRatio("2:1", 2.0)
_VIDEO_WIDE = AspectRatio("16:9", 16 / 9)


def _build() -> dict[tuple[Channel, Format], FormatSpec]:
    specs = [
        # Email: images are linked, not attached, so there is no hard size limit, only load time.
        FormatSpec(Channel.EMAIL, Format.TEXT, note="Subject and body."),
        FormatSpec(
            Channel.EMAIL,
            Format.TEXT_IMAGE,
            asset_kinds=_IMAGE,
            mime_types=_EMAIL_IMAGES,
            recommended_max_bytes=1 * MB,
            min_width=600,
            note="One image above or inside the body; 600 px is the usual email width.",
        ),
        FormatSpec(
            Channel.EMAIL,
            Format.CAROUSEL,
            asset_kinds=_CARDS,
            mime_types=_EMAIL_IMAGES,
            recommended_max_bytes=1 * MB,
            min_width=600,
            min_cards=2,
            max_cards=10,
            note="Email cannot swipe, so a carousel is sent as images stacked one under another.",
        ),
        # WhatsApp: only pre-approved templates may open a conversation; media goes in the header.
        FormatSpec(Channel.WHATSAPP, Format.TEXT, note="Template body with parameters."),
        FormatSpec(
            Channel.WHATSAPP,
            Format.TEXT_IMAGE,
            asset_kinds=_IMAGE,
            mime_types=_JPEG_PNG,
            max_bytes=5 * MB,
            aspect_ratios=(_LANDSCAPE,),
            note="Image header on an approved template; JPEG or PNG up to 5 MB.",
        ),
        FormatSpec(
            Channel.WHATSAPP,
            Format.VIDEO,
            asset_kinds=_VIDEO,
            mime_types=_WA_VIDEO,
            max_bytes=16 * MB,
            recommended_max_duration_s=60.0,
            aspect_ratios=(_VIDEO_WIDE,),
            note="Video header on an approved template; MP4 or 3GPP up to 16 MB.",
        ),
        FormatSpec(
            Channel.WHATSAPP,
            Format.CAROUSEL,
            asset_kinds=_CARDS,
            mime_types=_JPEG_PNG,
            max_bytes=5 * MB,
            aspect_ratios=(_SQUARE, _LANDSCAPE),
            min_cards=2,
            max_cards=10,
            same_aspect_cards=True,
            requires_flag=CAROUSEL_TEMPLATE,
            note="Only with an approved carousel template: 2 to 10 cards, all the same shape.",
        ),
        FormatSpec(Channel.SMS, Format.TEXT, note="Text only; MMS is out of scope."),
        FormatSpec(Channel.PUSH, Format.TEXT, note="Title and body."),
        FormatSpec(
            Channel.PUSH,
            Format.TEXT_IMAGE,
            asset_kinds=_IMAGE,
            mime_types=_JPEG_PNG,
            max_bytes=1 * MB,
            aspect_ratios=(_WIDE,),
            note="Rich notification image; Firebase limits it to 1 MB, Android shows it at 2:1.",
        ),
        FormatSpec(Channel.WEB, Format.TEXT, note="On-site message or banner text."),
        FormatSpec(
            Channel.WEB,
            Format.TEXT_IMAGE,
            asset_kinds=_IMAGE,
            mime_types=_WEB_IMAGES,
            recommended_max_bytes=1 * MB,
            note="On-site banner with one image.",
        ),
        # Webhook: the client's own tool renders the message; we pass the asset URL along.
        FormatSpec(Channel.WEBHOOK, Format.TEXT, note="The client's tool renders it."),
        FormatSpec(Channel.WEBHOOK, Format.TEXT_IMAGE, asset_kinds=_IMAGE, note="We pass the URL."),
        FormatSpec(Channel.WEBHOOK, Format.CAROUSEL, asset_kinds=_CARDS, note="We pass the URLs."),
        FormatSpec(Channel.WEBHOOK, Format.VIDEO, asset_kinds=_VIDEO, note="We pass the URL."),
    ]
    return {(s.channel, s.format): s for s in specs}


SUPPORT: Mapping[tuple[Channel, Format], FormatSpec] = MappingProxyType(_build())
"""The channel x format matrix. A missing cell means the channel cannot carry that format."""

_KIND_FORMAT = {
    AssetKind.IMAGE: Format.TEXT_IMAGE,
    AssetKind.VIDEO: Format.VIDEO,
    AssetKind.CAROUSEL_CARD: Format.CAROUSEL,
}

_EXTENSION_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".3gp": "video/3gpp",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
}


def spec_for(channel: Channel, fmt: Format) -> FormatSpec | None:
    """The matrix cell, or None when the channel cannot carry the format at all."""
    return SUPPORT.get((channel, fmt))


def supports(channel: Channel, fmt: Format, *, carousel_template: bool = False) -> bool:
    """True when the channel can carry the format for this brand, flags considered."""
    spec = spec_for(channel, fmt)
    if spec is None:
        return False
    return spec.requires_flag != CAROUSEL_TEMPLATE or carousel_template


def formats_for(channel: Channel, *, carousel_template: bool = False) -> tuple[Format, ...]:
    """The formats a channel can carry, in `Format` order (text first), flags considered."""
    return tuple(f for f in Format if supports(channel, f, carousel_template=carousel_template))


def format_for_kind(kind: AssetKind) -> Format:
    """The format an asset of this kind is normally sent as."""
    return _KIND_FORMAT[kind]


def mime_type_of(asset: Asset) -> str | None:
    """The asset's declared type, or a guess from its URL's extension, or None."""
    if asset.mime_type:
        return asset.mime_type.strip().lower()
    suffix = PurePosixPath(urlsplit(asset.url).path).suffix.lower()
    return _EXTENSION_MIME.get(suffix)


def _mb(n: int) -> str:
    return f"{n / MB:.1f} MB"


def _asset_problems(asset: Asset) -> list[Problem]:
    """Problems with the asset whatever the channel: approval, alt text, a fetchable URL."""
    problems: list[Problem] = []

    def add(code: ProblemCode, message: str) -> None:
        problems.append(Problem(code, Severity.ERROR, message, asset.asset_id))

    if not asset.is_approved:
        add(ProblemCode.NOT_APPROVED, f"Asset is {asset.status.value}; only approved assets are sent.")
    if not asset.alt_text.strip():
        add(ProblemCode.MISSING_ALT_TEXT, "Alt text is blank; describe what the asset shows.")
    if urlsplit(asset.url).scheme.lower() != "https":
        add(ProblemCode.NOT_HTTPS, "URL must be https so the channel can fetch it safely.")
    return problems


def _channel_problems(asset: Asset, spec: FormatSpec) -> list[Problem]:
    """Problems with the asset against one matrix cell's limits and guidance."""
    problems: list[Problem] = []
    where = f"{spec.channel.value} {spec.format.value}"

    def add(code: ProblemCode, severity: Severity, message: str) -> None:
        problems.append(Problem(code, severity, message, asset.asset_id))

    if spec.mime_types:
        mime = mime_type_of(asset)
        if mime is None:
            add(ProblemCode.FILE_TYPE_UNKNOWN, Severity.WARNING, "File type unknown; set mime_type.")
        elif mime not in spec.mime_types:
            allowed = ", ".join(sorted(spec.mime_types))
            add(ProblemCode.FILE_TYPE, Severity.ERROR, f"{mime} is not accepted on {where} ({allowed}).")

    if spec.max_bytes is not None or spec.recommended_max_bytes is not None:
        if asset.size_bytes is None:
            add(
                ProblemCode.SIZE_UNKNOWN,
                Severity.WARNING,
                "File size unknown; set size_bytes to check limits.",
            )
        elif spec.max_bytes is not None and asset.size_bytes > spec.max_bytes:
            add(
                ProblemCode.TOO_LARGE,
                Severity.ERROR,
                f"{_mb(asset.size_bytes)} is over the {_mb(spec.max_bytes)} limit on {where}.",
            )
        elif spec.recommended_max_bytes is not None and asset.size_bytes > spec.recommended_max_bytes:
            add(
                ProblemCode.LARGE,
                Severity.WARNING,
                f"{_mb(asset.size_bytes)} is over the suggested {_mb(spec.recommended_max_bytes)}; "
                "it may load slowly.",
            )

    if asset.kind is AssetKind.VIDEO and (
        spec.max_duration_s is not None or spec.recommended_max_duration_s is not None
    ):
        if asset.duration_s is None:
            add(ProblemCode.DURATION_UNKNOWN, Severity.WARNING, "Video length unknown; set duration_s.")
        elif spec.max_duration_s is not None and asset.duration_s > spec.max_duration_s:
            add(
                ProblemCode.TOO_LONG,
                Severity.ERROR,
                f"{asset.duration_s:g}s is over the {spec.max_duration_s:g}s limit on {where}.",
            )
        elif (
            spec.recommended_max_duration_s is not None and asset.duration_s > spec.recommended_max_duration_s
        ):
            add(
                ProblemCode.LONG,
                Severity.WARNING,
                f"{asset.duration_s:g}s is longer than the suggested {spec.recommended_max_duration_s:g}s.",
            )

    if spec.aspect_ratios or spec.min_width is not None:
        ratio = asset.aspect_ratio
        if ratio is None or asset.width is None:
            add(ProblemCode.DIMENSIONS_UNKNOWN, Severity.WARNING, "Width and height unknown; set them.")
        else:
            if spec.aspect_ratios and not any(
                abs(ratio - r.value) / r.value <= ASPECT_TOLERANCE for r in spec.aspect_ratios
            ):
                labels = " or ".join(r.label for r in spec.aspect_ratios)
                add(
                    ProblemCode.ASPECT_RATIO,
                    Severity.WARNING,
                    f"{asset.width}x{asset.height} will be cropped or letterboxed; {where} suits {labels}.",
                )
            if spec.min_width is not None and asset.width < spec.min_width:
                add(
                    ProblemCode.TOO_NARROW,
                    Severity.WARNING,
                    f"{asset.width} px wide may look blurry; use at least {spec.min_width} px.",
                )
    return problems


def validate(
    asset: Asset,
    channel: Channel,
    fmt: Format | None = None,
    *,
    carousel_template: bool = False,
) -> list[Problem]:
    """Every problem with sending `asset` on `channel` as `fmt`. Empty means it is fine.

    `fmt` defaults to the asset's natural format (image: text_image, video: video, card:
    carousel). Pass it to check, say, an image used as one card of an email carousel. Errors
    (`Problem.blocking`) must stop the send; warnings are advice for the library's reviewer.
    """
    fmt = fmt if fmt is not None else format_for_kind(asset.kind)
    problems = _asset_problems(asset)
    spec = spec_for(channel, fmt)
    if spec is None:
        problems.append(
            Problem(
                ProblemCode.FORMAT_NOT_SUPPORTED,
                Severity.ERROR,
                f"{channel.value} cannot carry {fmt.value}; it carries "
                f"{', '.join(f.value for f in formats_for(channel)) or 'nothing'}.",
                asset.asset_id,
            )
        )
        return problems
    if spec.requires_flag == CAROUSEL_TEMPLATE and not carousel_template:
        problems.append(
            Problem(
                ProblemCode.NEEDS_FLAG,
                Severity.ERROR,
                f"{channel.value} {fmt.value} needs an approved carousel template ({CAROUSEL_TEMPLATE}).",
                asset.asset_id,
            )
        )
    if not spec.needs_asset:
        problems.append(
            Problem(
                ProblemCode.NO_ASSET_NEEDED,
                Severity.ERROR,
                f"{fmt.value} carries no asset; choose a format with an image or video.",
                asset.asset_id,
            )
        )
        return problems
    if asset.kind not in spec.asset_kinds:
        kinds = ", ".join(sorted(k.value for k in spec.asset_kinds))
        problems.append(
            Problem(
                ProblemCode.WRONG_KIND,
                Severity.ERROR,
                f"{channel.value} {fmt.value} takes {kinds}, not {asset.kind.value}.",
                asset.asset_id,
            )
        )
        return problems
    problems.extend(_channel_problems(asset, spec))
    return problems


def validate_carousel(
    cards: Sequence[Asset],
    channel: Channel,
    *,
    carousel_template: bool = False,
) -> list[Problem]:
    """Every problem with sending these cards, in this order, as one carousel on `channel`."""
    problems: list[Problem] = []
    for card in cards:
        problems.extend(validate(card, channel, Format.CAROUSEL, carousel_template=carousel_template))
    spec = spec_for(channel, Format.CAROUSEL)
    if spec is None:
        return problems
    count = len(cards)
    low, high = spec.min_cards, spec.max_cards
    if (low is not None and count < low) or (high is not None and count > high):
        problems.append(
            Problem(
                ProblemCode.CARD_COUNT,
                Severity.ERROR,
                f"{count} cards; {channel.value} carousels need {low} to {high}.",
            )
        )
    if spec.same_aspect_cards:
        ratios = [r for c in cards if (r := c.aspect_ratio) is not None]
        if ratios and (max(ratios) - min(ratios)) / min(ratios) > SAME_ASPECT_TOLERANCE:
            problems.append(
                Problem(
                    ProblemCode.MIXED_ASPECT,
                    Severity.ERROR,
                    f"{channel.value} carousel cards must all have the same shape.",
                )
            )
    return problems


def blocking(problems: Iterable[Problem]) -> list[Problem]:
    """Only the problems that must stop a send."""
    return [p for p in problems if p.blocking]


def is_sendable(
    asset: Asset,
    channel: Channel,
    fmt: Format | None = None,
    *,
    carousel_template: bool = False,
) -> bool:
    """True when `validate` finds no blocking problem."""
    return not blocking(validate(asset, channel, fmt, carousel_template=carousel_template))
