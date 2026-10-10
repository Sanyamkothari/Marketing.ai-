"""The look at the sealed 20%: every model fitted on DEV, scored on SEALED. Nothing is chosen from it.

All five candidates are reported whatever they score; the baseline is the engine's own recipe (median impute,
standardise, one-hot country, logistic regression). The paired bootstrap resamples the sealed rows.

The first look had the baseline and three other families (random forest, extra trees, gradient boosting). Review
pointed out that logistic regression with stronger shrinkage (`C=0.1`), the only candidate ahead of the baseline
in most dev folds (16 of 25), had been left out; it was added to the same run, so the other rows are the ones
already reported (same seed, same fits). The extra-column variants of `feat_cv.py` are not in it: they need the
raw invoice log and were not ahead of the baseline on dev. It is a second look at the same sealed rows, said so
in `run_report.md`.
"""

import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import ExtraTreesClassifier, HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

warnings.filterwarnings("ignore")
DATA = Path(__file__).resolve().parent.parent / "data"
HERE = Path(__file__).resolve().parent
dev = pd.read_csv(DATA / "dev.csv", dtype={"customer_id": str})
sealed = pd.read_csv(DATA / "sealed.csv", dtype={"customer_id": str})
drop = ["customer_id", "snapshot_date", "reactivated_90d"]
top = dev["country"].value_counts().index[:6]
for f in (dev, sealed):
    f["country"] = f["country"].where(f["country"].isin(top), "Other")
cols = [c for c in dev.columns if c not in drop]
num = [c for c in cols if c != "country"]


def pre(scale: bool) -> ColumnTransformer:
    steps = [("i", SimpleImputer(strategy="median"))] + ([("s", StandardScaler())] if scale else [])
    return ColumnTransformer(
        [("n", Pipeline(steps), num), ("c", OneHotEncoder(handle_unknown="ignore"), ["country"])]
    )


models = {
    "baseline (logistic regression)": Pipeline([("p", pre(True)), ("m", LogisticRegression(max_iter=2000))]),
    "logistic regression, stronger shrinkage": Pipeline(
        [("p", pre(True)), ("m", LogisticRegression(max_iter=2000, C=0.1))]
    ),
    "random forest": Pipeline(
        [("p", pre(False)), ("m", RandomForestClassifier(500, min_samples_leaf=5, n_jobs=2, random_state=0))]
    ),
    "extra trees": Pipeline(
        [("p", pre(False)), ("m", ExtraTreesClassifier(500, min_samples_leaf=5, n_jobs=2, random_state=0))]
    ),
    "gradient boosting": Pipeline(
        [
            ("p", pre(False)),
            (
                "m",
                HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=150, random_state=0),
            ),
        ]
    ),
}
y_dev, y = dev["reactivated_90d"].to_numpy(), sealed["reactivated_90d"].to_numpy()
scores = {}
for name, m in models.items():
    m.fit(dev[cols], y_dev)
    scores[name] = m.predict_proba(sealed[cols])[:, 1]
rng = np.random.default_rng(1319)
n = len(y)
idx = [rng.integers(0, n, n) for _ in range(2000)]
idx219 = [rng.integers(0, n, 219) for _ in range(2000)]
base = scores["baseline (logistic regression)"]
print(f"sealed rows {n}, positive rate {y.mean():.4f}; prior PR-AUC = {y.mean():.4f}")
print(f"{'model':40} {'PR-AUC':>7} {'ROC':>7} {'d PR-AUC':>9} {'95% interval':>18} {'SD on 219 rows':>15}")
for name, p in scores.items():
    pr, roc = average_precision_score(y, p), roc_auc_score(y, p)
    if name.startswith("baseline"):
        print(f"{name:40} {pr:7.4f} {roc:7.4f}")
        continue
    d = np.array(
        [
            average_precision_score(y[i], p[i]) - average_precision_score(y[i], base[i])
            for i in idx
            if y[i].min() != y[i].max()
        ]
    )
    d219 = np.array(
        [average_precision_score(y[i], p[i]) - average_precision_score(y[i], base[i]) for i in idx219]
    )
    lo, hi = np.percentile(d, [2.5, 97.5])
    print(
        f"{name:40} {pr:7.4f} {roc:7.4f} {pr - average_precision_score(y, base):+9.4f} [{lo:+.4f}, {hi:+.4f}] {d219.std():15.4f}"
    )
