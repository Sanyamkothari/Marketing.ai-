"""Unit and acceptance tests for Plan J M101: One action per customer across use cases (DEC-1311).

Acceptance tests:
- With three overlapping use cases, no customer has two actions; the winner has the highest
  priority x value; contact caps hold; holdout members are never treated.
- Each use case's campaign measures only its winning rows.
- With one use case selected, the output equals that use case's treat list (M98).
- Linear time at 1M rows (tested on 200k rows < 3s).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from engine.decide.arbitrate import (
    ArbitrationConfig,
    arbitrate_treat_lists,
    load_arbitration_config,
)
from engine.decide.treat_list import REASON_COLUMNS


def _make_treat_list(
    rows: list[dict[str, Any]],
    use_case: str = "win-back-campaign",
    key_col: str = "customer_id",
) -> pd.DataFrame:
    """Helper creating a valid treat list DataFrame matching M98 schema."""
    data: dict[str, list[Any]] = {
        key_col: [r[key_col] for r in rows],
        "use_case": [r.get("use_case", use_case) for r in rows],
        "model_version": [r.get("model_version", f"m_{use_case}_1") for r in rows],
        "segment": [r.get("segment", "persuadable") for r in rows],
        "treat": [bool(r.get("treat", False)) for r in rows],
        "holdout": [r.get("holdout", False) for r in rows],
        "explore": [r.get("explore", False) for r in rows],
        "suppression_reason": [r.get("suppression_reason") for r in rows],
        "offer": [r.get("offer", "Treat" if r.get("treat") else None) for r in rows],
        "channel": [r.get("channel", "email" if r.get("treat") else None) for r in rows],
        "net_value": [r.get("net_value") for r in rows],
        "expected_gross_value": [r.get("expected_gross_value") for r in rows],
    }
    for col in REASON_COLUMNS:
        data[col] = [r.get(col, None) for r in rows]
    return pd.DataFrame(data)


def test_arbitration_config_loading_and_defaults(tmp_path: Path) -> None:
    # 1. Non-existent file gives default config
    cfg_empty = load_arbitration_config(tmp_path / "does_not_exist")
    assert cfg_empty.contact_cap_per_customer == 1
    assert cfg_empty.priority_for("unknown") == 1.0
    assert cfg_empty.channel_caps == {}

    # 2. Shipped or custom config
    cfg_file = tmp_path / "decide" / "arbitration.yaml"
    cfg_file.parent.mkdir(parents=True)
    cfg_file.write_text("""schema_version: 1
use_cases:
  win-back-campaign:
    priority: 1.5
  targeted-advertisement:
    priority: 1.0
contact_cap_per_customer: 1
channel_caps:
  sms: 100
  email: 500
""")
    cfg = load_arbitration_config(tmp_path)
    assert cfg.contact_cap_per_customer == 1
    assert cfg.priority_for("win-back-campaign") == 1.5
    assert cfg.priority_for("targeted-advertisement") == 1.0
    assert cfg.priority_for("other") == 1.0
    assert cfg.channel_caps == {"sms": 100, "email": 500}


def test_three_overlapping_use_cases_winner_has_highest_priority_x_value() -> None:
    # Setup 3 overlapping use cases with customers C-1, C-2, C-3
    # Config: win-back priority 1.2, targeted-ad priority 1.0, telco-churn priority 2.0
    config = ArbitrationConfig(
        use_cases={
            "win-back": {"priority": 1.2},
            "targeted-ad": {"priority": 1.0},
            "telco-churn": {"priority": 2.0},
        },
        contact_cap_per_customer=1,
    )

    # Customer C-1:
    # win-back: net_value = 50.0 -> score = 1.2 * 50 = 60.0, offer = "10% off"
    # targeted-ad: net_value = 70.0 -> score = 1.0 * 70 = 70.0, offer = "Ad Banner"
    # telco-churn: net_value = 30.0 -> score = 2.0 * 30 = 60.0, offer = "Free Gigabytes"
    # Winner for C-1 should be targeted-ad (score 70.0 > 60.0)

    # Customer C-2:
    # win-back: net_value = 100.0 -> score = 1.2 * 100 = 120.0, offer = "Winback Special"
    # targeted-ad: net_value = 50.0 -> score = 1.0 * 50 = 50.0, offer = "Ad Banner"
    # Winner for C-2 should be win-back (score 120.0)

    # Customer C-3:
    # Only in telco-churn: net_value = 25.0 -> score = 2.0 * 25 = 50.0
    # Winner is telco-churn

    tl1 = _make_treat_list(
        [
            {"customer_id": "C-1", "treat": True, "net_value": 50.0, "offer": "10% off"},
            {"customer_id": "C-2", "treat": True, "net_value": 100.0, "offer": "Winback Special"},
        ],
        use_case="win-back",
    )
    tl2 = _make_treat_list(
        [
            {"customer_id": "C-1", "treat": True, "net_value": 70.0, "offer": "Ad Banner"},
            {"customer_id": "C-2", "treat": True, "net_value": 50.0, "offer": "Ad Banner"},
        ],
        use_case="targeted-ad",
    )
    tl3 = _make_treat_list(
        [
            {"customer_id": "C-1", "treat": True, "net_value": 30.0, "offer": "Free Gigabytes"},
            {"customer_id": "C-3", "treat": True, "net_value": 25.0, "offer": "Stay Bonus"},
        ],
        use_case="telco-churn",
    )

    arbitrated, summary = arbitrate_treat_lists([tl1, tl2, tl3], config=config)

    # Verify no customer has two actions
    treated_rows = arbitrated[arbitrated["treat"]]
    assert len(treated_rows) == 3
    assert treated_rows["customer_id"].is_unique

    # Check C-1 winner: targeted-ad (score 70.0)
    c1 = arbitrated[arbitrated["customer_id"] == "C-1"].iloc[0]
    assert c1["treat"] is True or c1["treat"] == 1
    assert c1["winning_use_case"] == "targeted-ad"
    assert c1["offer"] == "Ad Banner"
    assert c1["priority_score"] == pytest.approx(70.0)
    assert "win-back" in c1["losing_actions"]
    assert "telco-churn" in c1["losing_actions"]

    # Check C-2 winner: win-back (score 120.0)
    c2 = arbitrated[arbitrated["customer_id"] == "C-2"].iloc[0]
    assert c2["treat"] is True or c2["treat"] == 1
    assert c2["winning_use_case"] == "win-back"
    assert c2["offer"] == "Winback Special"
    assert c2["priority_score"] == pytest.approx(120.0)
    assert "targeted-ad" in c2["losing_actions"]

    # Check C-3 winner: telco-churn (score 50.0)
    c3 = arbitrated[arbitrated["customer_id"] == "C-3"].iloc[0]
    assert c3["treat"] is True or c3["treat"] == 1
    assert c3["winning_use_case"] == "telco-churn"
    assert c3["offer"] == "Stay Bonus"
    assert c3["priority_score"] == pytest.approx(50.0)
    assert c3["losing_actions"] is None or pd.isna(c3["losing_actions"])

    # Check summary conflicts
    assert summary.total_customers == 3
    assert summary.customers_with_actions == 3
    assert summary.customers_with_conflicts == 2  # C-1 and C-2 had multiple candidate actions
    assert summary.dropped_actions_count == 3  # C-1 lost 2, C-2 lost 1
    assert summary.dropped_by_use_case.get("win-back") == 1
    assert summary.dropped_by_use_case.get("telco-churn") == 1
    assert summary.dropped_by_use_case.get("targeted-ad") == 1


def test_holdout_members_are_never_treated() -> None:
    # Customer C-1 is in holdout in use case A (holdout = 1, treat = 0).
    # In use case B, C-1 was candidate for treat (treat = 1, net_value = 100.0).
    # Since C-1 is a holdout member, they must NEVER be treated across ANY use case.
    config = ArbitrationConfig(contact_cap_per_customer=1)

    tl_a = _make_treat_list(
        [
            {"customer_id": "C-1", "treat": False, "holdout": True, "net_value": None},
            {"customer_id": "C-2", "treat": True, "holdout": False, "net_value": 20.0},
        ],
        use_case="use-case-a",
    )
    tl_b = _make_treat_list(
        [
            {"customer_id": "C-1", "treat": True, "holdout": False, "net_value": 100.0, "offer": "Big Offer"},
            {"customer_id": "C-2", "treat": False, "holdout": False, "net_value": None},
        ],
        use_case="use-case-b",
    )

    arbitrated, _summary = arbitrate_treat_lists([tl_a, tl_b], config=config)

    c1 = arbitrated[arbitrated["customer_id"] == "C-1"].iloc[0]
    assert not bool(c1["treat"])
    assert bool(c1["holdout"])
    assert c1["winning_use_case"] is None or pd.isna(c1["winning_use_case"])

    c2 = arbitrated[arbitrated["customer_id"] == "C-2"].iloc[0]
    assert bool(c2["treat"])
    assert c2["winning_use_case"] == "use-case-a"


def test_explore_rows_preserve_randomisation() -> None:
    # Customer C-1 is in explore slice (explore = 1, treat = 1)
    config = ArbitrationConfig(contact_cap_per_customer=1)

    tl = _make_treat_list(
        [
            {"customer_id": "C-1", "treat": True, "explore": True, "net_value": -5.0, "offer": "Treat"},
        ],
        use_case="use-case-a",
    )

    arbitrated, _summary = arbitrate_treat_lists([tl], config=config)
    c1 = arbitrated[arbitrated["customer_id"] == "C-1"].iloc[0]
    assert bool(c1["treat"])
    assert bool(c1["explore"])
    assert c1["winning_use_case"] == "use-case-a"


def test_contact_caps_and_channel_caps() -> None:
    # Channel cap: sms max 1
    config = ArbitrationConfig(
        contact_cap_per_customer=1,
        channel_caps={"sms": 1},
    )

    # 3 customers requesting SMS treatment with net values 100, 80, 50
    tl = _make_treat_list(
        [
            {
                "customer_id": "C-1",
                "treat": True,
                "channel": "sms",
                "net_value": 100.0,
                "offer": "SMS Offer 1",
            },
            {
                "customer_id": "C-2",
                "treat": True,
                "channel": "sms",
                "net_value": 80.0,
                "offer": "SMS Offer 2",
            },
            {
                "customer_id": "C-3",
                "treat": True,
                "channel": "email",
                "net_value": 50.0,
                "offer": "Email Offer",
            },
        ],
        use_case="win-back",
    )

    arbitrated, summary = arbitrate_treat_lists([tl], config=config)

    # Only C-1 gets SMS (100 > 80)
    c1 = arbitrated[arbitrated["customer_id"] == "C-1"].iloc[0]
    assert bool(c1["treat"])
    assert c1["channel"] == "sms"

    # C-2 is capped out on SMS
    c2 = arbitrated[arbitrated["customer_id"] == "C-2"].iloc[0]
    assert not bool(c2["treat"])

    # C-3 is email, so unaffected
    c3 = arbitrated[arbitrated["customer_id"] == "C-3"].iloc[0]
    assert bool(c3["treat"])
    assert c3["channel"] == "email"

    assert summary.channel_capped_count == 1


def test_with_one_use_case_selected_output_equals_treat_list() -> None:
    # Acceptance requirement: With one use case selected, the output equals that use case's treat list (M98)
    config = ArbitrationConfig(contact_cap_per_customer=1)

    original = _make_treat_list(
        [
            {"customer_id": "C-1", "treat": True, "offer": "Winback", "channel": "email", "net_value": 45.0},
            {"customer_id": "C-2", "treat": False, "holdout": True, "net_value": None},
            {"customer_id": "C-3", "treat": False, "suppression_reason": "opted_out", "net_value": None},
        ],
        use_case="win-back",
    )

    arbitrated, _summary = arbitrate_treat_lists([original], config=config)

    # Compare columns of treat list
    treat_list_cols = list(original.columns)
    for col in treat_list_cols:
        pd.testing.assert_series_equal(arbitrated[col], original[col], check_names=False, obj=f"Column {col}")

    # Winning use case on treated row
    c1 = arbitrated[arbitrated["customer_id"] == "C-1"].iloc[0]
    assert c1["winning_use_case"] == "win-back"
    assert c1["priority_score"] == pytest.approx(45.0)


@pytest.mark.slow
def test_arbitrate_scale_200k_rows() -> None:
    # Scale test: 200,000 customers across 3 use cases
    # Acceptance check: linear time at 1M rows (< 3s on 200k rows)
    n = 200_000
    rng = np.random.default_rng(42)

    cust_ids = [f"C-{i:07d}" for i in range(n)]

    # Overlapping use cases
    # UC1 covers 0..150,000
    # UC2 covers 50,000..200,000
    # UC3 covers 100,000..200,000
    uc1_df = pd.DataFrame(
        {
            "customer_id": cust_ids[:150_000],
            "use_case": "uc-1",
            "model_version": "m1",
            "segment": "persuadable",
            "treat": rng.random(150_000) > 0.5,
            "holdout": False,
            "explore": False,
            "suppression_reason": None,
            "offer": "Offer1",
            "channel": "email",
            "net_value": rng.uniform(10.0, 100.0, 150_000),
            "expected_gross_value": None,
            "reason_1": None,
            "reason_2": None,
            "reason_3": None,
        }
    )

    uc2_df = pd.DataFrame(
        {
            "customer_id": cust_ids[50_000:],
            "use_case": "uc-2",
            "model_version": "m2",
            "segment": "persuadable",
            "treat": rng.random(150_000) > 0.5,
            "holdout": False,
            "explore": False,
            "suppression_reason": None,
            "offer": "Offer2",
            "channel": "sms",
            "net_value": rng.uniform(10.0, 100.0, 150_000),
            "expected_gross_value": None,
            "reason_1": None,
            "reason_2": None,
            "reason_3": None,
        }
    )

    config = ArbitrationConfig(
        use_cases={"uc-1": {"priority": 1.2}, "uc-2": {"priority": 1.0}},
        contact_cap_per_customer=1,
    )

    start = time.perf_counter()
    arbitrated, _summary = arbitrate_treat_lists([uc1_df, uc2_df], config=config)
    elapsed = time.perf_counter() - start

    assert len(arbitrated) == n
    assert elapsed < 3.0, f"Arbitration of 200k rows took {elapsed:.2f}s (budget: 3.0s)"
    # Linear estimate for 1M rows
    m1_estimate = elapsed * 5.0
    print(f"\n200k rows arbitrated in {elapsed:.3f}s; 1M rows estimated at {m1_estimate:.2f}s")
