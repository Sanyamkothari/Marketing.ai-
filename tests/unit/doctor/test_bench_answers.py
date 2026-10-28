"""The benchmark's scripted person names the option it really sends (scripts/plan_o_benchmark.py `_Answers`)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]


def _module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "plan_o_benchmark", REPO / "scripts" / "plan_o_benchmark.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("plan_o_benchmark", module)
    spec.loader.exec_module(module)
    return module


def test_with_no_truth_the_recommended_option_is_sent_and_named() -> None:
    answers = _module()._Answers(None)
    question = {
        "code": next(iter(_module()._Answers._ROLES)),
        "options": [{"option_id": "c0", "label": "first"}, {"option_id": "c1", "label": "second"}],
        "recommended": "c1",
    }
    assert answers(question) == "c1"
    assert answers.not_measured == [answers.not_measured[0]] and "'second'" in answers.not_measured[0]
