"""The six uplift validation checks of plan B §4, run after Phase 1's own checks.

Phase 1 validation decides whether a file can be modelled at all (a key, a binary outcome, enough
rows). An uplift run needs more: an experiment. These checks decide whether the file records one -
a treatment column, two arms big enough to compare, and an assignment nobody chose - and whether
the data was captured at the right moment, before the campaign changed anything and long enough
after it for the outcome to be final. They write `uplift_validation.json`; a run needs both
reports to pass.

| Code                       | Severity | Blocks | Why it exists                                                     |
|----------------------------|----------|--------|-------------------------------------------------------------------|
| TREATMENT_COLUMN_MISSING   | error    | yes    | no experiment to learn from                                       |
| TREATMENT_NOT_BINARY       | error    | yes    | a row in neither arm, or in some third arm, cannot be compared    |
| TREATMENT_ARM_TOO_SMALL    | error    | yes    | an effect measured on a handful of customers is noise             |
| TREATMENT_NOT_RANDOM       | error    | unless acknowledged | a targeted campaign's "effect" is who was targeted   |
| OUTCOME_WINDOW_IMMATURE    | warning  | no     | a customer treated yesterday has not had time to convert          |
| FEATURE_AFTER_TREATMENT    | error    | yes    | a snapshot taken after the campaign already contains its effect   |

**Randomness.** Under random assignment nothing about a customer predicts whether they were
treated, so a classifier predicting treatment from the features scores an AUC of about 0.5. The check
fits a small LightGBM with 3-fold stratified cross-validation on exactly the features the uplift
learners will use (:func:`engine.uplift.data.fit_feature_spec`) and compares the out-of-fold AUC with
`uplift.randomness_auc_max`. Above it, the file describes a targeted campaign: comparing treated
with untreated customers then measures the targeting rule as much as the treatment. The user may
acknowledge that (`TREATMENT_NOT_RANDOM`, or `TREATMENT_NOT_RANDOM:<treatment column>`); the run then
goes ahead with `causal: false`, and every uplift output carries `NOT_CAUSAL_NOTE`. The threshold is
deliberately not agent-editable (plan B §12). An error inside the model fit is *not* swallowed: a
check that silently skipped here would let a targeted campaign be called causal.

**Skipped, not failed twice.** A check that cannot run because an earlier one failed (no treatment
column, so no arms) adds nothing; the report carries the one finding that explains the problem.

**Maturity.** With `uplift.treatment_date_column` and `uplift.outcome_window_days` set, a row whose
treatment date plus the window is after `now` has an outcome that may still change, and a row whose
date is blank or unreadable cannot be shown to be final. Both are dropped (returned as the `keep`
mask) and counted - never guessed - and the arm sizes are checked on what is left.

Messages are business language with the numbers filled in, like the Phase 1 validation table, and
never quote a data value (plan section 13.7). `numpy`, `pandas`, `sklearn` and `lightgbm` are imported
inside function bodies so `import engine` stays fast.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from engine.contracts import Severity
from engine.uplift.contracts import UpliftCheck, UpliftValidationReport
from engine.uplift.data import (
    apply_feature_spec,
    coerce_outcome,
    coerce_treatment,
    date_like_columns,
    detect_treatment_column,
    fit_feature_spec,
    reserved_feature_exclusions,
)
from engine.utils.logging import get_logger, log_stage
from engine.utils.time import utc_now

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from datetime import datetime

    import numpy as np
    import numpy.typing as npt
    import pandas as pd

    from engine.config import UseCaseConfig

    BoolArray = npt.NDArray[np.bool_]
    IntArray = npt.NDArray[np.int_]

__all__ = [
    "CHECK_ORDER",
    "RANDOMNESS_FOLDS",
    "RANDOMNESS_MIN_ARM_ROWS",
    "RANDOMNESS_SAMPLE_ROWS",
    "SIGNAL_MIN_GAIN_SHARE",
    "TOP_SIGNALS",
    "UpliftCheckResult",
    "run_uplift_checks",
    "treatment_predictability",
]

_LOGGER = get_logger(__name__)

CHECK_ORDER: Final[tuple[str, ...]] = (
    "TREATMENT_COLUMN_MISSING",
    "TREATMENT_NOT_BINARY",
    "TREATMENT_ARM_TOO_SMALL",
    "TREATMENT_NOT_RANDOM",
    "OUTCOME_WINDOW_IMMATURE",
    "FEATURE_AFTER_TREATMENT",
)
"""Plan B §4 table order; findings are listed errors first, then in this order."""

RANDOMNESS_FOLDS: Final[int] = 3
RANDOMNESS_MIN_ARM_ROWS: Final[int] = 30
"""Below this many rows in either arm a 3-fold AUC is noise; the randomness check is skipped
(TREATMENT_ARM_TOO_SMALL already blocks such a file)."""
RANDOMNESS_SAMPLE_ROWS: Final[int] = 50_000
"""Rows the randomness classifier sees at most (a seeded sample, stratified on treatment)."""
TOP_SIGNALS: Final[int] = 3
"""Features named in the TREATMENT_NOT_RANDOM message, strongest first."""
SIGNAL_MIN_GAIN_SHARE: Final[float] = 0.10
"""A feature is named as a sign of targeting only if it carries at least this share of the split gain,
so a noise column the trees nibbled at is not blamed."""

_RANDOMNESS_PARAMS: Final[dict[str, Any]] = {
    "n_estimators": 100,
    "learning_rate": 0.1,
    "num_leaves": 15,
    "min_child_samples": 50,
    "importance_type": "gain",
    "n_jobs": 2,
    "deterministic": True,
    "force_row_wise": True,
    "verbose": -1,
}

_SEVERITY_RANK: Final[dict[Severity, int]] = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}


@dataclass(frozen=True)
class UpliftCheckResult:
    """The report, and which rows survive the maturity check.

    `keep` is a boolean mask over the frame's rows (immature and undated rows `False`), or `None`
    when the maturity check did not run - no treatment date column or no outcome window configured.
    """

    report: UpliftValidationReport
    keep: BoolArray | None


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
def _n(count: int) -> str:
    return f"{count:,}"


def _join(items: Sequence[str]) -> str:
    quoted = [f"'{item}'" for item in items]
    if len(quoted) <= 1:
        return quoted[0] if quoted else ""
    return f"{', '.join(quoted[:-1])} and {quoted[-1]}"


def _finding(
    code: str,
    severity: Severity,
    message: str,
    suggestion: str,
    *,
    column: str | None = None,
    details: dict[str, Any] | None = None,
    acknowledgeable: bool = False,
    acknowledged: bool = False,
) -> UpliftCheck:
    return UpliftCheck(
        code=code,
        severity=severity,
        message=message,
        suggestion=suggestion,
        column=column,
        details=details or {},
        acknowledgeable=acknowledgeable,
        acknowledged=acknowledged,
    )


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------
def _treatment_missing(config: UseCaseConfig) -> UpliftCheck:
    uplift = config.uplift
    if uplift.treatment_column is not None:
        message = f"The treatment column '{uplift.treatment_column}' is not in this file."
        suggestion = (
            "Choose the column that records who received the campaign in Setup, or upload the file "
            "that contains it."
        )
    else:
        message = (
            "No column in this file says which customers received the campaign. Looked for "
            f"{_join(uplift.treatment_column_hints)}."
        )
        suggestion = (
            "Add a column with 1 for customers who were contacted and 0 for the randomly held-out "
            "customers, or choose the column in Setup."
        )
    return _finding(
        "TREATMENT_COLUMN_MISSING",
        Severity.ERROR,
        message,
        suggestion,
        column=uplift.treatment_column,
        details={"configured": uplift.treatment_column, "looked_for": list(uplift.treatment_column_hints)},
    )


def _treatment_not_binary(column: str, series: pd.Series, bad: int) -> UpliftCheck:
    rows = len(series)
    nulls = int(series.isna().sum())
    share = bad / rows if rows else 0.0
    return _finding(
        "TREATMENT_NOT_BINARY",
        Severity.ERROR,
        f"'{column}' should be 1 for treated customers and 0 for held-out customers, but {_n(bad)} of "
        f"{_n(rows)} rows ({share:.1%}) are blank or hold another value.",
        "Record every customer as 1 (treated) or 0 (held out); true and false work too. Remove "
        "customers whose treatment is unknown from the file.",
        column=column,
        details={"bad_values": bad, "null_values": nulls, "rows": rows, "bad_rate": round(share, 4)},
    )


def _arm_too_small(
    *,
    target: str,
    treated_rows: int,
    control_rows: int,
    treated_positives: int | None,
    control_positives: int | None,
    min_rows: int,
    min_positives: int,
    rows_excluded: int,
) -> UpliftCheck | None:
    shortfalls: list[str] = []
    for arm, rows, positives in (
        ("treated group", treated_rows, treated_positives),
        ("control group", control_rows, control_positives),
    ):
        if rows < min_rows:
            shortfalls.append(f"the {arm} has {_n(rows)} customers (at least {_n(min_rows)} needed)")
        if positives is not None and positives < min_positives:
            shortfalls.append(
                f"only {_n(positives)} customers in the {arm} had a positive '{target}' "
                f"(at least {_n(min_positives)} needed)"
            )
    if not shortfalls:
        return None
    after = (
        f" after leaving out {_n(rows_excluded)} customers whose outcome is not final yet"
        if rows_excluded
        else ""
    )
    return _finding(
        "TREATMENT_ARM_TOO_SMALL",
        Severity.ERROR,
        f"There are too few customers to measure what the campaign changed{after}: "
        f"{'; '.join(shortfalls)}.",
        "Use a longer period or a larger campaign, or hold out a bigger control group next time.",
        details={
            "treated_rows": treated_rows,
            "control_rows": control_rows,
            "treated_positives": treated_positives,
            "control_positives": control_positives,
            "min_arm_rows": min_rows,
            "min_arm_positives": min_positives,
            "rows_excluded": rows_excluded,
        },
    )


def treatment_predictability(
    features: pd.DataFrame, t: IntArray, *, seed: int
) -> tuple[float, tuple[str, ...], int] | None:
    """`(out-of-fold AUC, strongest features, rows used)` of a classifier predicting treatment.

    3-fold stratified cross-validation of a small, deterministic LightGBM; the AUC is computed once
    over all out-of-fold predictions. Features are ranked by their split gain summed over the folds,
    and only those carrying at least :data:`SIGNAL_MIN_GAIN_SHARE` of it are named. `None` when
    there is no feature or either arm has fewer than :data:`RANDOMNESS_MIN_ARM_ROWS` rows. At most
    :data:`RANDOMNESS_SAMPLE_ROWS` rows are used, sampled per arm with `np.random.default_rng(seed)`.
    """
    import numpy as np
    from lightgbm import LGBMClassifier
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import StratifiedKFold

    treatment = np.asarray(t, dtype=np.int_)
    if features.shape[1] == 0:
        return None
    if min(int((treatment == 1).sum()), int((treatment == 0).sum())) < RANDOMNESS_MIN_ARM_ROWS:
        return None
    positions = np.arange(len(treatment))
    if len(positions) > RANDOMNESS_SAMPLE_ROWS:
        rng = np.random.default_rng(seed)
        share = RANDOMNESS_SAMPLE_ROWS / len(positions)
        picked = [
            rng.permutation(np.flatnonzero(treatment == arm))[: round(share * int((treatment == arm).sum()))]
            for arm in (0, 1)
        ]
        positions = np.sort(np.concatenate(picked))
    x = features.iloc[positions].reset_index(drop=True)
    y = treatment[positions]
    oof = np.zeros(len(y), dtype=np.float64)
    gains = np.zeros(x.shape[1], dtype=np.float64)
    folds = StratifiedKFold(n_splits=RANDOMNESS_FOLDS, shuffle=True, random_state=seed)
    for train_idx, test_idx in folds.split(np.zeros(len(y)), y):
        model: Any = LGBMClassifier(random_state=seed, **_RANDOMNESS_PARAMS)
        model.fit(x.iloc[train_idx], y[train_idx])
        oof[test_idx] = np.asarray(model.predict_proba(x.iloc[test_idx]))[:, 1]
        gains += np.asarray(model.feature_importances_, dtype=np.float64)
    auc = float(roc_auc_score(y, oof))
    order = sorted(range(x.shape[1]), key=lambda i: (-gains[i], i))
    total = float(gains.sum())
    signals = tuple(
        str(x.columns[i]) for i in order if total > 0.0 and gains[i] >= SIGNAL_MIN_GAIN_SHARE * total
    )[:TOP_SIGNALS]
    return auc, signals, len(y)


def _not_random(
    column: str, auc: float, signals: Sequence[str], rows_used: int, threshold: float, acknowledged: bool
) -> UpliftCheck:
    if not signals:
        strongest = ""
    elif len(signals) == 1:
        strongest = f" The strongest sign was {_join(signals)}."
    else:
        strongest = f" The strongest signs were {_join(signals)}."
    return _finding(
        "TREATMENT_NOT_RANDOM",
        Severity.ERROR,
        f"Who was treated can be predicted from the customers' own data (AUC {auc:.2f}, where a "
        f"random assignment scores about 0.50 and the limit is {threshold:.2f}).{strongest} The "
        "campaign looks targeted, so comparing treated with untreated customers would mix what the "
        "campaign changed with how the chosen customers already differed.",
        "Use data from a campaign with a randomly chosen hold-out group. If you go ahead anyway, "
        "every uplift result will be labelled not causal.",
        column=column,
        details={
            "auc": round(auc, 4),
            "threshold": threshold,
            "folds": RANDOMNESS_FOLDS,
            "rows_used": rows_used,
            "top_features": list(signals),
            "acknowledge": "TREATMENT_NOT_RANDOM",
        },
        acknowledgeable=True,
        acknowledged=acknowledged,
    )


def _treatment_dates(frame: pd.DataFrame, column: str) -> pd.Series:
    import pandas as pd

    return pd.to_datetime(frame[column], errors="coerce", utc=True, format="mixed")


def _maturity(
    frame: pd.DataFrame, column: str, window_days: int, now: datetime
) -> tuple[BoolArray, UpliftCheck | None, int]:
    """`(keep mask, finding or None, rows dropped)` for the outcome-window check."""
    import numpy as np
    import pandas as pd

    dates = _treatment_dates(frame, column)
    undated = dates.isna().to_numpy()
    matures_at = dates + pd.Timedelta(days=window_days)
    immature = (matures_at > pd.Timestamp(now)).fillna(value=False).to_numpy(dtype=bool) & ~undated
    keep = np.asarray(~(immature | undated), dtype=np.bool_)
    n_immature = int(immature.sum())
    n_undated = int(undated.sum())
    dropped = n_immature + n_undated
    if dropped == 0:
        return keep, None, 0
    complete_on = matures_at[immature].max().date().isoformat() if n_immature else None
    parts: list[str] = []
    if n_immature:
        parts.append(
            f"{_n(n_immature)} customers were treated less than {window_days} days before "
            f"{now.date().isoformat()}, so their outcome is not final yet."
        )
    if n_undated:
        parts.append(
            f"{_n(n_undated)} rows have no readable date in '{column}', so their outcome cannot be shown to be final."
        )
    parts.append("They are left out of training and evaluation.")
    suggestion = (
        f"Nothing to fix now. Re-run on or after {complete_on} to include every customer."
        if complete_on is not None
        else f"Fill in '{column}' for every customer to include them."
    )
    finding = _finding(
        "OUTCOME_WINDOW_IMMATURE",
        Severity.WARNING,
        " ".join(parts),
        suggestion,
        column=column,
        details={
            "rows_immature": n_immature,
            "rows_undated": n_undated,
            "rows_dropped": dropped,
            "outcome_window_days": window_days,
            "results_complete_on": complete_on,
            "as_of": now.isoformat(),
        },
    )
    return keep, finding, dropped


def _date_column_missing(column: str) -> UpliftCheck:
    return _finding(
        "OUTCOME_WINDOW_IMMATURE",
        Severity.WARNING,
        f"The treatment date column '{column}' is not in this file, so it cannot be checked whether "
        "every customer's outcome is final.",
        "Add the treatment date to the file, or clear the treatment date setting if outcomes are "
        "already final.",
        column=column,
        details={"treatment_date_column": column, "column_missing": True},
    )


def _features_after_treatment(
    frame: pd.DataFrame, date_column: str, columns: Sequence[str]
) -> list[UpliftCheck]:
    """One finding per date-like candidate column later (by calendar day) than the treatment date."""
    import pandas as pd

    treated_on = _treatment_dates(frame, date_column).dt.normalize()
    findings: list[UpliftCheck] = []
    for name in columns:
        values = pd.to_datetime(frame[name], errors="coerce", utc=True, format="mixed").dt.normalize()
        both = values.notna() & treated_on.notna()
        later = int((values[both] > treated_on[both]).sum())
        if later == 0:
            continue
        compared = int(both.sum())
        findings.append(
            _finding(
                "FEATURE_AFTER_TREATMENT",
                Severity.ERROR,
                f"'{name}' is later than the treatment date in {_n(later)} of {_n(compared)} rows, so "
                "this data was captured after the campaign reached customers. Other columns may "
                "already show what the campaign changed, which would make its effect look larger or "
                "smaller than it was.",
                "Upload customer data as it was before the treatment date. If this column only "
                "records the outcome, exclude it in Data preparation.",
                column=name,
                details={
                    "rows_after_treatment": later,
                    "rows_compared": compared,
                    "treatment_date_column": date_column,
                },
            )
        )
    return findings


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def _is_acknowledged(code: str, column: str | None, acknowledged: frozenset[str]) -> bool:
    if code in acknowledged:
        return True
    return column is not None and f"{code}:{column}" in acknowledged


def run_uplift_checks(
    frame: pd.DataFrame,
    config: UseCaseConfig,
    *,
    primary_key: str,
    target: str,
    upload_id: str,
    run_id: str | None = None,
    acknowledged: Iterable[str] = (),
    now: datetime | None = None,
    seed: int = 0,
) -> UpliftCheckResult:
    """Run the six plan B §4 checks and return `uplift_validation.json` plus the rows to keep.

    `acknowledged` is merged with `config.validation.acknowledged`; only `TREATMENT_NOT_RANDOM` can be
    acknowledged. `passed` is true when no error is left unacknowledged. `causal` is false whenever
    `TREATMENT_NOT_RANDOM` fired - acknowledged, it lets the run go ahead labelled not causal;
    unacknowledged, it blocks the run anyway.
    """
    import numpy as np

    started = time.perf_counter()
    moment = now or utc_now()
    wanted = frozenset(acknowledged) | frozenset(config.validation.acknowledged)
    uplift = config.uplift
    rows = len(frame)
    findings: list[UpliftCheck] = []
    keep: BoolArray | None = None
    rows_immature = 0
    randomness_auc: float | None = None
    causal = True

    # OUTCOME_WINDOW_IMMATURE first in time: the arm sizes are counted on the rows it keeps.
    date_column = uplift.treatment_date_column
    date_present = date_column is not None and date_column in frame.columns
    maturity: UpliftCheck | None = None
    if date_column is not None and uplift.outcome_window_days is not None:
        if date_present:
            keep, maturity, rows_immature = _maturity(frame, date_column, uplift.outcome_window_days, moment)
        else:
            maturity = _date_column_missing(date_column)

    treatment_column = detect_treatment_column([str(c) for c in frame.columns], uplift)
    if treatment_column is None:
        findings.append(_treatment_missing(config))
    else:
        t_all, bad = coerce_treatment(frame[treatment_column])
        if t_all is None:
            findings.append(_treatment_not_binary(treatment_column, frame[treatment_column], bad))
        else:
            mask = keep if keep is not None else np.ones(rows, dtype=np.bool_)
            t = t_all[mask]
            y: IntArray | None = None
            if target in frame.columns:
                try:
                    y_all, _ = coerce_outcome(frame[target], config.target.positive_label)
                    y = y_all[mask]
                except ValueError:
                    y = None  # Phase 1's TARGET_* checks report an unusable outcome.
            too_small = _arm_too_small(
                target=target,
                treated_rows=int((t == 1).sum()),
                control_rows=int((t == 0).sum()),
                treated_positives=None if y is None else int(y[t == 1].sum()),
                control_positives=None if y is None else int(y[t == 0].sum()),
                min_rows=uplift.min_arm_rows,
                min_positives=uplift.min_arm_positives,
                rows_excluded=rows_immature,
            )
            if too_small is not None:
                findings.append(too_small)
            kept = frame.loc[mask]
            spec = fit_feature_spec(
                kept, config, primary_key=primary_key, target=target, treatment_column=treatment_column
            )
            measured = treatment_predictability(apply_feature_spec(kept, spec), t, seed=seed)
            if measured is not None:
                auc, signals, rows_used = measured
                randomness_auc = round(auc, 4)
                if auc > uplift.randomness_auc_max:
                    causal = False
                    findings.append(
                        _not_random(
                            treatment_column,
                            auc,
                            signals,
                            rows_used,
                            uplift.randomness_auc_max,
                            _is_acknowledged("TREATMENT_NOT_RANDOM", treatment_column, wanted),
                        )
                    )

    if maturity is not None:
        findings.append(maturity)

    if date_column is not None and date_present:
        reserved = reserved_feature_exclusions(
            config, primary_key=primary_key, target=target, treatment_column=treatment_column
        )
        candidates = [str(c) for c in frame.columns if str(c) not in reserved]
        findings.extend(_features_after_treatment(frame, date_column, date_like_columns(frame, candidates)))

    ordered = sorted(findings, key=lambda item: (_SEVERITY_RANK[item.severity], CHECK_ORDER.index(item.code)))
    passed = not any(item.severity is Severity.ERROR and not item.acknowledged for item in ordered)
    report = UpliftValidationReport(
        run_id=run_id,
        upload_id=upload_id,
        treatment_column=treatment_column,
        checks=tuple(ordered),
        passed=passed,
        causal=causal,
        randomness_auc=randomness_auc,
        rows_checked=rows,
        rows_immature=rows_immature,
        checked_at=moment,
    )
    log_stage(_LOGGER, "uplift_checks", rows=rows, seconds=time.perf_counter() - started)
    _LOGGER.info(
        "uplift_checks findings=%d errors=%d passed=%s causal=%s",
        len(ordered),
        sum(1 for item in ordered if item.severity is Severity.ERROR),
        passed,
        causal,
    )
    return UpliftCheckResult(report=report, keep=keep)
