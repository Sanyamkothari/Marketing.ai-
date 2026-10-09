"""Audit a campaign another tool ran (Plan J M103, DEC-1313): who was in which group, what happened, and how far to trust it.

The fastest route to "net value proven against a control" is measuring a campaign that already
happened. A prospect's past randomised campaign needs no integration: they upload **who was in which
group** (an assignment file) and **what happened** (an outcomes file), map the columns, and get a
readout. This module is the pure part of that: it turns the two uploaded frames into the campaign's
own files, decides what kind of claim the numbers support, and writes it down. The route
(`POST /campaigns/audit`, `api/routes/campaigns.py`) stores the files and calls
`engine.measurement.measure.measure_campaign` **unchanged** on them; `run_id` of the report carries the
campaign id, and no run record is written.

**The assignment file.** One row per customer: the id, a group column, and optionally the date the
customer was sent to (`sent_date_column`), whether the customer was meant to be contacted at all
(`intended_column`; every customer when absent, so the comparison is intent to treat) and any other
columns, which are the customer details used to test the claim below (they are never stored). The
group column is `0/1` (or yes/no, treated/control and similar words, read by `parse_groups`) or, for
several offers, the offer each treated customer got with a named control value. A customer with no
group is in neither (counted, never guessed). It is written as `assignment.parquet` in the shape of a
scored campaign's, so every reader of a campaign reads it unchanged.

**What the numbers can claim (the causal labels).** A difference between contacted and held-back
customers is the campaign's effect only if the groups were chosen at random. The label follows the
plan exactly:

* **Causal** (`verified_random`): the engine verified the assignment is random - it tried to predict who
  was contacted from the customer details in the file (`engine.uplift.checks.treatment_predictability`,
  the check an uplift run applies to its own training file) and could not beat the same threshold
  (`uplift.randomness_auc_max`, 0.60). `causal` is true. (A group the engine itself drew is `engine_random`,
  the scored campaign's basis; an audit never has one.)
* **Random by your statement, not verified** (`declared_random`): the person said the groups were chosen
  at random, but the file has no customer details to test it with, or too few customers. `causal` is
  false: the numbers are shown with this label beside them, never as a proven effect.
* **Descriptive only** (`not_random`): the person said the groups were not random, or said they were and the
  check says they were not. The report is stored with `causal` false and a sentence that describes how
  the groups differ and says it does not show what the campaign changed; no verdict "the campaign added N"
  is ever drawn from it.

**Amounts and the adjusted estimate.** The outcome may be an amount (`outcome_kind="continuous"`). An
audit never uses the adjusted estimate (CUPED): it must be named in a test plan registered before the
outcomes are read, and a campaign audited after the fact has none (`measure_campaign` refuses an
adjustment with no plan).

`pandas` and `numpy` are imported inside the function bodies, never at module level, so `import
engine` stays fast.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, Literal

from pydantic import AwareDatetime, Field

from engine.config import PrimaryKey, StrictBase, key_columns
from engine.contracts import Artefact

if TYPE_CHECKING:
    import pandas as pd

    from engine.uplift.contracts import IncrementalityReport
    from engine.uplift.measure import CampaignVerdict

__all__ = [
    "AUDIT_ARM_UNREADABLE",
    "AUDIT_CODES",
    "AUDIT_FILENAME",
    "AUDIT_SEED",
    "CAUSAL_LABEL",
    "DECLARED_LABEL",
    "DESCRIPTIVE_LABEL",
    "AssignmentBasis",
    "AuditInputError",
    "AuditReadout",
    "BuiltAssignment",
    "CausalBasisKind",
    "GroupSplit",
    "RandomnessCheck",
    "audit_verdict",
    "build_assignment_frame",
    "build_outcomes_frame",
    "check_randomness",
    "decide_basis",
    "declared_summary",
    "descriptive_summary",
    "earliest_date",
    "encode_features",
    "label_report",
    "parse_groups",
    "rename_keys",
]

AUDIT_ARM_UNREADABLE: Final[str] = "AUDIT_ARM_UNREADABLE"
"""422 of `POST /campaigns/audit`: the group column cannot be read as contacted against held back."""

AUDIT_CODES: Final[frozenset[str]] = frozenset({AUDIT_ARM_UNREADABLE})
"""M103's audit code, joined into `engine.decide.codes.PLAN_J_CODES` at integration (one definition)."""

AUDIT_FILENAME: Final[str] = "audit.json"
"""`campaigns/<id>/audit.json`: the audit's label, check and counts. Aggregate: no customer id, no value."""
AUDIT_SEED: Final[int] = 103
"""The seed of the randomness check, so the same file always gets the same answer."""

CAUSAL_LABEL: Final[str] = "Causal"
DECLARED_LABEL: Final[str] = "Random by your statement, not verified"
DESCRIPTIVE_LABEL: Final[str] = "Descriptive only"

AssignmentBasis = Literal["random", "not_random"]
"""What the person says about how the groups were chosen."""
CausalBasisKind = Literal["verified_random", "declared_random", "not_random"]
"""What the engine can stand behind (`Campaign.causal_basis`'s values an audit can produce)."""

_ARM_TREATED: Final[str] = "treated"
_ARM_HOLDOUT: Final[str] = "holdout"
_ARM_SUPPRESSED: Final[str] = "suppressed"
_TREATED_TOKENS: Final[frozenset[str]] = frozenset(
    {"1", "true", "yes", "y", "t", "treated", "treatment", "test", "contacted", "sent", "exposed"}
)
_CONTROL_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "0",
        "false",
        "no",
        "n",
        "c",
        "control",
        "holdout",
        "hold-out",
        "held back",
        "untreated",
        "none",
        "not sent",
    }
)
_MAX_LEVELS: Final[int] = 100
"""A text column with more different values than this is not used to test the claim (it is close to an id)."""
_SENT_COLUMN: Final[str] = "sent_date"


class AuditInputError(ValueError):
    """A file the audit cannot read, in a plain sentence with counts and column names, never values.

    `code` is the route's error code (`CAMPAIGN_INVALID` by default) and `path` the request field.
    """

    def __init__(self, message: str, *, code: str = "CAMPAIGN_INVALID", path: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.path = path


# ---------------------------------------------------------------------------
# The records
# ---------------------------------------------------------------------------
class RandomnessCheck(StrictBase):
    """Whether the customer details in the assignment file can tell the contacted from the held-back customers."""

    status: Literal["passed", "failed", "not_run"] = Field(
        description="`passed`: could not do better than chance; `failed`: could; `not_run`: nothing to test with."
    )
    auc: float | None = Field(
        default=None,
        description="The guessing score, 0.5 being chance (the worst offer's, with several); null when not run.",
    )
    threshold: float = Field(description="The score above which the groups are taken not to be random.")
    rows_used: int | None = Field(
        default=None, description="Customers the test used (a sample of large files)."
    )
    signals: tuple[str, ...] = Field(
        default=(),
        description="The columns that gave the groups away, strongest first; empty when it passed.",
    )
    columns_used: int = Field(default=0, description="Columns of customer details the test could use.")
    reason: str | None = Field(
        default=None, description="Why it was not run, in a sentence; null when it was."
    )


class AuditReadout(Artefact):
    """`campaigns/<id>/audit.json`: how an outside campaign was read, and what its numbers can claim."""

    campaign_id: str = Field(description="The external campaign the audit created.")
    stated_basis: AssignmentBasis = Field(
        description="What the person said about how the groups were chosen."
    )
    causal_basis: CausalBasisKind = Field(description="What the engine can stand behind.")
    causal: bool = Field(description="True only when the engine verified the assignment random.")
    label: str = Field(
        description="`Causal`, `Random by your statement, not verified` or `Descriptive only`."
    )
    explanation: str = Field(description="Why the label was given, in plain sentences.")
    randomness: RandomnessCheck = Field(description="The test of the claim that the groups were random.")
    assignment_upload_id: str = Field(description="The upload the groups were read from.")
    assignment_file_name: str = Field(description="Its name, as uploaded.")
    assignment_rows: int = Field(description="Customers in the assignment file.")
    rows_without_group: int = Field(description="Customers with no group, left in neither.")
    offers: tuple[str, ...] | None = Field(
        default=None, description="The offers measured against the shared control; null with one treatment."
    )
    control_level: str | None = Field(default=None, description="The control value, with several offers.")
    outcome_kind: Literal["binary", "continuous"] = Field(description="A yes/no outcome or an amount.")
    outcome_is_good: bool = Field(description="False when the campaign aimed to make the outcome rarer.")
    sent_dates: Literal["assignment", "outcomes", "entered"] = Field(
        description="Where the date of contact came from: a column of the assignment file, of the outcomes file, or one entered."
    )
    synthetic: bool = Field(
        default=False, description="True when either upload was marked as generated data."
    )
    notes: tuple[str, ...] = Field(default=(), description="Plain sentences worth reading beside the result.")
    audited_at: AwareDatetime = Field(description="When the audit was run.")


@dataclass(frozen=True)
class GroupSplit:
    """The group column read: each row's group, and the offers when there are several."""

    arm: pd.Series[Any]
    offer: pd.Series[Any] | None
    offers: tuple[str, ...]
    control_level: str | None
    blank: int


@dataclass(frozen=True)
class BuiltAssignment:
    """An assignment file read: `assignment.parquet`'s frame, the dates sent, the details and the counts."""

    assignment: pd.DataFrame
    sent: pd.Series[Any] | None
    features: pd.DataFrame
    offers: tuple[str, ...]
    control_level: str | None
    rows_without_group: int


# ---------------------------------------------------------------------------
# Reading the groups
# ---------------------------------------------------------------------------
def _text(values: pd.Series[Any]) -> pd.Series[Any]:
    """A column as trimmed text; a whole number is written without its `.0`; a blank or null is NA."""
    import pandas as pd

    text = values.astype("string").str.strip()
    if not pd.api.types.is_bool_dtype(values.dtype) and not pd.api.types.is_string_dtype(values.dtype):
        numeric = pd.to_numeric(values, errors="coerce").astype("float64")
        integral = numeric.notna() & (numeric == numeric.round()) & (numeric.abs() < 2**53)
        text = text.where(~integral, numeric.round().astype("Int64").astype("string"))
    return text.where(text.notna() & (text != ""), other=pd.NA)


def parse_groups(
    values: pd.Series[Any],
    *,
    control_value: str | None = None,
    treated_values: tuple[str, ...] | None = None,
) -> GroupSplit:
    """Read a group column as contacted, held back, or neither.

    With `control_value`, customers with that value are held back; with `treated_values` the customers
    with one of those values are contacted (each value an offer when there are several), otherwise
    every other value is. With neither, the column must hold only words and numbers that mean
    contacted (`1`, `yes`, `treated`, ...) and held back (`0`, `no`, `control`, ...). A blank is no
    group. Raises `AuditInputError` (`AUDIT_ARM_UNREADABLE`) when the values cannot be told apart, naming
    them as counts, and when no customer is contacted.
    """
    import pandas as pd

    text = _text(values)
    lowered = text.str.lower()
    present = text.notna().to_numpy(dtype=bool)
    # One level per spelling-insensitive value: `Yes` and `yes` are the same group (the first spelling stands).
    spelled: dict[str, str] = {}
    for value in sorted({str(v) for v in text[present].unique()}):
        spelled.setdefault(value.lower(), value)
    levels = sorted(spelled.values(), key=str.lower)
    if not levels:
        raise AuditInputError(
            "The group column is empty, so no customer can be told apart as contacted or held back.",
            code=AUDIT_ARM_UNREADABLE,
            path="assignment.arm_column",
        )
    if control_value is not None:
        control = {str(control_value).strip().lower()}
        treated_set = (
            {str(v).strip().lower() for v in treated_values}
            if treated_values
            else {level.lower() for level in levels} - control
        )
    else:
        lowered_levels = {level.lower() for level in levels}
        if not lowered_levels <= (_TREATED_TOKENS | _CONTROL_TOKENS):
            raise AuditInputError(
                f"The group column holds {len(levels)} different values that cannot be told apart as contacted "
                f"and held back. Say which value means held back (control_value), and which mean contacted if "
                f"there is more than one offer.",
                code=AUDIT_ARM_UNREADABLE,
                path="assignment.arm_column",
            )
        control = lowered_levels & _CONTROL_TOKENS
        treated_set = lowered_levels & _TREATED_TOKENS
    stray = {level.lower() for level in levels} - control - treated_set
    if stray:
        raise AuditInputError(
            f"{len(stray)} value(s) of the group column are neither a held-back value nor a contacted one. "
            f"List every contacted value (treated_values), or leave the others out of the file.",
            code=AUDIT_ARM_UNREADABLE,
            path="assignment.arm_column",
        )
    is_control = lowered.isin(control).fillna(value=False).to_numpy(dtype=bool)
    is_treated = lowered.isin(treated_set).fillna(value=False).to_numpy(dtype=bool)
    if not is_treated.any():
        raise AuditInputError(
            "No customer in the group column is marked as contacted, so there is nothing to measure.",
            code=AUDIT_ARM_UNREADABLE,
            path="assignment.arm_column",
        )
    arm = pd.Series(_ARM_SUPPRESSED, index=text.index, dtype="string")
    arm[is_treated] = _ARM_TREATED
    arm[is_control] = _ARM_HOLDOUT
    blank = int((~present).sum())
    treated_levels = [level for level in levels if level.lower() in treated_set]
    if treated_values:
        order = {str(v).strip().lower(): position for position, v in enumerate(treated_values)}
        treated_levels = sorted(treated_levels, key=lambda level: order.get(level.lower(), len(order)))
    offers: tuple[str, ...] = tuple(treated_levels) if len(treated_levels) > 1 else ()
    offer: pd.Series[Any] | None = None
    control_level: str | None = None
    if offers:
        offer = lowered.map(spelled).astype("string").where(is_treated, other=pd.NA)
        control_level = next((level for level in levels if level.lower() in control), None)
        if control_level is None:
            control_level = str(control_value).strip() if control_value is not None else "control"
    return GroupSplit(arm=arm, offer=offer, offers=offers, control_level=control_level, blank=blank)


# ---------------------------------------------------------------------------
# The assignment and outcomes files
# ---------------------------------------------------------------------------
def rename_keys(
    frame: pd.DataFrame, file_keys: tuple[str, ...] | None, primary_key: PrimaryKey, what: str
) -> pd.DataFrame:
    """`frame` with its id column(s) named as the campaign's primary key is (`file_keys` names them in the file)."""
    wanted = key_columns(primary_key)
    given = file_keys if file_keys else wanted
    if len(given) != len(wanted):
        raise AuditInputError(
            f"The {what} file names {len(given)} id column(s) but the campaign's customer id has {len(wanted)}.",
            path=f"{what}.key_columns",
        )
    missing = [name for name in given if name not in frame.columns]
    if missing:
        raise AuditInputError(
            f"The {what} file has no column {', '.join(repr(name) for name in missing)}.",
            path=f"{what}.key_columns",
        )
    if tuple(given) == tuple(wanted):
        return frame
    clash = [name for name in wanted if name in frame.columns and name not in given]
    if clash:
        raise AuditInputError(
            f"The {what} file already has a column {', '.join(repr(n) for n in clash)} that is not its id column.",
            path=f"{what}.key_columns",
        )
    return frame.rename(columns=dict(zip(given, wanted, strict=True)))


def _key_columns_as_text(frame: pd.DataFrame, columns: tuple[str, ...], *, what: str) -> pd.DataFrame:
    """The id column(s) as trimmed text, so `1` in one file is `1` in the other; refuses blank or repeated ids."""
    from engine.keys import key_text
    from engine.uplift.incrementality import _joined_keys

    out = frame.copy()
    for name in columns:
        out[name] = key_text(frame[name]).str.strip().to_numpy()
    joined = _joined_keys(out, columns)
    blank = int((joined.str.replace("|", "", regex=False) == "").sum())
    if blank:
        raise AuditInputError(f"The {what} file has {blank:,} row(s) with no customer id.", path=f"{what}")
    repeated = int(joined.duplicated().sum())
    if repeated:
        raise AuditInputError(
            f"The {what} file lists {repeated:,} customer(s) more than once; each customer must appear once, or "
            f"their outcome would be counted twice.",
            path=f"{what}",
        )
    return out.reset_index(drop=True)


def build_assignment_frame(
    frame: pd.DataFrame,
    *,
    primary_key: PrimaryKey,
    arm_column: str,
    file_keys: tuple[str, ...] | None = None,
    control_value: str | None = None,
    treated_values: tuple[str, ...] | None = None,
    sent_date_column: str | None = None,
    intended_column: str | None = None,
) -> BuiltAssignment:
    """An uploaded assignment file as the campaign's `assignment.parquet`, with the customer details beside it.

    The returned `assignment` has the id column(s) as text, `arm` (`treated`, `holdout` or `suppressed`
    for a customer with no group), `intended`, `band` (empty) and, with several offers, `offer`. Raises
    `AuditInputError` for a missing column, a customer without an id or listed twice, or a group column
    that cannot be read.
    """
    import pandas as pd

    from engine.uplift.incrementality import _flag

    columns = key_columns(primary_key)
    frame = rename_keys(frame, file_keys, primary_key, "assignment")
    named = [arm_column, *(c for c in (sent_date_column, intended_column) if c is not None)]
    missing = [name for name in named if name not in frame.columns]
    if missing:
        raise AuditInputError(
            f"The assignment file has no column {', '.join(repr(name) for name in missing)}.",
            path="assignment",
        )
    frame = _key_columns_as_text(frame, columns, what="assignment")
    split = parse_groups(frame[arm_column], control_value=control_value, treated_values=treated_values)
    arm = split.arm
    if intended_column is not None:
        intended = _flag(frame[intended_column]).to_numpy(dtype=bool)
    else:
        intended = pd.Series(True, index=frame.index).to_numpy(dtype=bool)
    suppressed = (arm == _ARM_SUPPRESSED).to_numpy(dtype=bool)
    out = frame[list(columns)].copy()
    out["arm"] = arm.to_numpy()
    out["arm"] = out["arm"].astype("string")
    out["intended"] = pd.Series(intended & ~suppressed, index=frame.index, dtype=bool)
    out["band"] = pd.Series(pd.NA, index=frame.index, dtype="string")
    if split.offer is not None:
        out["offer"] = split.offer.astype("string")
    used = {*columns, arm_column, *(c for c in (sent_date_column, intended_column) if c is not None)}
    features = frame[[name for name in frame.columns if name not in used]].copy()
    sent = frame[sent_date_column].astype("string") if sent_date_column is not None else None
    return BuiltAssignment(
        assignment=out,
        sent=sent,
        features=features,
        offers=split.offers,
        control_level=split.control_level,
        rows_without_group=split.blank,
    )


def build_outcomes_frame(
    frame: pd.DataFrame,
    *,
    primary_key: PrimaryKey,
    outcome_column: str,
    file_keys: tuple[str, ...] | None = None,
    treatment_date_column: str | None = None,
    assignment: BuiltAssignment | None = None,
    also: Sequence[str] = (),
) -> tuple[pd.DataFrame, str | None]:
    """`outcomes.parquet`'s frame and the name of its treatment-date column.

    The id column(s), the outcome and, when the dates of contact are given, the date column. The dates
    are the outcomes file's own `treatment_date_column`, or else the assignment file's `sent_date_column`
    joined on the id (a customer with no date there gets none, and is counted as having no usable
    date). Giving the date in both files is refused: which one counts would be a guess.
    """
    import pandas as pd

    from engine.uplift.incrementality import _joined_keys

    columns = key_columns(primary_key)
    frame = rename_keys(frame, file_keys, primary_key, "outcomes")
    wanted = [outcome_column, *(c for c in (treatment_date_column,) if c is not None), *also]
    missing = [name for name in wanted if name not in frame.columns]
    if missing:
        raise AuditInputError(
            f"The outcomes file has no column {', '.join(repr(name) for name in missing)}.", path="outcomes"
        )
    sent = assignment.sent if assignment is not None else None
    if treatment_date_column is not None and sent is not None:
        raise AuditInputError(
            "The date of contact is given in both the assignment file and the outcomes file. Give it in one.",
            path="outcomes.treatment_date_column",
        )
    frame = _key_columns_as_text(frame, columns, what="outcomes")
    kept = frame[[*columns, outcome_column]].copy()
    date_name: str | None = treatment_date_column
    if treatment_date_column is not None:
        kept[treatment_date_column] = frame[treatment_date_column].to_numpy()
    elif sent is not None and assignment is not None:
        date_name = _SENT_COLUMN if outcome_column != _SENT_COLUMN else "sent_on"
        by_key = pd.Series(
            sent.to_numpy(dtype=object),
            index=pd.Index(_joined_keys(assignment.assignment, columns).to_numpy()),
        )
        kept[date_name] = by_key.reindex(_joined_keys(kept, columns).to_numpy()).to_numpy(dtype=object)
    for name in dict.fromkeys(also):
        kept[name] = frame[name].to_numpy()
    return kept, date_name


def earliest_date(values: pd.Series[Any]) -> datetime | None:
    """The earliest date `values` holds, as a UTC datetime, or None when none can be read (never guessed)."""
    from engine.uplift.incrementality import _parse_dates

    parsed = _parse_dates(values.reset_index(drop=True)).dropna()
    if parsed.empty:
        return None
    moment: datetime = parsed.min().to_pydatetime()
    return moment


# ---------------------------------------------------------------------------
# Testing the claim that the groups were random
# ---------------------------------------------------------------------------
def encode_features(frame: pd.DataFrame) -> pd.DataFrame:
    """Customer details as columns the randomness test can read: numbers, and text as categories.

    A yes/no becomes 0/1, a date a day count. A column that is empty, constant, an id (a different text in
    every row) or text with more than 100 different values is left out: it can tell nothing, or would
    memorise the rows.
    """
    import numpy as np
    import pandas as pd

    out: dict[str, pd.Series[Any]] = {}
    for name in frame.columns:
        column = frame[name]
        if column.notna().sum() == 0 or column.nunique(dropna=True) <= 1:
            continue
        if pd.api.types.is_bool_dtype(column.dtype):
            out[str(name)] = column.astype("float64")
        elif pd.api.types.is_datetime64_any_dtype(column.dtype):
            days = column.dt.tz_localize(None) if getattr(column.dt, "tz", None) is not None else column
            out[str(name)] = (days - pd.Timestamp("1970-01-01")).dt.total_seconds() / 86400.0
        elif pd.api.types.is_numeric_dtype(column.dtype):
            numeric = column.astype("float64")
            out[str(name)] = numeric.where(np.isfinite(numeric))
        else:
            levels = int(column.nunique(dropna=True))
            if levels > _MAX_LEVELS or levels >= int(column.notna().sum()):
                continue
            out[str(name)] = column.astype("string").astype("category")
    return pd.DataFrame(out, index=frame.index)


def check_randomness(
    built: BuiltAssignment,
    *,
    primary_key: PrimaryKey,
    threshold: float,
    seed: int = AUDIT_SEED,
) -> RandomnessCheck:
    """Try to tell contacted from held-back customers using the customer details in the file.

    Run inside the population the campaign is measured on (`intended`), once per offer against the
    shared control when there are several; the result is the worst (highest) score. `not_run` with a
    plain reason when there are no usable details, or either group has fewer than 30 customers.
    """
    import numpy as np

    from engine.keys import entity_column, is_composite, key_text
    from engine.uplift.checks import RANDOMNESS_MIN_ARM_ROWS, treatment_predictability

    assignment = built.assignment
    features = encode_features(built.features)
    if features.shape[1] == 0:
        return RandomnessCheck(
            status="not_run",
            threshold=threshold,
            reason=(
                "The assignment file has no customer details besides the id and the group (age, region, past "
                "purchases and so on), so nothing could be used to test whether the groups were chosen at random."
            ),
        )
    arm = assignment["arm"].astype("string")
    population = assignment["intended"].to_numpy(dtype=bool) & (arm != _ARM_SUPPRESSED).to_numpy(dtype=bool)
    treated = (arm == _ARM_TREATED).to_numpy(dtype=bool)
    held = (arm == _ARM_HOLDOUT).to_numpy(dtype=bool)
    groups = None
    if is_composite(primary_key):
        groups = key_text(assignment[entity_column(primary_key)]).to_numpy(dtype=object)
    if built.offers:
        offer = assignment["offer"].astype("string")
        contrasts = [
            population & (held | (offer == level).fillna(value=False).to_numpy(dtype=bool))
            for level in built.offers
        ]
        flags = [
            treated & (offer == level).fillna(value=False).to_numpy(dtype=bool) for level in built.offers
        ]
    else:
        contrasts = [population & (held | treated)]
        flags = [treated]
    worst: float | None = None
    signals: tuple[str, ...] = ()
    rows_used = 0
    for mask, flag in zip(contrasts, flags, strict=True):
        if min(int((flag & mask).sum()), int((~flag & mask).sum())) < RANDOMNESS_MIN_ARM_ROWS:
            return RandomnessCheck(
                status="not_run",
                threshold=threshold,
                columns_used=int(features.shape[1]),
                reason=(
                    f"Each group needs at least {RANDOMNESS_MIN_ARM_ROWS} customers for the test, and this file "
                    f"does not have that many contacted and held back."
                ),
            )
        measured = treatment_predictability(
            features.loc[mask].reset_index(drop=True),
            flag[mask].astype(np.int_),
            seed=seed,
            groups=None if groups is None else groups[mask],
        )
        if measured is None:  # pragma: no cover - guarded by the counts above
            continue
        auc, found, used = measured
        rows_used = max(rows_used, used)
        if worst is None or auc > worst:
            worst, signals = auc, found
    if worst is None:  # pragma: no cover - the loop returns or measures
        return RandomnessCheck(status="not_run", threshold=threshold, reason="The test could not be run.")
    passed = worst <= threshold
    return RandomnessCheck(
        status="passed" if passed else "failed",
        auc=round(worst, 4),
        threshold=threshold,
        rows_used=rows_used,
        signals=() if passed else signals,
        columns_used=int(features.shape[1]),
    )


def decide_basis(stated: AssignmentBasis, check: RandomnessCheck) -> tuple[CausalBasisKind, bool, str, str]:
    """`(basis, causal, label, explanation)`: what the numbers of an audited campaign can claim.

    Causal only when the engine verified the assignment random; otherwise the label says why not.
    """
    if stated == "not_random":
        return (
            "not_random",
            False,
            DESCRIPTIVE_LABEL,
            "You said the customers were not chosen at random. The numbers describe how the contacted and "
            "the other customers differed. They do not show what the campaign changed, because the customers "
            "who were contacted may have differed from the others before it started.",
        )
    if check.status == "failed" and check.auc is not None:
        named = f" The details that gave it away were {_join(check.signals)}." if check.signals else ""
        return (
            "not_random",
            False,
            DESCRIPTIVE_LABEL,
            f"You said the customers were chosen at random, but the details in your file predict who was "
            f"contacted (a guessing score of {check.auc:.2f}, where 0.50 is chance and {check.threshold:.2f} is "
            f"our limit).{named} The groups were not chosen at random, so the numbers describe how they "
            f"differed. They do not show what the campaign changed.",
        )
    if check.status == "passed" and check.auc is not None:
        return (
            "verified_random",
            True,
            CAUSAL_LABEL,
            f"We tried to tell contacted customers from held-back ones using the details in your file and could "
            f"not do better than a guessing score of {check.auc:.2f} (0.50 is chance; our limit is "
            f"{check.threshold:.2f}), so the two groups look randomly chosen. The difference between them can be "
            f"read as what the campaign changed.",
        )
    return (
        "declared_random",
        False,
        DECLARED_LABEL,
        f"You said the customers were chosen at random, and we could not check it. {check.reason or ''} "
        f"The numbers are shown as you described the campaign; they are not a proven effect.".replace(
            "  ", " "
        ),
    )


def _join(items: tuple[str, ...]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


# ---------------------------------------------------------------------------
# Labelling the stored report
# ---------------------------------------------------------------------------
def descriptive_summary(report: IncrementalityReport) -> str:
    """A sentence for a report that is descriptive only: the two groups' rates or averages, and no claim of cause."""
    tail = (
        " The groups were not chosen at random, so this describes how they differ. It does not show what the "
        "campaign changed."
    )
    if report.treated_mean is not None and report.control_mean is not None:
        return (
            f"Contacted customers averaged {report.treated_mean:,.2f} and the others {report.control_mean:,.2f}."
            + tail
        )
    if report.treated_rate is not None and report.control_rate is not None:
        return (
            f"{report.treated_rate:.1%} of contacted customers had the outcome against {report.control_rate:.1%} "
            f"of the others." + tail
        )
    return report.summary


def declared_summary(report: IncrementalityReport) -> str:
    """The sentence for a report whose groups are random only by the person's statement.

    It states the two groups' numbers under the person's own condition ("if the groups were chosen at
    random as you said") and never says the campaign added, prevented or caused anything: the engine has not
    checked how the groups were chosen. A report without a comparison keeps its own sentence.
    """
    lead = f"{DECLARED_LABEL}. If the groups were chosen at random as you said, "
    tail = " We could not check how the groups were chosen, so this is not shown as a proven effect."
    estimate = report.adjusted_interval if report.adjusted_interval is not None else report.mean_difference_ci
    if (
        report.outcome_kind == "continuous"
        and report.treated_mean is not None
        and report.control_mean is not None
        and estimate is not None
        and estimate.ci_low is not None
        and estimate.ci_high is not None
    ):
        what = (
            f"contacted customers averaged {report.treated_mean:,.2f} and the others {report.control_mean:,.2f}, "
            f"a difference of {estimate.value:+,.2f} per customer (95% interval {estimate.ci_low:+,.2f} to "
            f"{estimate.ci_high:+,.2f})"
        )
        chance = "" if estimate.excludes_zero else ", which could be chance"
        return f"{lead}{what}{chance}.{tail}"
    lift = report.absolute_lift
    if (
        lift is not None
        and lift.ci_low is not None
        and lift.ci_high is not None
        and report.treated_rate is not None
        and report.control_rate is not None
    ):
        from engine.uplift.incrementality import _points

        what = (
            f"{report.treated_rate:.1%} of contacted customers had the outcome against {report.control_rate:.1%} "
            f"of the others, a difference of {_points(lift.value)} (95% interval {_points(lift.ci_low)} to "
            f"{_points(lift.ci_high)})"
        )
        incremental = report.incremental_conversions
        if lift.excludes_zero and incremental is not None:
            direction = "more" if incremental.value > 0 else "fewer"
            what += (
                f", about {abs(incremental.value):,.0f} {direction} conversions among the contacted customers"
            )
        else:
            what += ", which could be chance"
        return f"{lead}{what}.{tail}"
    return f"{DECLARED_LABEL}. {report.summary}"


def label_report(report: IncrementalityReport, basis: CausalBasisKind) -> IncrementalityReport:
    """The report to store: exactly `measure_campaign`'s when the groups are verified random, else labelled.

    `causal` follows the claim the numbers support, so every later reader (the value view, the Proof
    Pack) sees it false for a campaign that is only described. A claim from the person's statement alone
    gets `declared_summary` (the numbers, under the person's condition, and no word of cause); a
    descriptive one gets `descriptive_summary`.
    """
    if basis == "verified_random":
        return report
    if report.early_look:  # an early look already states the counts and no conclusion
        return report.model_copy(update={"causal": False})
    if basis == "declared_random":
        return report.model_copy(update={"causal": False, "summary": declared_summary(report)})
    return report.model_copy(update={"causal": False, "summary": descriptive_summary(report)})


def audit_verdict(
    report: IncrementalityReport,
    readout: AuditReadout,
    *,
    outcome_label: str | None = None,
) -> CampaignVerdict | None:
    """The plain verdict of an audited campaign, or none when it must not be drawn.

    None for an early look and for a descriptive-only campaign (no "the campaign added N" from groups that
    were not random). For a campaign random by the person's statement the usual verdict is drawn, but its
    headline is conditional and carries the label ("Random by your statement, not verified: about N more
    conversions among contacted customers"), never "the campaign added" or "caused": the engine has not
    verified the randomness.
    """
    from engine.measurement.measure import campaign_verdict_for
    from engine.uplift.measure import VerdictKind

    if report.early_look or readout.causal_basis == "not_random":
        return None
    standing = report if report.causal else report.model_copy(update={"causal": True})
    verdict = campaign_verdict_for(
        standing, outcome_is_good=readout.outcome_is_good, outcome_label=outcome_label
    )
    if verdict is None or readout.causal:
        return verdict
    update: dict[str, object] = {"detail": f"{DECLARED_LABEL}. {verdict.detail}"}
    if verdict.amount is not None and verdict.kind in (
        VerdictKind.ADDED,
        VerdictKind.PREVENTED,
        VerdictKind.HARMED,
    ):
        more = verdict.kind is VerdictKind.ADDED or (
            verdict.kind is VerdictKind.HARMED and not readout.outcome_is_good
        )
        if report.outcome_kind == "continuous":
            what = outcome_label or report.outcome_column
        else:
            word = "conversion" if readout.outcome_is_good else "case"
            what = word if verdict.amount == 1 else f"{word}s"
        update["headline"] = (
            f"{DECLARED_LABEL}: about {verdict.amount:,} {'more' if more else 'fewer'} {what} among "
            f"contacted customers"
        )
    return verdict.model_copy(update=update)
