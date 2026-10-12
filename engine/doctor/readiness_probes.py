"""The fifteen X-probes (Plan O M234, DEC-1804; the design §10.2; [RDY] §1.7 and §5).

Each probe closes a gap where `check_plan` - exactly what `POST /runs` validates - passes and the run
then fails, or silently loses rows, at a later step. A probe never guesses at that later step: wherever
it can, it **runs the engine's own code** on the prepared copy, the way the run will:

* `engine.stages.prepare.prepare_rows` - the column drops, the time cast, the consent filter, the
  missing-outcome drop and the de-duplication - gives the rows the split will see;
* `engine.stages.prepare.split_dataset` is then called on those rows, with fixed dry-run ids, so a
  split the run cannot make fails here first, with the engine's own sentence;
* the uplift flow's own `coerce_treatment` / `coerce_arms` read the treatment column.

What the run reads past the profile row cap, and what the CSV reader drops, needs the stored file
itself; `scan_source` streams it once for each (`SourceScan`), with memory bounded by the key column.

Every probe is listed in every report: `skipped`, with a reason, when it does not apply to the use case
or the mode. Messages and details carry counts, shares and column names - never a cell value.
"""

from __future__ import annotations

import csv
import io
import math
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Literal

from engine import keys
from engine.config import GenerativeKind, PrimaryKey, ProblemType, RunMode, SplitType, UseCaseConfig
from engine.doctor.readiness_contracts import (
    PROBE_CODES,
    PROBE_GATES,
    PostFilterCounts,
    ProbeResult,
    ProbeStatus,
    SimulatedPart,
)

if TYPE_CHECKING:
    import pandas as pd

    from engine.storage import Storage

__all__ = [
    "DRY_RUN_IDS",
    "FALSY_TEXT",
    "TRUTHY_TEXT",
    "ProbeRun",
    "SourceScan",
    "label_key",
    "run_probes",
    "scan_source",
]

DRY_RUN_IDS: Final[tuple[str, ...]] = ("readiness-dry-run-1", "readiness-dry-run-2", "readiness-dry-run-3")
"""The run ids the simulated split is seeded from. A random or grouped split depends on the run's own
seed, which does not exist yet, so it is simulated three times and must work every time; a time-based
split does not depend on the seed and is simulated once."""

TRUTHY_TEXT: Final[frozenset[str]] = frozenset({"true", "t", "yes", "y", "1"})
"""What `prepare._truthy` and `actions._truthy` read as consent (`prepare.py:175`, `actions.py:137`)."""
FALSY_TEXT: Final[frozenset[str]] = frozenset({"false", "f", "no", "n", "0"})
"""Spellings that plainly mean no; anything outside both sets is silently read as no consent."""

LABEL_MATURITY_WARN_RATIO: Final[float] = 0.5
"""The newest part's outcome rate below this share of the training rate, significantly, is a warning."""
LABEL_MATURITY_Z: Final[float] = 3.0
VALUE_COLUMN_MAX_MISSING: Final[float] = 0.10
"""Above this share missing, no money figure is shown (`uplift/policy.py:276-330`)."""
LOST_LINES_SHOWN: Final[int] = 20
_CHUNK_ROWS: Final[int] = 100_000


# ---------------------------------------------------------------------------
# Reading the stored file: parser loss and the key past the row cap
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SourceScan:
    """What one streaming pass over the stored file found (`scan_source`)."""

    file_format: Literal["csv", "parquet"]
    rows_parsed: int
    rows_physical: int | None = None
    """Data records in the file, counted by a plain CSV reader; null for Parquet (no parser to lose rows)."""
    lost_lines: tuple[int, ...] = ()
    """Line numbers of records with more fields than the header - the ones pandas drops (first 20)."""
    parse_error: str | None = None
    full_key_rows: int | None = None
    full_key_distinct: int | None = None
    full_key_nulls: int | None = None
    key_error: str | None = None

    @property
    def rows_dropped(self) -> int | None:
        if self.rows_physical is None:
            return None
        return max(0, self.rows_physical - self.rows_parsed)


def scan_source(
    storage: Storage,
    key: str,
    *,
    file_format: Literal["csv", "parquet"],
    rows_parsed: int,
    encoding: str | None = None,
    delimiter: str | None = None,
    key_columns: Sequence[str] = (),
    full_keys: bool = True,
) -> SourceScan:
    """Count the file's records the way a person would, and every key value past the row cap.

    `rows_parsed` is what the engine's reader keeps (`profile.row_count`). For a CSV the records are
    counted with Python's `csv` module, which, unlike `pd.read_csv(on_bad_lines="warn")`
    (`ingest.py:492`), keeps a line with too many fields. `full_keys=False` skips the key pass (the
    caller knows the file is not larger than the rows the checks read).
    """
    physical: int | None = None
    lost: tuple[int, ...] = ()
    parse_error: str | None = None
    if file_format == "csv":
        try:
            physical, lost = _count_records(
                storage, key, encoding=encoding or "utf-8", delimiter=delimiter or ","
            )
        except (csv.Error, UnicodeDecodeError, ValueError) as exc:
            parse_error = type(exc).__name__
    key_rows: int | None = None
    distinct: int | None = None
    nulls: int | None = None
    key_error: str | None = None
    if full_keys and key_columns:
        try:
            key_rows, distinct, nulls = _scan_keys(
                storage,
                key,
                file_format=file_format,
                key_columns=tuple(key_columns),
                encoding=encoding or "utf-8",
                delimiter=delimiter or ",",
            )
        except (KeyError, ValueError, UnicodeDecodeError) as exc:
            key_error = type(exc).__name__
    return SourceScan(
        file_format=file_format,
        rows_parsed=rows_parsed,
        rows_physical=physical,
        lost_lines=lost,
        parse_error=parse_error,
        full_key_rows=key_rows,
        full_key_distinct=distinct,
        full_key_nulls=nulls,
        key_error=key_error,
    )


def _count_records(
    storage: Storage, key: str, *, encoding: str, delimiter: str
) -> tuple[int, tuple[int, ...]]:
    csv.field_size_limit(min(sys.maxsize, 2**31 - 1))
    records = 0
    lost: list[int] = []
    with storage.open_read(key) as raw:
        text = io.TextIOWrapper(raw, encoding=encoding, newline="")
        reader = csv.reader(text, delimiter=delimiter, quotechar='"', doublequote=True, skipinitialspace=True)
        header: list[str] | None = None
        for record in reader:
            if not record:
                continue  # a blank line: pandas skips it too (`skip_blank_lines=True`)
            if header is None:
                header = record
                continue
            records += 1
            if len(record) > len(header) and len(lost) < LOST_LINES_SHOWN:
                lost.append(reader.line_num)
    return records, tuple(lost)


def _scan_keys(
    storage: Storage,
    key: str,
    *,
    file_format: Literal["csv", "parquet"],
    key_columns: tuple[str, ...],
    encoding: str,
    delimiter: str,
) -> tuple[int, int, int]:
    """`(rows, distinct non-empty keys, rows with an empty key part)` over every row of the stored file."""
    import numpy as np
    import pandas as pd

    hashes: list[Any] = []
    rows = 0
    nulls = 0

    def take(chunk: pd.DataFrame) -> None:
        nonlocal rows, nulls
        missing = [name for name in key_columns if name not in chunk.columns]
        if missing:
            raise KeyError(missing[0])
        rows += len(chunk)
        empty = chunk[list(key_columns)].isna().any(axis=1)
        nulls += int(empty.sum())
        joined = keys.key_series(chunk.loc[~empty], list(key_columns))
        hashes.append(pd.util.hash_pandas_object(joined, index=False).to_numpy())

    with storage.open_read(key) as raw:
        if file_format == "parquet":
            take(pd.read_parquet(raw, columns=list(key_columns)))
        else:
            text = io.TextIOWrapper(raw, encoding=encoding, newline="")
            reader = pd.read_csv(
                text,
                sep=delimiter,
                chunksize=_CHUNK_ROWS,
                header=0,
                quotechar='"',
                doublequote=True,
                skipinitialspace=True,
                skip_blank_lines=True,
                keep_default_na=True,
                na_values=[""],
                engine="c",
                on_bad_lines="skip",  # the run's reader drops them too; X_PARSER_ROW_LOSS counts them
                usecols=lambda name: str(name) in key_columns,
            )
            for chunk in reader:
                take(chunk)
    every = np.concatenate(hashes) if hashes else np.array([], dtype=np.uint64)
    return rows, int(np.unique(every).size), nulls


# ---------------------------------------------------------------------------
# Running the probes
# ---------------------------------------------------------------------------
@dataclass
class ProbeRun:
    """Every probe's result, and what the simulated prepare and split found on the way."""

    probes: tuple[ProbeResult, ...]
    post_filter: PostFilterCounts | None
    hidden_columns: tuple[str, ...] = ()
    split_error: str | None = None


@dataclass
class _State:
    """What one probe pass learns, shared by the probes that need it."""

    config: UseCaseConfig
    mode: RunMode
    frame: pd.DataFrame
    key_columns: tuple[str, ...]
    target: str | None
    row_count: int
    scan: SourceScan | None
    results: dict[str, ProbeResult] = field(default_factory=dict)
    rows: pd.DataFrame | None = None
    rows_note: str | None = None
    hidden: tuple[str, ...] = ()
    counts: PostFilterCounts | None = None
    parts: dict[str, pd.DataFrame] | None = None
    split_error: str | None = None
    split_blamed: bool = False
    positive_key: str | None = None

    @property
    def binary(self) -> bool:
        return self.config.problem_type is ProblemType.BINARY_CLASSIFICATION

    @property
    def sampled(self) -> bool:
        return self.row_count > len(self.frame)

    @property
    def trains(self) -> bool:
        """A training file the generic train flow will prepare and split."""
        return (
            self.mode is RunMode.TRAIN
            and self.config.problem_type is not ProblemType.UPLIFT
            and self.config.generative.kind is not GenerativeKind.RAG_ASSISTANT
        )


def run_probes(
    frame: pd.DataFrame,
    config: UseCaseConfig,
    *,
    mode: RunMode,
    primary_key: PrimaryKey | None,
    target: str | None,
    row_count: int,
    scan: SourceScan | None = None,
) -> ProbeRun:
    """Run all fifteen probes on the prepared copy `frame` (capped at the profile row cap) and its scan."""
    key_columns: tuple[str, ...] = ()
    if primary_key is not None:
        key_columns = (primary_key,) if isinstance(primary_key, str) else tuple(primary_key)
    state = _State(
        config=config,
        mode=mode,
        frame=frame,
        key_columns=key_columns,
        target=target,
        row_count=row_count,
        scan=scan,
    )
    _parser_row_loss(state)
    _pk_unique_full(state)
    _consent_vocab(state)
    _simulate_prepare(state)
    _post_filter_thresholds(state)
    _time_all_parse(state)
    _time_cuts_feasible(state)
    _groups_enough(state)
    _simulate_split(state)
    _split_classes(state)
    _label_maturity(state)
    _regression_target_numeric(state)
    _treatment_tokens(state)
    _channel_columns_present(state)
    _value_column_missing_share(state)
    _allowed_fields_filled(state)
    _complaint_column_present(state)
    ordered = tuple(state.results[code] for code in PROBE_CODES)
    return ProbeRun(
        probes=ordered,
        post_filter=state.counts,
        hidden_columns=state.hidden,
        split_error=state.split_error,
    )


def _put(
    state: _State,
    code: str,
    status: ProbeStatus,
    *,
    message: str = "",
    fix: str = "",
    column: str | None = None,
    measured: float | None = None,
    threshold: float | None = None,
    skip_reason: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    clean: dict[str, int | float | str | bool | None] = {}
    for name, value in (details or {}).items():
        if isinstance(value, float) and not math.isfinite(value):
            value = None
        clean[name] = value
    state.results[code] = ProbeResult(
        code=code,
        gate=PROBE_GATES[code],
        status=status,
        column=column,
        measured=None if measured is None or not math.isfinite(measured) else float(measured),
        threshold=threshold,
        message=message,
        fix=fix,
        skip_reason=skip_reason,
        details=clean,
    )


def _skip(state: _State, code: str, reason: str) -> None:
    _put(state, code, "skipped", skip_reason=reason)


def _fmt(count: float) -> str:
    return f"{count:,.0f}"


def label_key(value: object) -> str:
    """One comparable spelling per outcome value, as the split compares them (`prepare._label_key`)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    item = getattr(value, "item", None)
    if callable(item) and not isinstance(value, str):
        return label_key(item())
    return str(value).strip().lower()


# -- the stored file ----------------------------------------------------------
def _parser_row_loss(state: _State) -> None:
    code = "X_PARSER_ROW_LOSS"
    scan = state.scan
    if scan is None:
        _put(
            state,
            code,
            "warn",
            message="The stored file was not read line by line, so lines the reader may drop were not counted.",
            fix="Check the file again with the stored file available.",
            details={"measured": False},
        )
        return
    if scan.file_format == "parquet":
        _put(state, code, "pass", message="A Parquet file has no lines to drop.", measured=0.0, threshold=0.0)
        return
    if scan.parse_error is not None or scan.rows_physical is None:
        _put(
            state,
            code,
            "fail",
            message="The file's lines could not be counted, so lines the reader drops cannot be ruled out.",
            fix="Save the file again as UTF-8 CSV with one record per line, and upload it again.",
            details={"error": scan.parse_error},
        )
        return
    dropped = scan.rows_dropped or 0
    details: dict[str, Any] = {
        "rows_physical": scan.rows_physical,
        "rows_parsed": scan.rows_parsed,
        "rows_dropped": dropped,
        "lines": ", ".join(str(line) for line in scan.lost_lines[:dropped] or scan.lost_lines),
    }
    if dropped:
        where = f" (line {details['lines']})" if scan.lost_lines else ""
        _put(
            state,
            code,
            "fail",
            message=(
                f"{_fmt(dropped)} of {_fmt(scan.rows_physical)} records in the file have more values than "
                f"there are column names, and the reader drops them without saying so{where}."
            ),
            fix="Fix or remove those lines in the export (often a comma inside an unquoted value), then upload again.",
            measured=float(dropped),
            threshold=0.0,
            details=details,
        )
        return
    _put(
        state,
        code,
        "pass",
        message=f"Every one of the {_fmt(scan.rows_physical)} records is read.",
        measured=0.0,
        threshold=0.0,
        details=details,
    )


def _pk_unique_full(state: _State) -> None:
    code = "X_PK_UNIQUE_FULL"
    if not state.key_columns:
        _skip(state, code, "no ID column is chosen yet")
        return
    label = ", ".join(state.key_columns)
    if not state.sampled:
        missing = [name for name in state.key_columns if name not in state.frame.columns]
        if missing:
            _skip(state, code, f"the file has no {missing[0]!r} column (the dry run says so)")
            return
        present = state.frame[list(state.key_columns)].dropna()
        rows = len(present)
        distinct = len(present.drop_duplicates())
        _key_verdict(state, code, label, rows=rows, distinct=distinct, sampled=False)
        return
    scan = state.scan
    if scan is None or scan.full_key_rows is None or scan.full_key_distinct is None:
        reason = scan.key_error if scan is not None else None
        _put(
            state,
            code,
            "fail" if reason else "warn",
            column=label,
            message=(
                f"The file has {_fmt(state.row_count)} rows and the checks read the first {_fmt(len(state.frame))}; "
                "the ID was not checked on the rest."
            ),
            fix="Check the file again with the stored file available, so every row's ID is read.",
            details={"rows": state.row_count, "rows_checked": len(state.frame), "error": reason},
        )
        return
    rows = scan.full_key_rows - (scan.full_key_nulls or 0)
    _key_verdict(state, code, label, rows=rows, distinct=scan.full_key_distinct, sampled=True)


def _key_verdict(state: _State, code: str, label: str, *, rows: int, distinct: int, sampled: bool) -> None:
    repeats = max(0, rows - distinct)
    details = {
        "rows_with_id": rows,
        "distinct_ids": distinct,
        "repeated_rows": repeats,
        "past_the_row_cap": sampled,
    }
    if repeats:
        _put(
            state,
            code,
            "fail",
            column=label,
            message=f"{_fmt(repeats)} rows repeat an ID already seen in the file, so there is more than one row per {state.config.entity}.",
            fix=f"Combine the rows so each {state.config.entity} appears once, or export one row per {state.config.entity}.",
            measured=float(repeats),
            threshold=0.0,
            details=details,
        )
        return
    _put(
        state,
        code,
        "pass",
        column=label,
        message=f"Every ID appears once in all {_fmt(rows)} rows.",
        measured=0.0,
        threshold=0.0,
        details=details,
    )


# -- consent --------------------------------------------------------------------
def _consent_columns(state: _State) -> list[tuple[str, str]]:
    config = state.config
    out: list[tuple[str, str]] = []
    consent = config.governance.consent_column
    if consent is not None and state.mode is RunMode.TRAIN:
        out.append((consent, "kept for training only when it says yes"))
    elif consent is not None:
        out.append((consent, "sent to only when it says yes"))
    suppression = config.actions.suppression
    if suppression.suppress_opted_out and suppression.opt_out_column is not None:
        out.append((suppression.opt_out_column, "contacted only when it says yes"))
    return out


def _unrecognised(series: pd.Series) -> tuple[int, int, int]:
    """`(non-empty values, values read as yes, values that are neither a yes nor a no word)`."""
    import pandas as pd

    present = series.dropna()
    if pd.api.types.is_bool_dtype(present.dtype) or pd.api.types.is_numeric_dtype(present.dtype):
        if pd.api.types.is_bool_dtype(present.dtype):
            yes = int(present.astype(bool).sum())
        else:
            yes = int(present.ne(0).sum())
        return len(present), yes, 0
    text = present.astype("string").str.strip().str.lower()
    yes = int(text.isin(TRUTHY_TEXT).sum())
    unknown = int((~text.isin(TRUTHY_TEXT | FALSY_TEXT)).sum())
    return len(present), yes, unknown


def _consent_vocab(state: _State) -> None:
    code = "X_CONSENT_VOCAB"
    columns = _consent_columns(state)
    if not columns:
        _skip(state, code, "the use case names no consent or opt-in column")
        return
    worst: tuple[str, int, int, int, str] | None = None
    checked = 0
    for column, use in columns:
        if column not in state.frame.columns:
            continue  # CONSENT_COLUMN_MISSING / SUPPRESSION_COLUMN_MISSING already say so
        checked += 1
        present, yes, unknown = _unrecognised(state.frame[column])
        if (unknown or (present and not yes)) and (worst is None or unknown > worst[3]):
            worst = (column, present, yes, unknown, use)
    if checked == 0:
        _skip(state, code, "the consent columns are not in the file (the dry run says so)")
        return
    if worst is None:
        _put(
            state,
            code,
            "pass",
            message="Every consent value reads plainly as yes or no.",
            measured=0.0,
            threshold=0.0,
        )
        return
    column, present, yes, unknown, use = worst
    distinct = 0
    if unknown:
        text = state.frame[column].dropna().astype("string").str.strip().str.lower()
        distinct = int(text[~text.isin(TRUTHY_TEXT | FALSY_TEXT)].nunique())
    _put(
        state,
        code,
        "fail",
        column=column,
        message=(
            f"{_fmt(unknown)} of {_fmt(present)} values in '{column}' ({distinct} spelling(s)) are not a yes or no "
            f"the engine reads, and {_fmt(yes)} read as yes. A {state.config.entity} is {use}, so every other "
            "row would be left out without a word."
        ),
        fix=f"Say which values of '{column}' mean yes (for example map them to true/false), then check again.",
        measured=float(unknown),
        threshold=0.0,
        details={
            "values": present,
            "read_as_yes": yes,
            "not_understood": unknown,
            "spellings_not_understood": distinct,
        },
    )


# -- the prepare stage, simulated ---------------------------------------------
def _simulate_prepare(state: _State) -> None:
    from engine.stages import prepare

    if not state.trains:
        return
    if not state.key_columns or not state.target or state.target not in state.frame.columns:
        state.rows_note = "the ID column and the outcome must be chosen first"
        return
    missing = [name for name in state.key_columns if name not in state.frame.columns]
    if missing:
        state.rows_note = f"the file has no {missing[0]!r} column"
        return
    framed = keys.with_row_key(state.frame, list(state.key_columns))
    carried = (*state.key_columns, keys.ROW_KEY_COLUMN) if len(state.key_columns) > 1 else state.key_columns
    try:
        rows, plan = prepare.prepare_rows(framed, state.config, primary_key=carried, target=state.target)
    except (ValueError, KeyError, TypeError) as exc:
        state.rows_note = f"the prepare step stopped: {exc}"
        return
    state.rows = rows
    state.hidden = tuple(column.name for column in plan.dropped_columns) + tuple(
        column.name for column in plan.carried_columns
    )
    removed = {removal.reason: removal.rows for removal in plan.row_removals}
    rows_in = plan.rows_in
    after_consent = rows_in - removed.get("consent_false", 0)
    after_target = after_consent - removed.get("missing_target", 0)
    after_dedupe = after_target - removed.get("duplicate", 0)
    scale = state.row_count / len(state.frame) if state.sampled and len(state.frame) else 1.0
    positives: int | None = None
    if state.binary:
        state.positive_key = _positive_key(rows, state)
        positives = _count_positives(rows, state)
    state.counts = PostFilterCounts(
        rows_in=round(rows_in * scale),
        rows_after_consent=round(after_consent * scale),
        rows_after_missing_target=round(after_target * scale),
        rows_after_dedupe=round(after_dedupe * scale),
        rows_after_filters=round(len(rows) * scale),
        positives_after_filters=None if positives is None else round(positives * scale),
        estimated=state.sampled,
    )


def _positive_key(rows: pd.DataFrame, state: _State) -> str | None:
    """The positive outcome value, chosen the way `TARGET_TOO_FEW_POSITIVES` chooses it
    (`validate.resolve_positive_label`: configured, else a known word, else the rarer value)."""
    from engine.stages import validate

    params = validate.params_from_config(state.config, target=state.target)
    label, _positives, _negatives = validate.resolve_positive_label(rows, params)
    return None if label is None else label_key(label)


def _count_positives(part: pd.DataFrame, state: _State) -> int | None:
    if state.positive_key is None:
        return None
    return int(part[str(state.target)].dropna().map(label_key).eq(state.positive_key).sum())


def _post_filter_thresholds(state: _State) -> None:
    code = "X_POST_FILTER_THRESHOLDS"
    if not state.trains:
        _skip(state, code, _not_trained_reason(state))
        return
    counts = state.counts
    if counts is None:
        _skip(state, code, state.rows_note or "the rows could not be prepared")
        return
    validation = state.config.validation
    rows = counts.rows_after_filters
    positives = counts.positives_after_filters
    short_rows = rows < validation.min_rows
    short_positives = positives is not None and positives < validation.min_positive
    details: dict[str, Any] = {
        "rows_in": counts.rows_in,
        "rows_after_filters": rows,
        "min_rows": validation.min_rows,
        "positives_after_filters": positives,
        "min_positive": validation.min_positive if state.binary else None,
        "estimated": counts.estimated,
    }
    removed = counts.rows_in - rows
    if short_rows or short_positives:
        parts = []
        if short_rows:
            parts.append(f"{_fmt(rows)} rows ({_fmt(validation.min_rows)} needed)")
        if short_positives and positives is not None:
            parts.append(f"{_fmt(positives)} positive outcomes ({_fmt(validation.min_positive)} needed)")
        _put(
            state,
            code,
            "fail",
            message=(
                f"The file passes the size checks as uploaded, but the run first removes {_fmt(removed)} rows "
                f"(no consent, no outcome, exact repeats) and would then train on {' and '.join(parts)}."
            ),
            fix="Collect more history, or rows with consent and a known outcome; the limits are not lowered for you.",
            measured=float(rows),
            threshold=float(validation.min_rows),
            details=details,
        )
        return
    _put(
        state,
        code,
        "pass",
        message=f"After the rows the run removes ({_fmt(removed)}), {_fmt(rows)} rows remain.",
        measured=float(rows),
        threshold=float(validation.min_rows),
        details=details,
    )


def _not_trained_reason(state: _State) -> str:
    if state.mode is not RunMode.TRAIN:
        return "a scoring file is not split or trained on"
    if state.config.problem_type is ProblemType.UPLIFT:
        return "the uplift flow draws its own parts; its own checks cover them"
    return "nothing is trained for this use case"


# -- dates and the split -----------------------------------------------------------
def _time_column(state: _State) -> str | None:
    split = state.config.split
    return split.time_column if split.type is SplitType.TIME_BASED else None


def _time_all_parse(state: _State) -> None:
    code = "X_TIME_ALL_PARSE"
    if not state.trains:
        _skip(state, code, _not_trained_reason(state))
        return
    if state.config.split.type is not SplitType.TIME_BASED:
        _skip(state, code, "the split is not by date")
        return
    column = _time_column(state)
    if state.rows is None:
        _skip(state, code, state.rows_note or "the rows could not be prepared")
        return
    if column is None or column not in state.rows.columns:
        _put(
            state,
            code,
            "fail",
            column=column,
            message="The split is by date, but the date column is not set or not in the file.",
            fix="Choose the column that dates each row.",
        )
        return
    total = len(state.rows)
    empty = int(state.rows[column].isna().sum())
    details = {"rows": total, "rows_without_a_date": empty}
    if empty:
        _put(
            state,
            code,
            "fail",
            column=column,
            message=(
                f"{_fmt(empty)} of {_fmt(total)} rows have no date the engine can read in '{column}'. The date "
                "check passes, because it ignores empty cells and allows 5% unreadable, but the split needs a "
                "date in every row and would stop."
            ),
            fix=f"Fill in or fix the dates in '{column}', or drop those rows, then check again.",
            measured=float(empty),
            threshold=0.0,
            details=details,
        )
        return
    _put(
        state,
        code,
        "pass",
        column=column,
        message="Every row has a readable date.",
        measured=0.0,
        threshold=0.0,
        details=details,
    )


def _part_sizes(rows: int, validation_fraction: float, test_fraction: float) -> tuple[int, int, int]:
    """The split's own arithmetic (`prepare._part_sizes`)."""
    n_test = min(rows, round(rows * test_fraction))
    n_validation = min(rows - n_test, round(rows * validation_fraction))
    return rows - n_validation - n_test, n_validation, n_test


def _tie_boundary(values: Sequence[Any], boundary: int, *, floor: int) -> int:
    """The split's own tie rule (`prepare._tie_boundary`): a date never straddles a cut."""
    if boundary <= floor or boundary >= len(values):
        return max(boundary, floor)
    moved = boundary
    while moved > floor and values[moved - 1] == values[boundary]:
        moved -= 1
    return moved


def _time_cuts_feasible(state: _State) -> None:
    code = "X_TIME_CUTS_FEASIBLE"
    if not state.trains:
        _skip(state, code, _not_trained_reason(state))
        return
    if state.config.split.type is not SplitType.TIME_BASED:
        _skip(state, code, "the split is not by date")
        return
    if state.results["X_TIME_ALL_PARSE"].status != "pass" or state.rows is None:
        _skip(state, code, "every row needs a readable date first (X_TIME_ALL_PARSE)")
        return
    column = str(_time_column(state))
    values = sorted(state.rows[column].tolist())
    rows = len(values)
    split = state.config.split
    wanted = _part_sizes(rows, split.validation_fraction, split.test_fraction)
    first_validation = _tie_boundary(values, wanted[0], floor=0)
    first_test = _tie_boundary(values, wanted[0] + wanted[1], floor=first_validation)
    got = (first_validation, first_test - first_validation, rows - first_test)
    distinct = len(set(values))
    details = {
        "rows": rows,
        "distinct_dates": distinct,
        "train_rows": got[0],
        "validation_rows": got[1],
        "test_rows": got[2],
    }
    empty = [
        name
        for name, want, have in zip(("training", "validation", "test"), wanted, got, strict=True)
        if want and not have
    ]
    if empty:
        _put(
            state,
            code,
            "fail",
            column=column,
            message=(
                f"Too many rows share the same date at a cut-off ({distinct} different dates in {_fmt(rows)} rows), "
                f"so the {empty[0]} part would be empty and the split would stop."
            ),
            fix="Use a finer date column (a day rather than a month), add more dates, or split at random.",
            measured=float(distinct),
            details=details,
        )
        return
    _put(
        state,
        code,
        "pass",
        column=column,
        message=f"The date cut-offs leave {got[0]:,} / {got[1]:,} / {got[2]:,} rows in the three parts.",
        measured=float(distinct),
        details=details,
    )


def _groups_enough(state: _State) -> None:
    code = "X_GROUPS_ENOUGH"
    split = state.config.split
    if not state.trains:
        _skip(state, code, _not_trained_reason(state))
        return
    if split.type is SplitType.TIME_BASED or split.group_column is None:
        _skip(state, code, "the rows are not split by group")
        return
    if state.rows is None:
        _skip(state, code, state.rows_note or "the rows could not be prepared")
        return
    column = split.group_column
    if column not in state.rows.columns:
        _put(
            state,
            code,
            "fail",
            column=column,
            message=f"The split keeps groups whole by '{column}', which is not in the file.",
            fix="Choose a group column the file has, or split at random.",
        )
        return
    rows = len(state.rows)
    _, n_validation, n_test = _part_sizes(rows, split.validation_fraction, split.test_fraction)
    needed = 1 + (1 if n_validation else 0) + (1 if n_test else 0)
    distinct = int(state.rows[column].astype("string").fillna(value="__missing__").nunique())
    details = {"groups": distinct, "groups_needed": needed}
    if distinct < needed:
        _put(
            state,
            code,
            "fail",
            column=column,
            message=f"'{column}' has {distinct} different value(s); keeping groups whole needs at least {needed}, one per part.",
            fix="Use a column with more groups, add data from more groups, or split at random.",
            measured=float(distinct),
            threshold=float(needed),
            details=details,
        )
        return
    _put(
        state,
        code,
        "pass",
        column=column,
        message=f"{distinct} groups for {needed} parts.",
        measured=float(distinct),
        threshold=float(needed),
        details=details,
    )


def _simulate_split(state: _State) -> None:
    from engine.stages import prepare

    if not state.trains or state.rows is None or not state.target:
        return
    ids = DRY_RUN_IDS[:1] if state.config.split.type is SplitType.TIME_BASED else DRY_RUN_IDS
    for run_id in ids:
        try:
            parts, _report = prepare.split_dataset(
                state.rows, state.config, run_id=run_id, target=state.target
            )
        except ValueError as exc:
            state.split_error = str(exc)
            state.parts = None
            state.split_blamed = any(
                state.results[code].status == "fail"
                for code in ("X_TIME_ALL_PARSE", "X_TIME_CUTS_FEASIBLE", "X_GROUPS_ENOUGH")
            )
            return
        if state.parts is None:
            state.parts = parts
        verdict = _classes_problem(parts, state)
        if verdict is not None:
            state.parts = parts  # keep the failing draw: it is the one the report describes
            return


def _classes_problem(parts: dict[str, pd.DataFrame], state: _State) -> str | None:
    split = state.config.split
    wanted = {"train": 1.0, "validation": split.validation_fraction, "test": split.test_fraction}
    for name in ("train", "validation", "test"):
        part = parts[name]
        if wanted[name] <= 0:
            continue
        if part.empty:
            return name
        if state.binary and part[str(state.target)].dropna().map(label_key).nunique() < 2:
            return name
    return None


def _split_classes(state: _State) -> None:
    code = "X_SPLIT_CLASSES"
    if not state.trains:
        _skip(state, code, _not_trained_reason(state))
        return
    if state.rows is None:
        _skip(state, code, state.rows_note or "the rows could not be prepared")
        return
    if state.split_error is not None:
        if state.split_blamed:
            _skip(state, code, "the split itself cannot be made (see the date and group probes)")
        else:
            _put(
                state,
                code,
                "fail",
                message=f"The split stops: {state.split_error}",
                fix="Change the split settings or the data so the split can be made.",
            )
        return
    parts = state.parts or {}
    target = str(state.target)
    simulated = []
    for name in ("train", "validation", "test"):
        part = parts.get(name)
        if part is None:
            continue
        values = part[target].dropna()
        positives = _count_positives(part, state) if state.binary else None
        simulated.append(
            SimulatedPart(
                name=name,
                rows=len(part),
                positives=positives,
                classes=int(values.map(label_key).nunique()) if state.binary else None,
            )
        )
    problem = _classes_problem(parts, state) if parts else None
    if state.counts is not None:
        test = next((part for part in simulated if part.name == "test"), None)
        state.counts = state.counts.model_copy(
            update={
                "parts": tuple(simulated),
                "test_primary_metric_defined": (
                    None
                    if test is None
                    else (test.rows > 0 and (not state.binary or (test.classes or 0) >= 2))
                ),
            }
        )
    details: dict[str, Any] = {f"{part.name}_rows": part.rows for part in simulated}
    details.update(
        {f"{part.name}_positives": part.positives for part in simulated if part.positives is not None}
    )
    if problem is not None:
        what = "no rows" if parts[problem].empty else "only one outcome value"
        _put(
            state,
            code,
            "fail",
            message=(
                f"In the simulated split the {problem} part holds {what}, so the model cannot be "
                f"{'scored' if problem != 'train' else 'trained'} on it."
            ),
            fix="Add rows with the rarer outcome (or more dates), or change how the rows are split.",
            details=details,
        )
        return
    minority = _minority_floor(state)
    if minority is not None:
        _put(state, code, "fail", message=minority, fix="Add rows with the rarer outcome.", details=details)
        return
    _put(
        state,
        code,
        "pass",
        message=(
            "Every simulated part holds both outcome values."
            if state.binary
            else "Every simulated part holds rows."
        ),
        details=details,
    )


def _minority_floor(state: _State) -> str | None:
    """A random split may draw a part without the rarer outcome when it has too few rows to go round."""
    if not state.binary or state.rows is None or state.config.split.type is SplitType.TIME_BASED:
        return None
    counts = state.rows[str(state.target)].dropna().map(label_key).value_counts()
    if len(counts) < 2:
        return None
    rarest = int(counts.min())
    split = state.config.split
    smallest = min(f for f in (split.validation_fraction, split.test_fraction) if f > 0)
    if rarest * smallest < 1:
        return f"Only {_fmt(rarest)} rows have the rarer outcome; a {smallest:.0%} part would be expected to hold none."
    return None


def _label_maturity(state: _State) -> None:
    code = "X_LABEL_MATURITY"
    if not state.trains:
        _skip(state, code, _not_trained_reason(state))
        return
    if not state.binary:
        _skip(state, code, "the outcome is not a yes/no outcome")
        return
    if state.config.split.type is not SplitType.TIME_BASED:
        _skip(state, code, "the rows are not ordered by date, so there is no newest part")
        return
    if state.parts is None or state.counts is None or not state.counts.parts:
        _skip(state, code, "the split could not be simulated (see the date probes)")
        return
    by_name = {part.name: part for part in state.counts.parts}
    train = by_name.get("train")
    test = by_name.get("test")
    if train is None or test is None or not train.rows or not test.rows:
        _skip(state, code, "a part of the simulated split is empty (see X_SPLIT_CLASSES)")
        return
    train_rate = (train.positives or 0) / train.rows
    test_rate = (test.positives or 0) / test.rows
    ratio = test_rate / train_rate if train_rate else None
    details = {
        "train_rate": round(train_rate, 4),
        "newest_rate": round(test_rate, 4),
        "newest_rows": test.rows,
    }
    column = _time_column(state)
    if train_rate > 0 and test.positives == 0:
        _put(
            state,
            code,
            "fail",
            column=column,
            message=(
                f"The newest {_fmt(test.rows)} rows have no positive outcome, against {train_rate:.1%} in the older "
                "rows: their outcomes are probably not known yet, so the test would measure nothing."
            ),
            fix="Leave out the newest dates whose outcome window has not ended, or wait until it has.",
            measured=0.0,
            details=details,
        )
        return
    pooled = ((train.positives or 0) + (test.positives or 0)) / (train.rows + test.rows)
    spread = math.sqrt(pooled * (1 - pooled) * (1 / train.rows + 1 / test.rows)) if 0 < pooled < 1 else 0.0
    z = (test_rate - train_rate) / spread if spread else 0.0
    if ratio is not None and ratio < LABEL_MATURITY_WARN_RATIO and z <= -LABEL_MATURITY_Z:
        _put(
            state,
            code,
            "warn",
            column=column,
            message=(
                f"The newest rows' positive rate ({test_rate:.1%}) is well below the older rows' ({train_rate:.1%}); "
                "some of their outcomes may not be known yet, which makes the test look worse than the model is."
            ),
            fix="Check that the newest dates' outcome window has ended; leave them out if not.",
            measured=round(ratio, 4),
            threshold=LABEL_MATURITY_WARN_RATIO,
            details=details,
        )
        return
    _put(
        state,
        code,
        "pass",
        column=column,
        message="The newest rows' outcome rate is in line with the older rows'.",
        measured=None if ratio is None else round(ratio, 4),
        threshold=LABEL_MATURITY_WARN_RATIO,
        details=details,
    )


def _regression_target_numeric(state: _State) -> None:
    import pandas as pd

    code = "X_REGRESSION_TARGET_NUMERIC"
    if state.mode is not RunMode.TRAIN:
        _skip(state, code, "a scoring file has no outcome to train on")
        return
    if state.config.problem_type is not ProblemType.REGRESSION:
        _skip(state, code, "the outcome is not an amount")
        return
    target = state.target
    if not target or target not in state.frame.columns:
        _skip(state, code, "no outcome column is chosen yet")
        return
    values = state.frame[target].dropna()
    numbers = (
        pd.to_numeric(values, errors="coerce") if not pd.api.types.is_bool_dtype(values.dtype) else values
    )
    bad = int(numbers.isna().sum()) if len(values) else 0
    if pd.api.types.is_bool_dtype(values.dtype):
        bad = len(values)
    details = {"values": len(values), "not_numbers": bad}
    if bad or not len(values):
        _put(
            state,
            code,
            "fail",
            column=target,
            message=(
                f"{_fmt(bad)} of {_fmt(len(values))} values in '{target}' are not numbers; a model of an amount "
                "cannot be trained on text and the run would stop at training."
            ),
            fix=f"Turn '{target}' into numbers (remove currency signs and words), or choose a yes/no outcome instead.",
            measured=float(bad),
            threshold=0.0,
            details=details,
        )
        return
    _put(
        state,
        code,
        "pass",
        column=target,
        message="Every outcome value is a number.",
        measured=0.0,
        threshold=0.0,
        details=details,
    )


# -- uplift, channels, money and generated text -----------------------------------
def _treatment_tokens(state: _State) -> None:
    from engine.uplift.data import coerce_arms, coerce_treatment, detect_treatment_column

    code = "X_TREATMENT_TOKENS"
    if state.config.problem_type is not ProblemType.UPLIFT:
        _skip(state, code, "the use case does not compare a contacted group with a control group")
        return
    if state.mode is not RunMode.TRAIN:
        _skip(state, code, "a scoring file is not split into contacted and control")
        return
    uplift = state.config.uplift
    column = detect_treatment_column([str(c) for c in state.frame.columns], uplift)
    if column is None:
        _skip(state, code, "no column says who was contacted (the uplift checks say so)")
        return
    levels = tuple(uplift.treatment_levels)
    values = state.frame[column]
    _codes, bad = coerce_arms(values, levels) if levels else coerce_treatment(values)
    details = {"rows": len(values), "not_understood": bad, "offers": len(levels) or None}
    if bad:
        expect = "one of the configured offers" if levels else "1/0 or true/false"
        _put(
            state,
            code,
            "fail",
            column=column,
            message=f"{_fmt(bad)} of {_fmt(len(values))} values in '{column}' are not {expect} (an empty cell counts too).",
            fix=f"Map '{column}' to {expect}, and say what an empty cell means.",
            measured=float(bad),
            threshold=0.0,
            details=details,
        )
        return
    _put(
        state,
        code,
        "pass",
        column=column,
        message="Every row says plainly whether it was contacted.",
        measured=0.0,
        threshold=0.0,
        details=details,
    )


def _channel_columns_present(state: _State) -> None:
    code = "X_CHANNEL_COLUMNS_PRESENT"
    channels = state.config.actions.suppression.channels
    if not channels:
        _skip(state, code, "the use case names no per-channel consent columns")
        return
    named: list[str] = []
    for channel in channels.values():
        named.extend(name for name in (channel.consent_column, channel.contactable_column) if name)
    missing = [name for name in named if name not in state.frame.columns]
    details = {"columns_named": len(named), "columns_missing": len(missing)}
    if missing:
        _put(
            state,
            code,
            "fail",
            column=missing[0],
            message=(
                f"{len(missing)} per-channel consent column(s) are not in the file, first '{missing[0]}'; when scoring "
                "they are skipped with only a log line, so nobody would be held back on that channel."
            ),
            fix="Add the per-channel consent columns to the export.",
            measured=float(len(missing)),
            threshold=0.0,
            details=details,
        )
        return
    _put(
        state,
        code,
        "pass",
        message="Every per-channel consent column is in the file.",
        measured=0.0,
        threshold=0.0,
        details=details,
    )


def _value_column_missing_share(state: _State) -> None:
    code = "X_VALUE_COLUMN_MISSING_SHARE"
    column = state.config.uplift.policy.value_column
    if column is None:
        _skip(state, code, "the use case names no value column")
        return
    if column not in state.frame.columns:
        _put(
            state,
            code,
            "fail",
            column=column,
            message=f"The value column '{column}' is not in the file, so no money figure can be shown.",
            fix=f"Add '{column}' to the export, or give a fixed value per conversion.",
            measured=1.0,
            threshold=VALUE_COLUMN_MAX_MISSING,
        )
        return
    rows = len(state.frame)
    missing = int(state.frame[column].isna().sum())
    share = missing / rows if rows else 1.0
    details = {"rows": rows, "missing": missing}
    if share > VALUE_COLUMN_MAX_MISSING:
        _put(
            state,
            code,
            "fail",
            column=column,
            message=f"{share:.0%} of '{column}' is empty (at most {VALUE_COLUMN_MAX_MISSING:.0%} allowed), so no money figure would be shown.",
            fix=f"Fill in '{column}' for more rows; empty values are never filled in for you.",
            measured=round(share, 4),
            threshold=VALUE_COLUMN_MAX_MISSING,
            details=details,
        )
        return
    _put(
        state,
        code,
        "pass",
        column=column,
        message=f"{share:.0%} of '{column}' is empty.",
        measured=round(share, 4),
        threshold=VALUE_COLUMN_MAX_MISSING,
        details=details,
    )


def _allowed_fields_filled(state: _State) -> None:
    code = "X_ALLOWED_FIELDS_FILLED"
    generative = state.config.generative
    if generative.kind is not GenerativeKind.CAMPAIGN_COPY:
        _skip(state, code, "the use case writes no messages")
        return
    fields = [name for name in generative.campaign_copy.allowed_fields if name in state.frame.columns]
    if not fields:
        _skip(state, code, "none of the message fields is a column of this file")
        return
    gaps = {name: int(state.frame[name].isna().sum()) for name in fields}
    worst = max(gaps, key=lambda name: gaps[name])
    details = {
        "fields": len(fields),
        "fields_with_gaps": sum(1 for v in gaps.values() if v),
        "most_empty": gaps[worst],
    }
    if gaps[worst]:
        _put(
            state,
            code,
            "fail",
            column=worst,
            message=(
                f"'{worst}' is empty in {_fmt(gaps[worst])} rows; a message that uses it cannot be written for those "
                "rows, and the whole batch would stop."
            ),
            fix=f"Fill in '{worst}', or take it out of the fields the messages may use.",
            measured=float(gaps[worst]),
            threshold=0.0,
            details=details,
        )
        return
    _put(
        state,
        code,
        "pass",
        message="Every message field is filled in every row.",
        measured=0.0,
        threshold=0.0,
        details=details,
    )


def _complaint_column_present(state: _State) -> None:
    code = "X_COMPLAINT_COLUMN_PRESENT"
    generative = state.config.generative
    if generative.kind is not GenerativeKind.ROOT_CAUSE_SUMMARY:
        _skip(state, code, "the use case writes no root-cause summary")
        return
    column = generative.root_cause.complaint_text_column
    if column is None:
        _skip(state, code, "the use case names no complaint text column")
        return
    if column not in state.frame.columns:
        _put(
            state,
            code,
            "fail",
            column=column,
            message=f"The complaint text column '{column}' is not in the file, so the root-cause summary would stop.",
            fix=f"Add '{column}' to the export.",
        )
        return
    text = state.frame[column].dropna().astype("string").str.strip()
    filled = int(text.ne("").sum())
    details = {"rows": len(state.frame), "rows_with_text": filled}
    if not filled:
        _put(
            state,
            code,
            "warn",
            column=column,
            message=f"'{column}' is empty in every row, so the summary would have no complaint text to read.",
            fix=f"Export '{column}' with its text.",
            measured=0.0,
            details=details,
        )
        return
    _put(
        state,
        code,
        "pass",
        column=column,
        message=f"{_fmt(filled)} rows have complaint text.",
        measured=float(filled),
        details=details,
    )
