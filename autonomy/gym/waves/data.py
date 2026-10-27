"""The real half of the benchmark: customers and their buying, from receipts, point in time.

`build_waves_data(receipts, clients, cutoff=...)` turns a receipts table (X5 RetailHero in CI, a tiny synthetic
table of the same shape in tests) into what the world needs:

* every customer's **features**, computed only from receipts before the cutoff (recency, frequency,
  monetary, mean receipt, active days) plus the client file's age, gender and card age. Receipts at or after the
  cutoff never touch them (`tests/v4/waves/test_data.py` changes them and checks nothing moves);
* the customer's **base purchase rate** per day over their own time as a cardholder before the cutoff, shrunk
  toward the population's, which the hidden effect scales;
* the customer's **real receipts after the cutoff**, in time order, which the world replays day by day as the
  no-message world.

`eligible_clients` picks the customers point in time: card holders at the cutoff (a card issued before it, or, with
no issue date, a receipt before it). Who buys after the cutoff plays no part in it.

`fixture_tables` makes a small synthetic table of the same shape so tests and smoke runs need no download. Its
numbers are not evidence about anything.

The shop's clock: receipts carry local (Moscow) wall-clock times; they are converted to UTC, and the world's
days are UTC days starting at 00:00 UTC on the cutoff date.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd

from autonomy.gym.waves.config import (
    BASE_PRIOR_DAYS,
    EMAIL_CONSENT_SHARE,
    HORIZON_DAYS,
    MAX_DAILY_PROB,
)

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]
BoolArray = npt.NDArray[np.bool_]

SHOP_TZ = "Europe/Moscow"
FEATURE_COLUMNS: tuple[str, ...] = (
    "recency_days",
    "n_receipts",
    "total",
    "mean_receipt",
    "active_days",
    "age",
    "gender",
    "tenure_days",
)
"""What the hidden effect may depend on, all known before the cutoff. `gender` is 0 (F), 1 (M) or 0.5 (unknown)."""
SECONDS_PER_DAY = 86_400.0
EMAIL_SALT = "autonomy-waves-v1:email"


def unit_hash(ids: Sequence[str], salt: str) -> FloatArray:
    """A number in [0, 1) per id from BLAKE2b of `salt:id`: the same on every machine."""
    out = np.empty(len(ids), dtype=np.float64)
    prefix = f"{salt}:".encode()
    for i, cid in enumerate(ids):
        digest = hashlib.blake2b(prefix + str(cid).encode(), digest_size=8).digest()
        out[i] = int.from_bytes(digest, "big") / 2.0**64
    return out


@dataclass(frozen=True, eq=False)
class WavesData:
    """The customers of one benchmark and their real buying. Arrays are per customer, in `customer_ids` order."""

    customer_ids: tuple[str, ...]
    start: datetime
    """00:00 UTC on the cutoff date: the first decision day. Nothing the policies see is later than this on day 0."""
    horizon_days: int
    features: pd.DataFrame
    """`FEATURE_COLUMNS`, point in time."""
    pre_days: float
    """Days of receipt history before the cutoff the features were computed over."""
    base_rate: FloatArray
    """Purchases per day, shrunk toward the population's (the hidden effect is a lift of this)."""
    value: FloatArray
    """The customer's own mean receipt before the cutoff (the population's mean receipt where they had none)."""
    sms_consent: BoolArray
    email_consent: BoolArray
    signup_s: FloatArray
    """Card issue time in seconds from `start` (negative, never later than a day before it: `eligible_clients`
    drops the customers whose card came later)."""
    hist_idx: IntArray
    hist_s: FloatArray
    hist_value: FloatArray
    hist_id: tuple[str, ...]
    post_idx: IntArray
    """Real receipts at or after the cutoff and before day `horizon_days`, in time order."""
    post_s: FloatArray
    post_value: FloatArray
    post_id: tuple[str, ...]
    age: FloatArray
    gender: tuple[str, ...]
    eligibility: Mapping[str, int] = field(default_factory=dict)
    """How the customers were chosen (`eligible_clients` counts), for the run's `data:` line."""

    @property
    def n(self) -> int:
        return len(self.customer_ids)

    def index_of(self) -> dict[str, int]:
        return {c: i for i, c in enumerate(self.customer_ids)}


def default_cutoff(receipts: pd.DataFrame, horizon_days: int = HORIZON_DAYS) -> pd.Timestamp:
    """The cutoff that leaves exactly `horizon_days` of real receipts after it: the day after the last
    receipt, minus the horizon. (For X5 that is 2018-12-25, with 34 days of history before it.)"""
    last = pd.to_datetime(receipts["transaction_datetime"]).max()
    return (last.normalize() + pd.Timedelta(days=1)) - pd.Timedelta(days=horizon_days)


def _to_utc(local: pd.Series, tz: str) -> pd.Series:
    stamped = pd.to_datetime(local).dt.tz_localize(tz, ambiguous="NaT", nonexistent="shift_forward")
    return stamped.dt.tz_convert("UTC")


def eligible_clients(
    receipts: pd.DataFrame, clients: pd.DataFrame, cutoff: pd.Timestamp, *, tz: str = SHOP_TZ
) -> tuple[pd.DataFrame, dict[str, int]]:
    """The clients who were customers at the cutoff, and the counts of who was kept and dropped.

    Kept: a card issued before 00:00 UTC on the cutoff date, or no issue date and a receipt before that instant.
    Dropped: a card issued at or after it (they were not customers yet, whatever they bought later), and clients
    with no issue date and no receipt before it. Receipts after the cutoff are never read."""
    start = pd.Timestamp(datetime(cutoff.year, cutoff.month, cutoff.day, tzinfo=UTC))
    issue = _to_utc(pd.to_datetime(clients["first_issue_date"], errors="coerce"), tz)
    ids = clients["client_id"].astype(str)
    issued_before = (issue < start).fillna(False).to_numpy(dtype=np.bool_)
    no_date = issue.isna().to_numpy()
    undated = set(ids[no_date])
    rec = receipts[receipts["client_id"].astype(str).isin(undated)] if undated else receipts.iloc[:0]
    ts = _to_utc(rec["transaction_datetime"], tz)
    bought_before = set(rec.loc[(ts < start).to_numpy(), "client_id"].astype(str))
    with_receipt = ids.isin(bought_before).to_numpy()
    keep = issued_before | (no_date & with_receipt)
    counts = {
        "clients": len(clients),
        "kept": int(keep.sum()),
        "dropped_issued_after_cutoff": int((~issued_before & ~no_date).sum()),
        "dropped_no_issue_date_no_receipt_before": int((no_date & ~with_receipt).sum()),
    }
    return clients[keep].reset_index(drop=True), counts


def build_waves_data(
    receipts: pd.DataFrame,
    clients: pd.DataFrame,
    *,
    cutoff: pd.Timestamp | None = None,
    horizon_days: int = HORIZON_DAYS,
    tz: str = SHOP_TZ,
) -> WavesData:
    """The benchmark's customers from a receipts table and a client table.

    `receipts`: client_id, transaction_id, transaction_datetime (shop-local), purchase_sum.
    `clients`: client_id, first_issue_date, age, gender (F, M or U). The customers are the rows of `clients`, in
    that order. `cutoff` is a shop-local date (default `default_cutoff`)."""
    cut = default_cutoff(receipts, horizon_days) if cutoff is None else pd.Timestamp(cutoff)
    start = datetime(cut.year, cut.month, cut.day, tzinfo=UTC)
    ids = tuple(str(c) for c in clients["client_id"])
    index = {c: i for i, c in enumerate(ids)}
    n = len(ids)

    rec = receipts[["client_id", "transaction_id", "transaction_datetime", "purchase_sum"]].copy()
    rec["client_id"] = rec["client_id"].astype(str)
    rec = rec[rec["client_id"].isin(index)]
    rec["ts"] = _to_utc(rec["transaction_datetime"], tz)
    rec = rec.dropna(subset=["ts"]).sort_values(["ts", "transaction_id"], kind="mergesort")
    start_ts = pd.Timestamp(start)
    end_ts = start_ts + pd.Timedelta(days=horizon_days)
    pre = rec[rec["ts"] < start_ts]
    post = rec[(rec["ts"] >= start_ts) & (rec["ts"] < end_ts)]
    data_start = pre["ts"].min() if len(pre) else start_ts - pd.Timedelta(days=1)
    pre_days = max(float((start_ts - data_start).total_seconds() / SECONDS_PER_DAY), 1.0)

    # point-in-time features: `pre` only
    g = pre.groupby("client_id")
    last = g["ts"].max()
    stats = pd.DataFrame(
        {
            "n_receipts": g.size(),
            "total": g["purchase_sum"].sum(),
            "mean_receipt": g["purchase_sum"].mean(),
            "recency_days": (start_ts - last).dt.total_seconds() / SECONDS_PER_DAY,
            "active_days": pre.assign(day=pre["ts"].dt.floor("D")).groupby("client_id")["day"].nunique(),
        }
    ).reindex(list(ids))
    f = pd.DataFrame(index=range(n))
    for col in ("n_receipts", "total", "active_days"):
        f[col] = stats[col].fillna(0.0).to_numpy(dtype=np.float64)
    pop_mean = float(pre["purchase_sum"].mean()) if len(pre) else 1.0
    f["mean_receipt"] = stats["mean_receipt"].fillna(0.0).to_numpy(dtype=np.float64)
    f["recency_days"] = stats["recency_days"].fillna(999.0).to_numpy(dtype=np.float64)
    c = clients.reset_index(drop=True)
    age = pd.to_numeric(c["age"], errors="coerce").clip(0, 100)
    age = age.fillna(age.median() if age.notna().any() else 40.0)
    f["age"] = age.to_numpy(dtype=np.float64)
    gender = tuple(str(x) if str(x) in ("F", "M") else "U" for x in c["gender"].fillna("U"))
    f["gender"] = np.array([{"F": 0.0, "M": 1.0}.get(x, 0.5) for x in gender])
    issue = pd.to_datetime(c["first_issue_date"], errors="coerce")
    issue_utc = _to_utc(issue.fillna(pd.Timestamp(cut) - pd.Timedelta(days=365)), tz)
    tenure = (start_ts - issue_utc).dt.total_seconds() / SECONDS_PER_DAY
    f["tenure_days"] = tenure.clip(lower=0.0).to_numpy(dtype=np.float64)
    f = f[list(FEATURE_COLUMNS)]

    # exposure: the customer's own days as a card holder before the cutoff (or since their first receipt, if
    # earlier), within the history the receipts cover
    n_pre = f["n_receipts"].to_numpy()
    first_days = (
        (start_ts - g["ts"].min()).dt.total_seconds().div(SECONDS_PER_DAY).reindex(list(ids)).fillna(0.0)
    ).to_numpy(dtype=np.float64)
    exposure = np.clip(np.maximum(f["tenure_days"].to_numpy(), first_days), 1.0, pre_days)
    prior_rate = float(n_pre.sum() / exposure.sum()) if exposure.sum() > 0 else 0.0
    base = (n_pre + BASE_PRIOR_DAYS * prior_rate) / (exposure + BASE_PRIOR_DAYS)
    base = np.clip(base, 1e-4, MAX_DAILY_PROB)
    value = np.where(n_pre > 0, f["mean_receipt"].to_numpy(), pop_mean)
    signup = np.minimum(
        (issue_utc - start_ts).dt.total_seconds().to_numpy(dtype=np.float64), -SECONDS_PER_DAY
    )

    def rows(frame: pd.DataFrame) -> tuple[IntArray, FloatArray, FloatArray, tuple[str, ...]]:
        idx = np.array([index[x] for x in frame["client_id"]], dtype=np.int64)
        secs: FloatArray = np.asarray((frame["ts"] - start_ts).dt.total_seconds(), dtype=np.float64)
        vals: FloatArray = np.asarray(frame["purchase_sum"], dtype=np.float64)
        return idx, secs, vals, tuple(str(x) for x in frame["transaction_id"])

    h_idx, h_s, h_v, h_id = rows(pre)
    p_idx, p_s, p_v, p_id = rows(post)
    return WavesData(
        customer_ids=ids,
        start=start,
        horizon_days=horizon_days,
        features=f,
        pre_days=pre_days,
        base_rate=base,
        value=value.astype(np.float64),
        sms_consent=np.ones(n, dtype=np.bool_),
        email_consent=unit_hash(ids, EMAIL_SALT) < EMAIL_CONSENT_SHARE,
        signup_s=signup,
        hist_idx=h_idx,
        hist_s=h_s,
        hist_value=h_v,
        hist_id=h_id,
        post_idx=p_idx,
        post_s=p_s,
        post_value=p_v,
        post_id=p_id,
        age=f["age"].to_numpy(dtype=np.float64),
        gender=gender,
    )


def fixture_tables(
    n: int = 600,
    seed: int = 0,
    *,
    pre_days: int = 60,
    horizon_days: int = HORIZON_DAYS,
    rate_mean: float = 0.07,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.Timestamp]:
    """A tiny synthetic X5-shaped table: `(receipts, clients, cutoff)`.

    `rate_mean` is the mean receipts per customer-day (0.07; X5 has about 0.17) and `pre_days` the history before
    the cutoff (60; X5 about 34). Customers differ in how often they buy (gamma), how much a receipt is (log-normal) and age/gender/card age;
    receipts are Poisson per day. It exists so tests and smoke runs need no download. Its numbers say nothing
    about real shoppers."""
    rng = np.random.default_rng([seed, 90210])
    cut = pd.Timestamp("2018-12-25")
    first_day = cut - pd.Timedelta(days=pre_days)
    total_days = pre_days + horizon_days
    rate = rng.gamma(1.2, rate_mean / 1.2, n)  # receipts per day, mean `rate_mean`
    scale = np.exp(rng.normal(6.0, 0.5, n))  # a customer's typical receipt, RUB
    rows: list[dict[str, Any]] = []
    counts = rng.poisson(rate[:, None] * np.ones((1, total_days)))
    for i in range(n):
        for d in np.flatnonzero(counts[i]).tolist():
            for k in range(int(counts[i, d])):
                secs = int(rng.integers(8 * 3600, 22 * 3600))
                ts = first_day + pd.Timedelta(days=int(d), seconds=secs)
                rows.append(
                    {
                        "client_id": f"c{i:05d}",
                        "transaction_id": f"t{i:05d}_{d:03d}_{k}",
                        "transaction_datetime": ts.strftime("%Y-%m-%d %H:%M:%S"),
                        "purchase_sum": float(round(scale[i] * np.exp(rng.normal(0.0, 0.3)), 2)),
                    }
                )
    receipts = pd.DataFrame(rows)
    clients = pd.DataFrame(
        {
            "client_id": [f"c{i:05d}" for i in range(n)],
            "first_issue_date": [
                (first_day - pd.Timedelta(days=int(x))).strftime("%Y-%m-%d %H:%M:%S")
                for x in rng.integers(1, 1500, n)
            ],
            "age": rng.normal(45, 15, n).round(),
            "gender": rng.choice(["F", "M", "U"], n, p=[0.6, 0.35, 0.05]),
        }
    )
    return receipts, clients, cut
