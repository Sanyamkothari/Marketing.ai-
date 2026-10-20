# Stream: Formats & assets

Branch `v2/formats`. Owns `autonomy/assets/`, `autonomy/agents/creative.py`,
`autonomy/agents/critic.py`, `autonomy/agents/prompts/creative.md`, `autonomy/channels/render.py`
and `docs/v2/FORMATS.md`, with tests in `tests/v2/formats/`. The strategy (safe-first: approved
assets only, generated images later through the Critic and a person, video much later) is in
`docs/v2/FORMATS.md`.

## What was built

| Module | What it does |
|---|---|
| `autonomy/assets/library.py` | The `Asset` model (approved image, video or carousel card, with required alt text and approver) and the `AssetLibrary` protocol, in memory or as a JSON directory (one file per asset, atomic writes, loud errors). Lookups return approved assets only by default, in a fixed order. |
| `autonomy/assets/support.py` | The channel x format matrix (`SUPPORT`) with hard limits and guidance per cell, and `validate()`, which lists every problem with sending an asset as data (errors block, warnings inform). |
| `autonomy/agents/creative.py` | Still writes the text variants as before; now also adds image, carousel and video variants of the same words from approved assets, wherever the channel carries that format. No extra LLM call. Adds the `recommended_product` slot. |
| `autonomy/agents/critic.py` | `BrandRules` now carries the approved assets; a new rule `format_problems` rejects unsupported formats, missing or unapproved assets, missing alt text, wrong asset kind or count. The judge sees the format and alt text. |
| `autonomy/agents/prompts/creative.md` | Guidance for the `{{recommended_product}}` slot, social proof, comparison, and copy that reads well with or without an image. |
| `autonomy/channels/render.py` | `Rendered` carries the format and media; WhatsApp gets a template header (`HeaderMedia`); refuses a format the channel cannot carry; `to_message()` builds the `Message` with `asset_url`; fills `{{recommended_product}}` with a safe fallback. |

`FORMAT_SUPPORT` in `render.py` is derived from `autonomy.assets.support.formats_for()`, so the
renderer, the Creative, the Critic and the asset checks share one matrix.

## How other streams use it

```python
from autonomy.assets import (
    Asset, AssetKind, AssetStatus, AssetLibrary, InMemoryAssetLibrary, JsonDirAssetLibrary,
    candidates, carousel_set, formats_for, validate, validate_carousel, is_sendable,
)

AssetLibrary.put(asset: Asset) -> None
AssetLibrary.get(asset_id: str) -> Asset | None              # any status, for audit
AssetLibrary.find(*, kind=None, item_id=None, category=None, theme=None, group_id=None,
                  approved_only=True) -> list[Asset]         # sorted by asset_id
JsonDirAssetLibrary(root: Path)                               # one <asset_id>.json per asset
candidates(library, *, kind, item_id=None, category=None, theme=None) -> list[Asset]
    # approved, most specific first (item, category, theme); never another product's picture
carousel_set(library, group_id) -> list[Asset]                # cards in position order

formats_for(channel, *, carousel_template=False) -> tuple[Format, ...]
validate(asset, channel, fmt=None, *, carousel_template=False) -> list[Problem]
validate_carousel(cards, channel, *, carousel_template=False) -> list[Problem]
is_sendable(asset, channel, fmt=None, *, carousel_template=False) -> bool

from autonomy.agents.creative import Creative
Creative(llm, *, model_id=None).build_library(brief, *, brand_voice, n_per_cell=2, now, themes=None,
                            assets: Iterable[ApprovedAsset] = (),
                            formats: Mapping[Channel, Sequence[Format]] | None = None)
    -> list[ContentItem]                                      # `formats` can only narrow the matrix

from autonomy.agents.critic import BrandRules, check_rules, format_problems
BrandRules.from_brief(brief, *, brand_voice, allowed_domains=(), assets=()) -> BrandRules

from autonomy.channels.render import render, to_message, FORMAT_SUPPORT, asset_urls, join_asset_urls
render(item, state, profile, offer, now) -> Rendered          # .format, .asset_url, .asset_urls, .header_media
to_message(rendered, item, *, message_id, decision_id, customer_id, to, send_at) -> Message
```

Pass the same asset list to `Creative.build_library(assets=...)` and `BrandRules.from_brief(assets=...)`;
`autonomy.assets.Asset` fits the Creative's `ApprovedAsset` protocol directly. For the decide
stream: learn format as a bandit arm over the `Format` label (not the asset id), limited to
formats that have a sendable approved asset, with text always available.

## Contract additions requested

- `ContentItem.asset_ids: tuple[str, ...]` (or `asset_urls`), so a carousel's cards are stored
  as a list instead of space-joined in `asset_url`, and `Action.asset_id` can be matched exactly.
- `Message.format` (or a media-kind field), so channel adapters know whether `asset_url` is an
  image, a video or carousel cards.
- Possibly move or re-export `Asset` into `autonomy/contracts` if decide or api/v2 need it as a
  shared shape. No contract was changed.

## Dependencies requested

None. Only pydantic and the stdlib.

## Known gaps

- Not wired yet (other streams' files): the dispatcher builds `Message` with `item.asset_url`
  instead of `render.to_message`; the WhatsApp adapter always sends an image header (video needs
  `Rendered.header_media`); the SES adapter ignores `asset_url`, so email images are not sent;
  `autonomy/runtime/demo.py` calls `build_library` and `BrandRules.from_brief` without assets, so
  the live loop makes no media variants yet.
- `autonomy.assets` is not re-exported from `autonomy/agents/__init__.py` or
  `autonomy/channels/__init__.py` (not this stream's files); import from the modules directly.
- The WhatsApp carousel template flag is modelled in `support.py` but the Creative and renderer
  use only the unflagged cells.
- `render()` does not check carousel card bounds (2 to 10); the Critic does.
- Limits are hand-written from provider docs (WhatsApp 5 MB image, 16 MB video; Firebase 1 MB
  push image). Real file size and dimensions come from upload metadata, not the files.
- Pre-existing: the Creative allows `{{store_name}}` but render has no filler for it.
- Checking that copy does not contradict its image is out of scope.
- mypy --strict passed locally on Python 3.13 targets only; the Python 3.11 run is left to CI.
