"""The M52 load-test harness (`scripts/load_test.py`), run tiny.

What these tests pin is the harness's honesty, not the API's speed: the statistics are nearest-rank
and never invent a latency, the synthetic runner never runs more jobs than it has workers and
reports the ones it had to queue, and a whole local load test - child-process server, real HTTP,
real ingest and validation, a seeded champion - finishes with zero errors and writes a report that
says it is a laptop number. A few seconds, because the server is a spawned interpreter.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from engine.contracts import RunState
from engine.jobs import CancelToken, JobRunner
from scripts import load_test
from scripts.load_test import (
    CAVEAT,
    LoadProfile,
    Sample,
    SyntheticJobRunner,
    latency_stats,
    percentile,
    queue_stats,
)


def test_percentile_is_nearest_rank_and_never_interpolates() -> None:
    values = [0.4, 0.1, 0.3, 0.2]
    assert percentile(values, 50) == 0.2
    assert percentile(values, 95) == 0.4
    assert percentile(values, 100) == 0.4
    assert percentile([], 50) is None
    assert all(percentile(values, pct) in values for pct in (1, 25, 50, 75, 99))
    with pytest.raises(ValueError, match="pct"):
        percentile(values, 0)


def test_latency_stats_counts_every_unexpected_answer_as_an_error() -> None:
    samples = [
        Sample("upload", "201", 0.010, True, None),
        Sample("upload", "409", 0.020, False, "UPLOAD_MODE_MISMATCH"),
        Sample("upload", "ReadTimeout", 30.0, False, "ReadTimeout"),
        Sample("upload", "201", 0.030, True, None),
    ]
    stats = latency_stats(samples)
    assert (stats.requests, stats.errors, stats.error_rate) == (4, 2, 0.5)
    assert stats.p50_ms == 20.0
    assert stats.max_ms == 30_000.0
    assert stats.statuses == {"201": 2, "409": 1, "ReadTimeout": 1}
    assert stats.error_codes == {"ReadTimeout": 1, "UPLOAD_MODE_MISMATCH": 1}
    assert latency_stats([]).p50_ms is None


def test_a_profile_that_cannot_run_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        LoadProfile(users=0)
    with pytest.raises(ValueError, match="mix"):
        LoadProfile(mix={"list_runs": 0})


def test_the_synthetic_runner_queues_beyond_its_workers_and_never_exceeds_them() -> None:
    runner = SyntheticJobRunner(max_workers=1, job_seconds=0.05)
    assert isinstance(runner, JobRunner)

    def never_called(_token: CancelToken) -> None:
        raise AssertionError("the API's closure must not run under the synthetic runner")

    for index in range(4):
        runner.submit(f"job-{index}", never_called)
    assert runner.drain(10.0)
    stats = queue_stats(runner, max_workers=1, drain_seconds=0.0, drained=True)
    runner.shutdown()

    assert (stats.submitted, stats.finished, stats.failed) == (4, 4, 0)
    assert stats.peak_running == 1
    assert stats.queued_jobs >= 2  # jobs 2..4 each waited at least one 50 ms job
    assert stats.peak_waiting >= 2
    assert stats.wait_max_ms is not None and stats.wait_max_ms >= 50


def test_an_idle_runner_reports_no_queue() -> None:
    runner = SyntheticJobRunner(max_workers=2, job_seconds=0.0)
    runner.submit("only", lambda _token: None)
    assert runner.drain(10.0)
    stats = queue_stats(runner, max_workers=2, drain_seconds=0.0, drained=True)
    runner.shutdown()
    assert (stats.queued_jobs, stats.peak_waiting) == (0, 0)


def test_cancelling_a_synthetic_job_ends_it_cancelled() -> None:
    runner = SyntheticJobRunner(max_workers=1, job_seconds=10.0)
    runner.submit("long", lambda _token: None)
    assert runner.cancel("long")
    assert runner.drain(5.0)
    assert runner.status("long").state is RunState.CANCELLED
    runner.shutdown()


def test_a_tiny_local_load_test_queues_scoring_runs_and_writes_a_laptop_report(tmp_path: Path) -> None:
    output = tmp_path / "load.json"
    exit_code = load_test.main(
        [
            "--users",
            "3",
            "--requests-per-user",
            "3",
            "--max-workers",
            "1",
            "--job-seconds",
            "0.3",
            "--rows-per-upload",
            "20",
            "--mix",
            "start_score_run=1",
            "--output",
            str(output),
        ]
    )
    report = load_test.LoadTestReport.model_validate(json.loads(output.read_text(encoding="utf-8")))

    assert exit_code == 0
    assert report.overall.requests == 9
    assert report.overall.errors == 0, report.overall.error_codes
    assert report.caveat == CAVEAT and "Laptop numbers, not AWS ones" in report.caveat
    assert report.target.startswith("local uvicorn")
    queue = report.queue
    assert queue is not None
    assert queue.drained and queue.finished == queue.submitted
    assert queue.submitted == 1 + report.by_action["start_score_run"].requests
    assert queue.peak_running <= queue.max_workers == 1
    # Ten 0.3 s jobs on one worker, submitted by three users at once: the runner must have queued.
    assert queue.submitted == 10
    assert queue.queued_jobs >= 1 and queue.peak_waiting >= 1


def test_the_mix_option_parses_weights_and_refuses_unknown_actions() -> None:
    assert load_test.parse_mix("upload=2, start_score_run=1") == {"upload": 2, "start_score_run": 1}
    with pytest.raises(argparse.ArgumentTypeError):
        load_test.parse_mix("delete_everything=1")
    with pytest.raises(argparse.ArgumentTypeError):
        load_test.parse_mix("upload=many")


def test_the_default_report_path_is_one_file_per_utc_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(load_test, "REPORT_DIR", tmp_path / "reports" / "load")
    report = load_test._report(  # the one place a report is assembled; no load needed to test the path
        LoadProfile(users=1, requests_per_user=1),
        [Sample("list_runs", "200", 0.001, True, None)],
        0.01,
        target="t",
        caveat=CAVEAT,
        queue=None,
        notes=(),
    )
    path = load_test.write_report(report)
    assert path == tmp_path / "reports" / "load" / f"{report.generated_at.date().isoformat()}.json"
    assert json.loads(path.read_text(encoding="utf-8"))["overall"]["requests"] == 1
