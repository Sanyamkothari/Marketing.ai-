"""Refill cycles: the catalogue's inference, the shrinkage, the hazard, the scores on state, real data."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from itertools import pairwise

import numpy as np
import pandas as pd
import pytest

from autonomy.catalog.cycle import (
    CycleConfig,
    CycleModel,
    backtest_last_interval,
    calibrate_at_cutoff,
)
from autonomy.catalog.enrich import (
    REFILL_DUE_DAYS,
    REFILL_KEYS,
    REFILL_P14,
    REFILL_WINDOW_OPEN,
    enrich_all,
    enrich_state,
)
from autonomy.catalog.products import Catalogue, Product, purchases_frame, to_days, trips
from autonomy.contracts import CustomerState, Event, EventType, stable_id
from autonomy.data.loaders import load_online_retail_sample

T0 = datetime(2026, 1, 1, 12, tzinfo=UTC)
AS_OF = datetime(2026, 12, 31, tzinfo=UTC)


def _rows(customer: str, item: str, gaps: list[float], category: str | None = "skincare", start: float = 0.0):
    day = to_days(T0) + start
    out = [(customer, item, category, day, 20.0, 1.0)]
    for g in gaps:
        day += g
        out.append((customer, item, category, day, 20.0, 1.0))
    return out


def _frame(rows: list[tuple[str, str, str | None, float, float, float]]) -> pd.DataFrame:
    cols = ["customer_id", "item_id", "category", "day", "value", "quantity"]
    return pd.DataFrame(rows, columns=cols).sort_values(["customer_id", "day"], kind="mergesort")


def _population(seed: int = 7, n: int = 60, median: float = 30.0, item: str = "serum") -> list[tuple]:
    """`n` customers buying `item` four times, gaps log-normal around `median` days."""
    rng = np.random.default_rng(seed)
    rows: list[tuple] = []
    for c in range(n):
        own = median * math.exp(rng.normal(0.0, 0.2))
        gaps = [max(1.0, round(own * math.exp(rng.normal(0.0, 0.2)))) for _ in range(3)]
        rows += _rows(f"p{c:03d}", item, gaps, start=float(rng.integers(0, 60)))
    return rows


def _purchase(customer: str, item: str | None, ts: datetime, category: str | None = "skincare") -> Event:
    return Event(
        event_id=stable_id("evt", customer, item, ts.isoformat()),
        customer_id=customer,
        type=EventType.PURCHASE,
        ts=ts,
        source="test",
        value=25.0,
        item_id=item,
        category=category,
    )


# ------------------------------------------------------------------------------------- catalogue
def test_catalogue_infers_replenishable_items_and_their_cycle() -> None:
    rows = _population(n=20)
    rows += [(f"o{c}", "mirror", "home", to_days(T0) + c, 80.0, 1.0) for c in range(20)]  # bought once each
    cat = Catalogue.infer(_frame(rows))
    serum, mirror = cat.get("serum"), cat.get("mirror")
    assert serum is not None and mirror is not None
    assert serum.replenishable and not mirror.replenishable
    assert serum.typical_cycle_days is not None and 20 <= serum.typical_cycle_days <= 45
    assert serum.category == "skincare" and serum.price == pytest.approx(20.0)
    assert cat.replenishable_items() == ("serum",)
    assert cat.given_cycle_items == frozenset()  # inferred cycles are not treated as merchant knowledge


def test_catalogue_keeps_what_the_merchant_said() -> None:
    table = pd.DataFrame(
        {
            "item_id": ["serum", "mirror"],
            "name": ["Night serum", "Mirror"],
            "replenishable": [False, np.nan],
            "typical_cycle_days": [45.0, np.nan],
        }
    )
    cat = Catalogue.from_frame(table, purchases=_frame(_population(n=20)))
    serum = cat.get("serum")
    assert serum is not None and serum.name == "Night serum"
    assert serum.replenishable is False  # stated, so not overridden by the data
    assert serum.typical_cycle_days == 45.0 and cat.given_cycle_items == frozenset({"serum"})
    mirror = cat.get("mirror")
    assert mirror is not None and mirror.replenishable is False and mirror.typical_cycle_days is None
    with pytest.raises(ValueError, match="twice"):
        Catalogue([Product(item_id="a"), Product(item_id="a")])


# ------------------------------------------------------------------------------------- shrinkage
def test_shrinkage_moves_from_the_item_toward_the_customer_as_their_own_gaps_add_up() -> None:
    # Pace off: in a one-item shop the order cadence *is* the item cadence, and would explain it all.
    model = CycleModel.fit(_frame(_population()), AS_OF, config=CycleConfig(max_pace_slope=0.0))
    item_median = model.prior("serum").median_days
    assert 24 <= item_median <= 37

    now = T0 + timedelta(days=200)
    times = [now - timedelta(days=10 * k + 1) for k in range(8)][::-1]  # a fast customer: every 10 days
    estimates = [model.estimate("serum", times[-n:], now) for n in (1, 2, 3, 8)]
    assert all(e is not None for e in estimates)
    medians = [e.median_interval_days for e in estimates if e is not None]
    weights = [e.own_weight for e in estimates if e is not None]
    assert medians[0] == pytest.approx(item_median, rel=1e-9)  # no own gap: the item's cycle
    assert weights[0] == 0.0
    assert all(a > b for a, b in pairwise(medians))  # pulled toward 10 days
    assert all(a < b for a, b in pairwise(weights))
    assert 10 < medians[-1] < 14 and weights[-1] > 0.8


def test_a_thin_item_borrows_its_categorys_cycle_and_a_stated_cycle_anchors_an_unseen_item() -> None:
    rows = _population(item="serum", median=30.0) + _rows("x1", "toner", [90.0])
    cat = Catalogue(
        [
            Product(item_id="serum", category="skincare", replenishable=True),
            Product(item_id="toner", category="skincare", replenishable=True),
            Product(item_id="balm", category="skincare", replenishable=True, typical_cycle_days=60.0),
            Product(item_id="cream", category="skincare", replenishable=True),
        ]
    )
    model = CycleModel.fit(_frame(rows), AS_OF, catalogue=cat)
    toner = model.prior("toner").median_days
    assert 30 < toner < 90 and toner < 60  # one customer at 90 days barely moves it from ~30
    assert model.prior("balm").median_days == pytest.approx(60.0)
    assert model.prior("cream").median_days == pytest.approx(model.groups["skincare"].median_days)
    assert model.prior("never-seen").median_days == pytest.approx(model.shop.median_days)


def test_customer_pace_shortens_the_cycle_for_frequent_shoppers() -> None:
    rows: list[tuple] = []
    for c in range(30):  # fast shoppers: orders every 7 days, the serum every ~20
        rows += _rows(f"f{c}", "serum", [20.0, 20.0, 20.0])
        rows += _rows(f"f{c}", "other", [7.0] * 8, category="misc", start=3.0)
    for c in range(30):  # slow shoppers: the serum every ~60 days, nothing else
        rows += _rows(f"s{c}", "serum", [60.0, 60.0, 60.0])
    model = CycleModel.fit(_frame(rows), AS_OF)
    assert model.pace_slope > 0
    now = T0 + timedelta(days=300)
    fast = model.pace_of([now - timedelta(days=7 * k + 1) for k in range(10)], now)
    slow = model.pace_of([now - timedelta(days=200), now - timedelta(days=100)], now)
    assert fast < 0 < slow
    one = [now - timedelta(days=5)]
    est_fast, est_slow = (model.estimate("serum", one, now, pace=p) for p in (fast, slow))
    assert est_fast is not None and est_slow is not None
    assert est_fast.median_interval_days < est_slow.median_interval_days


# ------------------------------------------------------------------------------------- timing
def test_days_until_due_is_negative_once_overdue() -> None:
    model = CycleModel.fit(_frame(_population()), AS_OF)
    last = T0 + timedelta(days=100)
    median = model.prior("serum").median_days
    early = model.estimate("serum", [last], last + timedelta(days=5))
    late = model.estimate("serum", [last], last + timedelta(days=median + 20))
    assert early is not None and late is not None
    assert early.days_until_due == pytest.approx(median - 5, abs=1e-6) and early.days_until_due > 0
    assert late.days_until_due < 0 and late.next_due_at < late.now
    assert early.next_due_at == late.next_due_at  # the due date does not move with now
    assert early.window_start_at < early.next_due_at < early.window_end_at


def test_hazard_rises_toward_the_due_date_then_fades_when_long_overdue() -> None:
    model = CycleModel.fit(_frame(_population()), AS_OF)
    last = T0 + timedelta(days=100)
    median = model.prior("serum").median_days
    times = [last - timedelta(days=60), last - timedelta(days=30), last]

    def at(days: float):
        est = model.estimate("serum", times, last + timedelta(days=days))
        assert est is not None
        return est

    rising = [at(d).p14 for d in (0.5, 5, 10, median - 14)]
    assert all(a < b for a, b in pairwise(rising))
    still = [at(d).still_likely for d in (1, median, 2 * median, 6 * median)]
    assert all(a > b for a, b in pairwise(still))
    assert at(8 * median).p14 < 0.25 * at(median - 7).p14  # long overdue: mostly lapsed
    assert at(median).window_open and not at(1).window_open and not at(6 * median).window_open
    probe = at(median - 10)
    horizons = [probe.p_within(h) for h in (1, 7, 14, 30, 90, 365)]
    assert all(0 <= a <= b <= 1 for a, b in pairwise(horizons))
    assert probe.p_within(14) == pytest.approx(probe.p14)


def test_purchases_after_now_or_after_as_of_are_never_used() -> None:
    rows = _population()
    model = CycleModel.fit(_frame(rows), AS_OF)
    future = _frame(rows + _rows("p000", "serum", [2.0] * 20, start=500.0))  # all after AS_OF
    assert CycleModel.fit(future, AS_OF).keys["serum"] == model.keys["serum"]
    now = T0 + timedelta(days=120)
    times = [now - timedelta(days=40), now - timedelta(days=10)]
    with_future = [*times, now + timedelta(days=1), now + timedelta(days=3)]
    assert model.estimate("serum", times, now) == model.estimate("serum", with_future, now)
    assert model.estimate("serum", [now + timedelta(days=1)], now) is None


def test_fitting_and_scoring_are_deterministic_and_the_vector_path_matches() -> None:
    frame = _frame(_population(seed=3) + _population(seed=4, item="cream", median=50.0))
    a = CycleModel.fit(frame, AS_OF)
    b = CycleModel.fit(frame.sample(frac=1.0, random_state=1), AS_OF)  # row order does not matter
    assert a.keys == b.keys and a.groups == b.groups and a.shop == b.shop
    assert a.repeat_odds_ratios == b.repeat_odds_ratios and a.background_rates == b.background_rates

    now = T0 + timedelta(days=150)
    table = trips(frame, "item_id")
    pace = a.customer_paces(frame, now)
    vector = a.score_trips(table, now, pace=pace).set_index(["customer_id", "key"])
    for (customer, key), row in vector.head(12).iterrows():
        rows = frame[(frame["customer_id"] == customer) & (frame["item_id"] == key)]
        est = a.estimate(
            str(key),
            [datetime.fromtimestamp(d * 86400.0, UTC) for d in rows["day"]],
            now,
            pace=float(pace[customer]),
        )
        assert est is not None
        assert row["p"] == pytest.approx(est.p14, rel=1e-9)
        assert row["median"] == pytest.approx(est.median_interval_days, rel=1e-9)
        assert bool(row["window_open"]) == est.window_open


def test_config_rejects_a_backwards_window() -> None:
    with pytest.raises(ValueError, match="quantiles"):
        CycleConfig(window_open_quantile=0.7, window_close_quantile=0.3)


# ------------------------------------------------------------------------------------- enrich
def test_enrich_state_adds_refill_scores_and_names_the_item() -> None:
    model = CycleModel.fit(_frame(_population()), AS_OF)
    cat = Catalogue(
        [
            Product(item_id="serum", category="skincare", replenishable=True),
            Product(item_id="mirror", category="home", replenishable=False),
        ]
    )
    now = T0 + timedelta(days=200)
    events = [
        _purchase("c1", "serum", now - timedelta(days=60)),
        _purchase("c1", "serum", now - timedelta(days=29)),
        _purchase("c1", "mirror", now - timedelta(days=3), category="home"),
        _purchase("c2", "serum", now - timedelta(days=1)),  # someone else's: ignored
        _purchase("c1", "serum", now + timedelta(days=2)),  # the future: ignored
    ]
    state = CustomerState(customer_id="c1", as_of=now, scores={"churn_risk": 0.2, REFILL_P14: 0.99})
    enriched, pick = enrich_state(state, events, cat, now, model=model)
    assert pick is not None and pick.item_id == "serum" and pick.category == "skincare"
    assert set(REFILL_KEYS) <= set(enriched.scores) and enriched.scores["churn_risk"] == 0.2
    assert enriched.scores[REFILL_P14] == pytest.approx(pick.estimate.p14)
    assert enriched.scores[REFILL_DUE_DAYS] == pytest.approx(pick.estimate.days_until_due)
    assert enriched.scores[REFILL_WINDOW_OPEN] in (0.0, 1.0)
    assert pick.estimate.last_purchase_at == now - timedelta(days=29)
    assert state.scores[REFILL_P14] == 0.99  # the input is untouched

    nothing, none = enrich_state(state, events[2:3], cat, now, model=model)  # only a mirror
    assert none is None and not set(REFILL_KEYS) & set(nothing.scores)
    assert nothing.scores == {"churn_risk": 0.2}


def test_enrich_all_maps_customers_to_items_and_works_without_a_fitted_model() -> None:
    cat = Catalogue(
        [Product(item_id="serum", category="skincare", replenishable=True, typical_cycle_days=30.0)]
    )
    now = T0 + timedelta(days=100)
    states = [CustomerState(customer_id=c, as_of=now) for c in ("a", "b", "c")]
    events = {
        "a": [_purchase("a", "serum", now - timedelta(days=28))],
        "b": [_purchase("b", "serum", now - timedelta(days=2))],
    }
    out, items = enrich_all(states, events, cat, now)
    assert [s.customer_id for s in out] == ["a", "b", "c"]
    assert items == {"a": "serum", "b": "serum"}
    assert out[0].scores[REFILL_P14] > out[1].scores[REFILL_P14]  # a is near the 30-day refill
    assert out[0].scores[REFILL_DUE_DAYS] == pytest.approx(2.0, abs=1e-6)
    assert out[2].scores == {}
    again, _ = enrich_all(states, events, cat, now)
    assert again == out


# ------------------------------------------------------------------------------------- real data
@pytest.fixture(scope="module")
def retail_sample() -> pd.DataFrame:
    return purchases_frame(load_online_retail_sample().events)


def test_on_the_online_retail_sample_the_model_beats_the_population_average(
    retail_sample: pd.DataFrame,
) -> None:
    """Order cadence (the sample has no item codes). Its in-between order dates are evenly spaced by
    the loader, so only customers with two orders have a fully real gap; those are checked alone."""
    report = backtest_last_interval(retail_sample, level="order")
    assert report.n_pairs > 400
    assert report.mae_model < report.mae_naive
    assert report.median_ae_model < report.median_ae_naive
    assert 0.5 < report.coverage_80 < 0.95
    first = next(s for s in report.slices if s.own_intervals == "0")
    assert first.n > 200 and first.mae_model < first.mae_naive * 1.05
    assert backtest_last_interval(retail_sample, level="order") == report


def test_on_the_online_retail_sample_p14_ranks_who_buys_next(retail_sample: pd.DataFrame) -> None:
    cal = calibrate_at_cutoff(retail_sample, datetime(2011, 3, 1, tzinfo=UTC), level="order")
    assert cal.n_pairs > 300 and 0 < cal.base_rate < 0.5
    assert cal.model.auc > 0.6
    assert cal.model.brier <= cal.naive.brier
    assert all(0 <= b.mean_p <= 1 for b in cal.bins)


def test_an_empty_history_and_the_category_level_still_work() -> None:
    empty = CycleModel.fit(_frame([]), AS_OF)
    assert empty.prior("anything").median_days == pytest.approx(CycleConfig().default_cycle_days)
    est = empty.estimate("anything", [T0], T0 + timedelta(days=3))
    assert est is not None and 0 < est.p14 < 1 and est.prior_source == "default"

    rows = _population() + _population(seed=9, item="cream", median=30.0)
    by_category = CycleModel.fit(_frame(rows), AS_OF, level="category")
    assert set(by_category.keys) == {"skincare"}
    found = by_category.estimate_customer(_frame(rows)[lambda f: f["customer_id"] == "p001"], AS_OF)
    assert set(found) == {"skincare"} and found["skincare"].level == "category"


def test_the_raw_log_cleaning_keeps_only_real_product_sales() -> None:
    from autonomy.catalog.cycle import retail_log_purchases, validation_markdown

    log = pd.DataFrame(
        {
            "InvoiceNo": ["1", "1", "C2", "3", "4", "5"],
            "StockCode": ["85123A", "POST", "85123A", "22423", "22423", "22423"],
            "Quantity": [6, 1, -6, 2, 0, 1],
            "InvoiceDate": ["12/1/2010 8:26"] * 4 + ["12/2/2010 9:00", "12/3/2010 9:00"],
            "UnitPrice": [2.55, 18.0, 2.55, 12.75, 12.75, 0.0],
            "CustomerID": ["17850.0", "17850", "17850", None, "13047", "13047"],
            "Country": ["United Kingdom"] * 6,
        }
    )
    clean = retail_log_purchases(log)
    assert clean[["customer_id", "item_id"]].values.tolist() == [["17850", "85123A"]]
    assert clean["day"].iloc[0] == pytest.approx(to_days(datetime(2010, 12, 1, 8, 26, tzinfo=UTC)))
    text = validation_markdown(None, _frame(_population(n=10)))
    assert "repeat customers" in text and "Raw log" not in text


def test_a_backtest_with_no_repeat_purchases_reports_nothing_rather_than_failing() -> None:
    report = backtest_last_interval(_frame(_rows("a", "serum", [])), level="item")
    assert report.n_pairs == 0 and report.slices == () and math.isnan(report.mae_model)


def test_the_most_pressing_of_several_items_is_picked_and_open_windows_are_counted() -> None:
    model = CycleModel.fit(_frame(_population() + _population(seed=5, item="cream", median=30.0)), AS_OF)
    cat = Catalogue(
        [
            Product(item_id="serum", category="skincare", replenishable=True),
            Product(item_id="cream", category="skincare", replenishable=True),
        ]
    )
    now = T0 + timedelta(days=200)
    due = now - timedelta(days=model.prior("cream").median_days - 3, hours=5, microseconds=7)
    events = [
        _purchase("c1", "serum", now - timedelta(days=1)),  # just bought: not pressing
        _purchase("c1", "cream", due),  # three days from its typical refill
    ]
    state = CustomerState(customer_id="c1", as_of=now)
    enriched, pick = enrich_state(state, events, cat, now, model=model)
    assert pick is not None and pick.item_id == "cream"
    assert [c.key for c in pick.candidates] == ["cream", "serum"]
    assert pick.candidates[0].p14 > pick.candidates[1].p14
    assert pick.estimate.last_purchase_at == due  # exact to the microsecond
    open_now = sum(1 for c in pick.candidates if c.window_open)
    assert pick.estimate.window_open and enriched.scores["refill_items_due"] == float(open_now)


def test_catalogue_records_read_text_flags_and_blank_values_as_not_given() -> None:
    records = [
        {"item_id": "serum", "replenishable": "yes", "typical_cycle_days": "", "price": None},
        {"item_id": " mirror ", "replenishable": "no", "category": "  "},
    ]
    cat = Catalogue.from_records(records, purchases=_frame(_population(n=20)))
    serum, mirror = cat.get("serum"), cat.get("mirror")
    assert serum is not None and serum.replenishable and serum.price == pytest.approx(20.0)
    assert serum.typical_cycle_days is not None and cat.given_cycle_items == frozenset()
    assert mirror is not None and not mirror.replenishable and mirror.category is None
