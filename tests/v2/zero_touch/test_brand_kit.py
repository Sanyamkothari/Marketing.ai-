"""Zero-touch item 3: the brand kit is read from the shop's own Klaviyo and Shopify, never invented.

The read goes through the real connector engine and HTTP clients against `FakeKlaviyo` / `FakeShopify`
(extended here with campaigns, brand settings and product images); every rule must trace to counted
copy; the Creative writes in the voice with no model configured; the Critic enforces the kit's hard
rules; and onboarding shows 3 samples for the top goal that one approval accepts and records.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from autonomy.agents.base import ScriptedLLM
from autonomy.agents.creative import EMAIL_SUBJECT_MAX, TEMPLATE_COPY, TEMPLATE_OFFER_LINE, Creative
from autonomy.agents.critic import BrandRules, check_rules
from autonomy.agents.strategist import BusinessFacts, template_brief
from autonomy.assets.library import AssetStatus, InMemoryAssetLibrary
from autonomy.brand import (
    BrandKit,
    BrandSources,
    CopySample,
    InMemoryBrandKitStore,
    SqlAssetLibrary,
    SqlBrandKitStore,
    StaleKitError,
    VoiceRules,
    approve,
    derive_kit,
    read_brand_sources,
    record_assets,
    save_read,
    summarise_tone,
    voice_problems,
)
from autonomy.brand.derive import SUBJECT_FLOOR
from autonomy.brand.samples import sample_messages
from autonomy.brand.text import AVOID_CANDIDATES, html_to_text
from autonomy.connectors.base import ConnectionStatus
from autonomy.contracts import (
    NO_OFFER,
    Channel,
    ContentItem,
    ContentStatus,
    Format,
    Goal,
    GoalKind,
    OfferKind,
    OfferOption,
    Theme,
    Tone,
)
from tests.v2.zero_touch.brand_support import GREEN, LOGO, BrandKlaviyo, BrandShopify, email_html
from tests.v2.zero_touch.wiring_support import BUSINESS, T0, Shop, make_shop


def seeded_shop(*, campaigns: int = 10, discounts: bool = True, connect_shopify: bool = True) -> Shop:
    klaviyo = BrandKlaviyo()
    shopify = BrandShopify(clock_now=T0)
    for i in range(4):
        klaviyo.add_template(f"TPL_SHOP_{i}", f"Acme newsletter {i}", email_html(i))
    for i in range(campaigns):
        offer = "Take 15% off with code TRAIL15." if discounts and i % 3 == 0 else ""
        klaviyo.add_campaign(
            f"C{i}", subject=f"Trail ready: new gear for the weekend {i}", html=email_html(i, discount=offer)
        )
    klaviyo.add_campaign(
        "DRAFT", subject="An unsent draft about a crazy sale " + "x" * 40, html=email_html(99), status="Draft"
    )
    for i in range(6):
        klaviyo.add_campaign(
            f"S{i}", channel="sms", body=f"Acme: new trail gear is in. Reply STOP to opt out. {i}"
        )
    for pid, title, ptype in (
        (11, "Ridge Rain Jacket", "Jackets"),
        (12, "Summit Pack 30L", "Packs"),
        (13, "Trail Jacket", "Jackets"),
    ):
        shopify.add_image_product(pid, title, ptype)
    shop = make_shop(klaviyo=klaviyo, shopify=shopify)
    shop.connect_klaviyo()
    if connect_shopify:
        shop.connect_shopify_oauth()
    return shop


def read_kit(shop: Shop, **kw: Any) -> BrandKit:
    return derive_kit(read_brand_sources(shop.engine, BUSINESS, shop.clock.now()), **kw)


def finding(kit: BrandKit, key: str) -> Any:
    return next(f for f in kit.findings if f.key == key)


def email(body: str, subject: str = "Trail news", *, offer_id: str = NO_OFFER.offer_id) -> ContentItem:
    return ContentItem(
        content_id="ci_1",
        channel=Channel.EMAIL,
        format=Format.TEXT,
        theme=Theme.REMINDER,
        tone=Tone.FRIENDLY,
        offer_id=offer_id,
        subject=subject,
        body=body + "\n\nYou can unsubscribe at any time.",
        status=ContentStatus.DRAFT,
        created_at=T0,
    )


def goal(kind: GoalKind = GoalKind.WIN_BACK) -> Goal:
    return Goal(
        goal_id="opp_win_back",
        business_id=BUSINESS,
        text="Win back lapsed customers",
        kind=kind,
        budget=500,
        starts_at=T0,
        created_at=T0,
    )


# --- reading, and every rule traced to the shop's own copy ------------------------------------------


def test_the_voice_is_read_from_their_klaviyo_templates_and_sent_campaigns() -> None:
    shop = seeded_shop()
    kit = read_kit(shop)

    # 4 templates (Autonomy's own send template left out), 10 sent email campaigns (the draft left out), 6 SMS.
    assert kit.messages_read == 20
    assert any(t["attributes"]["name"].startswith("Autonomy ·") for t in shop.klaviyo.templates.values())
    assert kit.sources[0] == "Klaviyo: 4 templates, 10 email campaigns, 6 text campaigns"
    assert kit.style.greeting == "Hey {{first_name}},"
    assert (kit.style.sign_off, kit.style.signer) == ("Cheers,", "The Acme team")
    assert finding(kit, "greeting").evidence.seen == 14 and finding(kit, "greeting").evidence.of == 14

    # Hard rules, each counted from their copy.
    assert kit.rules.subject_max == 40  # 9 in 10 subjects are 39 characters, rounded up to 5
    assert kit.rules.max_emoji == 0 and kit.rules.max_exclamations == 0
    assert kit.rules.discount_kinds == (OfferKind.PERCENT_OFF,)
    assert kit.style.usual_discount == 15 and kit.style.discount_phrase == "{n}% off"
    assert (
        "crazy" in kit.rules.banned_words and "sale" in kit.rules.banned_words
    )  # only the unsent draft says them
    assert all(w in AVOID_CANDIDATES for w in kit.rules.banned_words)
    assert kit.rules.sms_body_max == 120  # their texts are ~55 characters; our floor is 120
    for f in kit.findings:
        assert f.evidence.of >= f.evidence.seen and f.evidence.source
    # The words they use come from their copy; their name and sign-off are not counted as wording.
    assert {"trail", "gear"} <= set(kit.style.words_they_use)
    assert not {"acme", "cheers", "team"} & set(kit.style.words_they_use)


def test_the_same_copy_always_gives_the_same_kit() -> None:
    shop = seeded_shop()
    assert read_kit(shop).fingerprint() == read_kit(shop).fingerprint()


def test_shopify_gives_the_shop_its_logo_colours_and_product_photos_with_their_records() -> None:
    shop = seeded_shop()
    kit = read_kit(shop)
    assert kit.shop_name == "Acme Outdoors" and kit.shop_domain == "acme.example" and kit.currency == "USD"
    assert kit.logo is not None and kit.logo.url == "https://cdn.shopify.com/brand/logo.png"
    assert [c.hex for c in kit.colours] == ["#1f6f43", "#f2b705"]
    assert kit.product_types == ("Jackets", "Packs")
    assert len(kit.assets) == 3
    jacket = kit.assets[0]
    assert jacket.asset_id == "shopify-acme-goods-11-1100"
    assert jacket.alt_text == "Ridge Rain Jacket"  # no alt text in Shopify: the product title describes it
    assert jacket.source.platform == "shopify" and jacket.source.record_id == "gid://shopify/Product/11"
    assert jacket.source.media_id == "gid://shopify/MediaImage/1100" and jacket.item_id == "11"


def test_without_shopify_brand_settings_the_logo_and_colours_come_from_their_email_templates() -> None:
    shop = seeded_shop()
    shop.shopify.brand_denied = True
    kit = read_kit(shop)
    assert kit.logo is not None and kit.logo.url == LOGO and "Klaviyo" in kit.logo.source
    assert kit.colours[0].hex == GREEN
    assert any("brand settings" in n for n in kit.notes)


def test_a_klaviyo_key_without_campaign_access_still_reads_the_templates_and_never_breaks_the_connection() -> (
    None
):
    shop = seeded_shop()
    shop.klaviyo.campaigns_denied = True
    kit = read_kit(shop)
    assert kit.messages_read == 4  # the templates only
    assert any("campaigns:read" in n for n in kit.notes)
    assert all(
        c.status is ConnectionStatus.ACTIVE for c in shop.engine.store.connections(business_id=BUSINESS)
    )
    # Four messages are too few for "never uses" rules: they are left out, not guessed.
    assert kit.rules.banned_words == () and kit.rules.max_emoji is None and kit.rules.discount_kinds is None
    assert kit.rules.subject_max is None
    assert any("left out" in n for n in kit.notes)


def test_a_shop_whose_copy_never_states_a_discount_gets_no_discount_offers() -> None:
    shop = seeded_shop(discounts=False)
    kit = read_kit(shop)
    assert kit.rules.discount_kinds == ()
    percent = OfferOption(offer_id="pct10", label="10% off", kind=OfferKind.PERCENT_OFF, amount=10)
    rules = BrandRules(offers=(NO_OFFER, percent), voice=kit.rules)
    problems = check_rules(email("Hey {{first_name}}, your offer: {{offer_label}}.", offer_id="pct10"), rules)
    assert any("never states a discount" in p for p in problems)
    facts = BusinessFacts(business_id=BUSINESS, channels=(Channel.EMAIL,), offers=(percent,))
    brief = template_brief(goal(), facts, T0)
    items = Creative(_down_llm()).build_library(brief, brand_voice="", n_per_cell=1, now=T0, kit=kit)
    assert items and all(i.offer_id == NO_OFFER.offer_id for i in items)


# --- the Critic checks every message against the kit's hard rules --------------------------------------


def test_the_critic_rejects_what_breaks_the_shops_own_habits_and_passes_what_fits() -> None:
    voice = VoiceRules(
        banned_words=("hurry", "act now"),
        subject_max=40,
        max_emoji=0,
        max_exclamations=0,
        discount_kinds=(OfferKind.PERCENT_OFF,),
    )
    money = OfferOption(offer_id="usd10", label="$10 off", kind=OfferKind.AMOUNT_OFF, amount=10)
    rules = BrandRules(offers=(NO_OFFER, money), voice=voice)
    assert check_rules(email("Hey {{first_name}}, the trail is calling."), rules) == []
    bad = email(
        "Hurry {{first_name}}, act now! The trail is calling \U0001f332",
        subject="A very long subject line about the new trail gear",
    )
    problems = check_rules(bad, rules)
    assert any("never says 'hurry'" in p for p in problems)
    assert any("never says 'act now'" in p for p in problems)
    assert any("own subjects stay within 40" in p for p in problems)
    assert any("emoji" in p for p in problems) and any("exclamation" in p for p in problems)
    assert all(p.startswith("brand kit: ") for p in problems)
    money_item = email("Hey {{first_name}}, your offer: {{offer_label}}.", offer_id="usd10")
    assert any("only states a percentage off" in p for p in check_rules(money_item, rules))
    # Without a kit the Critic is exactly as before.
    assert voice_problems(bad, None, None) == []


# --- the Creative writes in the voice, with no model configured -------------------------------------


class _Down:
    def complete(self, prompt: str, **_: Any) -> Any:
        from engine.llm import LLMError

        raise LLMError("LLM_UNAVAILABLE", "down")


def _down_llm() -> Any:
    return _Down()


def test_with_no_model_the_template_copy_is_put_into_the_shops_voice_and_passes_the_critic() -> None:
    kit = read_kit(seeded_shop())
    percent = OfferOption(offer_id="pct15", label="15% off", kind=OfferKind.PERCENT_OFF, amount=15)
    facts = BusinessFacts(business_id=BUSINESS, channels=(Channel.EMAIL, Channel.SMS), offers=(percent,))
    brief = template_brief(goal(), facts, T0)
    creative = Creative(_down_llm())
    items = creative.build_library(brief, brand_voice="", n_per_cell=2, now=T0, kit=kit)
    assert creative.templated and items
    rules = BrandRules.from_brief(brief, brand_voice="", kit=kit)
    for item in items:
        assert check_rules(item, rules) == [], (item.subject, item.body)
        if item.channel is Channel.EMAIL:
            assert item.body.startswith("Hey {{first_name}}, ")
            assert "Cheers,\nThe Acme team" in item.body
            assert len(item.subject or "") <= 40
    assert "Hey {{first_name}}" in rules.brand_voice  # the judge sees the voice too


def test_every_theme_has_a_template_subject_within_our_floor_and_no_template_uses_a_hype_word() -> None:
    for theme, rows in TEMPLATE_COPY.items():
        assert min(len(s) for s, _ in rows) <= SUBJECT_FLOOR, theme
        for subject, body in rows:
            text = f"{subject} {body}{TEMPLATE_OFFER_LINE}".lower()
            assert not [w for w in AVOID_CANDIDATES if f" {w} " in f" {text} "], theme
    assert SUBJECT_FLOOR <= EMAIL_SUBJECT_MAX


def test_with_a_model_the_creative_is_told_the_kit_rules() -> None:
    kit = read_kit(seeded_shop())
    answer = json.dumps(
        {"variants": [{"subject": "Trail ready", "body": "Hey {{first_name}}, the trail is calling."}]}
    )
    llm = ScriptedLLM(lambda system, prompt: answer)
    brief = template_brief(goal(), BusinessFacts(business_id=BUSINESS, channels=(Channel.EMAIL,)), T0)
    Creative(llm).build_library(brief, brand_voice="", n_per_cell=1, now=T0, kit=kit)
    prompt = llm.calls[0][1]
    assert "Never use these words (the shop never does): cheap" in prompt
    assert "subject of at most 40 characters" in prompt and "No emoji." in prompt
    assert "Open with 'Hey {{first_name}},'" in prompt


# --- an LLM may refine the tone only by picking from fixed lists, near what was measured -------------


def _sample_copy() -> list[CopySample]:
    return [
        CopySample(
            source="klaviyo_campaign",
            source_id=f"c{i}",
            subject="Trail ready",
            text=html_to_text(email_html(i)),
        )
        for i in range(10)
    ]


def test_the_models_tone_picks_count_only_within_one_step_of_what_was_measured() -> None:
    copy = _sample_copy()
    measured = derive_kit(BrandSources(business_id=BUSINESS, read_at=T0, copy_samples=tuple(copy))).style
    assert (measured.formality, measured.energy) == ("casual", "calm")

    near = json.dumps({"formality": "neutral", "energy": "upbeat", "warmth": "warm", "humour": True})
    style, note = summarise_tone(ScriptedLLM(lambda s, p: near), copy, measured)
    assert (style.formality, style.energy, style.warmth, style.humour) == ("neutral", "upbeat", "warm", True)
    assert style.tone_source == "measured_and_model" and note is None

    far = json.dumps({"formality": "formal", "energy": "excited", "warmth": "neutral", "humour": False})
    style, note = summarise_tone(ScriptedLLM(lambda s, p: far), copy, measured)
    assert (style.formality, style.energy) == ("casual", "calm")
    assert note is not None and "too far" in note

    style, note = summarise_tone(
        ScriptedLLM(lambda s, p: "a lovely rustic voice, use the word 'epic'"), copy, measured
    )
    assert style == measured and note is not None
    style, note = summarise_tone(_down_llm(), copy, measured)
    assert style == measured and note is not None and "could not be reached" in note
    # Free text never becomes a rule: the kit's rules do not depend on the model at all.
    with_model = derive_kit(
        BrandSources(business_id=BUSINESS, read_at=T0, copy_samples=tuple(copy)),
        llm=ScriptedLLM(lambda s, p: near),
    )
    without = derive_kit(BrandSources(business_id=BUSINESS, read_at=T0, copy_samples=tuple(copy)))
    assert with_model.rules == without.rules


# --- onboarding: 3 samples for the top goal, one approval ----------------------------------------------


def test_three_samples_for_the_top_goal_pass_the_rules_with_no_model() -> None:
    kit = read_kit(seeded_shop())
    samples = sample_messages(kit, goal(), now=T0)
    assert samples.written_by == "template" and len(samples.samples) == 3
    assert samples.fingerprint == kit.fingerprint()
    assert [s.offer_label for s in samples.samples] == [None, None, "15% off"]  # their own usual offer
    for s in samples.samples:
        assert s.preview_body.startswith("Hey [first name], ") and "{{" not in s.preview_body
        assert s.image_url in {a.url for a in kit.assets} and s.format is Format.TEXT_IMAGE
    assert "jackets" in samples.samples[0].preview_body  # their biggest product type


def test_one_approval_records_the_voice_and_approves_the_product_photos() -> None:
    shop = seeded_shop()
    store = SqlBrandKitStore(shop.db)
    library = SqlAssetLibrary(shop.db, BUSINESS)
    kit = save_read(store, read_kit(shop))
    record_assets(library, kit)
    assert library.find() == [] and len(library.find(approved_only=False)) == 3  # drafts until approved
    with pytest.raises(StaleKitError):
        approve(store, BUSINESS, approved_by="Dana", now=T0, fingerprint="not-the-one")
    kit = approve(
        store, BUSINESS, approved_by="Dana", now=T0, fingerprint=kit.fingerprint(), sample_ids=("a", "b", "c")
    )
    assert kit.approved and store.get(BUSINESS) == kit
    record_assets(library, kit)
    approved = library.find()
    assert len(approved) == 3 and all(a.status is AssetStatus.APPROVED for a in approved)
    assert approved[0].approved_by == "Dana (brand kit, 2026-10-05)"
    assert [a.sample_ids for a in store.approvals(BUSINESS)] == [("a", "b", "c")]
    # A fresh read of the same tools keeps the approval; a change in their copy asks again.
    assert save_read(store, read_kit(shop)).approved
    shop.klaviyo.add_template("TPL_NEW", "Loud promo", email_html(7, bang="!", emoji="\U0001f525"))
    changed = save_read(store, read_kit(shop))
    assert not changed.approved and changed.rules.max_emoji == 1
    assert len(store.approvals(BUSINESS)) == 1  # the earlier approval stays on record


def test_record_assets_keeps_a_photo_a_person_retired() -> None:
    kit = read_kit(seeded_shop())
    library = InMemoryAssetLibrary()
    record_assets(library, kit)
    retired = library.get(kit.assets[0].asset_id)
    assert retired is not None
    library.put(retired.model_copy(update={"status": AssetStatus.RETIRED}))
    store = InMemoryBrandKitStore()
    save_read(store, kit)
    record_assets(
        library, approve(store, BUSINESS, approved_by="Dana", now=T0, fingerprint=kit.fingerprint())
    )
    again = library.get(kit.assets[0].asset_id)
    assert again is not None and again.status is AssetStatus.RETIRED


def _client(shop: Shop) -> Any:
    from fastapi.testclient import TestClient

    from api.v2.app import create_app
    from api.v2.connectors import ConnectorServices
    from api.v2.deps import build_services_from_runtime
    from api.v2.onboarding_brand import BrandServices

    services = build_services_from_runtime(shop.runtime, clock=shop.clock.now, business_id=BUSINESS)
    assert shop.runtime.queue is not None
    connectors = ConnectorServices(
        engine=shop.engine, queue=shop.runtime.queue, business_id=BUSINESS, clock=shop.clock.now
    )
    brand = BrandServices(
        store=SqlBrandKitStore(shop.db), clock=shop.clock.now, assets=SqlAssetLibrary(shop.db, BUSINESS)
    )
    app = create_app(services, connectors=connectors, local_connectors=False, brand=brand)
    return TestClient(app), brand


def test_onboarding_reads_shows_three_samples_and_one_click_accepts_the_voice() -> None:
    shop = seeded_shop()
    client, brand = _client(shop)
    with client:
        page = client.get("/v2/onboarding/brand").json()
        assert page["status"] == "not_read" and page["samples"] == []

        page = client.post("/v2/onboarding/brand/read").json()
        assert page["status"] == "waiting" and page["shop_name"] == "Acme Outdoors"
        assert len(page["samples"]) == 3 and page["written_by"] == "template"
        assert page["words"] == "Approve these 3 messages and Autonomy writes like this from now on."
        assert page["goal"]["kind"] in {k.value for k in GoalKind}
        hard = {f["key"] for f in page["found"] if f["hard"]}
        assert {"subject_max", "max_emoji", "banned_words", "discount_kinds"} <= hard
        assert page["assets_count"] == 3 and page["assets"][0]["source"].startswith("Shopify product gid://")

        stale = client.post("/v2/onboarding/brand/approve", json={"fingerprint": "old"})
        assert stale.status_code == 409

        done = client.post(
            "/v2/onboarding/brand/approve", json={"fingerprint": page["fingerprint"], "approved_by": "Dana"}
        ).json()
        assert done["status"] == "approved" and done["approved_by"] == "Dana"
        assert done["words"].startswith("Approved by Dana")
        recorded = brand.store.approvals(BUSINESS)
        assert [r.sample_ids for r in recorded] == [tuple(s["content_id"] for s in page["samples"])]
        assert brand.assets is not None and len(brand.assets.find()) == 3
        assert client.get("/v2/onboarding/brand").json()["status"] == "approved"


def test_onboarding_read_without_a_connected_tool_says_what_to_do() -> None:
    shop = make_shop(klaviyo=BrandKlaviyo(), shopify=BrandShopify(clock_now=T0))
    client, _ = _client(shop)
    with client:
        answer = client.post("/v2/onboarding/brand/read")
        assert answer.status_code == 409 and "Connect Klaviyo or Shopify" in answer.json()["detail"]


def test_the_demo_app_serves_the_brand_page_with_no_setup() -> None:
    from fastapi.testclient import TestClient

    from api.v2.app import create_app
    from api.v2.deps import build_demo_services

    app = create_app(build_demo_services(T0, n_customers=10, days=1))
    with TestClient(app) as client:
        page = client.get("/v2/onboarding/brand")
        assert page.status_code == 200 and page.json()["status"] == "not_read"


def test_the_production_app_keeps_the_brand_kit_in_the_deployment_database(tmp_path: Any) -> None:
    pytest.importorskip("cryptography")
    from fastapi.testclient import TestClient

    from api.v2.app import create_production_app
    from api.v2.onboarding_brand import STATE_KEY

    app = create_production_app(
        {
            "AUTONOMY_DATABASE_URL": f"sqlite:///{tmp_path / 'autonomy.db'}",
            "AUTONOMY_CONNECTORS": "true",
            "AUTONOMY_BUSINESS_ID": "biz_1",
            "MARKETING_AI_CONNECTIONS_KEY": "zuFhGz46VLdLK_sr2rzzaQLE5Kowyi6HZkQNzmIihLo=",
        }
    )
    brand = getattr(app.state, STATE_KEY)
    assert isinstance(brand.store, SqlBrandKitStore) and isinstance(brand.assets, SqlAssetLibrary)
    with TestClient(app) as client:
        assert client.get("/v2/onboarding/brand").json()["status"] == "not_read"
        assert client.post("/v2/onboarding/brand/read").status_code == 409  # nothing connected yet


# --- review: counts that are not evidence, rules that must not block their own voice, re-approval -----


def test_a_campaign_whose_body_could_not_be_read_counts_for_its_subject_only() -> None:
    copy = [
        CopySample(
            source="klaviyo_template",
            source_id=f"t{i}",
            text="Hey {first_name},\nNew gear is in for the season.\nCheers,\nThe Acme team",
        )
        for i in range(6)
    ]
    copy += [
        CopySample(source="klaviyo_campaign", source_id=f"c{i}", subject=f"New gear for autumn {i}", text="")
        for i in range(10)
    ]
    kit = derive_kit(BrandSources(business_id=BUSINESS, read_at=T0, copy_samples=tuple(copy)))
    # Six bodies, all greeting: an empty body is not "an email without a greeting".
    assert kit.style.greeting == "Hey {{first_name}}," and finding(kit, "greeting").evidence.of == 6
    # Six bodies are too few for "never uses" rules, however many subjects came back.
    assert kit.rules.banned_words == () and kit.rules.max_exclamations is None
    assert kit.rules.subject_max is not None  # the subjects still count for their length
    assert kit.messages_read == 16 and any("only the subject" in n for n in kit.notes)


def test_a_greeting_with_an_exclamation_mark_takes_a_comma_when_their_messages_use_none() -> None:
    copy = [
        CopySample(
            source="klaviyo_campaign",
            source_id=f"c{i}",
            subject=f"Autumn arrivals {i}",
            text=("Hey {first_name}!\n" if i < 4 else "Our new range is here.\n")
            + "Good things for colder days.",
        )
        for i in range(12)
    ]
    copy += [
        CopySample(
            source="klaviyo_sms",
            source_id=f"s{i}",
            channel=Channel.SMS,
            text="Acme: the new range is in. Reply STOP to opt out.",
        )
        for i in range(30)
    ]
    kit = derive_kit(BrandSources(business_id=BUSINESS, read_at=T0, copy_samples=tuple(copy)))
    assert kit.rules.max_exclamations == 0
    assert kit.style.greeting == "Hey {{first_name}}," and "with a comma" in finding(kit, "greeting").use
    facts = BusinessFacts(business_id=BUSINESS, channels=(Channel.EMAIL, Channel.SMS))
    brief = template_brief(goal(), facts, T0)
    items = Creative(_down_llm()).build_library(brief, brand_voice="", n_per_cell=2, now=T0, kit=kit)
    rules = BrandRules.from_brief(brief, brand_voice="", kit=kit)
    assert items and all(check_rules(i, rules) == [] for i in items)


def test_template_texts_fit_the_shops_text_length_even_with_a_long_greeting() -> None:
    copy = [
        CopySample(
            source="klaviyo_campaign",
            source_id=f"c{i}",
            subject=f"Autumn arrivals {i}",
            text="Good afternoon {first_name},\nGood things for colder days.\nKind regards,\nThe Acme team",
        )
        for i in range(12)
    ]
    copy += [
        CopySample(
            source="klaviyo_sms",
            source_id=f"s{i}",
            channel=Channel.SMS,
            text="Acme: new range in. STOP to end.",
        )
        for i in range(6)
    ]
    kit = derive_kit(BrandSources(business_id=BUSINESS, read_at=T0, copy_samples=tuple(copy)))
    assert kit.rules.sms_body_max == 120 and kit.style.greeting == "Good afternoon {{first_name}},"
    guide = OfferOption(offer_id="guide", label="our care guide", kind=OfferKind.CONTENT)
    facts = BusinessFacts(business_id=BUSINESS, channels=(Channel.SMS,), offers=(guide,))
    brief = template_brief(goal(), facts, T0)
    items = Creative(_down_llm()).build_library(brief, brand_voice="", n_per_cell=2, now=T0, kit=kit)
    rules = BrandRules.from_brief(brief, brand_voice="", kit=kit)
    texts = [i for i in items if i.channel is Channel.SMS]
    assert texts and all(check_rules(i, rules) == [] for i in texts)
    assert all(len(i.body) <= 120 for i in texts)


def test_product_changes_in_shopify_keep_the_approval_and_retire_photos_of_products_gone() -> None:
    shop = seeded_shop()
    store = InMemoryBrandKitStore()
    library = InMemoryAssetLibrary()
    kit = save_read(store, read_kit(shop))
    kit = approve(store, BUSINESS, approved_by="Dana", now=T0, fingerprint=kit.fingerprint())
    record_assets(library, kit)
    assert len(library.find()) == 3

    # A new product and an archived one: the voice did not change, so nobody is asked again.
    shop.shopify.product_images = [p for p in shop.shopify.product_images if not p["id"].endswith("/12")]
    shop.shopify.add_image_product(14, "Camp Mug", "Kitchen")
    again = save_read(store, read_kit(shop))
    assert again.approved and again.catalogue_read
    record_assets(library, again)
    approved = {a.asset_id for a in library.find()}
    assert "shopify-acme-goods-14-1400" in approved  # the shop's own new product photo
    gone = library.get("shopify-acme-goods-12-1200")
    assert gone is not None and gone.status is AssetStatus.DRAFT  # its product is no longer active

    # A refused products read says nothing about what is active: the photos read before stand,
    # none is demoted, and the approval holds.
    shop.shopify.products_denied = True
    refused = save_read(store, read_kit(shop))
    assert not refused.catalogue_read and "shopify_products" in refused.incomplete
    assert {a.asset_id for a in refused.assets} == {a.asset_id for a in again.assets}
    assert refused.approved and any("could not be read in full" in n for n in refused.notes)
    record_assets(library, refused)
    assert {a.asset_id for a in library.find()} == approved


def test_a_klaviyo_outage_on_a_later_read_keeps_the_rules_read_before() -> None:
    shop = seeded_shop()
    store = InMemoryBrandKitStore()
    kit = save_read(store, read_kit(shop))
    kit = approve(store, BUSINESS, approved_by="Dana", now=T0, fingerprint=kit.fingerprint())
    shop.klaviyo.campaigns_denied = True  # e.g. a rate limit or outage on the campaigns endpoints
    later = save_read(store, read_kit(shop))
    # Without the earlier read, four templates would leave every "never uses" rule out (None).
    assert later.rules == kit.rules and later.rules.banned_words and later.messages_read == 20
    assert later.approved and any("voice read on" in n for n in later.notes)


def test_evidence_quotes_only_the_shops_own_copy_and_counts_what_it_says() -> None:
    kit = read_kit(seeded_shop())
    banned = finding(kit, "banned_words")
    assert banned.evidence.examples == ()  # those words are, by definition, not in their copy
    colours = finding(kit, "colours")
    assert (colours.evidence.seen, colours.evidence.of) == (1, 1)  # one Shopify brand setting
    sms = finding(kit, "sms_body_max")
    assert sms.evidence.seen <= sms.evidence.of == 6
