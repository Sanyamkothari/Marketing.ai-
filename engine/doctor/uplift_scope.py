"""Uplift goals in the Data Doctor: is this a campaign test with a random control group? (roadmap 2.2).

A campaign test (Hillstrom's e-mail file: treated customers, a control group, the outcome of the next two
weeks) can answer "who is changed by the campaign" - but only when who got the campaign was decided at random.
When it was chosen, the groups already differed before the campaign, and every difference in outcome mixes what
the campaign did with who was picked. So the goal is offered under three hard rules, measured on the file:

* the treatment column names a **control group** (no campaign): without one there is nothing to compare;
* every group has at least `uplift.min_arm_rows` customers (the Run button's own floor);
* the groups are **balanced** on the columns known before the campaign.

Balance is measured per column, between the control group and each treated group:

* a number: the standardized mean difference (difference of the means over the pooled standard deviation);
* a category: Cramer's V of the chi-square test of group by category.

A column is flagged only when the difference is both big (SMD above `BALANCE_MAX_SMD`, V above
`BALANCE_MAX_CRAMERS_V`, the usual 0.1 rule of thumb) **and** unlikely by chance (p below `BALANCE_ALPHA`).
The size rule alone raises false alarms on small groups (a tiny group differs by 0.1 by luck); the p-value alone
flags trivial differences on a big file. One flagged column puts the goal out of scope with the existing
`UPLIFT_NOT_RANDOM` sentence: the check is a hard rule, never an option the person can switch off. The Run
button's own check (`TREATMENT_NOT_RANDOM`, a classifier predicting the treatment) still runs after this one.

Nothing here reads a cell into a message: findings name columns and numbers only.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Final, Literal

import numpy as np
from pydantic import Field

from engine.config import StrictBase
from engine.doctor.out_of_scope import out_of_scope

if TYPE_CHECKING:
    import pandas as pd

    from engine.config import UseCaseConfig

__all__ = [
    "BALANCE_ALPHA",
    "BALANCE_MAX_CRAMERS_V",
    "BALANCE_MAX_SMD",
    "ColumnBalance",
    "TreatmentBalance",
    "UpliftScope",
    "check_treatment_balance",
    "control_level",
    "uplift_scope",
]

BALANCE_MAX_SMD: Final[float] = 0.1
"""A standardized mean difference above this is a visible imbalance (the common rule of thumb)."""

BALANCE_MAX_CRAMERS_V: Final[float] = 0.1
"""The same rule for a category: Cramer's V (0 = same mix in every group, 1 = completely different)."""

BALANCE_ALPHA: Final[float] = 0.001
"""A difference counts only when chance explains it less than once in a thousand. Many columns are tested, so
this is strict on purpose: a random assignment should not be rejected by the luck of one column."""

MAX_LEVELS: Final[int] = 20
"""Categories tested per column; the rarer ones are pooled as one, so a column of names is not a test."""

MAX_SAMPLE_ROWS: Final[int] = 200_000
"""Rows measured (a seeded sample per group); the tests do not need more, and a big file stays fast."""

_SEED: Final[int] = 20240607

_CONTROL_NAME: Final[re.Pattern[str]] = re.compile(
    r"^(0|false|no|none|control|controls|holdout|hold[ _-]?out|untreated|baseline|not[ _-]?treated)$"
    r"|^no[ _-]|control|hold[ _-]?out|untreated",
    re.IGNORECASE,
)
_ID_LIKE: Final[float] = 0.9
"""A text column with this share of distinct values is an ID or free text, not a description of the customer."""


class ColumnBalance(StrictBase):
    """How one column differs between the control group and the most different treated group."""

    column: str
    measure: Literal["smd", "cramers_v"] = Field(description="How the difference is measured.")
    difference: float = Field(
        description="The standardized mean difference or Cramer's V, largest over groups."
    )
    p_value: float
    group: str = Field(description="The treated group that differs most from the control group.")
    flagged: bool = Field(description="Big enough and unlikely enough by chance to show the groups differ.")


class TreatmentBalance(StrictBase):
    """The groups of a campaign test, and whether they look the same before the campaign."""

    treatment_column: str
    control_group: str | None = Field(
        description="The group that got no campaign; null when the file has none."
    )
    groups: dict[str, int] = Field(
        description="Customers per group, in the file's order of first appearance."
    )
    columns_tested: int
    balanced: bool
    flagged: tuple[ColumnBalance, ...] = Field(description="Columns whose groups differ, largest first.")
    largest: ColumnBalance | None = Field(
        description="The column with the largest difference, flagged or not."
    )


class UpliftScope(StrictBase):
    """Whether a campaign test is in scope: `ok`, or why not."""

    status: Literal["ok", "no_control", "arm_too_small", "not_random"]
    treatment_column: str
    balance: TreatmentBalance | None
    message: str = Field(
        description="One plain sentence for the person; the out-of-scope sentence when out of scope."
    )


def control_level(levels: Sequence[object], configured: Sequence[str] = ()) -> str | None:
    """The level that means "no campaign": the first configured level, else the one named like it, else None.

    `configured` is `uplift.treatment_levels` (control first). A 0/1 column has the control `0`/`False`.
    """
    names = [_text(level) for level in levels]
    if configured:
        wanted = _text(configured[0])
        return wanted if wanted in names else None
    named = [name for name in names if _CONTROL_NAME.search(name)]
    return named[0] if len(named) == 1 else None


def _text(value: object) -> str:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def _description_columns(frame: pd.DataFrame, config: UseCaseConfig, treatment: str) -> list[str]:
    """The columns known before the campaign: all but the treatment, the outcome, the configured exclusions
    (outcomes of the same weeks, the treatment date), and anything that is an ID or free text."""
    skip = {
        treatment,
        *(config.uplift.reserved_columns()),
        *(config.prepare.exclude_columns),
        *(c for c in (config.target.column, config.split.time_column) if c),
    }
    rows = max(len(frame), 1)
    chosen: list[str] = []
    for name in frame.columns:
        column = str(name)
        if column in skip:
            continue
        series = frame[name]
        if series.notna().sum() == 0:
            continue
        if _id_named(column):
            continue
        if not _is_number(series) and series.nunique(dropna=True) / rows > _ID_LIKE:
            continue
        chosen.append(column)
    return chosen


def _id_named(column: str) -> bool:
    return bool(re.search(r"(^|_)(id|key|uuid|guid)($|_)", column, re.IGNORECASE))


def _is_number(series: pd.Series) -> bool:
    import pandas as pd

    return bool(pd.api.types.is_numeric_dtype(series.dtype)) and not pd.api.types.is_bool_dtype(series.dtype)


def _smd(treated: pd.Series, control: pd.Series) -> tuple[float, float] | None:
    """`(standardized mean difference, two-sided p)` of two numeric groups; None when either has under 2 values."""
    from scipy import stats  # type: ignore[import-untyped]  # a scikit-learn dependency, no stubs

    a = treated.dropna().to_numpy(dtype=float)
    b = control.dropna().to_numpy(dtype=float)
    if len(a) < 2 or len(b) < 2:
        return None
    va, vb = float(a.var(ddof=1)), float(b.var(ddof=1))
    pooled = math.sqrt((va + vb) / 2.0)
    gap = float(a.mean() - b.mean())
    if pooled == 0.0:
        return (0.0, 1.0) if gap == 0.0 else (math.inf, 0.0)
    error = math.sqrt(va / len(a) + vb / len(b))
    z = gap / error if error else 0.0
    return abs(gap) / pooled, float(2.0 * stats.norm.sf(abs(z)))


def _cramers_v(treated: pd.Series, control: pd.Series) -> tuple[float, float] | None:
    """`(Cramer's V, p)` of the chi-square test of group by category; None when there is one category only."""
    import pandas as pd
    from scipy import stats

    both = pd.concat([treated, control], ignore_index=True).astype(str)
    top = both.value_counts().index[:MAX_LEVELS]
    pooled = both.where(both.isin(top), "__other__")
    group = np.r_[np.ones(len(treated), dtype=int), np.zeros(len(control), dtype=int)]
    table = pd.crosstab(group, pooled.to_numpy())
    if table.shape[0] < 2 or table.shape[1] < 2:
        return None
    chi2, p, _dof, _expected = stats.chi2_contingency(table.to_numpy(), correction=False)
    n = float(table.to_numpy().sum())
    return math.sqrt(chi2 / (n * (min(table.shape) - 1))), float(p)


def check_treatment_balance(
    frame: pd.DataFrame,
    treatment_column: str,
    config: UseCaseConfig,
    *,
    control: str | None,
) -> TreatmentBalance:
    """Measure the groups of `treatment_column` against each other on the columns known before the campaign.

    Each treated group is compared with the control group; with no control group (`control` None) nothing is
    compared and the result is not balanced. Rows with no group are left out.
    """
    frame = frame.reset_index(drop=True)  # members are positions, so the index must be unique
    labels = frame[treatment_column].dropna().map(_text)
    counts = labels.value_counts(sort=False)
    order = list(dict.fromkeys(labels.tolist()))
    groups = {name: int(counts[name]) for name in order}
    if control is None or control not in groups or len(groups) < 2:
        return TreatmentBalance(
            treatment_column=treatment_column,
            control_group=control if control in groups else None,
            groups=groups,
            columns_tested=0,
            balanced=False,
            flagged=(),
            largest=None,
        )
    rng = np.random.default_rng(_SEED)
    members = {}
    for name in groups:
        positions = np.flatnonzero((labels == name).to_numpy())
        if len(positions) > MAX_SAMPLE_ROWS:
            positions = np.sort(rng.choice(positions, size=MAX_SAMPLE_ROWS, replace=False))
        members[name] = labels.index[positions]
    tested: list[ColumnBalance] = []
    for column in _description_columns(frame, config, treatment_column):
        series = frame[column]
        numeric = _is_number(series)
        worst: ColumnBalance | None = None
        for name, rows in members.items():
            if name == control:
                continue
            measured = (_smd if numeric else _cramers_v)(series.loc[rows], series.loc[members[control]])
            if measured is None:
                continue
            difference, p_value = measured
            limit = BALANCE_MAX_SMD if numeric else BALANCE_MAX_CRAMERS_V
            found = ColumnBalance(
                column=column,
                measure="smd" if numeric else "cramers_v",
                difference=round(min(difference, 1e6), 4),
                p_value=round(p_value, 6),
                group=name,
                flagged=difference > limit and p_value < BALANCE_ALPHA,
            )
            if worst is None or (found.flagged, found.difference) > (worst.flagged, worst.difference):
                worst = found
        if worst is not None:
            tested.append(worst)
    flagged = tuple(sorted((t for t in tested if t.flagged), key=lambda t: -t.difference))
    largest = max(tested, key=lambda t: t.difference, default=None)
    return TreatmentBalance(
        treatment_column=treatment_column,
        control_group=control,
        groups=groups,
        columns_tested=len(tested),
        balanced=not flagged and bool(tested),
        flagged=flagged,
        largest=largest,
    )


def uplift_scope(frame: pd.DataFrame, treatment_column: str, config: UseCaseConfig) -> UpliftScope:
    """Whether the campaign test in `frame` is in scope for an uplift goal, with the numbers behind the answer."""
    levels = list(dict.fromkeys(frame[treatment_column].dropna().map(_text).tolist()))
    control = control_level(levels, config.uplift.treatment_levels)
    balance = check_treatment_balance(frame, treatment_column, config, control=control)
    rule = out_of_scope("UPLIFT_NOT_RANDOM")
    if balance.control_group is None:
        return UpliftScope(
            status="no_control",
            treatment_column=treatment_column,
            balance=balance,
            message=(
                f"The column {treatment_column} has no control group (customers who got no campaign), "
                f"so there is nothing to compare. {rule}"
            ),
        )
    floor = config.uplift.min_arm_rows
    small = [name for name, count in balance.groups.items() if count < floor]
    if small:
        return UpliftScope(
            status="arm_too_small",
            treatment_column=treatment_column,
            balance=balance,
            message=(
                f"Each group in {treatment_column} needs at least {floor:,} customers; "
                f"the smallest has {min(balance.groups.values()):,}."
            ),
        )
    if not balance.balanced:
        names = ", ".join(c.column for c in balance.flagged[:5]) or "none measured"
        reason = (
            f"The groups in {treatment_column} already differed before the campaign on: {names}."
            if balance.flagged
            else f"No column of the file could be used to check that {treatment_column} was assigned at random."
        )
        return UpliftScope(
            status="not_random",
            treatment_column=treatment_column,
            balance=balance,
            message=f"{reason} {rule}",
        )
    largest = balance.largest
    detail = (
        f" The largest difference is {largest.difference:.2f} on {largest.column}."
        if largest is not None
        else ""
    )
    return UpliftScope(
        status="ok",
        treatment_column=treatment_column,
        balance=balance,
        message=(
            f"The {len(balance.groups)} groups in {treatment_column} look the same on "
            f"{balance.columns_tested} columns known before the campaign, so the assignment looks random.{detail}"
        ),
    )
