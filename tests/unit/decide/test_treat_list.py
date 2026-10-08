"""Unit tests for Plan J M98: business-language reasons (`configs/decide/reasons.yaml`) and Guided setup wording.

A reason's `direction` is whether the feature pushed the **score** up or down (`engine.contracts.Reason`),
not whether the customer's value went up or down. The first review found the phrases read it as the
value's direction and asserted numbers no row carried; the tests below pin the corrected rules:

- a phrase is keyed by feature and direction and says which way the score moved;
- it may print the row's own `{value}` and nothing else numeric (the shipped file and the loader both refuse a digit);
- a feature or direction with no phrase, and a value the row does not have, keep today's text.

Also here: `jargon_in` finds nothing, the Arrow reader and the row reader agree, `config_root` finds the
file, null slots, and the wording Guided setup suggests.
"""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import yaml

from engine.contracts import Direction, Reason, RowExplanation
from engine.decide.reasons import (
    REASON_DIRECTIONS,
    REASONS_FILE,
    VALUE_PLACEHOLDER,
    BusinessReasonDictionary,
    extract_and_map_reasons_from_parquet,
    load_reasons_dictionary,
    map_reasons,
    map_reasons_from_explanations,
    reasons_path,
)
from engine.pilot.plain import jargon_in
from engine.stages.explain import MISSING_VALUE, row_explanation_schema

SHIPPED = Path("configs/decide/reasons.yaml")


def _reason(feature: str, value: str, direction: Direction, text: str | None = None) -> Reason:
    sign = {Direction.UP: 0.2, Direction.DOWN: -0.2, Direction.NONE: 0.0}[direction]
    return Reason(
        feature=feature,
        value=value,
        contribution=sign,
        direction=direction,
        text=text or f"{feature} = {value}",
    )


def _table(rows: list[list[Reason]]) -> pa.Table:
    explained = [
        RowExplanation(primary_key=f"C-{i}", score=0.5, reasons=tuple(reasons))
        for i, reasons in enumerate(rows)
    ]
    return pa.Table.from_pylist(
        [e.model_dump(mode="json") for e in explained], schema=row_explanation_schema()
    )


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


def test_no_shipped_phrase_holds_a_number_of_its_own() -> None:
    """The review's first finding: 'for 3 months' and 'two complaints' were numbers no row carried."""
    document: dict[str, Any] = yaml.safe_load(SHIPPED.read_text(encoding="utf-8"))
    phrases = [
        (feature, direction, phrase)
        for feature, by_direction in document["features"].items()
        for direction, phrase in by_direction.items()
    ]
    assert phrases, "the shipped file maps something"
    for feature, direction, phrase in phrases:
        assert direction in REASON_DIRECTIONS, (feature, direction)
        bare = phrase.replace(VALUE_PLACEHOLDER, "")
        assert not re.search(
            r"\d", bare
        ), f"{feature}.{direction} holds a digit outside {{value}}: {phrase!r}"
        assert not re.search(r"\b(two|three|four|five|six|seven|eight|nine|ten)\b", bare, re.I), phrase
        # A trend is a claim about the customer's value; the direction is about the score.
        assert not re.search(
            r"\b(less|fewer|more|higher|lower|newer|increased|decreased|longer)\b", bare, re.I
        ), f"{feature}.{direction} asserts a trend of the value: {phrase!r}"


def test_the_loader_refuses_a_phrase_with_a_number_or_a_stray_brace() -> None:
    for bad in ("Spent less each month for 3 months", "{value} of {other}", "{value} and {value}", ""):
        with pytest.raises(ValueError):
            BusinessReasonDictionary(features={"monthly_spend": {"down": bad}})
    with pytest.raises(ValueError, match="direction"):
        BusinessReasonDictionary(features={"monthly_spend": {"sideways": "Monthly spend of {value}"}})
    BusinessReasonDictionary(
        features={"monthly_spend": {"down": "Monthly spend of {value} lowers the chance"}}
    )


def test_a_feature_that_pushed_the_score_down_gets_the_down_phrase_with_its_own_value() -> None:
    reasons = [
        [
            _reason(
                "monthly_spend", "1499", Direction.DOWN
            ),  # the customer's spend is HIGH, and it pushed down
            _reason("monthly_spend", "120", Direction.UP),
            _reason("tenure_months", "7.5", Direction.NONE),
        ]
    ]
    frame = map_reasons_from_explanations(reasons)
    assert frame.loc[0, "reason_1"] == "Monthly spend of 1499 pushes the score down"
    assert frame.loc[0, "reason_2"] == "Monthly spend of 120 pushes the score up"
    assert "measured" in str(frame.loc[0, "reason_3"]) and "7.5 months" in str(frame.loc[0, "reason_3"])
    # Same feature, opposite directions: the two phrases differ, and neither claims the value fell or rose.
    assert frame.loc[0, "reason_1"] != frame.loc[0, "reason_2"]


def test_value_renders_the_rows_value_and_a_missing_value_keeps_todays_text() -> None:
    dictionary = BusinessReasonDictionary(
        features={
            "monthly_spend": {
                "up": "Monthly spend of {value} raises the chance",
                "down": "Spend lowers the chance",
            }
        }
    )
    texts = np.array(["monthly_spend ↑ (5)"] * 4, dtype=object)
    mapped = map_reasons(
        ["monthly_spend"] * 4,
        ["up", "up", "up", "down"],
        texts,
        values=["5", MISSING_VALUE, "", "9"],
        dictionary=dictionary,
    )
    assert mapped[0] == "Monthly spend of 5 raises the chance"
    assert mapped[1] == texts[1], "'missing' is not a value to print"
    assert mapped[2] == texts[2], "an empty value is not one either"
    assert mapped[3] == "Spend lowers the chance", "a phrase with no {value} needs none"
    # A value that looks like a placeholder is data, printed as it is, never expanded again.
    odd = map_reasons(["monthly_spend"], ["up"], ["t"], values=["{value}"], dictionary=dictionary)
    assert odd[0] == "Monthly spend of {value} raises the chance"


def test_reason_mapping_renders_phrase_for_mapped_and_keeps_original_for_unmapped() -> None:
    dictionary = BusinessReasonDictionary(
        features={
            "monthly_spend": {
                "down": "Spend of {value} lowers the chance",
                "up": "Spend of {value} raises the chance",
            },
            "complaints_30d": {"up": "Complaints ({value}) raise the chance"},
        }
    )
    reasons_list = [
        [
            _reason("monthly_spend", "50", Direction.DOWN, "monthly_spend was 50, pushing down"),
            _reason("complaints_30d", "2", Direction.UP, "complaints_30d was 2, pushing up"),
        ],
        # Unmapped feature, and a mapped feature in a direction it has no phrase for: both keep today's text.
        [_reason("tenure_months", "12", Direction.DOWN, "tenure_months was 12")],
        [_reason("complaints_30d", "0", Direction.DOWN, "complaints_30d down (0)")],
        [],
    ]
    df_reasons = map_reasons_from_explanations(reasons_list, dictionary=dictionary)
    assert list(df_reasons.columns) == ["reason_1", "reason_2", "reason_3"]

    assert df_reasons.loc[0, "reason_1"] == "Spend of 50 lowers the chance"
    assert df_reasons.loc[0, "reason_2"] == "Complaints (2) raise the chance"
    assert df_reasons.loc[0, "reason_3"] is None
    assert df_reasons.loc[1, "reason_1"] == "tenure_months was 12"
    assert df_reasons.loc[2, "reason_1"] == "complaints_30d down (0)"
    for slot in ("reason_1", "reason_2", "reason_3"):
        assert df_reasons.loc[3, slot] is None, "fewer reasons than slots leaves null, not an empty string"
    assert df_reasons.loc[1, "reason_2"] is None and df_reasons.loc[1, "reason_3"] is None


def test_the_arrow_reader_and_the_row_reader_agree_and_use_the_value() -> None:
    rows = [
        [_reason("monthly_spend", "1499", Direction.DOWN), _reason("tenure_months", "3", Direction.UP)],
        [_reason("visits_30d", "12", Direction.NONE)],
        [],
        [_reason("not_in_the_file", "x", Direction.UP, "not_in_the_file ↑ (x)")],
    ]
    dictionary = load_reasons_dictionary()
    from_rows = map_reasons_from_explanations(rows, dictionary=dictionary)
    from_arrow = extract_and_map_reasons_from_parquet(_table(rows), dictionary=dictionary)
    pd.testing.assert_frame_equal(from_rows, from_arrow)
    assert from_arrow.loc[0, "reason_1"] == "Monthly spend of 1499 pushes the score down"
    assert from_arrow.loc[3, "reason_1"] == "not_in_the_file ↑ (x)"

    buffer = io.BytesIO()
    pq.write_table(_table(rows), buffer)  # type: ignore[no-untyped-call]
    again = extract_and_map_reasons_from_parquet(pq.read_table(io.BytesIO(buffer.getvalue())), dictionary=dictionary)  # type: ignore[no-untyped-call]
    pd.testing.assert_frame_equal(from_arrow, again)


def test_a_table_without_reasons_gives_null_columns() -> None:
    table = pa.table({"primary_key": ["a", "b"]})
    frame = extract_and_map_reasons_from_parquet(table, dictionary=BusinessReasonDictionary())
    assert frame.shape == (2, 3) and frame.isna().all().all()


def test_the_dictionary_is_read_under_the_config_root_not_the_working_directory(tmp_path: Path) -> None:
    (tmp_path / REASONS_FILE.parent).mkdir(parents=True)
    (tmp_path / REASONS_FILE).write_text(
        'schema_version: 2\nfeatures:\n  tenure_months:\n    up: "Tenure of {value} months raises the chance"\n',
        encoding="utf-8",
    )
    assert reasons_path(tmp_path) == tmp_path.resolve() / REASONS_FILE
    dictionary = load_reasons_dictionary(root=tmp_path)
    assert list(dictionary.features) == ["tenure_months"]
    # An edit is picked up (the cache is keyed on the file's modification time).
    (tmp_path / REASONS_FILE).write_text(
        'schema_version: 2\nfeatures:\n  visits_30d:\n    up: "Visits of {value} raise the chance"\n',
        encoding="utf-8",
    )
    assert list(load_reasons_dictionary(root=tmp_path).features) == ["visits_30d"]
    # No file: nothing is mapped (and a warning says so); every reason keeps its text.
    assert load_reasons_dictionary(root=tmp_path / "missing").features == {}


def test_the_missing_marker_is_the_explain_stages() -> None:
    from engine.decide import reasons

    assert reasons._MISSING == MISSING_VALUE


def test_scale_200k_rows_linear_reason_mapping() -> None:
    """Performance test: 200,000 rows must run well under 3 seconds in vectorised time."""
    import time

    dictionary = load_reasons_dictionary()
    n = 200_000
    rng = np.random.default_rng(0)
    features = rng.choice(["monthly_spend", "complaints_30d", "other_feature"], size=n)
    directions = rng.choice(["up", "down"], size=n)
    values = rng.integers(0, 5000, size=n).astype(str)
    texts = np.array([f"Feature {f} was {d}" for f, d in zip(features, directions, strict=True)])

    t0 = time.perf_counter()
    mapped = map_reasons(features, directions, texts, values=values, dictionary=dictionary)
    elapsed = time.perf_counter() - t0

    assert len(mapped) == n
    assert elapsed < 2.0, f"200k rows took {elapsed:.3f}s; must be < 2.0s"
    print(f"\n[Perf] 200k rows reason mapping: {elapsed:.4f}s (estimated 1M: {elapsed * 5.0:.3f}s)")


def test_suggest_reason_phrases() -> None:
    from engine.agent.contracts import AgentConfidence
    from engine.agent.recommend import suggest_reason_phrases

    features = ("monthly_spend", "customer_credit_score", "order_frequency", "visits_7d", "7d_30d")
    suggestions = suggest_reason_phrases(features)
    # monthly_spend is mapped in reasons.yaml; "7d_30d" has nothing of its name left once the digits are out,
    # so a person words that one.
    assert [s.feature for s in suggestions] == ["customer_credit_score", "order_frequency", "visits_7d"]
    assert suggestions[2].up_phrase == "Visits of {value} pushes the score up"
    for s in suggestions:
        assert s.confidence == AgentConfidence.CHECK
        for phrase, way in ((s.up_phrase, "up"), (s.down_phrase, "down")):
            assert VALUE_PLACEHOLDER in phrase and phrase.endswith(f"pushes the score {way}")
            assert jargon_in(phrase) == ()
            assert (
                "Higher" not in phrase and "Lower" not in phrase
            ), "a direction is the score's, not the value's"
        # A suggested phrase is one the dictionary would accept as written.
        BusinessReasonDictionary(features={s.feature: {"up": s.up_phrase, "down": s.down_phrase}})
    assert suggest_reason_phrases(("monthly_spend",)) == ()
