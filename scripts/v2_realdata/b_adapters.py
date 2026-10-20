"""Family-B adapters: each public transaction dataset read and mapped into Autonomy's input.

This is what a customer would have to write: read the export (CSV, zip, gzip, whitespace text), join
tables where the person id and the money live in different files, and state an `EventMapping`
(`autonomy/data/mapping.py`). Nothing here changes product code; where a dataset cannot be mapped
without inventing facts, the adapter says so in `notes` / `cannot` instead of inventing them
silently. Invented facts (consent) are added only by the Shadow runner, and only in its labelled
"consent invented" variant.

Two grains per dataset:

- **order grain** (`order_events`): one PURCHASE (or REFUND) per order, which is what customer state
  wants (`purchases` counts orders, as the repo's online-retail loader does);
- **line grain** (`purchases`, `baskets`): one row per order line with its `item_id`, which is what
  the catalogue needs. Built from line-level `Event`s through the product's own
  `purchases_frame` / `basket_frame`, so the catalogue sees exactly what the runtime would.

Not a runner. `b_ingest.py`, `b_catalogue.py` and `b_shadow.py` call `adapt(name)`.
"""

from __future__ import annotations

import gzip
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from b_common import DATASETS, Clock

from autonomy.catalog.products import purchases_frame
from autonomy.catalog.recommend import BASKET_COLUMNS, basket_frame
from autonomy.contracts import Event, EventType
from autonomy.data.loaders import online_retail_log_events, read_online_retail_log
from autonomy.data.mapping import EventMapping, MappingProblem, map_frame, problem_counts

FAMILY_B = (
    "online_retail_i",
    "online_retail_ii",
    "olist",
    "cdnow",
    "ta_feng",
    "superstore",
    "instacart_nbr",
    "dunnhumby_nbr",
)


@dataclass
class DoctorInput:
    """One file as a customer would upload it to the Data Doctor (`/v2/engines/doctor/profile`)."""

    label: str
    path: Path
    read_bytes: Callable[[], bytes]
    """The bytes the customer would upload (decompressed if the published file is zipped)."""
    as_published: str
    """What the published file is: 'csv', 'csv.gz', 'zip', 'whitespace text', ..."""
    delimiter: str = ","
    primary_key: str | None = None
    date_columns: tuple[str, ...] | None = None


@dataclass
class Adapted:
    name: str
    currency: str
    timezone: str
    synthetic: bool
    order_events: list[Event] | None
    order_problems: dict[str, int]
    order_rows: int
    purchases: pd.DataFrame | None
    """Line-grain purchase table (`PURCHASE_COLUMNS`) for the cycle model; None without items or dates."""
    baskets: pd.DataFrame | None
    """Line-grain basket table (`BASKET_COLUMNS`) for the recommender."""
    line_rows: int = 0
    line_problems: dict[str, int] = field(default_factory=dict)
    doctor: list[DoctorInput] = field(default_factory=list)
    mappings: dict[str, Any] = field(default_factory=dict)
    """The EventMapping(s) used, as JSON: what a customer would post to `map-events`."""
    notes: list[str] = field(default_factory=list)
    cannot: list[str] = field(default_factory=list)
    """What a customer could not do here without writing code."""
    facts: dict[str, Any] = field(default_factory=dict)
    """Dataset-level facts measured by the adapter (duplicates, statuses, ...)."""
    invented_time: bool = False
    """True when the basket table's times are invented (no dates in the data)."""


def _gz(path: Path) -> Callable[[], bytes]:
    return lambda: gzip.decompress(path.read_bytes())


def _zip_member(path: Path, member: str) -> Callable[[], bytes]:
    def read() -> bytes:
        with zipfile.ZipFile(path) as z:
            return z.read(member)

    return read


def _counts(problems: list[MappingProblem]) -> dict[str, int]:
    return problem_counts(problems)


def _line_tables(events: list[Event]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The product's own purchase and basket tables from line-level events."""
    return purchases_frame(events), basket_frame(events)


# --------------------------------------------------------------------------- Online Retail I


def adapt_online_retail_i(clock: Clock) -> Adapted:
    path = DATASETS / "online_retail_i" / "online-retail-dataset.csv"
    with clock.step("read"):
        log = read_online_retail_log(path)
    with clock.step("map_order_grain", trace=True) as s:
        order = online_retail_log_events(log, grain="invoice")
        s.extra = {"events": len(order.events)}
    with clock.step("map_line_grain", trace=True) as s:
        line = online_retail_log_events(log, grain="line")
        s.extra = {"events": len(line.events)}
    purchases, baskets = _line_tables(line.events)
    n_line = len(line.events)
    del line
    return Adapted(
        name="online_retail_i",
        currency="GBP",
        timezone="Europe/London",
        synthetic=False,
        order_events=order.events,
        order_problems=_counts(order.problems),
        order_rows=len(log),
        purchases=purchases,
        baskets=baskets,
        line_rows=n_line,
        line_problems={},
        doctor=[DoctorInput("online-retail-dataset.csv", path, path.read_bytes, "csv", date_columns=None)],
        mappings={"loader": "autonomy.data.loaders.online_retail_log_events (built in)"},
        notes=[
            "Read with the repository's own loader (online_retail_log_events), the only family-B "
            "dataset with a ready loader. No adapter code was needed.",
            "Line grain keeps non-product StockCodes (POST, DOT, M, BANK CHARGES, ...) as purchases; "
            "the catalogue validation in docs/v2/CATALOG.md drops them with clean_retail_lines, the "
            "event path does not (see b_catalogue's 'repo-clean' vs 'events' rows).",
        ],
    )


# --------------------------------------------------------------------------- Online Retail II

_OR2_RENAME = {"Invoice": "InvoiceNo", "Price": "UnitPrice", "Customer ID": "CustomerID"}


def or2_to_loader_frame(raw: pd.DataFrame) -> pd.DataFrame:
    """Online Retail II's columns as the built-in Online Retail loader wants them.

    The loader insists on '%m/%d/%Y %H:%M' (the UCI/databricks mirror's format); this file writes
    ISO. A customer cannot pass another format to the loader, so the column is rewritten."""
    log = raw.rename(columns=_OR2_RENAME)[
        ["InvoiceNo", "StockCode", "Quantity", "InvoiceDate", "UnitPrice", "CustomerID", "Country"]
    ].copy()
    log["InvoiceDate"] = pd.to_datetime(log["InvoiceDate"]).dt.strftime("%m/%d/%Y %H:%M")
    return log


def adapt_online_retail_ii(clock: Clock) -> Adapted:
    path = DATASETS / "online_retail_ii" / "online_retail_II.csv"
    with clock.step("read"):
        raw = pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[""])
    with clock.step("adapter_rename_and_redate"):
        log = or2_to_loader_frame(raw)
    dup_mask = raw.duplicated(keep="first")
    q = pd.to_numeric(raw["Quantity"], errors="coerce")
    p = pd.to_numeric(raw["Price"], errors="coerce")
    known = raw["Customer ID"].notna() & (p > 0)
    gross = (q * p)[known & (q > 0)]
    facts = {
        "exact_duplicate_rows": int(dup_mask.sum()),
        "duplicate_share_of_known_customer_sales_value": round(
            float(gross[dup_mask[gross.index]].sum() / gross.sum()), 4
        ),
    }
    with clock.step("map_order_grain", trace=True) as s:
        order = online_retail_log_events(log, grain="invoice")
        s.extra = {"events": len(order.events)}
    with clock.step("map_line_grain", trace=True) as s:
        line = online_retail_log_events(log, grain="line")
        s.extra = {"events": len(line.events)}
    with clock.step("line_tables"):
        purchases, baskets = _line_tables(line.events)
    n_line = len(line.events)
    del line
    return Adapted(
        name="online_retail_ii",
        currency="GBP",
        timezone="Europe/London",
        synthetic=False,
        order_events=order.events,
        order_problems=_counts(order.problems),
        order_rows=len(log),
        purchases=purchases,
        baskets=baskets,
        line_rows=n_line,
        doctor=[
            DoctorInput("online_retail_II.csv", path, path.read_bytes, "csv"),
            DoctorInput(
                "online_retail_II.xlsx",
                DATASETS / "online_retail_ii" / "online_retail_II.xlsx",
                (DATASETS / "online_retail_ii" / "online_retail_II.xlsx").read_bytes,
                "xlsx (two sheets)",
            ),
        ],
        mappings={
            "loader": "online_retail_log_events after renaming Invoice/Price/Customer ID and "
            "rewriting InvoiceDate from ISO to %m/%d/%Y %H:%M"
        },
        notes=[
            "Same shop as Online Retail I, two years instead of one. The built-in loader needs three "
            "columns renamed and the date column rewritten: its date format is fixed, not a parameter.",
            f"{facts['exact_duplicate_rows']:,} rows are exact repeats of another row. The loader has no "
            "row key at line grain, so they are kept and summed into their invoices (double counting "
            "them if they are entry errors; the data cannot say whether a repeated line is an error "
            "or a second identical line).",
        ],
        cannot=[
            "Use the built-in Online Retail loader without renaming columns and rewriting dates.",
            "Upload the published .xlsx to the Data Doctor (CSV only), or the 95 MB CSV (20 MB cap).",
        ],
        facts=facts,
    )


# --------------------------------------------------------------------------- Olist

_OLIST_BUY = ("delivered", "shipped", "invoiced", "processing", "approved", "created")


def adapt_olist(clock: Clock) -> Adapted:
    d = DATASETS / "olist"
    with clock.step("read"):
        orders = pd.read_csv(d / "olist_orders_dataset.csv.gz", dtype=str)
        customers = pd.read_csv(d / "olist_customers_dataset.csv.gz", dtype=str)
        items = pd.read_csv(d / "olist_order_items_dataset.csv.gz", dtype=str)
        products = pd.read_csv(d / "olist_products_dataset.csv.gz", dtype=str)
        names = pd.read_csv(d / "product_category_name_translation.csv.gz", dtype=str)
    with clock.step("adapter_join"):
        items["price"] = pd.to_numeric(items["price"])
        items["freight_value"] = pd.to_numeric(items["freight_value"])
        products = products.merge(names, on="product_category_name", how="left")
        products["category"] = products["product_category_name_english"].fillna(
            products["product_category_name"]
        )
        order_value = (
            items.assign(v=items["price"] + items["freight_value"])
            .groupby("order_id", as_index=False)
            .agg(order_value=("v", "sum"), lines=("v", "size"))
        )
        table = orders.merge(
            customers[["customer_id", "customer_unique_id", "customer_state"]], on="customer_id"
        )
        table = table.merge(order_value, on="order_id", how="left")
    mapping = EventMapping(
        customer_id="customer_unique_id",
        timestamp="order_purchase_timestamp",
        timezone="America/Sao_Paulo",
        event_type_column="order_status",
        event_type_values=dict.fromkeys(_OLIST_BUY, EventType.PURCHASE),
        value_column="order_value",
        attrs={"invoice_no": "order_id", "region": "customer_state", "lines": "lines"},
        fixed_attrs={"country": "BR", "timezone": "America/Sao_Paulo"},
        row_key=("order_id",),
    )
    with clock.step("map_order_grain", trace=True) as s:
        events, problems = map_frame(table, mapping, "olist")
        s.extra = {"events": len(events)}
    with clock.step("adapter_line_join"):
        lines = items.merge(
            table[["order_id", "customer_unique_id", "order_purchase_timestamp", "order_status"]],
            on="order_id",
        )
        lines = lines.merge(products[["product_id", "category"]], on="product_id", how="left")
        lines = lines[lines["order_status"].isin(_OLIST_BUY)]
        lines["quantity"] = 1
    line_mapping = EventMapping(
        customer_id="customer_unique_id",
        timestamp="order_purchase_timestamp",
        timezone="America/Sao_Paulo",
        event_type=EventType.PURCHASE,
        value_column="price",
        item_column="product_id",
        category_column="category",
        attrs={"invoice_no": "order_id", "quantity": "quantity"},
        row_key=("order_id", "order_item_id"),
    )
    with clock.step("map_line_grain", trace=True) as s:
        line_events, line_problems = map_frame(lines, line_mapping, "olist-items")
        s.extra = {"events": len(line_events)}
    purchases, baskets = _line_tables(line_events)
    statuses = orders["order_status"].value_counts().to_dict()
    no_items = int(table["order_value"].isna().sum())
    return Adapted(
        name="olist",
        currency="BRL",
        timezone="America/Sao_Paulo",
        synthetic=False,
        order_events=events,
        order_problems=_counts(problems),
        order_rows=len(table),
        purchases=purchases,
        baskets=baskets,
        line_rows=len(line_events),
        line_problems=_counts(line_problems),
        doctor=[
            DoctorInput(f.name, d / f.name, _gz(d / f.name), "csv.gz")
            for f in (
                d / "olist_orders_dataset.csv.gz",
                d / "olist_order_items_dataset.csv.gz",
                d / "olist_customers_dataset.csv.gz",
            )
        ],
        mappings={"order": mapping.model_dump(mode="json"), "line": line_mapping.model_dump(mode="json")},
        notes=[
            "Three tables had to be joined before any row has both a person and an amount: orders "
            "(time, status) + customers (customer_unique_id is the person; customer_id is per order) "
            "+ order items (price, freight).",
            "Order value = sum of item price + freight. canceled and unavailable orders are left "
            "unmapped on purpose (they show as unknown_event_type); orders with no item rows have no "
            "value and are refused by the Event contract (invalid_event).",
            "customer_state (Brazilian state) rides along as attrs['region']: nothing in state reads it.",
        ],
        cannot=[
            "Join orders, customers and items without code (the Doctor takes one CSV).",
            "Upload .csv.gz (CSV only); after gunzip the items and orders files are under 20 MB each.",
            "Use customer_unique_id without knowing that customer_id is per order: mapping customer_id "
            "(the obvious name) would make every order a new customer and repeat rate 0%.",
        ],
        facts={"order_status": statuses, "orders_without_items": no_items},
    )


# --------------------------------------------------------------------------- CDNOW


def adapt_cdnow(clock: Clock) -> Adapted:
    path = DATASETS / "cdnow" / "CDNOW_master.txt"
    with clock.step("read"):
        frame = pd.read_csv(path, sep=r"\s+", dtype=str)
    mapping = EventMapping(
        customer_id="customer_id",
        timestamp="date",
        timestamp_format="%Y%m%d",
        timezone="America/New_York",
        event_type=EventType.PURCHASE,
        value_column="dollar_value",
        attrs={"quantity": "number_of_cds"},
        fixed_attrs={"country": "US"},
    )
    with clock.step("map_order_grain", trace=True) as s:
        events, problems = map_frame(frame, mapping, "cdnow")
        s.extra = {"events": len(events)}
    same_day = int(frame.duplicated(["customer_id", "date"]).sum())
    return Adapted(
        name="cdnow",
        currency="USD",
        timezone="America/New_York",
        synthetic=False,
        order_events=events,
        order_problems=_counts(problems),
        order_rows=len(frame),
        purchases=purchases_frame(events),
        baskets=None,
        doctor=[DoctorInput(path.name, path, path.read_bytes, "whitespace text", delimiter=r"\s+")],
        mappings={"order": mapping.model_dump(mode="json")},
        notes=[
            "Whitespace-separated with a padded header: read with sep='\\s+'. Dates are YYYYMMDD "
            "(read as text, format %Y%m%d). Ids keep their leading zeros.",
            f"No product column: no item-level refill model and no recommender. {same_day:,} rows "
            "share a customer and a day with another row; each is its own purchase event.",
        ],
        cannot=["Feed the catalogue (no item ids): only the order-level cycle can run."],
        facts={"rows_sharing_customer_day": same_day},
    )


# --------------------------------------------------------------------------- Ta-Feng


def adapt_ta_feng(clock: Clock) -> Adapted:
    path = DATASETS / "ta_feng" / "ta-feng-grocery-dataset.zip"
    member = "ta_feng_all_months_merged.csv"
    with clock.step("read"), zipfile.ZipFile(path) as z:
        raw = pd.read_csv(z.open(member), dtype=str, encoding="utf-8-sig")
    with clock.step("adapter_baskets"):
        for c in ("AMOUNT", "ASSET", "SALES_PRICE"):
            raw[c] = pd.to_numeric(raw[c])
        raw["margin"] = raw["SALES_PRICE"] - raw["ASSET"]
        raw["basket"] = raw["CUSTOMER_ID"] + "|" + raw["TRANSACTION_DT"]
        order = raw.groupby(["CUSTOMER_ID", "TRANSACTION_DT"], as_index=False, sort=False).agg(
            value=("SALES_PRICE", "sum"),
            margin=("margin", "sum"),
            lines=("SALES_PRICE", "size"),
            items=("AMOUNT", "sum"),
            age_group=("AGE_GROUP", "first"),
            pin_code=("PIN_CODE", "first"),
        )
    mapping = EventMapping(
        customer_id="CUSTOMER_ID",
        timestamp="TRANSACTION_DT",
        timestamp_format="%m/%d/%Y",
        timezone="Asia/Taipei",
        event_type=EventType.PURCHASE,
        value_column="value",
        margin_column="margin",
        attrs={"lines": "lines", "items": "items", "age_group": "age_group", "pin_code": "pin_code"},
        fixed_attrs={"country": "TW", "timezone": "Asia/Taipei"},
        row_key=("CUSTOMER_ID", "TRANSACTION_DT"),
    )
    with clock.step("map_order_grain", trace=True) as s:
        events, problems = map_frame(order, mapping, "ta-feng")
        s.extra = {"events": len(events)}
    line_mapping = EventMapping(
        customer_id="CUSTOMER_ID",
        timestamp="TRANSACTION_DT",
        timestamp_format="%m/%d/%Y",
        timezone="Asia/Taipei",
        event_type=EventType.PURCHASE,
        value_column="SALES_PRICE",
        margin_column="margin",
        item_column="PRODUCT_ID",
        category_column="PRODUCT_SUBCLASS",
        attrs={"invoice_no": "basket", "quantity": "AMOUNT"},
    )
    with clock.step("map_line_grain", trace=True) as s:
        line_events, line_problems = map_frame(raw, line_mapping, "ta-feng-lines")
        s.extra = {"events": len(line_events)}
    with clock.step("line_tables"):
        purchases, baskets = _line_tables(line_events)
    n_line = len(line_events)
    del line_events
    return Adapted(
        name="ta_feng",
        currency="TWD",
        timezone="Asia/Taipei",
        synthetic=False,
        order_events=events,
        order_problems=_counts(problems),
        order_rows=len(order),
        purchases=purchases,
        baskets=baskets,
        line_rows=n_line,
        line_problems=_counts(line_problems),
        doctor=[DoctorInput(member, path, _zip_member(path, member), "zip")],
        mappings={"order": mapping.model_dump(mode="json"), "line": line_mapping.model_dump(mode="json")},
        notes=[
            "No basket id: a basket is one customer on one day (grouped by the adapter). Dates have no "
            "time of day, so every purchase is at local midnight.",
            "Margin is SALES_PRICE - ASSET (ASSET is the cost); "
            f"{float((raw['margin'] < 0).mean()):.1%} of lines sell below cost.",
            "AGE_GROUP and PIN_CODE are customer attributes: carried as attrs, ignored by the state fold.",
        ],
        cannot=[
            "Upload the zip to the Doctor (CSV only); the unzipped CSV is 64 MB, over the 20 MB cap.",
            "Say 'one customer-day is one order' in the mapping: the grouping needs code.",
        ],
        facts={"lines_below_cost_share": round(float((raw["margin"] < 0).mean()), 4)},
    )


# --------------------------------------------------------------------------- Superstore


def adapt_superstore(clock: Clock) -> Adapted:
    path = DATASETS / "superstore" / "sample_superstore.csv"
    with clock.step("read"):
        raw = pd.read_csv(path, dtype=str)
    with clock.step("adapter_orders"):
        for c in ("sales", "profit", "discount", "quantity"):
            raw[c] = pd.to_numeric(raw[c])
        raw["offer"] = np.where(raw["discount"] > 0, "discount", None)
        top = raw.sort_values("sales", ascending=False).drop_duplicates("order_id")[["order_id", "category"]]
        order = raw.groupby(["order_id", "customer_name"], as_index=False, sort=False).agg(
            order_date=("order_date", "first"),
            value=("sales", "sum"),
            margin=("profit", "sum"),
            offer=("offer", lambda s: "discount" if s.notna().any() else None),
            region=("region", "first"),
        )
        order = order.merge(top, on="order_id", how="left")
    mapping = EventMapping(
        customer_id="customer_name",
        timestamp="order_date",
        timezone="America/New_York",
        event_type=EventType.PURCHASE,
        value_column="value",
        margin_column="margin",
        category_column="category",
        attrs={"invoice_no": "order_id", "offer_id": "offer", "region": "region"},
        fixed_attrs={"country": "US"},
        row_key=("order_id", "customer_name"),
    )
    with clock.step("map_order_grain", trace=True) as s:
        events, problems = map_frame(order, mapping, "superstore")
        s.extra = {"events": len(events)}
    line_mapping = EventMapping(
        customer_id="customer_name",
        timestamp="order_date",
        timezone="America/New_York",
        event_type=EventType.PURCHASE,
        value_column="sales",
        margin_column="profit",
        item_column="product_name",
        category_column="sub_category",
        attrs={"invoice_no": "order_id", "quantity": "quantity"},
    )
    with clock.step("map_line_grain", trace=True) as s:
        line_events, line_problems = map_frame(raw, line_mapping, "superstore-lines")
        s.extra = {"events": len(line_events)}
    purchases, baskets = _line_tables(line_events)
    return Adapted(
        name="superstore",
        currency="USD",
        timezone="America/New_York",
        synthetic=True,
        order_events=events,
        order_problems=_counts(problems),
        order_rows=len(order),
        purchases=purchases,
        baskets=baskets,
        line_rows=len(line_events),
        line_problems=_counts(line_problems),
        doctor=[DoctorInput(path.name, path, path.read_bytes, "csv")],
        mappings={"order": mapping.model_dump(mode="json"), "line": line_mapping.model_dump(mode="json")},
        notes=[
            "Fictional (Tableau sample). The person is known only by name; the name is the customer id.",
            "Each order event carries its largest line's category, so top_categories counts orders. "
            "An order with any discounted line carries attrs offer_id='discount', so the fold's "
            "offer_sensitivity has something real to read (the only family-B set where it does).",
            f"Real margin: profit per line (negative on {float((raw['profit'] < 0).mean()):.1%} of lines).",
        ],
        cannot=["Aggregate lines to orders or derive an offer flag from a discount column without code."],
        facts={"lines_with_discount_share": round(float((raw["discount"] > 0).mean()), 4)},
    )


# --------------------------------------------------------------------------- next-basket sets

NBR_CUTOFF = datetime(2020, 1, 1, tzinfo=UTC)
NBR_STEP = timedelta(days=7)


def nbr_baskets(hist: pd.DataFrame, fut: pd.DataFrame) -> pd.DataFrame:
    """A basket table (`BASKET_COLUMNS`) from a next-basket history and future file.

    No dates at all. To use the recommender's time split, each customer's orders are put one week
    apart, the last history order a week before `NBR_CUTOFF` and the future basket a week after it.
    These times are INVENTED: only the order of baskets is real."""
    hist = hist.assign(n=hist["ORDER_NUMBER"].astype(int))
    last = hist.groupby("CUSTOMER_ID")["n"].transform("max")
    base = int(NBR_CUTOFF.timestamp())
    step = int(NBR_STEP.total_seconds())
    hist_t = base - (last - hist["n"] + 1) * step

    def table(f: pd.DataFrame, t: pd.Series) -> pd.DataFrame:  # type: ignore[type-arg]
        return pd.DataFrame(
            {
                "customer_id": f["CUSTOMER_ID"].astype(str),
                "item_id": f["MATERIAL_NUMBER"].astype(str),
                "t": t.astype(np.int64),
                "category": None,
                "basket_id": f["CUSTOMER_ID"].astype(str) + "|" + f["ORDER_NUMBER"].astype(str),
            }
        )

    rows = pd.concat(
        [table(hist, hist_t), table(fut, pd.Series(base + step, index=fut.index))], ignore_index=True
    )[list(BASKET_COLUMNS)]
    return rows.drop_duplicates(["customer_id", "item_id", "basket_id"]).reset_index(drop=True)


def _adapt_nbr(name: str, prefix: str, clock: Clock) -> Adapted:
    d = DATASETS / name
    with clock.step("read"):
        hist = pd.read_csv(d / f"{prefix}_history.csv", dtype=str)
        fut = pd.read_csv(d / f"{prefix}_future.csv", dtype=str)
    with clock.step("adapter_invent_time"):
        rows = nbr_baskets(hist, fut)
    return Adapted(
        name=name,
        currency="none (no prices)",
        timezone="UTC",
        synthetic=False,
        order_events=None,
        order_problems={},
        order_rows=len(hist) + len(fut),
        purchases=None,
        baskets=rows,
        invented_time=True,
        doctor=[
            DoctorInput(
                f"{prefix}_history.csv",
                d / f"{prefix}_history.csv",
                (d / f"{prefix}_history.csv").read_bytes,
                "csv",
            )
        ],
        notes=[
            "Preprocessed next-basket file: CUSTOMER_ID, ORDER_NUMBER, MATERIAL_NUMBER only. No date "
            "and no price, so no Event can be made without inventing both (an Event needs a "
            "timezone-aware ts, and a purchase needs a value). State, goals and Shadow are not run.",
            "Only the recommender runs, on the real order of baskets with invented weekly spacing "
            "(history = train, the published future basket = test).",
        ],
        cannot=[
            "Ingest at all: the Event contract requires a time and, for a purchase, a value.",
        ],
    )


def adapt_instacart_nbr(clock: Clock) -> Adapted:
    return _adapt_nbr("instacart_nbr", "Instacart", clock)


def adapt_dunnhumby_nbr(clock: Clock) -> Adapted:
    return _adapt_nbr("dunnhumby_nbr", "Dunnhumby", clock)


ADAPTERS: dict[str, Callable[[Clock], Adapted]] = {
    "online_retail_i": adapt_online_retail_i,
    "online_retail_ii": adapt_online_retail_ii,
    "olist": adapt_olist,
    "cdnow": adapt_cdnow,
    "ta_feng": adapt_ta_feng,
    "superstore": adapt_superstore,
    "instacart_nbr": adapt_instacart_nbr,
    "dunnhumby_nbr": adapt_dunnhumby_nbr,
}


def adapt(name: str, clock: Clock | None = None) -> Adapted:
    if name not in ADAPTERS:
        raise SystemExit(f"unknown dataset {name!r}; family B is {', '.join(FAMILY_B)}")
    return ADAPTERS[name](clock or Clock())


def csv_bytes_size(read: Callable[[], bytes]) -> int:
    return len(read())


def text_frame(data: bytes, delimiter: str) -> pd.DataFrame:
    """The Doctor's own reader (`read_csv_bytes`), imported lazily (it pulls in FastAPI)."""
    from api.v2.engines.doctor import read_csv_bytes

    return read_csv_bytes(data, delimiter=delimiter)


__all__ = ["ADAPTERS", "FAMILY_B", "Adapted", "DoctorInput", "adapt", "text_frame"]
