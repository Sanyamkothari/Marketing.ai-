"""Co-purchase recommendations: counting, exclusion, reasons, determinism, and beating popularity."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from autonomy.catalog.evaluate import (
    POPULARITY,
    evaluate,
    retail_purchases,
    synthetic_planted_pairs,
    write_section,
)
from autonomy.catalog.recommend import (
    BASKET_COLUMNS,
    CoPurchaseConfig,
    CoPurchaseModel,
    RefillWindowInfo,
    basket_frame,
    next_best_item,
    recommend,
)
from autonomy.contracts import Event, EventType, stable_id

T0 = datetime(2026, 1, 5, 12, tzinfo=UTC)
DAY = 86400
LOOSE = CoPurchaseConfig(min_pair_count=1)


def _rows(baskets: list[tuple[str, int, list[str]]], category: dict[str, str] | None = None) -> pd.DataFrame:
    """(customer, day, items) baskets as a basket table."""
    t0 = int(T0.timestamp())
    rows = [
        (cid, item, t0 + day * DAY, (category or {}).get(item), f"{cid}-{day}")
        for cid, day, items in baskets
        for item in items
    ]
    return pd.DataFrame(rows, columns=list(BASKET_COLUMNS))


def _event(cid: str, item: str, day: int, category: str | None = None) -> Event:
    return Event(
        event_id=stable_id("evt", cid, item, day),
        customer_id=cid,
        type=EventType.PURCHASE,
        ts=T0 + timedelta(days=day),
        source="test",
        value=10.0,
        item_id=item,
        category=category,
    )


TINY = _rows(
    [
        ("c1", 0, ["A", "B"]),
        ("c2", 0, ["A", "B"]),
        ("c3", 0, ["A", "C"]),
        ("c4", 0, ["B", "C"]),
        ("c5", 0, ["D"]),
    ]
)
NOW = T0 + timedelta(days=1)


def test_co_purchase_counts_and_cosine_on_a_tiny_basket_set() -> None:
    model = CoPurchaseModel.fit(TINY, as_of=NOW, config=LOOSE)
    assert model.n_trips == 5
    assert dict(zip(model.items, model.item_trips.tolist(), strict=True)) == {"A": 3, "B": 3, "C": 2, "D": 1}
    related = dict(model.related("A"))
    assert related["B"] == pytest.approx(2 / math.sqrt(3 * 3))
    assert related["C"] == pytest.approx(1 / math.sqrt(3 * 2))
    assert [i for i, _ in model.related("A")] == ["B", "C"]
    assert model.related("D") == []
    assert model.related("unknown") == []


def test_lift_and_min_pair_count() -> None:
    lift = CoPurchaseModel.fit(TINY, as_of=NOW, config=CoPurchaseConfig(metric="lift", min_pair_count=1))
    assert dict(lift.related("A"))["B"] == pytest.approx(2 * 5 / (3 * 3))
    strict = CoPurchaseModel.fit(TINY, as_of=NOW, config=CoPurchaseConfig(min_pair_count=2))
    assert [i for i, _ in strict.related("A")] == ["B"]  # A+C was seen once: not a pattern


def test_baskets_within_the_window_are_one_trip() -> None:
    frame = _rows([("c1", 0, ["A"]), ("c1", 10, ["B"]), ("c2", 0, ["A"]), ("c2", 40, ["C"])])
    month = CoPurchaseModel.fit(frame, as_of=T0 + timedelta(days=60), config=LOOSE)
    assert [i for i, _ in month.related("A")] == ["B"]  # C came 40 days later: a different trip
    same_basket = CoPurchaseModel.fit(
        frame, as_of=T0 + timedelta(days=60), config=CoPurchaseConfig(min_pair_count=1, window=timedelta(0))
    )
    assert same_basket.related("A") == []


def test_wholesale_trips_are_left_out_of_pair_counts() -> None:
    big = [f"X{i}" for i in range(10)]
    frame = _rows([("c1", 0, big), ("c2", 0, ["X0", "X1"])])
    model = CoPurchaseModel.fit(frame, as_of=NOW, config=CoPurchaseConfig(min_pair_count=1, max_trip_items=5))
    assert [i for i, _ in model.related("X0")] == ["X1"]
    assert model.n_trips == 1


def test_future_purchases_are_not_learnt() -> None:
    frame = _rows([("c1", 0, ["A", "B"]), ("c2", 5, ["A", "C"])])
    model = CoPurchaseModel.fit(frame, as_of=T0 + timedelta(days=1), config=LOOSE)
    assert "C" not in model.items


def test_owned_items_are_excluded_and_the_reason_names_the_owned_item() -> None:
    model = CoPurchaseModel.fit(TINY, as_of=NOW, config=LOOSE, names={"A": "Vitamin C serum"})
    recs = recommend(model, [_event("new", "A", 0)], k=3, now=NOW)
    assert "A" not in [r.item_id for r in recs]
    item, score, reason = recs[0]
    assert item == "B"
    assert score > 0
    assert reason == "Often bought with your Vitamin C serum"

    both = model.recommend([_event("new", "A", 0), _event("new", "B", 0)], k=2, now=NOW)
    assert both[0].item_id == "C"
    assert (
        both[0].reason.startswith("Often bought with your ")
        and "and 1 other thing you bought" in both[0].reason
    )
    assert not {"A", "B"} & {r.item_id for r in both}

    kept = model.recommend([_event("new", "A", 0), _event("new", "B", 0)], k=4, now=NOW, exclude_owned=False)
    assert {"A", "B"} <= {r.item_id for r in kept}


def test_events_after_now_are_ignored_when_recommending() -> None:
    model = CoPurchaseModel.fit(TINY, as_of=NOW, config=LOOSE)
    recs = model.recommend([_event("new", "A", 30)], k=2, now=NOW)
    assert all(r.reason.startswith("Popular") for r in recs)


def test_popularity_fills_the_gap_and_always_ranks_below_co_purchase() -> None:
    model = CoPurchaseModel.fit(TINY, as_of=NOW, config=LOOSE)
    cold = model.recommend([], k=2, now=NOW)
    assert [r.item_id for r in cold] == ["A", "B"]  # most buyers, then the item id breaks the tie
    assert all(r.reason == "Popular with other customers right now" for r in cold)

    recs = model.recommend([_event("new", "C", 0)], k=4, now=NOW)
    co = [r for r in recs if r.reason.startswith("Often")]
    pop = [r for r in recs if r.reason.startswith("Popular")]
    assert co and pop
    assert min(r.score for r in co) > max(r.score for r in pop)
    assert [r.score for r in recs] == sorted((r.score for r in recs), reverse=True)


def test_fallback_prefers_the_customers_own_categories() -> None:
    cats = {"A": "skin", "B": "skin", "C": "hair", "D": "hair", "E": "hair"}
    frame = _rows([("c1", 0, ["A"]), ("c2", 0, ["A"]), ("c3", 0, ["B"]), ("c4", 0, ["E"])], cats)
    model = CoPurchaseModel.fit(frame, as_of=NOW, config=LOOSE)
    recs = model.recommend([_event("new", "Z", 0, category="hair")], k=1, now=NOW)
    assert recs[0].item_id == "E"
    assert recs[0].reason == "Popular right now with customers who buy hair"


def test_category_diversity_promotes_a_second_category() -> None:
    cats = {"S": "serum", "M1": "moisturiser", "M2": "moisturiser", "M3": "moisturiser", "K": "cleanser"}
    baskets = [(f"c{i}", 0, ["S", "M1", "M2", "M3"]) for i in range(6)]
    baskets += [(f"d{i}", 0, ["S", "K"]) for i in range(3)]
    frame = _rows(baskets, cats)
    plain = CoPurchaseModel.fit(frame, as_of=NOW, config=CoPurchaseConfig(diversity=1.0))
    varied = CoPurchaseModel.fit(frame, as_of=NOW, config=CoPurchaseConfig(diversity=0.5))
    owner = [_event("new", "S", 0, category="serum")]
    assert [r.item_id for r in plain.recommend(owner, k=2, now=NOW)] == ["M1", "M2"]
    picked = [r.item_id for r in varied.recommend(owner, k=2, now=NOW)]
    assert picked == ["M1", "K"]


def test_recommendations_are_deterministic_and_ignore_row_order() -> None:
    data = synthetic_planted_pairs(seed=3, customers=200)
    cutoff = datetime(2011, 9, 10, tzinfo=UTC)
    one = CoPurchaseModel.fit(data, as_of=cutoff)
    two = CoPurchaseModel.fit(data.sample(frac=1.0, random_state=11), as_of=cutoff)
    events = [_event("x", "A01", 0), _event("x", "N03", 2)]
    later = T0 + timedelta(days=5)
    assert one.recommend(events, k=5, now=later) == two.recommend(events, k=5, now=later)
    assert one.recommend(events, k=5, now=later) == one.recommend(list(reversed(events)), k=5, now=later)


def test_events_become_a_basket_table() -> None:
    e1 = _event("c1", "A", 0).model_copy(update={"attrs": {"invoice_no": "INV1"}})
    e2 = _event("c1", "B", 3).model_copy(update={"attrs": {"invoice_no": "INV1"}})
    visit = Event(event_id="v", customer_id="c1", type=EventType.VISIT, ts=T0, source="test")
    frame = basket_frame([e1, e2, visit])
    assert list(frame["basket_id"]) == ["INV1", "INV1"]
    model = CoPurchaseModel.fit([e1, e2], as_of=T0 + timedelta(days=5), config=LOOSE)
    assert model.related("A")[0][0] == "B"


def test_one_instant_with_two_utc_offsets_is_one_basket() -> None:
    paris = timezone(timedelta(hours=1))
    e1 = _event("c1", "A", 0)
    e2 = _event("c1", "B", 0).model_copy(update={"ts": T0.astimezone(paris)})
    frame = basket_frame([e1, e2])
    assert frame["basket_id"].nunique() == 1


def test_category_only_purchases_steer_the_fallback() -> None:
    cats = {"A": "skin", "B": "skin", "E": "hair"}
    frame = _rows([("c1", 0, ["A"]), ("c2", 0, ["A"]), ("c3", 0, ["B"]), ("c4", 0, ["E"])], cats)
    model = CoPurchaseModel.fit(frame, as_of=NOW, config=LOOSE)
    no_item = Event(
        event_id="p",
        customer_id="new",
        type=EventType.PURCHASE,
        ts=T0,
        source="test",
        value=5.0,
        category="hair",
    )
    recs = model.recommend([no_item], k=1, now=NOW)
    assert [r.item_id for r in recs] == ["E"]


def test_next_best_item_refills_when_the_window_is_open_and_complements_when_not() -> None:
    model = CoPurchaseModel.fit(TINY, as_of=NOW, config=LOOSE, names={"A": "Vitamin C serum"})
    events = [_event("new", "A", 0)]
    refill = next_best_item(
        events,
        model,
        [
            RefillWindowInfo("C", window_open=True, days_until_due=2.0),
            RefillWindowInfo("A", window_open=True, days_until_due=-3.0),
        ],
        now=NOW,
    )
    assert refill is not None
    assert (refill.item_id, refill.kind) == ("A", "refill")
    assert "Vitamin C serum" in refill.reason

    closed = next_best_item(events, model, [RefillWindowInfo("A", window_open=False)], now=NOW)
    assert closed is not None
    assert (closed.item_id, closed.kind) == ("B", "complement")
    assert closed.reason == "Often bought with your Vitamin C serum"

    by_category = next_best_item(
        events, model, [RefillWindowInfo("skin", window_open=True, level="category")], now=NOW
    )
    assert by_category is not None and by_category.kind == "complement"

    empty = CoPurchaseModel.fit([], as_of=NOW)
    assert next_best_item(events, empty, [], now=NOW) is None


def test_evaluation_beats_popularity_on_planted_pairs() -> None:
    scores = evaluate(synthetic_planted_pairs(seed=7), ks=(1, 5))
    by = {(s.method, s.k): s for s in scores}
    for k in (1, 5):
        ours, base = by[("co-purchase (cosine)", k)], by[(POPULARITY, k)]
        assert ours.customers == base.customers > 100
        assert ours.hit_rate > base.hit_rate + 0.1
        assert ours.recall > base.recall
    assert by[("co-purchase (cosine)", 1)].hit_rate > 0.4


def test_retail_log_cleaning() -> None:
    log = pd.DataFrame(
        {
            "InvoiceNo": ["1", "1", "1", "C2", "3", "4", "5"],
            "StockCode": ["85123A", "POST", "22423", "85123A", "22423", "71053", "84029g"],
            "Quantity": [6, 1, 2, -6, 1, 3, 1],
            "InvoiceDate": ["12/1/2010 8:26"] * 6 + ["6/1/2011 9:00"],
            "UnitPrice": [2.55, 18.0, 12.75, 2.55, 12.75, 0.0, 3.39],
            "CustomerID": ["17850.0", "17850", "17850", "17850", None, "17850", "17850"],
            "Country": ["United Kingdom"] * 7,
        }
    )
    frame = retail_purchases(log)
    assert list(frame["item_id"]) == ["85123A", "22423", "84029G"]
    assert set(frame["customer_id"]) == {"17850"}
    assert int(frame["t"].iloc[0]) == int(datetime(2010, 12, 1, 8, 26, tzinfo=UTC).timestamp())
    # June is British Summer Time: 9:00 in London is 8:00 UTC.
    assert int(frame["t"].iloc[2]) == int(datetime(2011, 6, 1, 8, 0, tzinfo=UTC).timestamp())


def test_write_section_keeps_the_other_sections(tmp_path: Path) -> None:
    doc = tmp_path / "CATALOG.md"
    write_section(doc, "Recommendations", "## Recommendations\n\nfirst\n")
    assert doc.read_text().startswith("# ")
    text = doc.read_text() + "\n## Refill cycle\n\nrefill numbers\n"
    doc.write_text(text)
    write_section(doc, "Recommendations", "## Recommendations\n\nsecond\n")
    out = doc.read_text()
    assert "second" in out and "first" not in out
    assert "## Refill cycle\n\nrefill numbers" in out
    assert out.index("## Recommendations") < out.index("## Refill cycle")
