"""An approved generated banner states one offer for one product: nothing downstream may use it elsewhere."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

from autonomy.agents.base import ScriptedLLM
from autonomy.agents.creative import Creative, assets_for, carousel_sets
from autonomy.agents.critic import BrandRules, check_rules
from autonomy.assets.generate import OfferSpec
from autonomy.assets.library import Asset, AssetKind, AssetStatus, InMemoryAssetLibrary
from autonomy.contracts import Format, Theme
from autonomy.fakes.samples import sample_brief
from tests.v3.images.support import OFFER, desk, make_image

NOW = datetime(2026, 10, 1, tzinfo=UTC)
PLAIN_URL = "https://cdn.example.com/plain.png"


def _script(system: str, prompt: str) -> str:
    channel = re.search(r"^Channel: ([a-z_]+)", prompt, re.MULTILINE)
    is_email = channel is not None and channel.group(1) == "email"
    body = "Hi {{first_name}}, a note for you." + (" {{offer_label}}." if "{{offer_label}}" in prompt else "")
    return json.dumps({"variants": [{"subject": "A note" if is_email else None, "body": body}]})


def library_with_a_banner() -> tuple[InMemoryAssetLibrary, Asset]:
    library = InMemoryAssetLibrary()
    d = desk(library=library)
    d.submit(make_image())  # '20% off, now ₹1,039.60', made for one customer type
    (banner,) = d.approve_batch("imgb_0001", "ana")
    return library, banner


def test_the_creative_never_picks_a_generated_20_percent_banner() -> None:
    library, banner = library_with_a_banner()
    assert banner.is_approved and banner.themes == ()  # suits any theme as far as themes go
    for fmt in (Format.TEXT_IMAGE, Format.CAROUSEL, Format.VIDEO):
        for theme in Theme:
            assert assets_for(library.find(), fmt, theme) == []
            assert carousel_sets(library.find(), theme) == []
    plain = Asset(
        asset_id="plain",
        kind=AssetKind.IMAGE,
        url=PLAIN_URL,
        alt_text="A pair of running shoes",
        status=AssetStatus.APPROVED,
        approved_by="ana",
        created_at=NOW,
    )
    library.put(plain)
    brief = sample_brief(NOW)
    assert any(o.offer_id == "none" for o in brief.envelope.offers)
    items = Creative(ScriptedLLM(_script)).build_library(
        brief, brand_voice="Plain.", n_per_cell=1, now=NOW, assets=library.find()
    )
    with_media = [i for i in items if i.asset_url]
    assert with_media, "the ordinary image is still used"
    assert all(banner.url not in (i.asset_url or "") for i in items)
    assert {i.asset_url for i in with_media} == {PLAIN_URL}
    assert any(i.offer_id == "none" for i in with_media)


def test_the_critic_refuses_an_item_that_carries_a_banner() -> None:
    library, banner = library_with_a_banner()
    brief = sample_brief(NOW)
    text_item, *_ = Creative(ScriptedLLM(_script)).build_library(
        brief, brand_voice="Plain.", n_per_cell=1, now=NOW
    )
    item = text_item.model_copy(update={"format": Format.TEXT_IMAGE, "asset_url": banner.url})
    rules = BrandRules.from_brief(brief, brand_voice="Plain.", assets=library.find())
    problems = check_rules(item, rules)
    assert any("offer_banner" in p for p in problems), problems


def test_approved_for_matches_product_customer_type_and_offer_exactly() -> None:
    library = InMemoryAssetLibrary()
    d = desk(library=library)
    d.submit(make_image())
    d.submit(make_image(offer=OfferSpec(currency="INR", discount_pct=10)))
    d.approve_batch("imgb_0001", "ana")
    (match,) = d.approved_for("shoe-0", "weekend-runners", OFFER)
    assert match.item_id == "shoe-0" and "20% off" in match.alt_text
    assert d.approved_for("shoe-0", "weekend-runners", OfferSpec(currency="INR")) == []
    assert d.approved_for("shoe-0", "bargain-hunters", OFFER) == []
    assert d.approved_for("shoe-1", "weekend-runners", OFFER) == []
