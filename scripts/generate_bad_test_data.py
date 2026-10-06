"""Generate bad test CSV datasets for every use case in Marketing.ai.

This script constructs comprehensive dirty and adversarial CSV datasets across all 12
use cases (11 predictive + 1 generative AI assistant) to stress-test the system's ingestion,
profiling, and validation capabilities.
"""

from __future__ import annotations

import os
from pathlib import Path
import numpy as np
import pandas as pd

from engine.config import ColumnRole, ColumnType, load_all_use_cases, load_use_case
from tests.fixtures.make_data import GenerationSpec, generate

OUTPUT_BASE_DIR = Path.home() / "Downloads" / "marketing_ai_bad_data_tests"


def _get_use_case_columns(config):
    pks = [c.name for c in config.template.columns if c.role == ColumnRole.PRIMARY_KEY]
    targets = [c.name for c in config.template.columns if c.role == ColumnRole.TARGET]
    times = [c.name for c in config.template.columns if c.role == ColumnRole.TIME]
    numerics = [
        c.name for c in config.template.columns
        if c.type in (ColumnType.INTEGER, ColumnType.FLOAT) and c.role not in (ColumnRole.PRIMARY_KEY, ColumnRole.TARGET)
    ]
    categoricals = [
        c.name for c in config.template.columns
        if c.type in (ColumnType.STRING, ColumnType.TEXT) and c.role not in (ColumnRole.PRIMARY_KEY, ColumnRole.TARGET)
    ]
    booleans = [
        c.name for c in config.template.columns
        if c.type == ColumnType.BOOLEAN and c.role not in (ColumnRole.PRIMARY_KEY, ColumnRole.TARGET)
    ]
    return {
        "pk": pks[0] if pks else None,
        "target": targets[0] if targets else None,
        "time": times[0] if times else None,
        "numerics": numerics,
        "categoricals": categoricals,
        "booleans": booleans,
    }


def _make_base_frame(use_case_id: str, rows: int = 3000, seed: int = 42) -> pd.DataFrame:
    if use_case_id == "ai-onboarding-assistant":
        ref_path = Path("tests/fixtures/docs/reference_qa.csv")
        base = pd.read_csv(ref_path)
        frames = [base]
        if len(base) < 65:
            extra = base.copy()
            extra["question"] = extra["question"] + " (variant)"
            frames.append(extra)
        df = pd.concat(frames, ignore_index=True)
        return df.iloc[:rows].copy()
    else:
        spec = GenerationSpec(
            use_case_id=use_case_id,
            rows=rows,
            variant="clean",
            positive_rate=0.25,
            seed=seed,
        )
        return generate(spec).copy()


# --- Corruptors ---

def inject_corrupted_pks(df: pd.DataFrame, pk_col: str, rng: np.random.Generator) -> pd.DataFrame:
    df = df.copy()
    n = len(df)
    if pk_col and pk_col in df.columns:
        # Duplicate keys: 50 rows get key of row 0
        first_key = df.iloc[0][pk_col]
        dup_indices = rng.choice(range(1, n), size=min(50, n - 1), replace=False)
        for idx in dup_indices:
            df.loc[idx, pk_col] = first_key
        # Null keys: 20 rows get empty string
        null_indices = rng.choice(
            [i for i in range(1, n) if i not in dup_indices],
            size=min(20, n - len(dup_indices) - 1),
            replace=False
        )
        for idx in null_indices:
            df.loc[idx, pk_col] = ""
    return df


def inject_corrupted_target(df: pd.DataFrame, target_col: str, rng: np.random.Generator) -> pd.DataFrame:
    df = df.copy()
    n = len(df)
    if not target_col or target_col not in df.columns:
        return df
    is_numeric = pd.api.types.is_numeric_dtype(df[target_col])
    third_label = 99 if is_numeric else "Unknown"
    indices = rng.choice(range(n), size=min(100, n), replace=False)
    for idx in indices:
        df.loc[idx, target_col] = third_label
    return df


def inject_leakage_and_pii(df: pd.DataFrame, target_col: str, rng: np.random.Generator) -> pd.DataFrame:
    df = df.copy()
    n = len(df)
    if target_col and target_col in df.columns:
        unique_vals = list(df[target_col].unique())
        t_num = df[target_col].map(lambda x: 1.0 if x == unique_vals[0] else 0.0).fillna(0.0)
        leak = t_num + rng.normal(0, 0.02, size=n)
        df["campaign_result_score"] = np.round(leak, 4)
    df["billing_contact_email"] = [f"client.user{i:04d}@example.invalid" for i in range(n)]
    df["billing_contact_phone"] = [f"+1-555-555-{int(rng.integers(1000, 9999)):04d}" for _ in range(n)]
    return df


def inject_schema_and_types(df: pd.DataFrame, cols: dict, rng: np.random.Generator) -> pd.DataFrame:
    df = df.copy()
    n = len(df)
    if cols["numerics"]:
        col = cols["numerics"][0]
        mask = rng.random(n) < 0.08
        df[col] = [
            "unknown" if m else ("" if pd.isna(v) else str(v))
            for m, v in zip(mask, df[col], strict=False)
        ]
    df["data_source"] = "legacy_crm_export"
    nps_vals = [f"{rng.integers(1, 10)}" if rng.random() > 0.88 else "" for _ in range(n)]
    df["survey_nps_score"] = nps_vals
    df["external_tracking_uuid"] = [f"TX-{rng.integers(100000, 999999)}-{i}" for i in range(n)]
    if cols["time"] and cols["time"] in df.columns:
        df[cols["time"]] = [f"FY26-W{(i % 52) + 1:02d}" for i in range(n)]
    return df


def build_fixable_messy_helper_test(df: pd.DataFrame, cols: dict, rng: np.random.Generator) -> pd.DataFrame:
    """Builds a messy file that Guided Setup / The Helper CAN read, propose fixes for, and train on!

    Guarantees:
    - Unique primary keys (no PK_NOT_UNIQUE)
    - Valid binary target with >= 300 positives (no TARGET errors)
    - Messy currencies, commas, percentages in numbers
    - Dirty booleans (Y / N / yes / no)
    - Mixed date formats (day first)
    - Trailing spaces and casing in categories
    - PII contact email and suspected leakage column
    """
    df = df.copy()
    n = len(df)

    # 1. Format numeric features with currency, commas, or percentages
    for col in cols["numerics"]:
        sample_val = df[col].dropna().iloc[0] if not df[col].dropna().empty else 100
        is_large = isinstance(sample_val, (int, float, np.number)) and sample_val > 50
        is_rate = isinstance(sample_val, (int, float, np.number)) and 0.0 <= sample_val <= 1.0

        if is_large:
            symbols = rng.choice(["₹", "$", "Rs. "], size=n)
            df[col] = [
                f"{sym}{val:,.2f}" if pd.notna(val) and isinstance(val, (int, float, np.number))
                else ("" if pd.isna(val) else str(val))
                for sym, val in zip(symbols, df[col], strict=False)
            ]
        elif is_rate:
            df[col] = [
                f"{val * 100:.1f}%" if pd.notna(val) and isinstance(val, (int, float, np.number))
                else ("" if pd.isna(val) else str(val))
                for val in df[col]
            ]
        else:
            df[col] = [
                f"{val:,}" if pd.notna(val) and isinstance(val, (int, np.integer))
                else str(val)
                for val in df[col]
            ]

    # 2. Categoricals: erratic casing and trailing spaces
    for col in cols["categoricals"]:
        df[col] = [
            f" {str(v).upper()} " if i % 3 == 0
            else f"{str(v).lower()} " if i % 3 == 1
            else str(v)
            for i, v in enumerate(df[col])
        ]

    # 3. Booleans: dirty representations
    for col in cols["booleans"]:
        choices = ["Y", "yes", "YES", "N", "No", "NO"]
        df[col] = rng.choice(choices, size=n)

    # 4. Dates: day-first format mixed with ISO
    if cols["time"] and cols["time"] in df.columns:
        dates = pd.to_datetime(df[cols["time"]], errors="coerce").fillna(pd.Timestamp("2026-08-01"))
        styles = rng.integers(0, 3, size=n)
        df[cols["time"]] = [
            (
                d.strftime("%d/%m/%Y") if s == 0
                else d.strftime("%Y-%m-%d") if s == 1
                else d.strftime("%d %b %Y")
            )
            for d, s in zip(dates, styles, strict=False)
        ]

    # 5. Injected personal email
    df["contact_email"] = [f"user{i:04d}@example.com" for i in range(n)]

    # 6. Injected leakage column (post-outcome score)
    target_col = cols["target"]
    if target_col and target_col in df.columns:
        unique_vals = list(df[target_col].unique())
        t_num = df[target_col].map(lambda x: 1.0 if x == unique_vals[0] else 0.0).fillna(0.0)
        leak = t_num + rng.normal(0, 0.03, size=n)
        df["campaign_result_score"] = np.round(leak, 4)

    return df


def inject_formatting_chaos(df: pd.DataFrame, cols: dict, rng: np.random.Generator) -> pd.DataFrame:
    df = df.copy()
    n = len(df)
    for col in cols["numerics"]:
        sample_val = df[col].dropna().iloc[0] if not df[col].dropna().empty else 100
        is_large = isinstance(sample_val, (int, float, np.number)) and sample_val > 50
        is_rate = isinstance(sample_val, (int, float, np.number)) and 0.0 <= sample_val <= 1.0

        if is_large:
            symbols = rng.choice(["$", "₹", "Rs. "], size=n)
            df[col] = [
                f"{sym}{val:,.2f}" if pd.notna(val) and isinstance(val, (int, float, np.number))
                else ("N/A" if rng.random() < 0.05 else str(val))
                for sym, val in zip(symbols, df[col], strict=False)
            ]
        elif is_rate:
            df[col] = [
                f"{val * 100:.1f}%" if pd.notna(val) and isinstance(val, (int, float, np.number))
                else ("unknown" if rng.random() < 0.05 else str(val))
                for val in df[col]
            ]
        else:
            df[col] = [
                f"{val:,}" if pd.notna(val) and isinstance(val, (int, np.integer))
                else str(val)
                for val in df[col]
            ]

    for col in cols["categoricals"]:
        df[col] = [
            f"  {str(v).upper()} " if i % 4 == 0
            else f"{str(v).lower()}   " if i % 4 == 1
            else str(v).title()
            for i, v in enumerate(df[col])
        ]

    for col in cols["booleans"]:
        choices = ["Y", "yes", "YES", "N", "No", "NO", "1", "0", "true", "false", ""]
        df[col] = rng.choice(choices, size=n)

    if cols["time"] and cols["time"] in df.columns:
        dates = pd.to_datetime(df[cols["time"]], errors="coerce").fillna(pd.Timestamp("2026-08-01"))
        styles = rng.integers(0, 4, size=n)
        df[cols["time"]] = [
            (
                d.strftime("%Y-%m-%d") if s == 0
                else d.strftime("%d/%m/%Y") if s == 1
                else d.strftime("%d %b %Y") if s == 2
                else f"FY26-W{d.isocalendar().week:02d}"
            )
            for d, s in zip(dates, styles, strict=False)
        ]

    return df


def build_extreme_stress_test(df: pd.DataFrame, cols: dict, positive_label: object, rng: np.random.Generator) -> pd.DataFrame:
    df = df.copy()
    pk_col = cols["pk"]
    target_col = cols["target"]

    if pk_col:
        df = inject_corrupted_pks(df, pk_col, rng)
    if target_col:
        df = inject_corrupted_target(df, target_col, rng)
    df = inject_leakage_and_pii(df, target_col, rng)
    df = inject_schema_and_types(df, cols, rng)
    df = inject_formatting_chaos(df, cols, rng)

    for col in ["complaint_text", "question", "reference_answer"]:
        if col in df.columns:
            for idx in rng.choice(range(len(df)), size=min(15, len(df)), replace=False):
                orig = str(df.loc[idx, col])
                pii_text = f" [Contact: user_{idx}@example.invalid or +1-555-555-01{idx:02d}]"
                df.loc[idx, col] = orig + pii_text

    return df


def generate_all_bad_datasets():
    all_use_cases = load_all_use_cases()
    OUTPUT_BASE_DIR.mkdir(parents=True, exist_ok=True)

    summary_records = []
    print(f"Generating bad test CSV files for {len(all_use_cases)} use cases...")

    for i, (uc_id, config) in enumerate(sorted(all_use_cases.items()), start=1):
        folder_name = f"{i:02d}_{uc_id.replace('-', '_')}"
        uc_dir = OUTPUT_BASE_DIR / folder_name
        uc_dir.mkdir(parents=True, exist_ok=True)

        cols = _get_use_case_columns(config)
        rows = 3000 if uc_id != "ai-onboarding-assistant" else 65
        rng = np.random.default_rng(20260930 + i)

        base_df = _make_base_frame(uc_id, rows=rows, seed=100 + i)
        pos_label = config.target.positive_label if config.target.positive_label is not None else 1

        # File 0: 00_fixable_messy_helper_test.csv (The one to test the AI Helper's auto-fix capabilities!)
        fixable_df = build_fixable_messy_helper_test(base_df, cols, rng)
        fixable_path = uc_dir / "00_fixable_messy_helper_test.csv"
        fixable_df.to_csv(fixable_path, index=False)

        # File 1: Extreme Stress Test (All bad data combined)
        extreme_df = build_extreme_stress_test(base_df, cols, pos_label, rng)
        extreme_path = uc_dir / f"{uc_id.replace('-', '_')}_extreme_stress_test.csv"
        extreme_df.to_csv(extreme_path, index=False)

        # File 2: 01_corrupted_primary_keys.csv
        pks_df = inject_corrupted_pks(base_df, cols["pk"], rng)
        pks_path = uc_dir / "01_corrupted_primary_keys.csv"
        pks_df.to_csv(pks_path, index=False)

        # File 3: 02_corrupted_target.csv
        target_df = inject_corrupted_target(base_df, cols["target"], rng)
        target_path = uc_dir / "02_corrupted_target.csv"
        target_df.to_csv(target_path, index=False)

        # File 4: 03_leakage_and_pii_injection.csv
        leak_df = inject_leakage_and_pii(base_df, cols["target"], rng)
        leak_path = uc_dir / "03_leakage_and_pii_injection.csv"
        leak_df.to_csv(leak_path, index=False)

        # File 5: 04_schema_and_type_degradation.csv
        schema_df = inject_schema_and_types(base_df, cols, rng)
        schema_path = uc_dir / "04_schema_and_type_degradation.csv"
        schema_df.to_csv(schema_path, index=False)

        # File 6: 05_formatting_and_parsing_chaos.csv
        format_df = inject_formatting_chaos(base_df, cols, rng)
        format_path = uc_dir / "05_formatting_and_parsing_chaos.csv"
        format_df.to_csv(format_path, index=False)

        print(f"[{i:02d}/{len(all_use_cases)}] Generated 7 bad CSV files for: {uc_id}")
        summary_records.append({
            "use_case_id": uc_id,
            "folder": str(uc_dir),
            "fixable_file": str(fixable_path),
            "extreme_file": str(extreme_path),
            "files_count": 7,
            "rows": rows,
        })

    print(f"\nSuccessfully generated test suites across all {len(all_use_cases)} use cases in {OUTPUT_BASE_DIR}!")
    return summary_records


if __name__ == "__main__":
    generate_all_bad_datasets()
