"""Tests for the synthetic data generator itself (plan §10).

`make_data.py` is test infrastructure that every later M2/M3/M4 test leans on, so it gets the same
treatment as engine code: determinism, the exact template header, row counts, the planted signal,
and — for each broken variant — proof that the structural property it claims is really there.
"""

from __future__ import annotations

import csv
import re
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import train_test_split

from engine.config import ColumnRole, ColumnType, TemplateColumn, UseCaseConfig, load_use_case
from tests.fixtures.make_data import (
    CLEAN,
    CONSTANT_COLUMN,
    HIGH_NULL_COLUMN,
    ID_LIKE_COLUMN,
    LEAKY_COLUMN,
    PII_EMAIL_COLUMN,
    PII_PHONE_COLUMN,
    SCORING,
    TYPE_CHANGE_SENTINEL,
    VARIANT_SPECS,
    VARIANTS,
    GenerationSpec,
    default_output_dir,
    generate,
    main,
    maker_for,
    predictive_use_case_ids,
    write_csv,
)

USE_CASE_IDS = predictive_use_case_ids()
SMALL = 2_000  # rows for the many per-variant tests; the shape checks do not need 10k

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
PHONE_RE = re.compile(r"^\+?\d[\d\-() ]{7,}\d$")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def config_of(use_case_id: str) -> UseCaseConfig:
    return load_use_case(use_case_id)


def target_name(config: UseCaseConfig) -> str:
    return config.template.by_role(ColumnRole.TARGET)[0].name


def key_name(config: UseCaseConfig) -> str:
    key = config.template.primary_key
    assert key is not None
    return key.name


def feature_matrix(config: UseCaseConfig, frame: pd.DataFrame) -> np.ndarray:
    """One-hot + median-imputed design matrix, the way a plain baseline model would build it."""
    parts: list[pd.DataFrame] = []
    for column in config.template.by_role(ColumnRole.FEATURE):
        series = frame[column.name]
        if column.type in (ColumnType.INTEGER, ColumnType.FLOAT):
            numeric = series.astype("float64")
            parts.append(numeric.fillna(numeric.median()).to_frame())
        elif column.type is ColumnType.BOOLEAN:
            parts.append((series == "true").astype(float).to_frame())
        elif column.type is ColumnType.STRING:
            parts.append(pd.get_dummies(series, prefix=column.name, dtype=float))
    matrix = pd.concat(parts, axis=1).to_numpy(dtype=float)
    spread = matrix.std(axis=0)
    return (matrix - matrix.mean(axis=0)) / np.where(spread == 0.0, 1.0, spread)


# ---------------------------------------------------------------------------
# The variant table
# ---------------------------------------------------------------------------
def test_every_code_the_plan_asks_for_has_a_variant() -> None:
    required = {
        "PK_NOT_UNIQUE",
        "PK_NULLS",
        "LEAKAGE_SUSPECTED",
        "TARGET_TOO_FEW_POSITIVES",
        "ROWS_TOO_FEW",
        "TARGET_CONSTANT",
        "TARGET_NOT_BINARY",
        "CONSTANT_COLUMN",
        "HIGH_NULL_COLUMN",
        "PII_DETECTED",
        "HIGH_CARDINALITY_ID_LIKE",
        "TIME_COLUMN_UNPARSEABLE",
        "SCHEMA_MISMATCH",
    }
    assert required <= {code for code in VARIANTS.values() if code is not None}


def test_the_two_healthy_variants_carry_no_code() -> None:
    assert VARIANTS[CLEAN] is None
    assert VARIANTS[SCORING] is None
    assert VARIANT_SPECS[CLEAN].has_target is True
    assert VARIANT_SPECS[SCORING].has_target is False


def test_variants_and_specs_agree() -> None:
    assert set(VARIANTS) == set(VARIANT_SPECS)
    assert all(VARIANTS[name] == spec.code for name, spec in VARIANT_SPECS.items())


def test_an_unknown_variant_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown variant"):
        GenerationSpec("targeted-advertisement", variant="nope")


@pytest.mark.parametrize(("rows", "rate"), [(0, 0.12), (10, 0.0), (10, 1.0)])
def test_a_nonsense_spec_is_refused(rows: int, rate: float) -> None:
    with pytest.raises(ValueError):
        GenerationSpec("targeted-advertisement", rows=rows, positive_rate=rate)


# ---------------------------------------------------------------------------
# Config driven: every shipped column has a registered maker
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("use_case_id", USE_CASE_IDS)
def test_every_non_target_column_has_a_maker(use_case_id: str) -> None:
    for column in config_of(use_case_id).template.columns:
        if column.role is not ColumnRole.TARGET:
            assert callable(maker_for(column))


def test_an_unregistered_role_type_pair_fails_loudly() -> None:
    column = TemplateColumn(
        name="weird",
        role=ColumnRole.CONSENT,
        type=ColumnType.TEXT,
        description="not a pair the registry knows",
        examples=("a", "b", "c", "d", "e"),
    )
    with pytest.raises(KeyError, match="no synthetic-data maker registered"):
        maker_for(column)


def test_the_six_predictive_use_cases_are_covered() -> None:
    assert USE_CASE_IDS == (
        "fault-prediction",
        "order-fulfillment",
        "payment-propensity",
        "rca",
        "targeted-advertisement",
        "win-back-campaign",
    )


# ---------------------------------------------------------------------------
# Shape: header, row count, dtypes
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("use_case_id", USE_CASE_IDS)
def test_header_matches_the_committed_template_exactly(use_case_id: str, repo_root: Path) -> None:
    frame = generate(GenerationSpec(use_case_id, rows=SMALL))
    template = repo_root / "templates" / f"{use_case_id.replace('-', '_')}_template.csv"
    with template.open(newline="", encoding="utf-8") as handle:
        expected = next(iter(csv.reader(handle)))
    assert list(frame.columns) == expected
    assert list(frame.columns) == list(config_of(use_case_id).template.column_names)


@pytest.mark.parametrize("use_case_id", USE_CASE_IDS)
def test_written_csv_header_matches_the_template(use_case_id: str, tmp_path: Path) -> None:
    written = write_csv(GenerationSpec(use_case_id, rows=200), tmp_path / "out.csv")
    first_line = written.read_text(encoding="utf-8").splitlines()[0]
    assert first_line == ",".join(config_of(use_case_id).template.column_names)


@pytest.mark.parametrize("use_case_id", USE_CASE_IDS)
def test_row_count_is_what_was_asked_for(use_case_id: str) -> None:
    assert len(generate(GenerationSpec(use_case_id, rows=SMALL))) == SMALL


def test_the_default_is_ten_thousand_rows() -> None:
    assert len(generate(GenerationSpec("targeted-advertisement"))) == 10_000


@pytest.mark.parametrize("use_case_id", USE_CASE_IDS)
def test_primary_key_is_unique_and_non_null_in_clean_data(use_case_id: str) -> None:
    config = config_of(use_case_id)
    frame = generate(GenerationSpec(use_case_id, rows=SMALL))
    keys = frame[key_name(config)]
    assert keys.is_unique
    assert keys.notna().all()


@pytest.mark.parametrize("use_case_id", USE_CASE_IDS)
def test_target_is_binary_zero_one(use_case_id: str) -> None:
    config = config_of(use_case_id)
    values = set(generate(GenerationSpec(use_case_id, rows=SMALL))[target_name(config)].dropna())
    assert values == {0, 1}


# ---------------------------------------------------------------------------
# Realistic shape
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("use_case_id", USE_CASE_IDS)
def test_snapshot_dates_are_iso_and_spread_out(use_case_id: str) -> None:
    config = config_of(use_case_id)
    time_column = config.template.by_role(ColumnRole.TIME)[0]
    values = generate(GenerationSpec(use_case_id, rows=SMALL))[time_column.name]
    assert values.map(lambda value: bool(ISO_DATE_RE.match(value))).all()
    parsed = pd.to_datetime(values)
    assert values.nunique() >= 20
    assert (parsed.max() - parsed.min()).days >= 300


@pytest.mark.parametrize("use_case_id", USE_CASE_IDS)
def test_a_time_ordered_split_sees_a_different_positive_rate(use_case_id: str) -> None:
    """The planted time trend is what makes an ordered (time-based) split meaningful."""
    config = config_of(use_case_id)
    frame = generate(GenerationSpec(use_case_id))
    ordered = frame.sort_values(config.template.by_role(ColumnRole.TIME)[0].name, kind="stable")
    target = ordered[target_name(config)].astype(float)
    cut = int(len(ordered) * 0.7)
    assert float(target.iloc[cut:].mean()) > float(target.iloc[:cut].mean())


@pytest.mark.parametrize("use_case_id", USE_CASE_IDS)
def test_consent_column_is_a_lowercase_boolean_mostly_true(use_case_id: str) -> None:
    config = config_of(use_case_id)
    consent = config.template.by_role(ColumnRole.CONSENT)[0]
    values = generate(GenerationSpec(use_case_id, rows=SMALL))[consent.name]
    assert set(values.unique()) == {"true", "false"}
    assert 0.5 < float((values == "true").mean()) < 0.95


@pytest.mark.parametrize("use_case_id", USE_CASE_IDS)
def test_last_contacted_at_is_sometimes_blank_and_never_after_the_snapshot(use_case_id: str) -> None:
    config = config_of(use_case_id)
    contact = config.template.by_role(ColumnRole.CONTACT)[0]
    snapshot = config.template.by_role(ColumnRole.TIME)[0]
    frame = generate(GenerationSpec(use_case_id, rows=SMALL))
    blank = frame[contact.name] == ""
    assert 0.05 < float(blank.mean()) < 0.8
    filled = frame.loc[~blank]
    assert (pd.to_datetime(filled[contact.name]) <= pd.to_datetime(filled[snapshot.name])).all()


@pytest.mark.parametrize("use_case_id", USE_CASE_IDS)
def test_a_few_feature_columns_have_plausible_missing_values(use_case_id: str) -> None:
    config = config_of(use_case_id)
    frame = generate(GenerationSpec(use_case_id))
    rates = {
        column.name: float(frame[column.name].isna().mean())
        for column in config.template.by_role(ColumnRole.FEATURE)
    }
    holed = {name: rate for name, rate in rates.items() if rate > 0.0}
    assert 1 <= len(holed) <= 3
    assert all(0.01 < rate < 0.20 for rate in holed.values()), holed


@pytest.mark.parametrize("use_case_id", USE_CASE_IDS)
def test_category_levels_come_from_the_template_examples(use_case_id: str) -> None:
    config = config_of(use_case_id)
    frame = generate(GenerationSpec(use_case_id, rows=SMALL))
    for column in config.template.by_role(ColumnRole.FEATURE):
        if column.type is not ColumnType.STRING:
            continue
        assert set(frame[column.name].unique()) <= set(column.examples)


@pytest.mark.parametrize("use_case_id", USE_CASE_IDS)
def test_numeric_features_stay_inside_a_range_the_examples_justify(use_case_id: str) -> None:
    config = config_of(use_case_id)
    frame = generate(GenerationSpec(use_case_id))
    for column in config.template.by_role(ColumnRole.FEATURE):
        if column.type not in (ColumnType.INTEGER, ColumnType.FLOAT):
            continue
        examples = [float(value) for value in column.examples if value.strip()]
        low, high = min(examples), max(examples)
        spread = max(high - low, 1.0)
        values = frame[column.name].astype("float64").dropna()
        assert values.min() >= low - 2.0 * spread, column.name
        assert values.max() <= high + 4.0 * spread, column.name


# ---------------------------------------------------------------------------
# Positive rate
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("use_case_id", USE_CASE_IDS)
def test_positive_rate_lands_near_the_default(use_case_id: str) -> None:
    config = config_of(use_case_id)
    frame = generate(GenerationSpec(use_case_id))
    assert float(frame[target_name(config)].astype(float).mean()) == pytest.approx(0.12, abs=0.02)


@pytest.mark.parametrize("requested", [0.05, 0.25, 0.45])
def test_positive_rate_follows_the_request(requested: float) -> None:
    frame = generate(GenerationSpec("targeted-advertisement", positive_rate=requested))
    assert float(frame["converted_30d"].astype(float).mean()) == pytest.approx(requested, abs=0.02)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("use_case_id", USE_CASE_IDS)
def test_same_seed_gives_byte_identical_csv(use_case_id: str, tmp_path: Path) -> None:
    spec = GenerationSpec(use_case_id, rows=SMALL)
    first = write_csv(spec, tmp_path / "a.csv").read_bytes()
    second = write_csv(spec, tmp_path / "b.csv").read_bytes()
    assert first == second


def test_a_different_seed_gives_different_data(tmp_path: Path) -> None:
    a = write_csv(GenerationSpec("targeted-advertisement", rows=SMALL, seed=1), tmp_path / "a.csv")
    b = write_csv(GenerationSpec("targeted-advertisement", rows=SMALL, seed=2), tmp_path / "b.csv")
    assert a.read_bytes() != b.read_bytes()


def test_determinism_survives_a_fresh_interpreter(repo_root: Path, tmp_path: Path) -> None:
    """A separate process has a different PYTHONHASHSEED; the bytes must not move."""
    script = (
        "import sys;"
        "from tests.fixtures.make_data import GenerationSpec, write_csv;"
        "from pathlib import Path;"
        "write_csv(GenerationSpec('targeted-advertisement', rows=500), Path(sys.argv[1]))"
    )
    here = write_csv(GenerationSpec("targeted-advertisement", rows=500), tmp_path / "here.csv")
    there = tmp_path / "there.csv"
    completed = subprocess.run(
        [sys.executable, "-c", script, str(there)],
        cwd=repo_root,
        env={"PYTHONPATH": str(repo_root), "PATH": "/usr/bin:/bin", "PYTHONHASHSEED": "7"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert here.read_bytes() == there.read_bytes()


def test_one_use_case_does_not_perturb_another() -> None:
    """Per-column seeding means the draws of one column never depend on another's."""
    first = generate(GenerationSpec("targeted-advertisement", rows=500))
    generate(GenerationSpec("rca", rows=777))
    second = generate(GenerationSpec("targeted-advertisement", rows=500))
    pd.testing.assert_frame_equal(first, second)


# ---------------------------------------------------------------------------
# The golden check (plan §10)
# ---------------------------------------------------------------------------
def test_logistic_regression_clears_the_golden_roc_auc_bar() -> None:
    config = config_of("targeted-advertisement")
    frame = generate(GenerationSpec("targeted-advertisement"))
    labels = frame[target_name(config)].astype(int).to_numpy()
    matrix = feature_matrix(config, frame)
    train_x, test_x, train_y, test_y = train_test_split(
        matrix, labels, test_size=0.25, random_state=0, stratify=labels
    )
    model = LogisticRegression(max_iter=2000).fit(train_x, train_y)
    achieved = roc_auc_score(test_y, model.predict_proba(test_x)[:, 1])
    assert achieved > 0.7, f"planted signal is too weak: ROC-AUC {achieved:.4f}"


@pytest.mark.parametrize("use_case_id", USE_CASE_IDS)
def test_every_use_case_carries_a_learnable_signal(use_case_id: str) -> None:
    config = config_of(use_case_id)
    frame = generate(GenerationSpec(use_case_id))
    labels = frame[target_name(config)].astype(int).to_numpy()
    matrix = feature_matrix(config, frame)
    train_x, test_x, train_y, test_y = train_test_split(
        matrix, labels, test_size=0.25, random_state=0, stratify=labels
    )
    model = LogisticRegression(max_iter=2000).fit(train_x, train_y)
    assert roc_auc_score(test_y, model.predict_proba(test_x)[:, 1]) > 0.7


def test_the_truth_is_not_purely_linear() -> None:
    """A gradient-boosted tree must be able to beat the linear baseline, or M3's golden check
    ("test ROC-AUC must exceed the logistic baseline") would be unwinnable on correct code."""
    from sklearn.ensemble import HistGradientBoostingClassifier

    config = config_of("targeted-advertisement")
    frame = generate(GenerationSpec("targeted-advertisement"))
    labels = frame[target_name(config)].astype(int).to_numpy()
    matrix = feature_matrix(config, frame)
    train_x, test_x, train_y, test_y = train_test_split(
        matrix, labels, test_size=0.25, random_state=0, stratify=labels
    )
    linear = LogisticRegression(max_iter=2000).fit(train_x, train_y)
    tree = HistGradientBoostingClassifier(random_state=0).fit(train_x, train_y)
    linear_auc = roc_auc_score(test_y, linear.predict_proba(test_x)[:, 1])
    tree_auc = roc_auc_score(test_y, tree.predict_proba(test_x)[:, 1])
    assert tree_auc > linear_auc


# ---------------------------------------------------------------------------
# The scoring variant
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("use_case_id", USE_CASE_IDS)
def test_scoring_variant_has_every_column_but_the_target(use_case_id: str) -> None:
    config = config_of(use_case_id)
    frame = generate(GenerationSpec(use_case_id, rows=SMALL, variant=SCORING))
    assert target_name(config) not in frame.columns
    expected = [name for name in config.template.column_names if name != target_name(config)]
    assert list(frame.columns) == expected
    assert len(frame) == SMALL


# ---------------------------------------------------------------------------
# Broken variants: each really has the structural property it claims
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("use_case_id", USE_CASE_IDS)
@pytest.mark.parametrize("variant", sorted(VARIANTS))
def test_every_variant_generates_for_every_use_case(use_case_id: str, variant: str) -> None:
    frame = generate(GenerationSpec(use_case_id, rows=SMALL, variant=variant))
    assert len(frame) > 0
    assert len(frame.columns) >= len(config_of(use_case_id).template.columns) - 2


def test_duplicate_keys_really_repeat() -> None:
    config = config_of("targeted-advertisement")
    frame = generate(GenerationSpec("targeted-advertisement", rows=SMALL, variant="duplicate_keys"))
    keys = frame[key_name(config)]
    assert not keys.is_unique
    assert int(keys.duplicated().sum()) == 50
    assert keys.notna().all()  # only the uniqueness is wrong


def test_null_keys_really_are_null() -> None:
    config = config_of("targeted-advertisement")
    frame = generate(GenerationSpec("targeted-advertisement", rows=SMALL, variant="null_keys"))
    keys = frame[key_name(config)]
    assert int(keys.isna().sum()) == 20
    assert keys.dropna().is_unique  # only the nulls are wrong


def test_the_leaky_column_is_near_perfectly_predictive() -> None:
    config = config_of("targeted-advertisement")
    frame = generate(GenerationSpec("targeted-advertisement", variant="leaky_column"))
    assert LEAKY_COLUMN in frame.columns
    labels = frame[target_name(config)].astype(int).to_numpy()
    single = frame[LEAKY_COLUMN].astype(float).to_numpy()
    assert roc_auc_score(labels, single) > 0.98
    # ...but not so fine-grained that it also looks like an identifier
    assert frame[LEAKY_COLUMN].nunique() < len(frame) / 10


def test_too_few_positives_is_below_min_positive_but_not_severely_imbalanced() -> None:
    config = config_of("targeted-advertisement")
    frame = generate(GenerationSpec("targeted-advertisement", variant="too_few_positives"))
    positives = int(frame[target_name(config)].astype(int).sum())
    assert positives == 150
    assert positives < config.validation.min_positive
    assert len(frame) >= config.validation.min_rows
    assert positives / len(frame) > 0.01  # keeps TARGET_IMBALANCE_SEVERE quiet


def test_too_few_rows_is_below_min_rows_and_nothing_else() -> None:
    config = config_of("targeted-advertisement")
    frame = generate(GenerationSpec("targeted-advertisement", variant="too_few_rows"))
    assert len(frame) == 900
    assert len(frame) < config.validation.min_rows
    positives = int(frame[target_name(config)].astype(int).sum())
    assert positives >= config.validation.min_positive
    assert frame[key_name(config)].is_unique


def test_constant_target_has_one_level() -> None:
    config = config_of("targeted-advertisement")
    frame = generate(GenerationSpec("targeted-advertisement", rows=SMALL, variant="constant_target"))
    assert frame[target_name(config)].nunique() == 1


def test_non_binary_target_has_three_levels() -> None:
    config = config_of("targeted-advertisement")
    frame = generate(GenerationSpec("targeted-advertisement", variant="non_binary_target"))
    values = frame[target_name(config)].astype(int)
    assert set(values.unique()) == {0, 1, 2}
    assert int((values == 2).sum()) == 120
    assert int((values == 1).sum()) >= config.validation.min_positive


def test_constant_column_has_one_distinct_value() -> None:
    frame = generate(GenerationSpec("targeted-advertisement", rows=SMALL, variant="constant_column"))
    assert frame[CONSTANT_COLUMN].nunique() == 1


def test_high_null_column_is_above_the_sixty_percent_line() -> None:
    frame = generate(GenerationSpec("targeted-advertisement", variant="high_null_column"))
    rate = float(frame[HIGH_NULL_COLUMN].isna().mean())
    assert rate > 0.60
    assert frame[HIGH_NULL_COLUMN].dropna().nunique() > 1  # high-null, not constant


def test_pii_columns_match_an_email_and_a_phone_pattern() -> None:
    frame = generate(GenerationSpec("targeted-advertisement", rows=SMALL, variant="pii_column"))
    emails = frame[PII_EMAIL_COLUMN]
    phones = frame[PII_PHONE_COLUMN]
    assert emails.map(lambda value: bool(EMAIL_RE.match(value))).all()
    assert phones.map(lambda value: bool(PHONE_RE.match(value))).all()
    # Unmistakably fake, and not a per-row identifier
    assert emails.str.endswith("@example.invalid").all()
    assert phones.str.startswith("+1-555-555-").all()
    assert emails.nunique() < len(frame) / 2


def test_id_like_column_is_distinct_on_every_row_and_is_not_the_key() -> None:
    config = config_of("targeted-advertisement")
    frame = generate(GenerationSpec("targeted-advertisement", rows=SMALL, variant="id_like_column"))
    assert frame[ID_LIKE_COLUMN].nunique() == len(frame)
    assert key_name(config) != ID_LIKE_COLUMN
    assert frame[key_name(config)].is_unique


@pytest.mark.parametrize("use_case_id", USE_CASE_IDS)
def test_unparseable_time_really_does_not_parse(use_case_id: str) -> None:
    config = config_of(use_case_id)
    time_name = config.template.by_role(ColumnRole.TIME)[0].name
    frame = generate(GenerationSpec(use_case_id, rows=SMALL, variant="unparseable_time"))
    values = frame[time_name]
    assert values.map(lambda value: not ISO_DATE_RE.match(value)).all()
    parsed = pd.to_datetime(values, errors="coerce", format="ISO8601")
    assert parsed.isna().all()


def test_renamed_column_renames_exactly_one_and_drops_the_target() -> None:
    config = config_of("targeted-advertisement")
    clean = generate(GenerationSpec("targeted-advertisement", rows=SMALL, variant=SCORING))
    broken = generate(GenerationSpec("targeted-advertisement", rows=SMALL, variant="renamed_column"))
    assert target_name(config) not in broken.columns
    missing = set(clean.columns) - set(broken.columns)
    extra = set(broken.columns) - set(clean.columns)
    assert len(missing) == 1
    assert extra == {f"{next(iter(missing))}_v2"}


def test_missing_column_drops_exactly_one_feature() -> None:
    config = config_of("targeted-advertisement")
    clean = generate(GenerationSpec("targeted-advertisement", rows=SMALL, variant=SCORING))
    broken = generate(GenerationSpec("targeted-advertisement", rows=SMALL, variant="missing_column"))
    missing = set(clean.columns) - set(broken.columns)
    assert len(missing) == 1
    assert next(iter(missing)) in {column.name for column in config.template.by_role(ColumnRole.FEATURE)}
    assert set(broken.columns) - set(clean.columns) == set()


def test_type_changed_column_really_changes_type(tmp_path: Path) -> None:
    config = config_of("targeted-advertisement")
    spec = GenerationSpec("targeted-advertisement", rows=SMALL, variant="type_changed_column")
    frame = generate(spec)
    numeric = next(
        column
        for column in config.template.by_role(ColumnRole.FEATURE)
        if column.type in (ColumnType.INTEGER, ColumnType.FLOAT)
    )
    assert set(frame.columns) == set(
        generate(GenerationSpec("targeted-advertisement", rows=SMALL, variant=SCORING)).columns
    )
    reread = pd.read_csv(write_csv(spec, tmp_path / "typed.csv"))
    assert reread[numeric.name].dtype == object
    assert (reread[numeric.name] == TYPE_CHANGE_SENTINEL).any()


# ---------------------------------------------------------------------------
# write_csv and the CLI
# ---------------------------------------------------------------------------
def test_write_csv_round_trips_through_pandas(tmp_path: Path) -> None:
    config = config_of("targeted-advertisement")
    spec = GenerationSpec("targeted-advertisement", rows=SMALL)
    path = write_csv(spec, tmp_path / "nested" / "out.csv")
    assert path.exists()
    reread = pd.read_csv(path)
    assert list(reread.columns) == list(config.template.column_names)
    assert len(reread) == SMALL
    assert not reread.to_csv(index=False, lineterminator="\n").startswith("﻿")


def test_written_booleans_and_dates_look_like_the_template(tmp_path: Path) -> None:
    path = write_csv(GenerationSpec("targeted-advertisement", rows=200), tmp_path / "out.csv")
    row = path.read_text(encoding="utf-8").splitlines()[1].split(",")
    assert row[7] in {"true", "false"}
    assert ISO_DATE_RE.match(row[1])
    assert row[9] in {"0", "1"}  # Int64 target, never "1.0"


def test_default_output_dir_is_outside_the_checkout(repo_root: Path) -> None:
    assert repo_root not in default_output_dir().parents
    assert default_output_dir() != repo_root


def test_file_name_says_synthetic() -> None:
    spec = GenerationSpec("targeted-advertisement", variant="leaky_column")
    assert spec.file_name == "targeted_advertisement_synthetic_leaky_column.csv"


def test_main_writes_the_files_it_is_asked_for(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(
        [
            "--use-case",
            "targeted-advertisement",
            "--variant",
            CLEAN,
            "--variant",
            "duplicate_keys",
            "--rows",
            "300",
            "--out-dir",
            str(tmp_path),
        ]
    )
    assert code == 0
    written = sorted(path.name for path in tmp_path.glob("*.csv"))
    assert written == [
        "targeted_advertisement_synthetic_clean.csv",
        "targeted_advertisement_synthetic_duplicate_keys.csv",
    ]
    assert str(tmp_path) in capsys.readouterr().out


def test_main_can_list_the_variant_table(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--list-variants"]) == 0
    printed = capsys.readouterr().out
    for name, code in VARIANTS.items():
        assert name in printed
        if code is not None:
            assert code in printed
