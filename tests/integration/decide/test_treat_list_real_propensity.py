"""The treat list of a real propensity scoring run (Plan J M98): an AutoGluon champion scoring a fresh file.

Slow: it reuses the two module fixtures of `tests/integration/test_score_flow.py` (a real
`Pipeline.run_train` and `Pipeline.run_score`, about a minute each), so it runs with `make test-all`, not
`make test`. The artefacts are the flow's own: `scores.parquet`, `row_explanations.parquet` (TreeSHAP or the
documented fallback tiers) and `run_config.json`. A default run engages no holdout, so its holdout flag is
null with a note and its treat flag is M92's selection over the scores.
"""

# ruff: noqa: F811, F401 - the imported pytest fixtures are used by name
from __future__ import annotations

import io

import pandas as pd
import pytest

from engine.decide.reasons import load_reasons_dictionary
from engine.decide.treat_list import TREAT_LIST_CSV, build_treat_list
from engine.stages.explain import ROW_EXPLANATIONS_FILENAME, read_row_explanations
from engine.stages.export import SCORES_CSV
from engine.storage import run_key
from tests.integration.test_score_flow import (
    Flow,
    Trained,
    scored,
    trained,
)

pytestmark = [pytest.mark.slow, pytest.mark.integration]


def test_the_treat_list_of_a_real_propensity_run(scored: Flow) -> None:
    before = scored.storage.read_bytes(scored.key(SCORES_CSV))
    summary = build_treat_list(scored.storage, scored.run_id)
    assert (
        scored.storage.read_bytes(scored.key(SCORES_CSV)) == before
    ), "a default run's scores.csv is untouched"

    text = scored.storage.read_bytes(run_key(scored.run_id, TREAT_LIST_CSV)).decode("utf-8")
    out = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)
    scores = scored.scores()
    assert len(out.index) == len(scores.index) == summary.total_rows
    assert list(out["customer_id"]) == [str(v) for v in scores["customer_id"]]

    # No holdout assignment on a default run: unknown, not false, and a plain note.
    assert (out["holdout"] == "").all() and summary.holdout_rows is None and summary.holdout_note
    # Treat is M92's selection: every contactable customer outside the lowest band and the control group.
    floor = scored.resolved.config.actions.bands[-1].name
    contactable = scores["suppressed_reason"].isna() & ~scores["control_group"].astype(bool)
    expected = contactable & (scores["band"] != floor)
    assert ((out["treat"] == "1").to_numpy() == expected.to_numpy()).all()
    assert (out["net_value"] == "").all() and (out["expected_gross_value"] == "").all()

    # Each customer's reasons are their own, in business words where reasons.yaml covers the feature.
    dictionary = load_reasons_dictionary()
    explanations = read_row_explanations(
        run_key(scored.run_id, ROW_EXPLANATIONS_FILENAME), storage=scored.storage
    )
    by_key = {e.primary_key: e for e in explanations}
    mapped = 0
    for customer, first, second, third in zip(
        out["customer_id"], out["reason_1"], out["reason_2"], out["reason_3"], strict=True
    ):
        phrases = [
            dictionary.render(r.feature, r.direction.value, r.value, r.text)
            for r in by_key[customer].reasons[:3]
        ]
        assert [first or None, second or None, third or None] == phrases + [None] * (
            3 - len(phrases)
        ), customer
        mapped += sum("pushes the score" in phrase for phrase in phrases)
    assert mapped > 0
