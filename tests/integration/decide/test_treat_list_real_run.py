"""The treat list of a real scoring run (Plan J M98): `Pipeline.run_score` over an uplift model, two-column key.

The run is the one `tests/integration/holdout/test_holdout_uplift_flow.py` makes (LightGBM, a persistent
universal holdout of 20%, a 10% explore slice, two snapshots per customer) and this module reuses its
fixture: every artefact below, `scores.parquet`, `holdout_assignment.parquet`, `row_explanations.parquet`
and `run_config.json`, is written by the product's own flow, not by a fixture. What is checked is what a
hand-made frame cannot show: the composite key as the flow writes it, the reasons of a trained model, and the
public route. The propensity counterpart (an AutoGluon model) is `test_treat_list_real_propensity.py` (slow).
"""

# ruff: noqa: F811, F401 - the imported pytest fixtures are used by name
from __future__ import annotations

import io

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine.decide.reasons import load_reasons_dictionary
from engine.decide.treat_list import TREAT_LIST_CSV, TREAT_LIST_PARQUET, build_treat_list
from engine.stages.explain import ROW_EXPLANATIONS_FILENAME, read_row_explanations
from engine.stages.export import SCORES_CSV
from engine.storage import run_key
from tests.integration.holdout.test_holdout_uplift_flow import (
    KEY,
    SCORE_RUN,
    Scored,
    scored,
)

pytestmark = pytest.mark.integration


def _flag(series: pd.Series) -> pd.Series:
    return series.map({True: "1", False: "0"})


@pytest.fixture(scope="module")
def built(scored: Scored) -> pd.DataFrame:
    scores_before = scored.storage.read_bytes(run_key(SCORE_RUN, SCORES_CSV))
    summary = build_treat_list(scored.storage, SCORE_RUN)
    assert summary.total_rows == len(scored.scores.index)
    assert scored.storage.read_bytes(run_key(SCORE_RUN, SCORES_CSV)) == scores_before, "scores.csv untouched"
    text = scored.storage.read_bytes(run_key(SCORE_RUN, TREAT_LIST_CSV)).decode("utf-8")
    return pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)


def test_the_flags_follow_the_assignment_on_both_key_columns(scored: Scored, built: pd.DataFrame) -> None:
    assert list(built.columns[:2]) == KEY
    assigned = scored.table.astype({"customer_id": str, "snapshot_date": str}).rename(
        columns={"explore": "assigned_explore"}
    )
    merged = built.merge(assigned, on=KEY, how="left", validate="one_to_one")
    assert merged["holdout_member"].notna().all(), "every scored row is in the assignment"
    assert (merged["holdout"] == _flag(merged["holdout_member"])).all()
    assert (merged["explore"] == _flag(merged["assigned_explore"])).all()
    assert (merged["treat"] == _flag(merged["treated"])).all(), "treat is M92's `treated`, by key"
    assert (
        (merged["holdout"] == "1").any()
        and (merged["explore"] == "1").any()
        and (merged["treat"] == "1").any()
    )


def test_treat_one_never_includes_holdout_suppressed_or_sleeping_dogs(
    scored: Scored, built: pd.DataFrame
) -> None:
    treat = built["treat"] == "1"
    assert not (treat & (built["holdout"] == "1")).any()
    assert not (treat & (built["suppression_reason"] != "")).any()
    assert not (treat & (built["segment"] == "sleeping_dog")).any()
    assert (built["segment"] == "sleeping_dog").any()
    # An explored customer is treated although the policy left them out, with the policy's treat action.
    explored = treat & (built["explore"] == "1")
    assert explored.any() and set(built.loc[explored, "offer"]) == {"Treat"}


def test_money_is_null_because_the_run_was_not_ranked_by_value(scored: Scored, built: pd.DataFrame) -> None:
    assert (built["net_value"] == "").all() and (built["expected_gross_value"] == "").all()
    summary = pd.read_json(
        io.BytesIO(scored.storage.read_bytes(run_key(SCORE_RUN, "treat_list_summary.json"))), typ="series"
    )
    assert summary["net_value_total"] is None or pd.isna(summary["net_value_total"])


def test_each_customers_reasons_are_their_own_in_business_words(scored: Scored, built: pd.DataFrame) -> None:
    dictionary = load_reasons_dictionary()
    explanations = read_row_explanations(
        run_key(SCORE_RUN, ROW_EXPLANATIONS_FILENAME), storage=scored.storage
    )
    expected = {
        explanation.primary_key: [
            dictionary.render(r.feature, r.direction.value, r.value, r.text) for r in explanation.reasons[:3]
        ]
        for explanation in explanations
    }
    keys = built["customer_id"] + "|" + built["snapshot_date"]
    mapped = 0
    for key, first, second, third in zip(
        keys, built["reason_1"], built["reason_2"], built["reason_3"], strict=True
    ):
        phrases = expected[key]
        assert [first or None, second or None, third or None] == phrases + [None] * (3 - len(phrases)), key
        mapped += sum("pushes the score" in phrase for phrase in phrases)
    assert mapped > 0, "a trained model's features are in reasons.yaml, so some reasons are in business words"


def test_the_route_serves_the_same_bytes(scored: Scored, built: pd.DataFrame, config_root) -> None:  # type: ignore[no-untyped-def]
    with TestClient(create_app(config_root=config_root, data_dir=scored.storage.root)) as client:
        response = client.get(f"/runs/{SCORE_RUN}/treat_list.csv")
    assert response.status_code == 200, response.text
    assert response.content == scored.storage.read_bytes(run_key(SCORE_RUN, TREAT_LIST_CSV))
    parquet = pd.read_parquet(io.BytesIO(scored.storage.read_bytes(run_key(SCORE_RUN, TREAT_LIST_PARQUET))))
    assert parquet["treat"].dtype == bool and len(parquet.index) == len(built.index)
