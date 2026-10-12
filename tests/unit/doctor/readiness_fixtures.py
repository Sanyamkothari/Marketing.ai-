"""Fixtures for the readiness report's tests (Plan O M234): small files that pass `check_plan`.

Every frame here is generated (no client data). `customers` is a Targeted Advertisement file and
`assets` a Fault Prediction one (split by date); both pass the run's own checks as they are, so a test
changes one thing and shows what that one thing does after validation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from engine.agent.checks import check_plan
from engine.config import RunMode, UseCaseConfig, load_use_case
from engine.contracts import ValidationReport
from engine.doctor.readiness import ReadinessInputs, check_readiness
from engine.doctor.readiness_contracts import DataReadinessReport
from engine.doctor.readiness_probes import SourceScan

CONFIGS = Path(__file__).resolve().parents[3] / "configs"


def config(use_case: str, **sections: dict[str, Any]) -> UseCaseConfig:
    """The shipped use case with some section values replaced (`split={"group_column": "region"}`)."""
    loaded = load_use_case(use_case, CONFIGS)
    update: dict[str, Any] = {}
    for section, values in sections.items():
        if section == "top":
            update.update(values)
            continue
        update[section] = getattr(loaded, section).model_copy(update=values)
    return loaded.model_copy(update=update)


def customers(rows: int = 2000, *, seed: int = 0, positive_rate: float = 0.3) -> pd.DataFrame:
    """A Targeted Advertisement training file that passes every blocking check."""
    rng = np.random.default_rng(seed)
    return pd.DataFrame(
        {
            "customer_id": [f"C-{i:06d}" for i in range(rows)],
            "visits_last_7d": rng.integers(0, 20, rows),
            "ad_ctr_90d": rng.random(rows).round(3),
            "plan_tier": rng.choice(["basic", "standard", "premium"], rows),
            "tenure_months": rng.integers(1, 80, rows),
            "region": rng.choice(["north", "south", "east", "west"], rows),
            "marketing_opt_in": rng.random(rows) < 0.9,
            "last_contacted_at": pd.to_datetime("2026-06-01")
            + pd.to_timedelta(rng.integers(0, 60, rows), unit="D"),
            "converted_30d": (rng.random(rows) < positive_rate).astype(int),
        }
    )


def assets(rows: int = 2400, *, seed: int = 0, days: int = 120, positive_rate: float = 0.3) -> pd.DataFrame:
    """A Fault Prediction training file, dated over `days` days, that passes every blocking check."""
    rng = np.random.default_rng(seed)
    dates = pd.Timestamp("2026-04-01") + pd.to_timedelta(rng.integers(0, days, rows), unit="D")
    return pd.DataFrame(
        {
            "asset_id": [f"A-{i:06d}" for i in range(rows)],
            "snapshot_date": dates.strftime("%Y-%m-%d"),
            "error_rate_24h": rng.random(rows).round(3) * 50,
            "latency_p95_1h": rng.random(rows).round(1) * 400,
            "firmware_age_days": rng.integers(1, 400, rows),
            "tickets_90d": rng.integers(0, 6, rows),
            "device_model": rng.choice(["ONT-220", "RTR-900", "ONT-410"], rows),
            "marketing_opt_in": rng.random(rows) < 0.9,
            "last_contacted_at": None,
            "fault_next_72h": (rng.random(rows) < positive_rate).astype(int),
        }
    )


KEYS: dict[str, tuple[str, str]] = {
    "targeted-advertisement": ("customer_id", "converted_30d"),
    "fault-prediction": ("asset_id", "fault_next_72h"),
}


def dry_run(frame: pd.DataFrame, cfg: UseCaseConfig, *, row_count: int | None = None) -> ValidationReport:
    """What `POST /runs` answers for this file today: `check_plan`, nothing else."""
    key, target = KEYS[cfg.id]
    return check_plan(
        frame,
        cfg,
        mode=RunMode.TRAIN,
        primary_key=key,
        target=target,
        upload_id="u-fixture",
        row_count=len(frame) if row_count is None else row_count,
    )


def report(
    frame: pd.DataFrame,
    cfg: UseCaseConfig,
    *,
    row_count: int | None = None,
    scan: SourceScan | None = None,
    **extra: Any,
) -> DataReadinessReport:
    key, target = KEYS.get(cfg.id, (None, None))
    key = extra.pop("primary_key", key)
    target = extra.pop("target", target)
    count = len(frame) if row_count is None else row_count
    if scan is None:
        scan = SourceScan(file_format="parquet", rows_parsed=count)
    return check_readiness(
        ReadinessInputs(
            frame=frame,
            config=cfg,
            mode=extra.pop("mode", RunMode.TRAIN),
            primary_key=key,
            target=target,
            upload_id="u-fixture",
            row_count=count,
            scan=scan,
            **extra,
        )
    )
