"""Real treat lists of several use cases over the same customers, for the arbitration tests (Plan J M101).

Each list is what the product writes: `write_run` (the fixture of the M98 tests) writes the scoring run
with Phase 1's `apply_actions` or M97's `apply_uplift_actions`, and `build_treat_list` builds the treat
list from the run's artefacts. Only the run record's use case id is changed (as the integration tests of
`POST /decide/arbitrate` do), because the fixture writes every run for the same use case. Nothing here
writes a treat list, a flag or a value by hand.
"""

from __future__ import annotations

import io
from typing import Any

import pandas as pd

from engine.contracts import RunRecord
from engine.decide.treat_list import TREAT_LIST_PARQUET, build_treat_list
from engine.runs import RUN_FILENAME
from engine.storage import Storage, run_key
from tests.fixtures.decide.treat_runs import write_run

__all__ = ["KEY", "ROWS", "run_for_use_case", "treat_list_for_use_case"]

KEY = ("customer_id",)
ROWS = 200
_COUNTER = {"n": 0}


def run_for_use_case(storage: Storage, use_case: str, **options: Any) -> str:
    """Write a scoring run of `use_case` (see `write_run` for the options) and return its run id."""
    _COUNTER["n"] += 1
    run_id = f"r_20261001_{_COUNTER['n']:08x}"
    options.setdefault("rows", ROWS)
    write_run(storage, run_id, **options)
    record = storage.read_model(run_key(run_id, RUN_FILENAME), RunRecord)
    renamed = record.model_copy(update={"use_case_id": use_case, "use_case_name": use_case})
    storage.write_model(run_key(run_id, RUN_FILENAME), renamed)
    return run_id


def treat_list_for_use_case(storage: Storage, use_case: str, **options: Any) -> pd.DataFrame:
    """The treat list the product builds for a new scoring run of `use_case`."""
    run_id = run_for_use_case(storage, use_case, **options)
    build_treat_list(storage, run_id)
    return pd.read_parquet(io.BytesIO(storage.read_bytes(run_key(run_id, TREAT_LIST_PARQUET))))
