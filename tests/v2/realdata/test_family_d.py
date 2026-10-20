"""Family-D real-data runners: adapter helpers, two silent mapping results, and the IPW warm start.

No files, no network: every test builds its rows inline and runs in about a second. They pin:

- the bank-marketing date reconstruction (year from row order, week from weekday order) and the
  KDD Cup 98 YYMM reader the adapters rely on, plus the product refusing a YYMM float ("9606.0");
- two silent results of `map_frame` documented in `docs/v2/real_data/D-domains-messy.md`: a
  newest-first re-export loses its new rows (row-position ids), and a placeholder id such as "0"
  becomes one real customer. These are **findings**: if the product fixes them, these assertions
  fail and the notes must be updated;
- that the product's warm start reads an estimated `logged_probability` and weights by it.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

RUNNERS = Path(__file__).resolve().parents[3] / "scripts" / "v2_realdata"
if str(RUNNERS) not in sys.path:
    sys.path.insert(0, str(RUNNERS))

from d_adapters import bank_dates, yymm  # noqa: E402
from d_ipw import _click, _send, product_warm_start  # noqa: E402

from autonomy.contracts import Channel  # noqa: E402
from autonomy.data.mapping import EventMapping, map_frame, problem_counts  # noqa: E402
from autonomy.fakes import InMemoryEventStore  # noqa: E402


def test_bank_dates_and_kdd_yymm_helpers() -> None:
    rows = pd.DataFrame(
        {
            "month": ["may", "may", "may", "jun", "dec", "mar"],
            "day_of_week": ["mon", "fri", "tue", "wed", "thu", "mon"],
        }
    )
    got = [d.date().isoformat() for d in bank_dates(rows)]
    # may 2008: Mon 5th, Fri 9th, then Tue of the next week (13th); jun; dec; then March 2009.
    assert got == ["2008-05-05", "2008-05-09", "2008-05-13", "2008-06-04", "2008-12-04", "2009-03-02"]

    cells = pd.Series(["9606", 9606.0, None, "9713"])
    parsed = yymm(cells)
    assert parsed.iloc[0] == pd.Timestamp("1996-06-01") and parsed.iloc[1] == pd.Timestamp("1996-06-01")
    assert pd.isna(parsed.iloc[2]) and pd.isna(parsed.iloc[3])  # month 13 is not a month

    # The product itself: '%y%m' reads "9606" but refuses the float pandas makes of it.
    m = EventMapping(customer_id="c", timestamp="t", timestamp_format="%y%m", event_type="visit")
    events, problems = map_frame(pd.DataFrame({"c": ["a", "b"], "t": ["9606", 9606.0]}), m, "kdd")
    assert len(events) == 1 and problem_counts(problems) == {"bad_timestamp": 1}


def test_finding_reexport_and_placeholder_ids_are_silent() -> None:
    m = EventMapping(customer_id="c", timestamp="t", event_type="purchase", value_column="v")
    old = pd.DataFrame({"c": ["a", "b", "c"], "t": ["2024-01-01T10:00:00"] * 3, "v": ["1", "2", "3"]})
    new = pd.DataFrame({"c": ["d"], "t": ["2024-01-08T10:00:00"], "v": ["40"]})
    store = InMemoryEventStore()
    store.append(map_frame(old, m, "orders.csv")[0])
    # This week's export lists the new order first; no row_key, so ids are row positions.
    reexport = pd.concat([new, old], ignore_index=True)
    events, problems = map_frame(reexport, m, "orders.csv")
    assert not problems
    store.append(events)
    kept = sorted((e.customer_id, e.value) for e in store.scan())
    assert ("d", 40.0) not in kept  # the new order is dropped as a repeat of row 0...
    assert kept.count(("c", 3.0)) == 2  # ...and the last old order is counted twice

    guests = pd.DataFrame({"c": ["0", "0", "NULL", ""], "t": ["2024-01-01T10:00:00"] * 4, "v": ["5"] * 4})
    events, problems = map_frame(guests, m, "pos.csv")
    assert [e.customer_id for e in events] == ["0", "0", "NULL"]  # two guests merged into customer "0"
    assert problem_counts(problems) == {"missing_customer": 1}  # only the blank cell is caught


def test_warm_start_weights_logged_sends_by_their_estimated_probability() -> None:
    now = datetime(2024, 3, 1, 12, tzinfo=UTC)
    t0 = now - timedelta(days=30)

    def history(with_p: bool) -> list:  # type: ignore[type-arg]
        out = []
        # Two customers called at p=0.2 (one responds), two at p=0.9 (none responds).
        for cid, p, responded in [("a", 0.2, True), ("b", 0.2, False), ("c", 0.9, False), ("d", 0.9, False)]:
            did = f"{cid}:call"
            out.append(_send(cid, t0, did, "cellular", p if with_p else None, "test"))
            if responded:
                out.append(_click(cid, t0 + timedelta(hours=1), did, "test"))
        return out

    weighted = product_warm_start(history(True), now, Channel.WEBHOOK)
    plain = product_warm_start(history(False), now, Channel.WEBHOOK)
    assert (
        weighted["history_sends_used"] == 4
        and weighted["with_propensity"] == 4
        and plain["with_propensity"] == 0
    )
    key = "webhook|none|*|cellular"
    # Hajek with weights 5, 5, 1.1, 1.1: 5 / 12.2 = 0.41 against the plain 1 / 4; both are shrunk
    # towards the same click prior, so the weighted rate must come out higher.
    assert weighted["learned_click_rate"][key]["*pooled*"] > plain["learned_click_rate"][key]["*pooled*"]
