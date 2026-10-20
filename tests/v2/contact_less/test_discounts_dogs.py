"""Discount discipline and sleeping dogs: offers priced on everyone who buys and believed only on
evidence; the contact-harm model, its shrinkage and what it does to bids."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from autonomy.contracts import (
    Channel,
    ChannelStats,
    CustomerState,
    DecisionContext,
    Event,
    EventType,
)
from autonomy.contracts.events import CHANNEL_REQUIRED
from autonomy.decide.bidders import (
    EVIDENCE_HALF_LIFE_DAYS,
    OFFER_PRIOR_FLOOR,
    OFFER_PRIOR_SPREAD,
    ContactHarm,
    GoalBidder,
    contact_harm_of,
    offer_sensitivity_factor,
    organic_chance,
    shrink_offer_extra,
)
from autonomy.decide.models import BetaResponseModel, ValueModel
from autonomy.fakes.samples import sample_brief, sample_content
from autonomy.learning import build_bundle, update_models
from autonomy.learning.observations import ContactEvidence, contact_evidence, contact_evidence_all

NOW = datetime(2026, 3, 2, 10, tzinfo=UTC)
REACHABLE = ChannelStats(address_known=True, consent=True)
VALUE = 100 * 0.3  # the state's order value x margin rate
EMAIL_COST = {Channel.EMAIL: 0.0}


def state(customer_id: str = "c1", **kw: object) -> CustomerState:
    base: dict[str, object] = {
        "customer_id": customer_id,
        "as_of": NOW,
        "purchases": 2,
        "revenue": 200.0,
        "margin": 60.0,
        "last_purchase_at": NOW - timedelta(days=90),
        "channels": {Channel.EMAIL: REACHABLE},
    }
    base.update(kw)
    return CustomerState.model_validate(base)


def ctx() -> DecisionContext:
    return DecisionContext(now=NOW, briefs=(sample_brief(NOW),))


def model(control: float, plain: float, offer: float, offer_strength: float = 1e6) -> BetaResponseModel:
    """Purchase rates in the window: untreated, after the plain email, after the 10%-off email."""
    strong = BetaResponseModel.priors_from_rates(
        {"treated/*/email|*|*": plain, "control/*": control}, strength=1e6
    )
    weak = BetaResponseModel.priors_from_rates({"treated/*/email|pct10|*": offer}, strength=offer_strength)
    return BetaResponseModel({**strong, **weak})


def by_offer(bids: list) -> dict[str, object]:  # type: ignore[type-arg]
    out: dict[str, object] = {}
    for b in bids:
        out.setdefault(b.action.offer_id, b)
    return out


# --- discounts -------------------------------------------------------------------------------------


def test_an_offer_is_paid_by_everyone_who_would_buy_anyway():
    # A sure thing: buys 30% of weeks on their own; the offer adds 4 points, the plain email 2.
    m = model(control=0.30, plain=0.32, offer=0.34)
    bids = by_offer(
        GoalBidder(sample_brief(NOW), sample_content(NOW), m, ValueModel(), EMAIL_COST).bids(state(), ctx())
    )
    offer, plain = bids["pct10"], bids["none"]
    assert organic_chance(m, state(), "email|none|reminder") == pytest.approx(0.30, abs=1e-4)
    # 10% off costs $6 for every redemption: the organic 30% plus the 4 points it adds
    assert offer.offer_cost == pytest.approx(6.0 * (0.30 + offer.uplift), abs=1e-4)
    assert offer.uplift == pytest.approx(0.04, abs=1e-3)
    assert offer.expected_net < 0 < plain.expected_net


def test_an_offer_is_believed_only_as_far_as_the_evidence_goes():
    # Buys 10% of weeks on their own: if 10% off really adds 10 points it more than pays for itself.
    strong = model(control=0.10, plain=0.12, offer=0.22)
    weak = model(control=0.10, plain=0.12, offer=0.22, offer_strength=10)
    shown = by_offer(
        GoalBidder(sample_brief(NOW), sample_content(NOW), strong, ValueModel(), EMAIL_COST).bids(
            state(), ctx()
        )
    )
    hinted = by_offer(
        GoalBidder(sample_brief(NOW), sample_content(NOW), weak, ValueModel(), EMAIL_COST).bids(
            state(), ctx()
        )
    )
    assert shown["pct10"].uplift == pytest.approx(0.12, abs=2e-3)
    assert shown["pct10"].expected_net > shown["none"].expected_net  # well evidenced: the offer wins
    # Ten sends' worth of evidence for the same means: most of the extra is not believed, and the
    # plain message is the better bet (default to no offer).
    assert 0.02 < hinted["pct10"].uplift < 0.02 + 0.2 * 0.10
    assert hinted["pct10"].expected_net < hinted["none"].expected_net


def test_pruning_drops_an_offer_that_cannot_beat_its_plain_version():
    # Well measured: the offer adds a point to a sure thing. It can never pay its redemptions.
    m = model(control=0.30, plain=0.32, offer=0.33)
    full = GoalBidder(sample_brief(NOW), sample_content(NOW), m, ValueModel(), EMAIL_COST).bids(
        state(), ctx()
    )
    pruned = GoalBidder(
        sample_brief(NOW), sample_content(NOW), m, ValueModel(), EMAIL_COST, prune_z=2.0
    ).bids(state(), ctx())
    assert "pct10" in {b.action.offer_id for b in full}  # the plain bidder lists every option
    assert {b.action.offer_id for b in pruned} == {"none"}


def test_the_gate_not_the_spread_keeps_a_thinly_evidenced_offer_out():
    # Ten sends' worth of evidence that 10% off adds 10 points: so uncertain that the spread-based
    # pruning alone would keep it (its optimistic net beats the plain message's pessimistic one),
    # but on the evidence it is expected to lose to the plain version, so it is not bid.
    m = model(control=0.10, plain=0.12, offer=0.22, offer_strength=10)
    full = by_offer(
        GoalBidder(sample_brief(NOW), sample_content(NOW), m, ValueModel(), EMAIL_COST).bids(state(), ctx())
    )
    offer, plain = full["pct10"], full["none"]
    z = 2.0
    assert offer.expected_net + z * offer.uncertainty > plain.expected_net - z * plain.uncertainty
    assert offer.expected_net < plain.expected_net
    pruned = GoalBidder(sample_brief(NOW), sample_content(NOW), m, ValueModel(), EMAIL_COST, prune_z=z).bids(
        state(), ctx()
    )
    assert {b.action.offer_id for b in pruned} == {"none"}


def test_the_gate_leaves_an_offer_a_rule_blocks_and_quote_can_skip_pruning():
    # The same thinly evidenced offer, now waiting for approval: it can never go out, so the gate
    # keeps it and the approval rule is what the decision reports. quote(prune=False) lists what
    # pruning left out, for explanations.
    m = model(control=0.10, plain=0.12, offer=0.22, offer_strength=10)
    brief = sample_brief(NOW)
    offers = type(brief.envelope.offers)(
        o.model_copy(update={"needs_approval": True}) if o.offer_id == "pct10" else o
        for o in brief.envelope.offers
    )
    waiting = brief.model_copy(update={"envelope": brief.envelope.model_copy(update={"offers": offers})})
    held = GoalBidder(waiting, sample_content(NOW), m, ValueModel(), EMAIL_COST, prune_z=2.0)
    assert "pct10" in by_offer(held.bids(state(), ctx()))
    gated = GoalBidder(sample_brief(NOW), sample_content(NOW), m, ValueModel(), EMAIL_COST, prune_z=2.0)
    assert "pct10" not in by_offer(gated.quote(state(), ctx()))
    assert "pct10" in by_offer(gated.quote(state(), ctx(), prune=False))


def test_shrink_offer_extra_and_offer_sensitivity():
    # no evidence: nothing is believed; a precise measurement passes almost unchanged
    assert shrink_offer_extra(0.05, 1.0, 0.02)[0] == pytest.approx(0.0, abs=1e-4)
    mean, sd = shrink_offer_extra(0.05, 1e-4, 0.02)
    assert mean == pytest.approx(0.05, rel=1e-3) and sd == pytest.approx(1e-4, rel=1e-2)
    # equal evidence and prior spread: half believed
    tau = OFFER_PRIOR_SPREAD * 0.02
    assert shrink_offer_extra(0.05, tau, 0.02)[0] == pytest.approx(0.025)
    # a plain message that does nothing: the spread does not collapse to zero, so a precisely
    # measured discount is still believed and a thinly measured one is not
    floor = OFFER_PRIOR_SPREAD * OFFER_PRIOR_FLOOR
    assert shrink_offer_extra(0.05, 0.0, 0.0) == (0.05, 0.0)
    assert shrink_offer_extra(0.05, 1e-4, -0.01)[0] == pytest.approx(0.05, rel=1e-3)
    assert shrink_offer_extra(0.05, 10 * floor, 0.0)[0] < 0.05 * 0.01 + 1e-9
    assert offer_sensitivity_factor(state()) == 1.0
    assert offer_sensitivity_factor(state(offer_sensitivity=0.3)) == pytest.approx(1.0)
    assert offer_sensitivity_factor(state(offer_sensitivity=0.06)) == pytest.approx(0.2)
    assert offer_sensitivity_factor(state(offer_sensitivity=0.95)) == pytest.approx(2.0)


def test_a_full_price_buyer_gets_less_credit_for_an_offer():
    m = model(control=0.02, plain=0.04, offer=0.14)
    bidder = GoalBidder(sample_brief(NOW), sample_content(NOW), m, ValueModel(), EMAIL_COST)
    usual = by_offer(bidder.bids(state(), ctx()))["pct10"]
    full_price = by_offer(bidder.bids(state(offer_sensitivity=0.06), ctx()))["pct10"]
    assert usual.uplift == pytest.approx(0.12, abs=2e-3)
    assert full_price.uplift == pytest.approx(0.02 + 0.2 * 0.10, abs=2e-3)


# --- contact evidence --------------------------------------------------------------------------------

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def ev(kind: EventType, days: float, *, decision: str | None = None, **attrs: object) -> Event:
    return Event.model_validate(
        {
            "event_id": f"e_{kind.value}_{days}",
            "customer_id": "c1",
            "type": kind,
            "ts": T0 + timedelta(days=days),
            "source": "test",
            "decision_id": decision,
            "channel": Channel.EMAIL if kind in CHANNEL_REQUIRED else None,
            "value": 50.0 if kind is EventType.PURCHASE else None,
            "attrs": attrs,
        }
    )


EVENTS = [
    ev(EventType.SIGNUP, 0),
    ev(EventType.SEND, 10, decision="d1", offer_id="pct10"),
    ev(EventType.CLICK, 10.1, decision="d1"),
    ev(EventType.VISIT, 10.15),  # came through the click
    ev(EventType.PURCHASE, 12),  # inside the offer's window, two days after the click
    ev(EventType.VISIT, 20),  # direct
    ev(EventType.SEND, 25, decision="d2", offer_id="none"),
    ev(EventType.FAIL, 25.001, decision="d2"),  # never arrived: exposes nothing
    ev(EventType.PURCHASE, 30),  # open day
    ev(EventType.UNSUBSCRIBE, 40),
]


def test_contact_evidence_splits_exposed_and_open_days():
    got = contact_evidence(EVENTS, T0 + timedelta(days=50), 7)
    assert got.exposed_days == pytest.approx(7.0)
    assert got.open_days == pytest.approx(43.0)
    assert got.offer_days == pytest.approx(7.0)
    assert (got.exposed_purchases, got.offer_purchases, got.open_purchases) == (1.0, 1.0, 1.0)
    assert (got.sends, got.offer_sends, got.clicks, got.stops) == (1.0, 1.0, 1.0, 1.0)
    assert (got.visits, got.direct_visits, got.message_purchases) == (2.0, 1.0, 1.0)


def test_contact_evidence_counts_each_moment_once_across_nights():
    once = contact_evidence(EVENTS, T0 + timedelta(days=50), 7)
    night1 = contact_evidence(
        [e for e in EVENTS if e.ts < T0 + timedelta(days=11)], T0 + timedelta(days=11), 7
    )
    # the second night gets the whole log again, or only the recent events: same answer
    night2 = contact_evidence(EVENTS, T0 + timedelta(days=50), 7, night1)
    recent = [e for e in EVENTS if e.ts >= T0 + timedelta(days=11)]
    night2b = contact_evidence(recent, T0 + timedelta(days=50), 7, night1)
    for got in (night2, night2b):
        assert got.exposed_days == pytest.approx(once.exposed_days)
        assert got.open_days == pytest.approx(once.open_days)
        assert got.purchases == once.purchases and got.clicks == once.clicks
        assert got.message_purchases == once.message_purchases
    assert night1.until == T0 + timedelta(days=11) and night1.exposed_until == T0 + timedelta(days=17)
    assert contact_evidence_all({"c1": EVENTS, "c2": []}, T0 + timedelta(days=50), 7).keys() == {"c1"}


# --- the contact-harm model ----------------------------------------------------------------------------


def population(
    n: int = 1000, share: float = 0.15, harmed_ratio: float = 0.4, seed: int = 3
) -> tuple[dict[str, ContactEvidence], set[str]]:
    """Customers with a year of history and 10-25 past messages. Ordinary ones buy 30% more in the
    week after a message and click on about one in five (their keenness varies); harmed ones buy
    `harmed_ratio` as much, almost never click, unsubscribe and visit directly more."""
    rng = np.random.default_rng(seed)
    out: dict[str, ContactEvidence] = {}
    harmed: set[str] = set()
    for i in range(n):
        cid = f"p{i:04d}"
        bad = bool(rng.random() < share)
        rate = float(rng.gamma(2.0, 0.02))  # own purchases per day
        sends = int(rng.integers(10, 25))
        exposed = sends * 7.0 * 0.6
        open_ = 365.0 - exposed
        ratio = harmed_ratio if bad else 1.3
        click = 0.005 if bad else float(rng.beta(4.0, 16.0))
        out[cid] = ContactEvidence(
            exposed_days=exposed,
            open_days=open_,
            exposed_purchases=float(rng.poisson(rate * ratio * exposed)),
            open_purchases=float(rng.poisson(rate * open_)),
            sends=float(sends),
            clicks=float(rng.binomial(sends, click)),
            stops=float(rng.random() < sends * (0.01 if bad else 0.002)),
            direct_visits=float(rng.poisson(365 * (0.04 if bad else 0.02))),
        )
        if bad:
            harmed.add(cid)
    return out, harmed


def fitted(evidence: dict[str, ContactEvidence]) -> ContactHarm:
    harm = ContactHarm()
    harm.observe(evidence)
    harm.refit()
    return harm


def test_harm_model_finds_who_contact_puts_off_without_labels():
    evidence, harmed = population()
    harm = fitted(evidence)
    assert harm.fit is not None
    assert harm.fit.coefficients[0] > 0  # clicking goes with buying more after a message
    assert harm.fit.band_ratios[0] < 0.8 < 1.0 < min(harm.fit.band_ratios[2:])
    dent_bad = np.mean([harm.dent(c) for c in harmed])
    dent_ok = np.mean([harm.dent(c) for c in evidence if c not in harmed])
    # the clues tilt the odds, they do not label anyone; the dent charged is the credible one
    assert dent_bad > 0.15 > 0.02 > dent_ok
    assert np.mean([harm.p_harm(c) for c in harmed]) > 2.5 * np.mean(
        [harm.p_harm(c) for c in evidence if c not in harmed]
    )
    assert "put off by messages" in harm.describe()


def test_harm_model_charges_nothing_when_contact_harms_nobody():
    for seed in (1, 2, 3):
        evidence, _ = population(share=0.0, seed=seed)
        harm = fitted(evidence)
        # no band is charged; a rare customer's own record can make a sliver of harm credible
        assert harm.share == 0.0
        assert max(harm.expected_loss(c, 0.2) for c in evidence) < 0.005
        assert sum(harm.dent(c) > 0 for c in evidence) < 0.02 * len(evidence)
        assert "No sign" in harm.describe()


def test_harm_is_shrunk_by_how_much_record_there_is():
    evidence, harmed = population()
    one = max(sorted(harmed), key=lambda c: evidence[c].sends)
    # a look-alike of a harmed customer with only two messages and no purchases yet
    evidence["thin"] = ContactEvidence(exposed_days=14.0, open_days=30.0, sends=2.0)
    harm = fitted(evidence)
    assert harm.dent("nobody") == 0.0 and harm.p_harm("nobody") == 0.0  # no record: the average
    assert harm.dent("thin") < harm.dent(one)
    assert ContactHarm().expected_loss("c1", 0.5) == 0.0  # not fitted: no charge
    small = ContactHarm()
    small.observe(dict(list(evidence.items())[:10]))
    small.refit()
    assert not small.fitted  # too few customers to say anything


def test_expected_harm_is_taken_off_every_option_and_clear_harm_means_no_bids():
    evidence, _ = population()
    harm = fitted(evidence)
    bad = max(evidence, key=harm.dent)
    m = model(control=0.20, plain=0.25, offer=0.26)
    plain_bidder = GoalBidder(sample_brief(NOW), sample_content(NOW), m, ValueModel(), EMAIL_COST)
    harmed_bidder = GoalBidder(sample_brief(NOW), sample_content(NOW), m, ValueModel(), EMAIL_COST, harm=harm)
    loss = harm.expected_loss(bad, 0.20)
    assert loss == pytest.approx(harm.dent(bad) * 0.20) and loss > 0.05  # more than the email adds
    before = by_offer(plain_bidder.bids(state(bad), ctx()))
    after = by_offer(harmed_bidder.bids(state(bad), ctx()))
    assert after["none"].uplift == pytest.approx(before["none"].uplift - loss, abs=1e-6)
    assert after["none"].expected_net == pytest.approx(before["none"].expected_net - loss * VALUE, abs=1e-4)
    # a stranger, with no record, is the average customer: no charge
    stranger = by_offer(harmed_bidder.bids(state("new_face"), ctx()))["none"]
    assert stranger.uplift == pytest.approx(before["none"].uplift, abs=1e-9)
    # with pruning on (the agent), clear harm leaves nothing to send
    pruned = GoalBidder(
        sample_brief(NOW), sample_content(NOW), m, ValueModel(), EMAIL_COST, harm=harm, prune_z=2.0
    )
    assert pruned.bids(state(bad), ctx()) == []
    assert pruned.bids(state("new_face"), ctx())


def test_update_models_learns_harm_once_and_bidders_find_it():
    bundle = build_bundle()
    assert contact_harm_of(bundle.response) is None
    log = {"c1": EVENTS}
    update_models(bundle, [], log, {}, T0 + timedelta(days=30), 7)
    update_models(bundle, [], log, {}, T0 + timedelta(days=50), 7)
    harm = contact_harm_of(bundle.response)
    assert harm is not None and not harm.fitted  # one customer: not enough to fit
    assert harm.data_start == T0 + timedelta(days=12)  # the first purchase on file
    got = harm.evidence["c1"]
    once = contact_evidence(
        EVENTS,
        T0 + timedelta(days=50),
        7,
        data_start=harm.data_start,
        half_life_days=EVIDENCE_HALF_LIFE_DAYS,
    )
    assert isinstance(got, ContactEvidence)
    assert got.exposed_days == pytest.approx(once.exposed_days)
    assert got.open_days == pytest.approx(once.open_days)
    assert got.purchases == pytest.approx(once.purchases)
    bidder = GoalBidder(sample_brief(NOW), sample_content(NOW), bundle.response)
    assert bidder.bids(state(), ctx())  # an unfitted model charges nothing


def test_a_record_that_opens_with_a_purchase_does_not_count_it():
    # An order import: the customer is on file because of their first order. Counting it as an
    # open-day purchase would make every such customer look as if messages put them off.
    imported = [e for e in EVENTS if e.type is not EventType.SIGNUP]
    imported.insert(0, ev(EventType.PURCHASE, 0))
    got = contact_evidence(imported, T0 + timedelta(days=50), 7)
    assert got.since == T0 and got.open_purchases == 1.0 and got.exposed_purchases == 1.0
    # with a signup first, a later first purchase is ordinary buying and is counted
    signed = contact_evidence([*EVENTS, ev(EventType.PURCHASE, 3)], T0 + timedelta(days=50), 7)
    assert signed.open_purchases == 2.0


def test_recency_weighting_counts_the_recent_stretch_more():
    now = T0 + timedelta(days=50)
    plain = contact_evidence(EVENTS, now, 7)
    weighted = contact_evidence(EVENTS, now, 7, half_life_days=20.0)
    assert weighted.open_purchases == pytest.approx(0.5)  # day 30, twenty days before now
    assert weighted.exposed_purchases == pytest.approx(0.5 ** (38 / 20))
    assert weighted.days < plain.days
    # decaying in two steps gives the same as one
    night1 = contact_evidence(EVENTS, T0 + timedelta(days=20), 7, half_life_days=20.0)
    night2 = contact_evidence(EVENTS, now, 7, night1, half_life_days=20.0)
    assert night2.open_days == pytest.approx(weighted.open_days)
    assert night2.exposed_purchases == pytest.approx(weighted.exposed_purchases)
