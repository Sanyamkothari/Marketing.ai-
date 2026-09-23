"""M36 item 2: odd column names get safe internal names, and every output shows the original.

The library found this through a template (`default.payment.next.month` cannot be a template column
name, `docs/CROSS_BRANCH_REQUESTS.md`) and renamed the column by hand in `fetch.py`. What an odd
header actually breaks inside the engine is the model: LightGBM and XGBoost refuse a feature name
with a JSON special character or a bracket, and AutoGluon skips the family rather than failing the
run. These tests pin the mapping (a Hypothesis property over arbitrary Unicode, dotted and spaced
names), show the refusal and its cure on the library's credit-default sample, and check each place
the mapping is applied or undone (DEC-093). The end-to-end run is
`tests/integration/test_odd_column_names.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from engine.column_names import (
    COLUMN_NAMES_FILENAME,
    ColumnNames,
    internal_importance,
    is_safe_name,
    load_for_model,
    present_explanations,
    restore_importance,
    safe_base,
    save_for_model,
)
from engine.config import (
    FeaturesConfig,
    Metric,
    ModelSearchConfig,
    PrepareConfig,
    ProblemType,
    Recipe,
    SplitConfig,
    ThresholdMode,
    resolve_config,
)
from engine.contracts import (
    Direction,
    FeatureImportance,
    FeatureImportanceItem,
    Reason,
    RowExplanation,
)
from engine.stages import ingest
from engine.stages.scorer import AutoGluonScorer, ScorerState
from engine.storage import LocalStorage
from engine.utils.time import utc_now

REPO = Path(__file__).resolve().parents[2]
LIBRARY = REPO / "library"
# The library\'s use cases ship in the repository\'s configs/ since Plan A M38 (DEC-085).

#: The published headers the library renamed by hand, and the names its `fetch.py` chose.
LIBRARY_RENAMES: dict[str, str] = {
    "default.payment.next.month": "default_payment_next_month",  # uci-credit-default
    "emp.var.rate": "emp_var_rate",  # uci-bank-marketing
    "cons.price.idx": "cons_price_idx",
    "cons.conf.idx": "cons_conf_idx",
    "nr.employed": "nr_employed",
}

#: Headers a client's export really carries, each breaking something a plain name does not.
ODD_HEADERS: dict[str, str] = {
    "LIMIT_BAL": "limit, bal",  # a comma: LightGBM's JSON check
    "SEX": 'sex"q',  # a quote
    "EDUCATION": "edu/level",
    "MARRIAGE": "婚姻",  # nothing left in ASCII
    "PAY_2": "pay:2{x}",  # colon and braces
    "AGE": "âge",
    "PAY_0": "PAY.0",
    "BILL_AMT1": "bill[amt]1",  # brackets: XGBoost's and LightGBM's
}


# ---------------------------------------------------------------------------
# The mapping
# ---------------------------------------------------------------------------
_NAMES = st.lists(st.text(min_size=1, max_size=24), min_size=1, max_size=12, unique=True)
_ODD = st.lists(
    st.text(alphabet=st.sampled_from(list("aZ9_. -[]{}:,\"'/éß婚🙂\t")), min_size=1, max_size=12),
    min_size=1,
    max_size=12,
    unique=True,
)


@settings(deadline=None, max_examples=300)
@given(columns=st.one_of(_NAMES, _ODD))
def test_arbitrary_names_round_trip(columns: list[str]) -> None:
    """Unicode, dots, spaces, brackets, emoji, clashes: every name maps to one safe name and back."""
    names = ColumnNames.for_columns(columns)
    internal = [names.internal(column) for column in columns]
    assert all(is_safe_name(name) for name in internal)
    assert len(set(internal)) == len(internal), "two headers may never share an internal name"
    assert [names.original(name) for name in internal] == columns
    for column in columns:
        if is_safe_name(column):
            assert names.internal(column) == column, "a safe header keeps its own name"
    # the frame round trip, which is what the model boundary actually does
    frame = pd.DataFrame([list(range(len(columns)))], columns=columns)
    there = names.to_internal(frame)
    assert list(there.columns) == internal
    back = names.to_original(there)
    assert list(back.columns) == columns
    # and the stored form is the same mapping
    assert ColumnNames.model_validate_json(names.model_dump_json()) == names


@settings(deadline=None, max_examples=100)
@given(columns=_NAMES)
def test_the_mapping_is_a_function_of_the_headers_alone(columns: list[str]) -> None:
    assert ColumnNames.for_columns(columns) == ColumnNames.for_columns(list(columns))


@pytest.mark.parametrize(("published", "renamed"), sorted(LIBRARY_RENAMES.items()))
def test_the_library_renames_are_exactly_the_sanitised_names(published: str, renamed: str) -> None:
    """The names the library chose by hand are the ones ingest now chooses, so its configs still fit."""
    assert safe_base(published) == renamed
    assert ColumnNames.for_columns([published]).internal(published) == renamed


def test_examples_and_clashes() -> None:
    assert safe_base("âge") == "age"
    assert safe_base("bill[amt]1") == "bill_amt_1"
    assert safe_base("2nd line") == "c_2nd_line"
    assert safe_base("婚姻") == "column"
    names = ColumnNames.for_columns(["a.b", "a_b", "a b", "婚姻", "家族"])
    assert names.renamed == {"a.b": "a_b_2", "a b": "a_b_3", "婚姻": "column", "家族": "column_2"}
    assert ColumnNames.for_columns(["customer_id", "churned"]).is_identity


# ---------------------------------------------------------------------------
# What the odd names break, on the library's credit-default sample
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def credit_default() -> pd.DataFrame:
    frame = pd.read_csv(LIBRARY / "uci-credit-default" / "sample.csv").head(600)
    return frame.rename(columns={**ODD_HEADERS, "default_payment_next_month": "default.payment.next.month"})


def test_lightgbm_and_xgboost_refuse_the_published_headers_and_take_the_internal_ones(
    credit_default: pd.DataFrame,
) -> None:
    """The failure AutoGluon hides ("No model was trained ... Skipping"), and its cure."""
    import lightgbm
    import xgboost

    target = "default.payment.next.month"
    features = credit_default.drop(columns=["ID", target])
    labels = credit_default[target]
    with pytest.raises(Exception, match=r"(?i)special JSON characters"):
        lightgbm.LGBMClassifier(n_estimators=2, verbose=-1).fit(features, labels)
    with pytest.raises(ValueError, match=r"(?i)feature_names"):
        xgboost.XGBClassifier(n_estimators=2).fit(features, labels)

    names = ColumnNames.for_columns(credit_default.columns)
    internal = names.to_internal(features)
    lightgbm.LGBMClassifier(n_estimators=2, verbose=-1).fit(internal, labels)
    xgboost.XGBClassifier(n_estimators=2).fit(internal, labels)


def test_a_configured_safe_name_finds_the_published_header(credit_default: pd.DataFrame) -> None:
    """The unchanged library use case offers the client's dotted header as the target."""
    config = resolve_config("card-default-propensity", {}, root=LIBRARY.parent / "configs").config
    assert config.target.column == "default_payment_next_month"
    profile = ingest.profile_dataset(
        credit_default,
        config,
        upload_id="upl_credit",
        file_name="UCI_Credit_Card.csv",
        file_format="csv",
        file_size_bytes=1024,
        delimiter=",",
        encoding="utf-8",
    )
    assert profile.target_candidate == "default.payment.next.month"
    assert [column.name for column in profile.columns] == list(credit_default.columns)


# ---------------------------------------------------------------------------
# The model boundary
# ---------------------------------------------------------------------------
@dataclass
class RecordingPredictor:
    """Serves `signal` as the probability and remembers the columns it was handed."""

    seen: list[list[str]] = field(default_factory=list)

    def predict_proba(self, frame: pd.DataFrame, as_multiclass: bool = True) -> pd.Series:
        self.seen.append([str(column) for column in frame.columns])
        return frame["signal"].astype(float)


def _state(**overrides: Any) -> ScorerState:
    values: dict[str, Any] = {
        "kind": "autogluon",
        "model_name": "LightGBM",
        "display_name": "LightGBM",
        "problem_type": ProblemType.BINARY_CLASSIFICATION,
        "target_column": "churned_flag",
        "feature_columns": ("signal", "limit_bal"),
        "classes": (0, 1),
        "primary_metric": Metric.ROC_AUC,
        "threshold": 0.5,
        "threshold_mode": ThresholdMode.AUTO,
        "threshold_detail": "Auto (maximises F1 on validation): 0.50",
        "recipe_hash": "0" * 64,
        "trained_at": utc_now(),
    }
    values.update(overrides)
    return ScorerState.model_validate(values)


def test_a_named_scorer_takes_and_reports_the_clients_headers() -> None:
    predictor = RecordingPredictor()
    internal = AutoGluonScorer(predictor, _state())
    named = internal.with_column_names({"limit, bal": "limit_bal", "churned.flag": "churned_flag"})
    assert named.feature_columns == ("signal", "limit, bal")
    assert named.target_column == "churned.flag"
    assert internal.feature_columns == ("signal", "limit_bal"), "the original object is untouched"

    frame = pd.DataFrame({"signal": [0.2, 0.9], "limit, bal": [1.0, 2.0], "churned.flag": [0, 1]})
    assert named.can_score(frame) == (True, "")
    scores = named.score(frame)
    assert predictor.seen[-1] == ["signal", "limit_bal"], "the model is handed its own names"
    assert list(scores) == [0.2, 0.9]
    ok, why = named.can_score(frame.drop(columns=["limit, bal"]))
    assert not ok and "limit, bal" in why
    # scorer.json is what the train stage wrote: the mapping is stored beside it, not inside it
    assert named.to_json() == internal.to_json()
    assert named.with_column_names({}).feature_columns == internal.feature_columns


def test_the_mapping_is_stored_beside_the_model(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    names = ColumnNames.for_columns(["default.payment.next.month", "ID"])
    key = save_for_model(storage, "runs/r_1/model", names)
    assert key == f"runs/r_1/model/{COLUMN_NAMES_FILENAME}"
    assert load_for_model(storage, "runs/r_1/model") == names
    # the identity mapping writes nothing, so an ordinary model directory is what it always was
    assert save_for_model(storage, "runs/r_2/model", ColumnNames.for_columns(["ID"])) is None
    assert not storage.exists(f"runs/r_2/model/{COLUMN_NAMES_FILENAME}")
    assert load_for_model(storage, "runs/r_2/model").is_identity


def test_the_recipe_crosses_the_boundary_translated() -> None:
    recipe = Recipe(
        use_case_id="card-default-propensity",
        problem_type=ProblemType.BINARY_CLASSIFICATION,
        target="default.payment.next.month",
        primary_key="Customer ID",
        feature_columns=("limit, bal", "AGE"),
        prepare=PrepareConfig(),
        split=SplitConfig(),
        features=FeaturesConfig(),
        model_search=ModelSearchConfig(),
        seed=1,
    )
    names = ColumnNames.for_columns(["Customer ID", "limit, bal", "AGE", "default.payment.next.month"])
    model = names.recipe_for_model(recipe)
    assert model.target == "default_payment_next_month"
    assert model.primary_key == "Customer_ID"
    assert model.feature_columns == ("limit_bal", "AGE")
    assert recipe.feature_columns == ("limit, bal", "AGE"), "the run's own recipe keeps the client's names"
    assert ColumnNames().recipe_for_model(recipe) is recipe


def test_importance_and_reasons_come_back_under_the_clients_names() -> None:
    names = ColumnNames.for_columns(["PAY.0", "AGE"])
    measured = FeatureImportance(
        run_id="r_1",
        method="permutation",
        top_n=2,
        items=(
            FeatureImportanceItem(rank=1, feature="PAY_0", importance=0.2, share_pct=66.7),
            FeatureImportanceItem(rank=2, feature="AGE", importance=0.1, share_pct=33.3),
        ),
        caption="Permutation importance on the test split.",
    )
    shown = restore_importance(measured, names)
    assert [item.feature for item in shown.items] == ["PAY.0", "AGE"]
    assert internal_importance(shown, names) == measured

    reason = Reason(feature="PAY_0", value="2", contribution=0.3, direction=Direction.UP, text="PAY_0 ↑ (2)")
    (explained,) = present_explanations(
        (RowExplanation(primary_key="42", score=0.7, reasons=(reason,)),), names
    )
    assert explained.reasons[0].feature == "PAY.0"
    assert explained.reasons[0].text == "PAY.0 ↑ (2)"
    assert explained.reasons[0].value == "2"


def test_numbers_are_untouched_by_the_renaming(credit_default: pd.DataFrame) -> None:
    names = ColumnNames.for_columns(credit_default.columns)
    back = names.to_original(names.to_internal(credit_default))
    pd.testing.assert_frame_equal(back, credit_default)
    assert np.array_equal(back.to_numpy(), credit_default.to_numpy())
