"""Plan section 10 and milestone M6: the config-only reuse claim, proved rather than asserted.

The public **Kaggle Telco Customer Churn** file (BlastChar's copy of the IBM sample) is mapped onto
this engine by `configs/use_cases/telco_churn.yaml` alone. Nothing under `engine/` or `api/` knows
the dataset exists, and `test_no_engine_or_api_file_names_either_reused_use_case` pins that: the two
halves of M6, `payment-propensity` and the Telco mapping, must both be invisible to the code.

The decisive test is :func:`test_the_telco_frame_validates_with_no_errors`. It builds a frame with
exactly the dataset's 21 columns and the dtypes `pandas.read_csv` gives them, then runs it through
the real config loader and the real validate stage. The two things that make this file a *test* of
the claim rather than a restatement of it:

* the target is the string pair ``Yes`` / ``No``, not the ``0`` / ``1`` every shipped use case uses
  (plan section 4.1: a binary target may carry any two labels), and
* the file has no date column, so the split must be random rather than time-based.

The Kaggle file is not in the repository and is never downloaded here. The frame below is
synthesised from the dataset's published *schema* - its column names, its value vocabularies and its
7,043 rows / 1,869 churners - and reproduces no row of it.
"""

from __future__ import annotations

import functools
import random
import re
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pandas as pd
import pytest

from engine.config import (
    ColumnRole,
    ProblemType,
    SplitType,
    UseCaseConfig,
    list_use_case_ids,
    load_industry,
    load_use_case,
)
from engine.contracts import Severity
from engine.stages.ingest import read_table
from engine.stages.prepare import prepare_rows, split_dataset
from engine.stages.validate import params_from_config, resolve_positive_label, validate_for_training
from engine.storage import LocalStorage
from engine.templates import render_template_csv, template_filenames

if TYPE_CHECKING:
    from collections.abc import Callable

    from engine.contracts import ValidationReport

USE_CASE_ID: Final[str] = "telco-churn"
"""The use case the Kaggle file is mapped to. It exists as one YAML file and nothing else."""

PARTNER_USE_CASE_ID: Final[str] = "payment-propensity"
"""M6's other half: it has to stay code-free too, or the claim is only half proved."""

TELCO_COLUMNS: Final[tuple[str, ...]] = (
    "customerID",
    "gender",
    "SeniorCitizen",
    "Partner",
    "Dependents",
    "tenure",
    "PhoneService",
    "MultipleLines",
    "InternetService",
    "OnlineSecurity",
    "OnlineBackup",
    "DeviceProtection",
    "TechSupport",
    "StreamingTV",
    "StreamingMovies",
    "Contract",
    "PaperlessBilling",
    "PaymentMethod",
    "MonthlyCharges",
    "TotalCharges",
    "Churn",
)
"""The dataset's header, in file order."""

TELCO_DTYPES: Final[dict[str, str]] = {
    name: (
        "int64"
        if name in {"SeniorCitizen", "tenure"}
        else "float64" if name == "MonthlyCharges" else "object"
    )
    for name in TELCO_COLUMNS
}
"""What `pandas.read_csv` gives each column. `TotalCharges` is `object`: it is blank at tenure 0."""

DATASET_ROWS: Final[int] = 7_043
DATASET_POSITIVES: Final[int] = 1_869
"""The published shape of the file: 7,043 subscribers, 1,869 of them churned."""

TARGET: Final[str] = "Churn"
PRIMARY_KEY: Final[str] = "customerID"
POSITIVE_LABEL: Final[str] = "Yes"
NEGATIVE_LABEL: Final[str] = "No"

_YES_NO: Final[tuple[str, str]] = ("Yes", "No")
_YES_NO_INTERNET: Final[tuple[str, str, str]] = ("Yes", "No", "No internet service")
_INTERNET: Final[tuple[str, str, str]] = ("DSL", "Fiber optic", "No")
_CONTRACT: Final[tuple[str, str, str]] = ("Month-to-month", "One year", "Two year")
_PAYMENT: Final[tuple[str, ...]] = (
    "Electronic check",
    "Mailed check",
    "Bank transfer (automatic)",
    "Credit card (automatic)",
)
_LETTERS: Final[str] = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_SEED: Final[int] = 20_260_921


def _constant(value: str) -> str:
    """A named stand-in for a constant-returning lambda, so the module stays `mypy --strict` clean."""
    return value


def telco_frame(rows: int = DATASET_ROWS, positives: int = DATASET_POSITIVES) -> pd.DataFrame:
    """A frame with the Telco header, value vocabularies and dtypes. Deterministic, never a real row."""
    rng = random.Random(_SEED)
    labels = [POSITIVE_LABEL] * positives + [NEGATIVE_LABEL] * (rows - positives)
    rng.shuffle(labels)
    records: list[dict[str, object]] = []
    for index in range(rows):
        tenure = rng.randint(0, 72)
        monthly = round(rng.uniform(18.25, 118.75), 2)
        phone = rng.choice(_YES_NO)
        internet = rng.choice(_INTERNET)
        add_on: Callable[[], str] = (
            functools.partial(rng.choice, _YES_NO_INTERNET)
            if internet != "No"
            else functools.partial(_constant, "No internet service")
        )
        suffix = "".join(rng.choice(_LETTERS) for _ in range(5))
        records.append(
            {
                "customerID": f"{1000 + index:04d}-{suffix}",
                "gender": rng.choice(("Female", "Male")),
                "SeniorCitizen": rng.choice((0, 0, 0, 0, 1)),
                "Partner": rng.choice(_YES_NO),
                "Dependents": rng.choice(_YES_NO),
                "tenure": tenure,
                "PhoneService": phone,
                "MultipleLines": rng.choice(_YES_NO) if phone == "Yes" else "No phone service",
                "InternetService": internet,
                "OnlineSecurity": add_on(),
                "OnlineBackup": add_on(),
                "DeviceProtection": add_on(),
                "TechSupport": add_on(),
                "StreamingTV": add_on(),
                "StreamingMovies": add_on(),
                "Contract": rng.choice(_CONTRACT),
                "PaperlessBilling": rng.choice(_YES_NO),
                "PaymentMethod": rng.choice(_PAYMENT),
                "MonthlyCharges": monthly,
                # The real file leaves TotalCharges blank for a subscriber whose tenure is 0, which
                # is why pandas reads the column as text. The fixture keeps that quirk.
                "TotalCharges": "" if tenure == 0 else f"{monthly * tenure:.2f}",
                "Churn": labels[index],
            }
        )
    frame = pd.DataFrame.from_records(records, columns=list(TELCO_COLUMNS))
    frame["SeniorCitizen"] = frame["SeniorCitizen"].astype("int64")
    frame["tenure"] = frame["tenure"].astype("int64")
    frame["MonthlyCharges"] = frame["MonthlyCharges"].astype("float64")
    return frame


@pytest.fixture(scope="module")
def config() -> UseCaseConfig:
    """The use case, loaded by the real loader from the real `configs/` tree."""
    return load_use_case(USE_CASE_ID)


@pytest.fixture(scope="module")
def frame() -> pd.DataFrame:
    return telco_frame()


@pytest.fixture(scope="module")
def report(config: UseCaseConfig, frame: pd.DataFrame) -> ValidationReport:
    return validate_for_training(
        frame,
        config,
        primary_key=PRIMARY_KEY,
        target=TARGET,
        upload_id="up_telco_churn",
    )


@pytest.fixture(scope="module")
def ingested(frame: pd.DataFrame, tmp_path_factory: pytest.TempPathFactory) -> pd.DataFrame:
    """The same data after a real CSV round trip, which is what a run actually sees."""
    storage = LocalStorage(tmp_path_factory.mktemp("telco") / "store")
    key = "uploads/up_telco_churn/telco_customer_churn.csv"
    storage.write_bytes(key, frame.to_csv(index=False).encode("utf-8"))
    return read_table(storage, key)


# ---------------------------------------------------------------------------
# The fixture is only evidence if it really is the dataset's schema.
# ---------------------------------------------------------------------------
def test_the_fixture_has_the_kaggle_header_and_dtypes(frame: pd.DataFrame) -> None:
    assert tuple(frame.columns) == TELCO_COLUMNS
    assert {name: str(dtype) for name, dtype in frame.dtypes.items()} == TELCO_DTYPES
    assert len(frame) == DATASET_ROWS
    assert sorted(frame[TARGET].unique()) == [NEGATIVE_LABEL, POSITIVE_LABEL]
    assert int((frame[TARGET] == POSITIVE_LABEL).sum()) == DATASET_POSITIVES
    assert frame[PRIMARY_KEY].is_unique
    assert (frame["TotalCharges"] == "").any(), "the tenure-0 blank is part of the real schema"


def test_the_template_column_list_is_the_dataset_header(config: UseCaseConfig) -> None:
    """The YAML maps the real file, not a cleaned-up idea of it."""
    assert config.template.column_names == TELCO_COLUMNS


# ---------------------------------------------------------------------------
# Config only: the YAML says everything, the code says nothing.
# ---------------------------------------------------------------------------
def test_the_use_case_exists_as_configuration(config: UseCaseConfig) -> None:
    assert USE_CASE_ID in list_use_case_ids()
    assert config.problem_type is ProblemType.BINARY_CLASSIFICATION
    assert config.target.column == TARGET
    assert config.target.positive_label == POSITIVE_LABEL
    assert config.primary_key_hints[0] == PRIMARY_KEY
    assert config.entity == "subscriber", "the copy the user reads is config too"
    primary_key = config.template.primary_key
    assert primary_key is not None and primary_key.name == PRIMARY_KEY


def test_the_split_is_random_because_the_file_has_no_date_column(config: UseCaseConfig) -> None:
    assert config.split.type is SplitType.RANDOM_STRATIFIED
    assert config.split.time_column is None
    assert config.template.by_role(ColumnRole.TIME) == ()


def test_the_file_carries_no_consent_or_contact_history(config: UseCaseConfig) -> None:
    """Suppression stays switched on; it simply has no column to act on, which is a config answer."""
    suppression = config.actions.suppression
    assert suppression.opt_out_column is None
    assert suppression.recently_contacted_column is None
    assert config.governance.consent_column is None
    assert config.template.by_role(ColumnRole.CONSENT) == ()


@pytest.mark.parametrize("use_case_id", [USE_CASE_ID, PARTNER_USE_CASE_ID])
def test_no_engine_or_api_file_names_either_reused_use_case(repo_root: Path, use_case_id: str) -> None:
    """M6 in one assertion: both reused use cases are YAML and nothing else."""
    spellings = (use_case_id, use_case_id.replace("-", "_"))
    pattern = re.compile("|".join(rf"(?<![A-Za-z0-9_-]){re.escape(s)}(?![A-Za-z0-9_-])" for s in spellings))
    offenders = [
        str(path.relative_to(repo_root))
        for directory in ("engine", "api")
        for path in sorted((repo_root / directory).rglob("*.py"))
        if pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], f"{use_case_id!r} is named in {offenders}; it must live in YAML alone"


def test_the_template_is_generated_from_the_config_not_hand_written(
    repo_root: Path, config: UseCaseConfig
) -> None:
    csv_name, readme_name = template_filenames(config)
    csv_path = repo_root / "templates" / csv_name
    assert csv_path.read_text(encoding="utf-8") == render_template_csv(config)
    assert (repo_root / "templates" / readme_name).is_file()
    header = csv_path.read_text(encoding="utf-8").splitlines()[0]
    assert tuple(header.split(",")) == TELCO_COLUMNS


def test_the_industry_file_offers_it_under_the_churn_stage(config: UseCaseConfig) -> None:
    industry = load_industry("telecom")
    stages = [stage.name for stage, ref in industry.all_refs() if ref.id == USE_CASE_ID]
    assert stages == [config.lifecycle_stage] == ["Churn"]


# ---------------------------------------------------------------------------
# Plan section 4.1: a binary target may be any two values.
# ---------------------------------------------------------------------------
def test_the_string_target_resolves_to_the_configured_positive_label(
    config: UseCaseConfig, frame: pd.DataFrame
) -> None:
    params = params_from_config(config, primary_key=PRIMARY_KEY, target=TARGET)
    label, positives, negatives = resolve_positive_label(frame, params)
    assert label == POSITIVE_LABEL
    assert positives == DATASET_POSITIVES
    assert negatives == DATASET_ROWS - DATASET_POSITIVES


def test_the_dataset_clears_the_configured_thresholds(config: UseCaseConfig) -> None:
    assert config.validation.min_rows <= DATASET_ROWS
    assert config.validation.min_positive <= DATASET_POSITIVES


# ---------------------------------------------------------------------------
# THE DECISIVE TEST
# ---------------------------------------------------------------------------
def test_the_telco_frame_validates_with_no_errors(report: ValidationReport) -> None:
    """A real public dataset, mapped by YAML alone, passes the real validate stage."""
    blocking = [
        f"{item.code} on {item.column}: {item.message}"
        for item in report.checks
        if item.severity is Severity.ERROR and not item.acknowledged
    ]
    assert blocking == [], f"the Telco mapping needs an engine change: {blocking}"
    assert report.error_count == 0
    assert report.passed


def test_the_dataset_raises_no_findings_at_all(report: ValidationReport) -> None:
    """The stronger claim, pinned as an exact empty set: the file is clean, not merely runnable.

    Nothing about the Telco file is remarkable to the validate stage. Its `Yes` / `No` target is a
    binary target (plan section 4.1), its missing date column is a random split rather than a
    defect, its `TotalCharges` blanks at tenure 0 are ordinary nulls, and `gender` is a two-level
    category rather than a roster of people: `engine.stages.ingest`'s personal-name detector asks
    its values-alone branch for an open vocabulary (`NAME_MIN_DISTINCT_RATIO`) before it reads
    capitalised words as names, so `Female` / `Male` no longer reads as one.

    So the whole of M6 lands on one assertion - a real public dataset validates *completely clean*
    through configuration alone - and any finding at all, of any severity, is a change to explain
    rather than a warning to absorb.
    """
    findings = {(item.code, item.column) for item in report.checks}
    assert findings == set(), f"the Telco mapping now raises {sorted(findings)}"
    assert report.warning_count == 0
    assert report.error_count == 0


def test_the_raw_csv_also_reads_and_validates_through_the_real_ingest(
    config: UseCaseConfig, ingested: pd.DataFrame
) -> None:
    """Uploading the file, not just holding a frame: ingest must parse the tenure-0 blanks too."""
    assert tuple(ingested.columns) == TELCO_COLUMNS
    assert str(ingested["TotalCharges"].dtype) == "float64"
    assert int(ingested["TotalCharges"].isna().sum()) > 0
    assert len(ingested) == DATASET_ROWS
    csv_report = validate_for_training(
        ingested,
        config,
        primary_key=PRIMARY_KEY,
        target=TARGET,
        upload_id="up_telco_churn_csv",
    )
    assert csv_report.error_count == 0
    assert csv_report.passed


def test_prepare_and_split_carry_the_string_target_through(
    config: UseCaseConfig, ingested: pd.DataFrame
) -> None:
    """The stages after validate must also cope with `Yes` / `No` and with no date column."""
    prepared, plan = prepare_rows(ingested, config, primary_key=PRIMARY_KEY, target=TARGET)
    assert len(prepared) == DATASET_ROWS
    assert plan.dropped_columns == ()
    assert plan.pii_columns == ()
    assert set(plan.feature_columns) == set(TELCO_COLUMNS) - {PRIMARY_KEY, TARGET}
    parts, _ = split_dataset(prepared, config, run_id="run_telco_churn", target=TARGET)
    assert sum(len(part) for part in parts.values()) == DATASET_ROWS
    for name, part in parts.items():
        assert not part.empty, name
        assert set(part[TARGET].unique()) == {POSITIVE_LABEL, NEGATIVE_LABEL}, name
