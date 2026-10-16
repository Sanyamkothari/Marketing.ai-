"""What your file already says (Plan L follow-up, idea #5, DEC-1596): a first look with no AI and no model.

A person without a data team learns something useful from their own file before anything is trained.
Everything here is counted on the rows the diagnosis read, by rules, with no AI and no model, and every
answer carries the same label: **"Describes your file. Not a cause and not a prediction."**

Two pictures, one contract (`FirstLook`):

* **The outcome, by column** (`outcome_look`), for a use case Data Doctor says is `ready` or `check` on a
  file: the file's base rate of "yes" (by the engine's own label rule), and the rate by the
  `FIRST_LOOK_COLUMNS` most informative columns. Each column's groups are the Ask-your-data `rate_by`
  tool's own (`engine.agent.tools`, DEC-1252): a number column in five ranges of about equal rows, a text
  column by its ten most common values, and **no group of fewer than `MIN_GROUP_ROWS` rows is ever named**
  (rate_by puts such a value in "(other)" and suppresses a group still that small, with the next smallest
  when needed so totals cannot give it back; a suppressed group is not shown here at all, only counted).
* **Recency and frequency** (`recency_look`), for a file that has no use case's outcome but has a customer
  ID column and a date column: how many customers last appear in each period before the file's latest
  date, and how many rows each customer has. Fixed periods, grouped by the same `rate_by` with the same
  rule.

**Most informative**, deterministically: for each column, the *spread* is the average distance between a
row's group rate and the file's rate, weighted by group size - `sum(rows_g / rows * |rate_g - base|)` over
the groups shown. Columns are ranked by spread (largest first; ties by the column's place in the file), and
a column needs at least two groups shown to be ranked. At most `MAX_LOOK_COLUMNS` columns are measured, in
the file's order, so the diagnosis stays bounded; how many were left unmeasured is said.

**Never used:** the outcome itself; personal-data columns (the profile's `pii_kinds` or
`free_text_pii_kinds`, and `agent.always_hide_columns`); columns that may hold the answer (the advisor's
`LEAKAGE_SUSPECTED` findings of the Run button's checks, any severity, and any column whose groups alone
separate yes from no beyond the use case's `validation.leakage_auc_threshold`); the ID column and columns
that look like IDs; date columns (they are not bucketed); free-text columns; columns with one value.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Final, Literal

from pydantic import Field

from engine.config import ColumnType, StrictBase
from engine.utils.logging import get_logger

if TYPE_CHECKING:
    import pandas as pd

    from engine.agent.advisor import Advice
    from engine.agent.tools import AgentContext
    from engine.config import UseCaseConfig
    from engine.contracts import DatasetProfile
    from engine.doctor.contracts import FileDiagnosis

__all__ = [
    "FIRST_LOOK_COLUMNS",
    "FIRST_LOOK_LABEL",
    "MAX_LOOK_COLUMNS",
    "FirstLook",
    "LeftOutColumn",
    "LookColumn",
    "LookGroup",
    "advised_leaks",
    "attach_recency",
    "outcome_look",
    "recency_look",
    "separation",
    "spread",
]

_LOG = get_logger(__name__)

FIRST_LOOK_LABEL: Final[str] = "Describes your file. Not a cause and not a prediction."
"""The label every first look carries, word for word."""

FIRST_LOOK_COLUMNS: Final[int] = 5
"""The most columns a first look shows."""

MAX_LOOK_COLUMNS: Final[int] = 20
"""The most columns a first look measures (each is one `rate_by` call on the rows read), in file order."""

RULE: Final[str] = (
    "Columns are ranked by how far their groups' rates are from the file's rate, weighted by how many rows "
    "each group holds; groups of fewer than 10 rows are never shown."
)

RECENCY_BUCKETS: Final[tuple[tuple[int, str], ...]] = (
    (30, "In the last 30 days"),
    (90, "31 to 90 days before"),
    (180, "91 to 180 days before"),
    (365, "181 to 365 days before"),
)
RECENCY_OLDEST: Final[str] = "More than a year before"
FREQUENCY_BUCKETS: Final[tuple[tuple[int, str], ...]] = (
    (1, "1 row"),
    (3, "2 to 3 rows"),
    (10, "4 to 10 rows"),
)
FREQUENCY_MOST: Final[str] = "11 rows or more"

_RECENCY_COLUMN: Final[str] = "Last row"
_FREQUENCY_COLUMN: Final[str] = "Rows per customer"

LeftOutReason = Literal["personal", "may_hold_answer", "id", "date", "free_text", "one_value"]


class LookGroup(StrictBase):
    """One group of a column, with at least `MIN_GROUP_ROWS` rows (smaller groups are never listed)."""

    group: str = Field(description="The value, the range or '(other)' / '(empty)', as `rate_by` names it.")
    rows: int = Field(description="Rows (or customers, for recency) in the group.")
    share: float = Field(description="`rows` as a share of the rows read.")
    rate: float | None = Field(
        default=None, description="The group's rate of 'yes'; null for recency and frequency."
    )


class LookColumn(StrictBase):
    """One column of a first look: its groups and how far apart their rates are."""

    column: str
    kind: str = Field(
        description="`values` (grouped by value) or `number_ranges` (ranges of about equal rows)."
    )
    spread: float = Field(
        default=0.0,
        description="Sum over the groups shown of rows / rows shown * |rate - base rate|; 0 for recency.",
    )
    groups: tuple[LookGroup, ...]
    groups_hidden: int = Field(
        default=0, description="Groups of fewer than 10 rows, not shown (no name, no count, no rate)."
    )
    summary: str = Field(description="One plain sentence.")


class LeftOutColumn(StrictBase):
    """A column the first look did not use, and why."""

    column: str
    reason: LeftOutReason


class FirstLook(StrictBase):
    """What a file already says, counted with no AI and no model (`engine.doctor.first_look`)."""

    kind: Literal["outcome", "recency"]
    label: str = Field(default=FIRST_LOOK_LABEL, description="Shown with every first look, word for word.")
    upload_id: str
    file_name: str
    rows: int = Field(description="Rows counted (the rows the diagnosis read).")
    file_rows: int = Field(description="Rows the file has.")
    sampled: bool = Field(
        description="True when `rows` is fewer than `file_rows`: a random sample of the file was read."
    )
    summary: str = Field(description="One plain sentence.")
    outcome_column: str | None = None
    positive_label: str | None = None
    base_rate: float | None = Field(default=None, description="The file's rate of 'yes'.")
    columns: tuple[LookColumn, ...] = Field(
        default=(), description="The most informative columns, largest spread first."
    )
    rule: str | None = Field(default=None, description="How the columns were chosen, in plain words.")
    left_out: tuple[LeftOutColumn, ...] = Field(default=(), description="Columns not used, and why.")
    not_measured: int = Field(
        default=0, description="Columns past the first `MAX_LOOK_COLUMNS` that were not measured."
    )
    id_column: str | None = None
    date_column: str | None = None
    latest: dt.date | None = Field(default=None, description="The file's latest date (recency).")
    customers: int | None = Field(default=None, description="Customers with a date (recency).")
    recency: LookColumn | None = None
    frequency: LookColumn | None = None


# ---------------------------------------------------------------------------
# The outcome, by column
# ---------------------------------------------------------------------------
def advised_leaks(advice: Advice, *, proven: bool = False) -> tuple[str, ...]:
    """Columns the advisor's run of the checks flagged as possibly holding the answer (any severity).

    `proven=True` keeps only the error-severity finds: the column tells yes from no almost by itself.
    """
    found: list[str] = []
    for result in advice.tool_results:
        if result.tool != "check_data":
            continue
        checks: Any = result.result.get("checks")
        for check in checks if isinstance(checks, list) else []:
            if isinstance(check, dict) and check.get("code") == "LEAKAGE_SUSPECTED" and check.get("column"):
                if proven and check.get("severity") != "error":
                    continue
                found.append(str(check["column"]))
    return tuple(dict.fromkeys(found))


def spread(groups: Sequence[LookGroup], base: float) -> float:
    """The ranking score: sum of rows_g / rows * |rate_g - base| over the groups with a rate."""
    rated = [g for g in groups if g.rate is not None]
    total = sum(g.rows for g in rated)
    if not total:
        return 0.0
    return round(sum(g.rows / total * abs(float(g.rate or 0.0) - base) for g in rated), 6)


def separation(groups: Sequence[LookGroup]) -> float | None:
    """How well the groups alone tell yes from no: the rank statistic of the group rate, 0.5 to 1.

    Rows of one group tie (half a pair each), so this is what the Run button's leak check computes for a
    column read by its group means, on the groups shown.
    """
    by_rate: dict[float, list[float]] = {}
    for group in groups:
        if group.rate is None:
            continue
        positives = group.rate * group.rows
        entry = by_rate.setdefault(group.rate, [0.0, 0.0])
        entry[0] += positives
        entry[1] += group.rows - positives
    yes = sum(entry[0] for entry in by_rate.values())
    no = sum(entry[1] for entry in by_rate.values())
    if yes <= 0 or no <= 0:
        return None
    below = 0.0
    pairs = 0.0
    for rate in sorted(by_rate):
        positives, negatives = by_rate[rate]
        pairs += positives * (below + 0.5 * negatives)
        below += negatives
    auc = pairs / (yes * no)
    return max(auc, 1.0 - auc)


def _hidden_names(config: UseCaseConfig) -> set[str]:
    return {entry.casefold() for entry in config.agent.always_hide_columns}


def _left_out_reason(
    name: str,
    profile: DatasetProfile,
    hidden: set[str],
    primary_key: str | None,
) -> LeftOutReason | None:
    from engine.agent.untrusted import display_name

    column = next((c for c in profile.columns if c.name == name), None)
    if (
        name.casefold() in hidden
        or display_name(name).casefold() in hidden
        or (column is not None and (column.pii_kinds or column.free_text_pii_kinds))
    ):
        return "personal"
    if name == primary_key or name in profile.primary_key_candidates:
        return "id"
    if column is None:
        return None
    if column.looks_like_id:
        return "id"
    numeric = column.inferred_type in (ColumnType.INTEGER, ColumnType.FLOAT, ColumnType.BOOLEAN)
    if column.inferred_type in (ColumnType.DATE, ColumnType.DATETIME) or (
        not numeric and (column.looks_like_time or name in profile.time_column_candidates)
    ):
        return "date"  # a number whose name sounds like time (`tenure_months`) is a number
    if column.inferred_type is ColumnType.TEXT:
        return "free_text"
    if column.is_constant:
        return "one_value"
    return None


def _groups(result: Mapping[str, Any], measure: str | None) -> tuple[list[LookGroup], int]:
    """A `rate_by` result's groups with figures; suppressed groups are counted, never listed."""
    from engine.agent.placeholders import number_text

    shown: list[LookGroup] = []
    hidden = 0
    for entry in result.get("groups") or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("suppressed") or entry.get("rows") is None:
            hidden += 1
            continue
        rate = entry.get(measure) if measure is not None else None
        name = str(entry.get("group"))
        low, high = entry.get("low"), entry.get("high")
        if low is not None and low == high:
            name = number_text(float(low))  # a range of one value reads as the value: "4", not "4 to 4"
        shown.append(
            LookGroup(
                group=name,
                rows=int(entry["rows"]),
                share=float(entry.get("share") or 0.0),
                rate=None if rate is None else float(rate),
            )
        )
    return shown, hidden


def _pct(value: float) -> str:
    return f"{value:.1%}"


def _named(group: str) -> str:
    return group if group.startswith("(") else f"({group})"


def _column_summary(column: str, groups: Sequence[LookGroup]) -> str:
    rated = [g for g in groups if g.rate is not None]
    low = min(rated, key=lambda g: (float(g.rate or 0.0), -g.rows))
    high = max(rated, key=lambda g: (float(g.rate or 0.0), g.rows))
    return (
        f"By {column}, the rate goes from {_pct(float(low.rate or 0.0))} {_named(low.group)} "
        f"to {_pct(float(high.rate or 0.0))} {_named(high.group)}."
    )


def outcome_look(
    ctx: AgentContext,
    *,
    outcome: str,
    file_name: str,
    file_rows: int,
    primary_key: str | None = None,
    leaks: Iterable[str] = (),
) -> FirstLook | None:
    """The base rate and the rate by the most informative columns, on `ctx.frame`; None for no yes/no outcome."""
    from engine.agent.tools import AgentToolError, call_tool
    from engine.agent.untrusted import display_name

    try:
        described: dict[str, Any] = dict(
            call_tool(ctx, "describe_outcome", {"column": outcome}, evidence_id="first_look").result
        )
    except AgentToolError:
        return None
    if not described.get("two_valued") or described.get("positive_rate") is None:
        return None
    base = float(described["positive_rate"])
    rows = len(ctx.frame)
    flagged = set(leaks)
    hidden_names = _hidden_names(ctx.config)
    threshold = ctx.config.validation.leakage_auc_threshold
    left_out: list[LeftOutColumn] = []
    candidates: list[str] = []
    for raw in ctx.frame.columns:
        name = str(raw)
        if name == outcome:
            continue
        if name in flagged:
            reason: LeftOutReason | None = "may_hold_answer"
        else:
            reason = _left_out_reason(name, ctx.profile, hidden_names, primary_key)
        if reason is not None:
            left_out.append(LeftOutColumn(column=display_name(name), reason=reason))
        else:
            candidates.append(name)
    measured, not_measured = candidates[:MAX_LOOK_COLUMNS], max(0, len(candidates) - MAX_LOOK_COLUMNS)
    ranked: list[tuple[float, int, LookColumn]] = []
    label: str | None = None
    for position, name in enumerate(measured):
        try:
            result = call_tool(
                ctx, "rate_by", {"column": name, "outcome_column": outcome}, evidence_id="first_look"
            ).result
        except AgentToolError as exc:
            _LOG.info("doctor.first_look_column_skipped code=%s", exc.code)
            continue
        if result.get("function") != "positive_rate":
            return None  # not a yes/no outcome by the engine's rule: nothing here is a rate
        block = result.get("outcome")
        if label is None and isinstance(block, dict) and block.get("positive_label") is not None:
            label = str(block["positive_label"])
        groups, hidden = _groups(result, "positive_rate")
        if sum(1 for g in groups if g.rate is not None) < 2:
            continue
        found = separation(groups)
        if found is not None and found > threshold:
            # Its groups alone tell yes from no almost perfectly: it may hold the answer.
            left_out.append(LeftOutColumn(column=display_name(name), reason="may_hold_answer"))
            continue
        shown = str(result.get("column") or display_name(name))
        score = spread(groups, base)
        ranked.append(
            (
                score,
                position,
                LookColumn(
                    column=shown,
                    kind=str(result.get("kind") or "values"),
                    spread=score,
                    groups=tuple(groups),
                    groups_hidden=hidden,
                    summary=_column_summary(shown, groups),
                ),
            )
        )
    ranked.sort(key=lambda item: (-item[0], item[1]))
    chosen = tuple(column for _, _, column in ranked[:FIRST_LOOK_COLUMNS])
    shown_outcome = display_name(outcome)
    answer = label if label is not None else str(described.get("positive_label"))
    known = int(described.get("positives") or 0) + int(described.get("negatives") or 0)
    summary = f"{_pct(base)} of the {known:,} rows with {shown_outcome} filled in are '{answer}'."
    if not chosen:
        summary += " No column splits the rows into groups of 10 or more with different rates."
    return FirstLook(
        kind="outcome",
        upload_id=ctx.upload_id,
        file_name=file_name,
        rows=rows,
        file_rows=max(file_rows, rows),
        sampled=rows < file_rows,
        summary=summary,
        outcome_column=shown_outcome,
        positive_label=answer,
        base_rate=base,
        columns=chosen,
        rule=RULE,
        left_out=tuple(left_out),
        not_measured=not_measured,
    )


# ---------------------------------------------------------------------------
# Recency and frequency, for a file with no outcome
# ---------------------------------------------------------------------------
def _bucket(value: int, buckets: Sequence[tuple[int, str]], last: str) -> str:
    for upper, name in buckets:
        if value <= upper:
            return name
    return last


def _ordered(column: LookColumn, order: Sequence[str]) -> LookColumn:
    place = {name: index for index, name in enumerate(order)}
    groups = sorted(column.groups, key=lambda g: place.get(g.group, len(order)))
    return column.model_copy(update={"groups": tuple(groups)})


def recency_look(
    frame: pd.DataFrame,
    diagnosis: FileDiagnosis,
    *,
    profile: DatasetProfile,
    config: UseCaseConfig,
    hide: Iterable[str] = (),
) -> FirstLook | None:
    """When customers last appear in the file and how many rows each has; None without an ID and a date."""
    import pandas as pd

    from engine.agent.tools import AgentContext, AgentToolError, call_tool
    from engine.config import RunMode, StandardType
    from engine.onboarding.transforms import cast_series

    hidden = _hidden_names(config) | {name.casefold() for name in hide}
    key = diagnosis.id_column
    if key is None or key not in frame.columns or _left_out_reason(key, profile, hidden, None) == "personal":
        return None
    date = next(
        (
            name
            for name in diagnosis.date_columns
            if name in frame.columns and _left_out_reason(name, profile, hidden, None) != "personal"
        ),
        None,
    )
    if date is None:
        return None
    times = pd.to_datetime(cast_series(frame[date], StandardType.DATE).values, errors="coerce")
    table = pd.DataFrame({"key": frame[key].astype("string"), "time": times}).dropna()
    if table.empty:
        return None
    latest = table["time"].max()
    per = table.groupby("key", sort=True)["time"].agg(["max", "size"])
    days = (latest - per["max"]).dt.days.astype(int)
    derived = pd.DataFrame(
        {
            _RECENCY_COLUMN: [_bucket(int(d), RECENCY_BUCKETS, RECENCY_OLDEST) for d in days],
            _FREQUENCY_COLUMN: [_bucket(int(n), FREQUENCY_BUCKETS, FREQUENCY_MOST) for n in per["size"]],
        }
    )
    ctx = AgentContext(
        use_case_id=config.id,
        config=config,
        config_root=None,
        upload_id=diagnosis.upload_id,
        mode=RunMode.TRAIN,
        profile=profile,
        frame=derived,
    )
    pictures: dict[str, LookColumn] = {}
    for name, order in (
        (_RECENCY_COLUMN, [*(n for _, n in RECENCY_BUCKETS), RECENCY_OLDEST]),
        (_FREQUENCY_COLUMN, [*(n for _, n in FREQUENCY_BUCKETS), FREQUENCY_MOST]),
    ):
        if name == _FREQUENCY_COLUMN and int(per["size"].max()) <= 1:
            continue  # one row per customer: every customer is "1 row", nothing to show
        try:
            result = call_tool(ctx, "rate_by", {"column": name}, evidence_id="first_look").result
        except AgentToolError:
            return None
        groups, hidden_groups = _groups(result, None)
        if not groups:
            continue
        pictures[name] = _ordered(
            LookColumn(
                column=name,
                kind="values",
                groups=tuple(groups),
                groups_hidden=hidden_groups,
                summary="",
            ),
            order,
        )
    recency = pictures.get(_RECENCY_COLUMN)
    if recency is None:
        return None
    customers = len(per)
    when = latest.date()
    recent = next((g for g in recency.groups if g.group == RECENCY_BUCKETS[0][1]), None)
    recency = recency.model_copy(
        update={
            "summary": (
                f"{recent.rows:,} of {customers:,} customers have a row in the 30 days up to {when:%d %b %Y}."
                if recent is not None
                else f"Fewer than 10 of {customers:,} customers have a row in the 30 days up to {when:%d %b %Y}."
            )
        }
    )
    frequency = pictures.get(_FREQUENCY_COLUMN)
    if frequency is not None:
        named = [g for g in frequency.groups if not g.group.startswith("(")] or list(frequency.groups)
        common = max(named, key=lambda g: (g.rows, -named.index(g)))
        frequency = frequency.model_copy(
            update={
                "summary": f"The most common is {common.group}: {common.rows:,} of {customers:,} customers."
            }
        )
    rows = len(frame)
    return FirstLook(
        kind="recency",
        upload_id=diagnosis.upload_id,
        file_name=diagnosis.file_name,
        rows=rows,
        file_rows=max(diagnosis.rows, rows),
        sampled=rows < diagnosis.rows,
        summary=(
            f"{customers:,} customers ({key}) with a date in {date}; the latest is {when:%d %b %Y}. "
            "Periods are counted back from that date."
        ),
        id_column=key,
        date_column=date,
        latest=when,
        customers=customers,
        recency=recency,
        frequency=frequency,
    )


def attach_recency(
    files: Sequence[FileDiagnosis],
    frames: Sequence[pd.DataFrame],
    profiles: Sequence[DatasetProfile],
    configs: Sequence[UseCaseConfig],
) -> tuple[FileDiagnosis, ...]:
    """Each file that names no offered use case's outcome gets its recency picture, when it has one."""
    from engine.doctor.diagnose import _outcome_named

    if not configs:
        return tuple(files)
    hide = {name for config in configs for name in config.agent.always_hide_columns}
    out: list[FileDiagnosis] = []
    for file, frame, profile in zip(files, frames, profiles, strict=True):
        if any(_outcome_named(frame, config) for config in configs):
            out.append(file)
            continue
        try:
            look = recency_look(frame, file, profile=profile, config=configs[0], hide=hide)
        except Exception as exc:  # a courtesy: a file this cannot read is still diagnosed
            _LOG.info("doctor.recency_skipped upload=%s error=%s", file.upload_id, type(exc).__name__)
            look = None
        out.append(file.model_copy(update={"first_look": look}) if look is not None else file)
    return tuple(out)
