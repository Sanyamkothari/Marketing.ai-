"""Run the engine end to end on a public dataset and write the numbers a `run_report.md` needs.

This is the harness behind every `library/<dataset>/run_report.md`. It owns no modelling: it
uploads a CSV, calls `Pipeline.run_train` exactly the way `POST /runs` does, and then reads the
artefacts the run wrote. Every number that reaches a report comes out of this script, so a report
can never quote a figure that no run produced.

Nothing here is engine code, and nothing here may become engine code: a dataset that needs a
behaviour the engine does not have is a `docs/CROSS_BRANCH_REQUESTS.md` entry, not a patch applied from
this file.

    python -m library.run_engine \
        --dataset uci-bank-marketing \
        --use-case bank-term-deposit \
        --csv library/uci-bank-marketing/data/bank-additional-full.prepared.csv \
        --primary-key client_id --target y

Writes `library/.runs/<dataset>/<run_id>/` (git-ignored) plus a `results.json` holding the
validation findings, the leaderboard, the test metrics, the baseline comparison, the decile-1 lift,
the top features and the wall-clock time.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:  # run as a script from a checkout without an editable install
    sys.path.insert(0, str(REPO_ROOT))

from engine.config import RunMode, resolve_config  # noqa: E402
from engine.contracts import (  # noqa: E402
    BaselineComparison,
    BestModel,
    DecileLift,
    EvaluationReport,
    FeatureImportance,
    Leaderboard,
    PrepareReport,
    RunState,
    SplitReport,
    ValidationReport,
)
from engine.jobs import CancelToken  # noqa: E402
from engine.pipeline import Pipeline, StageContext  # noqa: E402
from engine.registry import LocalModelRegistry  # noqa: E402
from engine.storage import LocalStorage, run_key, upload_key  # noqa: E402
from engine.utils.ids import new_run_id  # noqa: E402

RUNS_DIR = Path(__file__).resolve().parent / ".runs"


class _NoJobs:
    """`run_train` runs on this thread; the runner only exists to satisfy the constructor."""

    def submit(self, job_id: str, fn: object) -> object:  # noqa: ARG002
        raise AssertionError("run_train must not submit jobs")

    def status(self, job_id: str) -> object:  # noqa: ARG002
        raise AssertionError("run_train must not read job status")

    def cancel(self, job_id: str) -> bool:  # noqa: ARG002
        return False

    def shutdown(self, *, wait: bool = True) -> None:  # noqa: ARG002
        return None


@dataclass
class RunOutcome:
    run_id: str
    state: str
    error: dict[str, Any] | None
    results: dict[str, Any]
    directory: Path


def _parse_override(raw: str) -> tuple[str, Any]:
    """`path=value` where the value is read as JSON when it parses as JSON, else as a string."""
    if "=" not in raw:
        raise argparse.ArgumentTypeError(f"--override wants path=value, got {raw!r}")
    path, _, value = raw.partition("=")
    try:
        return path, json.loads(value)
    except json.JSONDecodeError:
        return path, value


def _artefact(storage: LocalStorage, run_id: str, name: str, model: type) -> Any | None:
    key = run_key(run_id, name)
    if not storage.exists(key):
        return None
    return storage.read_model(key, model)


def _validation_rows(report: ValidationReport | None) -> list[dict[str, Any]]:
    if report is None:
        return []
    return [
        {
            "code": check.code,
            "severity": check.severity.value,
            "column": check.column,
            "message": check.message,
            "suggestion": check.suggestion,
            "acknowledgeable": check.acknowledgeable,
            "acknowledged": check.acknowledged,
        }
        for check in report.checks
    ]


def run(
    *,
    dataset: str,
    use_case: str,
    csv_path: Path,
    primary_key: str,
    target: str,
    overrides: dict[str, Any],
    config_root: Path | None = None,
) -> RunOutcome:
    """Upload `csv_path`, run the train flow, and collect every number a report may quote."""
    directory = RUNS_DIR / dataset
    directory.mkdir(parents=True, exist_ok=True)
    storage = LocalStorage(directory / "data")
    registry = LocalModelRegistry(directory / "registry.db")

    upload_id = f"u_{dataset.replace('-', '_')}"
    upload = upload_key(upload_id, csv_path.name)
    storage.write_bytes(upload, csv_path.read_bytes())

    resolved = resolve_config(use_case, overrides, root=config_root)
    run_id = new_run_id()
    storage.write_model(run_key(run_id, "run_config.json"), resolved)

    ctx = StageContext(
        run_id=run_id,
        mode=RunMode.TRAIN,
        config=resolved.config,
        resolved=resolved,
        storage=storage,
        registry=registry,
        cancel=CancelToken(),
        primary_key=primary_key,
        target=target,
        upload_key=upload,
        model_version_id=None,
    )

    started = time.perf_counter()
    failure: dict[str, Any] | None = None
    state = "unknown"
    try:
        record = Pipeline(storage, registry, _NoJobs()).run_train(ctx)
        state = record.state.value
        if record.error is not None:
            failure = {"code": record.error.code, "message": record.error.message}
    except Exception as exc:  # a refused run is a result the report has to state, not a crash
        state = RunState.FAILED.value
        failure = {"code": type(exc).__name__, "message": str(exc)}
    elapsed = time.perf_counter() - started

    validation = _artefact(storage, run_id, "validation.json", ValidationReport)
    prepare = _artefact(storage, run_id, "prepare.json", PrepareReport)
    split = _artefact(storage, run_id, "split.json", SplitReport)
    leaderboard = _artefact(storage, run_id, "leaderboard.json", Leaderboard)
    best = _artefact(storage, run_id, "best_model.json", BestModel)
    evaluation = _artefact(storage, run_id, "evaluation.json", EvaluationReport)
    baseline = _artefact(storage, run_id, "baseline.json", BaselineComparison)
    decile = _artefact(storage, run_id, "decile_lift.json", DecileLift)
    importance = _artefact(storage, run_id, "feature_importance.json", FeatureImportance)

    results: dict[str, Any] = {
        "dataset": dataset,
        "use_case": use_case,
        "csv": str(csv_path),
        "primary_key": primary_key,
        "target": target,
        "overrides": overrides,
        "run_id": run_id,
        "state": state,
        "error": failure,
        "wall_clock_seconds": round(elapsed, 1),
        "validation": {
            "passed": validation.passed if validation else None,
            "error_count": validation.error_count if validation else None,
            "warning_count": validation.warning_count if validation else None,
            "checks": _validation_rows(validation),
        },
    }

    if prepare is not None:
        results["prepare"] = {
            "rows_in": prepare.rows_in,
            "rows_out": prepare.rows_out,
            "columns_in": prepare.columns_in,
            "columns_out": prepare.columns_out,
            "feature_columns": list(prepare.feature_columns),
            "dropped_columns": [
                {"name": d.name, "reason": d.reason, "detail": d.detail} for d in prepare.dropped_columns
            ],
            "pii_columns": list(prepare.pii_columns),
        }
    if split is not None:
        results["split"] = {
            "type": split.type.value,
            "time_column": split.time_column,
            "parts": [
                {"name": p.name, "rows": p.rows, "positive_rate": p.positive_rate} for p in split.parts
            ],
        }
    if leaderboard is not None:
        results["leaderboard"] = {
            "metric": leaderboard.metric.value,
            "models_trained": leaderboard.models_trained,
            "entries": [
                {
                    "rank": e.rank,
                    "model_name": e.model_name,
                    "family_label": e.family_label,
                    "validation_score": e.validation_score,
                    "test_score": e.test_score,
                    "fit_time_seconds": e.fit_time_seconds,
                    "is_ensemble": e.is_ensemble,
                }
                for e in leaderboard.entries
            ],
        }
    if best is not None:
        results["best_model"] = {
            "display_name": best.display_name,
            "model_name": best.model_name,
            "metric": best.metric.value,
            "validation_score": best.validation_score,
            "test_score": best.test_score,
            "training_rows": best.training_rows,
            "feature_count": best.feature_count,
            "hyperparameters_summary": best.hyperparameters_summary,
        }
    if evaluation is not None:
        results["evaluation"] = {
            "rows_evaluated": evaluation.rows_evaluated,
            "positive_rate": evaluation.positive_rate,
            "primary_metric": evaluation.primary_metric.value,
            "headline_score": evaluation.headline_score,
            "threshold": evaluation.threshold,
            "threshold_mode": evaluation.threshold_mode.value,
            "metrics": {m.id.value: m.value for m in evaluation.metrics},
            "extra_metrics": dict(evaluation.extra_metrics),
        }
    if baseline is not None:
        results["baseline"] = {
            "baseline_name": baseline.baseline_name,
            "model_beats_baseline": baseline.model_beats_baseline,
            "rows": [
                {
                    "metric": r.id.value,
                    "label": r.label,
                    "model_value": r.model_value,
                    "baseline_value": r.baseline_value,
                    "delta": r.delta,
                    "model_better": r.model_better,
                }
                for r in baseline.rows
            ],
        }
    if decile is not None:
        results["decile_lift"] = {
            "base_rate_pct": decile.base_rate_pct,
            "unit": decile.unit,
            "caption": decile.caption,
            "values": list(decile.values),
            "bins": [
                {
                    "decile": b.decile,
                    "label": b.label,
                    "rows": b.rows,
                    "positives": b.positives,
                    "actual_rate_pct": b.actual_rate_pct,
                    "lift": b.lift,
                    "cumulative_lift": b.cumulative_lift,
                    "cumulative_capture_pct": b.cumulative_capture_pct,
                }
                for b in decile.bins
            ],
        }
    if importance is not None:
        results["feature_importance"] = {
            "method": importance.method,
            "items": [
                {
                    "rank": i.rank,
                    "feature": i.feature,
                    "importance": i.importance,
                    "share_pct": i.share_pct,
                }
                for i in importance.items[:10]
            ],
        }

    out = directory / f"{run_id}.results.json"
    out.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    return RunOutcome(run_id=run_id, state=state, error=failure, results=results, directory=directory)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m library.run_engine", description=__doc__)
    parser.add_argument("--dataset", required=True, help="library/<dataset> slug")
    parser.add_argument("--use-case", required=True, help="use-case id in configs/use_cases/")
    parser.add_argument("--csv", required=True, type=Path, help="the one-row-per-entity CSV to upload")
    parser.add_argument("--primary-key", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument(
        "--config-root",
        type=Path,
        default=None,
        help="config root to load the use case from; defaults to the repo's configs/",
    )
    parser.add_argument(
        "--override",
        action="append",
        default=[],
        metavar="PATH=VALUE",
        help="run override, repeatable; the value is read as JSON when it parses as JSON",
    )
    args = parser.parse_args(argv)

    outcome = run(
        dataset=args.dataset,
        use_case=args.use_case,
        csv_path=args.csv,
        primary_key=args.primary_key,
        target=args.target,
        overrides=dict(_parse_override(o) for o in args.override),
        config_root=args.config_root,
    )
    print(json.dumps(outcome.results, indent=2, default=str))
    return 0 if outcome.state == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
