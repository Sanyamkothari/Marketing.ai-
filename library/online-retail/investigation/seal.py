"""Step 1 of the M109 investigation: seal 20% of the committed prepared file before any experiment.

Stratified, fixed seed. `dev.csv` (80%) is all that `dev_cv.py` and `feat_cv.py` read; `sealed.csv` (20%) is read
once, by `sealed_eval.py`, after the candidates are fixed. Both go to `data/`, which is not committed.
"""

from pathlib import Path

import pandas as pd
from sklearn.model_selection import train_test_split

OUT = Path(__file__).resolve().parent.parent / "data"
SRC = Path(__file__).resolve().parent.parent / "sample.csv"
OUT.mkdir(exist_ok=True)
df = pd.read_csv(SRC, dtype={"customer_id": str})
dev, sealed = train_test_split(df, test_size=0.20, stratify=df["reactivated_90d"], random_state=1319)
dev.to_csv(OUT / "dev.csv", index=False)
sealed.to_csv(OUT / "sealed.csv", index=False)
print(
    len(dev), len(sealed), round(dev["reactivated_90d"].mean(), 4), round(sealed["reactivated_90d"].mean(), 4)
)
