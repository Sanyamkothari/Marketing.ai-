"""Write deliberately dirty CSV files for every configured use case, to try the engine's checks on.

    python -m scripts.generate_bad_test_data [--out-dir data/bad_data_tests] [--rows 3000] [--seed 20260930]

For each use case the output directory gets one folder, named by the use case's id, holding:

- `00_fixable_messy_helper_test.csv` - messy but usable: currency symbols, thousands separators and
  percentages in number columns, erratic casing and stray spaces in categories, `Y`/`no` booleans,
  mixed date formats, a personal email column and a column that leaks the outcome. Guided setup's
  helper is meant to propose a fix for each.
- `01_corrupted_primary_keys.csv` - 50 repeated keys and 20 empty ones.
- `02_corrupted_target.csv` - 100 outcome cells set to a third value.
- `03_leakage_and_pii_injection.csv` - a near-copy of the outcome, a contact email and a phone column.
- `04_schema_and_type_degradation.csv` - text in a number column, constant and mostly-empty columns,
  an ID-like column and week codes in the date column.
- `05_formatting_and_parsing_chaos.csv` - the formatting of file 00, with unreadable cells mixed in.
- `06_extreme_stress_test.csv` - all of the above at once.

A generative use case (the document assistant) has no table to train on, so its files are its
bundled reference question set (`tests/fixtures/docs/reference_qa.csv`), corrupted the same way where
a corruption applies. Every file is synthetic: built by `tests.fixtures.make_data`'s generator and the
corruptions here, from the use case's own template, so no real person's data is involved and the
emails and phone numbers are in reserved ranges (`example.invalid`, `+1-555-555-xxxx`).

The default output directory is under `data/`, which git ignores. `python -m scripts.audit_bad_data`
then runs the engine's checks over the files and writes a report beside them.
"""

from __future__ import annotations

import argparse
import math
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import numpy as np
import pandas as pd
from tests.fixtures.make_data import GenerationSpec, generate

from engine.config import AiType, ColumnRole, ColumnType, UseCaseConfig, load_all_use_cases

COMMAND: Final[str] = "python -m scripts.generate_bad_test_data"
REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR: Final[Path] = REPO_ROOT / "data" / "bad_data_tests"
"""Under `data/`, which `.gitignore` excludes: generated files never end up in a commit."""

REFERENCE_QA: Final[Path] = REPO_ROOT / "tests" / "fixtures" / "docs" / "reference_qa.csv"
"""The bundled reference question set, the base of a generative use case's files."""

DEFAULT_ROWS: Final[int] = 3_000
DEFAULT_SEED: Final[int] = 20_260_930
REFERENCE_ROWS: Final[int] = 65
"""Rows of a generative use case's files: the bundled set, repeated as variants up to this many."""

FIXABLE: Final[str] = "00_fixable_messy_helper_test.csv"
PRIMARY_KEYS: Final[str] = "01_corrupted_primary_keys.csv"
TARGET: Final[str] = "02_corrupted_target.csv"
LEAKAGE_AND_PII: Final[str] = "03_leakage_and_pii_injection.csv"
SCHEMA_AND_TYPES: Final[str] = "04_schema_and_type_degradation.csv"
FORMATTING: Final[str] = "05_formatting_and_parsing_chaos.csv"
EXTREME: Final[str] = "06_extreme_stress_test.csv"
FILE_NAMES: Final[tuple[str, ...]] = (
    FIXABLE,
    PRIMARY_KEYS,
    TARGET,
    LEAKAGE_AND_PII,
    SCHEMA_AND_TYPES,
    FORMATTING,
    EXTREME,
)
"""Every file a use case's folder gets, in the order they are written."""

FREE_TEXT_COLUMNS: Final[tuple[str, ...]] = ("complaint_text", "question", "reference_answer")
"""Text columns that get a contact detail written into some of their cells by the stress test."""


@dataclass(frozen=True)
class Columns:
    """The columns of a use case's template, by the role a corruption needs."""

    primary_key: str | None
    target: str | None
    time: str | None
    numbers: tuple[str, ...]
    categories: tuple[str, ...]
    booleans: tuple[str, ...]


@dataclass(frozen=True)
class Written:
    """One use case's folder: where it is and how many rows each file has."""

    use_case_id: str
    folder: Path
    rows: int


def columns_of(config: UseCaseConfig) -> Columns:
    """The template's columns by role; features only for the number, category and boolean lists."""
    template = config.template.columns

    def first(role: ColumnRole) -> str | None:
        return next((column.name for column in template if column.role is role), None)

    features = [
        column for column in template if column.role not in (ColumnRole.PRIMARY_KEY, ColumnRole.TARGET)
    ]
    return Columns(
        primary_key=first(ColumnRole.PRIMARY_KEY),
        target=first(ColumnRole.TARGET),
        time=first(ColumnRole.TIME),
        numbers=tuple(c.name for c in features if c.type in (ColumnType.INTEGER, ColumnType.FLOAT)),
        categories=tuple(c.name for c in features if c.type in (ColumnType.STRING, ColumnType.TEXT)),
        booleans=tuple(c.name for c in features if c.type is ColumnType.BOOLEAN),
    )


def base_frame(config: UseCaseConfig, *, rows: int, seed: int) -> pd.DataFrame:
    """A clean frame for `config`: the synthetic generator's, or the reference set for a generative one."""
    if config.ai_type is AiType.GENERATIVE:
        base = pd.read_csv(REFERENCE_QA)
        frames = [base]
        while sum(len(frame) for frame in frames) < REFERENCE_ROWS:
            variant = base.copy()
            variant["question"] = variant["question"] + f" (variant {len(frames)})"
            frames.append(variant)
        return pd.concat(frames, ignore_index=True).iloc[:REFERENCE_ROWS].copy()
    spec = GenerationSpec(use_case_id=config.id, rows=rows, variant="clean", positive_rate=0.25, seed=seed)
    return generate(spec).copy()


# ---------------------------------------------------------------------------
# The corruptions. Each returns a new frame and leaves its input as it was.
# ---------------------------------------------------------------------------
def corrupt_primary_keys(frame: pd.DataFrame, columns: Columns, rng: np.random.Generator) -> pd.DataFrame:
    """Up to 50 rows repeat the first row's key and up to 20 others have an empty one."""
    out = frame.copy()
    key = columns.primary_key
    if key is None or key not in out.columns or len(out) < 2:
        return out
    out[key] = out[key].astype(object)
    rest = np.arange(1, len(out))
    repeated = rng.choice(rest, size=min(50, len(rest)), replace=False)
    out.loc[repeated, key] = out.at[0, key]
    others = np.setdiff1d(rest, repeated)
    out.loc[rng.choice(others, size=min(20, len(others)), replace=False), key] = ""
    return out


def corrupt_target(frame: pd.DataFrame, columns: Columns, rng: np.random.Generator) -> pd.DataFrame:
    """Up to 100 outcome cells set to a third value: `99` in a number outcome, `Unknown` in a text one."""
    out = frame.copy()
    target = columns.target
    if target is None or target not in out.columns:
        return out
    third: int | str = 99 if pd.api.types.is_numeric_dtype(out[target]) else "Unknown"
    values = out[target].astype(object).tolist()
    for index in rng.choice(len(out), size=min(100, len(out)), replace=False):
        values[int(index)] = third
    out[target] = values
    return out


def _leak(
    frame: pd.DataFrame, target: str | None, rng: np.random.Generator, noise: float
) -> pd.Series | None:
    """A column that is the outcome plus a little noise: what a post-outcome score looks like."""
    if target is None or target not in frame.columns:
        return None
    first = frame[target].iloc[0]
    outcome = (frame[target] == first).astype(float)
    leak: pd.Series = (outcome + rng.normal(0.0, noise, size=len(frame))).round(4)
    return leak


def inject_leakage_and_pii(frame: pd.DataFrame, columns: Columns, rng: np.random.Generator) -> pd.DataFrame:
    """A near-copy of the outcome, a contact email and a phone number column."""
    out = frame.copy()
    leak = _leak(out, columns.target, rng, 0.02)
    if leak is not None:
        out["campaign_result_score"] = leak
    out["billing_contact_email"] = [f"client.user{i:04d}@example.invalid" for i in range(len(out))]
    out["billing_contact_phone"] = [
        f"+1-555-555-{int(n):04d}" for n in rng.integers(1000, 9999, size=len(out))
    ]
    return out


def degrade_schema_and_types(frame: pd.DataFrame, columns: Columns, rng: np.random.Generator) -> pd.DataFrame:
    """Text in a number column, a constant column, a mostly-empty one, an ID-like one, week codes as dates."""
    out = frame.copy()
    rows = len(out)
    if columns.numbers:
        number = columns.numbers[0]
        unknown = rng.random(rows) < 0.08
        out[number] = [
            "unknown" if flag else ("" if pd.isna(value) else str(value))
            for flag, value in zip(unknown, out[number], strict=True)
        ]
    out["data_source"] = "legacy_crm_export"
    out["survey_nps_score"] = [
        str(int(n)) if keep else ""
        for n, keep in zip(rng.integers(1, 10, size=rows), rng.random(rows) > 0.88, strict=True)
    ]
    out["external_tracking_uuid"] = [
        f"TX-{int(n)}-{i}" for i, n in enumerate(rng.integers(100_000, 999_999, size=rows))
    ]
    if columns.time is not None and columns.time in out.columns:
        out[columns.time] = [f"FY26-W{(i % 52) + 1:02d}" for i in range(rows)]
    return out


def _is_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, int | float | np.number):
        return False
    return not math.isnan(float(value))


def _reformat_numbers(
    frame: pd.DataFrame, columns: Columns, rng: np.random.Generator, *, unreadable: str | None
) -> None:
    """Currency and thousands separators on large numbers, percentages on rates, in place.

    With `unreadable`, about one cell in twenty of a large-number or rate column becomes that text.
    """
    rows = len(frame)
    for name in columns.numbers:
        present = frame[name].dropna()
        sample = present.iloc[0] if not present.empty else 100
        large = _is_number(sample) and float(sample) > 50
        rate = _is_number(sample) and 0.0 <= float(sample) <= 1.0
        spoil = rng.random(rows) < 0.05 if unreadable is not None else np.zeros(rows, dtype=bool)
        if large:
            symbols = rng.choice(["₹", "$", "Rs. "], size=rows)
            frame[name] = [
                unreadable if bad and unreadable else f"{symbol}{value:,.2f}" if _is_number(value) else ""
                for symbol, value, bad in zip(symbols, frame[name], spoil, strict=True)
            ]
        elif rate:
            frame[name] = [
                unreadable if bad and unreadable else f"{value * 100:.1f}%" if _is_number(value) else ""
                for value, bad in zip(frame[name], spoil, strict=True)
            ]
        else:
            frame[name] = [
                (
                    f"{value:,}"
                    if isinstance(value, int | np.integer)
                    else ("" if pd.isna(value) else str(value))
                )
                for value in frame[name]
            ]


def _reformat_dates(
    frame: pd.DataFrame, columns: Columns, rng: np.random.Generator, *, week_codes: bool
) -> None:
    """Day-first, ISO and `01 Aug 2026` dates mixed in the time column; with `week_codes`, `FY26-W31` too."""
    if columns.time is None or columns.time not in frame.columns:
        return
    dates = pd.to_datetime(frame[columns.time], errors="coerce", format="mixed").fillna(
        pd.Timestamp("2026-08-01")
    )
    formats = ["%d/%m/%Y", "%Y-%m-%d", "%d %b %Y"]
    styles = rng.integers(0, len(formats) + int(week_codes), size=len(frame))
    frame[columns.time] = [
        f"FY26-W{date.isocalendar().week:02d}" if style == len(formats) else date.strftime(formats[style])
        for date, style in zip(dates, styles, strict=True)
    ]


def fixable_messy(frame: pd.DataFrame, columns: Columns, rng: np.random.Generator) -> pd.DataFrame:
    """Messy but usable: keys and outcome intact, everything else in the shapes real exports arrive in."""
    out = frame.copy()
    _reformat_numbers(out, columns, rng, unreadable=None)
    for name in columns.categories:
        out[name] = [
            f" {str(v).upper()} " if i % 3 == 0 else f"{str(v).lower()} " if i % 3 == 1 else str(v)
            for i, v in enumerate(out[name])
        ]
    for name in columns.booleans:
        out[name] = rng.choice(["Y", "yes", "YES", "N", "No", "NO"], size=len(out))
    _reformat_dates(out, columns, rng, week_codes=False)
    out["contact_email"] = [f"user{i:04d}@example.invalid" for i in range(len(out))]
    leak = _leak(out, columns.target, rng, 0.03)
    if leak is not None:
        out["campaign_result_score"] = leak
    return out


def formatting_chaos(frame: pd.DataFrame, columns: Columns, rng: np.random.Generator) -> pd.DataFrame:
    """The formatting of `fixable_messy`, with unreadable cells, blanks and week codes mixed in."""
    out = frame.copy()
    _reformat_numbers(out, columns, rng, unreadable="N/A")
    for name in columns.categories:
        out[name] = [
            f"  {str(v).upper()} " if i % 4 == 0 else f"{str(v).lower()}   " if i % 4 == 1 else str(v).title()
            for i, v in enumerate(out[name])
        ]
    for name in columns.booleans:
        out[name] = rng.choice(
            ["Y", "yes", "YES", "N", "No", "NO", "1", "0", "true", "false", ""], size=len(out)
        )
    _reformat_dates(out, columns, rng, week_codes=True)
    return out


def extreme_stress(frame: pd.DataFrame, columns: Columns, rng: np.random.Generator) -> pd.DataFrame:
    """Every corruption above at once, plus contact details written into free-text cells."""
    out = frame
    for corrupt in (
        corrupt_primary_keys,
        corrupt_target,
        inject_leakage_and_pii,
        degrade_schema_and_types,
        formatting_chaos,
    ):
        out = corrupt(out, columns, rng)
    for name in FREE_TEXT_COLUMNS:
        if name in out.columns:
            out[name] = out[name].astype(object)
            for index in rng.choice(len(out), size=min(15, len(out)), replace=False):
                out.at[index, name] = (
                    f"{out.at[index, name]} [Contact: user_{index}@example.invalid or +1-555-555-{index % 10_000:04d}]"
                )
    return out


Corruption = Callable[[pd.DataFrame, Columns, np.random.Generator], pd.DataFrame]
CORRUPTIONS: Final[dict[str, Corruption]] = {
    FIXABLE: fixable_messy,
    PRIMARY_KEYS: corrupt_primary_keys,
    TARGET: corrupt_target,
    LEAKAGE_AND_PII: inject_leakage_and_pii,
    SCHEMA_AND_TYPES: degrade_schema_and_types,
    FORMATTING: formatting_chaos,
    EXTREME: extreme_stress,
}


def write_use_case(config: UseCaseConfig, out_dir: Path, *, rows: int, seed: int) -> Written:
    """Write every file of `FILE_NAMES` for `config` into `out_dir / config.id`."""
    folder = out_dir / config.id
    folder.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    base = base_frame(config, rows=rows, seed=seed)
    columns = columns_of(config)
    for name in FILE_NAMES:
        CORRUPTIONS[name](base, columns, rng).to_csv(folder / name, index=False)
    return Written(use_case_id=config.id, folder=folder, rows=len(base))


def generate_all(out_dir: Path, *, rows: int, seed: int, use_case_ids: Sequence[str] = ()) -> list[Written]:
    """Write the files for every configured use case (or only `use_case_ids`), in id order."""
    configs = load_all_use_cases()
    unknown = sorted(set(use_case_ids) - set(configs))
    if unknown:
        raise SystemExit(f"Unknown use case id: {', '.join(unknown)}. Known: {', '.join(sorted(configs))}.")
    chosen = sorted(use_case_ids) if use_case_ids else sorted(configs)
    written: list[Written] = []
    for offset, use_case_id in enumerate(chosen):
        done = write_use_case(configs[use_case_id], out_dir, rows=rows, seed=seed + offset)
        print(
            f"[{offset + 1:02d}/{len(chosen)}] {len(FILE_NAMES)} files, {done.rows} rows each: {done.folder}"
        )
        written.append(done)
    return written


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=COMMAND, description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help=f"default: {DEFAULT_OUT_DIR}")
    parser.add_argument("--rows", type=int, default=DEFAULT_ROWS, help="rows per predictive file")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="the same seed writes the same files")
    parser.add_argument("--use-case", action="append", default=[], help="only this use case id; repeatable")
    args = parser.parse_args(argv)
    if args.rows < 2:
        parser.error("--rows must be at least 2")
    written = generate_all(args.out_dir, rows=args.rows, seed=args.seed, use_case_ids=args.use_case)
    print(f"Wrote {len(written) * len(FILE_NAMES)} files for {len(written)} use cases under {args.out_dir}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
