"""The world: replay of real receipts, common random numbers, decoupled clicks, pull-forward, holdout."""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd

from autonomy.contracts import Channel, Event, EventType
from autonomy.gym.waves.config import EFFECT_DAYS, MARGIN, PULL_FORWARD_DAYS, get_world
from autonomy.gym.waves.data import SECONDS_PER_DAY, SHOP_TZ, WavesData
from autonomy.gym.waves.truth import CHANNEL_INDEX, cumulative_net_purchases
from autonomy.gym.waves.world import WavesWorld, receipt_event_id
from tests.v4.waves.helpers import make_world, purchases, send


def expected_receipts(
    tables: tuple[pd.DataFrame, pd.DataFrame, pd.Timestamp], data: WavesData, days: int
) -> set[tuple[str, str, str, float]]:
    """The fixture's own receipts at or after the cutoff, derived straight from the raw table."""
    receipts, clients, _cutoff = tables
    ts = pd.to_datetime(receipts["transaction_datetime"]).dt.tz_localize(SHOP_TZ).dt.tz_convert("UTC")
    start = pd.Timestamp(data.start)
    keep = (
        (ts >= start)
        & (ts < start + pd.Timedelta(days=days))
        & receipts["client_id"].isin(set(clients["client_id"]))
    )
    sel = receipts[keep.to_numpy()]
    stamps = ts[keep.to_numpy()]
    return {
        (str(c), receipt_event_id(str(t)), pd.Timestamp(s).isoformat(), round(float(v), 2))
        for c, t, s, v in zip(
            sel["client_id"], sel["transaction_id"], stamps, sel["purchase_sum"], strict=True
        )
    }


def seen_receipts(events: list[Event]) -> set[tuple[str, str, str, float]]:
    return {
        (e.customer_id, e.event_id, pd.Timestamp(e.ts).isoformat(), float(e.value or 0.0))
        for e in purchases(events)
    }


def real_ids(data: WavesData) -> set[str]:
    return {receipt_event_id(t) for t in (*data.hist_id, *data.post_id)}


def run_days(world: WavesWorld, days: int) -> list[Event]:
    world.reset(0)
    return list(world.advance(world.start + timedelta(days=days)))


def test_the_no_message_world_is_the_real_receipts_exactly(data, tables) -> None:
    days = 28
    world = make_world(data, "heterogeneous", days=days)
    events = run_days(world, days)
    want = expected_receipts(tables, data, days)
    assert want, "the fixture must have receipts after the cutoff"
    assert seen_receipts(events) == want
    # nothing else bought: no incremental purchases, no removed receipts
    assert len(purchases(events)) == len(want)
    assert world.incremental_purchases == 0 and world.real_receipts_removed == 0


def test_a_world_where_messages_do_nothing_still_replays_the_real_receipts_exactly(data, tables) -> None:
    days = 28
    world = make_world(data, "null", days=days)
    world.reset(0)
    events = list(world.advance(world.start + timedelta(hours=10)))
    world.act([send(world, c) for c in data.customer_ids])
    events += list(world.advance(world.start + timedelta(days=days)))
    assert seen_receipts(events) == expected_receipts(tables, data, days)
    assert any(e.type is EventType.SEND for e in events)
    assert len(purchases(events)) == len(seen_receipts(events))


def test_history_holds_only_what_was_known_before_the_cutoff(data) -> None:
    world = make_world(data)
    history = world.reset(0)
    assert history and all(e.ts < data.start for e in history)
    assert [e.ts for e in history] == sorted(e.ts for e in history)
    kinds = {e.type for e in history}
    assert {EventType.SIGNUP, EventType.CONSENT_GRANTED, EventType.PREFERENCE, EventType.PURCHASE} <= kinds
    assert not any(e.type in (EventType.SEND, EventType.CLICK) for e in history)


def test_a_run_is_repeatable_and_two_policies_share_the_same_luck(data) -> None:
    ids = list(data.customer_ids)

    def run(contacted: list[str], second: list[str]) -> list[Event]:
        w = make_world(data, "heterogeneous", days=28)
        w.reset(0)
        w.advance(w.start + timedelta(hours=10))
        w.act([send(w, c) for c in contacted])
        w.advance(w.start + timedelta(days=1, hours=10))
        w.act([send(w, c, day=1) for c in second])
        return list(w.advance(w.start + timedelta(days=28)))

    once = run(ids[:100], [])
    again = run(ids[:100], [])
    assert [e.event_id for e in once] == [e.event_id for e in again]
    assert [(e.ts, e.value) for e in once] == [(e.ts, e.value) for e in again]

    # a second contact on top only adds incremental purchases and clicks for those it reaches: with the
    # same random numbers, everything the first run produced for them is still there when the effect is positive
    twice = run(ids[:100], ids[:100])
    world = make_world(data, "heterogeneous", days=28)
    world.reset(0)
    positive = {data.customer_ids[i] for i in np.flatnonzero(world.truth.rel > 0)}
    real = real_ids(data)
    inc_once = {e.event_id for e in purchases(once) if e.event_id not in real and e.customer_id in positive}
    inc_twice = {e.event_id for e in purchases(twice) if e.event_id not in real and e.customer_id in positive}
    assert inc_once
    assert inc_once <= inc_twice
    # customers nobody contacted live in exactly the same world in every run
    untouched = set(ids[100:])
    assert {e.event_id for e in purchases(once) if e.customer_id in untouched} == {
        e.event_id for e in purchases(twice) if e.customer_id in untouched
    }


def test_clicks_and_opt_outs_do_not_depend_on_the_hidden_effect(data) -> None:
    def run(world_name: str) -> list[Event]:
        w = make_world(data, world_name, days=21)
        w.reset(0)
        w.advance(w.start + timedelta(hours=10))
        w.act([send(w, c, ch) for c in data.customer_ids for ch in (Channel.SMS,)])
        w.act([send(w, c, Channel.EMAIL, day=0, hour=12) for c in data.customer_ids])
        return list(w.advance(w.start + timedelta(days=21)))

    null, hetero = run("null"), run("heterogeneous")

    def of(events: list[Event], kind: EventType) -> set[tuple[str, str]]:
        return {(e.event_id, e.ts.isoformat()) for e in events if e.type is kind}

    assert of(null, EventType.CLICK) == of(hetero, EventType.CLICK) and of(null, EventType.CLICK)
    assert of(null, EventType.UNSUBSCRIBE) == of(hetero, EventType.UNSUBSCRIBE)
    # while the purchases they sit beside do differ
    assert {e.event_id for e in purchases(null)} != {e.event_id for e in purchases(hetero)}


def test_click_chance_is_not_a_function_of_the_effect(data) -> None:
    world = make_world(data, "heterogeneous")
    rates = []
    for seed in range(6):
        world.reset(seed)
        rel = world.truth.rel
        assert rel.std() > 0
        rho = pd.Series(rel).corr(pd.Series(world.p_click[0]), method="spearman")
        rates.append(abs(float(rho)))
    assert max(rates) < 0.4, rates
    # the click chance itself is the same in every world and seed
    other = make_world(data, "sleeping_dogs")
    other.reset(5)
    assert np.array_equal(world.p_click, other.p_click)


def test_clicks_rank_the_value_of_a_contact_only_through_the_base_rate(data) -> None:
    """The effect in roubles grows with the base rate and so does the click chance, so clicks do rank the value of a
    contact somewhat (docs/v4/WAVES.md gives the size). Beyond the base rate they carry nothing about it."""
    world = make_world(data, "heterogeneous")
    base = pd.Series(data.base_rate)
    overall, within = [], []
    everyone = np.arange(data.n, dtype=np.int64)
    for seed in range(5):
        world.reset(seed)
        net = pd.Series(world.truth.effects(everyone, Channel.SMS, 0).net)
        click = pd.Series(world.p_click[CHANNEL_INDEX[Channel.SMS]])
        overall.append(float(click.corr(net, method="spearman")))
        bands = pd.qcut(base.rank(method="first"), 5, labels=False)
        within.append(
            float(np.mean([click[bands == b].corr(net[bands == b], method="spearman") for b in range(5)]))
        )
    assert max(overall) < 0.6, overall
    assert max(abs(x) for x in within) < 0.2, within


def test_an_extra_purchase_looks_like_one_of_the_customers_own_receipts(data) -> None:
    world = make_world(data, "heterogeneous", days=28, mean_lift=3.0)
    world.reset(0)
    world.advance(world.start + timedelta(hours=10))
    world.act([send(world, c) for c in data.customer_ids])
    events = list(world.advance(world.start + timedelta(days=28)))
    real = real_ids(data)
    extra = [e for e in purchases(events) if e.event_id not in real]
    assert extra
    shape = {(e.event_id.split("_")[0], len(e.event_id)) for e in purchases(events)}
    assert len(shape) == 1  # one id format for real and extra purchases
    own: dict[str, set[tuple[float, int]]] = {}
    for i, s, v in zip(data.hist_idx.tolist(), data.hist_s.tolist(), data.hist_value.tolist(), strict=True):
        own.setdefault(data.customer_ids[i], set()).add((round(v, 2), int(s % SECONDS_PER_DAY)))
    every = {x for xs in own.values() for x in xs}
    for e in extra:
        seconds = int((e.ts - world.start).total_seconds() % SECONDS_PER_DAY)
        pair = (float(e.value or 0.0), seconds)
        assert pair in own.get(e.customer_id, every), (e.customer_id, pair)


def test_the_world_realises_exactly_the_loss_the_truth_charges_customer_by_customer(data) -> None:
    """A sleeping dog's lost receipts and the pull-forward payback are taken from the customer's real receipts, and
    the truth charges what that removes in expectation: so a customer with no receipts after the cutoff loses
    nothing and is charged nothing, and a heavy buyer is charged for what they really buy."""
    days = 84
    for world_name in ("sleeping_dogs", "heterogeneous"):
        world = make_world(data, world_name, days=days)
        world.reset(0)
        world.advance(world.start + timedelta(hours=10))
        world.act(
            [send(world, c) for c in data.customer_ids]
        )  # day 0: all of its effect lands within 84 days
        led = world.truth.ledger.merged()
        idx = np.asarray(led["idx"], dtype=np.int64)
        gross = np.bincount(idx, weights=np.asarray(led["gross"], dtype=np.float64), minlength=data.n)
        truth = world.truth
        gain = (
            np.minimum(truth.incremental, 0.95).sum(axis=1) * MARGIN * data.value
        )  # extra purchases, at the customer's mean receipt
        day = np.floor(data.post_s / SECONDS_PER_DAY).astype(np.int64)
        removal = np.minimum(truth.removal[data.post_idx, day], 1.0)
        realised_loss = np.bincount(
            data.post_idx, weights=removal * MARGIN * data.post_value, minlength=data.n
        )
        assert np.allclose(gross, gain - realised_loss, rtol=1e-9, atol=1e-9), world_name
        silent = np.setdiff1d(np.arange(data.n), data.post_idx)
        assert silent.size, "the fixture has customers with no receipt after the cutoff"
        assert np.allclose(realised_loss[silent], 0.0)
        if world_name == "sleeping_dogs":
            dogs = np.flatnonzero(truth.rel < 0)
            buying_dogs = np.intersect1d(dogs, data.post_idx)
            # a dog loses only the receipts inside its window (days 1..14 here)
            assert buying_dogs.size and (gross[buying_dogs] <= 0).all() and (gross[buying_dogs] < 0).any()
            assert np.allclose(gross[np.intersect1d(dogs, silent)], 0.0)
        # and the world's own draws follow it: a run's removed receipts are close to the expected count
        events = list(world.advance(world.start + timedelta(days=days)))
        assert purchases(events)
        expected_removed = float(removal.sum())
        assert abs(world.real_receipts_removed - expected_removed) < 5 * np.sqrt(expected_removed) + 3


def test_pull_forward_makes_the_seven_day_reading_bigger_than_the_forty_four_day_truth() -> None:
    purchases_14d = 0.2
    seven = cumulative_net_purchases(purchases_14d, 0.4, 7)
    window = cumulative_net_purchases(purchases_14d, 0.4, EFFECT_DAYS)
    full = cumulative_net_purchases(purchases_14d, 0.4, EFFECT_DAYS + PULL_FORWARD_DAYS)
    assert seven < window  # the effect is still arriving at day 7
    assert full < window  # and then part of it is paid back
    assert abs(full - 0.6 * purchases_14d) < 1e-12
    assert cumulative_net_purchases(purchases_14d, 0.0, 44) == window
    # a negative effect is not paid back: it is just lost
    assert cumulative_net_purchases(-0.1, 0.4, 44) == cumulative_net_purchases(-0.1, 0.4, EFFECT_DAYS)


def test_pull_forward_takes_receipts_out_of_the_following_thirty_days(data) -> None:
    cid = None
    with_pf = make_world(data, "heterogeneous", days=70, pull_forward=1.0)
    with_pf.reset(0)
    i = int(np.flatnonzero(with_pf.truth.rel > 0)[0])
    cid = data.customer_ids[i]
    with_pf.advance(with_pf.start + timedelta(hours=10))
    with_pf.act([send(with_pf, cid)])
    # the effect lands on days 1..14 and the payback on days 15..44 after the send's day (day 0)
    inc, rem = with_pf.truth.incremental, with_pf.truth.removal
    assert (inc[i, 1 : EFFECT_DAYS + 1] > 0).all()
    assert (rem[i, EFFECT_DAYS + 1 : EFFECT_DAYS + 1 + PULL_FORWARD_DAYS] > 0).all()
    assert rem[i, : EFFECT_DAYS + 1].sum() == 0
    none = make_world(data, "heterogeneous", days=70, pull_forward=0.0)
    none.reset(0)
    none.advance(none.start + timedelta(hours=10))
    none.act([send(none, cid)])
    assert none.truth.removal[i].sum() == 0
    # for a customer who buys at their base rate, the payback is the pulled-forward share of what went in
    put_in = float(inc[i].sum())
    taken = float(rem[i].sum()) * float(data.base_rate[i])
    assert abs(taken - put_in) < 1e-4 * max(put_in, 1.0)


def test_a_negative_lift_removes_real_receipts_inside_the_window_not_after(data) -> None:
    w = make_world(data, "sleeping_dogs", days=40)
    w.reset(0)
    i = int(np.flatnonzero(w.truth.rel < 0)[0])
    w.advance(w.start + timedelta(hours=10))
    w.act([send(w, data.customer_ids[i])])
    assert (w.truth.removal[i, 1 : EFFECT_DAYS + 1] > 0).all()
    assert w.truth.removal[i, EFFECT_DAYS + 1 :].sum() == 0
    assert w.truth.incremental[i].sum() == 0


def test_the_unsubscribe_blocks_later_sends_on_that_channel_only(data) -> None:
    w = make_world(data, "null", days=14)
    w.reset(0)
    w._unsub_s[:, 0] = 0.0  # everyone has already left SMS
    w.advance(w.start + timedelta(hours=10))
    w.act([send(w, c) for c in data.customer_ids[:20]])
    w.act([send(w, c, Channel.EMAIL, hour=12) for c in data.customer_ids[:20]])
    events = list(w.advance(w.start + timedelta(days=2)))
    assert sum(e.type is EventType.FAIL and e.channel is Channel.SMS for e in events) == 20
    assert any(e.type is EventType.DELIVER and e.channel is Channel.EMAIL for e in events)
    assert not any(e.type is EventType.DELIVER and e.channel is Channel.SMS for e in events)
    assert get_world("null").name == "null"
