"""The real half: point-in-time features and the replayed receipts."""

from __future__ import annotations

import numpy as np
import pandas as pd

from autonomy.gym.waves.config import EFFECT_WEIGHTS, HORIZON_DAYS
from autonomy.gym.waves.data import (
    FEATURE_COLUMNS,
    SHOP_TZ,
    build_waves_data,
    default_cutoff,
    eligible_clients,
    unit_hash,
)


def test_features_never_read_a_receipt_at_or_after_the_cutoff(
    tables: tuple[pd.DataFrame, pd.DataFrame, pd.Timestamp],
) -> None:
    receipts, clients, cutoff = tables
    before = build_waves_data(receipts, clients, cutoff=cutoff)
    # wreck everything the world could peek at: scale, shift and add receipts after the cutoff
    local = pd.to_datetime(receipts["transaction_datetime"]).dt.tz_localize(SHOP_TZ).dt.tz_convert("UTC")
    start = pd.Timestamp(before.start)
    late = local >= start
    assert late.any()
    wrecked = receipts.copy()
    wrecked.loc[late, "purchase_sum"] = wrecked.loc[late, "purchase_sum"] * 17.0
    extra = (
        receipts[late].head(40).assign(transaction_id=lambda f: "x" + f["transaction_id"], purchase_sum=1e6)
    )
    wrecked = pd.concat([wrecked, extra], ignore_index=True)
    after = build_waves_data(wrecked, clients, cutoff=cutoff)
    pd.testing.assert_frame_equal(before.features, after.features)
    assert np.array_equal(before.base_rate, after.base_rate)
    assert np.array_equal(before.value, after.value)
    assert before.hist_id == after.hist_id
    assert before.pre_days == after.pre_days
    # and the replayed receipts did change (so the test could have caught a leak)
    assert not np.array_equal(before.post_value, after.post_value[: len(before.post_value)])


def test_a_receipt_just_before_midnight_utc_is_history_and_just_after_is_replayed(
    tables: tuple[pd.DataFrame, pd.DataFrame, pd.Timestamp],
) -> None:
    receipts, clients, cutoff = tables
    cid = str(clients["client_id"].iloc[0])
    # Moscow is UTC+3, so the cutoff instant (00:00 UTC) is 03:00 Moscow time
    probe = pd.DataFrame(
        {
            "client_id": [cid, cid],
            "transaction_id": ["edge_before", "edge_after"],
            "transaction_datetime": [
                f"{cutoff:%Y-%m-%d} 02:59:59",
                f"{cutoff:%Y-%m-%d} 03:00:00",
            ],
            "purchase_sum": [100.0, 200.0],
        }
    )
    d = build_waves_data(pd.concat([receipts, probe], ignore_index=True), clients, cutoff=cutoff)
    assert "edge_before" in d.hist_id and "edge_before" not in d.post_id
    assert "edge_after" in d.post_id and "edge_after" not in d.hist_id
    assert d.post_s[d.post_id.index("edge_after")] == 0.0


def test_replayed_receipts_are_in_time_order_and_inside_the_horizon(data) -> None:
    assert np.all(np.diff(data.post_s) >= 0)
    assert data.post_s.min() >= 0
    assert data.post_s.max() < HORIZON_DAYS * 86_400
    assert data.hist_s.max() < 0


def test_default_cutoff_leaves_the_horizon_after_the_last_receipt(
    tables: tuple[pd.DataFrame, pd.DataFrame, pd.Timestamp],
) -> None:
    receipts, _clients, _cutoff = tables
    cut = default_cutoff(receipts, 84)
    last = pd.to_datetime(receipts["transaction_datetime"]).max()
    assert cut + pd.Timedelta(days=84) > last
    assert cut + pd.Timedelta(days=83) <= last.normalize() + pd.Timedelta(days=1)


def test_base_rate_is_a_shrunk_positive_rate(data) -> None:
    assert (data.base_rate > 0).all() and (data.base_rate < 1).all()
    none_before = data.features["n_receipts"].to_numpy() == 0
    assert none_before.any()
    assert data.base_rate[none_before].min() > 0
    # a customer with receipts is not rated below one without any
    assert data.base_rate[~none_before].mean() > data.base_rate[none_before].mean()


def test_unit_hash_is_stable_and_uniform() -> None:
    ids = [f"c{i}" for i in range(2000)]
    a, b = unit_hash(ids, "s"), unit_hash(ids, "s")
    assert np.array_equal(a, b)
    assert not np.array_equal(a, unit_hash(ids, "t"))
    assert abs(a.mean() - 0.5) < 0.03


def test_effect_weights_sum_to_one_and_are_front_loaded() -> None:
    assert abs(sum(EFFECT_WEIGHTS) - 1.0) < 1e-12
    assert 0.7 < sum(EFFECT_WEIGHTS[:7]) < 0.76


def test_the_features_are_exactly_the_documented_columns(data) -> None:
    assert tuple(data.features.columns) == FEATURE_COLUMNS


def test_customers_are_chosen_point_in_time(tables: tuple[pd.DataFrame, pd.DataFrame, pd.Timestamp]) -> None:
    """A card issued after the cutoff is not a customer yet, whatever they buy later; a client with no issue date
    counts only with a receipt before the cutoff."""
    receipts, clients, cutoff = tables
    c = clients.copy()
    late, undated_buyer, undated_silent = c["client_id"].iloc[:3].tolist()
    c.loc[0, "first_issue_date"] = f"{cutoff + pd.Timedelta(days=10):%Y-%m-%d %H:%M:%S}"
    c.loc[1, "first_issue_date"] = None
    c.loc[2, "first_issue_date"] = None
    # the third client buys only after the cutoff, the second before it
    r = receipts[receipts["client_id"] != undated_silent]
    r = r[~((r["client_id"] == undated_buyer) & (pd.to_datetime(r["transaction_datetime"]) >= cutoff))]
    after = pd.DataFrame(
        {
            "client_id": [undated_silent, late, undated_buyer],
            "transaction_id": ["late_1", "late_2", "early_1"],
            "transaction_datetime": [
                f"{cutoff + pd.Timedelta(days=3):%Y-%m-%d} 12:00:00",
                f"{cutoff + pd.Timedelta(days=3):%Y-%m-%d} 12:00:00",
                f"{cutoff - pd.Timedelta(days=3):%Y-%m-%d} 12:00:00",
            ],
            "purchase_sum": [500.0, 500.0, 300.0],
        }
    )
    r = pd.concat([r, after], ignore_index=True)
    kept, counts = eligible_clients(r, c, cutoff)
    ids = set(kept["client_id"])
    assert late not in ids and undated_silent not in ids
    assert undated_buyer in ids
    assert counts["dropped_issued_after_cutoff"] == 1
    assert counts["dropped_no_issue_date_no_receipt_before"] == 1
    assert counts["kept"] == len(clients) - 2
    # what is bought after the cutoff never changes who is chosen
    more = pd.concat([r, after.iloc[:2].assign(transaction_id=["late_3", "late_4"])], ignore_index=True)
    assert set(eligible_clients(more, c, cutoff)[0]["client_id"]) == ids


def test_base_rate_counts_each_customers_own_time_as_a_card_holder(
    tables: tuple[pd.DataFrame, pd.DataFrame, pd.Timestamp],
) -> None:
    receipts, clients, cutoff = tables
    c = clients.copy()
    before = build_waves_data(receipts, c, cutoff=cutoff)
    i = int(np.argmax(before.features["n_receipts"].to_numpy()))
    cid = before.customer_ids[i]
    own = receipts[
        (receipts["client_id"] == cid) & (pd.to_datetime(receipts["transaction_datetime"]) < cutoff)
    ]
    # the same receipts, but the card (and every receipt) only in the last 10 days
    first = pd.to_datetime(own["transaction_datetime"]).min()
    shifted = receipts[~receipts.index.isin(own.index)]
    moved = own.assign(
        transaction_datetime=[
            f"{cutoff - pd.Timedelta(days=10) + pd.Timedelta(hours=10 + k % 9):%Y-%m-%d %H:%M:%S}"
            for k in range(len(own))
        ]
    )
    c.loc[i, "first_issue_date"] = f"{cutoff - pd.Timedelta(days=10):%Y-%m-%d %H:%M:%S}"
    after = build_waves_data(pd.concat([shifted, moved], ignore_index=True), c, cutoff=cutoff)
    assert len(own) > 0 and first < cutoff - pd.Timedelta(days=10)
    assert after.base_rate[i] > 2 * before.base_rate[i]
