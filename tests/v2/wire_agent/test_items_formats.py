"""Product- and format-aware bids: refill picks, recommended products, formats and `explain_bids`.

Tiny hand-built briefs, content and states; no Gym run. Each test runs in well under a second.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from autonomy.assets.library import Asset, AssetKind, AssetStatus
from autonomy.catalog import enrich as catalog_enrich
from autonomy.contracts import (
    NO_OFFER,
    Audience,
    AudienceFilter,
    Brief,
    Channel,
    ChannelStats,
    ContentItem,
    ContentStatus,
    CustomerState,
    DecisionContext,
    DecisionKind,
    Envelope,
    Format,
    GoalKind,
    Group,
    Objective,
    OfferKind,
    OfferOption,
    Theme,
)
from autonomy.decide import bidders as bidders_mod
from autonomy.decide.bidders import (
    REFILL_LIFT,
    REFILL_P14_HALF,
    GoalBidder,
    asset_lookup,
    refill_boost,
)
from autonomy.decide.models import ANY, PersonalEffects, action_features, action_key, base_key
from autonomy.runtime.agent import BASELINE_GROUP, ClickResponseModel, build_agent_policy

NOW = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)  # a Monday, 10:00 UTC
REACHABLE = ChannelStats(address_known=True, consent=True)
SERUM = "sku_serum"
MOISTURISER = "sku_moisturiser"
FREE_SHIPPING = OfferOption(
    offer_id="ship", label="Free shipping", kind=OfferKind.FREE_SHIPPING, cost_per_redemption=5.0
)


def brief(
    goal_id: str = "g1",
    *,
    channels: tuple[Channel, ...] = (Channel.EMAIL,),
    offers: tuple[OfferOption, ...] = (NO_OFFER,),
    audience: Audience | None = None,
) -> Brief:
    return Brief(
        brief_id=f"b_{goal_id}",
        goal_id=goal_id,
        objective=Objective.GROSS_PROFIT,
        audience=audience or Audience(description="everyone"),
        envelope=Envelope(
            channels=channels, offers=offers, budget=1_000.0, quiet_start_hour=22, quiet_end_hour=8
        ),
        created_at=NOW,
        approved_at=NOW,
    )


def item(
    content_id: str,
    *,
    channel: Channel = Channel.EMAIL,
    theme: Theme = Theme.REMINDER,
    fmt: Format = Format.TEXT,
    body: str = "Hi {{first_name}}, time to stock up?",
    slots: tuple[str, ...] = ("first_name",),
    asset_url: str | None = None,
    offer_id: str = "none",
) -> ContentItem:
    return ContentItem(
        content_id=content_id,
        channel=channel,
        format=fmt,
        theme=theme,
        offer_id=offer_id,
        subject="Hello" if channel is Channel.EMAIL else None,
        body=body,
        slots=slots,
        asset_url=asset_url,
        status=ContentStatus.APPROVED,
        created_at=NOW,
    )


def state(customer_id: str = "c1", **kw: object) -> CustomerState:
    base: dict[str, object] = {
        "customer_id": customer_id,
        "as_of": NOW,
        "channels": {Channel.EMAIL: REACHABLE, Channel.WHATSAPP: REACHABLE, Channel.SMS: REACHABLE},
        "purchases": 3,
        "revenue": 150.0,
        "margin": 60.0,
        "first_seen_at": NOW - timedelta(days=300),
        "last_purchase_at": NOW - timedelta(days=40),
    }
    base.update(kw)
    return CustomerState.model_validate(base)


def ctx(b: Brief, content: list[ContentItem], seed: int = 1) -> DecisionContext:
    return DecisionContext(
        now=NOW, briefs=(b,), content=tuple(content), budget_left={b.goal_id: 1_000.0}, seed=seed
    )


OPEN = {"refill_p14": 0.3, "refill_due_days": 1.0, "refill_window_open": 1.0}
CLOSED = {"refill_p14": 0.3, "refill_due_days": 40.0, "refill_window_open": 0.0}


# --- refill ---------------------------------------------------------------------------------------


def test_refill_score_names_match_the_catalogue() -> None:
    assert bidders_mod.REFILL_P14 == catalog_enrich.REFILL_P14
    assert bidders_mod.REFILL_DUE_DAYS == catalog_enrich.REFILL_DUE_DAYS
    assert bidders_mod.REFILL_WINDOW_OPEN == catalog_enrich.REFILL_WINDOW_OPEN


def test_refill_window_raises_the_refill_option_and_sets_item_id() -> None:
    b = brief(offers=(NO_OFFER, FREE_SHIPPING))
    content = [item("ci_plain"), item("ci_ship", offer_id="ship")]
    picks = {"c1": (SERUM, "Your serum is probably running low")}
    bidder = GoalBidder(b, content, kind=GoalKind.REPEAT_PURCHASE, item_lookup=picks.get)
    c = ctx(b, content)

    def plain_bid(scores: dict[str, float], cid: str = "c1") -> tuple[float, float, str | None]:
        found = [x for x in bidder.bids(state(cid, scores=scores), c) if x.action.offer_id == "none"]
        return found[0].uplift, found[0].uncertainty, found[0].action.item_id

    open_uplift, open_sd, open_item = plain_bid(OPEN)
    closed_uplift, closed_sd, closed_item = plain_bid(CLOSED)
    assert open_item == closed_item == SERUM  # the action is about the serum either way
    expected = 1.0 + REFILL_LIFT * 0.3 / (0.3 + REFILL_P14_HALF)
    assert open_uplift == pytest.approx(closed_uplift * expected)
    assert open_sd == pytest.approx(closed_sd * expected)
    # no refill pick: no boost and no item, even with the scores
    other_uplift, _, other_item = plain_bid(OPEN, cid="c2")
    assert other_item is None
    assert other_uplift == pytest.approx(closed_uplift)

    # free shipping is paid by everyone who refills while it is open: at least p14 of them
    ship = next(x for x in bidder.bids(state(scores=OPEN), c) if x.action.offer_id == "ship")
    assert ship.offer_cost >= FREE_SHIPPING.cost_per_redemption * 0.3


def test_refill_boost_is_shrunk_and_needs_an_open_window() -> None:
    assert refill_boost(state(scores=CLOSED)) == 1.0
    assert refill_boost(state()) == 1.0  # no refill scores at all
    low = refill_boost(state(scores={**OPEN, "refill_p14": 0.02}))
    high = refill_boost(state(scores={**OPEN, "refill_p14": 0.9}))
    assert 1.0 < low < 1.2 < high < 1.0 + REFILL_LIFT
    # without the window flag, the due date decides
    assert refill_boost(state(scores={"refill_p14": 0.3, "refill_due_days": 3.0})) > 1.0
    assert refill_boost(state(scores={"refill_p14": 0.3, "refill_due_days": 30.0})) == 1.0


def test_an_audience_on_refill_scores_makes_a_refill_goal() -> None:
    audience = Audience(
        description="refill due",
        filters=(AudienceFilter(field="scores.refill_window_open", op="eq", value=1.0),),
    )
    assert GoalBidder(brief(audience=audience), []).refill
    assert not GoalBidder(brief(), []).refill
    assert GoalBidder(brief(), [], kind=GoalKind.REPEAT_PURCHASE).refill


# --- recommended product --------------------------------------------------------------------------


PRODUCT_ITEM = item(
    "ci_product",
    theme=Theme.PERSONAL,
    body="Hi {{first_name}}, {{recommended_product}} goes well with your serum.",
    slots=("first_name", "recommended_product"),
)


def test_no_item_makes_recommended_product_content_ineligible() -> None:
    b = brief()
    content = [item("ci_plain"), PRODUCT_ITEM]
    c = ctx(b, content)
    alone = GoalBidder(b, content)
    assert [x.action.content_id for x in alone.bids(state(), c)] == ["ci_plain"]

    complements = {"c1": (MOISTURISER, "Often bought with your serum")}
    paired = GoalBidder(b, content, recommender=complements.get)
    by_content = {x.action.content_id: x.action.item_id for x in paired.bids(state(), c)}
    # a plain goal names the product only where the content does
    assert by_content == {"ci_plain": None, "ci_product": MOISTURISER}
    # someone without a recommendation still gets the plain message only
    assert [x.action.content_id for x in paired.bids(state("c2"), c)] == ["ci_plain"]

    cross = GoalBidder(b, content, kind=GoalKind.CROSS_SELL, recommender=complements.get)
    assert {x.action.item_id for x in cross.bids(state(), c)} == {MOISTURISER}
    choice = cross.item_for(state())
    assert choice is not None and choice.reason == "Often bought with your serum"


# --- formats --------------------------------------------------------------------------------------


def asset(asset_id: str, url: str, *, kind: AssetKind = AssetKind.IMAGE, item_id: str | None = None) -> Asset:
    return Asset(
        asset_id=asset_id,
        kind=kind,
        url=url,
        alt_text="A bottle of serum",
        status=AssetStatus.APPROVED,
        approved_by="ana",
        item_id=item_id,
        created_at=NOW,
    )


def test_format_options_respect_channel_support_and_name_their_asset() -> None:
    b = brief(channels=(Channel.EMAIL, Channel.SMS, Channel.WHATSAPP))
    img = "https://cdn.example.com/serum.jpg"
    content = [
        item("e_text"),
        item("e_img", fmt=Format.TEXT_IMAGE, asset_url=img),
        item("e_img_no_media", theme=Theme.EDUCATION, fmt=Format.TEXT_IMAGE),  # nothing to show
        item("e_video", fmt=Format.VIDEO, asset_url="https://cdn.example.com/v.mp4"),  # email: no video
        item("s_text", channel=Channel.SMS),
        item("s_img", channel=Channel.SMS, fmt=Format.TEXT_IMAGE, asset_url=img),  # SMS: text only
        item("w_text", channel=Channel.WHATSAPP),
        item("w_card", channel=Channel.WHATSAPP, fmt=Format.CAROUSEL, asset_url=f"{img} {img}"),
    ]
    lookup = asset_lookup([asset("serum_shot", img, item_id=SERUM)])
    c = ctx(b, content)

    def offered(carousel_template: bool = False) -> dict[str, tuple[Format, str | None]]:
        bidder = GoalBidder(b, content, asset_of=lookup, carousel_template=carousel_template)
        return {
            x.action.content_id or "": (x.action.format, x.action.asset_id) for x in bidder.bids(state(), c)
        }

    plain = offered()
    assert plain == {
        "e_text": (Format.TEXT, None),
        "e_img": (Format.TEXT_IMAGE, "serum_shot"),
        "s_text": (Format.TEXT, None),
        "w_text": (Format.TEXT, None),
    }
    # WhatsApp carousels need the brand's carousel templates
    assert offered(carousel_template=True)["w_card"] == (Format.CAROUSEL, "serum_shot")

    # an action about the moisturiser never shows the serum
    picks = {"c1": MOISTURISER}
    refill = GoalBidder(b, content, kind=GoalKind.REPEAT_PURCHASE, item_lookup=picks.get, asset_of=lookup)
    assert "e_img" not in {x.action.content_id for x in refill.bids(state(), c)}


def test_format_is_its_own_option_key() -> None:
    img_key = action_key(Channel.EMAIL, "none", Theme.EDUCATION, Format.TEXT_IMAGE)
    assert img_key == "email|none|education|text_image"
    assert action_key(Channel.EMAIL, "none", Theme.EDUCATION, Format.TEXT) == "email|none|education"
    assert base_key(img_key) == "email|none|education"
    assert action_features("email|none|education") == ("email", "none", "education", "text")
    assert action_features(img_key)[3] == "text_image"


def test_format_factor_learns_from_clicks() -> None:
    people = PersonalEffects()
    text = "email|none|education"
    image = "email|none|education|text_image"
    for i in range(90):
        # answers images, ignores the same words in plain text
        people.observe("fan", image, expected=0.1, clicks=1.0 if i % 3 == 0 else 0.0)
        people.observe("fan", text, expected=0.1, clicks=0.0)
    fan_image, _ = people.factor("fan", image)
    fan_text, _ = people.factor("fan", text)
    assert fan_image > 1.5 * fan_text
    # it is a taste for the format, so it carries to images on another channel and theme
    elsewhere_image, _ = people.factor("fan", "whatsapp|none|reminder|text_image")
    elsewhere_text, _ = people.factor("fan", "whatsapp|none|reminder")
    assert elsewhere_image > 1.5 * elsewhere_text
    # someone we know nothing about is exactly their segment
    assert people.factor("stranger", image)[0] == 1.0


def test_click_model_shrinks_a_format_towards_its_words_and_learns_the_person() -> None:
    model = ClickResponseModel()
    text = "email|none|education"
    image = f"{text}|text_image"
    for _ in range(50):
        model.observe_send("x", "light", text)
    for _ in range(10):
        model.observe_click("x", "light", text)
    # an image option nobody has had yet starts at its words' business-wide rate
    assert model.click_rate(ANY, image).mean == pytest.approx(model.click_rate(ANY, text).mean)
    for i in range(30):
        model.observe_send("fan", "light", image)
        model.observe_send("fan", "light", text)
        if i % 2 == 0:
            model.observe_click("fan", "light", image)
    fan = state("fan", segment="light")
    assert model.predict(fan, image)[0] > model.predict(fan, text)[0]


# --- explain_bids ---------------------------------------------------------------------------------


NEEDS_OK = OfferOption(
    offer_id="pct20",
    label="20% off",
    kind=OfferKind.PERCENT_OFF,
    amount=20.0,
    cost_per_redemption=1.0,
    needs_approval=True,
)


def _agent_setup() -> tuple[Brief, list[ContentItem], dict[str, tuple[str, str]]]:
    b = brief(
        "g_refill", channels=(Channel.EMAIL, Channel.WHATSAPP), offers=(NO_OFFER, FREE_SHIPPING, NEEDS_OK)
    )
    img = "https://cdn.example.com/serum.jpg"
    content = [
        item("e_remind"),
        item("e_edu", theme=Theme.EDUCATION),
        item("e_img", theme=Theme.EDUCATION, fmt=Format.TEXT_IMAGE, asset_url=img),
        item("e_ship", offer_id="ship"),
        item("e_pct", offer_id="pct20", theme=Theme.SAVINGS),
        item("w_remind", channel=Channel.WHATSAPP),
    ]
    return b, content, {"c1": (SERUM, "Your serum is probably running low")}


def test_explain_bids_has_no_side_effects() -> None:
    b, content, picks = _agent_setup()
    kinds = {b.goal_id: GoalKind.REPEAT_PURCHASE}
    explained = build_agent_policy([b], content, kinds=kinds, item_lookup=picks.get)
    plain = build_agent_policy([b], content, kinds=kinds, item_lookup=picks.get)
    c = ctx(b, content, seed=7)
    s = state(scores=OPEN)

    first = explained.explain_bids(s, c)
    assert first == explained.explain_bids(s, c)  # asking twice gives the same answer
    assert first and first[0].allowed and first[0].bid.action.item_id == SERUM
    assert first[0].item_reason == "Your serum is probably running low"
    nets = [e.net for e in first if e.allowed]
    assert nets == sorted(nets, reverse=True)

    after = explained.decide(s, c)
    alone = plain.decide(s, c)
    assert after.model_dump() == alone.model_dump()
    # the decision logs the top of the very ranking explain_bids gave
    assert list(after.bids) == [e.bid for e in first if e.allowed][: len(after.bids)]
    if after.action is not None:
        assert after.action.item_id == SERUM


def test_explain_bids_reports_block_codes() -> None:
    b, content, picks = _agent_setup()
    agent = build_agent_policy([b], content, item_lookup=picks.get)
    c = ctx(b, content)
    capped = agent.explain_bids(state(scores=OPEN, contacts_7d=5), c)
    assert capped and not any(e.allowed or e.considered for e in capped)
    codes = {e.bid.action.offer_id: e.blocked_by for e in capped}
    # the discount waiting for approval is blocked for that; everything else by the weekly cap
    assert codes == {"none": "weekly_cap", "ship": "weekly_cap", "pct20": "offer_needs_approval"}
    decision = agent.decide(state(scores=OPEN, contacts_7d=5), c)
    assert decision.kind is DecisionKind.NOTHING
    assert set(decision.blocked_by) == {"weekly_cap", "offer_needs_approval"}

    held = agent.explain_bids(state("c9", group=Group.HOLDOUT), c)
    assert held and {e.blocked_by for e in held} == {"holdout"}
    base = agent.explain_bids(state("c8", group=Group.BASELINE), c)
    assert base and {e.blocked_by for e in base} == {BASELINE_GROUP}


def test_format_costs_are_charged_on_the_rich_option_only() -> None:
    b = brief()
    img = "https://cdn.example.com/serum.jpg"
    content = [item("e_text"), item("e_img", fmt=Format.TEXT_IMAGE, asset_url=img)]
    c = ctx(b, content)
    free = {x.action.content_id: x for x in GoalBidder(b, content).bids(state(), c)}
    paid = {
        x.action.content_id: x
        for x in GoalBidder(b, content, format_costs={Format.TEXT_IMAGE: 0.5}).bids(state(), c)
    }
    assert paid["e_text"].expected_net == pytest.approx(free["e_text"].expected_net)
    assert paid["e_img"].channel_cost == pytest.approx(free["e_img"].channel_cost + 0.5)
    assert paid["e_img"].expected_net == pytest.approx(free["e_img"].expected_net - 0.5)


def test_asset_lookup_ignores_assets_that_are_not_approved() -> None:
    img = "https://cdn.example.com/serum.jpg"
    draft = asset("serum_draft", img).model_copy(update={"status": AssetStatus.DRAFT, "approved_by": None})
    content = item("e_img", fmt=Format.TEXT_IMAGE, asset_url=img)
    assert asset_lookup([draft])(content) is None
    found = asset_lookup([draft, asset("serum_shot", img)])(content)
    assert found is not None and found.asset_id == "serum_shot"


def test_explain_bids_sees_a_send_already_scheduled() -> None:
    b, content, picks = _agent_setup()
    agent = build_agent_policy(
        [b], content, kinds={b.goal_id: GoalKind.REPEAT_PURCHASE}, item_lookup=picks.get
    )
    s = state(scores=OPEN)
    first = agent.decide(s, ctx(b, content, seed=3))
    assert first.kind is DecisionKind.ACT  # scheduled, not yet in the state
    later = ctx(b, content, seed=3).model_copy(update={"now": NOW + timedelta(minutes=30)})
    explained = agent.explain_bids(s, later, trigger="cart_1")
    decision = agent.decide(s, later, trigger="cart_1")
    # the pending send holds the next message (the gap rule), in the explanation as in the decision
    assert {e.blocked_by for e in explained} == set(decision.blocked_by)
    assert "too_soon" in decision.blocked_by
    assert list(decision.bids) == [e.bid for e in explained if e.allowed][: len(decision.bids)]
