"""A messy client file: the synthetic Targeted Advertisement data, written the way people write it.

Each mess is one real-world habit, and each maps to one fix the helper must propose:

* `ad_ctr_90d` as `"2.4%"` - numbers with a percent sign;
* `monthly_spend` as `"₹1,234.50"` / `"Rs. 1,234.50"` - currency and thousands separators;
* `signup_date` in three date styles, day first;
* `is_premium` as `Y` / `yes` / `N` / `No`;
* `plan_tier` in three cases, `region` with trailing spaces;
* `contact_email` - personal data;
* the generator's own leaky column, which almost perfectly predicts the outcome.

Pure: the same arguments give the same frame.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from tests.fixtures.make_data import GenerationSpec, generate

__all__ = ["messy_frame"]


def messy_frame(rows: int = 3_000, *, seed: int = 7, leaky: bool = True) -> pd.DataFrame:
    frame = generate(
        GenerationSpec(
            use_case_id="targeted-advertisement", rows=rows, variant="leaky_column" if leaky else "clean"
        )
    )
    rng = np.random.default_rng(seed)
    frame["ad_ctr_90d"] = [
        f"{value * 100:.1f}%" if pd.notna(value) else None for value in frame["ad_ctr_90d"]
    ]
    spend = rng.gamma(2.0, 600.0, size=rows)
    symbols = rng.choice(["₹", "Rs. "], size=rows)
    frame["monthly_spend"] = [f"{symbol}{value:,.2f}" for symbol, value in zip(symbols, spend, strict=True)]
    days = pd.Timestamp("2022-01-01") + pd.to_timedelta(rng.integers(0, 900, size=rows), unit="D")
    styles = rng.integers(0, 3, size=rows)
    frame["signup_date"] = [
        (
            day.strftime("%d/%m/%Y")
            if style == 0
            else day.strftime("%Y-%m-%d") if style == 1 else day.strftime("%d %b %Y")
        )
        for day, style in zip(days, styles, strict=True)
    ]
    frame["is_premium"] = rng.choice(["Y", "yes", "N", "No"], size=rows)
    frame["plan_tier"] = [
        value.upper() if i % 4 == 0 else value.title() if i % 4 == 1 else value
        for i, value in enumerate(frame["plan_tier"].astype(str))
    ]
    frame["region"] = [
        f"{value} " if i % 5 == 0 else value for i, value in enumerate(frame["region"].astype(str))
    ]
    frame["contact_email"] = [f"user{i}@example.com" for i in range(rows)]
    return frame
