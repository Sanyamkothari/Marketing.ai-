"""Plan A M36 item 3, end to end: the library dataset whose `auto` threshold flagged every row.

`library/online-retail` - a weak model over a 41 % base rate - is trained through the real pipeline
with the library's own `retail-win-back` configuration and the one-minute budget its library test
uses. On the full data an earlier run's F1-maximising threshold came out at "everybody is positive":
recall 1.0, specificity 0.0, precision equal to the base rate, which the Model page would have
reported as 100 % recall. With `auto` defined precisely (DEC-094) the chosen threshold can never
flag every validation row nor more than the configured 30 %, and when it had to refuse its optimum
the artefacts say so.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from library.run_engine import run

from engine.config import Metric
from engine.contracts import EvaluationReport, RunState
from engine.stages.scorer import THRESHOLD_FALLBACK
from engine.storage import LocalStorage, run_key

pytestmark = [pytest.mark.slow, pytest.mark.integration]

LIBRARY = Path(__file__).resolve().parents[2] / "library"
# The library\'s use cases ship in the repository\'s configs/ since Plan A M38 (DEC-085).
FAST: dict[str, object] = {"model_search.time_limit_minutes": 1, "model_search.strategy": "fast"}


def test_the_retail_model_no_longer_calls_every_shopper_positive(tmp_path: Path) -> None:
    outcome = run(
        dataset="online-retail",
        use_case="retail-win-back",
        csv_path=LIBRARY / "online-retail" / "sample.csv",
        primary_key="customer_id",
        target="reactivated_90d",
        overrides=dict(FAST),
        config_root=LIBRARY.parent / "configs",
        runs_dir=tmp_path,
    )
    assert outcome.state == RunState.DONE.value, outcome.error
    storage = LocalStorage(tmp_path / "online-retail" / "data")
    evaluation = storage.read_model(run_key(outcome.run_id, "evaluation.json"), EvaluationReport)
    recall = next(metric.value for metric in evaluation.metrics if metric.id is Metric.RECALL)
    assert recall is not None and recall < 1.0
    assert evaluation.extra_metrics["specificity"] > 0.0, "some shopper is predicted not to return"

    scorer = json.loads(storage.read_text(run_key(outcome.run_id, "model/scorer.json")))
    fallback = scorer.get("threshold_fallback")
    if fallback is None:
        # the optimum was within the ceiling, and the sentence says it was the optimum
        assert evaluation.threshold_detail.startswith("Auto (maximises F1 on validation)")
    else:
        assert fallback["code"] == THRESHOLD_FALLBACK
        assert (
            fallback["optimum_flagged_rate"] > fallback["max_flagged_rate"]
            or fallback["optimum_flagged_rate"] == 1.0
        )
        assert fallback["fallback_flagged_rate"] < 0.30
        assert THRESHOLD_FALLBACK in evaluation.threshold_detail
