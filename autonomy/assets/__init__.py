"""Assets and formats: the brand's approved images and videos, and which channel can carry what.

- `library`: the `Asset` model and the `AssetLibrary` protocol (in memory, or a JSON directory).
  Every lookup returns approved assets only unless asked otherwise.
- `support`: the channel x format matrix with its limits, `formats_for(channel)`, and
  `validate(asset, channel)`, which lists every problem with a send as data.

The agent never generates an image or a video here; it chooses among what the brand approved.
See docs/v2/FORMATS.md for the strategy.
"""

from autonomy.assets.library import (
    Asset,
    AssetKind,
    AssetLibrary,
    AssetStatus,
    InMemoryAssetLibrary,
    JsonDirAssetLibrary,
    candidates,
    carousel_set,
    matches,
)
from autonomy.assets.support import (
    CAROUSEL_TEMPLATE,
    SUPPORT,
    AspectRatio,
    FormatSpec,
    Problem,
    ProblemCode,
    Severity,
    blocking,
    format_for_kind,
    formats_for,
    is_sendable,
    mime_type_of,
    spec_for,
    supports,
    validate,
    validate_carousel,
)

__all__ = [
    "CAROUSEL_TEMPLATE",
    "SUPPORT",
    "AspectRatio",
    "Asset",
    "AssetKind",
    "AssetLibrary",
    "AssetStatus",
    "FormatSpec",
    "InMemoryAssetLibrary",
    "JsonDirAssetLibrary",
    "Problem",
    "ProblemCode",
    "Severity",
    "blocking",
    "candidates",
    "carousel_set",
    "format_for_kind",
    "formats_for",
    "is_sendable",
    "matches",
    "mime_type_of",
    "spec_for",
    "supports",
    "validate",
    "validate_carousel",
]
