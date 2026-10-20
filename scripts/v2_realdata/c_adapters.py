"""Family C adapters: the code a customer would write to get an event-stream export into Autonomy.

One function per dataset. Each returns a pandas frame plus the `EventMapping` that the product's own
`map_frame` turns into `Event`s. Where the file cannot be mapped as published, the "as published"
mapping is kept too, so the runners can show the exact problem a customer would hit first.

* **RetailRocket** (`retailrocket/retailrocket.zip`, member `retailrocket/events.csv`): 2,756,101
  rows `timestamp,visitorid,event,itemid,transactionid`. Two things stop it as published:
  the time is epoch **milliseconds** (the mapping reads ISO 8601 or one `strptime` format, so every
  row is `bad_timestamp`), and a `transaction` has **no amount** (a `purchase` event must carry a
  `value`, so every transaction row is `invalid_event`). The adapter converts the time with
  `pd.to_datetime(unit="ms", utc=True)` and puts a **placeholder value of 0.0** on transactions,
  labelled `attrs.value_placeholder=True`. Revenue is therefore 0 for everyone: a real customer would
  join their order amounts here.
* **Taobao subset** (`taobao_sample/`): `uid, sid, <behaviour>` with **no time column at all**. An
  `Event` needs a time, so this file cannot become events without inventing one.
* **REES46** (`rees46_sample/*.parquet.snappy`): parquet; pandas cannot read it without pyarrow or
  fastparquet, neither installed. The 170-row CSV sample (`rees46_csv_tiny/`) can be read, but its
  time is written `2019-10-01 00:00:00 UTC`, which ISO parsing refuses (the ` UTC` suffix); the
  adapter passes `timestamp_format="%Y-%m-%d %H:%M:%S UTC"`. Its `remove_from_cart` has no Autonomy
  event type.

Python 3.11 compatible.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pandas as pd
from c_common import DATASETS

from autonomy.contracts import EventType
from autonomy.data.mapping import EventMapping

RR_ZIP = DATASETS / "retailrocket" / "retailrocket.zip"
RR_MEMBER = "retailrocket/events.csv"
RR_ROWS = 2_756_101

RR_MAPPING_AS_PUBLISHED = EventMapping(
    customer_id="visitorid",
    timestamp="timestamp",
    event_type_column="event",
    event_type_values={
        "view": EventType.VIEW,
        "addtocart": EventType.CART,
        "transaction": EventType.PURCHASE,
    },
    item_column="itemid",
    timezone="UTC",
)
"""What a customer would write first, straight off the file's columns."""

RR_MAPPING = EventMapping(
    customer_id="visitorid",
    timestamp="ts",
    event_type_column="event",
    event_type_values={
        "view": EventType.VIEW,
        "addtocart": EventType.CART,
        "transaction": EventType.PURCHASE,
    },
    item_column="itemid",
    value_column="value",
    attrs={"transaction_id": "transactionid", "value_placeholder": "value_placeholder"},
    timezone="UTC",
    row_key=("timestamp", "visitorid", "event", "itemid"),
)
"""After the adapter: time converted from epoch ms, a 0.0 placeholder amount on transactions."""

RR_MAPPING_ORDERS = RR_MAPPING.model_copy(
    update={
        "attrs": {**RR_MAPPING.attrs, "items": "items"},
        "row_key": ("visitorid", "event", "row_id"),
    }
)
"""With `adapt_retailrocket(frame, grain="order")`: one purchase event per transaction id."""


def read_retailrocket(nrows: int | None = None) -> pd.DataFrame:
    """The first `nrows` rows of RetailRocket's events.csv, in file order (not sorted by time)."""
    with zipfile.ZipFile(RR_ZIP) as zf, zf.open(RR_MEMBER) as fh:
        return pd.read_csv(
            fh,
            nrows=nrows,
            dtype={"timestamp": "int64", "visitorid": "int64", "event": "string", "itemid": "int64"},
        )


def adapt_retailrocket(frame: pd.DataFrame, grain: str = "line") -> pd.DataFrame:
    """Add `ts` (aware UTC) from epoch ms and a placeholder `value` on transactions (labelled).

    `grain="line"` (the default, what the scale runs in `c_ingest.py` measured) keeps one row per
    item line, so a transaction of three items becomes three purchase events and `purchases` counts
    lines, not orders. `grain="order"` collapses each `transactionid` to one row at its earliest
    timestamp (with an `items` count), which is what customer state wants (the product's own
    online-retail loader says the same: one event per invoice): 22,457 transaction rows are 17,672
    orders, and 1,063 visitors (not 2,576) have two or more orders. Use it with `RR_MAPPING_ORDERS`.
    """
    if grain not in ("line", "order"):
        raise ValueError("grain is 'line' or 'order'")
    if grain == "order":
        frame = _collapse_transactions(frame)
    out = frame.copy()
    out["ts"] = pd.to_datetime(out["timestamp"], unit="ms", utc=True)
    is_tx = out["event"] == "transaction"
    out["value"] = pd.Series(float("nan"), index=out.index).where(~is_tx, 0.0)
    out["value_placeholder"] = is_tx.where(is_tx, other=pd.NA).astype("boolean")
    out["transactionid"] = out["transactionid"].astype("Int64").astype("string")
    if grain == "order":
        key = out["timestamp"].astype("string") + "/" + out["itemid"].astype("string")
        out["row_id"] = ("tx:" + out["transactionid"]).where(is_tx, key)
    return out


def _collapse_transactions(frame: pd.DataFrame) -> pd.DataFrame:
    """One row per (visitor, transaction id) at the earliest of its lines' times; `items` = lines."""
    is_tx = frame["event"] == "transaction"
    tx = frame[is_tx]
    orders = (
        tx.sort_values("timestamp", kind="stable")
        .groupby(["visitorid", "transactionid"], as_index=False, sort=False)
        .agg(
            timestamp=("timestamp", "min"),
            event=("event", "first"),
            itemid=("itemid", "first"),
            items=("itemid", "size"),
        )
    )
    rest = frame[~is_tx].assign(items=pd.NA)
    return pd.concat([rest, orders[rest.columns]], ignore_index=True)


TAOBAO_DIR = DATASETS / "taobao_sample"


def read_taobao() -> pd.DataFrame:
    """pv, cart and buy rows stacked, as published: uid, sid, behaviour. No time column exists."""
    pv = pd.read_csv(TAOBAO_DIR / "pv.csv", sep="\t", dtype=str)
    cart = pd.read_csv(TAOBAO_DIR / "cart.csv", sep="\t", dtype=str)
    buy = pd.read_csv(TAOBAO_DIR / "buy.train.txt", sep="\t", dtype=str)
    pv.columns = cart.columns = ["uid", "sid", "behaviour"]
    buy.columns = ["uid", "sid"]
    buy["behaviour"] = "buy"
    return pd.concat([pv, cart, buy], ignore_index=True)


TAOBAO_MAPPING = EventMapping(
    customer_id="uid",
    timestamp="time",  # the file has none: this is the column a customer would need to add
    event_type_column="behaviour",
    event_type_values={"pv": EventType.VIEW, "cart": EventType.CART, "buy": EventType.PURCHASE},
    item_column="sid",
)

REES46_PARQUET = sorted((DATASETS / "rees46_sample").glob("*.parquet*"))
REES46_CSV = sorted((DATASETS / "rees46_csv_tiny").glob("*.csv"))

REES46_MAPPING_ISO = EventMapping(
    customer_id="user_id",
    timestamp="event_time",
    event_type_column="event_type",
    event_type_values={"view": EventType.VIEW, "cart": EventType.CART, "purchase": EventType.PURCHASE},
    item_column="product_id",
    category_column="category_code",
    value_column="price",
)
"""ISO parsing, as a customer would try first."""

REES46_MAPPING = REES46_MAPPING_ISO.model_copy(update={"timestamp_format": "%Y-%m-%d %H:%M:%S UTC"})


def read_rees46_csv() -> pd.DataFrame:
    return pd.concat([pd.read_csv(p, dtype=str) for p in REES46_CSV], ignore_index=True)


def read_rees46_parquet(path: Path) -> pd.DataFrame:
    return pd.read_parquet(path)
