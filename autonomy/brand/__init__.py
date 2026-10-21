"""The brand kit, read from the shop's own tools: zero-touch setup, item 3 (docs/v2/ZERO_TOUCH.md).

We read these, never invent them:

- **Voice** from the shop's Klaviyo templates and recent sent campaigns (`read.read_klaviyo_copy`):
  greeting, sign-off, the words they use, tone (measured; a model may refine it only by picking from
  fixed lists, within one step of the measure).
- **Hard rules** the Critic enforces on every message (`kit.VoiceRules`, `rules.voice_problems`):
  hype words their copy never uses, their subject and SMS lengths, emoji and '!' per message, and
  the discount forms their copy states. Each needs enough of their copy; with too little it is left
  out (None), and `BrandKit.notes` says so.
- **Product names and images** from Shopify (`read.read_shopify_brand`): the shop's own product
  photos become `autonomy.assets` records (`store.record_assets`), DRAFT until the one approval,
  then APPROVED with who approved; the record each came from stays on the kit (`KitAsset.source`).
- **Logo and colours** from the shop's Shopify brand settings, else its email templates.

Every rule and style choice is a `Finding` ("what we found -> what we will use") with its
`Evidence` (source, how many messages showed it, of how many read).

Flow (`api/v2/onboarding_brand.py`): read (`read.read_brand_sources` -> `derive.derive_kit` ->
`store.save_read`), show three samples for the goal we propose first (`samples.sample_messages`),
and one approval (`store.approve`) accepts the voice, records who and when with the kit's
fingerprint and the samples shown, and approves the product images. A later read that changes
anything the approval covers needs a new approval; one that changes nothing keeps it.

Who uses it: `Creative.build_library(..., kit=)` writes in the voice (template copy too, so it
works with no model configured) and `BrandRules.from_brief(..., kit=)` gives the Critic the hard
rules. The hard rules only ever narrow what may be sent, so they apply from the first read; the
voice and the product images are meant to be used once the kit is approved (`BrandKit.approved`).

`samples` imports the agents and is not imported here (the agents import `kit` and `rules`).
"""

from autonomy.brand.derive import (
    MIN_MESSAGES,
    BrandSources,
    ProductImage,
    ShopFacts,
    ToneSummary,
    derive_kit,
    summarise_tone,
)
from autonomy.brand.kit import (
    AssetSource,
    BrandApproval,
    BrandColour,
    BrandKit,
    BrandLogo,
    CopySample,
    Evidence,
    Finding,
    KitAsset,
    VoiceRules,
    VoiceStyle,
)
from autonomy.brand.read import active_connection, read_brand_sources, read_klaviyo_copy, read_shopify_brand
from autonomy.brand.rules import apply_voice, offer_in_style, voice_problems
from autonomy.brand.store import (
    BrandKitStore,
    InMemoryBrandKitStore,
    SqlAssetLibrary,
    SqlBrandKitStore,
    StaleKitError,
    approve,
    create_brand_tables,
    record_assets,
    save_read,
)

__all__ = [
    "MIN_MESSAGES",
    "AssetSource",
    "BrandApproval",
    "BrandColour",
    "BrandKit",
    "BrandKitStore",
    "BrandLogo",
    "BrandSources",
    "CopySample",
    "Evidence",
    "Finding",
    "InMemoryBrandKitStore",
    "KitAsset",
    "ProductImage",
    "ShopFacts",
    "SqlAssetLibrary",
    "SqlBrandKitStore",
    "StaleKitError",
    "ToneSummary",
    "VoiceRules",
    "VoiceStyle",
    "active_connection",
    "apply_voice",
    "approve",
    "create_brand_tables",
    "derive_kit",
    "offer_in_style",
    "read_brand_sources",
    "read_klaviyo_copy",
    "read_shopify_brand",
    "record_assets",
    "save_read",
    "summarise_tone",
    "voice_problems",
]
