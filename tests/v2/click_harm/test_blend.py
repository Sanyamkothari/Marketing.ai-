"""The purchase-weighted gain: the click model's gain weighed against each customer's own purchases.

A put-off customer still clicks, so uplift read from clicks credits them with a message's full gain
while their buying drops. `ContactHarm.purchase_shift` combines the click reading with their own
purchases inside and outside message weeks by how well each is measured, and the bidder takes the
purchase drop off once (the larger of that and the credible dent, never both)."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from autonomy.contracts import Channel, ChannelStats, CustomerState, DecisionContext
from autonomy.decide import bidders
from autonomy.decide.bidders import CLICK_GAP_MIN, ContactHarm, GoalBidder
from autonomy.decide.models import BetaResponseModel, ValueModel
from autonomy.fakes.samples import sample_brief, sample_content
from autonomy.learning.observations import ContactEvidence
from autonomy.runtime.agent import ClickResponseModel

NOW = datetime(2026, 3, 2, 10, tzinfo=UTC)
REACHABLE = ChannelStats(address_known=True, consent=True)
ORGANIC = 0.10  # their chance of buying in a message's week without one
GAIN = 0.03  # what the click model credits a message with: a ratio of 1.3
GAIN_SD = 0.01


def population(
    n: int = 600, share: float = 0.15, harmed_ratio: float = 0.4, ok_ratio: float = 1.3, seed: int = 5
) -> tuple[dict[str, ContactEvidence], set[str]]:
    """A year of history: ordinary customers buy `ok_ratio` as much in a message's week and click on
    about one in five messages; harmed ones buy `harmed_ratio` as much and hardly click."""
    rng = np.random.default_rng(seed)
    out: dict[str, ContactEvidence] = {}
    harmed: set[str] = set()
    for i in range(n):
        cid = f"p{i:04d}"
        bad = bool(rng.random() < share)
        rate = float(rng.gamma(2.0, 0.02))
        sends = int(rng.integers(10, 25))
        exposed = sends * 7.0 * 0.6
        open_ = 365.0 - exposed
        ratio = harmed_ratio if bad else ok_ratio
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


def record(purchases: float, ratio: float, exposed: float = 180.0, open_: float = 180.0) -> ContactEvidence:
    """One customer's record with exactly the purchases a ratio gives (half their days exposed)."""
    rate = purchases / (exposed * ratio + open_)
    return ContactEvidence(
        exposed_days=exposed,
        open_days=open_,
        exposed_purchases=round(rate * ratio * exposed),
        open_purchases=purchases - round(rate * ratio * exposed),
        sends=40.0,
        clicks=6.0,
    )


def read(evidence: dict[str, ContactEvidence], gain: float = GAIN, sd: float = GAIN_SD) -> ContactHarm:
    """A harm model fitted to these records, with the click model having credited everyone with
    `gain` (as the bidders note it), so the refit reads the gap between records and clicks."""
    harm = ContactHarm()
    harm.observe(evidence)
    for cid in evidence:
        harm.note_click(cid, ORGANIC, gain, sd)
    harm.refit()
    return harm


def fitted(extra: dict[str, ContactEvidence] | None = None, **kw: float) -> ContactHarm:
    evidence, _ = population(**kw)  # type: ignore[arg-type]
    evidence.update(extra or {})
    return read(evidence)


def blended(harm: ContactHarm, cid: str, gain: float = GAIN, sd: float = GAIN_SD) -> float:
    return gain - harm.purchase_shift(cid, ORGANIC, gain, sd)


def test_little_history_keeps_the_click_estimate() -> None:
    harm = fitted({"thin": record(2.0, 0.5), "one": record(1.0, 0.2), "dog": record(80.0, 0.3)})
    # no record at all: exactly the click gain
    assert harm.purchase_shift("nobody", ORGANIC, GAIN, GAIN_SD) == 0.0
    # one or two purchases say little: the ratio the click gain implies (1.3) moves by about 0.1
    # at most (here a sixth of customers are put off, so people's ratios really do differ), a
    # fraction of what a long record with the same ratio does
    assert blended(harm, "thin") == pytest.approx(GAIN, abs=0.05 * ORGANIC)
    assert blended(harm, "one") == pytest.approx(GAIN, abs=0.12 * ORGANIC)
    long_shift = harm.purchase_shift("dog", ORGANIC, GAIN, GAIN_SD)
    assert harm.purchase_shift("one", ORGANIC, GAIN, GAIN_SD) < 0.25 * long_shift
    # an unfitted model, or a customer with no chance of buying, moves nothing
    assert ContactHarm().purchase_shift("thin", ORGANIC, GAIN, GAIN_SD) == 0.0
    assert harm.purchase_shift("thin", 0.0, GAIN, GAIN_SD) == 0.0


def test_a_clear_record_of_buying_less_after_messages_takes_the_gain_to_zero_or_below() -> None:
    harm = fitted({"dog": record(80.0, 0.3), "dog_thin": record(8.0, 0.3)})
    assert blended(harm, "dog") <= 0.1 * GAIN
    # the same ratio on a tenth of the purchases moves the gain less: the record's weight grows
    # with how well it is measured
    assert blended(harm, "dog") < blended(harm, "dog_thin") < GAIN


def test_a_clear_record_of_buying_more_keeps_the_gain() -> None:
    harm = fitted({"keen": record(80.0, 1.6), "as_said": record(80.0, 1.3)})
    assert blended(harm, "keen") >= GAIN
    # Here a sixth of the customers are put off, and the click gain is the same for everyone, so
    # it is too low for the rest: the gap all records share (`click_gap.offset`) is below 0, and a
    # record that agrees with the click reading reads a little more. It never reads less. (The
    # bidder adds nothing for either: `test_purchases_take_off_a_gain_but_never_add_to_it`.)
    assert harm.click_gap.offset < 0
    assert GAIN <= blended(harm, "as_said") <= 1.5 * GAIN
    # in a shop where nobody is put off, a record that agrees with the click reading keeps its gain
    calm = fitted({"as_said": record(80.0, 1.3)}, share=0.0, seed=7)
    assert blended(calm, "as_said") == pytest.approx(GAIN, abs=0.1 * GAIN)


def test_a_vaguer_click_estimate_gives_the_record_more_weight() -> None:
    harm = fitted({"dog": record(12.0, 0.3)})
    sure = harm.purchase_shift("dog", ORGANIC, GAIN, 0.0)
    vague = harm.purchase_shift("dog", ORGANIC, GAIN, 0.05)
    assert 0 < sure < vague


def test_in_a_shop_without_dogs_the_blend_does_not_cut_gains_across_the_board() -> None:
    evidence, _ = population(share=0.0, seed=7)
    harm = read(evidence)
    gains = np.array([blended(harm, c) for c in evidence])
    # one customer's record moves their own gain both ways; on average the click gain stands
    assert gains.mean() == pytest.approx(GAIN, abs=0.1 * GAIN)
    assert (gains > GAIN).mean() > 0.3 and (gains < GAIN).mean() > 0.3


def test_put_off_customers_lose_much_more_of_their_gain_than_the_rest() -> None:
    harm = fitted(seed=3)
    evidence, harmed = population(seed=3)
    bad = np.mean([blended(harm, c) for c in harmed])
    ok = np.mean([blended(harm, c) for c in evidence if c not in harmed])
    assert bad < 0.6 * GAIN and ok > 0.9 * GAIN


# --- the gap between records and click readings -----------------------------------------------------


def test_a_bias_every_record_shares_moves_no_one() -> None:
    """Records that all read 1.5 times the ratio the click model implies (1.3): the shared part is
    the record's own bias (messages go out when people buy anyway), not anyone's gain."""
    evidence, _ = population(share=0.0, ok_ratio=1.3 * 1.5, seed=7)
    harm = read(evidence)
    assert harm.click_gap.offset == pytest.approx(math.log(1.5), abs=0.1)
    gains = np.array([blended(harm, c) for c in evidence])
    assert gains.mean() == pytest.approx(GAIN, abs=0.1 * GAIN)
    # read as if there were no shared gap, everyone's gain would have gone up
    harm.click_gap = replace(harm.click_gap, offset=0.0, spread=max(harm.click_gap.spread, 0.3))
    assert np.mean([blended(harm, c) for c in evidence]) > 1.2 * GAIN


def test_people_who_differ_from_their_click_reading_widen_the_gap() -> None:
    calm, _ = population(share=0.0, seed=7)
    mixed, _ = population(share=0.15, seed=7)
    assert read(calm).click_gap.spread < read(mixed).click_gap.spread
    assert read(mixed).click_gap.customers > 500


def test_the_gap_is_not_read_from_a_handful_of_customers() -> None:
    evidence, _ = population(n=600, seed=7)
    harm = ContactHarm()
    harm.observe(evidence)
    for cid in sorted(evidence)[: CLICK_GAP_MIN - 1]:
        harm.note_click(cid, ORGANIC, GAIN, GAIN_SD)
    harm.refit()
    assert harm.click_gap.offset == 0.0 and harm.click_gap.spread == 0.0
    assert 0 < harm.click_gap.customers < CLICK_GAP_MIN
    # with no gap read, a record still moves a gain only as far as the click model's own spread
    # lets it: a sure click reading keeps its gain
    dog = sorted(evidence)[0]
    assert abs(harm.purchase_shift(dog, ORGANIC, GAIN, 0.0)) < 0.2 * GAIN


def test_a_large_shop_reads_the_gap_from_a_fixed_sample(monkeypatch: pytest.MonkeyPatch) -> None:
    """Above `CLICK_GAP_SAMPLE` customers the gap is read from that many, picked by a fixed hash of
    their id: the same customers (and so the same gap) on every refit, whatever order they came in."""
    monkeypatch.setattr(bidders, "CLICK_GAP_SAMPLE", 250)
    evidence, _ = population(seed=7)
    first = read(evidence)
    again = read(dict(reversed(list(evidence.items()))))
    assert first.click_gap.customers == 250
    assert again.click_gap == first.click_gap


def test_note_click_ignores_readings_that_say_nothing() -> None:
    harm = ContactHarm()
    harm.note_click("a", 0.0, GAIN, GAIN_SD)
    harm.note_click("b", ORGANIC, math.nan, GAIN_SD)
    harm.note_click("c", ORGANIC, GAIN, GAIN_SD)
    assert set(harm._click) == {"c"}
    harm.forget("c")
    assert not harm._click


# --- the bidder: the purchase drop counted once ------------------------------------------------------


def state(customer_id: str) -> CustomerState:
    return CustomerState.model_validate(
        {
            "customer_id": customer_id,
            "as_of": NOW,
            "purchases": 2,
            "revenue": 200.0,
            "margin": 60.0,
            "last_purchase_at": NOW - timedelta(days=90),
            "channels": {Channel.EMAIL: REACHABLE},
        }
    )


def ctx() -> DecisionContext:
    return DecisionContext(now=NOW, briefs=(sample_brief(NOW),))


def model() -> BetaResponseModel:
    """A message adds 3 points to a 10% chance of buying in its week."""
    return BetaResponseModel(
        BetaResponseModel.priors_from_rates({"treated/*/email|*|*": 0.13, "control/*": 0.10}, strength=1e6)
    )


def plain_uplift(bidder: GoalBidder, cid: str) -> float:
    return next(b.uplift for b in bidder.bids(state(cid), ctx()) if b.action.offer_id == "none")


def bidder(harm: ContactHarm | None, weighted: bool | None) -> GoalBidder:
    return GoalBidder(
        sample_brief(NOW),
        sample_content(NOW),
        model(),
        ValueModel(),
        {Channel.EMAIL: 0.0},
        harm=harm,
        purchase_weighted=weighted,
    )


def test_the_purchase_drop_is_taken_off_once_not_twice() -> None:
    harm = fitted({"dog": record(80.0, 0.3)}, seed=3)
    m = model()
    gain, sd = m.predict(state("dog"), "email|none|education")
    gain = min(max(gain, -1.0), 1.0)
    dent_loss = harm.expected_loss("dog", ORGANIC)
    shift = harm.purchase_shift("dog", ORGANIC, gain, sd)
    assert dent_loss > 0 and shift > 0  # two readings of the same drop
    before = plain_uplift(bidder(None, None), "dog")
    weighted = plain_uplift(bidder(harm, True), "dog")
    assert before - weighted == pytest.approx(max(shift, dent_loss), abs=1e-9)
    assert before - weighted < shift + dent_loss - 1e-6
    # off (a model whose uplift is read from purchases): only the credible dent, as before
    assert before - plain_uplift(bidder(harm, None), "dog") == pytest.approx(dent_loss, abs=1e-9)


def test_purchases_take_off_a_gain_but_never_add_to_it() -> None:
    harm = fitted({"keen": record(80.0, 1.8), "dog": record(80.0, 0.3)}, share=0.0, seed=7)
    assert harm.dent("keen") == 0.0
    assert harm.purchase_shift("keen", ORGANIC, GAIN, GAIN_SD) < 0  # the record reads more buying
    before = plain_uplift(bidder(None, None), "keen")
    # a high record may be when messages went out, not a keener customer: the click gain stands
    assert plain_uplift(bidder(harm, True), "keen") == pytest.approx(before, abs=1e-12)
    assert plain_uplift(bidder(harm, True), "dog") < before
    # a stranger keeps the click model's gain
    assert plain_uplift(bidder(harm, True), "stranger") == pytest.approx(before, abs=1e-12)


def test_purchase_weighting_is_off_by_default_and_can_be_switched_on() -> None:
    # Off by default: the full CI Gym showed no gain beyond seed noise (docs/v2/GYM_CI_RESULTS.md).
    assert not GoalBidder(sample_brief(NOW), sample_content(NOW), ClickResponseModel()).purchase_weighted
    assert GoalBidder(
        sample_brief(NOW), sample_content(NOW), ClickResponseModel(), purchase_weighted=True
    ).purchase_weighted
    assert not GoalBidder(sample_brief(NOW), sample_content(NOW), model()).purchase_weighted
    assert not GoalBidder(
        sample_brief(NOW), sample_content(NOW), ClickResponseModel(), purchase_weighted=False
    ).purchase_weighted


def test_bids_note_the_click_reading_and_quotes_do_not() -> None:
    harm = fitted({"dog": record(80.0, 0.3)}, seed=3)
    harm._click.pop("dog", None)
    b = bidder(harm, True)
    b.quote(state("dog"), ctx())
    assert "dog" not in harm._click
    b.bids(state("dog"), ctx())
    assert harm._click["dog"][0] == pytest.approx(math.log(1.3), abs=1e-6)
