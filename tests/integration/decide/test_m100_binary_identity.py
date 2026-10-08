"""Plan J M100 acceptance: with one treatment level every uplift artefact is byte-identical (DEC-668).

A default `win-back-campaign` uplift run and a scoring run of its model go through the product's own
API with pinned ids (`tests/fixtures/decide/m100_binary.py`). Their artefacts' digests, timestamps and
durations masked, must equal the digests the code wrote before M100 (`golden/m100_binary_digests.json`,
recorded on the commit before the change): `uplift_evaluation.json`, `policy_recommendation.json`,
`segments.json`, `uplift_validation.json`, the Qini curve, the split, the hold-out file, `scores.csv`
and the scoring summary. A new optional field that serialised as `null`, a new column or a changed
seed would each change a digest.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.fixtures.decide.m100_binary import GOLDEN, default_run_digests

pytestmark = pytest.mark.integration


def test_a_default_uplift_run_writes_the_same_bytes_as_before_m100(config_root: Path, tmp_path: Path) -> None:
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    produced = default_run_digests(config_root, tmp_path / "data")
    changed = sorted(name for name in expected if produced.get(name) != expected[name])
    assert set(produced) == set(expected)
    assert changed == [], f"artefacts no longer byte-identical: {changed}"
