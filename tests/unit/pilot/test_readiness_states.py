"""The readiness report's verdict follows the build's own state (Plan E M60, DEC-907)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from engine.clients import LocalClientStore
from engine.contracts import RunState
from engine.onboarding.datasets import DATASET_STATUS_FILENAME, dataset_key
from engine.onboarding.specs import BuildStatus
from engine.pilot.readiness import collect_readiness, readiness_document
from engine.storage import LocalStorage

NOW = datetime(2026, 9, 23, tzinfo=UTC)


def status(state: RunState, error: str | None = None) -> BuildStatus:
    return BuildStatus(
        dataset_id="ds_1",
        client_id="c_1",
        spec_id="spec_1",
        state=state,
        updated_at=NOW,
        stages=(),
        progress_pct=10,
        error=error,
    )


def verdict(tmp_path: Path, build: BuildStatus) -> tuple[str, str]:
    storage = LocalStorage(tmp_path)
    storage.write_model(dataset_key("ds_1", DATASET_STATUS_FILENAME), build)
    facts = collect_readiness(storage, LocalClientStore(tmp_path / "clients.db"), "ds_1")
    first = readiness_document(facts, now=NOW).blocks[0]
    return first.state, first.title  # type: ignore[union-attr]


def test_a_build_still_running_is_not_called_a_failure(tmp_path: Path) -> None:
    assert verdict(tmp_path, status(RunState.RUNNING)) == ("info", "Still building")


def test_a_build_that_failed_before_its_report_is_not_ready(tmp_path: Path) -> None:
    state, title = verdict(tmp_path, status(RunState.FAILED, error="The build stopped."))
    assert state == "not_ready" and title == "Not ready: the dataset could not be built"


def test_a_corrupt_artefact_degrades_the_report_instead_of_failing(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    storage.write_model(dataset_key("ds_1", DATASET_STATUS_FILENAME), status(RunState.RUNNING))
    storage.write_text(dataset_key("ds_1", "dataset_manifest.json"), '{"bogus": 1}')
    facts = collect_readiness(storage, LocalClientStore(tmp_path / "clients.db"), "ds_1")
    assert facts.manifest is None
