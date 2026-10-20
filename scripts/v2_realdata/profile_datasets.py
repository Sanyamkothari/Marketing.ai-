"""Profile the downloaded real datasets for docs/v2/real_data/CATALOGUE.md.

Counts rows, distinct customers and the time span of every dataset under
/home/user/datasets/<name>/ (the data never lives in the repo). Every number
in the catalogue comes from running this script:

    python3.11 -I scripts/v2_realdata/profile_datasets.py \
        --root /home/user/datasets --out /home/user/datasets/_profile/profile.json

Optional: --only name1,name2 to profile a subset. Big files are read in chunks
with only the columns needed, so peak memory stays modest; wall time and peak
RSS are recorded per dataset.

Light deps only: pandas + numpy + stdlib. Parquet needs pyarrow or fastparquet;
when neither is installed the dataset is recorded as unreadable (that is a
finding, not a crash).
"""

from __future__ import annotations

import argparse
import json
import resource
import sys
import time
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

CHUNK = 2_000_000


def _rss_mb() -> float:
    # ru_maxrss is KiB on Linux; it is the process peak so far (monotonic).
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def _span(
    series: pd.Series, fmt: str | None = None, unit: str | None = None
) -> tuple[str | None, str | None, int]:
    if unit:
        t = pd.to_datetime(series, unit=unit, errors="coerce")
    else:
        t = pd.to_datetime(series, format=fmt, errors="coerce")
    bad = int(t.isna().sum() - series.isna().sum())
    if t.notna().any():
        return str(t.min()), str(t.max()), bad
    return None, None, bad


def _chunked(
    path: str, usecols: list[str], cust: str | None, time_col: str | None, fmt: str | None = None, **kw: Any
) -> dict[str, Any]:
    rows = 0
    custs: set = set()
    tmin = tmax = None
    bad = 0
    for ch in pd.read_csv(path, usecols=usecols, chunksize=CHUNK, **kw):
        rows += len(ch)
        if cust:
            custs.update(ch[cust].dropna().unique().tolist())
        if time_col:
            t = pd.to_datetime(ch[time_col], format=fmt, errors="coerce")
            bad += int(t.isna().sum() - ch[time_col].isna().sum())
            if t.notna().any():
                a, b = t.min(), t.max()
                tmin = a if tmin is None or a < tmin else tmin
                tmax = b if tmax is None or b > tmax else tmax
    return {
        "rows": rows,
        "customers": len(custs) if cust else None,
        "time_min": str(tmin) if tmin is not None else None,
        "time_max": str(tmax) if tmax is not None else None,
        "unparsed_dates": bad,
    }


def p_hillstrom(r: str) -> dict:
    df = pd.read_csv(f"{r}/hillstrom/hillstorm_no_indices.csv.gz")
    return {
        "rows": len(df),
        "customers": len(df),
        "columns": list(df.columns),
        "arms": df["segment"].value_counts().to_dict(),
        "visit_rate": float(df["visit"].mean()),
        "conversion_rate": float(df["conversion"].mean()),
        "note": "one row per customer; no customer id column (row index is the id); no dates",
    }


def p_criteo(r: str) -> dict:
    rows = 0
    t = c = v = e = 0
    for ch in pd.read_csv(
        f"{r}/criteo_uplift/criteo-research-uplift-v2.1.csv.gz",
        usecols=["treatment", "conversion", "visit", "exposure"],
        chunksize=CHUNK,
    ):
        rows += len(ch)
        t += int(ch["treatment"].sum())
        c += int(ch["conversion"].sum())
        v += int(ch["visit"].sum())
        e += int(ch["exposure"].sum())
    return {
        "rows": rows,
        "customers": rows,
        "treated": t,
        "conversions": c,
        "visits": v,
        "exposed": e,
        "columns": [f"f{i}" for i in range(12)] + ["treatment", "conversion", "visit", "exposure"],
        "note": "one row per user, no user id, no dates, 12 anonymised float features",
    }


def p_lenta(r: str) -> dict:
    path = f"{r}/lenta/lenta_dataset.csv.gz"
    cols = list(pd.read_csv(path, nrows=0).columns)
    df = pd.read_csv(path, usecols=["group", "response_att", "gender"])
    return {
        "rows": len(df),
        "customers": len(df),
        "n_columns": len(cols),
        "arms": df["group"].value_counts().to_dict(),
        "response_rate": float(df["response_att"].mean()),
        "gender_values": df["gender"].value_counts(dropna=False).astype(int).to_dict(),
        "note": "one row per customer, no id, no dates; gender coded in Cyrillic (Ж/М)",
    }


def p_megafon(r: str) -> dict:
    df = pd.read_csv(f"{r}/megafon/megafon_dataset.csv.gz", usecols=["treatment_group", "conversion"])
    return {
        "rows": len(df),
        "customers": len(df),
        "arms": df["treatment_group"].value_counts().to_dict(),
        "conversion_rate": float(df["conversion"].mean()),
        "note": "one row per subscriber, no id, no dates, 50 anonymised features X_1..X_50",
    }


def p_x5(r: str) -> dict:
    d = f"{r}/x5_retailhero"
    up = pd.read_csv(f"{d}/uplift_train.csv.gz")
    cl = pd.read_csv(f"{d}/clients.csv.gz")
    out = _chunked(
        f"{d}/purchases.csv.gz",
        ["client_id", "transaction_id", "transaction_datetime"],
        "client_id",
        "transaction_datetime",
        fmt="%Y-%m-%d %H:%M:%S",
    )
    tx: set = set()
    for ch in pd.read_csv(f"{d}/purchases.csv.gz", usecols=["transaction_id"], chunksize=CHUNK):
        tx.update(ch["transaction_id"].unique().tolist())
    out.update(
        {
            "purchase_rows": out.pop("rows"),
            "purchase_customers": out.pop("customers"),
            "transactions": len(tx),
            "clients_rows": len(cl),
            "uplift_train_rows": len(up),
            "treated": int(up["treatment_flg"].sum()),
            "target_rate": float(up["target"].mean()),
        }
    )
    out["rows"] = out["purchase_rows"]
    out["customers"] = len(cl)
    out["note"] = "uplift_train has treatment+target per client; purchases are line items BEFORE the campaign"
    return out


def p_olist(r: str) -> dict:
    d = f"{r}/olist"
    o = pd.read_csv(f"{d}/olist_orders_dataset.csv.gz")
    c = pd.read_csv(f"{d}/olist_customers_dataset.csv.gz")
    it = pd.read_csv(f"{d}/olist_order_items_dataset.csv.gz")
    a, b, bad = _span(o["order_purchase_timestamp"], "%Y-%m-%d %H:%M:%S")
    m = o.merge(c, on="customer_id", how="left")
    per = m.groupby("customer_unique_id")["order_id"].nunique()
    return {
        "rows": len(it),
        "orders": len(o),
        "order_items": len(it),
        "customers": int(c["customer_unique_id"].nunique()),
        "customer_id_values": int(c["customer_id"].nunique()),
        "time_min": a,
        "time_max": b,
        "unparsed_dates": bad,
        "repeat_customer_share": float((per > 1).mean()),
        "order_status": o["order_status"].value_counts().to_dict(),
        "note": "customer_id is per ORDER; the real person id is customer_unique_id in customers table",
    }


def p_online_retail_i(r: str) -> dict:
    df = pd.read_csv(f"{r}/online_retail_i/online-retail-dataset.csv", encoding="latin-1")
    a, b, bad = _span(df["InvoiceDate"], "%m/%d/%Y %H:%M")
    return {
        "rows": len(df),
        "customers": int(df["CustomerID"].nunique()),
        "invoices": int(df["InvoiceNo"].nunique()),
        "missing_customer_rows": int(df["CustomerID"].isna().sum()),
        "negative_qty_rows": int((df["Quantity"] < 0).sum()),
        "cancel_invoices": int(df["InvoiceNo"].astype(str).str.startswith("C").sum()),
        "countries": int(df["Country"].nunique()),
        "time_min": a,
        "time_max": b,
        "unparsed_dates": bad,
    }


def p_online_retail_ii(r: str) -> dict:
    df = pd.read_csv(f"{r}/online_retail_ii/online_retail_II.csv", encoding="latin-1")
    a, b, bad = _span(df["InvoiceDate"], "%Y-%m-%d %H:%M:%S")
    out = {
        "rows": len(df),
        "customers": int(df["Customer ID"].nunique()),
        "invoices": int(df["Invoice"].nunique()),
        "missing_customer_rows": int(df["Customer ID"].isna().sum()),
        "negative_qty_rows": int((df["Quantity"] < 0).sum()),
        "countries": int(df["Country"].nunique()),
        "time_min": a,
        "time_max": b,
        "unparsed_dates": bad,
        "exact_duplicate_rows": int(df.duplicated().sum()),
    }
    try:
        import openpyxl  # noqa: F401

        out["xlsx_sheets"] = pd.ExcelFile(f"{r}/online_retail_ii/online_retail_II.xlsx").sheet_names
    except Exception as e:  # pragma: no cover
        out["xlsx_error"] = repr(e)
    return out


def p_starbucks_promo(r: str) -> dict:
    tr = pd.read_csv(f"{r}/starbucks_promo/training.csv")
    te = pd.read_csv(f"{r}/starbucks_promo/Test.csv")
    return {
        "rows": len(tr) + len(te),
        "customers": int(pd.concat([tr["ID"], te["ID"]]).nunique()),
        "train_rows": len(tr),
        "test_rows": len(te),
        "arms": tr["Promotion"].value_counts().to_dict(),
        "purchase_rate": float(tr["purchase"].mean()),
        "note": "randomised promotion Yes/No, no dates, anonymised V1..V7",
    }


def p_starbucks_rewards(r: str) -> dict:
    d = f"{r}/starbucks_rewards"
    tr = pd.read_json(f"{d}/transcript.json", lines=True)
    pr = pd.read_json(f"{d}/profile.json", lines=True)
    po = pd.read_json(f"{d}/portfolio.json", lines=True)
    return {
        "rows": len(tr),
        "customers": int(pr["id"].nunique()),
        "event_persons": int(tr["person"].nunique()),
        "offers": len(po),
        "events": tr["event"].value_counts().to_dict(),
        "time_min_hours": int(tr["time"].min()),
        "time_max_hours": int(tr["time"].max()),
        "profile_age_118_rows": int((pr["age"] == 118).sum()),
        "note": "JSON lines, nested 'value' dict ('offer id' vs 'offer_id' keys differ by event); time = hours since start; SIMULATED",
    }


def p_cdnow(r: str) -> dict:
    df = pd.read_csv(
        f"{r}/cdnow/CDNOW_master.txt", sep=r"\s+", header=0, dtype={"customer_id": str, "date": str}
    )
    a, b, bad = _span(df["date"], "%Y%m%d")
    return {
        "rows": len(df),
        "customers": int(df["customer_id"].nunique()),
        "time_min": a,
        "time_max": b,
        "unparsed_dates": bad,
        "note": "whitespace-separated with a space-padded header, date YYYYMMDD, one row per customer-day",
    }


def p_nbr(r: str, name: str, stem: str) -> dict:
    h = pd.read_csv(f"{r}/{name}/{stem}_history.csv")
    f = pd.read_csv(f"{r}/{name}/{stem}_future.csv")
    return {
        "rows": len(h) + len(f),
        "history_rows": len(h),
        "future_rows": len(f),
        "customers": int(pd.concat([h["CUSTOMER_ID"], f["CUSTOMER_ID"]]).nunique()),
        "baskets_history": int(h.groupby(["CUSTOMER_ID", "ORDER_NUMBER"]).ngroups),
        "products": int(pd.concat([h["MATERIAL_NUMBER"], f["MATERIAL_NUMBER"]]).nunique()),
        "note": "preprocessed next-basket set: CUSTOMER_ID, ORDER_NUMBER, MATERIAL_NUMBER only; NO dates, NO prices",
    }


def p_open_bandit(r: str) -> dict:
    out: dict[str, Any] = {"rows": 0, "files": {}}
    for f in sorted(p.name for p in Path(f"{r}/open_bandit_sample").iterdir()):
        if not f.endswith(".csv"):
            continue
        df = pd.read_csv(f"{r}/open_bandit_sample/{f}", index_col=0)
        a, b, _ = _span(df["timestamp"].str.slice(0, 26), "%Y-%m-%d %H:%M:%S.%f")
        out["files"][f] = {
            "rows": len(df),
            "items": int(df["item_id"].nunique()),
            "ctr": float(df["click"].mean()),
            "propensity_min": float(df["propensity_score"].min()),
            "time_min": a,
            "time_max": b,
        }
        out["rows"] += len(df)
    out["customers"] = None
    out["note"] = (
        "logged bandit feedback with propensities; random_* = uniform-random logging policy; no user id (hashed features only)"
    )
    return out


def p_ta_feng(r: str) -> dict:
    with zipfile.ZipFile(f"{r}/ta_feng/ta-feng-grocery-dataset.zip") as z:
        df = pd.read_csv(
            z.open("ta_feng_all_months_merged.csv"), encoding="utf-8-sig", dtype={"CUSTOMER_ID": str}
        )
    a, b, bad = _span(df["TRANSACTION_DT"], "%m/%d/%Y")
    return {
        "rows": len(df),
        "customers": int(df["CUSTOMER_ID"].nunique()),
        "time_min": a,
        "time_max": b,
        "unparsed_dates": bad,
        "columns": list(df.columns),
        "note": "UTF-8 BOM, CRLF, every field quoted, ids with leading zeros, no order id (customer+date = basket)",
    }


def p_retailrocket(r: str) -> dict:
    with zipfile.ZipFile(f"{r}/retailrocket/retailrocket.zip") as z:
        df = pd.read_csv(z.open("retailrocket/events.csv"))
    a, b, _bad = _span(df["timestamp"], unit="ms")
    return {
        "rows": len(df),
        "customers": int(df["visitorid"].nunique()),
        "events": df["event"].value_counts().to_dict(),
        "transactions": int(df["transactionid"].nunique()),
        "time_min": a,
        "time_max": b,
        "note": "epoch-ms timestamps; no prices/amounts in events.csv (item_properties not in this mirror)",
    }


def p_rees46(r: str) -> dict:
    out: dict[str, Any] = {"rows": None, "customers": None}
    try:
        frames = [
            pd.read_parquet(f"{r}/rees46_sample/{f}")
            for f in sorted(p.name for p in Path(f"{r}/rees46_sample").iterdir())
            if f.endswith(".snappy")
        ]
        df = pd.concat(frames)
        out.update({"rows": len(df), "customers": int(df["user_id"].nunique())})
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"[:300]
    return out


def p_taobao(r: str) -> dict:
    d = f"{r}/taobao_sample"
    pv = pd.read_csv(f"{d}/pv.csv", sep="\t")
    cart = pd.read_csv(f"{d}/cart.csv", sep="\t")
    buy = pd.read_csv(f"{d}/buy.train.txt", sep="\t")
    users = pd.concat([pv["uid"], cart["uid"], buy["uid"]]).nunique()
    return {
        "rows": len(pv) + len(cart) + len(buy),
        "pv_rows": len(pv),
        "cart_rows": len(cart),
        "buy_rows": len(buy),
        "customers": int(users),
        "items": int(pd.concat([pv["sid"], cart["sid"], buy["sid"]]).nunique()),
        "note": "tab-separated, re-indexed ids, NO timestamps (column header '3' holds the behaviour type)",
    }


def p_superstore(r: str) -> dict:
    df = pd.read_csv(f"{r}/superstore/sample_superstore.csv")
    a, b, bad = _span(df["order_date"], "%Y-%m-%d")
    return {
        "rows": len(df),
        "customers": int(df["customer_name"].nunique()),
        "orders": int(df["order_id"].nunique()),
        "time_min": a,
        "time_max": b,
        "unparsed_dates": bad,
        "note": "customer identified only by NAME (no id); fictional",
    }


def p_telco(r: str) -> dict:
    df = pd.read_csv(f"{r}/telco_churn/Telco-Customer-Churn.csv")
    tc = pd.to_numeric(df["TotalCharges"], errors="coerce")
    return {
        "rows": len(df),
        "customers": int(df["customerID"].nunique()),
        "churn_rate": float((df["Churn"] == "Yes").mean()),
        "TotalCharges_blank": int(tc.isna().sum()),
        "note": "snapshot, no dates; TotalCharges is text with blanks",
    }


def p_bank(r: str) -> dict:
    df = pd.read_csv(f"{r}/bank_marketing/bank-additional-full.csv", sep=";")
    return {
        "rows": len(df),
        "customers": None,
        "y_rate": float((df["y"] == "yes").mean()),
        "months": df["month"].value_counts().to_dict(),
        "contact": df["contact"].value_counts().to_dict(),
        "note": "';' separated, no customer id, month/day_of_week but no year; outcome y; NOT randomised",
    }


def p_hins(r: str) -> dict:
    df = pd.read_csv(f"{r}/health_insurance_xsell/train.csv")
    return {
        "rows": len(df),
        "customers": int(df["id"].nunique()),
        "response_rate": float(df["Response"].mean()),
        "sales_channels": int(df["Policy_Sales_Channel"].nunique()),
        "note": "snapshot, no dates; not randomised",
    }


def p_hotels(r: str) -> dict:
    df = pd.read_csv(f"{r}/hotel_bookings/hotels.csv")
    a, b, bad = _span(df["reservation_status_date"], "%Y-%m-%d")
    return {
        "rows": len(df),
        "customers": None,
        "cancel_rate": float(df["is_canceled"].mean()),
        "time_min": a,
        "time_max": b,
        "unparsed_dates": bad,
        "countries": int(df["country"].nunique()),
        "note": "no guest id; arrival date split over year/month-name/day columns; 'NULL' strings for agent/company",
    }


def p_credit(r: str) -> dict:
    df = pd.read_csv(f"{r}/uci_credit_default/UCI_Credit_Card.csv")
    return {
        "rows": len(df),
        "customers": int(df["ID"].nunique()),
        "default_rate": float(df["default.payment.next.month"].mean()),
        "note": "snapshot of 6 monthly bills/payments as wide columns, no dates",
    }


def p_kdd98(r: str) -> dict:
    df = pd.read_csv(f"{r}/kdd98_sample/cup98LRN.txt", low_memory=False)
    return {
        "rows": len(df),
        "customers": int(df["CONTROLN"].nunique()),
        "n_columns": df.shape[1],
        "target_b_rate": float(df["TARGET_B"].mean()),
        "note": "SAMPLE of first rows only; dates as YYMM integers",
    }


PROFILERS: dict[str, Callable[[str], dict]] = {
    "hillstrom": p_hillstrom,
    "criteo_uplift": p_criteo,
    "lenta": p_lenta,
    "megafon": p_megafon,
    "x5_retailhero": p_x5,
    "starbucks_promo": p_starbucks_promo,
    "starbucks_rewards": p_starbucks_rewards,
    "open_bandit_sample": p_open_bandit,
    "olist": p_olist,
    "online_retail_i": p_online_retail_i,
    "online_retail_ii": p_online_retail_ii,
    "cdnow": p_cdnow,
    "ta_feng": p_ta_feng,
    "dunnhumby_nbr": lambda r: p_nbr(r, "dunnhumby_nbr", "Dunnhumby"),
    "instacart_nbr": lambda r: p_nbr(r, "instacart_nbr", "Instacart"),
    "superstore": p_superstore,
    "retailrocket": p_retailrocket,
    "rees46_sample": p_rees46,
    "taobao_sample": p_taobao,
    "telco_churn": p_telco,
    "bank_marketing": p_bank,
    "health_insurance_xsell": p_hins,
    "hotel_bookings": p_hotels,
    "uci_credit_default": p_credit,
    "kdd98_sample": p_kdd98,
}


def _default(o: Any) -> Any:
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    return str(o)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/home/user/datasets")
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    names = [n for n in a.only.split(",") if n] or list(PROFILERS)
    result: dict[str, Any] = {}
    out_path = Path(a.out)
    if out_path.exists():
        with out_path.open() as fh:
            result = json.load(fh)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    for n in names:
        t0 = time.perf_counter()
        try:
            res = PROFILERS[n](a.root)
        except Exception as e:
            res = {"error": f"{type(e).__name__}: {e}"[:500]}
        res["seconds"] = round(time.perf_counter() - t0, 2)
        res["peak_rss_mb_so_far"] = round(_rss_mb(), 1)
        result[n] = res
        print(n, json.dumps(res, default=_default)[:400], flush=True)
        with out_path.open("w") as fh:
            json.dump(result, fh, indent=1, default=_default)
    return 0


if __name__ == "__main__":
    sys.exit(main())
