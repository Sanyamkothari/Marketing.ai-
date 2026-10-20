"""Format-aware content: the Creative adds image, carousel and video variants from approved assets
where the channel can carry them, the Critic checks the media, and rendering carries it through."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from autonomy.agents.base import ScriptedLLM
from autonomy.agents.creative import ALLOWED_SLOTS, Creative
from autonomy.agents.critic import BrandRules, Critic, check_rules, format_problems
from autonomy.channels.render import (
    FALLBACK_RECOMMENDED_PRODUCT,
    FORMAT_SUPPORT,
    KNOWN_SLOTS,
    HeaderMedia,
    RenderError,
    asset_urls,
    render,
    to_message,
)
from autonomy.contracts import (
    Brief,
    Channel,
    ChannelStats,
    ContentItem,
    ContentStatus,
    CustomerState,
    Format,
    Theme,
)
from autonomy.fakes.samples import sample_brief

NOW = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
VOICE = "Warm, plain and brief."


@dataclass(frozen=True)
class FakeAsset:
    """Shaped like `autonomy.assets.library.Asset`, as far as the Creative's protocol goes."""

    asset_id: str
    kind: str
    url: str
    alt_text: str | None = "A pair of trail shoes on a rock"
    item_id: str | None = None
    category: str | None = None
    themes: tuple[str, ...] = ()
    status: str = "approved"
    group_id: str | None = None
    position: int = 0


ASSETS = (
    FakeAsset("img_any", "image", "https://cdn.example/any.jpg"),
    FakeAsset("img_reminder", "image", "https://cdn.example/rem.jpg", themes=("reminder",)),
    FakeAsset("img_savings", "image", "https://cdn.example/sav.jpg", themes=("savings",)),
    FakeAsset("img_noalt", "image", "https://cdn.example/noalt.jpg", alt_text=" "),
    FakeAsset("img_draft", "image", "https://cdn.example/draft.jpg", status="draft"),
    FakeAsset("vid_1", "video", "https://cdn.example/clip.mp4", alt_text="A runner on a trail"),
)


def _brief(*channels: Channel) -> Brief:
    brief = sample_brief(NOW)
    return brief.model_copy(update={"envelope": brief.envelope.model_copy(update={"channels": channels})})


def _script(_system: str, prompt: str) -> str:
    channel = re.search(r"^Channel: ([a-z_]+)", prompt, re.MULTILINE)
    assert channel
    variants = [
        {
            "subject": f"Note {k}" if channel.group(1) == "email" else None,
            "body": f"Hi {{{{first_name}}}}, take a look at {{{{recommended_product}}}} ({k}).",
        }
        for k in range(2)
    ]
    return json.dumps({"variants": variants})


def _library(
    *channels: Channel, assets: tuple[FakeAsset, ...] = ASSETS
) -> tuple[list[ContentItem], Creative]:
    creative = Creative(ScriptedLLM(_script))
    items = creative.build_library(
        _brief(*channels), brand_voice=VOICE, now=NOW, themes=[Theme.REMINDER], assets=assets
    )
    return items, creative


def _state() -> CustomerState:
    return CustomerState.model_validate(
        {
            "customer_id": "c1",
            "as_of": NOW,
            "top_categories": ("trail shoes",),
            "channels": {Channel.EMAIL: ChannelStats(address_known=True, consent=True)},
        }
    )


def _item(
    channel: Channel,
    fmt: Format,
    asset_url: str | None,
    *,
    body: str = "Hi {{first_name}}, new for you.",
    template: str | None = None,
) -> ContentItem:
    return ContentItem(
        content_id="ci_f",
        channel=channel,
        format=fmt,
        theme=Theme.REMINDER,
        subject="Hello" if channel is Channel.EMAIL else None,
        body=body + (" You can unsubscribe at any time." if channel is Channel.EMAIL else ""),
        asset_url=asset_url,
        template_name=template or ("t_wa" if channel is Channel.WHATSAPP else None),
        status=ContentStatus.APPROVED,
        created_at=NOW,
    )


# --- Creative --------------------------------------------------------------------------------


def test_format_variants_follow_the_channel_support_matrix() -> None:
    items, _ = _library(Channel.EMAIL, Channel.WHATSAPP, Channel.SMS)
    by_channel: dict[Channel, set[Format]] = {}
    for item in items:
        by_channel.setdefault(item.channel, set()).add(item.format)
    assert by_channel[Channel.SMS] == {Format.TEXT}
    # Email: no video. Only two loose images fit the reminder theme, enough for a carousel.
    assert by_channel[Channel.EMAIL] == {Format.TEXT, Format.TEXT_IMAGE, Format.CAROUSEL}
    assert by_channel[Channel.WHATSAPP] == {Format.TEXT, Format.TEXT_IMAGE, Format.VIDEO}
    for item in items:
        assert item.format in FORMAT_SUPPORT[item.channel]
        urls = asset_urls(item.asset_url)
        assert (item.format is Format.TEXT) == (not urls)
        assert "https://cdn.example/sav.jpg" not in urls  # tagged for another theme
        assert "https://cdn.example/noalt.jpg" not in urls  # no alt text
        assert "https://cdn.example/draft.jpg" not in urls  # not approved
        if item.format is Format.VIDEO:
            assert urls == ("https://cdn.example/clip.mp4",)
        if item.format is Format.CAROUSEL:
            assert set(urls) == {"https://cdn.example/rem.jpg", "https://cdn.example/any.jpg"}
    assert len({i.content_id for i in items}) == len(items)
    names = [i.template_name for i in items if i.channel is Channel.WHATSAPP]
    assert len(set(names)) == len(names) and all(n and re.fullmatch(r"[a-z0-9_]+", n) for n in names)


def test_text_variants_are_kept_unchanged_and_media_costs_no_llm_call() -> None:
    plain, _ = _library(Channel.EMAIL, Channel.WHATSAPP, assets=())
    llm = ScriptedLLM(_script)
    rich = Creative(llm).build_library(
        _brief(Channel.EMAIL, Channel.WHATSAPP),
        brand_voice=VOICE,
        now=NOW,
        themes=[Theme.REMINDER],
        assets=ASSETS,
    )
    assert [i for i in rich if i.format is Format.TEXT] == plain
    assert len(llm.calls) == 2 * len(sample_brief(NOW).envelope.offers)
    texts = {(i.channel, i.offer_id, i.body) for i in plain}
    assert all((i.channel, i.offer_id, i.body) in texts for i in rich)  # same words, new format
    # Theme-tagged images come first, and variants rotate through the fitting images.
    images = [i.asset_url for i in rich if i.format is Format.TEXT_IMAGE and i.channel is Channel.EMAIL]
    assert images[:2] == ["https://cdn.example/rem.jpg", "https://cdn.example/any.jpg"]
    assert rich == Creative(ScriptedLLM(_script)).build_library(
        _brief(Channel.EMAIL, Channel.WHATSAPP),
        brand_voice=VOICE,
        now=NOW,
        themes=[Theme.REMINDER],
        assets=ASSETS,
    )


def test_assets_without_alt_text_or_approval_give_no_media_variants() -> None:
    bad = (ASSETS[3], ASSETS[4])
    items, creative = _library(Channel.EMAIL, assets=bad)
    assert {i.format for i in items} == {Format.TEXT}
    assert any("no text_image variant" in d for d in creative.dropped)


def test_carousel_uses_the_brands_own_card_set_in_order() -> None:
    cards = tuple(
        FakeAsset(f"card_{p}", "carousel_card", f"https://cdn.example/c{p}.jpg", group_id="set_a", position=p)
        for p in (2, 0, 1)
    )
    items, _ = _library(Channel.EMAIL, assets=(*ASSETS, *cards))
    carousels = [i for i in items if i.format is Format.CAROUSEL]
    assert carousels
    assert all(
        asset_urls(i.asset_url) == tuple(f"https://cdn.example/c{p}.jpg" for p in range(3)) for i in carousels
    )


def test_formats_override_only_narrows_the_support_matrix() -> None:
    creative = Creative(ScriptedLLM(_script))
    items = creative.build_library(
        _brief(Channel.EMAIL, Channel.WHATSAPP),
        brand_voice=VOICE,
        now=NOW,
        themes=[Theme.REMINDER],
        assets=ASSETS,
        # Email narrowed to images; WhatsApp asks for a carousel it cannot carry by default.
        formats={Channel.EMAIL: (Format.TEXT, Format.TEXT_IMAGE), Channel.WHATSAPP: (Format.CAROUSEL,)},
    )
    by_channel: dict[Channel, set[Format]] = {}
    for item in items:
        by_channel.setdefault(item.channel, set()).add(item.format)
    assert by_channel == {Channel.EMAIL: {Format.TEXT, Format.TEXT_IMAGE}, Channel.WHATSAPP: {Format.TEXT}}


def test_two_records_of_one_image_never_make_a_carousel_repeat_it() -> None:
    twin = FakeAsset("img_any_copy", "image", "https://cdn.example/any.jpg")
    cards = (
        FakeAsset("card_a", "carousel_card", "https://cdn.example/same.jpg", group_id="g", position=0),
        FakeAsset("card_b", "carousel_card", "https://cdn.example/same.jpg", group_id="g", position=1),
    )
    items, creative = _library(Channel.EMAIL, assets=(ASSETS[0], twin, *cards))
    # One distinct loose image and a set that repeats its picture: no carousel can be built.
    assert Format.CAROUSEL not in {i.format for i in items}
    assert any("no carousel variant" in d for d in creative.dropped)
    rules = BrandRules.from_brief(_brief(Channel.EMAIL), brand_voice=VOICE, assets=(ASSETS[0], twin, *cards))
    assert all(check_rules(i, rules) == [] for i in items)


def test_prompt_guides_comparison_and_social_proof_and_lists_the_new_slot() -> None:
    llm = ScriptedLLM(_script)
    Creative(llm).build_library(
        _brief(Channel.EMAIL), brand_voice=VOICE, now=NOW, themes=[Theme.SOCIAL_PROOF, Theme.QUALITY]
    )
    system = llm.calls[0][0]
    assert "{{recommended_product}}" in system and "Social proof" in system and "Comparison" in system
    assert "$" not in system  # every placeholder in the prompt file was filled
    prompts = [p for _s, p in llm.calls]
    assert any("Angle: Social proof" in p for p in prompts)
    assert any("Angle: A comparison angle" in p for p in prompts)


# --- Critic ----------------------------------------------------------------------------------


def test_critic_approves_the_creatives_media_items() -> None:
    items, _ = _library(Channel.EMAIL, Channel.WHATSAPP, Channel.SMS)
    rules = BrandRules.from_brief(_brief(Channel.EMAIL), brand_voice=VOICE, assets=ASSETS)
    assert {a.asset_id for a in rules.approved_assets} == {a.asset_id for a in ASSETS} - {"img_draft"}
    for item in items:
        assert check_rules(item, rules) == [], item


def test_critic_rejects_media_items_without_approved_assets() -> None:
    rules = BrandRules(
        approved_assets=BrandRules.from_brief(
            sample_brief(NOW), brand_voice="", assets=ASSETS
        ).approved_assets
    )
    cases = {
        "needs an approved asset": _item(Channel.EMAIL, Format.TEXT_IMAGE, None),
        "not an approved asset": _item(Channel.EMAIL, Format.TEXT_IMAGE, "https://elsewhere.example/x.jpg"),
        "has no alt text": _item(Channel.EMAIL, Format.TEXT_IMAGE, "https://cdn.example/noalt.jpg"),
        "needs video assets": _item(Channel.WHATSAPP, Format.VIDEO, "https://cdn.example/any.jpg"),
        "cannot carry the carousel": _item(
            Channel.WHATSAPP, Format.CAROUSEL, "https://cdn.example/any.jpg https://cdn.example/rem.jpg"
        ),
        "sms cannot carry": _item(Channel.SMS, Format.TEXT_IMAGE, "https://cdn.example/any.jpg"),
        "a carousel needs": _item(Channel.EMAIL, Format.CAROUSEL, "https://cdn.example/any.jpg"),
        "a text item carries an asset": _item(Channel.EMAIL, Format.TEXT, "https://cdn.example/any.jpg"),
        "draft": _item(Channel.EMAIL, Format.TEXT_IMAGE, "https://cdn.example/draft.jpg"),
    }
    for expected, item in cases.items():
        problems = format_problems(item, rules)
        assert problems, expected
        if expected != "draft":
            assert any(expected in p for p in problems), (expected, problems)
    judge = ScriptedLLM(lambda _s, _p: "never called")
    reviewed = Critic(judge).review(list(cases.values()), rules)
    assert all(r.status is ContentStatus.REJECTED for r in reviewed)
    assert not judge.calls


def test_judge_sees_the_assets_alt_text() -> None:
    scores = {"scores": [{"criterion": c, "score": 5, "reason": "ok"} for c in ("brand_voice", "honesty")]}
    judge = ScriptedLLM(lambda _s, _p: json.dumps(scores))
    rules = BrandRules.from_brief(sample_brief(NOW), brand_voice=VOICE, assets=ASSETS)
    [out] = Critic(judge).review(
        [_item(Channel.EMAIL, Format.TEXT_IMAGE, "https://cdn.example/any.jpg")], rules
    )
    assert out.status is ContentStatus.APPROVED
    assert "format: text_image, shown with: A pair of trail shoes on a rock" in judge.calls[0][1]


# --- Render ----------------------------------------------------------------------------------


def test_render_carries_media_and_builds_the_whatsapp_header() -> None:
    image = render(
        _item(Channel.WHATSAPP, Format.TEXT_IMAGE, "https://cdn.example/any.jpg"), _state(), {}, None, NOW
    )
    assert image.asset_url == "https://cdn.example/any.jpg" and image.format is Format.TEXT_IMAGE
    assert image.header_media == HeaderMedia(kind="image", link="https://cdn.example/any.jpg")
    assert image.header_media.component() == {
        "type": "header",
        "parameters": [{"type": "image", "image": {"link": "https://cdn.example/any.jpg"}}],
    }
    video = render(
        _item(Channel.WHATSAPP, Format.VIDEO, "https://cdn.example/clip.mp4"), _state(), {}, None, NOW
    )
    assert video.header_media is not None and video.header_media.component()["parameters"] == [
        {"type": "video", "video": {"link": "https://cdn.example/clip.mp4"}}
    ]
    item = _item(Channel.EMAIL, Format.CAROUSEL, "https://cdn.example/a.jpg https://cdn.example/b.jpg")
    stacked = render(item, _state(), {}, None, NOW)
    assert stacked.asset_urls == ("https://cdn.example/a.jpg", "https://cdn.example/b.jpg")
    assert stacked.header_media is None
    message = to_message(
        stacked, item, message_id="m1", decision_id="d1", customer_id="c1", to="a@example.test", send_at=NOW
    )
    assert message.asset_url == item.asset_url and message.body == stacked.body
    # Text stays as it was: no media, no header.
    plain = render(_item(Channel.WHATSAPP, Format.TEXT, None), _state(), {}, None, NOW)
    assert plain.asset_url is None and plain.asset_urls == () and plain.header_media is None


def test_render_refuses_media_the_channel_cannot_carry() -> None:
    two = "https://cdn.example/a.jpg https://cdn.example/b.jpg"
    for item, why in (
        (_item(Channel.WHATSAPP, Format.CAROUSEL, two), "cannot carry"),
        (_item(Channel.SMS, Format.TEXT_IMAGE, "https://cdn.example/a.jpg"), "cannot carry"),
        (_item(Channel.EMAIL, Format.TEXT_IMAGE, None), "no asset"),
        (_item(Channel.EMAIL, Format.TEXT_IMAGE, two), "2 assets"),
    ):
        with pytest.raises(RenderError, match=why):
            render(item, _state(), {}, None, NOW)


def test_recommended_product_slot_fills_and_falls_back() -> None:
    assert "recommended_product" in ALLOWED_SLOTS and "recommended_product" in KNOWN_SLOTS
    item = _item(Channel.WHATSAPP, Format.TEXT, None, body="Hi {{first_name}}, try {{recommended_product}}.")
    assert check_rules(item, BrandRules()) == []
    filled = render(item, _state(), {"recommended_product": "Trail\nRunner 2"}, None, NOW)
    assert filled.body == "Hi there, try Trail Runner 2."
    assert filled.template_params == ("there", "Trail Runner 2")
    for profile in ({}, {"recommended_product": " \t "}):
        assert (
            render(item, _state(), profile, None, NOW).body
            == f"Hi there, try {FALLBACK_RECOMMENDED_PRODUCT}."
        )


def test_the_asset_librarys_model_fits_the_creatives_protocol() -> None:
    library = pytest.importorskip("autonomy.assets.library")
    asset = library.Asset(
        asset_id="img_real",
        kind=library.AssetKind.IMAGE,
        url="https://cdn.example/real.jpg",
        alt_text="Shoes",
        themes=(Theme.REMINDER,),
        status=library.AssetStatus.APPROVED,
        approved_by="founder",
        created_at=NOW,
    )
    items, _ = _library(Channel.EMAIL, assets=(asset,))
    assert {i.asset_url for i in items if i.format is Format.TEXT_IMAGE} == {"https://cdn.example/real.jpg"}
    rules = BrandRules.from_brief(sample_brief(NOW), brand_voice=VOICE, assets=[asset])
    assert all(check_rules(i, rules) == [] for i in items)
