"""Outcome ingestion: real-world performance of a past scoring run, once its outcome window matured (M49).

A scoring run predicts something that has not happened yet - who will churn in the next 60 days, who
will convert in 30. Once that window has passed, the client knows what really happened, and this
module turns that knowledge into two artefacts under the run:

* **`outcome_report.json`** (`OutcomeReport`) - the model's metric measured on the real outcomes,
  beside the test score it was chosen on, and whether the relative drop exceeds
  `monitoring.performance_alert_drop_pct` (a `performance_drop` alert when it does). This is what
  Phase 5's Monitor reads; its schema is versioned and only ever extended.
* **`incrementality_input.json`** (`IncrementalityInput`) - for a run that held out a control group
  (`actions.control_group_fraction`, marked `control_group = True` in `scores.parquet` by the actions
  stage), the outcome rate of the treated rows and of the control rows, overall and per band. This is
  the interface Plan B's incrementality report consumes; Plan B is not in this repository, so the
  pydantic model below *is* the contract, and `schema_version` is how a consumer detects a change.

**The window (DEC-771).** The horizon is the label's `horizon_days`: the recipe's own label when the
run scored a built dataset (that is the definition its scores predict), else the use case's
`label`. It runs from the scoring snapshot when the dataset records one (a periodic build's last
snapshot date, the moment its features describe), otherwise from when the run finished scoring - a
single-snapshot build stands at the end of its data and records no date, and an uploaded file has
none - which is also when the actions it produced could first be taken. Before
`anchor + horizon_days` the outcomes are incomplete by construction, and ingesting them would report
a performance drop that is only the window still being open: refused with
`OUTCOME_WINDOW_NOT_MATURED`, naming the date. With no horizon anywhere, `OUTCOME_WINDOW_UNKNOWN`.

**The same metric, measured honestly (DEC-772).** The metric is the model version's own (`metric` on
`ModelVersion`), computed by `engine.stages.evaluate`'s own functions - the ones that produced
`test_score` - at the decision threshold the evaluation chose. Which rows it is measured on matters
more than it looks: the treated rows were *acted on* (offered a retention deal, sent a campaign),
so a model that correctly ranked the customers most likely to churn, followed by a campaign that
saved them, scores *worse* on those rows - the campaign's success reads as a model failure. The
control group is exactly the untreated sample, so the metric is measured there when it holds at
least `MIN_CONTROL_ROWS_FOR_METRIC` matched rows (and both outcomes, for a classifier); otherwise on
every matched row, and `metric_basis` says which.

**No values leave this module.** The uploaded file is read in memory and never stored - it is a
list of customer ids with outcomes, which would be one more store for retention and erasure to
reach - and nothing written is row-level: counts, rates and metric values only. Logging is counts.
"""

from __future__ import annotations

import io
import math
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from engine.clients import ClientStoreError
from engine.config import Metric, ProblemType, ResolvedConfig, RunMode, UseCaseConfig, get_catalog
from engine.contracts import EvaluationReport, FeatureSchema, ModelVersion, RunRecord, RunState
from engine.onboarding.datasets import DATASET_MANIFEST_FILENAME, dataset_key
from engine.onboarding.specs import DatasetManifest
from engine.registry import ModelRegistry, RegistryError, to_utc
from engine.runs import RUN_CONFIG_FILENAME, RUN_FILENAME
from engine.scheduling.alerts import AlertKind, AlertSink, new_alert
from engine.stages.actions import BAND_COLUMN, CONTROL_GROUP_COLUMN, SUPPRESSED_REASON_COLUMN
from engine.storage import Storage, StorageError, run_key
from engine.utils.logging import get_logger
from engine.utils.time import utc_now

if TYPE_CHECKING:
    from pathlib import Path

    import pandas as pd

    from engine.clients import ClientStore

__all__ = [
    "INCREMENTALITY_INPUT_FILENAME",
    "MIN_CONTROL_ROWS_FOR_METRIC",
    "OUTCOME_ERROR_STATUS",
    "OUTCOME_REPORT_FILENAME",
    "BandOutcome",
    "GroupOutcome",
    "IncrementalityInput",
    "OutcomeError",
    "OutcomeReport",
    "OutcomeWindow",
    "ingest_outcomes",
    "outcome_window",
    "read_outcome_report",
]

_LOGGER = get_logger(__name__)

OUTCOME_REPORT_FILENAME: Final[str] = "outcome_report.json"
INCREMENTALITY_INPUT_FILENAME: Final[str] = "incrementality_input.json"
EVALUATION_FILENAME: Final[str] = "evaluation.json"
SCORES_FILENAME: Final[str] = "scores.parquet"

MIN_CONTROL_ROWS_FOR_METRIC: Final[int] = 50
"""Fewer matched control rows than this and the metric is measured on every matched row instead: a
rank metric on a few dozen rows moves by more than any alert threshold on noise alone."""

_POSITIVE: Final[frozenset[str]] = frozenset({"1", "1.0", "true", "yes", "y", "t"})
_NEGATIVE: Final[frozenset[str]] = frozenset({"0", "0.0", "false", "no", "n", "f"})

OUTCOME_ERROR_STATUS: Final[dict[str, int]] = {
    "OUTCOME_RUN_NOT_FOUND": 404,
    "OUTCOME_MODEL_NOT_FOUND": 404,
    "OUTCOME_REPORT_NOT_FOUND": 404,
    "OUTCOME_RUN_NOT_SCORED": 409,
    "OUTCOME_SCORES_MISSING": 409,
    "OUTCOME_WINDOW_NOT_MATURED": 409,
    "OUTCOME_WINDOW_UNKNOWN": 422,
    "OUTCOME_FILE_UNREADABLE": 422,
    "OUTCOME_COLUMN_MISSING": 422,
    "OUTCOME_DUPLICATE_KEYS": 422,
    "OUTCOME_VALUES_INVALID": 422,
    "OUTCOME_NO_MATCH": 422,
    "OUTCOME_PROBLEM_TYPE_UNSUPPORTED": 422,
}
"""`OutcomeError.code` -> the HTTP status the API answers with."""


class OutcomeError(Exception):
    """Outcomes could not be ingested. `code` is a key of `OUTCOME_ERROR_STATUS`; `message` says what to do."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# The contracts
# ---------------------------------------------------------------------------
class OutcomeWindow(BaseModel):
    """When a scoring run's outcomes are complete."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    horizon_days: int = Field(description="Days after the anchor the predicted outcome is observed over.")
    horizon_source: Literal["recipe_label", "use_case_label"] = Field(
        description="Which label definition the horizon was read from."
    )
    anchor: datetime = Field(description="UTC start of the window.")
    anchor_source: Literal["snapshot_date", "scored_at"] = Field(
        description="The dataset's scoring snapshot, or when the run finished for an uploaded file."
    )
    matures_at: datetime = Field(description="UTC time the window closes; outcomes are accepted from then.")


class GroupOutcome(BaseModel):
    """What happened to one group of scored rows. Aggregates only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rows: int = Field(description="Matched rows in the group.")
    positives: int | None = Field(
        description="Rows whose outcome happened (binary outcomes); null otherwise."
    )
    outcome_rate: float | None = Field(
        description="Share of rows whose outcome happened, 0 to 1 (binary); null when the group is empty."
    )
    outcome_mean: float | None = Field(
        description="Mean outcome value (continuous outcomes); null for binary outcomes or an empty group."
    )


class BandOutcome(BaseModel):
    """Treated and control outcomes within one score band."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    band: str = Field(description="Band name from actions.bands.")
    treated: GroupOutcome = Field(description="Rows of this band that were acted on.")
    control: GroupOutcome = Field(description="Rows of this band held out as control.")


class IncrementalityInput(BaseModel):
    """`incrementality_input.json` - what Plan B's incrementality report consumes (schema version 1).

    Treated rows are the matched rows that were eligible and *not* held out; suppressed rows (opted
    out, recently contacted, no consent) were never eligible for either group and are excluded and
    counted. `observed_difference` is the treated rate (or mean) minus the control one: a descriptive
    difference with no interval, which is Plan B's to compute.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Bumped only on a breaking change.")
    run_id: str = Field(description="Scoring run whose actions were taken.")
    use_case_id: str = Field(description="Use case of the run.")
    client_id: str | None = Field(description="Client of the run, if any.")
    model_version_id: str = Field(description="Model version that produced the scores.")
    outcome_name: str = Field(description="Name of the outcome column that was ingested.")
    outcome_kind: Literal["binary", "continuous"] = Field(description="How the outcome is measured.")
    window: OutcomeWindow = Field(description="The outcome window the outcomes were observed over.")
    control_group_fraction: float = Field(description="actions.control_group_fraction of the run.")
    treated: GroupOutcome = Field(description="All matched treated rows.")
    control: GroupOutcome = Field(description="All matched control rows.")
    by_band: tuple[BandOutcome, ...] = Field(description="The same split within each band, in band order.")
    observed_difference: float | None = Field(
        description="Treated minus control outcome rate (binary) or mean (continuous); null if a group is empty."
    )
    suppressed_rows_excluded: int = Field(description="Matched rows excluded because they were suppressed.")
    unmatched_scored_rows: int = Field(description="Scored rows the outcomes file had no row for.")
    created_at: datetime = Field(description="UTC time this input was written.")


class OutcomeReport(BaseModel):
    """`outcome_report.json` - real-world performance of one scoring run (schema version 1).

    Read by the API's monitoring routes and by Phase 5's Monitor; fields are only ever added.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Bumped only on a breaking change.")
    run_id: str = Field(description="Scoring run the outcomes belong to.")
    use_case_id: str = Field(description="Use case of the run.")
    client_id: str | None = Field(description="Client of the run, if any.")
    model_version_id: str = Field(description="Model version that produced the scores.")
    problem_type: ProblemType = Field(description="binary_classification or regression.")
    outcome_name: str = Field(description="Name of the outcome column that was ingested.")
    metric: Metric = Field(description="The model version's own metric.")
    metric_label: str = Field(description="Catalog label of the metric.")
    greater_is_better: bool = Field(description="Direction of the metric.")
    test_score: float = Field(description="The metric on the model's test split, when it was chosen.")
    real_world_score: float | None = Field(
        description="The metric on the real outcomes; null when it is undefined on these rows."
    )
    metric_basis: Literal["control_group", "all_matched"] = Field(
        description="Which rows the real-world metric was measured on (see DEC-772)."
    )
    metric_undefined_reason: str | None = Field(
        description="Why real_world_score is null, e.g. ONE_OUTCOME_CLASS; null when it was measured."
    )
    decision_threshold: float | None = Field(
        description="Score cut-off the threshold metrics used (classification); null for regression."
    )
    relative_drop_pct: float | None = Field(
        description="How much worse than test_score, in percent of it; negative when better. Null if unmeasured."
    )
    alert_threshold_pct: int = Field(description="monitoring.performance_alert_drop_pct of the run.")
    alert_raised: bool = Field(description="Whether a performance_drop alert was raised.")
    alert_id: str | None = Field(description="That alert's id.")
    window: OutcomeWindow = Field(description="The outcome window.")
    rows_scored: int = Field(description="Rows in the run's scores.")
    rows_in_file: int = Field(description="Rows in the uploaded outcomes file.")
    rows_matched: int = Field(description="Scored rows the file had an outcome for.")
    rows_unmatched_in_file: int = Field(description="File rows whose key the run never scored.")
    rows_evaluated: int = Field(description="Rows the real-world metric was computed on.")
    control_rows_matched: int = Field(description="Matched rows that were held out as control.")
    incrementality_key: str | None = Field(
        description="Storage key of incrementality_input.json; null when the run had no control group."
    )
    computed_at: datetime = Field(description="UTC time the report was computed.")


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------
def _dataset_manifest(storage: Storage, run: RunRecord) -> DatasetManifest | None:
    if run.dataset_id is None:
        return None
    try:
        return storage.read_model(dataset_key(run.dataset_id, DATASET_MANIFEST_FILENAME), DatasetManifest)
    except (StorageError, ValueError):
        return None


def outcome_window(
    run: RunRecord,
    *,
    storage: Storage,
    config: UseCaseConfig,
    client_store: ClientStore | None = None,
) -> OutcomeWindow:
    """The run's outcome window (DEC-771), or `OutcomeError(OUTCOME_WINDOW_UNKNOWN)`."""
    manifest = _dataset_manifest(storage, run)
    horizon: int | None = None
    source: Literal["recipe_label", "use_case_label"] = "use_case_label"
    if manifest is not None and client_store is not None:
        try:
            label = client_store.get_spec(manifest.spec_id).label_spec
        except ClientStoreError:  # a recipe deleted since: fall back to the use case's own label
            label = None
        if label is not None and label.horizon_days is not None:
            horizon, source = label.horizon_days, "recipe_label"
    if horizon is None and config.label is not None and config.label.horizon_days is not None:
        horizon = config.label.horizon_days
    if horizon is None:
        raise OutcomeError(
            "OUTCOME_WINDOW_UNKNOWN",
            f"{config.name} defines no outcome horizon, so there is no way to tell when this run's "
            "outcomes are complete. Add a label with horizon_days to the use case.",
        )
    anchor_source: Literal["snapshot_date", "scored_at"]
    if manifest is not None and manifest.snapshot_dates:
        anchor = datetime.combine(manifest.snapshot_dates[-1], time(0, 0), tzinfo=UTC)
        anchor_source = "snapshot_date"
    else:
        anchor = to_utc(run.finished_at or run.created_at)
        anchor_source = "scored_at"
    return OutcomeWindow(
        horizon_days=horizon,
        horizon_source=source,
        anchor=anchor,
        anchor_source=anchor_source,
        matures_at=anchor + timedelta(days=horizon),
    )


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------
def _read_file(payload: bytes, file_format: Literal["csv", "parquet"], key: str) -> pd.DataFrame:
    import pandas as pd

    try:
        if file_format == "parquet":
            return pd.read_parquet(io.BytesIO(payload))
        return pd.read_csv(io.BytesIO(payload), dtype={key: str})
    except Exception as exc:  # whatever pandas raised about the bytes; never quoted, it may hold a value
        raise OutcomeError(
            "OUTCOME_FILE_UNREADABLE",
            f"The outcomes file could not be read as {file_format}. Export it again and upload it.",
        ) from exc


def _binary(values: pd.Series) -> pd.Series:
    """True/False per row from 1/0, true/false, yes/no; `OUTCOME_VALUES_INVALID` for anything else."""
    text = values.astype("string").str.strip().str.lower()
    positive = text.isin(_POSITIVE)
    negative = text.isin(_NEGATIVE)
    invalid = int((~(positive | negative)).sum())
    if invalid:
        raise OutcomeError(
            "OUTCOME_VALUES_INVALID",
            f"{invalid} row(s) have an outcome that is not yes/no (1/0, true/false). Fix them and upload again.",
        )
    return positive.astype(bool)


def _continuous(values: pd.Series) -> pd.Series:
    import pandas as pd

    numbers = pd.to_numeric(values, errors="coerce")
    invalid = int(numbers.isna().sum())
    if invalid:
        raise OutcomeError(
            "OUTCOME_VALUES_INVALID",
            f"{invalid} row(s) have an outcome that is not a number. Fix them and upload again.",
        )
    return numbers.astype(float)


def _group(actual: pd.Series, *, binary: bool) -> GroupOutcome:
    rows = len(actual.index)
    if binary:
        positives = int(actual.sum())
        return GroupOutcome(
            rows=rows,
            positives=positives,
            outcome_rate=None if rows == 0 else round(positives / rows, 6),
            outcome_mean=None,
        )
    return GroupOutcome(
        rows=rows,
        positives=None,
        outcome_rate=None,
        outcome_mean=None if rows == 0 else round(float(actual.mean()), 6),
    )


def _level(group: GroupOutcome) -> float | None:
    return group.outcome_rate if group.outcome_mean is None else group.outcome_mean


def _threshold(storage: Storage, version: ModelVersion) -> float:
    key = version.artefact_keys.get(EVALUATION_FILENAME, run_key(version.run_id, EVALUATION_FILENAME))
    try:
        return float(storage.read_model(key, EvaluationReport).threshold)
    except (StorageError, ValueError):
        return 0.5


def _target_name(storage: Storage, version: ModelVersion) -> str | None:
    """The outcome the model was trained to predict: its saved schema's target, else its run's."""
    try:
        target = storage.read_model(version.schema_key, FeatureSchema).target
    except (StorageError, ValueError):
        target = None
    if target:
        return target
    try:
        return storage.read_model(run_key(version.run_id, RUN_FILENAME), RunRecord).target
    except (StorageError, ValueError):
        return None


def _metric(
    version: ModelVersion,
    actual: pd.Series,
    scores: pd.Series,
    *,
    binary: bool,
    threshold: float | None,
) -> tuple[float | None, str | None]:
    """The version's metric on these rows, by `engine.stages.evaluate`'s own functions."""
    import numpy as np

    from engine.stages.evaluate import _classification_metrics, _regression_metrics

    if len(actual.index) == 0:
        return None, "NO_ROWS"
    predictions = scores.to_numpy(dtype=float)
    one_class = False
    if binary:
        observed = actual.to_numpy(dtype=bool)
        one_class = len(np.unique(observed)) < 2
        values = _classification_metrics(observed, predictions, predictions >= float(threshold or 0.5))
    else:
        values = _regression_metrics(actual.to_numpy(dtype=float), predictions)
    measured = values.get(version.metric)
    if measured is None or not math.isfinite(measured):
        return None, "ONE_OUTCOME_CLASS" if one_class else "METRIC_UNDEFINED"
    return float(measured), None


def ingest_outcomes(
    run_id: str,
    payload: bytes,
    *,
    file_format: Literal["csv", "parquet"],
    storage: Storage,
    registry: ModelRegistry,
    alerts: AlertSink,
    outcome_column: str | None = None,
    client_store: ClientStore | None = None,
    config_root: Path | None = None,
    now: datetime | None = None,
) -> OutcomeReport:
    """Join an uploaded outcomes file to a past scoring run and write its outcome artefacts.

    The file holds the run's primary key and the actual outcome (`outcome_column`, by default the
    name of the target the model was trained on). Returns the `OutcomeReport` it wrote; raises
    `OutcomeError` for everything a person must fix, with the code the API maps to a status.
    """
    import pandas as pd

    moment = now or utc_now()
    try:
        run = storage.read_model(run_key(run_id, RUN_FILENAME), RunRecord)
    except StorageError as exc:
        raise OutcomeError("OUTCOME_RUN_NOT_FOUND", f"No run with id {run_id!r}.") from exc
    if run.mode is not RunMode.SCORE or run.state is not RunState.DONE:
        raise OutcomeError(
            "OUTCOME_RUN_NOT_SCORED", "Outcomes can only be added to a scoring run that finished."
        )
    config = storage.read_model(run_key(run_id, RUN_CONFIG_FILENAME), ResolvedConfig).config
    if config.problem_type not in (ProblemType.BINARY_CLASSIFICATION, ProblemType.REGRESSION):
        raise OutcomeError(
            "OUTCOME_PROBLEM_TYPE_UNSUPPORTED",
            f"Outcomes can be measured for classification and regression, not {config.problem_type.value}.",
        )
    binary = config.problem_type is ProblemType.BINARY_CLASSIFICATION
    window = outcome_window(run, storage=storage, config=config, client_store=client_store)
    if moment < window.matures_at:
        raise OutcomeError(
            "OUTCOME_WINDOW_NOT_MATURED",
            f"This run's outcomes are not complete until {window.matures_at.date().isoformat()} "
            f"({window.horizon_days} days after {window.anchor.date().isoformat()}). Add them after that date.",
        )
    if run.model_version_id is None:
        raise OutcomeError("OUTCOME_MODEL_NOT_FOUND", "This run does not record the model that scored it.")
    try:
        version = registry.get(run.model_version_id)
    except RegistryError as exc:
        raise OutcomeError(
            "OUTCOME_MODEL_NOT_FOUND", f"Model {run.model_version_id!r} is not registered."
        ) from exc

    key = str(run.primary_key if isinstance(run.primary_key, str) else run.primary_key[0])
    outcome_name = outcome_column or _target_name(storage, version) or "outcome"
    frame = _read_file(payload, file_format, key)
    missing = [name for name in (key, outcome_name) if name not in frame.columns]
    if missing:
        raise OutcomeError(
            "OUTCOME_COLUMN_MISSING",
            f"The outcomes file needs the columns {key!r} and {outcome_name!r}; it has no {missing[0]!r}.",
        )
    outcomes = frame[[key, outcome_name]].copy()
    outcomes[key] = outcomes[key].astype("string")
    duplicates = int(outcomes[key].duplicated().sum())
    if duplicates:
        raise OutcomeError(
            "OUTCOME_DUPLICATE_KEYS",
            f"{duplicates} {key} value(s) appear more than once. Keep one outcome per row and upload again.",
        )
    outcomes["__actual"] = _binary(outcomes[outcome_name]) if binary else _continuous(outcomes[outcome_name])

    try:
        scores = pd.read_parquet(io.BytesIO(storage.read_bytes(run_key(run_id, SCORES_FILENAME))))
    except StorageError as exc:
        raise OutcomeError("OUTCOME_SCORES_MISSING", "This run's scores are no longer stored.") from exc
    score_field = config.actions.score_field
    scores = scores[[key, score_field, BAND_COLUMN, SUPPRESSED_REASON_COLUMN, CONTROL_GROUP_COLUMN]].copy()
    scores[key] = scores[key].astype("string")
    matched = scores.merge(outcomes[[key, "__actual"]], on=key, how="inner")
    if matched.empty:
        raise OutcomeError(
            "OUTCOME_NO_MATCH",
            f"None of the file's {key} values were scored by this run. Check it is the right file.",
        )
    control = matched[CONTROL_GROUP_COLUMN].astype(bool)
    suppressed = matched[SUPPRESSED_REASON_COLUMN].notna()

    control_rows = matched[control]
    use_control = len(control_rows.index) >= MIN_CONTROL_ROWS_FOR_METRIC and (
        not binary or control_rows["__actual"].nunique() == 2
    )
    evaluated = control_rows if use_control else matched
    threshold = _threshold(storage, version) if binary else None
    real, undefined = _metric(
        version, evaluated["__actual"], evaluated[score_field], binary=binary, threshold=threshold
    )
    catalog = get_catalog(config_root)
    greater = catalog.metrics[version.metric].greater_is_better
    drop: float | None = None
    if real is not None and version.test_score != 0:
        change = (version.test_score - real) if greater else (real - version.test_score)
        drop = round(100.0 * change / abs(version.test_score), 4)
    limit = config.monitoring.performance_alert_drop_pct
    alert_id: str | None = None
    if drop is not None and drop > limit:
        raised = alerts.raise_alert(
            new_alert(
                AlertKind.PERFORMANCE_DROP,
                use_case_id=run.use_case_id,
                client_id=run.client_id,
                run_id=run_id,
                model_id=version.model_id,
                message=(
                    f"Measured on real outcomes, {config.name}'s model scored {drop:.1f}% worse on "
                    f"{catalog.metric_label(version.metric)} than on its test data (the alert level is "
                    f"{limit}%). Consider retraining it on recent data."
                ),
                now=moment,
            )
        )
        alert_id = raised.alert_id

    incrementality_key: str | None = None
    if bool(scores[CONTROL_GROUP_COLUMN].astype(bool).any()):
        incrementality_key = run_key(run_id, INCREMENTALITY_INPUT_FILENAME)
        storage.write_model(
            incrementality_key,
            _incrementality(
                run,
                version,
                config,
                window=window,
                matched=matched,
                control=control,
                suppressed=suppressed,
                binary=binary,
                outcome_name=outcome_name,
                unmatched=len(scores.index) - len(matched.index),
                now=moment,
            ),
        )

    report = OutcomeReport(
        run_id=run_id,
        use_case_id=run.use_case_id,
        client_id=run.client_id,
        model_version_id=version.model_id,
        problem_type=config.problem_type,
        outcome_name=outcome_name,
        metric=version.metric,
        metric_label=catalog.metric_label(version.metric),
        greater_is_better=greater,
        test_score=version.test_score,
        real_world_score=None if real is None else round(real, 6),
        metric_basis="control_group" if use_control else "all_matched",
        metric_undefined_reason=undefined,
        decision_threshold=threshold,
        relative_drop_pct=drop,
        alert_threshold_pct=limit,
        alert_raised=alert_id is not None,
        alert_id=alert_id,
        window=window,
        rows_scored=len(scores.index),
        rows_in_file=len(outcomes.index),
        rows_matched=len(matched.index),
        rows_unmatched_in_file=len(outcomes.index) - len(matched.index),
        rows_evaluated=len(evaluated.index),
        control_rows_matched=len(control_rows.index),
        incrementality_key=incrementality_key,
        computed_at=moment,
    )
    storage.write_model(run_key(run_id, OUTCOME_REPORT_FILENAME), report)
    _LOGGER.info(
        "outcomes.ingested run_id=%s rows_in_file=%d rows_matched=%d rows_evaluated=%d basis=%s alert=%s",
        run_id,
        report.rows_in_file,
        report.rows_matched,
        report.rows_evaluated,
        report.metric_basis,
        report.alert_raised,
    )
    return report


def _incrementality(
    run: RunRecord,
    version: ModelVersion,
    config: UseCaseConfig,
    *,
    window: OutcomeWindow,
    matched: pd.DataFrame,
    control: pd.Series,
    suppressed: pd.Series,
    binary: bool,
    outcome_name: str,
    unmatched: int,
    now: datetime,
) -> IncrementalityInput:
    treated_mask = ~control & ~suppressed
    control_mask = control & ~suppressed
    treated = _group(matched.loc[treated_mask, "__actual"], binary=binary)
    held_out = _group(matched.loc[control_mask, "__actual"], binary=binary)
    bands: list[BandOutcome] = []
    for band in config.actions.bands:
        in_band = matched[BAND_COLUMN].astype("string") == band.name
        bands.append(
            BandOutcome(
                band=band.name,
                treated=_group(matched.loc[in_band & treated_mask, "__actual"], binary=binary),
                control=_group(matched.loc[in_band & control_mask, "__actual"], binary=binary),
            )
        )
    treated_level, control_level = _level(treated), _level(held_out)
    difference = (
        None if treated_level is None or control_level is None else round(treated_level - control_level, 6)
    )
    return IncrementalityInput(
        run_id=run.run_id,
        use_case_id=run.use_case_id,
        client_id=run.client_id,
        model_version_id=version.model_id,
        outcome_name=outcome_name,
        outcome_kind="binary" if binary else "continuous",
        window=window,
        control_group_fraction=config.actions.control_group_fraction,
        treated=treated,
        control=held_out,
        by_band=tuple(bands),
        observed_difference=difference,
        suppressed_rows_excluded=int(suppressed.sum()),
        unmatched_scored_rows=unmatched,
        created_at=now,
    )


def read_outcome_report(storage: Storage, run_id: str) -> OutcomeReport:
    """A run's stored `OutcomeReport`, or `OutcomeError(OUTCOME_REPORT_NOT_FOUND)`."""
    try:
        return storage.read_model(run_key(run_id, OUTCOME_REPORT_FILENAME), OutcomeReport)
    except StorageError as exc:
        raise OutcomeError(
            "OUTCOME_REPORT_NOT_FOUND", "No outcomes have been added to this run yet."
        ) from exc
