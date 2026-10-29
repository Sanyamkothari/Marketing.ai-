"""Synthetic data generators for Data Doctor scenarios (Stage 0).

Deterministic, fast, seeded generators matching the required scenario shapes.
"""

from __future__ import annotations

from typing import Final

import numpy as np
import pandas as pd

__all__ = [
    "generate_bank_marketing",
    "generate_churn_list",
    "generate_customers_and_orders",
    "generate_hillstrom_campaign",
    "generate_order_log",
    "generate_two_currencies",
    "generate_wide_monthly",
]

_DEFAULT_SEED: Final[int] = 42


def generate_order_log(seed: int = _DEFAULT_SEED) -> pd.DataFrame:
    """Synthetic order log: ~2,000 customers, 14 months, ~25% blank IDs, partial last month, duplicate rows."""
    rng = np.random.default_rng(seed)
    customers = [f"C{i}" for i in range(1000, 3000)]
    products = [f"P{i}" for i in range(100, 300)]
    dates = pd.date_range("2023-01-01", "2024-02-10", freq="D")

    rows: list[dict[str, object]] = []
    for c in customers:
        n_orders = int(rng.integers(1, 6))
        c_dates = rng.choice(dates, size=n_orders)
        for d in sorted(c_dates):
            inv = str(rng.integers(500000, 520000))
            is_blank = rng.random() < 0.25
            is_uk = rng.random() < 0.85
            n_items = int(rng.integers(1, 4))
            items = rng.choice(products, size=n_items)
            for p in items:
                rows.append(
                    {
                        "InvoiceNo": inv,
                        "StockCode": p,
                        "Description": "RETRO CANDLE HOLDER",
                        "Quantity": int(rng.integers(1, 10)),
                        "InvoiceDate": f"{str(d)[:10]} 12:00:00",
                        "UnitPrice": round(float(rng.uniform(1.5, 45.0)), 2),
                        "CustomerID": "" if is_blank else c,
                        "Country": "United Kingdom" if is_uk else "Germany",
                    }
                )

    df = pd.DataFrame(rows)
    # Add a few exact duplicate rows
    duplicates = df.iloc[:10].copy()
    return pd.concat([df, duplicates], ignore_index=True)


def generate_churn_list(seed: int = _DEFAULT_SEED) -> pd.DataFrame:
    """Customer list with an outcome column already present (churn shape)."""
    rng = np.random.default_rng(seed)
    n = 1500
    rows: list[dict[str, object]] = []
    contracts = ["Month-to-month", "One year", "Two year"]

    for i in range(n):
        tenure = int(rng.integers(1, 72))
        monthly = round(float(rng.uniform(20.0, 110.0)), 2)
        contract = str(rng.choice(contracts))
        # Logistic probability for realistic learnable signal
        prob = 1.0 / (1.0 + np.exp(0.03 * tenure - 0.02 * monthly + (1.0 if contract == "Two year" else 0.0)))
        churn = "Yes" if rng.random() < max(prob, 0.22) else "No"
        rows.append(
            {
                "customerID": f"C{1000 + i}",
                "tenure": tenure,
                "MonthlyCharges": monthly,
                "Contract": contract,
                "Churn": churn,
            }
        )
    return pd.DataFrame(rows)


def generate_bank_marketing(seed: int = _DEFAULT_SEED) -> pd.DataFrame:
    """File with no ID (one row per call, the bank marketing shape)."""
    rng = np.random.default_rng(seed)
    n = 1500
    jobs = ["admin.", "technician", "services", "management", "blue-collar"]
    maritals = ["married", "single", "divorced"]
    educations = ["secondary", "tertiary", "primary"]

    rows: list[dict[str, object]] = []
    for _ in range(n):
        duration = int(rng.integers(10, 1200))
        prob = 1.0 / (1.0 + np.exp(-0.003 * duration + 1.2))
        y_val = "yes" if rng.random() < max(prob, 0.22) else "no"
        rows.append(
            {
                "age": int(rng.integers(18, 75)),
                "job": str(rng.choice(jobs)),
                "marital": str(rng.choice(maritals)),
                "education": str(rng.choice(educations)),
                "balance": round(float(rng.uniform(-200.0, 15000.0)), 2),
                "duration": duration,
                "campaign": int(rng.integers(1, 8)),
                "y": y_val,
            }
        )
    return pd.DataFrame(rows)


def generate_hillstrom_campaign(seed: int = _DEFAULT_SEED) -> pd.DataFrame:
    """Campaign test with treatment and control groups (Hillstrom shape)."""
    rng = np.random.default_rng(seed)
    n = 1500
    segments = ["Womens E-Mail", "Mens E-Mail", "No E-Mail"]
    channels = ["Phone", "Web", "Multichannel"]
    zips = ["Urban", "Suburban", "Rural"]

    rows: list[dict[str, object]] = []
    for i in range(n):
        segment = str(rng.choice(segments))
        treated = segment != "No E-Mail"
        prob_conv = 0.25 if treated else 0.18
        conversion = int(rng.random() < prob_conv)
        spend = round(float(rng.uniform(25.0, 200.0)), 2) if conversion else 0.0
        rows.append(
            {
                "customer_id": f"C{1000 + i}",
                "recency": int(rng.integers(1, 12)),
                "history": round(float(rng.uniform(50.0, 900.0)), 2),
                "used_discount": int(rng.random() < 0.5),
                "used_bogo": int(rng.random() < 0.5),
                "zip_code": str(rng.choice(zips)),
                "is_referral": int(rng.random() < 0.3),
                "channel": str(rng.choice(channels)),
                "segment": segment,
                "visit": int(conversion or rng.random() < 0.1),
                "spend": spend,
                "conversion": conversion,
            }
        )
    return pd.DataFrame(rows)


def generate_two_currencies(seed: int = _DEFAULT_SEED) -> pd.DataFrame:
    """File with amounts in two currencies."""
    rng = np.random.default_rng(seed)
    n = 1500
    rows: list[dict[str, object]] = []
    for i in range(n):
        curr = "USD" if i < 800 else "EUR"
        rows.append(
            {
                "customer_id": f"C{1000 + i}",
                "amount": round(float(rng.uniform(10.0, 400.0)), 2),
                "currency": curr,
                "churn": int(rng.random() < 0.22),
            }
        )
    return pd.DataFrame(rows)


def generate_wide_monthly(seed: int = _DEFAULT_SEED) -> pd.DataFrame:
    """Wide monthly sheet (spend_jan..spend_dec)."""
    rng = np.random.default_rng(seed)
    n = 1500
    months = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
    rows: list[dict[str, object]] = []
    for i in range(n):
        row: dict[str, object] = {"customer_id": f"C{1000 + i}"}
        for m in months:
            row[f"spend_{m}"] = round(float(rng.uniform(0.0, 150.0)), 2) if rng.random() > 0.3 else 0.0
        row["active"] = int(rng.random() < 0.7)
        rows.append(row)
    return pd.DataFrame(rows)


def generate_customers_and_orders(seed: int = _DEFAULT_SEED) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Two files to join: customers plus orders."""
    rng = np.random.default_rng(seed)
    n_cust = 1500
    cust_rows: list[dict[str, object]] = []
    for i in range(n_cust):
        c = f"C{1000 + i}"
        cust_rows.append(
            {
                "customer_id": c,
                "country": "United Kingdom" if rng.random() < 0.8 else "France",
                "signup_date": "2023-01-10",
            }
        )
    customers_df = pd.DataFrame(cust_rows)

    n_orders = 4000
    dates = pd.date_range("2023-02-01", "2023-11-30", freq="D")
    order_rows: list[dict[str, object]] = []
    for i in range(n_orders):
        c = f"C{1000 + int(rng.integers(0, n_cust))}"
        order_rows.append(
            {
                "order_id": f"O{5000 + i}",
                "customer_id": c,
                "order_date": str(rng.choice(dates))[:10],
                "amount": round(float(rng.uniform(5.0, 150.0)), 2),
            }
        )
    orders_df = pd.DataFrame(order_rows)
    return customers_df, orders_df
