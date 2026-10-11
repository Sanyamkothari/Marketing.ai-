"""Things we checked: the checks a training run already ran, read back from its artefacts (DEC-1599).

A finished training run has already done a good deal of checking: the file was checked before training
(`validation.json`), a column that gives the answer away was looked for (LEAKAGE_SUSPECTED, in
`validation.json`, when `run_config.json` says the check was on), a test set was kept apart (`split.json`),
the model was compared with a simple yardstick (`baseline.json`), its results were split by a sensitive
column when one was set (`fairness.json`), and - new with this module - its score was held against a
"too good to be true" line (`RESULT_SUSPICIOUSLY_GOOD`, `engine.model_gates.suspiciously_good`).

`things_checked` turns those artefacts into a short list for the Model page. Every item is read from an
artefact the run wrote; an artefact that is absent leaves its item out, and a check the run's own
settings switched off is listed as **not run** with the reason, never as passed. Nothing is recomputed
except the alarm's comparison of a stored score with a configured line. Read-only.

Not here yet: the dataset build's own checks (the future-data probe and `LABEL_RATE_UNSTABLE`) live in
the dataset's build report and the Readiness report, not in the run's artefacts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from engine.config import ProblemType, ResolvedConfig, RunMode
from engine.contracts import (
    BaselineComparison,
    EvaluationReport,
    FairnessReport,
    FeatureImportance,
    RunRecord,
    SplitReport,
    ValidationReport,
)
from engine.model_gates import RESULT_SUSPICIOUSLY_GOOD, load_suspicion_config, suspiciously_good
from engine.storage import Storage, StorageError, run_key

__all__ = ["CheckStatus", "CheckedItem", "RunChecks", "things_checked"]

CheckStatus = Literal["passed", "warning", "info", "not_run"]

_M = TypeVar("_M", bound=BaseModel)

_LEAKAGE: Final[str] = "LEAKAGE_SUSPECTED"


class CheckedItem(BaseModel):
    """One line of "Things we checked"."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Literal["too_good", "before_training", "leak_columns", "split", "baseline", "fairness"] = Field(
        description="Which check this line is about."
    )
    title: str = Field(description="What was checked, in words.")
    status: CheckStatus = Field(
        description="passed; warning (worth a look, blocks nothing); info (measured, no pass line); not_run."
    )
    message: str = Field(description="The plain sentence the page shows, numbers filled in.")
    code: str | None = Field(default=None, description="The warning's code, e.g. RESULT_SUSPICIOUSLY_GOOD.")
    source: str = Field(description="The run artefact the line is read from.")


class RunChecks(BaseModel):
    """`GET /runs/{run_id}/checks`: what a training run checked, from its own artefacts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(description="The run the checks belong to.")
    checks: tuple[CheckedItem, ...] = Field(description="The lines, the too-good alarm first.")
    warnings: int = Field(description="How many lines are warnings.")
    note: str | None = Field(default=None, description="Why the list is empty, when it is.")


def _read(storage: Storage, run_id: str, name: str, model: type[_M]) -> _M | None:
    key = run_key(run_id, name)
    try:
        return storage.read_model(key, model) if storage.exists(key) else None
    except (StorageError, ValueError, OSError):
        return None


def _plural(count: int, word: str) -> str:
    return f"{count:,} {word}" if count == 1 else f"{count:,} {word}s"


def _too_good(
    evaluation: EvaluationReport, importance: FeatureImportance | None, use_case_id: str, root: Path | None
) -> CheckedItem:
    alarm = suspiciously_good(evaluation, importance, load_suspicion_config(root).thresholds_for(use_case_id))
    title = "Not too good to be true"
    if alarm is None:
        return CheckedItem(
            id="too_good",
            title=title,
            status="not_run",
            message="Not judged: no 'too good to be true' line is set for this kind of model.",
            source="evaluation.json",
        )
    return CheckedItem(
        id="too_good",
        title=title,
        status="passed" if alarm.passed else "warning",
        message=alarm.message,
        code=None if alarm.passed else RESULT_SUSPICIOUSLY_GOOD,
        source="evaluation.json",
    )


def _before_training(validation: ValidationReport) -> CheckedItem:
    kept = sum(1 for check in validation.checks if check.acknowledged)
    if validation.warning_count == 0 and kept == 0:
        message = "The file was checked before training and nothing was reported."
    else:
        message = (
            f"The file was checked before training: {_plural(validation.warning_count, 'warning')}"
            + (f", and {_plural(kept, 'finding')} you looked at and let through" if kept else "")
            + ". The Data page lists them."
        )
    return CheckedItem(
        id="before_training",
        title="The file, before training",
        status="passed" if validation.warning_count == 0 and kept == 0 else "info",
        message=message,
        source="validation.json",
    )


def _leak_columns(validation: ValidationReport, config: ResolvedConfig | None) -> CheckedItem | None:
    """LEAKAGE_SUSPECTED's verdict; None when the run's settings cannot say whether it ran."""
    title = "A column that gives the answer away"
    if config is None:
        return None
    if not config.config.validation.leakage_check:
        return CheckedItem(
            id="leak_columns",
            title=title,
            status="not_run",
            message="Not run: the check was switched off in this run's settings.",
            source="run_config.json",
        )
    if config.config.problem_type is not ProblemType.BINARY_CLASSIFICATION:
        return CheckedItem(
            id="leak_columns",
            title=title,
            status="not_run",
            message="Not run: this check looks only at yes-or-no outcomes.",
            source="run_config.json",
        )
    found = [check for check in validation.checks if check.code == _LEAKAGE]
    if not found:
        return CheckedItem(
            id="leak_columns",
            title=title,
            status="passed",
            message="Before training, no column was found that predicts the outcome almost on its own.",
            source="validation.json",
        )
    named = sorted({f"'{check.column}'" for check in found if check.column})
    together = any(not check.column for check in found)
    parts = [*named, "the columns taken together"] if together else named
    what = ", ".join(parts)
    return CheckedItem(
        id="leak_columns",
        title=title,
        status="warning",
        message=(
            f"Before training, these looked as if they might give the answer away: {what}. Training went "
            "ahead; make sure each is known before the outcome."
        ),
        code=_LEAKAGE,
        source="validation.json",
    )


def _split(split: SplitReport) -> CheckedItem:
    test = next((part for part in split.parts if part.name == "test"), None)
    rows = f"{test.rows:,} rows" if test is not None else "a share of the rows"
    if split.type.value == "time_based":
        how = "the most recent rows, after the ones it learned from"
    elif split.group_column:
        how = f"rows whose {split.group_column} it never saw while learning"
    else:
        how = "rows chosen at random and kept apart"
    return CheckedItem(
        id="split",
        title="Measured on rows it never learned from",
        status="passed",
        message=f"Every score on this page was measured on {rows}: {how}.",
        source="split.json",
    )


def _baseline(baseline: BaselineComparison) -> CheckedItem:
    if baseline.model_beats_baseline:
        message = f"It beats a simple yardstick ({baseline.baseline_name}) on its main measure."
    else:
        message = (
            f"It does not beat a simple yardstick ({baseline.baseline_name}) on its main measure: "
            "the simpler model may do as well."
        )
    return CheckedItem(
        id="baseline",
        title="Better than a simple yardstick",
        status="passed" if baseline.model_beats_baseline else "warning",
        message=message,
        source="baseline.json",
    )


def _fairness(fairness: FairnessReport) -> CheckedItem:
    if not fairness.evaluated:
        reason = (fairness.reason_not_evaluated or "no column to compare groups by was set").rstrip(".")
        return CheckedItem(
            id="fairness",
            title="Works as well for each group",
            status="not_run",
            message=f"Not run: {reason[:1].lower() + reason[1:]}.",
            source="fairness.json",
        )
    return CheckedItem(
        id="fairness",
        title=f"Works as well for each {fairness.column or 'group'}",
        status="info",
        message=fairness.note,
        source="fairness.json",
    )


def things_checked(storage: Storage, record: RunRecord, root: Path | None = None) -> RunChecks:
    """The checks `record`'s training run ran, from its artefacts; empty with a note for a scoring run."""
    run_id = record.run_id
    if record.mode is not RunMode.TRAIN:
        return RunChecks(
            run_id=run_id,
            checks=(),
            warnings=0,
            note="A scoring run trains nothing: the checks are on its model's training run.",
        )
    items: list[CheckedItem] = []
    evaluation = _read(storage, run_id, "evaluation.json", EvaluationReport)
    if evaluation is not None:
        importance = _read(storage, run_id, "feature_importance.json", FeatureImportance)
        items.append(_too_good(evaluation, importance, record.use_case_id, root))
    validation = _read(storage, run_id, "validation.json", ValidationReport)
    if validation is not None:
        items.append(_before_training(validation))
        leak = _leak_columns(validation, _read(storage, run_id, "run_config.json", ResolvedConfig))
        if leak is not None:
            items.append(leak)
    split = _read(storage, run_id, "split.json", SplitReport)
    if split is not None:
        items.append(_split(split))
    baseline = _read(storage, run_id, "baseline.json", BaselineComparison)
    if baseline is not None:
        items.append(_baseline(baseline))
    fairness = _read(storage, run_id, "fairness.json", FairnessReport)
    if fairness is not None:
        items.append(_fairness(fairness))
    return RunChecks(
        run_id=run_id,
        checks=tuple(items),
        warnings=sum(1 for item in items if item.status == "warning"),
        note=None if items else "This run wrote none of the reports the checks are read from.",
    )
