"""Plan J M100 part B: a run of one offer chooses nothing and writes nothing new (DEC-1310 (q) on).

The default `win-back-campaign` uplift run of `tests/fixtures/decide/m100_binary.py` (the product's API,
pinned ids), as the M100 binary identity golden runs it: its digests still equal the ones recorded
before M100, the scoring run writes no `offer_choice.*` and no catalogue stamp, and its treat list has
the three new columns empty.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pandas as pd
import pytest

from engine.decide.treat_list import TREAT_LIST_CSV, build_treat_list
from engine.storage import LocalStorage, run_key
from tests.fixtures.decide.m100_binary import GOLDEN, SCORE_RUN, default_run_digests

pytestmark = pytest.mark.integration


def test_a_run_of_one_offer_is_byte_identical_and_chooses_nothing(config_root: Path, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    produced = default_run_digests(config_root, data_dir)
    assert produced == json.loads(GOLDEN.read_text(encoding="utf-8"))
    storage = LocalStorage(data_dir)
    for name in ("offer_choice.parquet", "offer_choice.json", "catalogue_stamp.json"):
        assert not storage.exists(run_key(SCORE_RUN, name)), name
    record = json.loads(storage.read_text(run_key(SCORE_RUN, "run.json")))
    assert not {"offer_choice.parquet", "offer_choice.json"} & set(record["artefacts"])
    summary = build_treat_list(storage, SCORE_RUN)
    assert summary.offer_counts is None and summary.channel_rows is None
    text = storage.read_bytes(run_key(SCORE_RUN, TREAT_LIST_CSV)).decode("utf-8")
    out = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)
    # Between contactable_channels and net_value: every earlier column keeps its position from the start,
    # and net_value, expected_gross_value and the three reasons theirs from the end (M98 pins those).
    after = list(out.columns).index("contactable_channels")
    assert list(out.columns[after + 1 : after + 4]) == [
        "runner_up_offer",
        "runner_up_net_value",
        "offer_reason",
    ]
    assert list(out.columns[-5:]) == [
        "net_value",
        "expected_gross_value",
        "reason_1",
        "reason_2",
        "reason_3",
    ]
    assert (out[["runner_up_offer", "runner_up_net_value", "offer_reason"]] == "").all().all()
    saved = json.loads(storage.read_text(run_key(SCORE_RUN, "treat_list_summary.json")))
    assert "offer_counts" not in saved and "channel_rows" not in saved
