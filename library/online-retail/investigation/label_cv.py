"""Step 3b of the M109 investigation: would a different label or window help? Dev shoppers only.

`fetch.py` fixes three settings: the snapshot (2011-09-10), the lapse rule (no purchase for 90 days) and the
outcome window (back within 90 days). This script rebuilds the win-back table with other values, using
`fetch.build_rows` itself and leaves out the 293 sealed shoppers (the ids in `sample.csv` that are not in
`dev.csv`; `sealed.csv` is not opened and no sealed label is used). A different label or window makes a
different set of lapsed shoppers, so the rows are the dev shoppers plus any shopper the sample never held. For
each setting it cross-validates the baseline (logistic regression) against gradient boosting on the same
folds and reports the difference in PR-AUC. The PR-AUC of different labels cannot be compared with each other
(each label has its own base rate), so what is read is the model-minus-baseline difference, per setting.

The log ends on 2011-12-09, so a longer outcome window needs an earlier snapshot, and that shortens the
history every feature is computed from. Both are shown together: the two cannot be separated in this file.
"""

import importlib.util
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score
from sklearn.model_selection import RepeatedStratifiedKFold
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

warnings.filterwarnings("ignore")
HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("retail_fetch", HERE.parent / "fetch.py")
assert spec is not None and spec.loader is not None
fetch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetch)

dev_ids = set(pd.read_csv(fetch.DATA / "dev.csv", dtype={"customer_id": str})["customer_id"])
sample_ids = set(
    pd.read_csv(fetch.SAMPLE, dtype={"customer_id": str}, usecols=["customer_id"])["customer_id"]
)
sealed_ids = sample_ids - dev_ids  # the ids only: no sealed row or label is read
transactions = fetch.read_transactions()

# (label, snapshot, lapse days, outcome days)
SETTINGS = [
    ("as built: snapshot 10 Sep, lapse 90, outcome 90", "2011-09-10", 90, 90),
    ("lapse 60 days", "2011-09-10", 60, 90),
    ("lapse 120 days", "2011-09-10", 120, 90),
    ("outcome 60 days", "2011-09-10", 90, 60),
    ("outcome 30 days", "2011-09-10", 90, 30),
    ("snapshot 11 Aug, outcome 90", "2011-08-11", 90, 90),
    ("snapshot 11 Aug, outcome 120", "2011-08-11", 90, 120),
    ("snapshot 11 Jul, outcome 150", "2011-07-11", 90, 150),
]


def make(kind: str, num: list[str]) -> Pipeline:
    steps = [("i", SimpleImputer(strategy="median"))] + ([("s", StandardScaler())] if kind == "lr" else [])
    pre = ColumnTransformer(
        [("n", Pipeline(steps), num), ("c", OneHotEncoder(handle_unknown="ignore"), ["country"])]
    )
    head = (
        LogisticRegression(max_iter=2000)
        if kind == "lr"
        else HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=150, random_state=0)
    )
    return Pipeline([("p", pre), ("m", head)])


print(f"{'setting':50} {'rows':>5} {'pos%':>6} {'LR PR':>7} {'GB PR':>7} {'GB-LR':>7} {'folds GB>LR':>12}")
for label, snapshot, lapse, outcome in SETTINGS:
    fetch.SNAPSHOT = pd.Timestamp(snapshot)
    fetch.LAPSE_DAYS = lapse
    fetch.OUTCOME_DAYS = outcome
    fetch.OUTCOME_END = fetch.SNAPSHOT + pd.Timedelta(days=outcome)
    rows = fetch.build_rows(transactions)
    rows = rows[~rows["customer_id"].isin(sealed_ids)].reset_index(drop=True)
    y = rows[fetch.TARGET].to_numpy()
    top = rows["country"].value_counts().index[:6]
    rows["country"] = rows["country"].where(rows["country"].isin(top), "Other")
    X = rows.drop(columns=["customer_id", "snapshot_date", fetch.TARGET])
    num = [c for c in X.columns if c != "country"]
    cv = RepeatedStratifiedKFold(n_splits=5, n_repeats=3, random_state=7)
    lr, gb = [], []
    for tr, te in cv.split(X, y):
        for store, kind in ((lr, "lr"), (gb, "hgb")):
            p = make(kind, num).fit(X.iloc[tr], y[tr]).predict_proba(X.iloc[te])[:, 1]
            store.append(average_precision_score(y[te], p))
    lr_a, gb_a = np.array(lr), np.array(gb)
    print(
        f"{label:50} {len(rows):5d} {y.mean() * 100:6.1f} {lr_a.mean():7.4f} {gb_a.mean():7.4f} "
        f"{(gb_a - lr_a).mean():+7.4f} {(gb_a > lr_a).sum():9d}/15"
    )
