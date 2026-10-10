"""Development experiments on the DEV rows only (the sealed 20% is never read here)."""

import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import ExtraTreesClassifier, HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, roc_auc_score
from sklearn.model_selection import RepeatedStratifiedKFold
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

warnings.filterwarnings("ignore")
DATA = Path(__file__).resolve().parent.parent / "data"
dev = pd.read_csv(DATA / "dev.csv", dtype={"customer_id": str})
y = dev["reactivated_90d"].to_numpy()
X = dev.drop(columns=["customer_id", "snapshot_date", "reactivated_90d"])
num = [c for c in X.columns if c != "country"]
# country has many rare levels: keep the frequent ones
top = X["country"].value_counts().index[:6]
X["country"] = X["country"].where(X["country"].isin(top), "Other")


def pre(scale: bool) -> ColumnTransformer:
    num_steps = [("imp", SimpleImputer(strategy="median"))] + ([("sc", StandardScaler())] if scale else [])
    return ColumnTransformer(
        [("n", Pipeline(num_steps), num), ("c", OneHotEncoder(handle_unknown="ignore"), ["country"])]
    )


def logx(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    for c in num:
        if (out[c] >= 0).all():
            out[c] = np.log1p(out[c])
    return out


MODELS = {
    "lr": lambda: Pipeline([("p", pre(True)), ("m", LogisticRegression(max_iter=2000))]),
    "lr_c0.1": lambda: Pipeline([("p", pre(True)), ("m", LogisticRegression(max_iter=2000, C=0.1))]),
    "rf": lambda: Pipeline(
        [("p", pre(False)), ("m", RandomForestClassifier(300, min_samples_leaf=5, n_jobs=2, random_state=0))]
    ),
    "et": lambda: Pipeline(
        [("p", pre(False)), ("m", ExtraTreesClassifier(300, min_samples_leaf=5, n_jobs=2, random_state=0))]
    ),
    "hgb": lambda: Pipeline(
        [
            ("p", pre(False)),
            (
                "m",
                HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=150, random_state=0),
            ),
        ]
    ),
}

cv = RepeatedStratifiedKFold(n_splits=5, n_repeats=5, random_state=7)
rows = {k: {"pr": [], "roc": []} for k in MODELS}
rows["lr_log"] = {"pr": [], "roc": []}
rows["prior"] = {"pr": [], "roc": []}
for tr, te in cv.split(X, y):
    for name, make in MODELS.items():
        m = make().fit(X.iloc[tr], y[tr])
        p = m.predict_proba(X.iloc[te])[:, 1]
        rows[name]["pr"].append(average_precision_score(y[te], p))
        rows[name]["roc"].append(roc_auc_score(y[te], p))
    Xl = logx(X.drop(columns="country")).assign(country=X["country"])
    m = Pipeline([("p", pre(True)), ("m", LogisticRegression(max_iter=2000))]).fit(Xl.iloc[tr], y[tr])
    p = m.predict_proba(Xl.iloc[te])[:, 1]
    rows["lr_log"]["pr"].append(average_precision_score(y[te], p))
    rows["lr_log"]["roc"].append(roc_auc_score(y[te], p))
    rows["prior"]["pr"].append(y[te].mean())
    rows["prior"]["roc"].append(0.5)

base = np.array(rows["lr"]["pr"])
print(f"{'model':8} {'PR-AUC':>8} {'ROC':>7} {'dPR vs lr':>10} {'wins/25':>8}")
for k, v in rows.items():
    pr = np.array(v["pr"])
    d = pr - base
    print(f"{k:8} {pr.mean():8.4f} {np.mean(v['roc']):7.4f} {d.mean():+10.4f} {(d > 0).sum():8d}")
