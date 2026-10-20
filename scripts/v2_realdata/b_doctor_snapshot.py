"""Family B, review add-on: how much of the Doctor's "future dates" verdict is the snapshot's doing.

`b_ingest.py` profiles every file with `snapshot` = 10:00 local on the day after the last *mapped*
purchase. On Olist that is earlier than the export itself (cancelled orders run later, and every
delivery and carrier date of the last orders falls after the last purchase), and on Superstore every
ship date of the last orders is after it. This profiles the same bytes three ways:

1. no snapshot (the route's default: the field is optional, and then no future check runs);
2. the runner's snapshot (as `b_ingest.py`);
3. the day after the latest *realised* timestamp in the file (purchase, approval, carrier and
   delivery dates for Olist; order and ship dates for Superstore): the moment the export describes.

It also counts what the Doctor does not mention on Online Retail I (blank CustomerID, negative
quantities, zero prices), and whether a YYYYMMDD value matches the Doctor's date shape.

Run:

    python3 -P scripts/v2_realdata/b_doctor_snapshot.py

Results: /home/user/datasets/_results/b/doctor_snapshot/all.json
"""

from __future__ import annotations

import gzip
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
for _p in (str(HERE), str(HERE.parents[1])):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pandas as pd  # noqa: E402
from b_adapters import adapt  # noqa: E402
from b_common import DATASETS, Clock, command_line, env_info, write_result  # noqa: E402
from b_ingest import decision_moment  # noqa: E402

from api.v2.engines.doctor import _DATE_SHAPE, profile_frame, read_csv_bytes  # noqa: E402


def _profile(frame: pd.DataFrame, snapshot: datetime | None) -> dict[str, Any]:
    r = profile_frame(frame, snapshot=snapshot, use_v1=False)
    future = {d.column: d.future_dated for d in r.temporal_integrity.date_columns if d.future_dated}
    return {
        "snapshot": snapshot,
        "readiness_score": r.readiness_score,
        "problems": [f"{p.code}/{p.severity}" for p in r.problems],
        "future_dated_by_column": future,
    }


def _three_ways(frame: pd.DataFrame, runner: datetime, realised: list[str]) -> dict[str, Any]:
    latest = max(pd.to_datetime(frame[c], errors="coerce").max() for c in realised)
    export = (latest + timedelta(days=1)).to_pydatetime().replace(tzinfo=UTC)
    return {
        "no_snapshot": _profile(frame, None),
        "runner_snapshot": _profile(frame, runner),
        "latest_realised_plus_1d": _profile(frame, export),
        "latest_realised_timestamp": latest,
        "realised_columns": realised,
    }


def main() -> int:
    out: dict[str, Any] = {"command": command_line(), "env": env_info()}
    olist = adapt("olist", Clock())
    runner = decision_moment(olist.order_events or [], olist.timezone)
    d = DATASETS / "olist"
    orders = read_csv_bytes(gzip.decompress((d / "olist_orders_dataset.csv.gz").read_bytes()))
    out["olist_orders"] = _three_ways(
        orders,
        runner,
        [
            "order_purchase_timestamp",
            "order_approved_at",
            "order_delivered_carrier_date",
            "order_delivered_customer_date",
        ],
    )
    items = read_csv_bytes(gzip.decompress((d / "olist_order_items_dataset.csv.gz").read_bytes()))
    out["olist_items"] = {"no_snapshot": _profile(items, None), "runner_snapshot": _profile(items, runner)}
    del olist

    ss = adapt("superstore", Clock())
    ss_runner = decision_moment(ss.order_events or [], ss.timezone)
    raw = read_csv_bytes((DATASETS / "superstore" / "sample_superstore.csv").read_bytes())
    out["superstore"] = _three_ways(raw, ss_runner, ["order_date", "ship_date"])

    ori = pd.read_csv(
        DATASETS / "online_retail_i" / "online-retail-dataset.csv",
        dtype=str,
        keep_default_na=False,
        na_values=[""],
    )
    out["online_retail_i_unflagged"] = {
        "rows": len(ori),
        "blank_customer_id": int(ori["CustomerID"].isna().sum()),
        "negative_quantity": int((pd.to_numeric(ori["Quantity"], errors="coerce") < 0).sum()),
        "zero_price": int((pd.to_numeric(ori["UnitPrice"], errors="coerce") == 0).sum()),
    }
    out["yyyymmdd_matches_date_shape"] = bool(_DATE_SHAPE.fullmatch("19970101"))
    path = write_result("doctor_snapshot", "all", out)
    for key in ("olist_orders", "superstore"):
        for variant in ("no_snapshot", "runner_snapshot", "latest_realised_plus_1d"):
            v = out[key][variant]
            print(f"{key} {variant}: score {v['readiness_score']} future {v['future_dated_by_column']}")
    for variant in ("no_snapshot", "runner_snapshot"):
        v = out["olist_items"][variant]
        print(f"olist_items {variant}: score {v['readiness_score']} future {v['future_dated_by_column']}")
    print(out["online_retail_i_unflagged"], "YYYYMMDD date shape:", out["yyyymmdd_matches_date_shape"])
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
