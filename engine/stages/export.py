"""Export stage (M4): the downloadable score files and the scoring summary.

Plan section 6.3, `export`: write `scores.csv` and `scores.parquet`, then `scoring_summary.json`
with the rows scored, the per-band and per-action counts, the suppression counts, the drift summary
and the configured KPI. Everything is written through the `Storage` protocol; this module never
touches a path itself, so the same code writes to S3 in Phase 4.

**The two files hold the same table.** Its header is `engine.contracts.scores_csv_columns` exactly -
primary key, score, band, action, one column per configured reason slot, suppression reason, control
flag - in that order, for both formats, whatever order the scored frame happens to carry. The CSV is
UTF-8 with no BOM and `\\n` line endings; an absent suppression reason is an empty cell and the
control flag is `True`/`False`, which pandas reads back as a boolean. Scores are written at full
precision: the file is a data deliverable that joins back to the source system, and only the numbers
destined for the Output page are rounded here (DEC-A5).

**Reason columns.** The scored frame carries one object column per reason slot, `reason_1` ..
`reason_n`, each holding an `engine.contracts.Reason` (or a mapping that validates as one) or a null
for a row with fewer reasons. The files store `Reason.text`, the ready-to-render sentence, while
`ScoringSummary.sample_rows` keeps the whole `Reason`, so the Output page's "Prediction output
(sample rows)" table renders key, score, band pill, top reason and action without reading the CSV.
A reason column missing altogether means the explain stage did not run: the cells are left empty and
no reason is invented (plan section 13.3). A cell holding something that is neither a `Reason`, a
mapping nor a null is a bug in the producing stage and raises `ValueError` rather than being
silently dropped - a bare string could fill the CSV but could never fill `sample_rows`, and half a
reason is worse than none.

**Suppression counts.** One entry per suppression rule that actually ran, including rules that
matched nothing (`rows: 0`), and no entry for a rule skipped because its column was absent from the
scoring file (`SUPPRESSION_COLUMN_MISSING`, DEC-030). See `engine.stages.actions` for the precedence
that decides which rule a multiply-suppressed row is counted under; the counts therefore add up to
the number of suppressed rows.

**KPI.** `output.kpi.formula` is parsed by `engine.config.KpiFormula`; this module evaluates the
three parsed forms with plain pandas and no `eval`, `exec` or `getattr` dispatch:

* `count_rows()` - every scored row, suppressed rows included.
* `count_where_band_in([...])` - rows whose band is one of the named bands. A band with no rows
  contributes nothing and the KPI is `0`; that is a real answer, not a missing one.
* `sum_where_band_in("col", [...])` - the total of `col` over those rows. Values that are not
  numbers contribute nothing.

A total is read from `kpi_source`, **the rows as they were uploaded**, joined to the scored rows on
the primary key - never from the scored frame itself. The scored frame holds *prepared* values: a
column that is also a model feature has been clipped to the training percentiles and filled with
training statistics, and a column prepare dropped is not there at all. Totalling clipped revenue and
printing it as revenue would be an invented number (plan section 13.3), and a KPI over a dropped
column would end an otherwise good run in a crash. So a sum KPI needs the uploaded rows, and
`summarise` refuses the formula rather than guess when they were not passed; the two counting forms
need nothing but the scored frame and are unaffected.

`KpiValue.display` is what the tile prints: `engine.utils.text.humanise_count` for the two counting
forms, and for a total the same humanised form from a thousand upwards and two decimals below it.

**Rounding.** Percentages are percentages (`share_pct`, one decimal); the score mean and median are
rounded to four decimals and sample-row scores to four, all at this producing end, so the UI only
renders (plan section 2.1, principle 2).

`pandas` and `pyarrow` are imported inside the function bodies, never at module level, so
`import engine` stays fast.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Final, Literal

from engine import keys
from engine.contracts import (
    ActionCount,
    BandCount,
    KpiValue,
    Reason,
    ScoreRow,
    ScoringSummary,
    SuppressionCount,
    scores_csv_columns,
)
from engine.stages.actions import (
    ACTION_COLUMN,
    APPLIED_RULES_ATTR,
    BAND_COLUMN,
    CONTROL_GROUP_COLUMN,
    SUPPRESSED_REASON_COLUMN,
    suppression_rules,
)
from engine.storage import run_key
from engine.utils.logging import get_logger, log_stage
from engine.utils.text import humanise_count

if TYPE_CHECKING:
    from datetime import datetime

    import pandas as pd

    from engine.config import PrimaryKey, UseCaseConfig
    from engine.contracts import DriftReport
    from engine.storage import Storage

__all__ = ["SAMPLE_ROWS", "SCORES_CSV", "SCORES_PARQUET", "summarise", "write_scores"]

_LOGGER = get_logger(__name__)

SCORES_CSV: Final[str] = "scores.csv"
SCORES_PARQUET: Final[str] = "scores.parquet"

SAMPLE_ROWS: Final[int] = 10
"""How many leading rows `ScoringSummary.sample_rows` carries for the Output page table."""

_REASON_CODES: Final[tuple[Literal["consent_false", "opted_out", "recently_contacted"], ...]] = (
    "consent_false",
    "opted_out",
    "recently_contacted",
)

_SHARE_DECIMALS: Final[int] = 1
_SCORE_DECIMALS: Final[int] = 4
_AMOUNT_DECIMALS: Final[int] = 2


def write_scores(
    frame: pd.DataFrame,
    config: UseCaseConfig,
    *,
    run_id: str,
    primary_key: PrimaryKey,
    storage: Storage,
    key_source: pd.DataFrame | None = None,
) -> dict[str, str]:
    """Write `scores.csv` and `scores.parquet` and return the artefact filename to storage key map.

    `frame` is the output of `engine.stages.actions.apply_actions`, optionally with reason columns.
    Both files carry `scores_csv_columns(config, primary_key)`, in that order and nothing else.

    A composite key is written as its original columns, each as the text the client uploaded, read
    from `key_source` - the rows as uploaded, carrying the run's row key - by row key, so a replayed
    transform can never change an id that has to join back to the client's systems (DEC-083).
    """
    import io

    import pyarrow  # noqa: F401  # imported here, not at module level, so `import engine` stays fast

    started = time.perf_counter()
    table = _output_frame(frame, config, primary_key=primary_key, key_source=key_source)

    csv_key = run_key(run_id, SCORES_CSV)
    storage.write_bytes(csv_key, table.to_csv(index=False, lineterminator="\n").encode("utf-8"))

    buffer = io.BytesIO()
    table.to_parquet(buffer, engine="pyarrow", index=False)
    parquet_key = run_key(run_id, SCORES_PARQUET)
    storage.write_bytes(parquet_key, buffer.getvalue())

    log_stage(_LOGGER, "export", rows=len(table), seconds=time.perf_counter() - started)
    return {SCORES_CSV: csv_key, SCORES_PARQUET: parquet_key}


def summarise(
    frame: pd.DataFrame,
    config: UseCaseConfig,
    *,
    run_id: str,
    model_version_id: str,
    model_display_name: str,
    primary_key: str,
    drift: DriftReport | None,
    files: Mapping[str, str],
    kpi_source: pd.DataFrame | None = None,
    rows_with_fallback_reasons: int = 0,
    scored_at: datetime | None = None,
) -> ScoringSummary:
    """Counts per band, per action and per suppression reason, plus the configured KPI.

    `primary_key`, `model_display_name` and `files` are values only the caller knows - the chosen
    key column, the registry record and the map :func:`write_scores` returned - and none of them may
    be invented here, so all three are required (see the report on this stage's signatures).

    `kpi_source` is the uploaded rows, before prepare replayed its transforms: a `sum_where_band_in`
    KPI is totalled from them, joined on `primary_key`, so the tile prints the real values rather
    than clipped ones and a column prepare dropped is still available. A configuration whose KPI
    counts rather than totals needs no source. `scored_at` defaults to the current UTC time.

    `rows_with_fallback_reasons` is another value only the caller holds: the explain stage counted
    it while it ran, and nothing in the scored frame records which tier explained a row, so this
    stage takes the number rather than guessing at it (DEC-056).
    """
    import pandas as pd

    from engine.utils.time import utc_now

    _require_columns(frame, (primary_key, config.actions.score_field, *_action_columns()))
    rows = len(frame)
    scores = pd.to_numeric(frame[config.actions.score_field], errors="coerce")
    return ScoringSummary(
        run_id=run_id,
        model_version_id=model_version_id,
        model_display_name=model_display_name,
        rows_scored=rows,
        score_field=config.actions.score_field,
        score_mean=_mean(scores),
        score_median=_median(scores),
        bands=_band_counts(frame, config),
        actions=_action_counts(frame),
        suppressed=_suppression_counts(frame, config),
        control_group_rows=int(frame[CONTROL_GROUP_COLUMN].astype(bool).sum()),
        rows_with_fallback_reasons=rows_with_fallback_reasons,
        kpi=_evaluate_kpi(frame, config, kpi_source=kpi_source, primary_key=primary_key),
        drift_status=None if drift is None else drift.status,
        drift_max_psi=None if drift is None else drift.max_psi,
        drift_summary=None if drift is None else drift.summary,
        files=dict(files),
        sample_rows=_sample_rows(frame, config, primary_key=primary_key),
        scored_at=utc_now() if scored_at is None else scored_at,
    )


# ---------------------------------------------------------------------------
# The exported table
# ---------------------------------------------------------------------------
def _key_output(
    frame: pd.DataFrame, primary_key: PrimaryKey, *, key_source: pd.DataFrame | None
) -> dict[str, pd.Series]:
    """The key column(s) of the exported table: the column itself, or each part of a composite key."""
    import pandas as pd

    if not keys.is_composite(primary_key):
        column = keys.row_key_column(primary_key)
        return {column: frame[column]}
    columns = keys.key_columns(primary_key)
    source = key_source if key_source is not None and keys.ROW_KEY_COLUMN in key_source.columns else frame
    _require_columns(source, (keys.ROW_KEY_COLUMN, *columns))
    lookup = pd.DataFrame(
        {name: keys.key_text(source[name]).to_numpy(dtype=object) for name in columns},
        index=source[keys.ROW_KEY_COLUMN].astype(str).to_numpy(),
    )
    lookup = lookup[~lookup.index.duplicated(keep="first")]
    rows = frame[keys.ROW_KEY_COLUMN].astype(str).to_numpy()
    aligned = lookup.reindex(rows)
    return {
        name: pd.Series(aligned[name].to_numpy(dtype=object), index=frame.index, dtype="object")
        for name in columns
    }


def _output_frame(
    frame: pd.DataFrame,
    config: UseCaseConfig,
    *,
    primary_key: PrimaryKey,
    key_source: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """The scored frame reduced to `scores_csv_columns(config, primary_key)`, in that order."""
    import pandas as pd

    columns = scores_csv_columns(config, primary_key)
    row_key = keys.row_key_column(primary_key)
    _require_columns(frame, (row_key, config.actions.score_field, *_action_columns()))
    reason_slots = config.evaluation.reasons_per_row
    data: dict[str, pd.Series] = {
        **_key_output(frame, primary_key, key_source=key_source),
        config.actions.score_field: pd.to_numeric(frame[config.actions.score_field], errors="coerce"),
        BAND_COLUMN: frame[BAND_COLUMN].astype("object"),
        ACTION_COLUMN: frame[ACTION_COLUMN].astype("object"),
    }
    for slot in range(1, reason_slots + 1):
        name = f"reason_{slot}"
        reasons = _reason_column(frame, name)
        data[name] = pd.Series(
            [None if reason is None else reason.text for reason in reasons],
            index=frame.index,
            dtype="object",
        )
    data[SUPPRESSED_REASON_COLUMN] = (
        frame[SUPPRESSED_REASON_COLUMN]
        .astype("object")
        .where(frame[SUPPRESSED_REASON_COLUMN].notna(), other=None)
    )
    data[CONTROL_GROUP_COLUMN] = frame[CONTROL_GROUP_COLUMN].astype(bool)
    return pd.DataFrame(data, columns=list(columns))


def _reason_column(frame: pd.DataFrame, name: str) -> list[Reason | None]:
    """The `Reason` of every row for one reason slot; an absent column is a column of nulls."""
    import pandas as pd

    if name not in frame.columns:
        return [None] * len(frame)
    parsed: list[Reason | None] = []
    for cell in frame[name].to_numpy():
        if isinstance(cell, Reason):
            parsed.append(cell)
        elif isinstance(cell, Mapping):
            parsed.append(Reason.model_validate(dict(cell)))
        elif cell is None or (pd.api.types.is_scalar(cell) and pd.isna(cell)):
            parsed.append(None)
        else:
            raise ValueError(
                f"Column {name!r} holds a {type(cell).__name__}; a reason column holds Reason objects, "
                f"mappings that validate as one, or nulls."
            )
    return parsed


def _action_columns() -> tuple[str, ...]:
    """The columns `apply_actions` must have added before anything can be exported."""
    return (BAND_COLUMN, ACTION_COLUMN, SUPPRESSED_REASON_COLUMN, CONTROL_GROUP_COLUMN)


def _require_columns(frame: pd.DataFrame, columns: tuple[str, ...]) -> None:
    missing = [name for name in columns if name not in frame.columns]
    if missing:
        raise ValueError(
            f"The scored frame is missing {', '.join(repr(name) for name in missing)}; "
            f"run engine.stages.actions.apply_actions before exporting."
        )


# ---------------------------------------------------------------------------
# The summary
# ---------------------------------------------------------------------------
def _band_counts(frame: pd.DataFrame, config: UseCaseConfig) -> tuple[BandCount, ...]:
    """One entry per configured band, in configured order, zero-row bands included."""
    counts = frame[BAND_COLUMN].value_counts()
    rows = len(frame)
    return tuple(
        BandCount(
            name=band.name,
            action=band.action,
            min_score=band.min_score,
            rows=int(counts.get(band.name, 0)),
            share_pct=_share_pct(int(counts.get(band.name, 0)), rows),
        )
        for band in config.actions.bands
    )


def _action_counts(frame: pd.DataFrame) -> tuple[ActionCount, ...]:
    """One entry per action that occurred, largest first, ties broken by action name."""
    counts = frame[ACTION_COLUMN].value_counts()
    rows = len(frame)
    pairs = [(str(action), int(n)) for action, n in counts.items()]
    pairs.sort(key=lambda item: (-item[1], item[0]))
    return tuple(ActionCount(action=action, rows=n, share_pct=_share_pct(n, rows)) for action, n in pairs)


def _suppression_counts(frame: pd.DataFrame, config: UseCaseConfig) -> tuple[SuppressionCount, ...]:
    """One entry per suppression rule that ran, in precedence order; skipped rules are left out."""
    recorded = frame.attrs.get(APPLIED_RULES_ATTR)
    if isinstance(recorded, tuple | list):
        applied = [str(code) for code in recorded]
    else:
        # The frame did not come from apply_actions (or lost its attrs): fall back to the rules whose
        # column the frame still carries, which is the same set apply_actions would have run.
        applied = [code for code, column in suppression_rules(config) if column in frame.columns]
    counts = frame[SUPPRESSED_REASON_COLUMN].value_counts()
    return tuple(
        SuppressionCount(reason=code, rows=int(counts.get(code, 0)))
        for code in _REASON_CODES  # precedence order, and the only codes the contract allows
        if code in applied
    )


def _sample_rows(frame: pd.DataFrame, config: UseCaseConfig, *, primary_key: str) -> tuple[ScoreRow, ...]:
    """The first ten scored rows, whole `Reason`s included, for the Output page's sample table."""
    import pandas as pd

    head = frame.head(SAMPLE_ROWS)
    keys = head[primary_key].to_numpy()
    scores = pd.to_numeric(head[config.actions.score_field], errors="coerce").to_numpy()
    reasons_by_slot = [
        _reason_column(head, f"reason_{slot}") for slot in range(1, config.evaluation.reasons_per_row + 1)
    ]
    rows: list[ScoreRow] = []
    for position in range(len(head)):
        suppressed = head[SUPPRESSED_REASON_COLUMN].to_numpy()[position]
        reasons = tuple(
            reason for reason in (slot[position] for slot in reasons_by_slot) if reason is not None
        )
        rows.append(
            ScoreRow(
                primary_key="" if pd.isna(keys[position]) else str(keys[position]),
                score=round(float(scores[position]), _SCORE_DECIMALS),
                band=str(head[BAND_COLUMN].to_numpy()[position]),
                action=str(head[ACTION_COLUMN].to_numpy()[position]),
                reasons=reasons,
                suppressed_reason=None if pd.isna(suppressed) else str(suppressed),
                control_group=bool(head[CONTROL_GROUP_COLUMN].to_numpy()[position]),
            )
        )
    return tuple(rows)


def _share_pct(rows: int, total: int) -> float:
    """`rows` as a percentage of `total`, one decimal; an empty file is zero, not a division error."""
    if total <= 0:
        return 0.0
    return round(100.0 * rows / total, _SHARE_DECIMALS)


def _mean(scores: pd.Series) -> float:
    return 0.0 if scores.empty else round(float(scores.mean()), _SCORE_DECIMALS)


def _median(scores: pd.Series) -> float:
    return 0.0 if scores.empty else round(float(scores.median()), _SCORE_DECIMALS)


# ---------------------------------------------------------------------------
# The KPI
# ---------------------------------------------------------------------------
def _evaluate_kpi(
    frame: pd.DataFrame,
    config: UseCaseConfig,
    *,
    kpi_source: pd.DataFrame | None = None,
    primary_key: str,
) -> KpiValue:
    """Evaluate the parsed `output.kpi.formula` over the banded frame. No `eval`, no `exec`."""
    kpi = config.output.kpi
    parsed = kpi.parsed
    if parsed.fn == "count_rows":
        count = len(frame)
        return KpiValue(
            label=kpi.label, formula=kpi.formula, value=float(count), display=humanise_count(count)
        )

    _require_columns(frame, (BAND_COLUMN,))
    selected = frame[BAND_COLUMN].isin(list(parsed.bands))
    if parsed.fn == "count_where_band_in":
        count = int(selected.sum())
        return KpiValue(
            label=kpi.label, formula=kpi.formula, value=float(count), display=humanise_count(count)
        )

    column = parsed.column
    if column is None:
        raise ValueError(f"The KPI {kpi.formula!r} names no column to sum.")
    values = _uploaded_values(
        frame, kpi_source, column=column, primary_key=primary_key, formula=kpi.formula, selected=selected
    )
    total = float(values[selected].sum())
    return KpiValue(
        label=kpi.label,
        formula=kpi.formula,
        value=round(total, _AMOUNT_DECIMALS),
        display=_format_amount(total),
    )


def _uploaded_values(
    frame: pd.DataFrame,
    kpi_source: pd.DataFrame | None,
    *,
    column: str,
    primary_key: str,
    formula: str,
    selected: pd.Series,
) -> pd.Series:
    """One KPI column as it was uploaded, joined onto the scored rows by primary key.

    The scored frame is not consulted for a total, even when it carries the column: its values are
    the replayed ones, which prepare may have clipped or filled for the model. Rows whose uploaded
    value is not a number contribute nothing, as the module docstring says; rows the uploaded frame
    does not have at all are refused, because a headline total silently missing rows is worse than a
    run that says what is wrong.
    """
    import pandas as pd

    if kpi_source is None:
        raise ValueError(
            f"The KPI {formula!r} totals {column!r}, so it needs the rows as uploaded: pass "
            f"kpi_source to summarise(). The scored frame carries the prepared values, which prepare "
            f"may have clipped or filled, and a total of those is not the total the tile claims."
        )
    missing = [name for name in (primary_key, column) if name not in kpi_source.columns]
    if missing:
        raise ValueError(
            f"The KPI {formula!r} totals {column!r} over the uploaded rows, which carry no "
            f"{', '.join(repr(name) for name in missing)}."
        )
    numbers = pd.to_numeric(kpi_source[column], errors="coerce").tolist()
    lookup: dict[str, float] = {}
    for key, value in zip(kpi_source[primary_key].tolist(), numbers, strict=True):
        text = str(key)
        if text in lookup:
            raise ValueError(
                f"The uploaded rows carry the primary key {text!r} more than once, so the KPI "
                f"{formula!r} cannot be joined onto the scored rows by key."
            )
        lookup[text] = float(value)
    keys = [str(value) for value in frame[primary_key].tolist()]
    unmatched = [key for key, keep in zip(keys, selected.tolist(), strict=True) if keep and key not in lookup]
    if unmatched:
        raise ValueError(
            f"{len(unmatched)} of the rows the KPI {formula!r} totals are not in the uploaded rows, "
            f"the first being {unmatched[0]!r}; the total would be short by them."
        )
    return pd.Series([lookup.get(key, float("nan")) for key in keys], index=frame.index, dtype="float64")


def _format_amount(value: float) -> str:
    """A summed KPI as the tile prints it: humanised from a thousand up, two decimals below."""
    if abs(value) >= 1000:
        return humanise_count(round(value))
    text = f"{value:.{_AMOUNT_DECIMALS}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text
