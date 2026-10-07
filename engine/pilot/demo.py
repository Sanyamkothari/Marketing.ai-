"""Demo mode: a synthetic client, already onboarded, trained, scored and measured (Plan E M63, DEC-910).

A sales or management demo must never wait for a model to train or touch a real client's file. So
the demo is *seeded* once, ahead of time, by `scripts/seed_demo.py` (`make demo-seed`), which drives
the platform's own API through every step a pilot takes - raw tables uploaded and mapped, a dataset
built, a churn model trained and made champion, next month scored with a control group, a win-back
uplift model trained on a past randomised campaign, and both campaigns measured once their outcome
windows had passed - and writes what it made into :class:`DemoManifest`. With `demo_mode` on, the
API serves that manifest (`GET /pilot/demo`) and the screens open on "Demo Company" (a neutral
name since Plan J M93: the product is sold to any B2C business, so the demo no longer says telecom).

Everything in it is synthetic: the tables come from the seeded generators the test-suite uses
(`tests/fixtures/raw/make_raw.py`, `tests/fixtures/make_uplift_data.py`), and the campaign outcomes
are simulated with a stated, planted effect. No public or non-commercial dataset is used - the
Criteo uplift sample in particular is excluded (Plan D, R2) - and the manifest says so
(`data_sources`), which a test pins.

**Quarantine (Plan J M95).** The planted effect must never be read as a result. Every run the demo
made is therefore recorded with `RunRecord.synthetic` true (:func:`mark_runs_synthetic`, called by the
seeder as each run finishes), and every report drawn from such a run - results and value, in HTML and
PDF - carries the block "Synthetic data: planted effect, not a forecast" (`engine.pilot.document`).

This module holds only the manifest and how to find it; it imports no test code, so the API and the
container can read a demo that a checkout seeded.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from collections.abc import Iterable

    from engine.storage import Storage

__all__ = [
    "DEMO_CLIENT_NAME",
    "DEMO_MANIFEST_KEY",
    "EXCLUDED_DATA",
    "DemoCampaign",
    "DemoManifest",
    "is_demo_client",
    "load_demo",
    "mark_runs_synthetic",
]

DEMO_CLIENT_NAME: Final[str] = "Demo Company"
DEMO_MANIFEST_KEY: Final[str] = "pilot/demo/demo.json"
"""Storage key of the manifest: under the artefact root, so a demo seeded into S3 is found there."""

DEMO_RAW_PREFIX: Final[str] = "pilot/demo/raw"
"""`<prefix>/<variant>/<table>.csv`: the raw tables, kept so a visitor can run the pre-flight check."""

EXCLUDED_DATA: Final[tuple[str, ...]] = ("criteo",)
"""Data the demo may never contain (Plan D, R2: non-commercial licence). Checked against
`DemoManifest.data_sources` by the seeder and by the tests."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DemoCampaign(_Strict):
    """One measured campaign of the demo, and the value inputs seeded beside it."""

    use_case_id: str
    title: str
    score_run_id: str
    outcome_column: str
    simulated_effect: str
    """How the outcomes were simulated, in one sentence, so nobody mistakes them for a real result."""


class DemoManifest(_Strict):
    schema_version: Literal[1] = 1
    client_id: str
    client_name: str = DEMO_CLIENT_NAME
    broken_client_id: str | None = None
    use_case_id: str
    train_dataset_id: str
    broken_dataset_id: str | None = None
    planted_problem: str | None = None
    """The one blocking code planted in the broken extract, for the acceptance exercise."""
    train_run_id: str
    champion_model_id: str
    score_dataset_id: str
    score_run_id: str
    uplift_use_case_id: str
    uplift_run_id: str
    uplift_score_run_id: str
    campaigns: tuple[DemoCampaign, ...]
    raw_variants: tuple[str, ...] = ("clean", "broken")
    data_sources: tuple[str, ...] = Field(
        description="Every generator the demo's data came from; synthetic only."
    )
    seeded_at: datetime
    seed: int

    @property
    def run_ids(self) -> tuple[str, ...]:
        """Every run the demo made: the churn model's training and scoring runs, the win-back model's
        training run and its scoring run. Each one is recorded as synthetic."""
        return (self.train_run_id, self.score_run_id, self.uplift_run_id, self.uplift_score_run_id)


def load_demo(storage: Storage) -> DemoManifest | None:
    """The seeded demo under `storage`, or None when nobody has seeded one."""
    from engine.storage import StorageError

    try:
        if not storage.exists(DEMO_MANIFEST_KEY):
            return None
        return storage.read_model(DEMO_MANIFEST_KEY, DemoManifest)
    except (StorageError, ValueError):  # a stale or corrupt manifest is no demo, not a 500
        return None


def is_demo_client(storage: Storage, client_id: str | None) -> bool:
    """True when `client_id` is the seeded demo's client (clean or broken extract) under `storage`.

    A dataset built from the demo's generated raw tables carries that client id, so a run started on it
    - a new training run, a schedule's firing - reads planted data and is recorded synthetic, exactly as
    the runs the seeder made are (Plan J M95).
    """
    if client_id is None:
        return False
    demo = load_demo(storage)
    return demo is not None and client_id in {demo.client_id, demo.broken_client_id}


def mark_runs_synthetic(storage: Storage, run_ids: Iterable[str]) -> tuple[str, ...]:
    """Record each run as `synthetic` in its `run.json`; returns the ids it changed.

    Idempotent: a run already marked is left alone and not listed. Call it once a run has finished -
    `update_run` reads, copies and writes `run.json`, so marking a run still writing would race the
    pipeline's own updates. A run that does not exist raises `StorageError`: nothing is silently
    skipped, because an unmarked demo run would show its planted effect as a result.
    """
    from engine.contracts import RunRecord
    from engine.runs import RUN_FILENAME, update_run
    from engine.storage import run_key

    changed: list[str] = []
    for run_id in run_ids:
        if storage.read_model(run_key(run_id, RUN_FILENAME), RunRecord).synthetic:
            continue
        update_run(storage, run_id, synthetic=True)
        changed.append(run_id)
    return tuple(changed)


def raw_key(variant: str, file_name: str) -> str:
    return f"{DEMO_RAW_PREFIX}/{variant}/{file_name}"
