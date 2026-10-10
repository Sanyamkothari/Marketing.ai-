"""Would extra columns (from the raw invoice log) help? Evaluated on the DEV rows only (sealed never read).

Extra columns, all computed from lines dated on or before the 2011-09-10 snapshot:
  dec2010_orders     invoices in December 2010 (the log starts 2010-12-01; a gift retailer's peak season)
  recency_over_gap   recency_days / days_between_orders_mean (how overdue the customer is for them)
  orders_last_180d   invoices in the 180 days up to the snapshot
  spend_last_180d    value of those invoices
  invoice_days       distinct days with an invoice
"""

import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, roc_auc_score
from sklearn.model_selection import RepeatedStratifiedKFold
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

warnings.filterwarnings("ignore")
DATA = Path(__file__).resolve().parent.parent / "data"
HERE = Path(__file__).resolve().parent
SNAP = pd.Timestamp("2011-09-10")

raw = pd.read_csv(
    DATA / "online-retail-dataset.csv",
    usecols=["InvoiceNo", "Quantity", "InvoiceDate", "UnitPrice", "CustomerID"],
)
raw = raw[raw["CustomerID"].notna()].copy()
raw["CustomerID"] = raw["CustomerID"].astype(int).astype(str)
raw["InvoiceDate"] = pd.to_datetime(raw["InvoiceDate"], format="%m/%d/%Y %H:%M")
raw = raw[
    (raw["InvoiceDate"] <= SNAP)
    & (~raw["InvoiceNo"].astype(str).str.startswith("C"))
    & (raw["Quantity"] > 0)
    & (raw["UnitPrice"] > 0)
]
raw["value"] = raw["Quantity"] * raw["UnitPrice"]
inv = (
    raw.groupby(["CustomerID", "InvoiceNo"])
    .agg(date=("InvoiceDate", "min"), value=("value", "sum"))
    .reset_index()
)
extra = pd.DataFrame(index=sorted(inv["CustomerID"].unique()))
extra["dec2010_orders"] = inv[inv["date"] < "2011-01-01"].groupby("CustomerID")["InvoiceNo"].nunique()
recent = inv[inv["date"] > SNAP - pd.Timedelta(days=180)]
extra["orders_last_180d"] = recent.groupby("CustomerID")["InvoiceNo"].nunique()
extra["spend_last_180d"] = recent.groupby("CustomerID")["value"].sum()
extra["invoice_days"] = inv.assign(day=inv["date"].dt.date).groupby("CustomerID")["day"].nunique()
extra = extra.fillna({"dec2010_orders": 0, "orders_last_180d": 0, "spend_last_180d": 0})

dev = pd.read_csv(DATA / "dev.csv", dtype={"customer_id": str})
dev = dev.join(extra, on="customer_id")
dev["recency_over_gap"] = dev["recency_days"] / dev["days_between_orders_mean"].replace(0, np.nan)
y = dev["reactivated_90d"].to_numpy()
base_cols = [
    c
    for c in dev.columns
    if c
    not in {"customer_id", "snapshot_date", "reactivated_90d", "country", *extra.columns, "recency_over_gap"}
]
new_cols = ["dec2010_orders", "orders_last_180d", "spend_last_180d", "invoice_days", "recency_over_gap"]
top = dev["country"].value_counts().index[:6]
dev["country"] = dev["country"].where(dev["country"].isin(top), "Other")


def make(kind: str, cols: list[str]) -> Pipeline:
    pre = ColumnTransformer(
        [
            ("n", Pipeline([("i", SimpleImputer(strategy="median")), ("s", StandardScaler())]), cols),
            ("c", OneHotEncoder(handle_unknown="ignore"), ["country"]),
        ]
    )
    head = (
        LogisticRegression(max_iter=2000)
        if kind == "lr"
        else HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=150, random_state=0)
    )
    return Pipeline([("p", pre), ("m", head)])


variants = {
    "lr base": ("lr", base_cols),
    "lr + extras": ("lr", base_cols + new_cols),
    "hgb base": ("hgb", base_cols),
    "hgb + extras": ("hgb", base_cols + new_cols),
}
cv = RepeatedStratifiedKFold(n_splits=5, n_repeats=4, random_state=7)
res = {k: [] for k in variants}
roc = {k: [] for k in variants}
for tr, te in cv.split(dev, y):
    for name, (kind, cols) in variants.items():
        m = make(kind, cols).fit(dev.iloc[tr][[*cols, "country"]], y[tr])
        p = m.predict_proba(dev.iloc[te][[*cols, "country"]])[:, 1]
        res[name].append(average_precision_score(y[te], p))
        roc[name].append(roc_auc_score(y[te], p))
b = np.array(res["lr base"])
print(f"{'variant':14} {'PR-AUC':>8} {'ROC':>7} {'dPR vs lr base':>15}")
for k, v in res.items():
    v = np.array(v)
    print(f"{k:14} {v.mean():8.4f} {np.mean(roc[k]):7.4f} {(v - b).mean():+15.4f}")
print("dev positive rate", y.mean().round(4), "rows", len(y))
for c in new_cols:
    s = dev[c]
    print(
        c,
        "corr with target",
        round(float(pd.concat([s, pd.Series(y, index=s.index)], axis=1).corr().iloc[0, 1]), 3),
    )
