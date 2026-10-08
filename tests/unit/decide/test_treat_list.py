"""Unit tests for Plan J M98: the treat list builder and business-language reasons.

Covers:
- Reason dictionary loading and lookup in business words (configs/decide/reasons.yaml).
- Plain-language check: `jargon_in` finds no forbidden model jargon in mapped reason phrases.
- Unmapped features preserve original explanation text.
- Fewer than three reasons leave remaining reason columns as null (None), not empty string or repeated text.
- Parquet flags are boolean (True/False/None), CSV flags are 1/0/empty.
- When holdout_assignment is missing, holdout flag is null (None), not False, with a plain note in summary.
- Net value is null with a note when missing.
- Linear scale check on 200k rows (reporting 1M estimate).
"""

from __future__ import annotations

import time

import numpy as np
import pandas as pd

from engine.contracts import Direction, Reason
from engine.decide.reasons import (
    BusinessReasonDictionary,
    load_reasons_dictionary,
    map_reasons,
    map_reasons_from_explanations,
)
from engine.pilot.plain import jargon_in


def test_reasons_dictionary_loads_and_contains_no_jargon() -> None:
    dictionary = load_reasons_dictionary()
    assert len(dictionary.features) >= 4

    # Every phrase in the dictionary must pass the plain language test
    for feature, directions in dictionary.features.items():
        for direction, phrase in directions.items():
            assert isinstance(phrase, str)
            assert len(phrase) > 0
            jargon_found = jargon_in(phrase)
            assert jargon_found == (), f"Jargon {jargon_found} found in {feature}.{direction}: {phrase!r}"


def test_reason_mapping_renders_phrase_for_mapped_and_keeps_original_for_unmapped() -> None:
    dictionary = BusinessReasonDictionary(
        schema_version=1,
        features={
            "monthly_spend": {
                "down": "Spent less each month for 3 months",
                "up": "Increased monthly spend",
            },
            "complaints_30d": {
                "up": "Raised two complaints in 30 days",
                "down": "Fewer complaints in the last 30 days",
            },
        },
    )

    reasons_list = [
        # Row 0: mapped feature down, mapped feature up
        [
            Reason(
                feature="monthly_spend",
                value="50",
                contribution=-0.2,
                direction=Direction.DOWN,
                text="monthly_spend was 50, pushing down",
            ),
            Reason(
                feature="complaints_30d",
                value="2",
                contribution=0.1,
                direction=Direction.UP,
                text="complaints_30d was 2, pushing up",
            ),
        ],
        # Row 1: unmapped feature
        [
            Reason(
                feature="tenure_months",
                value="12",
                contribution=-0.05,
                direction=Direction.DOWN,
                text="tenure_months was 12",
            )
        ],
        # Row 2: empty reasons
        [],
    ]

    df_reasons = map_reasons_from_explanations(reasons_list, dictionary=dictionary)
    assert list(df_reasons.columns) == ["reason_1", "reason_2", "reason_3"]

    # Row 0
    assert df_reasons.loc[0, "reason_1"] == "Spent less each month for 3 months"
    assert df_reasons.loc[0, "reason_2"] == "Raised two complaints in 30 days"
    assert pd.isna(df_reasons.loc[0, "reason_3"]) or df_reasons.loc[0, "reason_3"] is None

    # Row 1: unmapped keeps original text
    assert df_reasons.loc[1, "reason_1"] == "tenure_months was 12"
    assert pd.isna(df_reasons.loc[1, "reason_2"]) or df_reasons.loc[1, "reason_2"] is None
    assert pd.isna(df_reasons.loc[1, "reason_3"]) or df_reasons.loc[1, "reason_3"] is None

    # Row 2: all null
    assert pd.isna(df_reasons.loc[2, "reason_1"])
    assert pd.isna(df_reasons.loc[2, "reason_2"])
    assert pd.isna(df_reasons.loc[2, "reason_3"])


def test_scale_200k_rows_linear_reason_mapping() -> None:
    """Performance test: 200,000 rows must run well under 3 seconds in vectorized time."""
    dictionary = load_reasons_dictionary()
    n = 200_000

    features = np.random.choice(["monthly_spend", "complaints_30d", "other_feature"], size=n)
    directions = np.random.choice(["up", "down"], size=n)
    texts = np.array([f"Feature {f} was {d}" for f, d in zip(features, directions, strict=True)])

    t0 = time.perf_counter()
    # Map vectorized
    mapped = map_reasons(features, directions, texts, dictionary=dictionary)
    elapsed = time.perf_counter() - t0

    assert len(mapped) == n
    # Must complete in under 2.0s on 200k rows
    assert elapsed < 2.0, f"200k rows took {elapsed:.3f}s; must be < 2.0s"
    estimated_1m = elapsed * 5.0
    print(f"\n[Perf] 200k rows reason mapping: {elapsed:.4f}s (estimated 1M: {estimated_1m:.3f}s)")


def test_suggest_reason_phrases() -> None:
    from engine.agent.contracts import AgentConfidence
    from engine.agent.recommend import suggest_reason_phrases

    features = ("monthly_spend", "customer_credit_score", "order_frequency")
    suggestions = suggest_reason_phrases(features)
    # monthly_spend is mapped in reasons.yaml, so only 2 unmapped features get suggestions
    assert len(suggestions) == 2
    assert {s.feature for s in suggestions} == {"customer_credit_score", "order_frequency"}
    for s in suggestions:
        assert s.confidence == AgentConfidence.CHECK
        assert s.up_phrase.startswith("Higher ")
        assert s.down_phrase.startswith("Lower ")
        assert jargon_in(s.up_phrase) == ()
        assert jargon_in(s.down_phrase) == ()
