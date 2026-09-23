"""Demo mode: a synthetic client, already onboarded, trained, scored and measured (Plan E M63, DEC-910).

A sales or management demo must never wait for a model to train or touch a real client's file. So
the demo is *seeded* once, ahead of time, by `scripts/seed_demo.py` (`make demo-seed`), which drives
the platform's own API through every step a pilot takes - raw tables uploaded and mapped, a dataset
built, a churn model trained and made champion, next month scored with a control group, a win-back
uplift model trained on a past randomised campaign, and both campaigns measured once their outcome
windows had passed - and writes what it made into :class:`DemoManifest`. With `demo_mode` on, the
API serves that manifest (`GET /pilot/demo`) and the screens open on "Demo Telecom".

Everything in it is synthetic: the tables come from the seeded generators the test-suite uses
(`tests/fixtures/raw/make_raw.py`, `tests/fixtures/make_uplift_data.py`), and the campaign outcomes
are simulated with a stated, planted effect. No public or non-commercial dataset is used - the
Criteo uplift sample in particular is excluded (Plan D, R2) - and the manifest says so
(`data_sources`), which a test pins.

This module holds only the manifest and how to find it; it imports no test code, so the API and the
container can read a demo that a checkout seeded.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from engine.storage import Storage

__all__ = [
    "DEMO_CLIENT_NAME",
    "DEMO_MANIFEST_KEY",
    "EXCLUDED_DATA",
    "DemoCampaign",
    "DemoManifest",
    "load_demo",
]

DEMO_CLIENT_NAME: Final[str] = "Demo Telecom"
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


def load_demo(storage: Storage) -> DemoManifest | None:
    """The seeded demo under `storage`, or None when nobody has seeded one."""
    from engine.storage import StorageError

    try:
        if not storage.exists(DEMO_MANIFEST_KEY):
            return None
        return storage.read_model(DEMO_MANIFEST_KEY, DemoManifest)
    except (StorageError, ValueError):  # a stale or corrupt manifest is no demo, not a 500
        return None


def raw_key(variant: str, file_name: str) -> str:
    return f"{DEMO_RAW_PREFIX}/{variant}/{file_name}"
