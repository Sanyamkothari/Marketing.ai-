"""Plan O M234 acceptance: on the 30 Plan L benchmark files, every `ready` file's split and training finish
(DEC-1804 (e)).

Each benchmark file goes the way Plan L M160 takes it - upload, Data Doctor, the hand-off, Guided setup to
Approve with no person - and the readiness report is computed on the prepared copy Approve wrote. One
test takes the plainest and the messiest file and runs the engine's own prepare, split and train-only fit
on every `ready` copy; the other takes all thirty and trains each `ready` copy to the end through
`POST /runs`, with plan §10's quick settings, and a last test holds the number of ready files to the
Plan L run's, so the claim cannot pass by finding no file ready. All three are slow (they drive Guided
setup to Approve on real benchmark files).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine import keys
from engine.config import ProblemType, RunMode, resolve_config
from engine.doctor.readiness import check_readiness, inputs_for_upload
from engine.doctor.readiness_contracts import DataReadinessReport
from engine.doctor.readiness_probes import label_key
from engine.stages import prepare
from engine.storage import LocalStorage, upload_key
from scripts.plan_l_benchmark import Messy, Outcome, _drive, cases, file_bytes

QUICK: dict[str, Any] = {
    "model_search.time_limit_minutes": 1,
    "model_search.strategy": "fast",
    "model_search.tuning_trials": 5,
}
"""Plan §10's quick training settings, as `tests/integration/test_train_flow.py` uses them."""


@dataclass
class Prepared:
    case: Messy
    upload_id: str | None
    primary_key: str | None
    target: str | None
    overrides: dict[str, Any]
    report: DataReadinessReport | None


class _Recording:
    """The test client, keeping Approve's answer: the upload, the roles and the overrides Run uses."""

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.applied: dict[str, Any] | None = None

    def post(self, url: str, **kwargs: Any) -> Any:
        response = self.client.post(url, **kwargs)
        if url.endswith("/apply") and response.status_code == 200:
            self.applied = response.json()
        return response

    def get(self, url: str, **kwargs: Any) -> Any:
        return self.client.get(url, **kwargs)


@pytest.fixture(scope="module")
def app(config_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[TestClient, Path]]:
    data_dir = tmp_path_factory.mktemp("readiness-bench")
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as client:
        yield client, data_dir


def _prepare(client: TestClient, data_dir: Path, case: Messy, config_root: Path) -> Prepared:
    """Plan L M160's journey to Approve, then the readiness report on the copy Approve wrote."""
    name, data, kind = file_bytes(case, config_root)
    response = client.post(
        "/uploads", files={"file": (name, data, kind)}, data={"use_case": case.use_case, "mode": "train"}
    )
    assert response.status_code == 201, response.text
    upload_id = response.json()["upload_id"]
    diagnosis = client.post("/doctor/diagnosis", json={"upload_ids": [upload_id]})
    verdict = (
        next((u for u in diagnosis.json()["use_cases"] if u["use_case_id"] == case.use_case), None)
        if diagnosis.status_code == 200
        else None
    )
    if verdict is not None and verdict["status"] in ("ready", "check"):
        handed = client.post("/doctor/handoff", json={"upload_id": upload_id, "use_case": case.use_case})
        if handed.status_code in (200, 201):
            upload_id = handed.json()["upload_id"]
    recording = _Recording(client)
    _drive(recording, upload_id, case.use_case, Outcome(name=case.name, use_case=case.use_case))
    if recording.applied is None:
        return Prepared(case, None, None, None, {}, None)
    applied = recording.applied
    overrides = {**applied["overrides"], **QUICK}
    cfg = resolve_config(case.use_case, overrides, root=config_root).config
    report = check_readiness(
        inputs_for_upload(
            LocalStorage(data_dir),
            applied["upload_id"],
            config=cfg,
            mode=RunMode.TRAIN,
            primary_key=applied["primary_key"],
            target=applied["target"],
            resolved_overrides=overrides,
        )
    )
    return Prepared(case, applied["upload_id"], applied["primary_key"], applied["target"], overrides, report)


def _split_and_fit_finish(storage: LocalStorage, prepared: Prepared, config_root: Path) -> None:
    """The engine's own prepare phase 1, split and train-only fit, on every row of the copy."""
    from engine.contracts import DatasetProfile
    from engine.stages import ingest

    assert prepared.upload_id and prepared.primary_key and prepared.target
    cfg = resolve_config(prepared.case.use_case, prepared.overrides, root=config_root).config
    profile = storage.read_model(upload_key(prepared.upload_id, "profile.json"), DatasetProfile)
    source = upload_key(prepared.upload_id, f"source.{profile.file_format}")
    frame = ingest.read_upload(storage, source, file_format=profile.file_format).frame
    target = prepared.target
    key_columns = list(keys.key_columns(prepared.primary_key))
    if len(key_columns) > 1:  # as `StageContext` does: a composite key splits by entity
        cfg = keys.split_config_for_key(
            resolve_config(prepared.case.use_case, prepared.overrides, root=config_root), key_columns
        ).config
    # As `pipeline._validate` then `_prepare` do: the joined row key, carried with the key's columns.
    framed = keys.with_row_key(frame, key_columns)
    carried = (*key_columns, keys.ROW_KEY_COLUMN) if len(key_columns) > 1 else tuple(key_columns)
    rows, plan = prepare.prepare_rows(framed, cfg, primary_key=carried, target=target)
    parts, _report = prepare.split_dataset(rows, cfg, run_id="bench-run-not-the-dry-run", target=target)
    prepare.fit_transforms(
        rows, cfg, plan, run_id="bench-run-not-the-dry-run", fit_index=parts["train"].index
    )
    for name, part in parts.items():
        assert not part.empty, name
        if cfg.problem_type is ProblemType.BINARY_CLASSIFICATION:
            assert part[target].dropna().map(label_key).nunique() == 2, name


@pytest.mark.slow
def test_the_plainest_and_the_messiest_ready_copy_split_and_fit(
    app: tuple[TestClient, Path], config_root: Path
) -> None:
    client, data_dir = app
    storage = LocalStorage(data_dir)
    plainest, messiest = cases()[0], cases()[5]
    seen = []
    for case in (plainest, messiest):
        prepared = _prepare(client, data_dir, case, config_root)
        assert prepared.report is not None, f"{case.name} did not reach Approve"
        seen.append(prepared.report.verdict)
        if prepared.report.verdict in ("ready", "ready_with_warnings"):
            assert prepared.report.dry_run_passed and not prepared.report.failing_probes
            _split_and_fit_finish(storage, prepared, config_root)
    assert seen[0] in ("ready", "ready_with_warnings"), "the plainest file is ready"


def _train(client: TestClient, prepared: Prepared, *, timeout_s: float = 900.0) -> dict[str, Any]:
    response = client.post(
        "/runs",
        json={
            "use_case": prepared.case.use_case,
            "mode": "train",
            "upload_id": prepared.upload_id,
            "primary_key": prepared.primary_key,
            "target": prepared.target,
            "overrides": prepared.overrides,
        },
    )
    assert response.status_code == 202, f"a ready file was refused by POST /runs: {response.text[:400]}"
    run_id = response.json()["run_id"]
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        record = client.get(f"/runs/{run_id}").json()["run"]
        if record["state"] in ("done", "failed", "cancelled"):
            return dict(record)
        time.sleep(2.0)
    raise AssertionError(f"{prepared.case.name}: run {run_id} did not finish in {timeout_s:.0f}s")


MIN_READY: int = 26
"""Ready (or ready with warnings) files among the thirty, at least: the Plan L run's count (26 `ready`
and 4 `ready_with_warnings`, re-measured in the M234 review), less a margin of four for a file a later
engine change rightly finds not ready. Fewer means the claim below was tested on too few files."""

_VERDICTS: dict[str, str] = {}


@pytest.mark.slow
@pytest.mark.parametrize("case", cases(), ids=[case.name for case in cases()])
def test_every_ready_benchmark_file_trains_to_the_end(
    case: Messy, app: tuple[TestClient, Path], config_root: Path
) -> None:
    client, data_dir = app
    prepared = _prepare(client, data_dir, case, config_root)
    _VERDICTS[case.name] = "no_report" if prepared.report is None else prepared.report.verdict
    if prepared.report is None or prepared.report.verdict not in ("ready", "ready_with_warnings"):
        return  # the claim is about ready files; the count is held by the test below
    record = _train(client, prepared)
    assert (
        record["state"] == "done"
    ), f"{case.name}: a ready file's run ended {record['state']}: {record.get('error')}"


@pytest.mark.slow
def test_enough_benchmark_files_were_ready_for_the_claim_to_mean_something() -> None:
    """After the thirty above: the claim "every ready file trains" was tested on at least MIN_READY files."""
    names = [case.name for case in cases()]
    missing = [name for name in names if name not in _VERDICTS]
    if missing:
        pytest.skip(f"run the whole module: {len(missing)} benchmark file(s) were not checked in this run")
    ready = [name for name in names if _VERDICTS[name] in ("ready", "ready_with_warnings")]
    assert len(ready) >= MIN_READY, {name: _VERDICTS[name] for name in names if name not in ready}
