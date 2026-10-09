"""Plan J M102 acceptance: on a binary outcome every existing field is identical and every new field null.

The JSON of four binary measurements (the run's path with immature rows, a campaign against a registered
plan that names a covariate, an early look, two offers) was recorded on the commit before M102
(`tests/fixtures/measurement/m102_binary_reports.json`, written by `m102_binary_golden.record`). The same
measurements now must give the same JSON - no field changed, no key added - and every M102 field must
read as null on the report.
"""

from __future__ import annotations

import json

import pytest

from engine.uplift.contracts import IncrementalityReport
from tests.unit.measurement.m102_binary_golden import GOLDEN, binary_reports

M102_FIELDS = (
    "outcome_kind",
    "treated_mean",
    "control_mean",
    "mean_difference",
    "mean_difference_ci",
    "covariate_column",
    "adjusted_lift",
    "adjusted_interval",
    "variance_reduction",
    "rows_covariate_missing",
    "adjustment_note",
    "outcome_warnings",
)


@pytest.fixture(scope="module")
def now() -> dict[str, dict[str, object]]:
    return binary_reports()


def test_every_binary_report_is_byte_for_byte_what_it_was(now: dict[str, dict[str, object]]) -> None:
    recorded = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert set(now) == set(recorded)
    for name, payload in recorded.items():
        assert now[name] == payload, f"the {name} measurement changed"


@pytest.mark.parametrize("name", ["run", "planned", "early_look", "offers"])
def test_every_new_field_is_null_on_a_binary_report(now: dict[str, dict[str, object]], name: str) -> None:
    report = IncrementalityReport.model_validate({**now[name], "computed_at": "2026-10-09T00:00:00Z"})
    assert {field: getattr(report, field) for field in M102_FIELDS} == dict.fromkeys(M102_FIELDS)
    assert not set(M102_FIELDS) & set(now[name]), "no M102 key appears in a binary report's JSON"
