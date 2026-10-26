"""The slow models: censoring, point in time, the held-out gate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from autonomy.contracts import Event, EventType
from autonomy.decide.slow_models import (
    CHURN_RISK,
    CLV_90D,
    SlowModelConfig,
    SlowModels,
    TrainingSet,
    _split,
    auc,
    build_training_set,
    month_ends,
)
from autonomy.profile import profile_for
from tests.v3.who.synth import END, purchase, shop

D = datetime


def at(month: int, day: int, year: int = 2026) -> datetime:
    return D(year, month, day, 12, tzinfo=UTC)


def by_customer(events: list[Event], until: datetime) -> dict[str, list[Event]]:
    out: dict[str, list[Event]] = {}
    for e in events:
        if e.ts <= until:
            out.setdefault(e.customer_id, []).append(e)
    return out


def test_month_ends_are_the_last_instant_of_each_month():
    got = month_ends(at(1, 10), at(4, 15))
    assert [c.date().isoformat() for c in got] == ["2026-01-31", "2026-02-28", "2026-03-31"]
    assert got[0] == D(2026, 2, 1, tzinfo=UTC) - timedelta(microseconds=1)
    assert month_ends(at(5, 1), at(5, 20)) == []


def test_auc_ranks_and_ties():
    assert auc(np.array([0.1, 0.2, 0.8, 0.9]), np.array([0.0, 0.0, 1.0, 1.0])) == 1.0
    assert auc(np.array([0.9, 0.8, 0.2, 0.1]), np.array([0.0, 0.0, 1.0, 1.0])) == 0.0
    assert auc(np.array([0.5, 0.5, 0.5, 0.5]), np.array([0.0, 1.0, 0.0, 1.0])) == 0.5
    assert auc(np.array([0.5, 0.6]), np.array([1.0, 1.0])) == 0.5


# --- censoring -------------------------------------------------------------------------------------


def test_a_window_that_is_not_over_gives_no_label_not_a_stopped_customer():
    # One order in January, nothing after. On 25 March the January month-end's 60-day window (to 1
    # April) is still open: the customer is not "stopped", there is simply no row yet.
    events = [purchase("a", at(1, 10))]
    assert build_training_set(events, at(3, 25)).rows == 0
    # On 2 April the window is over and they did stop. The 90-day value window is still open.
    later = build_training_set(events, at(4, 2))
    assert later.rows == 1
    assert later.stopped[0] == 1.0
    assert np.isnan(later.value[0])
    # By 2 May the 90-day window is over too, and it holds nothing.
    full = build_training_set(events, at(5, 2))
    assert full.value[0] == 0.0


def test_the_end_of_the_data_is_as_of_not_the_customers_last_event():
    # b ordered in January and again on 20 February; c ordered in January only. With as_of 2 April
    # the January window is closed for both: b did not stop, c did.
    events = [purchase("b", at(1, 10)), purchase("b", at(2, 20)), purchase("c", at(1, 12))]
    data = build_training_set(events, at(4, 2))
    by = {cid: data.stopped[i] for i, cid in enumerate(data.customers) if data.cut_of[i] == 0}
    assert by == {"b": 0.0, "c": 1.0}


def test_censored_rows_are_counted_and_left_out_of_the_fit():
    data = build_training_set(shop(200), END)
    assert data.censored_risk > 0 and data.censored_value > data.censored_risk
    assert data.rows > 0
    # no row belongs to a month-end whose risk window and value window are both still open
    last_closed = max(c for c in month_ends(D(2025, 6, 1, tzinfo=UTC), END) if c + timedelta(days=60) <= END)
    assert max(data.cuts[i] for i in set(data.cut_of.tolist())) == last_closed
    # a row's label is missing, not zero, where its window is open
    open_value = [i for i in range(data.rows) if data.cuts[data.cut_of[i]] + timedelta(days=90) > END]
    assert open_value and all(np.isnan(data.value[i]) for i in open_value)


# --- point in time ---------------------------------------------------------------------------------


def test_features_at_a_month_end_read_nothing_after_it():
    events = shop(150, seed=3)
    data = build_training_set(events, END)
    k = 4
    cut = data.cuts[k]
    once = by_customer(events, cut)
    rows = [i for i in range(data.rows) if data.cut_of[i] == k]
    assert rows
    for i in rows[:25]:
        expected = profile_for(once[data.customers[i]], cut)
        np.testing.assert_allclose(data.x[i], [expected[n] for n in data.names])


def test_nothing_after_as_of_is_read():
    events = shop(120, seed=5)
    cut_off = D(2026, 5, 15, tzinfo=UTC)
    known = [e for e in events if e.ts <= cut_off]
    future = [purchase(e.customer_id, cut_off + timedelta(days=3), 9_999.0, "x") for e in events[:50]]
    a = build_training_set(known, cut_off)
    b = build_training_set(known + future, cut_off)
    assert a.customers == b.customers
    np.testing.assert_array_equal(a.x, b.x)
    np.testing.assert_array_equal(a.stopped, b.stopped)
    np.testing.assert_array_equal(a.value, b.value)
    m1, m2 = SlowModels(), SlowModels()
    m1.fit(known, cut_off)
    m2.fit(known + future, cut_off)
    assert m1.risk_check == m2.risk_check and m1.value_check == m2.value_check


def test_a_label_comes_only_from_the_window_after_the_month_end():
    # d bought every week up to the end of January and never again: before the cut they are as busy
    # as can be, after it they stopped. Their label must be "stopped", from the window alone.
    events = [purchase("d", D(2026, 1, 1, 9, tzinfo=UTC) + timedelta(days=7 * i)) for i in range(5)]
    data = build_training_set(events, at(6, 1))
    i = next(i for i in range(data.rows) if data.cut_of[i] == 0)
    assert data.stopped[i] == 1.0 and data.value[i] == 0.0
    # an order in the window flips only that label
    events.append(purchase("d", at(2, 10)))
    again = build_training_set(events, at(6, 1))
    j = next(i for i in range(again.rows) if again.cut_of[i] == 0)
    assert again.stopped[j] == 0.0 and again.value[j] == pytest.approx(50.0 * 0.4)


# --- the held-out gate -----------------------------------------------------------------------------


def test_a_model_that_passes_its_check_is_used_and_scores_sensibly():
    events = shop(500)
    models = SlowModels()
    models.fit(events, END)
    assert models.risk_usable and models.value_usable
    assert models.risk_check.auc is not None and models.risk_check.auc >= 0.6
    assert models.value_check.skill is not None and models.value_check.skill >= 0.05
    mine = by_customer(events, END)["c00007"]
    scores = models.scores(profile_for(mine, END))
    assert 0.0 <= scores[CHURN_RISK] <= 1.0 and scores[CLV_90D] >= 0.0


def test_the_held_out_month_is_later_than_every_training_label():
    events = shop(300)
    models = SlowModels()
    models.fit(events, END)
    cfg = models.config
    data = build_training_set(events, END)
    labelled = ~np.isnan(data.stopped)
    train, held, cut = _split(data, labelled, cfg.risk_window_days)
    assert cut == models.risk_check.holdout_cut
    for i in np.flatnonzero(train):
        assert data.cuts[data.cut_of[i]] + timedelta(days=cfg.risk_window_days) <= cut
    assert not (train & held).any()
    assert {data.cuts[data.cut_of[i]] for i in np.flatnonzero(held)} == {cut}


def test_a_bar_the_model_cannot_pass_closes_the_gate():
    events = shop(500)
    models = SlowModels(SlowModelConfig(min_auc=0.99, min_value_skill=0.95))
    models.fit(events, END)
    assert not models.risk_usable and not models.value_usable
    assert "below the bar" in models.risk_check.reason and models.risk_check.auc is not None
    assert "below the bar" in models.value_check.reason
    mine = by_customer(events, END)["c00007"]
    assert models.scores(profile_for(mine, END)) == {}
    assert "not used" in models.describe()


def test_too_little_history_or_too_few_rows_is_not_trusted():
    short = SlowModels()
    short.fit(shop(500), D(2025, 11, 10, tzinfo=UTC))
    assert not short.risk_usable and not short.value_usable
    small = SlowModels()
    small.fit(shop(25), END)
    assert not small.risk_usable and "too few" in small.risk_check.reason
    empty = SlowModels()
    empty.fit([], END)
    assert not empty.risk_usable and not empty.value_usable


def test_nobody_with_no_order_gets_a_score_and_a_lapsed_customer_gets_no_risk():
    models = SlowModels()
    models.fit(shop(500), END)
    assert models.scores({}) == {}
    assert models.scores({"buy_ordered": 0.0}) == {}
    lapsed = models.scores({"buy_ordered": 1.0, "buy_days_since_last_order": 400.0})
    assert CHURN_RISK not in lapsed and CLV_90D in lapsed


def test_leavers_score_higher_than_stayers():
    events = shop(500, seed=2)
    as_of = D(2026, 5, 15, tzinfo=UTC)
    models = SlowModels()
    models.fit(events, as_of)
    assert models.risk_usable
    known = by_customer(events, as_of)
    risk = {}
    for cid, evs in known.items():
        s = models.scores(profile_for(evs, as_of))
        if CHURN_RISK in s:
            risk[cid] = s[CHURN_RISK]
    bought_again = {
        e.customer_id for e in events if as_of < e.ts <= as_of + timedelta(days=60) and e.customer_id in risk
    }
    stopped = set(risk) - bought_again
    assert len(stopped) > 20 and len(bought_again) > 20
    assert np.mean([risk[c] for c in stopped]) > np.mean([risk[c] for c in bought_again]) + 0.05


# --- labels read purchases as the profile does ------------------------------------------------------


def unpriced(cid: str, ts: datetime, value: float, event_id: str, margin: float | None = None) -> Event:
    """A purchase that reports no margin unless one is given."""
    return Event(
        event_id=event_id,
        customer_id=cid,
        type=EventType.PURCHASE,
        ts=ts,
        source="test",
        value=value,
        margin=margin,
    )


def first_cut_value(data: TrainingSet, cid: str) -> float:
    return float(
        data.value[next(i for i in range(data.rows) if data.customers[i] == cid and data.cut_of[i] == 0)]
    )


def test_a_free_order_is_an_order_not_a_stopped_customer():
    # 'a' ordered on 10 January and again on 10 February, both fully discounted (value 0)
    events = [purchase("a", at(1, 10), 0.0), purchase("a", at(2, 10), 0.0)]
    data = build_training_set(events, at(6, 1))
    i = next(i for i in range(data.rows) if data.cut_of[i] == 0)
    assert data.stopped[i] == 0.0


def test_a_negative_purchase_takes_money_off_the_value():
    # margin 40 on the 100 order; the -40 order reports no margin and is read at the shop's rate (0.4)
    events = [
        purchase("b", at(1, 10)),
        purchase("b", at(2, 10), 100.0),
        unpriced("b", at(2, 15), -40.0, "b-neg"),
    ]
    data = build_training_set(events, at(6, 1))
    assert first_cut_value(data, "b") == pytest.approx(40.0 - 40.0 * 0.4)


def test_the_margin_rate_of_a_label_is_the_one_known_at_the_end_of_its_window():
    # c's February order reports no margin and is read at the shop's rate (0.4 so far). A margin
    # reported after the January window closed (20 May) must not change that window's label.
    events = [
        purchase("c", at(1, 10)),
        unpriced("c", at(2, 10), 100.0, "c-plain"),
        purchase("o", at(1, 5), 100.0),
    ]
    before = build_training_set(events, at(6, 1))
    after = build_training_set([*events, unpriced("o", at(5, 20), 100.0, "late", margin=90.0)], at(6, 1))
    assert first_cut_value(before, "c") == pytest.approx(100.0 * 0.4)
    assert first_cut_value(after, "c") == pytest.approx(100.0 * 0.4)


def test_nobody_is_valued_at_exactly_nothing():
    events = shop(500)
    models = SlowModels()
    models.fit(events, END)
    assert models.value_usable
    known = by_customer(events, END)
    values = [models.scores(profile_for(evs, END))[CLV_90D] for evs in known.values()]
    assert min(values) > 0.0
    lapsed = models.scores({"buy_ordered": 1.0, "buy_days_since_last_order": 400.0})
    assert 0.0 < lapsed[CLV_90D] < float(np.median(values))
