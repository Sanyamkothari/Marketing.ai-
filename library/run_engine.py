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
        --csv library/uci-bank-marketing/data/prepared.csv \
        --primary-key client_id --target y

**The whole journey (Plan J M110).** `python -m library.run_engine journey --dataset hillstrom-email` runs
every step a person takes on a randomised dataset - readiness, the risk and campaign-effect models, the
approval checks, the treat list, the off-policy and campaign measurements and the Value Proof Pack -
through the product's own API (`library/journey.py`), and renders `library/<dataset>/run_report.md` from
what the run wrote (`library/journey_report.py`).

**The audit readout (Plan J, DEC-1322).** `python -m library.run_engine audit --dataset hillstrom-email` reads the
dataset's *original* randomised campaign (who got which e-mail, and what happened) through the route a
client uses, `POST /campaigns/audit` (`library/audit.py`), records the label, the per-offer effects, the
Value Proof Packs and the programme route's answer, and renders them as section 8 of the same
`run_report.md` (`library/audit_report.py`). The report is the journey's sections followed by the audit's,
rendered here and nowhere else. Both results files are committed beside the report (aggregates only; M111
committed the journey's), and `journey_report_text` reads the committed audit results unless it is given others,
so the committed report is checkable from a clean checkout. The audit command writes its results to
`library/.runs/<dataset>/audit/` and to `library/<dataset>/audit.results.json`, and renders the report from the
committed journey results (`--journey-results` names others).

Writes the run directory to `library/.runs/<dataset>/data/runs/<run_id>/` (git-ignored) and, beside
the dataset directory, `library/.runs/<dataset>/<run_id>.results.json` holding the validation
findings, the leaderboard, the test metrics, the baseline comparison, the decile-1 lift, the top
features and the wall-clock time.

**The CLI keeps one model registry per dataset**, at `library/.runs/<dataset>/registry.db`, so that
successive runs of the same dataset can be compared. That file is created, never migrated: when
`ModelVersionRow` gains a column, an old `registry.db` has to be deleted before the next run
(`engine.registry.LocalModelRegistry`, DEC-341). The library's tests do not share this - `run()`
takes a `runs_dir`, and every test passes a fresh temporary directory, so a test run leaves nothing
in the repository and can never trip over a registry an earlier run created.
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
AUDIT_RESULTS_NAME = "audit.results.json"
"""The audit results, kept in `.runs/<dataset>/audit/` and committed as `library/<dataset>/` + this (DEC-1322)."""


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
    runs_dir: Path | None = None,
) -> RunOutcome:
    """Upload `csv_path`, run the train flow, and collect every number a report may quote.

    `runs_dir` defaults to `library/.runs`, which persists between runs so the CLI's artefacts can
    be read afterwards. Tests pass a temporary directory instead, so they are hermetic.
    """
    directory = (runs_dir if runs_dir is not None else RUNS_DIR) / dataset
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


def committed_results_path(dataset: str, name: str) -> Path:
    """`library/<dataset>/<name>`: a results file committed beside the dataset's `run_report.md`."""
    return Path(__file__).resolve().parent / dataset / name


def journey_report_text(
    results: dict[str, Any], audit: dict[str, Any] | None = None, *, committed_audit: bool = True
) -> str:
    """The `run_report.md` of a journey's results, exactly as `journey_main` writes it.

    A function of the results alone (and of where the CLI keeps them), so the committed report can be
    checked against the run it names: `library/tests/test_hillstrom_email.py` does. The audit readout
    follows the journey as section 8 (DEC-1322): the `audit` results given, else the dataset's committed
    `audit.results.json` when there is one (`committed_audit=False` renders the journey alone).
    """
    from library.journey_report import render_report

    dataset = str(results["dataset"])
    link = f"../.runs/{dataset}/journey/journey.results.json"
    command = f"python library/{dataset}/fetch.py\npython -m library.run_engine journey --dataset {dataset}"
    text = render_report(results, results_path=link, command=command)
    if audit is None and committed_audit:
        audit = _read_json(committed_results_path(dataset, AUDIT_RESULTS_NAME))
    if audit is None:
        return text
    from library.audit_report import render_audit

    audit_link = f"{AUDIT_RESULTS_NAME}"
    audit_command = (
        f"python library/{dataset}/fetch.py\npython -m library.run_engine audit --dataset {dataset}"
    )
    return text.rstrip("\n") + "\n\n" + render_audit(audit, results_path=audit_link, command=audit_command)


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def journey_main(argv: list[str]) -> int:
    """`python -m library.run_engine journey --dataset <slug>`: the whole Plan J journey (M110).

    Runs `library.journey.run_journey` on the dataset's prepared file, writes its results to
    `library/.runs/<dataset>/journey/journey.results.json` and renders `library/<dataset>/run_report.md`
    from them. The report is produced here and nowhere else, so every number in it comes out of this run.
    """
    from library.journey import JOURNEYS, run_journey

    parser = argparse.ArgumentParser(
        prog="python -m library.run_engine journey", description=journey_main.__doc__
    )
    parser.add_argument("--dataset", required=True, choices=sorted(JOURNEYS), help="library/<dataset> slug")
    parser.add_argument("--csv", type=Path, default=None, help="default: library/<dataset>/data/prepared.csv")
    parser.add_argument("--config-root", type=Path, default=REPO_ROOT / "configs")
    parser.add_argument(
        "--runs-dir", type=Path, default=RUNS_DIR, help="default: library/.runs; any other writes no report"
    )
    args = parser.parse_args(argv)
    spec = JOURNEYS[args.dataset]
    library = Path(__file__).resolve().parent
    csv_path = (args.csv or library / spec.dataset / "data" / "prepared.csv").resolve()
    if not csv_path.is_file():
        print(f"{csv_path} is missing: run python library/{spec.dataset}/fetch.py first", file=sys.stderr)
        return 2
    outcome = run_journey(spec, csv_path=csv_path, config_root=args.config_root, runs_dir=args.runs_dir)
    results = outcome.results
    if csv_path.is_relative_to(REPO_ROOT):  # the report names the file as a checkout spells it
        results["csv"] = csv_path.relative_to(REPO_ROOT).as_posix()
    results_file = outcome.directory / "journey.results.json"
    results_file.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"results  {results_file}")
    if args.runs_dir.resolve() == RUNS_DIR.resolve():
        report_path = library / spec.dataset / "run_report.md"
        report_path.write_text(
            journey_report_text(json.loads(results_file.read_text(encoding="utf-8"))),
            encoding="utf-8",
        )
        print(f"report   {report_path}")
    return 0


def audit_main(argv: list[str]) -> int:
    """`python -m library.run_engine audit --dataset <slug>`: the audit readout on the original campaign.

    Runs `library.audit.run_audit` on the dataset's prepared file, writes its results to
    `library/.runs/<dataset>/audit/audit.results.json` and, when the dataset's journey results are there too,
    renders `library/<dataset>/run_report.md` from both. The audit section is produced here and nowhere else.
    """
    from library.audit import AUDITS, run_audit

    parser = argparse.ArgumentParser(
        prog="python -m library.run_engine audit", description=audit_main.__doc__
    )
    parser.add_argument("--dataset", required=True, choices=sorted(AUDITS), help="library/<dataset> slug")
    parser.add_argument("--csv", type=Path, default=None, help="default: library/<dataset>/data/prepared.csv")
    parser.add_argument("--config-root", type=Path, default=REPO_ROOT / "configs")
    parser.add_argument(
        "--runs-dir", type=Path, default=RUNS_DIR, help="default: library/.runs; any other writes no report"
    )
    parser.add_argument(
        "--journey-results",
        type=Path,
        default=None,
        help="the journey results the report's first sections are rendered from; default: the committed "
        "library/<dataset>/journey.results.json, else library/.runs/<dataset>/journey/journey.results.json",
    )
    args = parser.parse_args(argv)
    spec = AUDITS[args.dataset]
    library = Path(__file__).resolve().parent
    csv_path = (args.csv or library / spec.dataset / "data" / "prepared.csv").resolve()
    if not csv_path.is_file():
        print(f"{csv_path} is missing: run python library/{spec.dataset}/fetch.py first", file=sys.stderr)
        return 2
    outcome = run_audit(spec, csv_path=csv_path, config_root=args.config_root, runs_dir=args.runs_dir)
    results = outcome.results
    if csv_path.is_relative_to(REPO_ROOT):  # the report names the file as a checkout spells it
        results["csv"] = csv_path.relative_to(REPO_ROOT).as_posix()
    results_file = outcome.directory / AUDIT_RESULTS_NAME
    results_file.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"results  {results_file}")
    if args.runs_dir.resolve() == RUNS_DIR.resolve():
        committed = committed_results_path(spec.dataset, AUDIT_RESULTS_NAME)
        committed.write_text(results_file.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"results  {committed}")
        journey = args.journey_results or committed_results_path(spec.dataset, "journey.results.json")
        if not journey.is_file():
            journey = RUNS_DIR / spec.dataset / "journey" / "journey.results.json"
        if not journey.is_file():
            print(
                f"no journey results yet: run python -m library.run_engine journey --dataset {spec.dataset} "
                "to write run_report.md with this audit",
                file=sys.stderr,
            )
            return 0
        report_path = library / spec.dataset / "run_report.md"
        report_path.write_text(
            journey_report_text(json.loads(journey.read_text(encoding="utf-8"))), encoding="utf-8"
        )
        print(f"report   {report_path}  (journey results: {journey})")
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments[:1] == ["journey"]:
        return journey_main(arguments[1:])
    if arguments[:1] == ["audit"]:
        return audit_main(arguments[1:])
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
        help=(
            "config root to load the use case from; defaults to the repo's configs/, where every "
            "library use case ships (DEC-085)"
        ),
    )
    parser.add_argument(
        "--override",
        action="append",
        default=[],
        metavar="PATH=VALUE",
        help="run override, repeatable; the value is read as JSON when it parses as JSON",
    )
    args = parser.parse_args(arguments)

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
