"""Family-B real-data runners: the adapter helpers and the Shadow consent variant, on tiny inline data.

No files, no network: each test builds a few rows by hand. They pin what the runners rely on:
Online Retail II's rewrite feeds the built-in loader correctly, the next-basket sets' invented time
keeps baskets in order around the cutoff, and without consent the Shadow day contacts nobody while
the labelled "consent invented" variant makes customers contactable.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

RUNNERS = Path(__file__).resolve().parents[3] / "scripts" / "v2_realdata"
if str(RUNNERS) not in sys.path:
    sys.path.insert(0, str(RUNNERS))

from b_adapters import NBR_CUTOFF, nbr_baskets, or2_to_loader_frame  # noqa: E402
from b_shadow import with_consent  # noqa: E402

from autonomy.contracts import Channel, Event, EventType  # noqa: E402
from autonomy.data.folder import StateFolder  # noqa: E402
from autonomy.data.loaders import online_retail_log_events  # noqa: E402
from autonomy.data.rebuild import rebuild_states  # noqa: E402
from autonomy.fakes import InMemoryEventStore, InMemoryStateStore  # noqa: E402


def test_online_retail_ii_rewrite_feeds_the_built_in_loader() -> None:
    raw = pd.DataFrame(
        {
            "Invoice": ["489434", "489434", "C489449", "489450"],
            "StockCode": ["85048", "79323P", "22087", "22111"],
            "Description": ["a", "b", "c", "d"],
            "Quantity": ["12", "6", "-2", "1"],
            "InvoiceDate": [
                "2009-12-01 07:45:00",
                "2009-12-01 07:45:00",
                "2009-12-01 10:33:00",
                "2009-12-01 10:41:00",
            ],
            "Price": ["6.95", "6.75", "2.10", "4.25"],
            "Customer ID": ["13085.0", "13085.0", "13085.0", None],
            "Country": ["United Kingdom"] * 4,
        }
    )
    result = online_retail_log_events(or2_to_loader_frame(raw), grain="invoice")
    kinds = sorted((e.type.value, e.customer_id, round(e.value or 0, 2)) for e in result.events)
    assert kinds == [("purchase", "13085", 123.9), ("refund", "13085", 4.2)]
    assert [p.code for p in result.problems] == ["excluded"]  # the guest line
    assert result.events[0].ts.tzinfo is not None


def test_next_basket_invented_time_keeps_order_around_the_cutoff() -> None:
    hist = pd.DataFrame(
        {
            "CUSTOMER_ID": ["1", "1", "1", "2"],
            "ORDER_NUMBER": ["1", "1", "2", "1"],
            "MATERIAL_NUMBER": ["a", "b", "a", "c"],
        }
    )
    fut = pd.DataFrame({"CUSTOMER_ID": ["1", "2"], "ORDER_NUMBER": ["3", "2"], "MATERIAL_NUMBER": ["d", "c"]})
    rows = nbr_baskets(hist, fut)
    cut = int(NBR_CUTOFF.timestamp())
    one = rows[rows["customer_id"] == "1"].sort_values("t")
    assert list(one["basket_id"].drop_duplicates()) == ["1|1", "1|2", "1|3"]
    assert (one[one["basket_id"] != "1|3"]["t"] < cut).all()
    assert (rows[rows["basket_id"].isin(["1|3", "2|2"])]["t"] > cut).all()
    # each customer's last history basket sits one week before the cutoff, whatever its number
    last = rows[rows["basket_id"].isin(["1|2", "2|1"])]["t"].unique().tolist()
    assert last == [cut - 7 * 86400]


def _orders() -> list[Event]:
    t0 = datetime(2024, 1, 10, 12, tzinfo=UTC)
    return [
        Event(
            event_id=f"e{i}",
            customer_id=c,
            type=EventType.PURCHASE,
            ts=t0 + timedelta(days=i),
            source="t",
            value=20.0,
        )
        for i, c in enumerate(["a", "b", "a"])
    ]


def test_order_history_alone_leaves_nobody_contactable_until_consent_is_invented() -> None:
    now = datetime(2024, 6, 1, 10, tzinfo=UTC)
    for consent, expected in ((False, set()), (True, {"a", "b"})):
        events = with_consent(_orders()) if consent else _orders()
        store, states = InMemoryEventStore(), InMemoryStateStore()
        store.append(events)
        rebuild_states(store, states, StateFolder(), now)
        reachable = {s.customer_id for s in states.scan() if Channel.EMAIL in s.contactable_channels()}
        assert reachable == expected
    invented = [e for e in with_consent(_orders()) if e.type is EventType.CONSENT_GRANTED]
    assert len(invented) == 2 and all(e.attrs.get("invented") is True for e in invented)
    assert all(e.source == "adapter-invented" for e in invented)
