"""Prepare and split stages (M3): row-level cleaning, a train-only statistical fit, and the split.

Two phases with one seam
------------------------
"Fit all transforms on train only" (plan §6.3, *fit-on-train-only for anything statistical*) cannot
be honoured by a stage that runs before the split and sees every row: a median or a 99th percentile
measured over the whole uploaded file carries the validation and test partitions into the numbers
used for training, and inflates the very hold-out figures the champion decision rests on. Prepare is
therefore two functions with an explicit seam between them, and the split runs in the seam:

* :func:`prepare_rows` - the **row-level phase**. Column exclusions, PII handling, the time-column
  cast, the consent filter, dropping rows with no target, deduplication and (when
  `missing_values: drop_rows`) dropping rows with a missing feature. None of these needs a statistic
  fitted on the data, and every one of them changes *which rows exist*, so all of them must run
  **before** the split: splitting first would put a duplicate in one partition and its twin in
  another, or let a row the customer never consented to decide where a boundary falls.
* :func:`fit_transforms` - the **statistical phase**. Fill values and clip bounds are **fitted only
  on the rows named by `fit_index`** - the training partition - and then applied to every row of
  every partition. These are the transforms recorded in `PrepareReport.transforms`, and
  :func:`replay` still reproduces them from the recorded parameters alone.

`prepare` keeps its old signature and behaviour: the two phases back to back with *every* surviving
row used as a fit row. That is the right answer only when the frame really is all training data (or
when there is no split at all), and it is the wrong answer for a training file, so the train pipeline
calls the two phases instead. Nothing was removed; `engine.pipeline` still imports `prepare`,
`replay` and `split_dataset` under those names.

Why `fit_index` and not two frames
----------------------------------
The seam names *rows*, not frames. `fit_index` is the index of the training rows inside the
row-prepared frame, so a row operation is applied to the whole frame exactly once and a statistic
reads a labelled subset of it - the asymmetry the user asked for, in one parameter. The alternatives
are worse: handing `fit_transforms` a dict of partitions would make it concatenate and re-split them
to fill one column, and letting it call `split_dataset` itself would silently disagree with the split
stage whenever the statistical phase removes rows (`outliers: remove_rows`), because a second split
of a shorter frame is a different split. `fit_index=None` means "every row is a fit row" and is what
the single-frame convenience uses; the number of rows each statistic saw is recorded on the transform
itself (`fit_rows`), so `prepare.json` says what a parameter was fitted on rather than leaving it to
be assumed.

How the pipeline must sequence this
-----------------------------------
Plan §6.1 orders the train flow `ingest -> validate -> prepare -> split -> train`, which is exactly
the order that makes fit-on-train-only impossible. The user's instruction wins and the stage list
stays as the plan writes it: the PREPARE stage is executed in two parts, around SPLIT (DEC-046).

```python
rows, plan = prepare_rows(df, config, primary_key=pk, target=target)          # PREPARE, phase 1
parts, split_report = split_dataset(rows, config, run_id=run_id, target=target)   # SPLIT
prepared, prepare_report = fit_transforms(                                    # PREPARE, phase 2
    rows, config, plan, run_id=run_id, fit_index=parts["train"].index
)
train = prepared.loc[prepared.index.intersection(parts["train"].index)]       # partitions, re-sliced
```

`prepare_rows` resets the index, so the labels the split hands back are positions in the row-prepared
frame; `fit_transforms` keeps those labels (it never reorders and only ever removes rows), so each
partition is recovered by intersecting its index with the fitted frame. `prepare.json` is written
once, when phase 2 finishes, and `split.json` when the split does; the Running screen is unchanged,
because plan §6.1's prepare and split stages already share one group label ("Preparing features").

Score time is untouched: a scoring file is never split, and :func:`replay` re-applies the recorded
parameters without measuring anything.

Order of operations
-------------------
Plan §6.3 lists what `prepare` does but not the order, and the order changes the answer, so it is
fixed here and never varies. Phase 1, before the split:

1. **Drop columns** - user-excluded, then PII (when the setting drops or redacts), then id-like,
   constant and high-null. Columns go first because every later step is fitted on what is left: a
   median, a percentile or a duplicate check computed over a column that is about to be thrown away
   is wasted work, and a row-level rule (`drop_rows`, deduplication) that still sees a doomed column
   removes the wrong rows. Within the step the order is a precedence, not a sequence: a column that
   the PII detectors match is handled by `prepare.pii_handling` before the id-like and constant
   heuristics get a chance to swallow it, because "we dropped your email column because it looked
   like an identifier" hides a governance decision behind a statistical one. Reserved columns (the
   primary key, target, time, group, consent, fairness and suppression columns) are never dropped by
   a heuristic; they are named in the configuration and the engine needs them downstream. These
   decisions read the whole file on purpose: they are *structural*, they put no value into any row,
   and a column has to be present or absent identically in every partition and in `schema.json`.
2. **Redact PII** (when `pii_handling: redact`), so no PII value reaches a statistic fitted below.
3. **Cast the time column** to timezone-aware UTC, so the split has one comparable ordering.
4. **Consent filter**, before anything statistical. A row the customer did not consent to may not
   influence a fill value, a percentile or a duplicate count, and it may not be trained on: filtering
   last would leak it into every fitted parameter.
5. **Drop rows with no target**, which cannot be trained on and must not shift a fitted statistic.
6. **Deduplicate**, *before* filling. Filling first manufactures duplicates - two rows that differed
   only in which value was missing become identical once both are filled with the same median - so
   deduplicating after filling deletes records that were never duplicates in the file the customer
   uploaded.
7. **Drop rows with a missing feature** when `missing_values: drop_rows`. It is a per-row rule with
   no fitted parameter, so it belongs in phase 1, where it also lets the split apply the configured
   fractions to the rows that actually survive.

Phase 2, after the split, fitted on `fit_index` and applied to every partition:

8. **Outliers**, fitted on the observed (non-null) *training* values. Clipping runs before filling so
   the percentiles come from real values only, never from values the engine itself inserted. Under
   `remove_rows` the training bounds decide which rows go from every partition, so all three parts
   keep coming from one population; `replay` still never drops a row from a scoring file.
9. **Missing values**: `auto` leaves them to AutoGluon, `fill` uses the training median (numeric) or
   the training mode (everything else). A column with nothing to measure *in the training rows* is
   recorded as not applied rather than filled from a value only the hold-out has seen.

Everything statistical is fitted on the training rows and recorded in the returned `PrepareReport`;
:func:`replay` re-applies those recorded numbers to a scoring file and never recomputes one.

`replay` deliberately does not repeat the row-level rules (deduplication, the consent filter, row
drops). Scoring must return one row per uploaded row, so prepare-time row removal would silently
lose customers; consent and opt-out at score time are the `actions` stage's job (plan §6.3), which
suppresses the row and keeps its score.

Ties and leakage in the time-based split
----------------------------------------
Rows that share the boundary timestamp all go to the *later* part. The boundary is therefore moved
earlier, never later, which shrinks training rather than the hold-out, and guarantees the invariant
the split exists for: every training timestamp is strictly before every validation timestamp, which
is strictly before every test timestamp. When a tie block is so large that a part which was asked for
rows would end up empty, the requested fractions are unachievable without straddling the cut, and the
split refuses with a `ValueError` instead of leaking a future row into training.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from time import perf_counter
from typing import TYPE_CHECKING, Final, Literal

from engine.config import MissingValues, Outliers, PiiHandling, ProblemType, SplitType
from engine.contracts import DroppedColumn, PrepareReport, RowRemoval, SplitPart, SplitReport, Transform
from engine.utils.ids import seed_from
from engine.utils.logging import get_logger
from engine.utils.text import humanise_count
from engine.utils.time import utc_now

if TYPE_CHECKING:
    from collections.abc import Sequence

    import pandas as pd

    from engine.config import UseCaseConfig

__all__ = ["RowPlan", "fit_transforms", "prepare", "prepare_rows", "replay", "split_dataset"]

_LOG = get_logger(__name__)

REDACTION: Final[str] = "[REDACTED]"

_LOWER_QUANTILE: Final[float] = 0.01
_UPPER_QUANTILE: Final[float] = 0.99
_ID_LIKE_DISTINCT_SHARE: Final[float] = 0.99
_PII_SAMPLE_ROWS: Final[int] = 1000
_PII_VALUE_SHARE: Final[float] = 0.5
_SEED_MODULUS: Final[int] = 2**32

_TRUTHY_TEXT: Final[frozenset[str]] = frozenset({"true", "t", "yes", "y", "1"})

_ID_LIKE_NAME: Final[re.Pattern[str]] = re.compile(r"(^|_)(id|uuid|guid|ref)(_|$)", re.IGNORECASE)

_PII_NAME: Final[re.Pattern[str]] = re.compile(
    r"(^|_)(e?mail|phone|mobile|msisdn|telephone|name|surname|address|street|postcode|zipcode"
    r"|aadhaar|aadhar|pan|ssn|passport)(_|$)",
    re.IGNORECASE,
)

_PII_VALUE_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("email", re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")),
    ("phone number", re.compile(r"\+?\d[\d\s().-]{7,17}\d")),
    ("PAN", re.compile(r"[A-Z]{5}\d{4}[A-Z]")),
    ("Aadhaar", re.compile(r"\d{4}\s?\d{4}\s?\d{4}")),
)

_ROW_LEVEL_KINDS: Final[frozenset[str]] = frozenset({"dedupe", "consent_filter"})

_ValueKind = Literal["number", "text", "boolean", "datetime"]


# ---------------------------------------------------------------------------
# the seam: what the row-level phase decided, handed to the statistical phase
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RowPlan:
    """The row-level phase's verdict: what it changed, and what the statistical phase still needs.

    Everything here is already decided and already applied to the frame `prepare_rows` returned. It
    travels to :func:`fit_transforms` so that one `PrepareReport` describes both phases, with the
    split sitting between them and no statistic fitted before it.
    """

    rows_in: int
    columns_in: int
    feature_columns: tuple[str, ...]
    dropped_columns: tuple[DroppedColumn, ...]
    row_removals: tuple[RowRemoval, ...]
    transforms: tuple[Transform, ...]
    pii_columns: tuple[str, ...]
    consent_column: str | None
    consent_rows_removed: int
    missing_rows_dropped: int

    @property
    def last_order(self) -> int:
        """The highest transform order used so far, so phase 2 keeps the numbering dense."""
        return max((transform.order for transform in self.transforms), default=0)


# ---------------------------------------------------------------------------
# prepare: phase 1 (row level), phase 2 (fitted on train), and the two together
# ---------------------------------------------------------------------------
def prepare_rows(
    df: pd.DataFrame,
    config: UseCaseConfig,
    *,
    primary_key: str,
    target: str | None,
) -> tuple[pd.DataFrame, RowPlan]:
    """Apply every rule that needs no fitted statistic, so the split sees the final set of rows.

    Steps 1-7 of the order above: column drops, redaction, the time cast, the consent filter, the
    missing-target drop, deduplication and the `drop_rows` missing-value rule. The returned frame has
    a reset index, and the returned `RowPlan` carries the decisions on to :func:`fit_transforms`.
    Nothing here reads the split, and nothing here is fitted: run it, split its output, then fit.
    """
    import pandas as pd

    started = perf_counter()
    frame = df.copy()
    rows_in = len(frame)
    columns_in = len(frame.columns)

    reserved = _reserved_columns(config, primary_key=primary_key, target=target)
    pii_found = _detect_pii(frame, reserved)
    dropped, redacted = _column_decisions(frame, config, reserved=reserved, pii=pii_found)

    transforms: list[Transform] = []
    removals: list[RowRemoval] = []
    order = 0

    if dropped:
        frame = frame.drop(columns=[column.name for column in dropped])

    if redacted:
        for column in redacted:
            frame[column] = REDACTION
        order += 1
        transforms.append(
            Transform(order=order, kind="redact", columns=redacted, parameters={"replacement": REDACTION})
        )

    time_column = config.split.time_column if config.split.type is SplitType.TIME_BASED else None
    if time_column is not None and time_column in frame.columns:
        frame[time_column] = pd.to_datetime(frame[time_column], errors="coerce", utc=True)
        order += 1
        transforms.append(
            Transform(
                order=order,
                kind="cast",
                columns=(time_column,),
                parameters={"to": "datetime", "utc": True},
            )
        )

    consent_column = config.governance.consent_column
    consent_removed = 0
    if consent_column is not None:
        order += 1
        if consent_column in frame.columns:
            keep = _truthy(frame, consent_column)
            consent_removed = int((~keep).sum())
            frame = frame.loc[keep].copy()
            transforms.append(
                Transform(
                    order=order,
                    kind="consent_filter",
                    columns=(consent_column,),
                    parameters={
                        "column": consent_column,
                        "rows_removed": float(consent_removed),
                        "applied": True,
                    },
                )
            )
            if consent_removed:
                removals.append(RowRemoval(reason="consent_false", rows=consent_removed))
        else:
            transforms.append(
                Transform(
                    order=order,
                    kind="consent_filter",
                    columns=(),
                    parameters={"column": consent_column, "applied": False, "reason": "column_absent"},
                )
            )

    if target is not None and target in frame.columns:
        labelled = frame[target].notna()
        unlabelled = int((~labelled).sum())
        if unlabelled:
            frame = frame.loc[labelled].copy()
            removals.append(RowRemoval(reason="missing_target", rows=unlabelled))

    if config.prepare.deduplicate:
        before = len(frame)
        frame = frame.drop_duplicates(keep="first")
        duplicates = before - len(frame)
        order += 1
        transforms.append(
            Transform(
                order=order,
                kind="dedupe",
                columns=(),
                parameters={
                    "subset": "all_columns",
                    "keep": "first",
                    "rows_removed": float(duplicates),
                    "applied": True,
                },
            )
        )
        if duplicates:
            removals.append(RowRemoval(reason="duplicate", rows=duplicates))

    feature_columns = tuple(
        column for column in frame.columns if column not in reserved and column not in redacted
    )

    missing_rows_dropped = 0
    if config.prepare.missing_values is MissingValues.DROP_ROWS:
        complete = _complete_rows(frame, features=feature_columns)
        if complete is not None:
            missing_rows_dropped = int((~complete).sum())
            frame = frame.loc[complete].copy()

    frame = frame.reset_index(drop=True)
    plan = RowPlan(
        rows_in=rows_in,
        columns_in=columns_in,
        feature_columns=feature_columns,
        dropped_columns=tuple(dropped),
        row_removals=tuple(removals),
        transforms=tuple(transforms),
        pii_columns=tuple(pii_found),
        consent_column=consent_column,
        consent_rows_removed=consent_removed,
        missing_rows_dropped=missing_rows_dropped,
    )
    _LOG.info(
        "stage=prepare phase=rows rows_in=%d rows_out=%d columns_in=%d columns_out=%d seconds=%.3f",
        rows_in,
        len(frame),
        columns_in,
        len(frame.columns),
        perf_counter() - started,
    )
    return frame, plan


def fit_transforms(
    df: pd.DataFrame,
    config: UseCaseConfig,
    plan: RowPlan,
    *,
    run_id: str,
    fit_index: pd.Index | None = None,
) -> tuple[pd.DataFrame, PrepareReport]:
    """Fit the statistical transforms on `fit_index` only, apply them everywhere, write the report.

    `df` is the frame :func:`prepare_rows` returned and `plan` is the `RowPlan` that came with it.
    `fit_index` names the training rows inside that frame - `parts["train"].index` from
    :func:`split_dataset`; `None` means every row is a fit row, which is only correct when the frame
    holds nothing but training data.

    The frame's index is preserved (rows are only ever removed, never reordered or renumbered) so the
    caller can recover each partition from the fitted frame by intersecting indexes.
    """
    started = perf_counter()
    frame = df.copy()
    fit = _fit_mask(frame, fit_index)
    fit_rows = int(fit.sum())

    transforms = list(plan.transforms)
    removals = list(plan.row_removals)
    order = plan.last_order
    features = tuple(column for column in plan.feature_columns if column in frame.columns)

    order, inside_bounds = _apply_outliers(
        frame, config, features=features, order=order, out=transforms, fit=fit
    )
    if inside_bounds is not None:
        removed = int((~inside_bounds).sum())
        frame = frame.loc[inside_bounds].copy()
        fit = fit.loc[frame.index]
        if removed:
            removals.append(RowRemoval(reason="outlier", rows=removed))

    order = _apply_missing_values(frame, config, features=features, order=order, out=transforms, fit=fit)

    rows_out = len(frame)
    report = PrepareReport(
        run_id=run_id,
        rows_in=plan.rows_in,
        rows_out=rows_out,
        columns_in=plan.columns_in,
        columns_out=len(frame.columns),
        feature_columns=plan.feature_columns,
        dropped_columns=plan.dropped_columns,
        row_removals=tuple(removals),
        transforms=tuple(transforms),
        pii_columns=plan.pii_columns,
        consent_column=plan.consent_column,
        consent_rows_removed=plan.consent_rows_removed,
        detail=_prepare_detail(
            rows_out=rows_out,
            features=len(plan.feature_columns),
            dropped=len(plan.dropped_columns),
            removals=removals,
            missing_rows=plan.missing_rows_dropped,
        ),
        prepared_at=utc_now(),
    )
    _LOG.info(
        "stage=prepare phase=fit rows=%d fit_rows=%d fit_scope=%s transforms=%d seconds=%.3f",
        rows_out,
        fit_rows,
        "all_rows" if fit_index is None else "train",
        len(transforms),
        perf_counter() - started,
    )
    return frame, report


def prepare(
    df: pd.DataFrame,
    config: UseCaseConfig,
    *,
    run_id: str,
    primary_key: str,
    target: str | None,
) -> tuple[pd.DataFrame, PrepareReport]:
    """Clean the table as the config asks and record every transform, drop and removal.

    The single-frame convenience: :func:`prepare_rows` followed by :func:`fit_transforms` with every
    surviving row used as a fit row. Correct when `df` is training data and nothing else - a frame
    that has already been split, or one that is never split. **The train pipeline must not call it**,
    because a statistic fitted over a whole uploaded file sees the validation and test rows; it calls
    the two phases around `split_dataset` instead (see the module docstring and DEC-046).
    """
    started = perf_counter()
    rows, plan = prepare_rows(df, config, primary_key=primary_key, target=target)
    frame, report = fit_transforms(rows, config, plan, run_id=run_id)
    frame = frame.reset_index(drop=True)
    _LOG.info(
        "stage=prepare rows_in=%d rows_out=%d columns_in=%d columns_out=%d seconds=%.3f",
        report.rows_in,
        report.rows_out,
        report.columns_in,
        report.columns_out,
        perf_counter() - started,
    )
    return frame, report


def _fit_mask(frame: pd.DataFrame, fit_index: pd.Index | None) -> pd.Series[bool]:
    """The rows every statistic is fitted on, as a mask aligned to `frame`.

    A label that is not in the frame is a wiring mistake between the split and this stage, and a
    training part with no rows would silently turn every fitted transform into a no-op, so both are
    refused here rather than quietly shrinking what the statistics see.
    """
    import pandas as pd

    if fit_index is None:
        return pd.Series(True, index=frame.index, dtype=bool)
    unknown = fit_index.difference(frame.index)
    if len(unknown):
        raise ValueError(
            f"fit_index names {len(unknown)} rows that are not in this frame; it must index the frame "
            f"prepare_rows returned, which is the frame that was split."
        )
    if len(frame) and not len(fit_index):
        raise ValueError(
            "fit_index selects no rows: a fill value or a clip bound cannot be fitted on an empty "
            "training part."
        )
    return pd.Series(frame.index.isin(fit_index), index=frame.index, dtype=bool)


def _reserved_columns(config: UseCaseConfig, *, primary_key: str, target: str | None) -> tuple[str, ...]:
    """Columns the engine needs by name, which no heuristic may drop and none of which is a feature."""
    candidates = (
        primary_key,
        target,
        config.target.column,
        config.split.time_column,
        config.split.group_column,
        config.evaluation.fairness_column,
        config.governance.consent_column,
        config.actions.suppression.opt_out_column,
        config.actions.suppression.recently_contacted_column,
    )
    reserved: list[str] = []
    for name in candidates:
        if name is not None and name not in reserved:
            reserved.append(name)
    return tuple(reserved)


def _detect_pii(frame: pd.DataFrame, reserved: Sequence[str]) -> dict[str, str]:
    """Columns that look like personal data, mapped to the detector that matched.

    A column matches on its **name** whatever its dtype, or on its **values** when it holds text and
    the majority of the first `_PII_SAMPLE_ROWS` observed values match one of the value patterns.
    Value matching is limited to text columns on purpose: a twelve-digit integer is an Aadhaar number
    only in a column that says so, and treating every long number as PII would redact real features.
    Reserved columns are never matched - the primary key is meant to identify a customer, and
    redacting it would make the scores unusable.

    Like every other column decision this reads the whole file, before the split: a column is
    personal data or it is not, whichever partition a row lands in, and the answer has to be the same
    for all three (see step 1 of the order above).
    """
    found: dict[str, str] = {}
    for column in frame.columns:
        name = str(column)
        if name in reserved:
            continue
        if _PII_NAME.search(name):
            found[name] = "column name"
            continue
        if not _is_texty(frame, name):
            continue
        sampled = [str(value) for value in frame[name].dropna().head(_PII_SAMPLE_ROWS).tolist()]
        if not sampled:
            continue
        needed = max(1, int(len(sampled) * _PII_VALUE_SHARE))
        for label, pattern in _PII_VALUE_PATTERNS:
            if sum(1 for value in sampled if pattern.fullmatch(value)) >= needed:
                found[name] = label
                break
    return found


def _column_decisions(
    frame: pd.DataFrame,
    config: UseCaseConfig,
    *,
    reserved: Sequence[str],
    pii: dict[str, str],
) -> tuple[list[DroppedColumn], tuple[str, ...]]:
    """The columns to drop (with their reason) and the PII columns to redact in place."""
    rows = len(frame)
    dropped: list[DroppedColumn] = []
    handled: set[str] = set()

    for name in config.prepare.exclude_columns:
        if name in frame.columns and name not in handled:
            dropped.append(
                DroppedColumn(name=name, reason="user_excluded", detail="Excluded in the run settings.")
            )
            handled.add(name)

    redacted: list[str] = []
    for name, detector in pii.items():
        if name in handled:
            continue
        if config.prepare.pii_handling is PiiHandling.DROP_COLUMNS:
            dropped.append(
                DroppedColumn(name=name, reason="pii", detail=f"Personal data, matched by {detector}.")
            )
            handled.add(name)
        elif config.prepare.pii_handling is PiiHandling.REDACT:
            redacted.append(name)
            handled.add(name)

    for column in frame.columns:
        name = str(column)
        if name in handled or name in reserved:
            continue
        observed = int(frame[name].count())
        distinct = int(frame[name].nunique(dropna=True))
        if _is_id_like(frame, name, rows=rows, distinct=distinct):
            dropped.append(
                DroppedColumn(
                    name=name,
                    reason="id_like",
                    detail=f"{distinct} distinct values in {rows} rows, so it identifies rows rather than describing them.",
                )
            )
            continue
        # One value and mostly present is "constant"; one value and mostly missing is "high_null",
        # which is the more useful thing to tell the user about a column that is nearly all blank.
        if (
            observed > 0
            and distinct <= 1
            and rows - observed <= rows * config.validation.high_null_column_rate
        ):
            dropped.append(
                DroppedColumn(name=name, reason="constant", detail="Only one value, so it explains nothing.")
            )
            continue
        null_rate = 0.0 if rows == 0 else (rows - observed) / rows
        if null_rate > config.validation.high_null_column_rate:
            dropped.append(
                DroppedColumn(
                    name=name,
                    reason="high_null",
                    detail=f"{null_rate:.0%} of the values are missing.",
                )
            )
    return dropped, tuple(redacted)


def _is_id_like(frame: pd.DataFrame, column: str, *, rows: int, distinct: int) -> bool:
    """Near-unique text, or a near-unique column whose name says identifier (plan §6.3)."""
    if rows == 0 or distinct < rows * _ID_LIKE_DISTINCT_SHARE:
        return False
    return _is_texty(frame, column) or bool(_ID_LIKE_NAME.search(column))


def _is_texty(frame: pd.DataFrame, column: str) -> bool:
    """True for object, string and categorical columns: the ones whose values may be free text."""
    import pandas as pd

    dtype = frame[column].dtype
    if isinstance(dtype, pd.CategoricalDtype):
        return True
    return bool(pd.api.types.is_object_dtype(dtype) or pd.api.types.is_string_dtype(dtype))


def _truthy(frame: pd.DataFrame, column: str) -> pd.Series[bool]:
    """A boolean mask of the rows whose consent value is truthy.

    A missing or unrecognised value is **not** consent: the absence of a record of consent is not a
    record of consent, so those rows are filtered out rather than kept.
    """
    import pandas as pd

    series = frame[column]
    if pd.api.types.is_bool_dtype(series.dtype):
        return series.fillna(value=False).astype(bool)
    if pd.api.types.is_numeric_dtype(series.dtype):
        return series.fillna(value=0).ne(0)
    text = series.astype("string").str.strip().str.lower()
    return text.isin(_TRUTHY_TEXT).fillna(value=False).astype(bool)


def _complete_rows(frame: pd.DataFrame, *, features: Sequence[str]) -> pd.Series[bool] | None:
    """Rows with a value in every feature column, or None when there is no feature to check."""
    present = [column for column in features if column in frame.columns]
    if not present:
        return None
    return frame[present].notna().all(axis=1)


def _apply_outliers(
    frame: pd.DataFrame,
    config: UseCaseConfig,
    *,
    features: Sequence[str],
    order: int,
    out: list[Transform],
    fit: pd.Series[bool],
) -> tuple[int, pd.Series[bool] | None]:
    """Clip or flag numeric outliers; returns the next transform order and the rows to keep.

    The percentiles come from the observed values of numeric feature columns **in the fit rows**: a
    flag has no meaningful first percentile, a missing value is not an extreme one, and a hold-out
    row may not decide where the training data is clipped. The bounds are then applied to every row,
    so all three partitions are on the same scale. `remove_rows` records a `RowRemoval` rather than a
    `Transform`, because a row dropped here is a training decision and `replay` never drops a row
    from a scoring file.
    """
    if config.prepare.outliers is Outliers.KEEP:
        return order, None
    fit_rows = float(int(fit.sum()))
    keep: pd.Series[bool] | None = None
    for column in features:
        if not _is_numeric_feature(frame, column):
            continue
        series = frame[column]
        observed = series[fit].dropna()
        if observed.empty:
            if config.prepare.outliers is Outliers.CLIP:
                order += 1
                out.append(
                    Transform(
                        order=order,
                        kind="clip_percentile",
                        columns=(column,),
                        parameters={
                            "applied": False,
                            "reason": "no_observed_values",
                            "fit_rows": fit_rows,
                        },
                    )
                )
            continue
        lower = float(observed.quantile(_LOWER_QUANTILE))
        upper = float(observed.quantile(_UPPER_QUANTILE))
        if config.prepare.outliers is Outliers.CLIP:
            frame[column] = series.clip(lower=lower, upper=upper)
            order += 1
            out.append(
                Transform(
                    order=order,
                    kind="clip_percentile",
                    columns=(column,),
                    parameters={
                        "lower": lower,
                        "upper": upper,
                        "lower_quantile": _LOWER_QUANTILE,
                        "upper_quantile": _UPPER_QUANTILE,
                        "fit_rows": fit_rows,
                        "applied": True,
                    },
                )
            )
        else:
            inside = series.isna() | ((series >= lower) & (series <= upper))
            keep = inside if keep is None else (keep & inside)
    if keep is not None:
        keep = keep.astype(bool)
    return order, keep


def _apply_missing_values(
    frame: pd.DataFrame,
    config: UseCaseConfig,
    *,
    features: Sequence[str],
    order: int,
    out: list[Transform],
    fit: pd.Series[bool],
) -> int:
    """Fill missing feature values from the fit rows; returns the next transform order.

    Under `fill`, a value is fitted and recorded for **every** feature column, not only the ones with
    a gap in the training file: a column that happens to be complete here may well have gaps in a
    scoring file, and `replay` may not measure a replacement on that file. A column with nothing to
    measure *in the fit rows* is recorded as not applied rather than filled with a value borrowed
    from the hold-out. `drop_rows` has already run in :func:`prepare_rows`, where it belongs: it
    needs no fitted parameter and the split has to see the rows that survive it.
    """
    if config.prepare.missing_values is not MissingValues.FILL:
        return order
    fit_rows = float(int(fit.sum()))
    for column in features:
        if column not in frame.columns:
            continue
        series = frame[column]
        kind, value, value_kind = _fill_plan(frame, column, fit=fit)
        order += 1
        if value is None:
            out.append(
                Transform(
                    order=order,
                    kind=kind,
                    columns=(column,),
                    parameters={
                        "applied": False,
                        "reason": "all_values_missing",
                        "fit_rows": fit_rows,
                    },
                )
            )
            continue
        frame[column] = series.fillna(value=_fill_object(value, value_kind))
        out.append(
            Transform(
                order=order,
                kind=kind,
                columns=(column,),
                parameters={
                    "value": value,
                    "value_kind": value_kind,
                    "fit_rows": fit_rows,
                    "applied": True,
                },
            )
        )
    return order


def _fill_plan(
    frame: pd.DataFrame, column: str, *, fit: pd.Series[bool]
) -> tuple[Literal["fill_median", "fill_mode"], float | str | bool | None, _ValueKind]:
    """The fill this column gets: median for numbers, most frequent value for everything else.

    Both statistics are measured on the fit rows alone. The value is returned in a form the
    `Transform.parameters` union can hold, so the recorded number or string is exactly what
    :func:`replay` will use - no statistic is ever recomputed at score time.
    """
    import pandas as pd

    series = frame[column]
    observed = series[fit].dropna()
    if pd.api.types.is_bool_dtype(series.dtype):
        modes = observed.mode()
        return "fill_mode", (None if modes.empty else bool(modes.iloc[0])), "boolean"
    if pd.api.types.is_numeric_dtype(series.dtype):
        if observed.empty:
            return "fill_median", None, "number"
        return "fill_median", float(observed.median()), "number"
    if pd.api.types.is_datetime64_any_dtype(series.dtype):
        modes = observed.mode()
        return "fill_mode", (None if modes.empty else str(modes.iloc[0])), "datetime"
    modes = observed.mode()
    return "fill_mode", (None if modes.empty else str(modes.iloc[0])), "text"


def _fill_object(value: float | str | bool, value_kind: _ValueKind) -> float | str | bool | pd.Timestamp:
    """Turn a recorded parameter back into the value that is written into the column."""
    import pandas as pd

    if value_kind == "datetime":
        return pd.Timestamp(str(value))
    if value_kind == "boolean":
        return bool(value)
    if value_kind == "number":
        return float(value)
    return str(value)


def _is_numeric_feature(frame: pd.DataFrame, column: str) -> bool:
    """Numeric and not boolean: a flag has no first or ninety-ninth percentile worth clipping."""
    import pandas as pd

    if column not in frame.columns:
        return False
    dtype = frame[column].dtype
    return bool(pd.api.types.is_numeric_dtype(dtype)) and not bool(pd.api.types.is_bool_dtype(dtype))


def _prepare_detail(
    *,
    rows_out: int,
    features: int,
    dropped: int,
    removals: Sequence[RowRemoval],
    missing_rows: int,
) -> str:
    """The Running-screen line: only the segments that actually happened (plan §2.1 principle 5)."""
    segments = [
        f"{humanise_count(rows_out)} rows ready",
        f"{features} {'feature' if features == 1 else 'features'}",
    ]
    if dropped:
        segments.append(f"{dropped} {'column' if dropped == 1 else 'columns'} dropped")
    removed = sum(removal.rows for removal in removals)
    if removed:
        segments.append(f"{humanise_count(removed)} rows removed")
    if missing_rows:
        segments.append(f"{humanise_count(missing_rows)} rows dropped for missing values")
    return " · ".join(segments)


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------
def replay(df: pd.DataFrame, report: PrepareReport) -> pd.DataFrame:
    """Re-apply a recorded `PrepareReport` to a scoring file, so training and scoring agree.

    Only the recorded parameters are used; nothing is measured on `df`. The rules for a frame that
    does not match the training file exactly are:

    * a **dropped column that is absent** is already gone - nothing to do;
    * a **transform whose column is absent** is skipped and logged by name, because the alternative
      would be to invent the column; the missing column itself is `validate_against_schema`'s
      `SCHEMA_MISMATCH` to report, and replay never raises on data;
    * a **clip on a column that is no longer numeric** is skipped for the same reason;
    * an **extra column** is left exactly as it is. Replay removes the columns the report names and
      nothing else; choosing the model's feature columns belongs to the predict stage, which has
      `schema.json`.

    Row-level rules (deduplication, the consent filter, row drops) are not replayed: every scored row
    must come back with a score, and consent and opt-out are applied by the `actions` stage.
    """
    import pandas as pd

    frame = df.copy()
    present = [column.name for column in report.dropped_columns if column.name in frame.columns]
    if present:
        frame = frame.drop(columns=present)

    for transform in sorted(report.transforms, key=lambda item: item.order):
        if transform.kind in _ROW_LEVEL_KINDS:
            continue
        if transform.parameters.get("applied") is False:
            continue
        for column in transform.columns:
            if column not in frame.columns:
                _LOG.warning(
                    "stage=replay transform=%s column=%s skipped=column_absent", transform.kind, column
                )
                continue
            if transform.kind == "redact":
                frame[column] = str(transform.parameters.get("replacement", REDACTION))
            elif transform.kind == "cast":
                if transform.parameters.get("to") != "datetime":
                    _LOG.warning("stage=replay transform=cast column=%s skipped=unknown_target", column)
                    continue
                frame[column] = pd.to_datetime(frame[column], errors="coerce", utc=True)
            elif transform.kind == "clip_percentile":
                if not _is_numeric_feature(frame, column):
                    _LOG.warning(
                        "stage=replay transform=clip_percentile column=%s skipped=not_numeric", column
                    )
                    continue
                frame[column] = frame[column].clip(
                    lower=float(transform.parameters["lower"]),
                    upper=float(transform.parameters["upper"]),
                )
            elif transform.kind in ("fill_median", "fill_mode"):
                value = transform.parameters["value"]
                kind = str(transform.parameters.get("value_kind", "text"))
                frame[column] = frame[column].fillna(value=_fill_object(value, _value_kind(kind)))
    return frame


def _value_kind(raw: str) -> _ValueKind:
    """Narrow a recorded `value_kind` string back to the literal `_fill_object` expects."""
    if raw == "number":
        return "number"
    if raw == "boolean":
        return "boolean"
    if raw == "datetime":
        return "datetime"
    return "text"


# ---------------------------------------------------------------------------
# split
# ---------------------------------------------------------------------------
def split_dataset(
    df: pd.DataFrame,
    config: UseCaseConfig,
    *,
    run_id: str,
    target: str,
) -> tuple[dict[str, pd.DataFrame], SplitReport]:
    """Split into train/validation/test by the configured strategy, seeded from the run id."""
    started = perf_counter()
    seed = seed_from(run_id)
    rows = len(df)
    positions: dict[str, list[int]]
    times: pd.Series[pd.Timestamp] | None = None
    note = ""
    if config.split.type is SplitType.TIME_BASED:
        column = config.split.time_column
        if column is None:
            raise ValueError("A time-based split needs split.time_column to name the date column.")
        if column not in df.columns:
            raise ValueError(
                f"A time-based split needs the column {column!r}, which this table does not have."
            )
        times = _times(df, column)
        positions = _time_based_positions(df, config, times)
    elif config.split.group_column is not None:
        positions = _grouped_positions(df, config, seed=seed)
        note = f"groups kept whole by {config.split.group_column}"
    else:
        positions, stratified = _stratified_positions(df, config, seed=seed, target=target)
        if not stratified:
            note = "not stratified: one class has too few rows"

    parts = {name: df.iloc[index].copy() for name, index in positions.items()}
    positive = (
        _positive_mask(df, target, config)
        if config.problem_type is ProblemType.BINARY_CLASSIFICATION and target in df.columns
        else None
    )

    summaries: list[SplitPart] = []
    for name in ("train", "validation", "test"):
        index = positions[name]
        part_rows = len(index)
        positive_rows = None if positive is None else int(positive.take(index).sum())
        start_date = None
        end_date = None
        if times is not None and part_rows:
            part_times = times.take(index)
            start_date = part_times.min().to_pydatetime()
            end_date = part_times.max().to_pydatetime()
        summaries.append(
            SplitPart(
                name=name,
                rows=part_rows,
                share=0.0 if rows == 0 else round(part_rows / rows, 4),
                positive_rows=positive_rows,
                positive_rate=(
                    None if positive_rows is None or part_rows == 0 else round(positive_rows / part_rows, 4)
                ),
                start_date=start_date,
                end_date=end_date,
            )
        )

    train_cutoff = summaries[0].end_date if times is not None else None
    test_cutoff = summaries[2].start_date if times is not None else None
    report = SplitReport(
        run_id=run_id,
        type=config.split.type,
        time_column=config.split.time_column if config.split.type is SplitType.TIME_BASED else None,
        group_column=config.split.group_column,
        parts=tuple(summaries),
        validation_fraction=config.split.validation_fraction,
        test_fraction=config.split.test_fraction,
        train_cutoff=train_cutoff,
        test_cutoff=test_cutoff,
        seed=seed,
        detail=_split_detail(summaries, test_cutoff=test_cutoff, note=note),
        split_at=utc_now(),
    )
    _LOG.info(
        "stage=split rows=%d train=%d validation=%d test=%d seconds=%.3f",
        rows,
        summaries[0].rows,
        summaries[1].rows,
        summaries[2].rows,
        perf_counter() - started,
    )
    return parts, report


def _part_sizes(rows: int, validation_fraction: float, test_fraction: float) -> tuple[int, int, int]:
    """Rows per part for `rows` rows: the three always add up to `rows`, so no row is lost."""
    n_test = min(rows, round(rows * test_fraction))
    n_validation = min(rows - n_test, round(rows * validation_fraction))
    return rows - n_validation - n_test, n_validation, n_test


def _times(df: pd.DataFrame, column: str) -> pd.Series[pd.Timestamp]:
    """The time column as timezone-aware UTC timestamps, without touching the caller's frame."""
    import pandas as pd

    parsed = pd.to_datetime(df[column], errors="coerce", utc=True)
    unparseable = int(parsed.isna().sum())
    if unparseable:
        raise ValueError(
            f"A time-based split needs a date in every row: {unparseable} of {len(df)} rows have no "
            f"usable value in {column!r}."
        )
    return parsed


def _time_based_positions(
    df: pd.DataFrame, config: UseCaseConfig, times: pd.Series[pd.Timestamp]
) -> dict[str, list[int]]:
    """Oldest rows train, then validation, newest test - with no timestamp on both sides of a cut."""
    column = str(config.split.time_column)
    rows = len(df)
    unsorted = list(times)
    ordered = sorted(range(rows), key=lambda position: unsorted[position])
    values = [unsorted[position] for position in ordered]
    wanted_train, wanted_validation, wanted_test = _part_sizes(
        rows, config.split.validation_fraction, config.split.test_fraction
    )
    first_validation = _tie_boundary(values, wanted_train, floor=0)
    first_test = _tie_boundary(values, wanted_train + wanted_validation, floor=first_validation)
    sizes = (first_validation, first_test - first_validation, rows - first_test)
    for name, wanted, got in zip(
        ("training", "validation", "test"),
        (wanted_train, wanted_validation, wanted_test),
        sizes,
        strict=True,
    ):
        if wanted > 0 and got == 0:
            raise ValueError(
                f"The time-based split cannot honour the configured fractions: too many rows share the "
                f"same value of {column!r} at the cut-off, so the {name} part would be empty. Use a "
                f"finer date column or a random split."
            )
    return {
        "train": ordered[:first_validation],
        "validation": ordered[first_validation:first_test],
        "test": ordered[first_test:],
    }


def _tie_boundary(values: Sequence[pd.Timestamp], boundary: int, *, floor: int) -> int:
    """Move `boundary` back to the first row of its tie block, so a timestamp never straddles a cut."""
    if boundary <= floor or boundary >= len(values):
        return max(boundary, floor)
    moved = boundary
    while moved > floor and values[moved - 1] == values[boundary]:
        moved -= 1
    return moved


def _stratified_positions(
    df: pd.DataFrame,
    config: UseCaseConfig,
    *,
    seed: int,
    target: str,
) -> tuple[dict[str, list[int]], bool]:
    """A random split that keeps each part's class mix, falling back to a plain shuffle when it cannot."""
    from sklearn.model_selection import train_test_split

    rows = len(df)
    _, n_validation, n_test = _part_sizes(rows, config.split.validation_fraction, config.split.test_fraction)
    labels = df[target].tolist() if target in df.columns else None
    positions = list(range(rows))
    stratified = True

    def draw(pool: list[int], take: int, state: int) -> tuple[list[int], list[int]]:
        nonlocal stratified
        if take <= 0:
            return pool, []
        if take >= len(pool):
            return [], pool
        strata = None if labels is None else [labels[position] for position in pool]
        if strata is not None:
            try:
                kept, taken = train_test_split(pool, test_size=take, stratify=strata, random_state=state)
            except ValueError:
                stratified = False
            else:
                return list(kept), list(taken)
        kept, taken = train_test_split(pool, test_size=take, random_state=state)
        return list(kept), list(taken)

    rest, test = draw(positions, n_test, seed)
    train, validation = draw(rest, n_validation, (seed + 1) % _SEED_MODULUS)
    return {"train": sorted(train), "validation": sorted(validation), "test": sorted(test)}, stratified


def _grouped_positions(df: pd.DataFrame, config: UseCaseConfig, *, seed: int) -> dict[str, list[int]]:
    """Every row of a group lands in one part (plan §6.3 asks for `GroupShuffleSplit`).

    `GroupShuffleSplit` divides *groups*, not rows, so the configured fractions are applied to the
    groups and the row counts only approximate them; the report's shares say what was actually split.
    """
    from sklearn.model_selection import GroupShuffleSplit

    column = config.split.group_column
    if column is None:
        raise ValueError("A grouped split needs split.group_column to name the group column.")
    if column not in df.columns:
        raise ValueError(f"A grouped split needs the column {column!r}, which this table does not have.")
    groups = df[column].astype("string").fillna(value="__missing__").tolist()
    rows = len(df)
    _, wanted_validation, wanted_test = _part_sizes(
        rows, config.split.validation_fraction, config.split.test_fraction
    )
    needed = 1 + (1 if wanted_validation else 0) + (1 if wanted_test else 0)
    distinct = len(set(groups))
    if distinct < needed:
        raise ValueError(
            f"A grouped split needs at least {needed} distinct values of {column!r} to fill "
            f"{needed} parts; this table has {distinct}."
        )
    positions = list(range(rows))

    def draw(pool: list[int], fraction: float, state: int) -> tuple[list[int], list[int]]:
        if fraction <= 0.0 or not pool:
            return pool, []
        pool_groups = [groups[position] for position in pool]
        splitter = GroupShuffleSplit(n_splits=1, test_size=fraction, random_state=state)
        kept_index, taken_index = next(splitter.split(pool, groups=pool_groups))
        return [pool[i] for i in kept_index], [pool[i] for i in taken_index]

    test_fraction = config.split.test_fraction
    rest, test = draw(positions, test_fraction, seed)
    remaining = 1.0 - test_fraction
    validation_fraction = 0.0 if remaining <= 0 else config.split.validation_fraction / remaining
    train, validation = draw(rest, validation_fraction, (seed + 1) % _SEED_MODULUS)
    return {"train": sorted(train), "validation": sorted(validation), "test": sorted(test)}


def _positive_mask(df: pd.DataFrame, target: str, config: UseCaseConfig) -> pd.Series[bool]:
    """Which rows carry the positive label, comparing labels as normalised strings.

    `target.positive_label` wins when it is configured. Otherwise the label is auto-detected (plan
    §5 allows omitting it): `true` over `false`, `1` over `0`, `yes` over `no`, and failing all three
    the rarer of the two labels, which is the marketing convention for the event being predicted.
    """
    keys = df[target].map(_label_key)
    configured = config.target.positive_label
    if configured is not None:
        return keys.eq(_label_key(configured))
    counts = keys.value_counts()
    labels = list(counts.index)
    for conventional in ("true", "1", "yes"):
        if conventional in labels:
            return keys.eq(conventional)
    if not labels:
        return keys.eq("\x00")
    rarest = min(labels, key=lambda label: (int(counts[label]), label))
    return keys.eq(rarest)


def _label_key(value: object) -> str:
    """One comparable spelling per label, so `1`, `1.0` and `"1"` are the same class."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    return str(value).strip().lower()


def _split_detail(parts: Sequence[SplitPart], *, test_cutoff: datetime | None, note: str) -> str:
    """The Running-screen line: the plan's sentence for a time-based split, sizes otherwise."""
    if test_cutoff is not None:
        return f"Training on data before {test_cutoff.date().isoformat()}, testing after"
    segments = [
        f"{humanise_count(parts[0].rows)} train",
        f"{humanise_count(parts[1].rows)} validation",
        f"{humanise_count(parts[2].rows)} test",
    ]
    if note:
        segments.append(note)
    return " · ".join(segments)
